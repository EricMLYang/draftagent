"""SlopeSpec：Level 1-3 坡度分析的輸入規格。

路徑一律以「執行指令時的工作目錄」（repo 根目錄）為基準解析，不以 spec 檔所在位置解析。
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from draftagent.verify.codes_lock import validate_codes_path

DEFAULT_CLASSES_PATH = (
    Path(__file__).resolve().parent.parent / "hydro" / "codes" / "slope_classes.yaml"
)


class SlopeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dtm_path: Path = Field(description="DTM GeoTIFF，必須為 EPSG:3826，單位 m")
    site_path: Path = Field(description="基地範圍向量（GeoPackage / SHP），必須為 EPSG:3826")
    site_layer: str | None = Field(
        default=None, description="site_path 內的圖層名稱，單一圖層可省略"
    )
    classes_path: Path = Field(
        default=DEFAULT_CLASSES_PATH,
        description="坡度分級門檻 yaml（法規數值唯一來源）；只能是 draftagent/*/codes/*.yaml，verifier 只接受 repo 預設",
    )
    output_raster: Path = Field(description="輸出：坡度分級 GeoTIFF（uint8，0 = 無值）")
    output_gpkg: Path = Field(description="輸出：各級多邊形 GeoPackage，layer slope_classes")
    output_csv: Path = Field(description="輸出：各級面積統計 CSV")

    @field_validator("classes_path")
    @classmethod
    def _classes_in_repo(cls, v: Path) -> Path:
        return validate_codes_path(v)

    @classmethod
    def from_json(cls, path: str | Path) -> SlopeSpec:
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))
