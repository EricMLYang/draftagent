"""draftagent.arch.inspect：DXF 體檢（單位推測、圖層候選、圖塊 / xref、recover、CLI）。"""

from __future__ import annotations

import json
from pathlib import Path

import ezdxf
import pytest
from typer.testing import CliRunner

from draftagent.arch.inspect import double_line_ratio, floor_tags, inspect_dxf
from draftagent.cli import app

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/archdxf"
runner = CliRunner()


def _rect(msp, x0, y0, w, h, layer):
    msp.add_lwpolyline([(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)], close=True,
                       dxfattribs={"layer": layer})


def make_plan(path: Path, *, scale: float = 1000.0, origin=(0.0, 0.0), insunits: int | None = 4,
              wall_layer="外牆", site_layer="地界", extra=None) -> Path:
    """30 m × 20 m 建物（雙線外牆，厚 0.2 m）+ 50 m × 40 m 地界 + 標註 / 文字。scale = 每 m 的圖面單位。"""
    doc = ezdxf.new("R2018")
    if insunits is not None:
        doc.header["$INSUNITS"] = insunits
    msp = doc.modelspace()
    ox, oy = origin
    s = scale
    for n in (wall_layer, site_layer, "DIM", "TEXT", "內牆"):
        doc.layers.add(n)
    _rect(msp, ox, oy, 30 * s, 20 * s, wall_layer)
    _rect(msp, ox + 0.2 * s, oy + 0.2 * s, 29.6 * s, 19.6 * s, wall_layer)
    _rect(msp, ox - 10 * s, oy - 10 * s, 50 * s, 40 * s, site_layer)
    msp.add_line((ox + 10 * s, oy), (ox + 10 * s, oy + 20 * s), dxfattribs={"layer": "內牆"})
    msp.add_text("一層平面圖", height=0.5 * s, dxfattribs={"layer": "TEXT"}).set_placement(
        (ox + 10 * s, oy - 3 * s))
    msp.add_linear_dim(base=(ox, oy - 2 * s), p1=(ox, oy), p2=(ox + 30 * s, oy),
                       dxfattribs={"layer": "DIM"}).render()
    if extra:
        extra(doc, msp)
    doc.saveas(path)
    return path


def test_floor_tags():
    assert floor_tags("一層平面圖") == ["1F"]
    assert floor_tags("地下一層平面圖") == ["B1"]
    assert floor_tags("B2F") == ["B2"]
    assert "RF" in floor_tags("屋頂層")
    assert floor_tags("A-WALL-1F") == ["1F"]
    assert floor_tags("十二樓") == ["12F"]
    assert floor_tags("A-WALL") == []


def test_double_line_ratio():
    segs = [((0, 0), (10, 0)), ((0, 0.2), (10, 0.2))]
    assert double_line_ratio(segs, (0.08, 0.6)) == 1.0
    assert double_line_ratio([((0, 0), (10, 0)), ((0, 5), (10, 5))], (0.08, 0.6)) == 0.0


def test_units_mm(tmp_path):
    rep = inspect_dxf(make_plan(tmp_path / "a.dxf"))
    assert rep["ok"]
    assert rep["units"]["guess"] == "mm"
    assert not any("$INSUNITS 可能錯誤" in w for w in rep["warnings"])
    wall = next(r for r in rep["layers"] if r["name"] == "外牆")
    assert wall["closed"]["count"] == 2
    assert wall["closed"]["area_max_m2"] == pytest.approx(600.0)
    assert wall["double_line_ratio"] > 0.9


def test_units_m(tmp_path):
    rep = inspect_dxf(make_plan(tmp_path / "a.dxf", scale=1.0, insunits=6))
    assert rep["units"]["guess"] == "m"
    assert not any("可能錯誤" in w for w in rep["warnings"])
    assert next(r for r in rep["layers"] if r["name"] == "外牆")["closed"]["area_max_m2"] == pytest.approx(600)


def test_units_twd97_m_and_mm(tmp_path):
    rep = inspect_dxf(make_plan(tmp_path / "a.dxf", scale=1.0, origin=(250000.0, 2650000.0), insunits=6))
    assert rep["units"]["guess"] == "m" and rep["units"]["twd97_like"] == "m"
    rep = inspect_dxf(make_plan(tmp_path / "b.dxf", scale=1000.0, origin=(250000e3, 2650000e3), insunits=4))
    assert rep["units"]["guess"] == "mm" and rep["units"]["twd97_like"] == "mm"


def test_wrong_insunits_warns(tmp_path):
    rep = inspect_dxf(make_plan(tmp_path / "a.dxf", scale=1000.0, insunits=6))  # 寫 m，實際 mm
    assert rep["units"]["guess"] == "mm"
    assert any("$INSUNITS 可能錯誤" in w for w in rep["warnings"])
    rep = inspect_dxf(make_plan(tmp_path / "b.dxf", insunits=0))
    assert any("未設定" in w for w in rep["warnings"])


def test_chinese_layer_candidates(tmp_path):
    rep = inspect_dxf(make_plan(tmp_path / "a.dxf"))
    ew = rep["candidates"]["exterior_wall"]
    assert ew[0]["layer"] == "外牆"
    assert rep["candidates"]["site_boundary"][0]["layer"] == "地界"
    # 內牆 / 標註不應排在外牆前面
    names = [d["layer"] for d in ew]
    if "內牆" in names:
        assert names.index("內牆") > names.index("外牆")
    assert rep["top"][0]["layer"] in ("外牆", "地界")
    assert any(t["tags"] == ["1F"] for t in rep["texts"])
    assert rep["clusters"] and "1F" in rep["clusters"][0]["floor_tags"]


def test_english_layers_and_basement(tmp_path):
    def extra(doc, msp):
        doc.layers.add("B1-WALL")
        _rect(msp, 100_000, 0, 30_000, 20_000, "B1-WALL")

    rep = inspect_dxf(make_plan(tmp_path / "a.dxf", wall_layer="A-WALL-1F", site_layer="PL", extra=extra))
    assert rep["candidates"]["exterior_wall"][0]["layer"] in ("A-WALL-1F", "B1-WALL")
    assert any(d["layer"] == "B1-WALL" for d in rep["candidates"]["basement"])
    assert any(d["layer"] == "A-WALL-1F" for d in rep["candidates"]["floor:1F"])
    assert rep["candidates"]["site_boundary"][0]["layer"] == "PL"
    assert len(rep["clusters"]) >= 2


def test_blocks_xref_missing_no_crash(tmp_path):
    def extra(doc, msp):
        blk = doc.blocks.new("DOOR")
        blk.add_line((0, 0), (900, 0))
        blk.add_arc((0, 0), 900, 0, 90)
        outer = doc.blocks.new("NEST")
        outer.add_blockref("DOOR", (0, 0))
        msp.add_blockref("DOOR", (1000, 0), dxfattribs={"layer": "外牆", "rotation": 90})
        msp.add_blockref("NEST", (5000, 0), dxfattribs={"xscale": -1, "yscale": 1})
        doc.add_xref_def("C:\\nowhere\\site.dxf", "SITE_XREF")
        msp.add_blockref("SITE_XREF", (0, 0))

    rep = inspect_dxf(make_plan(tmp_path / "a.dxf", extra=extra))
    assert rep["ok"]
    b = rep["blocks"]
    door = next(d for d in b["definitions"] if d["name"] == "DOOR")
    assert door["inserted"] == 2 and not door["nested"]
    assert next(d for d in b["definitions"] if d["name"] == "NEST")["nested"]
    assert b["xrefs"][0]["name"] == "SITE_XREF" and b["xrefs"][0]["found"] is False
    assert b["inserts"]["mirrored"] == 1
    assert any("SITE_XREF" in w for w in rep["warnings"])
    json.dumps(rep, ensure_ascii=False)


def test_recover_mode(tmp_path):
    p = make_plan(tmp_path / "a.dxf")
    text = p.read_text(encoding="utf-8")
    # 拿掉 HEADER 的 ENDSEC 讓一般讀檔失敗（recover 可修）
    i = text.index("ENDSEC")
    bad = tmp_path / "bad.dxf"
    bad.write_text(text[: i - 4] + text[i + len("ENDSEC") + 1:], encoding="utf-8")
    rep = inspect_dxf(bad)
    assert rep["read"]["mode"] in ("recover", "normal")
    if rep["read"]["mode"] == "recover":
        assert rep["ok"] and any("recover" in w for w in rep["warnings"])


def test_unreadable_reports_error(tmp_path):
    p = tmp_path / "x.dxf"
    p.write_bytes(b"AC1032\x00\x00garbage")
    rep = inspect_dxf(p)
    assert not rep["ok"] and "DWG" in rep["error"]
    p2 = tmp_path / "y.dxf"
    p2.write_text("not a dxf at all\n", encoding="utf-8")
    rep = inspect_dxf(p2)
    assert isinstance(rep["ok"], bool)
    json.dumps(rep, ensure_ascii=False)


def test_cli_json(tmp_path):
    p = make_plan(tmp_path / "a.dxf")
    r = runner.invoke(app, ["inspect-dxf", str(p), "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["ok"] and data["units"]["guess"] == "mm"
    r = runner.invoke(app, ["inspect-dxf", str(p)])
    assert r.exit_code == 0 and "前 5 名候選圖層" in r.output
    r = runner.invoke(app, ["inspect-dxf", str(tmp_path / "missing.dxf"), "--json"])
    assert r.exit_code == 1


def test_cli_png(tmp_path):
    pytest.importorskip("matplotlib")
    p = make_plan(tmp_path / "a.dxf")
    r = runner.invoke(app, ["inspect-dxf", str(p), "--json", "--png", str(tmp_path / "all.png"),
                            "--layer-png-dir", str(tmp_path / "layers")])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert Path(data["png"]).exists()
    assert data["layer_pngs"] and all(Path(x).exists() for x in data["layer_pngs"])


# ---------------------------------------------------------------- 另一 agent 的合成 fixture

CASES = sorted(d for d in FIXTURES.glob("*") if (d / "truth.json").exists()) if FIXTURES.exists() else []


@pytest.mark.skipif(not CASES, reason="tests/fixtures/archdxf/ 尚未產生")
@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_fixture_candidates(case: Path):
    truth = json.loads((case / "truth.json").read_text(encoding="utf-8"))
    rep = inspect_dxf(case / "drawing.dxf")
    assert rep["ok"], rep.get("error")
    assert rep["units"]["guess"] == truth["units"], rep["units"]["why"]
    want = truth["layers"]["exterior_wall"]
    top3 = [d["layer"] for d in rep["candidates"].get("exterior_wall", [])[:3]]
    assert want in top3, f"exterior_wall={want} 不在前 3 名 {top3}"
