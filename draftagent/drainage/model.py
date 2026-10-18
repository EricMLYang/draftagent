"""DrainageNetworkSpec：排水網路（節點 / 管溝 / 集水分區 / 滯洪設施）的 JSON spec。

單位 / 座標
-----------
- `units = "m"`、`crs = "EPSG:3826"`（TWD97 TM2 本島）：所有 x（E）、y（N）為 TWD97 座標原值 m，
  高程 m。平面圖 DXF 的 modelspace 直接使用這些座標（$INSUNITS = 6），圖框在 paperspace mm。
- 尺寸：u_ditch / box 的 w、h，pipe 的 d，一律 **m**（0.30 = 30 cm）。

agent 只填幾何與拓樸，以下一律**不得**出現在 spec（`extra="forbid"`）：
長度、坡度、面積、流量、貯集量。程式由座標、高程、polygon 計算（`drainage/network.py`、
`drainage/derive.py`），verifier 重算比對。

高程（受納 / 滯洪池水位）：`existing_drains[].design_water_depth_m`（受納溝設計水深，工程師提供）、
`storage.drain_mode / bottom_el / max_water_el / top_el / orifice_*_el / overflow_el / overflow_at /
pump_start_el / pump_stop_el / pump_q_cms / allow_backwater + backwater_note`；全部選填（向後相容），
未給者由 `drainage/hydraulics.resolve_storage` 推導並 WARNING，相容性檢查 E1–E8 見該模組。

驗證（schema 層，只看單一欄位與參照；拓樸 / 坡度在 `network.check_network`，讓 verifier 能回報）
- id 唯一；link from / to、subcatchment.outlet_node、storage inlet / outlet 必須是既有節點
- material 必須是 `hydro/codes/taoyuan_runoff.yaml` 曼寧表 key，且 shape 與斷面相符；
  n 為範圍的材料必須給 n_override（範圍內），單值材料不得給
- landuse 必須是 yaml 逕流係數 key；c_override 在範圍內
- polygon 須為有效面（shapely is_valid、面積 > 0）；座標須在 TWD97 TM2 合理範圍
- 比例分母：plan / catchment 不得超過 `hydro/codes/taoyuan_drawing.yaml` 門檻
- codes_path：只能是 `draftagent/*/codes/*.yaml`（verify/codes_lock）；verifier 另要求 = repo 預設並一律以預設重算
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from draftagent.hydro.drawing_codes import load_drawing_codes
from draftagent.schema.drainage import DEFAULT_CODES_PATH, Outlet, OverflowWeir
from draftagent.schema.sheet import SheetSpec
from draftagent.verify.codes_lock import validate_codes_path

PLAN_SCALE_KEY = "drainage_plan"
CATCHMENT_SCALE_KEY = "catchment_map"
PROFILE_SCALE_KEY = "drainage_profile"

# TWD97 TM2（EPSG:3826）本島座標合理範圍（粗篩：抓經緯度 / 區域座標誤填，非精確界線）
TM2_E_RANGE = (140_000.0, 360_000.0)
TM2_N_RANGE = (2_400_000.0, 2_820_000.0)

NodeKind = Literal[
    "catch_basin", "manhole", "outlet_well", "storage_inlet", "outfall", "existing_mh"
]
NODE_KIND_NAME: dict[str, str] = {
    "catch_basin": "集水井",
    "manhole": "人孔",
    "outlet_well": "放流井",
    "storage_inlet": "進流井",
    "outfall": "出流口",
    "existing_mh": "既有人孔",
}

Point = tuple[float, float]
DrainMode = Literal["gravity", "gravity_pump", "pump"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# ---------------------------------------------------------------- 斷面


class UDitch(_Model):
    type: Literal["u_ditch"] = "u_ditch"
    w: float = Field(gt=0, description="溝寬 m")
    h: float = Field(gt=0, description="溝深 m")

    @property
    def shape(self) -> str:
        return "rectangular"

    @property
    def size_m(self) -> float:
        return self.h

    def text(self) -> str:
        return f"U溝W{_cm(self.w)}×H{_cm(self.h)}"

    def dims_text(self) -> str:
        return f"W{_cm(self.w)}×H{_cm(self.h)} cm"


class PipeSection(_Model):
    type: Literal["pipe"] = "pipe"
    d: float = Field(gt=0, description="管內徑 m")

    @property
    def shape(self) -> str:
        return "circular"

    @property
    def size_m(self) -> float:
        return self.d

    def text(self) -> str:
        return f"管D{self.d * 1000:.0f}"

    def dims_text(self) -> str:
        return f"D{self.d * 1000:.0f} mm"


class BoxSection(_Model):
    type: Literal["box"] = "box"
    w: float = Field(gt=0, description="箱涵寬 m")
    h: float = Field(gt=0, description="箱涵高 m")

    @property
    def shape(self) -> str:
        return "rectangular"

    @property
    def size_m(self) -> float:
        return self.h

    def text(self) -> str:
        return f"箱涵W{_cm(self.w)}×H{_cm(self.h)}"

    def dims_text(self) -> str:
        return f"W{_cm(self.w)}×H{_cm(self.h)} cm"


Section = Annotated[UDitch | PipeSection | BoxSection, Field(discriminator="type")]


def _cm(v: float) -> str:
    return f"{v * 100:g}"


# ---------------------------------------------------------------- 網路元素


class Node(_Model):
    id: str = Field(min_length=1, pattern=r"^\S+$")
    kind: NodeKind
    x: float = Field(description="TWD97 E m")
    y: float = Field(description="TWD97 N m")
    ground_el: float = Field(description="地面（井頂）高程 m")
    invert_el: float = Field(description="井底 / 溝底（管底）高程 m")
    top_el: float | None = Field(
        default=None, description="渠頂高程 m；None → 圖面「頂 EL」採 ground_el（井頂 = 地面）"
    )

    @model_validator(mode="after")
    def _check(self) -> Node:
        if self.invert_el >= self.ground_el:
            raise ValueError(f"node {self.id}: invert_el 必須低於 ground_el")
        if self.top_el is not None and self.top_el <= self.invert_el:
            raise ValueError(f"node {self.id}: top_el 必須高於 invert_el")
        return self

    @property
    def top(self) -> float:
        """圖面「頂 EL」與 TOP 屬性：top_el，未給則 ground_el。"""
        return self.top_el if self.top_el is not None else self.ground_el


class Link(_Model):
    id: str = Field(min_length=1, pattern=r"^\S+$")
    from_: str = Field(alias="from", description="上游節點 id")
    to: str = Field(description="下游節點 id")
    section: Section
    material: str = Field(description="yaml manning_n.materials 的 key")
    n_override: float | None = Field(default=None, gt=0)
    existing: bool = Field(default=False, description="既有設施（圖面虛線、不計入數量表）")
    pump: bool = Field(default=False, description="抽水段：允許逆坡")


class Subcatchment(_Model):
    id: str = Field(min_length=1, pattern=r"^\S+$")
    polygon: list[Point] = Field(min_length=3)
    landuse: str = Field(description="yaml runoff_coefficients.classes 的 key")
    c_override: float | None = Field(default=None, description="須在該 landuse 範圍內")
    outlet_node: str


class Storage(_Model):
    id: str = Field(min_length=1, pattern=r"^\S+$")
    type: Literal["raft", "surface"] = "raft"
    polygon: list[Point] = Field(min_length=3, description="滯洪設施外框（圖面 hatch 範圍）")
    cells: list[list[Point]] | None = Field(
        default=None,
        description="貯集格（筏基坑）矩形 polygon；None → polygon 本身須為矩形（單格）",
    )
    effective_depth: float = Field(gt=0, description="有效貯集水深 m（工程師決定）")
    inlet_node: str
    outlet_node: str
    outlet_well_polygon: list[Point] | None = Field(
        default=None,
        description="位於貯集槽內的出流井 / 抽水集水坑範圍；面積由程式算並自貯集量扣除",
    )

    # ---- 高程（工程師決定；全部 m）。未給者由 drainage/hydraulics.resolve_storage 推導並 WARNING
    drain_mode: DrainMode = Field(
        default="gravity",
        description="gravity：孔口重力排放；gravity_pump：上層重力孔口 + 下層抽水；pump：全抽水",
    )
    bottom_el: float | None = Field(default=None, description="有效貯集底（池底）高程")
    max_water_el: float | None = Field(
        default=None, description="設計滿水位（貯留水位）；= bottom_el + effective_depth（兩者都給須一致），須 ≤ overflow_el"
    )
    top_el: float | None = Field(
        default=None, description="池頂（筏基坑頂版底 / 地面池池緣）高程；None → inlet_node 地面"
    )
    orifice_invert_el: float | None = Field(default=None, description="孔口底高程（與 center 擇一）")
    orifice_center_el: float | None = Field(
        default=None, description="孔口中心高程；孔口底 = 中心 − 孔口高 / 2"
    )
    overflow_el: float | None = Field(default=None, description="溢流口（堰頂）底高程")
    overflow_at: Literal["storage", "inlet_well"] = Field(
        default="storage",
        description="溢流位置：storage（池內溢流至放流井）/ inlet_well（進流井溢流管直通放流井）",
    )
    pump_start_el: float | None = Field(default=None, description="起抽水位")
    pump_stop_el: float | None = Field(default=None, description="停機水位")
    pump_q_cms: float | None = Field(default=None, gt=0, description="抽水機設計抽水量 m³/s")
    inflow_invert_el: float | None = Field(
        default=None,
        description="進流管入池處（池壁）管底高程；None → 進流井底（hydraulics 推導並 WARNING）",
    )
    pump_outlet_invert_el: float | None = Field(
        default=None, description="pump 型：抽水出水管入放流井管底；None → 放流井底（WARNING）"
    )
    overflow_pipe_invert_el: float | None = Field(
        default=None,
        description="overflow_at=inlet_well：溢流管入放流井管底；None → 放流井底（WARNING）",
    )
    raft_cells: int | None = Field(
        default=None, ge=1, le=4, description="詳圖剖面方向坑格數；None → 由 cells 的欄數推導"
    )
    beam_width: float | None = Field(
        default=None, gt=0, description="地梁寬 m（詳圖用）；None → detail_defaults 示意值"
    )
    allow_backwater: bool = Field(
        default=False, description="工程師說明集水管網可承受滯洪池滿水迴水（需 backwater_note）"
    )
    backwater_note: str | None = Field(default=None, description="可承受迴水的理由（工程師提供）")

    @model_validator(mode="after")
    def _levels(self) -> Storage:
        if self.orifice_invert_el is not None and self.orifice_center_el is not None:
            raise ValueError(f"storage {self.id}: orifice_invert_el 與 orifice_center_el 擇一")
        if self.drain_mode in ("pump", "gravity_pump") and (
            self.pump_start_el is None or self.pump_stop_el is None
        ):
            raise ValueError(
                f"storage {self.id}: drain_mode={self.drain_mode} 需 pump_start_el 與 pump_stop_el"
                "（起抽 / 停機水位，檢核表必列）"
            )
        if self.drain_mode == "gravity" and (
            self.pump_start_el is not None
            or self.pump_stop_el is not None
            or self.pump_q_cms
            or self.pump_outlet_invert_el is not None
        ):
            raise ValueError(f"storage {self.id}: drain_mode=gravity 不得給抽水機欄位")
        if self.allow_backwater and not (self.backwater_note or "").strip():
            raise ValueError(f"storage {self.id}: allow_backwater=true 必須以 backwater_note 說明理由")
        return self


class ExistingDrainPoint(_Model):
    x: float
    y: float
    top_el: float | None = None
    invert_el: float | None = None


class ExistingDrain(_Model):
    """基地附近既有排水（道路側溝等）。點依**流向**排序（第一點最上游）。"""

    id: str = Field(min_length=1, pattern=r"^\S+$")
    name: str = Field(min_length=1, description="如「既有道路側溝」")
    points: list[ExistingDrainPoint] = Field(min_length=2)
    section: Section
    design_water_depth_m: float | None = Field(
        default=None,
        gt=0,
        description="受納溝設計水深 m（自溝底起算；工程師提供）。None → 只能做溝底檢查並 WARNING",
    )

    @model_validator(mode="after")
    def _levels(self) -> ExistingDrain:
        n = sum(1 for p in self.points if p.top_el is not None and p.invert_el is not None)
        if n < 2:
            raise ValueError(
                f"existing_drain {self.id}: 至少 2 點需同時給 top_el 與 invert_el"
                "（審查：每直線段 ≥ 2 點渠頂 / 渠底標高）"
            )
        if self.design_water_depth_m is not None and self.design_water_depth_m > self.section.size_m:
            raise ValueError(
                f"existing_drain {self.id}: design_water_depth_m {self.design_water_depth_m} "
                f"> 溝深 / 管徑 {self.section.size_m}"
            )
        return self


class RoadPoint(_Model):
    x: float
    y: float
    el: float = Field(description="道路中心樁高程 m（測量值）")


class CalcParams(_Model):
    """流出抑制計算由工程師決定的參數（見 schema/drainage.py 同名欄位）。"""

    station: str | None = None
    tc_min: float | None = Field(default=None, gt=0)
    outlet: Outlet | None = Field(
        default=None, description="孔口；storage.drain_mode=pump 時可省略（排放量 = pump_q_cms）"
    )
    overflow_weir: OverflowWeir | None = None


class Scales(_Model):
    plan: int = Field(gt=0, description="排水系統平面設計圖比例分母")
    catchment: int = Field(gt=0, description="排水系統集水區分析圖比例分母")
    profile_h: int = Field(default=200, gt=0, description="縱斷面水平比例分母")
    profile_v: int = Field(default=50, gt=0, description="縱斷面垂直比例分母")
    profile_outlet_h: int | None = Field(
        default=None, gt=0, description="出水側縱斷面水平比例分母；None → profile_h"
    )
    profile_outlet_v: int | None = Field(
        default=None, gt=0, description="出水側縱斷面垂直比例分母；None → profile_v"
    )


class DrawingNos(_Model):
    plan: str = Field(min_length=1)
    catchment: str = Field(min_length=1)
    profile: str = Field(min_length=1)
    profile_outlet: str | None = Field(default=None, description="出水側縱斷面；None → 預設編號")
    detail: str | None = Field(default=None, description="排水系統各部詳圖；None → 預設編號")


# ---------------------------------------------------------------- 詳圖構造參數
#
# 以下全部是**示意值，工程師確認**：構造尺寸（壁厚、版厚、蓋板、配筋文字…）不是法規數值，
# 也不影響水理計算；詳圖上的高程與容量一律由模型 / 計算結果帶入（drainage/derive.derive_details），
# 這裡只放「畫得出來」需要、但模型沒有的構造尺寸。長度單位 mm（與 DetailSheetSpec 一致）。
SCHEMATIC_NOTE = "示意值，工程師確認"


class UDitchDefaults(_Model):
    wall_t: float = Field(default=120.0, gt=0, description=f"U 溝壁厚 mm（{SCHEMATIC_NOTE}）")
    base_t: float = Field(default=120.0, gt=0, description=f"底厚 mm（{SCHEMATIC_NOTE}）")
    gravel_t: float = Field(default=100.0, ge=0, description=f"碎石基礎 mm（{SCHEMATIC_NOTE}）")
    cover_t: float | None = Field(default=100.0, gt=0, description=f"蓋板厚 mm；None = 無蓋（{SCHEMATIC_NOTE}）")
    cover_text: str = Field(default="預鑄 RC 蓋板", min_length=1)
    rebar_bar: str | None = Field(default="#3", description=f"鋼筋示意文字（{SCHEMATIC_NOTE}）")
    rebar_spacing: str = Field(default="@200", min_length=1)
    scale: int = Field(default=10, gt=0)


class RaftDefaults(_Model):
    beam_width: float = Field(default=400.0, gt=0, description=f"地梁寬 mm（storage.beam_width 未給時；{SCHEMATIC_NOTE}）")
    base_slab_t: float = Field(default=600.0, gt=0, description=f"筏基底板厚 mm（{SCHEMATIC_NOTE}）")
    top_slab_t: float = Field(default=200.0, gt=0, description=f"頂版厚 mm（{SCHEMATIC_NOTE}）")
    gravel_t: float = Field(default=100.0, ge=0)
    manhole_size: float = Field(default=600.0, gt=0)
    ladder: bool = True
    vent: bool = True
    overflow_dia: float = Field(default=150.0, gt=0, description=f"池內溢流管徑 mm（{SCHEMATIC_NOTE}）")
    outflow_pipe_dia: float = Field(default=100.0, gt=0, description=f"孔口後放流管徑 mm（{SCHEMATIC_NOTE}）")
    sump_depth: float = Field(default=400.0, gt=0, description=f"抽水集水坑深 mm（{SCHEMATIC_NOTE}）")
    pump_discharge_dia: float = Field(default=100.0, gt=0, description=f"抽水出水管徑 mm（{SCHEMATIC_NOTE}）")
    scale: int = Field(default=50, gt=0)


class OutletWellDefaults(_Model):
    inner_width: float = Field(default=800.0, gt=0, description=f"井內寬 mm（{SCHEMATIC_NOTE}）")
    chamber_a_length: float = Field(default=800.0, gt=0)
    chamber_b_length: float = Field(default=800.0, gt=0)
    wall_t: float = Field(default=150.0, gt=0)
    partition_t: float = Field(default=150.0, gt=0)
    base_t: float = Field(default=150.0, gt=0)
    gravel_t: float = Field(default=100.0, ge=0)
    cleanout_size: float = Field(default=600.0, gt=0)
    cover_text: str = Field(default="鑄鐵蓋板", min_length=1)
    overflow_pipe_dia: float = Field(default=200.0, gt=0, description=f"進流井溢流管徑 mm（{SCHEMATIC_NOTE}）")
    ditch_wall_t: float = Field(default=120.0, gt=0, description=f"既有側溝壁厚 mm（{SCHEMATIC_NOTE}）")
    scale: int = Field(default=30, gt=0)


class OutfallDefaults(_Model):
    detail: Literal["auto", "ditch_joint", "protection", "none"] = Field(
        default="auto",
        description="auto：出流口 2 m 內有既有排水 → ditch_joint（接既有側溝接頭），否則 protection（保護工）",
    )
    seal_text: str = Field(default="管周填封材料依設計", min_length=1)
    headwall_height: float = Field(default=900.0, gt=0)
    headwall_t: float = Field(default=200.0, gt=0)
    apron_length: float = Field(default=1500.0, gt=0)
    apron_t: float = Field(default=200.0, gt=0)
    apron_drop: float = Field(default=100.0, ge=0, description="護坦頂低於出水管底 mm（示意）")
    cutoff_depth: float = Field(default=600.0, ge=0)
    riprap_length: float = Field(default=1500.0, ge=0)
    scale: int = Field(default=20, gt=0)


class NameplateDefaults(_Model):
    plate_width: float = Field(default=600.0, gt=0)
    plate_height: float = Field(default=400.0, gt=0)
    maintainer: str = Field(default="○○○", min_length=1, description="維護管理單位（使用者提供）")
    phone: str = Field(default="○○○", min_length=1, description="聯絡電話（使用者提供）")
    material_text: str = Field(default="○○○", min_length=1)
    fixing_text: str = Field(default="○○○", min_length=1)
    warnings: list[str] = Field(default_factory=list, description="警語（使用者提供）")
    scale: int = Field(default=10, gt=0)


class OrificePlateDefaults(_Model):
    """孔口板詳圖（calc.outlet 為孔口時衍生）；孔徑 / 孔口尺寸與孔心 EL 由 calc.outlet 與高程帶入。"""

    margin: float = Field(default=150.0, gt=0, description=f"孔口邊緣至板邊 mm（{SCHEMATIC_NOTE}）")
    plate_t: float = Field(default=6.0, gt=0, description=f"板厚 mm（{SCHEMATIC_NOTE}）")
    material_text: str = Field(default="不銹鋼板（材質由工程師指定）", min_length=1)
    fixing_text: str = Field(default="膨脹螺栓固定於井壁（工程師確認）", min_length=1)
    bolt_text: str = Field(default="M10 膨脹螺栓 ×4", description=f"{SCHEMATIC_NOTE}")
    scale: int = Field(default=10, gt=0)


class DetailDefaults(_Model):
    """衍生詳圖用的構造參數（全部為示意值，工程師確認）。"""

    sheet_size: Literal["A3", "A1"] = "A1"
    u_ditch: UDitchDefaults = Field(default_factory=UDitchDefaults)
    raft: RaftDefaults = Field(default_factory=RaftDefaults)
    outlet_well: OutletWellDefaults = Field(default_factory=OutletWellDefaults)
    outfall: OutfallDefaults = Field(default_factory=OutfallDefaults)
    nameplate: NameplateDefaults = Field(default_factory=NameplateDefaults)
    orifice_plate: OrificePlateDefaults = Field(default_factory=OrificePlateDefaults)


class ProjectInfo(_Model):
    name: str = Field(min_length=1, description="工程名稱（使用者提供）")
    district: str = Field(min_length=1, description="行政區，例「桃園區」；雨量站由 yaml 決定")
    location: str = Field(default="", description="基地位置描述（使用者提供）")


# ---------------------------------------------------------------- spec


class DrainageNetworkSpec(_Model):
    kind: Literal["drainage-plan"] = "drainage-plan"
    units: Literal["m"] = "m"
    crs: Literal["EPSG:3826"] = "EPSG:3826"
    title: str = Field(min_length=1)
    project: ProjectInfo
    site_boundary: list[Point] = Field(min_length=3)
    building_footprints: list[list[Point]] = Field(default_factory=list)
    nodes: list[Node] = Field(min_length=2)
    links: list[Link] = Field(min_length=1)
    subcatchments: list[Subcatchment] = Field(min_length=1)
    storage: Storage | None = None
    existing_drains: list[ExistingDrain] = Field(default_factory=list)
    road_centerline_els: list[RoadPoint] = Field(
        default_factory=list, description="道路中心樁（依里程排序）"
    )
    profile_path: list[str] | None = Field(
        default=None, description="縱斷面路徑節點 id（上游→下游）；None → 最長的連續管溝路徑"
    )
    calc: CalcParams | None = None
    scales: Scales
    sheet: SheetSpec
    drawing_nos: DrawingNos | None = Field(
        default=None, description="None → sheet.title_block.drawing_no 加 -1 / -2 / -3 / -4 / -5"
    )
    detail_defaults: DetailDefaults = Field(
        default_factory=DetailDefaults, description="衍生詳圖構造參數（示意值，工程師確認）"
    )
    codes_path: Path = Field(
        default=DEFAULT_CODES_PATH,
        description="taoyuan_runoff.yaml；只能是 draftagent/*/codes/ 下的檔（verifier 另要求 = repo 預設）",
    )

    @field_validator("codes_path")
    @classmethod
    def _codes_in_repo(cls, v):
        return validate_codes_path(v)

    @model_validator(mode="after")
    def _check(self) -> DrainageNetworkSpec:
        from shapely.geometry import Polygon

        from draftagent.hydro.taoyuan import load_codes

        for kind, items in (
            ("node", self.nodes),
            ("link", self.links),
            ("subcatchment", self.subcatchments),
            ("existing_drain", self.existing_drains),
        ):
            ids = [i.id for i in items]
            dup = sorted({i for i in ids if ids.count(i) > 1})
            if dup:
                raise ValueError(f"{kind} id 重複：{dup}")
        nodes = {n.id for n in self.nodes}

        pts: list[Point] = [(n.x, n.y) for n in self.nodes] + list(self.site_boundary)
        for e, n in pts:
            if not (
                TM2_E_RANGE[0] <= e <= TM2_E_RANGE[1] and TM2_N_RANGE[0] <= n <= TM2_N_RANGE[1]
            ):
                raise ValueError(
                    f"座標 ({e}, {n}) 不在 TWD97 TM2（EPSG:3826）合理範圍 E{TM2_E_RANGE} N{TM2_N_RANGE}；"
                    "是否誤填經緯度、TWD67 或區域座標？"
                )

        def _poly(name: str, coords) -> Polygon:
            p = Polygon(coords)
            if not p.is_valid or p.area <= 0:
                raise ValueError(f"{name} polygon 無效或面積為 0")
            return p

        _poly("site_boundary", self.site_boundary)
        for i, b in enumerate(self.building_footprints):
            _poly(f"building_footprints[{i}]", b)

        codes = load_codes(self.codes_path)
        mats = codes["manning_n"]["materials"]
        for lk in [*self.links]:
            for end in (lk.from_, lk.to):
                if end not in nodes:
                    raise ValueError(f"link {lk.id}: 節點 {end!r} 不存在")
            if lk.from_ == lk.to:
                raise ValueError(f"link {lk.id}: from 與 to 相同")
            _check_material(f"link {lk.id}", lk.material, lk.n_override, lk.section, mats)

        cls = codes["runoff_coefficients"]["classes"]
        for s in self.subcatchments:
            _poly(f"subcatchment {s.id}", s.polygon)
            if s.outlet_node not in nodes:
                raise ValueError(f"subcatchment {s.id}: outlet_node {s.outlet_node!r} 不存在")
            if s.landuse not in cls:
                raise ValueError(
                    f"subcatchment {s.id}: landuse {s.landuse!r} 不在 yaml {sorted(cls)}"
                )
            c = cls[s.landuse]
            if s.c_override is not None and not c["min"] <= s.c_override <= c["max"]:
                raise ValueError(
                    f"subcatchment {s.id}: c_override {s.c_override} 超出 [{c['min']}, {c['max']}]"
                )

        if self.storage is not None:
            st = self.storage
            sp = _poly(f"storage {st.id}", st.polygon)
            for end in (st.inlet_node, st.outlet_node):
                if end not in nodes:
                    raise ValueError(f"storage {st.id}: 節點 {end!r} 不存在")
            if st.inlet_node == st.outlet_node:
                raise ValueError(f"storage {st.id}: inlet_node 與 outlet_node 相同")
            for i, c in enumerate(st.cells or []):
                cp = _poly(f"storage {st.id} cells[{i}]", c)
                if cp.difference(sp.buffer(1e-6)).area > 1e-6:
                    raise ValueError(f"storage {st.id} cells[{i}] 超出 storage polygon")
            if st.outlet_well_polygon is not None:
                _poly(f"storage {st.id} outlet_well_polygon", st.outlet_well_polygon)
        if self.calc is not None:
            mode = self.storage.drain_mode if self.storage is not None else "gravity"
            if mode != "pump" and self.calc.outlet is None:
                raise ValueError(f"calc.outlet（孔口）必填（drain_mode={mode}）")
            if mode == "pump" and self.storage.pump_q_cms is None:
                raise ValueError("drain_mode=pump 的流出抑制計算需 storage.pump_q_cms（抽水排放量）")

        if self.profile_path is not None:
            bad = [p for p in self.profile_path if p not in nodes]
            if bad or len(self.profile_path) < 2:
                raise ValueError(f"profile_path 需 ≥ 2 個既有節點；不存在：{bad}")

        dc = load_drawing_codes()
        for key, denom in (
            (PLAN_SCALE_KEY, self.scales.plan),
            (CATCHMENT_SCALE_KEY, self.scales.catchment),
            (PROFILE_SCALE_KEY, self.scales.profile_h),
            (PROFILE_SCALE_KEY, self.scales.profile_outlet_h or self.scales.profile_h),
        ):
            rule = dc.scale(key)
            if denom > rule.max_denominator:
                raise ValueError(
                    f"{rule.name} 比例 1/{denom} 小於 S≧1/{rule.max_denominator}（{rule.source}）"
                )
        return self

    # ------------------------------------------------------------ helpers

    def node(self, node_id: str) -> Node:
        for n in self.nodes:
            if n.id == node_id:
                return n
        raise KeyError(node_id)

    def drawing_no(
        self, which: Literal["plan", "catchment", "profile", "profile_outlet", "detail"]
    ) -> str:
        if self.drawing_nos is not None and getattr(self.drawing_nos, which) is not None:
            return getattr(self.drawing_nos, which)
        suffix = {"plan": "1", "catchment": "2", "profile": "3", "profile_outlet": "4", "detail": "5"}
        return f"{self.sheet.title_block.drawing_no}-{suffix[which]}"

    @classmethod
    def from_json(cls, path: str | Path) -> DrainageNetworkSpec:
        return cls.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


def _check_material(where: str, material: str, n_override, section, mats) -> None:
    if material not in mats:
        raise ValueError(f"{where}: material {material!r} 不在 yaml {sorted(mats)}")
    m = mats[material]
    if m["shape"] != section.shape:
        raise ValueError(
            f"{where}: 材料 {material} 為 {m['shape']}，斷面 {section.type} 為 {section.shape}"
        )
    if "n_range" in m:
        lo, hi = m["n_range"]
        if n_override is None or not lo <= n_override <= hi:
            raise ValueError(f"{where}: {material} 的 n 為範圍 [{lo}, {hi}]，須以 n_override 指定")
    elif n_override is not None:
        raise ValueError(f"{where}: {material} 的 n 由 yaml 決定，不得 n_override")
