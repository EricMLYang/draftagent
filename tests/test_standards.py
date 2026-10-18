"""製圖標準（render/codes/layers.yaml、render/standards.py、verify/standards_check.py）。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import ezdxf
import pytest

from draftagent.render import detail, drainage_plan, dxf, profile, sheet
from draftagent.render.standards import (
    DEFAULT_CJK_FONT,
    FONT_ENV,
    WINDOWS_CJK_FONT,
    apply_layers,
    load_layers,
    load_standards,
    text_style,
)
from draftagent.verify.standards_check import check_standards

ROOT = Path(__file__).resolve().parents[1]
PLAN_SPEC = ROOT / "tests" / "level1" / "1-1" / "input" / "spec.json"
PROFILE_SPEC = ROOT / "tests" / "level2" / "profile-ty-1" / "input" / "spec.json"
DETAIL_SPEC = ROOT / "tests" / "level2" / "detail-ty-1" / "input" / "spec.json"

RENDER_LAYERS = {
    "plan": dxf.LAYERS,
    "profile": profile.LAYERS,
    "detail": detail.LAYERS,
    "sheet": sheet.SHEET_LAYERS,
    "drainage_plan": {**drainage_plan.MODEL_LAYERS, **drainage_plan.PAPER_LAYERS},
}


# ---------------------------------------------------------------- yaml


def test_yaml_loads_as_convention():
    std = load_standards()
    assert std.kind == "convention"
    assert "非台灣官方標準" in std.source
    layers = load_layers()
    assert layers["A-WALL"].lineweight == 50  # 建築平面圖牆線 0.50 mm
    assert layers["H-STRC"].lineweight == 50
    assert layers["X-FRAME"].lineweight == 70
    assert layers["X-VPORT"].plot is False
    for name, s in layers.items():
        assert name.startswith(f"{s.discipline}-")
        assert s.description


@pytest.mark.parametrize("kind", sorted(RENDER_LAYERS))
def test_yaml_covers_all_render_layers(kind):
    layers = load_layers()
    missing = [n for n in RENDER_LAYERS[kind] if n.upper() not in layers]
    assert not missing, f"{kind} 用到但 layers.yaml 沒有：{missing}"


@pytest.mark.parametrize("kind", sorted(RENDER_LAYERS))
def test_yaml_keeps_renderer_colors(kind):
    """yaml 顏色 / 線型與各 renderer 原本的圖層定義一致（向後相容）。"""
    layers = load_layers()
    for name, v in RENDER_LAYERS[kind].items():
        color, lt = v if isinstance(v, tuple) else (v, "Continuous")
        s = layers[name]
        assert s.color == color, name
        assert s.linetype == lt.upper(), name


def test_yaml_rejects_invalid_lineweight(tmp_path):
    p = tmp_path / "layers.yaml"
    p.write_text(
        "kind: convention\nlayers:\n  A-WALL: {description: x, color: 7, linetype: CONTINUOUS,"
        " lineweight: 45, plot: true, discipline: A}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="合法線寬"):
        load_standards(p)


# ---------------------------------------------------------------- apply_layers / text_style


def test_apply_layers_creates_and_updates():
    doc = ezdxf.new("R2010", setup=True)
    doc.layers.add("A-WALL", color=1, lineweight=13)  # 既有、屬性錯 → 以 yaml 為準
    names = apply_layers(doc, ["A-WALL", "C-PROP", "X-VPORT"])
    assert names == ["A-WALL", "C-PROP", "X-VPORT"]
    wall = doc.layers.get("A-WALL")
    assert (wall.dxf.color, wall.dxf.lineweight, wall.dxf.linetype) == (7, 50, "Continuous")
    assert doc.layers.get("C-PROP").dxf.linetype == "DA-CHAIN"
    assert "DA-CHAIN" in doc.linetypes  # 自訂線型自動建立
    assert doc.layers.get("X-VPORT").dxf.plot == 0
    assert doc.header["$LWDISPLAY"] == 1


def test_apply_layers_all_and_unknown():
    doc = ezdxf.new("R2010", setup=True)
    assert len(apply_layers(doc)) == len(load_layers())
    with pytest.raises(KeyError, match=r"layers\.yaml"):
        apply_layers(doc, ["Z-NOPE"])


def test_text_style_env_override(monkeypatch):
    monkeypatch.delenv(FONT_ENV, raising=False)
    doc = ezdxf.new("R2010", setup=True)
    assert doc.styles.get(text_style(doc)).dxf.font == DEFAULT_CJK_FONT
    assert sheet.CJK_FONT == DEFAULT_CJK_FONT  # 向後相容常數
    monkeypatch.setenv(FONT_ENV, WINDOWS_CJK_FONT)
    text_style(doc)
    assert doc.styles.get("CJK").dxf.font == WINDOWS_CJK_FONT
    assert sheet.new_sheet_doc().styles.get("CJK").dxf.font == WINDOWS_CJK_FONT


# ---------------------------------------------------------------- 各圖種範例 → standards_check 綠


@pytest.fixture(scope="module")
def plan_dxf(tmp_path_factory) -> Path:
    from draftagent.schema.plan import PlanSpec

    raw = json.loads(PLAN_SPEC.read_text(encoding="utf-8"))
    raw.pop("kind", None)
    raw.pop("units", None)
    return dxf.write_plan(PlanSpec.model_validate(raw), tmp_path_factory.mktemp("p") / "plan.dxf")


@pytest.fixture(scope="module")
def profile_dxf(tmp_path_factory) -> Path:
    from draftagent.schema.profile import ProfileSpec

    return profile.write_profile(
        ProfileSpec.from_json(PROFILE_SPEC), tmp_path_factory.mktemp("pr") / "profile.dxf"
    )


@pytest.fixture(scope="module")
def detail_dxf(tmp_path_factory) -> Path:
    from draftagent.schema.detail import DetailSheetSpec

    raw = json.loads(DETAIL_SPEC.read_text(encoding="utf-8"))
    return detail.write_detail_sheet(
        DetailSheetSpec.model_validate(raw), tmp_path_factory.mktemp("d") / "detail.dxf"
    )


@pytest.mark.parametrize("fixture", ["plan_dxf", "profile_dxf", "detail_dxf"])
def test_examples_pass(fixture, request):
    res = check_standards(request.getfixturevalue(fixture))
    assert res.ok, res.render()
    assert not res.metrics["unknown_layers"]
    assert res.metrics["layers_checked"] > 0


def test_plan_wall_is_050(plan_dxf):
    doc = ezdxf.readfile(plan_dxf)
    assert doc.layers.get("A-WALL").dxf.lineweight == 50
    assert doc.layers.get("A-DIMS").dxf.lineweight == 18
    assert doc.header["$LWDISPLAY"] == 1
    walls = doc.modelspace().query("LWPOLYLINE[layer=='A-WALL']")
    assert walls and all(e.dxf.lineweight == -1 for e in walls)  # BYLAYER → 0.50
    assert all(t.dxf.style == "CJK" for t in doc.modelspace().query("TEXT"))


# ---------------------------------------------------------------- 故意錯


def test_wall_013_is_red(plan_dxf, tmp_path):
    doc = ezdxf.readfile(plan_dxf)
    doc.layers.get("A-WALL").dxf.lineweight = 13
    bad = tmp_path / "bad.dxf"
    doc.saveas(bad)
    res = check_standards(bad)
    assert not res.ok
    assert any("A-WALL" in e and "lineweight" in e for e in res.errors)


def test_wrong_linetype_is_red(plan_dxf):
    doc = ezdxf.readfile(plan_dxf)
    doc.layers.get("A-WALL").dxf.linetype = "DASHED"
    res = check_standards(doc)
    assert not res.ok
    assert any("linetype" in e for e in res.errors)


def test_unknown_layer_is_warning(plan_dxf):
    doc = ezdxf.readfile(plan_dxf)
    doc.layers.add("MY-STUFF")
    doc.modelspace().add_line((0, 0), (1, 1), dxfattribs={"layer": "MY-STUFF"})
    res = check_standards(doc)
    assert res.ok
    assert res.metrics["unknown_layers"] == ["MY-STUFF"]
    assert any("MY-STUFF" in w for w in res.warnings)


def test_lwdisplay_off_is_red(plan_dxf):
    doc = ezdxf.readfile(plan_dxf)
    doc.header["$LWDISPLAY"] = 0
    assert not check_standards(doc).ok


def test_color_override_warning(plan_dxf):
    doc = ezdxf.readfile(plan_dxf)
    for e in doc.modelspace():
        e.dxf.color = 1
    res = check_standards(doc)
    assert res.ok
    assert any("覆寫圖層顏色" in w for w in res.warnings)
    assert not any("覆寫圖層顏色" in w for w in check_standards(plan_dxf).warnings)


def test_cli(plan_dxf):
    r = subprocess.run(
        [sys.executable, "-m", "draftagent.verify.standards_check", str(plan_dxf), "--json"],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads(r.stdout)["ok"] is True
