"""DetailSheetSpec → 排水系統各部詳圖 DXF（ezdxf）。純函式、確定性，不得呼叫 LLM。

座標與比例
----------
- 全部畫在 modelspace，座標 = **圖紙 mm**（$INSUNITS=4，1:1 出圖），與 render/sheet.py 的圖框一致。
- 每個詳圖有自己的比例分母 `scale`：圖紙座標 = 詳圖原點 + 實尺 mm / scale。
- **尺寸標註採 dimlfac 法**：DIMENSION 直接量圖紙上的距離，override `dimlfac = scale`，
  所以顯示值 = 圖紙量測值 × scale = 實尺 mm。沒有改用「實尺畫在 block 再縮放插入」，
  因為那會讓文字高、hatch 密度、線型比例都跟著縮放，每張詳圖都要反算；dimlfac 法只動標註。
  verifier（verify/detail_check.py）以 `get_measurement() × dimlfac` 重算顯示值並對照 spec。
- Hatch 密度以**圖紙 mm** 固定（與比例無關）：AR-CONC / EARTH / GRAVEL 的 ISO 定義乘上
  `HATCH_SCALE`，出圖後每張詳圖的材料符號大小一致。
- 高程：每個模板以一個參考高程把 `_el`（m）換成實尺 y（mm）：y = y_ref + (EL − EL_ref) × 1000。

圖層
----
- H-STRC：結構輪廓（粗）　H-HATC：材料 hatch　H-HIDN：隱藏線 / 背後輪廓（DASHED）
- H-WATR：水位線（DASHED）＋▽　H-PIPE：管線、閥件符號　H-RBAR：鋼筋示意
- H-GRND：地面線　H-CL：中心線 / 剖切線（CENTER）
- H-DIMS：DIMENSION（dimlfac = scale）　H-TEXT：引線註記（LINE+TEXT，不用 MLEADER）、EL 文字、
  詳圖標題、剖面代號、比例文字
- X-FRAME / X-TTLB：見 render/sheet.py

每個詳圖畫完後，其 modelspace 實體收進 DXF GROUP `DETAIL-<id>`，verifier 據此分辨各詳圖。

排版：`render_detail_sheet` 先在暫存 doc 量每個詳圖的 bbox，再以 shelf packing（由上而下、
由左而右，避開右下角圖簽）放進內框；放不下 raise ValueError。
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ezdxf.document import Drawing
from ezdxf.render import ARROWS
from shapely.geometry import LineString, Point, Polygon
from shapely.geometry import box as sbox
from shapely.ops import unary_union

from draftagent.render.sheet import (
    CJK_STYLE,
    LW_MED,
    LW_THICK,
    LW_THIN,
    Align,
    add_text,
    draw_frame,
    draw_title_block,
    est_text_width,
    new_sheet_doc,
    title_block_size,
)
from draftagent.render.standards import apply_layers
from draftagent.schema.detail import (
    DetailSheetSpec,
    DitchJointDetail,
    NameplateDetail,
    OrificePlateDetail,
    OutfallProtectionDetail,
    OutletWellDetail,
    RaftDetentionSection,
    UDitchSection,
)

# 名稱 → (ACI, linetype)：向後相容的名稱清單；實際屬性以 render/codes/layers.yaml 為準
LAYERS: dict[str, tuple[int, str]] = {
    "H-STRC": (7, "Continuous"),
    "H-HATC": (8, "Continuous"),
    "H-HIDN": (8, "DASHED"),
    "H-WATR": (5, "DASHED"),
    "H-PIPE": (7, "Continuous"),
    "H-RBAR": (1, "Continuous"),
    "H-GRND": (7, "Continuous"),
    "H-CL": (1, "CENTER"),
    "H-DIMS": (7, "Continuous"),
    "H-TEXT": (7, "Continuous"),
}
DIMSTYLE = "H-DTL"
GROUP_PREFIX = "DETAIL-"
TEXT_H = 2.5  # 註記字高 mm（圖紙）
DIM_TXT = 2.2
TITLE_H = 4.5
SCALE_H = 3.0
CALLOUT_GAP = 4.6  # 引線註記最小垂直間距
SHEET_GAP = 12.0  # 詳圖間距
# ISO pattern × scale → 圖紙 mm 上的合理密度（已用 PNG 預覽確認）
HATCH_SCALE = {"conc": 0.035, "earth": 0.6, "gravel": 0.09, "riprap": 0.2, "steel": 0.6}
HATCH_PATTERN = {
    "conc": "AR-CONC",
    "earth": "EARTH",
    "gravel": "GRAVEL",
    "riprap": "GRAVEL",
    "steel": "ANSI31",
}


def fmt_el(v: float) -> str:
    return f"{v:.2f}"


def el_text(label: str, el: float) -> str:
    return f"{label} EL={fmt_el(el)}"


# ---------------------------------------------------------------- 共用繪圖 context


@dataclass
class _Callout:
    tx: float  # 標示點（圖紙）
    ty: float
    text: str
    side: str  # "R" | "L"
    marker: str  # "dot" | "el" | "none"


@dataclass
class _Ctx:
    """單一詳圖的繪圖 context：實尺 mm → 圖紙 mm，並追蹤 bbox。"""

    msp: object
    ox: float
    oy: float
    scale: int
    ext: list[float] = field(default_factory=lambda: [math.inf, math.inf, -math.inf, -math.inf])
    callouts: list[_Callout] = field(default_factory=list)
    obs: list = field(default_factory=list)  # 已畫物件（圖紙 shapely），供文字避讓

    @property
    def k(self) -> float:
        return 1.0 / self.scale

    def P(self, x: float, y: float) -> tuple[float, float]:
        return (self.ox + x * self.k, self.oy + y * self.k)

    def grow(self, pts) -> None:
        for x, y in pts:
            self.ext[0] = min(self.ext[0], x)
            self.ext[1] = min(self.ext[1], y)
            self.ext[2] = max(self.ext[2], x)
            self.ext[3] = max(self.ext[3], y)

    def sub(self, dx_paper: float, dy_paper: float) -> _Ctx:
        """同一詳圖內的另一視圖（例：平面在剖面上方）；共用 ext。"""
        c = _Ctx(self.msp, self.ox + dx_paper, self.oy + dy_paper, self.scale)
        c.ext = self.ext
        c.obs = self.obs
        return c

    # -------- primitives（輸入實尺 mm）

    def poly(self, pts, layer="H-STRC", lw=LW_MED, close=True, paper=False, **attr):
        pp = list(pts) if paper else [self.P(x, y) for x, y in pts]
        self.grow(pp)
        self.obs.append(LineString(pp + pp[:1] if close else pp))
        return self.msp.add_lwpolyline(
            pp, close=close, dxfattribs={"layer": layer, "lineweight": lw, **attr}
        )

    def rect(self, x0, y0, x1, y1, layer="H-STRC", lw=LW_MED, **attr):
        return self.poly([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], layer, lw, True, **attr)

    def line(self, a, b, layer="H-STRC", lw=LW_THIN, paper=False, **attr):
        pa, pb = (a, b) if paper else (self.P(*a), self.P(*b))
        self.grow([pa, pb])
        self.obs.append(LineString([pa, pb]))
        return self.msp.add_line(pa, pb, dxfattribs={"layer": layer, "lineweight": lw, **attr})

    def circle(self, c, r, layer="H-STRC", lw=LW_THIN, paper=False):
        pc = c if paper else self.P(*c)
        rr = r if paper else r * self.k
        self.grow([(pc[0] - rr, pc[1] - rr), (pc[0] + rr, pc[1] + rr)])
        self.obs.append(Point(pc).buffer(rr, 8).exterior)
        return self.msp.add_circle(pc, rr, dxfattribs={"layer": layer, "lineweight": lw})

    def hatch_geom(self, geom, kind: str) -> None:
        """shapely (Multi)Polygon（實尺 mm）→ HATCH（外環 + 洞）。"""
        polys = getattr(geom, "geoms", [geom])
        for pg in polys:
            if pg.is_empty or pg.area <= 0:
                continue
            self.obs.append(Polygon([self.P(x, y) for x, y in pg.exterior.coords]))
            h = self.msp.add_hatch(color=256, dxfattribs={"layer": "H-HATC"})
            h.set_pattern_fill(HATCH_PATTERN[kind], color=256, scale=HATCH_SCALE[kind])
            h.paths.add_polyline_path([self.P(x, y) for x, y in pg.exterior.coords[:-1]], flags=1)
            for ring in pg.interiors:
                h.paths.add_polyline_path([self.P(x, y) for x, y in ring.coords[:-1]], flags=0)

    def outline_geom(self, geom, layer="H-STRC", lw=LW_MED) -> None:
        for pg in getattr(geom, "geoms", [geom]):
            if pg.is_empty:
                continue
            for ring in [pg.exterior, *pg.interiors]:
                self.poly(ring.coords[:-1], layer, lw)

    def solid_dot(self, pc, r=0.45):
        self.circle(pc, r, "H-TEXT", paper=True)
        h = self.msp.add_hatch(color=256, dxfattribs={"layer": "H-TEXT"})
        h.paths.add_edge_path().add_arc(pc, r, 0, 360)

    def text(
        self,
        s,
        pos,
        h=TEXT_H,
        align=Align.BOTTOM_LEFT,
        layer="H-TEXT",
        rotation=0.0,
        max_width=None,
    ):
        """pos 為圖紙座標。給 max_width 時以寬度因子壓縮（下限 0.5）。"""
        add_text(self.msp, s, pos, h, layer, align, rotation=rotation, max_width=max_width)
        w = est_text_width(s, h)
        if max_width is not None:
            w = min(w, max(max_width, 0.5 * w))
        x, y = pos
        name = align.name
        x0 = x - w if name.endswith("RIGHT") else x - w / 2 if name.endswith("CENTER") else x
        y0 = y - h if name.startswith("TOP") else y - h / 2 if name.startswith("MIDDLE") else y
        if rotation:
            self.grow([(x - h * 1.2, y - w), (x + h * 1.2, y + w)])
            self.obs.append(sbox(x - h * 1.2, y - w, x + h * 1.2, y + w))
        else:
            self.grow([(x0, y0 - 0.3 * h), (x0 + w, y0 + 1.3 * h)])
            self.obs.append(sbox(x0, y0 - 0.3 * h, x0 + w, y0 + 1.3 * h))

    # -------- 尺寸（實尺座標，off 為圖紙 mm，正 = +y / +x 方向）

    def _dim(self, p1, p2, base, angle, text_dx=0.0):
        override = {"dimlfac": float(self.scale)}
        meas = abs((p2[0] - p1[0]) if angle == 0 else (p2[1] - p1[1]))
        label_w = est_text_width(f"{meas * self.scale:.0f}", DIM_TXT) + 0.6
        loc = None
        if meas < label_w:  # 文字放不進兩條界線之間 → 改放尺寸線另一側（遠離圖形）
            # 相鄰的短尺寸（如碎石層 + 底板）外移文字會互疊 → 依序再往外錯開
            placed = self.__dict__.setdefault("_dim_out_boxes", [])
            for k in range(4):
                step = k * (DIM_TXT + 0.8)
                if angle == 0:
                    loc = ((p1[0] + p2[0]) / 2, base[1] - DIM_TXT * 0.5 - 1.0 - step)
                    bx_ = sbox(
                        loc[0] - label_w / 2,
                        loc[1] - DIM_TXT * 0.6,
                        loc[0] + label_w / 2,
                        loc[1] + DIM_TXT * 0.6,
                    )
                else:
                    loc = (base[0] + DIM_TXT * 0.5 + 1.0 + step, (p1[1] + p2[1]) / 2)
                    bx_ = sbox(
                        loc[0] - DIM_TXT * 0.6,
                        loc[1] - label_w / 2,
                        loc[0] + DIM_TXT * 0.6,
                        loc[1] + label_w / 2,
                    )
                if not any(bx_.intersects(o) for o in placed):
                    break
            placed.append(bx_)
        elif text_dx:
            if angle == 0:
                loc = ((p1[0] + p2[0]) / 2 + text_dx, base[1] + DIM_TXT * 0.5 + 0.6)
            else:
                loc = (base[0] - DIM_TXT * 0.5 - 0.6, (p1[1] + p2[1]) / 2 + text_dx)
        dim = self.msp.add_linear_dim(
            base=base,
            p1=p1,
            p2=p2,
            angle=angle,
            location=loc,
            dimstyle=DIMSTYLE,
            override=override,
            dxfattribs={"layer": "H-DIMS"},
        )
        dim.render()
        pad = DIM_TXT + 1.5
        if angle == 0:
            xs = [p1[0], p2[0]]
            lo = base[1] - (DIM_TXT + 1.5 if loc and loc[1] < base[1] else 0.5)
            box = (min(xs) - 1.5, lo, max(xs) + 1.5, base[1] + pad)
            self.grow([(box[0], min(p1[1], p2[1], lo)), (box[2], max(p1[1], p2[1], box[3]))])
        else:
            ys = [p1[1], p2[1]]
            hi = base[0] + (DIM_TXT + 1.5 if loc and loc[0] > base[0] else 0.5)
            box = (base[0] - pad, min(ys) - 1.5, hi, max(ys) + 1.5)
            self.grow([(min(p1[0], p2[0], box[0]), box[1]), (max(p1[0], p2[0], hi), box[3])])
        self.obs.append(sbox(*box))
        return dim

    def dim_h(self, x1, x2, y_feat, base_y_paper, text_dx=0.0):
        """水平尺寸：量 x1..x2（實尺），界線起點 y_feat（實尺），尺寸線在圖紙 y=base_y_paper。"""
        p1, p2 = self.P(x1, y_feat), self.P(x2, y_feat)
        return self._dim(p1, p2, (p1[0], base_y_paper), 0, text_dx)

    def dim_v(self, y1, y2, x_feat, base_x_paper, text_dx=0.0):
        p1, p2 = self.P(x_feat, y1), self.P(x_feat, y2)
        return self._dim(p1, p2, (base_x_paper, p1[1]), 90, text_dx)

    # -------- 註記

    def note(self, x, y, text, side="auto", marker="dot", paper=False):
        tx, ty = (x, y) if paper else self.P(x, y)
        self.callouts.append(_Callout(tx, ty, text, side, marker))

    def el(self, x, y, label, el, side="auto"):
        """高程 ▽（頂點在 (x,y)）＋「label EL=xx.xx」。

        flush 時先試著把文字直接放在 ▽ 右側（或左側）、緊貼標高線上方；若會碰到已畫的線、
        hatch 或文字，改用引線拉到註記欄。
        """
        tx, ty = self.P(x, y)
        tri = [(tx - 1.2, ty + 2.0), (tx + 1.2, ty + 2.0), (tx, ty)]
        self.poly(tri, "H-TEXT", LW_THIN, paper=True)
        self.callouts.append(_Callout(tx, ty, el_text(label, el), side, "el"))

    def flush_callouts(self, col_r: float | None = None, col_l: float | None = None) -> None:
        """把註記排成右（左）側一欄；依標示點 y 由上而下排序並保持最小間距，避免文字互疊。

        side="auto" 的註記依標示點在圖形中線左右分到左 / 右欄，縮短引線。
        """
        cx = (self.ext[0] + self.ext[2]) / 2
        pending = []
        for c in self.callouts:
            if c.marker == "el" and self._inline_el(c):
                continue
            if c.side == "auto":
                c.side = "R" if c.tx >= cx else "L"
            pending.append(c)
        self.callouts = pending
        for side, col0 in (("R", col_r), ("L", col_l)):
            items = sorted((c for c in self.callouts if c.side == side), key=lambda c: -c.ty)
            if not items:
                continue
            col = col0
            if col is None:
                col = self.ext[2] + 8.0 if side == "R" else self.ext[0] - 8.0
            ys: list[float] = []
            for c in items:
                y = c.ty
                if ys and y > ys[-1] - CALLOUT_GAP:
                    y = ys[-1] - CALLOUT_GAP
                ys.append(y)
            # 整體往上平移，讓位移量正負平衡（不超過第一個標示點上方 2 格）
            shift = sum(c.ty - y for c, y in zip(items, ys, strict=True)) / len(items)
            shift = min(shift, 2 * CALLOUT_GAP)
            ys = [y + shift for y in ys]
            for i in range(1, len(ys)):  # 平移後再確保間距
                ys[i] = min(ys[i], ys[i - 1] - CALLOUT_GAP)
            for c, y in zip(items, ys, strict=True):
                w = est_text_width(c.text, TEXT_H)
                if side == "R":
                    elbow, end = col - 2.0, col + w + 0.5
                    self.text(c.text, (col, y + 0.6), align=Align.BOTTOM_LEFT)
                else:
                    elbow, end = col + 2.0, col - w - 0.5
                    self.text(c.text, (col, y + 0.6), align=Align.BOTTOM_RIGHT)
                self.poly([(c.tx, c.ty), (elbow, y), (end, y)], "H-TEXT", LW_THIN, False, True)
                if c.marker == "dot":
                    self.solid_dot((c.tx, c.ty))
        self.callouts.clear()

    def _inline_el(self, c: _Callout) -> bool:
        w = est_text_width(c.text, TEXT_H)
        y0 = c.ty + 0.35
        for x0, al in ((c.tx + 1.8, Align.BOTTOM_LEFT), (c.tx - 1.8 - w, Align.BOTTOM_RIGHT)):
            cand = sbox(x0 - 0.4, y0, x0 + w + 0.4, y0 + TEXT_H * 1.3 + 0.3)
            if c.side == "R" and al == Align.BOTTOM_RIGHT:
                continue
            if c.side == "L" and al == Align.BOTTOM_LEFT:
                continue
            if any(cand.intersects(o) for o in self.obs):
                continue
            pos = (c.tx + 1.8, y0) if al == Align.BOTTOM_LEFT else (c.tx - 1.8, y0)
            self.text(c.text, pos, align=al)
            return True
        return False

    def title(self, item) -> None:
        """詳圖標題（剖面代號 + 圖名）＋ 雙底線 ＋ 比例文字，置於目前 bbox 下方置中。"""
        cx = (self.ext[0] + self.ext[2]) / 2
        y = self.ext[1] - 6.0 - TITLE_H
        s = f"{item.section_code}　{item.title}"
        w = est_text_width(s, TITLE_H)
        self.text(s, (cx, y), TITLE_H, Align.BOTTOM_CENTER)
        self.line((cx - w / 2, y - 1.2), (cx + w / 2, y - 1.2), "H-TEXT", LW_THICK, paper=True)
        self.line((cx - w / 2, y - 2.2), (cx + w / 2, y - 2.2), "H-TEXT", LW_THIN, paper=True)
        self.text(item.scale_text(), (cx, y - 3.5), SCALE_H, Align.TOP_CENTER)

    def bbox(self) -> tuple[float, float, float, float]:
        return tuple(self.ext)  # type: ignore[return-value]


def _bowtie(ctx: _Ctx, x, yc, d, *, check: bool) -> None:
    """閥件符號（實尺）：兩個相對三角形；逆止閥另加一條垂直擋線。"""
    w, h = d * 0.8, d * 0.7
    ctx.poly([(x - w, yc - h), (x - w, yc + h), (x + w, yc - h), (x + w, yc + h)], "H-PIPE")
    if check:
        ctx.line((x, yc - h * 1.3), (x, yc + h * 1.3), "H-PIPE", LW_MED)
    else:  # 電磁閥 / 一般閥：上方加驅動器方框
        ctx.line((x, yc), (x, yc + h * 1.6), "H-PIPE")
        ctx.rect(x - h * 0.5, yc + h * 1.6, x + h * 0.5, yc + h * 2.4, "H-PIPE", LW_THIN)


def _pipe(ctx: _Ctx, x0, x1, inv_y, d, layer="H-PIPE") -> None:
    ctx.line((x0, inv_y), (x1, inv_y), layer, LW_MED)
    ctx.line((x0, inv_y + d), (x1, inv_y + d), layer, LW_MED)
    ctx.line((x0, inv_y + d / 2), (x1, inv_y + d / 2), "H-CL", LW_THIN, ltscale=0.4)


# ---------------------------------------------------------------- U 型溝


def draw_u_ditch(msp, origin, item: UDitchSection) -> tuple[float, float, float, float]:
    ctx = _Ctx(msp, origin[0], origin[1], item.scale)
    W, H, t, tb, g, ge = (
        item.inner_width,
        item.inner_depth,
        item.wall_t,
        item.base_t,
        item.gravel_t,
        item.gravel_ext,
    )
    B = W + 2 * t
    yb, yi = g, g + tb
    yt = yi + H
    E = max(250.0, 0.4 * W)  # 回填土示意寬
    conc = sbox(0, yb, B, yt).difference(sbox(t, yi, t + W, yt + 1))
    ctx.hatch_geom(conc, "conc")
    ctx.outline_geom(conc, lw=LW_THICK)
    if g > 0:
        grav = sbox(-ge, 0, B + ge, g)
        ctx.hatch_geom(grav, "gravel")
        ctx.outline_geom(grav, lw=LW_THIN)
    earth = unary_union([sbox(-ge - E, 0, -ge, yt), sbox(-ge, g, 0, yt)])
    earth = unary_union([earth, sbox(B + ge, 0, B + ge + E, yt), sbox(B, g, B + ge, yt)])
    ctx.hatch_geom(earth, "earth")
    ctx.line((-ge - E, yt), (0, yt), "H-GRND", LW_MED)
    ctx.line((B, yt), (B + ge + E, yt), "H-GRND", LW_MED)
    ytop = yt
    if item.cover:
        ct = item.cover.thickness
        ctx.rect(0, yt, B, yt + ct, lw=LW_MED)
        if "RC" in item.cover.type_text or "混凝土" in item.cover.type_text:
            ctx.hatch_geom(sbox(0, yt, B, yt + ct), "conc")
        ytop = yt + ct
        ctx.note(B * 0.7, yt + ct / 2, f"蓋板：{item.cover.type_text} t={ct:g}")
    if item.rebar:
        c = item.rebar.cover
        path = [(c, yt - c), (c, yb + c), (B - c, yb + c), (B - c, yt - c)]
        ctx.poly(path, "H-RBAR", LW_THIN, close=False)
        inner = [(t - c, yt - c), (t - c, yi - c), (t + W + c, yi - c), (t + W + c, yt - c)]
        ctx.poly(inner, "H-RBAR", LW_THIN, close=False)
        ctx.note(B - c, yi + H * 0.55, f"鋼筋 {item.rebar.bar}{item.rebar.spacing}（示意）")
    if item.invert_el is not None:
        ctx.el(t + W * 0.75, yi, "溝底", item.invert_el)
    ctx.note(B - t / 2, yb + tb * 0.5, "RC 溝體")
    if g > 0:
        ctx.note(B + ge * 0.5, g * 0.5, f"碎石級配基礎 t={g:g}")
    ctx.note(B + ge + E * 0.5, yt * 0.6, "回填土（分層夯實）")

    # 尺寸：下方 t | W | t、全寬；左側 碎石 / 底厚 / 內深 / 蓋板
    by = ctx.P(0, 0)[1] - 6.0
    ctx.dim_h(0, t, 0, by)
    ctx.dim_h(t, t + W, 0, by)
    ctx.dim_h(t + W, B, 0, by)
    ctx.dim_h(0, B, 0, by - 7.0)
    xl = -ge - E
    bx = ctx.P(xl, 0)[0] - 5.0
    if g > 0:
        ctx.dim_v(0, g, xl, bx)
    ctx.dim_v(yb, yi, xl, bx)
    ctx.dim_v(yi, yt, xl, bx)
    if item.cover:
        ctx.dim_v(yt, ytop, xl, bx)
    ctx.flush_callouts()
    ctx.title(item)
    return ctx.bbox()


# ---------------------------------------------------------------- 筏基滯洪坑


def draw_raft_detention(
    msp, origin, item: RaftDetentionSection
) -> tuple[float, float, float, float]:
    ctx = _Ctx(msp, origin[0], origin[1], item.scale)
    n, cw, bw = item.n_cells, item.cell_width, item.beam_width
    g, bs, pd, ts = item.gravel_t, item.base_slab_t, item.pit_depth, item.top_slab_t
    yb0, yb1 = g, g + bs
    yt0 = yb1 + pd
    yt1 = yt0 + ts
    L = n * cw + (n + 1) * bw

    def Y(el: float) -> float:
        return yb1 + (el - item.bottom_el) * 1000.0

    def cell_x0(i: int) -> float:
        return bw + i * (cw + bw)

    cells = [sbox(cell_x0(i), yb1, cell_x0(i) + cw, yt0) for i in range(n)]
    inflow = item.inflow_pipe
    yin = Y(inflow.invert_el)
    o = item.orifice
    pump = item.pump
    dout = item.outflow_pipe_dia
    dov = item.overflow_dia
    m0 = cell_x0(item.manhole_cell) + 150.0
    m1 = min(m0 + item.manhole_size, cell_x0(item.manhole_cell) + cw)
    openings = [sbox(-1, yin, bw + 1, yin + inflow.diameter), sbox(m0, yt0 - 1, m1, yt1 + 1)]
    yoc = Y(o.center_el) if o is not None else None
    if o is not None:
        openings.append(sbox(L - bw - 1, yoc - dout / 2, L + 1, yoc + dout / 2))
    yov = Y(item.overflow_el) if item.overflow_el is not None else None
    if yov is not None:
        openings.append(sbox(L - bw - 1, yov, L + 1, yov + dov))
    sump = None
    if pump is not None:
        cx0 = cell_x0(pump.cell)
        sx1 = cx0 + cw - 150.0
        sx0 = sx1 - pump.sump_width
        sump = (sx0, yb1 - pump.sump_depth, sx1, yb1)
        openings.append(sbox(sx0, yb1 - pump.sump_depth, sx1, yb1 + 1))
        px = (sx0 + sx1) / 2
        dd = pump.discharge_dia
        openings.append(sbox(px - dd / 2, yt0 - 1, px + dd / 2, yt1 + 1))
    vx = None
    if item.vent_cell is not None:
        vx = cell_x0(item.vent_cell) + cw * 0.7
        if sump is not None and item.vent_cell == pump.cell:
            vx = cell_x0(item.vent_cell) + cw * 0.3
        vd = item.vent_diameter
        openings.append(sbox(vx - vd / 2, yt0 - 1, vx + vd / 2, yt1 + 1))
    conc = sbox(0, yb0, L, yt1).difference(unary_union(cells + openings))
    ctx.hatch_geom(conc, "conc")
    ctx.outline_geom(conc, lw=LW_THICK)
    if g > 0:
        grav = sbox(-150, 0, L + 150, g)
        ctx.hatch_geom(grav, "gravel")
        ctx.outline_geom(grav, lw=LW_THIN)
    earth = sbox(-150, -350, L + 150, 0)
    ctx.hatch_geom(earth, "earth")

    # 水位、有效水深
    yh = Y(item.storage_level_el)
    for i in range(n):
        x0 = cell_x0(i)
        ctx.line((x0, yh), (x0 + cw, yh), "H-WATR", LW_THIN, ltscale=0.6)
    yfl = None
    if item.overflow_el is not None and item.overflow_flood_el is not None:
        yfl = Y(item.overflow_flood_el)  # 溢流時最高水位：只畫在溢流口所在（右側）坑格
        x0 = cell_x0(n - 1)
        ctx.line((x0 + cw * 0.45, yfl), (x0 + cw, yfl), "H-WATR", LW_THIN, ltscale=0.3)
    xe = cell_x0(n - 1) + cw * 0.3 if pump is None else cell_x0(0) + cw * 0.55
    ctx.dim_v(yb1, yb1 + item.effective_depth, xe, ctx.P(xe, 0)[0])
    ctx.note(xe, yb1 + item.effective_depth * 0.3, f"有效水深 h={item.effective_depth:g}")

    # 進流管（左側地梁）
    xin_out = -700.0
    _pipe(ctx, xin_out, bw + 150.0, yin, inflow.diameter)
    ctx.line((bw + 150.0, yin), (bw + 150.0, yin + inflow.diameter), "H-PIPE", LW_MED)
    xout_end = L + 1500.0
    plate_x = L - bw
    pipes = {"inflow": (xin_out + 250.0, yin + inflow.diameter / 2, inflow.diameter)}
    if o is not None:  # 放流：右側地梁內側孔口板 + 放流管
        ctx.line((plate_x, yoc - dout), (plate_x, yoc - o.size_h() / 2), "H-PIPE", LW_THICK)
        ctx.line((plate_x, yoc + o.size_h() / 2), (plate_x, yoc + dout), "H-PIPE", LW_THICK)
        _pipe(ctx, plate_x, xout_end, yoc - dout / 2, dout)
        pipes["outflow"] = (L + 450.0, yoc, dout)
    if yov is not None:
        _pipe(ctx, L - bw - 100.0, xout_end, yov, dov)
        pipes["overflow"] = (L + 450.0, yov + dov / 2, dov)
    if pump is not None:  # 集水坑 + 抽水機 + 出水管（立管穿頂版 → 水平往右）
        sx0, sy0, sx1, _ = sump
        px = (sx0 + sx1) / 2
        dd = pump.discharge_dia
        pr = min(pump.sump_width * 0.22, 220.0)
        pc = (px, sy0 + pr + 40.0)
        ctx.circle(pc, pr, "H-PIPE", LW_MED)
        ctx.text("P", ctx.P(*pc), TEXT_H, Align.MIDDLE_CENTER)
        y_run = yt1 + 400.0
        for xx in (px - dd / 2, px + dd / 2):
            ctx.line((xx, pc[1] + pr), (xx, y_run + (dd if xx > px else 0)), "H-PIPE", LW_MED)
        ctx.line((px - dd / 2, y_run + dd), (xout_end, y_run + dd), "H-PIPE", LW_MED)
        ctx.line((px + dd / 2, y_run), (xout_end, y_run), "H-PIPE", LW_MED)
        ctx.line((px, y_run + dd / 2), (xout_end, y_run + dd / 2), "H-CL", LW_THIN, ltscale=0.4)
        pipes["outflow"] = (L + 450.0, y_run + dd / 2, dd)
        q = f" Q={pump.q_cms:.4f} m³/s" if pump.q_cms is not None else ""
        ctx.note(px + pr, pc[1], f"抽水機{q}")
        ctx.note(
            sx0, sy0 + pump.sump_depth * 0.4, f"集水坑 {pump.sump_width:g}×深{pump.sump_depth:g}"
        )
        ctx.note(xout_end, y_run + dd / 2, f"抽水出水管 D{dd:g}（接{pump.discharge_to}）")
        ys, yp = Y(pump.start_el), Y(pump.stop_el)
        cx0 = cell_x0(pump.cell)
        for yy in (ys, yp):
            ctx.line((cx0, yy), (cx0 + cw, yy), "H-WATR", LW_THIN, ltscale=0.6)
        ctx.el(cx0 + cw * 0.25, ys, "起抽水位", pump.start_el)
        ctx.el(cx0 + cw * 0.12, yp, "停機水位", pump.stop_el)
    valve_x = {"inflow": 0, "outflow": 0, "overflow": 0}
    if item.check_valve_at and item.check_valve_at in pipes:
        x, yc, d = pipes[item.check_valve_at]
        _bowtie(ctx, x, yc, d, check=True)
        side = "L" if item.check_valve_at == "inflow" else "R"
        ctx.note(x, yc - d * 0.7, "逆止閥", side=side)
        valve_x[item.check_valve_at] += 1
    for v in item.valves:
        if v.at not in pipes:
            continue
        x, yc, d = pipes[v.at]
        if v.at == "inflow" and v.label.startswith("浮球"):
            # 浮球閥：進流管端下方浮球 + 連桿
            fx = bw + 150.0
            r = inflow.diameter * 0.45
            ctx.line((fx, yin), (fx + r, yin - 2.2 * r), "H-PIPE")
            ctx.circle((fx + r, yin - 3.2 * r), r, "H-PIPE")
            ctx.note(fx + r, yin - 3.2 * r, v.label)
            continue
        x += valve_x[v.at] * 500.0
        valve_x[v.at] += 1
        _bowtie(ctx, x, yc, d, check=False)
        ctx.note(x, yc + d * 0.7 * 2.4, v.label, side="L" if v.at == "inflow" else "R")

    # 人孔 / 爬梯 / 通氣管
    ctx.rect(m0 - 60, yt1, m1 + 60, yt1 + 50, "H-STRC", LW_MED)
    ctx.note((m0 + m1) / 2, yt1 + 50, f"人孔 {item.manhole_size:g}×{item.manhole_size:g}（加蓋）")
    if item.ladder:
        lx0, lx1 = m0 + 100.0, min(m0 + 500.0, m1 - 50.0)
        ctx.line((lx0, yb1 + 150), (lx0, yt1), "H-PIPE", LW_MED)
        ctx.line((lx1, yb1 + 150), (lx1, yt1), "H-PIPE", LW_MED)
        y = yb1 + 300.0
        while y < yt1 - 100:
            ctx.line((lx0, y), (lx1, y), "H-PIPE", LW_THIN)
            y += 300.0
        ctx.note(lx1, yb1 + pd * 0.55, "不銹鋼爬梯")
    if vx is not None:
        vd = item.vent_diameter
        ctx.rect(vx - vd / 2, yt0 - 100, vx + vd / 2, yt1 + 600, "H-PIPE", LW_MED)
        ctx.note(vx + vd / 2, yt1 + 400, f"通氣管 D{vd:g}")

    # 高程
    xr = cell_x0(n - 1) if pump is None else cell_x0(0)
    ctx.el(xr + cw * 0.8, yb1, "坑底", item.bottom_el)
    ctx.el(xr + cw * 0.9, yt0, "坑頂（頂版底）", item.top_el())
    ctx.el(xr + cw * 0.6, yh, "設計滿水位", item.storage_level_el)
    if yfl is not None:
        ctx.el(cell_x0(n - 1) + cw * 0.75, yfl, "溢流時最高水位", item.overflow_flood_el)
    if yov is not None:
        ctx.el(xout_end - 200.0, yov, f"溢流口 D{dov:g}", item.overflow_el)
    else:
        ctx.note(cell_x0(0) + cw * 0.2, yt0 - pd * 0.15, item.overflow_note)
    if o is not None:
        to = "（接放流井）" if item.outflow_to_outlet_well else ""
        ctx.note(xout_end, yoc, f"放流管 D{dout:g}{to}")
        ctx.el(plate_x, yoc, f"放流孔口 {o.size_text()} 中心", o.center_el)  # ▽ 尖端 = 孔口中心
    ctx.el(xin_out + 80.0, yin, f"進流管 D{inflow.diameter:g} 管底", inflow.invert_el, side="L")

    # 尺寸
    by = ctx.P(0, -350)[1] - 6.0
    xs = [0.0]
    for _ in range(n):
        xs += [xs[-1] + bw, xs[-1] + bw + cw]
    xs.append(L)
    for a, b in zip(xs, xs[1:], strict=False):
        ctx.dim_h(a, b, -350, by)
    ctx.dim_h(0, L, -350, by - 7.0)
    bx = ctx.P(xin_out, 0)[0] - 6.0
    if g > 0:
        ctx.dim_v(0, g, xin_out, bx)
    ctx.dim_v(yb0, yb1, xin_out, bx)
    ctx.dim_v(yb1, yt0, xin_out, bx)
    ctx.dim_v(yt0, yt1, xin_out, bx)
    ctx.dim_h(m0, m1, yt1 + 50, ctx.P(0, yt1 + 50)[1] + 5.0)
    if sump is not None:
        sx0, sy0, sx1, _ = sump
        ctx.dim_h(sx0, sx1, -350, by - 14.0)
        ctx.dim_v(sy0, yb1, sx1, ctx.P(sx1, 0)[0] + 4.0)
    ctx.flush_callouts(col_l=ctx.ext[0] - 6.0)
    ctx.title(item)
    return ctx.bbox()


# ---------------------------------------------------------------- 放流井


def draw_outlet_well(msp, origin, item: OutletWellDetail) -> tuple[float, float, float, float]:
    ctx = _Ctx(msp, origin[0], origin[1], item.scale)
    t, pt, a, b = item.wall_t, item.partition_t, item.chamber_a_length, item.chamber_b_length
    g, bt = item.gravel_t, item.base_t
    Lw = 2 * t + a + pt + b
    yb0, yb1 = g, g + bt
    single = item.single_chamber

    def Y(el: float) -> float:
        return yb1 + (el - item.bottom_el) * 1000.0

    ytop = Y(item.top_el)
    xa0, xp0 = t, t + a
    xp1 = xp0 + pt
    xb1 = xp1 + b
    o, wr, op, dt = item.orifice, item.weir, item.outlet_pipe, item.ditch
    yoc = Y(o.center_el) if o is not None else None
    ycr = Y(wr.crest_el) if wr is not None else ytop
    yop = Y(op.invert_el)
    yope = Y(item.outlet_exit_el())
    xd0 = Lw + item.outlet_pipe_length  # 側溝內壁面（放流管口切齊此面）
    xd_w0 = xd0 - dt.wall_t
    xd1 = xd0 + dt.inner_width
    ydb, ydt = Y(dt.bottom_el), Y(dt.top_el)

    # ---- 剖面
    holes = [sbox(xb1 - 1, yop, Lw + 1, yop + op.diameter)]
    if o is not None:
        holes.append(sbox(xp0 - 1, yoc - o.size_h() / 2, xp1 + 1, yoc + o.size_h() / 2))
    left_pipes = [p_ for p_ in (item.inflow_pipe, item.pump_inflow) if p_ is not None]
    for lp in left_pipes:
        yl = Y(lp.invert_el)
        holes.append(sbox(-1, yl, t + 1, yl + lp.diameter))
    parts = [sbox(0, yb0, Lw, yb1), sbox(0, yb1, t, ytop), sbox(xb1, yb1, Lw, ytop)]
    if not single:
        parts.append(sbox(xp0, yb1, xp1, ycr))
    well = unary_union(parts).difference(unary_union(holes))
    ditch = unary_union(
        [
            sbox(xd_w0, ydb - dt.wall_t, xd1 + dt.wall_t, ydb),
            sbox(xd_w0, ydb, xd0, ydt),
            sbox(xd1, ydb, xd1 + dt.wall_t, ydt),
        ]
    ).difference(sbox(xd_w0 - 1, yope, xd0 + 1, yope + op.diameter))
    for geom in (well, ditch):
        ctx.hatch_geom(geom, "conc")
        ctx.outline_geom(geom, lw=LW_THICK)
    if not single and wr is not None:
        # 隔牆堰口以外部分（剖面後方可見輪廓）
        ctx.rect(xp0, ycr, xp1, ytop, "H-HIDN", LW_THIN)
    if g > 0:
        grav = sbox(-100, 0, Lw + 100, g)
        ctx.hatch_geom(grav, "gravel")
        ctx.outline_geom(grav, lw=LW_THIN)
    y_low = min(0.0, ydb - dt.wall_t)
    pipe_band = unary_union(
        [
            Polygon(
                [(Lw - 1, yop), (xd0, yope), (xd0, yope + op.diameter), (Lw - 1, yop + op.diameter)]
            )
        ]
    )
    cuts = [pipe_band]
    for lp in left_pipes:
        yl = Y(lp.invert_el)
        cuts.append(sbox(-500, yl, 0, yl + lp.diameter))
    # 井外地面：井頂 → 側溝溝緣（直線漸變；側溝高於井頂時取平）
    yg_end = min(ytop, ydt) if item.outlet_pipe_end_el is not None else ytop
    right_earth = Polygon([(Lw, y_low), (xd_w0, y_low), (xd_w0, yg_end), (Lw, ytop)])
    earth = unary_union(
        [sbox(-400, y_low, -100, ytop), sbox(-100, g, 0, ytop), right_earth]
    ).difference(unary_union(cuts))
    ctx.hatch_geom(earth, "earth")
    ctx.line((-400, ytop), (0, ytop), "H-GRND", LW_MED)
    ctx.line((Lw, ytop), (xd_w0, yg_end), "H-GRND", LW_MED)
    # 蓋板（A 室）與清掃孔（B 室）
    ctx.rect(xa0, ytop - 50, xp0, ytop, "H-STRC", LW_MED)
    cs = min(item.cleanout_size, b)
    cx0 = xp1 + (b - cs) / 2
    ctx.rect(cx0, ytop - 50, cx0 + cs, ytop, "H-STRC", LW_MED)
    ctx.note((xa0 + xp0) / 2, ytop - 25, item.cover_text)
    ctx.note(cx0 + cs * 0.6, ytop - 25, f"清掃孔 {cs:g}×{cs:g}（{item.cover_text}）")
    if o is not None:  # 孔口板（上游面）
        top_plate = ycr if wr is not None else ytop
        ctx.line((xp0 - 10, yb1), (xp0 - 10, yoc - o.size_h() / 2), "H-PIPE", LW_THICK)
        ctx.line((xp0 - 10, yoc + o.size_h() / 2), (xp0 - 10, top_plate), "H-PIPE", LW_THICK)
    # 管線
    if item.inflow_pipe:
        ip = item.inflow_pipe
        yin = Y(ip.invert_el)
        _pipe(ctx, -500, t + 80, yin, ip.diameter)
        ctx.el(-420, yin, f"進流管 D{ip.diameter:g} 管底", ip.invert_el, side="L")
    if item.pump_inflow:
        pp = item.pump_inflow
        yp_ = Y(pp.invert_el)
        _pipe(ctx, -500, t + 80, yp_, pp.diameter)
        ctx.el(-300, yp_, f"抽水出水管 D{pp.diameter:g} 管底", pp.invert_el, side="L")
    if item.overflow_inflow:  # 自後側井壁入井（剖面看到管口，虛線圓）
        ov = item.overflow_inflow
        r = ov.diameter / 2
        c = (xa0 + a * 0.5, Y(ov.invert_el) + r)
        ctx.circle(c, r, "H-HIDN", LW_THIN)
        ctx.el(
            c[0] + r * 0.2,
            Y(ov.invert_el),
            f"溢流管 D{ov.diameter:g}（自{item.overflow_inflow_from}）管底",
            ov.invert_el,
        )
    for k in (0.0, op.diameter):
        ctx.line((xb1, yop + k), (Lw, yop + k), "H-PIPE", LW_MED)
        ctx.line((Lw, yop + k), (xd0, yope + k), "H-PIPE", LW_MED)
    ctx.line(
        (xb1, yop + op.diameter / 2), (Lw, yop + op.diameter / 2), "H-CL", LW_THIN, ltscale=0.4
    )
    ctx.line(
        (Lw, yop + op.diameter / 2), (xd0, yope + op.diameter / 2), "H-CL", LW_THIN, ltscale=0.4
    )
    ctx.line((xd0, yope), (xd0, yope + op.diameter), "H-PIPE", LW_THICK)
    ctx.note(xd0, yope + op.diameter * 0.5, "放流管口切齊溝緣，不得突出")
    # 高程
    ctx.el(xa0 + a * 0.3, ytop, "井頂", item.top_el)
    ctx.el(xa0 + a * 0.7, yb1, "井底", item.bottom_el)
    if wr is not None:
        ctx.el(xp1, ycr, f"溢流堰頂（堰寬 {wr.width:g}）", wr.crest_el)
    if o is not None:
        ctx.el(xp1, yoc, f"孔口 {o.size_text()} 中心", o.center_el)  # ▽ 尖端 = 孔口中心
    ctx.el(xb1 - 80, yop, f"放流管 D{op.diameter:g} 管底", op.invert_el)
    if item.outlet_pipe_end_el is not None:
        ctx.el(xd0 - 150, yope, "放流管出口管底", item.outlet_pipe_end_el)
    ctx.el(xd0 + dt.inner_width * 0.5, ydb, "側溝底", dt.bottom_el)
    ctx.el(xd1 + dt.wall_t * 0.5, ydt, "側溝溝緣", dt.top_el)
    if dt.design_wl_el is not None:
        yw = Y(dt.design_wl_el)
        ctx.line((xd0, yw), (xd1, yw), "H-WATR", LW_THIN, ltscale=0.4)
        ctx.el(xd0 + dt.inner_width * 0.7, yw, "受納溝設計水位", dt.design_wl_el)
    # 尺寸
    by = ctx.P(0, y_low)[1] - 6.0
    seq = (
        ((0, xa0), (xa0, xb1), (xb1, Lw))
        if single
        else ((0, xa0), (xa0, xp0), (xp0, xp1), (xp1, xb1), (xb1, Lw))
    )
    for x0, x1 in seq:
        ctx.dim_h(x0, x1, y_low, by)
    ctx.dim_h(0, Lw, y_low, by - 7.0)
    ctx.dim_h(Lw, xd0, y_low, by)
    ctx.dim_h(xd0, xd1, ydb - dt.wall_t, by)
    bx = ctx.P(-400, 0)[0] - 5.0
    if g > 0:
        ctx.dim_v(0, g, -400, bx)
    ctx.dim_v(yb0, yb1, -400, bx)
    ctx.text(f"{item.section_code} 剖面", ctx.P(0, ytop + 120), TEXT_H + 0.5, Align.BOTTOM_LEFT)
    ctx.flush_callouts(col_l=ctx.ext[0] - 4.0)

    # ---- 平面（剖面上方，x 對齊）
    sec_top = ctx.ext[3]
    Wp = item.inner_width + 2 * t
    dy = sec_top + 14.0 - ctx.P(0, -300)[1]
    pl = ctx.sub(0.0, dy)
    pl.rect(0, 0, Lw, Wp, lw=LW_THICK)
    yc = Wp / 2
    if single:
        pl.rect(xa0, t, xb1, t + item.inner_width, lw=LW_MED)
    else:
        pl.rect(xa0, t, xp0, t + item.inner_width, lw=LW_MED)
        pl.rect(xp1, t, xb1, t + item.inner_width, lw=LW_MED)
        if wr is not None:
            pl.line((xp0, yc - wr.width / 2), (xp1, yc - wr.width / 2), "H-STRC", LW_THIN)
            pl.line((xp0, yc + wr.width / 2), (xp1, yc + wr.width / 2), "H-STRC", LW_THIN)
    pl.rect(
        cx0,
        t + (item.inner_width - cs) / 2,
        cx0 + cs,
        t + (item.inner_width + cs) / 2,
        "H-HIDN",
        LW_THIN,
    )
    # 側溝（平面沿 y 延伸）
    for x0, x1 in ((xd_w0, xd0), (xd1, xd1 + dt.wall_t)):
        pl.rect(x0, -300, x1, Wp + 300, lw=LW_MED)
    pl.line((Lw, yc - op.diameter / 2), (xd0, yc - op.diameter / 2), "H-PIPE", LW_MED)
    pl.line((Lw, yc + op.diameter / 2), (xd0, yc + op.diameter / 2), "H-PIPE", LW_MED)
    for lp in left_pipes:
        pl.line((-500, yc - lp.diameter / 2), (0, yc - lp.diameter / 2), "H-PIPE", LW_MED)
        pl.line((-500, yc + lp.diameter / 2), (0, yc + lp.diameter / 2), "H-PIPE", LW_MED)
    if item.overflow_inflow:
        ov = item.overflow_inflow
        xo = xa0 + a * 0.5
        pl.line((xo - ov.diameter / 2, Wp), (xo - ov.diameter / 2, Wp + 500), "H-PIPE", LW_MED)
        pl.line((xo + ov.diameter / 2, Wp), (xo + ov.diameter / 2, Wp + 500), "H-PIPE", LW_MED)
        # 引線往右拉（左上角是「平面」標題，往左拉會壓字）
        pl.note(
            xo + ov.diameter / 2, Wp + 400, f"溢流管（自{item.overflow_inflow_from}）", side="R"
        )
    # 剖切線 A — A
    letter = item.section_code.split("-")[0]
    half = max(wr.width if wr is not None else 0.0, op.diameter) / 2
    cut_y = yc + half + 80
    pl.line((-600, cut_y), (xd1 + dt.wall_t + 200, cut_y), "H-CL", LW_THIN, ltscale=0.5)
    for xx, al in ((-600, Align.BOTTOM_RIGHT), (xd1 + dt.wall_t + 200, Align.BOTTOM_LEFT)):
        p = pl.P(xx, cut_y)
        pl.line(p, (p[0], p[1] - 4.0), "H-TEXT", LW_THICK, paper=True)
        pl.text(
            letter,
            (p[0] + (-1.0 if al == Align.BOTTOM_RIGHT else 1.0), p[1] + 0.5),
            TEXT_H + 0.5,
            al,
        )
    pl.note(xd0 + dt.inner_width / 2, Wp + 200, "道路側溝")
    pl.note(cx0 + cs * 0.8, t + (item.inner_width + cs) / 2, "清掃孔")
    if wr is not None:
        pl.note(xp1, yc + wr.width / 2, f"溢流堰 寬 {wr.width:g}")
    pl.dim_v(t, t + item.inner_width, 0, pl.P(-600, 0)[0] - 6.0)
    if single:
        pl.dim_h(xa0, xb1, Wp, pl.P(0, Wp)[1] + 5.0)
    else:
        pl.dim_h(xa0, xp0, Wp, pl.P(0, Wp)[1] + 5.0)
        pl.dim_h(xp1, xb1, Wp, pl.P(0, Wp)[1] + 5.0)
    if wr is not None:
        pl.dim_v(yc - wr.width / 2, yc + wr.width / 2, xp0, pl.P(xa0 + 60, 0)[0])
    pl.text("平面", pl.P(0, Wp + 300), TEXT_H + 0.5, Align.BOTTOM_LEFT)
    pl.flush_callouts()
    ctx.title(item)
    return ctx.bbox()


# ---------------------------------------------------------------- 孔口板


def draw_orifice_plate(msp, origin, item: OrificePlateDetail) -> tuple[float, float, float, float]:
    ctx = _Ctx(msp, origin[0], origin[1], item.scale)
    W, H, th = item.plate_width, item.plate_height, item.plate_t
    o = item.orifice
    cx, cy = W / 2, item.orifice_center_height
    ctx.rect(0, 0, W, H, lw=LW_THICK)
    if o.shape == "circle":
        ctx.circle((cx, cy), o.diameter / 2, "H-STRC", LW_MED)
    else:
        ctx.rect(cx - o.width / 2, cy - o.height / 2, cx + o.width / 2, cy + o.height / 2)
    e = item.bolt_edge
    for bx_, by_ in ((e, e), (W - e, e), (e, H - e), (W - e, H - e)):
        ctx.circle((bx_, by_), item.bolt_hole_dia / 2, "H-STRC", LW_THIN)
    ce = 3.0 * item.scale  # 中心線外伸圖紙 3 mm
    r_h = o.size_h() / 2
    ctx.line((cx, cy - r_h - ce), (cx, cy + r_h + ce), "H-CL", LW_THIN, ltscale=0.3)
    ctx.line((-ce, cy), (W + ce, cy), "H-CL", LW_THIN, ltscale=0.3)
    # 側視（剖面）
    sx = W + 25.0 * item.scale  # 側視與正視間距：圖紙 25 mm
    sec = sbox(sx, 0, sx + th, H).difference(
        sbox(sx - 1, cy - o.size_h() / 2, sx + th + 1, cy + o.size_h() / 2)
    )
    ctx.hatch_geom(sec, "steel")
    ctx.outline_geom(sec, lw=LW_MED)
    ctx.line((sx - ce, cy), (sx + th + ce, cy), "H-CL", LW_THIN, ltscale=0.3)
    ctx.note(sx + th, H * 0.8, f"板厚 t={th:g}")
    ctx.note(sx + th, H * 0.6, item.material_text)
    ctx.note(sx + th, H * 0.35, item.fixing_text)
    if item.bolt_text:
        ctx.note(e, H - e, item.bolt_text, side="L")
    ctx.el(sx + th, cy, "孔心", o.center_el)
    ctx.text("正視", ctx.P(0, H + 60), TEXT_H, Align.BOTTOM_LEFT)
    ctx.text("側視", ctx.P(sx, H + 60), TEXT_H, Align.BOTTOM_LEFT)
    # 尺寸
    by = ctx.P(0, 0)[1] - 6.0
    ctx.dim_h(0, W, 0, by)
    hole_by = ctx.P(0, cy - r_h)[1] - 5.0  # 孔徑尺寸放在孔下方（板內），文字偏離中心線
    dx = est_text_width("000", DIM_TXT) * 0.5 + 1.5
    ctx.dim_h(cx - o.size_w() / 2, cx + o.size_w() / 2, cy, hole_by, text_dx=dx)
    if o.shape == "rect":
        ctx.dim_v(cy - r_h, cy + r_h, cx + o.width / 2, ctx.P(cx + o.width / 2, 0)[0] + 5.0)
    ctx.dim_h(0, e, H, ctx.P(0, H)[1] + 5.0)
    bx = ctx.P(0, 0)[0] - 6.0
    ctx.dim_v(0, H, 0, bx - 7.0)
    ctx.dim_v(0, cy, 0, bx)
    ctx.flush_callouts()
    ctx.title(item)
    return ctx.bbox()


# ---------------------------------------------------------------- 出水口保護工


def draw_outfall_protection(
    msp, origin, item: OutfallProtectionDetail
) -> tuple[float, float, float, float]:
    ctx = _Ctx(msp, origin[0], origin[1], item.scale)
    hwt, hh, al, at = item.headwall_t, item.headwall_height, item.apron_length, item.apron_t
    g, cd, ctt = item.gravel_t, item.cutoff_depth, item.cutoff_t
    ya0 = max(cd, g) + 300.0  # 護坦底；下方留土壤示意
    ya1 = ya0 + at

    def Y(el: float) -> float:
        return ya1 + (el - item.apron_el) * 1000.0

    x_end = hwt + al
    pipe = item.pipe
    yp = Y(pipe.invert_el)
    parts = [sbox(0, ya0 - g, hwt, ya1 + hh), sbox(hwt, ya0, x_end, ya1)]
    if cd > 0:
        parts.append(sbox(x_end - ctt, ya0 - cd, x_end, ya0))
    if item.sill_height > 0:
        parts.append(sbox(x_end - item.sill_t, ya1 - 1, x_end, ya1 + item.sill_height))
    hole = sbox(-1, yp, hwt + 1, yp + pipe.diameter)
    conc = unary_union(parts).difference(hole)
    ctx.hatch_geom(conc, "conc")
    ctx.outline_geom(conc, lw=LW_THICK)
    if g > 0:
        x_g1 = x_end - ctt if cd > 0 else x_end
        grav = sbox(hwt, ya0 - g, x_g1, ya0)
        ctx.hatch_geom(grav, "gravel")
        ctx.outline_geom(grav, lw=LW_THIN)
    rl, rt = item.riprap_length, item.riprap_t
    x_r1 = x_end + rl
    if rl > 0:
        rip = sbox(x_end, ya1 - rt, x_r1, ya1)
        ctx.hatch_geom(rip, "riprap")
        ctx.outline_geom(rip, lw=LW_MED)
        ctx.note(x_end + rl * 0.6, ya1 - rt * 0.5, item.riprap_text)
    y_soil = min(ya0 - g, ya0 - cd, ya1 - rt) - 300.0
    earth = unary_union(
        [
            sbox(-800, y_soil, x_r1 + 300, ya1 - rt if rl > 0 else ya0),
            sbox(-800, y_soil, 0, ya1 + hh),
        ]
    ).difference(unary_union([conc, hole, sbox(-801, yp, 0, yp + pipe.diameter)]))
    if g > 0:
        earth = earth.difference(sbox(hwt, ya0 - g, x_end, ya0))
    ctx.hatch_geom(earth, "earth")
    ctx.line((-800, ya1 + hh), (0, ya1 + hh), "H-GRND", LW_MED)
    _pipe(ctx, -800, hwt, yp, pipe.diameter)
    ctx.line((hwt, yp), (hwt, yp + pipe.diameter), "H-PIPE", LW_THICK)
    # 流向箭頭
    fy = ya1 + max(item.sill_height, 0) + 250.0
    p0, p1 = ctx.P(hwt + al * 0.25, fy), ctx.P(hwt + al * 0.6, fy)
    ctx.line(p0, p1, "H-TEXT", LW_MED, paper=True)
    arrow = [p1, (p1[0] - 2.5, p1[1] + 0.9), (p1[0] - 2.5, p1[1] - 0.9)]
    ctx.poly(arrow, "H-TEXT", LW_THIN, paper=True)
    ctx.text("流向", ((p0[0] + p1[0]) / 2, p0[1] + 1.0), TEXT_H, Align.BOTTOM_CENTER)
    ctx.note(hwt / 2, ya1 + hh * 0.75, "RC 端牆")
    ctx.note(hwt + al * 0.75, ya0 + at * 0.5, "RC 護坦（消能）")
    if item.sill_height > 0:
        ctx.note(x_end - item.sill_t / 2, ya1 + item.sill_height, f"消能檻 高 {item.sill_height:g}")
    if cd > 0:
        ctx.note(x_end - ctt / 2, ya0 - cd * 0.6, "截水牆")
    if g > 0:
        ctx.note(hwt + al * 0.4, ya0 - g / 2, f"碎石級配 t={g:g}")
    ctx.el(hwt + al * 0.1, ya1, "護坦頂", item.apron_el)
    ctx.el(-500, yp, f"出水管 D{pipe.diameter:g} 管底", pipe.invert_el, side="L")
    # 尺寸
    by = ctx.P(0, y_soil)[1] - 6.0
    ctx.dim_h(0, hwt, y_soil, by)
    ctx.dim_h(hwt, x_end, y_soil, by)
    if rl > 0:
        ctx.dim_h(x_end, x_r1, y_soil, by)
    bx = ctx.P(-800, 0)[0] - 5.0
    ctx.dim_v(ya0, ya1, -800, bx)
    ctx.dim_v(ya1, ya1 + hh, -800, bx)
    rx = ctx.P(x_r1 + 300, 0)[0] + 6.0
    if rl > 0:
        ctx.dim_v(ya1 - rt, ya1, x_r1, rx)
    if cd > 0:
        ctx.dim_v(ya0 - cd, ya0, x_end, rx + 8.0)
    ctx.flush_callouts(col_l=ctx.ext[0] - 4.0)
    ctx.title(item)
    return ctx.bbox()


# ---------------------------------------------------------------- 出流口接既有側溝


def draw_ditch_joint(msp, origin, item: DitchJointDetail) -> tuple[float, float, float, float]:
    """側溝橫斷面（溝寬方向）＋放流管自左側穿溝壁，管口切齊內壁；設計水位 ▽。"""
    ctx = _Ctx(msp, origin[0], origin[1], item.scale)
    dt, pipe = item.ditch, item.pipe
    t, bt, g = dt.wall_t, item.base_t, item.gravel_t
    W = dt.inner_width
    yb1 = g + bt  # 溝底（內）

    def Y(el: float) -> float:
        return yb1 + (el - dt.bottom_el) * 1000.0

    ydt = Y(dt.top_el)
    yp = Y(pipe.invert_el)
    xi0 = 0.0  # 左側溝壁內面
    xw0 = -t
    xi1 = W
    ground = Y(item.ground_el) if item.ground_el is not None else ydt
    x_left = xw0 - 1500.0
    ditch = unary_union(
        [sbox(xw0, g, xi1 + t, yb1), sbox(xw0, yb1, xi0, ydt), sbox(xi1, yb1, xi1 + t, ydt)]
    ).difference(sbox(xw0 - 1, yp, xi0 + 1, yp + pipe.diameter))
    ctx.hatch_geom(ditch, "conc")
    ctx.outline_geom(ditch, lw=LW_THICK)
    if g > 0:
        grav = sbox(xw0 - 100, 0, xi1 + t + 100, g)
        ctx.hatch_geom(grav, "gravel")
        ctx.outline_geom(grav, lw=LW_THIN)
    y_soil = min(0.0, yp - 300.0)
    earth = unary_union(
        [sbox(x_left, y_soil, xw0, max(ground, ydt)), sbox(xi1 + t, y_soil, xi1 + t + 400, ydt)]
    ).difference(sbox(x_left - 1, yp, xw0 + 1, yp + pipe.diameter))
    ctx.hatch_geom(earth, "earth")
    ctx.line((x_left, max(ground, ydt)), (xw0, max(ground, ydt)), "H-GRND", LW_MED)
    ctx.line((xi1 + t, ydt), (xi1 + t + 400, ydt), "H-GRND", LW_MED)
    _pipe(ctx, x_left, xi0, yp, pipe.diameter)
    ctx.line((xi0, yp), (xi0, yp + pipe.diameter), "H-PIPE", LW_THICK)
    if dt.design_wl_el is not None:
        yw = Y(dt.design_wl_el)
        ctx.line((xi0, yw), (xi1, yw), "H-WATR", LW_THIN, ltscale=0.4)
        ctx.el(xi0 + W * 0.65, yw, "受納溝設計水位", dt.design_wl_el)
    ctx.note(xi0, yp + pipe.diameter * 0.5, "放流管口切齊溝緣（側溝內壁），不得突出")
    ctx.note(xw0 - 300.0, yp + pipe.diameter, item.seal_text, side="L")
    ctx.note(xi1 + t / 2, yb1 - bt / 2, "既有側溝")
    ctx.el(x_left + 300.0, yp, f"出流口 D{pipe.diameter:g} 管底", pipe.invert_el, side="L")
    ctx.el(xi0 + W * 0.3, yb1, "側溝底", dt.bottom_el)
    ctx.el(xi1 + t * 0.5, ydt, "側溝溝緣", dt.top_el)
    if item.ground_el is not None:
        ctx.el(x_left + 700.0, ground, "地面", item.ground_el, side="L")
    # 尺寸
    by = ctx.P(0, y_soil)[1] - 6.0
    ctx.dim_h(xw0, xi0, y_soil, by)
    ctx.dim_h(xi0, xi1, y_soil, by)
    ctx.dim_h(xi1, xi1 + t, y_soil, by)
    rx = ctx.P(xi1 + t + 400, 0)[0] + 6.0
    ctx.dim_v(yb1, ydt, xi1 + t, rx)
    ctx.dim_v(g, yb1, xi1 + t, rx)
    ctx.flush_callouts(col_l=ctx.ext[0] - 4.0)
    ctx.title(item)
    return ctx.bbox()


# ---------------------------------------------------------------- 銘牌 / 告示牌


def draw_nameplate(msp, origin, item: NameplateDetail) -> tuple[float, float, float, float]:
    ctx = _Ctx(msp, origin[0], origin[1], item.scale)
    W, H = item.plate_width, item.plate_height
    ctx.rect(0, 0, W, H, lw=LW_THICK)
    m = min(W, H) * 0.05
    ctx.rect(m, m, W - m, H - m, lw=LW_THIN)
    rows = item.rows()
    n_rows = 1 + len(rows) + len(item.warnings)
    rh = (H - 2 * m) / n_rows
    th = min(rh * 0.5 * ctx.k, 3.5)  # 圖紙字高
    lab_w = (W - 2 * m) * 0.36
    y = H - m - rh
    ctx.line((m, y), (W - m, y))
    full = (W - 2 * m - rh * 0.6) * ctx.k
    ctx.text(item.heading, ctx.P(W / 2, y + rh / 2), th * 1.15, Align.MIDDLE_CENTER, max_width=full)
    for label, value in rows:
        y0 = y - rh
        ctx.line((m, y0), (W - m, y0))
        lw_p = (lab_w - rh * 0.4) * ctx.k
        vw_p = (W - 2 * m - lab_w - rh * 0.6) * ctx.k
        ctx.text(label, ctx.P(m + lab_w / 2, y0 + rh / 2), th, Align.MIDDLE_CENTER, max_width=lw_p)
        ctx.text(
            value, ctx.P(m + lab_w + rh * 0.3, y0 + rh / 2), th, Align.MIDDLE_LEFT, max_width=vw_p
        )
        ctx.line((m + lab_w, y0), (m + lab_w, y))
        y = y0
    for w in item.warnings:
        y0 = y - rh
        full = (W - 2 * m - rh * 0.6) * ctx.k
        ctx.text(w, ctx.P(W / 2, y0 + rh / 2), th, Align.MIDDLE_CENTER, max_width=full)
        if y0 > m + 1:
            ctx.line((m, y0), (W - m, y0), "H-STRC", LW_THIN)
        y = y0
    ctx.note(W, H * 0.85, f"材質：{item.material_text}")
    ctx.note(W, H * 0.25, f"固定：{item.fixing_text}")
    by = ctx.P(0, 0)[1] - 6.0
    ctx.dim_h(0, W, 0, by)
    ctx.dim_v(0, H, 0, ctx.P(0, 0)[0] - 6.0)
    ctx.flush_callouts()
    ctx.title(item)
    return ctx.bbox()


DRAWERS: dict[str, Callable] = {
    "u_ditch": draw_u_ditch,
    "raft_detention": draw_raft_detention,
    "outlet_well": draw_outlet_well,
    "orifice_plate": draw_orifice_plate,
    "outfall_protection": draw_outfall_protection,
    "ditch_joint": draw_ditch_joint,
    "nameplate": draw_nameplate,
}


# ---------------------------------------------------------------- 圖紙


def _setup_doc() -> Drawing:
    doc = new_sheet_doc()
    doc.header["$LTSCALE"] = 1.0
    apply_layers(doc, LAYERS)  # 屬性以 render/codes/layers.yaml 為準
    ds = doc.dimstyles.new(DIMSTYLE)
    ds.dxf.dimtxt = DIM_TXT
    ds.dxf.dimasz = 1.2
    ds.dxf.dimexo = 0.8
    ds.dxf.dimexe = 1.0
    ds.dxf.dimgap = 0.6
    ds.dxf.dimtad = 1
    ds.dxf.dimtih = 0
    ds.dxf.dimtoh = 0
    ds.dxf.dimdec = 0
    ds.dxf.dimlfac = 1.0  # 每個 DIMENSION 以 override 設為該詳圖比例分母
    ds.dxf.dimtmove = 2  # 文字可自由放（小尺寸抬高一層），不畫引線
    ds.set_arrows(blk=ARROWS.architectural_tick)
    ds.dxf.dimtxsty = CJK_STYLE
    return doc


def _measure(item) -> tuple[float, float, float, float]:
    tmp = _setup_doc()
    return DRAWERS[item.template](tmp.modelspace(), (0.0, 0.0), item)


def layout_details(spec: DetailSheetSpec) -> list[tuple[float, float]]:
    """Shelf packing：回傳每個詳圖的原點（圖紙 mm）。放不下 raise ValueError。"""
    sheet = spec.sheet
    ix0, iy0, ix1, iy1 = sheet.inner()
    tbw, tbh = title_block_size(sheet)
    G = SHEET_GAP
    tb = (ix1 - tbw - G, iy0, ix1, iy0 + tbh + G)
    boxes = [_measure(d) for d in spec.details]
    rows: list[list[int]] = []
    pos: dict[int, tuple[float, float]] = {}  # i → (左下角 x, 頂 y)
    row_top, cursor, row_h = iy1 - G, ix0 + G, 0.0
    for i, (x0, y0, x1, y1) in enumerate(boxes):
        w, h = x1 - x0, y1 - y0
        for attempt in range(2):
            ok = cursor + w <= ix1 - G and row_top - h >= iy0 + G
            bottom = row_top - h
            if ok and cursor + w > tb[0] and bottom < tb[3]:
                ok = False
            if ok:
                break
            if attempt == 1 or not rows or not rows[-1]:
                raise ValueError(
                    f"詳圖 '{spec.details[i].id}'（{w:.0f}×{h:.0f} mm）放不進 {sheet.size} 內框；"
                    "請改用 A1、調整比例或分張"
                )
            row_top -= row_h + G
            cursor, row_h = ix0 + G, 0.0
            rows.append([])
        if not rows:
            rows.append([])
        rows[-1].append(i)
        pos[i] = (cursor, row_top)
        cursor += w + G
        row_h = max(row_h, h)
    # 每列水平平均分配剩餘空間
    for r in rows:
        if not r:
            continue
        right = max(pos[i][0] + boxes[i][2] - boxes[i][0] for i in r)
        lim = ix1 - G
        if any(pos[i][1] - (boxes[i][3] - boxes[i][1]) < tb[3] for i in r):
            lim = tb[0]
        extra = max(0.0, lim - right) / (len(r) + 1)
        for k, i in enumerate(r):
            pos[i] = (pos[i][0] + extra * (k + 1), pos[i][1])
    origins = []
    for i, (x0, _y0, _x1, y1) in enumerate(boxes):
        lx, ty = pos[i]
        origins.append((lx - x0, ty - y1))
    return origins


def render_detail_sheet(spec: DetailSheetSpec) -> Drawing:
    doc = _setup_doc()
    msp = doc.modelspace()
    sheet = spec.sheet
    draw_frame(doc, sheet)
    draw_title_block(doc, sheet, title=spec.title, scale_text=spec.scale_text())
    origins = layout_details(spec)
    for item, org in zip(spec.details, origins, strict=True):
        n0 = len(msp)
        DRAWERS[item.template](msp, org, item)
        ents = list(msp)[n0:]
        grp = doc.groups.new(f"{GROUP_PREFIX}{item.id}", description=item.template)
        grp.set_data(ents)
    return doc


def write_detail_sheet(spec: DetailSheetSpec, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    render_detail_sheet(spec).saveas(path)
    return path


__all__ = [
    "DRAWERS",
    "GROUP_PREFIX",
    "LAYERS",
    "draw_ditch_joint",
    "draw_nameplate",
    "draw_orifice_plate",
    "draw_outfall_protection",
    "draw_outlet_well",
    "draw_raft_detention",
    "draw_u_ditch",
    "el_text",
    "layout_details",
    "render_detail_sheet",
    "write_detail_sheet",
]
