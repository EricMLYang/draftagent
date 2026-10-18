"""DrainageNetworkSpec → 排水系統平面設計圖 + 排水系統集水區分析圖（1 個 DXF、2 個 paperspace）。

純函式、確定性，不得呼叫 LLM。

座標 / 單位
-----------
- modelspace = **TWD97 TM2 座標原值 m**（$INSUNITS = 6），可與測量 DXF、TWD97 shapefile 直接對位。
- paperspace（layout「平面設計圖」「集水區分析圖」）= **圖紙 mm**，各一個 viewport 依比例顯示 modelspace：
  viewport 比例 = 視埠高(mm) / view_height(m)，比例分母 = 1000 / 比例。
- 模型文字高（m）= 圖紙文字高(mm) × 比例分母 / 1000；符號 block 以圖紙 mm 定義、插入比例同上。
- 同一 modelspace 由兩個 viewport 以不同比例顯示：只屬於某張圖的註記放在專屬圖層，
  另一個 viewport 以 VP freeze 凍結（平面註記 C-STRM-TEXT / C-STRM-FLOW / C-ROAD-TEXT / C-STRM-STOR；
  集水區註記 C-CTCH / C-CTCH-TEXT / C-CTCH-FLOW）。

圖層
- modelspace：C-PROP 基地界線、A-BLDG 建物外框、C-STRM 新設管溝、C-STRM-EXST 既有排水（虛線）、
  C-STRM-NODE 節點符號（block：DR-<kind>，ATTRIB ID / TOP / INV）、C-STRM-FLOW 流向箭頭、
  C-STRM-TEXT 管段 / 節點 / 既有排水標註、C-STRM-STOR 滯洪設施範圍與 hatch、C-ROAD 道路中心線、
  C-ROAD-TEXT 道路中心樁高程、C-CTCH 集水分區界線、C-CTCH-TEXT 分區標註、C-CTCH-FLOW 分區流向
- paperspace：X-FRAME / X-TTLB（render/sheet.py）、X-VPORT 視埠、X-GRID 座標刻度（E / N）、
  C-LGND 圖例、C-TABL 表格（線 + TEXT）、X-TEXT 圖名 / 比例 / 附註

圖面契約（verifier 依此讀回；改動須同步 verify/drainage_plan_check.py）
- 管段標籤：單一 TEXT「<id> <斷面> S=<x.xx>% L=<xx.xx> m」（C-STRM-TEXT）
- 節點標籤：同一 x、由上而下的 TEXT 群：「<id> <種類>」「頂EL=<x.xx>」「底EL=<x.xx>」
  （縱斷面路徑上另有「樁號 <0+000.00>」；出流口另有「接<既有排水 id>」，受納溝有設計水深時再加
  「受納溝設計水位 EL=<x.xx>」）
- 分區標註：polygon 內的 TEXT「<id>」「<x.xx> m²」「<x.xxxx> ha」「C=<x.xx>」（C-CTCH-TEXT）
- 座標刻度：TEXT「E 280,510」（TOP_CENTER，x = 刻度位置）、「N 2,768,520」（旋轉 90°，y = 刻度位置）
- 表格：C-TABL 上同一列的 TEXT 中心 y 相同；第一列為欄名
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from ezdxf import units
from ezdxf.document import Drawing
from shapely.geometry import LineString, Point, Polygon
from shapely.geometry import box as shp_box
from shapely.ops import polylabel

from draftagent.drainage import sheet_vocab as V
from draftagent.drainage.derive import (
    catchment_rows,
    profile_path,
    quantities,
    storage_volume_m3,
)
from draftagent.drainage.hydraulics import (
    RECEIVE_MAX_DIST_M,
    receiving_point,
    storage_profile_levels,
)
from draftagent.drainage.model import NODE_KIND_NAME, DrainageNetworkSpec
from draftagent.drainage.network import (
    link_length,
    link_slope,
    site_area,
    stationing,
)
from draftagent.hydro.drawing_codes import load_drawing_codes
from draftagent.render.sheet import (
    LW_MED,
    LW_THICK,
    LW_THIN,
    Align,
    add_text,
    draw_frame,
    draw_north_arrow,
    draw_scale_bar,
    draw_title_block,
    est_text_width,
    new_sheet_doc,
    title_block_size,
)
from draftagent.render.standards import apply_layers
from draftagent.schema.profile import format_station

PLAN_LAYOUT = "平面設計圖"
CATCH_LAYOUT = "集水區分析圖"
PLAN_KEY = "drainage_plan"
CATCH_KEY = "catchment_map"

MODEL_LAYERS: dict[str, tuple[int, str]] = {
    "C-PROP": (1, "DA-CHAIN"),
    "A-BLDG": (8, "Continuous"),
    "C-STRM": (5, "Continuous"),
    "C-STRM-EXST": (3, "DA-DASH"),
    "C-STRM-NODE": (7, "Continuous"),
    "C-STRM-FLOW": (5, "Continuous"),
    "C-STRM-TEXT": (7, "Continuous"),
    "C-STRM-STOR": (4, "Continuous"),
    "C-ROAD": (8, "DA-CENTER"),
    "C-ROAD-TEXT": (8, "Continuous"),
    "C-CTCH": (6, "DA-DASH"),
    "C-CTCH-TEXT": (6, "Continuous"),
    "C-CTCH-FLOW": (6, "Continuous"),
}
PAPER_LAYERS: dict[str, tuple[int, str]] = {
    "X-VPORT": (8, "Continuous"),
    "X-GRID": (8, "Continuous"),
    "C-LGND": (7, "Continuous"),
    "C-TABL": (7, "Continuous"),
    "X-TEXT": (7, "Continuous"),
}
PLAN_ONLY = ("C-STRM-TEXT", "C-STRM-FLOW", "C-ROAD-TEXT", "C-STRM-STOR")
CATCH_ONLY = ("C-CTCH", "C-CTCH-TEXT", "C-CTCH-FLOW")
# 自訂線型：圖紙 mm 定義（[總長, 線, −空白, …]）；model 端以 entity ltscale = 分母/1000 換算
LINETYPES = {
    "DA-DASH": ([4.5, 3.0, -1.5], "Dashed __ __ __"),
    "DA-CHAIN": ([12.0, 8.0, -1.5, 1.0, -1.5], "Chain ____ _ ____"),
    "DA-CENTER": ([16.0, 11.0, -2.0, 1.0, -2.0], "Center _____ _ _____"),
}
NODE_BLOCK = {k: f"DR-{k.upper()}" for k in NODE_KIND_NAME}
NODE_TAGS = ("ID", "TOP", "INV")
NORTH_BLOCK = "NORTH-ARROW"

TEXT_MM = 2.5  # 一般註記字高（圖紙 mm）
SMALL_MM = 2.2  # 座標刻度
TITLE_MM = 5.0
SYM_MM = 3.2  # 節點符號外框（圖紙 mm）
ARROW_L_MM, ARROW_W_MM = 3.2, 1.6
LINE_GAP = 1.45  # 多行文字行距（× 字高）
PANEL_W = 170.0  # 右側欄寬（= A3 圖簽寬）
TICK_MIN_MM = 30.0
VIEW_PAD_M = 2.0  # viewport 在圖面範圍外加的邊界 m（最小值）
VIEW_PAD_MAX_M = 6.0  # 圖紙放得下時加大邊界（給標籤避讓空間），由大到小取第一個放得下的
# 標籤避讓搜尋（圖紙 mm）：第一圈離 anchor LABEL_GAP_MM，每圈加 LABEL_RING_MM，共 LABEL_RINGS 圈。
# verifier 以「LABEL_GAP_MM + (LABEL_RINGS − 1) × LABEL_RING_MM + 字寬」當標籤離自己物件的最大距離。
LABEL_GAP_MM = 1.2
LABEL_RING_MM = 3.0
LABEL_RINGS = 16


def fmt2(v: float) -> str:
    return f"{v:.2f}"


def fmt_coord(v: float) -> str:
    return f"{v:,.0f}"


DRAIN_MODE_NAME = {"gravity": "重力排放", "gravity_pump": "重力 + 抽水", "pump": "抽水排放"}


def storage_note_lines(spec: DrainageNetworkSpec, no: int) -> list[str]:
    """附註：滯洪設施明確給定的高程（推導值不標，避免把假設畫成設計）。每行 ≤ 3 項。"""
    st = spec.storage
    if st is None:
        return []
    items = [f"池底EL={fmt2(st.bottom_el)}"] if st.bottom_el is not None else []
    if st.top_el is not None:
        items.append(f"池頂EL={fmt2(st.top_el)}")
    for label, el, _ in storage_profile_levels(spec):
        if label == "溢流口" and st.overflow_at == "inlet_well":
            label = V.OVERFLOW_AT_INLET_LABEL
        items.append(f"{label}EL={fmt2(el)}")
    for label, el in (  # 銜接管（明確給定者才標）
        ("入池進流管底", st.inflow_invert_el),
        (V.PUMP_OUTLET_LABEL, st.pump_outlet_invert_el),
        (V.OVERFLOW_PIPE_LABEL, st.overflow_pipe_invert_el),
    ):
        if el is not None:
            items.append(f"{label}EL={fmt2(el)}")
    if st.pump_q_cms is not None:
        items.append(f"抽水量 Q={st.pump_q_cms:.4f} m³/s")
    if not items:
        return []
    out = [f"{no}. {st.id}（{DRAIN_MODE_NAME[st.drain_mode]}）高程："]
    out += ["    " + "、".join(items[i : i + 3]) for i in range(0, len(items), 3)]
    return out


def link_label(spec: DrainageNetworkSpec, lk) -> str:
    s = link_slope(spec, lk) * 100
    return f"{lk.id} {lk.section.text()} S={s:.2f}% L={link_length(spec, lk):.2f} m"


def node_label_lines(spec: DrainageNetworkSpec, node, station: float | None) -> list[str]:
    lines = [
        f"{node.id} {NODE_KIND_NAME[node.kind]}",
        f"頂EL={fmt2(node.top)}",
        f"底EL={fmt2(node.invert_el)}",
    ]
    if station is not None:
        lines.append(f"樁號 {format_station(station)}")
    return lines


# ---------------------------------------------------------------- doc setup


def _new_doc() -> Drawing:
    doc = new_sheet_doc()
    doc.units = units.M  # modelspace = TWD97 m
    doc.header["$MEASUREMENT"] = 1
    doc.header["$LTSCALE"] = 1.0
    doc.header["$PSLTSCALE"] = 0  # model 線型以 model 單位 × entity ltscale
    for name, (pattern, desc) in LINETYPES.items():
        if name not in doc.linetypes:
            doc.linetypes.add(name, pattern=pattern, description=desc)
    # 圖層屬性（顏色 / 線型 / 線寬 / 出圖）以 render/codes/layers.yaml 為準，並設 $LWDISPLAY=1
    apply_layers(doc, {**MODEL_LAYERS, **PAPER_LAYERS})
    _define_node_blocks(doc)
    return doc


def _define_node_blocks(doc: Drawing) -> None:
    """節點符號（圖紙 mm 為 1 單位）；ATTDEF ID / TOP / INV 不可見（數值另以 TEXT 標註）。"""
    r = SYM_MM / 2
    lay = {"layer": "C-STRM-NODE", "lineweight": LW_MED}
    for kind, name in NODE_BLOCK.items():
        blk = doc.blocks.new(name, base_point=(0, 0))
        sq = [(-r, -r), (r, -r), (r, r), (-r, r)]
        if kind == "catch_basin":  # 方框 + 對角線
            blk.add_lwpolyline(sq, close=True, dxfattribs=lay)
            blk.add_line((-r, -r), (r, r), dxfattribs=lay)
            blk.add_line((-r, r), (r, -r), dxfattribs=lay)
        elif kind == "manhole":  # 圓
            blk.add_circle((0, 0), r, dxfattribs=lay)
            blk.add_circle((0, 0), r * 0.35, dxfattribs=lay)
        elif kind == "existing_mh":  # 圓 + 十字
            blk.add_circle((0, 0), r, dxfattribs=lay)
            blk.add_line((-r, 0), (r, 0), dxfattribs=lay)
            blk.add_line((0, -r), (0, r), dxfattribs=lay)
        elif kind == "storage_inlet":  # 方框 + 內三角
            blk.add_lwpolyline(sq, close=True, dxfattribs=lay)
            blk.add_lwpolyline(
                [(-r * 0.6, r * 0.5), (r * 0.6, r * 0.5), (0, -r * 0.6)], close=True, dxfattribs=lay
            )
        elif kind == "outlet_well":  # 雙框 + 實心內框
            blk.add_lwpolyline(sq, close=True, dxfattribs=lay)
            inner = [
                (-r * 0.55, -r * 0.55),
                (r * 0.55, -r * 0.55),
                (r * 0.55, r * 0.55),
                (-r * 0.55, r * 0.55),
            ]
            h = blk.add_hatch(color=7, dxfattribs={"layer": "C-STRM-NODE"})
            h.paths.add_polyline_path(inner, is_closed=True)
        elif kind == "outfall":  # 實心三角 + 圓
            blk.add_circle((0, 0), r, dxfattribs=lay)
            tri = [(-r * 0.7, r * 0.45), (r * 0.7, r * 0.45), (0, -r * 0.75)]
            h = blk.add_hatch(color=7, dxfattribs={"layer": "C-STRM-NODE"})
            h.paths.add_polyline_path(tri, is_closed=True)
        for tag in NODE_TAGS:
            blk.add_attdef(
                tag,
                insert=(r + 0.5, 0),
                height=TEXT_MM,
                dxfattribs={"layer": "C-STRM-NODE", "flags": 1, "style": "CJK"},
            )


# ---------------------------------------------------------------- placement


@dataclass
class _Ctx:
    msp: object
    k: float  # m per 圖紙 mm（= 分母 / 1000）
    obstacles: list
    bounds: Polygon | None = None  # viewport 可見範圍（標籤不得超出被裁切）

    @property
    def h(self) -> float:
        return TEXT_MM * self.k

    def blocked(self, geom) -> bool:
        if self.bounds is not None and not self.bounds.contains(geom):
            return True
        return any(geom.intersects(o) for o in self.obstacles)


def _rot_box(cx, cy, w, h, ang_deg):
    a = math.radians(ang_deg)
    ux, uy = math.cos(a), math.sin(a)
    vx, vy = -uy, ux
    pts = [
        (cx + sx * w / 2 * ux + sy * h / 2 * vx, cy + sx * w / 2 * uy + sy * h / 2 * vy)
        for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))
    ]
    return Polygon(pts)


def _text_wh(ctx: _Ctx, text: str, h: float | None = None) -> tuple[float, float]:
    h = h or ctx.h
    return est_text_width(text, h), h * 1.3


def _place_line_label(ctx: _Ctx, a, b, text: str, layer: str, accept=None) -> None:
    """沿線標註：候選 = 線上位置 × 兩側 × 兩種離線距；全被擋 → 水平文字 + 引線。

    accept(x, y)：插入點的額外條件（管段標籤：插入點須離自己管段最近，verifier 依此對位）。
    """
    (x0, y0), (x1, y1) = a, b
    L = math.hypot(x1 - x0, y1 - y0)
    ang = math.degrees(math.atan2(y1 - y0, x1 - x0))
    if ang > 90.0 + 1e-9 or ang <= -90.0 + 1e-9:
        ang = ang - 180.0 if ang > 0 else ang + 180.0
    w, hh = _text_wh(ctx, text)
    ux, uy = (x1 - x0) / L, (y1 - y0) / L
    nx, ny = -uy, ux
    pad = 0.6 * ctx.k
    for t in (0.5, 0.32, 0.68, 0.2, 0.8):
        for off in (hh / 2 + 1.2 * ctx.k, hh / 2 + 3.0 * ctx.k):
            for side in (1, -1):
                cx = x0 + ux * L * t + nx * off * side
                cy = y0 + uy * L * t + ny * off * side
                box = _rot_box(cx, cy, w + 2 * pad, hh + 2 * pad, ang)
                if not ctx.blocked(box) and (accept is None or accept(cx, cy)):
                    add_text(
                        ctx.msp, text, (cx, cy), ctx.h, layer, Align.MIDDLE_CENTER, rotation=ang
                    )
                    ctx.obstacles.append(_rot_box(cx, cy, w, hh, ang))
                    return
    mid = (x0 + ux * L / 2, y0 + uy * L / 2)
    _place_free_label(ctx, mid, [text], layer, leader=True, accept=accept)


def _place_free_label(
    ctx: _Ctx,
    anchor,
    lines: list[str],
    layer: str,
    *,
    r0: float = 0.0,
    leader: bool = True,
    accept=None,
) -> tuple[float, float]:
    """水平多行文字群（左對齊）放在 anchor 周圍第一個不碰障礙的位置；遠離時畫引線。

    accept(x, y)：第一行插入點（MIDDLE_LEFT）的額外條件。回傳文字群左上角。
    """
    ax, ay = anchor
    h = ctx.h
    step = h * LINE_GAP
    w = max(est_text_width(s, h) for s in lines)
    tot = step * (len(lines) - 1) + h * 1.3
    pad = 0.6 * ctx.k
    g = r0 + LABEL_GAP_MM * ctx.k
    best = None
    for ring in range(LABEL_RINGS):
        d = g + ring * LABEL_RING_MM * ctx.k
        # 16 方位：文字框中心 = anchor + dir·d + (dir.x·w/2, dir.y·tot/2)；先試正東 / 正西
        for j in (0, 8, 2, 6, 10, 14, 1, 7, 9, 15, 4, 12, 3, 5, 11, 13):
            t = math.radians(j * 22.5)
            ux, uy = math.cos(t), math.sin(t)
            ccx = ax + ux * d + ux * w / 2
            ccy = ay + uy * d + uy * tot / 2
            x, ytop = ccx - w / 2, ccy + tot / 2
            box = shp_box(x - pad, ytop - tot - pad, x + w + pad, ytop + pad)
            if not ctx.blocked(box) and (accept is None or accept(x, ytop - h * 1.15 + h / 2)):
                best = (x, ytop, ring)
                break
        if best:
            break
    if best is None:
        import sys

        print(f"[drainage_plan] 標籤無處可放，強制放置：{lines[0]}", file=sys.stderr)
        best = (ax + g, ay + tot / 2, 0)
    x, ytop, ring = best
    for i, s in enumerate(lines):
        add_text(ctx.msp, s, (x, ytop - h * 1.15 - i * step + h / 2), h, layer, Align.MIDDLE_LEFT)
    rect = shp_box(x, ytop - tot, x + w, ytop)
    ctx.obstacles.append(rect)
    if leader and ring > 0:
        near = rect.exterior.interpolate(rect.exterior.project(Point(ax, ay)))
        start = Point(ax, ay)
        seg = LineString([start, near])
        if seg.length > r0:
            sx = ax + (near.x - ax) * r0 / seg.length
            sy = ay + (near.y - ay) * r0 / seg.length
            ctx.msp.add_line(
                (sx, sy), (near.x, near.y), dxfattribs={"layer": layer, "lineweight": LW_THIN}
            )
            ctx.obstacles.append(LineString([(sx, sy), (near.x, near.y)]))
    return x, ytop


def _arrow(msp, tip, direction, k: float, layer: str) -> Polygon:
    """實心三角箭頭（尖端 tip、方向 direction 單位向量）；回傳其 polygon（障礙用）。"""
    ux, uy = direction
    nx, ny = -uy, ux
    L, W = ARROW_L_MM * k, ARROW_W_MM * k
    bx, by = tip[0] - ux * L, tip[1] - uy * L
    pts = [tip, (bx + nx * W / 2, by + ny * W / 2), (bx - nx * W / 2, by - ny * W / 2)]
    msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": layer})
    h = msp.add_hatch(color=256, dxfattribs={"layer": layer})
    h.paths.add_polyline_path(pts, is_closed=True)
    return Polygon(pts)


def _poly(msp, coords, layer, k, *, lw=LW_THIN, ltscale=True):
    attribs = {"layer": layer, "lineweight": lw}
    if ltscale:
        attribs["ltscale"] = k
    return msp.add_lwpolyline(list(coords), close=True, dxfattribs=attribs)


# ---------------------------------------------------------------- model content


def _model_extents(
    spec: DrainageNetworkSpec, *, include_context: bool
) -> tuple[float, float, float, float]:
    xs = [p[0] for p in spec.site_boundary]
    ys = [p[1] for p in spec.site_boundary]
    xs += [n.x for n in spec.nodes]
    ys += [n.y for n in spec.nodes]
    if include_context:
        for d in spec.existing_drains:
            xs += [p.x for p in d.points]
            ys += [p.y for p in d.points]
        xs += [p.x for p in spec.road_centerline_els]
        ys += [p.y for p in spec.road_centerline_els]
    return min(xs), min(ys), max(xs), max(ys)


def _draw_model(doc: Drawing, spec: DrainageNetworkSpec) -> None:
    msp = doc.modelspace()
    kp = spec.scales.plan / 1000.0
    kc = spec.scales.catchment / 1000.0
    # ---------------- 共用幾何
    _poly(msp, spec.site_boundary, "C-PROP", kp, lw=LW_THICK)
    base_obs: list = [LineString([*spec.site_boundary, spec.site_boundary[0]])]
    for b in spec.building_footprints:
        _poly(msp, b, "A-BLDG", kp, lw=LW_MED, ltscale=False)
        base_obs.append(LineString([*b, b[0]]))
    if len(spec.road_centerline_els) >= 2:
        pts = [(p.x, p.y) for p in spec.road_centerline_els]
        msp.add_lwpolyline(
            pts, dxfattribs={"layer": "C-ROAD", "lineweight": LW_THIN, "ltscale": kp}
        )
        base_obs.append(LineString(pts))

    link_lines = []
    for lk in spec.links:
        a, b = spec.node(lk.from_), spec.node(lk.to)
        layer = "C-STRM-EXST" if lk.existing else "C-STRM"
        attribs = {"layer": layer, "lineweight": LW_THICK if not lk.existing else LW_MED}
        if lk.existing:
            attribs["ltscale"] = kp
        msp.add_line((a.x, a.y), (b.x, b.y), dxfattribs=attribs)
        link_lines.append(LineString([(a.x, a.y), (b.x, b.y)]))
    for d in spec.existing_drains:
        pts = [(p.x, p.y) for p in d.points]
        msp.add_lwpolyline(
            pts, dxfattribs={"layer": "C-STRM-EXST", "lineweight": LW_MED, "ltscale": kp}
        )
        link_lines.append(LineString(pts))

    for n in spec.nodes:
        ref = msp.add_blockref(
            NODE_BLOCK[n.kind],
            (n.x, n.y),
            dxfattribs={"layer": "C-STRM-NODE", "xscale": kp, "yscale": kp},
        )
        ref.add_auto_attribs({"ID": n.id, "TOP": fmt2(n.top), "INV": fmt2(n.invert_el)})
        for att in ref.attribs:
            att.dxf.flags = 1  # 不可見
            att.dxf.layer = "C-STRM-NODE"

    _draw_plan_annotations(msp, spec, kp, base_obs, link_lines)
    _draw_catchment_annotations(msp, spec, kc, base_obs, link_lines)


def _map_region(spec: DrainageNetworkSpec) -> tuple[float, float, float, float]:
    """圖面 viewport 可用區（圖紙 mm）：內框扣右欄。"""
    ix0, iy0, ix1, iy1 = spec.sheet.inner()
    px0 = ix1 - PANEL_W
    return (ix0 + 12.0, iy0 + 9.0, px0 - 6.0, iy1 - 7.0)


def view_pad(spec: DrainageNetworkSpec, include_context: bool, denom: int) -> float:
    """viewport 邊界 m：VIEW_PAD_MAX_M → VIEW_PAD_M 由大到小取第一個放得下圖紙的（都放不下 → 最小值，
    由 _viewport raise）。"""
    ex0, ey0, ex1, ey1 = _model_extents(spec, include_context=include_context)
    rx0, ry0, rx1, ry1 = _map_region(spec)
    k = 1000.0 / denom
    pad = VIEW_PAD_MAX_M
    while pad > VIEW_PAD_M:
        if (ex1 - ex0 + 2 * pad) * k <= rx1 - rx0 and (ey1 - ey0 + 2 * pad) * k <= ry1 - ry0:
            return pad
        pad -= 1.0
    return VIEW_PAD_M


def _visible(spec: DrainageNetworkSpec, include_context: bool, k: float) -> Polygon:
    x0, y0, x1, y1 = _model_extents(spec, include_context=include_context)
    m = view_pad(spec, include_context, round(k * 1000)) - 1.0 * k
    return shp_box(x0 - m, y0 - m, x1 + m, y1 + m)


def grid_step(denom: int) -> int:
    """座標刻度間距 m：圖上間距 ≥ TICK_MIN_MM 的最小整數間距。"""
    k = 1000.0 / denom
    return next(s for s in (5, 10, 20, 25, 50, 100, 200, 500) if s * k >= TICK_MIN_MM)


def _grid_obstacles(bounds: Polygon, denom: int, k: float) -> list:
    """格網交點十字（paperspace 畫）在 model 的位置，當標籤障礙避免壓線。"""
    step = grid_step(denom)
    x0, y0, x1, y1 = bounds.bounds
    r = 2.0 * k
    out = []
    e = math.ceil(x0 / step) * step
    while e <= x1:
        n = math.ceil(y0 / step) * step
        while n <= y1:
            out.append(shp_box(e - r, n - r, e + r, n + r))
            n += step
        e += step
    return out


def _draw_plan_annotations(msp, spec: DrainageNetworkSpec, k: float, base_obs, link_lines) -> None:
    ctx = _Ctx(msp=msp, k=k, obstacles=[*base_obs], bounds=_visible(spec, True, k))
    ctx.obstacles += _grid_obstacles(ctx.bounds, spec.scales.plan, k)
    sym = SYM_MM * k
    ctx.obstacles += [ln.buffer(0.35 * k) for ln in link_lines]
    for n in spec.nodes:
        ctx.obstacles.append(shp_box(n.x - sym / 2, n.y - sym / 2, n.x + sym / 2, n.y + sym / 2))

    # 滯洪設施範圍 + hatch
    if spec.storage is not None:
        st = spec.storage
        _poly(msp, st.polygon, "C-STRM-STOR", k, lw=LW_MED, ltscale=False)
        h = msp.add_hatch(color=4, dxfattribs={"layer": "C-STRM-STOR"})
        h.set_pattern_fill("ANSI31", scale=2.5 * k / 3.175 * 1.0, color=4)
        h.paths.add_polyline_path(st.polygon, is_closed=True)
        for c in st.cells or []:
            _poly(msp, c, "C-STRM-STOR", k, lw=LW_THIN, ltscale=False)
        if st.outlet_well_polygon:
            _poly(msp, st.outlet_well_polygon, "C-STRM-STOR", k, lw=LW_MED, ltscale=False)
        ctx.obstacles.append(Polygon(st.polygon))  # 標籤不壓 hatch

    # 流向箭頭（每條 link 中點；既有排水每段中點）
    for lk in spec.links:
        a, b = spec.node(lk.from_), spec.node(lk.to)
        L = math.hypot(b.x - a.x, b.y - a.y)
        u = ((b.x - a.x) / L, (b.y - a.y) / L)
        tip = (
            (a.x + b.x) / 2 + u[0] * ARROW_L_MM * k / 2,
            (a.y + b.y) / 2 + u[1] * ARROW_L_MM * k / 2,
        )
        ctx.obstacles.append(_arrow(msp, tip, u, k, "C-STRM-FLOW").buffer(0.3 * k))
    for d in spec.existing_drains:
        for p, q in zip(d.points, d.points[1:], strict=False):
            L = math.hypot(q.x - p.x, q.y - p.y)
            u = ((q.x - p.x) / L, (q.y - p.y) / L)
            for t in (0.2, 0.8):
                tip = (p.x + (q.x - p.x) * t, p.y + (q.y - p.y) * t)
                ctx.obstacles.append(_arrow(msp, tip, u, k, "C-STRM-FLOW").buffer(0.3 * k))

    # 管段標籤（插入點須離自己管段最近：verifier 以最近管段對位標籤）
    segs = {
        lk.id: LineString([(spec.node(lk.from_).x, spec.node(lk.from_).y), (spec.node(lk.to).x, spec.node(lk.to).y)])
        for lk in spec.links
    }

    def own_nearest(lid):
        def ok(x, y):
            p = Point(x, y)
            d = segs[lid].distance(p)
            return all(g.distance(p) > d + 1e-6 for o, g in segs.items() if o != lid)

        return ok

    for lk in sorted(spec.links, key=lambda x: link_length(spec, x)):
        a, b = spec.node(lk.from_), spec.node(lk.to)
        _place_line_label(
            ctx, (a.x, a.y), (b.x, b.y), link_label(spec, lk), "C-STRM-TEXT", accept=own_nearest(lk.id)
        )

    # 節點標籤（縱斷面路徑上加樁號）
    try:
        path = profile_path(spec)
        st_of = dict(zip(path, stationing(spec, path), strict=True))
    except ValueError:
        st_of = {}
    for n in spec.nodes:
        lines = node_label_lines(spec, n, st_of.get(n.id))
        if n.kind == "outfall":
            near = [
                d
                for d in spec.existing_drains
                if LineString([(p.x, p.y) for p in d.points]).distance(Point(n.x, n.y)) < 1.0
            ]
            if near:
                lines.append(f"接{near[0].id}")
            rcv = receiving_point(spec, n.x, n.y) if spec.existing_drains else None
            if rcv is not None and rcv.design_wl is not None and rcv.distance_m <= RECEIVE_MAX_DIST_M:
                lines.append(f"受納溝設計水位 EL={fmt2(rcv.design_wl)}")
        _place_free_label(ctx, (n.x, n.y), lines, "C-STRM-TEXT", r0=sym * 0.75)

    # 滯洪設施標註
    if spec.storage is not None:
        st = spec.storage
        sp = Polygon(st.polygon)
        x0_, y0_, x1_, y1_ = sp.bounds
        c = Point(x1_, (y0_ + y1_) / 2)  # 自東緣中點引出
        lines = [
            f"{st.id}（{DRAIN_MODE_NAME[st.drain_mode]}）",
            f"有效水深 {st.effective_depth:.2f} m",
            f"V={storage_volume_m3(spec):.2f} m³",
        ]
        _place_free_label(ctx, (c.x, c.y), lines, "C-STRM-TEXT", r0=0.0)

    # 既有排水：名稱 + 斷面 + 各點頂 / 底高程
    for d in spec.existing_drains:
        p0, p1 = d.points[0], d.points[1]
        _place_line_label(
            ctx,
            (p0.x, p0.y),
            (p1.x, p1.y),
            f"{d.id} {d.name} {d.section.text()}（既有）",
            "C-STRM-TEXT",
        )
        for p in d.points:
            if p.top_el is None or p.invert_el is None:
                continue
            ctx.msp.add_circle((p.x, p.y), 0.6 * k, dxfattribs={"layer": "C-STRM-TEXT"})
            _place_free_label(
                ctx,
                (p.x, p.y),
                [f"頂EL={fmt2(p.top_el)}", f"底EL={fmt2(p.invert_el)}"],
                "C-STRM-TEXT",
                r0=0.6 * k,
            )

    # 道路中心樁高程
    for p in spec.road_centerline_els:
        msp.add_circle((p.x, p.y), 0.6 * k, dxfattribs={"layer": "C-ROAD-TEXT"})
        _place_free_label(ctx, (p.x, p.y), [f"路中EL={fmt2(p.el)}"], "C-ROAD-TEXT", r0=0.6 * k)


def _draw_catchment_annotations(
    msp, spec: DrainageNetworkSpec, k: float, base_obs, link_lines
) -> None:
    ctx = _Ctx(msp=msp, k=k, obstacles=[*base_obs], bounds=_visible(spec, False, k))
    ctx.obstacles += _grid_obstacles(ctx.bounds, spec.scales.catchment, k)
    ctx.obstacles += [ln.buffer(0.3 * k) for ln in link_lines]
    sym = SYM_MM * k * spec.scales.plan / spec.scales.catchment
    for n in spec.nodes:
        ctx.obstacles.append(shp_box(n.x - sym, n.y - sym, n.x + sym, n.y + sym))
    rows = {r.id: r for r in catchment_rows(spec)}
    h = ctx.h
    step = h * LINE_GAP
    for s in spec.subcatchments:
        _poly(msp, s.polygon, "C-CTCH", k, lw=LW_MED)
    for s in spec.subcatchments:
        poly = Polygon(s.polygon)
        r = rows[s.id]
        lines = [s.id, f"{r.area_m2:.2f} m²", f"{r.area_ha:.4f} ha", f"C={r.c:.2f}"]
        w = max(est_text_width(t, h) for t in lines)
        tot = step * (len(lines) - 1) + h * 1.3
        cx, cy = _spot_in_polygon(ctx, poly, w, tot + h * 0.4, base_obs)
        x0, ytop = cx - w / 2, cy + tot / 2
        add_text(msp, lines[0], (cx, ytop - h * 0.65), h * 1.4, "C-CTCH-TEXT", Align.MIDDLE_CENTER)
        for i, t in enumerate(lines[1:], start=1):
            add_text(
                msp,
                t,
                (cx, ytop - h * 1.15 - i * step + h / 2 - h * 0.2),
                h,
                "C-CTCH-TEXT",
                Align.MIDDLE_CENTER,
            )
        box = shp_box(x0, ytop - tot - h * 0.2, x0 + w, ytop + h * 0.2)
        ctx.obstacles.append(box)
        # 流向箭頭：自文字框邊緣指向出流節點
        on = spec.node(s.outlet_node)
        tgt = Point(on.x, on.y)
        dist = Point(cx, cy).distance(tgt)
        if dist < 1e-6:
            continue
        ux, uy = (on.x - cx) / dist, (on.y - cy) / dist
        start = box.exterior.intersection(LineString([(cx, cy), (on.x, on.y)]))
        sx, sy = (
            (start.x, start.y)
            if start.geom_type == "Point"
            else (cx + ux * w / 2, cy + uy * tot / 2)
        )
        sx, sy = sx + ux * 1.0 * k, sy + uy * 1.0 * k
        tip = (on.x - ux * (sym + 0.8 * k), on.y - uy * (sym + 0.8 * k))
        if (tip[0] - sx) * ux + (tip[1] - sy) * uy <= ARROW_L_MM * k:
            continue
        msp.add_line((sx, sy), tip, dxfattribs={"layer": "C-CTCH-FLOW", "lineweight": LW_MED})
        _arrow(msp, tip, (ux, uy), k, "C-CTCH-FLOW")


def _spot_in_polygon(
    ctx: _Ctx, poly: Polygon, w: float, hh: float, base_obs
) -> tuple[float, float]:
    """分區標註中心：自 polylabel 起，由近而遠找文字框完全在分區內且不碰障礙的點。"""
    pole = polylabel(poly, tolerance=0.05)
    inner = poly.buffer(-0.8 * ctx.k)
    x0, y0, x1, y1 = poly.bounds
    stp = max(ctx.k * 1.5, 0.2)
    cands = [(pole.x, pole.y)]
    nx, ny = int((x1 - x0) / stp) + 1, int((y1 - y0) / stp) + 1
    grid = [(x0 + i * stp, y0 + j * stp) for i in range(nx) for j in range(ny)]
    grid.sort(key=lambda p: (p[0] - pole.x) ** 2 + (p[1] - pole.y) ** 2)
    cands += grid
    # 分區界線本身不算障礙（文字框只要在分區內即可）
    obs = [o for o in ctx.obstacles if not any(o is b for b in base_obs)]
    for cx, cy in cands:
        box = shp_box(cx - w / 2, cy - hh / 2, cx + w / 2, cy + hh / 2)
        if inner.contains(box) and not any(box.intersects(o) for o in obs):
            return cx, cy
    import sys

    print(f"[drainage_plan] 分區標註找不到空位，置於 polylabel：{poly.bounds}", file=sys.stderr)
    return pole.x, pole.y


# ---------------------------------------------------------------- paperspace


@dataclass
class _Sheet:
    name: str
    key: str
    denom: int
    drawing_no: str


def _setup_layout(doc: Drawing, name: str, spec: DrainageNetworkSpec):
    if name == PLAN_LAYOUT and "Layout1" in doc.layouts:
        doc.layouts.rename("Layout1", name)
        psp = doc.layouts.get(name)
    else:
        psp = doc.layouts.new(name)
    w, h = spec.sheet.paper()
    psp.page_setup(size=(w, h), margins=(0, 0, 0, 0), units="mm")
    return psp


def _viewport(psp, spec, region, extents, denom: int, frozen: tuple[str, ...], pad_m: float):
    """在 region（圖紙 mm）內置中放 viewport，比例 1/denom；回傳 (vp 框 bbox, view center)。"""
    rx0, ry0, rx1, ry1 = region
    ex0, ey0, ex1, ey1 = extents
    k = 1000.0 / denom  # mm / m
    need_w = (ex1 - ex0 + 2 * pad_m) * k
    need_h = (ey1 - ey0 + 2 * pad_m) * k
    avail_w, avail_h = rx1 - rx0, ry1 - ry0
    if need_w > avail_w + 1e-6 or need_h > avail_h + 1e-6:
        raise ValueError(
            f"1/{denom} 下圖面範圍 {need_w:.0f}×{need_h:.0f} mm 放不進 {spec.sheet.size} 可用 "
            f"{avail_w:.0f}×{avail_h:.0f} mm；請改 A1 或較大比例分母（不得超過送審門檻）"
        )
    vw, vh = need_w, need_h
    cx, cy = (rx0 + rx1) / 2, (ry0 + ry1) / 2
    vcx, vcy = (ex0 + ex1) / 2, (ey0 + ey1) / 2
    vp = psp.add_viewport(
        center=(cx, cy), size=(vw, vh), view_center_point=(vcx, vcy), view_height=vh / k
    )
    vp.dxf.layer = "X-VPORT"
    vp.frozen_layers = list(frozen)
    bbox = (cx - vw / 2, cy - vh / 2, cx + vw / 2, cy + vh / 2)
    lay = {"layer": "X-GRID", "lineweight": LW_MED}
    x0, y0, x1, y1 = bbox
    psp.add_lwpolyline([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], close=True, dxfattribs=lay)
    return bbox, (vcx, vcy), k


def _grid_ticks(psp, bbox, view_c, k: float) -> None:
    """TWD97 座標刻度：上下緣 E、左右緣 N；刻度短線向內 2 mm，數值在框外。"""
    x0, y0, x1, y1 = bbox
    vcx, vcy = view_c
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    step = grid_step(round(1000.0 / k))
    e_lo, e_hi = vcx + (x0 - cx) / k, vcx + (x1 - cx) / k
    n_lo, n_hi = vcy + (y0 - cy) / k, vcy + (y1 - cy) / k
    lay = {"layer": "X-GRID", "lineweight": LW_THIN}
    e = math.ceil(e_lo / step) * step
    while e <= e_hi:
        x = cx + (e - vcx) * k
        if x0 + 3 < x < x1 - 3:
            psp.add_line((x, y0), (x, y0 + 2.0), dxfattribs=lay)
            psp.add_line((x, y1), (x, y1 - 2.0), dxfattribs=lay)
            add_text(psp, f"E {fmt_coord(e)}", (x, y0 - 1.2), SMALL_MM, "X-GRID", Align.TOP_CENTER)
        e += step
    n = math.ceil(n_lo / step) * step
    while n <= n_hi:
        y = cy + (n - vcy) * k
        if y0 + 3 < y < y1 - 3:
            psp.add_line((x0, y), (x0 + 2.0, y), dxfattribs=lay)
            psp.add_line((x1, y), (x1 - 2.0, y), dxfattribs=lay)
            add_text(
                psp,
                f"N {fmt_coord(n)}",
                (x0 - 1.2, y),
                SMALL_MM,
                "X-GRID",
                Align.BOTTOM_CENTER,
                rotation=90.0,
            )
        n += step
    # 格網交點十字（圖紙 mm，細線）
    e = math.ceil(e_lo / step) * step
    while e <= e_hi:
        n = math.ceil(n_lo / step) * step
        x = cx + (e - vcx) * k
        while n <= n_hi:
            y = cy + (n - vcy) * k
            if x0 + 3 < x < x1 - 3 and y0 + 3 < y < y1 - 3:
                psp.add_line((x - 1.5, y), (x + 1.5, y), dxfattribs=lay)
                psp.add_line((x, y - 1.5), (x, y + 1.5), dxfattribs=lay)
            n += step
        e += step


def _table(
    psp,
    x0: float,
    ytop: float,
    cols: list[float],
    rows: list[tuple[str, ...]],
    title: str,
    *,
    row_h: float = 6.0,
) -> float:
    """線 + TEXT 表格（不用 ACAD_TABLE）；第一列為欄名。回傳表格底 y。"""
    add_text(psp, title, (x0 + sum(cols) / 2, ytop), 3.0, "C-TABL", Align.TOP_CENTER)
    y = ytop - 5.0
    width = sum(cols)
    lay = {"layer": "C-TABL", "lineweight": LW_THIN}
    n = len(rows)
    yb = y - n * row_h
    psp.add_lwpolyline(
        [(x0, yb), (x0 + width, yb), (x0 + width, y), (x0, y)],
        close=True,
        dxfattribs={**lay, "lineweight": LW_MED},
    )
    xs = [x0]
    for c in cols:
        xs.append(xs[-1] + c)
    for x in xs[1:-1]:
        psp.add_line((x, yb), (x, y), dxfattribs=lay)
    for i, row in enumerate(rows):
        yc = y - (i + 0.5) * row_h
        if i > 0:
            psp.add_line((x0, y - i * row_h), (x0 + width, y - i * row_h), dxfattribs=lay)
        for j, cell in enumerate(row):
            if not cell:
                continue
            add_text(
                psp,
                cell,
                ((xs[j] + xs[j + 1]) / 2, yc),
                TEXT_MM,
                "C-TABL",
                Align.MIDDLE_CENTER,
                max_width=cols[j] - 2.0,
            )
    return yb


def _legend(
    psp, doc, x0: float, ytop: float, items: list[tuple[str, str, dict]], width: float
) -> float:
    """圖例（兩欄）。items = [(type, 說明, opts)]；type ∈ line / arrow / block / hatch。回傳底 y。"""
    add_text(psp, "圖例", (x0 + 2.0, ytop), 3.0, "C-LGND", Align.TOP_LEFT)
    row_h = 6.5
    col_w = width / 2
    y = ytop - 6.0
    rows = math.ceil(len(items) / 2)
    yb = y - rows * row_h
    psp.add_lwpolyline(
        [(x0, yb - 1), (x0 + width, yb - 1), (x0 + width, ytop + 2), (x0, ytop + 2)],
        close=True,
        dxfattribs={"layer": "C-LGND", "lineweight": LW_THIN},
    )
    for i, (typ, label, opt) in enumerate(items):
        col, row = i % 2, i // 2
        sx = x0 + 4.0 + col * col_w
        yc = y - (row + 0.5) * row_h
        if typ == "line":
            attribs = {
                "layer": "C-LGND",
                "lineweight": opt.get("lw", LW_MED),
                "color": opt["color"],
                "linetype": opt.get("lt", "Continuous"),
            }
            psp.add_line((sx, yc), (sx + 14.0, yc), dxfattribs=attribs)
        elif typ == "arrow":
            psp.add_line(
                (sx, yc), (sx + 10.0, yc), dxfattribs={"layer": "C-LGND", "color": opt["color"]}
            )
            pts = [
                (sx + 14.0, yc),
                (sx + 14.0 - ARROW_L_MM, yc + ARROW_W_MM / 2),
                (sx + 14.0 - ARROW_L_MM, yc - ARROW_W_MM / 2),
            ]
            psp.add_lwpolyline(
                pts, close=True, dxfattribs={"layer": "C-LGND", "color": opt["color"]}
            )
            h = psp.add_hatch(color=opt["color"], dxfattribs={"layer": "C-LGND"})
            h.paths.add_polyline_path(pts, is_closed=True)
        elif typ == "block":
            psp.add_blockref(opt["block"], (sx + 7.0, yc), dxfattribs={"layer": "C-LGND"})
        elif typ == "hatch":
            pts = [(sx, yc - 2.0), (sx + 14.0, yc - 2.0), (sx + 14.0, yc + 2.0), (sx, yc + 2.0)]
            psp.add_lwpolyline(pts, close=True, dxfattribs={"layer": "C-LGND", "color": 4})
            h = psp.add_hatch(color=4, dxfattribs={"layer": "C-LGND"})
            h.set_pattern_fill("ANSI31", scale=2.5 / 3.175, color=4)
            h.paths.add_polyline_path(pts, is_closed=True)
        add_text(
            psp,
            label,
            (sx + 17.0, yc),
            TEXT_MM,
            "C-LGND",
            Align.MIDDLE_LEFT,
            max_width=col_w - 22.0,
        )
    return yb - 1


def _sheet_common(
    doc, psp, spec: DrainageNetworkSpec, sh: _Sheet, include_context: bool, frozen
) -> tuple[float, float, float]:
    """圖框、圖簽、viewport、座標刻度、圖名 / 比例、指北針、比例尺。回傳右欄 (x0, 目前 y, 寬)。"""
    sheet = spec.sheet
    ix0, iy0, ix1, iy1 = draw_frame(doc, sheet, layout=psp)
    rule = load_drawing_codes().scale(sh.key)
    tb = sheet.title_block.model_copy(
        update={"drawing_title": rule.name, "drawing_no": sh.drawing_no}
    )
    sheet2 = sheet.model_copy(update={"title_block": tb})
    draw_title_block(doc, sheet2, title=rule.name, scale_text=f"1/{sh.denom}", layout=psp)

    px0 = ix1 - PANEL_W
    extents = _model_extents(spec, include_context=include_context)
    pad = view_pad(spec, include_context, sh.denom)
    bbox, vc, k = _viewport(psp, spec, _map_region(spec), extents, sh.denom, frozen, pad)
    _grid_ticks(psp, bbox, vc, k)

    y = iy1 - 6.0
    add_text(psp, rule.name, (px0 + PANEL_W / 2, y), TITLE_MM, "X-TEXT", Align.TOP_CENTER)
    y -= TITLE_MM + 3.0
    add_text(psp, f"比例 1/{sh.denom}", (px0 + PANEL_W / 2, y), 3.0, "X-TEXT", Align.TOP_CENTER)
    y -= 3.0 + 6.0
    if sheet.north_arrow is not False:
        draw_north_arrow(doc, (px0 + 18.0, y - 12.0), layout=psp)
    if sheet.show_scale_bar:
        draw_scale_bar(
            doc,
            (px0 + 45.0, y - 14.0),
            sh.denom,
            label=f"比例尺 1/{sh.denom}",
            layout=psp,
            target_mm=50.0,
        )
    y -= 30.0
    return px0, y, PANEL_W


def _notes(psp, x0: float, y: float, lines: list[str], ymin: float) -> float:
    for i, s in enumerate(lines):
        yy = y - i * 4.5
        if yy < ymin:
            raise ValueError("附註放不下：右欄空間不足")
        add_text(psp, s, (x0 + 2.0, yy), TEXT_MM, "X-TEXT", Align.TOP_LEFT, max_width=PANEL_W - 4.0)
    return y - len(lines) * 4.5


def render_drainage_plan(spec: DrainageNetworkSpec) -> Drawing:
    doc = _new_doc()
    _draw_model(doc, spec)
    kinds_present = [k for k in NODE_KIND_NAME if any(n.kind == k for n in spec.nodes)]
    _, tb_h = title_block_size(spec.sheet)
    ymin = spec.sheet.inner()[1] + tb_h + 4.0

    # ---------------- 平面設計圖
    psp = _setup_layout(doc, PLAN_LAYOUT, spec)
    sh = _Sheet(PLAN_LAYOUT, PLAN_KEY, spec.scales.plan, spec.drawing_no("plan"))
    px0, y, pw = _sheet_common(doc, psp, spec, sh, True, CATCH_ONLY)
    items = [
        ("line", "基地界線", {"color": 1, "lt": "DA-CHAIN", "lw": LW_THICK}),
        ("line", "建物外框", {"color": 8}),
        ("line", "新設排水管溝", {"color": 5, "lw": LW_THICK}),
        ("line", "既有排水設施", {"color": 3, "lt": "DA-DASH"}),
        ("arrow", "流向", {"color": 5}),
    ]
    if spec.storage is not None:
        items.append(("hatch", "滯洪設施範圍", {}))
    if spec.road_centerline_els:
        items.append(("line", "道路中心線", {"color": 8, "lt": "DA-CENTER", "lw": LW_THIN}))
    items += [("block", NODE_KIND_NAME[k], {"block": NODE_BLOCK[k]}) for k in kinds_present]
    y = _legend(psp, doc, px0 + 2.0, y, items, pw - 4.0) - 6.0
    qrows = [("項目", "規格", "數量", "單位")] + [r.cells() for r in quantities(spec)]
    y = _table(psp, px0 + 4.0, y, [28.0, 84.0, 26.0, 24.0], qrows, "設施尺寸數量一覽表") - 5.0
    _notes(
        psp,
        px0,
        y,
        [
            "附註：",
            "1. 座標系統 TWD97 TM2（EPSG:3826），單位 m；高程單位 m。",
            "2. 坡度 S、長度 L 由節點座標與底高程計算；數量由模型計算。",
            "3. 「頂EL」為渠頂（未給者為井頂地面）高程，「底EL」為溝底 / 管底高程。",
            "4. 樁號沿縱斷面路徑自上游起算，與排水系統縱斷面圖一致。",
            *storage_note_lines(spec, 5),
        ],
        ymin,
    )

    # ---------------- 集水區分析圖
    psp2 = _setup_layout(doc, CATCH_LAYOUT, spec)
    sh2 = _Sheet(CATCH_LAYOUT, CATCH_KEY, spec.scales.catchment, spec.drawing_no("catchment"))
    px0, y, pw = _sheet_common(doc, psp2, spec, sh2, False, PLAN_ONLY)
    items = [
        ("line", "基地界線", {"color": 1, "lt": "DA-CHAIN", "lw": LW_THICK}),
        ("line", "集水分區界線", {"color": 6, "lt": "DA-DASH"}),
        ("arrow", "分區逕流方向", {"color": 6}),
        ("line", "排水管溝", {"color": 5, "lw": LW_THICK}),
        ("line", "建物外框", {"color": 8}),
    ]
    items += [("block", NODE_KIND_NAME[k], {"block": NODE_BLOCK[k]}) for k in kinds_present]
    y = _legend(psp2, doc, px0 + 2.0, y, items, pw - 4.0) - 6.0
    crow = catchment_rows(spec)
    total = sum(r.area_m2 for r in crow)
    rows = [("分區", "土地使用", "C", "面積 m²", "面積 ha", "面積 坪", "出流至")]
    rows += [r.cells() for r in crow]
    rows.append(("合計", "", "", f"{total:.2f}", f"{total / 1e4:.4f}", f"{total * 0.3025:.2f}", ""))
    sa = site_area(spec)
    rows.append(("基地", "", "", f"{sa:.2f}", f"{sa / 1e4:.4f}", f"{sa * 0.3025:.2f}", ""))
    y = (
        _table(
            psp2, px0 + 4.0, y, [16.0, 40.0, 14.0, 24.0, 22.0, 24.0, 22.0], rows, "集水分區面積表"
        )
        - 5.0
    )
    _notes(
        psp2,
        px0,
        y,
        [
            "附註：",
            "1. 座標系統 TWD97 TM2（EPSG:3826），單位 m。",
            "2. 分區面積由分區界線 polygon 計算；1 坪 = 0.3025 × m²。",
            "3. 逕流係數 C 依桃園市雨水流出抑制設施設計參考手冊（草案）表 7-1。",
        ],
        ymin,
    )
    return doc


def write_drainage_plan(spec: DrainageNetworkSpec, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    render_drainage_plan(spec).saveas(path)
    return path
