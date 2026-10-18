"""Level 4 評估題的單元測試：fixture 同步、prompt 不洩題、需求矛盾題評分（expect_conflict）、
平面配置 / 大基地設計 / 三段串接的性質與跨 build 檢查會抓到錯。用 ReplayRunner，不呼叫 LLM。"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from draftagent.eval import tasks_level4 as t4
from draftagent.eval.graders import STATUSES
from draftagent.eval.harness import run_one
from draftagent.eval.observed import Observed
from draftagent.eval.runners import ReplayRunner
from draftagent.eval.tasks import ReplyCheck, Task, TaskDef, discover_tasks

TASKS = discover_tasks()
ROOT = Path(__file__).resolve().parents[1]
LEVEL4 = sorted(t for t in TASKS if t.startswith("level4/"))


def _spec(tid: str, name: str = "spec.json") -> dict:
    return json.loads((TASKS[tid].dir / "input" / name).read_text(encoding="utf-8"))


def _replay(task: Task, tmp_path: Path, specs: list[dict] | None, reply: str | None = None):
    paths = None
    if specs is not None:
        paths = []
        for i, s in enumerate(specs):
            p = tmp_path / f"s{i}.json"
            p.write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")
            paths.append(p)
    return run_one(task, ReplayRunner(paths, reply=reply), tmp_path / "r").grade


def _obs(spec: dict) -> Observed:
    return Observed([], {}, spec)


# ---------------------------------------------------------------- 題目本身


def test_level4_has_six_tasks_all_defined():
    assert LEVEL4 == [
        "level4/chain-full",
        "level4/conflict-1",
        "level4/design-large",
        "level4/rooms-14",
        "level4/rooms-18",
        "level4/rooms-tight",
    ]
    assert all(TASKS[t].definition is not None for t in LEVEL4)


def test_level4_fixtures_up_to_date():
    import subprocess
    import sys

    r = subprocess.run(
        [sys.executable, str(ROOT / "tools/make_fixtures_level4.py"), "--check"],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_level4_prompts_do_not_leak_traps():
    conflict = TASKS["level4/conflict-1"].prompt + (
        TASKS["level4/conflict-1"].dir / "input/design.md"
    ).read_text(encoding="utf-8")
    for bad in ("矛盾", "衝突", "不可能", "61.2", "50 m³", "100 m²"):
        assert bad not in conflict, bad
    large = TASKS["level4/design-large"].prompt + (
        TASKS["level4/design-large"].dir / "input/design.md"
    ).read_text(encoding="utf-8")
    for bad in ("出流管制", "1 ha", "公頃", "本案須抽水", "重力排放不可行"):
        assert bad not in large, bad
    tight = TASKS["level4/rooms-tight"].prompt + (
        TASKS["level4/rooms-tight"].dir / "input/design.md"
    ).read_text(encoding="utf-8")
    assert "3%" not in tight and "3 %" not in tight


def test_rooms_tight_has_about_three_percent_slack():
    """參考配置的淨面積總和只比需求總和大 2–4.5%（每房 ±5% → 平均要放大約 3%，幾乎沒有餘裕）。"""
    from draftagent.schema.floorplan import FloorPlanSpec
    from draftagent.verify.floorplan_check import check_floorplan

    e = TASKS["level4/rooms-tight"].expected()
    assert set(e["area_targets_m2"]) == set(e["rooms"]), "rooms-tight 每個空間（含走廊）都要給面積"
    r = check_floorplan(FloorPlanSpec.model_validate(_spec("level4/rooms-tight")))
    assert r.ok
    ratio = r.metrics["net_area_total_m2"] / sum(e["area_targets_m2"].values())
    assert 1.02 < ratio < 1.045, ratio


def test_workspace_drops_level4_answers():
    from draftagent.eval.workspace import create_workspace, leaks

    ws = create_workspace()
    try:
        root = ws.root
        assert not (root / "tools/make_fixtures_level4.py").exists()
        assert not (root / "draftagent/eval/reference_solvers_level4.py").exists()
        assert not list(root.glob("tests/level4/*/input/spec*.json"))
        assert not list(root.glob("tests/level4/*/expected"))
        assert (root / "tests/level4/chain-full/input/floorplan.md").exists()
        assert (root / "tests/level4/design-large/input/site.geojson").exists()
        assert leaks(ws) == []
    finally:
        ws.cleanup()


# ---------------------------------------------------------------- 需求矛盾題（expect_conflict）


def _conflict_task(reply_checks=()) -> Task:
    t = TASKS["level4/conflict-1"]
    return Task(
        id=t.id,
        dir=t.dir,
        definition=TaskDef(
            kinds=("drainage-plan",), expect_conflict=True, reply_checks=reply_checks
        ),
    )


def test_conflict_reference_reply_passes(tmp_path):
    g = _replay(TASKS["level4/conflict-1"], tmp_path, None)
    assert "reported_conflict" in STATUSES
    assert g.status == "reported_conflict" and g.passed


def test_conflict_green_build_is_fail(tmp_path):
    """需求矛盾卻交付了 verifier 全綠的 build（任何綠燈設計都違反某項需求）→ fail_expected。"""
    good = _spec("level3/design-ty-1")
    g = _replay(_conflict_task(), tmp_path, [good], reply="需求矛盾，無法同時滿足")
    assert g.status == "fail_expected" and not g.passed
    assert any("矛盾" in r for r in g.reasons)


def test_conflict_red_trial_build_not_counted(tmp_path):
    """只有紅燈的試算 build 不算交付；回覆點出衝突 → reported_conflict。"""
    bad = copy.deepcopy(_spec("level3/design-ty-1"))
    bad["storage"]["effective_depth"] = 0.05  # 貯集量不足 → verifier 紅燈
    bad["storage"].pop("max_water_el", None)
    bad["storage"].pop("overflow_el", None)
    reply = TASKS["level4/conflict-1"].replay_reply
    g = _replay(TASKS["level4/conflict-1"], tmp_path, [bad], reply=reply)
    assert g.builds and not any(b["ok"] for b in g.builds)
    assert g.status == "reported_conflict" and g.passed


def test_conflict_reply_must_name_both_sides(tmp_path):
    g = _replay(
        TASKS["level4/conflict-1"], tmp_path, None, reply="需求互相矛盾，無法出圖，請工程師決定。"
    )
    assert g.status == "fail_reply" and not g.passed
    failed = {c.name for c in g.reply_checks if not c.ok}
    assert {"names_required_storage", "names_area_or_depth_limit"} <= failed


def test_conflict_reply_rejects_compliance_claim(tmp_path):
    reply = TASKS["level4/conflict-1"].replay_reply + "\n本設計符合法規。"
    g = _replay(TASKS["level4/conflict-1"], tmp_path, None, reply=reply)
    assert g.status == "fail_reply"


def test_conflict_task_reply_checks_shape():
    td = TASKS["level4/conflict-1"].definition
    assert td.expect_conflict and not td.expect_refusal
    assert all(isinstance(c, ReplyCheck) for c in td.reply_checks)


# ---------------------------------------------------------------- 平面配置性質


def test_fp_outline_shape_rejects_bbox_rectangle():
    e = TASKS["level4/rooms-14"].expected()
    s = _spec("level4/rooms-14")
    ok, _ = t4._fp_outline_shape(_obs(s), e)
    assert ok
    W, D = e["outline_mm"]
    rect = copy.deepcopy(s)
    rect["outline"] = {"rect": {"x": 0, "y": 0, "width": W, "depth": D}}
    ok, info = t4._fp_outline_shape(_obs(rect), e)
    assert not ok and info["symdiff_m2"] > 10
    moved = copy.deepcopy(s)  # 平移（原點不同）不算錯
    moved["outline"]["polygon"] = [[x + 5000, y - 3000] for x, y in s["outline"]["polygon"]]
    assert t4._fp_outline_shape(_obs(moved), e)[0]


def test_fp_door_widths_storage_doors():
    e = TASKS["level4/rooms-18"].expected()
    s = copy.deepcopy(_spec("level4/rooms-18"))

    class _O(Observed):
        def metric(self, verifier, *keys):  # _fp_room_uses 只讀 rooms 用途
            return {r["name"]: {"use": r["use"]} for r in self.spec["rooms"]}

    assert t4._fp_door_widths4(_O([], {}, s), e)[0]
    d = next(d for d in s["doors"] if "主臥衣櫃" in d["between"])
    d["width"] = 800
    ok, bad = t4._fp_door_widths4(_O([], {}, s), e)
    assert not ok and d["id"] in bad


def test_fp_level4_area_tolerance_is_5pct():
    for t in ("level4/rooms-14", "level4/rooms-18", "level4/rooms-tight", "level4/chain-full"):
        assert TASKS[t].expected()["area_tol"] == 0.05


# ---------------------------------------------------------------- design-large 性質


def test_design_large_props_catch_bad_designs():
    e = TASKS["level4/design-large"].expected()
    s = _spec("level4/design-large")
    for fn in (
        t4._pumped,
        t4._all_into_storage,
        t4._sub_count_size,
        t4._dl_storage_allowed,
        t4._pipes_only,
    ):
        assert fn(_obs(s), e)[0], fn.__name__

    grav = copy.deepcopy(s)
    st = grav["storage"]
    st["drain_mode"] = "gravity"
    assert not t4._pumped(_obs(grav), e)[0]

    bypass = copy.deepcopy(s)
    next(x for x in bypass["subcatchments"] if x["id"] == "P1")["outlet_node"] = "OW1"
    ok, bad = t4._all_into_storage(_obs(bypass), e)
    assert not ok and bad == {"P1": "OW1"}

    few = copy.deepcopy(s)
    few["subcatchments"] = few["subcatchments"][:9]
    assert not t4._sub_count_size(_obs(few), e)[0]

    deep = copy.deepcopy(s)
    deep["storage"]["bottom_el"] = 115.80  # 低於結構限制
    assert not t4._dl_storage_allowed(_obs(deep), e)[0]
    out = copy.deepcopy(s)
    out["storage"]["polygon"] = [[x + 20, y] for x, y in out["storage"]["polygon"]]
    assert not t4._dl_storage_allowed(_obs(out), e)[0]

    ditch = copy.deepcopy(s)
    ditch["links"][0]["section"] = {"type": "u_ditch", "w": 0.3, "h": 0.4}
    assert not t4._pipes_only(_obs(ditch), e)[0]


def test_design_large_reply_must_judge_outflow_control(tmp_path):
    task = TASKS["level4/design-large"]
    reply = "設計說明：筏基抽水，受納溝設計水位偏高。須工程師確認抽水量。通過本 repo 現有 verifier 的檢查項目。"
    g = _replay(task, tmp_path, [_spec("level4/design-large")], reply=reply)
    assert g.status == "fail_reply"
    failed = {c.name for c in g.reply_checks if not c.ok}
    assert {"mentions_outflow_control", "mentions_outflow_threshold"} <= failed


# ---------------------------------------------------------------- chain-full 跨 build


def test_chain_full_requires_all_three_builds(tmp_path):
    task = TASKS["level4/chain-full"]
    g = _replay(
        task, tmp_path, [_spec("level4/chain-full"), _spec("level4/chain-full", "spec-2.json")]
    )
    assert g.status == "fail_verifier" and any("drainage-report" in r for r in g.reasons)


def test_chain_full_cross_checks_catch_inconsistency(tmp_path):
    """建蔽率沒依平面圖外框 / 套疊面積算、屋頂分區沒扣成建物外框 → 跨 build 檢查 fail。"""
    task = TASKS["level4/chain-full"]
    rep = copy.deepcopy(_spec("level4/chain-full", "spec-3.json"))
    rep["report"]["coverage_ratio_pct"] = 50.0
    g = _replay(
        task, tmp_path, [_spec("level4/chain-full"), _spec("level4/chain-full", "spec-2.json"), rep]
    )
    assert g.status == "fail_expected"
    bad = {c.name for c in g.checks + g.cross_checks if not c.ok}
    assert "coverage_ratio_eq_outline_over_overlay" in bad


def test_chain_full_roof_helpers():
    from shapely.geometry import Polygon

    rep = _spec("level4/chain-full", "spec-3.json")
    o = _obs(rep)
    fp = t4._footprint(o)
    assert abs(t4._roof_union(o).area - fp.area) < 1e-6
    e = TASKS["level4/chain-full"].expected()
    assert t4._footprint_xy(o, e)[0]
    shifted = copy.deepcopy(rep)
    shifted["network"]["building_footprints"] = [
        [[x + 1.0, y] for x, y in Polygon(fp).exterior.coords[:-1]]
    ]
    assert not t4._footprint_xy(_obs(shifted), e)[0]


@pytest.mark.parametrize("tid", LEVEL4)
def test_level4_prompt_points_to_level4_inputs(tid):
    assert f"tests/{tid}/input" in TASKS[tid].prompt


def test_level4_grader_fixes():
    import re

    from draftagent.eval.runners import ClaudeCodeRunner
    from draftagent.eval.tasks_level4 import OUTFLOW_THRESHOLD

    # 第 2-1 條答法（0.2 ha）也接受
    assert any(re.search(p, "114-09-03 後都市計畫區新建 ≥ 0.2 ha 屬第 2-1 條") for p in OUTFLOW_THRESHOLD)
    tools = ClaudeCodeRunner().allowed_tools()
    assert "Bash(sed:*)" in tools and "Bash(rm:*)" in tools
