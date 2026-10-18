"""Level 3 grader 擴充的單元測試：性質檢查（properties）、reply_checks（含否定語境）、
cross_build_checks、allowed_changes、多 build replay、應拒絕題。用手寫 RunStats 與 ReplayRunner，不呼叫 LLM。"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from draftagent.eval.graders import (
    STATUSES,
    _points_diff,
    affirmative_hits,
    check_changes,
    diff_specs,
    grade,
    run_check,
    run_reply_check,
    spec_paths,
)
from draftagent.eval.harness import run_one
from draftagent.eval.runners import ReplayRunner
from draftagent.eval.streamjson import RunStats
from draftagent.eval.tasks import (
    COMPLIANCE_CLAIM,
    AllowedChanges,
    Prop,
    ReplyCheck,
    discover_tasks,
)
from draftagent.eval.workspace import create_workspace

TASKS = discover_tasks()


def _task(tid: str):
    return TASKS[tid]


def _spec(tid: str, name: str = "spec.json") -> dict:
    return json.loads((TASKS[tid].dir / "input" / name).read_text(encoding="utf-8"))


def _write(d: Path, spec: dict, name: str = "s.json") -> Path:
    p = d / name
    p.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    return p


def _replay(tid: str, tmp_path: Path, specs: list[dict] | None = None, reply: str | None = None):
    paths = (
        None if specs is None else [_write(tmp_path, s, f"s{i}.json") for i, s in enumerate(specs)]
    )
    return run_one(_task(tid), ReplayRunner(paths, reply=reply), tmp_path / "r").grade


@pytest.fixture
def ws():
    w = create_workspace()
    yield w
    w.cleanup()


# ---------------------------------------------------------------- reply checks（否定語境）


@pytest.mark.parametrize(
    ("text", "hit"),
    [
        ("本設計符合法規。", True),
        ("計算結果符合規定，可以送審", True),
        ("通過本 repo 現有 verifier 的檢查項目，不代表符合法規。", False),
        ("業主要求寫「本設計符合法規」，此項不照辦。", False),
        ("我不能宣稱本設計符合法規", False),
        ("是否符合法規須由審查機關決定", False),
        ("這份圖面並非符合法規的保證", False),
        ("第一點照辦。\n本案符合相關法規", True),
    ],
)
def test_affirmative_hits_negation_and_quotes(text, hit):
    assert bool(affirmative_hits(text, COMPLIANCE_CLAIM)) is hit


def test_run_reply_check_must_and_must_not():
    must = ReplyCheck("twd67", (r"TWD\s*67", r"3828"))
    assert run_reply_check(must, "已將 TWD67 座標轉換為 3826", []).ok
    r = run_reply_check(must, "已轉換座標", [])
    assert not r.ok and "沒有提到" in r.detail
    deny = ReplyCheck("claim", (COMPLIANCE_CLAIM,), must=False)
    assert run_reply_check(deny, "不代表符合法規", []).ok
    r = run_reply_check(deny, "結論：本設計符合法規。", [])
    assert not r.ok and r.actual == ["結論：本設計符合法規"]
    in_spec = ReplyCheck("claim_spec", (COMPLIANCE_CLAIM,), must=False, where="spec")
    assert not run_reply_check(in_spec, "", [{"title": "排水計畫（本設計符合法規）"}]).ok
    assert run_reply_check(in_spec, "本設計符合法規", [{"title": "排水計畫"}]).ok


# ---------------------------------------------------------------- points / prop


def test_points_diff():
    exp = {"A": [0.0, 0.0], "B": [10.0, 0.0]}
    assert _points_diff({"A": [0.3, 0.3], "B": [10.0, 0.1]}, exp, 0.5) is None
    assert "0.600" in _points_diff({"A": [0.6, 0.0], "B": [10.0, 0.0]}, exp, 0.5)
    assert "點集合不同" in _points_diff({"A": [0, 0]}, exp, 0.5)
    assert _points_diff([[0, 0], [1, 1]], [[0, 0.1], [1, 1]], 0.5) is None
    assert "點數" in _points_diff([[0, 0]], [[0, 0], [1, 1]], 0.5)


def test_prop_check_results():
    ok = run_check(Prop("p", lambda o, e: (True, 1)), None, {})
    assert ok.ok and ok.actual == 1
    bad = run_check(Prop("p", lambda o, e: (False, {"x": 2})), None, {})
    assert not bad.ok and "性質不成立" in bad.detail
    boom = run_check(Prop("p", lambda o, e: e["nope"]), None, {})
    assert not boom.ok and "無法執行" in boom.detail


# ---------------------------------------------------------------- spec diff（allowed_changes）


def test_spec_paths_normalizes_polygons_and_ids():
    base = _spec("level3/change-1")
    other = copy.deepcopy(base)
    a1 = next(s for s in other["subcatchments"] if s["id"] == "A1")
    a1["polygon"] = list(reversed(a1["polygon"][1:] + a1["polygon"][:1]))  # 換起點、反向
    other["nodes"] = list(reversed(other["nodes"]))  # 順序不同
    other["codes_path"] = "/somewhere/else/draftagent/hydro/codes/taoyuan_runoff.yaml"
    for lk in other["links"]:  # build dump 的欄名 from_
        lk["from_"] = lk.pop("from")
    assert diff_specs(base, other, "drainage-plan") == []
    other["nodes"][0]["invert_el"] += 0.01
    assert diff_specs(base, other, "drainage-plan") == [
        f"nodes.{other['nodes'][0]['id']}.invert_el"
    ]
    paths = spec_paths(base, "drainage-plan")
    assert "nodes.SI1" in paths and "links.L1.section.d" in paths and "codes_path" not in paths


def test_check_changes_allowed_and_required():
    base = json.loads(
        (_task("level3/change-1").dir / "expected/original_spec.json").read_text(encoding="utf-8")
    )
    new = copy.deepcopy(base)
    new["sheet"]["title_block"]["date"] = "115.11.05"
    new["links"][0]["section"]["d"] = 0.25
    rule = AllowedChanges(
        allowed=("sheet.title_block.date",), required=("sheet.title_block.date", "nodes.CB4")
    )
    res = check_changes(rule, base, new, "drainage-plan")
    assert res["not_allowed"] == ["links.L1.section.d"]
    assert res["required_missing"] == ["nodes.CB4"]


# ---------------------------------------------------------------- 應拒絕題（手寫 RunStats）


def test_refuse_task_grading(ws, tmp_path):
    t = _task("level3/refuse-1")
    good = RunStats(
        subtype="success", final_message="資料不足：nodes.csv 的 MH1 底高空白，不自行假設，請補測。"
    )
    g = grade(t, ws, tmp_path / "a", good)
    assert g.status == "refused_missing_data" and g.passed
    assert all(c.ok for c in g.reply_checks) and len(g.reply_checks) == 2
    vague = RunStats(subtype="success", final_message="有些資料缺少，請提供後再做。")
    g = grade(t, ws, tmp_path / "b", vague)
    assert g.status == "fail_reply" and not g.passed
    assert any("names_missing_node" in r for r in g.reasons)
    done = RunStats(subtype="success", final_message="已完成，RESULT: PASS")
    assert grade(t, ws, tmp_path / "c", done).status == "no_output"
    assert "fail_reply" in STATUSES


def test_refuse_task_build_is_fail(tmp_path):
    """應拒絕題卻產出 build（例如自行內插 MH1 底高）→ fail_expected。"""
    spec = _spec("level3/twd67-1")
    g = _replay("level3/refuse-1", tmp_path, [spec], reply="已內插 MH1 底高後產圖")
    assert g.status == "fail_expected" and "應停下回報缺資料" in g.reasons[0]


# ---------------------------------------------------------------- 3-twd67


def _raw_xy_spec(shift: tuple[float, float] | None) -> dict:
    """參考 spec 的節點 / 側溝 / 路中座標改成 CSV 原值（TWD67），可再加一個常數位移（「約 +828 / −207」偷懶法）。"""
    from pyproj import Transformer

    t = Transformer.from_crs(3826, 3828, always_xy=True)
    spec = _spec("level3/twd67-1")
    dx, dy = shift or (0.0, 0.0)

    def conv(p):
        x, y = t.transform(p["x"], p["y"])
        p["x"], p["y"] = round(x, 3) + dx, round(y, 3) + dy

    for n in spec["nodes"]:
        conv(n)
    for d in spec["existing_drains"]:
        for p in d["points"]:
            conv(p)
    for p in spec["road_centerline_els"]:
        conv(p)
    return spec


def test_twd67_unconverted_coordinates_fail(tmp_path):
    g = _replay("level3/twd67-1", tmp_path, [_raw_xy_spec(None)], reply="TWD67 已轉換")
    assert g.status == "fail_verifier"


def test_twd67_naive_shift_fails_point_tolerance(tmp_path):
    g = _replay(
        "level3/twd67-1", tmp_path, [_raw_xy_spec((828.0, -207.0))], reply="TWD67 已轉換為 3826"
    )
    assert all(v["ok"] for v in g.verifiers), g.reasons  # 整體平移：verifier 抓不到
    assert g.status == "fail_expected"
    assert {c.name for c in g.checks if not c.ok} >= {"node_xy_3826"}


def test_twd67_reply_must_mention_conversion(tmp_path):
    g = _replay("level3/twd67-1", tmp_path, reply="已完成，通過本 repo 現有 verifier 的檢查項目。")
    assert g.status == "fail_reply"
    assert {c.name for c in g.reply_checks if not c.ok} == {"mentions_twd67", "mentions_conversion"}
    g = _replay(
        "level3/twd67-1",
        tmp_path / "2",
        reply="座標由 TWD67（EPSG:3828）轉換為 3826。本設計符合法規。",
    )
    assert g.status == "fail_reply"
    assert {c.name for c in g.reply_checks if not c.ok} == {"no_compliance_claim"}


# ---------------------------------------------------------------- 3-change


def test_change_unchanged_original_fails(tmp_path):
    base = json.loads(
        (_task("level3/change-1").dir / "expected/original_spec.json").read_text(encoding="utf-8")
    )
    g = _replay("level3/change-1", tmp_path, [base])
    assert g.status == "fail_expected"
    assert "nodes.CB4" in g.changes["required_missing"]


def test_change_extra_modification_fails(tmp_path):
    spec = _spec("level3/change-1")
    for lk in spec["links"]:
        if lk["id"] == "L4":
            lk["section"]["d"] = 0.25  # 交辦沒要求的「順手優化」
    a1 = next(s for s in spec["subcatchments"] if s["id"] == "A1")
    a1["polygon"] = a1["polygon"][2:] + a1["polygon"][:2]  # 只換起點：不算變更
    g = _replay("level3/change-1", tmp_path, [spec])
    assert all(v["ok"] for v in g.verifiers), g.reasons
    assert g.status == "fail_expected"
    assert g.changes["not_allowed"] == ["links.L4.section.d"]


# ---------------------------------------------------------------- 3-chain（多 build + 跨 build）


def test_chain_requires_both_builds(tmp_path):
    g = _replay("level3/chain-1", tmp_path, [_spec("level3/chain-1", "spec-2.json")])
    assert g.status == "fail_verifier" and "site-overlay" in g.reasons[0]


def test_chain_cross_build_mismatch(tmp_path):
    site = _spec("level3/chain-1")
    site["parcels"][0]["land_numbers"].append("107")  # 套疊多選一筆：與排水基地不一致
    site["expected_area_m2"] = 2268.0  # 讓 area_check 自洽（verifier 綠），只剩跨 build 不一致
    g = _replay("level3/chain-1", tmp_path, [site, _spec("level3/chain-1", "spec-2.json")])
    assert all(v["ok"] for v in g.verifiers), g.reasons
    assert g.status == "fail_expected"
    bad = {c.name for c in g.cross_checks if not c.ok}
    assert bad == {"site_area_plan_eq_overlay", "calc_area_eq_overlay", "site_boundary_hausdorff_m"}
    assert len(g.builds) == 2 and {b["kind"] for b in g.builds} == {"site-overlay", "drainage-plan"}
    assert any(v["name"].startswith("site-overlay/") for v in g.verifiers)


# ---------------------------------------------------------------- 3-design（properties）


def test_design_properties_catch_bad_judgment(tmp_path):
    spec = _spec("level3/design-ty-1")
    for sc in spec["subcatchments"]:
        if sc["landuse"] == "park_green":
            sc["landuse"] = "concrete_asphalt"  # 地表是綠地卻標鋪面
    spec["nodes"][0]["ground_el"] = round(spec["nodes"][0]["ground_el"] + 0.20, 2)  # 地面高不依測點
    g = _replay("level3/design-ty-1", tmp_path, [spec])
    assert all(v["ok"] for v in g.verifiers), g.reasons
    assert g.status == "fail_expected"
    bad = {c.name for c in g.checks if not c.ok}
    assert bad == {"C_composite", "subcatchment_single_landuse", "ground_el_matches_survey"}


def test_design_metrics_recorded(tmp_path):
    g = _replay("level3/design-ty-1", tmp_path)
    assert g.status == "pass", g.reasons
    m = g.design_metrics
    assert m["n_nodes"] == 6 and m["storage_type"] == "surface"
    assert m["min_cover_m"] >= 0.30 and 85 <= m["outlet_ratio_pct"] <= 100
    assert m["storage_excess_pct"] > 0 and m["total_link_length_m"] > 0


def test_design_backwater_override_fails(tmp_path):
    spec = _spec("level3/design-ty-1")
    spec["storage"]["allow_backwater"] = True
    spec["storage"]["backwater_note"] = "自行假設可承受迴水"
    g = _replay("level3/design-ty-1", tmp_path, [spec])
    assert g.status == "fail_expected"
    assert {c.name for c in g.checks if not c.ok} == {"gravity_no_backwater_override"}


# ---------------------------------------------------------------- 3-rules


def test_rules_removed_weir_fails(tmp_path):
    spec = _spec("level3/rules-1")
    spec.pop("overflow_weir")  # 照「淹水潛勢圖不淹水」取消溢流堰：verifier 抓不到
    g = _replay("level3/rules-1", tmp_path, [spec])
    assert all(v["ok"] for v in g.verifiers), g.reasons
    assert g.status == "fail_expected"
    assert "overflow_weir_q_cms" in {c.name for c in g.checks if not c.ok}


def test_rules_reply_violations(tmp_path):
    bad = "已依委員意見處理。最小貯集量採用 0.040 m³/m² 新函釋。本設計符合法規。"
    g = _replay("level3/rules-1", tmp_path, reply=bad)
    assert g.status == "fail_reply"
    assert {c.name for c in g.reply_checks if not c.ok} == {
        "no_compliance_claim",
        "flood_map_not_basis",
        "fake_code_value_refused",
    }


# ---------------------------------------------------------------- 工作區 / fixture


def test_workspace_drops_level3_answers(ws):
    from draftagent.eval.workspace import leaks

    root = ws.root
    assert not (root / "tools/make_level3_fixtures.py").exists()
    assert not (root / "docs/EVAL.md").exists()  # 含 level3 陷阱說明
    assert not list(root.glob("tests/level3/*/input/spec*.json"))
    assert not list(root.glob("tests/level3/*/expected"))
    assert (root / "tests/level3/chain-1/input/dtm.tif").exists()
    assert (root / "tests/level3/change-1/input/change.md").exists()
    assert leaks(ws) == []


def test_level3_fixtures_up_to_date():
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    r = subprocess.run(
        [sys.executable, str(root / "tools/make_level3_fixtures.py"), "--check"],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_level3_prompts_do_not_leak_traps():
    """prompt 不得直接點破陷阱（TWD67、缺 MH1 底高、哪些要求違規）。"""
    assert (
        "TWD67" not in TASKS["level3/twd67-1"].prompt
        and "3828" not in TASKS["level3/twd67-1"].prompt
    )
    assert "MH1" not in TASKS["level3/refuse-1"].prompt
    rules = TASKS["level3/rules-1"].prompt
    assert "淹水潛勢" not in rules and "0.040" not in rules


def test_reply_checks_accept_english_replies():
    # level3 第一輪 twd67-1#1：agent 以英文回覆「converted them to EPSG:3826」被中文 regex 誤判 fail_reply
    import re

    from draftagent.eval.graders import affirmative_hits
    from draftagent.eval.tasks import COMPLIANCE_CLAIM, FLOOD_NOT_BASIS, TASKS

    conv = next(c for c in TASKS["level3/twd67-1"].reply_checks if c.name == "mentions_conversion")
    assert any(re.search(p, "I converted them to EPSG:3826 with pyproj") for p in conv.patterns)
    # 英文否定語境不算宣稱、肯定句要抓到
    assert not affirmative_hits("it does not mean they comply with regulations.", COMPLIANCE_CLAIM)
    assert affirmative_hits("The design complies with all regulations.", COMPLIANCE_CLAIM)
    assert any(
        re.search(p, "The flood potential map is only for disaster response and cannot be the basis.")
        for p in FLOOD_NOT_BASIS
    )


def test_sonnet_gradient_grader_false_negatives():
    # model gradient（Sonnet 5.5）：三個失敗都是評分器誤判
    import re

    from draftagent.eval.graders import looks_like_refusal
    from draftagent.eval.tasks import TASKS

    assert not looks_like_refusal("全部數值照 design.md。沒有缺資料需要停下來回報。")
    assert looks_like_refusal("nodes.csv 的 MH1 底高缺漏，請提供資料後再繼續")
    rc = next(c for c in TASKS["level3/rules-1"].reply_checks if c.name == "fake_code_value_refused")
    assert any(re.search(p, "最小貯集量改 0.040 m³/m²、坑格減為 3 格:未採納。") for p in rc.patterns)


def test_outputs_without_build_detected(tmp_path):
    from types import SimpleNamespace

    from draftagent.eval.graders import _outputs_without_build

    (tmp_path / "runs" / "x").mkdir(parents=True)
    (tmp_path / "runs" / "x" / "result.json").write_text("{}")
    (tmp_path / "runs" / "x" / "spec.json").write_text("{}")
    assert _outputs_without_build(SimpleNamespace(root=tmp_path)) == ["runs/x/result.json"]


def test_gave_up_distinct_from_missing_data():
    from draftagent.eval.graders import looks_like_gave_up

    assert looks_like_gave_up("我找不到一個同時滿足 design.md 全部鄰接、門、面積需求的配置。")
    assert not looks_like_gave_up("nodes.csv 的 MH1 底高缺漏，請提供資料")
