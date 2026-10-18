"""FloorPlanSpec → DXF（ezdxf）。純函式、確定性，不得呼叫 LLM。

幾何約定見 `schema/floorplan.py` docstring（房間以外牆內緣 / 內牆中心線為界、共用牆只畫一次）。

圖層（render/codes/layers.yaml）：
- A-WALL：外牆（外框 − 室內範圍 − 開口）封閉 LWPOLYLINE，線寬 BYLAYER（0.50 mm）；
  內牆（所有共用邊界合併後依中心線兩側各半牆厚 − 開口）封閉 LWPOLYLINE，實體線寬覆寫
  `INTERIOR_WALL_LW`（0.35 mm）——外牆 / 內牆線寬不同
- A-DOOR：門扇線 + 90° 開啟弧（1 門 = 1 ARC）
- A-WIND：窗框（1 窗 = 1 封閉框）+ 中線
- A-TEXT：房名、淨面積「12.34 m²」（各 1 TEXT，房間淨空內）
- A-DIMS：外總尺寸（南側總長、西側總寬）、尺寸鏈（外緣 → 內牆中心線 → 外緣）
- X-FRAME：圖框與圖名
"""

from __future__ import annotations

import math
from pathlib import Path

import ezdxf
from ezdxf import units
from ezdxf.document import Drawing
from ezdxf.enums import TextEntityAlignment
from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import polylabel, unary_union

from draftagent.render.standards import apply_layers, text_style
from draftagent.schema.floorplan import (
    FloorPlanSpec,
    LocateError,
    Placement,
    door_placement,
    inner_polygon,
    interior_wall_lines,
    room_polygon,
    window_placement,
)

LAYERS: dict[str, int] = {
    "A-WALL": 7,
    "A-DOOR": 3,
    "A-WIND": 4,
    "A-DIMS": 1,
    "A-TEXT": 2,
    "X-FRAME": 8,
}
DIMSTYLE = "EZDXF"
INTERIOR_WALL_LW = 35  # 1/100 mm；外牆用圖層 A-WALL 的 0.50 mm（製圖慣例，非法規）
TEXT_PRINT_MM = 2.5
DIM_TEXT_PRINT_MM = 2.5
TITLE_PRINT_MM = 3.75
FRAME_MARGIN = 1500.0
CHAIN_MIN_TEXTS = 4.0  # 尺寸鏈分段至少 4 倍字高才標（太短的段併入相鄰段，避免數字重疊）
ROW_GAP_TEXTS = 3.5  # 尺寸鏈與總尺寸兩排間距（倍字高）
_EPS = 1.0  # 開口切割往牆外 / 內多延伸 1 mm


# ---------------------------------------------------------------- 幾何（renderer 自用）


def wall_regions(spec: FloorPlanSpec) -> tuple[object, object]:
    """(外牆區域, 內牆區域)，均已扣除開口。"""
    outer = Polygon(spec.outline.outline())
    inner = inner_polygon(spec)
    ext = outer.difference(inner)
    ti = spec.wall_thickness.interior
    bands = [
        _band(s.start, s.end, ti / 2, ti / 2, cap=ti / 2) for s in interior_wall_lines(spec)
    ]
    intr = unary_union(bands).intersection(inner) if bands else Polygon()
    cuts = [opening_rect(spec, p) for p in placements(spec).values() if p is not None]
    if cuts:
        cut = unary_union(cuts)
        ext, intr = ext.difference(cut), intr.difference(cut)
    return ext, intr


def _band(p, q, left: float, right: float, cap: float = 0.0) -> Polygon:
    """線段 p→q 左側 left、右側 right 的矩形帶，兩端各延伸 cap。"""
    L = math.dist(p, q)
    ux, uy = (q[0] - p[0]) / L, (q[1] - p[1]) / L
    nx, ny = -uy, ux
    p0 = (p[0] - ux * cap, p[1] - uy * cap)
    q0 = (q[0] + ux * cap, q[1] + uy * cap)
    return Polygon(
        [
            (p0[0] - nx * right, p0[1] - ny * right),
            (q0[0] - nx * right, q0[1] - ny * right),
            (q0[0] + nx * left, q0[1] + ny * left),
            (p0[0] + nx * left, p0[1] + ny * left),
        ]
    )


def placements(spec: FloorPlanSpec) -> dict[str, Placement | None]:
    """門窗 id → Placement；找不到牆段者為 None（不畫，由 verifier 報 error）。"""
    out: dict[str, Placement | None] = {}
    for d in spec.doors:
        try:
            out[d.id] = door_placement(spec, d)
        except (LocateError, KeyError):
            out[d.id] = None
    for w in spec.windows:
        try:
            out[w.id] = window_placement(spec, w)
        except (LocateError, KeyError):
            out[w.id] = None
    return out


def _mv(p, v, s):
    return (p[0] + v[0] * s, p[1] + v[1] * s)


def opening_rect(spec: FloorPlanSpec, pl: Placement) -> Polygon:
    """開口貫穿牆厚的矩形（法向各多 1 mm）。外牆：內緣往外 t；內牆：中心線兩側各 t/2。"""
    n = pl.n_out
    if pl.exterior:
        lo, hi = -_EPS, pl.thickness + _EPS
    else:
        lo, hi = -pl.thickness / 2 - _EPS, pl.thickness / 2 + _EPS
    a, b = pl.a, pl.b
    return Polygon([_mv(a, n, lo), _mv(b, n, lo), _mv(b, n, hi), _mv(a, n, hi)])


def net_polygons(spec: FloorPlanSpec) -> dict[str, object]:
    """房名 → 淨空多邊形（房間 − 內牆，不扣開口）。"""
    ti = spec.wall_thickness.interior
    bands = [_band(s.start, s.end, ti / 2, ti / 2, cap=ti / 2) for s in interior_wall_lines(spec)]
    walls = unary_union(bands) if bands else Polygon()
    return {r.name: room_polygon(r).difference(walls) for r in spec.rooms}


# ---------------------------------------------------------------- render


def render_floorplan(spec: FloorPlanSpec) -> Drawing:
    doc = ezdxf.new("R2010", setup=True)
    doc.units = units.MM
    doc.header["$MEASUREMENT"] = 1
    doc.header["$LUNITS"] = 2
    apply_layers(doc, LAYERS)
    style = text_style(doc)
    msp = doc.modelspace()
    ps = spec.print_scale

    ext, intr = wall_regions(spec)
    for geom, lw in ((ext, None), (intr, INTERIOR_WALL_LW)):
        for poly in _polys(geom):
            for ring in [poly.exterior, *poly.interiors]:
                pts = [(round(float(x), 6), round(float(y), 6)) for x, y in list(ring.coords)[:-1]]
                attribs = {"layer": "A-WALL"}
                if lw is not None:
                    attribs["lineweight"] = lw
                msp.add_lwpolyline(pts, close=True, dxfattribs=attribs)

    # 房名 + 淨面積
    room_h = TEXT_PRINT_MM * ps
    pls = placements(spec)
    swings = unary_union(
        [_swing_zone(spec, d, pls[d.id]) for d in spec.doors if pls[d.id] is not None]
    )
    for name, net in net_polygons(spec).items():
        if net.is_empty:
            continue
        poly = max(_polys(net), key=lambda g: g.area)
        free = [g for g in _polys(poly.difference(swings)) if g.area > (2 * room_h) ** 2]
        c = polylabel(max(free, key=lambda g: g.area) if free else poly, tolerance=1.0)
        area = net.area / 1e6
        msp.add_text(name, height=room_h, dxfattribs={"layer": "A-TEXT", "style": style}).set_placement(
            (c.x, c.y + 0.7 * room_h), align=TextEntityAlignment.MIDDLE_CENTER
        )
        msp.add_text(
            f"{area:.2f} m²", height=room_h * 0.8, dxfattribs={"layer": "A-TEXT", "style": style}
        ).set_placement((c.x, c.y - 0.7 * room_h), align=TextEntityAlignment.MIDDLE_CENTER)

    for d in spec.doors:
        pl = pls[d.id]
        if pl is not None:
            _draw_door(msp, spec, d, pl)
    for w in spec.windows:
        pl = pls[w.id]
        if pl is not None:
            _draw_window(msp, pl)

    _draw_dims(msp, spec)

    if spec.frame:
        minx, miny, maxx, maxy = spec.bbox()
        dim_h = _dim_h(spec)
        m = spec.dimensions.offset + ROW_GAP_TEXTS * dim_h + FRAME_MARGIN
        fx0, fy0, fx1, fy1 = minx - m, miny - m, maxx + m, maxy + m
        msp.add_lwpolyline(
            [(fx0, fy0), (fx1, fy0), (fx1, fy1), (fx0, fy1)], close=True, dxfattribs={"layer": "X-FRAME"}
        )
        if spec.title:
            msp.add_text(
                spec.title, height=TITLE_PRINT_MM * ps, dxfattribs={"layer": "X-FRAME", "style": style}
            ).set_placement((fx1 - 6 * ps, fy0 + 6 * ps), align=TextEntityAlignment.BOTTOM_RIGHT)
    return doc


def write_floorplan(spec: FloorPlanSpec, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    render_floorplan(spec).saveas(path)
    return path


def _polys(geom) -> list[Polygon]:
    if geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return list(geom.geoms)
    return [g for g in getattr(geom, "geoms", []) if isinstance(g, Polygon) and g.area > 0]


def door_geometry(spec: FloorPlanSpec, d, pl: Placement):
    """(鉸點, 關閉方向 u, 開啟方向 n)：門往 swing_into 房間開，鉸點在該房間側的牆面。"""
    # n_out 由 locate 的第一個房間（between 中第一個房間）往外；外牆門的 swing_room 必為該房間
    first = d.rooms()[0]
    toward_first = (-pl.n_out[0], -pl.n_out[1])
    n_in = toward_first if d.swing_room() == first else pl.n_out
    face = 0.0 if pl.exterior else pl.thickness / 2  # 外牆參考線 = 內緣 = 室內側牆面
    base = pl.a if d.hinge == "start" else pl.b
    hinge = _mv(base, n_in, face)
    u = pl.seg.u
    closed = u if d.hinge == "start" else (-u[0], -u[1])
    return hinge, closed, n_in


def _swing_zone(spec: FloorPlanSpec, d, pl: Placement) -> Polygon:
    """門扇掃過的方形範圍（房名避開用）。"""
    hinge, closed, opened = door_geometry(spec, d, pl)
    w = d.width
    far = _mv(_mv(hinge, closed, w), opened, w)
    return Polygon([hinge, _mv(hinge, closed, w), far, _mv(hinge, opened, w)]).buffer(100)


def _draw_door(msp, spec: FloorPlanSpec, d, pl: Placement) -> None:
    hinge, closed, opened = door_geometry(spec, d, pl)
    tip = _mv(hinge, opened, d.width)
    msp.add_line(hinge, tip, dxfattribs={"layer": "A-DOOR"})
    a_closed = math.degrees(math.atan2(closed[1], closed[0]))
    a_open = math.degrees(math.atan2(opened[1], opened[0]))
    cross = closed[0] * opened[1] - closed[1] * opened[0]
    start, end = (a_closed, a_open) if cross > 0 else (a_open, a_closed)
    msp.add_arc(hinge, d.width, start, end, dxfattribs={"layer": "A-DOOR"})


def _draw_window(msp, pl: Placement) -> None:
    n, t = pl.n_out, pl.thickness
    a, b = pl.a, pl.b
    msp.add_lwpolyline([a, b, _mv(b, n, t), _mv(a, n, t)], close=True, dxfattribs={"layer": "A-WIND"})
    msp.add_line(_mv(a, n, t / 2), _mv(b, n, t / 2), dxfattribs={"layer": "A-WIND"})


# ---------------------------------------------------------------- 尺寸


def _dim_h(spec: FloorPlanSpec) -> float:
    d = spec.dimensions
    return d.text_height if d.text_height is not None else DIM_TEXT_PRINT_MM * spec.print_scale


def chain_stations(spec: FloorPlanSpec, axis: int) -> list[float]:
    """尺寸鏈站點（axis 0 = x、1 = y）：外緣兩端 + 與該軸垂直的內牆中心線；過短的段併掉。"""
    minx, miny, maxx, maxy = spec.bbox()
    lo, hi = (minx, maxx) if axis == 0 else (miny, maxy)
    other = 1 - axis
    cands = sorted(
        {
            round(s.start[axis], 3)
            for s in interior_wall_lines(spec)
            if abs(s.start[axis] - s.end[axis]) < 1e-3 and abs(s.start[other] - s.end[other]) > 0
        }
    )
    min_seg = CHAIN_MIN_TEXTS * _dim_h(spec)
    out = [lo]
    for c in cands:
        if c - out[-1] >= min_seg and hi - c >= min_seg:
            out.append(c)
    out.append(hi)
    return out


def _draw_dims(msp, spec: FloorPlanSpec) -> None:
    d = spec.dimensions
    dim_h = _dim_h(spec)
    ps = spec.print_scale
    override = {
        "dimtxt": dim_h,
        "dimasz": dim_h * 0.6,
        "dimexo": 2.0 * ps,
        "dimexe": 2.0 * ps,
        "dimgap": 1.2 * ps,
        "dimdec": 0,
        "dimlfac": 1,
    }
    attribs = {"layer": "A-DIMS"}
    minx, miny, maxx, maxy = spec.bbox()

    def hdim(x0, x1, y):
        msp.add_linear_dim(
            base=(x0, y), p1=(x0, miny), p2=(x1, miny), angle=0, dimstyle=DIMSTYLE,
            override=override, dxfattribs=attribs,
        ).render()  # fmt: skip

    def vdim(y0, y1, x):
        msp.add_linear_dim(
            base=(x, y0), p1=(minx, y0), p2=(minx, y1), angle=90, dimstyle=DIMSTYLE,
            override=override, dxfattribs=attribs,
        ).render()  # fmt: skip

    row1 = d.offset
    row2 = d.offset + (ROW_GAP_TEXTS * dim_h if d.room_chains else 0.0)
    if d.room_chains:
        xs = chain_stations(spec, 0)
        if len(xs) > 2:
            for a, b in zip(xs, xs[1:], strict=False):
                hdim(a, b, miny - row1)
        ys = chain_stations(spec, 1)
        if len(ys) > 2:
            for a, b in zip(ys, ys[1:], strict=False):
                vdim(a, b, minx - row1)
    if d.auto_overall:
        hdim(minx, maxx, miny - row2)
        vdim(miny, maxy, minx - row2)
