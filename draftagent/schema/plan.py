"""PlanSpec：Level 1-1 平面圖的 JSON spec 定義。單位一律 mm。

幾何慣例
--------
- 座標系：x 向東、y 向北，原點任意（通常放房間西南角）。
- 房間外框 = **外牆外緣**。牆由外緣往內長 `wall_thickness`，因此
  `rect.width` / `rect.depth` 就是外尺寸，也就是自動總尺寸標註的量測值。
- 每個房間各自一圈牆（v0 不處理共用牆；多房間相鄰見 Level 3-1）。
- 開口位置：
  - `side` 只能用在 `rect` 房間。N / S 牆的 `offset` 自西側外角起算，
    E / W 牆自南側外角起算。
  - `edge` 用在 `polygon` 房間：第 i 段牆 = vertices[i] → vertices[i+1]，
    `offset` 自 vertices[i] 起算。
  - 開口必須離牆段兩端至少 `wall_thickness`（不得切進轉角的垂直牆）。
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Side = Literal["N", "E", "S", "W"]
Point = tuple[float, float]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Rect(_Model):
    """矩形外框（外牆外緣），(x, y) 為西南角。"""

    x: float = 0.0
    y: float = 0.0
    width: float = Field(gt=0, description="東西向外尺寸 mm")
    depth: float = Field(gt=0, description="南北向外尺寸 mm")


class Room(_Model):
    name: str = Field(min_length=1)
    rect: Rect | None = None
    polygon: list[Point] | None = Field(
        default=None, min_length=3, description="外牆外緣頂點，mm；不需重複首點"
    )

    @model_validator(mode="after")
    def _one_shape(self) -> Room:
        if (self.rect is None) == (self.polygon is None):
            raise ValueError(f"room '{self.name}': rect 與 polygon 必須且只能給一個")
        if self.polygon is not None and abs(_signed_area(self.polygon)) < 1e-6:
            raise ValueError(f"room '{self.name}': polygon 面積為 0")
        return self

    def outline(self) -> list[Point]:
        """外牆外緣頂點（不封閉）。rect 依 S→E→N→W 逆時針排列。"""
        if self.rect is not None:
            r = self.rect
            return [
                (r.x, r.y),
                (r.x + r.width, r.y),
                (r.x + r.width, r.y + r.depth),
                (r.x, r.y + r.depth),
            ]
        return [tuple(p) for p in self.polygon]  # type: ignore[union-attr]

    def segment(self, side: Side | None = None, edge: int | None = None) -> tuple[Point, Point]:
        """回傳開口所在牆段 (起點, 終點)，offset 自起點量。"""
        if side is not None:
            if self.rect is None:
                raise ValueError(f"room '{self.name}': side 只能用在 rect 房間，請改用 edge")
            r = self.rect
            x0, y0, x1, y1 = r.x, r.y, r.x + r.width, r.y + r.depth
            return {
                "S": ((x0, y0), (x1, y0)),
                "N": ((x0, y1), (x1, y1)),
                "W": ((x0, y0), (x0, y1)),
                "E": ((x1, y0), (x1, y1)),
            }[side]
        pts = self.outline()
        if edge is None or not 0 <= edge < len(pts):
            raise ValueError(f"room '{self.name}': edge 必須介於 0..{len(pts) - 1}")
        return pts[edge], pts[(edge + 1) % len(pts)]


class Opening(_Model):
    type: Literal["door", "window"]
    room: str | None = Field(default=None, description="所屬房間名；只有一個房間時可省略")
    side: Side | None = None
    edge: int | None = Field(default=None, ge=0)
    offset: float = Field(ge=0, description="開口起點距牆段起點 mm")
    width: float = Field(gt=0, description="開口淨寬 mm")
    swing: Literal["in", "out"] = Field(default="in", description="門：往室內或室外開")
    hinge: Literal["start", "end"] = Field(
        default="start", description="門：鉸鏈在開口靠牆段起點端或終點端"
    )

    @model_validator(mode="after")
    def _one_locator(self) -> Opening:
        if (self.side is None) == (self.edge is None):
            raise ValueError("opening: side 與 edge 必須且只能給一個")
        return self


class Dimensions(_Model):
    auto_overall: bool = Field(default=True, description="自動標註總長（南側）與總寬（西側）")
    offset: float = Field(default=800, gt=0, description="尺寸線距外牆外緣 mm")
    text_height: float | None = Field(
        default=None,
        gt=0,
        description="標註字高（model mm）；省略 = 印出 2.5 mm × print_scale",
    )


class PlanSpec(_Model):
    kind: Literal["plan"] = "plan"
    units: Literal["mm"] = "mm"
    title: str = ""
    wall_thickness: float = Field(gt=0, description="牆厚 mm")
    rooms: list[Room] = Field(min_length=1)
    openings: list[Opening] = Field(default_factory=list)
    dimensions: Dimensions = Field(default_factory=Dimensions)
    frame: bool = Field(default=True, description="是否畫 X-FRAME 圖框")
    print_scale: float = Field(
        default=50,
        gt=0,
        description="出圖比例分母（50 = 1/50）；renderer 依此設定文字 / 標註字高，"
        "verifier 依此換算印出字高",
    )

    @model_validator(mode="after")
    def _check(self) -> PlanSpec:
        names = [r.name for r in self.rooms]
        if len(set(names)) != len(names):
            raise ValueError("room 名稱不得重複")
        _check_rooms_disjoint(self.rooms)
        t = self.wall_thickness
        for i, op in enumerate(self.openings):
            room = self.room_of(op)
            (x0, y0), (x1, y1) = room.segment(op.side, op.edge)
            length = math.hypot(x1 - x0, y1 - y0)
            if length <= 2 * t:
                raise ValueError(f"openings[{i}]: 牆段長 {length:g} ≤ 2×牆厚")
            if op.offset < t or op.offset + op.width > length - t:
                raise ValueError(
                    f"openings[{i}]: 開口 [{op.offset:g}, {op.offset + op.width:g}] 超出牆段可用範圍 "
                    f"[{t:g}, {length - t:g}]（牆段長 {length:g}，兩端須留牆厚 {t:g}）"
                )
        # 同一牆段的開口區間不得重疊
        by_wall: dict[tuple[str, str], list[tuple[float, float, int]]] = {}
        for i, op in enumerate(self.openings):
            key = (self.room_of(op).name, f"side:{op.side}" if op.side else f"edge:{op.edge}")
            by_wall.setdefault(key, []).append((op.offset, op.offset + op.width, i))
        for (room, wall), spans in by_wall.items():
            spans.sort()
            for (a0, a1, i), (b0, b1, j) in zip(spans, spans[1:], strict=False):
                if b0 < a1 - 1e-9:
                    raise ValueError(
                        f"openings[{i}] [{a0:g}, {a1:g}] 與 openings[{j}] [{b0:g}, {b1:g}] "
                        f"在房間 '{room}' 同一牆段（{wall}）重疊"
                    )
        return self

    def room_of(self, op: Opening) -> Room:
        if op.room is None:
            if len(self.rooms) != 1:
                raise ValueError("opening.room 在多房間時必填")
            return self.rooms[0]
        for r in self.rooms:
            if r.name == op.room:
                return r
        raise ValueError(f"opening.room '{op.room}' 不存在")

    def bbox(self) -> tuple[float, float, float, float]:
        """所有房間外緣的 (minx, miny, maxx, maxy)。"""
        pts = [p for r in self.rooms for p in r.outline()]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return min(xs), min(ys), max(xs), max(ys)

    def overall_dims(self) -> tuple[float, float]:
        minx, miny, maxx, maxy = self.bbox()
        return maxx - minx, maxy - miny

    @classmethod
    def from_json(cls, path: str | Path) -> PlanSpec:
        return cls.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


def _check_rooms_disjoint(rooms: list[Room]) -> None:
    """房間外框（外牆外緣）必須是有效多邊形，且兩兩不得重疊（相鄰 / 共邊可）。"""
    from shapely.geometry import Polygon

    polys = []
    for r in rooms:
        poly = Polygon(r.outline())
        if not poly.is_valid:
            raise ValueError(f"room '{r.name}': polygon 自相交或無效")
        polys.append(poly)
    for i in range(len(rooms)):
        for j in range(i + 1, len(rooms)):
            area = polys[i].intersection(polys[j]).area
            if area > 1e-6:
                raise ValueError(
                    f"room '{rooms[i].name}' 與 '{rooms[j].name}' 重疊 {area:g} mm²（房間不得重疊）"
                )


def _signed_area(pts: list[Point]) -> float:
    return 0.5 * sum(
        pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1]
        for i in range(len(pts))
    )
