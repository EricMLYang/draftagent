"""排水系統平面設計圖 + 集水區分析圖 DXF verifier。純函式，不得呼叫 LLM。

用法：python -m draftagent.verify.drainage_plan_check <dxf> --spec <spec.json> [--json]

所有數字由 spec 重算（座標 → 長度、invert → 坡度、polygon → 面積），不採信圖面。

檢查項目
- $INSUNITS = 6（m，TWD97 座標）；任何 layout 的 0 層無實體
- 網路拓樸（drainage/network.check_network）：單一出流口、無迴圈、皆可達出流口、順坡（pump 除外）
- 高程相容性（drainage/hydraulics.check_elevations，E1–E8 全部 error 照算）：出流口 vs 受納溝底 / 設計水位 /
  溝頂、重力排放可行性、滯洪池滿水迴水、溢流 / 孔口 / 池頂次序、起抽 / 停機水位與 15 min 運轉、隱含邊順坡；
  受納溝有 design_water_depth_m 時，出流口標籤須有「受納溝設計水位 EL=」且與重算一致
- 每條 link 有標籤「<id> <斷面> S=x.xx% L=xx.xx m」，坡度 / 長度與重算一致（±0.01）、斷面文字一致
- 每個節點有符號 INSERT（位置 ±0.01 m），ATTRIB ID / TOP / INV 與 spec 一致（±0.005）；
  節點標籤群有「頂EL=」「底EL=」且與 spec 一致；縱斷面路徑節點的「樁號」與重算一致
- 既有排水有名稱標籤
- layout「平面設計圖」「集水區分析圖」各自：恰 1 個圖面 viewport；比例分母 = spec、
  ≤ `hydro/codes/taoyuan_drawing.yaml` 門檻（verified: false → WARNING）；指北針 block；
  「比例 1/N」文字（N = viewport 分母）；「圖例」；座標刻度 E、N 各 ≥ 2 且位置與 viewport 換算一致；
  圖簽 ATTRIB PROJECT / TITLE / DWG_NO / SCALE 非空、TITLE = 統一圖名、SCALE = 1/N
- 集水分區：Σ面積 = 基地面積（0.5%）、互不重疊、不超出基地；分區內面積文字（m²、ha）與重算一致
- 設施尺寸數量一覽表：每列（項目 / 規格 / 數量 / 單位）= 模型重算；集水分區面積表每列與合計 / 基地列一致
- 法規 yaml：spec.codes_path ≠ repo 預設 → error；重算一律讀預設 yaml（verify/codes_lock）
- 平面拓樸幾何：每條 link 有 C-STRM（新設）/ C-STRM-EXST（既有）線，兩端點 = from / to 節點 ±0.01 m；
  C-STRM-FLOW 箭頭方向與 (to − from) 內積 > 0、位置在管段 0.5 m 緩衝內（每條 link ≥ 1 個、不得有反向）；
  管段標籤插入點離自己管段最近，且距離 < 標籤避讓最大半徑；C-CTCH 折線與 spec 分區多邊形
  Hausdorff < 0.01 m（一對一）；C-PROP 與基地界線、既有排水折線與 spec 同樣比對
- viewport 可見性：各 layout 必要圖層不得在 vp.frozen_layers、圖層須 on / 未凍結 / plot；
  基地 bbox 必須落在 viewport 的 model 視窗內
- 物理合理性：管 / 箱涵覆土 = ground − (invert + 管高) ≥ 0（兩端節點）；節點 top_el ≤ ground_el；
  節點在基地內（outfall、existing_mh 除外）；surface 型滯洪池不得與建物重疊；既有排水溝底沿點序不得上升；
  有 existing_drains 但出流口距受納溝 > 2 m → error（hydraulics）
- 出圖可讀性（legibility_check）：兩個 paperspace layout 印出字高 ≥ 2.0 mm、文字框重疊 > 10% → error
  （viewport 凍結圖層的 model 文字不計）
- 管渠設計流量（`check_network_calc`，kinds/drainage_plan 的計算 verifier 呼叫）：上游集水分區面積加權 C
  的**獨立實作**（自建上游集合、yaml 直讀 C 與 Horner）重算每段 C / 承接面積 / Q，與 calc spec 的
  pipes[].design_c 及 result.json 比對（result.json / calc.md 的逐值 / 逐字對帳由 drainage_calc_check 執行）
"""

from __future__ import annotations

import argparse
import itertools
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import ezdxf
from shapely.geometry import LineString, Point, Polygon

from draftagent.drainage.derive import catchment_rows, profile_path, quantities
from draftagent.drainage.hydraulics import RECEIVE_MAX_DIST_M, check_elevations, receiving_point
from draftagent.drainage.model import NODE_KIND_NAME, DrainageNetworkSpec
from draftagent.drainage.network import (
    check_network,
    link_length,
    link_slope,
    site_area,
    stationing,
)
from draftagent.hydro.drawing_codes import DEFAULT_PATH, load_drawing_codes
from draftagent.render.drainage_plan import (
    CATCH_KEY,
    CATCH_LAYOUT,
    LABEL_GAP_MM,
    LABEL_RING_MM,
    LABEL_RINGS,
    NODE_BLOCK,
    PLAN_KEY,
    PLAN_LAYOUT,
    TEXT_MM,
)
from draftagent.render.sheet import est_text_width
from draftagent.schema.drainage import DEFAULT_CODES_PATH
from draftagent.schema.profile import parse_station
from draftagent.verify.base import VerifyResult, emit
from draftagent.verify.codes_lock import assert_default_codes

NAME = "drainage_plan_check"
INSUNITS_M = 6
VAL_TOL = 0.01  # 坡度 %、長度 m
EL_TOL = 0.005
POS_TOL = 0.01  # m
GEOM_TOL = 0.01  # m：線端點 / 分區界線 Hausdorff
ARROW_BUF = 0.5  # m：流向箭頭須在管段緩衝帶內
MIN_PRINT_MM = 2.0
OVERLAP_RATIO = 0.10
Q_REL_TOL = 1e-6
# 各 layout 必須可見的圖層
REQUIRED_LAYERS = {
    PLAN_LAYOUT: ("C-PROP", "C-STRM", "C-STRM-NODE", "C-STRM-TEXT", "C-STRM-FLOW"),
    CATCH_LAYOUT: ("C-PROP", "C-STRM", "C-STRM-NODE", "C-CTCH", "C-CTCH-TEXT", "C-CTCH-FLOW"),
}
SITE_EXEMPT_KINDS = ("outfall", "existing_mh")
AREA_TOL = 0.005  # 相對；與 area_check 相同，只能收緊
TICK_TOL_MM = 0.5
ROW_TOL_MM = 0.3
REQUIRED_ATTRIBS = ("PROJECT", "TITLE", "DWG_NO", "SCALE")
NORTH_BLOCK = "NORTH-ARROW"

_RE_LINK = re.compile(r"^(\S+) (\S+) S=(-?\d+\.\d+)% L=(\d+\.\d+) m$")
_RE_TOP = re.compile(r"^頂EL=(-?\d+\.\d+)$")
_RE_INV = re.compile(r"^底EL=(-?\d+\.\d+)$")
_RE_RWL = re.compile(r"^受納溝設計水位 EL=(-?\d+\.\d+)$")
_RE_STA = re.compile(r"^樁號 (\d+\+\d+\.\d+)$")
_RE_SCALE = re.compile(r"比例尺?\s*1\s*/\s*(\d+)")
_RE_E = re.compile(r"^E ([\d,]+(?:\.\d+)?)$")
_RE_N = re.compile(r"^N ([\d,]+(?:\.\d+)?)$")
_RE_M2 = re.compile(r"^(\d+\.\d+) m²$")
_RE_HA = re.compile(r"^(\d+\.\d+) ha$")


@dataclass
class _Text:
    layer: str
    text: str
    x: float
    y: float


def _texts(layout) -> list[_Text]:
    out = []
    for e in layout.query("TEXT"):
        _, p1, _ = e.get_placement()
        out.append(_Text(e.dxf.get("layer", "0").upper(), e.dxf.text, p1[0], p1[1]))
    return out


def _rows(texts: list[_Text], tol: float = ROW_TOL_MM) -> list[tuple[str, ...]]:
    rows: dict[float, list[_Text]] = defaultdict(list)
    for t in sorted(texts, key=lambda t: -t.y):
        key = next((k for k in rows if abs(k - t.y) <= tol), t.y)
        rows[key].append(t)
    return [
        tuple(t.text for t in sorted(v, key=lambda t: t.x))
        for _, v in sorted(rows.items(), key=lambda kv: -kv[0])
    ]


def _load_spec(res: VerifyResult, spec) -> DrainageNetworkSpec | None:
    if isinstance(spec, DrainageNetworkSpec):
        return spec
    try:
        return DrainageNetworkSpec.from_json(spec)
    except Exception as e:  # noqa: BLE001 - 回報給使用者
        res.error(f"spec 讀取失敗：{e}")
        return None


def check_drainage_plan(
    path: str | Path, spec: str | Path | DrainageNetworkSpec, codes_path: str | Path = DEFAULT_PATH
) -> VerifyResult:
    res = VerifyResult(name=NAME)
    try:
        doc = ezdxf.readfile(str(path))
    except (OSError, ezdxf.DXFStructureError) as e:
        res.error(f"無法讀取 DXF：{e}")
        return res
    sp = _load_spec(res, spec)
    if sp is None:
        return res
    assert_default_codes(sp.codes_path, DEFAULT_CODES_PATH, res, "codes_path")
    sp = sp.model_copy(update={"codes_path": DEFAULT_CODES_PATH})  # 一律讀 repo 預設 yaml

    ins = doc.header.get("$INSUNITS", 0)
    res.metrics["insunits"] = ins
    if ins != INSUNITS_M:
        res.error(f"$INSUNITS = {ins}，應為 6（m，TWD97 TM2 座標）")
    on_zero = Counter(
        e.dxftype() for lay in doc.layouts for e in lay if e.dxf.get("layer", "0") == "0"
    )
    if on_zero:
        res.error(f"0 層有 {sum(on_zero.values())} 個實體：{dict(on_zero)}")

    # ---------------- 拓樸
    rep = check_network(sp)
    for e in rep.errors:
        res.error(f"網路：{e}")
    for w in rep.warnings:
        res.warn(f"網路：{w}")
    res.metrics["outfall"] = rep.outfall

    # ---------------- 高程相容性（受納水位 / 滯洪池水位 / 迴水；drainage/hydraulics.py E1–E8）
    if not rep.errors:
        el = check_elevations(sp)
        for e in el.errors:
            res.error(f"高程：{e}")
        for w in el.warnings:
            res.warn(f"高程：{w}")
        res.metrics["elevations"] = el.metrics
    else:
        res.warn("網路拓樸有錯，未做高程相容性檢查（E1–E8）")

    _check_physical(res, sp)

    msp = doc.modelspace()
    mt = _texts(msp)
    _check_links(res, sp, mt)
    _check_nodes(res, sp, msp, mt)
    _check_plan_geometry(res, sp, msp, mt)
    for d in sp.existing_drains:
        if not any(t.text.startswith(f"{d.id} {d.name}") for t in mt):
            res.error(f"既有排水 {d.id} 缺名稱 / 斷面標籤（審查：基地附近排水系統現況）")

    # ---------------- 圖紙
    codes = load_drawing_codes(codes_path)
    if not codes.verified:
        res.warn(f"比例尺門檻 yaml verified: false — {codes.note}（{codes.path.name}）")
    for lname, key, want in (
        (PLAN_LAYOUT, PLAN_KEY, sp.scales.plan),
        (CATCH_LAYOUT, CATCH_KEY, sp.scales.catchment),
    ):
        if lname not in doc.layouts:
            res.error(f"缺少 layout「{lname}」")
            continue
        _check_layout(res, doc, doc.layouts.get(lname), codes.scale(key), want)

    _check_catchments(res, sp, mt)
    _check_tables(res, sp, doc)
    _check_visibility(res, sp, doc)
    _check_legibility(res, doc)
    return res


# ---------------------------------------------------------------- modelspace


def _check_links(res: VerifyResult, sp: DrainageNetworkSpec, mt: list[_Text]) -> None:
    labels: dict[str, list[re.Match]] = defaultdict(list)
    for t in mt:
        if t.layer == "C-STRM-TEXT" and (m := _RE_LINK.match(t.text)):
            labels[m.group(1)].append(m)
    out = {}
    for lk in sp.links:
        L = link_length(sp, lk)
        S = link_slope(sp, lk) * 100 if L > 0 else float("nan")
        out[lk.id] = f"S={S:.4f}% L={L:.4f} m"
        ms = labels.get(lk.id, [])
        if not ms:
            res.error(f"管段 {lk.id} 圖面缺標籤（編號 斷面 S=x.xx% L=xx.xx m）")
            continue
        if len(ms) > 1:
            res.error(f"管段 {lk.id} 有 {len(ms)} 個標籤")
        m = ms[0]
        if m.group(2) != lk.section.text():
            res.error(f"管段 {lk.id} 斷面文字「{m.group(2)}」≠ spec「{lk.section.text()}」")
        s_txt, l_txt = float(m.group(3)), float(m.group(4))
        if not abs(s_txt - S) <= VAL_TOL:
            res.error(f"管段 {lk.id} 坡度：圖面 {s_txt:.2f}% ≠ 重算 {S:.4f}%")
        if not abs(l_txt - L) <= VAL_TOL:
            res.error(f"管段 {lk.id} 長度：圖面 {l_txt:.2f} m ≠ 重算 {L:.4f} m")
    res.metrics["links"] = out


def _check_nodes(res: VerifyResult, sp: DrainageNetworkSpec, msp, mt: list[_Text]) -> None:
    inserts = {}
    for ins in msp.query("INSERT"):
        att = {a.dxf.tag.upper(): a.dxf.text for a in ins.attribs}
        if "ID" in att:
            inserts[att["ID"]] = (ins, att)
    try:
        path = profile_path(sp)
        st_of = dict(zip(path, stationing(sp, path), strict=True))
    except ValueError as e:
        res.warn(f"無法決定縱斷面路徑，未檢查樁號：{e}")
        st_of = {}
    h_line = 0.0
    for n in sp.nodes:
        if n.id not in inserts:
            res.error(f"節點 {n.id} 缺符號 INSERT（含 ATTRIB ID）")
        else:
            ins, att = inserts[n.id]
            if ins.dxf.name != NODE_BLOCK[n.kind]:
                res.error(
                    f"節點 {n.id} 符號 {ins.dxf.name} ≠ 種類 {n.kind} 應用 {NODE_BLOCK[n.kind]}"
                )
            p = ins.dxf.insert
            if math.hypot(p[0] - n.x, p[1] - n.y) > POS_TOL:
                res.error(
                    f"節點 {n.id} 符號位置 ({p[0]:.3f}, {p[1]:.3f}) ≠ spec ({n.x:.3f}, {n.y:.3f})"
                )
            for tag, want in (("TOP", n.top), ("INV", n.invert_el)):
                try:
                    got = float(att.get(tag, ""))
                except ValueError:
                    res.error(f"節點 {n.id} 屬性 {tag} 缺少或非數值：{att.get(tag)!r}")
                    continue
                if abs(got - want) > EL_TOL:
                    res.error(f"節點 {n.id} 屬性 {tag}={got:.2f} ≠ spec {want:.2f}")
        # 標籤群
        heads = [
            t
            for t in mt
            if t.layer == "C-STRM-TEXT" and t.text == f"{n.id} {NODE_KIND_NAME[n.kind]}"
        ]
        if not heads:
            res.error(f"節點 {n.id} 缺標籤「{n.id} {NODE_KIND_NAME[n.kind]}」")
            continue
        hd = heads[0]
        h_line = h_line or 2.5 * sp.scales.plan / 1000 * 1.5
        group = [
            t
            for t in mt
            if t.layer == "C-STRM-TEXT"
            and abs(t.x - hd.x) < 1e-3
            and hd.y - 8 * h_line < t.y < hd.y
        ]
        for rx, want, label in ((_RE_TOP, n.top, "頂EL"), (_RE_INV, n.invert_el, "底EL")):
            vals = [float(m.group(1)) for t in group if (m := rx.match(t.text))]
            if not vals:
                res.error(f"節點 {n.id} 標籤缺「{label}=」（審查：每段至少兩點標渠頂 / 渠底標高）")
            elif abs(vals[0] - want) > EL_TOL:
                res.error(f"節點 {n.id} 標籤 {label}={vals[0]:.2f} ≠ spec {want:.2f}")
        if n.kind == "outfall" and sp.existing_drains:
            rcv = receiving_point(sp, n.x, n.y)
            if rcv.design_wl is not None and rcv.distance_m <= RECEIVE_MAX_DIST_M:
                vals = [float(m.group(1)) for t in group if (m := _RE_RWL.match(t.text))]
                if not vals:
                    res.error(f"出流口 {n.id} 標籤缺「受納溝設計水位 EL=」（spec 有 design_water_depth_m）")
                elif abs(vals[0] - rcv.design_wl) > EL_TOL:
                    res.error(
                        f"出流口 {n.id} 標籤受納溝設計水位 {vals[0]:.2f} ≠ 重算 {rcv.design_wl:.2f}"
                    )
        if n.id in st_of:
            sts = [parse_station(m.group(1)) for t in group if (m := _RE_STA.match(t.text))]
            if not sts:
                res.error(f"節點 {n.id} 在縱斷面路徑上但缺「樁號」標示")
            elif abs(sts[0] - st_of[n.id]) > VAL_TOL:
                res.error(f"節點 {n.id} 樁號 {sts[0]:.2f} ≠ 重算 {st_of[n.id]:.2f}")
    if st_of:
        res.metrics["stations"] = {k: round(v, 3) for k, v in st_of.items()}


def _check_catchments(res: VerifyResult, sp: DrainageNetworkSpec, mt: list[_Text]) -> None:
    site = Polygon(sp.site_boundary)
    sa = site.area
    polys = {s.id: Polygon(s.polygon) for s in sp.subcatchments}
    tot = sum(p.area for p in polys.values())
    rel = abs(tot - sa) / sa
    res.metrics.update(
        site_area_m2=round(sa, 3),
        subcatchment_total_m2=round(tot, 3),
        subcatchment_diff_pct=round(rel * 100, 4),
    )
    if rel > AREA_TOL:
        res.error(
            f"集水分區面積合計 {tot:.2f} m² ≠ 基地 {sa:.2f} m²（差 {rel * 100:.3f}% > {AREA_TOL * 100}%）"
        )
    for (a, pa), (b, pb) in itertools.combinations(polys.items(), 2):
        ov = pa.intersection(pb).area
        if ov > 0.01:
            res.error(f"集水分區 {a} 與 {b} 重疊 {ov:.2f} m²")
    for sid, p in polys.items():
        out = p.difference(site).area
        if out > 0.01:
            res.error(f"集水分區 {sid} 超出基地 {out:.2f} m²")
    ctx = [t for t in mt if t.layer == "C-CTCH-TEXT"]
    for sid, p in polys.items():
        inside = [t for t in ctx if p.contains(Point(t.x, t.y))]
        if not any(t.text == sid for t in inside):
            res.error(f"集水分區 {sid} 圖面缺編號標示")
        m2 = [float(m.group(1)) for t in inside if (m := _RE_M2.match(t.text))]
        ha = [float(m.group(1)) for t in inside if (m := _RE_HA.match(t.text))]
        if not m2:
            res.error(f"集水分區 {sid} 圖面缺面積（m²）標示")
        elif abs(m2[0] - p.area) > VAL_TOL:
            res.error(f"集水分區 {sid} 面積文字 {m2[0]:.2f} m² ≠ 重算 {p.area:.2f} m²")
        if not ha:
            res.error(f"集水分區 {sid} 圖面缺面積（ha）標示")
        elif abs(ha[0] - p.area / 1e4) > 5e-5:
            res.error(f"集水分區 {sid} 面積文字 {ha[0]:.4f} ha ≠ 重算 {p.area / 1e4:.4f} ha")


# ---------------------------------------------------------------- paperspace


def _check_layout(res: VerifyResult, doc, psp, rule, want: int) -> None:
    name = psp.name
    vps = [v for v in psp.query("VIEWPORT") if v.dxf.id != 1]
    if len(vps) != 1:
        res.error(f"「{name}」應有 1 個圖面 viewport，實際 {len(vps)}")
        return
    vp = vps[0]
    scale = vp.dxf.height / vp.dxf.view_height  # 圖紙 mm / model m
    denom = 1000.0 / scale
    res.metrics[f"{name}_scale"] = f"1/{denom:g}"
    if abs(denom - want) > 0.5:
        res.error(f"「{name}」viewport 比例 1/{denom:g} ≠ spec 1/{want}")
    if denom > rule.max_denominator + 1e-6:
        res.error(
            f"「{name}」viewport 比例 1/{denom:g} 小於{rule.name}要求 S≧1/{rule.max_denominator}"
            f"（分母須 ≤ {rule.max_denominator}；{rule.source}）"
        )
    texts = _texts(psp)
    if not any(i.dxf.name == NORTH_BLOCK for i in psp.query("INSERT")):
        res.error(f"「{name}」缺指北針（block {NORTH_BLOCK}）")
    scales = [int(m.group(1)) for t in texts if (m := _RE_SCALE.search(t.text))]
    if not scales:
        res.error(f"「{name}」缺比例尺數值文字（例：「比例 1/{want}」）")
    for s in sorted(set(scales)):
        if abs(s - denom) > 0.5:
            res.error(f"「{name}」比例文字 1/{s} 與 viewport 1/{denom:g} 不符")
    if not any(t.text == "圖例" for t in texts):
        res.error(f"「{name}」缺圖例")

    c = vp.dxf.center
    vc = vp.dxf.view_center_point
    es = [
        (t, float(m.group(1).replace(",", "")))
        for t in texts
        if t.layer == "X-GRID" and (m := _RE_E.match(t.text))
    ]
    ns = [
        (t, float(m.group(1).replace(",", "")))
        for t in texts
        if t.layer == "X-GRID" and (m := _RE_N.match(t.text))
    ]
    res.metrics[f"{name}_ticks"] = {"E": len(es), "N": len(ns)}
    if len(es) < 2 or len(ns) < 2:
        res.error(
            f"「{name}」座標刻度不足（E {len(es)}、N {len(ns)}，各需 ≥ 2；審查常見缺失：缺經緯線 / 座標刻度）"
        )
    for t, v in es:
        x = c[0] + (v - vc[0]) * scale
        if abs(t.x - x) > TICK_TOL_MM:
            res.error(f"「{name}」刻度 {t.text} 位置 x={t.x:.2f} mm ≠ viewport 換算 {x:.2f} mm")
    for t, v in ns:
        y = c[1] + (v - vc[1]) * scale
        if abs(t.y - y) > TICK_TOL_MM:
            res.error(f"「{name}」刻度 {t.text} 位置 y={t.y:.2f} mm ≠ viewport 換算 {y:.2f} mm")

    tb = next(
        (
            {a.dxf.tag.upper(): a.dxf.text for a in i.attribs}
            for i in psp.query("INSERT")
            if i.attribs
        ),
        None,
    )
    if tb is None:
        res.error(f"「{name}」找不到圖簽（含 ATTRIB 的 INSERT）")
        return
    for tag in REQUIRED_ATTRIBS:
        if not tb.get(tag, "").strip():
            res.error(f"「{name}」圖簽屬性 {tag} 空白或缺少")
    if tb.get("TITLE") and tb["TITLE"] != rule.name:
        res.error(f"「{name}」圖簽圖名「{tb['TITLE']}」≠ 統一圖名「{rule.name}」")
    if tb.get("SCALE") and tb["SCALE"].replace(" ", "") != f"1/{denom:g}":
        res.error(f"「{name}」圖簽比例「{tb['SCALE']}」≠ viewport 1/{denom:g}")


def _check_tables(res: VerifyResult, sp: DrainageNetworkSpec, doc) -> None:
    if PLAN_LAYOUT in doc.layouts:
        rows = {
            r
            for r in _rows([t for t in _texts(doc.layouts.get(PLAN_LAYOUT)) if t.layer == "C-TABL"])
            if len(r) == 4
        }
        want = {r.cells() for r in quantities(sp)}
        header = ("項目", "規格", "數量", "單位")
        if header not in rows:
            res.error("平面設計圖缺「設施尺寸數量一覽表」（欄：項目 / 規格 / 數量 / 單位）")
        for r in sorted(want - rows):
            res.error(f"數量一覽表缺少或數量不符：{' / '.join(r)}（模型重算）")
        for r in sorted(rows - want - {header}):
            res.error(f"數量一覽表有模型沒有的列：{' / '.join(r)}")
    if CATCH_LAYOUT in doc.layouts:
        rows = [
            r
            for r in _rows(
                [t for t in _texts(doc.layouts.get(CATCH_LAYOUT)) if t.layer == "C-TABL"]
            )
            if len(r) >= 4
        ]
        crow = catchment_rows(sp)
        for cr in crow:
            if cr.cells() not in rows:
                res.error(f"集水分區面積表缺少或不符：{' / '.join(cr.cells())}（重算）")
        tot = sum(c.area_m2 for c in crow)
        sa = site_area(sp)
        for label, val in (("合計", tot), ("基地", sa)):
            hit = [r for r in rows if r[0] == label]
            if not hit:
                res.error(f"集水分區面積表缺「{label}」列")
                continue
            nums = []
            for cell in hit[0][1:]:
                try:
                    nums.append(float(cell))
                except ValueError:
                    pass
            if not nums:
                res.error(f"集水分區面積表「{label}」列無法解析：{hit[0]}")
                continue
            got = nums[0]  # 第一個數值欄 = 面積 m²
            if abs(got - val) > VAL_TOL:
                res.error(f"集水分區面積表「{label}」{got:.2f} m² ≠ 重算 {val:.2f} m²")


# ---------------------------------------------------------------- 物理合理性


def _check_physical(res: VerifyResult, sp: DrainageNetworkSpec) -> None:
    site = Polygon(sp.site_boundary)
    for lk in sp.links:
        if lk.existing or lk.section.type == "u_ditch":
            continue
        hgt = lk.section.d if lk.section.type == "pipe" else lk.section.h
        for nid in (lk.from_, lk.to):
            n = sp.node(nid)
            cover = n.ground_el - (n.invert_el + hgt)
            if cover < -EL_TOL:
                res.error(
                    f"覆土不足：{lk.id} 於 {nid} 管頂 EL={n.invert_el + hgt:.2f}"
                    f"（底 {n.invert_el:.2f} + {lk.section.text()}）高於地面 EL={n.ground_el:.2f}"
                    f"（覆土 {cover:.2f} m < 0）"
                )
    for n in sp.nodes:
        if n.top_el is not None and n.top_el > n.ground_el + EL_TOL:
            res.error(f"節點 {n.id} 渠頂 EL={n.top_el:.2f} 高於地面 EL={n.ground_el:.2f}")
        if n.kind in SITE_EXEMPT_KINDS:
            continue
        if not site.buffer(POS_TOL).contains(Point(n.x, n.y)):
            res.error(
                f"節點 {n.id}（{NODE_KIND_NAME[n.kind]}）({n.x:.2f}, {n.y:.2f}) 不在基地範圍內"
            )
    st = sp.storage
    if st is not None and st.type == "surface":
        pond = Polygon(st.polygon)
        for i, b in enumerate(sp.building_footprints):
            ov = pond.intersection(Polygon(b)).area
            if ov > 0.01:
                res.error(f"地面滯洪池 {st.id} 與建物 building_footprints[{i}] 重疊 {ov:.2f} m²")
        out = pond.difference(site).area
        if out > 0.01:
            res.error(f"地面滯洪池 {st.id} 超出基地 {out:.2f} m²")
    for d in sp.existing_drains:
        inv = [(i, p.invert_el) for i, p in enumerate(d.points) if p.invert_el is not None]
        for (i, a), (j, b) in zip(inv, inv[1:], strict=False):
            if b > a + EL_TOL:
                res.error(
                    f"既有排水 {d.id} 逆坡：點 {i} 溝底 EL={a:.2f} → 點 {j} EL={b:.2f}"
                    "（點須依流向排序，溝底不得上升）"
                )


# ---------------------------------------------------------------- 平面拓樸幾何


def _seg_entities(msp, layers: tuple[str, ...]) -> list[tuple[str, LineString]]:
    out = []
    for e in msp.query("LINE LWPOLYLINE"):
        lay = e.dxf.get("layer", "0").upper()
        if lay not in layers:
            continue
        if e.dxftype() == "LINE":
            s, t = e.dxf.start, e.dxf.end
            out.append((lay, LineString([(s[0], s[1]), (t[0], t[1])])))
        else:
            pts = [(p[0], p[1]) for p in e.get_points("xy")]
            if e.closed:
                pts.append(pts[0])
            if len(pts) >= 2:
                out.append((lay, LineString(pts)))
    return out


def _ring_entities(msp, layer: str) -> list[LineString]:
    out = []
    for e in msp.query("LWPOLYLINE"):
        if e.dxf.get("layer", "0").upper() != layer:
            continue
        pts = [(p[0], p[1]) for p in e.get_points("xy")]
        if len(pts) >= 3:
            out.append(LineString([*pts, pts[0]]))
    return out


def _ring(coords) -> LineString:
    pts = [tuple(c) for c in coords]
    return LineString([*pts, pts[0]])


def _arrows(msp) -> list[tuple[Point, tuple[float, float]]]:
    """C-STRM-FLOW 三角箭頭 → (重心, 方向單位向量)；方向 = 尖端 − 底邊中點（尖端 = 最短邊的對角）。"""
    out = []
    for e in msp.query("LWPOLYLINE"):
        if e.dxf.get("layer", "0").upper() != "C-STRM-FLOW":
            continue
        pts = [(p[0], p[1]) for p in e.get_points("xy")]
        if len(pts) != 3:
            continue
        sides = [math.dist(pts[(i + 1) % 3], pts[(i + 2) % 3]) for i in range(3)]
        i = min(range(3), key=sides.__getitem__)
        tip = pts[i]
        b0, b1 = pts[(i + 1) % 3], pts[(i + 2) % 3]
        bx, by = (b0[0] + b1[0]) / 2, (b0[1] + b1[1]) / 2
        L = math.hypot(tip[0] - bx, tip[1] - by)
        if L <= 0:
            continue
        c = Point(sum(p[0] for p in pts) / 3, sum(p[1] for p in pts) / 3)
        out.append((c, ((tip[0] - bx) / L, (tip[1] - by) / L)))
    return out


def _check_plan_geometry(
    res: VerifyResult, sp: DrainageNetworkSpec, msp, mt: list[_Text]
) -> None:
    k = sp.scales.plan / 1000.0
    segs = _seg_entities(msp, ("C-STRM", "C-STRM-EXST"))
    # 線路：每條 link 一條線，端點 = from / to
    link_geom: dict[str, LineString] = {}
    used: set[int] = set()
    for lk in sp.links:
        a, b = sp.node(lk.from_), sp.node(lk.to)
        want = "C-STRM-EXST" if lk.existing else "C-STRM"
        link_geom[lk.id] = LineString([(a.x, a.y), (b.x, b.y)])
        hit = None
        for i, (lay, g) in enumerate(segs):
            if i in used or lay != want or len(g.coords) != 2:
                continue
            p, q = g.coords[0], g.coords[-1]
            fwd = math.dist(p, (a.x, a.y)) <= GEOM_TOL and math.dist(q, (b.x, b.y)) <= GEOM_TOL
            rev = math.dist(q, (a.x, a.y)) <= GEOM_TOL and math.dist(p, (b.x, b.y)) <= GEOM_TOL
            if fwd or rev:
                hit = i
                break
        if hit is None:
            res.error(
                f"管段 {lk.id} 圖面缺 {want} 線（端點須 = {lk.from_} ({a.x:.2f}, {a.y:.2f}) / "
                f"{lk.to} ({b.x:.2f}, {b.y:.2f}) ±{GEOM_TOL} m）"
            )
        else:
            used.add(hit)
    drain_geom: dict[str, list[LineString]] = {}
    for d in sp.existing_drains:
        line = LineString([(p.x, p.y) for p in d.points])
        drain_geom[d.id] = [
            LineString([(p.x, p.y), (q.x, q.y)]) for p, q in zip(d.points, d.points[1:], strict=False)
        ]
        if not any(
            lay == "C-STRM-EXST" and g.hausdorff_distance(line) < GEOM_TOL for lay, g in segs
        ):
            res.error(f"既有排水 {d.id} 圖面缺 C-STRM-EXST 折線（或與 spec 點位不符 ≥ {GEOM_TOL} m）")

    # 流向箭頭：指派給最近的管段 / 既有排水段
    targets: list[tuple[str, LineString]] = list(link_geom.items())
    for did, gs in drain_geom.items():
        targets += [(f"{did}#{i}", g) for i, g in enumerate(gs)]
    by_target: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for c, u in _arrows(msp):
        tid, g = min(targets, key=lambda t: t[1].distance(c))
        if g.distance(c) > ARROW_BUF:
            res.error(f"流向箭頭 ({c.x:.2f}, {c.y:.2f}) 不在任何管段 {ARROW_BUF} m 緩衝帶內")
            continue
        (x0, y0), (x1, y1) = g.coords[0], g.coords[-1]
        by_target[tid].append((u[0] * (x1 - x0) + u[1] * (y1 - y0), 0.0))
    for tid, _g in targets:
        dots = [d for d, _ in by_target.get(tid, [])]
        name = f"管段 {tid}" if tid in link_geom else f"既有排水 {tid.split('#')[0]}"
        if not dots:
            res.error(f"{name} 缺流向箭頭（C-STRM-FLOW，{ARROW_BUF} m 緩衝帶內）")
        elif any(d <= 0 for d in dots):
            res.error(f"{name} 流向箭頭方向與流向（from → to / 點序）相反")

    # 管段標籤：離自己管段最近，且在避讓半徑內
    max_r0 = (LABEL_GAP_MM + (LABEL_RINGS - 1) * LABEL_RING_MM) * k
    for t in mt:
        if t.layer != "C-STRM-TEXT" or not (m := _RE_LINK.match(t.text)):
            continue
        lid = m.group(1)
        if lid not in link_geom:
            res.error(f"管段標籤「{t.text}」的 {lid} 不是 spec 的管段")
            continue
        p = Point(t.x, t.y)
        own = link_geom[lid].distance(p)
        near_id, near_g = min(link_geom.items(), key=lambda kv: kv[1].distance(p))
        if near_id != lid and near_g.distance(p) < own - 1e-6:
            res.error(
                f"管段 {lid} 標籤離管段 {near_id} 較近（{near_g.distance(p):.2f} m < 自己 {own:.2f} m）："
                "標籤位置錯誤"
            )
        lim = max_r0 + est_text_width(t.text, TEXT_MM * k)
        if own > lim:
            res.error(f"管段 {lid} 標籤距管段 {own:.2f} m > 標籤避讓最大半徑 {lim:.2f} m")

    # 集水分區界線 / 基地界線
    rings = _ring_entities(msp, "C-CTCH")
    free = list(range(len(rings)))
    for s in sp.subcatchments:
        want = _ring(s.polygon)
        hit = next((i for i in free if rings[i].hausdorff_distance(want) < GEOM_TOL), None)
        if hit is None:
            best = min((rings[i].hausdorff_distance(want) for i in free), default=float("inf"))
            res.error(
                f"集水分區 {s.id} 圖面 C-CTCH 界線與 spec 多邊形不符"
                f"（最近 Hausdorff {best:.3f} m ≥ {GEOM_TOL} m）"
            )
        else:
            free.remove(hit)
    for i in free:
        res.error(f"C-CTCH 有 spec 沒有的集水分區界線（{len(rings[i].coords) - 1} 點）")
    site = _ring(sp.site_boundary)
    if not any(r.hausdorff_distance(site) < GEOM_TOL for r in _ring_entities(msp, "C-PROP")):
        res.error(f"圖面 C-PROP 基地界線與 spec site_boundary 不符（Hausdorff ≥ {GEOM_TOL} m）")


# ---------------------------------------------------------------- viewport 可見性


def _check_visibility(res: VerifyResult, sp: DrainageNetworkSpec, doc) -> None:
    sx = [p[0] for p in sp.site_boundary]
    sy = [p[1] for p in sp.site_boundary]
    for lname, layers in REQUIRED_LAYERS.items():
        if lname not in doc.layouts:
            continue
        for lay_name in layers:
            if lay_name not in doc.layers:
                res.error(f"「{lname}」必要圖層 {lay_name} 不存在")
                continue
            lay = doc.layers.get(lay_name)
            if not lay.is_on() or lay.is_frozen():
                res.error(f"圖層 {lay_name} 關閉或凍結（「{lname}」必要圖層，出圖會空白）")
            if not bool(lay.dxf.get("plot", 1)):
                res.error(f"圖層 {lay_name} 設為不出圖（plot=0；「{lname}」必要圖層）")
        vps = [v for v in doc.layouts.get(lname).query("VIEWPORT") if v.dxf.id != 1]
        for vp in vps:
            frozen = {n.upper() for n in (vp.frozen_layers or [])}
            bad = [n for n in layers if n.upper() in frozen]
            if bad:
                res.error(f"「{lname}」viewport 凍結必要圖層 {bad}（出圖看不到）")
            scale = vp.dxf.height / vp.dxf.view_height
            vc = vp.dxf.view_center_point
            hw, hh = vp.dxf.width / scale / 2, vp.dxf.view_height / 2
            win = (vc[0] - hw, vc[1] - hh, vc[0] + hw, vc[1] + hh)
            if not (
                win[0] <= min(sx) and win[1] <= min(sy) and max(sx) <= win[2] and max(sy) <= win[3]
            ):
                res.error(
                    f"「{lname}」viewport model 視窗 E {win[0]:.1f}–{win[2]:.1f}、N {win[1]:.1f}–{win[3]:.1f}"
                    f" 未完整涵蓋基地 E {min(sx):.1f}–{max(sx):.1f}、N {min(sy):.1f}–{max(sy):.1f}"
                )


def _check_legibility(res: VerifyResult, doc) -> None:
    from draftagent.verify.legibility_check import check_text_heights, check_text_overlaps

    for lname in (PLAN_LAYOUT, CATCH_LAYOUT):
        if lname not in doc.layouts:
            continue
        for m in check_text_heights(doc, lname, min_mm=MIN_PRINT_MM):
            res.error(f"「{lname}」{m}")
        errs, warns = check_text_overlaps(doc, lname, overlap_ratio=OVERLAP_RATIO)
        for e in errs:
            res.error(f"「{lname}」{e}")
        for w in warns:
            res.warn(f"「{lname}」{w}")


# ---------------------------------------------------------------- 管渠設計流量（獨立實作）


def independent_pipe_flows(sp: DrainageNetworkSpec) -> dict[str, dict[str, float]]:
    """不經 network / hydro.taoyuan：自建上游集合、yaml 直讀 C 與 Horner，重算管渠設計流量。

    納入：新設、非抽水逆坡（S > 0）、不在滯洪設施出流端以下的 link。回傳 {link: {C, A_m2, I, Q}}。
    """
    import yaml

    codes = yaml.safe_load(Path(DEFAULT_CODES_PATH).read_text(encoding="utf-8"))
    cls = codes["runoff_coefficients"]["classes"]
    dc = codes["design_criteria"]
    nodes = {n.id: n for n in sp.nodes}
    edges = [(lk.from_, lk.to) for lk in sp.links]
    if sp.storage is not None:
        edges.append((sp.storage.inlet_node, sp.storage.outlet_node))
    preds: dict[str, set[str]] = defaultdict(set)
    succs: dict[str, set[str]] = defaultdict(set)
    for a, b in edges:
        preds[b].add(a)
        succs[a].add(b)

    def closure(start: str, nbr) -> set[str]:
        seen, stack = {start}, [start]
        while stack:
            for m in nbr[stack.pop()]:
                if m not in seen:
                    seen.add(m)
                    stack.append(m)
        return seen

    below = closure(sp.storage.outlet_node, succs) if sp.storage is not None else set()
    a_at: dict[str, float] = defaultdict(float)
    ca_at: dict[str, float] = defaultdict(float)
    for s in sp.subcatchments:
        c = float(s.c_override) if s.c_override is not None else float(cls[s.landuse]["median"])
        area = Polygon(s.polygon).area
        a_at[s.outlet_node] += area
        ca_at[s.outlet_node] += c * area
    station = sp.calc.station if sp.calc is not None and sp.calc.station else None
    stations = codes["horner"]["stations"]
    if station is None:
        station = next(k for k, v in stations.items() if sp.project.district in (v.get("districts") or []))
    T = int(dc["return_period_yr"]["collection"])
    t = sp.calc.tc_min if sp.calc is not None and sp.calc.tc_min is not None else float(dc["tc_min_recommended"])
    hp = stations[station][f"T{T}"]
    intensity = hp["a"] / (t + hp["b"]) ** hp["c"]
    out = {}
    for lk in sp.links:
        a, b = nodes[lk.from_], nodes[lk.to]
        if lk.existing or lk.from_ in below or a.invert_el - b.invert_el <= 0:
            continue
        ups = closure(lk.from_, preds)
        A = sum(a_at[u] for u in ups)
        CA = sum(ca_at[u] for u in ups)
        if A <= 0:
            continue
        out[lk.id] = {"C": CA / A, "A_m2": A, "I": intensity, "Q": CA / 1e4 * intensity / 360.0}
    return out


def check_network_calc(
    res: VerifyResult,
    sp: DrainageNetworkSpec,
    calc,
    result_path: str | Path,
) -> None:
    """drainage-plan 的流出抑制計算：網路獨立不變量 + 管渠設計流量的獨立實作交叉驗證。

    `calc` 為由 spec 重新衍生（預設 yaml）的 TaoyuanSiteDrainageSpec（pipes[].design_c =
    上游集水分區加權 C）。貯集 / 排放 / 管渠判定與 result.json / calc.md 逐值 / 逐字對帳
    由 drainage_calc_check 對同一份 calc spec 執行（kinds/drainage_plan.verify_calc）。
    """
    import json

    # ---- 獨立不變量：calc 的面積 / 分區 = spec 幾何
    sa = Polygon(sp.site_boundary).area
    if not math.isclose(calc.site_area_m2, sa, rel_tol=1e-9, abs_tol=1e-9):
        res.error(f"計算書基地面積 {calc.site_area_m2:.3f} m² ≠ 基地多邊形面積 {sa:.3f} m²")
    want = [
        (s.id, Polygon(s.polygon).area, s.landuse, s.c_override) for s in sp.subcatchments
    ]
    got = [(s.name, s.area_m2, s.landuse, s.c_override) for s in calc.subareas]
    if len(got) != len(want) or any(
        g[0] != w[0] or g[2] != w[2] or g[3] != w[3] or not math.isclose(g[1], w[1], rel_tol=1e-9)
        for g, w in zip(got, want, strict=False)
    ):
        res.error(f"計算書分區 {got} ≠ spec 集水分區（id、polygon 面積、landuse、c_override）{want}")

    try:
        given = json.loads(Path(result_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        res.error(f"result.json 無法讀取：{e}")
        return

    ind = independent_pipe_flows(sp)
    # ---- calc spec 的 design_c / 承接面積：獨立實作重算
    cp = {p.name: p for p in calc.pipes}
    if set(ind) != set(cp):
        res.error(f"計算書管渠 {sorted(cp)} ≠ 獨立重算應檢核的管渠 {sorted(ind)}")
    for lid, f in ind.items():
        p = cp.get(lid)
        if p is None:
            continue
        if p.design_c is None or not math.isclose(p.design_c, f["C"], rel_tol=Q_REL_TOL, abs_tol=1e-12):
            res.error(f"管渠 {lid} design_c：計算書 {p.design_c} ≠ 獨立重算（上游加權）{f['C']:.6g}")
        if not math.isclose(p.design_area_m2, f["A_m2"], rel_tol=Q_REL_TOL, abs_tol=1e-12):
            res.error(f"管渠 {lid} 承接面積：計算書 {p.design_area_m2:.6g} ≠ 獨立重算 {f['A_m2']:.6g}")

    # ---- 管渠設計流量：獨立實作重算 vs result.json
    rp = {p.get("name"): p for p in given.get("pipes") or [] if isinstance(p, dict)}
    if set(ind) != set(rp):
        res.error(f"result.json 管渠 {sorted(rp)} ≠ 獨立重算應檢核的管渠 {sorted(ind)}")
    for lid, f in ind.items():
        p = rp.get(lid)
        if p is None:
            continue
        try:
            q = float(p["Q_design_cms"]["value"])
            c = float(p["C_design"]["value"])
            area = float(p["design_area_m2"])
            cap = float(p["capacity_cms"]["value"])
        except (KeyError, TypeError, ValueError):
            res.error(f"result.json 管渠 {lid} 缺 Q_design_cms / C_design / design_area_m2 / capacity_cms")
            continue
        for label, g, w in (("C", c, f["C"]), ("承接面積", area, f["A_m2"]), ("設計流量 Q", q, f["Q"])):
            if not math.isclose(g, w, rel_tol=Q_REL_TOL, abs_tol=1e-12):
                res.error(f"管渠 {lid} {label}：result.json {g:.6g} ≠ 獨立重算（上游加權）{w:.6g}")
        if f["Q"] > cap * (1 + 1e-9):
            res.error(f"管渠 {lid}：上游加權設計流量 {f['Q']:.5f} m³/s > 通水容量 {cap:.5f} m³/s")
    res.metrics["pipe_flows_independent"] = {
        k: f"C={v['C']:.4f} A={v['A_m2']:.1f} Q={v['Q']:.5f}" for k, v in ind.items()
    }



def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        prog="python -m draftagent.verify.drainage_plan_check",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("dxf")
    ap.add_argument("--spec", required=True, help="DrainageNetworkSpec JSON")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    emit(check_drainage_plan(a.dxf, a.spec), a.json)


if __name__ == "__main__":
    main()
