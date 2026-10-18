"""Level 1-1：PlanSpec → DXF → dxf_check。含故意錯的 fixture，確認 verifier 會抓到。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import ezdxf
import pytest
from ezdxf import units
from pydantic import ValidationError

from draftagent.render.dxf import render_plan, write_plan
from draftagent.schema.plan import PlanSpec
from draftagent.verify.dxf_check import check_dxf

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "tests" / "level1" / "1-1"
SPEC = CASE / "input" / "spec.json"
EXPECTED = json.loads((CASE / "expected" / "expected.json").read_text(encoding="utf-8"))


@pytest.fixture
def spec() -> PlanSpec:
    return PlanSpec.from_json(SPEC)


@pytest.fixture
def good_dxf(tmp_path, spec) -> Path:
    return write_plan(spec, tmp_path / "plan.dxf")


def _bad_doc(
    *,
    layers=("A-WALL", "A-DOOR", "A-DIMS"),
    dim=6000.0,
    insunits=units.MM,
    closed=True,
    wall_layer="A-WALL",
    dimlfac=1.0,
):
    """用 ezdxf 直接造一張（可能是壞的）6×4 平面圖。"""
    doc = ezdxf.new("R2010", setup=True)
    doc.units = insunits
    for name in layers:
        doc.layers.add(name)
    msp = doc.modelspace()
    msp.add_lwpolyline(
        [(0, 0), (6000, 0), (6000, 4000), (0, 4000)], close=closed, dxfattribs={"layer": wall_layer}
    )
    dim_layer = "A-DIMS" if "A-DIMS" in layers else "0"
    msp.add_linear_dim(
        base=(0, -800),
        p1=(0, 0),
        p2=(dim, 0),
        dimstyle="EZDXF",
        override={"dimlfac": dimlfac},
        dxfattribs={"layer": dim_layer},
    ).render()
    return doc


def _check(doc, tmp_path, **kw):
    path = tmp_path / "bad.dxf"
    doc.saveas(path)
    return check_dxf(path, **kw)


# ------------------------------------------------------------------ green


def test_good_spec_passes(good_dxf):
    res = check_dxf(good_dxf, expect_dims=EXPECTED["dimensions"], spec_path=SPEC)
    assert res.ok, res.render()
    assert res.metrics["insunits"] == EXPECTED["insunits"]
    assert set(EXPECTED["required_layers"]) <= set(res.metrics["layers"])
    assert sorted(res.metrics["dimensions"]) == sorted(EXPECTED["dimensions"])
    assert res.metrics["doors"] == f"{EXPECTED['doors']}/{EXPECTED['doors']}"


def test_render_geometry(spec):
    msp = render_plan(spec).modelspace()
    assert not [e for e in msp if e.dxf.layer == "0"]
    walls = msp.query('LWPOLYLINE[layer=="A-WALL"]')
    assert len(walls) == 1 and walls[0].is_closed  # 一個門 → 一條 C 形封閉輪廓
    xs = sorted({round(p[0]) for p in walls[0].get_points("xy")})
    assert {0, 200, 2550, 3450, 5800, 6000} == set(xs)
    assert len(msp.query('ARC[layer=="A-DOOR"]')) == 1
    assert len(msp.query("DIMENSION")) == 2


def test_windows_and_polygon_room(tmp_path):
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
                {"type": "door", "edge": 0, "offset": 1000, "width": 900, "swing": "out"},
                {"type": "window", "edge": 1, "offset": 400, "width": 1200},
                {"type": "window", "edge": 5, "offset": 1000, "width": 1500},
            ],
        }
    )
    path = write_plan(spec, tmp_path / "l.dxf")
    res = check_dxf(path, expect_dims=[5000, 4000], spec_path=None)
    assert res.ok, res.render()
    spec_path = tmp_path / "l.json"
    spec_path.write_text(spec.model_dump_json(), encoding="utf-8")
    res = check_dxf(path, spec_path=spec_path)
    assert res.ok, res.render()
    assert res.metrics["windows"] == "2/2"


def test_cli_roundtrip(tmp_path):
    out = tmp_path / "cli.dxf"
    subprocess.run(
        [sys.executable, str(ROOT / "tools" / "gen_dxf.py"), str(SPEC), "-o", str(out)], check=True
    )
    r = subprocess.run(
        [
            sys.executable,
            "-m",
            "draftagent.verify.dxf_check",
            str(out),
            "--expect-dim",
            "6000",
            "--expect-dim",
            "4000",
            "--spec",
            str(SPEC),
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout
    assert json.loads(r.stdout)["ok"] is True


# ------------------------------------------------------------------ red


def test_entity_on_layer_zero(tmp_path):
    doc = _bad_doc()
    doc.modelspace().add_line((0, 0), (1, 1))  # 預設 0 層
    res = _check(doc, tmp_path)
    assert not res.ok and any("0 層" in e for e in res.errors)


def test_wrong_dimension_value(tmp_path):
    res = _check(_bad_doc(dim=5000), tmp_path, expect_dims=[6000])
    assert not res.ok and any("6000" in e for e in res.errors)


def test_missing_door_layer(tmp_path):
    res = _check(_bad_doc(layers=("A-WALL", "A-DIMS")), tmp_path)
    assert not res.ok and any("A-DOOR" in e for e in res.errors)


def test_open_wall_polyline(tmp_path):
    res = _check(_bad_doc(closed=False), tmp_path)
    assert not res.ok and any("未封閉" in e for e in res.errors)


def test_wrong_units(tmp_path):
    res = _check(_bad_doc(insunits=units.M), tmp_path)
    assert not res.ok and any("INSUNITS" in e for e in res.errors)


def test_no_dimension(tmp_path):
    doc = _bad_doc()
    for e in list(doc.modelspace().query("DIMENSION")):
        doc.modelspace().delete_entity(e)
    res = _check(doc, tmp_path)
    assert not res.ok and any("DIMENSION" in e for e in res.errors)


def test_dimension_text_override(tmp_path):
    doc = _bad_doc()
    doc.modelspace().query("DIMENSION")[0].dxf.text = "5000"
    res = _check(doc, tmp_path, expect_dims=[6000])
    assert not res.ok and any("覆寫" in e for e in res.errors)


def test_dimension_scale_factor(tmp_path):
    # EZDXF 樣式預設 dimlfac=100：量測 6000 但畫面顯示 600000
    res = _check(_bad_doc(dimlfac=100), tmp_path, expect_dims=[6000])
    assert not res.ok and any("dimlfac" in e for e in res.errors)


def test_rendered_plan_displays_measured_value(good_dxf):
    res = check_dxf(good_dxf, expect_dims=EXPECTED["dimensions"], spec_path=SPEC)
    assert sorted(res.metrics["dimensions_displayed"]) == sorted(EXPECTED["dimensions"])


def test_spec_door_count_mismatch(tmp_path, good_dxf):
    data = json.loads(SPEC.read_text(encoding="utf-8"))
    data["openings"].append({"type": "door", "side": "N", "offset": 1000, "width": 900})
    data["rooms"].append({"name": "多一間", "rect": {"x": 8000, "width": 3000, "depth": 3000}})
    data["openings"][0]["room"] = data["openings"][1]["room"] = "房間"
    p = tmp_path / "spec2.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    res = check_dxf(good_dxf, spec_path=p)
    assert not res.ok
    assert any("門數" in e for e in res.errors)
    assert any("房間不符" in e for e in res.errors)


# ------------------------------------------------------------------ schema


def test_schema_rejects_extra_field():
    data = json.loads(SPEC.read_text(encoding="utf-8"))
    data["wall"] = 200
    with pytest.raises(ValidationError):
        PlanSpec.model_validate(data)


@pytest.mark.parametrize("offset,width", [(0, 900), (5500, 900), (100, 900)])
def test_schema_rejects_opening_outside_wall(offset, width):
    data = json.loads(SPEC.read_text(encoding="utf-8"))
    data["openings"][0].update(offset=offset, width=width)
    with pytest.raises(ValidationError):
        PlanSpec.model_validate(data)


def test_schema_side_requires_rect():
    with pytest.raises(ValidationError):
        PlanSpec.model_validate(
            {
                "wall_thickness": 200,
                "rooms": [{"name": "a", "polygon": [[0, 0], [3000, 0], [0, 3000]]}],
                "openings": [{"type": "door", "side": "S", "offset": 500, "width": 900}],
            }
        )
