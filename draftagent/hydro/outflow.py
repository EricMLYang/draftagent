"""出流管制計畫書：確定性水文 / 水理計算（純函式，不得呼叫 LLM）。

所有法規 / 手冊數值從 `draftagent/hydro/codes/outflow_control.yaml` 讀；程式內不寫門檻或係數。
結果中的數值附 `source`（yaml key 路徑或 spec 欄位），方便人工追溯。

方法（依據見 yaml 各段 clause）
------------------------------
1. 設計雨型（基準 第五、六點；手冊 4.3）：Horner I = a/(t+b)^c，延時 1440 min、刻度 10 min，
   交替區塊法（最大值置中、右大左小）。總量 = R24 = I(1440)·24。
2. 有效降雨（第七點）：SCS-CN 累積法 Pe = (P−0.2Y)²/(P+0.8Y)，Y = 25.4(1000/CN − 10)；
   逐時段有效雨量 = 累積 Pe 之差分。開發前 CN > 70 以 70 計（手冊 4.4（三））。
   多分區 CN 以面積加權（本 repo 慣例）。
3. 集流時間（第八點）：Tc = T1 + T2，< 10 min 以 10 min 計。
4. SCS 無因次單位歷線（第九點、附表五）：Tp = tr/2 + 0.6Tc，Qp = 0.208·A·Re/Tp。
   離散化後單位歷線以比例校正至體積恰為 1 mm × A（本 repo 慣例，回報校正前比值）。
5. 外水位歷線（第十點）：Ht = hp/4 + (3/4)(Qt/Qp)hp + Hb。
6. 滯洪演算：修正 Puls（storage-indication），每子步以二分法解
   V(H₂) + O(H₂)Δt/2 = V₁ − O₁Δt/2 + (I₁+I₂)Δt/2，外水位隨時間變化亦適用。
7. 出流工（手冊 附件一）：垂直孔口 Q = C0·A0·√(2gZ)；水平孔口 min(Cw·Pe·he^1.5, n·C0·A0·√(2g·he))；
   堰 Q = Cd·(2/3)·√(2g)·L·H1^1.5（Cd 依 H1/BW 或 H1/P1，式 1-9～1-12）。
8. H-A-V（附 1.3）：Vn = ΔH(An + An+1 + √(An·An+1))/3。
9. 檢核（第十二～十四點）：開發後 Q2/Q5/Q10 ≤ 開發前；滯洪體積安全係數 ≥ 1.2；簡易檢核 520 m³/ha、
   0.16 cms/ha；適用門檻依 114-09-03 修正辦法。判定在 `draftagent.verify.outflow_check`。
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
from collections.abc import Callable, Sequence
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from draftagent.schema.outflow import OutflowControlSpec

PING_PER_M2 = 0.3025
M2_PER_HA = 10_000.0
KM2_PER_HA = 0.01
DEFAULT_CODES_PATH = Path(__file__).resolve().parent / "codes" / "outflow_control.yaml"

_SOURCED_GROUPS = (
    "design_storm",
    "scs_cn",
    "cn_table",
    "time_of_concentration",
    "scs_unit_hydrograph",
    "tailwater",
    "peak_criterion",
    "detention",
    "simple_check",
    "hydraulics",
    "applicability",
)


# ---------------------------------------------------------------- yaml


@lru_cache(maxsize=8)
def _load_cached(path: str, mtime: float) -> dict[str, Any]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if "verified" not in raw:
        raise ValueError(f"{path}: 缺少頂層 verified 欄位")
    for g in _SOURCED_GROUPS:
        grp = raw.get(g)
        if not isinstance(grp, dict):
            raise TypeError(f"{path}: 缺少 {g} 或格式不是 mapping")
        for k in ("source", "url", "as_of"):
            if not grp.get(k):
                raise ValueError(f"{path}: {g}.{k} 不得為空")
    raw["_path"] = path
    return raw


def load_codes(path: str | Path | None = None) -> dict[str, Any]:
    """讀取並檢查 yaml：每組數值需有 source / url / as_of。"""
    p = Path(path) if path is not None else DEFAULT_CODES_PATH
    p = p.resolve()
    return _load_cached(str(p), p.stat().st_mtime)


def _v(value: Any, unit: str, source: str) -> dict[str, Any]:
    return {"value": value, "unit": unit, "source": source}


# ---------------------------------------------------------------- 雨量


def horner(a: float, b: float, c: float, t: float) -> float:
    """Horner 降雨強度 I = a/(t+b)^c（mm/hr；t: min）。基準 第五、六點。"""
    return a / (t + b) ** c


def design_storm_alternating_block(
    horner_params: dict[str, float], duration_min: int = 1440, dt: int = 10
) -> dict[str, Any]:
    """交替區塊法設計雨型（手冊 4.3）。

    回傳 {"depth_mm": [每時段雨量], "R24_mm", "dt_min", "peak_index", "source"}。
    最大值放第 (n−1)//2 個區塊，之後依序右、左交替（yaml design_storm.note）。
    """
    if duration_min % dt:
        raise ValueError("duration_min 必須為 dt 的整數倍")
    a, b, c = horner_params["a"], horner_params["b"], horner_params["c"]
    n = duration_min // dt
    cum = [horner(a, b, c, k * dt) * (k * dt) / 60.0 for k in range(1, n + 1)]
    inc = [cum[0]] + [cum[k] - cum[k - 1] for k in range(1, n)]
    ranked = sorted(inc, reverse=True)
    out = [0.0] * n
    center = (n - 1) // 2
    out[center] = ranked[0]
    right, left = center + 1, center - 1
    for i, v in enumerate(ranked[1:]):
        if (i % 2 == 0 and right < n) or left < 0:
            out[right] = v
            right += 1
        else:
            out[left] = v
            left -= 1
    return {
        "depth_mm": out,
        "R24_mm": cum[-1],
        "dt_min": dt,
        "peak_index": center,
        "source": "design_storm（基準 第五、六點；手冊 4.3）",
    }


# ---------------------------------------------------------------- SCS-CN


def scs_max_retention(cn: float) -> float:
    """Y = 25.4(1000/CN − 10)，mm（基準 第七點）。"""
    if not 0 < cn <= 100:
        raise ValueError(f"CN {cn} 不在 (0, 100]")
    return 25.4 * (1000.0 / cn - 10.0)


def scs_cumulative_excess(P: float, cn: float, ia_ratio: float = 0.2) -> float:
    Y = scs_max_retention(cn)
    ia = ia_ratio * Y
    if P <= ia:
        return 0.0
    return (P - ia) ** 2 / (P + (1.0 - ia_ratio) * Y)


def scs_runoff(P_series: Sequence[float], CN: float, ia_ratio: float = 0.2) -> dict[str, Any]:
    """逐時段雨量 → 逐時段有效雨量（累積法）。回傳 {"excess_mm", "Re_mm", "P_mm"}。"""
    cum_p, prev, out = 0.0, 0.0, []
    for p in P_series:
        cum_p += p
        pe = scs_cumulative_excess(cum_p, CN, ia_ratio)
        out.append(pe - prev)
        prev = pe
    return {"excess_mm": out, "Re_mm": prev, "P_mm": cum_p, "source": "scs_cn.formula"}


def composite_cn(subareas: Sequence[tuple[float, float]]) -> float:
    """面積加權 CN：subareas = [(area, cn), …]（本 repo 慣例）。"""
    total = sum(a for a, _ in subareas)
    return sum(a * c for a, c in subareas) / total


def lookup_cn(scs_class: int, soil_group: str, codes: dict[str, Any]) -> float:
    cls = codes["cn_table"]["classes"]
    if scs_class not in cls:
        raise KeyError(f"SCS 分類 {scs_class} 不在 yaml cn_table")
    return float(cls[scs_class][soil_group])


# ---------------------------------------------------------------- 集流時間


def overland_inlet_time_hr(length_m: float, slope_pct: float, cn: float) -> float:
    """漫地流流入時間 T1 = L^0.8 (Y+25.4)^0.7 / (4238 H^0.5)，hr（基準 第八點（一））。"""
    Y = scs_max_retention(cn)
    return length_m**0.8 * (Y + 25.4) ** 0.7 / (4238.0 * slope_pct**0.5)


def manning_velocity(n: float, R: float, S: float) -> float:
    return (1.0 / n) * R ** (2.0 / 3.0) * S**0.5


def apply_tc_floor(tc_min: float, codes: dict[str, Any]) -> dict[str, Any]:
    floor = codes["time_of_concentration"]["tc_min_floor"]
    used = max(tc_min, floor)
    return {
        "input_min": tc_min,
        "used_min": used,
        "floored": tc_min < floor,
        "source": "time_of_concentration.tc_min_floor" if tc_min < floor else "spec",
    }


# ---------------------------------------------------------------- 單位歷線


def scs_unit_hydrograph(
    A_km2: float, Tc_hr: float, dt_hr: float, codes: dict[str, Any] | None = None,
    *, normalize: bool = True,
) -> dict[str, Any]:
    """SCS 無因次單位歷線（1 mm 有效雨量、延時 tr = dt）。

    縱距 U_k 於 t = k·dt（k = 0…），依 yaml 表線性內插。normalize=True 時以比例校正使
    Σ U·dt·3600 = 1000·A（m³），並回報校正前比值 volume_ratio_raw。
    """
    codes = codes or load_codes()
    uh = codes["scs_unit_hydrograph"]
    tr = dt_hr
    Tp = tr / 2.0 + uh["lag_ratio"] * Tc_hr
    Qp = uh["peak_factor"] * A_km2 * 1.0 / Tp
    xs = [r[0] for r in uh["table"]]
    ys = [r[1] for r in uh["table"]]
    tmax = xs[-1] * Tp
    n = int(math.ceil(tmax / dt_hr)) + 1
    ords = [Qp * _interp(k * dt_hr / Tp, xs, ys) for k in range(n)]
    target = 1000.0 * A_km2  # m³（1 mm × A）
    vol = sum(ords) * dt_hr * 3600.0
    ratio = vol / target
    if normalize:
        ords = [q / ratio for q in ords]
    return {
        "Tp_hr": Tp,
        "Tlag_hr": uh["lag_ratio"] * Tc_hr,
        "tr_hr": tr,
        "Qp_cms_per_mm": Qp,
        "dt_hr": dt_hr,
        "ordinates_cms": ords,
        "volume_ratio_raw": ratio,
        "normalized": normalize,
        "volume_m3": sum(ords) * dt_hr * 3600.0,
        "target_volume_m3": target,
        "source": "scs_unit_hydrograph（基準 第九點、附表五）",
    }


def convolve(excess: Sequence[float], uh: Sequence[float]) -> list[float]:
    """Q(t_n) = Σ_j excess_j · U(t_n − t_j)。excess: mm/時段；uh: cms/mm。長度 len(e)+len(u)−1。"""
    out = [0.0] * (len(excess) + len(uh) - 1)
    for j, e in enumerate(excess):
        if e == 0.0:
            continue
        for k, u in enumerate(uh):
            out[j + k] += e * u
    return out


# ---------------------------------------------------------------- 外水位


def tailwater_series(
    inflow: Sequence[float], Hp: float, Hb: float, codes: dict[str, Any]
) -> list[float]:
    """Ht = (1/4)hp + (3/4)(Qt/Qp)hp + Hb（基準 第十點）。Qt、Qp 為滯洪池入流歷線。"""
    tw = codes["tailwater"]
    hp = Hp - Hb
    qp = max(inflow) if inflow else 0.0
    return [
        tw["coef_base"] * hp + tw["coef_ratio"] * ((q / qp) if qp > 0 else 0.0) * hp + Hb
        for q in inflow
    ]


# ---------------------------------------------------------------- H-A-V


def hav_from_layers(levels: Sequence[float], areas: Sequence[float]) -> dict[str, list[float]]:
    """分層 H-A-V（手冊 附 1.3）：Vn = ΔH(An + An+1 + √(An·An+1))/3，累積體積自最低層起算。"""
    if len(levels) != len(areas) or len(levels) < 2:
        raise ValueError("levels 與 areas 長度需相同且 ≥ 2")
    if any(b <= a for a, b in zip(levels, levels[1:], strict=False)):
        raise ValueError("levels 必須嚴格遞增")
    if any(a < 0 for a in areas):
        raise ValueError("areas 不得為負")
    vols = [0.0]
    for i in range(len(levels) - 1):
        a1, a2 = areas[i], areas[i + 1]
        vols.append(vols[-1] + (levels[i + 1] - levels[i]) * (a1 + a2 + math.sqrt(a1 * a2)) / 3.0)
    return {"levels": list(levels), "areas": list(areas), "volumes": vols}


def hav_volume(hav: dict[str, list[float]], H: float) -> float:
    """任意水位之體積：層內面積線性內插 + 截錐公式；高於頂層以頂面積垂直外插。"""
    L, A, V = hav["levels"], hav["areas"], hav["volumes"]
    if H <= L[0]:
        return 0.0
    if H >= L[-1]:
        return V[-1] + A[-1] * (H - L[-1])
    i = bisect.bisect_right(L, H) - 1
    a1 = A[i]
    a_h = a1 + (A[i + 1] - a1) * (H - L[i]) / (L[i + 1] - L[i])
    return V[i] + (H - L[i]) * (a1 + a_h + math.sqrt(a1 * a_h)) / 3.0


def hav_level(hav: dict[str, list[float]], vol: float) -> float:
    """體積 → 水位（二分法）。"""
    L = hav["levels"]
    lo, hi = L[0], L[-1]
    while hav_volume(hav, hi) < vol:
        hi += (L[-1] - L[0]) or 1.0
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if hav_volume(hav, mid) < vol:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


# ---------------------------------------------------------------- 出流工


def orifice_q(
    kind: str,
    ws: float,
    invert: float,
    codes: dict[str, Any],
    *,
    diameter: float | None = None,
    width: float | None = None,
    height: float | None = None,
    cd: float | None = None,
    tailwater: float | None = None,
    clog_ratio: float = 0.0,
) -> float:
    """孔口流量（手冊 附件一 附 1.2）。

    kind：vertical_circular / vertical_rect / horizontal_circular / horizontal_rect。
    ws 池水位、invert 孔口底（水平孔口為下緣）高程，m。tailwater 外水位（None = 自由流）。
    垂直孔口：滿口時 Q = n·C0·A0·√(2gZ)，Z = ws − max(孔口中心, 外水位)；未滿口時以
    Q_full(h=D)·(h/D)^1.5 接續（本 repo 慣例）。水平孔口：min(Cw·Pe·he^1.5, n·C0·A0·√(2gZ))。
    外水位 ≥ 池水位時 Q = 0（不倒灌）。
    """
    hy = codes["hydraulics"]
    g = hy["g"]
    c0 = hy["orifice_cd_default"] if cd is None else cd
    nfac = 1.0 - clog_ratio
    if kind.endswith("circular"):
        if diameter is None:
            raise ValueError(f"{kind} 缺 diameter")
        area, D, perim = math.pi * diameter**2 / 4.0, diameter, math.pi * diameter
    elif kind.endswith("rect"):
        if width is None or height is None:
            raise ValueError(f"{kind} 缺 width / height")
        area, D, perim = width * height, height, 2.0 * (width + height)
    else:
        raise ValueError(f"未知孔口型式 {kind}")
    h = ws - invert
    if h <= 0 or (tailwater is not None and tailwater >= ws):
        return 0.0
    if kind.startswith("vertical"):
        center = invert + D / 2.0
        if h >= D:
            ref = center if tailwater is None else max(center, tailwater)
            return nfac * c0 * area * math.sqrt(2.0 * g * (ws - ref))
        q_full = nfac * c0 * area * math.sqrt(2.0 * g * D / 2.0)
        return q_full * (h / D) ** 1.5
    if kind.startswith("horizontal"):
        qw = hy["horizontal_orifice_weir_cw"] * perim * nfac * h**1.5
        Z = h if tailwater is None or tailwater <= invert else ws - tailwater
        q0 = nfac * c0 * area * math.sqrt(2.0 * g * Z)
        return min(qw, q0)
    raise ValueError(f"未知孔口型式 {kind}")


def weir_cd(
    H1: float, codes: dict[str, Any], *, crest_width: float | None = None,
    weir_height: float | None = None,
) -> dict[str, Any]:
    """堰流量係數 Cd（手冊 式 1-9～1-12）。crest_width = BW（順流向堰頂寬），weir_height = P1。"""
    if H1 <= 0:
        return {"cd": 0.0, "type": "dry", "source": "-"}
    if crest_width is not None and crest_width > 0:
        r = H1 / crest_width
        if r <= 0.1:
            return {"cd": 0.561 * r**0.022, "type": "long", "source": "hydraulics.weir_cd.long"}
        if r <= 0.35:
            return {"cd": 0.028 * r + 0.521, "type": "broad_crested",
                    "source": "hydraulics.weir_cd.broad_crested"}
        if r <= 1.5:
            return {"cd": 0.120 * r + 0.492, "type": "narrow", "source": "hydraulics.weir_cd.narrow"}
    if weir_height is not None and weir_height > 0:
        return {"cd": 0.08 * H1 / weir_height + 0.611, "type": "sharp",
                "source": "hydraulics.weir_cd.sharp"}
    if crest_width is not None and crest_width > 0:
        # H1/BW > 1.5 且無 P1：外插窄堰式（本 repo 慣例，結果標記 extrapolated）
        r = H1 / crest_width
        return {"cd": 0.120 * r + 0.492, "type": "narrow_extrapolated",
                "source": "hydraulics.weir_cd.narrow（外插）"}
    raise ValueError("堰需給 cd，或 crest_width（BW）/ weir_height（P1）以依手冊式 1-9～1-12 計算")


def weir_q(length: float, H1: float, cd: float, codes: dict[str, Any]) -> float:
    """Q = Cd·(2/3)·√(2g)·L·H1^1.5（手冊 式 1-8，自由流）。"""
    if H1 <= 0:
        return 0.0
    return cd * (2.0 / 3.0) * math.sqrt(2.0 * codes["hydraulics"]["g"]) * length * H1**1.5


# ---------------------------------------------------------------- 滯洪演算


def routing_modified_puls(
    inflow: Sequence[float],
    hav_curve: dict[str, list[float]],
    outlet_fn: Callable[..., float],
    dt: float,
    *,
    tailwater: Sequence[float] | None = None,
    initial_level: float | None = None,
    substeps: int = 1,
) -> dict[str, Any]:
    """修正 Puls（storage-indication）滯洪演算。

    inflow：等間距入流（m³/s），間距 dt（秒）。outlet_fn(H) → Q；給 tailwater（與 inflow 同長）時
    呼叫 outlet_fn(H, tw)。substeps：每個 dt 細分的子步數（入流與外水位線性內插）。
    回傳細網格序列與體積收支（入流量、出流量、期初 / 期末蓄量、殘差）。
    """
    if substeps < 1:
        raise ValueError("substeps ≥ 1")
    if tailwater is not None and len(tailwater) != len(inflow):
        raise ValueError("tailwater 長度須與 inflow 相同")
    h = dt / substeps
    I = _refine(list(inflow), substeps)
    TW = _refine(list(tailwater), substeps) if tailwater is not None else None

    def O(H: float, k: int) -> float:
        return outlet_fn(H) if TW is None else outlet_fn(H, TW[k])

    H0 = hav_curve["levels"][0] if initial_level is None else initial_level
    Hs, Qs, Vs = [H0], [O(H0, 0)], [hav_volume(hav_curve, H0)]
    vin = vout = 0.0
    lo_bound = hav_curve["levels"][0]
    span = hav_curve["levels"][-1] - lo_bound
    for k in range(len(I) - 1):
        rhs = Vs[-1] - Qs[-1] * h / 2.0 + (I[k] + I[k + 1]) * h / 2.0

        def f(Hx: float, k1: int = k + 1) -> float:
            return hav_volume(hav_curve, Hx) + O(Hx, k1) * h / 2.0

        lo, hi = lo_bound, max(Hs[-1], lo_bound) + span
        while f(hi) < rhs:
            hi += span
        if f(lo) >= rhs:
            H2 = lo
        else:
            for _ in range(80):
                mid = 0.5 * (lo + hi)
                if f(mid) < rhs:
                    lo = mid
                else:
                    hi = mid
            H2 = 0.5 * (lo + hi)
        Q2 = O(H2, k + 1)
        vin += (I[k] + I[k + 1]) * h / 2.0
        vout += (Qs[-1] + Q2) * h / 2.0
        Hs.append(H2)
        Qs.append(Q2)
        Vs.append(hav_volume(hav_curve, H2))
    resid = vin - vout - (Vs[-1] - Vs[0])
    imax = max(range(len(Qs)), key=Qs.__getitem__)
    smax = max(range(len(Hs)), key=Hs.__getitem__)
    return {
        "dt_s": h,
        "inflow_cms": I,
        "outflow_cms": Qs,
        "level_m": Hs,
        "storage_m3": Vs,
        "tailwater_m": TW,
        "peak_outflow_cms": Qs[imax],
        "peak_outflow_time_s": imax * h,
        "max_level_m": Hs[smax],
        "max_level_time_s": smax * h,
        "max_storage_m3": max(Vs) - Vs[0],
        "inflow_volume_m3": vin,
        "outflow_volume_m3": vout,
        "storage_start_m3": Vs[0],
        "storage_end_m3": Vs[-1],
        "balance_residual_m3": resid,
        "balance_rel": resid / vin if vin > 0 else 0.0,
    }


# ---------------------------------------------------------------- 檢核門檻


def simple_check(area_ha: float, volume_m3: float, q10_out: float,
                 codes: dict[str, Any] | None = None) -> dict[str, Any]:
    """簡易檢核（基準 第十四點）：滯洪體積 ≥ 520 m³/ha × A、Q10 出流 ≤ 0.16 cms/ha × A。"""
    codes = codes or load_codes()
    sc = codes["simple_check"]
    v_req = sc["volume_m3_per_ha"] * area_ha
    q_max = sc["q10_cms_per_ha"] * area_ha
    return {
        "area_ha": area_ha,
        "eligible_by_area": area_ha <= sc["max_area_ha"],
        "volume_required_m3": _v(v_req, "m3", "simple_check.volume_m3_per_ha"),
        "volume_provided_m3": volume_m3,
        "volume_ok": volume_m3 >= v_req - 1e-9,
        "q10_max_cms": _v(q_max, "m3/s", "simple_check.q10_cms_per_ha"),
        "q10_out_cms": q10_out,
        "q10_ok": q10_out <= q_max + 1e-12,
        "conditions": list(sc["conditions"]),
        "source": "simple_check（基準 第十四點）",
    }


def applicability(
    area_ha: float,
    in_urban_plan: bool,
    is_new_building: bool,
    *,
    gravity_drainage: bool | None = None,
    outfall_q10_capacity_ok: bool | None = None,
    codes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """依 114-09-03 修正辦法第 2、2-1 條與基準第十四點，回傳應走的路徑（引用 yaml key）。

    route：
    - "outflow_plan"：應提出流管制計畫書（第 2 條 ≥ 1 ha，或第 2-1 條且 ≥ 1 ha）
    - "outflow_plan_or_exempt_filing"：第 2-1 條 0.2–1 ha，可提計畫書，或以流出抑制 / 675 m³/ha
      貯留措施於建照前備查而免提
    - "not_required"：未達門檻（其他法規如建技 4-3、地方逕流量標準仍可能適用，本函式不判定）
    開發樣態是否屬第 2 條所列由工程師判定，本函式不判定。
    """
    codes = codes or load_codes()
    ap = codes["applicability"]
    sc = codes["simple_check"]
    urban_rule = in_urban_plan and is_new_building
    reasons: list[str] = []
    if area_ha >= ap["general_threshold_ha"]:
        route = "outflow_plan"
        reasons.append(f"面積 {area_ha} ha ≥ {ap['general_threshold_ha']} ha（applicability.general_threshold_ha，第 2 條）")
    elif urban_rule and area_ha >= ap["urban_new_building_threshold_ha"]:
        route = "outflow_plan_or_exempt_filing"
        reasons.append(
            f"都市計畫區新建 {area_ha} ha ≥ {ap['urban_new_building_threshold_ha']} ha"
            f"（applicability.urban_new_building_threshold_ha，第 2-1 條第 1 項），"
            f"且 < {ap['urban_exempt_below_ha']} ha 得依第 2-1 條第 2 項備查免提"
        )
    else:
        route = "not_required"
        reasons.append("未達第 2 條或第 2-1 條門檻")
    exempt = None
    if route == "outflow_plan_or_exempt_filing":
        exempt = {
            "options": dict(ap["exempt_options"]),
            "retention_required_m3": _v(ap["retention_m3_per_ha"] * area_ha, "m3",
                                        "applicability.retention_m3_per_ha"),
        }
    simple_ok = None
    if route != "not_required":
        conds = [area_ha <= sc["max_area_ha"], gravity_drainage, outfall_q10_capacity_ok]
        simple_ok = None if any(c is None for c in conds) else all(conds)
    return {
        "route": route,
        "reasons": reasons,
        "exempt_filing": exempt,
        "simple_check_eligible": simple_ok,
        "simple_check_conditions": {
            "area_le_max": _v(area_ha <= sc["max_area_ha"], "-", "simple_check.max_area_ha"),
            "gravity_drainage": gravity_drainage,
            "outfall_q10_capacity_ok": outfall_q10_capacity_ok,
        },
        "source": "applicability（114-09-03 修正辦法 第 2、2-1 條）；simple_check（基準 第十四點）",
        "note": "開發樣態、例外免辦（第 2 條第 3～5 項）由工程師判定",
    }


# ---------------------------------------------------------------- 管線


def _interp(x: float, xs: Sequence[float], ys: Sequence[float]) -> float:
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1] if x == xs[-1] else 0.0
    i = bisect.bisect_right(xs, x) - 1
    return ys[i] + (ys[i + 1] - ys[i]) * (x - xs[i]) / (xs[i + 1] - xs[i])


def _refine(series: list[float], m: int) -> list[float]:
    if m == 1:
        return list(series)
    out: list[float] = []
    for a, b in zip(series, series[1:], strict=False):
        out.extend(a + (b - a) * j / m for j in range(m))
    out.append(series[-1])
    return out


def _catchment(spec_c: Any, codes: dict[str, Any], *, pre: bool) -> dict[str, Any]:
    subs = []
    for s in spec_c.subareas:
        if s.cn is not None:
            cn, src = float(s.cn), "spec.cn"
        else:
            cn = lookup_cn(s.scs_class, s.soil_group, codes)
            src = f"cn_table.classes.{s.scs_class}.{s.soil_group}"
        subs.append({"name": s.name, "area_ha": s.area_ha, "cn": cn, "cn_source": src})
    area = sum(s["area_ha"] for s in subs)
    cn_w = composite_cn([(s["area_ha"], s["cn"]) for s in subs])
    cap = codes["scs_cn"]["pre_dev_cn_max"]
    capped = pre and cn_w > cap
    cn_used = min(cn_w, cap) if pre else cn_w
    if spec_c.tc_min is not None:
        tc_in, tc_parts = spec_c.tc_min, {"tc_min": spec_c.tc_min, "source": "spec.tc_min"}
    else:
        fp = spec_c.flow_path
        if fp.inlet_time_min is not None:
            t1 = fp.inlet_time_min
            t1_src = "spec.flow_path.inlet_time_min（time_of_concentration.inlet_time_min 範圍）"
        else:
            t1 = 60.0 * overland_inlet_time_hr(fp.overland_length_m, fp.overland_slope_pct, cn_used)
            t1_src = "time_of_concentration.overland_formula"
        t2 = 0.0
        reaches = []
        for r in fp.channels:
            V = r.velocity_m_s if r.velocity_m_s is not None else manning_velocity(r.n, r.R_m, r.S)
            t = r.length_m / V / 60.0
            reaches.append({"length_m": r.length_m, "V_m_s": V, "t_min": t})
            t2 += t
        tc_in = t1 + t2
        tc_parts = {"T1_min": t1, "T1_source": t1_src, "T2_min": t2, "reaches": reaches,
                    "T2_source": "time_of_concentration.channel_formula"}
    tc = apply_tc_floor(tc_in, codes)
    return {
        "area_ha": area,
        "area_km2": area * KM2_PER_HA,
        "subareas": subs,
        "cn_weighted": cn_w,
        "cn_used": _v(cn_used, "-", "scs_cn.pre_dev_cn_max" if capped else "Σ(CN·A)/ΣA"),
        "cn_capped": capped,
        "tc": {**tc, **tc_parts},
    }


def _outlet_fn(spec: OutflowControlSpec, codes: dict[str, Any]) -> Callable[..., float]:
    o = spec.outlet

    def fn(H: float, tw: float | None = None) -> float:
        q = 0.0
        for orf in o.orifices:
            q += orifice_q(
                orf.type, H, orf.invert_m, codes,
                diameter=orf.diameter_m, width=orf.width_m, height=orf.height_m,
                cd=orf.cd, tailwater=tw, clog_ratio=orf.clog_ratio,
            )
        for w in o.weirs:
            H1 = H - w.crest_m
            if H1 <= 0 or (tw is not None and tw >= H):
                continue
            cd = w.cd if w.cd is not None else weir_cd(
                H1, codes, crest_width=w.crest_width_m, weir_height=w.weir_height_m)["cd"]
            q += weir_q(w.length_m, H1, cd, codes)
        return q

    return fn


def _sample(series: Sequence[float], m: int) -> list[float]:
    return [series[i] for i in range(0, len(series), m)]


def compute(spec: OutflowControlSpec) -> dict[str, Any]:
    """spec → OutflowResult dict。只計算，不判定（判定在 verify.outflow_check）。"""
    codes = load_codes(spec.codes_path)
    ds = codes["design_storm"]
    dt_min = int(ds["dt_min"])
    dur = int(ds["duration_min"])
    dt_hr = dt_min / 60.0
    ia = codes["scs_cn"]["initial_abstraction_ratio"]
    det = codes["detention"]
    design_T = int(det["design_return_period_yr"])

    rps = spec.return_periods or list(ds["return_periods_yr"])
    rps_src = "spec.return_periods" if spec.return_periods else "design_storm.return_periods_yr"
    if design_T not in rps:
        rps = sorted({*rps, design_T})

    site = spec.site
    app = applicability(
        site.area_ha, site.in_urban_plan, site.is_new_building,
        gravity_drainage=site.gravity_drainage,
        outfall_q10_capacity_ok=site.outfall_q10_capacity_ok, codes=codes,
    )
    pre = _catchment(spec.pre, codes, pre=True)
    post = _catchment(spec.post, codes, pre=False)
    uh_pre = scs_unit_hydrograph(pre["area_km2"], pre["tc"]["used_min"] / 60.0, dt_hr, codes)
    uh_post = scs_unit_hydrograph(post["area_km2"], post["tc"]["used_min"] / 60.0, dt_hr, codes)

    det_spec = spec.detention
    hav = hav_from_layers([lv.elev_m for lv in det_spec.layers], [lv.area_m2 for lv in det_spec.layers])
    H0 = det_spec.initial_elev_m if det_spec.initial_elev_m is not None else hav["levels"][0]
    outlet = _outlet_fn(spec, codes)
    m = spec.routing.substeps
    tail_steps = int(round(spec.routing.tail_hr * 60 / dt_min))

    events: dict[str, Any] = {}
    for T in rps:
        hp = spec.horner.params.get(T)
        if hp is None:
            raise KeyError(f"spec.horner.params 缺 {T} 年重現期係數（由工程師提供，不得自行補值）")
        storm = design_storm_alternating_block(hp.model_dump(), dur, dt_min)
        ev: dict[str, Any] = {
            "T_yr": T,
            "horner": {**hp.model_dump(), "source": f"spec.horner.params.{T}"},
            "R24_mm": _v(storm["R24_mm"], "mm", f"design_storm.horner_form + spec.horner.params.{T}"),
            "rain_mm": storm["depth_mm"],
        }
        for label, cat, uh in (("pre", pre, uh_pre), ("post", post, uh_post)):
            ro = scs_runoff(storm["depth_mm"], cat["cn_used"]["value"], ia)
            q = convolve(ro["excess_mm"], uh["ordinates_cms"])
            ipk = max(range(len(q)), key=q.__getitem__)
            ev[label] = {
                "excess_mm": ro["excess_mm"],
                "Re_mm": ro["Re_mm"],
                "hydrograph_cms": q,
                "peak_cms": _v(q[ipk], "m3/s", "convolve(scs_runoff, scs_unit_hydrograph)"),
                "peak_time_min": ipk * dt_min,
                "volume_m3": sum(q) * dt_min * 60.0,
            }
        inflow = ev["post"]["hydrograph_cms"] + [0.0] * tail_steps
        tw_series = None
        tw_info: dict[str, Any] = {"mode": spec.tailwater.mode}
        if spec.tailwater.mode == "fixed":
            tw_series = [spec.tailwater.elev_m] * len(inflow)
            tw_info["source"] = "spec.tailwater.elev_m"
        elif spec.tailwater.mode == "hydrograph":
            Hp = spec.tailwater.Hp_m[T]
            tw_series = tailwater_series(inflow, Hp, spec.tailwater.Hb_m, codes)
            tw_info.update({"Hp_m": Hp, "Hb_m": spec.tailwater.Hb_m, "source": "tailwater.formula"})
        r = routing_modified_puls(
            inflow, hav, outlet, dt_min * 60.0, tailwater=tw_series, initial_level=H0, substeps=m
        )
        weir_submerged = bool(
            tw_series is not None and spec.outlet.weirs
            and any(t > min(w.crest_m for w in spec.outlet.weirs) for t in r["tailwater_m"])
        )
        ev["routing"] = {
            "inflow_cms": _sample(r["inflow_cms"], m),
            "outflow_cms": _sample(r["outflow_cms"], m),
            "level_m": _sample(r["level_m"], m),
            "tailwater_m": _sample(r["tailwater_m"], m) if r["tailwater_m"] else None,
            "peak_inflow_cms": max(inflow),
            "peak_outflow_cms": _v(r["peak_outflow_cms"], "m3/s", "routing_modified_puls"),
            "peak_outflow_time_min": r["peak_outflow_time_s"] / 60.0,
            "max_level_m": r["max_level_m"],
            "max_storage_m3": r["max_storage_m3"],
            "inflow_volume_m3": r["inflow_volume_m3"],
            "outflow_volume_m3": r["outflow_volume_m3"],
            "storage_start_m3": r["storage_start_m3"],
            "storage_end_m3": r["storage_end_m3"],
            "balance_residual_m3": r["balance_residual_m3"],
            "balance_rel": r["balance_rel"],
            "substeps": m,
            "weir_submerged": weir_submerged,
        }
        ev["tailwater"] = tw_info
        events[str(T)] = ev

    peak_checks = []
    for T in codes["peak_criterion"]["return_periods_yr"]:
        if str(T) not in events:
            continue
        e = events[str(T)]
        pre_q = e["pre"]["peak_cms"]["value"]
        post_q = e["routing"]["peak_outflow_cms"]["value"]
        peak_checks.append({"T_yr": T, "pre_peak_cms": pre_q, "post_out_peak_cms": post_q,
                            "ok": post_q <= pre_q + 1e-12, "source": "peak_criterion.rule"})

    # 滯洪體積
    if spec.safety_factor is not None:
        sf = _v(spec.safety_factor, "-", "spec.safety_factor")
    else:
        sf = _v(det["safety_factor_min"], "-", "detention.safety_factor_min")
    v0 = hav_volume(hav, H0)
    req = events[str(design_T)]["routing"]["max_storage_m3"]
    top = hav["levels"][-1]
    fb = det_spec.freeboard_m
    design_top = top - (fb or 0.0)
    provided = hav_volume(hav, design_top) - v0
    q10_out = events[str(design_T)]["routing"]["peak_outflow_cms"]["value"]

    return {
        "codes_path": codes["_path"],
        "codes_verified": bool(codes["verified"]),
        "codes_note": str(codes.get("note", "")),
        "site": {
            "area_ha": site.area_ha,
            "area_m2": site.area_ha * M2_PER_HA,
            "area_ping": site.area_ha * M2_PER_HA * PING_PER_M2,
            "in_urban_plan": site.in_urban_plan,
            "is_new_building": site.is_new_building,
        },
        "applicability": app,
        "horner": {"station": spec.horner.station, "source_note": spec.horner.source_note},
        "return_periods": _v(rps, "yr", rps_src),
        "dt_min": _v(dt_min, "min", "design_storm.dt_min"),
        "duration_min": _v(dur, "min", "design_storm.duration_min"),
        "catchments": {"pre": pre, "post": post},
        "unit_hydrographs": {"pre": uh_pre, "post": uh_post},
        "hav": hav,
        "events": events,
        "peak_checks": peak_checks,
        "detention": {
            "design_T_yr": _v(design_T, "yr", "detention.design_return_period_yr"),
            "initial_elev_m": H0,
            "required_m3": _v(req, "m3", f"events.{design_T}.routing.max_storage_m3"),
            "safety_factor": sf,
            "safety_factor_min": _v(det["safety_factor_min"], "-", "detention.safety_factor_min"),
            "required_with_sf_m3": req * sf["value"],
            "top_elev_m": top,
            "freeboard_m": _v(fb, "m", "spec.detention.freeboard_m") if fb is not None else None,
            "design_top_elev_m": design_top,
            "provided_m3": _v(provided, "m3", "hav_volume(design_top) − hav_volume(initial)"),
            "provided_ratio": provided / req if req > 0 else math.inf,
            "max_level_m": events[str(design_T)]["routing"]["max_level_m"],
        },
        "simple_check": simple_check(site.area_ha, provided, q10_out, codes),
    }


# ---------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="出流管制計畫書水文水理計算（result.json；可選附圖 / SWMM inp）")
    ap.add_argument("spec")
    ap.add_argument("-o", "--out-dir", required=True)
    ap.add_argument("--charts", action="store_true", help="產出 附圖 16 / 18 / 19（PNG + PDF）")
    ap.add_argument("--swmm", action="store_true", help="輸出 SWMM 5 .inp（只寫檔，不執行）")
    args = ap.parse_args(argv)

    from draftagent.schema.outflow import OutflowControlSpec

    spec = OutflowControlSpec.model_validate_json(Path(args.spec).read_text(encoding="utf-8"))
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    res = compute(spec)
    (out / "result.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"result: {out / 'result.json'}")
    if args.charts:
        from draftagent.render.charts import render_all

        for name, p in render_all(res, out).items():
            print(f"{name}: {p}")
    if args.swmm:
        from draftagent.hydro.swmm_export import write_inp

        design_T = int(res["detention"]["design_T_yr"]["value"])
        p = write_inp(spec, out / f"outflow_T{design_T}.inp", T=design_T, result=res)
        print(f"swmm inp: {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
