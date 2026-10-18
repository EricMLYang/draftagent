"""管渠設計逕流係數 Pipe.design_c（上游集水分區面積加權 C）。

- schema：design_c 範圍 0–1
- hydro.taoyuan.compute：有 design_c → Q 用它並列 C_design；無 → 沿用全基地綜合 C（舊行為不變）
- drainage.derive.derive_calc：寫入上游加權 C；對衍生 calc spec 直接跑 drainage_calc_check 綠燈
- 計算表管渠列有「C」欄
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from draftagent.drainage import derive
from draftagent.drainage.model import DrainageNetworkSpec
from draftagent.hydro.taoyuan import M2_PER_HA, compute, rational_q
from draftagent.hydro.taoyuan_report import render_table
from draftagent.schema.drainage import TaoyuanSiteDrainageSpec
from draftagent.verify.drainage_calc_check import check

ROOT = Path(__file__).resolve().parent.parent
CALC_TY = ROOT / "examples" / "drainage-calc-ty.json"
PLAN = ROOT / "examples" / "drainage-plan.json"


@pytest.fixture()
def base() -> dict:
    return json.loads(CALC_TY.read_text(encoding="utf-8"))


def _write(tmp_path: Path, d: dict, name: str = "spec.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return p


def _i_col(r: dict) -> float:
    return r["rainfall"]["collection"]["I_mm_hr"]["value"]


@pytest.mark.parametrize("bad", [-0.01, 1.01, 2.0])
def test_design_c_out_of_range_rejected(base, bad):
    d = copy.deepcopy(base)
    d["pipes"][0]["design_c"] = bad
    with pytest.raises(ValidationError):
        TaoyuanSiteDrainageSpec.model_validate(d)


@pytest.mark.parametrize("ok", [0.0, 0.5, 1.0])
def test_design_c_in_range_accepted(base, ok):
    d = copy.deepcopy(base)
    d["pipes"][0]["design_c"] = ok
    assert TaoyuanSiteDrainageSpec.model_validate(d).pipes[0].design_c == ok


def test_q_uses_design_c(base):
    d = copy.deepcopy(base)
    assert d["pipes"], "範例需至少一條管渠"
    d["pipes"][0]["design_c"] = 0.5
    r = compute(TaoyuanSiteDrainageSpec.model_validate(d))
    p0 = r["pipes"][0]
    want = rational_q(0.5, _i_col(r), p0["design_area_m2"] / M2_PER_HA)
    assert p0["Q_design_cms"]["value"] == pytest.approx(want, rel=1e-12)
    assert p0["C_design"]["value"] == 0.5
    assert p0["Q_design_cms"]["source"].startswith("rational(C_upstream")
    # 其他沒給 design_c 的管渠仍用全基地綜合 C
    C = r["C_composite"]["value"]
    for p in r["pipes"][1:]:
        assert "C_design" not in p
        assert p["Q_design_cms"]["value"] == pytest.approx(
            rational_q(C, _i_col(r), p["design_area_m2"] / M2_PER_HA), rel=1e-12
        )


def test_without_design_c_behavior_unchanged(base):
    """舊 spec（無 design_c 鍵）與明確 design_c=None 結果相同，且 Q 用 C_composite。"""
    assert all("design_c" not in p for p in base["pipes"])
    r_old = compute(TaoyuanSiteDrainageSpec.model_validate(base))
    d = copy.deepcopy(base)
    for p in d["pipes"]:
        p["design_c"] = None
    r_none = compute(TaoyuanSiteDrainageSpec.model_validate(d))
    assert r_old == r_none
    C = r_old["C_composite"]["value"]
    for p in r_old["pipes"]:
        assert "C_design" not in p
        assert p["Q_design_cms"]["source"].startswith("rational(C_composite")
        assert p["Q_design_cms"]["value"] == pytest.approx(
            rational_q(C, _i_col(r_old), p["design_area_m2"] / M2_PER_HA), rel=1e-12
        )


def test_calc_table_has_c_column(base):
    d = copy.deepcopy(base)
    d["pipes"][0]["design_c"] = 0.5
    r = compute(TaoyuanSiteDrainageSpec.model_validate(d))
    md = render_table(r)
    assert "| 管渠 | n | 坡度 | 集水面積 m²（坪） | C |" in md
    rows = [ln for ln in md.splitlines() if ln.startswith(f"| {d['pipes'][0]['name']} |")]
    assert rows and "| 0.50 |" in rows[0]
    # 沒給 design_c 的管渠列顯示全基地綜合 C
    if len(d["pipes"]) > 1:
        c = f"| {r['C_composite']['value']:.2f} |"
        assert c in next(
            ln for ln in md.splitlines() if ln.startswith(f"| {d['pipes'][1]['name']} |")
        )


def test_verifier_recomputes_with_design_c(tmp_path, base):
    """result.json 用綜合 C 算、spec 寫了 design_c → 重算不符，紅燈。"""
    d = copy.deepcopy(base)
    r_old = compute(TaoyuanSiteDrainageSpec.model_validate(d))
    d["pipes"][0]["design_c"] = 1.0
    sp = _write(tmp_path, d)
    rp = tmp_path / "result.json"
    rp.write_text(json.dumps(r_old, ensure_ascii=False), encoding="utf-8")
    res = check(sp, rp)
    assert not res.ok
    assert any("Q_design_cms" in e or "C_design" in e for e in res.errors)
    # 用正確的重算結果則綠
    rp.write_text(
        json.dumps(compute(TaoyuanSiteDrainageSpec.model_validate(d)), ensure_ascii=False), "utf-8"
    )
    res = check(sp, rp)
    assert res.ok, res.errors
    assert "C=1.0000" in res.metrics[f"pipe[{d['pipes'][0]['name']}]"]


def test_derive_calc_writes_upstream_c_and_check_green(tmp_path):
    spec = DrainageNetworkSpec.model_validate_json(PLAN.read_text(encoding="utf-8"))
    calc = derive.derive_calc(spec)
    want = derive.pipe_design_c(spec)
    assert {p.name: p.design_c for p in calc.pipes} == want
    assert all(p.design_c is not None for p in calc.pipes)
    r = compute(calc)
    sp = tmp_path / "calc_spec.json"
    sp.write_text(calc.model_dump_json(indent=2) + "\n", encoding="utf-8")
    rp = tmp_path / "result.json"
    rp.write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    md = tmp_path / "calc.md"
    md.write_text(render_table(r) + "\n", encoding="utf-8")
    res = check(sp, rp, md)
    assert res.ok, res.errors
    for p in r["pipes"]:
        assert p["C_design"]["value"] == pytest.approx(want[p["name"]], rel=1e-12)


def test_low_design_c_warns(tmp_path, base):
    d = copy.deepcopy(base)
    d["pipes"][0]["design_c"] = 0.1
    res = check(_write(tmp_path, d))
    assert any("design_c=0.1000 低於全基地綜合 C" in w for w in res.warnings)
