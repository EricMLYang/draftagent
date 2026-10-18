"""共用 pytest fixture 與標記處理。

- `tmp_run_dir`：模仿 `runs/<timestamp>/` 的暫存輸出目錄
- `has_cjk_font`：本機是否有 ezdxf / matplotlib 可用的中文字型
- `visual` 標記的測試：Linux 找不到中文字型時自動 skip（CI 以 apt 裝 fonts-noto-cjk；
  Windows 內建微軟正黑體 / 細明體 / 標楷體，macOS 內建 Hiragino / STHeiti）
"""

from __future__ import annotations

import sys
from datetime import datetime
from functools import cache
from pathlib import Path

import pytest


@cache
def _detect_cjk_font() -> str | None:
    """回傳第一個可用的 CJK 字型檔名；沒有則 None。與 render.preview 的 fallback 清單一致。"""
    try:
        from ezdxf.fonts import fonts

        from draftagent.render.preview import CJK_FALLBACK_FILES, cjk_font_paths
    except Exception:  # noqa: BLE001 - 任何 import 失敗都視為沒有字型
        return None
    fm = fonts.font_manager
    for name in CJK_FALLBACK_FILES:
        if fm.has_font(name):
            return name
    # 完整路徑候選：macOS / Windows（%WINDIR%\Fonts 的 msjh、mingliu、kaiu）/ Linux Noto CJK
    return next((p.name for p in cjk_font_paths() if p.exists()), None)


@pytest.fixture(scope="session")
def has_cjk_font() -> bool:
    return _detect_cjk_font() is not None


@pytest.fixture
def tmp_run_dir(tmp_path: Path) -> Path:
    """`<tmp>/runs/<YYYYmmdd-HHMMSS>/`，結構同 agent 的 runs 目錄，測試結束自動清除。"""
    d = tmp_path / "runs" / datetime.now().strftime("%Y%m%d-%H%M%S")
    d.mkdir(parents=True)
    return d


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if not sys.platform.startswith("linux") or _detect_cjk_font() is not None:
        return
    skip = pytest.mark.skip(reason="Linux 無中文字型（apt install fonts-noto-cjk）")
    for item in items:
        if "visual" in item.keywords:
            item.add_marker(skip)
