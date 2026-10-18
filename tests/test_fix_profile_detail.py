"""回歸測試：縱斷面 / 詳圖 verifier 稽核缺口（#2 DIMENSION 顯示值、#5 縱斷面幾何、
#8 縱斷面物理合理性、#9/#10 字高與文字重疊、#13 詳圖 EL / 幾何 / 註記）。

每個情境：由 examples spec 產圖 → 竄改 DXF（或 spec）→ verifier 必須紅燈；未竄改的基準必須綠燈。
"""

from __future__ import annotations

import copy
import json
import re
import shutil
from pathlib import Path

import ezdxf
import pytest
from ezdxf import bbox as ezbbox
from ezdxf.math import Matrix44

from draftagent.render.detail import write_detail_sheet
from draftagent.render.profile import write_profile
from draftagent.schema.detail import DetailSheetSpec
from draftagent.schema.profile import ProfileSpec
from draftagent.verify.detail_check import check_detail
from draftagent.verify.profile_check import check_profile

ROOT = Path(__file__).resolve().parents[1]
PROFILE_RAW = json.loads((ROOT / "examples/drainage-profile.json").read_text(encoding="utf-8"))
DETAIL_RAW = json.loads((ROOT / "examples/drainage-detail.json").read_text(encoding="utf-8"))


def _errors(res) -> str:
    return "\n".join(res.errors)


# ---------------------------------------------------------------- fixtures


def _build_profile(tmp: Path, raw: dict) -> tuple[Path, Path]:
    sp = tmp / "profile_spec.json"
    sp.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    dxf = write_profile(ProfileSpec.model_validate(raw), tmp / "profile.dxf")
    return dxf, sp


def _build_detail(tmp: Path, raw: dict) -> tuple[Path, Path]:
    sp = tmp / "detail_spec.json"
    sp.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    dxf = write_detail_sheet(DetailSheetSpec.model_validate(raw), tmp / "detail.dxf")
    return dxf, sp


@pytest.fixture(scope="module")
def profile(tmp_path_factory):
    return _build_profile(tmp_path_factory.mktemp("prof"), copy.deepcopy(PROFILE_RAW))


@pytest.fixture(scope="module")
def detail(tmp_path_factory):
    return _build_detail(tmp_path_factory.mktemp("det"), copy.deepcopy(DETAIL_RAW))


def _mutate(src: Path, dst: Path, fn) -> Path:
    shutil.copy(src, dst)
    doc = ezdxf.readfile(str(dst))
    fn(doc)
    doc.saveas(str(dst))
    return dst


def _move_text(t, xy) -> None:
    """一致地搬動文字（insert 與對齊點一起動）。"""
    al, _, _ = t.get_placement()
    t.set_placement(xy, align=al)


def _prof_lines(doc):
    """H-PROF 上與節點數同長的 LWPOLYLINE：(地面線, 溝底線)。"""
    n = len(PROFILE_RAW["nodes"])
    pls = [
        e
        for e in doc.modelspace().query("LWPOLYLINE[layer=='H-PROF']")
        if len(e) == n and not e.closed
    ]
    pls.sort(key=lambda e: -sum(p[1] for p in e.get_points("xy")))
    return pls[0], pls[1]


# ---------------------------------------------------------------- 基準綠燈


def test_profile_baseline_green(profile):
    res = check_profile(*profile)
    assert res.ok, res.render()


def test_detail_baseline_green(detail):
    res = check_detail(*detail)
    assert res.ok, res.render()


@pytest.mark.parametrize(
    "spec_rel",
    ["tests/level2/profile-ty-1/input/spec.json"],
)
def test_level2_profile_green(tmp_path, spec_rel):
    raw = json.loads((ROOT / spec_rel).read_text(encoding="utf-8"))
    res = check_profile(*_build_profile(tmp_path, raw))
    assert res.ok, res.render()


def test_level2_detail_green(tmp_path):
    raw = json.loads(
        (ROOT / "tests/level2/detail-ty-1/input/spec.json").read_text(encoding="utf-8")
    )
    res = check_detail(*_build_detail(tmp_path, raw))
    assert res.ok, res.render()


# ---------------------------------------------------------------- #5 縱斷面幾何


def test_pr1_invert_line_moved_is_red(profile, tmp_path):
    def fn(doc):
        _, inv = _prof_lines(doc)
        inv.set_points([(x, y + 20) for x, y in inv.get_points("xy")], format="xy")

    res = check_profile(_mutate(profile[0], tmp_path / "x.dxf", fn), profile[1])
    assert not res.ok and "H-PROF 溝底線" in _errors(res)


def test_pr2_node_labels_swapped_is_red(profile, tmp_path):
    def fn(doc):
        ts = {t.dxf.text: t for t in doc.modelspace().query("TEXT[layer=='H-TEXT']")}
        ts["IN1"].dxf.text, ts["MH-A"].dxf.text = "MH-A", "IN1"

    res = check_profile(_mutate(profile[0], tmp_path / "x.dxf", fn), profile[1])
    assert not res.ok and "節點名「IN1」不在樁號 0" in _errors(res)


def test_pr3_flow_arrow_reversed_is_red(profile, tmp_path):
    def fn(doc):
        for e in doc.modelspace().query("LWPOLYLINE[layer=='H-TEXT']"):
            pts = list(e.get_points("xy"))
            if e.closed and len(pts) == 3:
                cx = sum(p[0] for p in pts) / 3
                e.set_points([(2 * cx - p[0], p[1]) for p in pts], format="xy")

    res = check_profile(_mutate(profile[0], tmp_path / "x.dxf", fn), profile[1])
    assert not res.ok and "流向箭頭指向樁號遞減方向" in _errors(res)


def test_pr4_ruler_numbers_shifted_is_red(profile, tmp_path):
    def fn(doc):
        for t in doc.modelspace().query("TEXT[layer=='H-GRID']"):
            if re.fullmatch(r"-?\d+\.\d{2}", t.dxf.text):
                t.dxf.text = f"{float(t.dxf.text) + 1:.2f}"

    res = check_profile(_mutate(profile[0], tmp_path / "x.dxf", fn), profile[1])
    assert not res.ok and "高程尺絕對值與基準高程" in _errors(res)


def test_pr5_level_text_at_wrong_station_is_red(profile, tmp_path):
    def fn(doc):
        t = next(t for t in doc.modelspace().query("TEXT") if t.dxf.text.startswith("放流管 EL="))
        n1 = next(t for t in doc.modelspace().query("TEXT") if t.dxf.text == "IN1")
        _move_text(t, (n1.get_placement()[1][0] + 6.0, t.get_placement()[1][1]))

    res = check_profile(_mutate(profile[0], tmp_path / "x.dxf", fn), profile[1])
    assert not res.ok and "「放流管 EL=44.71」文字離樁號 21" in _errors(res)


def test_pr5_half_moved_text_anchor_is_red(profile, tmp_path):
    def fn(doc):
        t = next(t for t in doc.modelspace().query("TEXT") if t.dxf.text.startswith("放流管 EL="))
        t.dxf.insert = (84.0, 190.0)  # 只改 insert：CAD 與檢視器顯示位置不同

    res = check_profile(_mutate(profile[0], tmp_path / "x.dxf", fn), profile[1])
    assert not res.ok and "插入點" in _errors(res)


def test_pr7_station_column_misaligned_is_red(profile, tmp_path):
    def fn(doc):
        st = sorted(
            (t for t in doc.modelspace().query("TEXT[layer=='H-TABL']") if "+" in t.dxf.text),
            key=lambda t: t.get_placement()[1][0],
        )
        p = st[1].get_placement()[1]
        _move_text(st[1], (p[0] + 30.0, p[1]))

    res = check_profile(_mutate(profile[0], tmp_path / "x.dxf", fn), profile[1])
    assert not res.ok and "找不到樁號格線" in _errors(res)


def test_pr7_value_column_misaligned_is_red(profile, tmp_path):
    def fn(doc):
        t = next(
            t for t in doc.modelspace().query("TEXT[layer=='H-TABL']") if t.dxf.text == "45.52"
        )
        p = t.get_placement()[1]
        _move_text(t, (p[0] + 30.0, p[1]))

    res = check_profile(_mutate(profile[0], tmp_path / "x.dxf", fn), profile[1])
    assert not res.ok and "未對齊任何樁號欄" in _errors(res)


@pytest.mark.parametrize("which", ["ground", "all"])
def test_pr9_pr10_profile_geometry_missing_is_red(profile, tmp_path, which):
    def fn(doc):
        msp = doc.modelspace()
        if which == "ground":
            msp.delete_entity(_prof_lines(doc)[0])
        else:
            for e in list(msp.query("*[layer=='H-PROF']")):
                msp.delete_entity(e)

    res = check_profile(_mutate(profile[0], tmp_path / "x.dxf", fn), profile[1])
    assert not res.ok and "H-PROF 地面線" in _errors(res)


# ---------------------------------------------------------------- #8 縱斷面物理合理性


def _prof_spec_mut(tmp_path, fn):
    raw = copy.deepcopy(PROFILE_RAW)
    fn(raw)
    return check_profile(*_build_profile(tmp_path, raw))


def test_pr13b_top_above_ground_is_red(tmp_path):
    res = _prof_spec_mut(tmp_path, lambda r: r["nodes"][0].__setitem__("top_el", 45.90))
    assert not res.ok and "溝頂 EL=45.90 高於地面" in _errors(res)


def test_pr15b_section_height_mismatch_is_red(tmp_path):
    def fn(r):
        r["nodes"][1]["top_el"] = 45.50
        r["nodes"][1]["ground_el"] = 45.66

    res = _prof_spec_mut(tmp_path, fn)
    assert not res.ok and "溝頂 − 溝底 = 0.64 m" in _errors(res)


def test_pr14_inlet_pipe_below_invert_is_red(tmp_path):
    res = _prof_spec_mut(tmp_path, lambda r: r["levels"][0].__setitem__("el", 44.80))
    assert not res.ok and "「進水管」EL=44.80 低於樁號 0 處溝底" in _errors(res)


def test_pr16_outlet_pipe_below_receiving_is_red(tmp_path):
    res = _prof_spec_mut(tmp_path, lambda r: r["levels"][2].__setitem__("el", 44.60))
    assert not res.ok and "低於受納點 道路側溝" in _errors(res)


def test_pipe_segment_not_subject_to_ditch_height_rule(tmp_path):
    def fn(r):  # PVC 管段終點給溝頂：不套用 U 溝 溝頂 − 溝底 = size_m
        r["nodes"][3]["top_el"] = 45.10

    res = _prof_spec_mut(tmp_path, fn)
    assert "溝頂 − 溝底" not in _errors(res), res.render()


# ---------------------------------------------------------------- #2 / #9 / #10 縱斷面


def test_profile_dimension_dimpost_is_red(profile, tmp_path):
    def fn(doc):
        msp = doc.modelspace()
        dim = msp.add_linear_dim(
            base=(100, 60),
            p1=(100, 55),
            p2=(130, 55),
            override={"dimpost": "<>0"},
            dxfattribs={"layer": "H-DIMS"},
        )
        dim.render()

    res = check_profile(_mutate(profile[0], tmp_path / "x.dxf", fn), profile[1])
    assert not res.ok and "dimpost='<>0'" in _errors(res)


def test_profile_small_text_is_red(profile, tmp_path):
    def fn(doc):
        for t in doc.modelspace().query("TEXT"):
            t.dxf.height = 1.0

    res = check_profile(_mutate(profile[0], tmp_path / "x.dxf", fn), profile[1])
    assert not res.ok and "字高" in _errors(res)


def test_profile_text_overlap_is_red(profile, tmp_path):
    def fn(doc):
        msp = doc.modelspace()
        note = next(t for t in msp.query("TEXT") if t.dxf.text.startswith("附註"))
        cell = next(t for t in msp.query("TEXT[layer=='H-TABL']") if t.dxf.text == "44.95")
        _move_text(note, cell.get_placement()[1])

    res = check_profile(_mutate(profile[0], tmp_path / "x.dxf", fn), profile[1])
    assert not res.ok and "文字重疊" in _errors(res)


# ---------------------------------------------------------------- #13 詳圖


def _grp(doc, gid):
    return list(doc.groups.get(f"DETAIL-{gid}"))


def _texts(doc, gid):
    return [e for e in _grp(doc, gid) if e.dxftype() == "TEXT"]


def _swap_el_values(doc, gid, a, b):
    ta = next(t for t in _texts(doc, gid) if t.dxf.text.startswith(a))
    tb = next(t for t in _texts(doc, gid) if t.dxf.text.startswith(b))
    va = re.search(r"EL=(\S+)", ta.dxf.text).group(1)
    vb = re.search(r"EL=(\S+)", tb.dxf.text).group(1)
    ta.dxf.text = ta.dxf.text.replace(va, vb)
    tb.dxf.text = tb.dxf.text.replace(vb, va)


@pytest.mark.parametrize(("gid", "a", "b"), [("RD", "坑頂", "坑底"), ("OW", "井頂", "井底")])
def test_d1_el_values_swapped_is_red(detail, tmp_path, gid, a, b):
    res = check_detail(
        _mutate(detail[0], tmp_path / "x.dxf", lambda doc: _swap_el_values(doc, gid, a, b)),
        detail[1],
    )
    assert not res.ok and f"{b} 圖面 EL=" in _errors(res)


def test_d7_el_text_positions_swapped_is_red(detail, tmp_path):
    def fn(doc):
        tt = {t.dxf.text.split(" ")[0]: t for t in _texts(doc, "OW")}
        a, b = tt["井頂"], tt["井底"]
        (aa, pa, _), (ab, pb, _) = a.get_placement(), b.get_placement()
        a.set_placement(pb, align=ab)
        b.set_placement(pa, align=aa)

    res = check_detail(_mutate(detail[0], tmp_path / "x.dxf", fn), detail[1])
    assert not res.ok and "標示點 y 與其他高程不成比例" in _errors(res)


def test_d2_nameplate_capacity_substring_is_red(detail, tmp_path):
    def fn(doc):
        for t in _texts(doc, "NP"):
            if t.dxf.text == "40.50 m³":
                t.dxf.text = "140.50 m³"

    res = check_detail(_mutate(detail[0], tmp_path / "x.dxf", fn), detail[1])
    assert not res.ok and "銘牌容量應為「40.50 m³」" in _errors(res)


def test_d4_negated_required_word_is_red(detail, tmp_path):
    def fn(doc):
        for t in _texts(doc, "RD"):
            if t.dxf.text == "逆止閥":
                t.dxf.text = "本案不設逆止閥"

    res = check_detail(_mutate(detail[0], tmp_path / "x.dxf", fn), detail[1])
    assert not res.ok and "缺必要註記「逆止閥」（僅見否定句）" in _errors(res)


def test_d10_section_letter_mismatch_is_red(detail, tmp_path):
    def fn(doc):
        for t in _texts(doc, "OW"):
            if t.dxf.text == "C":
                t.dxf.text = "B"

    res = check_detail(_mutate(detail[0], tmp_path / "x.dxf", fn), detail[1])
    assert not res.ok and "剖切線字母" in _errors(res)


def test_d9b_water_line_moved_is_red(detail, tmp_path):
    def fn(doc):
        for e in _grp(doc, "RD"):
            if e.dxf.layer == "H-WATR":
                e.translate(0, 15, 0)

    res = check_detail(_mutate(detail[0], tmp_path / "x.dxf", fn), detail[1])
    assert not res.ok and "不是任何水位" in _errors(res)


def test_d3b_body_scaled_dims_untouched_is_red(detail, tmp_path):
    def fn(doc):
        ents = [
            e for e in _grp(doc, "UD") if e.dxftype() in ("LWPOLYLINE", "HATCH", "CIRCLE", "LINE")
        ]
        c = ezbbox.extents(ents).center
        m = Matrix44.chain(
            Matrix44.translate(-c.x, -c.y, 0),
            Matrix44.scale(1.15, 1.15, 1),
            Matrix44.translate(c.x, c.y, 0),
        )
        for e in ents:
            e.transform(m)

    res = check_detail(_mutate(detail[0], tmp_path / "x.dxf", fn), detail[1])
    assert not res.ok and "本體幾何" in _errors(res)


def test_d6_hatch_scale_x100_is_red(detail, tmp_path):
    def fn(doc):
        for e in doc.modelspace().query("HATCH"):
            if e.dxf.pattern_name != "SOLID":
                e.set_pattern_fill(e.dxf.pattern_name, scale=e.dxf.pattern_scale * 100)

    res = check_detail(_mutate(detail[0], tmp_path / "x.dxf", fn), detail[1])
    assert not res.ok and "pattern_scale" in _errors(res)


def test_d12_dimpost_rerendered_is_red(detail, tmp_path):
    def fn(doc):
        for dim in doc.modelspace().query("DIMENSION"):
            ov = dim.override()
            ov["dimpost"] = "<>0"
            ov.commit()
            ov.render()

    res = check_detail(_mutate(detail[0], tmp_path / "x.dxf", fn), detail[1])
    errs = _errors(res)
    assert not res.ok and "dimpost='<>0'" in errs and "顯示「1200」" in errs


def test_dimrnd_rerendered_is_red(detail, tmp_path):
    def fn(doc):
        for dim in doc.modelspace().query("DIMENSION"):
            ov = dim.override()
            ov["dimrnd"] = 1000
            ov.commit()
            ov.render()

    res = check_detail(_mutate(detail[0], tmp_path / "x.dxf", fn), detail[1])
    assert not res.ok and "dimrnd=1000" in _errors(res)


def test_detail_small_text_and_overlap_are_red(detail, tmp_path):
    def fn(doc):
        ts = _texts(doc, "RD")
        al, p0, _ = ts[0].get_placement()
        ts[1].set_placement(p0, align=al)  # 同錨點、同對齊 → 文字疊在一起
        for t in doc.modelspace().query("TEXT"):
            if t.dxf.text == "RC 溝體":
                t.dxf.height = 0.8

    res = check_detail(_mutate(detail[0], tmp_path / "x.dxf", fn), detail[1])
    errs = _errors(res)
    assert not res.ok and "字高" in errs and "文字重疊" in errs


def test_orifice_el_marker_at_center(detail):
    """詳圖「孔口 … 中心 EL=」的 ▽ 標示點在孔口中心（不是孔口底）。"""
    res = check_detail(*detail)
    assert "中心 EL=" not in _errors(res)
