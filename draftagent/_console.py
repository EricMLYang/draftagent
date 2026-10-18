"""終端輸出編碼：Windows console / 重導向預設 cp950，印 🟢🔴 或罕用字會 UnicodeEncodeError。

`ensure_utf8_stdio()` 在 Windows 上把 stdout / stderr reconfigure 成 UTF-8（`errors="replace"`）；
其他平台、已是 UTF-8、或串流不支援 reconfigure（被測試框架 / IDE 換掉）時不動。
"""

from __future__ import annotations

import codecs
import sys


def _is_utf8(enc: str | None) -> bool:
    try:
        return codecs.lookup(enc or "").name == "utf-8"
    except LookupError:
        return False


def ensure_utf8_stdio(*, force: bool = False) -> list[str]:
    """回傳被改成 UTF-8 的串流名稱。`force=True` 不看平台（測試用）。"""
    if not force and sys.platform != "win32":
        return []
    changed: list[str] = []
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None or _is_utf8(getattr(stream, "encoding", None)):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # 已有讀寫進行中 / 已關閉
            continue
        changed.append(name)
    return changed
