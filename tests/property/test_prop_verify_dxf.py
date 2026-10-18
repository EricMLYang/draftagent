"""屬性測試：隨機矩形房間 render_plan → dxf_check 必綠；注入 dimlfac≠1 必紅。"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from draftagent.render.dxf import render_plan
from draftagent.schema.plan import PlanSpec
from draftagent.verify.dxf_check import check_dxf

size = st.integers(min_value=2000, max_value=20000)
# 0.0 與 1.0 附近都避開：dimlfac=0 在 ezdxf 有特殊意義，1±1e-9 在 verifier 容差內
bad_lfac = st.one_of(
    st.floats(min_value=0.01, max_value=0.999),
    st.floats(min_value=1.001, max_value=1000.0),
)


def _spec(width: int, depth: int) -> PlanSpec:
    return PlanSpec.model_validate(
        {
            "title": "prop",
            "wall_thickness": 200,
            "rooms": [{"name": "R", "rect": {"x": 0, "y": 0, "width": width, "depth": depth}}],
            "openings": [{"type": "door", "side": "S", "offset": 400, "width": 900}],
        }
    )


@settings(max_examples=30, deadline=None)
@given(width=size, depth=size)
def test_random_rect_room_passes(width, depth):
    spec = _spec(width, depth)
    doc = render_plan(spec)
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "plan.dxf"
        doc.saveas(path)
        res = check_dxf(path, expect_dims=[width, depth])
    assert res.ok, res.render()
    assert sorted(res.metrics["dimensions_displayed"]) == pytest.approx(sorted([width, depth]))
    assert sorted(res.metrics["dimensions"]) == pytest.approx(sorted([width, depth]))


@settings(max_examples=30, deadline=None)
@given(width=size, depth=size, lfac=bad_lfac, which=st.integers(0, 1))
def test_injected_dimlfac_fails(width, depth, lfac, which):
    doc = render_plan(_spec(width, depth))
    dims = list(doc.modelspace().query("DIMENSION"))
    assert dims
    ov = dims[which % len(dims)].override()
    ov["dimlfac"] = lfac
    ov.commit()
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "bad.dxf"
        doc.saveas(path)
        res = check_dxf(path)
    assert not res.ok
    assert any("dimlfac" in e for e in res.errors), res.render()
