"""稽核回歸（2026-10-18 verifier false-negative 稽核 #2 plan、#9、#10、#17）。

每個測試以稽核情境代號命名；竄改後 verifier 必須紅燈。
"""

from __future__ import annotations

import json
from pathlib import Path

import ezdxf
import pytest
from pydantic import ValidationError

from draftagent.render.dxf import write_plan
from draftagent.schema.plan import PlanSpec
from draftagent.verify.dxf_check import check_dxf
from draftagent.verify.legibility_check import check_text_heights, check_text_overlaps
from draftagent.verify.standards_check import check_standards, min_print_height_mm

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "plan.json"


@pytest.fixture
def run(tmp_path):
    spec = PlanSpec.from_json(EXAMPLE)
    sp = tmp_path / "spec.json"
    sp.write_text(spec.model_dump_json(), encoding="utf-8")
    return write_plan(spec, tmp_path / "plan.dxf"), sp


def _tamper(run, fn, tmp_path):
    dxf, sp = run
    doc = ezdxf.readfile(dxf)
    fn(doc)
    out = tmp_path / "bad.dxf"
    doc.saveas(out)
    return check_dxf(out, spec_path=sp)


def _red(res, *keys):
    assert not res.ok, res.render()
    for k in keys:
        assert any(k in e for e in res.errors), (k, res.errors)


def test_baseline_green(run):
    dxf, sp = run
    res = check_dxf(dxf, spec_path=sp)
    assert res.ok, res.render()
    assert res.metrics["wall_symdiff_ratio"] < 1e-6
    assert res.metrics["text_too_small"] == 0


# ---------------------------------------------------------------- #2 顯示值


def _dims(fn):
    def g(doc):
        for dim in doc.modelspace().query("DIMENSION"):
            fn(dim)

    return g


def _rerender(attrs):
    def f(dim):
        ov = dim.override()
        for k, v in attrs.items():
            ov[k] = v
        ov.commit()
        ov.render()

    return f


def test_P6b_dimrnd_rerendered_red(run, tmp_path):
    _red(_tamper(run, _dims(_rerender({"dimrnd": 1000})), tmp_path), "dimrnd", "實際顯示 '4000'")


def test_P8b_dimpost_rerendered_red(run, tmp_path):
    _red(_tamper(run, _dims(_rerender({"dimpost": "<>0"})), tmp_path), "dimpost", "'45000'")


def test_P7_override_substring_red(run, tmp_path):
    def f(dim):
        dim.dxf.text = f"{dim.get_measurement():.0f}0"

    _red(_tamper(run, _dims(f), tmp_path), "文字覆寫為 '45000'")


def test_override_with_brackets_ok(run, tmp_path):
    def f(dim):
        dim.dxf.text = "<>"

    assert _tamper(run, _dims(f), tmp_path).ok


# ---------------------------------------------------------------- #9 字高


def test_P4_text_height_1mm_red(run, tmp_path):
    def f(doc):
        for t in doc.modelspace().query("TEXT"):
            t.dxf.height = 1

    _red(_tamper(run, f, tmp_path), "文字太小")


def test_P5b_dimtxt_rerendered_red(run, tmp_path):
    _red(_tamper(run, _dims(_rerender({"dimtxt": 0.5})), tmp_path), "文字太小", "DIMENSION")


def test_P16_spec_dim_text_height_red(tmp_path):
    data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    data["dimensions"]["text_height"] = 2  # model 2 mm @1/50 = 0.04 mm
    spec = PlanSpec.model_validate(data)
    sp = tmp_path / "s.json"
    sp.write_text(spec.model_dump_json(), encoding="utf-8")
    _red(check_dxf(write_plan(spec, tmp_path / "p.dxf"), spec_path=sp), "文字太小")


def test_print_scale_drives_text_height(tmp_path):
    data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    data.pop("print_scale")
    assert PlanSpec.model_validate(data).print_scale == 50
    data["print_scale"] = 100
    spec = PlanSpec.model_validate(data)
    doc = ezdxf.readfile(write_plan(spec, tmp_path / "p.dxf"))
    room = doc.modelspace().query("TEXT[layer=='A-TEXT']")[0]
    assert room.dxf.height == pytest.approx(2.5 * 100)
    assert not check_text_heights(doc, "Model", 2.0, model_scale=100)
    assert check_text_heights(doc, "Model", 2.0, model_scale=200)  # 同一張圖 1/200 印太小


def test_standards_check_text_height(run):
    dxf, _ = run
    assert min_print_height_mm() == 2.0
    assert check_standards(dxf, print_scale=50).ok
    res = check_standards(dxf, print_scale=500)
    assert not res.ok and any("文字太小" in e for e in res.errors)


def test_paperspace_and_viewport_heights():
    doc = ezdxf.new("R2010", setup=True)
    msp = doc.modelspace()
    doc.layers.add("T1")
    msp.add_text("model", height=0.5, dxfattribs={"layer": "T1"}).set_placement((100, 100))
    psp = doc.layouts.get("Layout1")
    psp.add_text("paper", height=1.5, dxfattribs={"layer": "T1"}).set_placement((10, 10))
    # 1 model 單位 = 5 圖紙 mm（m 圖 @1/200）：0.5 → 2.5 mm OK
    vp = psp.add_viewport(
        center=(150, 100), size=(100, 100), view_center_point=(100, 100), view_height=20
    )
    msgs = check_text_heights(doc, None, 2.0)
    assert len(msgs) == 1 and "paper" in msgs[0]
    vp.dxf.view_height = 100  # 1:1 → 0.5 mm
    assert any("model" in m for m in check_text_heights(doc, "Layout1", 2.0))
    vp.frozen_layers = ["T1"]  # 凍結後不出圖 → 不檢查
    assert not any("model" in m for m in check_text_heights(doc, "Layout1", 2.0))


# ---------------------------------------------------------------- #10 重疊


def test_D8_text_on_text_overlap_red(run, tmp_path):
    def f(doc):
        msp = doc.modelspace()
        t = msp.query("TEXT[layer=='A-TEXT']")[0]
        msp.add_text(
            "重疊字", height=t.dxf.height, dxfattribs={"layer": "A-TEXT", "style": "CJK"}
        ).set_placement(t.dxf.align_point, align=t.get_align_enum())

    _red(_tamper(run, f, tmp_path), "文字重疊")


def test_overlap_respects_viewport_freeze():
    doc = ezdxf.new("R2010", setup=True)
    msp = doc.modelspace()
    for n in ("LA", "LB"):
        doc.layers.add(n)
    msp.add_text("AAAA", height=2, dxfattribs={"layer": "LA"}).set_placement((100, 100))
    msp.add_text("BBBB", height=2, dxfattribs={"layer": "LB"}).set_placement((100.5, 100))
    errs, _ = check_text_overlaps(doc, "Model")
    assert errs
    assert not check_text_overlaps(doc, "Model", frozen_layers=["LB"])[0]
    psp = doc.layouts.get("Layout1")
    v1 = psp.add_viewport(
        center=(60, 60), size=(80, 80), view_center_point=(100, 100), view_height=80
    )
    v2 = psp.add_viewport(
        center=(200, 60), size=(80, 80), view_center_point=(100, 100), view_height=80
    )
    v1.frozen_layers = ["LB"]
    v2.frozen_layers = ["LA"]
    assert not check_text_overlaps(doc, "Layout1")[0]  # 不同 viewport 各自只看到一個
    v2.frozen_layers = []
    assert check_text_overlaps(doc, "Layout1")[0]  # v2 兩個都看得到


def test_text_on_line_warning():
    doc = ezdxf.new("R2010", setup=True)
    msp = doc.modelspace()
    doc.layers.add("A-WALL")
    msp.add_text("WALL", height=200).set_placement((0, 0))
    msp.add_line((-100, 50), (1000, 50), dxfattribs={"layer": "A-WALL"})
    errs, warns = check_text_overlaps(doc, "Model", line_layers=["A-WALL"])
    assert not errs and any("壓線" in w for w in warns)


# ---------------------------------------------------------------- #17 幾何


def _walls(fn):
    def f(doc):
        for e in doc.modelspace().query("LWPOLYLINE[layer=='A-WALL']"):
            e.set_points([fn(x, y) for x, y in e.get_points("xy")], format="xy")

    return f


def test_P1_wall_scaled_red(run, tmp_path):
    _red(_tamper(run, _walls(lambda x, y: (x * 0.9, y)), tmp_path), "牆幾何")


def test_P11_wall_thickness_50_red(run, tmp_path):
    def g(x, y):
        x = {150: 50, 4350: 4450}.get(round(x), x)
        y = {150: 50, 3150: 3250}.get(round(y), y)
        return x, y

    _red(_tamper(run, _walls(g), tmp_path), "牆幾何")


def test_P3_door_on_wrong_wall_red(run, tmp_path):
    def f(doc):
        for e in doc.modelspace().query("ARC LINE"):
            if e.dxf.layer == "A-DOOR":
                e.translate(-4000, 2000, 0)

    _red(_tamper(run, f, tmp_path), "鉸點")


def test_door_swing_flipped_red(run, tmp_path):
    def f(doc):
        for a in doc.modelspace().query("ARC[layer=='A-DOOR']"):
            a.dxf.start_angle, a.dxf.end_angle = a.dxf.start_angle + 180, a.dxf.end_angle + 180

    _red(_tamper(run, f, tmp_path), "A-DOOR ARC")


def test_P10_window_floating_red(run, tmp_path):
    def f(doc):
        for e in doc.modelspace():
            if e.dxf.layer == "A-WIND":
                e.translate(0, -1500, 0)

    _red(_tamper(run, f, tmp_path), "A-WIND")


def test_P2_room_text_outside_red(run, tmp_path):
    def f(doc):
        for t in doc.modelspace().query("TEXT[layer=='A-TEXT']"):
            t.dxf.insert = (20000, 20000)

    _red(_tamper(run, f, tmp_path), "房名")


def test_P14_overlapping_rooms_rejected():
    data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    data["rooms"].append(
        {"name": "臥室", "rect": {"x": 3000, "y": 0, "width": 3000, "depth": 3300}}
    )
    for o in data["openings"]:
        o["room"] = "書房"
    with pytest.raises(ValidationError, match="重疊"):
        PlanSpec.model_validate(data)
    data["rooms"][1]["rect"]["x"] = 4500  # 相鄰（共邊）可
    PlanSpec.model_validate(data)


def test_P17_overlapping_openings_rejected():
    data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    data["openings"].append({"type": "window", "side": "N", "offset": 1000, "width": 1800})
    with pytest.raises(ValidationError, match="重疊"):
        PlanSpec.model_validate(data)
    data["openings"][-1].update(offset=3150, width=600)  # 緊鄰不重疊可
    PlanSpec.model_validate(data)


def test_polygon_room_geometry_green(tmp_path):
    spec = PlanSpec.model_validate(
        {
            "wall_thickness": 150,
            "rooms": [
                {
                    "name": "L 形",
                    "polygon": [
                        [0, 0],
                        [5000, 0],
                        [5000, 2000],
                        [2000, 2000],
                        [2000, 4000],
                        [0, 4000],
                    ],
                }
            ],
            "openings": [
                {
                    "type": "door",
                    "edge": 0,
                    "offset": 1000,
                    "width": 900,
                    "swing": "out",
                    "hinge": "end",
                },
                {"type": "window", "edge": 1, "offset": 400, "width": 1200},
                {"type": "window", "edge": 3, "offset": 500, "width": 1000},
            ],
        }
    )
    sp = tmp_path / "s.json"
    sp.write_text(spec.model_dump_json(), encoding="utf-8")
    res = check_dxf(write_plan(spec, tmp_path / "l.dxf"), spec_path=sp)
    assert res.ok, res.render()
