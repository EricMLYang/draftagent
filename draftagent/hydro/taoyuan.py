"""桃園建築基地雨水流出抑制設施：確定性水文 / 水理計算。

純函式、確定性，不得呼叫 LLM。所有法規 / 手冊數值從
`draftagent/hydro/codes/taoyuan_runoff.yaml` 讀，程式內不寫任何門檻或係數；
結果中每個數值附 `source`（yaml key 路徑），方便人工追溯。

公式
----
- Horner 降雨強度（手冊 表 7-3，yaml `horner.stations.<站>.T<年>`）::

      I = a / (t + b)^c          I: mm/hr，t: 集流時間 min

- 合理化公式::

      Q = C · I · A / 360        Q: m³/s，I: mm/hr，A: ha

- 加權逕流係數 C = Σ(Cᵢ·Aᵢ) / ΣAᵢ；Cᵢ = c_override 或 yaml 中值（`runoff_coefficients`）
- 孔口 / 堰（手冊 7.7，係數 yaml `orifice_weir.*`）::

      矩形孔口  Q = k_r · L · B · (y − L/2)^0.5      k_r = rect_orifice_coeff
      圓形孔口  Q = k_c · L² · (y − L/2)^0.5         k_c = circ_orifice_coeff
      矩形堰    Q = k_w · B · y^1.5                  k_w = rect_weir_coeff

  L：孔口高（圓形為孔徑），B：寬，y：自孔口底（堰為堰頂）起算之水深，皆 m。
  圓形孔口另有「2.0862 × W × L」寫法，疑點見 yaml `orifice_weir.note`。

- 曼寧公式 V = (1/n) · R^(2/3) · S^(1/2)，Q = A · V；n 讀 yaml `manning_n`
  圓管部分滿流：θ = 2·acos(1 − 2y/D)，A = D²(θ − sinθ)/8，P = Dθ/2
  矩形 / U 溝（明渠）：A = b·y，P = b + 2y

貯集量與排放量
--------------
- 必要貯集量 = max(As × runoff_standard.min_storage_m3_per_m2,
                   As × building_code_4_3.min_storage_m3_per_m2)，兩者都回報並標明採用者
- 容許排放量 = As × runoff_standard.max_release_cms_per_m2；設計排放量須在
  design_release_ratio 範圍內
- As（計算用基地面積）：無既有建物時 = site_area_m2。
  有既有建物（增建 / 改建）時 As = min(site_area_m2, 新增建築面積 / 法定建蔽率)。
  規則來源：依任務規格（桃園逕流量標準 / 建技規則 4-3 增改建換算原則）實作；
  研究彙整 docs/research/2026-10-17-taoyuan-drainage.md 未收錄該條文原文，**待人工核對**，
  verifier 遇到 existing_building 時一律 WARNING。
- 排放量：release_mode=orifice → 孔口公式；release_mode=pump → spec.pump_q_cms（抽水機設計抽水量），
  一樣以容許排放量 85–100% 檢核（「抽水排放量亦不得超過容許排放量」原文研究文件未收錄，verifier WARNING）。
- 提供貯集量 = (Σ cellᵢ.length × width − outlet_well_area) × effective_depth。
  出流井面積扣除為本 repo 的保守處理慣例，非法規數值。
"""

from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from draftagent.schema.drainage import Pipe, TaoyuanSiteDrainageSpec

PING_PER_M2 = 0.3025
M2_PER_HA = 10_000.0
DEFAULT_CODES_PATH = Path(__file__).resolve().parent / "codes" / "taoyuan_runoff.yaml"

_SOURCED_GROUPS = (
    "runoff_standard",
    "building_code_4_3",
    "horner",
    "runoff_coefficients",
    "manning_n",
    "design_criteria",
    "orifice_weir",
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
        if not grp.get("source") or not grp.get("as_of"):
            raise ValueError(f"{path}: {g} 的 source / as_of 不得為空")
        if not grp.get("url") and not grp.get("url_note"):
            raise ValueError(f"{path}: {g} 缺 url 時必須有 url_note 說明")
    raw["_path"] = path
    return raw


def load_codes(path: str | Path | None = None) -> dict[str, Any]:
    """讀取並檢查 yaml：每組數值需有 source / as_of；缺 url 須有 url_note。"""
    p = Path(path) if path is not None else DEFAULT_CODES_PATH
    p = p.resolve()
    return _load_cached(str(p), p.stat().st_mtime)


def _v(value: float, unit: str, source: str) -> dict[str, Any]:
    return {"value": value, "unit": unit, "source": source}


# ---------------------------------------------------------------- 雨量


def station_for_district(district: str, codes: dict[str, Any]) -> str:
    """依 yaml `horner.stations.<站>.districts` 決定雨量站。找不到拋 KeyError。"""
    hits = [s for s, v in codes["horner"]["stations"].items() if district in (v.get("districts") or [])]
    if not hits:
        raise KeyError(f"行政區 {district!r} 在 yaml 無對應雨量站")
    if len(hits) > 1:
        raise ValueError(f"行政區 {district!r} 對應到多個雨量站 {hits}，yaml 有誤")
    return hits[0]


def horner_params(station: str, T: int, codes: dict[str, Any]) -> dict[str, float]:
    st = codes["horner"]["stations"]
    if station not in st:
        raise KeyError(f"雨量站 {station!r} 不在 yaml")
    key = f"T{int(T)}"
    if key not in st[station]:
        raise KeyError(f"雨量站 {station} 無 {T} 年重現期係數（yaml 缺），不得自行補值")
    return st[station][key]


def horner_intensity(station: str, T: int, t: float, codes: dict[str, Any] | None = None) -> float:
    """I = a / (t + b)^c，mm/hr。t: min。"""
    codes = codes or load_codes()
    p = horner_params(station, T, codes)
    return p["a"] / (t + p["b"]) ** p["c"]


def rational_q(C: float, I: float, A_ha: float) -> float:
    """合理化公式 Q = C·I·A/360（m³/s；I mm/hr；A ha）。"""
    return C * I * A_ha / 360.0


# ---------------------------------------------------------------- 逕流係數


def runoff_c(landuse: str, c_override: float | None, codes: dict[str, Any]) -> float:
    cls = codes["runoff_coefficients"]["classes"][landuse]
    if c_override is None:
        return float(cls["median"])
    if not (cls["min"] <= c_override <= cls["max"]):
        raise ValueError(f"c_override {c_override} 超出 {landuse} 範圍 [{cls['min']}, {cls['max']}]")
    return float(c_override)


def composite_c(subareas: list[dict[str, Any]]) -> float:
    """加權 C：subareas = [{area_m2, c}, …]。"""
    total = sum(s["area_m2"] for s in subareas)
    return sum(s["c"] * s["area_m2"] for s in subareas) / total


# ---------------------------------------------------------------- 貯集 / 排放


def effective_site_area(
    site_area_m2: float,
    new_building_area_m2: float | None = None,
    legal_coverage_ratio: float | None = None,
) -> dict[str, Any]:
    """計算用基地面積 As。規則見模組 docstring（增改建換算，待核）。"""
    if new_building_area_m2 is None:
        return {"As_m2": site_area_m2, "rule": "As = 基地面積（無既有建物）"}
    conv = new_building_area_m2 / legal_coverage_ratio
    return {
        "As_m2": min(site_area_m2, conv),
        "rule": "As = min(基地面積, 新增建築面積 / 法定建蔽率)（增改建換算，規則原文待核）",
        "converted_m2": conv,
    }


def required_storage(As_m2: float, codes: dict[str, Any]) -> dict[str, Any]:
    """必要貯集量 = max(桃園 0.051·As, 建技 4-3 0.045·As)；回報各自數值與採用者。"""
    k_ty = codes["runoff_standard"]["min_storage_m3_per_m2"]
    k_bc = codes["building_code_4_3"]["min_storage_m3_per_m2"]
    ty, bc = As_m2 * k_ty, As_m2 * k_bc
    adopted = "runoff_standard" if ty >= bc else "building_code_4_3"
    return {
        "taoyuan_m3": _v(ty, "m3", "runoff_standard.min_storage_m3_per_m2"),
        "building_code_m3": _v(bc, "m3", "building_code_4_3.min_storage_m3_per_m2"),
        "required_m3": _v(max(ty, bc), "m3", f"{adopted}.min_storage_m3_per_m2"),
        "adopted": adopted,
    }


def provided_storage(
    cells: list[tuple[float, float]], effective_depth_m: float, outlet_well_area_m2: float = 0.0
) -> dict[str, Any]:
    """提供貯集量 = (Σ L·W − 出流井面積) · 有效水深。"""
    plan = sum(length * width for length, width in cells)
    net = plan - outlet_well_area_m2
    if net <= 0:
        raise ValueError("貯集槽平面積扣除出流井後 ≤ 0")
    return {
        "cells_plan_area_m2": _v(plan, "m2", "spec.storage.cells"),
        "net_plan_area_m2": _v(net, "m2", "spec.storage"),
        "provided_m3": _v(net * effective_depth_m, "m3", "spec.storage"),
    }


def allowed_release(As_m2: float, codes: dict[str, Any]) -> dict[str, Any]:
    rs = codes["runoff_standard"]
    qmax = As_m2 * rs["max_release_cms_per_m2"]
    lo, hi = rs["design_release_ratio"]
    return {
        "allowed_cms": _v(qmax, "m3/s", "runoff_standard.max_release_cms_per_m2"),
        "design_min_cms": _v(qmax * lo, "m3/s", "runoff_standard.design_release_ratio[0]"),
        "design_max_cms": _v(qmax * hi, "m3/s", "runoff_standard.design_release_ratio[1]"),
    }


def orifice_q(
    kind: str,
    head_m: float,
    codes: dict[str, Any],
    *,
    height_m: float | None = None,
    width_m: float | None = None,
    diameter_m: float | None = None,
) -> float:
    """孔口流量（手冊 7.7）。公式見模組 docstring；y 須 ≥ L（孔口完全淹沒）否則拋錯。"""
    ow = codes["orifice_weir"]
    L = height_m if kind == "rect_orifice" else diameter_m
    if L is None:
        raise ValueError(f"{kind} 缺尺寸")
    if head_m < L:
        raise ValueError(f"設計水深 {head_m} m < 孔口高 {L} m，孔口未淹沒，公式不適用")
    h = (head_m - L / 2.0) ** 0.5
    if kind == "rect_orifice":
        if width_m is None:
            raise ValueError("rect_orifice 缺 width_m")
        return ow["rect_orifice_coeff"] * L * width_m * h
    if kind == "circ_orifice":
        return ow["circ_orifice_coeff"] * L * L * h
    raise ValueError(f"未知孔口型式 {kind}")


def weir_q(width_m: float, head_m: float, codes: dict[str, Any]) -> float:
    """矩形堰 Q = k_w · B · y^1.5（手冊 7.7）。"""
    return codes["orifice_weir"]["rect_weir_coeff"] * width_m * head_m**1.5


# ---------------------------------------------------------------- 曼寧


def _manning(area: float, perim: float, n: float, slope: float) -> dict[str, float]:
    R = area / perim if perim > 0 else 0.0
    V = (1.0 / n) * R ** (2.0 / 3.0) * slope**0.5
    return {"area_m2": area, "wetted_perimeter_m": perim, "R_m": R, "V_m_s": V, "Q_cms": area * V}


def manning_circular(D: float, n: float, slope: float, y: float | None = None) -> dict[str, float]:
    """圓管曼寧；y=None 為滿管。"""
    if y is None or y >= D:
        return _manning(math.pi * D * D / 4.0, math.pi * D, n, slope)
    if y <= 0:
        return {"area_m2": 0.0, "wetted_perimeter_m": 0.0, "R_m": 0.0, "V_m_s": 0.0, "Q_cms": 0.0}
    th = 2.0 * math.acos(1.0 - 2.0 * y / D)
    return _manning(D * D * (th - math.sin(th)) / 8.0, D * th / 2.0, n, slope)


def manning_rect(b: float, n: float, slope: float, y: float) -> dict[str, float]:
    """矩形 / U 溝明渠曼寧（無頂蓋）。"""
    return _manning(b * y, b + 2.0 * y, n, slope)


def _bisect(f, lo: float, hi: float, target: float, iters: int = 200) -> float:
    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        if f(mid) < target:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def normal_depth_circular(D: float, n: float, slope: float, Q: float) -> float | None:
    """圓管等流水深；Q 超過滿管流量回傳 None。在 [0, y_qmax] 內 Q 單調，以二分法求解。"""
    if Q > manning_circular(D, n, slope)["Q_cms"]:
        return None
    h = D * 1e-6

    def neg_slope(y: float) -> float:  # −dQ/dy；在 Q 最大處由負轉正
        return manning_circular(D, n, slope, y)["Q_cms"] - manning_circular(D, n, slope, y + h)["Q_cms"]

    y_qmax = _bisect(neg_slope, 0.5 * D, D - 2 * h, 0.0, iters=60)
    return _bisect(lambda y: manning_circular(D, n, slope, y)["Q_cms"], 0.0, y_qmax, Q)


def normal_depth_rect(b: float, depth: float, n: float, slope: float, Q: float) -> float | None:
    if Q > manning_rect(b, n, slope, depth)["Q_cms"]:
        return None
    return _bisect(lambda y: manning_rect(b, n, slope, y)["Q_cms"], 0.0, depth, Q)


# ---------------------------------------------------------------- 管渠檢查（回傳 (ok, 訊息)）


def check_velocity(v: float, codes: dict[str, Any]) -> tuple[bool, str]:
    lo, hi = codes["design_criteria"]["velocity_m_s"]
    ok = lo <= v <= hi
    return ok, f"流速 {v:.3f} m/s {'在' if ok else '不在'} [{lo}, {hi}] m/s（design_criteria.velocity_m_s）"


def check_freeboard(width_m: float, depth_m: float, y: float, codes: dict[str, Any]) -> tuple[bool | None, str]:
    """U 溝出水高 ≥ max(ratio × 水深, min)；寬度超出 yaml 規則適用範圍回傳 None。"""
    fb = codes["design_criteria"]["freeboard_u_ditch"]
    if width_m > fb["width_max_m"]:
        return None, f"渠寬 {width_m} m > {fb['width_max_m']} m，yaml 無出水高規則"
    need = max(fb["ratio_of_depth"] * y, fb["min_m"])
    have = depth_m - y
    ok = have >= need - 1e-12
    return ok, (
        f"出水高 {have:.3f} m {'≥' if ok else '<'} 需求 {need:.3f} m"
        "（design_criteria.freeboard_u_ditch）"
    )


def min_pipe_diameter_mm(area_m2: float, codes: dict[str, Any]) -> int | None:
    """依集水面積查最小管徑 mm；超過 yaml 規則上限回傳 None。"""
    for row in codes["design_criteria"]["min_pipe_diameter"]:
        if area_m2 <= row["area_m2_max"]:
            return int(row["diameter_min_mm"])
    return None


def pipe_n(pipe: Pipe, codes: dict[str, Any]) -> float:
    m = codes["manning_n"]["materials"][pipe.material]
    return float(pipe.n_override) if "n_range" in m else float(m["n"])


def pipe_hydraulics(
    pipe: Pipe,
    Q: float,
    codes: dict[str, Any],
    q_source: str = "rational(C_composite, I_collection, design_area)",
) -> dict[str, Any]:
    """單一管渠：滿管容量、等流水深、流速、出水高、最小管徑、材料與管徑相符性。"""
    n = pipe_n(pipe, codes)
    mat = codes["manning_n"]["materials"][pipe.material]
    out: dict[str, Any] = {
        "name": pipe.name,
        "shape": pipe.shape,
        "material": pipe.material,
        "n": _v(n, "-", f"manning_n.materials.{pipe.material}" + (" + n_override" if "n_range" in mat else "")),
        "slope": pipe.slope,
        "design_area_m2": pipe.design_area_m2,
        "Q_design_cms": _v(Q, "m3/s", q_source),
        "messages": [],
        "errors": [],
        "warnings": [],
    }
    if pipe.shape == "circular":
        D = float(pipe.diameter_m)
        full = manning_circular(D, n, pipe.slope)
        y = normal_depth_circular(D, n, pipe.slope, Q)
        out["capacity_cms"] = _v(full["Q_cms"], "m3/s", "manning 滿管")
        lo_d, hi_d = mat.get("diameter_min_m"), mat.get("diameter_max_m")
        if lo_d is not None and D < lo_d:
            out["errors"].append(f"材料 {pipe.material} 適用管徑 ≥ {lo_d} m，實際 {D} m")
        if hi_d is not None and D >= hi_d:
            out["errors"].append(f"材料 {pipe.material} 適用管徑 < {hi_d} m，實際 {D} m")
        dmin = min_pipe_diameter_mm(pipe.design_area_m2, codes)
        out["min_diameter_mm"] = dmin
        if dmin is None:
            out["warnings"].append(
                f"集水面積 {pipe.design_area_m2} m² 超出 yaml 最小管徑規則上限，未檢核最小管徑"
            )
        elif D * 1000.0 < dmin - 1e-9:
            out["errors"].append(
                f"管徑 {D * 1000:.0f} mm < 最小 {dmin} mm（design_criteria.min_pipe_diameter）"
            )
        else:
            out["messages"].append(f"管徑 {D * 1000:.0f} mm ≥ 最小 {dmin} mm")
        hyd = manning_circular(D, n, pipe.slope, y) if y is not None else None
    else:
        b, d = float(pipe.width_m), float(pipe.depth_m)
        full = manning_rect(b, n, pipe.slope, d)
        y = normal_depth_rect(b, d, n, pipe.slope, Q)
        out["capacity_cms"] = _v(full["Q_cms"], "m3/s", "manning 滿渠（水深 = 渠深）")
        hyd = manning_rect(b, n, pipe.slope, y) if y is not None else None
        if y is not None:
            ok, msg = check_freeboard(b, d, y, codes)
            out["freeboard_m"] = _v(d - y, "m", "design_criteria.freeboard_u_ditch")
            if ok is None:
                out["warnings"].append(msg)
            elif ok:
                out["messages"].append(msg)
            else:
                out["errors"].append(msg)
    if y is None:
        out["errors"].append(f"設計流量 {Q:.5f} m³/s > 通水容量 {full['Q_cms']:.5f} m³/s")
        out["normal_depth_m"] = None
        out["velocity_m_s"] = None
        return out
    out["normal_depth_m"] = _v(y, "m", "manning 等流水深")
    out["velocity_m_s"] = _v(hyd["V_m_s"], "m/s", "manning")
    ok, msg = check_velocity(hyd["V_m_s"], codes)
    (out["messages"] if ok else out["errors"]).append(msg)
    return out


# ---------------------------------------------------------------- 管線


def compute(spec: TaoyuanSiteDrainageSpec) -> dict[str, Any]:
    """spec → DrainageResult dict。只計算，不判定通過與否（判定在 verify.drainage_calc_check）。

    管渠設計逕流係數：pipe.design_c（排水網路上游集水分區面積加權 C，drainage-plan 由
    drainage/derive.derive_calc 寫入）有值 → Q 用它，結果另列 `C_design`；
    None（drainage-calc-ty 沒有網路拓樸）→ 維持原行為，用全基地加權 C（C_composite），不列 `C_design`。
    """
    codes = load_codes(spec.codes_path)
    dc = codes["design_criteria"]
    rp = dc["return_period_yr"]

    station = spec.station or station_for_district(spec.district, codes)
    station_src = "spec.station（覆寫）" if spec.station else f"horner.stations.{station}.districts"
    tc = spec.tc_min if spec.tc_min is not None else float(dc["tc_min_recommended"])
    tc_src = "spec.tc_min" if spec.tc_min is not None else "design_criteria.tc_min_recommended"

    subs = []
    for s in spec.subareas:
        c = runoff_c(s.landuse, s.c_override, codes)
        subs.append(
            {
                "name": s.name,
                "landuse": s.landuse,
                "landuse_name": codes["runoff_coefficients"]["classes"][s.landuse]["name"],
                "area_m2": s.area_m2,
                "area_ping": s.area_m2 * PING_PER_M2,
                "c": c,
                "c_source": "spec.c_override" if s.c_override is not None
                else f"runoff_coefficients.classes.{s.landuse}.median",
            }
        )
    sub_total = sum(s["area_m2"] for s in subs)
    C = composite_c(subs)

    eb = spec.existing_building
    as_info = effective_site_area(
        spec.site_area_m2,
        eb.new_building_area_m2 if eb else None,
        eb.legal_coverage_ratio if eb else None,
    )
    As = as_info["As_m2"]

    intensities = {}
    for label, T in (("storage", rp["storage"]), ("release", rp["release"]), ("collection", rp["collection"])):
        I = horner_intensity(station, T, tc, codes)
        intensities[label] = {
            "T_yr": T,
            "T_source": f"design_criteria.return_period_yr.{label}",
            "I_mm_hr": _v(I, "mm/hr", f"horner.stations.{station}.T{T}"),
            "Q_site_cms": _v(
                rational_q(C, I, sub_total / M2_PER_HA), "m3/s",
                f"rational(C_composite, horner.stations.{station}.T{T}, Σsubareas)",
            ),
        }

    req = required_storage(As, codes)
    prov = provided_storage(
        [(c.length_m, c.width_m) for c in spec.storage.cells],
        spec.storage.effective_depth_m,
        spec.storage.outlet_well_area_m2,
    )
    rel = allowed_release(As, codes)
    allowed = rel["allowed_cms"]["value"]
    if spec.release_mode == "pump":
        q_out = float(spec.pump_q_cms)
        outlet = {
            "type": "pump",
            "Q_cms": _v(q_out, "m3/s", "spec.pump_q_cms（抽水機設計抽水量）"),
            "ratio_to_allowed": q_out / allowed,
        }
    else:
        o = spec.outlet
        q_out = orifice_q(
            o.type, o.design_head_m, codes,
            height_m=o.height_m, width_m=o.width_m, diameter_m=o.diameter_m,
        )
        outlet = {
            "type": o.type,
            "Q_cms": _v(q_out, "m3/s", f"orifice_weir.{'rect' if o.type == 'rect_orifice' else 'circ'}_orifice_coeff"),
            "ratio_to_allowed": q_out / allowed,
            "design_head_m": o.design_head_m,
        }
    weir = None
    if spec.overflow_weir is not None:
        w = spec.overflow_weir
        weir = {
            "width_m": w.width_m,
            "head_m": w.head_m,
            "Q_cms": _v(weir_q(w.width_m, w.head_m, codes), "m3/s", "orifice_weir.rect_weir_coeff"),
        }

    I_col = intensities["collection"]["I_mm_hr"]["value"]
    pipes = []
    for p in spec.pipes:
        if p.design_c is None:
            pipes.append(pipe_hydraulics(p, rational_q(C, I_col, p.design_area_m2 / M2_PER_HA), codes))
            continue
        cp = float(p.design_c)
        out = pipe_hydraulics(
            p,
            rational_q(cp, I_col, p.design_area_m2 / M2_PER_HA),
            codes,
            q_source="rational(C_upstream, I_collection, design_area)",
        )
        out["C_design"] = _v(cp, "-", "spec.pipes[].design_c：Σ(C·A)/ΣA（上游集水分區）")
        pipes.append(out)

    return {
        "codes_path": codes["_path"],
        "codes_verified": bool(codes["verified"]),
        "codes_note": str(codes.get("note", "")),
        "district": spec.district,
        "station": _v(station, "-", station_src),
        "tc_min": _v(tc, "min", tc_src),
        "site_area_m2": spec.site_area_m2,
        "site_area_ping": spec.site_area_m2 * PING_PER_M2,
        "exempt_site_area_m2_max": _v(
            codes["runoff_standard"]["exempt_site_area_m2_max"], "m2",
            "runoff_standard.exempt_site_area_m2_max",
        ),
        "As": {**as_info, "As_ping": As * PING_PER_M2},
        "subareas": subs,
        "subarea_total_m2": sub_total,
        "C_composite": _v(C, "-", "Σ(C·A)/ΣA"),
        "rainfall": intensities,
        "storage_required": req,
        "storage_provided": {**prov, "type": spec.storage.type},
        "release": rel,
        "outlet": outlet,
        "overflow_weir": weir,
        "pipes": pipes,
    }
