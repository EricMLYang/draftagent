"""kinds/ 共用的薄 adapter 工具（不放領域邏輯）。純函式，不得呼叫 LLM。

既有 verifier 多半吃「spec 檔路徑」：build 會把 spec.json 放進 artifacts["spec"]；
單獨呼叫 verifier（沒有 spec 檔）時才寫一份暫存 spec.json。
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from draftagent.verify.base import VerifyResult

REPO_ROOT = Path(__file__).resolve().parents[2]


def example(rel: str) -> Path:
    return REPO_ROOT / rel


def dump_spec_json(spec: BaseModel) -> str:
    return spec.model_dump_json(indent=2) + "\n"


@contextmanager
def spec_file(spec: BaseModel, artifacts: dict[str, Path]) -> Iterator[Path]:
    """artifacts["spec"] 存在就用它；否則寫暫存 spec.json（離開 with 後刪除）。"""
    if "spec" in artifacts and Path(artifacts["spec"]).exists():
        yield Path(artifacts["spec"])
        return
    with tempfile.TemporaryDirectory(prefix="draftagent-spec-") as d:
        p = Path(d) / "spec.json"
        p.write_text(dump_spec_json(spec), encoding="utf-8")
        yield p


def need(artifacts: dict[str, Path], key: str, name: str) -> Path | VerifyResult:
    """取 artifacts[key]；缺檔時回傳一個紅燈 VerifyResult。"""
    p = artifacts.get(key)
    if p is None or not Path(p).exists():
        res = VerifyResult(name=name)
        res.error(f"找不到產物 {key!r}（artifacts: {sorted(artifacts)}）")
        return res
    return Path(p)


def insunits_verifier(spec: Any, artifacts: dict[str, Path]) -> VerifyResult:
    """DXF $INSUNITS 必須與 spec.units 一致（單位規則：spec 宣告，verifier 檢查）。"""
    import ezdxf

    from draftagent.units import check_insunits

    p = need(artifacts, "dxf", "units_check")
    if isinstance(p, VerifyResult):
        return p
    return check_insunits(ezdxf.readfile(str(p)), spec.units)
