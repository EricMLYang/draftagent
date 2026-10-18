"""Agent 評估 harness（draftagent/eval/）本身的測試。不呼叫任何 LLM / claude CLI。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from draftagent.eval import leakcheck
from draftagent.eval.graders import (
    STATUSES,
    compare,
    grade,
    integrity_flags,
    looks_like_refusal,
    rebase_codes_paths,
)
from draftagent.eval.harness import append_results, report, run_one
from draftagent.eval.runners import (
    PREAMBLE,
    ClaudeCodeRunner,
    ReplayRunner,
    abs_rule_path,
    build_prompt,
)
from draftagent.eval.streamjson import RunStats, parse_stream
from draftagent.eval.tasks import Task, TaskDef, discover_tasks, select_tasks
from draftagent.eval.workspace import create_workspace, leaks

ROOT = Path(__file__).resolve().parents[1]
SPEC_1_1 = ROOT / "tests/level1/1-1/input/spec.json"


@pytest.fixture
def ws():
    w = create_workspace()
    yield w
    w.cleanup()


def _task(tid: str):
    return discover_tasks()[tid]


# ---------------------------------------------------------------- tasks


def test_discover_and_select():
    tasks = discover_tasks()
    assert {"level1/1-1", "level1/1-2", "level1/1-3"} <= set(tasks)
    assert [t.id for t in select_tasks(["level1"])] == ["level1/1-1", "level1/1-2", "level1/1-3"]
    assert [t.id for t in select_tasks(["1-2"])] == ["level1/1-2"]
    assert all(t.level == "level2" for t in select_tasks(["level2"]))
    with pytest.raises(KeyError):
        select_tasks(["nope"])
    assert all(tasks[t].definition is not None for t in ("level1/1-1", "level1/1-2", "level1/1-3"))


def test_prompt_has_preamble():
    p = build_prompt(_task("level1/1-1"))
    assert p.startswith(PREAMBLE)
    assert "6×4 m" in p


# ---------------------------------------------------------------- workspace


def test_workspace_has_no_answers(ws):
    root = ws.root
    assert not leaks(ws)
    assert not list(root.glob("tests/level*/*/expected"))
    assert not list(root.glob("tests/level*/*/input/spec.json"))
    assert not (root / ".git").exists()
    assert not (root / "draftagent/eval").exists()
    assert list((root / "runs").iterdir()) == []
    # 原始圖資保留（1-2、1-3 需要）
    assert (root / "tests/level1/1-2/input/cadastre.shp").exists()
    assert (root / "tests/level1/1-2/input/zoning.shp").exists()
    assert (root / "tests/level1/1-3/input/dtm.tif").exists()
    assert (root / "tests/level1/1-3/input/site.gpkg").exists()
    assert (root / "tests/level1/1-1/prompt.md").exists()
    # examples/ 不是答案，保留（draftagent new 需要）
    assert (root / "examples/plan.json").exists()
    assert (root / "examples/data/site/cadastre.shp").exists()
    assert (root / "tools/make_examples.py").exists()
    assert (root / "CLAUDE.md").exists()
    assert (root / ".claude/skills").is_dir()
    # 工作區 venv 載入的是工作區的 draftagent，不是原 repo
    out = subprocess.run(
        [str(ws.exe("python")), "-c", "import draftagent; print(draftagent.__file__)"],
        cwd=Path(root.anchor),
        capture_output=True,
        text=True,
        check=True,
        env=ws.env(),
    ).stdout.strip()
    assert Path(out).resolve().is_relative_to(root.resolve())
    assert ws.exe("draftagent").exists()


def test_protected_change_detected(ws):
    assert ws.protected_changes() == []
    p = ws.root / "draftagent/verify/dxf_check.py"
    p.write_text(p.read_text(encoding="utf-8") + "\n# 放寬門檻\n", encoding="utf-8")
    assert ws.protected_changes() == ["draftagent/verify/dxf_check.py"]
    st = RunStats(tool_inputs=['{"file_path": "tests/level1/1-1/expected/expected.json"}'])
    flags = integrity_flags(ws, st)
    assert any("dxf_check.py" in f for f in flags)
    assert any("expected.json" in f for f in flags)


# ---------------------------------------------------------------- examples / leakcheck


def test_examples_do_not_leak_answers():
    assert leakcheck.check() == []


def test_examples_dir_covers_kinds():
    from draftagent import registry

    for k in registry.all_kinds():
        assert k.example == ROOT / "examples" / f"{k.name}.json", k.name
        assert k.example.exists()
    big = [p for p in (ROOT / "examples").rglob("*") if p.is_file() and p.stat().st_size > 200_000]
    assert big == []


def test_leakcheck_detects_copies(tmp_path):
    spec = json.loads(SPEC_1_1.read_text(encoding="utf-8"))
    ex = tmp_path / "examples"
    ex.mkdir()
    (ex / "plan.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    reasons = {f.reason.split("：")[0] for f in leakcheck.check(ex)}
    assert {"JSON 內容完全相同", "關鍵欄位全部相同"} <= reasons
    # 只改標題 / 標註距離：關鍵欄位仍全同
    spec["title"] = "改個名字"
    spec["dimensions"]["offset"] = 700
    (ex / "plan.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    hits = leakcheck.check(ex)
    assert hits and all(f.task == "level1/1-1" for f in hits)
    assert any("關鍵欄位" in f.reason for f in hits)
    # 換掉尺寸 → 不算洩題
    spec["rooms"][0]["rect"].update(width=5200, depth=3800)
    spec["openings"][0]["offset"] = 2150
    (ex / "plan.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    assert leakcheck.check(ex) == []
    # 檔名不是該圖種，內容完全相同仍抓得到
    (ex / "plan.json").unlink()
    (ex / "whatever.json").write_text(SPEC_1_1.read_text(encoding="utf-8"), encoding="utf-8")
    assert [f.reason for f in leakcheck.check(ex)] == ["JSON 內容完全相同"]


def test_leakcheck_numeric_overlap():
    task = {"a": [12.5, 33.0, 47.25, 81.0, 99.5], "b": "x"}
    near = {"a": [12.5, 33.0, 47.25, 81.0, 123.0], "c": 1}
    assert any("數值重疊" in r for r in leakcheck.compare("unknown-kind", near, task))
    far = {"a": [12.5, 34.0, 48.25, 82.0, 123.0]}
    assert leakcheck.compare("unknown-kind", far, task) == []
    assert leakcheck.extract({"r": [{"x": 1}, {"x": 2}, {"y": 3}]}, "r[*].x") == [1, 2]


def test_workspace_keeps_raw_inputs_and_drops_answers(ws):
    src_tests = ROOT / "tests"
    for inp in sorted(src_tests.glob("level*/*/input")):
        for f in inp.rglob("*"):
            rel = f.relative_to(ROOT).as_posix()
            answer = f.name == "spec.json" or (f.name.startswith("spec-") and f.suffix == ".json")
            if f.is_file() and not answer:
                assert (ws.root / rel).exists(), f"原始資料 {rel} 沒有保留在工作區"
            elif f.is_file():
                assert not (ws.root / rel).exists(), f"參考答案 {rel} 留在工作區"
    assert (ws.root / "tests/level2/profile-ty-1/input/survey.csv").exists()
    assert (ws.root / "tests/level2/drainage-plan-ty-1/input/site.geojson").exists()
    assert not (ws.root / "draftagent/eval/reference_solvers.py").exists()
    assert not (ws.root / "tests/test_task_solvable.py").exists()
    assert not list(ws.root.rglob("reference_solvers.py"))


def test_workspace_leaks_flags_reference_solver(ws):
    assert leaks(ws) == []
    src = ROOT / "draftagent/eval/reference_solvers.py"
    shutil.copy(src, ws.root / "work" / "reference_solvers.py")
    shutil.copy(ROOT / "tests/test_task_solvable.py", ws.root / "tests" / "test_task_solvable.py")
    assert set(leaks(ws)) == {"work/reference_solvers.py", "tests/test_task_solvable.py"}


def test_workspace_leaks_flags_copied_example(ws):
    assert leaks(ws) == []
    shutil.copy(SPEC_1_1, ws.root / "examples" / "plan.json")
    bad = leaks(ws)
    assert bad and all(b.startswith("examples/plan.json") for b in bad)


@pytest.fixture(scope="module")
def ws_module():
    w = create_workspace()
    yield w
    w.cleanup()


def _kind_params():
    from draftagent import registry

    return [k.name for k in registry.all_kinds()]


@pytest.mark.parametrize("name", _kind_params())
def test_workspace_new_each_kind(ws_module, name):
    w = ws_module
    out = w.root / "work" / f"{name.replace('-', '_')}.py"
    r = subprocess.run(
        [str(w.exe("draftagent")), "new", name, "-o", str(out), "--force"],
        cwd=w.root,
        capture_output=True,
        text=True,
        env=w.env(),
        timeout=120,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert out.exists() and "spec = " in out.read_text(encoding="utf-8")


def test_workspace_new_spec_builds_green(ws_module):
    w = ws_module
    out = w.root / "work" / "site_new.py"
    env = w.env()
    run = {"cwd": w.root, "capture_output": True, "text": True, "env": env, "timeout": 300}
    r = subprocess.run(
        [str(w.exe("draftagent")), "new", "site-overlay", "-o", str(out), "--force"], **run
    )
    assert r.returncode == 0, r.stdout + r.stderr
    r = subprocess.run([str(w.exe("draftagent")), "build", str(out), "--no-preview"], **run)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "RESULT: PASS" in r.stdout


def test_skill_leak_script_clean_and_detects(tmp_path):
    script = ROOT / "scripts" / "check_skill_leaks.py"
    r = subprocess.run([sys.executable, str(script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    # 合成 root：skill 寫出考題答案 → 命中
    t = tmp_path / "tests/level1/x"
    (t / "expected").mkdir(parents=True)
    (t / "input").mkdir()
    (t / "expected/expected.json").write_text('{"total_m2": 4321.75, "note": "offset 2550"}')
    (t / "input/spec.json").write_text('{"w": 7300, "d": 2900, "o": 2550, "x": 640, "y": 33.3}')
    s = tmp_path / ".claude/skills/demo"
    s.mkdir(parents=True)
    (s / "SKILL.md").write_text(
        "面積 4321.75 m²，門 offset 2550；第 3 條、EPSG:3826 不算。\n\n"
        '```json\n{"w": 7300, "d": 2900, "o": 2550, "x": 640, "y": 33.3}\n```\n',
        encoding="utf-8",
    )
    r = subprocess.run(
        [sys.executable, str(script), "--root", str(tmp_path)], capture_output=True, text=True
    )
    assert r.returncode == 1
    assert "4321.75" in r.stdout and "2550" in r.stdout and "完整考題 spec" in r.stdout
    assert "3826" not in r.stdout.split("check_skill_leaks:")[0].replace("EPSG:3826 不算", "")


# ---------------------------------------------------------------- replay → grader


def test_replay_oracle_spec_passes(tmp_path):
    rec = run_one(_task("level1/1-1"), ReplayRunner(), tmp_path / "r")
    assert rec.grade.status == "pass", rec.grade.reasons
    assert rec.stats.builds == 1
    assert [v["name"] for v in rec.grade.verifiers] == ["dxf_check", "units_check"]
    assert all(c.ok for c in rec.grade.checks) and rec.grade.checks
    out = rec.out_dir
    assert (out / "grade.json").exists()
    assert json.loads((out / "final_spec.json").read_text(encoding="utf-8"))["kind"] == "plan"
    assert (out / "regrade").is_dir()
    g = json.loads((out / "grade.json").read_text(encoding="utf-8"))
    assert g["status"] == "pass" and g["task"] == "level1/1-1"


def test_replay_broken_spec_fails_expected(tmp_path):
    spec = json.loads(SPEC_1_1.read_text(encoding="utf-8"))
    spec["rooms"][0]["rect"]["width"] = 5000  # 6000 → 5000：verifier 仍綠（與 spec 自洽），但答案錯
    spec["openings"][0]["offset"] = 2050
    bad = tmp_path / "bad_spec.json"
    bad.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    rec = run_one(_task("level1/1-1"), ReplayRunner(bad), tmp_path / "r")
    assert rec.grade.status == "fail_expected", rec.grade.reasons
    failed = {c.name for c in rec.grade.checks if not c.ok}
    assert failed == {"overall_dimensions"}
    assert all(v["ok"] for v in rec.grade.verifiers)


def test_rebase_codes_paths(ws):
    codes = ws.root / "draftagent/hydro/codes/taoyuan_runoff.yaml"
    other = ws.root / "tests/level1/1-2/input/cadastre.shp"
    spec = {"codes_path": str(codes), "x": [{"p": str(other)}], "n": 1}
    out = rebase_codes_paths(spec, ws)
    assert out["codes_path"] == str(ws.source / "draftagent/hydro/codes/taoyuan_runoff.yaml")
    assert out["x"][0]["p"] == str(other) and out["n"] == 1


def test_no_output(tmp_path):
    rec = run_one(_task("level1/1-1"), ReplayRunner(build=False), tmp_path / "r")
    assert rec.grade.status == "no_output"


# ---------------------------------------------------------------- refused_missing_data

REFUSAL_TEXT = (
    "我沒有產出圖，因為題目要用的測量資料不在 repo 裡。縱斷面圖需要各節點的溝底高程，"
    "這些都是測量數據，不能自己編。請提供以下資料。"
)


def test_looks_like_refusal():
    assert "refused_missing_data" in STATUSES
    assert looks_like_refusal(REFUSAL_TEXT)
    assert looks_like_refusal("input/ 缺少 nodes.csv 的底高資料，停下回報。")
    assert not looks_like_refusal("")
    assert not looks_like_refusal("RESULT: PASS，已貼出 verifier 輸出。")


def test_refusal_on_complete_task_is_not_pass(ws, tmp_path):
    st = RunStats(subtype="success", final_message=REFUSAL_TEXT)
    g = grade(_task("level2/profile-ty-1"), ws, tmp_path / "g", st)
    assert g.status == "refused_missing_data" and not g.passed
    assert "資料應齊全" in g.reasons[0]
    st = RunStats(subtype="success", final_message="跑到一半就結束了。")
    assert grade(_task("level2/profile-ty-1"), ws, tmp_path / "g2", st).status == "no_output"


def _refusal_task() -> Task:
    t = _task("level1/1-1")
    return Task(id=t.id, dir=t.dir, definition=TaskDef(kinds=("plan",), expect_refusal=True))


def test_refusal_expected_task_passes(ws, tmp_path):
    st = RunStats(subtype="success", final_message=REFUSAL_TEXT)
    g = grade(_refusal_task(), ws, tmp_path / "g", st)
    assert g.status == "refused_missing_data" and g.passed
    assert "正確行為" in g.reasons[0]


def test_refusal_expected_task_fails_if_it_builds(tmp_path):
    rec = run_one(_refusal_task(), ReplayRunner(), tmp_path / "r")
    assert rec.grade.status == "fail_expected" and not rec.grade.passed
    assert "應停下回報缺資料" in rec.grade.reasons[0]


def test_refusal_with_integrity_flag_is_integrity(ws, tmp_path):
    st = RunStats(subtype="success", final_message=REFUSAL_TEXT)
    st.tool_inputs = ['{"command": "cat expected/expected.json"}']
    g = grade(_task("level2/profile-ty-1"), ws, tmp_path / "g", st)
    assert g.status == "fail_integrity"


def test_results_md_and_report(tmp_path):
    rec = run_one(
        _task("level1/1-1"), ReplayRunner(), tmp_path / "eval" / "ts" / "level1/1-1" / "0"
    )
    md = tmp_path / "RESULTS.md"
    append_results(md, rec, note="測試")
    append_results(md, rec)
    text = md.read_text(encoding="utf-8")
    rows = [line for line in text.splitlines() if "level1/1-1" in line]
    assert len(rows) == 2 and "| replay |" in rows[0] and "✅" in rows[0] and "測試" in rows[0]
    rep = report(tmp_path / "eval")
    assert "level1/1-1" in rep and "1/1 (100%)" in rep


# ---------------------------------------------------------------- compare


def test_compare():
    assert compare(6000.0, 6000) is None
    assert compare(1.004, 1.0, rtol=0.005) is None
    assert compare(1.006, 1.0, rtol=0.005)
    assert compare({"a": [1, 2]}, {"a": [1, 2]}) is None
    assert "鍵不同" in compare({"a": 1}, {"b": 1})
    assert compare([1, 2], [1, 2, 3])
    assert compare(True, 1)  # bool 不當數值
    assert compare("S", "S") is None


# ---------------------------------------------------------------- stream-json


FAKE_STREAM = [
    {
        "type": "system",
        "subtype": "init",
        "session_id": "s-1",
        "model": "claude-test-1",
        "tools": ["Bash", "Read"],
        "cwd": "/w",
    },
    {
        "type": "assistant",
        "parent_tool_use_id": None,
        "session_id": "s-1",
        "message": {
            "id": "m1",
            "model": "claude-test-1",
            "content": [{"type": "text", "text": "先讀 CLAUDE.md"}],
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_creation_input_tokens": 1000,
                "cache_read_input_tokens": 0,
            },
        },
    },
    # 同一則 message 拆成第二個事件（usage 重複，不可重複加總）
    {
        "type": "assistant",
        "parent_tool_use_id": None,
        "session_id": "s-1",
        "message": {
            "id": "m1",
            "model": "claude-test-1",
            "content": [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "Read",
                    "input": {"file_path": "CLAUDE.md"},
                }
            ],
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_creation_input_tokens": 1000,
                "cache_read_input_tokens": 0,
            },
        },
    },
    {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "..."}],
        },
    },
    {
        "type": "assistant",
        "parent_tool_use_id": None,
        "message": {
            "id": "m2",
            "content": [
                {
                    "type": "tool_use",
                    "id": "t2",
                    "name": "Write",
                    "input": {"file_path": "work/plan.json", "content": "{}"},
                },
                {
                    "type": "tool_use",
                    "id": "t3",
                    "name": "Bash",
                    "input": {"command": ".venv/bin/draftagent build work/plan.json"},
                },
            ],
            "usage": {
                "input_tokens": 3,
                "output_tokens": 40,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 1000,
            },
        },
    },
    {
        "type": "assistant",
        "parent_tool_use_id": None,
        "message": {
            "id": "m3",
            "content": [
                {
                    "type": "tool_use",
                    "id": "t4",
                    "name": "Bash",
                    "input": {"command": "python -m draftagent.cli build work/plan.json --json"},
                },
                {
                    "type": "tool_use",
                    "id": "t5",
                    "name": "Bash",
                    "input": {"command": "draftagent kinds && draftagent schema plan"},
                },
            ],
            "usage": {"input_tokens": 2, "output_tokens": 20},
        },
    },
    "not json at all",
    {
        "type": "assistant",
        "parent_tool_use_id": None,
        "message": {
            "id": "m4",
            "content": [{"type": "text", "text": "RESULT: PASS"}],
            "usage": {"input_tokens": 1, "output_tokens": 7},
        },
    },
]
FAKE_RESULT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "num_turns": 4,
    "duration_ms": 61000,
    "duration_api_ms": 50000,
    "result": "完成。RESULT: PASS",
    "total_cost_usd": 0.4321,
    "session_id": "s-1",
    "permission_denials": [],
    "usage": {
        "input_tokens": 16,
        "output_tokens": 72,
        "cache_creation_input_tokens": 1000,
        "cache_read_input_tokens": 1000,
    },
}


def _lines(events):
    return [e if isinstance(e, str) else json.dumps(e, ensure_ascii=False) for e in events]


def test_parse_stream_with_result():
    st = parse_stream(_lines([*FAKE_STREAM, FAKE_RESULT]))
    assert st.session_id == "s-1" and st.model == "claude-test-1"
    assert st.subtype == "success" and st.is_error is False
    assert st.num_turns == 4
    assert st.tool_calls == 5
    assert st.tool_calls_by_name == {"Read": 1, "Write": 1, "Bash": 3}
    assert (
        st.builds == 2
    )  # `draftagent build` 與 `python -m draftagent.cli build`；kinds/schema 不算
    assert st.total_cost_usd == pytest.approx(0.4321)
    assert st.usage == FAKE_RESULT["usage"]
    assert st.tokens_in == 2016 and st.tokens_out == 72
    assert st.final_message == "完成。RESULT: PASS"
    assert st.parse_errors == 1
    assert st.duration_ms == 61000


def test_parse_stream_without_result_dedupes_usage():
    st = parse_stream(_lines(FAKE_STREAM))  # 例如 timeout 被砍，沒有 result 事件
    assert st.subtype is None and st.total_cost_usd is None
    assert st.assistant_messages == 4 and st.num_turns == 4
    # m1 只算一次：in 10+3+2+1、out 5+40+20+7
    assert st.usage["input_tokens"] == 16
    assert st.usage["output_tokens"] == 72
    assert st.usage["cache_creation_input_tokens"] == 1000
    assert st.usage["cache_read_input_tokens"] == 1000
    assert st.final_message == "RESULT: PASS"


def test_claude_command_flags(ws):
    r = ClaudeCodeRunner("sonnet", max_turns=12, max_budget_usd=3.0)
    cmd = r.command(ws)
    assert cmd[:2] == ["claude", "-p"]
    for flag in (
        "--output-format",
        "--verbose",
        "--max-turns",
        "--permission-mode",
        "--allowedTools",
        "--disallowedTools",
        "--model",
        "--max-budget-usd",
    ):
        assert flag in cmd
    assert cmd[cmd.index("--output-format") + 1] == "stream-json"
    assert cmd[cmd.index("--max-turns") + 1] == "12"
    assert cmd[cmd.index("--permission-mode") + 1] == "acceptEdits"
    allowed = cmd[cmd.index("--allowedTools") + 1]
    assert "Bash(.venv/bin/draftagent:*)" in allowed and " " not in allowed
    denied = cmd[cmd.index("--disallowedTools") + 1]
    assert f"Read(//{abs_rule_path(ws.source)}/**)" in denied
    if os.name != "nt":
        assert f"Read(/{ws.source}/**)" in denied  # POSIX：// + /Users/... 去掉開頭 /
    assert not any(PREAMBLE in c for c in cmd)  # prompt 走 stdin，不在 argv


def test_allowed_tools_include_absolute_workspace_paths(tmp_path):
    # 第二輪評估：agent 以絕對路徑呼叫 build、或用「cd <ws>; …」複合指令而被拒
    from types import SimpleNamespace

    from draftagent.eval.runners import ClaudeCodeRunner

    ws = SimpleNamespace(root=tmp_path / "repo")
    tools = ClaudeCodeRunner().allowed_tools(ws)
    assert f"Bash({tmp_path / 'repo' / '.venv' / 'bin' / 'draftagent'}:*)" in tools
    assert "Bash(cd:*)" in tools


def test_blocked_build_is_harness_blocked_not_refusal():
    from draftagent.eval.graders import _blocked_builds
    from draftagent.eval.streamjson import RunStats

    st = RunStats(
        permission_denials=[
            {"tool_name": "Bash", "tool_input": {"command": "/x/repo/.venv/bin/draftagent build work/a.py"}}
        ],
        final_message="權限被擋，請提供 build 權限",
    )
    assert _blocked_builds(st)
