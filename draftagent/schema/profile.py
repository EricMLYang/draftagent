"""ProfileSpec：排水系統縱斷面圖的 JSON spec 定義。

單位
----
- spec 內樁號、高程、尺寸一律 **m**（測量習慣）。
- DXF 以 **圖紙 mm** 為繪圖座標（$INSUNITS=4）：renderer 依 `h_scale` / `v_scale`
  把樁號 m、高程 m 換算成圖紙 mm（x = Δstation × 1000 / h_scale，y = Δel × 1000 / v_scale）。

慣例
----
- 流向 = 樁號遞增方向（第一個節點為最上游）。
- 坡度**不由 agent 填**：renderer / verifier 由 invert_el 與樁號差計算，
  S = (上游溝底 − 下游溝底) / 距離 × 100 %，順坡為正。
- 水平比例上限（桃園檢核表 S≧1/200）從 `hydro/codes/taoyuan_drawing.yaml` 讀，不寫死。
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from draftagent.hydro.drawing_codes import load_drawing_codes
from draftagent.schema.sheet import SheetSpec

NodeKind = Literal["catch_pit", "manhole", "inlet", "outlet_pit", "outfall", "junction", "other"]
LevelKind = Literal[
    "pipe_in", "pipe_out", "overflow", "design_wl", "pump_start", "pump_stop", "other"
]
WATER_LEVEL_KINDS = frozenset({"design_wl", "pump_start", "pump_stop"})
PROFILE_SCALE_KEY = "drainage_profile"

# 縱斷面下方表格帶的列（由上而下）：key → 列名文字。renderer 與 verifier 共用的圖面契約：
# 每列的值 TEXT 以 MIDDLE_CENTER 對齊在列中心，verifier 以「與列名同 y」辨識所屬列。
PROFILE_TABLE_ROWS: dict[str, str] = {
    "station": "樁號",
    "ground": "地面高程 EL",
    "top": "溝頂高程 EL",
    "invert": "溝底高程 EL",
    "road": "道路中心樁高程 EL",
    "slope": "坡度 S (%)",
    "length": "距離 (m)",
    "section": "斷面",
}
NODE_ROWS = ("station", "ground", "top", "invert", "road")
SEGMENT_ROWS = ("slope", "length", "section")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProfileNode(_Model):
    station_m: float = Field(ge=0, description="樁號 m（0+012.00 → 12.0）")
    label: str = Field(min_length=1, description="節點名稱，如 MH1、集水井 CP1、放流井")
    kind: NodeKind = "manhole"
    ground_el: float = Field(description="地面高程 m")
    invert_el: float = Field(description="溝底（管底）高程 m")
    top_el: float | None = Field(default=None, description="溝頂高程 m")
    road_center_el: float | None = Field(default=None, description="道路中心樁高程 m")

    @model_validator(mode="after")
    def _check(self) -> ProfileNode:
        if self.invert_el >= self.ground_el:
            raise ValueError(f"node '{self.label}': invert_el 必須低於 ground_el")
        if self.top_el is not None and not self.invert_el < self.top_el:
            raise ValueError(f"node '{self.label}': top_el 必須高於 invert_el")
        return self


class ProfileSegment(_Model):
    """相鄰節點 i → i+1 之間的溝/管段。segments[i] 連接 nodes[i] 與 nodes[i+1]。"""

    section: str = Field(min_length=1, description="斷面文字，如「U型溝 W40×H50」「RCP D300」")
    size_m: float = Field(gt=0, description="溝高或管徑 m")
    n: float | None = Field(default=None, gt=0, description="曼寧 n（僅記錄，本圖不計算）")
    pump: bool = Field(default=False, description="抽水段：允許逆坡")


class ProfileLevel(_Model):
    """特殊高程標示（進水管、放流管、溢流、設計水位、起抽/停機水位…）。"""

    station_m: float = Field(ge=0)
    el: float
    label: str = Field(min_length=1, description="如「進水管」「放流管」「溢流高程」")
    kind: LevelKind = "other"


StructureKind = Literal["detention", "well", "ditch"]
STRUCTURE_KIND_NAME = {"detention": "滯洪池", "well": "井", "ditch": "受納側溝"}


class StructureMark(_Model):
    """構造物內的高程標示（水位線 / 管底）。kind 屬 WATER_LEVEL_KINDS → 水位虛線＋▽，否則實線短標。"""

    label: str = Field(min_length=1)
    el: float
    kind: LevelKind = "other"


class ProfileStructure(_Model):
    """縱斷面上的構造物示意（滯洪池 / 井 / 受納側溝斷面）。

    x0_m / x1_m 為樁號 m（可 < 第一樁或 > 最後樁：例如筏基延伸到路徑終點之外）。
    圖面文字契約（profile_check 依此檢查，sheetset_check 依此解析）：
    「<label>頂 EL=xx.xx」「<label>底 EL=xx.xx」，每個 mark「<mark.label> EL=xx.xx」。
    """

    kind: StructureKind
    label: str = Field(min_length=1, description="如「筏基滯洪」「放流井OW1」「受納溝」")
    x0_m: float
    x1_m: float
    bottom_el: float
    top_el: float
    marks: list[StructureMark] = Field(default_factory=list)
    pump_station_m: float | None = Field(default=None, description="抽水機位置樁號（detention）")
    pump_outlet_el: float | None = Field(default=None, description="抽水出水管入井管底（立管頂）")

    @model_validator(mode="after")
    def _check(self) -> ProfileStructure:
        if not self.x1_m > self.x0_m:
            raise ValueError(f"structure '{self.label}': x1_m 必須大於 x0_m")
        if not self.top_el > self.bottom_el:
            raise ValueError(f"structure '{self.label}': top_el 必須高於 bottom_el")
        return self

    def expected_texts(self) -> list[tuple[str, float]]:
        out = [(f"{self.label}頂", self.top_el), (f"{self.label}底", self.bottom_el)]
        out += [(m.label, m.el) for m in self.marks]
        return out

    def els(self) -> list[float]:
        vals = [self.bottom_el, self.top_el] + [m.el for m in self.marks]
        if self.pump_outlet_el is not None:
            vals.append(self.pump_outlet_el)
        return vals


class ProfileSpec(_Model):
    kind: Literal["profile"] = "profile"
    units: Literal["mm"] = Field(
        default="mm", description="DXF 繪圖單位（圖紙 mm，$INSUNITS=4）；樁號 / 高程欄位仍為 m"
    )
    title: str = Field(min_length=1, description="圖名，如「U1 排水幹線縱斷面圖」")
    line_name: str = Field(min_length=1, description="如「U1 排水幹線」")
    datum_el: float | None = Field(
        default=None, description="基準高程 m；None → floor(最低高程) − 0.5"
    )
    h_scale: int = Field(gt=0, description="水平比例分母，如 200 → 1/200")
    v_scale: int = Field(gt=0, description="垂直比例分母，如 50 → 1/50（可誇張）")
    nodes: list[ProfileNode] = Field(min_length=2)
    segments: list[ProfileSegment]
    levels: list[ProfileLevel] = Field(default_factory=list)
    structures: list[ProfileStructure] = Field(
        default_factory=list, description="構造物示意（滯洪池 / 放流井 / 受納側溝）；選填"
    )
    sheet: SheetSpec

    @model_validator(mode="after")
    def _check(self) -> ProfileSpec:
        rule = load_drawing_codes().scale(PROFILE_SCALE_KEY)
        if self.h_scale > rule.max_denominator:
            raise ValueError(
                f"h_scale 1/{self.h_scale} 小於 {rule.name}要求 S≧1/{rule.max_denominator}"
                f"（{rule.source}）；分母須 ≤ {rule.max_denominator}"
            )
        st = [n.station_m for n in self.nodes]
        for i in range(1, len(st)):
            if not st[i] > st[i - 1]:
                raise ValueError(f"nodes[{i}].station_m {st[i]:g} 必須大於前一節點 {st[i - 1]:g}")
        if len(self.segments) != len(self.nodes) - 1:
            raise ValueError(
                f"segments 數量 {len(self.segments)} 必須 = nodes 數量 − 1（{len(self.nodes) - 1}）"
            )
        names = [n.label for n in self.nodes]
        if len(set(names)) != len(names):
            raise ValueError("node label 不得重複")
        for i, lv in enumerate(self.levels):
            if not st[0] <= lv.station_m <= st[-1]:
                raise ValueError(
                    f"levels[{i}] '{lv.label}' station_m {lv.station_m:g} 超出 [{st[0]:g}, {st[-1]:g}]"
                )
        if self.datum_el is not None and self.datum_el >= self.min_el():
            raise ValueError(f"datum_el {self.datum_el:g} 必須低於最低高程 {self.min_el():g}")
        return self

    # ------------------------------------------------------------ derived

    def min_el(self) -> float:
        vals = [n.invert_el for n in self.nodes] + [lv.el for lv in self.levels]
        for st in self.structures:
            vals += st.els()
        return min(vals)

    def station_range(self) -> tuple[float, float]:
        """繪圖範圍（含構造物）的樁號 (min, max)。"""
        lo = [self.nodes[0].station_m] + [st.x0_m for st in self.structures]
        hi = [self.nodes[-1].station_m] + [st.x1_m for st in self.structures]
        return min(lo), max(hi)

    def max_el(self) -> float:
        vals = [n.ground_el for n in self.nodes]
        vals += [n.top_el for n in self.nodes if n.top_el is not None]
        vals += [n.road_center_el for n in self.nodes if n.road_center_el is not None]
        vals += [lv.el for lv in self.levels]
        for st in self.structures:
            vals += st.els()
        for i, s in enumerate(self.segments):
            vals += [self.nodes[i].invert_el + s.size_m, self.nodes[i + 1].invert_el + s.size_m]
        return max(vals)

    def resolved_datum(self) -> float:
        return self.datum_el if self.datum_el is not None else math.floor(self.min_el()) - 0.5

    def lengths(self) -> list[float]:
        return [b.station_m - a.station_m for a, b in zip(self.nodes, self.nodes[1:], strict=False)]

    def slopes_pct(self) -> list[float]:
        """各段坡度 %（順坡為正）＝(上游溝底 − 下游溝底) / 距離 × 100。"""
        return [
            (a.invert_el - b.invert_el) / (b.station_m - a.station_m) * 100
            for a, b in zip(self.nodes, self.nodes[1:], strict=False)
        ]

    def scale_text(self) -> str:
        return f"水平 1/{self.h_scale}　垂直 1/{self.v_scale}"

    @classmethod
    def from_json(cls, path: str | Path) -> ProfileSpec:
        return cls.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


def format_station(m: float) -> str:
    """12.0 → '0+012.00'。"""
    m = round(m, 2)
    km = int(m // 1000)
    return f"{km}+{m - km * 1000:06.2f}"


def parse_station(text: str) -> float:
    """'0+012.00' → 12.0。"""
    km, rest = text.strip().split("+")
    return int(km) * 1000 + float(rest)
