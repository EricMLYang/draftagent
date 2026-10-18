#!/usr/bin/env python3
"""掃描 `.claude/skills/` 是否含評估考題的答案數值或完整範例 spec。

skills 會整包載入評估工作區；若 skill 裡寫著與考題答案相同的數字（例如某題的總面積、門的 offset），
level1 / level2 的 pass rate 會偏樂觀。本工具列出可疑命中，命中 → exit 1。

答案數值（來源）：
- 每個 `tests/level*/*/expected/*.json`（expected.json、original_spec.json…）的數值葉節點，與字串值裡出現的數字 token
- 每個 `tests/level*/*/input/spec*.json` 的數值葉節點（考題輸入，抄了就等於抄答案；多 build 題有 spec-2.json…）

不列入（太常見或屬法規 / 慣例常數，出現在 skill 裡是正常的）：
- `COMMON`：0、1、100、0.5、3826 等常見值，及 `abs(x) < SMALL_INT_MAX` 的整數
- `draftagent/*/codes/*.yaml` 與 `examples/**/*.json` 裡出現的所有數字（法規常數、範例值）

另檢查 skill 內 ```json 區塊：可解析且與任一考題 spec 完全相同，或非平凡數值與考題重疊
≥ `BLOCK_OVERLAP_MAX` → 命中（完整範例 spec 外洩）。

排除 skill：`EXCLUDED_SKILLS`（目前無）。
只用標準函式庫（CI lint job 不裝依賴）。

用法：python scripts/check_skill_leaks.py [--root DIR] [--verbose]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_SKILLS: set[str] = set()
# 常見值、EPSG 碼、TM2 中央經線 121 / 119、坪換算係數
COMMON = {
    0.0, 0.5, 1.0, 2.0, 3.0, 4.0, 5.0, 10.0, 20.0, 50.0, 100.0, 1000.0,
    3825.0, 3826.0, 3827.0, 3828.0, 4326.0, 3857.0, 121.0, 119.0, 0.3025,
    0.05,  # 容差（level4 面積 ±5% = prompt 明文；skill 的 E3「溝頂 + 0.05 m」是 verifier 常數）
}  # fmt: skip
SMALL_INT_MAX = 20  # |x| < 20 的整數不比（太常見：圖號、條號、序號）
BLOCK_OVERLAP_MAX = 0.6
BLOCK_OVERLAP_MIN_COUNT = 4

# 數字 token：前後不可接英數字底線或小數點（避免切到 EPSG:3826 以外的識別字、版本號）
_NUM = re.compile(r"(?<![0-9A-Za-z_.])-?\d+(?:\.\d+)?(?![0-9A-Za-z_]|\.\d)")
_JSON_BLOCK = re.compile(r"```json\s*\n(.*?)```", re.S)


def _f(x: str | float) -> float:
    return round(float(x), 9)


def numbers_in_text(text: str) -> set[float]:
    return {_f(m.group()) for m in _NUM.finditer(text)}


def numeric_leaves(data: object, *, strings: bool = False) -> set[float]:
    out: set[float] = set()
    if isinstance(data, bool) or data is None:
        return out
    if isinstance(data, int | float):
        out.add(_f(data))
    elif isinstance(data, str):
        if strings:
            out |= numbers_in_text(data)
    elif isinstance(data, dict):
        for v in data.values():
            out |= numeric_leaves(v, strings=strings)
    elif isinstance(data, list):
        for v in data:
            out |= numeric_leaves(v, strings=strings)
    return out


def trivial(x: float) -> bool:
    return x in COMMON or (x == int(x) and abs(x) < SMALL_INT_MAX)


@dataclass(frozen=True)
class Hit:
    path: str
    line: int
    value: str
    sources: tuple[str, ...]
    kind: str = "number"

    def __str__(self) -> str:
        src = ", ".join(self.sources[:3]) + (" …" if len(self.sources) > 3 else "")
        return f"{self.path}:{self.line}: {self.kind} {self.value}（{src}）"


def answer_numbers(root: Path) -> dict[float, set[str]]:
    ans: dict[float, set[str]] = defaultdict(set)
    tests = root / "tests"
    for p in sorted(tests.glob("level*/*/expected/*.json")):  # expected.json、original_spec.json…
        tid = p.parent.parent.relative_to(tests).as_posix()
        for x in numeric_leaves(json.loads(p.read_text(encoding="utf-8")), strings=True):
            ans[x].add(f"{tid}/expected")
    for p in sorted(
        tests.glob("level*/*/input/spec*.json")
    ):  # spec.json、spec-2.json（多 build 題）
        tid = p.parent.parent.relative_to(tests).as_posix()
        for x in numeric_leaves(json.loads(p.read_text(encoding="utf-8"))):
            ans[x].add(f"{tid}/spec")
    return ans


def allowed_numbers(root: Path) -> set[float]:
    ok: set[float] = set()
    for p in sorted(root.glob("draftagent/**/codes/*.yaml")):
        ok |= numbers_in_text(p.read_text(encoding="utf-8"))
    for p in sorted(root.glob("examples/**/*.json")):
        ok |= numeric_leaves(json.loads(p.read_text(encoding="utf-8")), strings=True)
    return ok


def task_specs(root: Path) -> list[tuple[str, object]]:
    tests = root / "tests"
    return [
        (p.parent.parent.relative_to(tests).as_posix(), json.loads(p.read_text(encoding="utf-8")))
        for p in sorted(tests.glob("level*/*/input/spec*.json"))
    ]


def skill_files(root: Path) -> list[Path]:
    base = root / ".claude" / "skills"
    return [
        p for p in sorted(base.rglob("*.md")) if p.relative_to(base).parts[0] not in EXCLUDED_SKILLS
    ]


def _canon(v: object) -> str:
    return json.dumps(v, sort_keys=True, ensure_ascii=False)


def scan(root: Path = ROOT) -> list[Hit]:
    ans = answer_numbers(root)
    ok = allowed_numbers(root)
    specs = task_specs(root)
    hits: list[Hit] = []
    for p in skill_files(root):
        rel = p.relative_to(root).as_posix()
        text = p.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            for m in _NUM.finditer(line):
                x = _f(m.group())
                if x in ans and not trivial(x) and x not in ok:
                    hits.append(Hit(rel, i, m.group(), tuple(sorted(ans[x]))))
        for m in _JSON_BLOCK.finditer(text):
            try:
                block = json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
            line = text.count("\n", 0, m.start()) + 1
            e = {x for x in numeric_leaves(block) if not trivial(x)}
            for tid, spec in specs:
                if _canon(block) == _canon(spec):
                    hits.append(Hit(rel, line, "```json```", (tid,), kind="完整考題 spec"))
                    continue
                t = {x for x in numeric_leaves(spec) if not trivial(x)}
                n = len(e & t)
                if e and n >= BLOCK_OVERLAP_MIN_COUNT and n / len(e) >= BLOCK_OVERLAP_MAX:
                    hits.append(Hit(rel, line, f"{n}/{len(e)}", (tid,), kind="範例 spec 數值重疊"))
    return hits


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--verbose", action="store_true", help="列出掃描的檔案")
    a = ap.parse_args(argv)
    files = skill_files(a.root)
    if a.verbose:
        for p in files:
            print(f"scan {p.relative_to(a.root)}")
    hits = scan(a.root)
    for h in hits:
        print(h)
    excluded = ", ".join(sorted(EXCLUDED_SKILLS)) or "無"
    print(f"check_skill_leaks: {len(files)} 個 skill 檔（排除 {excluded}），{len(hits)} 個可疑命中")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
