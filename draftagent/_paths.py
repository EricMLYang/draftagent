"""跨平台路徑小工具：venv 執行檔位置（POSIX `.venv/bin/x`；Windows `.venv\\Scripts\\x.exe`）。"""

from __future__ import annotations

import os
from pathlib import Path


def is_windows() -> bool:
    return os.name == "nt"


def venv_bin_dir(venv: str | Path, *, windows: bool | None = None) -> Path:
    """venv 目錄下放執行檔的資料夾。`venv` 是 `.venv` 本身（不是 repo 根目錄）。"""
    win = is_windows() if windows is None else windows
    return Path(venv) / ("Scripts" if win else "bin")


def venv_bin(root: str | Path, name: str, *, windows: bool | None = None) -> Path:
    """`<root>/.venv` 內名為 name 的執行檔路徑（Windows 自動補 .exe）。"""
    win = is_windows() if windows is None else windows
    exe = f"{name}.exe" if win and not name.lower().endswith(".exe") else name
    return venv_bin_dir(Path(root) / ".venv", windows=win) / exe


def is_within(child: str | Path, parent: str | Path) -> bool:
    """child 是否在 parent 底下（兩者都先 resolve()）。

    Windows：磁碟代號 / 大小寫不敏感（`c:\\Repo` == `C:\\repo`）、8.3 短檔名與 symlink 由
    resolve() 展開；不同磁碟代號一律 False。POSIX：照字面（大小寫敏感）比較。
    """
    c, p = Path(child).resolve(), Path(parent).resolve()
    if is_windows():
        nc, np_ = os.path.normcase(str(c)), os.path.normcase(str(p)).rstrip("\\/")
        return nc == np_ or nc.startswith(np_ + os.sep)
    return c.is_relative_to(p)
