"""Agent 評估 harness（docs/PLAN.md §6：pass rate、修正輪數、token 成本）。

流程：發現任務（tests/level*/<task>/prompt.md）→ 建隔離工作區（移除 expected/、input/spec.json、
runs/）→ 交給外部 coding agent CLI（claude / codex）或 ReplayRunner → 在工作區找最新 run →
用**原 repo** 的程式重新 build + 全部 verifier → 與 expected/expected.json 比對 → 寫結果。

本 package 會呼叫外部 agent CLI；render/、verify/ 仍不得有任何 LLM 呼叫。
用法見 docs/EVAL.md 與 `python tools/eval.py --help`。
"""

from draftagent.eval.tasks import TASKS, Task, discover_tasks, select_tasks

__all__ = ["TASKS", "Task", "discover_tasks", "select_tasks"]
