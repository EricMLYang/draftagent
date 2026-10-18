"""合成「雜亂建築師 DXF」fixture（tools/make_fixtures_archdxf.py）的自我一致性檢查。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import ezdxf
import numpy as np
import pytest
from ezdxf import recover
from shapely.geometry import Polygon

REPO = Path(__file__).resolve().parents[1]
ROOT = REPO / "tests" / "fixtures" / "archdxf"
CASES = sorted(p.name for p in ROOT.iterdir() if p.is_dir()) if ROOT.exists() else []
REQUIRED = {
    "case",
    "units",
    "insunits_header",
    "layers",
    "control_points",
    "footprint_twd97",
    "basement_twd97",
    "floor_filter",
    "site_twd97",
    "gap_mm",
    "notes",
}


def _truth(case: str) -> dict:
    return json.loads((ROOT / case / "truth.json").read_text(encoding="utf-8"))


def _fit(cps: list[dict]) -> tuple[complex, complex, np.ndarray]:
    """最小二乘相似變換 w = a·z + b（複數），回傳 a, b 與各點殘差（TWD97 m）。"""
    z = np.array([complex(*c["drawing"]) for c in cps])
    w = np.array([complex(*c["twd97"]) for c in cps])
    zc, wc = z - z.mean(), w - w.mean()
    a = (np.conj(zc) @ wc) / (np.conj(zc) @ zc)
    b = w.mean() - a * z.mean()
    return a, b, np.abs(a * z + b - w)


def test_at_least_8_cases():
    assert len(CASES) >= 8, CASES
    for must in (
        "clean_mm",
        "layer_names_cn",
        "rotated",
        "double_line_walls",
        "gaps_unjoined",
        "blocks_xref",
        "multi_floor",
        "wrong_insunits",
        "noise_heavy",
    ):
        assert must in CASES


@pytest.mark.parametrize("case", CASES)
def test_dxf_readable_and_small(case):
    p = ROOT / case / "drawing.dxf"
    assert p.stat().st_size < 300 * 1024
    try:
        doc = ezdxf.readfile(p)
    except ezdxf.DXFStructureError:
        doc, _ = recover.readfile(p)
    t = _truth(case)
    assert doc.header.get("$INSUNITS", 0) == (t["insunits_header"] or 0)
    layers = {ly.dxf.name for ly in doc.layers}
    for key in ("exterior_wall", "basement"):
        if t["layers"].get(key):
            assert t["layers"][key] in layers


@pytest.mark.parametrize("case", CASES)
def test_truth_fields(case):
    t = _truth(case)
    assert REQUIRED <= set(t), REQUIRED - set(t)
    assert t["case"] == case
    assert t["units"] in ("mm", "m")
    assert len(t["control_points"]) >= 3
    assert t["notes"].strip()
    for poly in (t["footprint_twd97"], t["site_twd97"]):
        assert poly[0] != poly[-1]
        for e, n in poly:
            assert 250000 <= e <= 320000 and 2700000 <= n <= 2800000


@pytest.mark.parametrize("case", CASES)
def test_control_points_consistent(case):
    t = _truth(case)
    a, _, res = _fit(t["control_points"])
    assert res.max() < 0.001, res  # < 1 mm（mm 圖）/ 0.001 m（m 圖）
    expected_scale = 0.001 if t["units"] == "mm" else 1.0
    assert abs(abs(a) - expected_scale) / expected_scale < 1e-6
    if case == "twd97_native":
        for c in t["control_points"]:
            assert c["drawing"] == c["twd97"]


@pytest.mark.parametrize("case", CASES)
def test_footprint_valid_in_site(case):
    t = _truth(case)
    fp = Polygon(t["footprint_twd97"])
    site = Polygon(t["site_twd97"])
    assert fp.is_valid and site.is_valid
    assert fp.exterior.is_ccw
    assert 300 < fp.area < 1600, fp.area  # 18–40 m 級集合住宅
    assert site.contains(fp)
    if t["basement_twd97"]:
        bs = Polygon(t["basement_twd97"])
        assert bs.is_valid and bs.exterior.is_ccw and site.contains(bs)


@pytest.mark.parametrize(
    "case", ["clean_mm", "rotated", "wrong_insunits", "layer_names_cn", "twd97_native"]
)
def test_simple_cases_polyline_matches_truth(case):
    """外牆為單一封閉 LWPOLYLINE 的 case：用 truth 控制點轉換後應與 footprint 真值相符。"""
    t = _truth(case)
    doc = ezdxf.readfile(ROOT / case / "drawing.dxf")
    a, b, _ = _fit(t["control_points"])
    pls = [
        e
        for e in doc.modelspace().query("LWPOLYLINE")
        if e.dxf.layer == t["layers"]["exterior_wall"] and e.closed
    ]
    assert len(pls) == 1
    pts = [a * complex(x, y) + b for x, y in pls[0].vertices()]
    got = Polygon([(p.real, p.imag) for p in pts])
    assert got.symmetric_difference(Polygon(t["footprint_twd97"])).area < 0.01


def test_generator_check_clean():
    r = subprocess.run(
        [sys.executable, str(REPO / "tools" / "make_fixtures_archdxf.py"), "--check"],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    assert r.returncode == 0, r.stdout + r.stderr
