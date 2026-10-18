"""Level 2 detail-ty-1：DetailSheetSpec → 排水詳圖 DXF → detail_check。含故意錯的 fixture。"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import ezdxf
import pytest
from pydantic import ValidationError

from draftagent.render.detail import render_detail_sheet, write_detail_sheet
from draftagent.schema.detail import DetailSheetSpec
from draftagent.verify.detail_check import check_detail

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "tests" / "level2" / "detail-ty-1"
SPEC = CASE / "input" / "spec.json"
RAW = json.loads(SPEC.read_text(encoding="utf-8"))
EXPECTED = json.loads((CASE / "expected" / "expected.json").read_text(encoding="utf-8"))


def _detail(raw: dict, did: str) -> dict:
    return next(d for d in raw["details"] if d["id"] == did)


def _build(tmp_path: Path, raw: dict) -> tuple[Path, Path]:
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    dxf = write_detail_sheet(DetailSheetSpec.model_validate(raw), tmp_path / "detail.dxf")
    return dxf, spec_path


@pytest.fixture(scope="module")
def good(tmp_path_factory) -> tuple[Path, Path]:
    return _build(tmp_path_factory.mktemp("good"), RAW)


def _errors(res) -> str:
    return "\n".join(res.errors)


def _mutate(dxf: Path, out: Path, fn) -> Path:
    doc = ezdxf.readfile(str(dxf))
    fn(doc)
    doc.saveas(out)
    return out


def _group(doc, did: str):
    return list(doc.groups.get(f"DETAIL-{did}"))


# ---------------------------------------------------------------- green


def test_good_sheet_passes(good):
    dxf, spec = good
    res = check_detail(dxf, spec)
    assert res.ok, res.render()
    assert res.metrics["insunits"] == EXPECTED["insunits"]
    for did, exp in EXPECTED["details"].items():
        assert res.metrics["dimensions_shown"][did] == pytest.approx(
            sorted(exp["dimensions_shown"])
        ), did
    assert any("verified: false" in w for w in res.warnings)


def test_good_sheet_contents(good):
    dxf, _ = good
    doc = ezdxf.readfile(str(dxf))
    assert doc.header["$INSUNITS"] == 4
    for did, exp in EXPECTED["details"].items():
        ents = _group(doc, did)
        texts = [e.dxf.text for e in ents if e.dxftype() == "TEXT"]
        assert any(exp["section_code"] in t for t in texts)
        assert f"S=1/{exp['scale']}" in texts
        for el in exp["els"]:
            assert any(f"EL={el}" in t for t in texts), (did, el)
        for dim in (e for e in ents if e.dxftype() == "DIMENSION"):
            assert dim.override()["dimlfac"] == exp["scale"]
    ttlb = [i for i in doc.modelspace().query("INSERT") if i.dxf.name.startswith("TTLB")]
    assert {a.dxf.tag: a.dxf.text for a in ttlb[0].attribs}["SCALE"] == "如圖示"


def test_render_is_deterministic():
    spec = DetailSheetSpec.model_validate(RAW)
    a = [e.dxftype() for e in render_detail_sheet(spec).modelspace()]
    b = [e.dxftype() for e in render_detail_sheet(spec).modelspace()]
    assert a == b


def test_cli(tmp_path):
    out = tmp_path / "cli.dxf"
    r = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "gen_detail.py"), str(SPEC), "-o", str(out)],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert r.returncode == 0, r.stderr
    r = subprocess.run(
        [sys.executable, "-m", "draftagent.verify.detail_check", str(out), "--spec", str(SPEC)],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PASS" in r.stdout


# ---------------------------------------------------------------- 故意錯：spec 幾何


def test_orifice_above_overflow_fails(tmp_path):
    raw = copy.deepcopy(RAW)
    _detail(raw, "D2")["orifice"]["center_el"] = 99.45  # 設計滿水位 = 溢流口 99.40
    res = check_detail(*_build(tmp_path, raw))
    assert not res.ok
    assert "放流孔口中心 EL=99.45 必須低於 設計滿水位 EL=99.40" in _errors(res)


# ---------------------------------------------------------------- 水位語意
# 設計滿水位（貯留水位 = 坑底 + 有效水深）≤ 溢流口 < 溢流時最高水位（溢流口 + 堰頂水頭）≤ 坑頂


def test_overflow_below_storage_level_fails(tmp_path):
    raw = copy.deepcopy(RAW)
    _detail(raw, "D2")["overflow_el"] = 99.30  # 設計滿水位 99.40
    res = check_detail(*_build(tmp_path, raw))
    assert "設計滿水位 EL=99.40 高於溢流口 EL=99.30" in _errors(res)


def test_flood_level_not_above_overflow_fails(tmp_path):
    raw = copy.deepcopy(RAW)
    _detail(raw, "D2")["overflow_flood_el"] = 99.40  # = 溢流口
    res = check_detail(*_build(tmp_path, raw))
    assert "溢流口 EL=99.40 必須低於 溢流時最高水位 EL=99.40" in _errors(res)


def test_flood_level_above_pit_top_fails(tmp_path):
    raw = copy.deepcopy(RAW)
    _detail(raw, "D2")["overflow_flood_el"] = 99.75  # 坑頂 99.70
    res = check_detail(*_build(tmp_path, raw))
    assert "溢流時最高水位 EL=99.75 高於坑頂 EL=99.70" in _errors(res)


def test_flood_level_missing_warns(tmp_path):
    raw = copy.deepcopy(RAW)
    del _detail(raw, "D2")["overflow_flood_el"]
    res = check_detail(*_build(tmp_path, raw))
    assert res.ok, res.render()
    assert any("未給溢流時最高水位" in w for w in res.warnings)


def test_flood_level_drawn_with_new_labels(good):
    dxf, _ = good
    texts = [e.dxf.text for e in _group(ezdxf.readfile(str(dxf)), "D2") if e.dxftype() == "TEXT"]
    assert "設計滿水位 EL=99.40" in texts
    assert "溢流時最高水位 EL=99.50" in texts
    assert not any("設計最高水位" in t for t in texts)


def test_legacy_design_hwl_el_maps_to_storage_level(tmp_path):
    from draftagent.schema.detail import LegacyFieldWarning

    raw = copy.deepcopy(RAW)
    d2 = _detail(raw, "D2")
    d2["design_hwl_el"] = d2.pop("storage_level_el")
    with pytest.warns(LegacyFieldWarning, match="storage_level_el"):
        spec = DetailSheetSpec.model_validate(raw)
    raft = next(d for d in spec.details if d.id == "D2")
    assert raft.storage_level_el == 99.40
    res = check_detail(*_build(tmp_path, raw))
    assert res.ok, res.render()
    assert any("舊欄位" in w and "design_hwl_el" in w for w in res.warnings)
    # 舊語意（設計最高水位當成溢流時水位，高於溢流口）→ 映射後必紅
    d2["design_hwl_el"] = 99.50
    res = check_detail(*_build(tmp_path, raw))
    assert "設計滿水位 EL=99.50 高於溢流口 EL=99.40" in _errors(res)
    d2["storage_level_el"] = 99.40
    with pytest.raises(ValidationError, match="不可同時給"):
        DetailSheetSpec.model_validate(raw)


def test_effective_depth_exceeds_pit_fails(tmp_path):
    raw = copy.deepcopy(RAW)
    _detail(raw, "D2")["effective_depth"] = 1200  # 坑深 1000
    res = check_detail(*_build(tmp_path, raw))
    assert not res.ok
    assert "有效水深 1200 mm 大於坑深 1000 mm" in _errors(res)


def test_outlet_pipe_below_ditch_fails(tmp_path):
    raw = copy.deepcopy(RAW)
    _detail(raw, "D3")["outlet_pipe"]["invert_el"] = 98.65  # 側溝底 98.70
    res = check_detail(*_build(tmp_path, raw))
    assert not res.ok
    assert "低於接入側溝底" in _errors(res)


# ---------------------------------------------------------------- 故意錯：DXF 被改


def test_dimension_text_overridden_fails(good, tmp_path):
    dxf, spec = good

    def fn(doc):
        dim = next(e for e in _group(doc, "D1") if e.dxftype() == "DIMENSION")
        dim.dxf.text = "450"

    res = check_detail(_mutate(dxf, tmp_path / "bad.dxf", fn), spec)
    assert not res.ok
    assert "文字覆寫「450」" in _errors(res)
    assert "spec 無此尺寸" in _errors(res)


def test_wrong_dimlfac_fails(good, tmp_path):
    dxf, spec = good

    def fn(doc):
        for dim in (e for e in _group(doc, "D2") if e.dxftype() == "DIMENSION"):
            ov = dim.override()
            ov["dimlfac"] = 1.0  # 忘了乘比例 → 顯示圖紙 mm
            ov.commit()

    res = check_detail(_mutate(dxf, tmp_path / "bad.dxf", fn), spec)
    assert not res.ok
    errs = _errors(res)
    assert "dimlfac=1 ≠ 比例分母 30" in errs
    assert "缺尺寸標註" in errs


def test_missing_check_valve_note_fails(good, tmp_path):
    dxf, spec = good

    def fn(doc):
        for e in _group(doc, "D2"):
            if e.dxftype() == "TEXT" and "逆止閥" in e.dxf.text:
                doc.modelspace().delete_entity(e)

    res = check_detail(_mutate(dxf, tmp_path / "bad.dxf", fn), spec)
    assert not res.ok
    assert "缺必要註記「逆止閥」" in _errors(res)


def test_missing_flush_outlet_note_fails(good, tmp_path):
    dxf, spec = good

    def fn(doc):
        for e in _group(doc, "D3"):
            if e.dxftype() == "TEXT" and "切齊溝緣" in e.dxf.text:
                e.dxf.text = "放流管"

    res = check_detail(_mutate(dxf, tmp_path / "bad.dxf", fn), spec)
    assert "缺必要註記「切齊溝緣」" in _errors(res)


def test_overlapping_details_fail(good, tmp_path):
    dxf, spec = good

    def fn(doc):
        from ezdxf import bbox

        a = bbox.extents(_group(doc, "D6"))
        b = bbox.extents(_group(doc, "D5"))
        dx, dy = b.extmin.x - a.extmin.x + 10, b.extmin.y - a.extmin.y + 10
        for e in _group(doc, "D6"):
            e.translate(dx, dy, 0)

    res = check_detail(_mutate(dxf, tmp_path / "bad.dxf", fn), spec)
    assert not res.ok
    assert "詳圖 D5 與 D6 的範圍重疊" in _errors(res)


def test_wrong_section_code_fails(good, tmp_path):
    dxf, spec = good

    def fn(doc):
        for e in _group(doc, "D1"):
            if e.dxftype() == "TEXT" and e.dxf.text.startswith("A-A"):
                e.dxf.text = e.dxf.text.replace("A-A", "B-B")

    res = check_detail(_mutate(dxf, tmp_path / "bad.dxf", fn), spec)
    assert "剖面代號「A-A」" in _errors(res)


def test_missing_el_fails(good, tmp_path):
    dxf, spec = good

    def fn(doc):
        for e in _group(doc, "D2"):
            if e.dxftype() == "TEXT" and "EL=99.40" in e.dxf.text:
                e.dxf.text = "溢流口"

    res = check_detail(_mutate(dxf, tmp_path / "bad.dxf", fn), spec)
    assert "溢流口 EL=99.40" in _errors(res)


def test_insunits_and_layer0(good, tmp_path):
    dxf, spec = good

    def fn(doc):
        doc.header["$INSUNITS"] = 6
        doc.modelspace().add_line((0, 0), (1, 1))

    errs = _errors(check_detail(_mutate(dxf, tmp_path / "bad.dxf", fn), spec))
    assert "$INSUNITS = 6" in errs
    assert "0 層有 1 個實體" in errs


# ---------------------------------------------------------------- schema / 排版


def test_schema_rejects_extra_and_three_decimal_el():
    raw = copy.deepcopy(RAW)
    _detail(raw, "D1")["color"] = "red"
    with pytest.raises(ValidationError):
        DetailSheetSpec.model_validate(raw)
    raw = copy.deepcopy(RAW)
    _detail(raw, "D2")["overflow_el"] = 97.951
    with pytest.raises(ValidationError, match="兩位小數"):
        DetailSheetSpec.model_validate(raw)
    raw = copy.deepcopy(RAW)
    _detail(raw, "D2")["section_code"] = "A-A"
    with pytest.raises(ValidationError, match="剖面代號不得重複"):
        DetailSheetSpec.model_validate(raw)


def test_too_big_for_a3_raises():
    raw = copy.deepcopy(RAW)
    raw["sheet"]["size"] = "A3"
    with pytest.raises(ValueError, match="放不進 A3"):
        render_detail_sheet(DetailSheetSpec.model_validate(raw))
