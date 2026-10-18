"""Agent runner 抽象：把 prompt 交給外部 coding agent CLI（或 replay），cwd = 隔離工作區。

- `ClaudeCodeRunner`：`claude -p`（prompt 由 stdin 傳入）+ stream-json
- `CodexRunner`：`codex exec --json`（旗標已查證；事件 schema best-effort）
- `ReplayRunner`：不呼叫 LLM，把既有 spec 複製進工作區並跑 `draftagent build`（測 harness / grader）

旗標皆以 `claude --help`（Claude Code 2.1.302）與 `codex exec --help`（codex-cli 0.160.0）查證；
`--max-turns` 不在 help 中但存在於 binary（隱藏旗標）。
"""

from __future__ import annotations

import contextlib
import os
import shlex
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from draftagent.eval.streamjson import RunStats, parse_codex_stream, parse_stream
from draftagent.eval.tasks import Task
from draftagent.eval.workspace import Workspace

PREAMBLE = (
    "你在 DraftAgent repo。依 CLAUDE.md 流程完成以下任務，"
    "最後必須用 draftagent build 產出並貼出 verifier 輸出。"
)


def build_prompt(task: Task) -> str:
    return f"{PREAMBLE}\n\n---\n\n{task.prompt.strip()}\n"


@dataclass
class RunOutcome:
    stats: RunStats
    exit_code: int | None
    timed_out: bool
    wall_s: float
    command: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "wall_s": round(self.wall_s, 2),
            "command": self.command,
            "error": self.error,
            "stats": self.stats.to_dict(),
        }


class Runner:
    name = "base"
    model: str | None = None

    def run(self, task: Task, ws: Workspace, out_dir: Path, *, timeout: float) -> RunOutcome:
        raise NotImplementedError


def _run_streaming(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    stdin_text: str,
    log: Path,
    err_log: Path,
    timeout: float,
) -> tuple[int | None, bool, float]:
    """執行子行程，stdout 逐行寫 log；超時砍整個 process group。"""
    t0 = time.monotonic()
    with log.open("w", encoding="utf-8") as out, err_log.open("w", encoding="utf-8") as err:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=out,
            stderr=err,
            text=True,
            start_new_session=True,
        )
        try:
            proc.communicate(stdin_text, timeout=timeout)
            return proc.returncode, False, time.monotonic() - t0
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            return proc.returncode, True, time.monotonic() - t0


# ---------------------------------------------------------------- claude


class ClaudeCodeRunner(Runner):
    name = "claude"

    def __init__(
        self,
        model: str | None = None,
        *,
        max_turns: int = 30,
        max_budget_usd: float | None = None,
        binary: str = "claude",
        user_settings: bool = False,
        extra_args: list[str] | None = None,
    ) -> None:
        self.model = model
        self.max_turns = max_turns
        self.max_budget_usd = max_budget_usd
        self.binary = binary
        self.user_settings = user_settings
        self.extra_args = extra_args or []

    def allowed_tools(self, ws: Workspace | None = None) -> list[str]:
        bash = [
            ".venv/bin/draftagent",
            ".venv/bin/python",
            ".venv/bin/pytest",
            "draftagent",
            "python",
            "python3",
            "pytest",
            "ls",
            "cat",
            "head",
            "tail",
            "mkdir",
            "wc",
            "cd",  # agent 常寫「cd <工作區>; …」複合指令，cd 不在清單會讓整串被拒
            "grep",
            "echo",
            # 工作區是暫存複本；agent 修改自己 work/ 檔案常用這些（Level 4 曾因 sed / rm / cat> 被擋）
            "sed",
            "rm",
            "cp",
            "mv",
            "touch",
            "find",
            "sort",
            "diff",
        ]
        if ws is not None:
            # agent 常用絕對路徑呼叫（/…/repo/.venv/bin/draftagent）；只放行工作區內的執行檔。
            # 第二輪評估 detail-ty-1 曾因此被拒絕執行 build，被誤判為「缺資料拒絕」。
            for exe in ("draftagent", "python", "pytest"):
                bash.append(str(ws.root / ".venv" / "bin" / exe))
        return ["Read", "Write", "Edit", "Glob", "Grep", *(f"Bash({b}:*)" for b in bash)]

    def disallowed_tools(self, ws: Workspace) -> list[str]:
        # 原 repo（含 expected/）不可讀寫；`//` 開頭 = 絕對路徑規則。
        # 規則用 `:*` 前綴語法（不含空白），才能安全地以逗號串成一個參數
        src = str(ws.source).lstrip("/")
        return [f"{t}(//{src}/**)" for t in ("Read", "Edit", "Write")] + ["WebFetch", "WebSearch"]

    def command(self, ws: Workspace) -> list[str]:
        cmd = [
            self.binary,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--max-turns",
            str(self.max_turns),
            "--permission-mode",
            "acceptEdits",
            "--permission-prompts",
            "none",
            "--no-session-persistence",
            "--strict-mcp-config",
            "--allowedTools",
            ",".join(self.allowed_tools(ws)),
            "--disallowedTools",
            ",".join(self.disallowed_tools(ws)),
        ]
        if not self.user_settings:
            cmd += ["--setting-sources", "project,local"]
        if self.model:
            cmd += ["--model", self.model]
        if self.max_budget_usd is not None:
            cmd += ["--max-budget-usd", str(self.max_budget_usd)]
        return cmd + self.extra_args

    def run(self, task: Task, ws: Workspace, out_dir: Path, *, timeout: float) -> RunOutcome:
        if shutil.which(self.binary) is None:
            return RunOutcome(RunStats(), None, False, 0.0, error=f"找不到 {self.binary} CLI")
        cmd = self.command(ws)
        log = out_dir / "stream.jsonl"
        code, to, wall = _run_streaming(
            cmd,
            cwd=ws.root,
            env=ws.env(),
            stdin_text=build_prompt(task),
            log=log,
            err_log=out_dir / "stderr.txt",
            timeout=timeout,
        )
        stats = parse_stream(log.read_text(encoding="utf-8").splitlines())
        stats.model = stats.model or self.model
        return RunOutcome(stats, code, to, wall, command=cmd)


# ---------------------------------------------------------------- codex


class CodexRunner(Runner):
    """`codex exec --json -C <ws> -s workspace-write [-m model] --skip-git-repo-check -`。

    codex 沒有 max-turns 旗標，只靠 timeout；無 total_cost_usd（cost 欄空白）。
    沙箱 workspace-write 只能寫工作區，但讀取不受限 → 依賴 integrity 掃描。
    """

    name = "codex"

    def __init__(
        self,
        model: str | None = None,
        *,
        binary: str = "codex",
        extra_args: list[str] | None = None,
    ) -> None:
        self.model = model
        self.binary = binary
        self.extra_args = extra_args or []

    def command(self, ws: Workspace, out_dir: Path) -> list[str]:
        cmd = [
            self.binary,
            "exec",
            "--json",
            "-C",
            str(ws.root),
            "-s",
            "workspace-write",
            "--skip-git-repo-check",
            "--ephemeral",
            "-o",
            str(out_dir / "last_message.txt"),
        ]
        if self.model:
            cmd += ["-m", self.model]
        return [*cmd, *self.extra_args, "-"]

    def run(self, task: Task, ws: Workspace, out_dir: Path, *, timeout: float) -> RunOutcome:
        if shutil.which(self.binary) is None:
            return RunOutcome(RunStats(), None, False, 0.0, error=f"找不到 {self.binary} CLI")
        cmd = self.command(ws, out_dir)
        log = out_dir / "stream.jsonl"
        code, to, wall = _run_streaming(
            cmd,
            cwd=ws.root,
            env=ws.env(),
            stdin_text=build_prompt(task),
            log=log,
            err_log=out_dir / "stderr.txt",
            timeout=timeout,
        )
        stats = parse_codex_stream(log.read_text(encoding="utf-8").splitlines())
        last = out_dir / "last_message.txt"
        if last.exists():
            stats.final_message = last.read_text(encoding="utf-8") or stats.final_message
        stats.model = self.model
        return RunOutcome(stats, code, to, wall, command=cmd)


# ---------------------------------------------------------------- replay


class ReplayRunner(Runner):
    """不呼叫 LLM：把 spec（預設 = 任務的 input/spec.json［、spec-2.json…］，取自**原 repo**）依序複製到
    工作區 `work/replay_spec[-N].json`，用工作區 venv 跑 `draftagent build`。

    reply（最後回覆）：預設 = 任務的 `expected/replay_reply.md`（參考行為；reply_checks / 拒絕題用），
    沒有則為 build 輸出。沒有任何 spec、只有 reply → 不 build（「應拒絕」題的參考行為）。"""

    name = "replay"

    def __init__(
        self,
        spec: Path | list[Path] | None = None,
        *,
        build: bool = True,
        reply: str | None = None,
    ) -> None:
        self.specs = (
            None if spec is None else ([spec] if isinstance(spec, str | Path) else list(spec))
        )
        self.build = build
        self.reply = reply
        self.model = "replay"

    def run(self, task: Task, ws: Workspace, out_dir: Path, *, timeout: float) -> RunOutcome:
        srcs = [Path(p) for p in self.specs] if self.specs is not None else task.oracle_specs
        reply = self.reply if self.reply is not None else task.replay_reply
        missing = [p for p in srcs if not p.exists()]
        if missing or (not srcs and not reply):
            return RunOutcome(
                RunStats(),
                None,
                False,
                0.0,
                error=f"replay spec 不存在：{missing or task.oracle_spec}",
            )
        stats = RunStats(model="replay", subtype="success", final_message=reply)
        if not self.build or not srcs:
            return RunOutcome(stats, 0, False, 0.0, command=[])
        log = out_dir / "stream.jsonl"
        logs: list[str] = []
        code, to, wall, cmds = 0, False, 0.0, []
        for i, src in enumerate(srcs):
            dst = ws.root / "work" / f"replay_spec{'' if i == 0 else f'-{i + 1}'}{src.suffix}"
            shutil.copy2(src, dst)
            cmd = [
                str(ws.venv_bin / "draftagent"),
                "build",
                str(dst.relative_to(ws.root)),
                "--no-preview",
            ]
            part = out_dir / f"stream{'' if i == 0 else f'-{i + 1}'}.jsonl"
            c, t, w = _run_streaming(
                cmd,
                cwd=ws.root,
                env=ws.env(),
                stdin_text="",
                log=part,
                err_log=out_dir / f"stderr{'' if i == 0 else f'-{i + 1}'}.txt",
                timeout=timeout,
            )
            logs.append(part.read_text(encoding="utf-8"))
            code, to, wall = (code or c), (to or t), wall + w
            cmds.append(shlex.join(cmd))
        log.write_text("\n".join(logs), encoding="utf-8")
        stats.builds = len(srcs)
        stats.tool_calls = len(srcs)
        stats.bash_commands = cmds
        stats.final_message = reply or "\n".join(logs)
        return RunOutcome(stats, code, to, wall, command=cmds[-1].split() if cmds else [])


def make_runner(
    name: str,
    *,
    model: str | None = None,
    max_turns: int = 30,
    max_budget_usd: float | None = None,
    replay_spec: Path | None = None,
    user_settings: bool = False,
) -> Runner:
    if name == "claude":
        return ClaudeCodeRunner(
            model, max_turns=max_turns, max_budget_usd=max_budget_usd, user_settings=user_settings
        )
    if name == "codex":
        return CodexRunner(model)
    if name == "replay":
        return ReplayRunner(replay_spec)
    raise KeyError(f"未知 runner {name!r}（claude | codex | replay）")
