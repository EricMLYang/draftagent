"""解析 `claude -p --output-format stream-json --verbose` 的 JSONL 事件流。

事件格式（以 Claude Code 2.1.302 binary 內字串查證，見 docs/EVAL.md）：
- `{"type":"system","subtype":"init","session_id","model","tools",...}`
- `{"type":"assistant","message":{"id","model","content":[{type:text|tool_use|thinking}],
   "usage":{...}},"parent_tool_use_id",...}`：同一則 API 回應可能拆成多個事件（共用 message.id，
   usage 重複），所以以 message.id 去重
- `{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id",...}]}}`
- `{"type":"result","subtype":"success"|"error_max_turns"|"error_during_execution",
   "is_error","num_turns","duration_ms","duration_api_ms","result","total_cost_usd",
   "usage":{input_tokens,output_tokens,cache_creation_input_tokens,cache_read_input_tokens},
   "modelUsage":{...},"permission_denials":[...],"session_id"}`

result 事件的 usage / total_cost_usd 是整個 session 的總和，有就以它為準；沒有（被 timeout 砍掉）
才用 assistant 事件去重加總。
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from typing import Any

USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)
# 一個 Bash 指令裡出現 `draftagent build`（含 .venv/bin/draftagent、python -m draftagent.cli build）
BUILD_RE = re.compile(r"(?:^|[\s/;&|(])draftagent(?:\.cli)?\s+build\b")


@dataclass
class RunStats:
    session_id: str | None = None
    model: str | None = None
    subtype: str | None = None  # result 事件的 subtype；None = 沒有 result（被砍 / 崩潰）
    is_error: bool | None = None
    num_turns: int = 0
    assistant_messages: int = 0
    tool_calls: int = 0
    tool_calls_by_name: dict[str, int] = field(default_factory=dict)
    bash_commands: list[str] = field(default_factory=list)
    builds: int = 0  # `draftagent build` 執行次數（= 修正輪數，見 docs/EVAL.md）
    usage: dict[str, int] = field(default_factory=dict)
    total_cost_usd: float | None = None
    duration_ms: int | None = None
    duration_api_ms: int | None = None
    permission_denials: list[Any] = field(default_factory=list)
    final_message: str = ""
    parse_errors: int = 0
    events: int = 0
    tool_inputs: list[str] = field(default_factory=list, repr=False)  # 給 integrity 掃描

    @property
    def tokens_in(self) -> int:
        """模型實際讀入的 token：未快取 + 寫入快取 + 讀快取。"""
        return sum(
            self.usage.get(k, 0)
            for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
        )

    @property
    def tokens_out(self) -> int:
        return self.usage.get("output_tokens", 0)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("tool_inputs")
        d["tokens_in"] = self.tokens_in
        d["tokens_out"] = self.tokens_out
        return d


def _add_usage(acc: dict[str, int], u: dict[str, Any] | None) -> None:
    for k in USAGE_KEYS:
        v = (u or {}).get(k)
        if isinstance(v, int | float):
            acc[k] = acc.get(k, 0) + int(v)


def parse_stream(lines: Iterable[str]) -> RunStats:
    st = RunStats()
    by_name: Counter[str] = Counter()
    seen_msgs: dict[str, dict[str, Any]] = {}
    last_text = ""
    result_usage: dict[str, Any] | None = None
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            st.parse_errors += 1
            continue
        if not isinstance(ev, dict):
            continue
        st.events += 1
        typ = ev.get("type")
        if typ == "system" and ev.get("subtype") == "init":
            st.session_id = ev.get("session_id", st.session_id)
            st.model = ev.get("model", st.model)
        elif typ == "assistant":
            msg = ev.get("message") or {}
            mid = msg.get("id") or f"_anon{st.events}"
            if mid not in seen_msgs:
                seen_msgs[mid] = msg.get("usage") or {}
                if ev.get("parent_tool_use_id") is None:
                    st.assistant_messages += 1
            st.model = st.model or msg.get("model")
            for block in msg.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and ev.get("parent_tool_use_id") is None:
                    last_text = block.get("text", "") or last_text
                elif block.get("type") == "tool_use":
                    name = block.get("name", "?")
                    inp = block.get("input") or {}
                    by_name[name] += 1
                    st.tool_inputs.append(json.dumps(inp, ensure_ascii=False))
                    if name == "Bash":
                        cmd = str(inp.get("command", ""))
                        st.bash_commands.append(cmd)
                        st.builds += len(BUILD_RE.findall(cmd))
        elif typ == "result":
            st.subtype = ev.get("subtype")
            st.is_error = ev.get("is_error")
            st.num_turns = int(ev.get("num_turns") or 0)
            st.duration_ms = ev.get("duration_ms")
            st.duration_api_ms = ev.get("duration_api_ms")
            st.total_cost_usd = ev.get("total_cost_usd")
            st.permission_denials = list(ev.get("permission_denials") or [])
            st.session_id = ev.get("session_id", st.session_id)
            if isinstance(ev.get("result"), str) and ev["result"]:
                last_text = ev["result"]
            result_usage = ev.get("usage")
    st.tool_calls = sum(by_name.values())
    st.tool_calls_by_name = dict(by_name)
    st.final_message = last_text
    if result_usage:
        _add_usage(st.usage, result_usage)
    else:
        for u in seen_msgs.values():
            _add_usage(st.usage, u)
    if not st.num_turns:
        st.num_turns = st.assistant_messages
    return st


def parse_codex_stream(lines: Iterable[str]) -> RunStats:
    """`codex exec --json` 的 JSONL（best-effort；事件 schema 未以文件查證，見 docs/EVAL.md）。

    寬鬆規則：任何帶 `usage` 的事件加總 token；任何 item 帶字串 `command` 視為一次 shell 呼叫；
    `turn.completed` 計一輪；最後一個 agent message 文字為 final_message。
    """
    st = RunStats()
    for raw in lines:
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            st.parse_errors += int(bool(raw.strip()))
            continue
        if not isinstance(ev, dict):
            continue
        st.events += 1
        typ = str(ev.get("type", ""))
        if typ == "thread.started":
            st.session_id = ev.get("thread_id")
        if typ == "turn.completed":
            st.num_turns += 1
        u = ev.get("usage")
        if isinstance(u, dict):
            _add_usage(
                st.usage,
                {
                    "input_tokens": u.get("input_tokens", 0) - u.get("cached_input_tokens", 0),
                    "cache_read_input_tokens": u.get("cached_input_tokens", 0),
                    "output_tokens": u.get("output_tokens", 0),
                },
            )
        item = ev.get("item")
        if isinstance(item, dict) and typ.endswith("completed"):
            cmd = item.get("command")
            if isinstance(cmd, str):
                st.tool_calls += 1
                st.bash_commands.append(cmd)
                st.tool_inputs.append(cmd)
                st.builds += len(BUILD_RE.findall(cmd))
            elif item.get("type") in ("agent_message", "assistant_message"):
                st.final_message = str(item.get("text", "")) or st.final_message
            elif item.get("type"):
                st.tool_calls += item.get("type") not in ("reasoning",)
                st.tool_inputs.append(json.dumps(item, ensure_ascii=False)[:2000])
    st.subtype = "success" if st.num_turns else None
    return st
