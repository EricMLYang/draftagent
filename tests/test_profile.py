"""Level 2 profile-ty-1：ProfileSpec → 縱斷面 DXF → profile_check。含故意錯的 fixture。"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import ezdxf
import pytest
from pydantic import ValidationError

from draftagent.render.profile import render_profile, write_profile
from draftagent.schema.profile import ProfileSpec, format_station, parse_station
from draftagent.verify.profile_check import check_profile

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "tests" / "level2" / "profile-ty-1"
SPEC = CASE / "input" / "spec.json"
RAW = json.loads(SPEC.read_text(encoding="utf-8"))
EXPECTED = json.loads((CASE / "expected" / "expected.json").read_text(encoding="utf-8"))


@pytest.fixture
def spec() -> ProfileSpec:
    return ProfileSpec.from_json(SPEC)


@pytest.fixture
def good_dxf(tmp_path, spec) -> Path:
    return write_profile(spec, tmp_path / "profile.dxf")


def _errors(res) -> str:
    return "\n".join(res.errors)


def _mutate(dxf: Path, fn) -> Path:
    doc = ezdxf.readfile(str(dxf))
    fn(doc)
    out = dxf.with_name("bad.dxf")
    doc.saveas(out)
    return out


# ---------------------------------------------------------------- green


def test_good_profile_passes(good_dxf):
    res = check_profile(good_dxf, SPEC)
    assert res.ok, res.render()
    assert res.metrics["insunits"] == EXPECTED["insunits"]
    assert res.metrics["stations"] == [parse_station(s) for s in EXPECTED["stations_text"]]
    assert res.metrics["slopes_pct"] == pytest.approx(EXPECTED["slopes_pct"], abs=1e-4)
    assert res.metrics["h_mm_per_m"] == pytest.approx(1000 / EXPECTED["h_scale"])
    assert res.metrics["v_mm_per_m"] == pytest.approx(1000 / EXPECTED["v_scale"])
    # yaml verified: false → 必須有 warning
    assert any("verified: false" in w for w in res.warnings)


def test_schema_derived_values(spec):
    assert spec.slopes_pct() == pytest.approx(EXPECTED["slopes_pct"])
    assert spec.lengths() == pytest.approx(EXPECTED["lengths_m"])
    assert spec.resolved_datum() == EXPECTED["datum_el"]
    assert [format_station(n.station_m) for n in spec.nodes] == EXPECTED["stations_text"]


def test_table_texts_and_layers(good_dxf):
    doc = ezdxf.readfile(str(good_dxf))
    msp = doc.modelspace()
    texts = {e.dxf.text for e in msp.query("TEXT")}
    for s in EXPECTED["slopes_text"] + EXPECTED["stations_text"]:
        assert s in texts
    assert "水平 1/200　垂直 1/50" in texts
    layers = {e.dxf.layer for e in msp}
    assert {"H-PROF", "H-PROF-WL", "H-TEXT", "H-GRID", "H-TABL", "X-FRAME", "X-TTLB"} <= layers
    assert "0" not in layers
    assert doc.styles.get("CJK").dxf.font.lower().endswith((".ttf", ".ttc", ".otf"))
    assert not list(msp.query("DIMENSION"))
    assert "NORTH-ARROW" not in [i.dxf.name for i in msp.query("INSERT")]


def test_cli_exit_codes(good_dxf):
    cmd = [
        sys.executable,
        "-m",
        "draftagent.verify.profile_check",
        str(good_dxf),
        "--spec",
        str(SPEC),
    ]
    ok = subprocess.run(cmd, check=False, capture_output=True, text=True, cwd=ROOT)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    bad = _mutate(good_dxf, lambda d: setattr(d, "units", 6))
    cmd[3] = str(bad)
    ng = subprocess.run(cmd + ["--json"], check=False, capture_output=True, text=True, cwd=ROOT)
    assert ng.returncode == 1
    assert json.loads(ng.stdout)["ok"] is False


def test_a1_and_north_arrow(tmp_path):
    raw = copy.deepcopy(RAW)
    raw["sheet"]["size"] = "A1"
    raw["sheet"]["north_arrow"] = True
    spec = ProfileSpec.model_validate(raw)
    out = write_profile(spec, tmp_path / "a1.dxf")
    assert check_profile(out, None).ok
    names = [i.dxf.name for i in ezdxf.readfile(str(out)).modelspace().query("INSERT")]
    assert "TTLB-A1" in names and "NORTH-ARROW" in names


def test_too_long_for_sheet_raises():
    raw = copy.deepcopy(RAW)
    for i, n in enumerate(raw["nodes"]):
        n["station_m"] = i * 30.0  # 120 m @1/200 = 600 mm > A3
    with pytest.raises(ValueError, match="放不進 A3"):
        render_profile(ProfileSpec.model_validate(raw))


def test_preview_png(tmp_path, good_dxf):
    pytest.importorskip("matplotlib")
    from draftagent.render.preview import render_png

    png = render_png(good_dxf, tmp_path / "p.png", dpi=40)
    assert png.stat().st_size > 1000


# ---------------------------------------------------------------- 故意錯


def _render_raw(tmp_path, raw) -> tuple[Path, Path]:
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return write_profile(ProfileSpec.model_validate(raw), tmp_path / "p.dxf"), spec_path


def test_adverse_slope_is_error(tmp_path):
    raw = copy.deepcopy(RAW)
    raw["nodes"][2]["invert_el"] = 120.25  # MH2 高於上游 MH1 120.18
    dxf, spec_path = _render_raw(tmp_path, raw)
    res = check_profile(dxf, spec_path)
    assert not res.ok
    assert "逆坡" in _errors(res) and "MH1→MH2" in _errors(res)


def test_adverse_slope_allowed_for_pump_segment(tmp_path):
    raw = copy.deepcopy(RAW)
    raw["nodes"][2]["invert_el"] = 120.25
    raw["nodes"][2]["top_el"] = 120.75  # 溝頂 − 溝底 必須 = U 溝 H50
    raw["segments"][1]["pump"] = True
    dxf, spec_path = _render_raw(tmp_path, raw)
    res = check_profile(dxf, spec_path)
    assert res.ok, res.render()


def test_missing_table_elevation_text(good_dxf):
    def drop(doc):
        msp = doc.modelspace()
        victim = next(
            e for e in msp.query("TEXT") if e.dxf.layer == "H-TABL" and e.dxf.text == "120.06"
        )
        msp.delete_entity(victim)

    res = check_profile(_mutate(good_dxf, drop), SPEC)
    assert not res.ok
    assert "溝底高程" in _errors(res) and "MH2=120.06" in _errors(res)


def test_h_scale_300_rejected_by_schema():
    raw = copy.deepcopy(RAW)
    raw["h_scale"] = 300
    with pytest.raises(ValidationError, match="1/200"):
        ProfileSpec.model_validate(raw)


def test_h_scale_300_caught_by_verifier(tmp_path, spec):
    bad = spec.model_copy(update={"h_scale": 300})  # 繞過 schema 驗證，模擬手改的圖
    dxf = write_profile(bad, tmp_path / "p300.dxf")
    res = check_profile(dxf, SPEC)
    assert not res.ok
    assert "水平比例 1/300" in _errors(res) and "S≧1/200" in _errors(res)
    # 無 spec 時也要抓到
    assert "水平比例 1/300" in _errors(check_profile(dxf, None))


def test_scale_text_not_matching_geometry(good_dxf):
    def relabel(doc):
        for e in doc.modelspace().query("TEXT"):
            e.dxf.text = e.dxf.text.replace("水平 1/200", "水平 1/100")
        for ins in doc.modelspace().query("INSERT"):
            for a in ins.attribs:
                a.dxf.text = a.dxf.text.replace("水平 1/200", "水平 1/100")

    res = check_profile(_mutate(good_dxf, relabel), None)
    assert not res.ok
    assert "水平幾何比例" in _errors(res)


def test_missing_outlet_pipe_level_text(good_dxf):
    def drop(doc):
        msp = doc.modelspace()
        for e in [e for e in msp.query("TEXT") if "放流管" in e.dxf.text]:
            msp.delete_entity(e)

    res = check_profile(_mutate(good_dxf, drop), SPEC)
    assert not res.ok
    assert "放流管 EL=119.92" in _errors(res)


def test_blank_title_attrib(good_dxf):
    def blank(doc):
        for ins in doc.modelspace().query("INSERT"):
            for a in ins.attribs:
                if a.dxf.tag == "TITLE":
                    a.dxf.text = ""

    res = check_profile(_mutate(good_dxf, blank), SPEC)
    assert not res.ok
    assert "TITLE" in _errors(res)


def test_dimlfac_100(good_dxf):
    def add_dim(doc):
        doc.modelspace().add_linear_dim(
            base=(100, 60),
            p1=(100, 70),
            p2=(175, 70),
            dimstyle="EZDXF",
            override={"dimlfac": 100},
            dxfattribs={"layer": "H-DIMS"},
        ).render()

    res = check_profile(_mutate(good_dxf, add_dim), SPEC)
    assert not res.ok
    assert "dimlfac=100" in _errors(res)


def test_entity_on_layer_zero(good_dxf):
    res = check_profile(_mutate(good_dxf, lambda d: d.modelspace().add_line((0, 0), (1, 1))), SPEC)
    assert not res.ok
    assert "0 層" in _errors(res)
