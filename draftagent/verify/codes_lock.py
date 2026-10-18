"""法規 yaml 路徑鎖定（CLAUDE.md：法規數值只能從 draftagent/*/codes/*.yaml 讀）。純函式，不得呼叫 LLM。

兩層防線
- schema 層：`validate_codes_path()` 擋不在 `draftagent/<pkg>/codes/` 下的路徑（pydantic validator 用）
- verifier 層：`assert_default_codes()` 要求 spec 給的路徑 resolve 後 = repo 預設檔，
  否則 error；verifier 一律以預設路徑重算，不讀 spec 指定的 yaml。

相對路徑以執行指令時的工作目錄解析；在其他工作目錄執行時另以 repo 根目錄解析一次。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

PKG_DIR = Path(__file__).resolve().parents[1]  # draftagent/
REPO_ROOT = PKG_DIR.parent
LOCK_MSG = "法規數值只能讀 repo 的 codes/*.yaml"


def _candidates(p: str | Path) -> list[Path]:
    p = Path(p)
    if p.is_absolute():
        return [p.resolve()]
    return [p.resolve(), (REPO_ROOT / p).resolve()]


def is_repo_codes_path(p: str | Path) -> bool:
    """p 是否為 draftagent/<pkg>/codes/*.yaml（resolve 後，含符號連結展開）。"""
    for c in _candidates(p):
        if (
            c.suffix.lower() in (".yaml", ".yml")
            and c.parent.name == "codes"
            and c.parent.parent.parent == PKG_DIR
        ):
            return True
    return False


def validate_codes_path(v: Any) -> Any:
    """pydantic field validator 用：不在 draftagent/*/codes/ 下 → ValueError。"""
    if v is None:
        return v
    if not is_repo_codes_path(v):
        raise ValueError(f"{v} 不在 draftagent/*/codes/ 下：{LOCK_MSG}")
    return v


def same_path(a: str | Path, b: str | Path) -> bool:
    bb = Path(b).resolve()
    return any(c == bb for c in _candidates(a))


def assert_default_codes(spec_value: Any, default_path: str | Path, res: Any, field: str) -> bool:
    """spec 的 codes 路徑 ≠ repo 預設（resolve 後比較）→ res.error；回傳是否一致。

    `res` 為 VerifyResult（有 .error / .metrics）。spec_value 為 None 視為預設。
    呼叫端之後一律用 default_path 重算，不採用 spec 指定的檔案。
    """
    if spec_value is None or same_path(spec_value, default_path):
        return True
    res.error(f"spec.{field} = {spec_value} ≠ repo 預設 {Path(default_path).resolve()}：{LOCK_MSG}")
    return False
