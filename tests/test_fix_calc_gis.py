"""稽核 false-negative 回歸測試（第 1、3、11、12、15、16 項）：法規 yaml 鎖定、outflow 重算與 .inp/附圖、
calc.md 逐字、site-overlay 獨立重疊、slope tif/csv/多邊形位置。

測試名稱以稽核情境編號命名（C1e、O1、O3、O4、O6、O7b、C2、S1–S8、SL1–SL5）。
"""

from __future__ import annotations

import copy
import json
import re
import shutil
from pathlib import Path

import geopandas as gpd
import numpy as np
import pyogrio
import pytest
import rasterio
import yaml
from pydantic import ValidationError

from draftagent import registry
from draftagent.build import build, collect_artifacts, load_spec, verify_artifacts
from draftagent.verify.base import VerifyResult
from draftagent.verify.codes_lock import LOCK_MSG, assert_default_codes, is_repo_codes_path

REPO = Path(__file__).resolve().parents[1]
CODES = REPO / "draftagent/hydro/codes"


def _example(kind: str) -> dict:
    return json.loads((REPO / f"examples/{kind}.json").read_text(encoding="utf-8"))


def _verify_dir(kind: str, d: Path) -> list[VerifyResult]:
    return verify_artifacts(load_spec(d / "spec.json", kind=kind), collect_artifacts(d))


def _errors(rs: list[VerifyResult] | VerifyResult) -> list[str]:
    rs = [rs] if isinstance(rs, VerifyResult) else rs
    return [e for r in rs for e in r.errors]


def _red(rs, pattern: str) -> None:
    errs = _errors(rs)
    assert errs, "expected RED but all verifiers green"
    assert any(re.search(pattern, e) for e in errs), errs[:10]


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    """每個圖種 example build 一次（綠燈基準）；各測試複製後竄改。"""
    out = tmp_path_factory.mktemp("runs")
    done = {}
    for kind in ("site-overlay", "slope", "drainage-calc-ty", "outflow-control"):
        r = build(load_spec(REPO / f"examples/{kind}.json", kind=kind), name="base", out=out, preview=False)
        assert r.ok, r.report()
        done[kind] = r.run_dir
    return done


def _copy(runs, kind: str, tmp_path: Path) -> Path:
    d = tmp_path / kind
    shutil.copytree(runs[kind], d)
    return d


def _spec_build(kind: str, data: dict, tmp_path: Path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    p = tmp_path / f"{kind}.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return build(load_spec(p, kind=kind), out=tmp_path / "out", preview=False)


def _rewrite_gpkg(path: Path, layer: str, fn) -> None:
    layers = [ly[0] for ly in pyogrio.list_layers(path)]
    dfs = {ly: gpd.read_file(path, layer=ly) for ly in layers}
    dfs[layer] = fn(dfs[layer])
    path.unlink()
    for ly, g in dfs.items():
        g.to_file(path, layer=ly, driver="GPKG")


# ------------------------------------------------------------------ #1 法規 yaml 鎖定


def _fake_yaml(tmp_path: Path, name: str, mutate) -> Path:
    d = yaml.safe_load((CODES / name).read_text(encoding="utf-8"))
    d["verified"] = True
    mutate(d)
    p = tmp_path / f"fake_{name}"
    p.write_text(yaml.safe_dump(d, allow_unicode=True), encoding="utf-8")
    return p


def test_codes_lock_helpers(tmp_path):
    assert is_repo_codes_path(CODES / "taoyuan_runoff.yaml")
    assert is_repo_codes_path("draftagent/hydro/codes/taoyuan_runoff.yaml")
    assert not is_repo_codes_path(tmp_path / "taoyuan_runoff.yaml")
    assert not is_repo_codes_path(REPO / "examples/slope.json")
    res = VerifyResult("x")
    assert assert_default_codes(None, CODES / "slope_classes.yaml", res, "classes_path")
    assert assert_default_codes(str(CODES / "slope_classes.yaml"), CODES / "slope_classes.yaml", res, "f")
    assert res.ok
    # 在 codes/ 下但不是預設檔 → verifier error
    assert not assert_default_codes(CODES / "outflow_control.yaml", CODES / "slope_classes.yaml", res, "f")
    assert not res.ok and LOCK_MSG in res.errors[0]


def test_C1e_fake_codes_path_red(tmp_path):
    from draftagent.schema.drainage import TaoyuanSiteDrainageSpec
    from draftagent.verify.drainage_calc_check import check

    def m(d):
        d["runoff_standard"]["min_storage_m3_per_m2"] = 0.001
        d["building_code_4_3"]["min_storage_m3_per_m2"] = 0.001

    fake = _fake_yaml(tmp_path, "taoyuan_runoff.yaml", m)
    data = _example("drainage-calc-ty")
    data["codes_path"] = str(fake)
    data["storage"]["effective_depth_m"] = 0.05
    with pytest.raises(ValidationError, match="法規數值只能讀"):
        TaoyuanSiteDrainageSpec.model_validate(data)
    p = tmp_path / "spec.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    _red(check(p), "法規數值只能讀")
    # 沒有 codes_path 的同一 spec 以真 yaml 重算 → 貯集量不足
    data.pop("codes_path")
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    _red(check(p), "提供貯集量")


def test_O1_fake_codes_path_red(tmp_path):
    from draftagent.schema.outflow import OutflowControlSpec
    from draftagent.verify.outflow_check import check

    fake = _fake_yaml(tmp_path, "outflow_control.yaml",
                      lambda d: d["detention"].__setitem__("safety_factor_min", 0.5))
    data = _example("outflow-control")
    data["codes_path"] = str(fake)
    data["safety_factor"] = 0.5
    with pytest.raises(ValidationError, match="法規數值只能讀"):
        OutflowControlSpec.model_validate(data)
    # 繞過 schema（model_copy 不驗證）→ verifier 仍紅，且以預設 yaml 檢核 SF
    spec = OutflowControlSpec.model_validate(_example("outflow-control"))
    forged = spec.model_copy(update={"codes_path": str(fake), "safety_factor": 0.5})
    v = check(forged)
    _red(v, "法規數值只能讀")
    _red(v, r"安全係數 0\.5 < 1\.2")


def test_SL5_fake_classes_path_red(tmp_path, runs):
    from draftagent.schema.slope import SlopeSpec
    from draftagent.verify.slope_check import check_artifacts

    def m(d):
        for c in d["classes"]:
            for k in ("min_pct", "max_pct"):
                if c.get(k) is not None:
                    c[k] *= 3

    fake = _fake_yaml(tmp_path, "slope_classes.yaml", m)
    data = _example("slope")
    data["classes_path"] = str(fake)
    with pytest.raises(ValidationError, match="法規數值只能讀"):
        SlopeSpec.model_validate(data)
    d = runs["slope"]
    spec = load_spec(d / "spec.json", kind="slope")
    v = check_artifacts(d / "slope_classes.gpkg", d / "slope_classes.csv", d / "slope_classes.tif",
                        spec.site_path, spec.site_layer, spec.dtm_path, fake)
    _red(v, "法規數值只能讀")


# ------------------------------------------------------------------ #3 / #15 outflow


def test_O7b_result_peak_checks_tampered_on_failing_design_red(tmp_path):
    from draftagent.hydro.outflow import compute
    from draftagent.schema.outflow import OutflowControlSpec
    from draftagent.verify.outflow_check import check

    data = _example("outflow-control")
    data["outlet"]["orifices"][0]["diameter_m"] = 0.6
    spec = OutflowControlSpec.model_validate(data)
    r = compute(spec)
    for pc in r["peak_checks"]:
        pc["post_out_peak_cms"] = pc["pre_peak_cms"] * 0.9
        pc["ok"] = True
    v = check(spec, json.loads(json.dumps(r)))
    _red(v, "開發後洪峰大於開發前")
    _red(v, r"result\.json peak_checks\[0\]\.post_out_peak_cms")


def test_O5_result_top_elev_and_extra_key_red(runs, tmp_path):
    d = _copy(runs, "outflow-control", tmp_path)
    r = json.loads((d / "result.json").read_text(encoding="utf-8"))
    r["detention"]["top_elev_m"] = 99.0
    r["bogus"] = 1
    del r["simple_check"]["q10_ok"]
    (d / "result.json").write_text(json.dumps(r, ensure_ascii=False), encoding="utf-8")
    rs = _verify_dir("outflow-control", d)
    _red(rs, r"detention\.top_elev_m = 99\.0")
    _red(rs, "重算沒有的鍵")
    _red(rs, r"缺鍵 simple_check\.q10_ok")


def test_O3_swmm_inp_tampered_red(runs, tmp_path):
    d = _copy(runs, "outflow-control", tmp_path)
    p = d / "outflow_T10.inp"
    p.write_text(p.read_text(encoding="utf-8").replace("860.0000", "86.0000"), encoding="utf-8")
    _red(_verify_dir("outflow-control", d), r"\[CURVES\]")
    # 孔口直徑竄改
    d2 = _copy(runs, "outflow-control", tmp_path / "b")
    p = d2 / "outflow_T10.inp"
    p.write_text(re.sub(r"(OR1  CIRCULAR  )\S+", r"\g<1>0.5000", p.read_text(encoding="utf-8")),
                 encoding="utf-8")
    _red(_verify_dir("outflow-control", d2), r"\[XSECTIONS\] OR1")


def test_O3_swmm_inp_missing_red(runs, tmp_path):
    d = _copy(runs, "outflow-control", tmp_path)
    (d / "outflow_T10.inp").unlink()
    _red(_verify_dir("outflow-control", d), "SWMM")


def test_O4_figures_blank_red(runs, tmp_path):
    from PIL import Image

    d = _copy(runs, "outflow-control", tmp_path)
    Image.new("RGB", (10, 10)).save(d / "fig16_hydrographs.png")
    Image.new("RGB", (1800, 1000), "white").save(d / "fig18_hav.png")
    (d / "fig19_routing.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
    rs = _verify_dir("outflow-control", d)
    _red(rs, r"fig16_hydrographs\.png 尺寸")
    _red(rs, r"fig18_hav\.png 近乎空白")
    _red(rs, r"fig19_routing\.pdf")


def test_O6_post_area_change_red(tmp_path):
    from draftagent.schema.outflow import OutflowControlSpec
    from draftagent.verify.outflow_check import check

    data = _example("outflow-control")
    data["post"]["subareas"][1]["area_ha"] = 0.1
    _red(check(OutflowControlSpec.model_validate(data)), "開發前集水面積")
    data["area_change_reason"] = "開發後部分區域改排至區外（工程師確認）"
    v = check(OutflowControlSpec.model_validate(data))
    assert not any("開發前集水面積" in e for e in v.errors)
    assert any("area_change_reason" in w for w in v.warnings)


# ------------------------------------------------------------------ #16 calc.md


def test_C2_calc_md_numbers_doubled_red(runs, tmp_path):
    d = _copy(runs, "drainage-calc-ty", tmp_path)
    p = d / "calc.md"
    t = p.read_text(encoding="utf-8")
    p.write_text(re.sub(r"(\d+)\.(\d+)", lambda m: f"{int(m.group(1)) * 2}.{m.group(2)}", t, count=40),
                 encoding="utf-8")
    _red(_verify_dir("drainage-calc-ty", d), "calc.md 與重算計算表不符")


def test_C2_calc_md_missing_red(runs, tmp_path):
    d = _copy(runs, "drainage-calc-ty", tmp_path)
    arts = collect_artifacts(d)
    (d / "calc.md").unlink()
    _red(verify_artifacts(load_spec(d / "spec.json", kind="drainage-calc-ty"), arts), "calc.md 不存在")


# ------------------------------------------------------------------ #11 site-overlay


def test_site_parcel_id_keeps_leading_zero(runs):
    s = json.loads((runs["site-overlay"] / "summary.json").read_text(encoding="utf-8"))
    assert {p["parcel_id"] for p in s["parcels"]} == {"0102:101", "0102:101-3", "0102:102"}


def test_S1_area_csv_wrong_red(runs, tmp_path):
    d = _copy(runs, "site-overlay", tmp_path)
    p = d / "site_areas.csv"
    t = p.read_text(encoding="utf-8-sig")
    p.write_text(t.replace("1575.00,476.44", "2575.00,776.94"), encoding="utf-8-sig")
    _red(_verify_dir("site-overlay", d), "面積表 CSV「住宅區」")


def test_S2_zoning_attribute_swapped_red(runs, tmp_path):
    d = _copy(runs, "site-overlay", tmp_path)

    def f(g):
        g["分區"] = g["分區"].map({"住宅區": "工業區", "工業區": "住宅區"})
        return g

    _rewrite_gpkg(d / "site.gpkg", "parcel_zoning", f)
    _red(_verify_dir("site-overlay", d), "重新疊圖")


def test_S3_area_attribute_columns_wrong_red(runs, tmp_path):
    d = _copy(runs, "site-overlay", tmp_path)

    def f(g):
        g["area_ping"] = g["area_ping"] * 2
        g["area_m2"] = g["area_m2"] + 100
        return g

    _rewrite_gpkg(d / "site.gpkg", "parcel_zoning", f)
    _red(_verify_dir("site-overlay", d), "area_m2 屬性")


def test_S4_shifted_geometry_labelled_3826_red(runs, tmp_path):
    d = _copy(runs, "site-overlay", tmp_path)
    for ly in ("parcels", "parcel_zoning"):
        _rewrite_gpkg(d / "site.gpkg", ly, lambda g: g.set_geometry(g.geometry.translate(-828.0, 207.0)))
    _red(_verify_dir("site-overlay", d), "Hausdorff")


def test_S5_summary_json_wrong_red(runs, tmp_path):
    d = _copy(runs, "site-overlay", tmp_path)
    s = json.loads((d / "summary.json").read_text(encoding="utf-8"))
    s["total_area_m2"] = 9999
    s["zones"][0]["area_m2"] += 50
    (d / "summary.json").write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")
    rs = _verify_dir("site-overlay", d)
    _red(rs, "summary.json total_area_m2")
    _red(rs, "summary.json zones")


def test_S6_zoning_geometry_swapped_within_parcel_red(runs, tmp_path):
    d = _copy(runs, "site-overlay", tmp_path)

    def f(g):
        g = g.copy()
        a, b = g.index[0], g.index[1]
        ga, gb = g.geometry[a], g.geometry[b]
        g.loc[a, "geometry"] = gb
        g.loc[b, "geometry"] = ga
        return g

    _rewrite_gpkg(d / "site.gpkg", "parcel_zoning", f)
    _red(_verify_dir("site-overlay", d), "重新疊圖")


def test_S7_no_expected_area_red(runs):
    from draftagent.schema.site import SiteSpec
    from draftagent.verify.area_check import check_overlay

    data = _example("site-overlay")
    data.pop("expected_area_m2")
    with pytest.raises(ValidationError, match="expected_area_m2"):
        SiteSpec.model_validate(data)
    # 繞過 schema 時 verifier 也是 error（不是 warning）
    d = runs["site-overlay"]
    spec = load_spec(d / "spec.json", kind="site-overlay").model_copy(update={"expected_area_m2": None})
    _red(check_overlay(spec, d / "site.gpkg"), "expected_area_m2")
    k = registry.get("site-overlay")
    rs = [v(spec, collect_artifacts(d)) for v in k.verifiers]
    _red(rs, "expected_area_m2")


def test_S8_missing_sibling_parcel_warns(tmp_path):
    # 漏列子號地號無法機器判定（業主可能合法地只取得部分子號）→ 只警告，請 agent 與使用者確認
    data = _example("site-overlay")
    data["parcels"][0]["land_numbers"] = ["101", "102"]
    data["expected_area_m2"] = 2475.0
    r = _spec_build("site-overlay", data, tmp_path)
    assert any("疑似漏列子號地號" in w for x in r.results for w in x.warnings), r.report()
    # 使用者確認不納入 → 改成確認訊息
    data["confirmed_excluded_parcels"] = ["101-3"]
    r = _spec_build("site-overlay", data, tmp_path / "ok")
    assert r.ok, r.report()
    assert any("confirmed_excluded_parcels" in w for x in r.results for w in x.warnings)


def test_site_overlay_parcel_id_old_format_still_matches(runs, tmp_path):
    """舊格式 parcel_id（段號前導 0 被吃掉）比對時正規化，不因格式差異誤判。"""
    d = _copy(runs, "site-overlay", tmp_path)
    for ly in ("parcels", "parcel_zoning"):
        _rewrite_gpkg(d / "site.gpkg", ly,
                      lambda g: g.assign(parcel_id=g["parcel_id"].str.replace("0102:", "102:")))
    rs = _verify_dir("site-overlay", d)
    assert all(r.ok for r in rs), _errors(rs)


# ------------------------------------------------------------------ #12 slope


def test_SL1_class_raster_all_class1_red(runs, tmp_path):
    d = _copy(runs, "slope", tmp_path)
    p = d / "slope_classes.tif"
    with rasterio.open(p) as src:
        arr, prof = src.read(1), src.profile
    with rasterio.open(p, "w", **prof) as dst:
        dst.write(np.where(arr > 0, 1, arr).astype(arr.dtype), 1)
    rs = _verify_dir("slope", d)
    _red(rs, "GeoTIFF 基地遮罩內 class")
    _red(rs, "與 DTM 重算不同")


def test_SL2_csv_table_wrong_red(runs, tmp_path):
    d = _copy(runs, "slope", tmp_path)
    p = d / "slope_classes.csv"
    lines = p.read_text(encoding="utf-8").splitlines()
    hdr = lines[0].split(",")
    i_n, i_a, i_p = hdr.index("pixel_count"), hdr.index("area_m2"), hdr.index("area_ping")
    row = lines[2].split(",")  # class 1
    n = int(row[i_n]) + 800
    row[i_n], row[i_a], row[i_p] = str(n), f"{n * 100.0}", f"{n * 100.0 * 0.3025}"
    lines[2] = ",".join(row)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    rs = _verify_dir("slope", d)
    _red(rs, r"^csv: ")
    _red(rs, "csv .* ≠ gpkg")


def test_SL3_class_polygons_moved_red(runs, tmp_path):
    d = _copy(runs, "slope", tmp_path)
    _rewrite_gpkg(d / "slope_classes.gpkg", "slope_classes",
                  lambda g: g.set_geometry(g.geometry.translate(5000, 0)))
    _red(_verify_dir("slope", d), "落在基地 buffer")


def test_SL3b_class_polygons_swapped_same_area_red(runs, tmp_path):
    """兩級多邊形互換（面積欄同步互換）仍須被柵格化比對抓到。"""
    d = _copy(runs, "slope", tmp_path)

    def f(g):
        g = copy.deepcopy(g)
        nz = g.index[g["pixel_count"] > 0][:2]
        a, b = nz[0], nz[1]
        for col in ("geometry", "pixel_count", "area_m2", "area_ping"):
            va, vb = g.loc[a, col], g.loc[b, col]
            g.loc[a, col], g.loc[b, col] = vb, va
        return g

    _rewrite_gpkg(d / "slope_classes.gpkg", "slope_classes", f)
    _red(_verify_dir("slope", d), "多邊形柵格化後")


def test_slope_and_examples_green(runs):
    for kind in ("site-overlay", "slope", "drainage-calc-ty", "outflow-control"):
        rs = _verify_dir(kind, runs[kind])
        assert all(r.ok for r in rs), (kind, _errors(rs))
