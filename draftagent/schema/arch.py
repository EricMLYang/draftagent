"""ArchImportSpec：建築師 DXF → TWD97 GeoJSON（圖種 `arch-import`）的 JSON spec。

原則：工具只「看懂」與「轉換」；哪個圖層是外牆、圖上單位、控制點，一律由 agent / 工程師判斷後寫進 spec
（`draftagent inspect-dxf` 只提供線索）。$INSUNITS 常常是錯的，所以 `drawing_units` 必填。

路徑：相對路徑以執行指令時的工作目錄（repo 根目錄）解析；`source_dxf` 必須是 repo 內的相對路徑。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

REPO_ROOT = Path(__file__).resolve().parents[2]

DrawingUnits = Literal["mm", "cm", "m"]
UNIT_TO_M: dict[str, float] = {"mm": 0.001, "cm": 0.01, "m": 1.0}
# 接線容差：預設 = 10 mm（換算成圖上單位），上限 50 mm——再大就是「幫建築師補圖」，應回頭問人
DEFAULT_JOIN_TOL_M = 0.01
MAX_JOIN_TOL_M = 0.05
# 雙線外牆的開口橋接寬度（門 / 窗 / 車道開口）：預設 3.0 m，上限 6.0 m
DEFAULT_OPENING_MAX_M = 3.0
MAX_OPENING_MAX_M = 6.0


def resolve_repo_path(p: str | Path) -> Path:
    """相對路徑：先以工作目錄解析，不存在時再以 repo 根目錄解析。"""
    p = Path(p)
    if p.is_absolute():
        return p
    if p.exists():
        return p.resolve()
    return (REPO_ROOT / p).resolve()


class ControlPoint(BaseModel):
    """控制點：圖上座標（drawing units）↔ TWD97 TM2（EPSG:3826，m）。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    drawing: tuple[float, float] = Field(description="圖上座標 [x, y]（drawing_units）")
    twd97: tuple[float, float] = Field(description="TWD97 TM2 座標 [E, N]（m，EPSG:3826）")
    source: str | None = Field(default=None, description="控制點來源（例「地籍界址點 K12」「現場 RTK 實測」）")


class ArchLayers(BaseModel):
    """各角色對應的 DXF 圖層（大小寫需與 DXF 一致）。"""

    model_config = ConfigDict(extra="forbid")

    exterior_wall: list[str] = Field(min_length=1, description="一樓外牆（必填）")
    basement: list[str] = Field(default_factory=list, description="地下室外牆 / 筏基外緣（選填）")
    open_space: list[str] = Field(default_factory=list, description="空地 / 綠地範圍（選填，封閉外框）")
    roof_drains: list[str] = Field(
        default_factory=list, description="落水頭 / 雨水立管（點：POINT、CIRCLE 圓心、INSERT 插入點）"
    )

    @model_validator(mode="after")
    def _no_dup(self) -> ArchLayers:
        seen: dict[str, str] = {}
        for role in ("exterior_wall", "basement", "open_space", "roof_drains"):
            for name in getattr(self, role):
                if name in seen and seen[name] != role:
                    raise ValueError(f"圖層 {name!r} 同時被指定為 {seen[name]} 與 {role}")
                seen[name] = role
        return self


class EntityFilter(BaseModel):
    """圖層之外再用實體屬性篩（例：外牆畫在 0 層，只能靠 color + lineweight 區分）。給的條件全部要符合。"""

    model_config = ConfigDict(extra="forbid")

    color: int | None = Field(default=None, ge=0, le=257, description="ACI 色號（實體 color 屬性原值，256 = BYLAYER）")
    lineweight: int | None = Field(default=None, ge=-3, le=211, description="線寬（1/100 mm，實體 lineweight 屬性原值）")

    @model_validator(mode="after")
    def _some(self) -> EntityFilter:
        if self.color is None and self.lineweight is None:
            raise ValueError("exterior_wall_filter 至少要給 color 或 lineweight")
        return self


class FloorFilter(BaseModel):
    """只取某一層（例 1F）的圖面範圍。bbox 與 title_text 二擇一。

    - bbox：[xmin, ymin, xmax, ymax]（圖上座標）
    - title_text + search_radius：找內容含 title_text 的 TEXT / MTEXT（例「一層平面圖」），
      以其插入點為中心、半徑 search_radius（圖上單位）的方框為範圍
    實體完全落在範圍內才採用（跨出範圍的線視為別張圖的）。
    """

    model_config = ConfigDict(extra="forbid")

    bbox: tuple[float, float, float, float] | None = None
    title_text: str | None = Field(default=None, min_length=1)
    search_radius: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _one_of(self) -> FloorFilter:
        if (self.bbox is None) == (self.title_text is None):
            raise ValueError("floor_filter：bbox 與 title_text 必須二擇一")
        if self.bbox is not None:
            x0, y0, x1, y1 = self.bbox
            if not (x1 > x0 and y1 > y0):
                raise ValueError("floor_filter.bbox 必須是 [xmin, ymin, xmax, ymax] 且 max > min")
        if self.title_text is not None and self.search_radius is None:
            raise ValueError("floor_filter：用 title_text 時必須給 search_radius（圖上單位）")
        return self


class SiteRef(BaseModel):
    """基地界線（EPSG:3826）：path（GeoJSON / GPKG / SHP）或 coords 二擇一。"""

    model_config = ConfigDict(extra="forbid")

    path: Path | None = None
    layer: str | None = None
    coords: list[tuple[float, float]] | None = Field(default=None, min_length=3)

    @model_validator(mode="after")
    def _one_of(self) -> SiteRef:
        if (self.path is None) == (self.coords is None):
            raise ValueError("site：path 與 coords 必須二擇一")
        return self


class ArchImportSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["arch-import"] = "arch-import"
    title: str | None = None
    source_dxf: Path = Field(description="建築師 DXF（repo 內相對路徑）")
    drawing_units: DrawingUnits = Field(
        description="圖上實際單位（必填；由 agent 依 inspect-dxf 的尺寸 / 標註判斷，不得只信 $INSUNITS）"
    )
    layers: ArchLayers
    wall_representation: Literal["outline", "double_line"] = Field(
        description="outline = 外牆畫成單線外框；double_line = 外牆雙線（取外緣，門窗開口會被橋接）"
    )
    floor_filter: FloorFilter | None = None
    exterior_wall_filter: EntityFilter | None = Field(
        default=None, description="外牆圖層上再以 color / lineweight 篩選（外牆與雜線同層時用）"
    )
    offsets: dict[Literal["exterior_wall", "basement", "open_space", "roof_drains"], tuple[float, float]] = Field(
        default_factory=dict,
        description=(
            "各角色的圖上平移 [dx, dy]（圖上單位），在套控制點前加上。用於多層並排在同一 modelspace："
            "例 B1 畫在 1F 右邊 60 m、控制點只對 1F → basement: [-60000, 0]（由各層軸線對位判斷）"
        ),
    )
    control_points: list[ControlPoint] = Field(
        min_length=2, description="≥ 2 點；2 點時轉換無法驗證（warning），建議 ≥ 3 點"
    )
    join_tolerance: float | None = Field(
        default=None, gt=0,
        description="接線容差（圖上單位）。省略 = 10 mm 換算；上限 50 mm 換算",
    )
    opening_max: float | None = Field(
        default=None, gt=0,
        description="double_line：可橋接的最大開口寬（圖上單位）。省略 = 3 m 換算；上限 6 m",
    )
    min_area_m2: float = Field(
        default=1.0, ge=0, le=10.0, description="小於此面積的封閉面視為雜線（略過並列 warning）"
    )
    allow_multiple_footprints: bool = Field(
        default=False, description="外牆抽出多個分離外框時是否允許（預設不允許 → arch_check error）"
    )
    site: SiteRef | None = Field(default=None, description="基地界線，用來檢查外框在基地內")
    crs: Literal["EPSG:3826"] = "EPSG:3826"

    @field_validator("source_dxf")
    @classmethod
    def _repo_relative(cls, v: Path) -> Path:
        if v.is_absolute():
            raise ValueError("source_dxf 必須是 repo 內的相對路徑")
        if ".." in v.parts:
            raise ValueError("source_dxf 不得含 ..（必須在 repo 內）")
        if v.suffix.lower() != ".dxf":
            raise ValueError("source_dxf 必須是 .dxf")
        return v

    @model_validator(mode="after")
    def _bounds(self) -> ArchImportSpec:
        k = UNIT_TO_M[self.drawing_units]
        if self.join_tolerance is not None and self.join_tolerance * k > MAX_JOIN_TOL_M + 1e-12:
            raise ValueError(
                f"join_tolerance {self.join_tolerance} {self.drawing_units} = {self.join_tolerance * k:.4f} m "
                f"超過上限 {MAX_JOIN_TOL_M} m：縫隙這麼大請回頭問建築師 / 工程師，不要硬接"
            )
        if self.opening_max is not None and self.opening_max * k > MAX_OPENING_MAX_M + 1e-12:
            raise ValueError(f"opening_max 換算 {self.opening_max * k:.3f} m 超過上限 {MAX_OPENING_MAX_M} m")
        ids = [c.id for c in self.control_points]
        if len(set(ids)) != len(ids):
            raise ValueError(f"control_points id 重複：{ids}")
        return self

    # ---- 衍生值（不寫回 spec，讓 spec.json 保持 agent 原本寫的樣子）
    @property
    def unit_to_m(self) -> float:
        return UNIT_TO_M[self.drawing_units]

    @property
    def join_tol(self) -> float:
        """接線容差（圖上單位）。"""
        return self.join_tolerance if self.join_tolerance is not None else DEFAULT_JOIN_TOL_M / self.unit_to_m

    @property
    def opening_bridge(self) -> float:
        """雙線開口橋接寬（圖上單位）。"""
        return self.opening_max if self.opening_max is not None else DEFAULT_OPENING_MAX_M / self.unit_to_m

    def dxf_path(self) -> Path:
        return resolve_repo_path(self.source_dxf)

    @classmethod
    def from_json(cls, path: str | Path) -> ArchImportSpec:
        return cls.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))
