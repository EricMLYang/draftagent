"""屬性測試：隨機遞增樁號 + 順坡溝底 → profile_check 綠；任一段逆坡 → 紅。"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from draftagent.render.profile import write_profile
from draftagent.schema.profile import ProfileSpec
from draftagent.verify.profile_check import check_profile

SHEET = {
    "size": "A1",  # A3 內框約只放得下 1/100 下 ~30 m；A1 讓隨機長度不被圖紙尺寸卡住
    "title_block": {"project": "屬性測試工程", "drawing_no": "P-01"},
    "north_arrow": False,
}


@st.composite
def profiles(draw):
    """2–6 個節點；樁號、高程皆取到 0.01 m（表格帶文字為 2 位小數）。"""
    n = draw(st.integers(2, 6))
    # 每段 3–12 m：5 段最長 60 m，在 1/100 下 600 mm 放得進 A1 內框（renderer 放不下會刻意報錯）
    gaps = draw(st.lists(st.integers(300, 1200), min_size=n - 1, max_size=n - 1))  # cm
    drops = draw(st.lists(st.integers(0, 30), min_size=n - 1, max_size=n - 1))  # cm，≥0 = 順坡
    start_st = draw(st.integers(0, 5000))  # cm
    inv0 = draw(st.integers(9000, 12000))  # cm
    cover = draw(st.lists(st.integers(60, 150), min_size=n, max_size=n))  # cm

    stations, inverts = [start_st], [inv0]
    for g, d in zip(gaps, drops, strict=True):
        stations.append(stations[-1] + g)
        inverts.append(inverts[-1] - d)
    nodes = [
        {
            "station_m": s / 100,
            "label": f"MH{i}",
            "ground_el": (inv + cv) / 100,
            "invert_el": inv / 100,
        }
        for i, (s, inv, cv) in enumerate(zip(stations, inverts, cover, strict=True))
    ]
    segments = [{"section": "RCP D300", "size_m": 0.3} for _ in range(n - 1)]
    return {
        "title": "屬性測試縱斷面",
        "line_name": "P1",
        "h_scale": draw(st.sampled_from([100, 200])),
        "v_scale": draw(st.sampled_from([50, 100])),
        "nodes": nodes,
        "segments": segments,
        "sheet": SHEET,
    }


def _check(raw: dict):
    spec = ProfileSpec.model_validate(raw)
    with tempfile.TemporaryDirectory() as d:
        sp = Path(d) / "spec.json"
        sp.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        dxf = write_profile(spec, Path(d) / "profile.dxf")
        return check_profile(dxf, sp)


@settings(max_examples=30, deadline=None)
@given(raw=profiles())
def test_downhill_profile_passes(raw):
    res = _check(raw)
    assert res.ok, res.render()


@settings(max_examples=30, deadline=None)
@given(raw=profiles(), data=st.data())
def test_adverse_slope_fails(raw, data):
    nodes = raw["nodes"]
    i = data.draw(st.integers(1, len(nodes) - 1), label="downstream node")
    rise = data.draw(st.integers(1, 30), label="rise cm") / 100
    up = nodes[i - 1]
    down = nodes[i]
    down["invert_el"] = round(up["invert_el"] + rise, 2)
    down["ground_el"] = max(down["ground_el"], round(down["invert_el"] + 0.6, 2))
    res = _check(raw)
    assert not res.ok
    assert any("逆坡" in e for e in res.errors), res.render()
