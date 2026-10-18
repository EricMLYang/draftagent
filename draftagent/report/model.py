"""DrainageReportSpec：排水計畫書本文 = 一份 DrainageNetworkSpec（重用）+ 計畫書專屬欄位。

計畫書的**數值一律由程式**從模型（`drainage/network.py`、`drainage/derive.py`）與計算結果
（`hydro.taoyuan.compute` → result.json）帶入；spec 只放使用者提供、程式無法推得的資料：

- 申請人 / 設計單位 / 簽證技師：未提供時以「○○○」佔位（不得寫入真實案名 / 公司名）
- 土地座落（段名 / 地號）、使用分區、建蔽率 / 開挖率：使用者提供（範例一律合成）
- 基地附近排水系統現況「描述」（測量數值在 network.existing_drains）
- 照片清單（路徑 + 說明，可空）
- 工程造價單價（數量由模型算；未提供單價 → 該欄留空並 WARNING）
- 章節補充文字：只能是描述性文字，**不得含任何數字**（數值結論一律由程式填）

`extra="forbid"`：不得在 spec 寫面積、貯集量、流量等計算結果。純資料模型，不得呼叫 LLM。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from draftagent.drainage.model import DrainageNetworkSpec

PLACEHOLDER = "○○○"
# 補充文字禁止的數字（半形 / 全形阿拉伯數字）；國字數字無法可靠判斷，不擋（已知限制）
_DIGIT = re.compile(r"[0-9０-９]")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _no_digits(v: str, where: str) -> str:
    if _DIGIT.search(v):
        raise ValueError(
            f"{where} 只能是描述性文字，不得含數字（數值一律由程式從模型 / 計算結果帶入）：{v!r}"
        )
    return v


class LandLot(_Model):
    section: str = Field(min_length=1, description="段名（使用者提供；範例為合成段名）")
    lot: str = Field(
        pattern=r"^\d{1,4}(-\d{1,4})?$", description="地號，例 0123 或 0123-0001（使用者提供）"
    )


class Photo(_Model):
    path: Path = Field(description="照片檔（png / jpg），相對於執行目錄或絕對路徑")
    caption: str = Field(min_length=1, description="照片說明（描述性文字，不得含數字）")

    @field_validator("caption")
    @classmethod
    def _cap(cls, v: str) -> str:
        return _no_digits(v, "photos[].caption")

    @field_validator("path")
    @classmethod
    def _ext(cls, v: Path) -> Path:
        if v.suffix.lower() not in (".png", ".jpg", ".jpeg"):
            raise ValueError(f"照片只接受 .png / .jpg：{v}")
        return v


class UnitPrice(_Model):
    """單價（使用者提供）：對應 `derive.quantities` 數量表的一列（項目 + 規格）。"""

    item: str = Field(min_length=1, description="數量表「項目」，例 排水管、集水井、地面滯洪池")
    spec: str | None = Field(
        default=None, description="數量表「規格」原文；None = 該項目在數量表只有一列"
    )
    unit_price: float = Field(gt=0, description="單價（新臺幣元 / 數量表單位）")


class ExtraCost(_Model):
    """模型無法計量的項目（例 抽水機、假設工程）：數量與單價皆由使用者提供，表內標「使用者提供」。"""

    item: str = Field(min_length=1)
    spec: str = Field(default="—")
    unit: str = Field(min_length=1)
    qty: float = Field(gt=0)
    unit_price: float | None = Field(default=None, gt=0)


ChapterKey = Literal["summary", "purpose", "scope", "overview", "basic_data", "facilities", "cost"]


class ReportInfo(_Model):
    title: str = Field(min_length=1, description="計畫書名稱")
    applicant: str = Field(default=PLACEHOLDER, description="申請人（未提供 → ○○○）")
    designer: str = Field(default=PLACEHOLDER, description="設計單位（未提供 → ○○○）")
    engineer: str = Field(default=PLACEHOLDER, description="簽證技師（未提供 → ○○○）")
    date: str = Field(min_length=1, description="日期（民國，例 115.10.18）")
    land: list[LandLot] = Field(min_length=1, description="土地座落（使用者提供）")
    zoning: str = Field(min_length=1, description="使用分區（使用者提供）")
    coverage_ratio_pct: float | None = Field(
        default=None,
        gt=0,
        le=100,
        description="建蔽率 %（使用者提供；None → 表內「未提供」並 WARNING）",
    )
    excavation_ratio_pct: float | None = Field(
        default=None,
        gt=0,
        le=100,
        description="開挖率 %（使用者提供；None → 「未提供」並 WARNING）",
    )
    existing_drainage: str = Field(
        min_length=1,
        description="基地附近排水系統現況描述（描述性，不得含數字；測量值在 network.existing_drains）",
    )
    photos: list[Photo] = Field(default_factory=list)
    unit_prices: list[UnitPrice] = Field(default_factory=list)
    extra_costs: list[ExtraCost] = Field(default_factory=list)
    notes: dict[ChapterKey, list[str]] = Field(
        default_factory=dict, description="各章補充文字（描述性，不得含數字）"
    )

    @field_validator("existing_drainage")
    @classmethod
    def _desc(cls, v: str) -> str:
        return _no_digits(v, "existing_drainage")

    @field_validator("notes")
    @classmethod
    def _notes(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        for k, paras in v.items():
            for p in paras:
                _no_digits(p, f"notes.{k}")
        return v


class DrainageReportSpec(_Model):
    kind: Literal["drainage-report"] = "drainage-report"
    network: DrainageNetworkSpec
    report: ReportInfo

    @property
    def units(self) -> str:
        return self.network.units

    @model_validator(mode="after")
    def _check(self) -> DrainageReportSpec:
        from draftagent.drainage.derive import quantities

        net = self.network
        if net.storage is None or net.calc is None:
            raise ValueError("排水計畫書需要 network.storage 與 network.calc（流出抑制計算）")
        rows = quantities(net)
        for i, up in enumerate(self.report.unit_prices):
            hits = [r for r in rows if r.item == up.item and (up.spec is None or r.spec == up.spec)]
            if len(hits) != 1:
                have = [f"{r.item}｜{r.spec}" for r in rows]
                raise ValueError(
                    f"report.unit_prices[{i}] ({up.item!r}, {up.spec!r}) 對應數量表 {len(hits)} 列"
                    f"（須恰好 1 列；數量表：{have}）"
                )
        keys = [(u.item, u.spec) for u in self.report.unit_prices]
        if len(set(keys)) != len(keys):
            raise ValueError("report.unit_prices 有重複項目")
        return self

    @classmethod
    def from_json(cls, path: str | Path) -> DrainageReportSpec:
        return cls.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))
