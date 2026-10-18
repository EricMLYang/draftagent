"""OutflowControlSpec → SWMM 5 `.inp`（只寫檔，不執行）；另提供子行程執行器。

審查實務要求附 SWMM 模式輸入 / 輸出檔（手冊 附件一 附 1.1），本模組為此準備。純函式，不得呼叫 LLM。

兩種入流模式
- "hydrograph"（預設）：本 repo 以 SCS 無因次單位歷線算出的開發後入流歷線，以 [INFLOWS] 直接灌入
  滯洪池節點；SWMM 只做滯洪池 + 出流工 + 外水位演算，可與 `hydro.outflow` 結果對照。
- "subcatchment"：雨量站（交替區塊雨型）+ 子集水區（CURVE_NUMBER 入滲）→ 滯洪池。SWMM 以非線性
  水庫產生逕流，**與 SCS 單位歷線不同**；寬度 / 坡度 / 糙度為佔位值，須由工程師檢視後調整。

約定（與 `hydro.outflow` 一致）
- 高程：Storage 節點 invert = 池底；LINK_OFFSETS DEPTH，孔口 / 堰之 offset = 高程 − 池底
- 孔口 Gated = YES（不倒灌，與 hydro.outflow 假設相同）
- 堰係數 Cw（SI）= Cd · (2/3) · √(2g)；spec 未給 Cd 時以池頂 − 出水高處水頭依手冊式 1-9～1-12 求值
- 外水位：free → FREE；fixed → FIXED；hydrograph → TIMESERIES（第十點公式）

**不要在主行程 import pyswmm / swmm.toolkit**（本機會被系統 SIGKILL）；請用 `run_swmm_subprocess`。
"""

from __future__ import annotations

import datetime as _dt
import math
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from draftagent.hydro.outflow import compute, load_codes, weir_cd

if TYPE_CHECKING:
    from draftagent.schema.outflow import OutflowControlSpec

START = _dt.datetime(2000, 1, 1)


def _hm(minutes: float) -> str:
    m = int(round(minutes))
    return f"{m // 60}:{m % 60:02d}"


def _dt_fmt(t: _dt.datetime) -> tuple[str, str]:
    return t.strftime("%m/%d/%Y"), t.strftime("%H:%M:%S")


def _ts(name: str, values: list[float], dt_min: float) -> list[str]:
    return [f"{name:<16} {_hm(i * dt_min):>8} {v:.6f}" for i, v in enumerate(values)]


def build_inp(
    spec: OutflowControlSpec,
    *,
    T: int = 10,
    result: dict[str, Any] | None = None,
    mode: Literal["hydrograph", "subcatchment"] = "hydrograph",
    width_m: float | None = None,
    slope_pct: float = 1.0,
    report_step_s: int = 60,
    routing_step_s: int = 5,
) -> str:
    """回傳 .inp 文字。result 省略時重算。"""
    codes = load_codes(spec.codes_path)
    res = result if result is not None else compute(spec)
    ev = res["events"][str(T)]
    dt_min = float(res["dt_min"]["value"])
    inflow = ev["routing"]["inflow_cms"]
    n = len(inflow)
    end = START + _dt.timedelta(minutes=dt_min * (n - 1))
    det = spec.detention
    bottom = det.layers[0].elev_m
    top = det.layers[-1].elev_m
    h0 = (det.initial_elev_m - bottom) if det.initial_elev_m is not None else 0.0
    g = codes["hydraulics"]["g"]

    L: list[str] = []
    a = L.append
    a("[TITLE]")
    a(f";;{spec.title or 'outflow control'} — T={T} 年；draftagent.hydro.swmm_export 產生（mode={mode}）")
    a(";;法規數值 yaml verified=" + str(res["codes_verified"]).lower())
    a("")
    a("[OPTIONS]")
    sd, st = _dt_fmt(START)
    ed, et = _dt_fmt(end)
    opts = [
        ("FLOW_UNITS", "CMS"), ("INFILTRATION", "CURVE_NUMBER"), ("FLOW_ROUTING", "DYNWAVE"),
        ("LINK_OFFSETS", "DEPTH"), ("START_DATE", sd), ("START_TIME", st),
        ("REPORT_START_DATE", sd), ("REPORT_START_TIME", st), ("END_DATE", ed), ("END_TIME", et),
        ("DRY_DAYS", "0"), ("REPORT_STEP", _hm(report_step_s / 60) + ":00"),
        ("WET_STEP", "00:01:00"), ("DRY_STEP", "00:01:00"),
        ("ROUTING_STEP", str(routing_step_s)), ("ALLOW_PONDING", "NO"),
        ("INERTIAL_DAMPING", "PARTIAL"), ("NORMAL_FLOW_LIMITED", "BOTH"),
        ("MIN_SURFAREA", "0"), ("THREADS", "1"),
    ]
    L += [f"{k:<20} {v}" for k, v in opts]
    a("")

    if mode == "subcatchment":
        area_ha = spec.post.area_ha
        cn = res["catchments"]["post"]["cn_used"]["value"]
        width = width_m if width_m is not None else math.sqrt(area_ha * 1e4)
        [d / dt_min * 60.0 for d in ev["rain_mm"]]
        a("[RAINGAGES]")
        a(f"RG1  INTENSITY  {_hm(dt_min)}  1.0  TIMESERIES  TS_RAIN")
        a("")
        a("[SUBCATCHMENTS]")
        a(";;Name RainGage Outlet Area(ha) %Imperv Width(m) %Slope CurbLen")
        a(f"S1  RG1  ST1  {area_ha:.6f}  0  {width:.3f}  {slope_pct}  0")
        a("")
        a("[SUBAREAS]")
        a(";;佔位值：N-Imperv N-Perv S-Imperv S-Perv PctZero RouteTo — 須由工程師檢視")
        a("S1  0.015  0.15  0  0  100  OUTLET")
        a("")
        a("[INFILTRATION]")
        a(f"S1  {cn:.3f}  0.5  7")
        a("")

    a("[OUTFALLS]")
    inverts = [o.invert_m for o in spec.outlet.orifices] + [w.crest_m for w in spec.outlet.weirs]
    tw = spec.tailwater
    if tw.mode == "free":
        a(f"OF1  {min(inverts):.4f}  FREE  NO")
    elif tw.mode == "fixed":
        a(f"OF1  {min(min(inverts), tw.elev_m):.4f}  FIXED  {tw.elev_m:.4f}  NO")
    else:
        a(f"OF1  {tw.Hb_m:.4f}  TIMESERIES  TS_TW  NO")
    a("")
    a("[STORAGE]")
    a(";;Name Elev MaxDepth InitDepth Shape Curve SurDepth/Apond Fevap")
    a(f"ST1  {bottom:.4f}  {top - bottom:.4f}  {h0:.4f}  TABULAR  SC_ST1  0  0")
    a("")

    links: list[tuple[str, str]] = []
    a("[ORIFICES]")
    a(";;Name From To Type Offset Qcoeff Gated CloseTime")
    xs: list[str] = []
    for i, o in enumerate(spec.outlet.orifices, 1):
        nm = f"OR{i}"
        links.append((nm, o.name))
        typ = "SIDE" if o.type.startswith("vertical") else "BOTTOM"
        cd = o.cd if o.cd is not None else codes["hydraulics"]["orifice_cd_default"]
        cd *= 1.0 - o.clog_ratio
        a(f"{nm}  ST1  OF1  {typ}  {o.invert_m - bottom:.4f}  {cd:.4f}  YES  0  ;{o.name}")
        if o.type.endswith("circular"):
            xs.append(f"{nm}  CIRCULAR  {o.diameter_m:.4f}  0  0  0")
        else:
            xs.append(f"{nm}  RECT_CLOSED  {o.height_m:.4f}  {o.width_m:.4f}  0  0")
    a("")
    a("[WEIRS]")
    a(";;Name From To Type CrestHt Qcoeff Gated EndCon EndCoeff Surcharge")
    design_top = top - (det.freeboard_m or 0.0)
    for i, w in enumerate(spec.outlet.weirs, 1):
        nm = f"W{i}"
        links.append((nm, w.name))
        if w.cd is not None:
            cd, note = w.cd, "spec.cd"
        else:
            H1 = max(design_top - w.crest_m, 0.01)
            info = weir_cd(H1, codes, crest_width=w.crest_width_m, weir_height=w.weir_height_m)
            cd, note = info["cd"], f"{info['source']} @H1={H1:.3f}m"
        cw = cd * (2.0 / 3.0) * math.sqrt(2.0 * g)
        a(f"{nm}  ST1  OF1  TRANSVERSE  {w.crest_m - bottom:.4f}  {cw:.4f}  NO  0  0  YES  ;{w.name} Cd={cd:.4f}（{note}）")
        xs.append(f"{nm}  RECT_OPEN  {max(top - w.crest_m, 0.01):.4f}  {w.length_m:.4f}  0  0")
    a("")
    a("[XSECTIONS]")
    a(";;Link Shape Geom1 Geom2 Geom3 Geom4")
    L += xs
    a("")

    if mode == "hydrograph":
        a("[INFLOWS]")
        a(";;Node Constituent TimeSeries Type Mfactor Sfactor")
        a("ST1  FLOW  TS_IN  FLOW  1.0  1.0")
        a("")

    a("[CURVES]")
    a(";;Name Type Depth(m) Area(m2)")
    for j, lv in enumerate(det.layers):
        a(f"SC_ST1  {'Storage' if j == 0 else '':<8} {lv.elev_m - bottom:.4f}  {lv.area_m2:.4f}")
    a("")
    a("[TIMESERIES]")
    a(";;Name Time(H:MM) Value")
    if mode == "hydrograph":
        L += _ts("TS_IN", inflow, dt_min)
    else:
        L += _ts("TS_RAIN", [d / dt_min * 60.0 for d in ev["rain_mm"]] + [0.0], dt_min)
    if tw.mode == "hydrograph":
        L += _ts("TS_TW", ev["routing"]["tailwater_m"], dt_min)
    a("")
    a("[REPORT]")
    a("INPUT NO")
    a("CONTROLS NO")
    a("NODES ALL")
    a("LINKS ALL")
    a("")
    a("[COORDINATES]")
    a("ST1  0  0")
    a("OF1  100  0")
    a("")
    return "\n".join(L) + "\n"


def write_inp(spec: OutflowControlSpec, path: str | Path, **kw: Any) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(build_inp(spec, **kw), encoding="utf-8")
    return p


def read_sections(text: str) -> dict[str, list[list[str]]]:
    """極簡 .inp 解析：{SECTION: [[欄位…], …]}，略過註解與空行（測試 / 自我檢查用）。"""
    out: dict[str, list[list[str]]] = {}
    cur = None
    for raw in text.splitlines():
        line = raw.split(";", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            cur = line[1:-1].upper()
            out.setdefault(cur, [])
            continue
        if cur is None:
            raise ValueError(f"section 外的內容：{raw!r}")
        out[cur].append(line.split())
    return out


_RUNNER = (
    "import sys\n"
    "from swmm.toolkit import solver\n"
    "solver.swmm_run(sys.argv[1], sys.argv[2], sys.argv[3])\n"
)


def run_swmm_subprocess(
    inp: str | Path, *, timeout_s: float = 120.0, python: str | None = None
) -> dict[str, Any]:
    """以子行程呼叫 swmm.toolkit.solver.swmm_run；回傳 {ok, returncode, rpt, out, message, errors}。

    失敗（逾時、被 signal 終止、rpt 有 ERROR）時 ok=False 並附說明，不拋例外。
    """
    inp = Path(inp).resolve()
    rpt, out = inp.with_suffix(".rpt"), inp.with_suffix(".out")
    cmd = [python or sys.executable, "-c", _RUNNER, str(inp), str(rpt), str(out)]
    info: dict[str, Any] = {"ok": False, "returncode": None, "rpt": str(rpt), "out": str(out),
                            "errors": [], "message": ""}
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=timeout_s)
    except subprocess.TimeoutExpired:
        info["message"] = f"SWMM 子行程逾時（> {timeout_s} s）"
        return info
    info["returncode"] = cp.returncode
    if cp.returncode < 0:
        info["message"] = (
            f"SWMM 子行程被 signal {-cp.returncode} 終止（本機 swmm-toolkit 動態庫可能被系統拒絕載入，"
            "例如簽章失效）；stderr: " + cp.stderr[-500:]
        )
        return info
    if rpt.exists():
        rpt_text = rpt.read_text(encoding="utf-8", errors="replace")
        info["errors"] = [ln.strip() for ln in rpt_text.splitlines() if "ERROR" in ln.upper()]
    if cp.returncode != 0 or info["errors"]:
        info["message"] = f"SWMM 執行失敗 rc={cp.returncode}；{info['errors'][:5]}；{cp.stderr[-500:]}"
        return info
    info["ok"] = True
    info["message"] = "SWMM 執行完成"
    return info
