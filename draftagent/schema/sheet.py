"""SheetSpec：圖紙、圖框、圖簽設定。單位一律圖紙 mm。

- 圖紙尺寸依 **ISO 216**（A 系列，ISO 216:2007 Table 1），程式常數 `ISO216_A`，一律橫式（landscape）。
- 邊距（裝訂邊 / 其他三邊）：CNS 3 原文未取得，**邊距暫定**，可由 spec `margins` 覆寫。
- 圖簽的「比例」欄由 renderer 依圖面實際比例產生，spec 不提供 scale_text 欄位。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# ISO 216 A 系列 (短邊, 長邊) mm；來源：ISO 216:2007 Table 1
ISO216_A: dict[str, tuple[float, float]] = {
    "A1": (594.0, 841.0),
    "A3": (297.0, 420.0),
}

# CNS 3 原文未取得，邊距暫定（左側為裝訂邊）
DEFAULT_MARGINS: dict[str, dict[str, float]] = {
    "A1": {"left": 25.0, "right": 10.0, "top": 10.0, "bottom": 10.0},
    "A3": {"left": 25.0, "right": 10.0, "top": 10.0, "bottom": 10.0},
}

SheetSize = Literal["A3", "A1"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Margins(_Model):
    """圖框內框距紙邊 mm。CNS 3 原文未取得，預設值暫定。"""

    left: float = Field(ge=0)
    right: float = Field(ge=0)
    top: float = Field(ge=0)
    bottom: float = Field(ge=0)


class TitleBlock(_Model):
    project: str = Field(min_length=1, description="工程名稱")
    drawing_title: str = Field(default="", description="圖名；空白時 renderer 用圖面 title")
    drawing_no: str = Field(min_length=1, description="圖號")
    date: str = Field(
        default="", description="日期（自由格式，如 115.10.17）；renderer 不自動填今天"
    )
    designer: str = ""
    checker: str = ""


class SheetSpec(_Model):
    size: SheetSize = "A3"
    margins: Margins | None = Field(default=None, description="None → DEFAULT_MARGINS（暫定）")
    title_block: TitleBlock
    north_arrow: bool | None = Field(
        default=None, description="None → 依圖別預設（縱斷面 false、平面 true）"
    )
    show_scale_bar: bool = True

    def paper(self) -> tuple[float, float]:
        """(寬, 高) mm，橫式。"""
        short, long_ = ISO216_A[self.size]
        return long_, short

    def resolved_margins(self) -> Margins:
        return self.margins or Margins(**DEFAULT_MARGINS[self.size])

    def inner(self) -> tuple[float, float, float, float]:
        """內框 (x0, y0, x1, y1) mm。"""
        w, h = self.paper()
        m = self.resolved_margins()
        return m.left, m.bottom, w - m.right, h - m.top
