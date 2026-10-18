"""桃園建築基地流出抑制計算表（markdown）。純函式，不得呼叫 LLM。

輸入為 `draftagent.hydro.taoyuan.compute()` 的結果 dict；面積附坪（× 0.3025）。
"""

from __future__ import annotations

from typing import Any

from draftagent.hydro.taoyuan import PING_PER_M2


def _row(item: str, value: str, unit: str, source: str) -> str:
    return f"| {item} | {value} | {unit} | {source} |"


def render_table(r: dict[str, Any]) -> str:
    v = lambda d, f="{:.4f}": f.format(d["value"])
    L = [
        "| 項目 | 數值 | 單位 | 出處 |",
        "|---|---:|---|---|",
        _row("行政區 / 雨量站", f"{r['district']} / {r['station']['value']}", "-", r["station"]["source"]),
        _row("集流時間 tc", f"{r['tc_min']['value']:g}", "min", r["tc_min"]["source"]),
        _row("基地面積", f"{r['site_area_m2']:.2f}（{r['site_area_ping']:.2f} 坪）", "m²", "spec"),
        _row("計算用面積 As", f"{r['As']['As_m2']:.2f}（{r['As']['As_ping']:.2f} 坪）", "m²", r["As"]["rule"]),
    ]
    for s in r["subareas"]:
        L.append(_row(
            f"分區 {s['name']}（{s['landuse_name']}）",
            f"{s['area_m2']:.2f}（{s['area_ping']:.2f} 坪），C={s['c']:.2f}", "m²", s["c_source"],
        ))
    L.append(_row("加權逕流係數 C", v(r["C_composite"]), "-", r["C_composite"]["source"]))
    for label, d in r["rainfall"].items():
        L.append(_row(f"I{d['T_yr']}（{label}）", v(d["I_mm_hr"], "{:.2f}"), "mm/hr", d["I_mm_hr"]["source"]))
        L.append(_row(f"Q{d['T_yr']} 全基地", v(d["Q_site_cms"]), "m³/s", d["Q_site_cms"]["source"]))
    sr = r["storage_required"]
    L += [
        _row("必要貯集量（桃園）", v(sr["taoyuan_m3"], "{:.2f}"), "m³", sr["taoyuan_m3"]["source"]),
        _row("必要貯集量（建技 4-3）", v(sr["building_code_m3"], "{:.2f}"), "m³", sr["building_code_m3"]["source"]),
        _row("**必要貯集量（採用）**", v(sr["required_m3"], "{:.2f}"), "m³", sr["required_m3"]["source"]),
        _row(
            f"提供貯集量（{r['storage_provided']['type']}）",
            v(r["storage_provided"]["provided_m3"], "{:.2f}"), "m³",
            f"淨平面 {r['storage_provided']['net_plan_area_m2']['value']:.2f} m²"
            f"（{r['storage_provided']['net_plan_area_m2']['value'] * PING_PER_M2:.2f} 坪）× 有效水深",
        ),
        _row("容許排放量", v(r["release"]["allowed_cms"], "{:.6f}"), "m³/s", r["release"]["allowed_cms"]["source"]),
        _row(
            "設計排放量範圍",
            f"{r['release']['design_min_cms']['value']:.6f} – {r['release']['design_max_cms']['value']:.6f}",
            "m³/s", "runoff_standard.design_release_ratio",
        ),
        _row(
            f"{'抽水' if r['outlet']['type'] == 'pump' else '孔口'}排放量（{r['outlet']['type']}）",
            f"{r['outlet']['Q_cms']['value']:.6f}（{100 * r['outlet']['ratio_to_allowed']:.1f}%）",
            "m³/s", r["outlet"]["Q_cms"]["source"],
        ),
    ]
    if r["overflow_weir"]:
        L.append(_row("溢流堰 Q", v(r["overflow_weir"]["Q_cms"]), "m³/s", r["overflow_weir"]["Q_cms"]["source"]))
    if r["pipes"]:
        L += ["", "| 管渠 | n | 坡度 | 集水面積 m²（坪） | C | Q 設計 m³/s | 容量 m³/s | 水深 m | 流速 m/s |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for p in r["pipes"]:
            y = "—" if p["normal_depth_m"] is None else f"{p['normal_depth_m']['value']:.3f}"
            vel = "—" if p["velocity_m_s"] is None else f"{p['velocity_m_s']['value']:.3f}"
            # C：pipes[].design_c（上游加權，C_design）；省略時為全基地綜合 C
            c = p["C_design"]["value"] if "C_design" in p else r["C_composite"]["value"]
            L.append(
                f"| {p['name']} | {p['n']['value']:.3f} | {p['slope']:.4f} | "
                f"{p['design_area_m2']:.1f}（{p['design_area_m2'] * PING_PER_M2:.1f}） | {c:.2f} | "
                f"{p['Q_design_cms']['value']:.5f} | {p['capacity_cms']['value']:.5f} | {y} | {vel} |"
            )
    if not r["codes_verified"]:
        L += ["", f"> ⚠ 法規 yaml verified: false — {r['codes_note'].strip()}"]
    return "\n".join(L)
