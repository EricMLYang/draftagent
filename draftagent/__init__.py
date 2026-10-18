"""DraftAgent：給 coding agent 用的台灣領域繪圖工具。

作者介面是 typed Python（或 JSON / YAML）；Pydantic spec 是唯一真相：

    from draftagent import build, PlanSpec, Room, Rect, Opening
    spec = PlanSpec(wall_thickness=200, rooms=[Room(name="房間", rect=Rect(width=6000, depth=4000))])
    print(build(spec))

重型模組（ezdxf、geopandas…）以 lazy `__getattr__` 延遲載入，`import draftagent` 很輕。
"""

from __future__ import annotations

import importlib
from importlib.metadata import PackageNotFoundError, version
from typing import Any

try:
    __version__ = version("draftagent")
except PackageNotFoundError:  # 未安裝（直接從原始碼執行）
    __version__ = "0.0.0+local"

# build 模組本身很輕（只 import pydantic / yaml / registry）。必須 eager import：
# 子模組 draftagent.build 與函式 build 同名，eager 綁定才能保證 `from draftagent import build` 拿到函式。
from draftagent._console import ensure_utf8_stdio
from draftagent.build import BuildFailed, BuildResult, build, load_spec, verify_artifacts
from draftagent.verify.base import VerifyResult

# Windows cp950 console：讓 🟢🔴 / 中文輸出不 UnicodeEncodeError（其他平台 no-op）。
# 放在套件層級，`python -m draftagent.verify.*` 與 tools/ 腳本也一併涵蓋。
ensure_utf8_stdio()

_LAZY: dict[str, str] = {
    # Spec 類別
    "PlanSpec": "draftagent.schema.plan",
    "Room": "draftagent.schema.plan",
    "Rect": "draftagent.schema.plan",
    "Opening": "draftagent.schema.plan",
    "Dimensions": "draftagent.schema.plan",
    "SiteSpec": "draftagent.schema.site",
    "ParcelGroup": "draftagent.schema.site",
    "SlopeSpec": "draftagent.schema.slope",
    "TaoyuanSiteDrainageSpec": "draftagent.schema.drainage",
    "ProfileSpec": "draftagent.schema.profile",
    "ProfileNode": "draftagent.schema.profile",
    "ProfileSegment": "draftagent.schema.profile",
    "ProfileLevel": "draftagent.schema.profile",
    "SheetSpec": "draftagent.schema.sheet",
    "TitleBlock": "draftagent.schema.sheet",
}

__all__ = [
    "BuildFailed",
    "BuildResult",
    "Dimensions",
    "Opening",
    "ParcelGroup",
    "PlanSpec",
    "ProfileLevel",
    "ProfileNode",
    "ProfileSegment",
    "ProfileSpec",
    "Rect",
    "Room",
    "SheetSpec",
    "SiteSpec",
    "SlopeSpec",
    "TaoyuanSiteDrainageSpec",
    "TitleBlock",
    "VerifyResult",
    "__version__",
    "build",
    "load_spec",
    "verify_artifacts",
]


def __getattr__(name: str) -> Any:
    mod = _LAZY.get(name)
    if mod is None:
        raise AttributeError(f"module 'draftagent' has no attribute {name!r}")
    value = getattr(importlib.import_module(mod), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY))
