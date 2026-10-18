"""SiteSpec：基地套疊（Level 1-2）的 JSON spec 定義。

agent 只能寫這個 model 認得的欄位（extra="forbid"），generator 只吃這個 model。
相對路徑一律以「執行指令時的工作目錄」（repo 根目錄）為基準解析。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ParcelGroup(BaseModel):
    """同一個地段內要選取的地號。段名與段號至少給一個；兩個都給時兩個都要符合。"""

    model_config = ConfigDict(extra="forbid")

    section_name: str | None = Field(default=None, description="段名，例如「測試段」")
    section_code: str | None = Field(default=None, description="段號，例如「0001」或「1」")
    land_numbers: list[str] = Field(
        min_length=1, description="地號清單，接受「12」「12-1」「0012-0001」「00120001」"
    )

    @model_validator(mode="after")
    def _need_section(self) -> ParcelGroup:
        if not (self.section_name or self.section_code):
            raise ValueError("段名（section_name）與段號（section_code）至少要給一個")
        return self


class SiteSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parcels: list[ParcelGroup] = Field(min_length=1)
    cadastre_path: Path = Field(description="地籍圖（SHP / GPKG），需含 段號 或 段名、地號 欄位")
    zoning_path: Path = Field(description="使用分區圖（SHP / GPKG）")
    zoning_field: str = Field(default="分區", description="分區圖中記錄分區名稱的欄位")
    target_crs: Literal["EPSG:3826", "EPSG:3825"] = Field(
        default="EPSG:3826", description="本島 3826；澎湖才用 3825"
    )
    output_path: Path = Field(description="輸出 GeoPackage 路徑（.gpkg）")
    expected_area_m2: float = Field(
        gt=0,
        description="必填：使用者 / 地政資料提供的登記面積合計（m²），供 area_check 對帳；不得由 agent 自行推算",
    )
    expected_area_source: str | None = Field(
        default=None, min_length=1, description="expected_area_m2 的來源（例「土地登記謄本 2026-10」「使用者提供」）"
    )

    confirmed_excluded_parcels: list[str] = Field(
        default_factory=list,
        description=(
            "使用者確認「不納入」的相鄰同母號地號（例「101-3」或「0102:101-3」）；"
            "與已選地號共邊且同母號、但未列入 parcels 也未列在這裡 → overlay_check WARNING（疑似漏列子號，需與使用者確認）"
        ),
    )

    @field_validator("output_path")
    @classmethod
    def _gpkg_only(cls, v: Path) -> Path:
        if v.suffix.lower() != ".gpkg":
            raise ValueError("output_path 必須是 .gpkg")
        return v

    @classmethod
    def from_json(cls, path: str | Path) -> SiteSpec:
        return cls.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))
