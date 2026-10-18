"""drainage/hydraulics.check_elevations（E1–E8）+ 抽水型流出抑制計算。

- 正式範例 drainage-plan-ty-1（抽水型筏基）與其重力型變體必綠
- E1–E8 各一個故意錯 fixture 必紅；改回即綠
- 回歸：舊版範例（bad/legacy_spec.json，出流口在溝底、筏基滿水高於進流管）必被抓到
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import ezdxf
import pytest
from pydantic import ValidationError

from draftagent.drainage import derive
from draftagent.drainage.hydraulics import (
    check_elevations,
    receiving_point,
    resolve_storage,
    storage_profile_levels,
)
from draftagent.drainage.model import DrainageNetworkSpec
from draftagent.render.drainage_plan import write_drainage_plan
from draftagent.verify.drainage_plan_check import check_drainage_plan

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "tests" / "level2" / "drainage-plan-ty-1"
RAW = json.loads((CASE / "input" / "spec.json").read_text(encoding="utf-8"))
LEGACY = json.loads((CASE / "bad" / "legacy_spec.json").read_text(encoding="utf-8"))
EXPECTED = json.loads((CASE / "expected" / "expected.json").read_text(encoding="utf-8"))


def _spec(raw, fn=None) -> DrainageNetworkSpec:
    raw = copy.deepcopy(raw)
    if fn is not None:
        fn(raw)
    return DrainageNetworkSpec.model_validate(raw)


def _node(raw, nid):
    return next(n for n in raw["nodes"] if n["id"] == nid)


def _codes(rep) -> set[str]:
    return {e.split(" ", 1)[0] for e in rep.errors}


def _gravity(raw) -> None:
    """重力型變體（地面淺池）：池底 119.70、有效水深 0.15 → 最高水位 119.85 ≤ 進流管底 119.88；
    孔口底 119.70 ≥ 放流井底 119.66、≥ 受納溝設計水位 119.5955；溢流口 119.85 ≤ 池頂 120.00。"""
    st = raw["storage"]
    for k in (
        "pump_start_el",
        "pump_stop_el",
        "pump_q_cms",
        "overflow_at",
        "pump_outlet_invert_el",
        "overflow_pipe_invert_el",
        "inflow_invert_el",
    ):
        st.pop(k, None)
    st.update(
        drain_mode="gravity",
        effective_depth=0.15,
        bottom_el=119.70,
        max_water_el=119.85,
        top_el=120.00,
        orifice_invert_el=119.70,
        overflow_el=119.85,
    )
    raw["calc"]["outlet"] = {"type": "circ_orifice", "diameter_m": 0.1, "design_head_m": 0.15}


def _then(*fns):
    def f(raw):
        for fn in fns:
            fn(raw)

    return f


# ---------------------------------------------------------------- green


def test_example_pump_design_green():
    sp = _spec(RAW)
    rep = check_elevations(sp)
    assert rep.ok, rep.errors
    r = rep.metrics["receiving"]
    for k in ("invert_el", "top_el", "design_wl"):
        assert r[k] == pytest.approx(EXPECTED["receiving"][k], abs=1e-4)
    assert rep.metrics["E7_pump_run_min"]["run_min"] == pytest.approx(EXPECTED["pump_run_min"], abs=1e-3)
    for key in ("E2_outfall_vs_design_wl", "E5_backwater", "E6_overflow_vs_outlet_well", "E8_inlet_vs_bottom"):
        assert key in rep.metrics
    assert not any("高程未明確給定" in w for w in rep.warnings)


def test_gravity_variant_green():
    rep = check_elevations(_spec(RAW, _gravity))
    assert rep.ok, rep.errors
    assert rep.metrics["E4_orifice_vs_design_wl"]["ok"]
    assert rep.metrics["E8_orifice_vs_outlet_well"]["ok"]


def test_receiving_interpolation():
    sp = _spec(RAW)
    of = sp.node("OF1")
    r = receiving_point(sp, of.x, of.y)
    # EX1 往西流：E 280543 底 119.40 → E 280499 底 119.20；OF1 在 E 280520（距上游 23 m）
    assert r.invert_el == pytest.approx(119.40 - 0.20 * 23 / 44)
    assert r.top_el == pytest.approx(120.00 - 0.20 * 23 / 44)
    assert r.design_wl == pytest.approx(r.invert_el + 0.30)


# ---------------------------------------------------------------- E1–E8 red


def test_e1_outfall_at_ditch_bottom():
    rep = check_elevations(_spec(RAW, lambda r: _node(r, "OF1").update(invert_el=119.30)))
    assert "E1" in _codes(rep)


def test_e2_outfall_below_design_wl():
    rep = check_elevations(_spec(RAW, lambda r: _node(r, "OF1").update(invert_el=119.55)))
    assert _codes(rep) == {"E2"}
    assert rep.metrics["E2_outfall_vs_design_wl"]["diff"] < 0


def test_e2_without_design_depth_is_warning():
    rep = check_elevations(
        _spec(RAW, lambda r: r["existing_drains"][0].pop("design_water_depth_m"))
    )
    assert rep.ok
    assert any(w.startswith("E2 ") for w in rep.warnings)


def test_e3_outfall_above_ditch_top():
    rep = check_elevations(
        _spec(RAW, lambda r: _node(r, "OF1").update(invert_el=120.00, top_el=None))
    )
    assert "E3" in _codes(rep)


def test_e4_gravity_infeasible():
    f = _then(_gravity, lambda r: r["existing_drains"][0].update(design_water_depth_m=0.45))
    rep = check_elevations(_spec(RAW, f))
    assert "E4" in _codes(rep)
    assert any("重力排放不可行" in e for e in rep.errors)


def test_e5_backwater_and_allow():
    def hi(r):
        _gravity(r)
        r["storage"].update(
            bottom_el=119.80, max_water_el=119.95, orifice_invert_el=119.80, overflow_el=119.95, top_el=120.10
        )

    rep = check_elevations(_spec(RAW, hi))
    assert _codes(rep) == {"E5"}
    assert rep.metrics["E5_backwater"]["L2"]["diff"] == pytest.approx(0.07)

    def allow(r):
        hi(r)
        r["storage"].update(allow_backwater=True, backwater_note="進流管段為壓力管，經工程師檢核可承受")

    rep = check_elevations(_spec(RAW, allow))
    assert rep.ok
    assert any(w.startswith("E5 ") and "可承受" in w for w in rep.warnings)


def test_e6_overflow_below_max_wl():
    rep = check_elevations(
        _spec(RAW, lambda r: r["storage"].update(overflow_el=116.80, overflow_at="storage"))
    )
    errs = "\n".join(rep.errors)
    assert "E6" in _codes(rep)
    assert "未達設計滿水位即溢流" in errs
    assert "溢流無法重力排至放流井" in errs


def test_e6_overflow_flood_above_top():
    # 池內溢流：溢流時最高水位 = 溢流口 119.85 + 堰頂水頭 0.15 = 120.00；池頂降到 119.95 → 溢頂
    rep = check_elevations(_spec(RAW, _then(_gravity, lambda r: r["storage"].update(top_el=119.95))))
    assert _codes(rep) == {"E6"}
    assert "溢流時最高水位 EL=120.00" in rep.errors[0] and "溢流期間溢頂" in rep.errors[0]
    assert rep.metrics["E6_overflow_flood_vs_top"]["diff"] == pytest.approx(0.05)


def test_storage_level_vs_overflow_flood_semantics():
    """設計滿水位（= 池底 + 有效水深）≤ 溢流口 < 溢流時最高水位（溢流口 + 堰頂水頭）≤ 池頂。"""
    lv, _, _ = resolve_storage(_spec(RAW, _gravity))
    assert lv.max_water_el == pytest.approx(lv.bottom_el + 0.15)
    assert lv.max_water_el <= lv.overflow_el < lv.overflow_flood_el <= lv.top_el
    assert lv.overflow_flood_el == pytest.approx(lv.overflow_el + RAW["calc"]["overflow_weir"]["head_m"])
    labels = [lab for lab, _, _ in storage_profile_levels(_spec(RAW, _gravity))]
    assert labels[:3] == ["設計滿水位", "溢流口", "溢流時最高水位"]
    # 溢流在進流井（ty-1 抽水型）：不在池體標溢流時最高水位
    assert "溢流時最高水位" not in [lab for lab, _, _ in storage_profile_levels(_spec(RAW))]


def test_e6_orifice_head_mismatch():
    f = _then(_gravity, lambda r: r["calc"]["outlet"].update(design_head_m=0.30))
    rep = check_elevations(_spec(RAW, f))
    assert _codes(rep) == {"E6"}
    assert "計算書與高程不一致" in rep.errors[0]


def test_e7_pump_run_too_short():
    rep = check_elevations(_spec(RAW, lambda r: r["storage"].update(pump_start_el=116.10)))
    assert _codes(rep) == {"E7"}
    assert rep.metrics["E7_pump_run_min"]["run_min"] < 15


def test_e7_stop_above_start():
    rep = check_elevations(
        _spec(RAW, lambda r: r["storage"].update(pump_start_el=116.40, pump_stop_el=116.60))
    )
    assert any("停機水位" in e and "應低於起抽水位" in e for e in rep.errors)


def test_e8_orifice_below_outlet_well():
    f = _then(_gravity, lambda r: _node(r, "OW1").update(invert_el=119.75))
    rep = check_elevations(_spec(RAW, f))
    assert _codes(rep) == {"E8"}
    assert rep.metrics["E8_orifice_vs_outlet_well"]["diff"] == pytest.approx(-0.05)


def test_bottom_plus_depth_must_match_max_wl():
    rep = check_elevations(_spec(RAW, lambda r: r["storage"].update(max_water_el=117.20)))
    assert any("≠ 池底" in e for e in rep.errors)


# ---------------------------------------------------------------- 回歸：舊版範例


def test_legacy_spec_caught():
    sp = _spec(LEGACY)
    lv, _, warns = resolve_storage(sp)
    assert lv.bottom_el == pytest.approx(119.40)  # 預設：池底 ≈ 放流井底
    assert any("高程未明確給定" in w for w in warns)
    rep = check_elevations(sp)
    assert {"E1", "E5"} <= _codes(rep)
    assert any(w.startswith("E2 ") for w in rep.warnings)  # 舊範例無設計水深 → E2 只能 WARNING


def test_legacy_spec_with_design_depth_caught():
    # 舊範例的高程 + 工程師提供的受納溝設計水深 0.30 m（與新範例相同）
    rep = check_elevations(
        _spec(LEGACY, lambda r: r["existing_drains"][0].update(design_water_depth_m=0.30))
    )
    assert {"E1", "E2", "E4", "E5"} <= _codes(rep)


def test_verifier_rejects_legacy(tmp_path):
    sp = _spec(LEGACY)
    res = check_drainage_plan(write_drainage_plan(sp, tmp_path / "x.dxf"), sp)
    assert not res.ok
    errs = "\n".join(res.errors)
    assert "高程：E1" in errs and "高程：E5" in errs


# ---------------------------------------------------------------- 圖面 / 縱斷面 / 計算


def test_outfall_design_wl_label_required(tmp_path):
    sp = _spec(RAW)
    p = write_drainage_plan(sp, tmp_path / "x.dxf")
    assert check_drainage_plan(p, sp).ok
    doc = ezdxf.readfile(str(p))
    msp = doc.modelspace()
    hits = [t for t in msp.query("TEXT") if t.dxf.text.startswith("受納溝設計水位 EL=")]
    assert [t.dxf.text for t in hits] == ["受納溝設計水位 EL=119.60"]
    for t in hits:
        msp.delete_entity(t)
    bad = tmp_path / "bad.dxf"
    doc.saveas(bad)
    res = check_drainage_plan(bad, sp)
    assert not res.ok
    assert any("受納溝設計水位" in e for e in res.errors)


def test_profile_levels():
    prof = derive.derive_profile(_spec(RAW))
    got = [[lv.label, lv.el, lv.kind] for lv in prof.levels]
    assert got == EXPECTED["profile_levels"]
    # 滯洪池水位改畫在構造物（池體）內，不再浮在進流井上方
    (st,) = prof.structures
    assert [st.kind, st.label, st.x0_m, st.x1_m, st.bottom_el, st.top_el] == EXPECTED[
        "profile_structure"
    ][:6]
    assert [[m.label, m.el, m.kind] for m in st.marks] == EXPECTED["profile_structure"][6]


def test_outlet_profile_levels():
    prof = derive.derive_outlet_profile(_spec(RAW))
    assert [n.label for n in prof.nodes] == ["OW1", "OF1"]
    assert [[lv.label, lv.el, lv.kind] for lv in prof.levels] == EXPECTED["outlet_profile_levels"]
    want = EXPECTED["outlet_profile_structures"]
    assert [s.label for s in prof.structures] == list(want)
    for s in prof.structures:
        assert s.kind == want[s.label][0]
        assert [s.x0_m, s.x1_m, s.bottom_el, s.top_el] == pytest.approx(want[s.label][1:], abs=1e-4)


def test_gravity_rejects_pump_outlet_field():
    def fn(raw):
        _gravity(raw)
        raw["storage"]["pump_outlet_invert_el"] = 119.9

    with pytest.raises(ValidationError, match="抽水機欄位"):
        _spec(RAW, fn)


def test_derived_calc_pump(tmp_path):
    from draftagent.hydro.taoyuan import compute
    from draftagent.verify.drainage_calc_check import check

    calc = derive.derive_calc(_spec(RAW))
    assert calc.release_mode == "pump" and calc.outlet is None
    r = compute(calc)
    assert r["outlet"]["type"] == "pump"
    assert r["outlet"]["Q_cms"]["value"] == pytest.approx(0.022)

    def run(c):
        cs, rp = tmp_path / "c.json", tmp_path / "r.json"
        cs.write_text(c.model_dump_json(), encoding="utf-8")
        rp.write_text(json.dumps(compute(c), ensure_ascii=False), encoding="utf-8")
        return check(cs, rp)

    res = run(calc)
    assert res.ok, res.render()
    assert any("抽水排放" in w for w in res.warnings)
    res = run(calc.model_copy(update={"pump_q_cms": 0.030}))
    assert not res.ok
    assert any(e.startswith("抽水排放量") for e in res.errors)


# ---------------------------------------------------------------- schema


def test_schema_pump_needs_levels():
    with pytest.raises(ValidationError, match="pump_start_el"):
        _spec(RAW, lambda r: r["storage"].pop("pump_start_el"))


def test_schema_backwater_needs_note():
    with pytest.raises(ValidationError, match="backwater_note"):
        _spec(RAW, lambda r: r["storage"].update(allow_backwater=True))


def test_schema_gravity_needs_orifice():
    with pytest.raises(ValidationError, match=r"calc\.outlet"):
        _spec(RAW, lambda r: r["storage"].update(drain_mode="gravity_pump"))
