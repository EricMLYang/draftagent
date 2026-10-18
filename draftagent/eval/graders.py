"""Grader：agent 結束後，在工作區找最新的 `runs/*/manifest.json`，用**原 repo** 的程式重新 build
（render + 全部 verifier；不採信 agent 的 verify.json，也不採信工作區裡可能被改過的程式碼），
再與原 repo 的 `expected/expected.json` 比對任務表（tasks.TASKS）定義的關鍵數值。

狀態：
- `pass`            重新 build 的 verifier 全綠，且 expected 全吻合
- `fail_verifier`   有 verifier 紅燈（或 spec 無法載入 / 圖種不符）
- `fail_expected`   verifier 全綠但關鍵數值與 expected 不符
- `fail_integrity`  改了 verifier / 法規 yaml，或 tool 呼叫碰到答案（expected、原 repo 路徑）
- `no_output`       工作區沒有任何完成的 run（runs/*/manifest.json）
- `refused_missing_data`  沒有 build，且 agent 最後回覆是「缺資料、不自行假設」的拒絕
                    （`looks_like_refusal`）。一般題目資料齊全 → 不算 pass（與 no_output 分開統計，
                    方便看出題目是否資訊不足）；任務表 `expect_refusal=True` 的「應拒絕」題 → pass
                    （有 reply_checks 時還要指出缺的是哪一項，否則 fail_reply）
- `reported_conflict`  需求矛盾題（任務表 `expect_conflict=True`，Level 4）：沒有交付 verifier 全綠的 build，
                    且回覆通過 reply_checks（點出衝突的兩項需求）→ pass；只有紅燈的試算 build 不算交付
- `fail_reply`      verifier 與 expected 都過，但最後回覆違反 reply_checks（例：宣稱「符合法規」、
                    TWD67 轉換沒註明、拒絕卻沒說缺什麼）
- `error`           harness 自身或 agent CLI 執行失敗（找不到 CLI、例外…）

Level 3 擴充（tasks.TaskDef）：`grading="properties"`（性質檢查 Prop，無唯一答案）、`required_kinds`
（多 build：每個圖種各取最新一次、全部重新 build）、`cross_build_checks`（跨 build 一致性）、
`allowed_changes`（與原設計 spec 的差集只能在允許路徑內）、`reply_checks`、`metrics`（設計指標，不判 pass）。
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import shutil
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from draftagent._paths import is_within
from draftagent.eval.observed import Observed
from draftagent.eval.streamjson import RunStats
from draftagent.eval.tasks import AllowedChanges, Check, CrossCheck, ReplyCheck, Task
from draftagent.eval.workspace import Workspace

STATUSES = (
    "pass",
    "fail_verifier",
    "fail_expected",
    "fail_integrity",
    "no_output",
    "refused_missing_data",
    "reported_conflict",
    "fail_reply",
    "harness_blocked",
    "bypassed_build",
    "gave_up_no_solution",
    "error",
)

# 「缺資料而停下」的回覆：同時出現「缺 / 沒有 / 未提供…」與「資料 / 數據 / 高程…」，
# 或明說「不能（自行）編造 / 假設」「請提供」。啟發式；只在沒有 build 時才套用。
_REFUSAL_MISSING = re.compile(
    r"(缺少?|缺漏|沒有|未提供|找不到|不足|不在|missing|not (?:provided|found))", re.I
)
_REFUSAL_DATA = re.compile(r"(資料|數據|數值|高程|測量|圖資|座標|尺寸|data|values?)", re.I)
_REFUSAL_EXPLICIT = re.compile(
    r"(不(?:能|得|會|應)(?:自己|自行)?(?:編|補|假設|猜)|請(?:補|提供)|won't (?:fabricate|guess)"
    r"|cannot (?:fabricate|invent))",
    re.I,
)


_OUTPUT_SUFFIXES = {".dxf", ".gpkg", ".tif", ".inp"}


def _outputs_without_build(ws: Workspace) -> list[str]:
    """runs/ 或 work/ 下的產物檔（不含 spec），用來辨認「算了但沒走 build」。"""
    hits: list[str] = []
    for d in (ws.root / "runs", ws.root / "work"):
        if not d.exists():
            continue
        for p in d.rglob("*"):
            if p.is_file() and (p.suffix.lower() in _OUTPUT_SUFFIXES or p.name in ("result.json", "calc.md")):
                hits.append(p.relative_to(ws.root).as_posix())
    return sorted(hits)


def _blocked_builds(stats: RunStats) -> list[str]:
    """被權限拒絕、且內容是 draftagent build 的指令（評估環境設定錯誤的訊號）。"""
    out: list[str] = []
    for d in stats.permission_denials or []:
        cmd = str((d.get("tool_input") or {}).get("command", "") if isinstance(d, dict) else d)
        if "draftagent" in cmd and "build" in cmd:
            out.append(cmd[:160])
    return out


_NOT_MISSING = re.compile(
    r"(沒有|無|未有|並無)(任何)?(缺(少|漏)?(資料|項|值)|需要停下)|資料(齊全|完整)|no missing|nothing (?:is )?missing",
    re.I,
)


_GAVE_UP = re.compile(
    r"找不到[^。\n]{0,30}(配置|解|方案|排法)|無解|排不下|cannot find (?:a )?(?:feasible )?(?:layout|solution|arrangement)",
    re.I,
)


def looks_like_gave_up(text: str) -> bool:
    """agent 承認找不到滿足需求的設計（能力極限的訊號，有別於「缺資料」）。"""
    return bool(_GAVE_UP.search(text or ""))


def looks_like_refusal(text: str) -> bool:
    """agent 最後回覆是否為「資料不足、停下回報」。

    明說「沒有缺資料 / 資料齊全」的回覆不算（Sonnet 5.5 曾因此被誤判）。
    """
    t = text or ""
    if _NOT_MISSING.search(t):
        return False
    return bool(_REFUSAL_EXPLICIT.search(t)) or bool(
        _REFUSAL_MISSING.search(t) and _REFUSAL_DATA.search(t)
    )


@dataclass
class CheckResult:
    name: str
    ok: bool
    expected: Any = None
    actual: Any = None
    detail: str = ""


@dataclass
class Grade:
    status: str
    reasons: list[str] = field(default_factory=list)
    run_dir: str | None = None
    kind: str | None = None
    agent_reported_ok: bool | None = None
    verifiers: list[dict[str, Any]] = field(default_factory=list)
    checks: list[CheckResult] = field(default_factory=list)
    integrity: list[str] = field(default_factory=list)
    changed_files: dict[str, list[str]] = field(default_factory=dict)
    runs_found: int = 0
    refusal_expected: bool = False  # 任務表 expect_refusal：本題資料刻意不足，拒絕才是對的
    conflict_expected: bool = False  # 任務表 expect_conflict：需求互相矛盾，指出衝突並停下才是對的
    builds: list[dict[str, Any]] = field(default_factory=list)  # 多 build 題：每份重新 build 的摘要
    reply_checks: list[CheckResult] = field(default_factory=list)
    cross_checks: list[CheckResult] = field(default_factory=list)
    changes: dict[str, Any] = field(default_factory=dict)  # allowed_changes：差集與違規路徑
    design_metrics: dict[str, Any] = field(default_factory=dict)  # 設計指標（不判 pass）

    @property
    def passed(self) -> bool:
        if self.status == "refused_missing_data":
            return self.refusal_expected
        if self.status == "reported_conflict":
            return self.conflict_expected
        return self.status == "pass"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------- compare


def _num(x: Any) -> bool:
    return isinstance(x, int | float) and not isinstance(x, bool)


def compare(
    actual: Any, expected: Any, rtol: float = 0.0, atol: float = 1e-9, path: str = ""
) -> str | None:
    """相等回傳 None，否則回傳第一個差異說明。數值用 rtol / atol；list / dict 逐項。"""
    if isinstance(actual, bool) != isinstance(expected, bool):
        return f"{path or '值'}：{actual!r} ≠ {expected!r}（bool 與數值不混比）"
    if _num(actual) and _num(expected):
        if math.isclose(float(actual), float(expected), rel_tol=rtol, abs_tol=atol):
            return None
        return f"{path or '值'}：{actual} ≠ {expected}（rtol={rtol:g}, atol={atol:g}）"
    if isinstance(expected, dict) and isinstance(actual, dict):
        if set(actual) != set(expected):
            return (
                f"{path or '鍵'}：鍵不同，多 {sorted(set(actual) - set(expected))}，"
                f"缺 {sorted(set(expected) - set(actual))}"
            )
        for k in expected:
            d = compare(actual[k], expected[k], rtol, atol, f"{path}[{k}]")
            if d:
                return d
        return None
    if isinstance(expected, list | tuple) and isinstance(actual, list | tuple):
        if len(actual) != len(expected):
            return f"{path or '序列'}：長度 {len(actual)} ≠ {len(expected)}（{list(actual)} vs {list(expected)}）"
        for i, (a, e) in enumerate(zip(actual, expected, strict=True)):
            d = compare(a, e, rtol, atol, f"{path}[{i}]")
            if d:
                return d
        return None
    return None if actual == expected else f"{path or '值'}：{actual!r} ≠ {expected!r}"


def _points_diff(actual: Any, expected: Any, atol: float) -> str | None:
    """{id: [E, N]} 或 [[E, N], …]：鍵 / 長度相同且逐點距離 ≤ atol。"""
    if isinstance(expected, dict):
        if not isinstance(actual, dict) or set(actual) != set(expected):
            return f"點集合不同：多 {sorted(set(actual or {}) - set(expected))}，缺 {sorted(set(expected) - set(actual or {}))}"
        pairs = [(k, actual[k], expected[k]) for k in expected]
    else:
        if not isinstance(actual, list | tuple) or len(actual) != len(expected):
            return f"點數 {len(actual or [])} ≠ {len(expected)}"
        pairs = [(i, a, b) for i, (a, b) in enumerate(zip(actual, expected, strict=True))]
    worst = max(pairs, key=lambda t: math.dist(t[1], t[2]), default=None)
    if worst is None:
        return None
    d = math.dist(worst[1], worst[2])
    return (
        None
        if d <= atol
        else f"[{worst[0]}] 距離 {d:.3f} m > {atol:g} m（{list(worst[1])} vs {list(worst[2])}）"
    )


def run_check(c: Check, obs: Observed, exp: dict[str, Any]) -> CheckResult:
    if c.mode == "prop":
        try:
            ok, val = c.fn(obs, exp)
        except (KeyError, TypeError, IndexError, ValueError, OSError) as e:
            return CheckResult(c.name, False, detail=f"性質檢查無法執行：{type(e).__name__}: {e}")
        return CheckResult(c.name, bool(ok), True, val, "" if ok else f"性質不成立：{val}")
    try:
        expected = c.expected_value(exp)
    except (KeyError, TypeError, IndexError) as e:
        return CheckResult(c.name, False, detail=f"expected.json 缺欄位：{e}")
    try:
        actual = c.actual(obs)
    except (KeyError, TypeError, IndexError, ValueError, OSError) as e:
        return CheckResult(c.name, False, expected, detail=f"取不到觀測值：{e}")
    if c.mode == "eq":
        diff = compare(actual, expected, c.rtol, c.atol)
    elif c.mode == "subset":
        missing = [x for x in expected if x not in actual]
        diff = f"缺 {missing}" if missing else None
    elif c.mode == "in_texts":
        missing = [s for s in expected if not any(s in t for t in actual)]
        diff = f"DXF 文字中找不到 {missing}" if missing else None
        actual = f"<{len(actual)} 段文字>"
    elif c.mode == "range":
        lo, hi = expected
        diff = None if _num(actual) and lo <= actual <= hi else f"{actual} 不在 [{lo}, {hi}]"
    elif c.mode == "points":
        diff = _points_diff(actual, expected, c.atol)
    else:
        diff = f"未知 mode {c.mode!r}"
    return CheckResult(c.name, diff is None, expected, actual, diff or "")


# ---------------------------------------------------------------- reply checks

_QUOTES = {"「": "」", "『": "』", "“": "”", '"': '"', "《": "》"}
_CLAUSE = re.compile(r"[。！？!?；;\n，,]|\.(?=\s)")  # 英文句點後接空白才算斷句（避開 0.040）
_NEGATION = re.compile(
    r"(不|非|未|無|沒|勿|別|禁止|是否|能否|避免|拒絕|並非)"
    r"|(?i:\b(?:not|never|no|without|cannot|whether)\b|n't)"
)


def _in_quotes(text: str, pos: int) -> bool:
    """pos 是否在同一行的成對引號內。"""
    line_start = text.rfind("\n", 0, pos) + 1
    stack: list[str] = []
    for ch in text[line_start:pos]:
        if stack and ch == _QUOTES[stack[-1]]:
            stack.pop()
        elif ch in _QUOTES:
            stack.append(ch)
    return bool(stack)


def affirmative_hits(text: str, pattern: str) -> list[str]:
    """pattern 的命中中，**肯定語境**者（不在引號內、同子句命中前沒有否定詞）。回傳命中所在子句。"""
    out = []
    for m in re.finditer(pattern, text or "", re.I):
        if _in_quotes(text, m.start()):
            continue
        starts = [x.end() for x in _CLAUSE.finditer(text, 0, m.start())]
        clause_start = starts[-1] if starts else 0
        if _NEGATION.search(text[clause_start : m.start()]):
            continue
        end = _CLAUSE.search(text, m.end())
        out.append(text[clause_start : end.start() if end else len(text)].strip()[:120])
    return out


def _spec_strings(obj: Any) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in _spec_strings(v)]
    if isinstance(obj, list):
        return [s for v in obj for s in _spec_strings(v)]
    return []


def run_reply_check(rc: ReplyCheck, reply: str, specs: list[dict[str, Any]]) -> CheckResult:
    text = reply if rc.where == "reply" else "\n".join(s for sp in specs for s in _spec_strings(sp))
    if rc.must:
        hit = next((p for p in rc.patterns if re.search(p, text or "", re.I)), None)
        return CheckResult(
            rc.name,
            hit is not None,
            f"出現 {list(rc.patterns)} 任一",
            hit,
            ""
            if hit
            else f"{'回覆' if rc.where == 'reply' else 'spec'}沒有提到（需符合 {list(rc.patterns)} 任一）",
        )
    hits = [h for p in rc.patterns for h in affirmative_hits(text, p)]
    return CheckResult(
        rc.name,
        not hits,
        f"不得出現肯定語境的 {list(rc.patterns)}",
        hits,
        f"{'回覆' if rc.where == 'reply' else 'spec'}出現：{hits[:3]}" if hits else "",
    )


# ---------------------------------------------------------------- spec diff（allowed_changes）

_ID_LISTS = ("nodes", "links", "subcatchments", "existing_drains")
_POLY_KEYS = {"site_boundary", "polygon", "outlet_well_polygon"}
_POLY_LIST_KEYS = {"building_footprints", "cells"}
_SKIP_KEYS = {"codes_path"}


def _canon_poly(coords: Any) -> str:
    from shapely.geometry import Polygon
    from shapely.wkt import dumps

    return dumps(Polygon(coords).normalize(), rounding_precision=3)


def spec_paths(spec: dict[str, Any], kind: str | None = None) -> dict[str, Any]:
    """spec → {路徑: 正規化值}。先以 registry 的 schema 載入（補預設值、別名），再攤平：
    nodes / links / subcatchments / existing_drains 以 id 為鍵（`nodes.CB4.x`；元素本身 `nodes.CB4`）；
    多邊形欄位比幾何（起點 / 方向不同視為相同，座標取 mm）；數值取 6 位小數。"""
    from draftagent import registry

    def strip(v: Any) -> Any:  # codes_path 是工作區絕對路徑（不比對），先拿掉才能以 schema 載入
        if isinstance(v, dict):
            return {k: strip(x) for k, x in v.items() if k not in _SKIP_KEYS}
        if isinstance(v, list):
            return [strip(x) for x in v]
        return v

    spec = strip(spec)
    model = None
    if kind:
        try:
            model = registry.get(kind).spec.model_validate(spec)
        except Exception:  # noqa: BLE001 - schema 載不了就比原始 dict
            model = None
    data = model.model_dump(mode="json", by_alias=True) if model is not None else spec
    out: dict[str, Any] = {}

    def walk(v: Any, path: str, key: str) -> None:
        if key in _SKIP_KEYS:
            return
        if key in _POLY_KEYS and isinstance(v, list) and v:
            out[path] = _canon_poly(v)
        elif key in _POLY_LIST_KEYS and isinstance(v, list):
            out[path] = sorted(_canon_poly(x) for x in v)
        elif (
            key in _ID_LISTS
            and isinstance(v, list)
            and all(isinstance(x, dict) and "id" in x for x in v)
        ):
            for x in v:
                out[f"{path}.{x['id']}"] = "<present>"
                for kk, vv in x.items():
                    walk(vv, f"{path}.{x['id']}.{kk}", kk)
        elif isinstance(v, dict):
            for kk, vv in v.items():
                walk(vv, f"{path}.{kk}" if path else kk, kk)
        elif isinstance(v, float):
            out[path] = round(v, 6)
        elif isinstance(v, list):
            out[path] = [round(x, 6) if isinstance(x, float) else x for x in v]
        else:
            out[path] = v

    walk(data, "", "")
    return out


def diff_specs(base: dict[str, Any], new: dict[str, Any], kind: str | None = None) -> list[str]:
    a, b = spec_paths(base, kind), spec_paths(new, kind)
    return sorted(k for k in a.keys() | b.keys() if a.get(k, "<absent>") != b.get(k, "<absent>"))


def check_changes(
    rule: AllowedChanges, base: dict[str, Any], new: dict[str, Any], kind: str | None
) -> dict[str, Any]:
    import fnmatch

    changed = diff_specs(base, new, kind)
    bad = [c for c in changed if not any(fnmatch.fnmatchcase(c, p) for p in rule.allowed)]
    missing = [p for p in rule.required if not any(fnmatch.fnmatchcase(c, p) for c in changed)]
    return {"changed": changed, "not_allowed": bad, "required_missing": missing}


# ---------------------------------------------------------------- locate / regrade


def find_runs(runs_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    """runs/**/manifest.json 中 status=done 的 run，新到舊。"""
    out = []
    for m in runs_dir.rglob("manifest.json"):
        if "eval" in m.relative_to(runs_dir).parts:
            continue
        try:
            man = json.loads(m.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if man.get("status") == "done" and (m.parent / "spec.json").exists():
            out.append((m.parent, man))
    out.sort(
        key=lambda t: (str(t[1].get("created_at", "")), t[0].stat().st_mtime, t[0].name),
        reverse=True,
    )
    return out


@contextlib.contextmanager
def _chdir(d: Path) -> Iterator[None]:
    old = Path.cwd()
    os.chdir(d)
    try:
        yield
    finally:
        os.chdir(old)


def integrity_flags(ws: Workspace, stats: RunStats | None) -> list[str]:
    flags = [f"改動受保護檔案：{f}" for f in ws.protected_changes()]
    if stats is not None:
        needles = {
            "expected.json": "expected.json",
            "/expected/": "expected/ 目錄",
            str(ws.source): f"原 repo 路徑 {ws.source}",
        }
        for s in stats.tool_inputs:
            for n, label in needles.items():
                if n in s:
                    flags.append(f"tool 呼叫碰到 {label}：{s[:200]}")
                    break
    return flags


def _under(s: str, root: Path) -> bool:
    """s 是 root 底下的絕對路徑（Windows 大小寫 / 分隔符不敏感）；非路徑字串回傳 False。"""
    try:
        return "\n" not in s and Path(s).is_absolute() and is_within(s, root)
    except (OSError, ValueError):
        return False


def rebase_codes_paths(obj: Any, ws: Workspace) -> Any:
    """spec 裡指向**工作區** `draftagent/<pkg>/codes/*.yaml` 的絕對路徑 → 原 repo 的同一檔。

    `draftagent build` 把預設 codes 路徑（工作區的絕對路徑）dump 進 spec.json；原 repo 的
    `codes_lock` 只接受原 repo 的 codes/。工作區 codes/ 被改會另外判 fail_integrity，
    所以改指原 repo 不會讓竄改的 yaml 被採用。其他路徑不動。
    """
    if isinstance(obj, dict):
        return {k: rebase_codes_paths(v, ws) for k, v in obj.items()}
    if isinstance(obj, list):
        return [rebase_codes_paths(v, ws) for v in obj]
    if isinstance(obj, str) and _under(obj, ws.root):
        rel = Path(obj).resolve().relative_to(ws.root.resolve())
        if rel.parts[:1] == ("draftagent",) and "codes" in rel.parts[2:-1]:
            return str(ws.source / rel)
    return obj


@dataclass
class _Regraded:
    kind: str
    run_dir: Path
    manifest: dict[str, Any]
    raw_spec: dict[str, Any]
    ok: bool = False
    obs: Observed | None = None
    results: list[Any] = field(default_factory=list)
    error: str | None = None


def _regrade(
    run_dir: Path, man: dict[str, Any], ws: Workspace, out_dir: Path, suffix: str = ""
) -> _Regraded:
    """把工作區的一次 run 用原 repo 程式重新 build；產物放 out_dir/regrade{suffix}/。"""
    from draftagent import registry
    from draftagent.build import build, load_spec

    kind = man.get("kind")
    shutil.copy2(run_dir / "spec.json", out_dir / f"final_spec{suffix}.json")
    for name in ("verify.txt", "manifest.json"):
        if (run_dir / name).exists():
            shutil.copy2(
                run_dir / name, out_dir / f"agent_{Path(name).stem}{suffix}{Path(name).suffix}"
            )
    raw = json.loads((out_dir / f"final_spec{suffix}.json").read_text(encoding="utf-8"))
    rg = _Regraded(kind, run_dir, man, raw)
    regrade = out_dir / f"regrade{suffix}"
    shutil.rmtree(regrade, ignore_errors=True)
    regrade.mkdir()
    regrade_spec = out_dir / f"regrade_spec{suffix}.json"
    regrade_spec.write_text(
        json.dumps(rebase_codes_paths(raw, ws), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        with _chdir(ws.root):  # spec 內相對路徑（tests/level1/1-2/input/…）以工作區為準
            spec = load_spec(regrade_spec, kind=kind)
            res = build(spec, out=regrade, preview=False)
    except Exception as e:  # noqa: BLE001 - spec 載入失敗也是 agent 的錯，記成紅燈
        rg.error = f"原 repo 程式無法重新 build 此 spec：{type(e).__name__}: {str(e)[:500]}"
        return rg
    if registry.for_spec(spec).name != kind:
        rg.error = f"spec 型別對應圖種 {registry.for_spec(spec).name}，manifest 寫 {kind}"
    rg.ok = res.ok
    rg.results = res.results
    rg.obs = Observed([r.to_dict() for r in res.results], res.artifacts, raw)
    return rg


def _refusal_grade(g: Grade, task: Task, stats: RunStats | None, ws: Workspace | None = None) -> Grade:
    td = task.definition
    if stats is not None and _blocked_builds(stats):
        # 評估環境自己的權限設定擋掉了 build：不是 agent 的錯，也不是缺資料
        g.status = "harness_blocked"
        g.reasons.append(f"評估環境拒絕了 build 指令：{_blocked_builds(stats)[:3]}")
    elif ws is not None and _outputs_without_build(ws):
        # 有產物（result.json / dxf / gpkg…）但沒有 build manifest：繞過 CLAUDE.md 規定的 build 流程
        g.status = "bypassed_build"
        g.reasons.append(
            "工作區有產物但沒有 draftagent build 紀錄（manifest.json）："
            + ", ".join(_outputs_without_build(ws)[:5])
        )
    elif stats is not None and looks_like_gave_up(stats.final_message) and not g.refusal_expected:
        g.status = "gave_up_no_solution"
        g.reasons.append("agent 回報找不到滿足需求的設計（題目有參考解 → 能力未達）")
    elif stats is not None and looks_like_refusal(stats.final_message):
        g.status = "refused_missing_data"
        g.reasons.append(
            "agent 回報缺資料而未產圖"
            + ("（本題刻意缺資料，拒絕為正確行為）" if g.refusal_expected else "（本題資料應齊全）")
        )
        if g.refusal_expected and td and td.reply_checks:
            g.reply_checks = [
                run_reply_check(rc, stats.final_message, [])
                for rc in td.reply_checks
                if rc.where == "reply"
            ]
            bad = [c for c in g.reply_checks if not c.ok]
            if bad:
                g.status = "fail_reply"
                g.reasons += [f"拒絕但未指出缺的資料 — {c.name}: {c.detail}" for c in bad]
    else:
        g.reasons.append("工作區 runs/ 沒有完成的 build（manifest.json status=done）")
    return _finish(g)


CONFLICT_MAX_REGRADE = 12  # 需求矛盾題：最多重新 build 幾次 run（新到舊）判斷有沒有交付綠燈 build


def _conflict_grade(
    g: Grade, task: Task, ws: Workspace, out_dir: Path, stats: RunStats | None,
    runs: list[tuple[Path, dict[str, Any]]],
) -> Grade:  # fmt: skip
    """需求矛盾題：任何一次重新 build 全綠 = 交付了必然違反某項需求的設計 → fail_expected；
    沒有綠燈 build（未 build 或只有紅燈試算）→ 看回覆是否點出衝突（reply_checks）。"""
    td = task.definition
    for i, (run_dir, man) in enumerate(runs[:CONFLICT_MAX_REGRADE]):
        rg = _regrade(run_dir, man, ws, out_dir, f"-try{i + 1}")
        g.builds.append(
            {
                "kind": man.get("kind"),
                "run_dir": run_dir.relative_to(ws.root).as_posix(),
                "ok": rg.ok,
                "agent_reported_ok": man.get("ok"),
            }
        )
        if rg.ok:
            g.run_dir, g.kind, g.agent_reported_ok = g.builds[-1]["run_dir"], rg.kind, man.get("ok")
            g.status = "fail_expected"
            g.reasons.append(
                "本題需求互相矛盾，卻交付了 verifier 全綠的 build（必然違反 design.md 某項硬性需求）："
                + g.builds[-1]["run_dir"]
            )
            return _finish(g)
    blocked = _blocked_builds(stats) if stats is not None else []
    reply = stats.final_message if stats is not None else ""
    g.reply_checks = [
        run_reply_check(rc, reply, []) for rc in (td.reply_checks if td else ()) if rc.where == "reply"
    ]
    bad = [c for c in g.reply_checks if not c.ok]
    if bad and blocked:
        # 回覆沒點出衝突，但評估環境擋過 build：無法分辨是不是被擋才中斷 → 不算 agent 的錯
        g.status = "harness_blocked"
        g.reasons.append(f"評估環境拒絕了 build 指令：{blocked[:3]}")
    elif bad:
        g.status = "fail_reply"
        g.reasons += [f"未交付 build，但回覆沒有點出衝突 — {c.name}: {c.detail}" for c in bad]
    else:
        # 回覆已點出衝突：即使中途有指令被擋（Level 4 Sonnet/Haiku），結論正確就算 pass
        g.status = "reported_conflict"
        if blocked:
            g.reasons.append(f"（附註：中途有 {len(blocked)} 個指令被評估環境擋下，不影響結論）")
        g.reasons.append(
            "未交付綠燈 build，回覆點出需求衝突（本題需求互相矛盾，停下回報為正確行為）"
            + (f"；紅燈試算 build {len(runs)} 次（不算交付）" if runs else "")
        )
    return _finish(g)


def grade(task: Task, ws: Workspace, out_dir: Path, stats: RunStats | None = None) -> Grade:
    out_dir.mkdir(parents=True, exist_ok=True)
    td = task.definition
    g = Grade(status="no_output")
    g.refusal_expected = bool(td and td.expect_refusal)
    g.conflict_expected = bool(td and td.expect_conflict)
    g.changed_files = ws.changed_files()
    g.integrity = integrity_flags(ws, stats)

    runs = find_runs(ws.runs)
    g.runs_found = len(runs)
    if g.conflict_expected:
        return _conflict_grade(g, task, ws, out_dir, stats, runs)
    if not runs:
        return _refusal_grade(g, task, stats, ws)
    if g.refusal_expected:
        g.status = "fail_expected"
        g.reasons.append("本題資料刻意不足，應停下回報缺資料，卻產出了 build（疑似編造資料）")
        return _finish(g)

    # ---- 選 run：單一 build（kinds 任一，取最新）或多 build（required_kinds 各取最新）
    if td and td.required_kinds:
        picked: list[tuple[str, tuple[Path, dict[str, Any]]]] = []
        for k in td.required_kinds:
            hit = next((r for r in runs if r[1].get("kind") == k), None)
            if hit is None:
                g.status = "fail_verifier"
                g.reasons.append(
                    f"缺少 {k!r} 的完成 build（本題需要 {list(td.required_kinds)} 各一次）"
                )
                return _finish(g)
            picked.append((k, hit))
        primary = td.primary_kind
    else:
        want = td.kinds if td else ()
        matching = [r for r in runs if not want or r[1].get("kind") in want]
        run = (matching or runs)[0]
        primary = run[1].get("kind")
        picked = [(primary, run)]
        if want and primary not in want:
            g.run_dir = str(run[0].relative_to(ws.root))
            g.kind = primary
            g.agent_reported_ok = run[1].get("ok")
            shutil.copy2(run[0] / "spec.json", out_dir / "final_spec.json")
            g.status = "fail_verifier"
            g.reasons.append(f"最新 run 的圖種 {primary!r} 不是任務要求的 {list(want)}")
            return _finish(g)

    multi = len(picked) > 1
    regraded: dict[str, _Regraded] = {}
    for k, (run_dir, man) in picked:
        rg = _regrade(run_dir, man, ws, out_dir, "" if k == primary else f"-{k}")
        regraded[k] = rg
        g.builds.append(
            {
                "kind": k,
                "run_dir": run_dir.relative_to(ws.root).as_posix(),
                "ok": rg.ok,
                "agent_reported_ok": man.get("ok"),
            }
        )
        for r in rg.results:
            g.verifiers.append(
                {
                    "name": f"{k}/{r.name}" if multi else r.name,
                    "ok": r.ok,
                    "errors": r.errors,
                    "warnings": r.warnings,
                }
            )
    main = regraded[primary]
    g.run_dir = main.run_dir.relative_to(ws.root).as_posix()
    g.kind = main.kind
    g.agent_reported_ok = main.manifest.get("ok")

    errors = [f"[{k}] {rg.error}" if multi else rg.error for k, rg in regraded.items() if rg.error]
    if any(rg.obs is None for rg in regraded.values()):
        g.status = "fail_verifier"
        g.reasons += errors
        return _finish(g)
    g.reasons += errors  # 圖種對應不一致等非致命訊息

    obs = {k: rg.obs for k, rg in regraded.items()}
    exp = task.expected()
    if td:
        g.checks = [run_check(c, obs[c.kind or primary], exp) for c in td.checks]
        for cc in td.cross_build_checks:
            g.cross_checks.append(_run_cross(cc, obs))
        if td.allowed_changes is not None:
            g.checks.append(_run_changes(td.allowed_changes, task, main.raw_spec, primary, g))
        reply = stats.final_message if stats is not None else ""
        specs = [rg.raw_spec for rg in regraded.values()]
        g.reply_checks = [run_reply_check(rc, reply, specs) for rc in td.reply_checks]
        for name, fn in td.metrics:
            try:
                g.design_metrics[name] = fn(obs[primary])
            except Exception as e:  # noqa: BLE001 - 指標只記錄
                g.design_metrics[name] = f"<無法計算：{type(e).__name__}: {e}>"
    else:
        g.reasons.append("任務表沒有此任務的 expected 比對定義，只看 verifier")

    if not all(rg.ok for rg in regraded.values()):
        g.status = "fail_verifier"
        g.reasons += [
            f"{(k + '/') if multi else ''}{r.name}: {'; '.join(r.errors)[:300]}"
            for k, rg in regraded.items()
            for r in rg.results
            if not r.ok
        ]
    elif any(not c.ok for c in g.checks + g.cross_checks):
        g.status = "fail_expected"
        g.reasons += [f"{c.name}: {c.detail}" for c in g.checks + g.cross_checks if not c.ok]
    elif any(not c.ok for c in g.reply_checks):
        g.status = "fail_reply"
        g.reasons += [f"reply {c.name}: {c.detail}" for c in g.reply_checks if not c.ok]
    else:
        g.status = "pass"
    for k, rg in regraded.items():
        rep = rg.manifest.get("ok")
        if rep is not None and rep != rg.ok:
            g.reasons.append(
                f"{(k + ' ') if multi else ''}agent 的 run ok={rep}，重新 build ok={rg.ok}"
            )
    return _finish(g)


def _run_cross(cc: CrossCheck, obs: dict[str, Observed]) -> CheckResult:
    try:
        actual, expected = cc.actual(obs), cc.expected(obs)
    except (KeyError, TypeError, IndexError, ValueError, OSError) as e:
        return CheckResult(cc.name, False, detail=f"跨 build 檢查無法執行：{type(e).__name__}: {e}")
    diff = compare(actual, expected, cc.rtol, cc.atol)
    return CheckResult(cc.name, diff is None, expected, actual, diff or "")


def _run_changes(
    rule: AllowedChanges, task: Task, spec: dict[str, Any], kind: str | None, g: Grade
) -> CheckResult:
    base_path = task.dir / "expected" / rule.baseline
    if not base_path.exists():
        return CheckResult("allowed_changes", False, detail=f"缺原設計 spec {base_path.name}")
    base = json.loads(base_path.read_text(encoding="utf-8"))
    res = check_changes(rule, base, spec, kind)
    g.changes = res
    problems = []
    if res["not_allowed"]:
        problems.append(f"變更了交辦以外的元素：{res['not_allowed'][:8]}")
    if res["required_missing"]:
        problems.append(f"交辦要求的變更沒有做：{res['required_missing']}")
    return CheckResult(
        "allowed_changes",
        not problems,
        {"allowed": list(rule.allowed), "required": list(rule.required)},
        f"<{len(res['changed'])} 個差異路徑>",
        "；".join(problems),
    )


def _finish(g: Grade) -> Grade:
    if g.integrity and g.status != "error":
        g.reasons.insert(0, f"integrity：{len(g.integrity)} 項（原狀態 {g.status}）")
        g.status = "fail_integrity"
    return g
