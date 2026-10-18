"""出流管制計畫書水文水理 verifier（純函式，不得呼叫 LLM）。

    python -m draftagent.verify.outflow_check <spec.json> [--result result.json] [--json]

重算 spec 並檢查：
- 開發後排水出流 Qp(T) ≤ 開發前 Qp(T)，T = 2、5、10（基準 第十二點）
- 滯洪池最高水位 ≤ 池頂 − 出水高（出水高由 spec 給；yaml 無法定值 → WARNING）；溢頂一律 ERROR
- 提供滯洪體積 ≥ 演算需求體積 × 安全係數；安全係數 ≥ yaml 下限 1.2（第十三點）
- 簡易檢核（第十四點）：spec 選用時須符合適用條件且 520 m³/ha、0.16 cms/ha 皆通過
- 水量守恆：入流總量 − 出流總量 − (期末 − 期初蓄量) ≈ 0（容差 1%）
- 單位歷線面積守恆：Σ U·Δt = 1 mm × A（容差 1%）
- 所有判定一律用重算值；--result：result.json 完整 flatten 後逐鍵比對（數值相對 1e-6，缺鍵 / 多鍵皆 ERROR）
- spec.codes_path ≠ repo 預設 yaml → ERROR；重算一律讀預設 yaml（codes_lock）
- 開發前後集水面積差 > 0.5% → ERROR（spec.area_change_reason 有說明時降為 WARNING）
- build 產物（verify_artifacts）：SWMM .inp 解析 [STORAGE]/[CURVES]/[ORIFICES]/[WEIRS]/[XSECTIONS]/
  [SUBCATCHMENTS]/[INFLOWS] 與 spec / 重算比對，且須與 build_inp(重算) 逐字相同；
  附圖 16/18/19 PNG 存在、短邊 > 600 px、灰階標準差 > 門檻（非空白）；PDF 須為有頁面的 PDF
- yaml verified: false → WARNING
回覆只能說「通過本 repo 現有 verifier 的檢查項目」，不得宣稱符合法規。
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

from draftagent.hydro.outflow import compute, load_codes
from draftagent.schema.outflow import DEFAULT_CODES_PATH, OutflowControlSpec
from draftagent.verify.base import VerifyResult, emit
from draftagent.verify.codes_lock import assert_default_codes

NAME = "outflow_check"
BALANCE_TOL = 0.01
UH_TOL = 0.01
UH_RAW_WARN = 0.05
CMP_RTOL = 1e-6
AREA_TOL = 0.005
INP_ATOL = 1.5e-4  # .inp 以 4 位小數寫出
TS_ATOL = 1.5e-6  # 時間序列以 6 位小數寫出
FIGURES = ("fig16_hydrographs", "fig18_hav", "fig19_routing")
FIG_MIN_PX = 600
FIG_MIN_STD = 5.0  # 灰階標準差（0–255）；空白 / 單色圖 ≈ 0
PDF_MIN_BYTES = 2048


def _val(x: Any) -> Any:
    return x["value"] if isinstance(x, dict) and "value" in x else x


def _close(a: float, b: float, rtol: float = CMP_RTOL) -> bool:
    return math.isclose(float(a), float(b), rel_tol=rtol, abs_tol=1e-9)


def _flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(obj, dict):
        if not obj and prefix:
            out[prefix] = {}
        for k, v in obj.items():
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list):
        if not obj:
            out[prefix] = []
        for i, v in enumerate(obj):
            out.update(_flatten(v, f"{prefix}[{i}]"))
    else:
        out[prefix] = obj
    return out


def _same(g: Any, c: Any) -> bool:
    if isinstance(c, bool) or not isinstance(c, (int, float)):
        return type(g) is type(c) and g == c
    if not isinstance(g, (int, float)) or isinstance(g, bool):
        return False
    if math.isnan(c) or math.isnan(g):
        return math.isnan(c) and math.isnan(g)
    return math.isclose(float(g), float(c), rel_tol=CMP_RTOL, abs_tol=1e-12)


def _compare(res: VerifyResult, given: dict[str, Any], calc: dict[str, Any]) -> None:
    """result.json 與重算值完整 flatten 逐鍵比對（json 往返後再比，鍵型別一致）。"""
    a = _flatten(json.loads(json.dumps(calc, ensure_ascii=False)))
    b = _flatten(given)
    for k in ("codes_path",):  # 機器相關的絕對路徑；路徑鎖定另由 codes_lock 檢查
        a.pop(k, None)
        b.pop(k, None)
    bad = 0
    shown = 0
    for k, c in a.items():
        if k not in b:
            bad += 1
            if shown < 20:
                res.error(f"result.json 缺鍵 {k}")
                shown += 1
            continue
        if not _same(b[k], c):
            bad += 1
            if shown < 20:
                res.error(f"result.json {k} = {b[k]!r} 與重算 {c!r} 不符")
                shown += 1
    extra = sorted(set(b) - set(a))
    if extra:
        bad += len(extra)
        res.error(f"result.json 有重算沒有的鍵 {len(extra)} 個：{extra[:5]}{'…' if len(extra) > 5 else ''}")
    if bad > shown + (1 if extra else 0):
        res.error(f"result.json 共 {bad} 處與重算不符（只列前 {shown} 筆）")
    res.metrics["result_compared_items"] = len(a)
    res.metrics["result_mismatches"] = bad


def check(spec: OutflowControlSpec, result: dict[str, Any] | None = None) -> VerifyResult:
    return _check(spec, result)[0]


def _check(
    spec: OutflowControlSpec, result: dict[str, Any] | None
) -> tuple[VerifyResult, dict[str, Any], OutflowControlSpec]:
    res = VerifyResult(NAME)
    assert_default_codes(spec.codes_path, DEFAULT_CODES_PATH, res, "codes_path")
    spec = spec.model_copy(update={"codes_path": str(DEFAULT_CODES_PATH)})  # 一律讀 repo 預設 yaml
    codes = load_codes(DEFAULT_CODES_PATH)
    calc = compute(spec)
    r = calc  # 判定一律用重算值；result.json 只做逐鍵比對

    if not codes["verified"]:
        res.warn(f"法規數值 yaml verified: false（{codes['_path']}）：{codes.get('note', '')}")
    if "示範" in spec.horner.source_note or "非實際" in spec.horner.source_note:
        res.warn(f"Horner 係數為示範值（{spec.horner.station}），不得用於實案")

    # 適用路徑
    app = calc["applicability"]
    res.metrics["route"] = app["route"]
    if app["route"] == "not_required":
        res.warn("依面積 / 條件未達出流管制計畫書門檻（第 2、2-1 條）；是否仍須提送由工程師判定")
    elif app["route"] == "outflow_plan_or_exempt_filing":
        rq = app["exempt_filing"]["retention_required_m3"]["value"]
        res.warn(
            "第 2-1 條：0.2–1 ha 都市計畫區新建，得以流出抑制措施或貯留 ≥ "
            f"{rq:.1f} m³（applicability.retention_m3_per_ha）於建照前備查免提計畫書；路徑由工程師 / 使用者決定"
        )

    # 集水面積
    for k in ("pre", "post"):
        a = calc["catchments"][k]["area_ha"]
        if abs(a - spec.site.area_ha) > AREA_TOL * spec.site.area_ha:
            res.warn(f"{k} 集水分區面積和 {a} ha ≠ 基地 {spec.site.area_ha} ha（含區外集水或漏列，請確認）")
        if calc["catchments"][k]["tc"]["floored"]:
            res.warn(f"{k} Tc {calc['catchments'][k]['tc']['input_min']:.2f} min < 10，以 10 min 計"
                     "（time_of_concentration.tc_min_floor）")
    a_pre, a_post = calc["catchments"]["pre"]["area_ha"], calc["catchments"]["post"]["area_ha"]
    d_rel = abs(a_post - a_pre) / a_pre
    res.metrics["catchment_area_change_pct"] = round(100 * d_rel, 4)
    if d_rel > AREA_TOL:
        msg = (f"開發前集水面積 {a_pre} ha 與開發後 {a_post} ha 相差 {d_rel:.2%} > {AREA_TOL:.1%}"
               "（漏列分區或集水範圍改變）")
        if spec.area_change_reason:
            res.warn(f"{msg}；spec.area_change_reason：{spec.area_change_reason}（請工程師確認）")
        else:
            res.error(f"{msg}；若屬實須於 spec.area_change_reason 說明")
    if calc["catchments"]["pre"]["cn_capped"]:
        res.warn(f"開發前 CN {calc['catchments']['pre']['cn_weighted']:.1f} > 70，以 70 計（scs_cn.pre_dev_cn_max）")

    # 洪峰檢核
    for pc in r["peak_checks"]:
        T, pre_q, post_q = pc["T_yr"], pc["pre_peak_cms"], pc["post_out_peak_cms"]
        line = f"T{T}: 開發後出流 {post_q:.4f} vs 開發前 {pre_q:.4f} m³/s"
        res.metrics[f"Qp_T{T}"] = line
        if post_q > pre_q + 1e-12:
            res.error(f"{line}：開發後洪峰大於開發前（peak_criterion.rule）")
    have = {pc["T_yr"] for pc in r["peak_checks"]}
    for T in codes["peak_criterion"]["return_periods_yr"]:
        if T not in have:
            res.error(f"缺 {T} 年洪峰檢核（peak_criterion.return_periods_yr）")

    # 水位 / 出水高
    d = r["detention"]
    top = d["top_elev_m"]
    fb = _val(d.get("freeboard_m"))
    for T, ev in r["events"].items():
        hmax = ev["routing"]["max_level_m"]
        if hmax > top + 1e-9:
            res.error(f"T{T} 最高水位 {hmax:.3f} m 超過池頂 {top:.3f} m（溢頂）")
        elif fb is not None and hmax > top - fb + 1e-9:
            res.error(f"T{T} 最高水位 {hmax:.3f} m > 池頂 − 出水高 = {top - fb:.3f} m")
        if ev["routing"].get("weir_submerged"):
            res.warn(f"T{T} 外水位高於堰頂；手冊附圖 1-5 潛沒折減未收錄，堰流量未折減（偏不保守）")
    res.metrics["max_level_T10_m"] = round(d["max_level_m"], 4)
    if codes["detention"].get("freeboard_m") is None:
        res.warn("yaml 無滯洪池出水高法定值（detention.freeboard_note）；出水高僅依 spec 工程師給值檢核"
                 if fb is not None else "yaml 無出水高法定值且 spec 未給 freeboard_m：只檢核未溢頂")

    # 安全係數 / 體積
    sf = _val(d["safety_factor"])
    sf_min = codes["detention"]["safety_factor_min"]
    if sf < sf_min - 1e-12:
        res.error(f"安全係數 {sf} < {sf_min}（detention.safety_factor_min，基準 第十三點（二））")
    req, prov = _val(d["required_m3"]), _val(d["provided_m3"])
    need = req * sf
    res.metrics["detention"] = f"需求 {req:.1f} m³ × SF {sf} = {need:.1f} m³；提供 {prov:.1f} m³"
    if prov < need - 1e-9:
        res.error(f"提供滯洪體積 {prov:.1f} m³ < 需求 {req:.1f} × {sf} = {need:.1f} m³")

    # 簡易檢核
    sc = r["simple_check"]
    elig = app["simple_check_eligible"]
    sc_line = (
        f"體積 {sc['volume_provided_m3']:.1f} / {_val(sc['volume_required_m3']):.1f} m³、"
        f"Q10 {sc['q10_out_cms']:.4f} / {_val(sc['q10_max_cms']):.4f} m³/s"
    )
    res.metrics["simple_check"] = f"{'選用' if spec.site.use_simple_check else '未選用'}；適用={elig}；{sc_line}"
    if spec.site.use_simple_check:
        if elig is not True:
            res.error(f"選用簡易檢核但適用條件不成立或未填（{app['simple_check_conditions']}）")
        if not sc["volume_ok"]:
            res.error(f"簡易檢核滯洪體積不足（simple_check.volume_m3_per_ha）：{sc_line}")
        if not sc["q10_ok"]:
            res.error(f"簡易檢核 Q10 出流超過（simple_check.q10_cms_per_ha）：{sc_line}")
        res.warn("520 m³/ha 是否已含安全係數 1.2 原文未明（simple_check.note），待核")
    elif elig and not (sc["volume_ok"] and sc["q10_ok"]):
        res.warn(f"簡易檢核未選用；若改用將不通過：{sc_line}")

    # 水量守恆 / 單位歷線體積：重算值必檢；有 result.json 時其自身收支也必須守恆
    sources: list[tuple[str, dict[str, Any]]] = [("", calc)]
    if result is not None:
        sources.append(("result.json ", result))
    for tag, src in sources:
        try:
            _internal_checks(res, src, calc, tag)
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as e:
            res.error(f"{tag}結構不完整，無法做守恆檢核：{type(e).__name__}: {e}")

    if result is not None:
        _compare(res, result, calc)
    res.metrics["site"] = (f"{spec.site.area_ha} ha = {spec.site.area_ha * 1e4:.0f} m²"
                           f"（{spec.site.area_ha * 1e4 * 0.3025:.1f} 坪）")
    return res, calc, spec


def _internal_checks(res: VerifyResult, r: dict[str, Any], calc: dict[str, Any], tag: str) -> None:
    worst = 0.0
    for T, ev in r["events"].items():
        ro = ev["routing"]
        vin = ro["inflow_volume_m3"]
        resid = vin - ro["outflow_volume_m3"] - (ro["storage_end_m3"] - ro["storage_start_m3"])
        rel = abs(resid) / vin if vin > 0 else 0.0
        worst = max(worst, rel)
        if rel > BALANCE_TOL:
            res.error(f"{tag}T{T} 水量不守恆：入流 {vin:.2f} − 出流 {ro['outflow_volume_m3']:.2f} − Δ蓄量 "
                      f"{ro['storage_end_m3'] - ro['storage_start_m3']:.2f} = {resid:.2f} m³（{rel:.2%} > 1%）")
    if not tag:
        res.metrics["mass_balance_max_rel"] = f"{worst:.2e}"
    for k in ("pre", "post"):
        uh = r["unit_hydrographs"][k]
        vol = sum(uh["ordinates_cms"]) * uh["dt_hr"] * 3600.0
        target = 1000.0 * calc["catchments"][k]["area_km2"]
        rel = abs(vol - target) / target
        if not tag:
            res.metrics[f"uh_{k}"] = (f"Tp {uh['Tp_hr'] * 60:.2f} min、體積 {vol:.2f} / {target:.2f} m³、"
                                      f"校正前比值 {uh['volume_ratio_raw']:.4f}")
        if rel > UH_TOL:
            res.error(f"{tag}{k} 單位歷線體積 {vol:.2f} m³ ≠ 1 mm × A = {target:.2f} m³（{rel:.2%} > 1%）")
        if not tag and abs(uh["volume_ratio_raw"] - 1.0) > UH_RAW_WARN:
            res.warn(f"{k} 單位歷線離散化體積比 {uh['volume_ratio_raw']:.3f}，偏離 1 超過 5%，已比例校正")


def _num(s: str) -> float:
    return float(s)


def _near(a: float, b: float, atol: float = INP_ATOL) -> bool:
    return abs(float(a) - float(b)) <= atol + 1e-9 * abs(float(b))


def check_inp(spec: OutflowControlSpec, calc: dict[str, Any], path: Path, res: VerifyResult) -> None:
    """解析 SWMM .inp 並與 spec / 重算比對；並要求與 build_inp(重算) 逐字相同。"""
    from draftagent.hydro.swmm_export import build_inp, read_sections

    text = path.read_text(encoding="utf-8")
    try:
        sec = read_sections(text)
    except ValueError as e:
        res.error(f"{path.name} 無法解析：{e}")
        return
    codes = load_codes(DEFAULT_CODES_PATH)
    det = spec.detention
    bottom, top = det.layers[0].elev_m, det.layers[-1].elev_m
    n0 = len(res.errors)

    def err(msg: str) -> None:
        res.error(f"{path.name} {msg}")

    st = [r for r in sec.get("STORAGE", []) if r[0] == "ST1"]
    if len(st) != 1:
        err("[STORAGE] 必須恰有一個 ST1")
    else:
        s = st[0]
        h0 = (det.initial_elev_m - bottom) if det.initial_elev_m is not None else 0.0
        for i, want, lab in ((1, bottom, "池底高程"), (2, top - bottom, "池深"), (3, h0, "起始水深")):
            if not _near(_num(s[i]), want):
                err(f"[STORAGE] ST1 {lab} {s[i]} ≠ spec {want:.4f}")
    curve = [r for r in sec.get("CURVES", []) if r[0] == "SC_ST1"]
    pts = [(float(r[-2]), float(r[-1])) for r in curve]
    want_pts = [(lv.elev_m - bottom, lv.area_m2) for lv in det.layers]
    if len(pts) != len(want_pts) or not all(
        _near(a, c) and _near(b, d) for (a, b), (c, d) in zip(pts, want_pts, strict=False)
    ):
        err(f"[CURVES] SC_ST1 水深-面積 {pts} ≠ spec layers {want_pts}")

    xs = {r[0]: r for r in sec.get("XSECTIONS", [])}
    ors = sec.get("ORIFICES", [])
    if len(ors) != len(spec.outlet.orifices):
        err(f"[ORIFICES] {len(ors)} 個 ≠ spec {len(spec.outlet.orifices)} 個")
    for row, o in zip(ors, spec.outlet.orifices, strict=False):
        nm = row[0]
        typ = "SIDE" if o.type.startswith("vertical") else "BOTTOM"
        cd = (o.cd if o.cd is not None else codes["hydraulics"]["orifice_cd_default"]) * (1 - o.clog_ratio)
        if row[1:4] != ["ST1", "OF1", typ]:
            err(f"[ORIFICES] {nm} 連接 / 型式 {row[1:4]} ≠ ST1 OF1 {typ}（{o.name}）")
        if not _near(_num(row[4]), o.invert_m - bottom):
            err(f"[ORIFICES] {nm} offset {row[4]} ≠ spec {o.invert_m - bottom:.4f}（{o.name}）")
        if not _near(_num(row[5]), cd):
            err(f"[ORIFICES] {nm} Qcoeff {row[5]} ≠ {cd:.4f}（{o.name}）")
        x = xs.get(nm)
        if o.type.endswith("circular"):
            want = ("CIRCULAR", o.diameter_m, 0.0)
        else:
            want = ("RECT_CLOSED", o.height_m, o.width_m)
        if x is None or x[1] != want[0] or not (_near(_num(x[2]), want[1]) and _near(_num(x[3]), want[2])):
            err(f"[XSECTIONS] {nm} {x[1:4] if x else '缺'} ≠ spec {want}（{o.name}）")
    ws = sec.get("WEIRS", [])
    if len(ws) != len(spec.outlet.weirs):
        err(f"[WEIRS] {len(ws)} 個 ≠ spec {len(spec.outlet.weirs)} 個")
    g = codes["hydraulics"]["g"]
    for row, w in zip(ws, spec.outlet.weirs, strict=False):
        nm = row[0]
        if row[1:4] != ["ST1", "OF1", "TRANSVERSE"]:
            err(f"[WEIRS] {nm} 連接 / 型式 {row[1:4]} ≠ ST1 OF1 TRANSVERSE（{w.name}）")
        if not _near(_num(row[4]), w.crest_m - bottom):
            err(f"[WEIRS] {nm} 堰頂 offset {row[4]} ≠ spec {w.crest_m - bottom:.4f}（{w.name}）")
        if w.cd is not None and not _near(_num(row[5]), w.cd * (2 / 3) * math.sqrt(2 * g)):
            err(f"[WEIRS] {nm} Qcoeff {row[5]} ≠ Cd {w.cd} × 2/3 × √2g（{w.name}）")
        x = xs.get(nm)
        if x is None or x[1] != "RECT_OPEN" or not (
            _near(_num(x[2]), max(top - w.crest_m, 0.01)) and _near(_num(x[3]), w.length_m)
        ):
            err(f"[XSECTIONS] {nm} {x[1:4] if x else '缺'} ≠ RECT_OPEN 高 {max(top - w.crest_m, 0.01):.4f} "
                f"長 {w.length_m}（{w.name}）")

    T = str(calc["detention"]["design_T_yr"]["value"])
    subs = sec.get("SUBCATCHMENTS", [])
    mode = "subcatchment" if subs else "hydrograph"
    for r in subs:
        if not _near(_num(r[3]), spec.post.area_ha, 1.5e-6):
            err(f"[SUBCATCHMENTS] {r[0]} 面積 {r[3]} ha ≠ 開發後集水面積 {spec.post.area_ha} ha")
    if mode == "hydrograph":
        inflow = calc["events"][T]["routing"]["inflow_cms"]
        ts = [float(r[2]) for r in sec.get("TIMESERIES", []) if r[0] == "TS_IN"]
        if len(ts) != len(inflow) or any(abs(a - b) > TS_ATOL for a, b in zip(ts, inflow, strict=False)):
            err(f"[TIMESERIES] TS_IN 入流歷線與重算 T{T} 開發後入流不符（{len(ts)} vs {len(inflow)} 點）")

    want_text = build_inp(spec, T=int(T), result=calc, mode=mode)
    if text != want_text:
        gl, wl = text.splitlines(), want_text.splitlines()
        i = next((i for i in range(max(len(gl), len(wl)))
                  if i >= len(gl) or i >= len(wl) or gl[i] != wl[i]), 0)
        err(f"與 build_inp(重算, T={T}, mode={mode}) 不逐字相同；第 {i + 1} 行 "
            f"{(gl[i] if i < len(gl) else '<缺>')[:80]!r} ≠ {(wl[i] if i < len(wl) else '<無>')[:80]!r}")
    res.metrics["swmm_inp"] = f"{path.name}（mode={mode}，T={T}）" + ("一致" if len(res.errors) == n0 else "不符")


def check_figures(artifacts: dict[str, Path], res: VerifyResult) -> None:
    """附圖 PNG 非空白、尺寸足夠；PDF 為有頁面的 PDF。"""
    import numpy as np
    from PIL import Image

    stats = {}
    for f in FIGURES:
        p = artifacts.get(f"{f}_png")
        if p is None or not Path(p).exists():
            res.error(f"缺附圖 {f}.png")
            continue
        try:
            with Image.open(p) as im:
                w, h = im.size
                std = float(np.asarray(im.convert("L"), dtype=float).std())
        except Exception as e:  # noqa: BLE001
            res.error(f"附圖 {Path(p).name} 無法讀取：{e}")
            continue
        stats[f] = f"{w}×{h} px，σ={std:.1f}"
        if min(w, h) <= FIG_MIN_PX:
            res.error(f"附圖 {Path(p).name} 尺寸 {w}×{h} px，短邊須 > {FIG_MIN_PX} px")
        if std <= FIG_MIN_STD:
            res.error(f"附圖 {Path(p).name} 近乎空白（灰階標準差 {std:.2f} ≤ {FIG_MIN_STD}）")
        q = artifacts.get(f"{f}_pdf")
        if q is not None:
            b = Path(q).read_bytes() if Path(q).exists() else b""
            if len(b) < PDF_MIN_BYTES or not b.startswith(b"%PDF") or b"/Page" not in b:
                res.error(f"附圖 {Path(q).name} 不是有效的 PDF 附圖（{len(b)} bytes）")
    res.metrics["figures"] = stats


def verify_artifacts(spec: OutflowControlSpec, artifacts: dict[str, Path]) -> VerifyResult:
    """registry 用：比對 result.json、SWMM .inp、附圖 16/18/19。"""
    p = artifacts.get("result")
    result = json.loads(Path(p).read_text(encoding="utf-8")) if p and Path(p).exists() else None
    res, calc, locked = _check(spec, result)
    if result is None:
        res.error("缺產物 result.json")
    inp = artifacts.get("swmm_inp")
    if inp is None or not Path(inp).exists():
        res.error("缺 SWMM .inp（swmm_inp）")
    else:
        check_inp(locked, calc, Path(inp), res)
    check_figures(artifacts, res)
    return res


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("spec")
    ap.add_argument("--result")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    spec = OutflowControlSpec.model_validate_json(Path(args.spec).read_text(encoding="utf-8"))
    result = json.loads(Path(args.result).read_text(encoding="utf-8")) if args.result else None
    emit(check(spec, result), as_json=args.json)


if __name__ == "__main__":
    main()
