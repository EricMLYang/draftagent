"""FloorPlanSpec：多房間平面圖（Level 3-1）的 JSON spec 定義與幾何約定。單位一律 mm。

幾何約定（renderer / verifier 共用的「合約」；不要在別處重新定義）
------------------------------------------------------------------
- 座標系：x 向東、y 向北，原點任意（通常放外牆西南外角）。
- `outline` = **外牆外緣**（總尺寸標註的量測值）。外牆由外緣往內長 `wall_thickness.exterior`，
  外牆內緣圍出的範圍稱「室內範圍」（inner）。
- **房間多邊形以「外牆內緣」與「內牆中心線」為界**：房間必須鋪滿室內範圍（不重疊、不留空），
  相鄰房間共用邊界 = 一道內牆的中心線，內牆厚 `wall_thickness.interior` 由中心線往兩側各一半。
  共用牆只畫一次（由所有房間兩兩共用的邊界合併而成）。
- 房間**淨面積** = 房間多邊形 − 內牆（各扣半牆厚）。DXF 面積文字、IFC NetFloorArea、
  verifier 的面積 metrics 都是淨面積（m²，兩位小數）。
- 門 `between = [A, B]`：A、B 為房名或 "outside"（室外）。程式自動找 A、B 的共用牆段
  （直線段；"outside" = A 的邊界落在外牆內緣上的部分）：
  * `side`（N/E/S/W）：只取「由 A 往 B（或室外）」方向朝該方位的牆段
  * 候選牆段依起點 (x, y) 排序；`segment` 給索引，省略 = 最長的一段
  * 牆段起點：非垂直段取 x 較小端，垂直段取 y 較小端（與 plan 的 N/S 自西、E/W 自南相同）
  * `offset` = 開口起點距牆段起點 mm；省略 = 置中
  * 開口須離牆段兩端 ≥ 該牆厚（內牆 interior、外牆 exterior），即門寬 ≤ 牆段長 − 2 × 牆厚
  * 內牆開口的參考線 = 中心線；外牆開口的參考線 = 外牆內緣，開口往外貫穿外牆厚
- 門 `swing_into`：往哪個房間開（預設 between 中第一個房間）；`hinge`：鉸鏈在開口靠牆段
  起點端（start）或終點端（end）。
- 窗：`room` 的外牆段（"outside" 牆段，規則同門），參考線 = 外牆內緣。

schema 只擋**結構性**錯誤（名稱重複、參照不存在、入口不是外門）。幾何與拓樸錯誤（重疊、
門不在共用牆、不可達、居室無窗）不在 schema 擋——錯誤 spec 仍可出圖給人看——由
`verify/floorplan_check.py` 報 error。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from draftagent.schema.plan import Rect

Side = Literal["N", "E", "S", "W"]
Point = tuple[float, float]
OUTSIDE = "outside"
RoomUse = Literal[
    "居室", "臥室", "客廳", "餐廳", "書房", "廚房", "浴廁", "走廊", "玄關", "儲藏", "其他"
]  # fmt: skip
EPS = 1e-6  # 幾何比較容差 mm
SNAP = 1e-3  # 共用邊界 / 座標比對容差 mm


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Shape(_Model):
    rect: Rect | None = None
    polygon: list[Point] | None = Field(default=None, min_length=3, description="頂點 mm，不重複首點")

    @model_validator(mode="after")
    def _one_shape(self):
        if (self.rect is None) == (self.polygon is None):
            raise ValueError(f"{self._label()}: rect 與 polygon 必須且只能給一個")
        if self.polygon is not None and abs(signed_area(self.polygon)) < 1e-6:
            raise ValueError(f"{self._label()}: polygon 面積為 0")
        return self

    def _label(self) -> str:
        return "outline"

    def outline(self) -> list[Point]:
        """頂點（不封閉）。rect 依 S→E→N→W 逆時針排列。"""
        if self.rect is not None:
            r = self.rect
            return [
                (r.x, r.y),
                (r.x + r.width, r.y),
                (r.x + r.width, r.y + r.depth),
                (r.x, r.y + r.depth),
            ]
        return [(float(p[0]), float(p[1])) for p in self.polygon]  # type: ignore[union-attr]


class Outline(_Shape):
    """外牆外緣。"""


class FPRoom(_Shape):
    name: str = Field(min_length=1)
    use: RoomUse = Field(description="用途；居室類（見 render/codes/plan_rules.yaml）必須至少一扇窗")

    def _label(self) -> str:
        return f"room '{self.name}'"


class WallThickness(_Model):
    exterior: float = Field(gt=0, description="外牆厚 mm")
    interior: float = Field(gt=0, description="內牆厚 mm（共用牆，中心線兩側各一半）")

    @model_validator(mode="after")
    def _check(self) -> WallThickness:
        if self.interior > 2 * self.exterior:
            raise ValueError("wall_thickness.interior 不得大於 2 × exterior")
        return self


class FPDoor(_Model):
    id: str = Field(min_length=1, description="門編號（例 D1）；entrance 以此參照")
    between: tuple[str, str] = Field(description='[房名, 房名或 "outside"]')
    side: Side | None = Field(default=None, description="由 between[0] 往 between[1] 的方位")
    segment: int | None = Field(default=None, ge=0, description="候選牆段索引（依起點排序）；省略 = 最長")
    offset: float | None = Field(default=None, ge=0, description="開口起點距牆段起點 mm；省略 = 置中")
    width: float = Field(gt=0, description="門淨寬 mm")
    swing_into: str | None = Field(default=None, description="往哪個房間開；省略 = between 中第一個房間")
    hinge: Literal["start", "end"] = "start"

    def rooms(self) -> list[str]:
        return [r for r in self.between if r != OUTSIDE]

    def swing_room(self) -> str:
        return self.swing_into or self.rooms()[0]


class FPWindow(_Model):
    id: str = Field(min_length=1, description="窗編號（例 W1）")
    room: str = Field(min_length=1)
    side: Side | None = Field(default=None, description="外牆方位；省略 = 該房間最長的外牆段")
    segment: int | None = Field(default=None, ge=0)
    offset: float | None = Field(default=None, ge=0, description="省略 = 置中")
    width: float = Field(gt=0, description="窗寬 mm")


class FPDimensions(_Model):
    auto_overall: bool = Field(default=True, description="外總尺寸（南側總長、西側總寬）")
    room_chains: bool = Field(default=True, description="尺寸鏈：外緣 → 內牆中心線 → 外緣（南、西側）")
    offset: float = Field(default=800, gt=0, description="第一排尺寸線距外牆外緣 mm")
    text_height: float | None = Field(
        default=None, gt=0, description="標註字高（model mm）；省略 = 印出 2.5 mm × print_scale"
    )


class IfcOptions(_Model):
    """IFC 立體化用的高度（示意值，平面圖本身不使用；不是法規數值，可依設計改）。"""

    storey_name: str = "1F"
    wall_height: float = Field(default=3000, gt=0)
    door_height: float = Field(default=2100, gt=0)
    window_sill: float = Field(default=900, ge=0)
    window_height: float = Field(default=1200, gt=0)


class FloorPlanSpec(_Model):
    kind: Literal["floor-plan"] = "floor-plan"
    units: Literal["mm"] = "mm"
    title: str = ""
    print_scale: float = Field(default=100, gt=0, description="出圖比例分母（100 = 1/100）")
    outline: Outline = Field(description="外牆外緣")
    wall_thickness: WallThickness
    rooms: list[FPRoom] = Field(min_length=1)
    doors: list[FPDoor] = Field(min_length=1)
    windows: list[FPWindow] = Field(default_factory=list)
    entrance: str = Field(description="主入口門 id（必須是通往 outside 的門）")
    dimensions: FPDimensions = Field(default_factory=FPDimensions)
    frame: bool = True
    ifc: IfcOptions = Field(default_factory=IfcOptions)

    @model_validator(mode="after")
    def _check(self) -> FloorPlanSpec:
        names = [r.name for r in self.rooms]
        dup = sorted({n for n in names if names.count(n) > 1})
        if dup:
            raise ValueError(f"room 名稱不得重複：{dup}")
        if OUTSIDE in names:
            raise ValueError(f'room 名稱不得為保留字 "{OUTSIDE}"')
        ids = [d.id for d in self.doors] + [w.id for w in self.windows]
        dup = sorted({i for i in ids if ids.count(i) > 1})
        if dup:
            raise ValueError(f"門窗 id 不得重複：{dup}")
        known = set(names)
        for d in self.doors:
            a, b = d.between
            if a == b:
                raise ValueError(f"door {d.id}: between 兩端相同 {a!r}")
            for r in d.between:
                if r != OUTSIDE and r not in known:
                    raise ValueError(f"door {d.id}: 房間 {r!r} 不存在（可用：{names}）")
            if not d.rooms():
                raise ValueError(f"door {d.id}: between 至少要有一個房間")
            if d.swing_into is not None and d.swing_into not in d.rooms():
                raise ValueError(f"door {d.id}: swing_into {d.swing_into!r} 必須是 between 中的房間")
            if d.side is not None and d.segment is not None:
                raise ValueError(f"door {d.id}: side 與 segment 只能給一個")
        for w in self.windows:
            if w.room not in known:
                raise ValueError(f"window {w.id}: 房間 {w.room!r} 不存在（可用：{names}）")
            if w.side is not None and w.segment is not None:
                raise ValueError(f"window {w.id}: side 與 segment 只能給一個")
        ent = next((d for d in self.doors if d.id == self.entrance), None)
        if ent is None:
            raise ValueError(f"entrance {self.entrance!r} 不是任何門的 id")
        if OUTSIDE not in ent.between:
            raise ValueError(f'entrance {self.entrance!r} 必須是通往 "{OUTSIDE}" 的門')
        return self

    def room(self, name: str) -> FPRoom:
        for r in self.rooms:
            if r.name == name:
                return r
        raise KeyError(name)

    def door(self, did: str) -> FPDoor:
        for d in self.doors:
            if d.id == did:
                return d
        raise KeyError(did)

    def bbox(self) -> tuple[float, float, float, float]:
        pts = self.outline.outline()
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return min(xs), min(ys), max(xs), max(ys)

    def overall_dims(self) -> tuple[float, float]:
        x0, y0, x1, y1 = self.bbox()
        return x1 - x0, y1 - y0

    @classmethod
    def from_json(cls, path: str | Path) -> FloorPlanSpec:
        return cls.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


def signed_area(pts: list[Point]) -> float:
    return 0.5 * sum(
        pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1]
        for i in range(len(pts))
    )


# ---------------------------------------------------------------- 幾何約定（合約）


class LocateError(ValueError):
    """開口找不到所在牆段 / 超出牆段範圍。"""


@dataclass(frozen=True)
class WallSeg:
    """一段直線牆段（參考線）：內牆 = 中心線；外牆 = 外牆內緣。start → end 依起點規則。"""

    start: Point
    end: Point
    exterior: bool

    @property
    def length(self) -> float:
        return math.dist(self.start, self.end)

    @property
    def u(self) -> Point:
        L = self.length
        return ((self.end[0] - self.start[0]) / L, (self.end[1] - self.start[1]) / L)


@dataclass(frozen=True)
class Placement:
    """開口位置。a → b 在參考線上（a = 起點 + offset）；n_out：由 between[0] / 窗所在房間往外的法向。"""

    seg: WallSeg
    offset: float
    width: float
    thickness: float
    a: Point
    b: Point
    n_out: Point

    @property
    def exterior(self) -> bool:
        return self.seg.exterior


def inner_polygon(spec: FloorPlanSpec):
    """室內範圍（外牆內緣）shapely Polygon。"""
    from shapely.geometry import Polygon

    outer = Polygon(spec.outline.outline())
    return outer.buffer(-spec.wall_thickness.exterior, join_style="mitre")


def room_polygon(room: FPRoom):
    from shapely.geometry import Polygon

    return Polygon(room.outline())


def canonical(p: Point, q: Point) -> tuple[Point, Point]:
    """牆段起點規則：非垂直取 x 較小端；垂直（|dx| ≤ SNAP）取 y 較小端。"""
    if abs(p[0] - q[0]) > SNAP:
        return (p, q) if p[0] < q[0] else (q, p)
    return (p, q) if p[1] < q[1] else (q, p)


def _straight_segments(geom) -> list[tuple[Point, Point]]:
    """線狀幾何 → 合併後的直線段（共線相接者合併）。"""
    from shapely.geometry import LineString, MultiLineString
    from shapely.ops import linemerge

    lines: list = []
    if geom.is_empty:
        return []
    parts = getattr(geom, "geoms", [geom])
    for g in parts:
        if isinstance(g, LineString) and g.length > SNAP:
            lines.append(g)
        elif isinstance(g, MultiLineString):
            lines += [x for x in g.geoms if x.length > SNAP]
        elif hasattr(g, "geoms"):
            lines += [x for x in g.geoms if isinstance(x, LineString) and x.length > SNAP]
    if not lines:
        return []
    merged = linemerge(lines)
    merged = list(getattr(merged, "geoms", [merged]))
    out: list[tuple[Point, Point]] = []
    for ln in merged:
        pts = [(float(x), float(y)) for x, y in ln.coords]
        cur = [pts[0]]
        for p in pts[1:]:
            if len(cur) >= 2 and _collinear(cur[-2], cur[-1], p):
                cur[-1] = p
            elif math.dist(cur[-1], p) > SNAP:
                cur.append(p)
        out += [(cur[i], cur[i + 1]) for i in range(len(cur) - 1)]
    return out


def _collinear(a: Point, b: Point, c: Point) -> bool:
    cross = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    return abs(cross) <= SNAP * max(math.dist(a, c), 1.0)


def shared_segments(spec: FloorPlanSpec, a: str, b: str) -> list[WallSeg]:
    """房間 a 與 b（或 "outside"）的共用直線牆段，依起點 (x, y) 排序。"""
    pa = room_polygon(spec.room(a))
    if b == OUTSIDE:
        geom = pa.boundary.intersection(inner_polygon(spec).boundary)
        ext = True
    else:
        geom = pa.boundary.intersection(room_polygon(spec.room(b)).boundary)
        ext = False
    segs = [WallSeg(*canonical(p, q), exterior=ext) for p, q in _straight_segments(geom)]
    return sorted(segs, key=lambda s: (round(s.start[0], 3), round(s.start[1], 3)))


def outward_normal(spec: FloorPlanSpec, room: str, seg: WallSeg) -> Point:
    """牆段上由 room 往外（往鄰房 / 室外）的單位法向。"""
    from shapely.geometry import Point as SPoint

    ux, uy = seg.u
    n = (-uy, ux)
    mid = ((seg.start[0] + seg.end[0]) / 2, (seg.start[1] + seg.end[1]) / 2)
    probe = SPoint(mid[0] + n[0] * 1.0, mid[1] + n[1] * 1.0)
    if room_polygon(spec.room(room)).buffer(0).contains(probe):
        n = (-n[0], -n[1])
    return n


def side_of(n: Point) -> Side:
    ang = math.degrees(math.atan2(n[1], n[0])) % 360.0
    if ang < 45 or ang >= 315:
        return "E"
    if ang < 135:
        return "N"
    if ang < 225:
        return "W"
    return "S"


def locate(
    spec: FloorPlanSpec,
    a: str,
    b: str,
    *,
    width: float,
    side: Side | None = None,
    segment: int | None = None,
    offset: float | None = None,
    label: str = "opening",
) -> Placement:
    """依合約找開口位置；找不到 / 超出範圍 → LocateError（訊息給 agent 修 spec 用）。"""
    segs = shared_segments(spec, a, b)
    other = "室外" if b == OUTSIDE else f"'{b}'"
    if not segs:
        raise LocateError(f"{label}: '{a}' 與 {other} 沒有共用牆（房間邊界不相接）")
    if side is not None:
        cands = [s for s in segs if side_of(outward_normal(spec, a, s)) == side]
        if not cands:
            have = sorted({side_of(outward_normal(spec, a, s)) for s in segs})
            raise LocateError(f"{label}: '{a}' 與 {other} 的共用牆沒有朝 {side} 的段（有：{have}）")
    else:
        cands = segs
    if segment is not None:
        if segment >= len(cands):
            raise LocateError(f"{label}: segment={segment} 超出候選牆段數 {len(cands)}")
        seg = cands[segment]
    else:
        seg = max(cands, key=lambda s: (round(s.length, 3), -cands.index(s)))
    t = spec.wall_thickness.exterior if seg.exterior else spec.wall_thickness.interior
    L = seg.length
    off = (L - width) / 2 if offset is None else offset
    if off < t - EPS or off + width > L - t + EPS:
        raise LocateError(
            f"{label}: 開口 [{off:g}, {off + width:g}] 超出牆段可用範圍 [{t:g}, {L - t:g}]"
            f"（'{a}'–{other} 牆段 {_fmt_pt(seg.start)}→{_fmt_pt(seg.end)} 長 {L:g}，"
            f"兩端須留牆厚 {t:g}；門窗寬 ≤ 牆段長 − 2 × 牆厚 = {L - 2 * t:g}）"
        )
    ux, uy = seg.u
    pa = (seg.start[0] + ux * off, seg.start[1] + uy * off)
    pb = (pa[0] + ux * width, pa[1] + uy * width)
    return Placement(seg, off, width, t, pa, pb, outward_normal(spec, a, seg))


def _fmt_pt(p: Point) -> str:
    return f"({p[0]:.0f}, {p[1]:.0f})"


def door_placement(spec: FloorPlanSpec, d: FPDoor) -> Placement:
    """門位置；between 寫成 ["outside", 房] 時視同 [房, "outside"]（n_out 一律由房間往外）。"""
    a, b = d.between if d.between[0] != OUTSIDE else (d.between[1], d.between[0])
    return locate(
        spec,
        a,
        b,
        width=d.width,
        side=d.side,
        segment=d.segment,
        offset=d.offset,
        label=f"door {d.id}",
    )


def window_placement(spec: FloorPlanSpec, w: FPWindow) -> Placement:
    return locate(
        spec,
        w.room,
        OUTSIDE,
        width=w.width,
        side=w.side,
        segment=w.segment,
        offset=w.offset,
        label=f"window {w.id}",
    )


def interior_wall_lines(spec: FloorPlanSpec) -> list[WallSeg]:
    """所有內牆中心線（房間兩兩共用邊界的聯集，合併成直線段）。"""
    from shapely.ops import unary_union

    polys = [room_polygon(r) for r in spec.rooms]
    parts = []
    for i in range(len(polys)):
        for j in range(i + 1, len(polys)):
            g = polys[i].boundary.intersection(polys[j].boundary)
            if not g.is_empty and g.length > SNAP:
                parts.append(g)
    if not parts:
        return []
    lines = unary_union(parts)
    segs = [WallSeg(*canonical(p, q), exterior=False) for p, q in _straight_segments(lines)]
    return sorted(segs, key=lambda s: (round(s.start[0], 3), round(s.start[1], 3), s.end))


def exterior_edges(spec: FloorPlanSpec) -> list[tuple[Point, Point, Point, Point]]:
    """外牆各邊的 (外緣起點, 外緣終點, 內緣終點, 內緣起點)，外框逆時針、轉角斜接（IFC 外牆分段用）。"""
    pts = spec.outline.outline()
    if signed_area(pts) < 0:
        pts = pts[::-1]
    t = spec.wall_thickness.exterior
    n = len(pts)

    def off_line(i: int) -> tuple[Point, Point]:
        p, q = pts[i], pts[(i + 1) % n]
        L = math.dist(p, q)
        nx, ny = -(q[1] - p[1]) / L, (q[0] - p[0]) / L  # 逆時針 → 左法向朝內
        return (p[0] + nx * t, p[1] + ny * t), (q[0] + nx * t, q[1] + ny * t)

    def inter(l1, l2) -> Point:
        (x1, y1), (x2, y2) = l1
        (x3, y3), (x4, y4) = l2
        den = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
        if abs(den) < 1e-12:
            return l1[1]
        a, b = x1 * y2 - y1 * x2, x3 * y4 - y3 * x4
        return ((a * (x3 - x4) - (x1 - x2) * b) / den, (a * (y3 - y4) - (y1 - y2) * b) / den)

    offs = [off_line(i) for i in range(n)]
    q = [inter(offs[i - 1], offs[i]) for i in range(n)]  # q[i] = 內緣頂點（對應外緣 pts[i]）
    return [(pts[i], pts[(i + 1) % n], q[(i + 1) % n], q[i]) for i in range(n)]
