"""可解性測試：每個評估任務只靠 prompt.md + input/ 原始資料就能機械地寫出 spec，且 grader 判 pass。

`draftagent/eval/reference_solvers.py` 每題一個函式。這裡把任務的 prompt.md 與 input/（**刪掉
spec.json**、不含 expected/）複製到暫存目錄再解，證明解答器沒有偷看答案；解出的 spec 交給
ReplayRunner → 原 repo grader 重新 build → 對 expected 比對，必須 pass。

出新題時：原始資料放 input/、在 SOLVERS 補一個函式；本測試失敗代表題目資訊不足（或解答器沒跟上
原始資料格式），不得放寬 expected 或 verifier。不呼叫任何 LLM。

Level 3：解答器回傳 `Solution`（多份 spec 依序 build、最後回覆；specs 空 = 拒絕）。「應拒絕」題
（refuse-1）另證明缺的那一項確實讓 spec 無法完整：原始資料解析丟 MissingDataError，補回設計值後可 build 綠燈。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from draftagent.eval import reference_solvers as rs
from draftagent.eval.harness import run_one
from draftagent.eval.runners import ReplayRunner
from draftagent.eval.tasks import discover_tasks

TASKS = discover_tasks()


def _blind_copy(task_dir: Path, dst: Path) -> Path:
    """prompt.md + input/（不含 spec*.json）；不複製 expected/。"""
    dst.mkdir(parents=True)
    shutil.copy2(task_dir / "prompt.md", dst / "prompt.md")
    if (task_dir / "input").is_dir():
        shutil.copytree(
            task_dir / "input",
            dst / "input",
            ignore=shutil.ignore_patterns("spec.json", "spec-*.json"),
        )
    return dst


def _write_specs(sol: rs.Solution, d: Path) -> list[Path]:
    paths = []
    for i, spec in enumerate(sol.specs):
        p = d / f"solved_spec{i}.json"
        p.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
        paths.append(p)
    return paths


def test_every_task_has_solver():
    missing = sorted(set(TASKS) - set(rs.SOLVERS))
    assert not missing, f"這些任務沒有參考解答器（題目可解性未證明）：{missing}"
    assert not sorted(set(rs.SOLVERS) - set(TASKS)), "SOLVERS 有不存在的任務"


@pytest.mark.parametrize("tid", sorted(rs.SOLVERS))
def test_task_solvable_from_raw_inputs(tid, tmp_path):
    task = TASKS[tid]
    if "floor-plan" in task.definition.kinds:
        pytest.importorskip("ifcopenshell")  # floor-plan 產 IFC（.[ifc]）
    blind = _blind_copy(task.dir, tmp_path / "task")
    sol = rs.solution(tid, blind)
    rec = run_one(task, ReplayRunner(_write_specs(sol, tmp_path), reply=sol.reply), tmp_path / "r")
    failed = [
        f"{c.name}: {c.detail}" for c in rec.grade.checks + rec.grade.cross_checks if not c.ok
    ]
    failed += [f"reply {c.name}: {c.detail}" for c in rec.grade.reply_checks if not c.ok]
    assert rec.grade.passed, (rec.grade.status, rec.grade.reasons, failed)
    if task.definition.expect_refusal:
        assert sol.refused and rec.grade.status == "refused_missing_data"
    elif task.definition.expect_conflict:  # 需求矛盾題（level4/conflict-1）：不 build、回覆點出衝突
        assert sol.refused and rec.grade.status == "reported_conflict"
    else:
        assert rec.grade.status == "pass"
        assert rec.grade.checks, "任務表沒有 expected 比對項"
    assert len(sol.specs) == len(task.oracle_specs), (
        "input/spec*.json 與參考解份數不同（重跑 fixture 產生器）"
    )


@pytest.mark.parametrize("tid", sorted(rs.SOLVERS))
def test_level_inputs_have_raw_data(tid):
    """input/spec*.json 以外要有原始資料（level1/1-1 是例外：題目全在 prompt 裡）。"""
    inp = TASKS[tid].dir / "input"
    raw = [p for p in inp.iterdir() if not rs._is_answer_name(p.name)] if inp.is_dir() else []
    if tid == "level1/1-1":
        return
    assert raw, f"{tid} 的 input/ 只有參考答案 spec.json，沒有原始資料"


@pytest.mark.parametrize("tid", sorted(TASKS))
def test_prompt_does_not_point_to_answers(tid):
    p = TASKS[tid].prompt
    for bad in ("input/spec.json", "expected/", "expected.json"):
        assert bad not in p, (
            f"{tid} prompt 提到答案檔 {bad!r}（評估時會被刪，且碰到會判 fail_integrity）"
        )


def test_task_inputs_refuses_answer_files(tmp_path):
    ti = rs.TaskInputs(tmp_path)
    for name in (
        "spec.json",
        "spec-2.json",
        "expected/expected.json",
        "../expected/expected.json",
        "x/spec.json",
    ):
        with pytest.raises(rs.AnswerAccessError):
            ti.path(name)
    assert ti.path("survey.csv") == tmp_path / "input" / "survey.csv"


def test_solver_fails_when_raw_data_missing(tmp_path):
    """原始資料缺檔 → 解答器失敗（不會默默用預設值補）。"""
    tid = "level2/profile-ty-1"
    blind = _blind_copy(TASKS[tid].dir, tmp_path / "task")
    (blind / "input" / "survey.csv").unlink()
    with pytest.raises(FileNotFoundError):
        rs.solve(tid, blind)


def test_md_parser():
    doc = rs.MdDoc.parse(
        "# t\n\n## 圖面\n\n- 圖號：D-07\n- 長句：第一行\n  第二行\n\n"
        "| 項目 | 值 |\n|---|---|\n| 內寬 | 400 |\n| 空白 |  |\n"
    )
    sec = doc.section("圖面")
    assert sec.bullets == {"圖號": "D-07", "長句": "第一行第二行"}
    assert sec.kv() == {"內寬": "400", "空白": ""}
    with pytest.raises(KeyError):
        doc.section("不存在")
    assert rs._station("0+048.00") == 48.0
    assert rs._station("1+002.50") == 1002.5


# ---------------------------------------------------------------- level3 refuse-1：缺的那項確實必要


def test_refuse_task_missing_value_blocks_spec(tmp_path):
    from pydantic import ValidationError

    from draftagent.drainage.model import Node

    tid = "level3/refuse-1"
    blind = _blind_copy(TASKS[tid].dir, tmp_path / "task")
    with pytest.raises(rs.MissingDataError, match=r"MH1.*底高"):
        rs._read_net_raw(rs.TaskInputs(blind))
    sol = rs.solution(tid, blind)
    assert sol.refused and "MH1" in sol.reply
    # schema 本身也不接受沒有底高的節點（不能「先留空」build）
    with pytest.raises(ValidationError):
        Node.model_validate({"id": "MH1", "kind": "manhole", "x": 1, "y": 2, "ground_el": 3})


def test_refuse_task_only_that_value_is_missing(tmp_path):
    """補回 fixture 的設計值（expected/hidden_values）→ 解得出 spec，且重新 build 全綠：題目只缺這一項。"""
    from draftagent.build import build, load_spec

    task = TASKS["level3/refuse-1"]
    hidden = task.expected()["hidden_values"]
    blind = _blind_copy(task.dir, tmp_path / "task")
    p = blind / "input" / "nodes.csv"
    lines = p.read_text(encoding="utf-8").splitlines()
    head = lines[0].split(",") if not lines[0].startswith("#") else lines[1].split(",")
    col = head.index("底高(m)")
    out = []
    for ln in lines:
        f = ln.split(",")
        if f[0] in hidden:
            f[col] = f"{hidden[f[0]]['底高(m)']:.2f}"
        out.append(",".join(f))
    p.write_text("\n".join(out) + "\n", encoding="utf-8")
    sol = rs.solution("level3/refuse-1", blind)
    assert not sol.refused
    (spec_path,) = _write_specs(sol, tmp_path)
    res = build(load_spec(spec_path), out=tmp_path / "b", preview=False)
    assert res.ok, [e for r in res.results for e in r.errors]


def test_design_task_solver_respects_brief(tmp_path):
    """3-design 參考解：重力、覆土 ≥ 交辦值、分區單一土地使用（性質由 grader 再驗一次）。"""
    sol = rs.solution(
        "level3/design-ty-1", _blind_copy(TASKS["level3/design-ty-1"].dir, tmp_path / "t")
    )
    (spec,) = sol.specs
    assert spec["storage"]["drain_mode"] == "gravity"
    assert not spec["storage"].get("allow_backwater")
