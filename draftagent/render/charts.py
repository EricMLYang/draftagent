"""出流管制計畫書附圖（matplotlib，PNG + PDF）。純函式，不得呼叫 LLM。

- 附圖 16 開發前後 2、5、10 年重現期距逕流歷線圖（開發前虛線、開發後實線；顏色 = 重現期）
- 附圖 18 滯洪池水深-面積-體積（H-A-V）曲線圖（面積、體積各一欄，共用高程軸）
- 附圖 19 滯洪演算成果圖（上：入流 / 出流；下：池水位 / 外水位；共用時間軸）

輸入為 `hydro.outflow.compute` 的 result dict。兩種量綱不共用一軸（不畫雙 y 軸）。
中文字型：依 `render.preview.cjk_font_paths()` 註冊第一個存在者——macOS STHeiti Light /
Hiragino Sans GB、Windows 微軟正黑體 / 細明體 / 標楷體、Linux Noto Sans CJK，fallback DejaVu Sans；
都找不到則改用英文標籤（略過中文）。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _cjk_font_files() -> list[str]:
    """macOS STHeiti / Hiragino → Windows 微軟正黑體等（%WINDIR%\\Fonts）→ Linux Noto CJK。"""
    from draftagent.render.preview import cjk_font_paths

    return [str(p) for p in cjk_font_paths()]

# 驗證過的類別色（依序取用，不循環）：藍、橘、青
SERIES = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100")
INK = "#0b0b0b"
INK2 = "#52514e"
GRID = "#e4e3df"

TITLES = {
    "fig16": ("附圖16 開發前後2、5、10年重現期距逕流歷線圖", "Fig.16 Pre/post-development runoff hydrographs (2/5/10-yr)"),
    "fig18": ("附圖18 滯洪池水深-面積-體積（H-A-V）曲線圖", "Fig.18 Detention pond H-A-V curves"),
    "fig19": ("附圖19 滯洪演算成果圖", "Fig.19 Detention routing results"),
}

LABELS = {
    "time_hr": ("時間 (hr)", "Time (hr)"),
    "q": ("流量 (m³/s)", "Discharge (m³/s)"),
    "elev": ("高程 (m)", "Elevation (m)"),
    "area": ("水面積 (m²)", "Surface area (m²)"),
    "vol": ("體積 (m³)", "Volume (m³)"),
    "pre": ("開發前", "Pre"),
    "post": ("開發後", "Post"),
    "yr": ("年", "-yr"),
    "inflow": ("入流", "Inflow"),
    "outflow": ("出流", "Outflow"),
    "level": ("池水位", "Pond level"),
    "tw": ("外水位", "Tailwater"),
    "top": ("池頂", "Pond top"),
    "design_top": ("池頂−出水高", "Top − freeboard"),
    "flow_panel": ("入流與出流歷線", "Inflow / outflow"),
    "stage_panel": ("池水位與外水位", "Pond level / tailwater"),
    "area_panel": ("高程-面積", "Elevation-area"),
    "vol_panel": ("高程-體積", "Elevation-volume"),
    "note_demo": ("註：Horner 係數來源：", "Note: Horner source: "),
}


def _setup() -> tuple[Any, bool]:
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import font_manager
    from matplotlib import pyplot as plt

    families: list[str] = []
    for f in _cjk_font_files():
        if Path(f).exists():
            try:
                font_manager.fontManager.addfont(f)
                families.append(font_manager.FontProperties(fname=f).get_name())
                break  # 只用第一個可用字型（多個 CJK family 併列時 matplotlib 會對缺字重警告）
            except Exception:  # noqa: BLE001 — 字型檔損壞時略過
                continue
    plt.rcParams["font.family"] = [*families, "DejaVu Sans"] if families else ["DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    plt.rcParams["pdf.fonttype"] = 42
    plt.rcParams.update({
        "axes.edgecolor": INK2, "axes.labelcolor": INK, "xtick.color": INK2, "ytick.color": INK2,
        "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6,
        "axes.spines.top": False, "axes.spines.right": False, "font.size": 10,
    })
    return plt, bool(families)


def _t(key: str, cjk: bool, table: dict[str, tuple[str, str]] = LABELS) -> str:
    return table[key][0 if cjk else 1]


def _save(fig: Any, out_dir: Path, stem: str) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    png, pdf = out_dir / f"{stem}.png", out_dir / f"{stem}.pdf"
    fig.savefig(png, dpi=200, bbox_inches="tight", facecolor="white")
    fig.savefig(pdf, bbox_inches="tight", facecolor="white")
    return {f"{stem}_png": png, f"{stem}_pdf": pdf}


def _footer(fig: Any, result: dict[str, Any], cjk: bool) -> None:
    src = result["horner"]["source_note"] if cjk else result["horner"]["station"]
    ver = "" if result.get("codes_verified") else "；yaml verified: false（法規數值待人工核對）"
    fig.text(0.01, -0.02, f"{_t('note_demo', cjk)}{src}{ver if cjk else ''}",
             fontsize=7.5, color=INK2, ha="left", va="top", wrap=True)


def render_hydrographs(result: dict[str, Any], out_dir: str | Path) -> dict[str, Path]:
    """附圖 16：開發前（虛線）/ 開發後入流（實線）逕流歷線，2、5、10 年。"""
    plt, cjk = _setup()
    dt_hr = result["dt_min"]["value"] / 60.0
    fig, ax = plt.subplots(figsize=(10, 5.2))
    Ts = sorted(int(t) for t in result["events"])
    lo, hi = None, None
    for i, T in enumerate(Ts):
        ev = result["events"][str(T)]
        color = SERIES[i]
        for key, ls in (("pre", "--"), ("post", "-")):
            q = ev[key]["hydrograph_cms"]
            t = [k * dt_hr for k in range(len(q))]
            yr = f"{T}{_t('yr', cjk)}"
            ax.plot(t, q, ls=ls, lw=1.6, color=color,
                    label=f"{_t(key, cjk)} {yr}（Qp={max(q):.3f}）" if cjk
                    else f"{_t(key, cjk)} {yr} (Qp={max(q):.3f})")
            nz = [k for k, v in enumerate(q) if v > 0.01 * max(q)]
            if nz:
                lo = nz[0] * dt_hr if lo is None else min(lo, nz[0] * dt_hr)
                hi = nz[-1] * dt_hr if hi is None else max(hi, nz[-1] * dt_hr)
    if lo is not None:
        ax.set_xlim(max(0.0, lo - 1.0), hi + 1.0)
    ax.set_ylim(bottom=0)
    ax.set_xlabel(_t("time_hr", cjk))
    ax.set_ylabel(_t("q", cjk))
    ax.set_title(_t("fig16", cjk, TITLES), loc="left", fontsize=12, color=INK)
    ax.legend(ncol=2, fontsize=8.5, frameon=False, loc="center right")
    _footer(fig, result, cjk)
    paths = _save(fig, Path(out_dir), "fig16_hydrographs")
    plt.close(fig)
    return paths


def render_hav(result: dict[str, Any], out_dir: str | Path) -> dict[str, Path]:
    """附圖 18：H-A-V 曲線（左：高程-面積；右：高程-體積）。"""
    plt, cjk = _setup()
    hav = result["hav"]
    det = result["detention"]
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(10, 5), sharey=True)
    ax1.plot(hav["areas"], hav["levels"], "-o", color=SERIES[0], lw=2, ms=6)
    ax2.plot(hav["volumes"], hav["levels"], "-o", color=SERIES[0], lw=2, ms=6)
    for v, lv in zip(hav["volumes"], hav["levels"], strict=False):
        ax2.annotate(f"{v:.0f} m³", (v, lv), textcoords="offset points", xytext=(-8, 4),
                     ha="right", fontsize=8, color=INK2)
    for ax in (ax1, ax2):
        ax.axhline(det["top_elev_m"], color=INK2, lw=1, ls="-")
        if det.get("freeboard_m"):
            ax.axhline(det["design_top_elev_m"], color=INK2, lw=1, ls=":")
        ax.axhline(det["max_level_m"], color=SERIES[1], lw=1.2, ls="--")
    x2 = ax2.get_xlim()[0]
    ax2.text(x2, det["top_elev_m"], f" {_t('top', cjk)} {det['top_elev_m']:.2f}", va="bottom",
             ha="left", fontsize=8, color=INK2)
    if det.get("freeboard_m"):
        ax2.text(x2, det["design_top_elev_m"], f" {_t('design_top', cjk)} {det['design_top_elev_m']:.2f}",
                 va="top", ha="left", fontsize=8, color=INK2)
    T = det["design_T_yr"]["value"]
    hm = f"T{T} max {det['max_level_m']:.3f}" if not cjk else f"{T}年最高水位 {det['max_level_m']:.3f}"
    ax1.text(ax1.get_xlim()[0], det["max_level_m"], f" {hm}", va="bottom", fontsize=8, color=SERIES[1])
    ax1.set_xlabel(_t("area", cjk))
    ax1.set_ylabel(_t("elev", cjk))
    ax2.set_xlabel(_t("vol", cjk))
    ax1.set_title(_t("area_panel", cjk), fontsize=10, color=INK2, loc="left")
    ax2.set_title(_t("vol_panel", cjk), fontsize=10, color=INK2, loc="left")
    fig.suptitle(_t("fig18", cjk, TITLES), x=0.01, ha="left", fontsize=12, color=INK)
    _footer(fig, result, cjk)
    paths = _save(fig, Path(out_dir), "fig18_hav")
    plt.close(fig)
    return paths


def render_routing(result: dict[str, Any], out_dir: str | Path, T: int | None = None) -> dict[str, Path]:
    """附圖 19：滯洪演算成果（上：入流 / 出流；下：池水位 / 外水位）。預設設計重現期（10 年）。"""
    plt, cjk = _setup()
    det = result["detention"]
    T = T or int(det["design_T_yr"]["value"])
    ev = result["events"][str(T)]
    ro = ev["routing"]
    dt_hr = result["dt_min"]["value"] / 60.0
    t = [k * dt_hr for k in range(len(ro["inflow_cms"]))]
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 7), sharex=True,
                                   gridspec_kw={"height_ratios": [1.2, 1]})
    pre_q = ev["pre"]["peak_cms"]["value"]
    ax1.plot(t, ro["inflow_cms"], color=SERIES[0], lw=1.8,
             label=f"{_t('inflow', cjk)}（Qp={ro['peak_inflow_cms']:.3f}）" if cjk
             else f"Inflow (Qp={ro['peak_inflow_cms']:.3f})")
    ax1.plot(t, ro["outflow_cms"], color=SERIES[1], lw=1.8,
             label=f"{_t('outflow', cjk)}（Qp={ro['peak_outflow_cms']['value']:.3f}）" if cjk
             else f"Outflow (Qp={ro['peak_outflow_cms']['value']:.3f})")
    ax1.axhline(pre_q, color=INK2, lw=1, ls="--")
    ax1.annotate((f"開發前 {T} 年洪峰 {pre_q:.3f}" if cjk else f"Pre-dev Q{T} {pre_q:.3f}"),
                 (t[-1], pre_q), textcoords="offset points", xytext=(0, 4),
                 ha="right", va="bottom", fontsize=8, color=INK2)
    ax1.set_ylabel(_t("q", cjk))
    ax1.set_ylim(bottom=0)
    ax1.set_title(_t("flow_panel", cjk), fontsize=10, color=INK2, loc="left")
    ax1.legend(frameon=False, fontsize=9, loc="upper left")

    ax2.plot(t, ro["level_m"], color=SERIES[0], lw=1.8, label=_t("level", cjk))
    if ro.get("tailwater_m"):
        ax2.plot(t, ro["tailwater_m"], color=SERIES[2], lw=1.8, label=_t("tw", cjk))
    ax2.axhline(det["top_elev_m"], color=INK2, lw=1)
    ax2.text(t[0], det["top_elev_m"], f" {_t('top', cjk)} {det['top_elev_m']:.2f}",
             va="bottom", fontsize=8, color=INK2)
    if det.get("freeboard_m"):
        ax2.axhline(det["design_top_elev_m"], color=INK2, lw=1, ls=":")
        ax2.text(t[0], det["design_top_elev_m"], f" {_t('design_top', cjk)} {det['design_top_elev_m']:.2f}",
                 va="top", fontsize=8, color=INK2)
    ax2.set_ylabel(_t("elev", cjk))
    ax2.set_xlabel(_t("time_hr", cjk))
    ax2.set_title(_t("stage_panel", cjk), fontsize=10, color=INK2, loc="left")
    ax2.legend(frameon=False, fontsize=9, loc="center right")
    ttl = _t("fig19", cjk, TITLES) + (f"（{T} 年重現期距）" if cjk else f" (T={T} yr)")
    fig.suptitle(ttl, x=0.01, ha="left", fontsize=12, color=INK)
    _footer(fig, result, cjk)
    paths = _save(fig, Path(out_dir), "fig19_routing")
    plt.close(fig)
    return paths


def render_all(result: dict[str, Any], out_dir: str | Path) -> dict[str, Path]:
    out: dict[str, Path] = {}
    out.update(render_hydrographs(result, out_dir))
    out.update(render_hav(result, out_dir))
    out.update(render_routing(result, out_dir))
    return out


def render_outflow(spec: Any, out_dir: Path) -> dict[str, Path]:
    """registry 用：spec → result.json + 附圖 + SWMM inp。"""
    from draftagent.hydro.outflow import compute
    from draftagent.hydro.swmm_export import write_inp

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    res = compute(spec)
    rp = out_dir / "result.json"
    rp.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    arts: dict[str, Path] = {"result": rp}
    arts.update(render_all(res, out_dir))
    T = int(res["detention"]["design_T_yr"]["value"])
    arts["swmm_inp"] = write_inp(spec, out_dir / f"outflow_T{T}.inp", T=T, result=res)
    return arts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="result.json → 出流管制附圖 16 / 18 / 19")
    ap.add_argument("result")
    ap.add_argument("-o", "--out-dir", required=True)
    args = ap.parse_args(argv)
    res = json.loads(Path(args.result).read_text(encoding="utf-8"))
    for k, p in render_all(res, args.out_dir).items():
        print(f"{k}: {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
