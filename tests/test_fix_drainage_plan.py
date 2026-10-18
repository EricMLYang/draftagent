"""drainage-plan verifier 稽核修正的回歸測試（稽核 #1、#4、#6、#7、#8、#9/#10、#14）。

每個情境都由「綠的 build 產物」出發，只竄改一處，必須轉紅。
"""

from __future__ import annotations

import copy
import json
import math
import shutil
from pathlib import Path

import ezdxf
import pytest
from pydantic import ValidationError

from draftagent.build import build
from draftagent.drainage import derive, network
from draftagent.drainage.model import DrainageNetworkSpec
from draftagent.kinds.drainage_plan import KIND, render
from draftagent.verify.drainage_plan_check import (
    check_drainage_plan,
    independent_pipe_flows,
)
from draftagent.verify.sheetset_check import check_sheetset

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "drainage-plan.json"
RAW = json.loads(EXAMPLE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def spec() -> DrainageNetworkSpec:
    return DrainageNetworkSpec.from_json(EXAMPLE)


@pytest.fixture(scope="module")
def arts(tmp_path_factory, spec) -> dict[str, Path]:
    return render(spec, tmp_path_factory.mktemp("fixset"))


def _copy(arts, tmp_path) -> dict[str, Path]:
    out = {}
    for k, p in arts.items():
        q = tmp_path / p.name
        shutil.copy(p, q)
        out[k] = q
    return out


def _run_all(spec, arts) -> list:
    return [v(spec, arts) for v in KIND.verifiers]


def _errors(results) -> str:
    if not isinstance(results, list):
        results = [results]
    return "\n".join(e for r in results for e in r.errors)


def _edit(path: Path, fn) -> None:
    doc = ezdxf.readfile(str(path))
    fn(doc)
    doc.saveas(path)


def _mut(fn) -> DrainageNetworkSpec:
    raw = copy.deepcopy(RAW)
    fn(raw)
    return DrainageNetworkSpec.model_validate(raw)


def _node(raw, nid):
    return next(n for n in raw["nodes"] if n["id"] == nid)


def _link(raw, lid):
    return next(lk for lk in raw["links"] if lk["id"] == lid)


# ---------------------------------------------------------------- 綠


def test_example_all_green(spec, arts):
    for r in _run_all(spec, arts):
        assert r.ok, r.render()


# ---------------------------------------------------------------- #1 codes_path


def test_codes_path_outside_repo_rejected(tmp_path):
    fake = tmp_path / "fake.yaml"
    fake.write_text((ROOT / "draftagent/hydro/codes/taoyuan_runoff.yaml").read_text("utf-8"), "utf-8")
    raw = copy.deepcopy(RAW)
    raw["codes_path"] = str(fake)
    with pytest.raises(ValidationError, match="codes"):
        DrainageNetworkSpec.model_validate(raw)


def test_codes_path_not_default_is_red(spec, arts):
    other = ROOT / "draftagent/hydro/codes/outflow_control.yaml"  # 在 codes/ 下但不是預設
    sp = spec.model_copy(update={"codes_path": other})
    res = check_drainage_plan(arts["dxf"], sp)
    assert "codes_path" in _errors(res)


# ---------------------------------------------------------------- #4 中間檔 / 上游加權 C


def test_tampered_calc_spec_and_result_regenerated_is_red(spec, arts, tmp_path):
    from draftagent.hydro.taoyuan import compute
    from draftagent.schema.drainage import TaoyuanSiteDrainageSpec

    a = _copy(arts, tmp_path)
    cs = json.loads(a["calc_spec"].read_text("utf-8"))
    for s in cs["subareas"]:
        s["landuse"], s["c_override"] = "park_green", None
    a["calc_spec"].write_text(json.dumps(cs, ensure_ascii=False), "utf-8")
    r = compute(TaoyuanSiteDrainageSpec.model_validate(cs))
    a["result"].write_text(json.dumps(r, ensure_ascii=False), "utf-8")
    from draftagent.kinds.drainage_plan import verify_calc

    assert "中間檔與模型不符" in _errors(verify_calc(spec, a))


def test_tampered_profile_spec_rerendered_is_red(spec, arts, tmp_path):
    from draftagent.kinds.drainage_plan import verify_profile
    from draftagent.render.profile import write_profile
    from draftagent.schema.profile import ProfileSpec

    a = _copy(arts, tmp_path)
    ps = json.loads(a["profile_spec"].read_text("utf-8"))
    for n in ps["nodes"]:
        n["ground_el"] = round(n["ground_el"] + 0.3, 2)
    a["profile_spec"].write_text(json.dumps(ps, ensure_ascii=False), "utf-8")
    write_profile(ProfileSpec.model_validate(ps), a["profile_dxf"])
    assert "中間檔與模型不符" in _errors(verify_profile(spec, a))


def test_tampered_detail_spec_rerendered_is_red(spec, arts, tmp_path):
    from draftagent.kinds.drainage_plan import verify_detail
    from draftagent.render.detail import write_detail_sheet
    from draftagent.schema.detail import DetailSheetSpec

    a = _copy(arts, tmp_path)
    ds = json.loads(a["detail_spec"].read_text("utf-8"))
    for d in ds["details"]:
        if d.get("template") == "orifice_plate":
            d["orifice"]["diameter"] = 300.0
            d["plate_width"] = d["plate_height"] = 600.0
            d["orifice_center_height"] = 300.0
    a["detail_spec"].write_text(json.dumps(ds, ensure_ascii=False), "utf-8")
    write_detail_sheet(DetailSheetSpec.model_validate(ds), a["detail_dxf"])
    assert "中間檔與模型不符" in _errors(verify_detail(spec, a))


def test_upstream_weighted_c(spec):
    rep = network.check_network(spec)
    # P1 / P2 只承接 B2（混凝土 C=0.90）；全基地綜合 C 約 0.74
    assert rep.link_c["P1"] == pytest.approx(0.90)
    assert rep.link_c["P3"] == pytest.approx(0.56)
    ind = independent_pipe_flows(spec)
    assert set(ind) == set(derive.pipe_design_c(spec))
    for lid, c in derive.pipe_design_c(spec).items():
        assert ind[lid]["C"] == pytest.approx(c, rel=1e-12)


def test_result_pipe_q_tampered_is_red(spec, arts, tmp_path):
    from draftagent.kinds.drainage_plan import verify_calc

    a = _copy(arts, tmp_path)
    r = json.loads(a["result"].read_text("utf-8"))
    r["pipes"][1]["Q_design_cms"]["value"] /= 2
    a["result"].write_text(json.dumps(r, ensure_ascii=False), "utf-8")
    assert "獨立重算" in _errors(verify_calc(spec, a))


def test_undersized_pipe_red_with_upstream_c(tmp_path):
    def f(r):
        _link(r, "P1")["section"]["d"] = 0.20
        _link(r, "P2")["section"]["d"] = 0.20

    sp = _mut(f)
    res = build(sp, name="ds6", out=tmp_path, preview=False)
    assert not res.ok
    assert "上游集水分區加權 C" in _errors(res.results)


def test_calc_ty_unchanged_without_network():
    """drainage-calc-ty（無網路拓樸）維持全基地綜合 C。"""
    from draftagent.hydro.taoyuan import compute
    from draftagent.schema.drainage import TaoyuanSiteDrainageSpec

    sp = TaoyuanSiteDrainageSpec.from_json(ROOT / "examples" / "drainage-calc-ty.json")
    r = compute(sp)
    assert all("C_design" not in p for p in r["pipes"])
    for p in r["pipes"]:
        assert p["Q_design_cms"]["source"].startswith("rational(C_composite")


# ---------------------------------------------------------------- #6 平面拓樸幾何


def test_flow_arrows_reversed_is_red(spec, arts, tmp_path):
    a = _copy(arts, tmp_path)

    def flip(doc):
        from ezdxf.math import Matrix44

        for e in doc.modelspace().query("LWPOLYLINE"):
            if e.dxf.layer == "C-STRM-FLOW":
                pts = list(e.get_points("xy"))
                cx = sum(p[0] for p in pts) / 3
                cy = sum(p[1] for p in pts) / 3
                e.transform(
                    Matrix44.chain(
                        Matrix44.translate(-cx, -cy, 0), Matrix44.z_rotate(math.pi), Matrix44.translate(cx, cy, 0)
                    )
                )

    _edit(a["dxf"], flip)
    assert "相反" in _errors(check_drainage_plan(a["dxf"], spec))


def test_link_labels_swapped_is_red(spec, arts, tmp_path):
    a = _copy(arts, tmp_path)

    def swap(doc):
        ts = {t.dxf.text.split()[0]: t for t in doc.modelspace().query("TEXT") if " S=" in t.dxf.text}
        x, y = ts["P1"], ts["P3"]
        (al, pa, _), (bl, pb, _) = x.get_placement(), y.get_placement()
        x.set_placement(pb, align=al)
        y.set_placement(pa, align=bl)

    _edit(a["dxf"], swap)
    assert "標籤離管段" in _errors(check_drainage_plan(a["dxf"], spec))


def test_pipe_line_missing_or_moved_is_red(spec, arts, tmp_path):
    a = _copy(arts, tmp_path)
    mh1, si1 = spec.node("MH1"), spec.node("SI1")

    def move(doc):
        for e in doc.modelspace().query("LINE"):
            if e.dxf.layer == "C-STRM" and math.dist((e.dxf.start.x, e.dxf.start.y), (mh1.x, mh1.y)) < 0.01:
                e.dxf.end = (si1.x - 3.0, si1.y + 5.0, 0)

    _edit(a["dxf"], move)
    assert "圖面缺 C-STRM 線" in _errors(check_drainage_plan(a["dxf"], spec))


def test_catchment_boundary_wrong_is_red(spec, arts, tmp_path):
    a = _copy(arts, tmp_path)

    def shrink(doc):
        for e in doc.modelspace().query("LWPOLYLINE"):
            if e.dxf.layer == "C-CTCH" and len(e) == 4:
                pts = list(e.get_points("xy"))
                x0 = pts[0][0]
                e.set_points([(x0 + (x - x0) * 0.7, y) for x, y in pts], format="xy")

    _edit(a["dxf"], shrink)
    assert "Hausdorff" in _errors(check_drainage_plan(a["dxf"], spec))


# ---------------------------------------------------------------- #7 viewport 可見性


def test_viewport_freezes_network_layers_is_red(spec, arts, tmp_path):
    a = _copy(arts, tmp_path)

    def freeze(doc):
        vp = next(v for v in doc.layouts.get("平面設計圖").query("VIEWPORT") if v.dxf.id != 1)
        vp.frozen_layers = [*vp.frozen_layers, "C-STRM"]

    _edit(a["dxf"], freeze)
    assert "凍結必要圖層" in _errors(check_drainage_plan(a["dxf"], spec))


def test_layer_noplot_is_red(spec, arts, tmp_path):
    a = _copy(arts, tmp_path)
    _edit(a["dxf"], lambda doc: setattr(doc.layers.get("C-STRM-TEXT").dxf, "plot", 0))
    assert "不出圖" in _errors(check_drainage_plan(a["dxf"], spec))


def test_viewport_panned_off_site_is_red(spec, arts, tmp_path):
    a = _copy(arts, tmp_path)

    def pan(doc):
        vp = next(v for v in doc.layouts.get("集水區分析圖").query("VIEWPORT") if v.dxf.id != 1)
        c = vp.dxf.view_center_point
        vp.dxf.view_center_point = (c[0] + 500, c[1] + 500, 0)

    _edit(a["dxf"], pan)
    assert "未完整涵蓋基地" in _errors(check_drainage_plan(a["dxf"], spec))


# ---------------------------------------------------------------- #8 物理合理性


@pytest.mark.parametrize(
    ("mut", "needle"),
    [
        (lambda r: _node(r, "MH1").__setitem__("ground_el", 101.20), "覆土不足"),
        (lambda r: _node(r, "CB3").update(x=268456.0, y=2760380.0), "不在基地範圍內"),
        (
            lambda r: r["storage"].__setitem__(
                "polygon",
                [[268446.0, 2760314.5], [268462.0, 2760314.5], [268462.0, 2760326.5], [268446.0, 2760326.5]],
            ),
            "與建物",
        ),
        (
            lambda r: r["existing_drains"][0]["points"][1].__setitem__("invert_el", 99.85),
            "逆坡",
        ),
        (
            lambda r: (_node(r, "OF1").__setitem__("y", 2760306.5)),
            "未接到既有排水",
        ),
    ],
    ids=["cover", "outside-site", "pond-under-building", "drain-adverse", "outfall-unconnected"],
)
def test_physical_plausibility_red(tmp_path, mut, needle):
    sp = _mut(mut)
    from draftagent.render.drainage_plan import write_drainage_plan

    p = write_drainage_plan(sp, tmp_path / "x.dxf")
    assert needle in _errors(check_drainage_plan(p, sp))


# ---------------------------------------------------------------- #9 / #10 可讀性


def test_tiny_text_is_red(spec, arts, tmp_path):
    a = _copy(arts, tmp_path)

    def tiny(doc):
        for t in doc.modelspace().query("TEXT"):
            t.dxf.height = 0.05

    _edit(a["dxf"], tiny)
    assert "印出字高" in _errors(check_drainage_plan(a["dxf"], spec))


def test_overlapping_labels_are_red(spec, arts, tmp_path):
    a = _copy(arts, tmp_path)

    def stack(doc):
        ts = [t for t in doc.modelspace().query("TEXT") if t.dxf.layer == "C-STRM-TEXT" and " S=" in t.dxf.text]
        al, p, _ = ts[0].get_placement()
        ts[1].set_placement(p, align=al)
        ts[1].dxf.rotation = ts[0].dxf.rotation

    _edit(a["dxf"], stack)
    assert "文字重疊" in _errors(check_drainage_plan(a["dxf"], spec))


# ---------------------------------------------------------------- #14 整套圖完整性


def test_orifice_plate_derived(spec):
    det = derive.derive_details(spec)
    op = [d for d in det.details if d.template == "orifice_plate"]
    assert len(op) == 1
    assert op[0].orifice.diameter == pytest.approx(spec.calc.outlet.diameter_m * 1000)


@pytest.mark.parametrize(
    ("fname", "old", "new", "needle"),
    [
        ("dxf", "V=124.80 m³", "V=224.80 m³", "貯集量"),
        ("dxf", "有效水深 0.65 m", "有效水深 1.65 m", "有效水深"),
        ("dxf", "頂EL=100.55", "頂EL=101.55", "EX2[0]"),
        ("dxf", "路中EL=101.42", "路中EL=199.99", "路中"),
        ("detail_dxf", "孔口板詳圖 D135", "孔口板詳圖 D200", "孔口尺寸"),
    ],
)
def test_sheetset_label_tamper_is_red(spec, arts, tmp_path, fname, old, new, needle):
    a = _copy(arts, tmp_path)
    n = []

    def f(doc):
        for lay in doc.layouts:
            for t in lay.query("TEXT"):
                if old in t.dxf.text:
                    t.dxf.text = t.dxf.text.replace(old, new)
                    n.append(t)
        for blk in doc.blocks:
            for t in blk.query("TEXT"):
                if old in t.dxf.text:
                    t.dxf.text = t.dxf.text.replace(old, new)
                    n.append(t)

    _edit(a[fname], f)
    assert n, f"沒有找到「{old}」"
    errs = _errors(check_sheetset(a, spec))
    assert needle in errs, errs


def test_sheetset_orifice_plate_missing_is_red(spec, arts, tmp_path):
    a = _copy(arts, tmp_path)

    def drop(doc):
        msp = doc.modelspace()
        for name, g in list(doc.groups):
            if name == "DETAIL-OP":
                for e in list(g):
                    msp.delete_entity(e)

    _edit(a["detail_dxf"], drop)
    assert "詳圖缺孔口尺寸文字" in _errors(check_sheetset(a, spec))
