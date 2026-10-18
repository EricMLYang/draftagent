"""整套排水圖（drainage-plan）：一個模型 → 平面 / 主幹與出水側縱斷面 / 詳圖 / 計算，跨圖一致性。

- 完整 build 全綠（含 detail_check、sheetset_check、standards_check）
- 衍生詳圖的高程 / 容量 = 模型與計算結果（expected.json details）
- 故意錯：詳圖放流井井底文字 +0.10 m、平面圖管段坡度文字被改、兩張圖同圖號 → sheetset_check 紅
- 縱斷面構造物文字缺漏 / 數值不符 → profile_check 紅
"""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
from pathlib import Path

import ezdxf
import pytest

from draftagent.build import build
from draftagent.drainage import derive
from draftagent.drainage.model import DrainageNetworkSpec
from draftagent.kinds.drainage_plan import render
from draftagent.verify.detail_check import check_detail
from draftagent.verify.profile_check import check_profile
from draftagent.verify.sheetset_check import check_sheetset

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "tests" / "level2" / "drainage-plan-ty-1"
SPEC = CASE / "input" / "spec.json"
RAW = json.loads(SPEC.read_text(encoding="utf-8"))
EXPECTED = json.loads((CASE / "expected" / "expected.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def spec() -> DrainageNetworkSpec:
    return DrainageNetworkSpec.from_json(SPEC)


@pytest.fixture(scope="module")
def arts(tmp_path_factory, spec) -> dict[str, Path]:
    return render(spec, tmp_path_factory.mktemp("set"))


def _copy(arts: dict[str, Path], tmp_path: Path) -> dict[str, Path]:
    out = {}
    for k, p in arts.items():
        q = tmp_path / p.name
        shutil.copy(p, q)
        out[k] = q
    return out


def _edit_text(path: Path, fn) -> None:
    doc = ezdxf.readfile(str(path))
    n = 0
    for layout in doc.layouts:
        for e in layout.query("TEXT"):
            new = fn(e.dxf.text)
            if new is not None and new != e.dxf.text:
                e.dxf.text = new
                n += 1
    assert n, "沒有改到任何文字"
    doc.saveas(path)


def _errors(res) -> str:
    return "\n".join(res.errors)


# ---------------------------------------------------------------- green


def test_full_build_green(tmp_path):
    r = build(SPEC, out=tmp_path, preview=False)
    assert r.ok, str(r)
    names = [v.name for v in r.results]
    for n in (
        "drainage_plan_check",
        "profile_check",
        "profile_check[outlet]",
        "drainage_calc_check",
        "detail_check",
        "sheetset_check",
        "standards_check",
    ):
        assert n in names
    for key in ("profile_outlet_dxf", "detail_dxf", "detail_spec", "result"):
        assert r.artifacts[key].exists()
    ss = next(v for v in r.results if v.name == "sheetset_check")
    assert ss.metrics["elements_compared"] >= 20
    sheets = ss.metrics["sheets"]
    assert [s.split(":")[0] for s in sheets] == ["D-06", "D-10", "D-07", "D-07-2", "D-09"]


def test_derived_details_from_model(spec):
    det = derive.derive_details(spec)
    by = {d.id: d for d in det.details}
    assert list(by) == list(EXPECTED["details"])
    for did, exp in EXPECTED["details"].items():
        d = by[did]
        assert (d.template, d.section_code, d.title, d.scale) == (
            exp["template"],
            exp["section_code"],
            exp["title"],
            exp["scale"],
        )
    raft, e = by["RAFT"], EXPECTED["details"]["RAFT"]
    assert raft.bottom_el == e["els"]["bottom_el"] and raft.top_el() == e["els"]["top_el"]
    assert raft.storage_level_el == e["els"]["storage_level_el"]
    assert raft.overflow_flood_el is None  # 溢流在進流井 → 池內不畫溢流時最高水位
    assert raft.inflow_pipe.invert_el == e["els"]["inflow"]
    assert (raft.pump.start_el, raft.pump.stop_el) == (
        e["els"]["pump_start"],
        e["els"]["pump_stop"],
    )
    assert raft.orifice is None and raft.overflow_el is None  # 抽水型；溢流在進流井
    for k in ("n_cells", "cell_width", "beam_width", "pit_depth", "effective_depth"):
        assert getattr(raft, k) == e[k], k
    assert raft.pump.sump_width == e["sump_width"]
    ow, e = by["OW"], EXPECTED["details"]["OW"]["els"]
    assert (ow.top_el, ow.bottom_el) == (e["top_el"], e["bottom_el"])
    assert ow.pump_inflow.invert_el == e["pump_inflow"]
    assert ow.overflow_inflow.invert_el == e["overflow_inflow"]
    assert (ow.outlet_pipe.invert_el, ow.outlet_pipe_end_el) == (
        e["outlet_pipe"],
        e["outlet_pipe_end"],
    )
    assert (ow.ditch.bottom_el, ow.ditch.top_el, ow.ditch.design_wl_el) == (
        e["ditch_bottom"],
        e["ditch_top"],
        e["design_wl"],
    )
    assert (
        ow.single_chamber
        and ow.outlet_pipe_length == EXPECTED["details"]["OW"]["outlet_pipe_length"]
    )
    of = by["OF"]
    assert of.pipe.invert_el == EXPECTED["details"]["OF"]["els"]["pipe"]
    # 銘牌容量 = 計算書 provided（與模型重算同值）
    assert by["NP"].capacity_m3 == EXPECTED["details"]["NP"]["capacity_m3"]
    assert by["NP"].capacity_m3 == pytest.approx(EXPECTED["storage_volume_m3"])
    assert det.sheet.title_block.drawing_no == EXPECTED["drawing_nos"]["detail"]


def test_capacity_follows_result(spec):
    det = derive.derive_details(spec, {"storage_provided": {"provided_m3": {"value": 90.123}}})
    assert next(d for d in det.details if d.id == "NP").capacity_m3 == 90.12


def test_sheetset_cli(arts, tmp_path):
    a = _copy(arts, tmp_path)
    shutil.copy(SPEC, tmp_path / "spec.json")
    r = subprocess.run(
        [sys.executable, "-m", "draftagent.verify.sheetset_check", str(tmp_path), "--json"],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads(r.stdout)["ok"] is True
    assert a


# ---------------------------------------------------------------- 故意錯：跨圖


def test_detail_outlet_well_bottom_changed_is_red(arts, spec, tmp_path):
    a = _copy(arts, tmp_path)
    _edit_text(
        a["detail_dxf"],
        lambda s: s.replace("井底 EL=119.66", "井底 EL=119.76") if s.startswith("井底") else None,
    )
    res = check_sheetset(a, spec)
    assert not res.ok
    msg = _errors(res)
    assert "元素 OW1 底 EL" in msg
    assert "119.660" in msg and "119.760" in msg
    assert "詳圖（D-09）" in msg and "平面設計圖" in msg
    # detail_check 現在依「名稱…EL=」標籤比對（稽核 #13），單張詳圖也抓得到
    assert not check_detail(a["detail_dxf"], a["detail_spec"]).ok


def test_plan_slope_text_changed_is_red(arts, spec, tmp_path):
    a = _copy(arts, tmp_path)
    _edit_text(a["dxf"], lambda s: s.replace("S=1.74%", "S=2.74%") if s.startswith("L1 ") else None)
    res = check_sheetset(a, spec)
    assert not res.ok
    msg = _errors(res)
    assert "元素 L1 坡度 S %" in msg and "2.74%" in msg and "1.74%" in msg
    assert "縱斷面（D-07）" in msg


def test_duplicate_drawing_no_is_red(arts, spec, tmp_path):
    a = _copy(arts, tmp_path)
    doc = ezdxf.readfile(str(a["profile_outlet_dxf"]))
    for ins in doc.modelspace().query("INSERT"):
        for att in ins.attribs:
            if att.dxf.tag == "DWG_NO":
                att.dxf.text = "D-07"
    doc.saveas(a["profile_outlet_dxf"])
    res = check_sheetset(a, spec)
    assert not res.ok
    assert "圖號「D-07」重複" in _errors(res)


def test_wrong_title_is_red(arts, spec, tmp_path):
    a = _copy(arts, tmp_path)
    doc = ezdxf.readfile(str(a["detail_dxf"]))
    for ins in doc.modelspace().query("INSERT"):
        for att in ins.attribs:
            if att.dxf.tag == "TITLE":
                att.dxf.text = "排水詳圖"
    doc.saveas(a["detail_dxf"])
    res = check_sheetset(a, spec)
    assert "≠ 統一圖名「排水系統各部詳圖」" in _errors(res)


def test_storage_volume_mismatch_with_result_is_red(arts, spec, tmp_path):
    a = _copy(arts, tmp_path)
    r = json.loads(a["result"].read_text(encoding="utf-8"))
    r["storage_provided"]["provided_m3"]["value"] = 80.0
    a["result"].write_text(json.dumps(r, ensure_ascii=False), encoding="utf-8")
    res = check_sheetset(a, spec)
    assert not res.ok
    assert "元素 storage 貯集量" in _errors(res)


# ---------------------------------------------------------------- 縱斷面構造物


def test_outlet_profile_structures_checked(arts, tmp_path):
    a = _copy(arts, tmp_path)
    assert check_profile(a["profile_outlet_dxf"], a["profile_outlet_spec"]).ok
    _edit_text(
        a["profile_outlet_dxf"],
        lambda s: "受納溝設計水位 EL=119.70" if s == "受納溝設計水位 EL=119.60" else None,
    )
    res = check_profile(a["profile_outlet_dxf"], a["profile_outlet_spec"])
    assert not res.ok
    assert "受納溝設計水位 EL=119.70」≠ spec 119.60" in _errors(res)


def test_profile_structure_text_missing(arts, tmp_path):
    a = _copy(arts, tmp_path)
    _edit_text(a["profile_dxf"], lambda s: "池" if s.startswith("筏基滯洪底 EL=") else None)
    res = check_profile(a["profile_dxf"], a["profile_spec"])
    assert not res.ok
    assert "缺少「筏基滯洪底 EL=116.00」" in _errors(res)


def test_profile_structure_water_outside_pool(spec, tmp_path):
    from draftagent.render.profile import write_profile

    prof = derive.derive_profile(spec)
    st = prof.structures[0]
    bad = st.model_copy(update={"marks": [*st.marks, st.marks[0].model_copy(update={"el": 117.5})]})
    prof = prof.model_copy(update={"structures": [bad]})
    sp = tmp_path / "p.json"
    sp.write_text(prof.model_dump_json(), encoding="utf-8")
    res = check_profile(write_profile(prof, tmp_path / "p.dxf"), sp)
    assert "不在池底 116.00 與池頂 117.20 之間" in _errors(res)


# ---------------------------------------------------------------- 重力型變體也能整套產出


def test_gravity_variant_full_set(tmp_path):
    raw = copy.deepcopy(RAW)
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
        inflow_invert_el=119.86,
    )
    raw["calc"]["outlet"] = {"type": "circ_orifice", "diameter_m": 0.1, "design_head_m": 0.15}
    sp = DrainageNetworkSpec.model_validate(raw)
    det = derive.derive_details(sp)
    raft = next(d for d in det.details if d.template == "raft_detention")
    ow = next(d for d in det.details if d.template == "outlet_well")
    assert raft.pump is None and raft.orifice.center_el == 119.75
    assert ow.inflow_pipe.invert_el == raft.outflow_invert_el() >= ow.bottom_el
    r = build(sp, out=tmp_path, preview=False)
    for v in r.results:
        if v.name in (
            "sheetset_check",
            "profile_check",
            "profile_check[outlet]",
            "standards_check",
        ):
            assert v.ok, v.render()


# ---------------------------------------------------------------- 重力型、池內溢流（水位語意回歸）


def _gravity_internal_overflow(raw: dict) -> dict:
    """ty-1 的重力型、池內溢流變體（合成）：受納溝整體降 1.0 m 讓筏基可重力排放。

    設計滿水位 = 池底 118.70 + 有效水深 1.00 = 119.70 = 溢流口；溢流時最高水位 = 119.70 + 堰頂水頭 0.15
    = 119.85 ≤ 坑頂 119.90，且 ≤ 進流井底 119.88（無迴水）；孔口底 118.70 ≥ 放流井底 118.66 ≥ 受納設計水位。
    舊模板語意（孔口 < 溢流口 < 設計最高水位）下，此設計衍生的詳圖必被 detail_check 擋下。
    """
    raw = copy.deepcopy(raw)
    for p in raw["existing_drains"][0]["points"]:
        p["invert_el"] = round(p["invert_el"] - 1.0, 2)
        p["top_el"] = round(p["top_el"] - 1.0, 2)
    nodes = {n["id"]: n for n in raw["nodes"]}
    nodes["OW1"]["invert_el"] = 118.66
    nodes["OF1"].update(invert_el=118.62, top_el=118.90)
    st = raw["storage"]
    for k in (
        "pump_start_el",
        "pump_stop_el",
        "pump_q_cms",
        "pump_outlet_invert_el",
        "overflow_pipe_invert_el",
    ):
        st.pop(k)
    st.update(
        drain_mode="gravity",
        overflow_at="storage",
        bottom_el=118.70,
        max_water_el=119.70,
        top_el=119.90,
        orifice_invert_el=118.70,
        overflow_el=119.70,
        inflow_invert_el=119.70,
    )
    raw["calc"]["outlet"] = {"type": "circ_orifice", "diameter_m": 0.105, "design_head_m": 1.0}
    return raw


def test_gravity_internal_overflow_full_build_green(tmp_path):
    raw = _gravity_internal_overflow(RAW)
    p = tmp_path / "spec.json"
    p.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    r = build(p, out=tmp_path / "runs", preview=False)
    assert r.ok, str(r)
    assert len(r.results) == 8
    det = json.loads(r.artifacts["detail_spec"].read_text(encoding="utf-8"))
    raft = next(d for d in det["details"] if d["template"] == "raft_detention")
    assert (raft["storage_level_el"], raft["overflow_el"], raft["overflow_flood_el"]) == (119.7, 119.7, 119.85)
    ss = next(v for v in r.results if v.name == "sheetset_check")
    assert ss.ok and ss.metrics["elements_compared"] >= 20
    # 溢流時最高水位出現在平面圖附註、主幹縱斷面池體與詳圖，跨圖一致
    for key in ("dxf", "profile_dxf", "detail_dxf"):
        doc = ezdxf.readfile(str(r.artifacts[key]))
        texts = [e.dxf.text for lo in doc.layouts for e in lo.query("TEXT")]
        assert any("溢流時最高水位" in t and "119.85" in t for t in texts), key
        assert not any("設計最高水位" in t for t in texts), key


def test_gravity_internal_overflow_detail_red_when_overflow_below_storage_level(tmp_path):
    raw = _gravity_internal_overflow(RAW)
    raw["storage"]["overflow_el"] = 119.60  # < 設計滿水位 119.70
    det = derive.derive_details(DrainageNetworkSpec.model_validate(raw))
    p = tmp_path / "detail_spec.json"
    p.write_text(det.model_dump_json(), encoding="utf-8")
    from draftagent.render.detail import write_detail_sheet

    res = check_detail(write_detail_sheet(det, tmp_path / "d.dxf"), p)
    assert "設計滿水位 EL=119.70 高於溢流口 EL=119.60" in _errors(res)
