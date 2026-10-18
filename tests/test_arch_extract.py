"""arch-import：建築師 DXF → TWD97 GeoJSON（extract + arch_check）。

測試用 DXF 都在本檔內以 ezdxf 現場產生（放在 repo 的 work/ 下，因 source_dxf 必須是 repo 內相對路徑）。
`python tests/test_arch_extract.py --write-example` 重寫 examples/data/arch/example_1f.dxf。
另一組參數化測試吃 tests/fixtures/archdxf/<case>/truth.json（存在時才跑）。
"""

from __future__ import annotations

import json
import math
import shutil
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import ezdxf
import pytest
from pydantic import ValidationError
from shapely.geometry import Polygon, shape

from draftagent import build
from draftagent.schema.arch import ArchImportSpec
from draftagent.verify.arch_check import check_arch

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/archdxf"
E0, N0 = 280000.0, 2770000.0


# ---------------------------------------------------------------- helpers


@pytest.fixture
def wd() -> Iterator[Path]:
    d = ROOT / "work" / f"_pytest_arch_{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def to_twd(p, theta_deg=0.0, k=0.001, t=(E0, N0)):
    c, s = math.cos(math.radians(theta_deg)), math.sin(math.radians(theta_deg))
    x, y = p[0] * k, p[1] * k
    return (c * x - s * y + t[0], s * x + c * y + t[1])


def cps(pts, theta_deg=0.0, k=0.001):
    return [{"id": f"CP{i + 1}", "drawing": list(p), "twd97": list(to_twd(p, theta_deg, k))} for i, p in enumerate(pts)]


RECT = [(0, 0), (20000, 0), (20000, 12000), (0, 12000)]
CP3 = [(-1000, -1000), (21000, -1000), (21000, 13000)]
CP4 = [*CP3, (-1000, 13000)]


def new_doc(insunits: int = 4):
    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = insunits
    for name in ("A-WALL", "A-FURN", "A-TEXT", "P-RDRN", "L-OPEN", "B1-WALL"):
        doc.layers.add(name)
    return doc


def save(doc, d: Path, name: str = "drawing.dxf") -> str:
    p = d / name
    doc.saveas(p)
    return str(p.relative_to(ROOT))


def mkspec(dxf: str, **kw) -> ArchImportSpec:
    data = {
        "source_dxf": dxf,
        "drawing_units": "mm",
        "layers": {"exterior_wall": ["A-WALL"]},
        "wall_representation": "outline",
        "control_points": cps(CP3),
    }
    data.update(kw)
    return ArchImportSpec.model_validate(data)


def footprints(res) -> list[Polygon]:
    gj = json.loads(res.artifacts["geojson"].read_text("utf-8"))
    return [shape(f["geometry"]) for f in gj["features"] if f["properties"]["role"] == "footprint"]


def errors(res) -> list[str]:
    return [e for r in res.results for e in r.errors]


def warnings(res) -> list[str]:
    return [w for r in res.results for w in r.warnings]


def truth_poly(pts, theta=0.0, k=0.001) -> Polygon:
    return Polygon([to_twd(p, theta, k) for p in pts])


def double_ring(msp, x0, y0, x1, y1, t, openings, layer="A-WALL", jambs=True):
    """雙線外牆（外緣 x0..x1、牆厚 t），底邊開口 [(xa, xb)]（有 / 無封口短線）。"""
    a = {"layer": layer}
    xs_o = [x0, *[v for o in openings for v in o], x1]
    xs_i = [x0 + t, *[v for o in openings for v in o], x1 - t]
    for i in range(0, len(xs_o), 2):
        msp.add_line((xs_o[i], y0), (xs_o[i + 1], y0), dxfattribs=a)
        msp.add_line((xs_i[i], y0 + t), (xs_i[i + 1], y0 + t), dxfattribs=a)
    if jambs:
        for xa, xb in openings:
            msp.add_line((xa, y0), (xa, y0 + t), dxfattribs=a)
            msp.add_line((xb, y0), (xb, y0 + t), dxfattribs=a)
    msp.add_lwpolyline([(x1, y0), (x1, y1), (x0, y1), (x0, y0)], dxfattribs=a)
    msp.add_lwpolyline([(x1 - t, y0 + t), (x1 - t, y1 - t), (x0 + t, y1 - t), (x0 + t, y0 + t)], dxfattribs=a)


def noise(msp):
    msp.add_circle((5000, 5000), 400, dxfattribs={"layer": "A-FURN"})
    msp.add_text("客廳", dxfattribs={"layer": "A-TEXT", "height": 300}).set_placement((6000, 6000))


# ---------------------------------------------------------------- schema


def test_schema_rules(wd):
    doc = new_doc()
    msp = doc.modelspace()
    msp.add_lwpolyline(RECT, close=True, dxfattribs={"layer": "A-WALL"})
    dxf = save(doc, wd)
    with pytest.raises(ValidationError, match="drawing_units"):
        ArchImportSpec.model_validate({"source_dxf": dxf, "layers": {"exterior_wall": ["A-WALL"]},
                                       "wall_representation": "outline", "control_points": cps(CP3)})
    with pytest.raises(ValidationError, match="相對路徑"):
        mkspec(str(ROOT / dxf))
    with pytest.raises(ValidationError, match="上限"):
        mkspec(dxf, join_tolerance=60)  # 60 mm > 50 mm
    with pytest.raises(ValidationError):
        mkspec(dxf, control_points=cps(CP3[:1]))
    with pytest.raises(ValidationError):
        mkspec(dxf, extra_field=1)
    s = mkspec(dxf)
    assert s.join_tol == pytest.approx(10.0) and s.crs == "EPSG:3826"
    assert mkspec(dxf, drawing_units="m").join_tol == pytest.approx(0.01)


# ---------------------------------------------------------------- 抽取


def test_outline_single_line(wd, tmp_path):
    doc = new_doc()
    msp = doc.modelspace()
    msp.add_lwpolyline(RECT, close=True, dxfattribs={"layer": "A-WALL"})
    noise(msp)
    res = build(mkspec(save(doc, wd)), out=tmp_path, preview=False)
    assert res.ok, str(res)
    (fp,) = footprints(res)
    assert fp.area == pytest.approx(240.0, rel=1e-6)
    assert fp.hausdorff_distance(truth_poly(RECT)) < 1e-6
    assert {"geojson", "transform", "overlay_png"} <= set(res.artifacts)
    tr = json.loads(res.artifacts["transform"].read_text("utf-8"))
    assert tr["scale"] == pytest.approx(1.0) and abs(tr["rotation_deg"]) < 1e-9


@pytest.mark.parametrize("jambs", [True, False])
def test_double_line_with_openings(wd, tmp_path, jambs):
    doc = new_doc()
    msp = doc.modelspace()
    double_ring(msp, 0, 0, 20000, 12000, 200, [(2000, 2900), (8000, 10400)], jambs=jambs)
    noise(msp)
    spec = mkspec(save(doc, wd), wall_representation="double_line")
    res = build(spec, out=tmp_path, preview=False)
    assert res.ok, str(res)
    (fp,) = footprints(res)
    assert fp.hausdorff_distance(truth_poly(RECT)) < 1e-3
    assert fp.area == pytest.approx(240.0, rel=1e-4)
    # 同一張圖若誤選 outline：開口沒接起來 → 紅燈，不硬補
    res2 = build(spec.model_copy(update={"wall_representation": "outline"}), out=tmp_path, preview=False)
    assert not res2.ok


def test_double_line_closed_ring_exact(wd, tmp_path):
    doc = new_doc()
    msp = doc.modelspace()
    double_ring(msp, 0, 0, 20000, 12000, 200, [])
    res = build(mkspec(save(doc, wd), wall_representation="double_line"), out=tmp_path, preview=False)
    assert res.ok, str(res)
    (fp,) = footprints(res)
    assert fp.area == pytest.approx(240.0, rel=1e-9)


def _gappy(msp, gap, overshoot=100.0):
    (x0, y0), (x1, _), (_, y1) = RECT[0], RECT[1], RECT[2]
    a = {"layer": "A-WALL"}
    msp.add_line((x0 + gap, y0), (x1 + overshoot, y0), dxfattribs=a)  # 底邊：左端留縫、右端過頭
    msp.add_line((x1, y0 + gap), (x1, y1 - gap), dxfattribs=a)
    msp.add_line((x1, y1), (x0 + gap, y1), dxfattribs=a)
    msp.add_line((x1, y1), (x0 + gap, y1), dxfattribs=a)  # 重複線
    msp.add_line((x0, y1 - gap), (x0, y0), dxfattribs=a)
    msp.add_line((8000, y1), (9000, y1), dxfattribs=a)  # 與頂邊重疊的短線


def test_gap_join_within_tolerance(wd, tmp_path):
    doc = new_doc()
    _gappy(doc.modelspace(), 5.0)
    res = build(mkspec(save(doc, wd)), out=tmp_path, preview=False)
    assert res.ok, str(res)
    (fp,) = footprints(res)
    assert fp.hausdorff_distance(truth_poly(RECT)) < 0.01
    tr = json.loads(res.artifacts["transform"].read_text("utf-8"))
    assert 4.9 < tr["seam_max_drawing"] <= 10.0


def test_gap_beyond_tolerance_is_red(wd, tmp_path):
    doc = new_doc()
    _gappy(doc.modelspace(), 30.0)
    dxf = save(doc, wd)
    res = build(mkspec(dxf), out=tmp_path, preview=False)
    assert not res.ok
    assert any("外框" in e for e in errors(res))
    # 左上角縫是斜向 30√2 ≈ 42.4 mm：40 仍不夠，45 才接得起來（仍在 50 mm 上限內）
    assert not build(mkspec(dxf, join_tolerance=40), out=tmp_path, preview=False).ok
    res = build(mkspec(dxf, join_tolerance=45), out=tmp_path, preview=False)
    assert res.ok, str(res)


def test_rotation_and_control_points(wd, tmp_path):
    doc = new_doc()
    doc.modelspace().add_lwpolyline(RECT, close=True, dxfattribs={"layer": "A-WALL"})
    res = build(mkspec(save(doc, wd), control_points=cps(CP4, theta_deg=30)), out=tmp_path, preview=False)
    assert res.ok, str(res)
    (fp,) = footprints(res)
    assert fp.hausdorff_distance(truth_poly(RECT, 30)) < 1e-4
    tr = json.loads(res.artifacts["transform"].read_text("utf-8"))
    assert tr["rotation_deg"] == pytest.approx(30.0)


def test_block_expansion_nested_layer0(wd, tmp_path):
    doc = new_doc()
    doc.layers.add("X-OTHER")
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 12000), (0, 0), (20000, 0)], dxfattribs={"layer": "A-WALL"})
    inner = doc.blocks.new("B_INNER")
    inner.add_line((0, 0), (6000, 0), dxfattribs={"layer": "0"})
    outer = doc.blocks.new("B_OUTER")
    outer.add_blockref("B_INNER", (0, 0), dxfattribs={"layer": "A-WALL", "xscale": 2, "yscale": 2, "rotation": 90})
    msp.add_blockref("B_OUTER", (20000, 0), dxfattribs={"layer": "X-OTHER"})  # 外層 INSERT 不在外牆層
    top = doc.blocks.new("B_TOP")
    top.add_line((0, 0), (-20000, 0), dxfattribs={"layer": "0"})
    msp.add_blockref("B_TOP", (20000, 12000), dxfattribs={"layer": "A-WALL"})
    doc.blocks.new("XREF_GONE", dxfattribs={"flags": 4, "xref_path": "missing/site.dxf"})
    res = build(mkspec(save(doc, wd)), out=tmp_path, preview=False)
    assert res.ok, str(res)
    (fp,) = footprints(res)
    assert fp.hausdorff_distance(truth_poly(RECT)) < 1e-6
    gj = json.loads(res.artifacts["geojson"].read_text("utf-8"))
    hs = gj["features"][0]["properties"]["source_handles"]
    assert any(h.count(">") == 2 for h in hs), hs  # B_OUTER > INSERT B_INNER > LINE
    tr = json.loads(res.artifacts["transform"].read_text("utf-8"))
    assert any("XREF_GONE" in w for w in tr["warnings"])


def test_floor_filter_title_and_bbox(wd, tmp_path):
    doc = new_doc()
    msp = doc.modelspace()
    msp.add_lwpolyline(RECT, close=True, dxfattribs={"layer": "A-WALL"})
    msp.add_lwpolyline([(x + 50000, y) for x, y in [(0, 0), (24000, 0), (24000, 14000), (0, 14000)]],
                       close=True, dxfattribs={"layer": "A-WALL"})
    msp.add_text("一層平面圖", dxfattribs={"layer": "A-TEXT", "height": 500}).set_placement((10000, -3000))
    msp.add_text("二層平面圖", dxfattribs={"layer": "A-TEXT", "height": 500}).set_placement((62000, -3000))
    dxf = save(doc, wd)
    res = build(mkspec(dxf), out=tmp_path, preview=False)
    assert not res.ok and any("分離外框" in e for e in errors(res))
    for ff in ({"title_text": "一層平面圖", "search_radius": 20000}, {"bbox": [-5000, -5000, 25000, 15000]}):
        res = build(mkspec(dxf, floor_filter=ff), out=tmp_path, preview=False)
        assert res.ok, str(res)
        (fp,) = footprints(res)
        assert fp.area == pytest.approx(240.0, rel=1e-6)


def test_drains_open_space_basement_offset(wd, tmp_path):
    doc = new_doc()
    msp = doc.modelspace()
    msp.add_lwpolyline(RECT, close=True, dxfattribs={"layer": "A-WALL"})
    msp.add_lwpolyline([(-1000, -1000), (-1000, 13000), (-5000, 13000), (-5000, -1000)], close=True,
                       dxfattribs={"layer": "L-OPEN"})
    msp.add_lwpolyline([(x + 60000, y) for x, y in [(-500, -500), (20500, -500), (20500, 12500), (-500, 12500)]],
                       close=True, dxfattribs={"layer": "B1-WALL"})
    rd = doc.blocks.new("RD")
    rd.add_circle((0, 0), 100)
    msp.add_blockref("RD", (500, 500), dxfattribs={"layer": "P-RDRN"})
    msp.add_point((19500, 11500), dxfattribs={"layer": "P-RDRN"})
    spec = mkspec(save(doc, wd), layers={"exterior_wall": ["A-WALL"], "basement": ["B1-WALL"],
                                         "open_space": ["L-OPEN"], "roof_drains": ["P-RDRN"]},
                  offsets={"basement": [-60000, 0]})
    res = build(spec, out=tmp_path, preview=False)
    assert res.ok, str(res)
    gj = json.loads(res.artifacts["geojson"].read_text("utf-8"))
    roles = [f["properties"]["role"] for f in gj["features"]]
    assert sorted(roles) == ["basement", "footprint", "open_space", "roof_drain", "roof_drain"]
    b = next(shape(f["geometry"]) for f in gj["features"] if f["properties"]["role"] == "basement")
    assert b.contains(footprints(res)[0])


# ---------------------------------------------------------------- verifier 紅燈 / 警告


def _rect_doc(wd, insunits=4) -> str:
    doc = new_doc(insunits)
    doc.modelspace().add_lwpolyline(RECT, close=True, dxfattribs={"layer": "A-WALL"})
    return save(doc, wd)


def test_wrong_units_caught_by_scale(wd, tmp_path):
    # 圖其實是 mm，agent 誤判為 m：控制點擬合出 s ≈ 0.001 → error
    res = build(mkspec(_rect_doc(wd), drawing_units="m"), out=tmp_path, preview=False)
    assert not res.ok
    assert any("縮放比" in e for e in errors(res))


def test_insunits_mismatch_warns(wd, tmp_path):
    res = build(mkspec(_rect_doc(wd, insunits=6)), out=tmp_path, preview=False)
    assert res.ok, str(res)
    assert any("$INSUNITS" in w for w in warnings(res))


def test_two_control_points_warn(wd, tmp_path):
    res = build(mkspec(_rect_doc(wd), control_points=cps(CP3[:2], 10)), out=tmp_path, preview=False)
    assert res.ok, str(res)
    assert any("2 個控制點" in w for w in warnings(res))


def test_bad_control_point_residual(wd, tmp_path):
    pts = cps(CP4, 15)
    pts[2]["twd97"][0] += 0.3
    res = build(mkspec(_rect_doc(wd), control_points=pts), out=tmp_path, preview=False)
    assert not res.ok
    assert any("殘差" in e for e in errors(res))


def test_footprint_outside_site(wd, tmp_path):
    site_in = [list(to_twd(p)) for p in [(-500, -500), (20500, -500), (20500, 12500), (-500, 12500)]]
    site_small = [list(to_twd(p)) for p in [(-500, -500), (15000, -500), (15000, 12500), (-500, 12500)]]
    dxf = _rect_doc(wd)
    assert build(mkspec(dxf, site={"coords": site_in}), out=tmp_path, preview=False).ok
    res = build(mkspec(dxf, site={"coords": site_small}), out=tmp_path, preview=False)
    assert not res.ok and any("基地外" in e for e in errors(res))


def test_tampered_geojson_caught(wd, tmp_path):
    spec = mkspec(_rect_doc(wd))
    res = build(spec, out=tmp_path, preview=False)
    assert res.ok, str(res)
    p = res.artifacts["geojson"]
    orig = json.loads(p.read_text("utf-8"))

    def run(mut) -> list[str]:
        gj = json.loads(json.dumps(orig))
        mut(gj["features"][0])
        p.write_text(json.dumps(gj), encoding="utf-8")
        return check_arch(spec, p, res.artifacts["transform"]).errors

    def shift(f):
        f["geometry"]["coordinates"][0] = [[x + 0.5, y] for x, y in f["geometry"]["coordinates"][0]]

    assert any("對稱差" in e for e in run(shift))
    assert any("area_m2" in e for e in run(lambda f: f["properties"].update(area_m2=999)))
    assert any("source_handle" in e for e in run(lambda f: f["properties"].update(source_handles=["FFFF"])))
    assert any("source_handles" in e for e in run(lambda f: f["properties"].update(source_handles=[])))
    p.write_text(json.dumps(orig), encoding="utf-8")
    assert check_arch(spec, p, res.artifacts["transform"]).ok
    t = json.loads(res.artifacts["transform"].read_text("utf-8"))
    t["scale"] = 1.002
    res.artifacts["transform"].write_text(json.dumps(t), encoding="utf-8")
    assert not check_arch(spec, p, res.artifacts["transform"]).ok


def test_example_builds_green(tmp_path):
    res = build(ROOT / "examples/arch-import.json", out=tmp_path, preview=False)
    assert res.ok, str(res)
    # L 形 24×9 + 15×7 m；同一牆段兩側各有開口（迴歸：舊的兩兩橋接法只補得到一個開口）
    (fp,) = footprints(res)
    assert fp.area == pytest.approx(321.0, rel=1e-4)
    gj = json.loads(res.artifacts["geojson"].read_text("utf-8"))
    assert sum(f["properties"]["role"] == "roof_drain" for f in gj["features"]) == 4


# ---------------------------------------------------------------- 合成 fixture（另一 agent 產生）

CASES = sorted(p.name for p in FIXTURES.iterdir() if (p / "truth.json").exists()) if FIXTURES.exists() else []


def fixture_spec(case: str) -> tuple[ArchImportSpec, dict]:
    d = FIXTURES / case
    t = json.loads((d / "truth.json").read_text("utf-8"))  # 容忍 truth 裡的額外鍵
    lay = t["layers"]
    layers = {"exterior_wall": [lay["exterior_wall"]]}
    data: dict = {
        "source_dxf": str((d / "drawing.dxf").relative_to(ROOT)),
        "drawing_units": t["units"],
        "wall_representation": "double_line" if t.get("wall_thickness_mm") else "outline",
        "control_points": t["control_points"],
        "site": {"coords": t["site_twd97"]},
    }
    if lay.get("basement"):
        layers["basement"] = [lay["basement"]]
        off = (t.get("floor_offsets_drawing") or {}).get("B1")
        if off:  # 各層並排：B1 平移回 1F 原位（agent 由軸線對位判斷；這裡讀 truth）
            data["offsets"] = {"basement": [-off[0], -off[1]]}
    if t.get("exterior_wall_filter"):
        f = t["exterior_wall_filter"]
        data["exterior_wall_filter"] = {k: f[k] for k in ("color", "lineweight") if k in f}
    # floor_filter（"1F-" 圖層前綴）已由 layers 直接指定 1F 圖層表達，不另設 bbox
    data["layers"] = layers
    return ArchImportSpec.model_validate(data), t


@pytest.mark.skipif(not CASES, reason="tests/fixtures/archdxf 尚未產生")
@pytest.mark.parametrize("case", CASES)
def test_fixture_case(case, tmp_path):
    spec, t = fixture_spec(case)
    res = build(spec, out=tmp_path, preview=False)
    assert res.ok, str(res)
    (fp,) = footprints(res)
    truth = Polygon(t["footprint_twd97"])
    assert fp.hausdorff_distance(truth) <= 0.05
    assert abs(fp.area - truth.area) / truth.area <= 0.005
    if t.get("basement_twd97"):
        gj = json.loads(res.artifacts["geojson"].read_text("utf-8"))
        (b,) = [shape(f["geometry"]) for f in gj["features"] if f["properties"]["role"] == "basement"]
        assert b.hausdorff_distance(Polygon(t["basement_twd97"])) <= 0.05
    if t.get("insunits_header") not in (None, {"mm": 4, "m": 6}[t["units"]]):
        assert any("$INSUNITS" in w for w in warnings(res))


# ---------------------------------------------------------------- 範例 DXF 產生器


def write_example_dxf(path: Path) -> None:
    """examples/data/arch/example_1f.dxf：雙線外牆 L 形 + 開口、落水頭、空地、雜訊、圖名、$INSUNITS=4。"""
    doc = new_doc()
    doc.layers.add("A-DOOR")
    msp = doc.modelspace()
    a = {"layer": "A-WALL"}
    t = 200
    outer = [(0, 0), (24000, 0), (24000, 9000), (15000, 9000), (15000, 16000), (0, 16000)]
    inner = [(t, t), (24000 - t, t), (24000 - t, 9000 - t), (15000 - t, 9000 - t), (15000 - t, 16000 - t),
             (t, 16000 - t)]
    # 南面兩個開口（門 1200、落地窗 2400），其餘邊連續
    ops = [(3000, 4200), (9000, 11400)]
    xs = [0, *[v for o in ops for v in o], 24000]
    xi = [t, *[v for o in ops for v in o], 24000 - t]
    for i in range(0, len(xs), 2):
        msp.add_line((xs[i], 0), (xs[i + 1], 0), dxfattribs=a)
        msp.add_line((xi[i], t), (xi[i + 1], t), dxfattribs=a)
    for xa, xb in ops:
        msp.add_line((xa, 0), (xa, t), dxfattribs=a)
        msp.add_line((xb, 0), (xb, t), dxfattribs=a)
    msp.add_lwpolyline([outer[1], *outer[2:], outer[0]], dxfattribs=a)
    msp.add_lwpolyline([inner[1], *inner[2:], inner[0]], dxfattribs=a)
    msp.add_arc((3000, 0), 1200, 270, 360, dxfattribs={"layer": "A-DOOR"})
    msp.add_lwpolyline([(16000, 10000), (23000, 10000), (23000, 15500), (16000, 15500)], close=True,
                       dxfattribs={"layer": "L-OPEN"})
    rd = doc.blocks.new("RD-100")
    rd.add_circle((0, 0), 100)
    for xy in [(500, 500), (23500, 500), (500, 15500), (14500, 15500)]:
        msp.add_blockref("RD-100", xy, dxfattribs={"layer": "P-RDRN"})
    noise(msp)
    msp.add_text("一層平面圖", dxfattribs={"layer": "A-TEXT", "height": 500}).set_placement((8000, -3000))
    doc.saveas(path)


if __name__ == "__main__" and "--write-example" in sys.argv:
    out = ROOT / "examples/data/arch/example_1f.dxf"
    out.parent.mkdir(parents=True, exist_ok=True)
    write_example_dxf(out)
    print(out)
