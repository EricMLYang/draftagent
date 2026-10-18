"""隔離工作區：複製 repo 到 `tempfile.mkdtemp(prefix="draftagent-eval-")`，移除答案。

移除（防止 agent 偷看答案）：
- 所有 `tests/level*/*/expected/` 與 `tests/level*/*/input/spec.json`
  （input/ 其他原始圖資 shp / tif / gpkg 保留，1-2、1-3 需要）
- `runs/`、`work/`、`.git/`（不能 `git show` 出被刪的答案）、`docs/RESULTS.md`、`docs/EVAL.md`
  （評估說明含 level3 各題的陷阱，agent 不需要）
- 評估 harness 自身（`draftagent/eval/`、`tools/eval.py`、`tests/test_eval.py`）
- 會直接算出 / 斷言標準答案的檔案：`tests/test_*.py`、`tests/property/`、`tools/make_fixtures_*.py`、
  `tools/make_level3_fixtures.py`（含 level3 的設計答案）
- 多 build 題的參考答案 `tests/level*/*/input/spec-N.json`
- 參考解答器 `draftagent/eval/reference_solvers.py`（隨 `draftagent/eval/` 移除；`ANSWER_KEY_NAMES`
  另以檔名全樹掃描，防止被複製到別處）與它的測試 `tests/test_task_solvable.py`

保留：各題 `input/` 下的**原始資料**（shp / tif / gpkg、survey.csv、nodes.csv、site.geojson、
design.md…）——題目要 agent 從這些原始資料寫 spec，`input/spec.json` 只是參考答案。

保留 `examples/`（`draftagent new <kind>` 的範例 spec 與合成圖資）：它們刻意與考題不同，
`leaks()` 會以 `leakcheck.check()` 確認沒有任何範例等於（或關鍵數值等於）考題 spec。

venv：不重新安裝。工作區 `.venv/` 是一個「覆蓋式」venv（約 1 秒）：
`bin/python` 指向原 venv 的同一個基底直譯器、`site-packages/_draftagent_eval.pth`
依序列出「工作區根目錄」與「原 venv 的 site-packages」。原 venv 的 editable `.pth`
不會被處理（.pth 只在 site-packages 本身被處理），所以 `import draftagent` 一定拿到
**工作區**的程式碼，`draftagent build` 的 runs/ 也寫在工作區。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

_IGNORE_NAMES = {
    ".git",
    ".venv",
    "runs",
    "work",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".DS_Store",
    "node_modules",
    ".hypothesis",
}
# 參考解答器（只讀原始資料就能機械地寫出答案 spec）：檔名在工作區任何位置出現都算洩題
ANSWER_KEY_NAMES = ("reference_solvers.py", "test_task_solvable.py")
_REMOVE_REL = (
    "draftagent/eval",
    "draftagent/eval/reference_solvers.py",
    "tests/test_task_solvable.py",
    "tools/eval.py",
    "tests/test_eval.py",
    "docs/RESULTS.md",
    "docs/EVAL.md",  # 含 level3 各題的陷阱與評分方式
    "tests/property",
    "tools/make_level3_fixtures.py",
)
_REMOVE_GLOBS = (
    "tests/level*/*/expected",
    "tests/level*/*/input/spec.json",
    "tests/level*/*/input/spec-*.json",
    "tests/test_*.py",
    "tools/make_fixtures_*.py",
)

_DRAFTAGENT_SCRIPT = """#!{python}
import sys
from draftagent.cli import app
if __name__ == "__main__":
    sys.exit(app())
"""
_PYTEST_SCRIPT = """#!{python}
import sys
import pytest
if __name__ == "__main__":
    sys.exit(pytest.console_main())
"""


@dataclass
class Workspace:
    root: Path
    source: Path
    removed: list[str] = field(default_factory=list)
    snapshot: dict[str, str] = field(default_factory=dict)  # draftagent/ 檔案 sha256

    @property
    def venv_bin(self) -> Path:
        return self.root / ".venv" / "bin"

    @property
    def runs(self) -> Path:
        return self.root / "runs"

    def env(self) -> dict[str, str]:
        """給 agent 子行程的環境：工作區 venv 在 PATH 最前面，清掉會干擾的變數。"""
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "CONDA_PREFIX")
        }
        env["VIRTUAL_ENV"] = str(self.root / ".venv")
        env["PATH"] = f"{self.venv_bin}{os.pathsep}{env.get('PATH', '')}"
        env["DRAFTAGENT_EVAL"] = "1"
        return env

    def changed_files(self) -> dict[str, list[str]]:
        """與建立時相比，draftagent/ 下被改動 / 新增 / 刪除的檔案。"""
        now = hash_tree(self.root / "draftagent", self.root)
        before = self.snapshot
        return {
            "modified": sorted(k for k in now.keys() & before.keys() if now[k] != before[k]),
            "added": sorted(now.keys() - before.keys()),
            "deleted": sorted(before.keys() - now.keys()),
        }

    def protected_changes(self) -> list[str]:
        ch = self.changed_files()
        return [f for f in ch["modified"] + ch["added"] + ch["deleted"] if is_protected(f)]

    def cleanup(self) -> None:
        shutil.rmtree(self.root.parent, ignore_errors=True)


def is_protected(rel: str) -> bool:
    """draftagent/verify/ 底下，或 draftagent/ 任一層 codes/ 底下（法規 yaml）。"""
    parts = rel.split("/")
    return parts[0] == "draftagent" and (parts[1:2] == ["verify"] or "codes" in parts[1:-1])


def hash_tree(d: Path, base: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not d.exists():
        return out
    for p in sorted(d.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc":
            out[p.relative_to(base).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _ignore(_dir: str, names: list[str]) -> set[str]:
    return {n for n in names if n in _IGNORE_NAMES or n.endswith(".pyc")}


def _make_venv(root: Path, source_venv: Path) -> None:
    site = _site_packages(source_venv)
    base = Path(sys.base_prefix)
    py = source_venv / "bin" / "python"
    real_py = py.resolve() if py.exists() else Path(sys.executable).resolve()
    venv = root / ".venv"
    rel_site = site.relative_to(source_venv)
    (venv / rel_site).mkdir(parents=True)
    (venv / "bin").mkdir(parents=True, exist_ok=True)
    cfg = source_venv / "pyvenv.cfg"
    if cfg.exists():
        shutil.copy2(cfg, venv / "pyvenv.cfg")
    else:
        (venv / "pyvenv.cfg").write_text(
            f"home = {base / 'bin'}\ninclude-system-site-packages = false\n", encoding="utf-8"
        )
    vpy = venv / "bin" / "python"
    vpy.symlink_to(real_py)
    for alias in ("python3", f"python{sys.version_info.major}.{sys.version_info.minor}"):
        (venv / "bin" / alias).symlink_to("python")
    (venv / rel_site / "_draftagent_eval.pth").write_text(f"{root}\n{site}\n", encoding="utf-8")
    for name, tpl in (("draftagent", _DRAFTAGENT_SCRIPT), ("pytest", _PYTEST_SCRIPT)):
        p = venv / "bin" / name
        p.write_text(tpl.format(python=vpy), encoding="utf-8")
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _site_packages(venv: Path) -> Path:
    hits = sorted((venv / "lib").glob("python3*/site-packages"))
    if not hits:
        raise FileNotFoundError(f"{venv} 找不到 site-packages；先 `uv venv && uv pip install -e .`")
    return hits[0]


def create_workspace(
    source: Path = REPO_ROOT, *, venv: bool = True, parent: Path | None = None
) -> Workspace:
    source = Path(source).resolve()
    root = Path(tempfile.mkdtemp(prefix="draftagent-eval-", dir=parent)).resolve() / "repo"
    shutil.copytree(source, root, ignore=_ignore, symlinks=True)
    removed: list[str] = []
    for rel in _REMOVE_REL:
        p = root / rel
        if p.exists():
            shutil.rmtree(p) if p.is_dir() else p.unlink()
            removed.append(rel)
    for g in _REMOVE_GLOBS:
        for p in sorted(root.glob(g)):
            shutil.rmtree(p) if p.is_dir() else p.unlink()
            removed.append(p.relative_to(root).as_posix())
    for name in ANSWER_KEY_NAMES:
        for p in sorted(root.rglob(name)):
            p.unlink()
            removed.append(p.relative_to(root).as_posix())
    (root / "runs").mkdir()
    (root / "work").mkdir()
    if venv:
        _make_venv(root, source / ".venv")
    ws = Workspace(root=root, source=source, removed=removed)
    ws.snapshot = hash_tree(root / "draftagent", root)
    return ws


def leaks(ws: Workspace) -> list[str]:
    """工作區裡不應存在的答案（給測試與 harness 自檢）。

    - 答案檔：`tests/level*/*/expected`、`tests/level*/*/input/spec[-N].json`、`.git`、
      `tools/make_level3_fixtures.py`
    - 參考解答器：任何位置的 `ANSWER_KEY_NAMES`（`reference_solvers.py`、`test_task_solvable.py`）
    - 洩題範例：工作區 `examples/*.json` 與**原 repo** 考題 spec 比對（`leakcheck`）
    """
    from draftagent.eval.leakcheck import check as leakcheck

    bad = [
        p.relative_to(ws.root).as_posix()
        for g in (
            "tests/level*/*/expected",
            "tests/level*/*/input/spec.json",
            "tests/level*/*/input/spec-*.json",
            "tools/make_level3_fixtures.py",
        )
        for p in ws.root.glob(g)
    ]
    if (ws.root / ".git").exists():
        bad.append(".git")
    bad += [
        p.relative_to(ws.root).as_posix()
        for name in ANSWER_KEY_NAMES
        for p in ws.root.rglob(name)
        if ".venv" not in p.relative_to(ws.root).parts
    ]
    bad += [f"examples/{f}" for f in leakcheck(ws.root / "examples", ws.source / "tests")]
    return bad
