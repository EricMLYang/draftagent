"""floor-plan 圖種（多房間平面圖 → DXF + IFC）：綠燈與故意錯（verifier 必須抓到）。不呼叫 LLM。"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import ezdxf
import pytest
from pydantic import ValidationError

from draftagent.build import build
from draftagent.render.floorplan import write_floorplan
from draftagent.schema.floorplan import FloorPlanSpec, door_placement, interior_wall_lines
from draftagent.verify.floorplan_check import (
    check_floorplan,
    check_floorplan_dxf,
    check_floorplan_ifc,
)

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "floor-plan.json"
ROOMS = [ROOT / "tests" / "level3" / f"rooms-{n}" / "input" / "spec.json" for n in (4, 7, 10)]



def _raw(path: Path = EXAMPLE) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _spec(raw: dict) -> FloorPlanSpec:
    return FloorPlanSpec.model_validate(raw)


def _errors(res) -> str:
    return "\n".join(res.errors)


def _room(raw: dict, name: str) -> dict:
    return next(r for r in raw["rooms"] if r["name"] == name)


# ---------------------------------------------------------------- 綠燈


@pytest.mark.parametrize("path", [EXAMPLE, *ROOMS], ids=lambda p: p.parent.parent.name)
def test_build_green(path, tmp_path):
    pytest.importorskip("ifcopenshell")
    res = build(path, out=tmp_path, preview=False)
    assert res.ok, str(res)
    assert {r.name for r in res.results} == {
        "floorplan_check",
        "floorplan_dxf_check",
        "floorplan_ifc_check",
        "standards_check",
        "units_check",
    }
    assert res.artifacts["dxf"].exists() and res.artifacts["ifc"].exists()


def test_shared_walls_merged_once():
    spec = _spec(_raw())
    lines = interior_wall_lines(spec)
    # 共用邊界只出現一次：任兩段內牆中心線不重疊
    from shapely.geometry import LineString

    segs = [LineString([s.start, s.end]) for s in lines]
    for i in range(len(segs)):
        for j in range(i + 1, len(segs)):
            assert segs[i].intersection(segs[j]).length < 1e-6
    res = check_floorplan(spec)
    assert res.ok, res.render()
    assert res.metrics["rooms"]["衛浴"]["area_m2"] == pytest.approx(5.26, abs=0.005)


def test_door_located_on_shared_wall_center():
    spec = _spec(_raw())
    pl = door_placement(spec, spec.door("D2"))  # 起居室 | 臥房：x = 5200 垂直牆，置中
    assert pl.a[0] == pytest.approx(5200) and pl.b[0] == pytest.approx(5200)
    assert (pl.a[1] + pl.b[1]) / 2 == pytest.approx((180 + 4000) / 2)


# ---------------------------------------------------------------- schema 結構錯誤


@pytest.mark.parametrize(
    ("mut", "msg"),
    [
        (lambda r: r.update(entrance="D2"), "outside"),
        (lambda r: r.update(entrance="D99"), "不是任何門"),
        (lambda r: r["doors"][1].update(between=["起居室", "倉庫"]), "不存在"),
        (lambda r: r["windows"][0].update(room="倉庫"), "不存在"),
        (lambda r: r["rooms"][1].update(name="起居室"), "重複"),
        (lambda r: r["doors"][1].update(swing_into="廚房"), "swing_into"),
    ],
)
def test_schema_rejects_structural_errors(mut, msg):
    raw = _raw()
    mut(raw)
    with pytest.raises(ValidationError, match=msg):
        _spec(raw)


# ---------------------------------------------------------------- floorplan_check（幾何 / 拓樸）


def test_overlap_is_red():
    raw = _raw()
    _room(raw, "臥房")["rect"]["x"] = 4800  # 往西伸進起居室
    _room(raw, "臥房")["rect"]["width"] = 4620
    res = check_floorplan(_spec(raw))
    assert not res.ok and "重疊" in _errors(res)


def test_room_outside_inner_and_gap_are_red():
    raw = _raw()
    _room(raw, "書房")["rect"]["depth"] = 3000  # 超出外牆內緣（壓進北側外牆）
    res = check_floorplan(_spec(raw))
    assert not res.ok and "超出室內範圍" in _errors(res)
    raw = _raw()
    _room(raw, "書房")["rect"]["depth"] = 2000  # 北側留空
    res = check_floorplan(_spec(raw))
    assert not res.ok and "未分配" in _errors(res)


def test_door_not_on_shared_wall_is_red():
    raw = _raw()
    raw["doors"][4]["between"] = ["書房", "廚房"]  # 兩房不相接
    res = check_floorplan(_spec(raw))
    assert not res.ok and "沒有共用牆" in _errors(res)


def test_door_too_wide_or_out_of_range_is_red():
    raw = _raw()
    raw["doors"][3]["width"] = 1500  # 衛浴|起居室 共用牆 1600 − 2 × 100 = 1400
    res = check_floorplan(_spec(raw))
    assert not res.ok and "牆段可用範圍" in _errors(res)
    raw = _raw()
    raw["doors"][1]["offset"] = 50  # 離牆端 < 牆厚
    assert "牆段可用範圍" in _errors(check_floorplan(_spec(raw)))


def test_overlapping_openings_on_same_wall_is_red():
    raw = _raw()
    raw["windows"][0]["offset"] = 3000  # 與入口門同一段南牆
    res = check_floorplan(_spec(raw))
    assert not res.ok and "同一牆段重疊" in _errors(res)


def test_unreachable_room_is_red():
    raw = _raw()
    raw["doors"] = [d for d in raw["doors"] if d["id"] != "D5"]  # 書房唯一的門
    res = check_floorplan(_spec(raw))
    assert not res.ok and "無法到達" in _errors(res) and "書房" in _errors(res)
    assert res.metrics["reachable"] is False


def test_habitable_room_without_window_is_red():
    raw = _raw()
    raw["windows"] = [w for w in raw["windows"] if w["room"] != "臥房"]
    res = check_floorplan(_spec(raw))
    assert not res.ok and "臥房" in _errors(res) and "對外窗" in _errors(res)
    # 非居室（廚房）沒窗不擋
    raw = _raw()
    raw["windows"] = [w for w in raw["windows"] if w["room"] != "廚房"]
    assert check_floorplan(_spec(raw)).ok


def test_window_on_interior_wall_is_impossible():
    raw = _raw()
    raw["windows"][0]["side"] = "N"  # 起居室北側是內牆
    res = check_floorplan(_spec(raw))
    assert not res.ok and "沒有朝 N" in _errors(res)


def test_corridors_must_connect():
    raw = _raw(ROOMS[1])  # rooms-7
    corr = _room(raw, "走廊")
    # 把走廊切成兩段，兩段之間不開門
    x, w = corr["rect"]["x"], corr["rect"]["width"]
    corr["rect"]["width"] = 3000
    raw["rooms"].append(
        {"name": "走廊2", "use": "走廊", "rect": {**corr["rect"], "x": x + 3000, "width": w - 3000}}
    )
    for d in raw["doors"]:
        if d["between"][0] in ("主臥室", "浴廁"):
            d["between"][1] = "走廊2"
    res = check_floorplan(_spec(raw))
    assert "未以門連通" in _errors(res)


def test_bathroom_only_via_bedroom_is_warning():
    spec = _spec(_raw(ROOMS[2]))  # rooms-10：主臥浴廁
    res = check_floorplan(spec)
    assert res.ok
    assert any("主臥浴廁" in w and "臥室" in w for w in res.warnings)


# ---------------------------------------------------------------- DXF 讀回


@pytest.fixture
def dxf(tmp_path):
    spec = _spec(_raw())
    p = write_floorplan(spec, tmp_path / "fp.dxf")
    return p, spec


def _mutate(path: Path, out: Path, fn) -> Path:
    doc = ezdxf.readfile(str(path))
    fn(doc)
    doc.saveas(out)
    return out


def test_dxf_green(dxf):
    res = check_floorplan_dxf(*dxf)
    assert res.ok, res.render()
    assert res.metrics["wall_lineweights"] == {"exterior": [50], "interior": [35]}


def test_dimension_dimpost_tamper_is_red(dxf, tmp_path):
    def fn(doc):
        for dim in doc.modelspace().query("DIMENSION"):
            ov = dim.override()
            ov["dimpost"] = "<>0"
            ov.commit()
            ov.render()

    res = check_floorplan_dxf(_mutate(dxf[0], tmp_path / "x.dxf", fn), dxf[1])
    assert not res.ok and "dimpost='<>0'" in _errors(res)


def test_chain_dimension_off_wall_is_red(dxf, tmp_path):
    def fn(doc):
        msp = doc.modelspace()
        dim = msp.add_linear_dim(
            base=(0, -2400), p1=(0, 0), p2=(3000, 0), angle=0,
            override={"dimlfac": 1, "dimtxt": 250}, dxfattribs={"layer": "A-DIMS"},
        )  # fmt: skip
        dim.render()

    res = check_floorplan_dxf(_mutate(dxf[0], tmp_path / "x.dxf", fn), dxf[1])
    assert not res.ok and "不在外緣或內牆中心線上" in _errors(res)


def test_text_overlap_is_red(dxf, tmp_path):
    def fn(doc):
        t = next(e for e in doc.modelspace().query("TEXT") if e.dxf.text == "書房")
        c = t.copy()
        c.dxf.text = "書房書房"
        doc.modelspace().add_entity(c)

    res = check_floorplan_dxf(_mutate(dxf[0], tmp_path / "x.dxf", fn), dxf[1])
    assert not res.ok and "文字重疊" in _errors(res)


def test_area_text_tamper_is_red(dxf, tmp_path):
    def fn(doc):
        t = next(e for e in doc.modelspace().query("TEXT") if e.dxf.text.endswith("m²"))
        t.dxf.text = "99.99 m²"

    res = check_floorplan_dxf(_mutate(dxf[0], tmp_path / "x.dxf", fn), dxf[1])
    assert not res.ok and "面積文字" in _errors(res)


def test_missing_door_arc_and_wall_shift_are_red(dxf, tmp_path):
    def drop_arc(doc):
        msp = doc.modelspace()
        msp.delete_entity(next(iter(msp.query("ARC"))))

    res = check_floorplan_dxf(_mutate(dxf[0], tmp_path / "a.dxf", drop_arc), dxf[1])
    assert not res.ok and "門數不符" in _errors(res)

    def shift_wall(doc):
        for e in doc.modelspace().query("LWPOLYLINE[layer=='A-WALL']"):
            if e.dxf.get("lineweight", -1) == 35:
                e.translate(300, 0, 0)

    res = check_floorplan_dxf(_mutate(dxf[0], tmp_path / "w.dxf", shift_wall), dxf[1])
    assert not res.ok and "牆幾何與 spec 不符" in _errors(res)


def test_equal_wall_lineweights_is_red(dxf, tmp_path):
    def fn(doc):
        for e in doc.modelspace().query("LWPOLYLINE[layer=='A-WALL']"):
            e.dxf.lineweight = -1  # 全部 BYLAYER

    res = check_floorplan_dxf(_mutate(dxf[0], tmp_path / "x.dxf", fn), dxf[1])
    assert not res.ok and "外牆線寬" in _errors(res)


# ---------------------------------------------------------------- IFC 讀回


@pytest.fixture
def ifc(tmp_path):
    pytest.importorskip("ifcopenshell")
    from draftagent.render.ifc import write_ifc

    spec = _spec(_raw())
    return write_ifc(spec, tmp_path / "fp.ifc"), spec


def _ifc_mutate(path: Path, out: Path, fn) -> Path:
    import ifcopenshell

    f = ifcopenshell.open(str(path))
    fn(f)
    f.write(str(out))
    return out


def test_ifc_green(ifc):
    res = check_floorplan_ifc(*ifc)
    assert res.ok, res.render()
    assert res.metrics["spaces"] == "5/5" and res.metrics["IfcDoor"] == "5/5"
    assert res.metrics["walls"]["external"] == 4


def test_ifc_missing_space_is_red(ifc, tmp_path):
    def fn(f):
        import ifcopenshell.api

        ifcopenshell.api.run("root.remove_product", f, product=f.by_type("IfcSpace")[0])

    res = check_floorplan_ifc(_ifc_mutate(ifc[0], tmp_path / "x.ifc", fn), ifc[1])
    assert not res.ok and "IfcSpace 數" in _errors(res)


def test_ifc_door_without_filling_is_red(ifc, tmp_path):
    def fn(f):
        f.remove(f.by_type("IfcRelFillsElement")[0])

    res = check_floorplan_ifc(_ifc_mutate(ifc[0], tmp_path / "x.ifc", fn), ifc[1])
    assert not res.ok and "IfcRelFillsElement" in _errors(res)


def test_ifc_wrong_units_and_area_are_red(ifc, tmp_path):
    def fn(f):
        for u in f.by_type("IfcSIUnit"):
            if u.UnitType == "LENGTHUNIT":
                u.Prefix = None
        import ifcopenshell.util.element as uel

        sp = f.by_type("IfcSpace")[0]
        q = uel.get_psets(sp, qtos_only=True)["Qto_SpaceBaseQuantities"]
        f.by_id(q["id"]).Quantities[0].AreaValue = 1.0

    res = check_floorplan_ifc(_ifc_mutate(ifc[0], tmp_path / "x.ifc", fn), ifc[1])
    errs = _errors(res)
    assert not res.ok and "MILLI METRE" in errs and "NetFloorArea" in errs


# ---------------------------------------------------------------- fixtures / 範例


def test_floorplan_fixtures_up_to_date():
    r = subprocess.run(
        [sys.executable, str(ROOT / "tools/make_fixtures_floorplan.py"), "--check"],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_level3_rooms_prompts_give_no_coordinates():
    for n in (4, 7, 10):
        d = ROOT / "tests" / "level3" / f"rooms-{n}"
        text = (d / "prompt.md").read_text(encoding="utf-8") + (d / "input" / "design.md").read_text(
            encoding="utf-8"
        )
        assert "rect" not in text and "polygon" not in text and "座標" not in text.replace("沒有座標", "")
        assert sorted(p.name for p in (d / "input").iterdir()) == ["design.md", "spec.json"]


def test_reference_specs_differ_from_example():
    ex = _raw()
    for p in ROOMS:
        r = _raw(p)
        assert r["outline"] != ex["outline"] and r["wall_thickness"] != ex["wall_thickness"]
        assert copy.deepcopy(r) != ex
