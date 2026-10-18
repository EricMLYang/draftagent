"""評估主流程：任務 × repeat → 工作區 → runner → grader → runs/eval/<ts>/<task>/<i>/ + docs/RESULTS.md。

每次 run 的輸出目錄：
  prompt.txt        實際送給 agent 的 prompt（含固定前言）
  stream.jsonl      agent 的事件流（claude stream-json / codex --json / replay 的 build 輸出）
  stderr.txt
  final_spec.json   工作區最新 run 的 spec（agent 的「答案」）
  agent_verify.txt  agent 自己那次 build 的 verifier 輸出（僅供參考，不採信）
  regrade/          原 repo 程式重新 build 的產物與 verify.json
  grade.json        狀態、原因、verifier、逐項 expected 比對、統計（turns / tokens / cost）
"""

from __future__ import annotations

import datetime as _dt
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from draftagent.eval.graders import Grade, grade
from draftagent.eval.runners import Runner, RunOutcome, build_prompt
from draftagent.eval.streamjson import RunStats
from draftagent.eval.tasks import Task
from draftagent.eval.workspace import create_workspace, leaks

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_MD = REPO_ROOT / "docs" / "RESULTS.md"
EVAL_ROOT = REPO_ROOT / "runs" / "eval"

RESULTS_HEADER = """# DraftAgent 評估結果

由 `python tools/eval.py run …` 自動 append，每次 run 一列；彙總用 `python tools/eval.py report`。
說明見 docs/EVAL.md。**build 次數**＝該次 agent 執行 `draftagent build` 的次數（1 = 一次就過，
即 PLAN §6 的「修正輪數」；0 = 沒有 build）。tokens in 含快取讀寫。pass 只代表
「通過本 repo 現有 verifier 與 expected 比對」，不代表圖面符合法規。

| 日期 | runner | model | task | pass | 修正輪數(build 次數) | turns | tokens in/out | cost USD | 耗時 s | 備註 |
|---|---|---|---|---|---|---|---|---|---|---|
"""


@dataclass
class RunRecord:
    task: str
    index: int
    runner: str
    model: str | None
    out_dir: Path
    grade: Grade
    outcome: RunOutcome
    started_at: str
    wall_s: float

    @property
    def stats(self) -> RunStats:
        return self.outcome.stats

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "index": self.index,
            "runner": self.runner,
            "model": self.model,
            "started_at": self.started_at,
            "wall_s": round(self.wall_s, 2),
            "status": self.grade.status,
            "passed": self.grade.passed,
            "grade": self.grade.to_dict(),
            "run": self.outcome.to_dict(),
        }


def _stamp() -> str:
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def run_one(
    task: Task,
    runner: Runner,
    out_dir: Path,
    *,
    index: int = 0,
    timeout: float = 900,
    keep_workdir: bool = False,
) -> RunRecord:
    out_dir.mkdir(parents=True, exist_ok=True)
    started = _dt.datetime.now().astimezone().isoformat(timespec="seconds")
    (out_dir / "prompt.txt").write_text(build_prompt(task), encoding="utf-8")
    t0 = time.monotonic()
    ws = create_workspace()
    try:
        bad = leaks(ws)
        if bad:
            raise RuntimeError(f"工作區仍含答案檔：{bad}")
        try:
            outcome = runner.run(task, ws, out_dir, timeout=timeout)
        except Exception as e:  # noqa: BLE001 - runner 崩潰記成 error，不中斷整批
            outcome = RunOutcome(
                RunStats(), None, False, time.monotonic() - t0, error=f"{type(e).__name__}: {e}"
            )
        if outcome.error:
            g = Grade(status="error", reasons=[outcome.error])
        else:
            try:
                g = grade(task, ws, out_dir, outcome.stats)
            except Exception as e:  # noqa: BLE001
                g = Grade(status="error", reasons=[f"grader 例外 {type(e).__name__}: {e}"])
            if outcome.timed_out:
                g.reasons.insert(0, f"agent 超時（{timeout:g}s）被終止")
            if outcome.stats.subtype and outcome.stats.subtype != "success":
                g.reasons.append(f"agent 結束狀態 {outcome.stats.subtype}")
        if keep_workdir:
            (out_dir / "workdir.txt").write_text(str(ws.root) + "\n", encoding="utf-8")
    finally:
        if not keep_workdir:
            ws.cleanup()
    rec = RunRecord(
        task.id,
        index,
        runner.name,
        outcome.stats.model or runner.model,
        out_dir,
        g,
        outcome,
        started,
        time.monotonic() - t0,
    )
    (out_dir / "grade.json").write_text(
        json.dumps(rec.to_dict(), ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    return rec


def run_eval(
    tasks: list[Task],
    runner: Runner,
    *,
    repeat: int = 1,
    timeout: float = 900,
    keep_workdir: bool = False,
    eval_root: Path = EVAL_ROOT,
    results_md: Path | None = RESULTS_MD,
    note: str = "",
    on_record: Any = None,
) -> list[RunRecord]:
    batch = eval_root / _stamp()
    recs: list[RunRecord] = []
    for task in tasks:
        for i in range(repeat):
            rec = run_one(
                task,
                runner,
                batch / task.id / str(i),
                index=i,
                timeout=timeout,
                keep_workdir=keep_workdir,
            )
            recs.append(rec)
            if results_md is not None:
                append_results(results_md, rec, note=note)
            if on_record:
                on_record(rec)
    return recs


# ---------------------------------------------------------------- RESULTS.md


def _k(n: float) -> str:
    if math.isnan(n):
        return "—"
    return f"{n / 1000:.1f}k" if n >= 1000 else str(int(n))


def results_row(rec: RunRecord, note: str = "") -> str:
    st = rec.stats
    cost = f"{st.total_cost_usd:.4f}" if st.total_cost_usd is not None else "—"
    status = rec.grade.status
    remark = [] if status == "pass" else [status]
    if rec.grade.reasons:
        remark.append(rec.grade.reasons[0][:120])
    if note:
        remark.append(note)
    cells = [
        rec.started_at[:16].replace("T", " "),
        rec.runner,
        rec.model or "—",
        rec.task,
        "✅" if rec.grade.passed else "❌",
        str(st.builds),
        str(st.num_turns),
        f"{_k(st.tokens_in)}/{_k(st.tokens_out)}",
        cost,
        f"{rec.wall_s:.0f}",
        "；".join(remark).replace("|", "\\|").replace("\n", " "),
    ]
    return "| " + " | ".join(cells) + " |"


def append_results(path: Path, rec: RunRecord, note: str = "") -> None:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(RESULTS_HEADER, encoding="utf-8")
    with path.open("a", encoding="utf-8") as f:
        f.write(results_row(rec, note) + "\n")


# ---------------------------------------------------------------- report


def load_grades(eval_root: Path = EVAL_ROOT) -> list[dict[str, Any]]:
    out = []
    for p in sorted(eval_root.glob("*/**/grade.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def report(eval_root: Path = EVAL_ROOT, *, runner: str | None = None) -> str:
    """依 (runner, model, task) 彙總：pass rate、平均 build 次數、turns、tokens、cost、耗時。"""
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for g in load_grades(eval_root):
        if runner and g.get("runner") != runner:
            continue
        key = (g.get("runner", "?"), str(g.get("model") or "—"), g.get("task", "?"))
        groups.setdefault(key, []).append(g)
    if not groups:
        return f"（{eval_root} 沒有任何 grade.json）"
    lines = [
        "| runner | model | task | n | pass rate | 平均 build 次數 | 平均 turns | "
        "平均 tokens in/out | 平均 cost USD | 平均耗時 s | 狀態分布 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for (rn, model, task), gs in sorted(groups.items()):
        n = len(gs)
        stats = [g["run"]["stats"] for g in gs]

        def mean(key: str, stats: list[dict[str, Any]] = stats) -> float:
            vals = [s.get(key) for s in stats if isinstance(s.get(key), int | float)]
            return sum(vals) / len(vals) if vals else float("nan")

        costs = [s["total_cost_usd"] for s in stats if s.get("total_cost_usd") is not None]
        dist: dict[str, int] = {}
        for g in gs:
            dist[g["status"]] = dist.get(g["status"], 0) + 1
        npass = sum(g["passed"] for g in gs)
        lines.append(
            f"| {rn} | {model} | {task} | {n} | {npass}/{n} ({npass / n:.0%}) | "
            f"{mean('builds'):.1f} | {mean('num_turns'):.1f} | "
            f"{_k(mean('tokens_in'))}/{_k(mean('tokens_out'))} | "
            f"{f'{sum(costs) / len(costs):.4f}' if costs else '—'} | "
            f"{sum(g['wall_s'] for g in gs) / n:.0f} | "
            f"{', '.join(f'{k}={v}' for k, v in sorted(dist.items()))} |"
        )
    return "\n".join(lines)
