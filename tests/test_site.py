"""Level 1-2 基地套疊：SiteSpec / CRS 正規化 / overlay / crs_check / area_check 單元測試。

所有圖資皆為合成資料（tools/make_fixtures_site.py），每個測試在 tmp_path 重新產生。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
from pydantic import ValidationError
from shapely.geometry import Point, box

from draftagent.schema.site import SiteSpec
from draftagent.site.crs import CRSMissingError, normalize_crs, read_vector
from draftagent.site.overlay import OverlayError, normalize_land_number, run_overlay
from draftagent.verify.area_check import check_area
from draftagent.verify.crs_check import check_crs

REPO = Path(__file__).resolve().parents[1]
CASE = REPO / "tests/level1/1-2"
EXPECTED = json.loads((CASE / "expected/expected.json").read_text(encoding="utf-8"))


def _load_fixture_module():
    spec = importlib.util.spec_from_file_location("make_fixtures_site", REPO / "tools/make_fixtures_site.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fixtures_mod = _load_fixture_module()


@pytest.fixture
def fx(tmp_path):
    return fixtures_mod.make_fixtures(tmp_path / "input")


def _spec(fx, tmp_path, **kw) -> SiteSpec:
    data = json.loads((CASE / "input/spec.json").read_text(encoding="utf-8"))
    data.update(
        cadastre_path=str(fx["cadastre"]),
        zoning_path=str(fx["zoning"]),
        output_path=str(tmp_path / "out/site.gpkg"),
    )
    data.update(kw)
    return SiteSpec.model_validate(data)


@pytest.fixture
def overlay_out(fx, tmp_path):
    spec = _spec(fx, tmp_path)
    return spec, run_overlay(spec)


# ---------- schema ----------

def test_committed_spec_parses():
    spec = SiteSpec.from_json(CASE / "input/spec.json")
    assert spec.target_crs == "EPSG:3826"
    assert spec.parcels[0].land_numbers == ["12", "12-1", "13"]


def test_spec_forbids_extra_and_requires_section(fx, tmp_path):
    with pytest.raises(ValidationError):
        _spec(fx, tmp_path, bonus_far=1.2)
    with pytest.raises(ValidationError):
        _spec(fx, tmp_path, parcels=[{"land_numbers": ["12"]}])
    with pytest.raises(ValidationError):
        _spec(fx, tmp_path, output_path="x.shp")


def test_analytic_expected_is_committed():
    assert fixtures_mod.analytic_expected() == EXPECTED


# ---------- 正常流程 ----------

def test_overlay_areas_match_analytic(overlay_out):
    _, summary = overlay_out
    assert summary["total_area_m2"] == pytest.approx(EXPECTED["total_m2"], rel=1e-6)
    zones = {z["分區"]: z["area_m2"] for z in summary["zones"]}
    for zone, area in EXPECTED["zones_m2"].items():
        assert zones[zone] == pytest.approx(area, abs=0.01)
    for z in summary["zones"]:
        assert z["area_ping"] == pytest.approx(z["area_m2"] * 0.3025, abs=0.01)
    assert any("4326" in n for n in summary["crs_notes"])  # 分區有被轉換
    assert {p["地號"] for p in summary["parcels"]} == {"12", "12-1", "13"}  # 14 沒被選到

    gpkg = Path(summary["output_gpkg"])
    layers = gpd.list_layers(gpkg)["name"].tolist()
    assert set(layers) == {"parcels", "parcel_zoning"}
    assert Path(summary["area_table_csv"]).read_text(encoding="utf-8-sig").startswith("分區,面積_m2")


def test_verifiers_green_on_output(overlay_out):
    spec, summary = overlay_out
    crs = check_crs(summary["output_gpkg"])
    assert crs.ok, crs.render()
    area = check_area(summary["output_gpkg"], spec.expected_area_m2)
    assert area.ok, area.render()
    assert area.metrics["zones_m2"]["住宅區"] == pytest.approx(EXPECTED["zones_m2"]["住宅區"], abs=0.01)


def test_twd67_cadastre_is_converted_and_noted(fx, tmp_path):
    spec = _spec(fx, tmp_path, cadastre_path=str(fx["cadastre_twd67"]))
    summary = run_overlay(spec)
    assert any("TWD67" in n for n in summary["crs_notes"])
    # 轉回 3826 後與原始地籍差在 pyproj 參數精度內（面積幾乎不變）
    assert summary["total_area_m2"] == pytest.approx(EXPECTED["total_m2"], rel=1e-3)


def test_land_number_normalization():
    assert normalize_land_number("12") == normalize_land_number("0012-0000") == "00120000"
    assert normalize_land_number("12-1") == normalize_land_number("00120001") == "00120001"
    with pytest.raises(OverlayError):
        normalize_land_number("十二")


def test_missing_land_number_raises(fx, tmp_path):
    spec = _spec(fx, tmp_path, parcels=[{"section_code": "1", "land_numbers": ["99"]}])
    with pytest.raises(OverlayError, match="99"):
        run_overlay(spec)


# ---------- 故意錯的 fixture 必須被抓 ----------

def test_crs_check_red_on_twd67(fx):
    res = check_crs(fx["cadastre_twd67"])
    assert not res.ok
    assert any("TWD67" in e for e in res.errors)


def test_crs_check_red_on_4326_zoning_with_suggestion(fx):
    res = check_crs(fx["zoning"])
    assert not res.ok
    assert any("4326" in e and "3826" in e for e in res.errors)


def test_area_check_red_when_expected_off_by_1pct(overlay_out):
    _, summary = overlay_out
    res = check_area(summary["output_gpkg"], EXPECTED["total_m2"] * 1.01)
    assert not res.ok
    # 0.3% 在門檻內仍綠燈
    assert check_area(summary["output_gpkg"], EXPECTED["total_m2"] * 1.003).ok


def test_area_check_refuses_loosened_tolerance(overlay_out):
    _, summary = overlay_out
    res = check_area(summary["output_gpkg"], EXPECTED["total_m2"] * 1.01, tolerance=0.05)
    assert not res.ok


def test_area_check_red_when_zoning_sum_mismatch(overlay_out, tmp_path):
    _, summary = overlay_out
    gpkg = Path(summary["output_gpkg"])
    pz = gpd.read_file(gpkg, layer="parcel_zoning")
    pz.iloc[:-1].to_file(gpkg, layer="parcel_zoning", driver="GPKG", mode="w")  # 少一塊
    res = check_area(gpkg, EXPECTED["total_m2"])
    assert not res.ok
    assert any("分區加總" in e for e in res.errors)


def test_area_check_red_on_invalid_geometry(tmp_path):
    from shapely.geometry import Polygon

    bowtie = Polygon([(300000, 2770000), (300010, 2770010), (300010, 2770000), (300000, 2770010)])
    p = tmp_path / "bad.gpkg"
    gpd.GeoDataFrame({"parcel_id": ["x"]}, geometry=[bowtie], crs=3826).to_file(p, layer="parcels")
    res = check_area(p, 100.0)
    assert not res.ok and any("無效" in e for e in res.errors)


def test_no_crs_file_is_error(fx, tmp_path):
    prj = fx["cadastre"].with_suffix(".prj")
    prj.unlink()
    res = check_crs(fx["cadastre"])
    assert not res.ok and any("沒有 CRS" in e for e in res.errors)
    with pytest.raises(CRSMissingError):
        read_vector(fx["cadastre"])
    with pytest.raises(CRSMissingError):
        run_overlay(_spec(fx, tmp_path))


def test_normalize_crs_3857_and_identity():
    g = gpd.GeoDataFrame(geometry=[box(300000, 2770000, 300010, 2770010)], crs=3826)
    same, notes = normalize_crs(g)
    assert same is g and "未轉換" in notes[0]
    back, notes = normalize_crs(g.to_crs(3857))
    assert "3857" in notes[0]
    assert back.geometry.area.iloc[0] == pytest.approx(100.0, rel=1e-6)


def test_crs_check_3826_label_on_lonlat_data_is_red(tmp_path):
    p = tmp_path / "mislabeled.gpkg"
    gpd.GeoDataFrame(geometry=[Point(121.5, 25.0)], crs=3826).to_file(p)
    res = check_crs(p)
    assert not res.ok and any("不在台灣" in e for e in res.errors)


def test_crs_check_penghu_3825_allow(tmp_path):
    p = tmp_path / "penghu.gpkg"
    gpd.GeoDataFrame(geometry=[Point(119.58, 23.57)], crs=4326).to_crs(3825).to_file(p)
    assert not check_crs(p).ok
    res = check_crs(p, allow={3825})
    assert res.ok and res.warnings
    far = tmp_path / "not_penghu.gpkg"
    gpd.GeoDataFrame(geometry=[Point(121.5, 25.0)], crs=4326).to_crs(3825).to_file(far)
    assert not check_crs(far, allow={3825}).ok


def test_crs_check_raster(tmp_path):
    import rasterio
    from rasterio.transform import from_origin

    def write(path, crs):
        with rasterio.open(path, "w", driver="GTiff", height=4, width=4, count=1, dtype="float32",
                           crs=crs, transform=from_origin(300000, 2770080, 20, 20)) as dst:
            dst.write(np.zeros((1, 4, 4), dtype="float32"))

    good, bad = tmp_path / "dtm.tif", tmp_path / "dtm67.tif"
    write(good, "EPSG:3826")
    write(bad, "EPSG:3828")
    assert check_crs(good).ok
    res = check_crs(bad)
    assert not res.ok and any("TWD67" in e for e in res.errors)
