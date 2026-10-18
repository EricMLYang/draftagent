"""Agent 評估 CLI（docs/EVAL.md）。

    python tools/eval.py list
    python tools/eval.py run level1/1-1 --runner replay              # 不呼叫 LLM，測 harness
    python tools/eval.py run level3 --runner replay --no-results     # level3 參考解 / 參考行為
    python tools/eval.py run level4 --runner replay --no-results     # level4 參考解（conflict-1 = 參考衝突回報）
    python tools/eval.py run level1 --runner claude --model sonnet --repeat 3 --max-turns 30
    python tools/eval.py report

結果寫 runs/eval/<ts>/<task>/<i>/，每次 run append 一列到 docs/RESULTS.md。
`--runner claude` 會實際呼叫 Claude Code 並計費：每題可能數十萬 tokens。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from draftagent.eval.harness import EVAL_ROOT, RESULTS_MD, RunRecord, report, run_eval
from draftagent.eval.runners import make_runner
from draftagent.eval.tasks import discover_tasks, select_tasks


def _print_record(rec: RunRecord) -> None:
    st = rec.stats
    cost = f"${st.total_cost_usd:.4f}" if st.total_cost_usd is not None else "—"
    print(
        f"[{rec.grade.status:>14}] {rec.task}#{rec.index}  builds={st.builds} "
        f"turns={st.num_turns} tokens={st.tokens_in}/{st.tokens_out} cost={cost} "
        f"{rec.wall_s:.0f}s  → {rec.out_dir}"
    )
    for r in rec.grade.reasons[:5]:
        print(f"    - {r}")
    for v in rec.grade.verifiers:
        print(f"    verifier {v['name']}: {'PASS' if v['ok'] else 'FAIL'}")
    for c in rec.grade.checks:
        print(f"    check {c.name}: {'ok' if c.ok else 'FAIL ' + c.detail}")
    for c in rec.grade.cross_checks:
        print(f"    cross {c.name}: {'ok' if c.ok else 'FAIL ' + c.detail}")
    for c in rec.grade.reply_checks:
        print(f"    reply {c.name}: {'ok' if c.ok else 'FAIL ' + c.detail}")
    if rec.grade.design_metrics:
        print(f"    metrics {rec.grade.design_metrics}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="列出任務與是否有 expected 比對定義")

    r = sub.add_parser("run", help="跑評估")
    r.add_argument("tasks", nargs="*", help="level1 | level2 | level1/1-1 | 1-1（預設 level1）")
    r.add_argument("--runner", default="replay", choices=["claude", "codex", "replay"])
    r.add_argument("--model", help="claude --model / codex -m")
    r.add_argument("--repeat", type=int, default=1)
    r.add_argument("--max-turns", type=int, default=30)
    r.add_argument("--max-budget-usd", type=float, help="claude --max-budget-usd（每次 run）")
    r.add_argument("--timeout", type=float, default=900, help="每次 run 秒數上限")
    r.add_argument("--keep-workdir", action="store_true", help="保留工作區（路徑寫在 workdir.txt）")
    r.add_argument(
        "--replay-spec", type=Path, help="replay：改用這個 spec（預設任務的 input/spec.json）"
    )
    r.add_argument(
        "--user-settings",
        action="store_true",
        help="claude：也載入使用者層 settings（預設只載 project,local，避免個人 hooks / 權限混入）",
    )
    r.add_argument("--no-results", action="store_true", help="不 append docs/RESULTS.md")
    r.add_argument("--note", default="", help="寫進 RESULTS.md 備註欄")
    r.add_argument("--eval-root", type=Path, default=EVAL_ROOT)

    rp = sub.add_parser("report", help="彙總 runs/eval/ 下所有 grade.json")
    rp.add_argument("--runner")
    rp.add_argument("--eval-root", type=Path, default=EVAL_ROOT)

    a = ap.parse_args(argv)
    if a.cmd == "list":
        for tid, t in discover_tasks().items():
            d = t.definition
            print(
                f"{tid:<28} {'kinds=' + ','.join(d.kinds) if d else '(只看 verifier)'}"
                f"  checks={len(d.checks) if d else 0}"
                + (f"  grading={d.grading}" if d and d.grading != "expected" else "")
                + ("  expect_refusal" if d and d.expect_refusal else "")
                + ("  expect_conflict" if d and d.expect_conflict else "")
                + (f"  reply={len(d.reply_checks)}" if d and d.reply_checks else "")
                + (f"  cross={len(d.cross_build_checks)}" if d and d.cross_build_checks else "")
                + ("  allowed_changes" if d and d.allowed_changes else "")
            )
        return 0
    if a.cmd == "report":
        print(report(a.eval_root, runner=a.runner))
        return 0

    tasks = select_tasks(a.tasks)
    if a.runner == "replay" and a.replay_spec and len(tasks) != 1:
        ap.error("--replay-spec 只能配一個任務")
    runner = make_runner(
        a.runner,
        model=a.model,
        max_turns=a.max_turns,
        max_budget_usd=a.max_budget_usd,
        replay_spec=a.replay_spec,
        user_settings=a.user_settings,
    )
    print(
        f"runner={runner.name} model={runner.model or '(預設)'} tasks={[t.id for t in tasks]} "
        f"repeat={a.repeat}"
    )
    recs = run_eval(
        tasks,
        runner,
        repeat=a.repeat,
        timeout=a.timeout,
        keep_workdir=a.keep_workdir,
        eval_root=a.eval_root,
        results_md=None if a.no_results else RESULTS_MD,
        note=a.note,
        on_record=_print_record,
    )
    n = sum(r.grade.passed for r in recs)
    print(f"\nRESULT: {n}/{len(recs)} pass")
    return 0 if n == len(recs) else 1


if __name__ == "__main__":
    sys.exit(main())
