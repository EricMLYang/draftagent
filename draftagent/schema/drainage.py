"""TaoyuanSiteDrainageSpec：桃園建築基地雨水流出抑制設施計算的輸入規格。

Agent 只描述幾何與工程師選定的設計參數（分區面積、土地使用、設施尺寸），
不填任何計算結果與法規數值；所有係數由 `draftagent/hydro/codes/taoyuan_runoff.yaml` 提供。

驗證規則（載入 yaml 後檢查）
- district：必須在 yaml 某雨量站的 districts 內；否則必須另給 station
- station（覆寫）：必須是 yaml 的站名
- subareas[].landuse：必須是 yaml 逕流係數表的 key；c_override 必須在該 landuse 的 [min, max]
- pipes[].material：必須是 yaml 曼寧 n 表的 key；材料為 n_range 時必須給 n_override 且在範圍內；
  單值材料給 n_override 會被拒（n 由 yaml 決定）
- 圓管（diameter_m）與矩形溝（width_m + depth_m）擇一，且需與材料的 shape 相符
- release_mode：orifice（預設）需 outlet；pump 需 pump_q_cms（抽水排放量，同樣以容許排放量檢核）
- pipes[].design_c：0–1；給了 → 管渠設計流量 Q = design_c · I · A；省略 → 用全基地綜合 C
  （drainage-plan 由排水網路上游集水分區面積加權算出後寫入；drainage-calc-ty 無網路拓樸時通常省略）

路徑以執行指令時的工作目錄（repo 根目錄）為基準解析。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from draftagent.verify.codes_lock import validate_codes_path

DEFAULT_CODES_PATH = (
    Path(__file__).resolve().parent.parent / "hydro" / "codes" / "taoyuan_runoff.yaml"
)


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExistingBuilding(_Base):
    new_building_area_m2: float = Field(gt=0, description="本次新增（增建 / 改建）建築面積 m²")
    legal_coverage_ratio: float = Field(gt=0, le=1, description="法定建蔽率（0–1）")


class Subarea(_Base):
    name: str
    area_m2: float = Field(gt=0)
    landuse: str = Field(description="yaml runoff_coefficients.classes 的 key")
    c_override: float | None = Field(
        default=None, description="工程師指定的 C；須在該 landuse 範圍內，省略則用中值"
    )


class StorageCell(_Base):
    length_m: float = Field(gt=0)
    width_m: float = Field(gt=0)


class Storage(_Base):
    type: Literal["raft", "surface"]
    cells: list[StorageCell] = Field(min_length=1)
    effective_depth_m: float = Field(gt=0, description="有效貯集水深 m")
    outlet_well_area_m2: float = Field(
        default=0.0, ge=0, description="位於貯集槽內的出流井平面積，不計入貯集量"
    )


class Outlet(_Base):
    type: Literal["rect_orifice", "circ_orifice"]
    height_m: float | None = Field(default=None, gt=0, description="矩形孔口高 L")
    width_m: float | None = Field(default=None, gt=0, description="矩形孔口寬 B")
    diameter_m: float | None = Field(default=None, gt=0, description="圓形孔口徑 L")
    design_head_m: float = Field(gt=0, description="設計水深 y（自孔口底起算）m")

    @model_validator(mode="after")
    def _dims(self) -> Outlet:
        if self.type == "rect_orifice":
            if self.height_m is None or self.width_m is None or self.diameter_m is not None:
                raise ValueError("rect_orifice 需 height_m 與 width_m，且不得給 diameter_m")
        else:
            if self.diameter_m is None or self.height_m is not None or self.width_m is not None:
                raise ValueError("circ_orifice 需 diameter_m，且不得給 height_m / width_m")
        return self

    @property
    def opening_height_m(self) -> float:
        return float(self.height_m if self.type == "rect_orifice" else self.diameter_m)


class OverflowWeir(_Base):
    width_m: float = Field(gt=0, description="矩形堰寬 B")
    head_m: float = Field(gt=0, description="堰頂水頭 y")


class Pipe(_Base):
    name: str
    diameter_m: float | None = Field(default=None, gt=0, description="圓管內徑 m")
    width_m: float | None = Field(default=None, gt=0, description="U 溝 / 矩形渠寬 m")
    depth_m: float | None = Field(default=None, gt=0, description="U 溝 / 矩形渠深 m")
    material: str = Field(description="yaml manning_n.materials 的 key")
    n_override: float | None = Field(default=None, gt=0)
    slope: float = Field(gt=0, description="縱坡 m/m（例 0.01 = 1%）")
    design_area_m2: float = Field(gt=0, description="此管渠承接的集水面積 m²")
    design_c: float | None = Field(
        default=None,
        ge=0,
        le=1,
        description="此管渠的設計逕流係數（上游集水分區面積加權 Σ(C·A)/ΣA，0–1）；"
        "None = 沿用全基地綜合 C（C_composite）",
    )

    @model_validator(mode="after")
    def _shape(self) -> Pipe:
        circ = self.diameter_m is not None
        rect = self.width_m is not None or self.depth_m is not None
        if circ == rect:
            raise ValueError(f"管渠 {self.name}：diameter_m 與 (width_m, depth_m) 必須擇一")
        if rect and (self.width_m is None or self.depth_m is None):
            raise ValueError(f"管渠 {self.name}：矩形斷面需同時給 width_m 與 depth_m")
        return self

    @property
    def shape(self) -> str:
        return "circular" if self.diameter_m is not None else "rectangular"


class TaoyuanSiteDrainageSpec(_Base):
    site_area_m2: float = Field(gt=0, description="基地面積 m²")
    existing_building: ExistingBuilding | None = None
    district: str = Field(description="桃園市行政區名，例「桃園區」；雨量站由 yaml 決定")
    station: str | None = Field(default=None, description="雨量站覆寫，須為 yaml 站名")
    subareas: list[Subarea] = Field(min_length=1)
    tc_min: float | None = Field(default=None, gt=0, description="集流時間 min；省略讀 yaml")
    storage: Storage
    release_mode: Literal["orifice", "pump"] = Field(
        default="orifice", description="排放方式：orifice（孔口重力）/ pump（抽水，排放量 = pump_q_cms）"
    )
    outlet: Outlet | None = Field(default=None, description="孔口；release_mode=orifice 必填")
    pump_q_cms: float | None = Field(
        default=None, gt=0, description="抽水機設計抽水量 m³/s；release_mode=pump 必填"
    )
    overflow_weir: OverflowWeir | None = None
    pipes: list[Pipe] = Field(default_factory=list)
    codes_path: Path = Field(
        default=DEFAULT_CODES_PATH,
        description="法規 / 手冊數值 yaml；只能是 draftagent/*/codes/*.yaml（verifier 只接受 repo 預設）",
    )

    @field_validator("codes_path")
    @classmethod
    def _codes_in_repo(cls, v: Path) -> Path:
        return validate_codes_path(v)

    @model_validator(mode="after")
    def _against_codes(self) -> TaoyuanSiteDrainageSpec:
        from draftagent.hydro.taoyuan import load_codes  # 延遲 import 避免循環

        if self.release_mode == "orifice" and (self.outlet is None or self.pump_q_cms is not None):
            raise ValueError("release_mode=orifice 需 outlet（孔口），且不得給 pump_q_cms")
        if self.release_mode == "pump" and (self.pump_q_cms is None or self.outlet is not None):
            raise ValueError("release_mode=pump 需 pump_q_cms，且不得給 outlet")

        codes = load_codes(self.codes_path)
        stations = codes["horner"]["stations"]
        if self.station is not None:
            if self.station not in stations:
                raise ValueError(f"station {self.station!r} 不在 yaml 站名 {sorted(stations)}")
        else:
            mapped = [s for s, v in stations.items() if self.district in (v.get("districts") or [])]
            if not mapped:
                raise ValueError(
                    f"行政區 {self.district!r} 在 yaml 無對應雨量站；"
                    f"請由工程師以 station 指定（{sorted(stations)}）並補 yaml"
                )

        cls = codes["runoff_coefficients"]["classes"]
        names = {s.name for s in self.subareas}
        if len(names) != len(self.subareas):
            raise ValueError("subareas 的 name 不得重複")
        for s in self.subareas:
            if s.landuse not in cls:
                raise ValueError(f"subarea {s.name}：landuse {s.landuse!r} 不在 yaml {sorted(cls)}")
            c = cls[s.landuse]
            if s.c_override is not None and not (c["min"] <= s.c_override <= c["max"]):
                raise ValueError(
                    f"subarea {s.name}：c_override {s.c_override} 超出 {s.landuse} "
                    f"範圍 [{c['min']}, {c['max']}]"
                )

        mats = codes["manning_n"]["materials"]
        for p in self.pipes:
            if p.material not in mats:
                raise ValueError(f"管渠 {p.name}：material {p.material!r} 不在 yaml {sorted(mats)}")
            m = mats[p.material]
            if m["shape"] != p.shape:
                raise ValueError(f"管渠 {p.name}：材料 {p.material} 為 {m['shape']}，斷面為 {p.shape}")
            if "n_range" in m:
                lo, hi = m["n_range"]
                if p.n_override is None or not (lo <= p.n_override <= hi):
                    raise ValueError(
                        f"管渠 {p.name}：{p.material} 的 n 為範圍 [{lo}, {hi}]，"
                        f"須由工程師以 n_override 指定範圍內的值"
                    )
            elif p.n_override is not None:
                raise ValueError(f"管渠 {p.name}：{p.material} 的 n 由 yaml 決定，不得 n_override")
        return self

    @classmethod
    def from_json(cls, path: str | Path) -> TaoyuanSiteDrainageSpec:
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))
