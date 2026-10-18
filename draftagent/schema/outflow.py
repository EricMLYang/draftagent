"""OutflowControlSpec：出流管制計畫書水文水理計算的輸入規格。

Agent 只描述幾何與工程師選定 / 提供的參數；不填任何計算結果與法規數值。
法規 / 手冊數值由 `draftagent/hydro/codes/outflow_control.yaml` 提供。

必須由工程師 / 使用者提供（程式不代選）
- horner：雨量站名、各重現期 a、b、c 與來源說明 source_note（治理規劃報告或鄰近站分析）
- 開發前後集水分區 CN（或 SCS 分類 + 土壤群）、Tc 或流路參數
- 滯洪池分層高程 / 面積、出水高、出流工型式與尺寸、外水位條件
- site.gravity_drainage / outfall_q10_capacity_ok（簡易檢核適用條件）

高程（池底、孔口、堰頂、外水位）一律同一高程系統，單位 m。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from draftagent.verify.codes_lock import validate_codes_path

DEFAULT_CODES_PATH = (
    Path(__file__).resolve().parent.parent / "hydro" / "codes" / "outflow_control.yaml"
)


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Site(_Base):
    area_ha: float = Field(gt=0, description="土地開發利用面積 ha")
    in_urban_plan: bool = Field(description="基地是否全部位於都市計畫區內")
    is_new_building: bool = Field(description="是否屬建築法第 9 條第 1 款前段新建造建築物")
    gravity_drainage: bool | None = Field(default=None, description="排水出流是否為重力排水")
    outfall_q10_capacity_ok: bool | None = Field(
        default=None, description="聯外排水路通洪能力是否達 10 年重現期距洪峰流量（工程師判定）"
    )
    use_simple_check: bool = Field(
        default=False, description="是否以第十四點簡易檢核為檢核基準（須符合適用條件）"
    )


class HornerParams(_Base):
    a: float = Field(gt=0)
    b: float = Field(ge=0)
    c: float = Field(gt=0)


class Horner(_Base):
    station: str = Field(min_length=1, description="雨量站名（工程師指定）")
    source_note: str = Field(min_length=1, description="係數來源（報告名稱 / 年份 / 頁次）")
    params: dict[int, HornerParams] = Field(description="{重現期(年): {a, b, c}}")


class CNSubarea(_Base):
    name: str
    area_ha: float = Field(gt=0)
    cn: float | None = Field(default=None, gt=0, le=100, description="直接指定 CN")
    scs_class: int | None = Field(default=None, description="yaml cn_table.classes 的分類編號")
    soil_group: Literal["A", "B", "C", "D"] | None = None

    @model_validator(mode="after")
    def _one(self) -> CNSubarea:
        by_table = self.scs_class is not None or self.soil_group is not None
        if self.cn is not None and by_table:
            raise ValueError("cn 與 scs_class/soil_group 擇一")
        if self.cn is None and (self.scs_class is None or self.soil_group is None):
            raise ValueError("未給 cn 時需同時給 scs_class 與 soil_group")
        return self


class ChannelReach(_Base):
    length_m: float = Field(gt=0)
    velocity_m_s: float | None = Field(default=None, gt=0)
    n: float | None = Field(default=None, gt=0)
    R_m: float | None = Field(default=None, gt=0)
    S: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _v(self) -> ChannelReach:
        if self.velocity_m_s is None and None in (self.n, self.R_m, self.S):
            raise ValueError("渠段需給 velocity_m_s，或 n、R_m、S 以曼寧公式計算")
        return self


class FlowPath(_Base):
    inlet_time_min: float | None = Field(
        default=None, gt=0, description="流入時間 T1（側溝 5–10 / 下水道 10–15 min，工程師取值）"
    )
    overland_length_m: float | None = Field(default=None, gt=0)
    overland_slope_pct: float | None = Field(default=None, gt=0)
    channels: list[ChannelReach] = Field(default_factory=list)

    @model_validator(mode="after")
    def _t1(self) -> FlowPath:
        overland = self.overland_length_m is not None or self.overland_slope_pct is not None
        if self.inlet_time_min is not None and overland:
            raise ValueError("inlet_time_min 與漫地流參數擇一")
        if self.inlet_time_min is None and (
            self.overland_length_m is None or self.overland_slope_pct is None
        ):
            raise ValueError("需給 inlet_time_min，或 overland_length_m + overland_slope_pct")
        return self


class Catchment(_Base):
    subareas: list[CNSubarea] = Field(min_length=1)
    tc_min: float | None = Field(default=None, gt=0, description="直接指定集流時間 min")
    flow_path: FlowPath | None = None

    @model_validator(mode="after")
    def _tc(self) -> Catchment:
        if (self.tc_min is None) == (self.flow_path is None):
            raise ValueError("tc_min 與 flow_path 擇一")
        return self

    @property
    def area_ha(self) -> float:
        return sum(s.area_ha for s in self.subareas)


class Layer(_Base):
    elev_m: float
    area_m2: float = Field(ge=0)


class Detention(_Base):
    layers: list[Layer] = Field(min_length=2, description="由低至高之分層高程與水面積（H-A）")
    freeboard_m: float | None = Field(default=None, ge=0, description="出水高（工程師給值）")
    initial_elev_m: float | None = Field(default=None, description="起始水位，省略為池底")

    @model_validator(mode="after")
    def _mono(self) -> Detention:
        e = [lv.elev_m for lv in self.layers]
        if any(b <= a for a, b in zip(e, e[1:], strict=False)):
            raise ValueError("layers.elev_m 必須嚴格遞增")
        if self.initial_elev_m is not None and not e[0] <= self.initial_elev_m <= e[-1]:
            raise ValueError("initial_elev_m 需在池底與池頂之間")
        if self.freeboard_m is not None and self.freeboard_m >= e[-1] - e[0]:
            raise ValueError("freeboard_m 不得 ≥ 池深")
        return self


class Orifice(_Base):
    name: str
    type: Literal["vertical_circular", "vertical_rect", "horizontal_circular", "horizontal_rect"]
    invert_m: float = Field(description="孔口底（水平孔口為下緣）高程 m")
    diameter_m: float | None = Field(default=None, gt=0)
    width_m: float | None = Field(default=None, gt=0)
    height_m: float | None = Field(default=None, gt=0)
    cd: float | None = Field(default=None, gt=0, le=1, description="省略用 yaml 0.61")
    clog_ratio: float = Field(default=0.0, ge=0, lt=1, description="堵塞比例 clog")

    @model_validator(mode="after")
    def _dims(self) -> Orifice:
        if self.type.endswith("circular"):
            if self.diameter_m is None or self.width_m is not None or self.height_m is not None:
                raise ValueError(f"{self.type} 需 diameter_m，且不得給 width_m / height_m")
        elif self.width_m is None or self.height_m is None or self.diameter_m is not None:
            raise ValueError(f"{self.type} 需 width_m 與 height_m，且不得給 diameter_m")
        return self


class Weir(_Base):
    name: str
    crest_m: float = Field(description="堰頂高程 m")
    length_m: float = Field(gt=0, description="堰長 L（垂直流向）")
    cd: float | None = Field(default=None, gt=0, le=1)
    crest_width_m: float | None = Field(default=None, gt=0, description="堰頂寬 BW（順流向）")
    weir_height_m: float | None = Field(default=None, gt=0, description="堰高 P1（銳緣堰）")

    @model_validator(mode="after")
    def _cd(self) -> Weir:
        if self.cd is None and self.crest_width_m is None and self.weir_height_m is None:
            raise ValueError("堰需給 cd，或 crest_width_m / weir_height_m 以手冊式 1-9～1-12 計算")
        return self


class Outlet(_Base):
    orifices: list[Orifice] = Field(default_factory=list)
    weirs: list[Weir] = Field(default_factory=list)

    @model_validator(mode="after")
    def _any(self) -> Outlet:
        if not self.orifices and not self.weirs:
            raise ValueError("出流工至少需一個孔口或堰")
        return self


class Tailwater(_Base):
    mode: Literal["free", "fixed", "hydrograph"] = "free"
    elev_m: float | None = Field(default=None, description="fixed：固定外水位")
    Hp_m: dict[int, float] | None = Field(default=None, description="hydrograph：各重現期洪水位 Hp")
    Hb_m: float | None = Field(default=None, description="hydrograph：渠底高程 Hb")

    @model_validator(mode="after")
    def _m(self) -> Tailwater:
        if self.mode == "fixed" and (self.elev_m is None or self.Hp_m or self.Hb_m is not None):
            raise ValueError("fixed 需 elev_m，且不得給 Hp_m / Hb_m")
        if self.mode == "hydrograph":
            if self.Hp_m is None or self.Hb_m is None or self.elev_m is not None:
                raise ValueError("hydrograph 需 Hp_m 與 Hb_m，且不得給 elev_m")
            if any(h < self.Hb_m for h in self.Hp_m.values()):
                raise ValueError("Hp 不得低於 Hb")
        if self.mode == "free" and (
            self.elev_m is not None or self.Hp_m is not None or self.Hb_m is not None
        ):
            raise ValueError("free 不得給高程")
        return self


class Routing(_Base):
    substeps: int = Field(default=10, ge=1, le=600, description="每 10 min 細分子步數")
    tail_hr: float = Field(default=6.0, ge=0, description="雨後延長演算時間 hr")


class OutflowControlSpec(_Base):
    kind: Literal["outflow_control"] = "outflow_control"
    title: str | None = None
    site: Site
    horner: Horner
    pre: Catchment
    post: Catchment
    detention: Detention
    outlet: Outlet
    tailwater: Tailwater = Field(default_factory=Tailwater)
    safety_factor: float | None = Field(
        default=None, gt=0, description="省略讀 yaml detention.safety_factor_min；主管機關可提高"
    )
    return_periods: list[int] | None = Field(default=None, description="省略讀 yaml（2/5/10）")
    routing: Routing = Field(default_factory=Routing)
    codes_path: str = Field(
        default=str(DEFAULT_CODES_PATH),
        description="法規 / 手冊數值 yaml；只能是 draftagent/*/codes/*.yaml（verifier 只接受 repo 預設）",
    )
    area_change_reason: str | None = Field(
        default=None,
        min_length=1,
        description="開發前後集水面積差 > 0.5% 時必填：說明原因（區外集水、分區調整…，工程師確認）",
    )

    @field_validator("codes_path")
    @classmethod
    def _codes_in_repo(cls, v: str) -> str:
        return validate_codes_path(v)

    @model_validator(mode="after")
    def _cross(self) -> OutflowControlSpec:
        rps = self.return_periods or [2, 5, 10]
        missing = [T for T in rps if T not in self.horner.params]
        if missing:
            raise ValueError(f"horner.params 缺重現期 {missing}（由工程師提供，不得自行補值）")
        if self.tailwater.mode == "hydrograph":
            miss = [T for T in rps if T not in (self.tailwater.Hp_m or {})]
            if miss:
                raise ValueError(f"tailwater.Hp_m 缺重現期 {miss}")
        bottom = self.detention.layers[0].elev_m
        for o in self.outlet.orifices:
            if o.invert_m < bottom:
                raise ValueError(f"孔口 {o.name} 底高 {o.invert_m} 低於池底 {bottom}")
        for w in self.outlet.weirs:
            if w.crest_m < bottom:
                raise ValueError(f"堰 {w.name} 頂高 {w.crest_m} 低於池底 {bottom}")
        return self
