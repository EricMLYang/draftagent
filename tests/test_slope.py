"""Level 1-3 坡度分析：hydro.slope 與 verify.slope_check 的單元測試。"""

from __future__ import annotations

import csv
import importlib.util
import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from draftagent.hydro.slope import (
    CRSMismatchError,
    classify,
    load_slope_classes,
    read_dtm,
    run_slope,
    slope_percent,
)
from draftagent.schema.slope import DEFAULT_CLASSES_PATH, SlopeSpec
from draftagent.verify.slope_check import check

ROOT = Path(__file__).resolve().parent.parent
TASK = ROOT / "tests" / "level1" / "1-3"


def _load_fixture_module():
    spec = importlib.util.spec_from_file_location(
        "mkfix", ROOT / "tools" / "make_fixtures_slope.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory) -> Path:
    """在暫存目錄重新產生 fixtures（確認產生器是確定性的，並避免依賴 repo 內檔案狀態）。"""
    out = tmp_path_factory.mktemp("fx13")
    mod = _load_fixture_module()
    import sys

    argv = sys.argv
    sys.argv = ["make_fixtures_slope.py", "--out", str(out)]
    try:
        mod.main()
    finally:
        sys.argv = argv
    return out


@pytest.fixture(scope="module")
def run_out(fixtures, tmp_path_factory) -> tuple[SlopeSpec, dict]:
    o = tmp_path_factory.mktemp("run13")
    spec = SlopeSpec(
        dtm_path=fixtures / "input" / "dtm.tif",
        site_path=fixtures / "input" / "site.gpkg",
        site_layer="site",
        output_raster=o / "slope.tif",
        output_gpkg=o / "slope.gpkg",
        output_csv=o / "slope.csv",
    )
    return spec, run_slope(spec)


# ---------------------------------------------------------------- 坡度計算


@pytest.mark.parametrize(
    "sx,sy,expect", [(0.10, 0.0, 10.0), (0.06, 0.08, 10.0), (0.0, -0.35, 35.0)]
)
def test_plane_slope_exact(sx, sy, expect):
    cell = 20.0
    cols, rows = np.meshgrid(np.arange(30), np.arange(25))
    x, y_north = cols * cell, -rows * cell  # row 往南遞增
    z = 50 + sx * x + sy * y_north
    s = slope_percent(z, cell, cell)
    assert np.isnan(s[0]).all() and np.isnan(s[-1]).all()
    assert np.isnan(s[:, 0]).all() and np.isnan(s[:, -1]).all()
    inner = s[1:-1, 1:-1]
    assert np.allclose(inner, expect, atol=0.1)


def test_nodata_propagates():
    z = np.tile(np.arange(10, dtype=float) * 2.0, (10, 1))
    z[5, 5] = np.nan
    s = slope_percent(z, 20.0, 20.0)
    assert np.isnan(s[4:7, 4:7]).all()
    assert not np.isnan(s[2, 2])


# ---------------------------------------------------------------- 分級


def test_classes_from_yaml_and_boundaries():
    t = load_slope_classes(DEFAULT_CLASSES_PATH)
    assert [c.cls for c in t.classes] == [1, 2, 3, 4, 5, 6]
    assert all(c.source and c.as_of for c in t.classes)
    edges = [c.max_pct for c in t.classes[:-1]]
    s = np.array([0.0, 3.0] + edges + [e + 1e-3 for e in edges] + [200.0, np.nan])
    got = classify(s, t)
    if t.boundary == "upper_inclusive":
        at_edge = [1, 2, 3, 4, 5]
    else:
        at_edge = [2, 3, 4, 5, 6]
    assert list(got) == [1, 1] + at_edge + [2, 3, 4, 5, 6] + [6, t.nodata_class]


def test_boundary_param_switch(tmp_path):
    raw = DEFAULT_CLASSES_PATH.read_text(encoding="utf-8")
    p = tmp_path / "lower.yaml"
    p.write_text(
        raw.replace("boundary: upper_inclusive", "boundary: lower_inclusive"), encoding="utf-8"
    )
    t = load_slope_classes(p)
    assert t.boundary == "lower_inclusive"
    assert classify(np.array([5.0]), t)[0] == 2
    # 浮點：5% 斜面算出 5.0000000001 仍要依 yaml 參數歸屬
    t_up = load_slope_classes(DEFAULT_CLASSES_PATH)
    assert classify(np.array([5.0 + 1e-10]), t_up)[0] == (
        1 if t_up.boundary == "upper_inclusive" else 2
    )


def test_yaml_missing_source_rejected(tmp_path):
    raw = DEFAULT_CLASSES_PATH.read_text(encoding="utf-8")
    p = tmp_path / "bad.yaml"
    p.write_text(
        raw.replace(
            "    source: 山坡地土地可利用限度分類標準（依山坡地保育利用條例訂定）\n", "", 1
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="source"):
        load_slope_classes(p)


# ---------------------------------------------------------------- 管線 vs 解析解


def test_pipeline_matches_analytic_expected(fixtures, run_out):
    _, summary = run_out
    exp = json.loads((fixtures / "expected" / "expected.json").read_text(encoding="utf-8"))
    got = {str(r["class"]): r for r in summary["classes"]}
    for cid, e in exp["classes"].items():
        assert got[cid]["pixel_count"] == e["pixel_count"], cid
        assert got[cid]["area_m2"] == pytest.approx(e["area_m2"])
    # 每一法定級別都出現
    assert all(exp["classes"][str(c)]["pixel_count"] > 0 for c in range(1, 7))
    total = sum(r["area_m2"] for r in summary["classes"])
    assert total == pytest.approx(summary["site_area_pixel_m2"])
    assert summary["site_area_vector_m2"] == pytest.approx(exp["site_area_vector_m2"])
    assert abs(summary["site_area_diff_pct"]) < 1.0
    assert summary["site_area_pixel_ping"] == pytest.approx(summary["site_area_pixel_m2"] * 0.3025)


def test_repo_expected_in_sync(fixtures):
    """repo 內的 expected.json 與重新產生的一致（產生器確定性）。"""
    a = json.loads((fixtures / "expected" / "expected.json").read_text(encoding="utf-8"))
    b = json.loads((TASK / "expected" / "expected.json").read_text(encoding="utf-8"))
    assert a == b


def test_outputs_written(run_out):
    spec, _ = run_out
    with rasterio.open(spec.output_raster) as src:
        assert src.crs.to_epsg() == 3826
        assert src.dtypes[0] == "uint8"
    g = gpd.read_file(spec.output_gpkg, layer="slope_classes")
    assert {"class", "area_m2"} <= set(g.columns)
    assert g.crs.to_epsg() == 3826


# ---------------------------------------------------------------- verifier 綠燈


@pytest.mark.parametrize("kind", ["gpkg", "csv"])
def test_verifier_green(run_out, kind):
    spec, _ = run_out
    out = spec.output_gpkg if kind == "gpkg" else spec.output_csv
    res = check(out, spec.site_path, "site", dtm=spec.dtm_path)
    assert res.ok, res.errors
    assert any("verified: false" in w for w in res.warnings)
    assert res.metrics["sample_class_agreement"] == 1.0


# ---------------------------------------------------------------- 故意錯的 fixtures


def test_non_3826_dtm_raises(tmp_path, fixtures):
    bad = tmp_path / "dtm_3857.tif"
    with rasterio.open(fixtures / "input" / "dtm.tif") as src:
        z, prof = src.read(1), src.profile
    prof.update(crs="EPSG:3828")  # TWD67 TM2
    with rasterio.open(bad, "w", **prof) as dst:
        dst.write(z, 1)
    with pytest.raises(CRSMismatchError, match="3826"):
        read_dtm(bad)
    spec = SlopeSpec(
        dtm_path=bad,
        site_path=fixtures / "input" / "site.gpkg",
        output_raster=tmp_path / "o.tif",
        output_gpkg=tmp_path / "o.gpkg",
        output_csv=tmp_path / "o.csv",
    )
    with pytest.raises(CRSMismatchError):
        run_slope(spec)


def test_dtm_without_crs_raises(tmp_path):
    p = tmp_path / "nocrs.tif"
    with rasterio.open(
        p,
        "w",
        driver="GTiff",
        height=5,
        width=5,
        count=1,
        dtype="float32",
        transform=from_origin(300000, 2770000, 20, 20),
    ) as dst:
        dst.write(np.zeros((5, 5), dtype="float32"), 1)
    with pytest.raises(CRSMismatchError):
        read_dtm(p)


def test_spec_extra_forbidden():
    with pytest.raises(ValueError):
        SlopeSpec.model_validate(
            {
                "dtm_path": "a.tif",
                "site_path": "s.gpkg",
                "output_raster": "o.tif",
                "output_gpkg": "o.gpkg",
                "output_csv": "o.csv",
                "slope_method": "guess",
            }
        )


def _rewrite_csv(src: Path, dst: Path, fn) -> None:
    with open(src, encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
        fields = list(rows[0])
    rows = fn(rows)
    with open(dst, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def test_tampered_csv_missing_class_red(run_out, tmp_path):
    spec, _ = run_out
    bad = tmp_path / "missing.csv"
    _rewrite_csv(spec.output_csv, bad, lambda rows: [r for r in rows if r["class"] != "3"])
    res = check(bad, spec.site_path, "site")
    assert not res.ok
    assert any("缺少" in e for e in res.errors)
    assert any("加總" in e for e in res.errors)


def test_tampered_gpkg_missing_class_red(run_out, tmp_path):
    spec, _ = run_out
    g = gpd.read_file(spec.output_gpkg, layer="slope_classes")
    bad = tmp_path / "missing.gpkg"
    g[g["class"] != 1].to_file(bad, layer="slope_classes", driver="GPKG")
    res = check(bad, spec.site_path, "site", dtm=spec.dtm_path)
    assert not res.ok
    assert any("缺少" in e for e in res.errors)


def test_tampered_small_shift_caught_by_dtm(run_out, tmp_path):
    """面積總和不變、只在兩級間挪 5 個像元：沒給 --dtm 抓不到，給 --dtm 必須紅燈。"""
    spec, _ = run_out

    def shift(rows):
        for r in rows:
            if r["class"] in ("2", "3"):
                d = 5 if r["class"] == "2" else -5
                n = int(r["pixel_count"]) + d
                r["pixel_count"] = n
                r["area_m2"] = n * 400.0
                r["area_ping"] = round(n * 400.0 * 0.3025, 4)
        return rows

    bad = tmp_path / "shift.csv"
    _rewrite_csv(spec.output_csv, bad, shift)
    assert check(bad, spec.site_path, "site").ok
    res = check(bad, spec.site_path, "site", dtm=spec.dtm_path)
    assert not res.ok
    assert any("重算" in e for e in res.errors)


def test_negative_and_unknown_class_red(run_out, tmp_path):
    spec, _ = run_out

    def corrupt(rows):
        rows[1]["area_m2"] = "-400.0"
        rows[1]["pixel_count"] = "-1"
        rows.append(
            dict(rows[2], **{"class": "7", "pixel_count": "0", "area_m2": "0", "area_ping": "0"})
        )
        return rows

    bad = tmp_path / "neg.csv"
    _rewrite_csv(spec.output_csv, bad, corrupt)
    res = check(bad, spec.site_path, "site")
    assert not res.ok
    assert any("負值" in e for e in res.errors)
    assert any("class 7" in e for e in res.errors)


def test_verifier_site_wrong_crs_red(run_out, tmp_path):
    spec, _ = run_out
    g = gpd.read_file(spec.site_path, layer="site").set_crs("EPSG:3825", allow_override=True)
    bad = tmp_path / "site_3825.gpkg"
    g.to_file(bad, layer="site", driver="GPKG")
    res = check(spec.output_csv, bad, "site")
    assert not res.ok
    assert any("3826" in e for e in res.errors)


def test_verified_true_no_warning(run_out, tmp_path):
    spec, _ = run_out
    p = tmp_path / "ok.yaml"
    p.write_text(
        DEFAULT_CLASSES_PATH.read_text(encoding="utf-8").replace(
            "verified: false", "verified: true"
        ),
        encoding="utf-8",
    )
    res = check(spec.output_csv, spec.site_path, "site", classes_path=p)
    assert res.ok
    assert not any("verified" in w for w in res.warnings)
