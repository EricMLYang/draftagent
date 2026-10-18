"""桃園建築基地雨水流出抑制設施計算 verifier。純函式，不得呼叫 LLM。

用法：
    python -m draftagent.verify.drainage_calc_check <spec.json> [--result result.json] [--calc calc.md] [--json]

檢查項目（全部由 spec 重算，不採信 result.json 的數字）
--------------------------------------------------------
1. yaml：verified: false → WARNING
2. spec 通過 schema（landuse / c_override / station / material 皆須在 yaml 內）
3. Σ subareas 面積 = site_area_m2（相對誤差 ≤ 0.5%，沿用 area_check 門檻）
4. site_area ≤ runoff_standard.exempt_site_area_m2_max → WARNING（免設提示，不報錯）
5. 提供貯集量 ≥ 必要貯集量（max(桃園 0.051, 建技 4-3 0.045) × As）
6. 孔口（或抽水 release_mode=pump，WARNING 原文待核）設計排放量 ≤ 容許排放量（As × 0.000014）且 ≥ 0.85 × 容許（design_release_ratio）
7. 溢流堰：有 → Q ≥ Q20（storage 重現期之全基地逕流量）；無 → WARNING
8. 管渠：設計流量 ≤ 通水容量、流速在 velocity_m_s、U 溝出水高、最小管徑、材料與管徑相符；
   設計流量 Q = C · I(collection) · A，C = pipes[].design_c（上游集水分區加權 C，0–1）或省略時用全基地綜合 C；
   design_c < 綜合 C → WARNING（無網路拓樸時無法驗證其來源）
9. --result：result.json 的所有數值必須與重算一致（相對 1e-6），鍵不得缺漏
10. existing_building（增改建 As 換算）→ WARNING（規則原文待核）；tc 非 yaml 建議值 → WARNING
11. spec.codes_path ≠ repo 預設 yaml → ERROR；重算一律讀預設 yaml（codes_lock）
12. --calc：calc.md 必須與 render_table(重算結果) 逐字相同
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from draftagent.hydro.taoyuan import compute, load_codes
from draftagent.hydro.taoyuan_report import render_table
from draftagent.schema.drainage import DEFAULT_CODES_PATH, TaoyuanSiteDrainageSpec
from draftagent.verify.base import VerifyResult, emit
from draftagent.verify.codes_lock import assert_default_codes

NAME = "drainage_calc_check"
AREA_TOLERANCE = 0.005  # 與 area_check 相同；只能收緊不能放寬
REL_TOL = 1e-6


def _flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.update(_flatten(v, f"{prefix}[{i}]"))
    else:
        out[prefix] = obj
    return out


def _compare_result(res: VerifyResult, recomputed: dict[str, Any], given: dict[str, Any]) -> None:
    a, b = _flatten(recomputed), _flatten(given)
    a.pop("codes_path", None)
    b.pop("codes_path", None)
    n_bad = 0
    for k, v in a.items():
        if k not in b:
            res.error(f"result.json 缺少 {k}")
            n_bad += 1
            continue
        w = b[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            if v != w:
                res.error(f"result.json {k} = {w!r}，重算 = {v!r}")
                n_bad += 1
        elif not isinstance(w, (int, float)) or not math.isclose(v, w, rel_tol=REL_TOL, abs_tol=1e-12):
            res.error(f"result.json {k} = {w!r}，重算 = {v!r}")
            n_bad += 1
    extra = sorted(set(b) - set(a))
    if extra:
        res.error(f"result.json 有重算沒有的鍵：{extra[:5]}{'…' if len(extra) > 5 else ''}")
        n_bad += len(extra)
    res.metrics["result_json_mismatches"] = n_bad


def _compare_calc_md(res: VerifyResult, recomputed: dict[str, Any], calc_path: Path) -> None:
    """calc.md 必須與 render_table(重算) 逐字相同（kinds/drainage_calc_ty 寫檔時末尾加一個換行）。"""
    if not calc_path.exists():
        res.error(f"calc.md 不存在：{calc_path}")
        return
    want = render_table(recomputed) + "\n"
    got = calc_path.read_text(encoding="utf-8")
    if got == want:
        res.metrics["calc_md"] = "與重算計算表逐字相同"
        return
    gl, wl = got.splitlines(), want.splitlines()
    diffs = [i for i in range(max(len(gl), len(wl)))
             if i >= len(gl) or i >= len(wl) or gl[i] != wl[i]]
    i = diffs[0]
    res.error(
        f"calc.md 與重算計算表不符（{len(diffs)} 行不同）；第 {i + 1} 行："
        f"{(gl[i] if i < len(gl) else '<缺>')[:120]!r} ≠ 重算 {(wl[i] if i < len(wl) else '<無>')[:120]!r}"
    )


def check(
    spec_path: str | Path,
    result_path: str | Path | None = None,
    calc_path: str | Path | None = None,
) -> VerifyResult:
    res = VerifyResult(name=NAME)
    spec_path = Path(spec_path)
    res.metrics["spec"] = str(spec_path)
    if not spec_path.exists():
        res.error(f"spec 不存在：{spec_path}")
        return res
    try:
        spec = TaoyuanSiteDrainageSpec.from_json(spec_path)
    except ValidationError as e:
        for err in e.errors():
            loc = ".".join(str(x) for x in err["loc"])
            res.error(f"schema：{loc + '：' if loc else ''}{err['msg']}")
        return res

    assert_default_codes(spec.codes_path, DEFAULT_CODES_PATH, res, "codes_path")
    spec = spec.model_copy(update={"codes_path": DEFAULT_CODES_PATH})  # 一律讀 repo 預設 yaml
    codes = load_codes(DEFAULT_CODES_PATH)
    if not codes["verified"]:
        res.warn(f"法規 yaml verified: false — {codes.get('note', '').strip()}")

    try:
        r = compute(spec)
    except (ValueError, KeyError) as e:
        res.error(f"計算失敗：{e}")
        return res

    rs = codes["runoff_standard"]
    dc = codes["design_criteria"]

    # --- 面積
    site, tot = spec.site_area_m2, r["subarea_total_m2"]
    rel = abs(tot - site) / site
    res.metrics.update(
        site_area_m2=site, subarea_total_m2=round(tot, 3), subarea_diff_pct=round(100 * rel, 4)
    )
    if rel > AREA_TOLERANCE:
        res.error(
            f"Σ subareas {tot:.3f} m² ≠ 基地 {site:.3f} m²（差 {100 * rel:.3f}% > {100 * AREA_TOLERANCE}%）"
        )
    if site <= rs["exempt_site_area_m2_max"]:
        res.warn(
            f"基地 {site} m² ≤ {rs['exempt_site_area_m2_max']} m²：依逕流量標準免設流出抑制設施"
            "（runoff_standard.exempt_site_area_m2_max），以下檢核僅供參考"
        )
    if spec.existing_building is not None:
        res.warn(f"使用增改建 As 換算：{r['As']['rule']}")
    if spec.station is not None:
        res.warn(f"雨量站由 spec 覆寫為 {spec.station}（行政區 {spec.district}），請工程師確認")
    tc_rec = dc["tc_min_recommended"]
    if r["tc_min"]["value"] != tc_rec:
        res.warn(f"集流時間 {r['tc_min']['value']} min ≠ yaml 建議值 {tc_rec} min")

    res.metrics.update(
        station=r["station"]["value"],
        tc_min=r["tc_min"]["value"],
        As_m2=round(r["As"]["As_m2"], 3),
        C_composite=round(r["C_composite"]["value"], 4),
    )
    for label, d in r["rainfall"].items():
        res.metrics[f"I{d['T_yr']}_{label}_mm_hr"] = round(d["I_mm_hr"]["value"], 3)

    # --- 貯集量
    req = r["storage_required"]["required_m3"]["value"]
    prov = r["storage_provided"]["provided_m3"]["value"]
    res.metrics.update(
        storage_taoyuan_m3=round(r["storage_required"]["taoyuan_m3"]["value"], 3),
        storage_building_code_m3=round(r["storage_required"]["building_code_m3"]["value"], 3),
        storage_required_m3=round(req, 3),
        storage_adopted=r["storage_required"]["adopted"],
        storage_provided_m3=round(prov, 3),
    )
    if prov + 1e-9 < req:
        res.error(f"提供貯集量 {prov:.3f} m³ < 必要 {req:.3f} m³")

    # --- 排放量
    allowed = r["release"]["allowed_cms"]["value"]
    qmin = r["release"]["design_min_cms"]["value"]
    qmax = r["release"]["design_max_cms"]["value"]
    q_out = r["outlet"]["Q_cms"]["value"]
    res.metrics.update(
        release_allowed_cms=round(allowed, 6),
        outlet_q_cms=round(q_out, 6),
        outlet_ratio_pct=round(100 * q_out / allowed, 2),
    )
    lo, hi = rs["design_release_ratio"]
    dev = "孔口"
    if r["outlet"]["type"] == "pump":
        dev = "抽水"
        res.warn(
            "抽水排放：抽水量以容許排放量（runoff_standard.max_release_cms_per_m2）與 "
            "design_release_ratio 檢核；「抽水排放量亦不得超過容許排放量」之手冊原文研究文件未收錄，待人工核對"
        )
    if q_out > qmax * (1 + 1e-9):
        res.error(f"{dev}排放量 {q_out:.6f} m³/s > 容許 {hi:.0%} × {allowed:.6f} = {qmax:.6f} m³/s")
    if q_out < qmin * (1 - 1e-9):
        res.error(f"{dev}排放量 {q_out:.6f} m³/s < 容許 {lo:.0%} × {allowed:.6f} = {qmin:.6f} m³/s")

    # --- 溢流堰
    q20 = r["rainfall"]["storage"]["Q_site_cms"]["value"]
    res.metrics["Q_storage_site_cms"] = round(q20, 6)
    if r["overflow_weir"] is None:
        res.warn("未設溢流堰，未檢核溢流能力")
    else:
        qw = r["overflow_weir"]["Q_cms"]["value"]
        res.metrics["overflow_weir_q_cms"] = round(qw, 6)
        if qw + 1e-12 < q20:
            res.error(f"溢流堰 Q {qw:.5f} m³/s < Q{r['rainfall']['storage']['T_yr']} {q20:.5f} m³/s")

    # --- 管渠
    for p in r["pipes"]:
        cd = p.get("C_design")
        tag = f"管渠 {p['name']}" + ("（上游集水分區加權 C）" if cd is not None else "")
        for e in p["errors"]:
            res.error(f"{tag}：{e}")
        for w in p["warnings"]:
            res.warn(f"{tag}：{w}")
        if cd is not None and cd["value"] < r["C_composite"]["value"]:
            res.warn(
                f"{tag}：design_c={cd['value']:.4f} 低於全基地綜合 C {r['C_composite']['value']:.4f}；"
                "本 verifier 無法驗證其上游集水分區來源，請確認（drainage-plan 另由網路拓樸獨立重算）"
            )
        v = p["velocity_m_s"]
        c = cd["value"] if cd is not None else r["C_composite"]["value"]
        res.metrics[f"pipe[{p['name']}]"] = (
            f"C={c:.4f}{'' if cd is not None else '(綜合)'} Q={p['Q_design_cms']['value']:.5f} "
            f"cap={p['capacity_cms']['value']:.5f} V={'—' if v is None else round(v['value'], 3)}"
        )

    if result_path is not None:
        rp = Path(result_path)
        if not rp.exists():
            res.error(f"result.json 不存在：{rp}")
        else:
            _compare_result(res, r, json.loads(rp.read_text(encoding="utf-8")))
    if calc_path is not None:
        _compare_calc_md(res, r, Path(calc_path))
    return res


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spec")
    ap.add_argument("--result", default=None)
    ap.add_argument("--calc", default=None, help="calc.md（逐字比對重算計算表）")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    emit(check(a.spec, a.result, a.calc), as_json=a.json)


if __name__ == "__main__":
    main()
