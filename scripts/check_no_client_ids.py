#!/usr/bin/env python3
"""掃描 repo 文字檔，找疑似真實案件識別資訊（地號、公司名、案件編號）。

用途：防止把真實案件的地號、委託單位、顧問公司、案號誤 commit 進 repo。
命中 → 印出清單並 exit 1；全部乾淨或都在白名單內 → exit 0。

白名單：scripts/client_id_allowlist.txt，每行 `<path-glob>:<regex>`
- path-glob 以 repo 根目錄為基準（fnmatch，`*` 可跨目錄）
- regex 在「命中所在的整行」上 search；符合就放行
- `#` 開頭為註解，空行略過

用法：python scripts/check_no_client_ids.py [--root DIR] [--allowlist FILE] [paths ...]
（給 paths 時只掃那些檔案，供 pre-commit 使用）
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ALLOWLIST = ROOT / "scripts" / "client_id_allowlist.txt"

EXCLUDE_DIRS = {
    ".git",
    ".venv",
    "venv",
    "runs",
    "__pycache__",
    ".ruff_cache",
    ".pytest_cache",
    ".hypothesis",
    ".mypy_cache",
    "node_modules",
    "dist",
    "build",
}
# 本工具與白名單本身含 pattern 字面，不掃
SELF_FILES = {"scripts/check_no_client_ids.py", "scripts/client_id_allowlist.txt"}
BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".tif", ".tiff", ".gpkg", ".shp", ".shx",
    ".dwg", ".ifc", ".zip", ".gz", ".whl", ".pyc", ".so", ".dylib", ".ttf", ".ttc", ".otf",
    ".sqlite", ".db", ".xlsx", ".docx", ".pptx",
}  # fmt: skip
MAX_BYTES = 5_000_000

_CJK = r"[一-鿿]"
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("地號（數字+地號）", re.compile(r"\d+(?:-\d+)?\s*地號")),
    ("地號（段+數字+地號）", re.compile(r"段\s*\d+(?:-\d*)?\s*地號")),
    ("地號（地號+數字）", re.compile(r"地號\s*[:：]?\s*\d+(?:-\d+)?")),
    ("顧問公司", re.compile(_CJK + r"{2,}(?:工程)?(?:技術)?顧問(?:股份)?有限公司")),
    ("顧問公司（簡稱）", re.compile(_CJK + r"{2,}工程技術顧問(?!(?:股份)?有限公司)")),
    ("建築師事務所", re.compile(_CJK + r"{2,}建築師事務所")),
    ("營造 / 測量 / 工程公司", re.compile(_CJK + r"{2,}(?:營造|測量|工程)(?:股份)?有限公司")),
    ("案件編號", re.compile(r"(?<![A-Za-z0-9])C\d{5}[A-Z]{2}(?![A-Za-z0-9])")),
]


@dataclass(frozen=True)
class Hit:
    path: str
    line_no: int
    kind: str
    match: str
    line: str

    def render(self) -> str:
        return (
            f"{self.path}:{self.line_no}: [{self.kind}] {self.match!r}  | {self.line.strip()[:160]}"
        )


def load_allowlist(path: Path) -> list[tuple[str, re.Pattern[str]]]:
    rules: list[tuple[str, re.Pattern[str]]] = []
    if not path.exists():
        return rules
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        glob, sep, rx = line.partition(":")
        if not sep or not glob or not rx:
            raise SystemExit(f"{path}:{n}: 格式應為 <path-glob>:<regex>，得到 {raw!r}")
        rules.append((glob.strip(), re.compile(rx.strip())))
    return rules


def _decode(path: Path) -> str | None:
    data = path.read_bytes()[:MAX_BYTES]
    if path.suffix.lower() == ".dbf":
        # shapefile 屬性表：NUL 換空白後依 .cpg 解碼（真實地籍最常從這裡漏出去）
        cpg = path.with_suffix(".cpg")
        enc = cpg.read_text(encoding="ascii", errors="replace").strip() if cpg.exists() else "utf-8"
        try:
            return data.replace(b"\x00", b" ").decode(enc, errors="ignore")
        except LookupError:
            return data.replace(b"\x00", b" ").decode("utf-8", errors="ignore")
    if b"\x00" in data[:8192]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return data.decode("big5")
        except UnicodeDecodeError:
            return None


def iter_files(root: Path):
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if any(part in EXCLUDE_DIRS for part in rel.parts[:-1]) or not p.is_file():
            continue
        if p.suffix.lower() in BINARY_SUFFIXES:
            continue
        yield p


def scan_file(path: Path, rel: str) -> list[Hit]:
    text = _decode(path)
    if text is None:
        return []
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        for kind, rx in PATTERNS:
            for m in rx.finditer(line):
                hits.append(Hit(rel, n, kind, m.group(0), line))
    return hits


def allowed(hit: Hit, rules: list[tuple[str, re.Pattern[str]]]) -> bool:
    return any(fnmatch.fnmatch(hit.path, g) and rx.search(hit.line) for g, rx in rules)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("paths", nargs="*", help="只掃這些檔案（預設掃整個 repo）")
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--allowlist", type=Path, default=DEFAULT_ALLOWLIST)
    args = ap.parse_args(argv)

    root = args.root.resolve()
    rules = load_allowlist(args.allowlist)
    if args.paths:
        files = [Path(p).resolve() for p in args.paths]
        files = [f for f in files if f.is_file() and f.suffix.lower() not in BINARY_SUFFIXES]
    else:
        files = list(iter_files(root))

    bad: list[Hit] = []
    n_allowed = 0
    for f in files:
        try:
            rel = f.relative_to(root).as_posix()
        except ValueError:
            rel = f.as_posix()
        if rel in SELF_FILES or any(part in EXCLUDE_DIRS for part in Path(rel).parts[:-1]):
            continue
        for h in scan_file(f, rel):
            if allowed(h, rules):
                n_allowed += 1
            else:
                bad.append(h)

    if bad:
        print(f"check_no_client_ids: {len(bad)} 個疑似真實案件識別資訊（白名單放行 {n_allowed}）：")
        for h in bad:
            print("  " + h.render())
        print(
            "\n若為合成 / 示範資料，請在 scripts/client_id_allowlist.txt 加 `<path-glob>:<regex>`；"
            "若為真實案件資料，請移除後再 commit。"
        )
        return 1
    print(f"check_no_client_ids: OK（掃描 {len(files)} 檔，白名單放行 {n_allowed}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
