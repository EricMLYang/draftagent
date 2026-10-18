"""DetailSheetSpec：排水系統各部詳圖（參數化模板）的 JSON spec 定義。

單位
----
- 尺寸欄位一律 **實尺 mm**（欄位名無 `_el` 者）。
- 高程欄位（名稱以 `_el` 結尾）一律 **m**，最多兩位小數（圖面以 `EL=xx.xx` 呈現）。
- DXF 以 **圖紙 mm** 為繪圖座標（$INSUNITS=4，1:1 出圖）；每個詳圖依自己的 `scale`
  分母把實尺 mm 縮成圖紙 mm（圖紙 = 實尺 / scale）。尺寸標註以 `dimlfac = scale`
  讓顯示值回到實尺 mm（見 render/detail.py）。

範圍
----
本 schema 只描述**幾何與文字**；不做結構、配筋或水理計算。鋼筋欄位只是示意文字。
幾何合理性（孔口 < 設計滿水位 ≤ 溢流口 < 溢流時最高水位 ≤ 坑頂、有效水深 ≤ 坑深、放流管底 ≥ 側溝底…）
刻意**不在 schema 擋**，由 `verify/detail_check.py` 從 spec 重算後回報 error，
讓錯誤 spec 仍可出圖供人檢視。
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from draftagent.schema.sheet import SheetSpec

PLACEHOLDER = "○○○"


class LegacyFieldWarning(DeprecationWarning):
    """spec 使用已更名的舊欄位（已自動映射；detail_check 會轉成 WARNING）。"""
DETAIL_SCALE_KEY = "drainage_details"  # hydro/codes/taoyuan_drawing.yaml


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _check_el(v: float | None) -> float | None:
    if v is not None and abs(round(v, 2) - v) > 1e-9:
        raise ValueError(f"高程 {v} m 超過兩位小數（EL 一律以 m、兩位小數表示）")
    return v


class _DetailBase(_Model):
    id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_-]+$", description="詳圖代號（唯一）")
    title: str = Field(min_length=1, description="詳圖名稱，如「U型溝斷面詳圖」")
    section_code: str = Field(
        pattern=r"^[A-Z]{1,2}-[A-Z]{1,2}$", description="剖面代號，如 A-A（全圖唯一）"
    )
    scale: int = Field(ge=1, description="比例分母，10 → S=1/10")

    @field_validator("*", mode="after")
    @classmethod
    def _el_two_decimals(cls, v, info):
        if info.field_name and info.field_name.endswith("_el"):
            return _check_el(v)
        return v

    def scale_text(self) -> str:
        return f"S=1/{self.scale}"


# ---------------------------------------------------------------- 共用子件


class Cover(_Model):
    thickness: float = Field(gt=0, description="蓋板厚 mm")
    type_text: str = Field(min_length=1, description="型式文字，如「預鑄 RC 蓋板」「鑄鐵格柵蓋」")


class RebarNote(_Model):
    """鋼筋示意（不做配筋計算）。"""

    bar: str = Field(min_length=1, description="筋號文字，如 #3、D10")
    spacing: str = Field(min_length=1, description="間距文字，如 @200")
    cover: float = Field(default=50.0, gt=0, description="示意保護層 mm（僅決定示意線位置）")


class Orifice(_Model):
    shape: Literal["circle", "rect"] = "circle"
    diameter: float | None = Field(default=None, gt=0, description="圓形孔徑 mm")
    width: float | None = Field(default=None, gt=0, description="矩形孔寬 mm")
    height: float | None = Field(default=None, gt=0, description="矩形孔高 mm")
    center_el: float = Field(description="孔口中心高程 m")

    @field_validator("center_el")
    @classmethod
    def _el(cls, v):
        return _check_el(v)

    @model_validator(mode="after")
    def _check(self) -> Orifice:
        if self.shape == "circle" and self.diameter is None:
            raise ValueError("圓形孔口必須給 diameter")
        if self.shape == "rect" and (self.width is None or self.height is None):
            raise ValueError("矩形孔口必須給 width 與 height")
        return self

    def size_h(self) -> float:
        """孔口垂直向尺寸 mm。"""
        return self.diameter if self.shape == "circle" else self.height  # type: ignore[return-value]

    def size_w(self) -> float:
        return self.diameter if self.shape == "circle" else self.width  # type: ignore[return-value]

    def size_text(self) -> str:
        if self.shape == "circle":
            return f"D{self.diameter:g}"
        return f"{self.width:g}×{self.height:g}"


class Pipe(_Model):
    diameter: float = Field(gt=0, description="管徑 mm")
    invert_el: float = Field(description="管底高程 m")

    @field_validator("invert_el")
    @classmethod
    def _el(cls, v):
        return _check_el(v)


ValveAt = Literal["inflow", "outflow", "overflow"]


class ValveNote(_Model):
    label: str = Field(min_length=1, description="閥件文字，如「浮球閥」「電磁閥」")
    at: ValveAt = "outflow"


# ---------------------------------------------------------------- 模板


class UDitchSection(_DetailBase):
    template: Literal["u_ditch"] = "u_ditch"
    inner_width: float = Field(gt=0, description="內寬 W mm")
    inner_depth: float = Field(gt=0, description="內深 H mm")
    wall_t: float = Field(gt=0, description="壁厚 t mm")
    base_t: float = Field(gt=0, description="底厚 tb mm")
    gravel_t: float = Field(default=100.0, ge=0, description="碎石級配基礎厚 mm（0 = 無）")
    gravel_ext: float = Field(default=100.0, ge=0, description="碎石基礎每側外伸 mm")
    cover: Cover | None = None
    rebar: RebarNote | None = None
    invert_el: float | None = Field(default=None, description="溝底高程 m（選填）")


class RaftPump(_Model):
    """抽水型筏基：集水坑（底板內凹）+ 抽水機 + 抽水出水管（穿頂版，接放流井）。"""

    cell: int = Field(ge=0, description="集水坑所在坑格（0 起算）")
    sump_width: float = Field(gt=0, description="集水坑淨寬 mm")
    sump_depth: float = Field(gt=0, description="集水坑深（自坑底往下）mm")
    start_el: float = Field(description="起抽水位 m")
    stop_el: float = Field(description="停機水位 m")
    discharge_dia: float = Field(default=100.0, gt=0, description="抽水出水管徑 mm")
    q_cms: float | None = Field(default=None, gt=0, description="抽水量 m³/s（僅註記）")
    discharge_to: str = Field(default="放流井", min_length=1, description="出水管接往何處（文字）")

    @field_validator("start_el", "stop_el")
    @classmethod
    def _el(cls, v):
        return _check_el(v)


class RaftDetentionSection(_DetailBase):
    """筏基滯洪坑剖面：n_cells 格，左側地梁進流、右側地梁放流孔口 / 溢流口。

    抽水型（`pump` 有值）：右側坑格底板內凹集水坑、抽水機、出水管穿頂版；`orifice` 可省略。
    `overflow_el` 可省略（溢流設於進流井時），圖面改註記「溢流口設於進流井」。
    """

    template: Literal["raft_detention"] = "raft_detention"
    n_cells: int = Field(default=2, ge=1, le=4)
    cell_width: float = Field(gt=0, description="坑格淨寬 mm（地梁間）")
    beam_width: float = Field(gt=0, description="地梁寬 mm")
    pit_depth: float = Field(gt=0, description="坑深 mm（底板頂 → 頂版底 = 地梁淨高）")
    base_slab_t: float = Field(gt=0, description="筏基底板厚 mm")
    top_slab_t: float = Field(gt=0, description="頂版（地下室樓板）厚 mm")
    gravel_t: float = Field(default=100.0, ge=0, description="底板下碎石 / PC 層厚 mm")
    bottom_el: float = Field(description="坑底（底板頂）高程 m")
    effective_depth: float = Field(gt=0, description="有效水深 mm")
    storage_level_el: float = Field(
        description="設計滿水位（貯留水位）m = 坑底 + 有效水深；須 ≤ 溢流口底（否則未滿即溢流）"
    )
    overflow_el: float | None = Field(
        default=None, description="溢流口（管底）高程 m；None = 池內不設溢流（溢流設於進流井）"
    )
    overflow_flood_el: float | None = Field(
        default=None,
        description="溢流時最高水位 m = 溢流口底 + 溢流堰水頭（Q20 過堰）；須 > 溢流口、≤ 坑頂。"
        "None = 未給（detail_check WARNING）；溢流設於進流井時不畫",
    )
    overflow_dia: float = Field(default=150.0, gt=0, description="溢流管徑 mm（右側地梁）")
    orifice: Orifice | None = Field(
        default=None, description="放流孔口（右側地梁內側孔口板）；抽水型可省略"
    )
    outflow_to_outlet_well: bool = Field(
        default=True, description="放流管標示「接放流井」（detail_check 重力銜接檢查依此）"
    )
    overflow_note: str = Field(
        default="溢流口設於進流井（溢流管重力接放流井）",
        min_length=1,
        description="overflow_el 為 None 時的溢流說明文字",
    )
    pump: RaftPump | None = None
    outflow_pipe_dia: float = Field(default=100.0, gt=0, description="孔口後放流管徑 mm")
    inflow_pipe: Pipe = Field(description="進流管（左側地梁）")
    manhole_cell: int = Field(default=0, ge=0, description="人孔所在坑格（0 起算）")
    manhole_size: float = Field(default=600.0, gt=0, description="人孔淨寬 mm")
    ladder: bool = Field(default=True, description="人孔下設爬梯")
    vent_cell: int | None = Field(default=None, ge=0, description="通氣管所在坑格；None = 不畫")
    vent_diameter: float = Field(default=100.0, gt=0)
    valves: list[ValveNote] = Field(
        default_factory=lambda: [
            ValveNote(label="浮球閥", at="inflow"),
            ValveNote(label="電磁閥", at="outflow"),
        ]
    )
    check_valve_at: ValveAt | None = Field(
        default="outflow", description="逆止閥位置；None = 不設（verifier 會紅燈）"
    )

    @model_validator(mode="before")
    @classmethod
    def _legacy_hwl(cls, data):
        """舊欄位 `design_hwl_el`（語意不明：曾被當成溢流時水位）→ `storage_level_el`。"""
        if isinstance(data, dict) and "design_hwl_el" in data:
            data = dict(data)
            v = data.pop("design_hwl_el")
            if "storage_level_el" in data:
                raise ValueError("design_hwl_el（舊）與 storage_level_el 不可同時給")
            data["storage_level_el"] = v
            warnings.warn(
                f"raft_detention.design_hwl_el 已更名為 storage_level_el（設計滿水位 = 坑底 + 有效水深，"
                f"須 ≤ 溢流口）；已映射 {v}。溢流時最高水位請另填 overflow_flood_el",
                LegacyFieldWarning,
                stacklevel=2,
            )
        return data

    @model_validator(mode="after")
    def _check(self) -> RaftDetentionSection:
        for name in ("manhole_cell", "vent_cell"):
            v = getattr(self, name)
            if v is not None and v >= self.n_cells:
                raise ValueError(f"{name}={v} 超出坑格數 n_cells={self.n_cells}")
        if self.orifice is None and self.pump is None:
            raise ValueError("raft_detention 需 orifice（重力孔口）或 pump（抽水）其中之一")
        if self.pump is not None:
            if self.pump.cell >= self.n_cells:
                raise ValueError(f"pump.cell={self.pump.cell} 超出坑格數 n_cells={self.n_cells}")
            if self.pump.sump_width > self.cell_width:
                raise ValueError("集水坑寬大於坑格淨寬")
            if self.pump.sump_depth > self.base_slab_t - 100.0:
                raise ValueError(
                    f"集水坑深 {self.pump.sump_depth:g} mm 須 ≤ 底板厚 − 100 = "
                    f"{self.base_slab_t - 100.0:g} mm（集水坑做在底板內）"
                )
        return self

    def outflow_invert_el(self) -> float | None:
        """重力放流管底 m（孔口中心 − 放流管徑 / 2）；無孔口 → None。"""
        if self.orifice is None:
            return None
        return round(self.orifice.center_el - self.outflow_pipe_dia / 2000.0, 3)

    def top_el(self) -> float:
        """坑頂（頂版底）高程 m。"""
        return round(self.bottom_el + self.pit_depth / 1000.0, 3)


class Weir(_Model):
    width: float = Field(gt=0, description="溢流堰寬 mm")
    crest_el: float = Field(description="堰頂高程 m")

    @field_validator("crest_el")
    @classmethod
    def _el(cls, v):
        return _check_el(v)


class SideDitch(_Model):
    inner_width: float = Field(gt=0, description="道路側溝內寬 mm")
    wall_t: float = Field(default=120.0, gt=0, description="側溝壁厚 mm")
    bottom_el: float = Field(description="側溝底高程 m")
    top_el: float = Field(description="側溝溝緣（頂）高程 m")
    design_wl_el: float | None = Field(
        default=None, description="受納溝設計水位 m（溝底 + 設計水深；工程師提供）"
    )

    @field_validator("bottom_el", "top_el", "design_wl_el")
    @classmethod
    def _el(cls, v):
        return _check_el(v)


class OutletWellDetail(_DetailBase):
    """放流井平面 + 剖面：A 室（進流）| 隔牆（孔口 + 溢流堰）| B 室 | 外牆 → 放流管 → 道路側溝。

    抽水型（筏基抽水出水管 `pump_inflow` + 進流井溢流管 `overflow_inflow` 直接入井）可不設
    孔口與溢流堰（`orifice` / `weir` 皆 None → 單室，不畫隔牆）。
    """

    template: Literal["outlet_well"] = "outlet_well"
    inner_width: float = Field(gt=0, description="井內寬 mm（平面 y 向）")
    chamber_a_length: float = Field(gt=0, description="A 室（進流側）淨長 mm")
    chamber_b_length: float = Field(gt=0, description="B 室（放流側）淨長 mm")
    wall_t: float = Field(gt=0, description="井壁厚 mm")
    partition_t: float = Field(gt=0, description="隔牆（孔口板 / 溢流堰）厚 mm")
    base_t: float = Field(gt=0, description="底板厚 mm")
    gravel_t: float = Field(default=100.0, ge=0)
    top_el: float = Field(description="井頂高程 m")
    bottom_el: float = Field(description="井底高程 m")
    cover_text: str = Field(default="鑄鐵蓋板", min_length=1)
    inflow_pipe: Pipe | None = None
    pump_inflow: Pipe | None = Field(default=None, description="抽水出水管（左側井壁入井）")
    overflow_inflow: Pipe | None = Field(
        default=None, description="溢流管（自進流井，後側井壁入井；剖面以虛線圓表示）"
    )
    overflow_inflow_from: str = Field(default="進流井", min_length=1)
    orifice: Orifice | None = None
    weir: Weir | None = None
    outlet_pipe: Pipe
    outlet_pipe_end_el: float | None = Field(
        default=None, description="放流管出口（側溝內壁）管底 m；None = 與井端同高"
    )
    outlet_pipe_length: float = Field(default=500.0, gt=0, description="井外壁至側溝內壁 mm")
    ditch: SideDitch
    cleanout_size: float = Field(default=600.0, gt=0, description="清掃孔（B 室上方）淨寬 mm")

    @property
    def single_chamber(self) -> bool:
        """無孔口、無溢流堰 → 單室（不畫隔牆）。"""
        return self.orifice is None and self.weir is None

    def outlet_exit_el(self) -> float:
        return self.outlet_pipe.invert_el if self.outlet_pipe_end_el is None else self.outlet_pipe_end_el


class OrificePlateDetail(_DetailBase):
    template: Literal["orifice_plate"] = "orifice_plate"
    plate_width: float = Field(gt=0)
    plate_height: float = Field(gt=0)
    plate_t: float = Field(gt=0, description="板厚 mm")
    orifice: Orifice
    orifice_center_height: float = Field(gt=0, description="孔心距板底 mm")
    material_text: str = Field(min_length=1, description="材質文字（由使用者指定）")
    fixing_text: str = Field(min_length=1, description="固定方式文字（由使用者指定）")
    bolt_text: str = Field(default="", description="螺栓文字，如「M10 膨脹螺栓 ×4」")
    bolt_hole_dia: float = Field(default=12.0, gt=0)
    bolt_edge: float = Field(default=30.0, gt=0, description="螺栓孔心距板邊 mm（四角）")

    @model_validator(mode="after")
    def _check(self) -> OrificePlateDetail:
        h, w = self.orifice.size_h(), self.orifice.size_w()
        if w >= self.plate_width or not (
            h / 2 < self.orifice_center_height < self.plate_height - h / 2
        ):
            raise ValueError("孔口超出孔口板範圍")
        return self


class OutfallProtectionDetail(_DetailBase):
    """出水口保護工剖面：端牆（含出水管）→ 混凝土護坦（含截水牆、消能檻）→ 拋石。"""

    template: Literal["outfall_protection"] = "outfall_protection"
    pipe: Pipe
    headwall_height: float = Field(gt=0, description="端牆高（自護坦頂）mm")
    headwall_t: float = Field(gt=0)
    apron_el: float = Field(description="護坦頂高程 m")
    apron_length: float = Field(gt=0)
    apron_t: float = Field(gt=0)
    cutoff_depth: float = Field(default=0.0, ge=0, description="護坦末端截水牆深（自護坦底）mm")
    cutoff_t: float = Field(default=200.0, gt=0)
    sill_height: float = Field(default=0.0, ge=0, description="消能檻高 mm（0 = 無）")
    sill_t: float = Field(default=200.0, gt=0)
    riprap_length: float = Field(default=0.0, ge=0, description="拋石段長 mm（0 = 無）")
    riprap_t: float = Field(default=300.0, gt=0)
    riprap_text: str = Field(default="拋石（粒徑依設計）", min_length=1)
    gravel_t: float = Field(default=100.0, ge=0)


class DitchJointDetail(_DetailBase):
    """出流口接既有側溝接頭詳圖：側溝橫斷面 + 放流管穿側溝壁（管口切齊內壁）+ 設計水位。"""

    template: Literal["ditch_joint"] = "ditch_joint"
    pipe: Pipe = Field(description="放流管（出流口端管底）")
    ditch: SideDitch
    base_t: float = Field(default=120.0, gt=0, description="側溝底板厚 mm（示意）")
    gravel_t: float = Field(default=100.0, ge=0)
    ground_el: float | None = Field(default=None, description="出流口處地面 m（選填）")
    seal_text: str = Field(default="管周填封材料依設計", min_length=1)

    @field_validator("ground_el")
    @classmethod
    def _el(cls, v):
        return _check_el(v)


class NameplateDetail(_DetailBase):
    """滯洪設施銘牌 / 告示牌。欄位值由使用者提供，未提供時為佔位字「○○○」。"""

    template: Literal["nameplate"] = "nameplate"
    plate_width: float = Field(gt=0)
    plate_height: float = Field(gt=0)
    material_text: str = Field(default=PLACEHOLDER, min_length=1)
    fixing_text: str = Field(default=PLACEHOLDER, min_length=1)
    heading: str = Field(default="雨水流出抑制設施", min_length=1, description="牌頭標題")
    facility_name: str = Field(default=PLACEHOLDER, min_length=1)
    capacity_m3: float | None = Field(default=None, gt=0, description="容量 m³；None → ○○○")
    maintainer: str = Field(default=PLACEHOLDER, min_length=1, description="維護管理單位")
    phone: str = Field(default=PLACEHOLDER, min_length=1, description="維護單位電話")
    warnings: list[str] = Field(default_factory=list, description="警語（由使用者提供）")

    def capacity_text(self) -> str:
        return PLACEHOLDER if self.capacity_m3 is None else f"{self.capacity_m3:.2f}"

    def rows(self) -> list[tuple[str, str]]:
        return [
            ("設施名稱", self.facility_name),
            ("滯洪容量", f"{self.capacity_text()} m³"),
            ("維護管理單位", self.maintainer),
            ("聯絡電話", self.phone),
        ]


DetailItem = Annotated[
    UDitchSection
    | RaftDetentionSection
    | OutletWellDetail
    | OrificePlateDetail
    | OutfallProtectionDetail
    | DitchJointDetail
    | NameplateDetail,
    Field(discriminator="template"),
]


class DetailSheetSpec(_Model):
    kind: Literal["drainage-detail"] = "drainage-detail"
    units: Literal["mm"] = Field(
        default="mm", description="圖紙 mm（$INSUNITS=4）；尺寸欄位為實尺 mm，_el 欄位為 m"
    )
    title: str = Field(default="排水系統各部詳圖", min_length=1)
    sheet: SheetSpec
    details: list[DetailItem] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> DetailSheetSpec:
        ids = [d.id for d in self.details]
        if len(set(ids)) != len(ids):
            raise ValueError("details[].id 不得重複")
        codes = [d.section_code for d in self.details]
        if len(set(codes)) != len(codes):
            raise ValueError(f"剖面代號不得重複：{codes}")
        return self

    def scale_text(self) -> str:
        """圖簽比例欄：單一比例 → 1/N；多種比例 → 「如圖示」（各詳圖另標 S=1/N）。"""
        scales = sorted({d.scale for d in self.details})
        return "如圖示" if len(scales) > 1 else f"1/{scales[0]}"

    @classmethod
    def from_json(cls, path: str | Path) -> DetailSheetSpec:
        return cls.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))
