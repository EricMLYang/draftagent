"""Spec 實例 → 可直接執行的 Python 建構式程式碼（給 `draftagent new`）。

只輸出「有被設定過」的欄位（model_fields_set），其餘交給 schema 預設值；
產出的程式碼 exec 後 model_dump() 必須與原 spec 相同（tests/test_build_cli.py 驗證）。
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path, PurePath
from typing import Any

from pydantic import BaseModel

LINE = 88
IND = "    "


class _Imports:
    def __init__(self) -> None:
        self.models: dict[str, set[str]] = {}
        self.path = False

    def add(self, cls: type) -> str:
        mod = importlib.import_module(cls.__module__)
        if getattr(mod, cls.__name__, None) is not cls:  # 非公開 / 動態類別 → 改用 dict
            raise LookupError(cls)
        self.models.setdefault(cls.__module__, set()).add(cls.__name__)
        return cls.__name__

    def lines(self) -> list[str]:
        out = ["from pathlib import Path", ""] if self.path else []
        for mod in sorted(self.models):
            names = sorted(self.models[mod])
            one = f"from {mod} import {', '.join(names)}"
            if len(one) <= LINE:
                out.append(one)
            else:
                out.append(f"from {mod} import (")
                out += [f"{IND}{n}," for n in names]
                out.append(")")
        return out


def _scalar(v: Any, imp: _Imports) -> str | None:
    if v is None or isinstance(v, bool):
        return repr(v)
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() and abs(v) < 1e15 else repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, PurePath):
        imp.path = True
        return f"Path({json.dumps(v.as_posix(), ensure_ascii=False)})"
    return None


def _join(open_: str, items: list[str], close: str, indent: int, trailing_one: bool = False) -> str:
    if not items:
        return open_ + close
    flat = open_ + ", ".join(items) + ("," if trailing_one and len(items) == 1 else "") + close
    if "\n" not in flat and len(IND * indent) + len(flat) <= LINE:
        return flat
    pad = IND * (indent + 1)
    body = "".join(f"{pad}{it},\n" for it in items)
    return f"{open_}\n{body}{IND * indent}{close}"


def _expr(v: Any, imp: _Imports, indent: int) -> str:
    s = _scalar(v, imp)
    if s is not None:
        return s
    if isinstance(v, BaseModel):
        cls = type(v)
        try:
            name = imp.add(cls)
        except LookupError:
            return _expr(v.model_dump(mode="json"), imp, indent)
        items = [
            f"{f}={_expr(getattr(v, f), imp, indent + 1)}"
            for f in cls.model_fields
            if f in v.model_fields_set
        ]
        return _join(f"{name}(", items, ")", indent)
    if isinstance(v, list):
        return _join("[", [_expr(x, imp, indent + 1) for x in v], "]", indent)
    if isinstance(v, tuple):
        return _join("(", [_expr(x, imp, indent + 1) for x in v], ")", indent, trailing_one=True)
    if isinstance(v, dict):
        items = [f"{_expr(k, imp, indent + 1)}: {_expr(x, imp, indent + 1)}" for k, x in v.items()]
        return _join("{", items, "}", indent)
    raise TypeError(f"codegen 不支援型別 {type(v).__name__}")


def spec_to_python(
    spec: BaseModel, *, kind: str, summary: str = "", source: str | Path | None = None
) -> str:
    imp = _Imports()
    body = _expr(spec, imp, 0)
    src = f"\n由 `draftagent new {kind}` 從範例 {source} 產生。" if source else ""
    doc = (
        f'"""draftagent spec：{kind} — {summary}\n{src}\n'
        f"修改下面的欄位後執行（會自動存 spec.json、跑 verifier、寫 runs/）：\n\n"
        f"    draftagent build <this file>\n\n"
        f"欄位與允許值：draftagent schema {kind}\n"
        f'"""'
    )
    main = (
        'if __name__ == "__main__":\n'
        f"{IND}from draftagent import build\n\n"
        f"{IND}result = build(spec)\n"
        f"{IND}print(result)\n"
        f"{IND}raise SystemExit(0 if result.ok else 1)\n"
    )
    return "\n".join([doc, "", *imp.lines(), "", f"spec = {body}", "", "", main])
