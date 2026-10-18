"""把 Pydantic ValidationError 轉成給 agent / 人看的友善訊息（欄位路徑 + 錯誤 + 允許值 / 範圍）。"""

from __future__ import annotations

import types
import typing
from typing import Any

from pydantic import BaseModel, ValidationError
from pydantic.fields import FieldInfo


class SpecLoadError(ValueError):
    """spec 檔讀不進來（格式、路徑、圖種判斷）。訊息已是友善格式。"""


def format_loc(loc: tuple[Any, ...]) -> str:
    out = ""
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += ("." if out else "") + str(part)
    return out or "(整份 spec)"


def _unwrap(t: Any) -> list[Any]:
    """把 X | None、Annotated 等拆成候選型別。"""
    origin = typing.get_origin(t)
    if origin is typing.Annotated:
        return _unwrap(typing.get_args(t)[0])
    if origin in (typing.Union, types.UnionType):
        return [a for arg in typing.get_args(t) if arg is not type(None) for a in _unwrap(arg)]
    return [t]


def _is_model(t: Any) -> bool:
    return isinstance(t, type) and issubclass(t, BaseModel)


def _walk(model: type[BaseModel], loc: tuple[Any, ...]) -> tuple[type[BaseModel] | None, FieldInfo | None]:
    """沿 loc 找到 (所在 model, 該欄位 FieldInfo)。找不到最後一段時 FieldInfo 為 None。"""
    t: Any = model
    container: type[BaseModel] | None = None
    fi: FieldInfo | None = None
    for part in loc:
        cands = _unwrap(t)
        if isinstance(part, int):
            nxt = None
            for c in cands:
                origin = typing.get_origin(c)
                args = typing.get_args(c)
                if not args:
                    continue
                if origin is tuple and not (len(args) == 2 and args[1] is ...):
                    nxt = args[min(part, len(args) - 1)]
                    break
                if origin in (list, tuple, set, frozenset):
                    nxt = args[0]
                    break
            if nxt is None:
                return container, fi
            t = nxt
            continue
        models = [c for c in cands if _is_model(c)]
        hit = next((m for m in models if part in m.model_fields), None)
        if hit is None:
            return (models[0] if models else container), None
        container, fi = hit, hit.model_fields[part]
        t = fi.annotation
    return container, fi


def _hint(err: dict[str, Any], container: type[BaseModel] | None, fi: FieldInfo | None) -> list[str]:
    typ = err.get("type", "")
    ctx = err.get("ctx") or {}
    hints: list[str] = []
    if typ == "extra_forbidden" and container is not None:
        hints.append(f"不認得的欄位；{container.__name__} 允許：{', '.join(container.model_fields)}")
    elif typ == "missing":
        hints.append("必填欄位")
    if "expected" in ctx:
        hints.append(f"允許值：{ctx['expected']}")
    rng = [f"{sym} {ctx[k]}" for k, sym in (("gt", ">"), ("ge", "≥"), ("lt", "<"), ("le", "≤"))
           if k in ctx]
    if rng:
        hints.append("範圍：" + "、".join(rng))
    if "min_length" in ctx:
        hints.append(f"至少 {ctx['min_length']} 項")
    if fi is not None:
        if fi.description:
            hints.append(f"說明：{fi.description}")
        if typ not in ("missing", "extra_forbidden"):
            a = fi.annotation
            ann = a.__name__ if isinstance(a, type) else str(a)
            hints.append(f"型別：{ann.replace('typing.', '').replace('pathlib.', '')}")
    if typ != "missing" and "input" in err and not isinstance(err["input"], dict | list):
        r = repr(err["input"])
        hints.append(f"輸入：{r if len(r) <= 60 else r[:57] + '...'}")
    return hints


def _model_by_name(name: str) -> type[BaseModel] | None:
    from draftagent import registry

    seen: set[type] = set()
    stack: list[Any] = [k.spec for k in registry.all_kinds()]
    while stack:
        t = stack.pop()
        if not _is_model(t) or t in seen:
            continue
        seen.add(t)
        if t.__name__ == name:
            return t
        for f in t.model_fields.values():
            for c in _unwrap(f.annotation):
                stack.append(c)
                stack.extend(typing.get_args(c))
    return None


def format_validation_error(e: ValidationError, model: type[BaseModel] | None = None) -> str:
    model = model or _model_by_name(e.title)
    errs = e.errors()
    lines = [f"{e.title} 驗證失敗（{len(errs)} 個錯誤）："]
    for err in errs:
        loc = tuple(err.get("loc", ()))
        msg = str(err.get("msg", "")).removeprefix("Value error, ").removeprefix("Assertion failed, ")
        container, fi = _walk(model, loc) if model is not None else (None, None)
        lines.append(f"  - {format_loc(loc)}：{msg}")
        lines += [f"      {h}" for h in _hint(err, container, fi)]
    if model is not None:
        from draftagent import registry

        kind = next((k.name for k in registry.all_kinds() if k.spec is model), "<kind>")
        lines.append(f"  完整欄位與允許值：draftagent schema {kind}")
    return "\n".join(lines)


def validation_details(e: ValidationError) -> list[dict[str, Any]]:
    """--json 用：每個錯誤的 loc / msg / type / ctx。"""
    return [
        {
            "loc": format_loc(tuple(err.get("loc", ()))),
            "msg": str(err.get("msg", "")).removeprefix("Value error, "),
            "type": err.get("type"),
            "ctx": {k: str(v) for k, v in (err.get("ctx") or {}).items()},
        }
        for err in e.errors()
    ]
