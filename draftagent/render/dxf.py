"""PlanSpec → DXF（ezdxf）。純函式、確定性，不得呼叫 LLM。

圖層：A-WALL 牆輪廓（封閉 LWPOLYLINE，門窗處由輪廓繞過開口）、A-DOOR 門扇線 + 開啟弧、
A-WIND 窗、A-DIMS DIMENSION 實體、A-TEXT 房名、X-FRAME 圖框與圖名。0 層不放任何實體。
圖層顏色 / 線型 / 線寬以 render/codes/layers.yaml 為準（standards.apply_layers；A-WALL 0.50 mm），
文字用統一 CJK TrueType 樣式（standards.text_style）。
"""

from __future__ import annotations

import math
from pathlib import Path

import ezdxf
from ezdxf import units
from ezdxf.document import Drawing
from ezdxf.enums import TextEntityAlignment
from shapely.geometry import MultiPolygon, Point, Polygon
from shapely.ops import unary_union

from draftagent.render.standards import apply_layers, text_style
from draftagent.schema.plan import Opening, PlanSpec

# 名稱 → ACI 顏色：向後相容的名稱清單；實際屬性以 render/codes/layers.yaml 為準
LAYERS: dict[str, int] = {
    "A-WALL": 7,
    "A-DOOR": 3,
    "A-WIND": 4,
    "A-DIMS": 1,
    "A-TEXT": 2,
    "X-FRAME": 8,
}
DIMSTYLE = "EZDXF"  # ezdxf.new(setup=True) 內建
# 印出字高（圖紙 mm）；model 字高 = 印出字高 × spec.print_scale（1/50 → 2.5 mm = 125 model mm）。
# 製圖慣例，不是法規數值；下限見 render/codes/layers.yaml `text.min_print_height_mm`
TEXT_PRINT_MM = 2.5  # 房名
DIM_TEXT_PRINT_MM = 2.5  # 標註文字
TITLE_PRINT_MM = 3.75  # 圖名
FRAME_MARGIN = 1500.0
_EPS = 1.0  # 開口切割矩形往牆外/內多延伸 1 mm，避免殘留薄片


def render_plan(spec: PlanSpec) -> Drawing:
    doc = ezdxf.new("R2010", setup=True)
    doc.units = units.MM  # $INSUNITS = 4
    doc.header["$MEASUREMENT"] = 1  # metric
    doc.header["$LUNITS"] = 2  # decimal
    apply_layers(doc, LAYERS)  # 含 $LWDISPLAY = 1
    style = text_style(doc)
    msp = doc.modelspace()
    t = spec.wall_thickness
    ps = spec.print_scale
    room_h = TEXT_PRINT_MM * ps

    # --- 牆 ---
    for room in spec.rooms:
        outer = Polygon(room.outline())
        if not outer.is_valid:
            raise ValueError(f"room '{room.name}': polygon 自相交或無效")
        wall = outer.difference(outer.buffer(-t, join_style="mitre"))
        cuts = [
            _opening_rect(spec, op) for op in spec.openings if spec.room_of(op).name == room.name
        ]
        if cuts:
            wall = wall.difference(unary_union(cuts))
        polys = wall.geoms if isinstance(wall, MultiPolygon) else [wall]
        for poly in polys:
            for ring in [poly.exterior, *poly.interiors]:
                pts = [(round(float(x), 6), round(float(y), 6)) for x, y in list(ring.coords)[:-1]]
                msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": "A-WALL"})

        c = outer.representative_point() if not outer.centroid.within(outer) else outer.centroid
        msp.add_text(
            room.name, height=room_h, dxfattribs={"layer": "A-TEXT", "style": style}
        ).set_placement((c.x, c.y), align=TextEntityAlignment.MIDDLE_CENTER)

    # --- 門窗 ---
    for op in spec.openings:
        if op.type == "door":
            _draw_door(msp, spec, op)
        else:
            _draw_window(msp, spec, op)

    # --- 尺寸 ---
    minx, miny, maxx, maxy = spec.bbox()
    d = spec.dimensions
    if d.auto_overall:
        dim_h = d.text_height if d.text_height is not None else DIM_TEXT_PRINT_MM * ps
        override = {
            "dimtxt": dim_h,
            "dimasz": dim_h * 0.6,
            "dimexo": 2.0 * ps,
            "dimexe": 2.0 * ps,
            "dimgap": 1.2 * ps,
            "dimdec": 0,
            "dimlfac": 1,  # EZDXF 樣式預設 100（m→cm），mm 圖面必須為 1
        }
        attribs = {"layer": "A-DIMS"}
        msp.add_linear_dim(
            base=(minx, miny - d.offset),
            p1=(minx, miny),
            p2=(maxx, miny),
            angle=0,
            dimstyle=DIMSTYLE,
            override=override,
            dxfattribs=attribs,
        ).render()
        msp.add_linear_dim(
            base=(minx - d.offset, miny),
            p1=(minx, miny),
            p2=(minx, maxy),
            angle=90,
            dimstyle=DIMSTYLE,
            override=override,
            dxfattribs=attribs,
        ).render()

    # --- 圖框 ---
    if spec.frame:
        m = d.offset + FRAME_MARGIN
        fx0, fy0, fx1, fy1 = minx - m, miny - m, maxx + m, maxy + m
        msp.add_lwpolyline(
            [(fx0, fy0), (fx1, fy0), (fx1, fy1), (fx0, fy1)],
            close=True,
            dxfattribs={"layer": "X-FRAME"},
        )
        if spec.title:
            msp.add_text(
                spec.title,
                height=TITLE_PRINT_MM * ps,
                dxfattribs={"layer": "X-FRAME", "style": style},
            ).set_placement((fx1 - 6 * ps, fy0 + 6 * ps), align=TextEntityAlignment.BOTTOM_RIGHT)
    return doc


def write_plan(spec: PlanSpec, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    render_plan(spec).saveas(path)
    return path


# ---------------------------------------------------------------- geometry


def _frame(spec: PlanSpec, op: Opening):
    """回傳開口的 (a, b, u, n)：a/b 為外牆外緣上開口兩端，u 沿牆段方向，n 指向室內。"""
    room = spec.room_of(op)
    (x0, y0), (x1, y1) = room.segment(op.side, op.edge)
    length = math.hypot(x1 - x0, y1 - y0)
    u = ((x1 - x0) / length, (y1 - y0) / length)
    n = (-u[1], u[0])
    mid = ((x0 + x1) / 2, (y0 + y1) / 2)
    probe = Point(mid[0] + n[0] * spec.wall_thickness / 2, mid[1] + n[1] * spec.wall_thickness / 2)
    if not Polygon(room.outline()).contains(probe):
        n = (-n[0], -n[1])
    a = (x0 + u[0] * op.offset, y0 + u[1] * op.offset)
    b = (a[0] + u[0] * op.width, a[1] + u[1] * op.width)
    return a, b, u, n


def _add(p, v, s):
    return (p[0] + v[0] * s, p[1] + v[1] * s)


def _opening_rect(spec: PlanSpec, op: Opening) -> Polygon:
    a, b, _, n = _frame(spec, op)
    t = spec.wall_thickness
    return Polygon(
        [_add(a, n, -_EPS), _add(b, n, -_EPS), _add(b, n, t + _EPS), _add(a, n, t + _EPS)]
    )


def _draw_door(msp, spec: PlanSpec, op: Opening) -> None:
    a, b, u, n = _frame(spec, op)
    face = spec.wall_thickness if op.swing == "in" else 0.0
    hinge = _add(a if op.hinge == "start" else b, n, face)
    closed = u if op.hinge == "start" else (-u[0], -u[1])
    opened = n if op.swing == "in" else (-n[0], -n[1])
    tip = _add(hinge, opened, op.width)
    msp.add_line(hinge, tip, dxfattribs={"layer": "A-DOOR"})
    a_closed = math.degrees(math.atan2(closed[1], closed[0]))
    a_open = math.degrees(math.atan2(opened[1], opened[0]))
    cross = closed[0] * opened[1] - closed[1] * opened[0]
    start, end = (a_closed, a_open) if cross > 0 else (a_open, a_closed)
    msp.add_arc(hinge, op.width, start, end, dxfattribs={"layer": "A-DOOR"})


def _draw_window(msp, spec: PlanSpec, op: Opening) -> None:
    a, b, _, n = _frame(spec, op)
    t = spec.wall_thickness
    msp.add_lwpolyline(
        [a, b, _add(b, n, t), _add(a, n, t)], close=True, dxfattribs={"layer": "A-WIND"}
    )
    msp.add_line(_add(a, n, t / 2), _add(b, n, t / 2), dxfattribs={"layer": "A-WIND"})
