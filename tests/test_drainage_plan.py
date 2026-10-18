"""Level 2 drainage-plan-ty-1：DrainageNetworkSpec → 平面設計圖 + 集水區分析圖 → drainage_plan_check。

含故意錯的 fixture：迴圈、兩個出流口、逆坡、標籤坡度被竄改、缺指北針、缺座標刻度、
平面設計圖 viewport 1/500、分區重疊、分區加總不符、節點 INV 屬性與 spec 不符。
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import ezdxf
import pytest
from pydantic import ValidationError

from draftagent.drainage import derive, network
from draftagent.drainage.model import DrainageNetworkSpec
from draftagent.render.drainage_plan import CATCH_LAYOUT, PLAN_LAYOUT, write_drainage_plan
from draftagent.verify.drainage_plan_check import check_drainage_plan

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "tests" / "level2" / "drainage-plan-ty-1"
SPEC = CASE / "input" / "spec.json"
RAW = json.loads(SPEC.read_text(encoding="utf-8"))
EXPECTED = json.loads((CASE / "expected" / "expected.json").read_text(encoding="utf-8"))
E0, N0 = 280500.0, 2768500.0


@pytest.fixture(scope="module")
def spec() -> DrainageNetworkSpec:
    return DrainageNetworkSpec.from_json(SPEC)


@pytest.fixture(scope="module")
def good_dxf(tmp_path_factory, spec) -> Path:
    return write_drainage_plan(spec, tmp_path_factory.mktemp("dp") / "drainage_plan.dxf")


def _errors(res) -> str:
    return "\n".join(res.errors)


def _mutate(dxf: Path, tmp_path: Path, fn) -> Path:
    doc = ezdxf.readfile(str(dxf))
    fn(doc)
    out = tmp_path / "bad.dxf"
    doc.saveas(out)
    return out


def _bad_spec(fn) -> DrainageNetworkSpec:
    raw = copy.deepcopy(RAW)
    fn(raw)
    return DrainageNetworkSpec.model_validate(raw)


def _render_check(tmp_path: Path, sp: DrainageNetworkSpec):
    p = write_drainage_plan(sp, tmp_path / "x.dxf")
    return check_drainage_plan(p, sp)


def _node(raw, nid):
    return next(n for n in raw["nodes"] if n["id"] == nid)


def _sub(raw, sid):
    return next(s for s in raw["subcatchments"] if s["id"] == sid)


def _rect(x0, y0, x1, y1):
    return [[E0 + x0, N0 + y0], [E0 + x1, N0 + y0], [E0 + x1, N0 + y1], [E0 + x0, N0 + y1]]


# ---------------------------------------------------------------- green


def test_good_plan_passes(good_dxf, spec):
    res = check_drainage_plan(good_dxf, SPEC)
    assert res.ok, res.render()
    assert res.metrics["insunits"] == EXPECTED["insunits"]
    assert res.metrics["outfall"] == EXPECTED["outfall"]
    for lay, s in EXPECTED["scales"].items():
        assert res.metrics[f"{lay}_scale"] == s
        assert res.metrics[f"{lay}_ticks"]["E"] >= 2 and res.metrics[f"{lay}_ticks"]["N"] >= 2
    assert res.metrics["site_area_m2"] == pytest.approx(EXPECTED["site_area_m2"])
    assert any("verified: false" in w for w in res.warnings)


def test_dxf_structure(good_dxf):
    doc = ezdxf.readfile(str(good_dxf))
    assert doc.header["$INSUNITS"] == 6
    for name in EXPECTED["layouts"]:
        assert name in doc.layouts
    texts = {e.dxf.text for e in doc.modelspace().query("TEXT")}
    for label in EXPECTED["link_labels"].values():
        assert label in texts
    # 平面註記圖層在集水區 viewport 凍結，反之亦然
    vp_plan = next(v for v in doc.layouts.get(PLAN_LAYOUT).query("VIEWPORT") if v.dxf.id != 1)
    vp_catch = next(v for v in doc.layouts.get(CATCH_LAYOUT).query("VIEWPORT") if v.dxf.id != 1)
    assert "C-CTCH-TEXT" in vp_plan.frozen_layers
    assert "C-STRM-TEXT" in vp_catch.frozen_layers


def test_network_numbers(spec):
    rep = network.check_network(spec)
    assert rep.ok, rep.errors
    assert rep.lengths == pytest.approx(EXPECTED["link_lengths_m"])
    assert {k: v * 100 for k, v in rep.slopes.items()} == pytest.approx(EXPECTED["link_slopes_pct"])
    assert rep.node_area_m2 == pytest.approx(EXPECTED["upstream_area_m2"])
    path = derive.profile_path(spec)
    assert path == EXPECTED["profile_path"]
    assert network.station_texts(network.stationing(spec, path)) == EXPECTED["stations_text"]
    assert network.stationing(spec, path, origin="outfall") == pytest.approx([39.0, 16.0, 0.0])


def test_derived_profile_matches_plan(spec, tmp_path):
    from draftagent.render.profile import write_profile
    from draftagent.verify.profile_check import check_profile

    prof = derive.derive_profile(spec)
    assert [n.label for n in prof.nodes] == EXPECTED["profile_path"]
    assert prof.lengths() == pytest.approx([EXPECTED["link_lengths_m"][k] for k in ("L1", "L2")])
    assert prof.slopes_pct() == pytest.approx(
        [EXPECTED["link_slopes_pct"][k] for k in ("L1", "L2")]
    )
    assert any(lv.label == "進水管L3" for lv in prof.levels)
    sp = tmp_path / "profile_spec.json"
    sp.write_text(prof.model_dump_json(), encoding="utf-8")
    res = check_profile(write_profile(prof, tmp_path / "profile.dxf"), sp)
    assert res.ok, res.render()


def test_derived_calc(spec, tmp_path):
    from draftagent.hydro.taoyuan import compute
    from draftagent.verify.drainage_calc_check import check

    calc = derive.derive_calc(spec)
    assert calc.site_area_m2 == pytest.approx(EXPECTED["site_area_m2"])
    assert {s.name: s.area_m2 for s in calc.subareas} == pytest.approx(
        EXPECTED["subcatchment_areas_m2"]
    )
    assert [p.name for p in calc.pipes] == ["L1", "L2", "L3"]  # L4 在滯洪出流端以下
    assert {p.name: p.design_area_m2 for p in calc.pipes} == pytest.approx(
        {"L1": 320.0, "L2": 320.0, "L3": 424.0}
    )
    r = compute(calc)
    assert r["storage_provided"]["provided_m3"]["value"] == pytest.approx(
        EXPECTED["storage_volume_m3"]
    )
    assert r["storage_required"]["required_m3"]["value"] == pytest.approx(
        EXPECTED["storage_required_m3"]
    )
    cs, rp = tmp_path / "calc.json", tmp_path / "result.json"
    cs.write_text(calc.model_dump_json(), encoding="utf-8")
    rp.write_text(json.dumps(r, ensure_ascii=False), encoding="utf-8")
    res = check(cs, rp)
    assert res.ok, res.render()


def test_quantities(spec):
    assert [list(r.cells()) for r in derive.quantities(spec)] == EXPECTED["quantities"]


def test_kind_registered_and_green(tmp_path):
    from draftagent import registry

    k = registry.get("drainage-plan")
    assert k.spec is DrainageNetworkSpec
    sp = k.spec.from_json(k.example)
    arts = k.render(sp, tmp_path)
    for key in ("dxf", "profile_dxf", "profile_spec", "calc_spec", "result"):
        assert arts[key].exists()
    for v in k.verifiers:
        res = v(sp, arts)
        assert res.ok, res.render()


def test_cli(good_dxf):
    r = subprocess.run(
        [
            sys.executable,
            "-m",
            "draftagent.verify.drainage_plan_check",
            str(good_dxf),
            "--spec",
            str(SPEC),
            "--json",
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads(r.stdout)["ok"] is True


# ---------------------------------------------------------------- schema


def test_schema_rejects_bad_material():
    with pytest.raises(ValidationError, match="斷面"):
        _bad_spec(lambda raw: raw["links"][0].update(material="rc_pipe_lt_600"))


def test_schema_rejects_lonlat():
    with pytest.raises(ValidationError, match="TWD97"):
        _bad_spec(lambda raw: _node(raw, "CB1").update(x=121.3, y=24.99))


def test_schema_rejects_plan_scale_over_limit():
    with pytest.raises(ValidationError, match="S≧1/300"):
        _bad_spec(lambda raw: raw["scales"].update(plan=500))


def test_schema_forbids_computed_fields():
    with pytest.raises(ValidationError):
        _bad_spec(lambda raw: raw["links"][0].update(slope=0.02))
    with pytest.raises(ValidationError):
        _bad_spec(lambda raw: raw["subcatchments"][0].update(area_m2=572.0))


# ---------------------------------------------------------------- red：拓樸


def test_loop_fails(tmp_path):
    def f(raw):
        raw["links"].append(
            {
                "id": "L5",
                "from": "SI1",
                "to": "MH1",
                "section": {"type": "pipe", "d": 0.2},
                "material": "plastic_frp_pipe",
                "n_override": 0.013,
                "pump": True,
            }
        )

    res = _render_check(tmp_path, _bad_spec(f))
    assert not res.ok
    assert "迴圈" in _errors(res)


def test_two_outfalls_fail(tmp_path):
    def f(raw):
        raw["nodes"].append(
            {
                "id": "OF2",
                "kind": "outfall",
                "x": E0 + 41.0,
                "y": N0 - 1.0,
                "ground_el": 120.45,
                "invert_el": 119.38,
            }
        )
        raw["links"].append(
            {
                "id": "L5",
                "from": "CB2",
                "to": "OF2",
                "section": {"type": "pipe", "d": 0.2},
                "material": "plastic_frp_pipe",
                "n_override": 0.013,
            }
        )

    res = _render_check(tmp_path, _bad_spec(f))
    assert not res.ok
    assert "恰好 1 個" in _errors(res)


def test_adverse_slope_fails(tmp_path):
    res = _render_check(tmp_path, _bad_spec(lambda raw: _node(raw, "MH1").update(invert_el=119.70)))
    assert not res.ok
    assert "逆坡：L2" in _errors(res)


# ---------------------------------------------------------------- red：圖面竄改


def test_tampered_slope_label_fails(good_dxf, tmp_path):
    def f(doc):
        for t in doc.modelspace().query("TEXT"):
            if t.dxf.text.startswith("L2 "):
                t.dxf.text = t.dxf.text.replace("S=0.75%", "S=1.50%")

    res = check_drainage_plan(_mutate(good_dxf, tmp_path, f), SPEC)
    assert not res.ok
    assert "管段 L2 坡度" in _errors(res)


def test_missing_north_arrow_fails(good_dxf, tmp_path):
    def f(doc):
        psp = doc.layouts.get(PLAN_LAYOUT)
        for i in list(psp.query("INSERT")):
            if i.dxf.name == "NORTH-ARROW":
                psp.delete_entity(i)

    res = check_drainage_plan(_mutate(good_dxf, tmp_path, f), SPEC)
    assert not res.ok
    assert "「平面設計圖」缺指北針" in _errors(res)


def test_missing_grid_ticks_fails(good_dxf, tmp_path):
    def f(doc):
        psp = doc.layouts.get(CATCH_LAYOUT)
        for t in list(psp.query("TEXT")):
            if t.dxf.text.startswith("N "):
                psp.delete_entity(t)

    res = check_drainage_plan(_mutate(good_dxf, tmp_path, f), SPEC)
    assert not res.ok
    assert "「集水區分析圖」座標刻度不足" in _errors(res)


def test_plan_viewport_1_500_fails(good_dxf, tmp_path):
    def f(doc):
        vp = next(v for v in doc.layouts.get(PLAN_LAYOUT).query("VIEWPORT") if v.dxf.id != 1)
        vp.dxf.view_height = vp.dxf.height * 500 / 1000  # 1/500

    res = check_drainage_plan(_mutate(good_dxf, tmp_path, f), SPEC)
    assert not res.ok
    err = _errors(res)
    assert "1/500" in err and "S≧1/300" in err


def test_node_inv_attrib_mismatch_fails(good_dxf, tmp_path):
    def f(doc):
        for i in doc.modelspace().query("INSERT"):
            att = {a.dxf.tag: a for a in i.attribs}
            if "ID" in att and att["ID"].dxf.text == "MH1":
                att["INV"].dxf.text = "119.50"

    res = check_drainage_plan(_mutate(good_dxf, tmp_path, f), SPEC)
    assert not res.ok
    assert "節點 MH1 屬性 INV=119.50" in _errors(res)


def test_layer_zero_fails(good_dxf, tmp_path):
    res = check_drainage_plan(
        _mutate(good_dxf, tmp_path, lambda d: d.modelspace().add_line((E0, N0), (E0 + 1, N0))), SPEC
    )
    assert not res.ok
    assert "0 層" in _errors(res)


# ---------------------------------------------------------------- red：集水分區


def test_overlapping_subcatchments_fail(tmp_path):
    res = _render_check(
        tmp_path, _bad_spec(lambda raw: _sub(raw, "A4").update(polygon=_rect(8, 0, 34, 16)))
    )
    assert not res.ok
    assert "A1 與 A4 重疊" in _errors(res)


def test_subcatchment_total_mismatch_fails(tmp_path):
    def f(raw):
        _sub(raw, "A2").update(polygon=_rect(0, 0, 7, 40))

    res = _render_check(tmp_path, _bad_spec(f))
    assert not res.ok
    assert "集水分區面積合計" in _errors(res)
