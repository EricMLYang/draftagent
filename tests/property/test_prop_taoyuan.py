"""屬性測試：hydro.taoyuan 的 Horner、合理化公式、曼寧、必要貯集量。

係數一律從 yaml 讀（load_codes），測試不寫死任何法規數值。
"""

from __future__ import annotations

import math

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from draftagent.hydro.taoyuan import (
    horner_intensity,
    load_codes,
    manning_circular,
    manning_rect,
    rational_q,
    required_storage,
)

CODES = load_codes()
STATIONS = [
    (s, int(k[1:])) for s, v in CODES["horner"]["stations"].items() for k in v if k.startswith("T")
]

pos = st.floats(min_value=1e-3, max_value=1e4, allow_nan=False, allow_infinity=False)
C_ = st.floats(min_value=0.05, max_value=1.0)
I_ = st.floats(min_value=1.0, max_value=500.0)
A_ = st.floats(min_value=1e-3, max_value=100.0)
k_ = st.floats(min_value=0.1, max_value=10.0)
slope_ = st.floats(min_value=1e-4, max_value=0.2)
n_ = st.floats(min_value=0.009, max_value=0.08)


@settings(max_examples=30, deadline=None)
@given(stn=st.sampled_from(STATIONS), t1=st.floats(1.0, 300.0), dt=st.floats(0.01, 300.0))
def test_horner_decreasing_in_t(stn, t1, dt):
    station, T = stn
    i1 = horner_intensity(station, T, t1, CODES)
    i2 = horner_intensity(station, T, t1 + dt, CODES)
    assert i1 > 0 and i2 > 0
    assert i2 < i1


@settings(max_examples=30, deadline=None)
@given(C=C_, I=I_, A=A_, k=k_)
def test_rational_linear(C, I, A, k):
    q = rational_q(C, I, A)
    assert q > 0
    assert rational_q(k * C, I, A) == pytest.approx(k * q, rel=1e-12)
    assert rational_q(C, k * I, A) == pytest.approx(k * q, rel=1e-12)
    assert rational_q(C, I, k * A) == pytest.approx(k * q, rel=1e-12)
    # 可加性：A = A1 + A2
    assert rational_q(C, I, A) + rational_q(C, I, 2 * A) == pytest.approx(
        rational_q(C, I, 3 * A), rel=1e-12
    )


@settings(max_examples=30, deadline=None)
@given(
    D=st.floats(0.1, 3.0),
    n=n_,
    s1=slope_,
    ds=st.floats(1e-5, 0.2),
    frac=st.one_of(st.none(), st.floats(0.05, 0.99)),
)
def test_manning_circular_increasing_in_slope(D, n, s1, ds, frac):
    y = None if frac is None else frac * D
    q1 = manning_circular(D, n, s1, y)["Q_cms"]
    q2 = manning_circular(D, n, s1 + ds, y)["Q_cms"]
    assert q2 > q1 > 0
    # Q ∝ √S
    assert q2 / q1 == pytest.approx(math.sqrt((s1 + ds) / s1), rel=1e-9)


@settings(max_examples=30, deadline=None)
@given(b=st.floats(0.1, 5.0), y=st.floats(0.01, 3.0), n=n_, s1=slope_, ds=st.floats(1e-5, 0.2))
def test_manning_rect_increasing_in_slope(b, y, n, s1, ds):
    q1 = manning_rect(b, n, s1, y)["Q_cms"]
    q2 = manning_rect(b, n, s1 + ds, y)["Q_cms"]
    assert q2 > q1 > 0


@settings(max_examples=30, deadline=None)
@given(As=st.floats(0.0, 1e6, allow_nan=False))
def test_required_storage_is_max_of_both_rules(As):
    k_ty = CODES["runoff_standard"]["min_storage_m3_per_m2"]
    k_bc = CODES["building_code_4_3"]["min_storage_m3_per_m2"]
    r = required_storage(As, CODES)
    req = r["required_m3"]["value"]
    assert req >= As * k_ty - 1e-9
    assert req >= As * k_bc - 1e-9
    assert req == pytest.approx(max(As * k_ty, As * k_bc), rel=1e-12, abs=1e-12)
    assert r["taoyuan_m3"]["value"] == pytest.approx(As * k_ty)
    assert r["building_code_m3"]["value"] == pytest.approx(As * k_bc)
    assume(As > 0)
    adopted_k = k_ty if r["adopted"] == "runoff_standard" else k_bc
    assert req == pytest.approx(As * adopted_k)
