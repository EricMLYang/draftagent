"""DIMENSION 實際顯示文字的讀取與檢查（所有 verifier 共用）。純函式，不得呼叫 LLM。

過去的缺陷：只用「量測值 × dimlfac」推算顯示值，漏掉 dimrnd（四捨五入）、dimpost（前後綴，
例如 "<>0" 會把 4500 顯示成 45000）、文字覆寫等。正確做法是讀 DIMENSION 的 geometry block
裡實際畫出來的 TEXT / MTEXT，再與期望值比對。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 會改變顯示數字、但本 repo 一律不使用的標註變數：出現非預設值即 error
FORBIDDEN_DIMVARS: dict[str, object] = {
    "dimrnd": 0.0,  # 捨入
    "dimpost": "",  # 前後綴（"<>" 等同預設）
    "dimalt": 0,  # 替代單位
    "dimzin": None,  # 前後導零（不檢查值，只是列出；None = 不限制）
}

_NUM = re.compile(r"[-+]?\d+(?:\.\d+)?")
# MTEXT 格式碼：\P 換行、\A1; 對齊、\H2.5; 字高、{\f...;} 字型等
_MTEXT_CODES = re.compile(r"\\[A-Za-z][^;\\]*;|\\P|[{}]")


@dataclass(frozen=True)
class DimText:
    handle: str
    text: str  # 實際顯示的純文字（多段以空白相接）
    numbers: tuple[float, ...]  # 文字中的所有數字


def _plain(s: str) -> str:
    return _MTEXT_CODES.sub(" ", s).replace("%%c", "Ø").strip()


def displayed_text(dim) -> DimText:
    """讀 DIMENSION 的 geometry block 中 TEXT / MTEXT 的實際內容。

    block 不存在（未 render）時回傳空字串——呼叫端應視為 error（無法確認顯示值）。
    """
    doc = dim.doc
    name = dim.dxf.get("geometry")
    parts: list[str] = []
    if doc is not None and name and name in doc.blocks:
        for e in doc.blocks[name]:
            t = e.dxftype()
            if t == "TEXT":
                parts.append(_plain(e.dxf.text))
            elif t == "MTEXT":
                parts.append(_plain(e.text))
    text = " ".join(p for p in parts if p)
    nums = tuple(float(m.group()) for m in _NUM.finditer(text.replace(",", "")))
    return DimText(dim.dxf.handle, text, nums)


def forbidden_vars(dim) -> list[str]:
    """回傳違規的標註變數描述（覆寫優先，其次 dimstyle）。"""
    ov = dim.override()
    bad: list[str] = []
    rnd = float(ov.get("dimrnd", 0.0) or 0.0)
    if abs(rnd) > 1e-12:
        bad.append(f"dimrnd={rnd:g}（捨入會改變顯示值）")
    post = str(ov.get("dimpost", "") or "")
    if post not in ("", "<>"):
        bad.append(f"dimpost={post!r}（前後綴會改變顯示值）")
    if int(ov.get("dimalt", 0) or 0):
        bad.append("dimalt=1（替代單位）")
    return bad


def matches_number(text: str, value: float, tol: float) -> bool:
    """文字中是否有「完整數字」等於 value（±tol）。不做子字串比對（"45000" 不匹配 4500）。"""
    return any(abs(float(m.group()) - value) <= tol for m in _NUM.finditer(text.replace(",", "")))
