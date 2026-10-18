"""排水網路高程相容性（受納水位 / 滯洪池水位 / 迴水）檢查。純函式、確定性，不得呼叫 LLM。

`check_elevations(spec) -> ElevationReport(errors, warnings, metrics)`；每項檢查的 metrics
列出比較的兩個數值與差值（`{"a": …, "b": …, "diff": a − b, "ok": …}`）。

受納點
------
出流口（kind=outfall）投影到最近的既有排水（existing_drains，點依流向排序）折線上，依沿線距離
線性內插溝底 EL 與溝頂 EL（只用有給值的點；超出範圍取端點值）。有 existing_drains 但距離 >
RECEIVE_MAX_DIST_M → ERROR「出流口未接到既有排水」（E1–E4 無法檢查，不得只以 WARNING 放行）。受納溝設計水位 = 內插溝底 + design_water_depth_m。

檢查（依據：桃園市雨水下水道送審排水報告內容檢核表、桃園水保 / 排水複審紀錄，見
docs/research/2026-10-17-taoyuan-drainage.md §2a / §2c；原文未逐字收錄者已在訊息註明）
- E1 出流口底 ≤ 受納溝底（+ EQ_TOL 視為同高）→ error（放流口在溝底，必倒灌）
- E2 有設計水深：出流口底 < 受納溝設計水位 → error（放流口應設於受納溝設計水深以上）；無 → WARNING
- E3 出流口底 > 受納溝頂 + FLUSH_TOL → error（放流口應切齊溝緣，不得突出 / 跌水）
- E4 gravity / gravity_pump：孔口底 < 受納溝設計水位（無設計水深時 ≤ 溝底）→ error（重力排放不可行）
水位名詞（兩者不同，勿混用）
- **設計滿水位**（貯留水位，`max_water_el`）= 池底 + 有效水深；必須 ≤ 溢流口底（否則未滿即溢流）
- **溢流時最高水位**（`overflow_flood_el`）= 溢流口底 + 溢流堰水頭（calc.overflow_weir.head_m，Q20 過堰）；
  必須 > 溢流口底、≤ 池頂（overflow_at=storage）；溢流在進流井時發生於進流井，不畫在池體

- E5 迴水：滯洪池滿水靜水位 max(設計滿水位, 溢流口底) > 任一進流管下游端底 → error
  （allow_backwater + backwater_note → WARNING）；溢流時水位（溢流口底 + 堰頂水頭）高於進流管底 → WARNING
- E6 溢流口底 ≥ 設計滿水位、≤ 池頂（inlet_well 溢流為進流井地面）、≥ 放流井底（溢流須能重力排出）；
  池內溢流（overflow_at=storage）時溢流時最高水位 ≤ 池頂；
  設計滿水位 ≤ 池頂；孔口底 ≥ 池底、< 溢流口底；孔口設計水深 = 設計滿水位 − 孔口底（±HEAD_TOL）
- E7 pump / gravity_pump：停機 < 起抽 ≤ 設計滿水位；起抽→停機容量 / 抽水量 ≥
  yaml design_criteria.pump_min_run_min（verified: false → WARNING）；停機水位高於池底 → WARNING（呆水）
- E8 隱含邊（滯洪設施不是管段）：進流井底 ≥ 池底；重力排放時孔口底 ≥ 放流井底。
- E9 銜接管：入池進流管底介於池底與進流井底；抽水出水管入井管底介於放流井底與井頂；
  進流井溢流管入放流井管底介於放流井底與溢流口（未給者推導並 WARNING）
  管段本身的順坡（上游底 ≥ 下游底，pump 段除外）由 network.check_network 檢查。

容差 EQ_TOL / FLUSH_TOL / HEAD_TOL / RECEIVE_MAX_DIST_M 是本 repo 的量測 / 繪圖容差，不是法規數值；
只能收緊不能放寬。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from shapely.geometry import LineString, Point

from draftagent.drainage.model import DrainageNetworkSpec, ExistingDrain
from draftagent.drainage.sheet_vocab import OVERFLOW_FLOOD_LABEL, STORAGE_LEVEL_LABEL

__all__ = [
    "ElevationReport",
    "Receiving",
    "StorageLevels",
    "check_elevations",
    "receiving_point",
    "resolve_storage",
    "storage_profile_levels",
]

EL_TOL = 0.005  # 高程比較的數值容差 m
EQ_TOL = 0.01  # 出流口底與受納溝底相差 ≤ 1 cm 視為同高（E1）
FLUSH_TOL = 0.05  # 放流口高出溝頂 ≤ 5 cm 視為切齊（E3）
HEAD_TOL = 0.01  # 孔口設計水深與高程推得的水深容差（E6）
RECEIVE_MAX_DIST_M = 2.0  # 出流口到受納溝線距離上限

SRC_CHECKLIST = "桃園市雨水下水道送審排水報告內容檢核表（研究文件 §2a）"
SRC_REVIEW = "桃園水保 / 排水複審紀錄：銜接處標高程、迴水檢核（研究文件 §2c）"


@dataclass
class ElevationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class Receiving:
    drain_id: str
    distance_m: float
    chainage_m: float
    invert_el: float
    top_el: float
    design_depth_m: float | None

    @property
    def design_wl(self) -> float | None:
        return None if self.design_depth_m is None else self.invert_el + self.design_depth_m


@dataclass(frozen=True)
class StorageLevels:
    mode: str
    bottom_el: float
    max_water_el: float
    top_el: float
    overflow_el: float
    orifice_invert_el: float | None
    orifice_height_m: float | None
    pump_start_el: float | None
    pump_stop_el: float | None
    pump_q_cms: float | None
    net_area_m2: float
    assumed: frozenset[str]  # 未明確給定、由程式推導的欄位
    inflow_invert_el: float = float("nan")  # 進流管入池管底
    pump_outlet_invert_el: float | None = None  # 抽水出水管入放流井管底
    overflow_pipe_invert_el: float | None = None  # 進流井溢流管入放流井管底
    overflow_flood_el: float | None = None  # 溢流時最高水位 = 溢流口底 + 堰頂水頭（未給堰 → None）


# ---------------------------------------------------------------- 受納點


def _interp(ch: list[tuple[float, float]], d: float) -> float:
    ch = sorted(ch)
    if d <= ch[0][0]:
        return ch[0][1]
    for (c0, v0), (c1, v1) in zip(ch, ch[1:], strict=False):
        if d <= c1:
            return v0 if c1 == c0 else v0 + (v1 - v0) * (d - c0) / (c1 - c0)
    return ch[-1][1]


def _drain_line(d: ExistingDrain) -> tuple[LineString, list[float]]:
    line = LineString([(p.x, p.y) for p in d.points])
    cum = [0.0]
    for a, b in zip(d.points, d.points[1:], strict=False):
        cum.append(cum[-1] + Point(a.x, a.y).distance(Point(b.x, b.y)))
    return line, cum


def receiving_point(spec: DrainageNetworkSpec, x: float, y: float) -> Receiving | None:
    """(x, y) 最近的既有排水與該處內插溝底 / 溝頂高程；沒有既有排水 → None。"""
    best = None
    for d in spec.existing_drains:
        line, cum = _drain_line(d)
        dist = line.distance(Point(x, y))
        if best is None or dist < best[0]:
            best = (dist, d, line, cum)
    if best is None:
        return None
    dist, d, line, cum = best
    s = line.project(Point(x, y))
    inv = [(c, p.invert_el) for c, p in zip(cum, d.points, strict=True) if p.invert_el is not None]
    top = [(c, p.top_el) for c, p in zip(cum, d.points, strict=True) if p.top_el is not None]
    return Receiving(
        drain_id=d.id,
        distance_m=dist,
        chainage_m=s,
        invert_el=_interp(inv, s),
        top_el=_interp(top, s),
        design_depth_m=d.design_water_depth_m,
    )


# ---------------------------------------------------------------- 滯洪設施高程


def resolve_storage(spec: DrainageNetworkSpec) -> tuple[StorageLevels | None, list[str], list[str]]:
    """把 storage 的高程欄位補齊。回傳 (levels, errors, warnings)；沒有 storage → (None, [], [])。

    未給的值：bottom_el ← max_water_el − 有效水深，皆無則 ← 放流井（outlet_node）底；
    max_water_el ← bottom_el + 有效水深；孔口底 ← 孔口中心 − 孔口高 / 2，皆無則 ← bottom_el；
    overflow_el ← max_water_el；top_el ← 進流井地面。推導值一律 WARNING「高程未明確給定」。
    """
    st = spec.storage
    if st is None:
        return None, [], []
    from draftagent.drainage.derive import outlet_well_area, storage_cells  # 避免循環 import

    errs: list[str] = []
    warns: list[str] = []
    assumed: set[str] = set()
    eff = st.effective_depth
    if st.bottom_el is not None and st.max_water_el is not None:
        bottom, mwl = st.bottom_el, st.max_water_el
        if abs(mwl - (bottom + eff)) > EL_TOL:
            errs.append(
                f"滯洪設施 {st.id}：設計滿水位 {mwl:.3f} ≠ 池底 {bottom:.3f} + 有效水深 {eff:.3f}"
                f" = {bottom + eff:.3f}（差 {mwl - bottom - eff:+.3f} m）"
            )
    elif st.max_water_el is not None:
        mwl, bottom = st.max_water_el, st.max_water_el - eff
        assumed.add("bottom_el")
    else:
        if st.bottom_el is not None:
            bottom = st.bottom_el
        else:
            bottom = spec.node(st.outlet_node).invert_el
            assumed.add("bottom_el")
        mwl = bottom + eff
        assumed.add("max_water_el")

    top = st.top_el
    if top is None:
        top = spec.node(st.inlet_node).ground_el
        assumed.add("top_el")

    L = None
    if spec.calc is not None and spec.calc.outlet is not None:
        L = spec.calc.outlet.opening_height_m
    orifice = None
    if st.drain_mode != "pump":
        if st.orifice_invert_el is not None:
            orifice = st.orifice_invert_el
        elif st.orifice_center_el is not None:
            if L is None:
                errs.append(f"滯洪設施 {st.id}：給了孔口中心高程但 calc.outlet 無孔口尺寸，無法換算孔口底")
            else:
                orifice = st.orifice_center_el - L / 2
        else:
            orifice = bottom
            assumed.add("orifice_invert_el")
    elif st.orifice_invert_el is not None or st.orifice_center_el is not None:
        warns.append(f"滯洪設施 {st.id}：drain_mode=pump，孔口高程不檢核")

    overflow = st.overflow_el
    if overflow is None:
        overflow = mwl
        assumed.add("overflow_el")

    if assumed:
        names = "、".join(sorted(assumed))
        warns.append(
            f"滯洪設施 {st.id} 高程未明確給定（{names}），已依預設推導：池底 ≈ 放流井底、"
            "設計滿水位 = 池底 + 有效水深、孔口底 = 池底、溢流口 = 設計滿水位、池頂 = 進流井地面；"
            "請工程師於 storage 填 bottom_el / max_water_el / orifice_*_el / overflow_el / top_el"
        )

    # 銜接管高程（詳圖 / 縱斷面用）；未給 → 推導並另行 WARNING（不併入上面的 assumed 訊息）
    link_assumed = []
    si_inv = spec.node(st.inlet_node).invert_el
    ow_inv = spec.node(st.outlet_node).invert_el
    inflow = st.inflow_invert_el
    if inflow is None:
        inflow = si_inv
        link_assumed.append("inflow_invert_el（← 進流井底）")
    pump_out = None
    if st.drain_mode in ("pump", "gravity_pump"):
        pump_out = st.pump_outlet_invert_el
        if pump_out is None:
            pump_out = ow_inv
            link_assumed.append("pump_outlet_invert_el（← 放流井底）")
    ov_pipe = None
    if st.overflow_at == "inlet_well":
        ov_pipe = st.overflow_pipe_invert_el
        if ov_pipe is None:
            ov_pipe = ow_inv
            link_assumed.append("overflow_pipe_invert_el（← 放流井底）")
    if link_assumed:
        warns.append(
            f"滯洪設施 {st.id} 銜接管高程未給定，已推導：{'、'.join(link_assumed)}；"
            "詳圖 / 縱斷面依推導值繪製，請工程師確認"
        )

    flood = None
    if spec.calc is not None and spec.calc.overflow_weir is not None:
        flood = overflow + spec.calc.overflow_weir.head_m

    plan = sum(a * b for a, b in storage_cells(spec))
    return (
        StorageLevels(
            mode=st.drain_mode,
            bottom_el=bottom,
            max_water_el=mwl,
            top_el=top,
            overflow_el=overflow,
            orifice_invert_el=orifice,
            orifice_height_m=L,
            pump_start_el=st.pump_start_el,
            pump_stop_el=st.pump_stop_el,
            pump_q_cms=st.pump_q_cms,
            net_area_m2=plan - outlet_well_area(spec),
            assumed=frozenset(assumed),
            inflow_invert_el=inflow,
            pump_outlet_invert_el=pump_out,
            overflow_pipe_invert_el=ov_pipe,
            overflow_flood_el=flood,
        ),
        errs,
        warns,
    )


def storage_profile_levels(spec: DrainageNetworkSpec) -> list[tuple[str, float, str]]:
    """縱斷面要標的滯洪設施水位 (label, el, ProfileLevel.kind)；只列明確給定（非推導）的高程。

    「溢流時最高水位」只在池內溢流（overflow_at=storage）、溢流口明確給定且有 calc.overflow_weir 時列出。
    """
    lv, _, _ = resolve_storage(spec)
    if lv is None:
        return []
    out: list[tuple[str, float, str]] = []
    if not {"bottom_el", "max_water_el"} <= lv.assumed:
        out.append((STORAGE_LEVEL_LABEL, lv.max_water_el, "design_wl"))
    if "overflow_el" not in lv.assumed:
        out.append(("溢流口", lv.overflow_el, "overflow"))
        if spec.storage.overflow_at == "storage" and lv.overflow_flood_el is not None:
            out.append((OVERFLOW_FLOOD_LABEL, round(lv.overflow_flood_el, 4), "design_wl"))
    if lv.orifice_invert_el is not None and "orifice_invert_el" not in lv.assumed:
        out.append(("孔口底", lv.orifice_invert_el, "other"))
    if lv.pump_start_el is not None:
        out.append(("起抽水位", lv.pump_start_el, "pump_start"))
    if lv.pump_stop_el is not None:
        out.append(("停機水位", lv.pump_stop_el, "pump_stop"))
    return out


# ---------------------------------------------------------------- 檢查


def _m(a_name: str, a: float, b_name: str, b: float, ok: bool) -> dict[str, Any]:
    return {a_name: round(a, 4), b_name: round(b, 4), "diff": round(a - b, 4), "ok": ok}


def _pump_min_run_min(spec: DrainageNetworkSpec) -> tuple[float, dict[str, Any]]:
    from draftagent.hydro.taoyuan import load_codes

    rule = load_codes(spec.codes_path)["design_criteria"].get("pump_min_run_min")
    if not isinstance(rule, dict) or "value" not in rule or not rule.get("source"):
        raise KeyError("taoyuan_runoff.yaml design_criteria.pump_min_run_min（含 value / source）缺漏")
    return float(rule["value"]), rule


def check_elevations(spec: DrainageNetworkSpec) -> ElevationReport:
    rep = ElevationReport()
    E, W, M = rep.errors.append, rep.warnings.append, rep.metrics

    outfalls = [n for n in spec.nodes if n.kind == "outfall"]
    of = outfalls[0] if len(outfalls) == 1 else None
    rcv = None
    if of is None:
        W("出流口不是恰好 1 個，未做受納點高程檢查（拓樸錯誤見網路檢查）")
    elif not spec.existing_drains:
        W(f"未給 existing_drains，出流口 {of.id} 的受納溝高程（E1–E4）無法檢查")
    else:
        r = receiving_point(spec, of.x, of.y)
        M["receiving"] = {
            "drain": r.drain_id,
            "distance_m": round(r.distance_m, 3),
            "chainage_m": round(r.chainage_m, 3),
            "invert_el": round(r.invert_el, 4),
            "top_el": round(r.top_el, 4),
            "design_wl": None if r.design_wl is None else round(r.design_wl, 4),
        }
        if r.distance_m > RECEIVE_MAX_DIST_M:
            E(
                f"出流口 {of.id} 距最近既有排水 {r.drain_id} {r.distance_m:.2f} m > "
                f"{RECEIVE_MAX_DIST_M} m：出流口未接到既有排水（E1–E4 無法檢查）；"
                "請修正出流口位置或補受納排水"
            )
        else:
            rcv = r

    lv, s_err, s_warn = resolve_storage(spec)
    rep.errors += s_err
    rep.warnings += s_warn

    # ---------------- E1–E3 出流口 vs 受納溝
    if rcv is not None and of is not None:
        a = of.invert_el
        ok = a > rcv.invert_el + EQ_TOL
        M["E1_outfall_vs_receiving_invert"] = _m("outfall_invert", a, "receiving_invert", rcv.invert_el, ok)
        if not ok:
            E(
                f"E1 出流口 {of.id} 底 EL={a:.2f} ≤ 受納溝 {rcv.drain_id} 該處溝底 EL={rcv.invert_el:.2f}"
                f"（差 {a - rcv.invert_el:+.3f} m）：放流口在溝底，受納溝有水即倒灌（{SRC_CHECKLIST}）"
            )
        if rcv.design_wl is not None:
            ok = a >= rcv.design_wl - 1e-9
            M["E2_outfall_vs_design_wl"] = _m("outfall_invert", a, "design_wl", rcv.design_wl, ok)
            if not ok:
                E(
                    f"E2 出流口 {of.id} 底 EL={a:.2f} < 受納溝設計水位 EL={rcv.design_wl:.2f}"
                    f"（溝底 {rcv.invert_el:.2f} + 設計水深 {rcv.design_depth_m:.2f}；差 "
                    f"{a - rcv.design_wl:+.3f} m）：放流口應設於受納溝設計水深以上（{SRC_CHECKLIST}；"
                    "「設計水深以上」原文研究文件未逐字收錄，待核）"
                )
        else:
            W(
                f"E2 受納溝 {rcv.drain_id} 未給 design_water_depth_m（設計水深須由工程師提供），"
                "只做溝底檢查（E1），無法確認放流口在設計水位以上"
            )
        ok = a <= rcv.top_el + FLUSH_TOL
        M["E3_outfall_vs_receiving_top"] = _m("outfall_invert", a, "receiving_top", rcv.top_el, ok)
        if not ok:
            E(
                f"E3 出流口 {of.id} 底 EL={a:.2f} > 受納溝頂 EL={rcv.top_el:.2f} + {FLUSH_TOL} m："
                f"放流口高於溝緣（突出 / 跌水），應切齊溝緣（{SRC_CHECKLIST}）"
            )

    if lv is None:
        return rep
    st = spec.storage
    sid = st.id
    M["storage_levels"] = {
        "mode": lv.mode,
        "bottom_el": round(lv.bottom_el, 4),
        "max_water_el": round(lv.max_water_el, 4),
        "top_el": round(lv.top_el, 4),
        "overflow_el": round(lv.overflow_el, 4),
        "orifice_invert_el": None if lv.orifice_invert_el is None else round(lv.orifice_invert_el, 4),
        "pump_start_el": lv.pump_start_el,
        "pump_stop_el": lv.pump_stop_el,
        "assumed": sorted(lv.assumed),
    }
    gravity = lv.mode in ("gravity", "gravity_pump")
    ow = spec.node(st.outlet_node)
    si = spec.node(st.inlet_node)

    # ---------------- E4 重力排放可行性
    if gravity and lv.orifice_invert_el is not None:
        o = lv.orifice_invert_el
        if rcv is not None and rcv.design_wl is not None:
            ok = o >= rcv.design_wl - 1e-9
            M["E4_orifice_vs_design_wl"] = _m("orifice_invert", o, "design_wl", rcv.design_wl, ok)
            if not ok:
                E(
                    f"E4 {sid} drain_mode={lv.mode}：孔口底 EL={o:.2f} < 受納溝設計水位 "
                    f"EL={rcv.design_wl:.2f}（差 {o - rcv.design_wl:+.3f} m），重力排放不可行，"
                    "需改 gravity_pump / pump（並標起抽 / 停機水位）"
                )
        elif rcv is not None:
            ok = o > rcv.invert_el + EQ_TOL
            M["E4_orifice_vs_receiving_invert"] = _m("orifice_invert", o, "receiving_invert", rcv.invert_el, ok)
            if not ok:
                E(
                    f"E4 {sid} drain_mode={lv.mode}：孔口底 EL={o:.2f} ≤ 受納溝底 EL={rcv.invert_el:.2f}，"
                    "重力排放不可行，需改 gravity_pump / pump"
                )
            else:
                W(f"E4 受納溝未給設計水深，只確認孔口底高於溝底（{o:.2f} > {rcv.invert_el:.2f}）")

    # ---------------- E5 迴水
    standing = max(lv.max_water_el, lv.overflow_el)
    inflow = [lk for lk in spec.links if lk.to == st.inlet_node]
    e5 = {}
    for lk in inflow:
        b = spec.node(lk.to).invert_el  # 進流管下游端底 = 進流井底（本 repo 管段以節點底高程為管底）
        ok = standing <= b + 1e-9
        e5[lk.id] = _m("storage_standing_wl", standing, "inflow_pipe_invert", b, ok)
        if not ok:
            msg = (
                f"E5 {sid} 滿水靜水位 EL={standing:.2f}（max(設計滿水位 {lv.max_water_el:.2f}, "
                f"溢流口 {lv.overflow_el:.2f})）> 進流管 {lk.id} 下游端底 EL={b:.2f}（差 {standing - b:+.3f} m）："
                f"滯洪池滿水時迴水進入集水管網（{SRC_REVIEW}）"
            )
            if st.allow_backwater:
                W(msg + f"；工程師說明可承受迴水：{st.backwater_note}")
            else:
                E(msg + "；應降低池水位 / 抬高進流管，或以 allow_backwater + backwater_note 說明管網可承受")
        if spec.calc is not None and spec.calc.overflow_weir is not None:
            wl = lv.overflow_el + spec.calc.overflow_weir.head_m
            if wl > b + 1e-9:
                W(
                    f"E5 溢流時水位 EL={wl:.2f}（溢流口 {lv.overflow_el:.2f} + 堰頂水頭 "
                    f"{spec.calc.overflow_weir.head_m:.2f}）> 進流管 {lk.id} 底 EL={b:.2f}：溢流期間有迴水"
                )
    if e5:
        M["E5_backwater"] = e5
    elif inflow == []:
        W(f"E5 沒有流入進流井 {st.inlet_node} 的管段，未做迴水檢核")

    # ---------------- E6 溢流 / 池頂 / 孔口
    ok = lv.overflow_el >= lv.max_water_el - EL_TOL
    M["E6_overflow_vs_max_wl"] = _m("overflow_el", lv.overflow_el, "max_water_el", lv.max_water_el, ok)
    if not ok:
        E(
            f"E6 {sid} 溢流口底 EL={lv.overflow_el:.2f} < 設計滿水位 EL={lv.max_water_el:.2f}："
            "未達設計滿水位即溢流，有效貯集量不成立"
        )
    upper = si.ground_el if st.overflow_at == "inlet_well" else lv.top_el
    up_name = f"進流井 {si.id} 地面" if st.overflow_at == "inlet_well" else "池頂"
    ok = lv.overflow_el <= upper + EL_TOL
    M["E6_overflow_vs_top"] = _m("overflow_el", lv.overflow_el, "top_el", upper, ok)
    if not ok:
        E(f"E6 {sid} 溢流口底 EL={lv.overflow_el:.2f} > {up_name} EL={upper:.2f}：溢流前即溢頂")
    if st.overflow_at == "storage" and lv.overflow_flood_el is not None:
        fl = lv.overflow_flood_el
        ok = fl <= lv.top_el + EL_TOL
        M["E6_overflow_flood_vs_top"] = _m("overflow_flood_el", fl, "top_el", lv.top_el, ok)
        if not ok:
            E(
                f"E6 {sid} 溢流時最高水位 EL={fl:.2f}（溢流口 {lv.overflow_el:.2f} + 堰頂水頭 "
                f"{fl - lv.overflow_el:.2f}）> 池頂 EL={lv.top_el:.2f}：溢流期間溢頂"
            )
    ok = lv.overflow_el >= ow.invert_el - EL_TOL
    M["E6_overflow_vs_outlet_well"] = _m("overflow_el", lv.overflow_el, "outlet_well_invert", ow.invert_el, ok)
    if not ok:
        E(
            f"E6 {sid} 溢流口底 EL={lv.overflow_el:.2f} < 放流井 {ow.id} 底 EL={ow.invert_el:.2f}："
            "溢流無法重力排至放流井（抽水型請將溢流設於進流井，overflow_at=inlet_well）"
        )
    ok = lv.max_water_el <= lv.top_el + EL_TOL
    M["E6_max_wl_vs_top"] = _m("max_water_el", lv.max_water_el, "top_el", lv.top_el, ok)
    if not ok:
        E(f"E6 {sid} 設計滿水位 EL={lv.max_water_el:.2f} > 池頂 EL={lv.top_el:.2f}：有效水深超出池體")
    if gravity and lv.orifice_invert_el is not None:
        o = lv.orifice_invert_el
        ok = o >= lv.bottom_el - EL_TOL
        M["E6_orifice_vs_bottom"] = _m("orifice_invert", o, "bottom_el", lv.bottom_el, ok)
        if not ok:
            E(f"E6 {sid} 孔口底 EL={o:.2f} < 池底 EL={lv.bottom_el:.2f}")
        ok = o < lv.overflow_el - EL_TOL
        M["E6_orifice_vs_overflow"] = _m("orifice_invert", o, "overflow_el", lv.overflow_el, ok)
        if not ok:
            E(f"E6 {sid} 孔口底 EL={o:.2f} ≥ 溢流口底 EL={lv.overflow_el:.2f}：孔口須低於溢流口")
        if spec.calc is not None and spec.calc.outlet is not None:
            y_geo = lv.max_water_el - o
            y = spec.calc.outlet.design_head_m
            ok = abs(y - y_geo) <= HEAD_TOL
            M["E6_orifice_head"] = _m("design_head_m", y, "max_wl_minus_orifice", y_geo, ok)
            if not ok:
                E(
                    f"E6 孔口設計水深 y={y:.3f} m ≠ 設計滿水位 {lv.max_water_el:.2f} − 孔口底 {o:.2f}"
                    f" = {y_geo:.3f} m：計算書與高程不一致"
                )

    # ---------------- E7 抽水
    if lv.mode in ("pump", "gravity_pump"):
        start, stop = lv.pump_start_el, lv.pump_stop_el
        ok = stop < start - EL_TOL
        M["E7_stop_vs_start"] = _m("pump_stop_el", stop, "pump_start_el", start, ok)
        if not ok:
            E(f"E7 {sid} 停機水位 EL={stop:.2f} 應低於起抽水位 EL={start:.2f}")
        ok = start <= lv.max_water_el + EL_TOL
        M["E7_start_vs_max_wl"] = _m("pump_start_el", start, "max_water_el", lv.max_water_el, ok)
        if not ok:
            E(f"E7 {sid} 起抽水位 EL={start:.2f} > 設計滿水位 EL={lv.max_water_el:.2f}")
        if stop > lv.bottom_el + EL_TOL:
            dead = lv.net_area_m2 * (stop - lv.bottom_el)
            W(
                f"E7 {sid} 停機水位 EL={stop:.2f} 高於池底 EL={lv.bottom_el:.2f}：池底至停機水位約 "
                f"{dead:.2f} m³ 抽不出（呆水），不宜計入有效貯集量"
            )
        if lv.mode == "gravity_pump" and lv.orifice_invert_el is not None and start > lv.orifice_invert_el + EL_TOL:
            W(
                f"E7 {sid} gravity_pump 起抽水位 EL={start:.2f} 高於重力孔口底 EL={lv.orifice_invert_el:.2f}"
                "：下層抽水 / 上層重力的分界不清"
            )
        if lv.pump_q_cms is None:
            W(f"E7 {sid} 未給 pump_q_cms，未檢核起抽後運轉時間")
        else:
            need, rule = _pump_min_run_min(spec)
            vol = lv.net_area_m2 * max(start - max(stop, lv.bottom_el), 0.0)
            run = vol / lv.pump_q_cms / 60.0
            ok = run >= need - 1e-9
            M["E7_pump_run_min"] = {
                **_m("run_min", run, "required_min", need, ok),
                "volume_m3": round(vol, 3),
                "pump_q_cms": lv.pump_q_cms,
            }
            if not ok:
                E(
                    f"E7 {sid} 起抽 EL={start:.2f} → 停機 EL={stop:.2f} 容量 {vol:.2f} m³ / 抽水量 "
                    f"{lv.pump_q_cms:.4f} m³/s = {run:.1f} min < {need:g} min（{rule['source']}）"
                )
            if not rule.get("verified", False):
                W(f"E7 起抽後最少運轉 {need:g} min 取自 yaml design_criteria.pump_min_run_min（verified: false，待核）")

    # ---------------- E9 銜接管（入池管 / 抽水出水管 / 溢流管）
    a = lv.inflow_invert_el
    ok = lv.bottom_el - EL_TOL <= a <= si.invert_el + EL_TOL
    M["E9_inflow_invert"] = _m("inflow_invert", a, "inlet_well_invert", si.invert_el, ok)
    if not ok:
        E(
            f"E9 {sid} 入池進流管底 EL={a:.2f} 須介於池底 EL={lv.bottom_el:.2f} 與進流井 {si.id} 底 "
            f"EL={si.invert_el:.2f} 之間（進流井→滯洪池須順坡）"
        )
    if lv.pump_outlet_invert_el is not None:
        a = lv.pump_outlet_invert_el
        ok = ow.invert_el - EL_TOL <= a <= ow.ground_el + EL_TOL
        M["E9_pump_outlet"] = _m("pump_outlet_invert", a, "outlet_well_invert", ow.invert_el, ok)
        if not ok:
            E(
                f"E9 {sid} 抽水出水管入井管底 EL={a:.2f} 須介於放流井 {ow.id} 底 EL={ow.invert_el:.2f}"
                f" 與井頂 EL={ow.ground_el:.2f} 之間"
            )
    if lv.overflow_pipe_invert_el is not None:
        a = lv.overflow_pipe_invert_el
        ok = ow.invert_el - EL_TOL <= a <= lv.overflow_el + EL_TOL
        M["E9_overflow_pipe"] = _m("overflow_pipe_invert", a, "overflow_el", lv.overflow_el, ok)
        if not ok:
            E(
                f"E9 {sid} 溢流管入放流井管底 EL={a:.2f} 須介於放流井 {ow.id} 底 EL={ow.invert_el:.2f} "
                f"與溢流口 EL={lv.overflow_el:.2f} 之間（溢流管須重力排入放流井）"
            )

    # ---------------- E8 隱含邊（滯洪設施）
    ok = si.invert_el >= lv.bottom_el - EL_TOL
    M["E8_inlet_vs_bottom"] = _m("inlet_invert", si.invert_el, "bottom_el", lv.bottom_el, ok)
    if not ok:
        E(f"E8 進流井 {si.id} 底 EL={si.invert_el:.2f} < {sid} 池底 EL={lv.bottom_el:.2f}：無法重力流入")
    if gravity and lv.orifice_invert_el is not None:
        o = lv.orifice_invert_el
        ok = o >= ow.invert_el - EL_TOL
        M["E8_orifice_vs_outlet_well"] = _m("orifice_invert", o, "outlet_well_invert", ow.invert_el, ok)
        if not ok:
            E(
                f"E8 {sid} 孔口底 EL={o:.2f} < 放流井 {ow.id} 底 EL={ow.invert_el:.2f}："
                f"{sid}→{ow.id} 逆坡，無法重力排放"
            )
    return rep
