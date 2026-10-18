"""排水計畫書本文的文字契約（章節 / 統一圖名讀 yaml；表名、「=」標籤、固定句為常數）。

`report/content.py` 依這裡的字串產生本文，`verify/report_check.py` 依同一份契約把 report.md /
report.docx 解析回「表 → 欄」「標籤 → 數值」再與重算結果比對。**只放文字，不放任何數值**
（數值由程式從模型 / 計算結果帶入；法規數值只從 codes/*.yaml 讀）。純常數，不得呼叫 LLM。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

FORMAT_PATH = Path(__file__).parent / "codes" / "report_format.yaml"


@dataclass(frozen=True)
class SheetName:
    no: int
    name: str
    scale_key: str | None
    produced_by: str | None
    provided_by: str | None


@dataclass(frozen=True)
class ReportFormat:
    path: Path
    verified: bool
    note: str
    source: str
    chapters: tuple[tuple[str, str], ...]  # (key, 章名)
    appendix_title: str
    sheets: tuple[SheetName, ...]
    layout: dict[str, Any]

    def chapter(self, key: str) -> str:
        for k, t in self.chapters:
            if k == key:
                return t
        raise KeyError(key)


@lru_cache(maxsize=4)
def load_format(path: str | Path = FORMAT_PATH) -> ReportFormat:
    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    for k in ("verified", "chapters", "sheets", "layout", "source"):
        if k not in raw:
            raise ValueError(f"{path}: 缺少 {k}")
    sheets = tuple(
        SheetName(
            no=int(s["item"]),
            name=str(s["name"]),
            scale_key=s.get("scale_key"),
            produced_by=s.get("produced_by"),
            provided_by=s.get("provided_by"),
        )
        for s in raw["sheets"]
    )
    return ReportFormat(
        path=path,
        verified=bool(raw["verified"]),
        note=str(raw.get("note", "")),
        source=str(raw["source"]),
        chapters=tuple((c["key"], c["title"]) for c in raw["chapters"]),
        appendix_title=str(raw.get("appendix_title", "附圖")),
        sheets=sheets,
        layout=raw["layout"],
    )


# ---------------------------------------------------------------- 固定句

CONCLUSION = (
    "本計畫書圖說通過本 repo 現有 verifier 之檢查項目；法規數值尚待人工核對原文，並不代表符合法規。"
)
VERIFIED_NOTICE_HEAD = "※ 法規 / 手冊數值核對狀態："
TOC_TITLE = "目錄"
TOC_PLACEHOLDER = "（目錄為 Word 功能變數：開啟文件後於目錄上按右鍵「更新功能變數」產生）"
PLACEHOLDER_NOTE = "（○○○ 為未提供資料之佔位，送審前由使用者填寫）"

# ---------------------------------------------------------------- 表名（caption 前綴 → 檢查器 key）

T_LAND = "表 2-1 土地座落"
T_SITE = "表 2-2 基地概要"
T_CODES = "表 4-1 設計基準與法規數值"
T_HORNER = "表 4-2 降雨強度（Horner 公式）"
T_RUNOFF_C = "表 4-3 逕流係數"
T_EXISTING = "表 4-4 基地附近排水系統現況"
T_PHOTOS = "表 4-5 基地現況照片"
T_CATCH = "表 5-1 集水分區"
T_PIPES = "表 5-2 管渠水理計算"
T_STORAGE = "表 5-3 貯集滯洪量計算"
T_RELEASE = "表 5-4 逕流排放量計算"
T_COST = "表 6-1 設施項目數量及工程造價"
T_SHEETS = "表 附-1 附圖清單（統一圖名對照）"

TABLES = (
    T_LAND, T_SITE, T_CODES, T_HORNER, T_RUNOFF_C, T_EXISTING, T_PHOTOS,
    T_CATCH, T_PIPES, T_STORAGE, T_RELEASE, T_COST, T_SHEETS,
)  # fmt: skip

# ---------------------------------------------------------------- 「標籤 = 數值」（摘要 / 5.3）

L_SITE_AREA = "基地面積 A"
L_AS = "計算用面積 As"
L_C = "綜合逕流係數 C"
L_V_REQ = "必要貯集量 V_req"
L_V_PROV = "提供貯集量 V_prov"
L_Q_ALLOW = "容許排放量 Q_allow"
L_Q_OUT = "設計排放量 Q_out"
L_RATIO = "排放量比 Q_out/Q_allow"
L_Q_WEIR = "溢流堰排洪量 Q_weir"
L_Q20 = "全基地洪峰流量 Q20"
L_COST_TOTAL = "總工程造價"
L_COST_PARTIAL = "總工程造價（不含未提供單價項目）"
# 法規數值（必須附〔yaml：…〕來源標註）
L_K_TY = "最小貯集量係數 k_TY"
L_K_BC = "最小貯集量係數 k_BC"
L_K_Q = "單位面積容許排放量 q"

REGULATORY_LABELS = {
    L_K_TY: ("taoyuan_runoff.yaml", "runoff_standard.min_storage_m3_per_m2"),
    L_K_BC: ("taoyuan_runoff.yaml", "building_code_4_3.min_storage_m3_per_m2"),
    L_K_Q: ("taoyuan_runoff.yaml", "runoff_standard.max_release_cms_per_m2"),
}

# 〔yaml：<檔名>：<路徑>；<出處>；verified: false〕
SRC_OPEN, SRC_CLOSE = "〔", "〕"
SRC_PREFIX = "yaml："
VERIFIED_FALSE = "verified: false"
VERIFIED_TRUE = "verified: true"

MODEL_SRC = "模型計算"
USER_SRC = "使用者提供"
NOT_GIVEN = "未提供"
FIG_PREFIX = "附圖"
