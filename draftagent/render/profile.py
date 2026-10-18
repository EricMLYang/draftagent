"""ProfileSpec → 排水系統縱斷面圖 DXF（ezdxf）。純函式、確定性，不得呼叫 LLM。

座標 = **圖紙 mm**（$INSUNITS=4，1:1 出圖）：
    x = 樁號原點 x + (station_m − 第一樁) × 1000 / h_scale
    y = 基準線 y + (el − datum_el) × 1000 / v_scale

圖層
- H-PROF：地面線（實線）、溝底線（粗實線）、溝頂線、節點（人孔/井）示意框、管線高程短線
- H-PROF-WL：水位線（DASHED）—設計水位、起抽/停機水位
- H-TEXT：節點名、特殊高程文字、引線、圖名、比例文字、流向、附註
- H-GRID：高程尺、刻度與數字、高程格線、表格帶格線、樁號格
- H-TABL：表格帶列名與數值（TEXT，不用 ACAD_TABLE / MLEADER）
- H-DIMS：保留（本圖不放 DIMENSION；距離以表格帶呈現）
- X-FRAME / X-TTLB：見 render/sheet.py
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from ezdxf.document import Drawing
from shapely.geometry import LineString
from shapely.geometry import box as shp_box

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
from draftagent.schema.profile import (
    PROFILE_TABLE_ROWS,
    WATER_LEVEL_KINDS,
    ProfileSpec,
    format_station,
)

# 名稱 → (ACI, linetype)：向後相容的名稱清單；實際屬性以 render/codes/layers.yaml 為準
LAYERS: dict[str, tuple[int, str]] = {
    "H-PROF": (7, "Continuous"),
    "H-PROF-WL": (5, "DASHED"),
    "H-TEXT": (7, "Continuous"),
    "H-GRID": (8, "Continuous"),
    "H-TABL": (7, "Continuous"),
    "H-DIMS": (7, "Continuous"),
    "H-HATC": (8, "Continuous"),
}
TEXT_H = 2.5  # 一般文字高 mm
MIN_TEXT_H = 2.0  # 印出字高下限 mm（legibility_check）
LABEL_H = 3.0  # 節點名
TITLE_H = 5.0
LABEL_COL = 40.0  # 表格帶列名欄寬（兼高程尺數字區）
PAD_L, PAD_R = 8.0, 12.0  # 第一 / 最後樁距表格邊
NODE_ROW_H = {"station": 17.0}  # 直書數值列高；未列者用 _ELEV_ROW_H
_ELEV_ROW_H = 13.0
SEG_ROW_H = 8.0
_TICK_MIN_MM = 8.0  # 高程刻度最小間距


@dataclass
class _Box:
    x0: float
    y0: float
    x1: float
    y1: float


def _row_heights() -> list[tuple[str, float]]:
    out = []
    for key in PROFILE_TABLE_ROWS:
        if key in ("slope", "length", "section"):
            out.append((key, SEG_ROW_H))
        else:
            out.append((key, NODE_ROW_H.get(key, _ELEV_ROW_H)))
    return out


def _tick_step(v_scale: int) -> float:
    for step in (0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0):
        if step * 1000 / v_scale >= _TICK_MIN_MM:
            return step
    return 10.0


def _place_label(x, y, w, obstacles, *, xlim, ylim, gap=6.0, pad=0.6):
    """回傳 (tx, ty, right, moved)：文字框左下角。先右後左，dy 依 0, −s, +s, −2s… 交替。"""
    s = TEXT_H * 0.8
    sides = []
    if x + gap + w <= xlim[1]:
        sides.append(True)
    if x - gap - w >= xlim[0]:
        sides.append(False)
    sides = sides or [True]
    for k in range(40):
        dy = 0.0 if k == 0 else s * ((k + 1) // 2) * (-1 if k % 2 else 1)
        for right in sides:
            tx = x + gap if right else x - gap - w
            ty = y + 0.6 + dy
            if ty < ylim[0] or ty + TEXT_H > ylim[1]:
                continue
            cand = shp_box(tx - pad, ty - pad, tx + w + pad, ty + TEXT_H * 1.3 + pad)
            if not any(cand.intersects(o) for o in obstacles):
                return tx, ty, right, k != 0
    return x + gap, y + 0.6, True, False


def fmt_el(v: float) -> str:
    return f"{v:.2f}"


def _label(msp, txt, x, y, obstacles, xlim, ylim, *, gap=6.0, ex=0.0) -> None:
    """避讓放置「… EL=xx.xx」文字；位移過就畫引線（標示端 → 文字底線）。"""
    w = est_text_width(txt, TEXT_H)
    tx, ty, right, moved = _place_label(x, y, w, obstacles, xlim=xlim, ylim=ylim, gap=gap)
    add_text(msp, txt, (tx, ty), TEXT_H, "H-TEXT", Align.BOTTOM_LEFT)
    obstacles.append(shp_box(tx, ty, tx + w, ty + TEXT_H * 1.3))
    if moved:
        e = x + ex if right else x - ex
        near, far = (tx, tx + w) if right else (tx + w, tx)
        leader = [(e, y), (near, ty - 0.6), (far, ty - 0.6)]
        msp.add_lwpolyline(leader, dxfattribs={"layer": "H-TEXT", "lineweight": LW_THIN})
        obstacles.append(LineString(leader))


def _wl_symbol(msp, x, y) -> None:
    msp.add_lwpolyline(
        [(x - 1.2, y + 2.0), (x + 1.2, y + 2.0), (x, y)],
        close=True,
        dxfattribs={"layer": "H-PROF-WL", "lineweight": LW_THIN},
    )


COL_GAP = 4.0  # 構造物邊到標示欄的距離 mm
COL_STEP = TEXT_H * 1.45  # 標示欄行距


def _struct_side(st, others=()) -> str:
    """標示欄位置：井放左側（右側通常是出流口 / 受納溝），其餘放右側。

    例外：滯洪池右側（下游）另有構造物（如放流井）且高程範圍重疊 → 標示欄改放左側，
    避免文字壓進井體。
    """
    if st.kind == "well":
        return "L"
    for o in others:
        if o is st or o.x1_m <= st.x1_m:
            continue
        if o.x0_m >= st.x1_m - 1e-6 and o.bottom_el < st.top_el and st.bottom_el < o.top_el:
            return "L"
    return "R"


def _struct_items(st) -> list[tuple[str, float, float | None]]:
    """構造物標示欄的 (文字, 標示點 EL, 標示點樁號 None=構造物邊)，依 EL 由高而低。"""
    items = [(f"{st.label}頂 EL={fmt_el(st.top_el)}", st.top_el, None)]
    items += [(f"{m.label} EL={fmt_el(m.el)}", m.el, None) for m in st.marks]
    items.append((f"{st.label}底 EL={fmt_el(st.bottom_el)}", st.bottom_el, None))
    if st.pump_station_m is not None:
        items.append(("抽水機（P）", st.bottom_el, st.pump_station_m))
    return sorted(items, key=lambda it: -it[1])


def _struct_col_width(st) -> float:
    return max(est_text_width(t, TEXT_H) for t, _, _ in _struct_items(st))


def _draw_structures(msp, spec: ProfileSpec, X, Y, y_min: float, avoid=None):
    """畫構造物＋標示欄，回傳障礙物（折線 / 文字框）。

    - detention：池體矩形（粗線）＋設計滿水位以下斜線陰影（有效水深）＋水位虛線 / ▽；
      有 pump_station_m 時畫抽水機（圓 + P）與出水立管（至 pump_outlet_el，未給 → 池頂）
    - well：井體矩形
    - ditch：U 形斷面（左右壁 + 底）＋設計水位虛線
    標示欄：構造物右側（井為左側）一欄，依高程排序、最小行距 COL_STEP，引線連到標示點。
    """
    obs: list = []
    prof = {"layer": "H-PROF", "lineweight": LW_THICK}
    thin = {"layer": "H-TEXT", "lineweight": LW_THIN}
    for st in spec.structures:
        x0, x1 = X(st.x0_m), X(st.x1_m)
        yb, yt = Y(st.bottom_el), Y(st.top_el)
        if st.kind == "ditch":
            pts = [(x0, yt), (x0, yb), (x1, yb), (x1, yt)]
        else:
            pts = [(x0, yb), (x1, yb), (x1, yt), (x0, yt), (x0, yb)]
        msp.add_lwpolyline(pts, dxfattribs=prof)
        obs.append(LineString(pts))
        wl = next((m for m in st.marks if m.kind == "design_wl"), None)
        if st.kind == "detention" and wl is not None and wl.el > st.bottom_el:
            ywl = Y(min(wl.el, st.top_el))
            h = msp.add_hatch(color=256, dxfattribs={"layer": "H-HATC"})
            h.set_pattern_fill("ANSI31", color=256, scale=0.5)
            h.paths.add_polyline_path([(x0, yb), (x1, yb), (x1, ywl), (x0, ywl)], is_closed=True)
        n_wl = 0
        for m in st.marks:
            y = Y(m.el)
            if m.kind in WATER_LEVEL_KINDS:
                msp.add_line(
                    (x0, y), (x1, y), dxfattribs={"layer": "H-PROF-WL", "lineweight": LW_THIN}
                )
                # 多條水位（設計滿水位 / 溢流時最高水位 / 起抽 / 停機）▽ 錯開，避免高程相近時互疊
                _wl_symbol(msp, x0 + (x1 - x0) * (0.5 + 0.12 * (n_wl % 3)), y)
                n_wl += 1
            else:
                msp.add_line(
                    (x0, y), (x0 + 3.0, y), dxfattribs={"layer": "H-PROF", "lineweight": LW_THIN}
                )
        if st.pump_station_m is not None:
            px = X(st.pump_station_m)
            r = 1.9  # 「P」字高 2.0 mm（印出字高下限）須放得進圓內
            pc = (px, yb + r + 0.3)
            msp.add_circle(pc, r, dxfattribs={"layer": "H-PROF", "lineweight": LW_THIN})
            add_text(msp, "P", pc, MIN_TEXT_H, "H-TEXT", Align.MIDDLE_CENTER)
            ytop_pipe = Y(st.pump_outlet_el) if st.pump_outlet_el is not None else yt
            riser = [(px, pc[1] + r), (px, ytop_pipe)]
            msp.add_line(*riser, dxfattribs={"layer": "H-PROF", "lineweight": LW_MED})
            obs.append(LineString(riser))

        # ---- 標示欄
        side = _struct_side(st, spec.structures)
        items = _struct_items(st)
        edge = x1 if side == "R" else x0
        col = edge + COL_GAP if side == "R" else edge - COL_GAP
        marks_y = [Y(el) for _, el, _ in items]
        ys: list[float] = []
        for y in marks_y:
            ys.append(min(y, ys[-1] - COL_STEP) if ys else y)
        shift = sum(m - y for m, y in zip(marks_y, ys, strict=True)) / len(ys)
        ys = [y + shift for y in ys]
        floor = y_min  # 最低一行不得進入表格帶
        if avoid is not None:  # 也不得壓到基準高程文字（avoid = (x0, x1, y_top)）
            cw = max(est_text_width(t, TEXT_H) for t, _, _ in items) + 1.0
            cx0, cx1 = (col, col + cw) if side == "R" else (col - cw, col)
            if cx0 < avoid[1] and avoid[0] < cx1:
                floor = max(floor, avoid[2] + TEXT_H * 0.7 + 0.5)
        lift = max(0.0, floor - ys[-1])
        ys = [y + lift for y in ys]
        for (txt, _el, sta), ym, yy in zip(items, marks_y, ys, strict=True):
            w = est_text_width(txt, TEXT_H)
            mx = X(sta) if sta is not None else edge
            if side == "R":
                add_text(msp, txt, (col + 0.5, yy), TEXT_H, "H-TEXT", Align.MIDDLE_LEFT)
                leader = [(mx, ym), (col - 1.0, yy), (col, yy)]
                obs.append(shp_box(col, yy - TEXT_H * 0.7, col + w + 1.0, yy + TEXT_H * 0.7))
            else:
                add_text(msp, txt, (col - 0.5, yy), TEXT_H, "H-TEXT", Align.MIDDLE_RIGHT)
                leader = [(mx, ym), (col + 1.0, yy), (col, yy)]
                obs.append(shp_box(col - w - 1.0, yy - TEXT_H * 0.7, col, yy + TEXT_H * 0.7))
            msp.add_lwpolyline(leader, dxfattribs=thin)
            obs.append(LineString(leader))
    return obs


def _struct_pads(spec: ProfileSpec, hk: float, s_lo: float, s_hi: float) -> tuple[float, float]:
    """標示欄需要的左右額外空間 mm（相對於樁號範圍兩端）。"""
    pad_l, pad_r = PAD_L, PAD_R
    for st in spec.structures:
        need = COL_GAP + _struct_col_width(st) + 3.0
        if _struct_side(st, spec.structures) == "R":
            pad_r = max(pad_r, need - (s_hi - st.x1_m) * hk)
        else:
            pad_l = max(pad_l, need - (st.x0_m - s_lo) * hk)
    return pad_l, pad_r


def render_profile(spec: ProfileSpec) -> Drawing:
    doc = new_sheet_doc()
    doc.header["$LTSCALE"] = 1.0
    apply_layers(doc, LAYERS)  # 屬性以 render/codes/layers.yaml 為準
    msp = doc.modelspace()
    sheet = spec.sheet

    ix0, iy0, ix1, iy1 = draw_frame(doc, sheet)
    tb_w, tb_h = title_block_size(sheet)
    draw_title_block(doc, sheet, title=spec.title, scale_text=spec.scale_text())

    hk = 1000.0 / spec.h_scale  # 圖紙 mm / 實長 m（水平）
    vk = 1000.0 / spec.v_scale
    datum = spec.resolved_datum()
    step = _tick_step(spec.v_scale)
    top_el = math.ceil((spec.max_el() + step * 0.5) / step) * step

    rows = _row_heights()
    table_h = sum(h for _, h in rows)
    tx0 = ix0 + 5.0
    axis_x = tx0 + LABEL_COL
    s0 = spec.nodes[0].station_m
    s_lo, s_hi = spec.station_range()  # 含構造物
    pad_l, pad_r = _struct_pads(spec, hk, s_lo, s_hi)
    px0 = axis_x + pad_l
    span = (s_hi - s_lo) * hk
    tx1 = px0 + span + pad_r
    ty0 = iy0 + tb_h + 8.0
    ty1 = ty0 + table_h  # 表格帶頂 = 基準線
    py1 = ty1 + (top_el - datum) * vk  # 斷面區頂

    def X(st: float) -> float:
        return px0 + (st - s_lo) * hk

    def Y(el: float) -> float:
        return ty1 + (el - datum) * vk

    need_top = py1 + 8.0 + LABEL_H + 4.0 + TITLE_H + 2.0 + TEXT_H + 4.0
    if tx1 > ix1 - 5.0 or need_top > iy1 - 3.0:
        raise ValueError(
            f"縱斷面放不進 {sheet.size} 內框：需要 {tx1 - ix0:.0f}×{need_top - iy0:.0f} mm，"
            f"可用 {ix1 - ix0 - 5:.0f}×{iy1 - iy0 - 3:.0f} mm。請改用 A1、縮短單張範圍或調整 v_scale"
            "（h_scale 不得超過送審比例上限）"
        )

    grid = {"layer": "H-GRID", "lineweight": LW_THIN}
    tabl = {"layer": "H-TABL", "lineweight": LW_THIN}
    xs = [X(n.station_m) for n in spec.nodes]
    boxes: list[_Box] = []

    # ---------------- 高程尺 + 格線
    msp.add_line((axis_x, ty1), (axis_x, py1), dxfattribs={**grid, "lineweight": LW_MED})
    msp.add_line((axis_x, ty1), (tx1, ty1), dxfattribs={**grid, "lineweight": LW_MED})
    e = math.ceil((datum + 1e-9) / step) * step
    while e <= top_el + 1e-9:
        y = Y(e)
        msp.add_line((axis_x - 1.5, y), (axis_x, y), dxfattribs=grid)
        msp.add_line((axis_x, y), (tx1, y), dxfattribs={**grid, "color": 9})
        add_text(msp, fmt_el(e), (axis_x - 2.5, y), TEXT_H, "H-GRID", Align.MIDDLE_RIGHT)
        e = round(e + step, 6)
    add_text(msp, "EL (m)", (axis_x, py1 + 2.0), TEXT_H, "H-GRID", Align.BOTTOM_CENTER)
    datum_txt = f"基準高程 EL={fmt_el(datum)}"
    add_text(msp, datum_txt, (axis_x + 1.0, ty1 + 1.0), TEXT_H, "H-TEXT", Align.BOTTOM_LEFT)
    boxes.append(_Box(axis_x, ty1, axis_x + 1 + est_text_width(datum_txt, TEXT_H), ty1 + 4.5))

    # ---------------- 節點（人孔 / 井）
    wells = [st for st in spec.structures if st.kind == "well"]
    for n, x in zip(spec.nodes, xs, strict=True):
        yb, yg = Y(n.invert_el), Y(n.ground_el)
        msp.add_line((x, ty1), (x, yb), dxfattribs={**grid, "color": 9})
        if any(w.x0_m <= n.station_m <= w.x1_m for w in wells):
            hw = 0.0  # 井以構造物（實際外尺寸）表示
        elif n.kind == "outfall":
            msp.add_line((x, yb), (x, yg), dxfattribs={"layer": "H-PROF", "lineweight": LW_MED})
            hw = 0.0
        else:
            hw = max(1.5, 0.5 * hk)  # 井寬示意 1.0 m（最少 3 mm）
            msp.add_lwpolyline(
                [(x - hw, yb), (x + hw, yb), (x + hw, yg), (x - hw, yg)],
                close=True,
                dxfattribs={"layer": "H-PROF", "lineweight": LW_MED},
            )
        boxes.append(_Box(x - hw, yb, x + hw, yg))
        y_lab = Y(max(v for v in (n.ground_el, n.top_el, n.road_center_el) if v is not None)) + 3.0
        add_text(msp, n.label, (x, y_lab), LABEL_H, "H-TEXT", Align.BOTTOM_CENTER)
        w = est_text_width(n.label, LABEL_H)
        boxes.append(_Box(x - w / 2, y_lab, x + w / 2, y_lab + LABEL_H))

    # ---------------- 地面線 / 溝底線 / 溝頂線
    prof_lines = [
        [(x, Y(n.ground_el)) for n, x in zip(spec.nodes, xs, strict=True)],
        [(x, Y(n.invert_el)) for n, x in zip(spec.nodes, xs, strict=True)],
    ]
    msp.add_lwpolyline(
        prof_lines[0],
        dxfattribs={"layer": "H-PROF", "lineweight": LW_MED},
    )
    msp.add_lwpolyline(
        prof_lines[1],
        dxfattribs={"layer": "H-PROF", "lineweight": LW_THICK},
    )
    for i, seg in enumerate(spec.segments):
        a, b = spec.nodes[i], spec.nodes[i + 1]
        if a.top_el is not None and b.top_el is not None:
            ya, yb_ = Y(a.top_el), Y(b.top_el)
        else:  # 無溝頂高程 → 以溝底 + 溝高/管徑 畫內頂線
            ya, yb_ = Y(a.invert_el + seg.size_m), Y(b.invert_el + seg.size_m)
        msp.add_line((xs[i], ya), (xs[i + 1], yb_), dxfattribs={"layer": "H-PROF"})
        prof_lines.append([(xs[i], ya), (xs[i + 1], yb_)])

    # ---------------- 構造物（滯洪池 / 井 / 受納側溝）
    db = boxes[0]  # 基準高程文字框
    struct_obs = _draw_structures(msp, spec, X, Y, ty1 + TEXT_H, avoid=(db.x0, db.x1, db.y1))

    # ---------------- 特殊高程（進水管 / 放流管 / 溢流 / 水位）
    # 文字避讓：障礙 = 已放文字框、井框、地面/溝底/溝頂線、構造物；自標示點右側（放不下改左側）起，
    # 上下交替位移找第一個不碰任何障礙的位置，位移過就畫引線。
    obstacles: list = [shp_box(b.x0, b.y0, b.x1, b.y1) for b in boxes]
    obstacles += [LineString(pl) for pl in prof_lines]
    obstacles += struct_obs
    xlim, ylim = (axis_x + 1.0, tx1 - 1.0), (ty1 + 0.5, py1 - 0.5)
    for lv in sorted(spec.levels, key=lambda v: (v.station_m, -v.el)):
        x, y = X(lv.station_m), Y(lv.el)
        wl = lv.kind in WATER_LEVEL_KINDS
        layer = "H-PROF-WL" if wl else "H-PROF"
        attribs = {"layer": layer, "lineweight": LW_THIN}
        if wl:
            attribs["ltscale"] = 1.5
        msp.add_line((x - 5.0, y), (x + 5.0, y), dxfattribs=attribs)
        obstacles.append(LineString([(x - 5.0, y), (x + 5.0, y)]))
        if wl:  # ▽ 水位符號
            msp.add_lwpolyline(
                [(x - 1.2, y + 2.0), (x + 1.2, y + 2.0), (x, y)],
                close=True,
                dxfattribs={"layer": "H-PROF-WL", "lineweight": LW_THIN},
            )
        _label(msp, f"{lv.label} EL={fmt_el(lv.el)}", x, y, obstacles, xlim, ylim, ex=5.0)

    # ---------------- 表格帶
    msp.add_lwpolyline(
        [(tx0, ty0), (tx1, ty0), (tx1, ty1), (tx0, ty1)],
        close=True,
        dxfattribs={**tabl, "lineweight": LW_MED},
    )
    msp.add_line((axis_x, ty0), (axis_x, ty1), dxfattribs=tabl)
    slopes, lengths = spec.slopes_pct(), spec.lengths()
    y = ty1
    for key, h in rows:
        yc = y - h / 2
        add_text(
            msp,
            PROFILE_TABLE_ROWS[key],
            (tx0 + 2.0, yc),
            TEXT_H,
            "H-TABL",
            Align.MIDDLE_LEFT,
            max_width=LABEL_COL - 4.0,
        )
        for x in xs:  # 樁號格線
            msp.add_line((x, y - h), (x, y), dxfattribs=grid)
        if key in ("slope", "length", "section"):
            for i, seg in enumerate(spec.segments):
                val = {
                    "slope": f"{slopes[i]:.2f}%",
                    "length": f"{lengths[i]:.2f}",
                    "section": seg.section,
                }[key]
                cx = (xs[i] + xs[i + 1]) / 2
                add_text(msp, val, (cx, yc), TEXT_H, "H-TABL", max_width=xs[i + 1] - xs[i] - 2.0)
        else:
            for n, x in zip(spec.nodes, xs, strict=True):
                v = {
                    "station": n.station_m,
                    "ground": n.ground_el,
                    "top": n.top_el,
                    "invert": n.invert_el,
                    "road": n.road_center_el,
                }[key]
                if v is None:
                    continue
                val = format_station(v) if key == "station" else fmt_el(v)
                # 直書，置於樁號格線左側，避免被格線穿過
                add_text(
                    msp,
                    val,
                    (x - TEXT_H * 0.5 - 0.6, yc),
                    TEXT_H,
                    "H-TABL",
                    rotation=90.0,
                    max_width=h - 2.0,
                )
        y -= h
        msp.add_line((tx0, y), (tx1, y), dxfattribs=tabl)

    # ---------------- 圖名 / 比例 / 流向
    cx = (tx0 + tx1) / 2
    y_title = need_top - TEXT_H - 4.0 - TITLE_H
    add_text(msp, spec.title, (cx, y_title), TITLE_H, "H-TEXT", Align.BOTTOM_CENTER)
    add_text(msp, spec.scale_text(), (cx, y_title - 2.0), TEXT_H + 0.5, "H-TEXT", Align.TOP_CENTER)
    w_title = est_text_width(spec.title, TITLE_H)
    # 流向箭頭放圖名右側；放不下（窄縱斷面）就放左側——不得壓到圖名文字
    fx = cx + w_title / 2 + 25.0
    if fx > ix1 - 5.0:
        fx = cx - w_title / 2 - 4.0
    fy = y_title + TITLE_H / 2
    msp.add_line((fx - 18.0, fy), (fx, fy), dxfattribs={"layer": "H-TEXT", "lineweight": LW_MED})
    arrow = [(fx, fy), (fx - 3.0, fy + 1.0), (fx - 3.0, fy - 1.0)]
    msp.add_lwpolyline(arrow, close=True, dxfattribs={"layer": "H-TEXT"})
    hatch = msp.add_hatch(color=7, dxfattribs={"layer": "H-TEXT"})
    hatch.paths.add_polyline_path(arrow, is_closed=True)
    add_text(msp, "流向", (fx - 9.0, fy + 1.0), TEXT_H, "H-TEXT", Align.BOTTOM_CENTER)

    # ---------------- 比例尺 / 附註 / 指北針（圖簽左側帶狀區）
    strip_y = iy0 + 6.0
    if sheet.show_scale_bar:
        _, _, bx1, _ = draw_scale_bar(
            doc, (ix0 + 10.0, strip_y + 14.0), spec.h_scale, label=f"水平 1/{spec.h_scale}"
        )
        draw_scale_bar(
            doc,
            (bx1 + 12.0, strip_y + 8.0),
            spec.v_scale,
            label=f"垂直 1/{spec.v_scale}",
            vertical=True,
            target_mm=20.0,
        )
    notes = [
        "附註：",
        f"1. 高程單位 m（EL），樁號以 {spec.nodes[0].label} 為 {format_station(s0)} 起算。",
        "2. 坡度 S 由溝底高程與距離計算（順坡為正）；流向為樁號遞增方向。",
    ]
    nx = ix1 - tb_w - 112.0
    for k, line in enumerate(notes):
        add_text(
            msp,
            line,
            (nx, iy0 + tb_h - 4.0 - k * (TEXT_H + 2.0)),
            TEXT_H,
            "H-TEXT",
            Align.TOP_LEFT,
            max_width=105.0,
        )
    if sheet.north_arrow:  # 縱斷面預設不畫（None → false）
        draw_north_arrow(doc, (ix1 - 15.0, iy1 - 18.0))
    return doc


def write_profile(spec: ProfileSpec, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    render_profile(spec).saveas(path)
    return path
