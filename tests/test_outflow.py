"""出流管制計畫書水文水理：hydro.outflow、schema.outflow、verify.outflow_check、swmm_export、charts。

標準答案：
- 解析 / 守恆：UH 體積 = 1 mm × A、Puls 對線性水庫解析解、交替區塊總量 = R24 = I(1440)·24
- 手算：SCS Pe（CN 80、P 100 mm → 50.539 mm）、Tp / Qp（A 1 km²、Tc 1 hr、tr 10 min）、
  垂直圓孔（D 0.1 m、Z 1.0 m、C0 0.61 → 0.021221 m³/s）、漫地流 T1（L 120 m、H 1%、CN 70 → 20.132 min）
- 故意錯：開發後洪峰 > 開發前、安全係數 1.1、result.json 水量被竄改、單位歷線被竄改
"""

from __future__ import annotations

import copy
import json
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from draftagent.hydro.outflow import (
    applicability,
    compute,
    convolve,
    design_storm_alternating_block,
    hav_from_layers,
    hav_volume,
    horner,
    load_codes,
    orifice_q,
    overland_inlet_time_hr,
    routing_modified_puls,
    scs_cumulative_excess,
    scs_runoff,
    scs_unit_hydrograph,
    simple_check,
    weir_cd,
    weir_q,
)
from draftagent.hydro.swmm_export import build_inp, read_sections, run_swmm_subprocess, write_inp
from draftagent.schema.outflow import OutflowControlSpec
from draftagent.verify.outflow_check import check

ROOT = Path(__file__).resolve().parent.parent
TASK = ROOT / "tests" / "level2" / "outflow-1"
SPEC = TASK / "input" / "spec.json"
EXPECTED = json.loads((TASK / "expected" / "expected.json").read_text(encoding="utf-8"))
RTOL = 1e-4


@pytest.fixture(scope="module")
def codes():
    return load_codes()


@pytest.fixture()
def base() -> dict:
    return json.loads(SPEC.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def result() -> dict:
    return compute(OutflowControlSpec.model_validate_json(SPEC.read_text(encoding="utf-8")))


def _spec(d: dict) -> OutflowControlSpec:
    return OutflowControlSpec.model_validate(d)


# ---------------------------------------------------------------- yaml


def test_codes_sources(codes):
    assert codes["verified"] is False
    tab = codes["scs_unit_hydrograph"]["table"]
    assert len(tab) == 33
    assert tab[0] == [0.0, 0.0] and tab[10] == [1.0, 1.0] and tab[-1] == [5.0, 0.0]
    assert codes["detention"]["safety_factor_min"] == 1.2
    assert codes["simple_check"]["volume_m3_per_ha"] == 520
    assert codes["simple_check"]["q10_cms_per_ha"] == 0.16
    assert codes["applicability"]["retention_m3_per_ha"] == 675
    assert codes["scs_cn"]["pre_dev_cn_max"] == 70
    for g in ("design_storm", "scs_unit_hydrograph", "hydraulics", "applicability"):
        assert codes[g]["source"] and codes[g]["url"] and codes[g]["as_of"]


# ---------------------------------------------------------------- 雨量 / 有效雨量


def test_alternating_block_total_equals_horner_24hr():
    p = {"a": 3270.0, "b": 30.0, "c": 0.75}
    st = design_storm_alternating_block(p, 1440, 10)
    d = st["depth_mm"]
    assert len(d) == 144
    R24 = horner(3270.0, 30.0, 0.75, 1440) * 24.0
    assert math.isclose(sum(d), R24, rel_tol=1e-12)
    assert math.isclose(st["R24_mm"], R24, rel_tol=1e-12)
    assert math.isclose(R24, 330.5757510562687, rel_tol=1e-9)
    c = st["peak_index"]
    assert c == 71 and d[c] == max(d)
    # 右大左小：同距離時右邊 ≥ 左邊，且向兩側遞減
    for k in range(1, 71):
        assert d[c + k] >= d[c - k]
        assert d[c + k] >= d[c + k + 1] and d[c - k] >= d[c - k - 1]
    # 第一區塊雨量 = I(10)·10/60
    assert math.isclose(max(d), horner(3270, 30, 0.75, 10) * 10 / 60, rel_tol=1e-12)


def test_scs_runoff_hand():
    # Y = 25.4(1000/80 − 10) = 63.5；Pe = (100 − 12.7)² / (100 + 50.8) = 50.539
    assert math.isclose(scs_cumulative_excess(100.0, 80.0), 87.3**2 / 150.8, rel_tol=1e-12)
    assert math.isclose(scs_cumulative_excess(100.0, 80.0), 50.539058, rel_tol=1e-6)
    assert scs_cumulative_excess(12.7, 80.0) == 0.0
    ro = scs_runoff([5.0, 10.0, 40.0, 45.0], 80.0)
    assert math.isclose(sum(ro["excess_mm"]), ro["Re_mm"], rel_tol=1e-12)
    assert math.isclose(ro["Re_mm"], scs_cumulative_excess(100.0, 80.0), rel_tol=1e-12)
    assert ro["excess_mm"][0] == 0.0
    assert all(e >= 0 for e in ro["excess_mm"])


def test_overland_inlet_time_hand():
    # T1 = 120^0.8 (108.857 + 25.4)^0.7 / (4238·1^0.5) hr = 20.132 min
    assert math.isclose(60 * overland_inlet_time_hr(120.0, 1.0, 70.0), 20.132211, rel_tol=1e-6)


# ---------------------------------------------------------------- 單位歷線


def test_uh_hand_tp_qp(codes):
    u = scs_unit_hydrograph(1.0, 1.0, 1 / 6, codes, normalize=False)
    assert math.isclose(u["Tp_hr"], 1 / 12 + 0.6, rel_tol=1e-12)
    assert math.isclose(u["Qp_cms_per_mm"], 0.208 / (1 / 12 + 0.6), rel_tol=1e-12)
    assert max(u["ordinates_cms"]) <= u["Qp_cms_per_mm"] + 1e-12


@pytest.mark.parametrize(("A", "tc"), [(0.008, 10 / 60), (0.008, 20 / 60), (1.0, 1.0), (25.0, 3.0)])
def test_uh_volume_conserved(codes, A, tc):
    u = scs_unit_hydrograph(A, tc, 1 / 6, codes)
    target = 1000.0 * A
    assert math.isclose(sum(u["ordinates_cms"]) * u["dt_hr"] * 3600, target, rel_tol=1e-9)
    # 校正前離散化誤差應很小（0.208 對應表面積 ≈ 1/(0.208·3.6)）
    assert abs(u["volume_ratio_raw"] - 1.0) < 0.05


def test_convolve_volume(codes):
    u = scs_unit_hydrograph(0.5, 0.5, 1 / 6, codes)
    ex = [0.0, 2.0, 10.0, 3.0, 0.0, 1.0]
    q = convolve(ex, u["ordinates_cms"])
    assert len(q) == len(ex) + len(u["ordinates_cms"]) - 1
    assert math.isclose(sum(q) * 600.0, sum(ex) * 1000.0 * 0.5, rel_tol=1e-9)


# ---------------------------------------------------------------- 演算 / 出流工


def test_puls_linear_reservoir_analytic():
    # 稜柱池 A=100 m²、出流 Q = K·h（K=0.01 m²/s）→ 線性水庫 k = A/K = 10000 s
    A, K, I0 = 100.0, 0.01, 0.05
    hav = hav_from_layers([0.0, 50.0], [A, A])
    n, dt = 600, 60.0
    r = routing_modified_puls([I0] * n, hav, lambda H: K * H, dt)
    k = A / K
    for i in (10, 100, 300, n - 1):
        exact = I0 * (1.0 - math.exp(-i * dt / k))
        assert math.isclose(r["outflow_cms"][i], exact, rel_tol=1e-4)
    assert abs(r["balance_rel"]) < 1e-9


def test_puls_tailwater_and_substeps():
    hav = hav_from_layers([0.0, 2.0], [100.0, 100.0])
    infl = [0.0, 0.05, 0.1, 0.05, 0.0] + [0.0] * 40

    def fn(H, tw=None):
        return orifice_q("vertical_circular", H, 0.0, load_codes(), diameter=0.1, tailwater=tw)

    free = routing_modified_puls(infl, hav, fn, 600.0, substeps=10)
    high = routing_modified_puls(infl, hav, fn, 600.0, tailwater=[0.3] * len(infl), substeps=10)
    assert high["peak_outflow_cms"] < free["peak_outflow_cms"]
    assert high["max_level_m"] > free["max_level_m"]
    for r in (free, high):
        assert abs(r["balance_rel"]) < 1e-9


def test_hav_frustum():
    hav = hav_from_layers([0.0, 1.0, 2.0], [100.0, 100.0, 400.0])
    assert hav["volumes"][1] == pytest.approx(100.0)
    assert hav["volumes"][2] == pytest.approx(100.0 + (100 + 400 + 200) / 3.0)
    assert hav_volume(hav, 0.5) == pytest.approx(50.0)
    assert hav_volume(hav, 3.0) == pytest.approx(hav["volumes"][2] + 400.0)


def test_orifice_weir_hand(codes):
    # 垂直圓孔：D 0.1、中心 0.05、水位 1.05 → Z 1.0；Q = 0.61·π/4·0.01·√(2·9.81)
    q = orifice_q("vertical_circular", 1.05, 0.0, codes, diameter=0.1)
    assert math.isclose(q, 0.61 * math.pi / 4 * 0.01 * math.sqrt(19.62), rel_tol=1e-12)
    assert math.isclose(q, 0.021221, rel_tol=1e-4)
    # 潛沒：外水位 0.55 → Z = 0.5
    qs = orifice_q("vertical_circular", 1.05, 0.0, codes, diameter=0.1, tailwater=0.55)
    assert math.isclose(qs, q * math.sqrt(0.5), rel_tol=1e-12)
    assert orifice_q("vertical_circular", 1.05, 0.0, codes, diameter=0.1, tailwater=1.2) == 0.0
    # 未滿口：在 h = D 處連續
    full = orifice_q("vertical_circular", 0.1, 0.0, codes, diameter=0.1)
    assert math.isclose(orifice_q("vertical_circular", 0.1 - 1e-9, 0.0, codes, diameter=0.1), full,
                        rel_tol=1e-6)
    # 水平孔口：小水頭為堰流 Cw·Pe·he^1.5
    qh = orifice_q("horizontal_circular", 0.02, 0.0, codes, diameter=0.3)
    assert math.isclose(qh, 1.7 * math.pi * 0.3 * 0.02**1.5, rel_tol=1e-12)
    # 堰：Cd 0.6、L 1、H 0.2
    assert math.isclose(weir_q(1.0, 0.2, 0.6, codes), 0.6 * 2 / 3 * math.sqrt(19.62) * 0.2**1.5)
    assert weir_cd(0.05, codes, crest_width=1.0)["type"] == "long"
    assert math.isclose(weir_cd(0.2, codes, crest_width=1.0)["cd"], 0.028 * 0.2 + 0.521)
    assert math.isclose(weir_cd(0.6, codes, crest_width=1.0)["cd"], 0.120 * 0.6 + 0.492)
    assert math.isclose(weir_cd(0.3, codes, weir_height=0.6)["cd"], 0.08 * 0.5 + 0.611)


# ---------------------------------------------------------------- 門檻


@pytest.mark.parametrize(
    ("area", "urban", "new", "route"),
    [
        (0.8, True, True, "outflow_plan_or_exempt_filing"),
        (0.2, True, True, "outflow_plan_or_exempt_filing"),
        (0.19, True, True, "not_required"),
        (0.8, False, True, "not_required"),
        (0.8, True, False, "not_required"),
        (1.0, False, False, "outflow_plan"),
        (3.0, True, True, "outflow_plan"),
    ],
)
def test_applicability(codes, area, urban, new, route):
    a = applicability(area, urban, new, gravity_drainage=True, outfall_q10_capacity_ok=True, codes=codes)
    assert a["route"] == route
    if route == "outflow_plan_or_exempt_filing":
        assert a["exempt_filing"]["retention_required_m3"]["value"] == pytest.approx(675 * area)
        assert a["exempt_filing"]["retention_required_m3"]["source"] == "applicability.retention_m3_per_ha"
    if route != "not_required":
        assert a["simple_check_eligible"] is True
    assert applicability(6.0, False, False, gravity_drainage=True, outfall_q10_capacity_ok=True,
                         codes=codes)["simple_check_eligible"] is False


def test_simple_check(codes):
    sc = simple_check(0.8, 500.0, 0.1, codes)
    assert sc["volume_required_m3"]["value"] == pytest.approx(416.0)
    assert sc["q10_max_cms"]["value"] == pytest.approx(0.128)
    assert sc["volume_ok"] and sc["q10_ok"]
    assert not simple_check(0.8, 400.0, 0.13, codes)["volume_ok"]


# ---------------------------------------------------------------- 範例


def test_example_matches_expected(result):
    e = EXPECTED
    assert result["applicability"]["route"] == e["route"]
    pre, post = result["catchments"]["pre"], result["catchments"]["post"]
    assert pre["cn_capped"] is True and pre["cn_used"]["value"] == 70
    assert pre["cn_used"]["source"] == "scs_cn.pre_dev_cn_max"
    assert math.isclose(pre["tc"]["used_min"], e["pre"]["tc_min"], rel_tol=RTOL)
    assert post["tc"]["floored"] and post["tc"]["used_min"] == 10
    assert math.isclose(post["tc"]["input_min"], e["post"]["tc_input_min"], rel_tol=RTOL)
    for T, ev in e["events"].items():
        r = result["events"][T]
        assert math.isclose(r["R24_mm"]["value"], ev["R24_mm"], rel_tol=RTOL)
        assert math.isclose(r["pre"]["peak_cms"]["value"], ev["pre_peak_cms"], rel_tol=RTOL)
        assert math.isclose(r["routing"]["peak_inflow_cms"], ev["post_inflow_peak_cms"], rel_tol=RTOL)
        assert math.isclose(r["routing"]["peak_outflow_cms"]["value"], ev["post_outflow_peak_cms"],
                            rel_tol=RTOL)
        assert math.isclose(r["routing"]["max_level_m"], ev["max_level_m"], rel_tol=RTOL)
    d = result["detention"]
    assert math.isclose(d["required_m3"]["value"], e["detention"]["required_m3"], rel_tol=RTOL)
    assert math.isclose(d["provided_m3"]["value"], e["detention"]["provided_m3"], rel_tol=RTOL)
    sc = result["simple_check"]
    assert sc["volume_required_m3"]["value"] == pytest.approx(e["simple_check"]["volume_required_m3"])
    assert sc["q10_ok"] is e["simple_check"]["q10_ok"]


def test_example_verifier_green(base, result):
    v = check(_spec(base), result)
    assert v.ok, v.render()
    assert any("verified: false" in w for w in v.warnings)
    assert any("示範" in w for w in v.warnings)
    assert v.metrics["result_mismatches"] == 0


# ---------------------------------------------------------------- 故意錯


def test_bad_post_peak_exceeds_pre(base):
    base["outlet"]["orifices"][0]["diameter_m"] = 0.45
    v = check(_spec(base))
    assert not v.ok
    assert any("開發後洪峰大於開發前" in e for e in v.errors), v.render()


def test_bad_safety_factor(base):
    base["safety_factor"] = 1.1
    v = check(_spec(base))
    assert not v.ok
    assert any("安全係數 1.1 < 1.2" in e for e in v.errors), v.render()


def test_bad_volume_insufficient(base):
    base["detention"]["layers"] = base["detention"]["layers"][:4]  # 池頂降到 101.5
    v = check(_spec(base))
    assert not v.ok
    assert any("超過池頂" in e or "提供滯洪體積" in e for e in v.errors), v.render()


def test_bad_mass_balance_tampered(base, result):
    bad = copy.deepcopy(result)
    bad["events"]["10"]["routing"]["outflow_volume_m3"] *= 0.9
    v = check(_spec(base), bad)
    assert not v.ok
    assert any("水量不守恆" in e for e in v.errors), v.render()
    assert any("outflow_volume_m3" in e for e in v.errors)


def test_bad_uh_tampered(base, result):
    bad = copy.deepcopy(result)
    bad["unit_hydrographs"]["post"]["ordinates_cms"] = [
        q * 1.05 for q in bad["unit_hydrographs"]["post"]["ordinates_cms"]
    ]
    v = check(_spec(base), bad)
    assert not v.ok
    assert any("單位歷線體積" in e for e in v.errors), v.render()


def test_simple_check_selected_fails(base):
    base["site"]["use_simple_check"] = True
    v = check(_spec(base))
    assert not v.ok
    assert any("簡易檢核 Q10" in e for e in v.errors), v.render()


def test_freeboard_violation(base):
    base["detention"]["freeboard_m"] = 0.9  # 池頂−出水高 = 101.6 < 最高水位 101.70
    v = check(_spec(base))
    assert any("池頂 − 出水高" in e for e in v.errors), v.render()


# ---------------------------------------------------------------- schema


def test_schema_rejects(base):
    with pytest.raises(ValidationError):
        _spec({**base, "Q10": 0.1})
    b = copy.deepcopy(base)
    b["post"]["subareas"][0]["cn"] = 98
    with pytest.raises(ValidationError):
        _spec(b)
    b = copy.deepcopy(base)
    del b["horner"]["params"]["2"]
    with pytest.raises(ValidationError, match=r"horner\.params 缺重現期"):
        _spec(b)
    b = copy.deepcopy(base)
    b["outlet"]["orifices"][0]["invert_m"] = 99.0
    with pytest.raises(ValidationError, match="低於池底"):
        _spec(b)
    b = copy.deepcopy(base)
    b["horner"]["source_note"] = ""
    with pytest.raises(ValidationError):
        _spec(b)


# ---------------------------------------------------------------- CLI


def _cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", *args], capture_output=True, text=True, cwd=ROOT,
                          timeout=120)


def test_cli_pipeline(tmp_path, base):
    cp = _cli("draftagent.hydro.outflow", str(SPEC), "-o", str(tmp_path), "--swmm")
    assert cp.returncode == 0, cp.stderr
    res = tmp_path / "result.json"
    cp = _cli("draftagent.verify.outflow_check", str(SPEC), "--result", str(res))
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert "PASS" in cp.stdout
    bad = json.loads(res.read_text(encoding="utf-8"))
    bad["events"]["10"]["routing"]["storage_end_m3"] += 500.0
    (tmp_path / "bad.json").write_text(json.dumps(bad), encoding="utf-8")
    cp = _cli("draftagent.verify.outflow_check", str(SPEC), "--result", str(tmp_path / "bad.json"),
              "--json")
    assert cp.returncode == 1
    assert json.loads(cp.stdout)["ok"] is False


# ---------------------------------------------------------------- SWMM


@pytest.mark.parametrize("mode", ["hydrograph", "subcatchment"])
def test_swmm_inp_structure(base, result, mode):
    spec = _spec(base)
    sec = read_sections(build_inp(spec, T=10, result=result, mode=mode))
    for s in ("OPTIONS", "STORAGE", "ORIFICES", "WEIRS", "OUTFALLS", "XSECTIONS", "CURVES",
              "TIMESERIES"):
        assert sec.get(s), s
    nodes = {r[0] for r in sec["STORAGE"]} | {r[0] for r in sec["OUTFALLS"]}
    links = list(sec["ORIFICES"] + sec["WEIRS"])
    assert len(links) == 3
    assert all(r[1] in nodes and r[2] in nodes for r in links)
    assert {r[0] for r in sec["XSECTIONS"]} == {r[0] for r in links}
    ts_names = {r[0] for r in sec["TIMESERIES"]}
    assert "TS_TW" in ts_names
    curve = [r for r in sec["CURVES"] if r[0] == "SC_ST1"]
    assert len(curve) == 6
    if mode == "hydrograph":
        assert sec["INFLOWS"][0][:3] == ["ST1", "FLOW", "TS_IN"]
        assert "SUBCATCHMENTS" not in sec
    else:
        assert sec["SUBCATCHMENTS"][0][2] == "ST1"
        assert "TS_RAIN" in ts_names
        assert "INFLOWS" not in sec
    opts = {r[0]: r[1] for r in sec["OPTIONS"]}
    assert opts["FLOW_UNITS"] == "CMS"


@pytest.mark.skipif(os.name == "nt", reason="SWMM 引擎（swmm-toolkit 動態庫）未在 Windows CI 驗證")
def test_swmm_engine_parses(tmp_path, base, result):
    """以子行程跑 swmm-toolkit；本機動態庫被系統終止或未安裝時 skip（不在主行程 import）。"""
    inp = write_inp(_spec(base), tmp_path / "t10.inp", T=10, result=result)
    info = run_swmm_subprocess(inp, timeout_s=120)
    if not info["ok"] and (info["returncode"] is None or info["returncode"] < 0
                           or "ModuleNotFoundError" in info["message"]):
        pytest.skip(f"SWMM 子行程無法執行：{info['message']}")
    assert info["ok"], info["message"]
    assert Path(info["rpt"]).exists()


# ---------------------------------------------------------------- 附圖


def test_charts_render(tmp_path, result):
    pytest.importorskip("matplotlib")
    from draftagent.render.charts import render_all

    paths = render_all(result, tmp_path)
    assert set(paths) == {f"fig{n}_{k}_{ext}" for n, k in ((16, "hydrographs"), (18, "hav"),
                                                          (19, "routing")) for ext in ("png", "pdf")}
    for p in paths.values():
        assert p.exists() and p.stat().st_size > 5000
