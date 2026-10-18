"""範例 spec 洩題檢查：`examples/*.json` 不得等於（或關鍵數值等於）任何考題 `input/spec.json`。

`draftagent new <kind>` 讀 `examples/<kind>.json`，評估工作區會保留 examples/。所以範例必須是
「另一題」：可以示範欄位寫法，但不能讓 agent 照抄就拿到考題答案。

三道檢查（任一命中 = 洩題）：
1. **內容相同**：example 與考題 spec 的 JSON（排序鍵後）完全相同。
2. **關鍵欄位相同**（同圖種才比）：`KEY_FIELDS[kind]` 列出「決定答案」的欄位路徑
   （例 plan 的房間尺寸 / 牆厚 / 開口位置），全部相同 → 洩題。路徑語法 `a.b[*].c`；
   兩邊都取不到值的欄位不計，至少要有一個欄位有值才判定。
3. **數值重疊度**（同圖種才比）：example 的非平凡數值葉節點（排除 `TRIVIAL_NUMBERS`）中，
   出現在考題 spec 的比例 ≥ `OVERLAP_MAX` 且至少 `OVERLAP_MIN_COUNT` 個 → 洩題
   （抓「只改一個欄位」的近似抄本）。

圖種對應：example 取檔名（`examples/<kind>.json`）；考題取 `tasks.TASKS[task].kinds`。
純函式、不呼叫 LLM。CLI：`python -m draftagent.eval.leakcheck [--examples DIR] [--tests DIR]`。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES_DIR = REPO_ROOT / "examples"
TESTS_DIR = REPO_ROOT / "tests"

# 每個圖種「決定答案」的欄位（JSON 路徑）。新增圖種時補這裡；沒列的圖種只做檢查 1、3。
KEY_FIELDS: dict[str, tuple[str, ...]] = {
    "plan": (
        "wall_thickness",
        "rooms[*].rect.width",
        "rooms[*].rect.depth",
        "rooms[*].polygon",
        "openings[*].side",
        "openings[*].offset",
        "openings[*].width",
    ),
    "site-overlay": (
        "parcels[*].section_name",
        "parcels[*].section_code",
        "parcels[*].land_numbers",
        "cadastre_path",
        "expected_area_m2",
    ),
    "slope": ("dtm_path", "site_path"),
    "drainage-calc-ty": (
        "site_area_m2",
        "district",
        "subareas[*].area_m2",
        "storage.cells",
        "storage.effective_depth_m",
        "outlet",
    ),
    "drainage-profile": ("nodes[*].station_m", "nodes[*].invert_el"),
    "outflow-control": ("site.area_ha", "horner.params", "detention.layers", "outlet"),
    "drainage-plan": ("site_boundary", "nodes", "subcatchments"),
    "drainage-detail": ("details",),
    "floor-plan": ("outline", "wall_thickness", "rooms[*].rect", "rooms[*].polygon"),
}

# 太常見、不具辨識力的數值（不列入重疊度計算）
TRIVIAL_NUMBERS = frozenset({0.0, 1.0, 2.0, 0.5, 10.0, 100.0, 1000.0, 3826.0})
OVERLAP_MAX = 0.6
OVERLAP_MIN_COUNT = 4


@dataclass(frozen=True)
class Finding:
    example: str
    task: str
    kind: str
    reason: str

    def __str__(self) -> str:
        return f"{self.example} ↔ {self.task}（{self.kind}）：{self.reason}"


def extract(data: Any, path: str) -> list[Any]:
    """取 `a.b[*].c` 路徑的所有值（取不到的分支略過）。"""
    cur = [data]
    for part in path.split("."):
        star = part.endswith("[*]")
        key = part[:-3] if star else part
        nxt: list[Any] = []
        for v in cur:
            if isinstance(v, dict) and key in v:
                w = v[key]
                if star:
                    if isinstance(w, list):
                        nxt.extend(w)
                else:
                    nxt.append(w)
        cur = nxt
    return cur


def _canon(v: Any) -> str:
    return json.dumps(_normalize(v), sort_keys=True, ensure_ascii=False)


def _normalize(v: Any) -> Any:
    if isinstance(v, bool) or v is None or isinstance(v, str):
        return v
    if isinstance(v, int | float):
        return float(v)
    if isinstance(v, dict):
        return {str(k): _normalize(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_normalize(x) for x in v]
    return str(v)


def numeric_leaves(data: Any) -> set[float]:
    out: set[float] = set()
    if isinstance(data, bool):
        return out
    if isinstance(data, int | float):
        out.add(round(float(data), 9))
    elif isinstance(data, dict):
        for v in data.values():
            out |= numeric_leaves(v)
    elif isinstance(data, list):
        for v in data:
            out |= numeric_leaves(v)
    return out


def overlap(example: Any, task: Any) -> tuple[float, int, int]:
    """(重疊比例, 重疊個數, example 非平凡數值個數)。"""
    e = numeric_leaves(example) - TRIVIAL_NUMBERS
    t = numeric_leaves(task) - TRIVIAL_NUMBERS
    if not e:
        return 0.0, 0, 0
    n = len(e & t)
    return n / len(e), n, len(e)


def key_fields_equal(kind: str, example: Any, task: Any) -> list[str] | None:
    """所有關鍵欄位相同 → 回傳有值的欄位清單；否則 None。"""
    fields = KEY_FIELDS.get(kind)
    if not fields:
        return None
    present: list[str] = []
    for f in fields:
        a, b = extract(example, f), extract(task, f)
        if not a and not b:
            continue
        if _canon(a) != _canon(b):
            return None
        present.append(f)
    return present or None


def compare(kind: str, example: Any, task: Any) -> list[str]:
    """一對 example / 考題 spec 的洩題理由（空 = 沒問題）。"""
    reasons: list[str] = []
    if _canon(example) == _canon(task):
        reasons.append("JSON 內容完全相同")
    same = key_fields_equal(kind, example, task)
    if same:
        reasons.append(f"關鍵欄位全部相同：{', '.join(same)}")
    ratio, n, total = overlap(example, task)
    if n >= OVERLAP_MIN_COUNT and ratio >= OVERLAP_MAX:
        reasons.append(f"數值重疊 {n}/{total}（{ratio:.0%} ≥ {OVERLAP_MAX:.0%}）")
    return reasons


def task_specs(tests_dir: Path = TESTS_DIR) -> list[tuple[str, tuple[str, ...], Any]]:
    """[(task id, kinds, spec data)]：所有 tests/level*/*/input/spec.json 與 spec-N.json（多 build 題）。"""
    from draftagent.eval.tasks import TASKS

    out = []
    for p in sorted(Path(tests_dir).glob("level*/*/input/spec*.json")):
        tid = p.parent.parent.relative_to(tests_dir).as_posix()
        td = TASKS.get(tid)
        kinds = td.kinds if td else ()
        out.append((tid, kinds, json.loads(p.read_text(encoding="utf-8"))))
    return out


def check(examples_dir: Path = EXAMPLES_DIR, tests_dir: Path = TESTS_DIR) -> list[Finding]:
    examples_dir, tests_dir = Path(examples_dir), Path(tests_dir)
    tasks = task_specs(tests_dir)
    findings: list[Finding] = []
    for ep in sorted(examples_dir.glob("*.json")):
        kind = ep.stem
        ex = json.loads(ep.read_text(encoding="utf-8"))
        rel = ep.name
        for tid, kinds, spec in tasks:
            if kind in kinds:
                reasons = compare(kind, ex, spec)
            elif _canon(ex) == _canon(spec):
                reasons = ["JSON 內容完全相同"]
            else:
                reasons = []
            findings += [Finding(rel, tid, kind, r) for r in reasons]
    return findings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="examples/*.json 與考題 input/spec.json 洩題檢查")
    ap.add_argument("--examples", type=Path, default=EXAMPLES_DIR)
    ap.add_argument("--tests", type=Path, default=TESTS_DIR)
    a = ap.parse_args(argv)
    findings = check(a.examples, a.tests)
    for f in findings:
        print(f"LEAK  {f}")
    n = len(list(a.examples.glob("*.json")))
    print(f"leakcheck: {n} 個範例，{len(findings)} 個命中")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
