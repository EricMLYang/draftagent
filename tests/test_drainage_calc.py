"""桃園建築基地流出抑制計算：hydro.taoyuan、schema.drainage、verify.drainage_calc_check。

標準答案取自設計參考手冊（草案）官方算例（見 docs/research/2026-10-17-taoyuan-drainage.md §3）：
- 桃園站 T=20、t=5 min → I = 161.78 mm/hr
- 1680 m² 基地：必要貯集量 85.68 m³、4 座筏基坑提供 90 m³ 通過、Q20 = 0.0596 cms
"""

from __future__ import annotations

import copy
import json
import math
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from draftagent.hydro.taoyuan import (
    compute,
    horner_intensity,
    load_codes,
    manning_circular,
    normal_depth_circular,
    orifice_q,
    rational_q,
    required_storage,
    station_for_district,
    weir_q,
)
from draftagent.schema.drainage import TaoyuanSiteDrainageSpec
from draftagent.verify.drainage_calc_check import check

ROOT = Path(__file__).resolve().parent.parent
TASK = ROOT / "tests" / "level2" / "drainage-ty-1"
SPEC = TASK / "input" / "spec.json"
EXPECTED = json.loads((TASK / "expected" / "expected.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def codes():
    return load_codes()


@pytest.fixture()
def base() -> dict:
    return json.loads(SPEC.read_text(encoding="utf-8"))


def _write(tmp_path: Path, d: dict, name: str = "spec.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return p


# ---------------------------------------------------------------- 官方算例


def test_horner_taoyuan_T20_t5(codes):
    assert horner_intensity("桃園", 20, 5, codes) == pytest.approx(161.78, abs=0.01)


def test_station_for_district(codes):
    assert station_for_district("桃園區", codes) == "桃園"
    with pytest.raises(KeyError):
        station_for_district("復興區", codes)


def test_required_storage_1680(codes):
    r = required_storage(1680.0, codes)
    assert r["required_m3"]["value"] == pytest.approx(85.68, abs=1e-9)
    assert r["building_code_m3"]["value"] == pytest.approx(75.6, abs=1e-9)
    assert r["adopted"] == "runoff_standard"


def test_q20_1680_official():
    q = rational_q(0.79, 161.78, 0.168)
    assert q == pytest.approx(0.0596, abs=5e-5)


def test_compute_level2_matches_expected():
    r = compute(TaoyuanSiteDrainageSpec.from_json(SPEC))
    e = EXPECTED
    assert r["station"]["value"] == e["station"]
    assert r["C_composite"]["value"] == pytest.approx(e["C_composite"])
    assert r["rainfall"]["storage"]["I_mm_hr"]["value"] == pytest.approx(e["I20_mm_hr"], abs=e["I20_tol"])
    assert r["rainfall"]["storage"]["Q_site_cms"]["value"] == pytest.approx(e["Q20_cms"], abs=e["Q20_tol"])
    assert r["storage_required"]["required_m3"]["value"] == pytest.approx(e["storage_required_m3"])
    assert r["storage_provided"]["provided_m3"]["value"] == pytest.approx(e["storage_provided_m3"])
    assert r["release"]["allowed_cms"]["value"] == pytest.approx(e["release_allowed_cms"])
    # 每個數值都附 yaml 出處
    assert r["rainfall"]["storage"]["I_mm_hr"]["source"] == "horner.stations.桃園.T20"
    assert r["storage_required"]["required_m3"]["source"] == "runoff_standard.min_storage_m3_per_m2"


def test_cli_then_verifier_green(tmp_path):
    out = tmp_path / "result.json"
    cli = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "drainage_calc.py"), str(SPEC), "-o", str(out)],
        capture_output=True, text=True, cwd=ROOT, check=True,
    )
    assert "| **必要貯集量（採用）** | 85.68 |" in cli.stdout
    assert "坪" in cli.stdout
    ver = subprocess.run(
        [sys.executable, "-m", "draftagent.verify.drainage_calc_check", str(SPEC), "--result", str(out)],
        capture_output=True, text=True, cwd=ROOT, check=False,
    )
    assert ver.returncode == 0, ver.stdout
    assert "PASS" in ver.stdout


# ---------------------------------------------------------------- 公式


def test_orifice_and_weir_formulas(codes):
    ow = codes["orifice_weir"]
    assert orifice_q("circ_orifice", 1.0, codes, diameter_m=0.1) == pytest.approx(
        ow["circ_orifice_coeff"] * 0.01 * math.sqrt(0.95)
    )
    assert orifice_q("rect_orifice", 1.0, codes, height_m=0.1, width_m=0.2) == pytest.approx(
        ow["rect_orifice_coeff"] * 0.1 * 0.2 * math.sqrt(0.95)
    )
    assert weir_q(0.5, 0.2, codes) == pytest.approx(ow["rect_weir_coeff"] * 0.5 * 0.2**1.5)
    with pytest.raises(ValueError):
        orifice_q("circ_orifice", 0.05, codes, diameter_m=0.1)  # 未淹沒


def test_manning_circular_full_and_partial():
    full = manning_circular(0.3, 0.013, 0.01)
    # 滿管 V = (1/n)(D/4)^(2/3) S^0.5
    assert full["V_m_s"] == pytest.approx((1 / 0.013) * (0.075 ** (2 / 3)) * 0.1)
    half = manning_circular(0.3, 0.013, 0.01, 0.15)
    assert half["V_m_s"] == pytest.approx(full["V_m_s"], rel=1e-9)  # 半滿 R = D/4
    assert half["Q_cms"] == pytest.approx(full["Q_cms"] / 2, rel=1e-9)
    y = normal_depth_circular(0.3, 0.013, 0.01, full["Q_cms"] / 2)
    assert y == pytest.approx(0.15, abs=1e-6)
    assert normal_depth_circular(0.3, 0.013, 0.01, full["Q_cms"] * 1.1) is None


# ---------------------------------------------------------------- verifier 綠燈 + result 比對


def test_verifier_pass_with_result(tmp_path, base):
    spec = _write(tmp_path, base)
    r = compute(TaoyuanSiteDrainageSpec.from_json(spec))
    res_path = tmp_path / "result.json"
    res_path.write_text(json.dumps(r, ensure_ascii=False), encoding="utf-8")
    res = check(spec, res_path)
    assert res.ok, res.errors
    assert any("verified: false" in w for w in res.warnings)
    assert res.metrics["result_json_mismatches"] == 0


def test_verifier_catches_tampered_result(tmp_path, base):
    spec = _write(tmp_path, base)
    r = compute(TaoyuanSiteDrainageSpec.from_json(spec))
    r["storage_provided"]["provided_m3"]["value"] = 999.0
    res_path = tmp_path / "result.json"
    res_path.write_text(json.dumps(r, ensure_ascii=False), encoding="utf-8")
    res = check(spec, res_path)
    assert not res.ok
    assert any("provided_m3" in e for e in res.errors)


# ---------------------------------------------------------------- 故意錯的 fixture


def test_storage_80_red(tmp_path, base):
    d = copy.deepcopy(base)
    d["storage"]["cells"] = [{"length_m": 5.0, "width_m": 4.0}] * 4  # 80 m³
    res = check(_write(tmp_path, d))
    assert not res.ok
    assert any("提供貯集量" in e and "85.680" in e for e in res.errors)


def test_orifice_too_large_red(tmp_path, base):
    d = copy.deepcopy(base)
    d["outlet"]["diameter_m"] = 0.15
    res = check(_write(tmp_path, d))
    assert not res.ok
    assert any("孔口排放量" in e and ">" in e for e in res.errors)


def test_orifice_too_small_red(tmp_path, base):
    d = copy.deepcopy(base)
    d["outlet"]["diameter_m"] = 0.08
    res = check(_write(tmp_path, d))
    assert not res.ok
    assert any("孔口排放量" in e and "<" in e for e in res.errors)


def test_weir_too_small_red(tmp_path, base):
    d = copy.deepcopy(base)
    d["overflow_weir"] = {"width_m": 0.2, "head_m": 0.2}
    res = check(_write(tmp_path, d))
    assert not res.ok
    assert any("溢流堰" in e for e in res.errors)


def test_unknown_landuse_rejected_by_schema(base):
    d = copy.deepcopy(base)
    d["subareas"][0]["landuse"] = "luxury_residential"
    with pytest.raises(ValidationError, match="landuse"):
        TaoyuanSiteDrainageSpec.model_validate(d)


def test_c_override_out_of_range_rejected(base):
    d = copy.deepcopy(base)
    d["subareas"][0]["c_override"] = 0.95  # 混合住宅區上限 0.89
    with pytest.raises(ValidationError, match="c_override"):
        TaoyuanSiteDrainageSpec.model_validate(d)


def test_station_override_must_exist(base):
    d = copy.deepcopy(base)
    d["station"] = "大溪"
    with pytest.raises(ValidationError, match="station"):
        TaoyuanSiteDrainageSpec.model_validate(d)


def test_unmapped_district_rejected(base):
    d = copy.deepcopy(base)
    d["district"] = "龜山區"
    with pytest.raises(ValidationError, match="雨量站"):
        TaoyuanSiteDrainageSpec.model_validate(d)
    d["station"] = "桃園"
    TaoyuanSiteDrainageSpec.model_validate(d)  # 工程師指定站名即可


def test_extra_field_forbidden(base):
    d = copy.deepcopy(base)
    d["required_storage_m3"] = 85.68
    with pytest.raises(ValidationError):
        TaoyuanSiteDrainageSpec.model_validate(d)


def test_range_material_requires_n_override(base):
    d = copy.deepcopy(base)
    del d["pipes"][0]["n_override"]
    with pytest.raises(ValidationError, match="n_override"):
        TaoyuanSiteDrainageSpec.model_validate(d)


def test_subarea_sum_mismatch_red(tmp_path, base):
    d = copy.deepcopy(base)
    d["subareas"][0]["area_m2"] = 1600.0
    res = check(_write(tmp_path, d))
    assert not res.ok
    assert any("Σ subareas" in e for e in res.errors)


def test_subarea_sum_within_tolerance_green(tmp_path, base):
    d = copy.deepcopy(base)
    d["subareas"] = [
        {"name": "A", "area_m2": 1000.0, "landuse": "mixed_residential"},
        {"name": "B", "area_m2": 678.0, "landuse": "mixed_residential"},  # 差 0.12%
    ]
    res = check(_write(tmp_path, d))
    assert res.ok, res.errors


def test_low_velocity_red(tmp_path, base):
    d = copy.deepcopy(base)
    # 管徑放大、坡度放緩讓 P1 流速約 0.5 m/s
    d["pipes"][0]["diameter_m"] = 0.30
    d["pipes"][0]["slope"] = 0.0015
    res = check(_write(tmp_path, d))
    r = compute(TaoyuanSiteDrainageSpec.model_validate(d))
    v = r["pipes"][0]["velocity_m_s"]["value"]
    assert 0.45 < v < 0.6
    assert not res.ok
    assert any("P1" in e and "流速" in e for e in res.errors)


def test_freeboard_red(tmp_path, base):
    d = copy.deepcopy(base)
    d["pipes"][1]["depth_m"] = 0.25  # 水深約 0.11 → 出水高 0.14 < 0.20
    res = check(_write(tmp_path, d))
    assert not res.ok
    assert any("D1" in e and "出水高" in e for e in res.errors)


def test_min_pipe_diameter_red(tmp_path, base):
    d = copy.deepcopy(base)
    d["pipes"][0]["diameter_m"] = 0.15
    d["pipes"][0]["design_area_m2"] = 800.0  # 601–1000 m² 需 ≥ 200 mm
    d["pipes"][0]["slope"] = 0.03
    res = check(_write(tmp_path, d))
    assert not res.ok
    assert any("P1" in e and "最小" in e for e in res.errors)


def test_rc_pipe_material_mismatch_red(tmp_path, base):
    d = copy.deepcopy(base)
    d["pipes"][0].pop("n_override")
    d["pipes"][0]["material"] = "rc_pipe_ge_600"
    res = check(_write(tmp_path, d))
    assert not res.ok
    assert any("rc_pipe_ge_600" in e for e in res.errors)


def test_small_site_exempt_warning_only(tmp_path):
    d = {
        "site_area_m2": 280.0,
        "district": "桃園區",
        "subareas": [{"name": "全基地", "area_m2": 280.0, "landuse": "mixed_residential"}],
        "storage": {"type": "surface", "cells": [{"length_m": 4.0, "width_m": 4.0}], "effective_depth_m": 1.0},
        # 容許 280 × 0.000014 = 0.00392 cms；D=0.044 m 孔口 ≈ 0.0039 cms
        "outlet": {"type": "circ_orifice", "diameter_m": 0.0435, "design_head_m": 1.0},
        "overflow_weir": {"width_m": 0.3, "head_m": 0.1},
    }
    res = check(_write(tmp_path, d))
    assert any("免設" in w for w in res.warnings)
    assert res.ok, res.errors


def test_existing_building_as_conversion_warns(tmp_path, base):
    d = copy.deepcopy(base)
    d["existing_building"] = {"new_building_area_m2": 300.0, "legal_coverage_ratio": 0.6}
    r = compute(TaoyuanSiteDrainageSpec.model_validate(d))
    assert r["As"]["As_m2"] == pytest.approx(500.0)
    res = check(_write(tmp_path, d))
    assert any("增改建" in w for w in res.warnings)
