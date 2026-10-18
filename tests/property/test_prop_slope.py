"""屬性測試：hydro.slope 的 Horn 坡度與分級面積守恆。"""

from __future__ import annotations

import math

import numpy as np
from hypothesis import given, settings
from hypothesis import strategies as st
from rasterio.transform import from_origin
from shapely.geometry import box

from draftagent.hydro.slope import (
    class_stats,
    classify,
    load_slope_classes,
    site_mask,
    slope_percent,
)
from draftagent.schema.slope import DEFAULT_CLASSES_PATH

TABLE = load_slope_classes(DEFAULT_CLASSES_PATH)

coef = st.floats(min_value=-2.0, max_value=2.0, allow_nan=False, allow_infinity=False)
cell = st.sampled_from([0.5, 1.0, 2.0, 5.0, 10.0])
dim = st.integers(min_value=3, max_value=24)


def _plane(nrows: int, ncols: int, cx: float, cy: float, a: float, b: float, c: float):
    """z = a·x + b·y + c，x 向東、y 向北；第 0 列在北（同 GeoTIFF）。"""
    cols = np.arange(ncols) * cx
    rows = -np.arange(nrows) * cy
    x, y = np.meshgrid(cols, rows)
    return a * x + b * y + c


@settings(max_examples=30, deadline=None)
@given(a=coef, b=coef, c=st.floats(-100, 3000), nrows=dim, ncols=dim, cx=cell, cy=cell)
def test_planar_dtm_interior_slope(a, b, c, nrows, ncols, cx, cy):
    z = _plane(nrows, ncols, cx, cy, a, b, c)
    s = slope_percent(z, cx, cy)
    want = 100.0 * math.hypot(a, b)
    inner = s[1:-1, 1:-1]
    assert np.all(np.isfinite(inner))
    np.testing.assert_allclose(inner, want, rtol=1e-9, atol=1e-6)
    # 最外圈一律 NaN
    assert np.all(np.isnan(s[0, :])) and np.all(np.isnan(s[-1, :]))
    assert np.all(np.isnan(s[:, 0])) and np.all(np.isnan(s[:, -1]))


@settings(max_examples=30, deadline=None)
@given(
    data=st.data(),
    nrows=st.integers(4, 30),
    ncols=st.integers(4, 30),
    cx=cell,
    cy=cell,
    seed=st.integers(0, 2**32 - 1),
)
def test_class_areas_sum_to_mask_area(data, nrows, ncols, cx, cy, seed):
    rng = np.random.default_rng(seed)
    z = rng.normal(100.0, data.draw(st.floats(0.0, 20.0)), size=(nrows, ncols))
    # 隨機挖幾個 nodata
    holes = rng.random((nrows, ncols)) < data.draw(st.floats(0.0, 0.2))
    z[holes] = np.nan

    x0, y0 = 300_000.0, 2_700_000.0
    transform = from_origin(x0, y0, cx, cy)
    # 基地 = 柵格範圍內任意矩形（可能部分超出）
    c0 = data.draw(st.integers(-2, ncols - 1))
    c1 = data.draw(st.integers(c0 + 1, ncols + 2))
    r0 = data.draw(st.integers(-2, nrows - 1))
    r1 = data.draw(st.integers(r0 + 1, nrows + 2))
    geom = box(x0 + c0 * cx, y0 - r1 * cy, x0 + c1 * cx, y0 - r0 * cy)
    mask = site_mask(geom, transform, z.shape)

    cls = classify(slope_percent(z, cx, cy), TABLE)
    rows = class_stats(cls, mask, cx * cy, TABLE)

    mask_area = float(mask.sum()) * cx * cy
    assert sum(r["pixel_count"] for r in rows) == int(mask.sum())
    assert math.isclose(sum(r["area_m2"] for r in rows), mask_area, abs_tol=1e-3 * len(rows))
    # 每個法定級別與 nodata 都有列出
    assert {r["class"] for r in rows} == {TABLE.nodata_class} | {c.cls for c in TABLE.classes}
