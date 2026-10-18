"""圖種註冊表：把「spec 型別 → 產圖 → 驗證」綁成一個 kind，讓 API / CLI 一鍵 build。

每個領域模組在 import 時呼叫 `register(...)`；`draftagent.build` 與 CLI 只認這張表。
render / verify 仍是純函式，不得呼叫 LLM。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from draftagent.verify.base import VerifyResult

# render(spec, out_dir) -> {"<artifact name>": Path}，例如 {"dxf": .../plan.dxf}
RenderFn = Callable[[Any, Path], dict[str, Path]]
# verify(spec, artifacts) -> VerifyResult
VerifyFn = Callable[[Any, dict[str, Path]], VerifyResult]


@dataclass(frozen=True)
class Kind:
    name: str  # CLI 用的短名，例如 "plan"、"drainage-profile"
    spec: type[BaseModel]
    render: RenderFn
    verifiers: tuple[VerifyFn, ...]
    summary: str  # 一行說明（給 `draftagent kinds`）
    example: Path | None = None  # 範例 spec（給 `draftagent new`）
    tags: tuple[str, ...] = field(default_factory=tuple)


_KINDS: dict[str, Kind] = {}


def register(kind: Kind) -> Kind:
    if kind.name in _KINDS and _KINDS[kind.name] is not kind:
        raise ValueError(f"kind {kind.name!r} 已註冊")
    _KINDS[kind.name] = kind
    return kind


def get(name: str) -> Kind:
    _load_builtin()
    try:
        return _KINDS[name]
    except KeyError:
        raise KeyError(f"未知圖種 {name!r}；可用：{', '.join(sorted(_KINDS))}") from None


def for_spec(spec: BaseModel) -> Kind:
    _load_builtin()
    for k in _KINDS.values():
        if type(spec) is k.spec:
            return k
    raise KeyError(f"{type(spec).__name__} 沒有註冊成圖種")


def all_kinds() -> list[Kind]:
    _load_builtin()
    return sorted(_KINDS.values(), key=lambda k: k.name)


def _load_builtin() -> None:
    # 各領域在 draftagent/kinds/*.py 註冊；import 即生效
    import importlib
    import pkgutil

    import draftagent.kinds as pkg

    for m in pkgutil.iter_modules(pkg.__path__):
        importlib.import_module(f"{pkg.__name__}.{m.name}")
