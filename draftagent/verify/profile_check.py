"""排水系統縱斷面圖 DXF verifier。純函式，不得呼叫 LLM。

檢查項目（從 DXF 讀回，--spec 時另對照 ProfileSpec）
- 檔案可讀；$INSUNITS = 4（mm）；任何 layout 的 0 層無實體；DIMENSION 若有則 dimlfac 必須 1
- 比例文字：圖面須有「水平 1/N」「垂直 1/M」文字；N ≤ 縱斷面比例分母上限
  （`hydro/codes/taoyuan_drawing.yaml` drainage_profile；verified: false → WARNING）
- 實際幾何比例：樁號列文字的 x 間距 / 樁號差 = 1000/N；高程尺數字 y 間距 / 高程差 = 1000/M（±0.5%）
- 圖簽：INSERT 的 ATTRIB PROJECT / TITLE / DWG_NO / SCALE 不可空
- 表格帶（H-TABL，列名見 schema.profile.PROFILE_TABLE_ROWS）：樁號遞增；
  --spec 時每個節點的樁號、地面、溝底（及有給的溝頂、道路中心樁）高程文字依序出現（容差 0.005）；
  坡度欄 = 由 spec 溝底高程與樁號差重算（±0.01%）；斷面文字一致
- 順坡：下游溝底不得高於上游（segment.pump=true 除外）→ error；坡度 0 → warning
- spec.levels 中 label 含「進水管」「放流管」者，圖上必須有「<label> EL=xx.xx」文字（審查常見缺失）；
  其他 levels 缺文字 → warning
- spec.structures（滯洪池 / 井 / 受納側溝）：每個構造物的「<label>頂 EL=」「<label>底 EL=」與每個 mark
  「<mark> EL=」文字必須存在且數值與 spec 一致（±0.005）→ 否則 error；滯洪池水位 mark 須介於池底與池頂

用法：python -m draftagent.verify.profile_check <profile.dxf> [--spec spec.json] [--json]
"""

from __future__ import annotations

import argparse
import itertools
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import ezdxf

from draftagent.hydro.drawing_codes import DEFAULT_PATH, load_drawing_codes
from draftagent.schema.profile import (
    NODE_ROWS,
    PROFILE_SCALE_KEY,
    PROFILE_TABLE_ROWS,
    WATER_LEVEL_KINDS,
    ProfileSpec,
    parse_station,
)
from draftagent.verify.base import VerifyResult, emit
from draftagent.verify.dimtext import displayed_text, forbidden_vars, matches_number
from draftagent.verify.textbox import est_text_box

NAME = "profile_check"
INSUNITS_MM = 4
EL_TOL = 0.005
SLOPE_TOL = 0.01  # %
SCALE_RTOL = 0.005
ROW_YTOL = 0.5  # mm：值文字與列名同列的 y 容差
DIM_TOL = 0.5  # mm
GEOM_TOL = 0.5  # mm（圖紙）：幾何頂點 / 刻度 / 樁號欄位置
LEVEL_MARK_HALF = 5.0  # 特殊高程短標線半長 mm（圖面契約）
LEVEL_X_TOL = 5.0  # 特殊高程文字距短標線的最大水平距離 mm
TOP_ABOVE_GROUND_TOL = 0.02  # m：溝頂可高出地面的容許值
SECTION_H_TOL = 0.01  # m：U 溝 溝頂 − 溝底 與 size_m 的容許差
MIN_TEXT_MM = 2.0
REQUIRED_LEVEL_WORDS = ("進水管", "放流管")
REQUIRED_ATTRIBS = ("PROJECT", "TITLE", "DWG_NO", "SCALE")
_RE_H = re.compile(r"水平\s*1\s*/\s*(\d+)")
_RE_V = re.compile(r"垂直\s*1\s*/\s*(\d+)")
_RE_NUM = re.compile(r"^-?\d+\.\d{2}$")
_RE_EL = re.compile(r"EL\s*=\s*(-?\d+(?:\.\d+)?)")


@dataclass
class _Text:
    layer: str
    text: str
    x: float
    y: float


def _texts(doc) -> list[_Text]:
    out = []
    for layout in doc.layouts:
        for e in layout:
            if e.dxftype() != "TEXT":
                continue
            _, p1, _ = e.get_placement()
            out.append(_Text(e.dxf.get("layer", "0").upper(), e.dxf.text, p1[0], p1[1]))
    return out


def _attribs(doc) -> list[dict[str, str]]:
    out = []
    for layout in doc.layouts:
        for ins in layout.query("INSERT"):
            if ins.attribs:
                out.append({a.dxf.tag.upper(): a.dxf.text for a in ins.attribs})
    return out


def _num(s: str) -> float | None:
    try:
        return float(s.strip().rstrip("%"))
    except ValueError:
        return None


def check_profile(
    path: str | Path, spec_path: str | Path | None = None, codes_path: str | Path = DEFAULT_PATH
) -> VerifyResult:
    res = VerifyResult(name=NAME)
    try:
        doc = ezdxf.readfile(str(path))
    except (OSError, ezdxf.DXFStructureError) as e:
        res.error(f"無法讀取 DXF：{e}")
        return res

    insunits = doc.header.get("$INSUNITS", 0)
    res.metrics["insunits"] = insunits
    if insunits != INSUNITS_MM:
        res.error(f"$INSUNITS = {insunits}，應為 4（mm，圖紙座標）")

    on_zero = Counter(
        e.dxftype() for layout in doc.layouts for e in layout if e.dxf.get("layer", "0") == "0"
    )
    if on_zero:
        res.error(f"0 層有 {sum(on_zero.values())} 個實體：{dict(on_zero)}")

    _check_dimensions(res, doc)

    texts = _texts(doc)
    attribs = _attribs(doc)
    all_strings = [t.text for t in texts] + [v for a in attribs for v in a.values()]

    # ---------------- 比例
    codes = load_drawing_codes(codes_path)
    rule = codes.scale(PROFILE_SCALE_KEY)
    if not codes.verified:
        res.warn(f"比例尺門檻 yaml verified: false — {codes.note}（{codes.path.name}）")
    h_found = sorted({int(m.group(1)) for s in all_strings for m in _RE_H.finditer(s)})
    v_found = sorted({int(m.group(1)) for s in all_strings for m in _RE_V.finditer(s)})
    res.metrics["scale_h"] = [f"1/{d}" for d in h_found]
    res.metrics["scale_v"] = [f"1/{d}" for d in v_found]
    res.metrics["scale_h_limit"] = f"S≧1/{rule.max_denominator}（{rule.source}）"
    if not h_found:
        res.error("圖面找不到水平比例文字（例：「水平 1/200」）")
    if not v_found:
        res.error("圖面找不到垂直比例文字（例：「垂直 1/50」）")
    if len(h_found) > 1 or len(v_found) > 1:
        res.error(f"比例文字不一致：水平 {h_found}、垂直 {v_found}")
    for d in h_found:
        if d > rule.max_denominator:
            res.error(
                f"水平比例 1/{d} 小於{rule.name}要求 S≧1/{rule.max_denominator}"
                f"（分母須 ≤ {rule.max_denominator}；{rule.source}）"
            )

    # ---------------- 圖簽
    tb = next((a for a in attribs if "TITLE" in a or "PROJECT" in a), None)
    if tb is None:
        res.error("找不到圖簽（含 PROJECT / TITLE 屬性的 INSERT）")
    else:
        for tag in REQUIRED_ATTRIBS:
            if not tb.get(tag, "").strip():
                res.error(f"圖簽屬性 {tag} 空白或缺少")
        res.metrics["title_block"] = {k: tb.get(k, "") for k in REQUIRED_ATTRIBS}

    # ---------------- 表格帶
    tabl = [t for t in texts if t.layer == "H-TABL"]
    label_of = {v: k for k, v in PROFILE_TABLE_ROWS.items()}
    labels = {label_of[t.text]: t for t in tabl if t.text in label_of}
    rows: dict[str, list[_Text]] = {}
    for key in PROFILE_TABLE_ROWS:
        if key not in labels:
            res.error(f"表格帶缺少列「{PROFILE_TABLE_ROWS[key]}」")
            continue
        ly = labels[key].y
        rows[key] = sorted(
            (t for t in tabl if t.text not in label_of and abs(t.y - ly) <= ROW_YTOL),
            key=lambda t: t.x,
        )

    stations: list[float] = []
    if "station" in rows:
        try:
            stations = [parse_station(t.text) for t in rows["station"]]
        except ValueError:
            res.error(f"樁號文字無法解析：{[t.text for t in rows['station']]}")
        res.metrics["stations"] = stations
        if any(b <= a for a, b in itertools.pairwise(stations)):
            res.error(f"樁號未遞增：{stations}")
        if len(stations) < 2:
            res.error("樁號列少於 2 個樁號")
        elif len(h_found) == 1:
            xs = [t.x for t in rows["station"]]
            k = (xs[-1] - xs[0]) / (stations[-1] - stations[0])
            want = 1000 / h_found[0]
            res.metrics["h_mm_per_m"] = round(k, 4)
            if abs(k - want) > want * SCALE_RTOL:
                res.error(f"水平幾何比例 {k:.4f} mm/m 與比例文字 1/{h_found[0]}（{want:.4f}）不符")

    ruler = [(t.y, float(t.text)) for t in texts if t.layer == "H-GRID" and _RE_NUM.match(t.text)]
    if len(ruler) < 2:
        res.error("找不到高程尺刻度數字（H-GRID）")
    elif len(v_found) == 1:
        ruler.sort(key=lambda p: p[1])
        k = (ruler[-1][0] - ruler[0][0]) / (ruler[-1][1] - ruler[0][1])
        want = 1000 / v_found[0]
        res.metrics["v_mm_per_m"] = round(k, 4)
        if abs(k - want) > want * SCALE_RTOL:
            res.error(f"垂直幾何比例 {k:.4f} mm/m 與比例文字 1/{v_found[0]}（{want:.4f}）不符")

    # ---------------- spec 對照
    spec = None
    if spec_path is not None:
        try:
            spec = ProfileSpec.from_json(spec_path)
        except Exception as e:  # noqa: BLE001 - 回報給使用者即可
            res.error(f"spec 讀取失敗：{e}")
    if spec is not None:
        _compare_spec(res, spec, rows, texts, h_found, v_found)
        _check_structures(res, spec, texts)
        _check_physical(res, spec)
    elif "invert" in rows:  # 無 spec：用圖面溝底列檢查順坡
        inv = [_num(t.text) for t in rows["invert"]]
        if None not in inv:
            for i, (a, b) in enumerate(itertools.pairwise(inv)):
                if b > a:
                    res.error(
                        f"第 {i + 1} 段逆坡：溝底 {a:.2f} → {b:.2f}（無 spec，無法確認是否為抽水段）"
                    )
    check_text_anchors(res, doc.modelspace())
    _check_geometry(res, doc, spec, rows, texts, stations)
    _check_legibility(res, doc)
    return res


def _compare_spec(res, spec: ProfileSpec, rows, texts, h_found, v_found) -> None:
    if h_found and h_found != [spec.h_scale]:
        res.error(f"圖面水平比例 {h_found} 與 spec h_scale 1/{spec.h_scale} 不符")
    if v_found and v_found != [spec.v_scale]:
        res.error(f"圖面垂直比例 {v_found} 與 spec v_scale 1/{spec.v_scale} 不符")

    nodes = spec.nodes
    st = [n.station_m for n in nodes]
    if any(b <= a for a, b in itertools.pairwise(st)):
        res.error(f"spec 樁號未遞增：{st}")

    # 節點列：依 x 順序比對 spec 值（optional 欄只比有給的節點）
    getters = {
        "station": lambda n: n.station_m,
        "ground": lambda n: n.ground_el,
        "top": lambda n: n.top_el,
        "invert": lambda n: n.invert_el,
        "road": lambda n: n.road_center_el,
    }
    for key in NODE_ROWS:
        if key not in rows:
            continue
        want = [(n.label, getters[key](n)) for n in nodes if getters[key](n) is not None]
        got_txt = [t.text for t in rows[key]]
        try:
            got = [parse_station(s) if key == "station" else float(s) for s in got_txt]
        except ValueError:
            res.error(f"「{PROFILE_TABLE_ROWS[key]}」列有無法解析的文字：{got_txt}")
            continue
        name = PROFILE_TABLE_ROWS[key]
        if len(got) != len(want):
            missing = [
                f"{lab}={v:.2f}" for lab, v in want if not any(abs(g - v) <= EL_TOL for g in got)
            ]
            res.error(
                f"表格帶「{name}」有 {len(got)} 個值，spec 有 {len(want)} 個"
                + (f"；缺：{', '.join(missing)}" if missing else "")
            )
            continue
        for (lab, v), g in zip(want, got, strict=True):
            if abs(g - v) > EL_TOL:
                res.error(f"表格帶「{name}」{lab}：圖面 {g:.2f} ≠ spec {v:.2f}")

    # 坡度：由 spec 重算
    slopes = [
        (a.invert_el - b.invert_el) / (b.station_m - a.station_m) * 100
        for a, b in itertools.pairwise(nodes)
    ]
    res.metrics["slopes_pct"] = [round(s, 4) for s in slopes]
    if "slope" in rows:
        got = [_num(t.text) for t in rows["slope"]]
        if len(got) != len(slopes) or None in got:
            res.error(
                f"坡度列有 {len(got)} 個值（{[t.text for t in rows['slope']]}），應有 {len(slopes)}"
            )
        else:
            for i, (g, s) in enumerate(zip(got, slopes, strict=True)):
                if abs(g - s) > SLOPE_TOL:
                    res.error(f"第 {i + 1} 段坡度：圖面 {g:.2f}% ≠ 重算 {s:.4f}%")
    if "length" in rows:
        got = [_num(t.text) for t in rows["length"]]
        want = [b - a for a, b in itertools.pairwise(st)]
        if len(got) != len(want) or any(
            g is None or abs(g - w) > EL_TOL for g, w in zip(got, want, strict=False)
        ):
            res.error(f"距離列 {[t.text for t in rows['length']]} 與樁號差 {want} 不符")
    if "section" in rows:
        got = [t.text for t in rows["section"]]
        want = [s.section for s in spec.segments]
        if got != want:
            res.error(f"斷面列 {got} 與 spec {want} 不符")

    # 順坡
    for i, (seg, s) in enumerate(zip(spec.segments, slopes, strict=True)):
        a, b = nodes[i], nodes[i + 1]
        if s < 0 and not seg.pump:
            res.error(
                f"逆坡：{a.label}→{b.label} 溝底 {a.invert_el:.2f} → {b.invert_el:.2f}"
                f"（S={s:.2f}%），非抽水段（pump=false）"
            )
        elif s == 0:
            res.warn(f"{a.label}→{b.label} 坡度 0%")

    # 特殊高程文字
    for lv in spec.levels:
        hit = any(
            lv.label in t.text
            and (m := _RE_EL.search(t.text)) is not None
            and abs(float(m.group(1)) - lv.el) <= EL_TOL
            for t in texts
        )
        if hit:
            continue
        msg = f"圖面缺少「{lv.label} EL={lv.el:.2f}」高程文字（樁號 {lv.station_m:g}）"
        if any(w in lv.label for w in REQUIRED_LEVEL_WORDS):
            res.error(msg + "；進水管 / 放流管高程為審查常見缺失")
        else:
            res.warn(msg)


def _check_structures(res, spec: ProfileSpec, texts) -> None:
    for st in spec.structures:
        for label, el in st.expected_texts():
            prefix = f"{label} EL="
            vals = [
                float(m.group(1))
                for t in texts
                if t.text.startswith(prefix) and (m := _RE_EL.search(t.text)) is not None
            ]
            if any(abs(v - el) <= EL_TOL for v in vals):
                continue
            if vals:
                res.error(f"構造物「{st.label}」：圖面「{label} EL={vals[0]:.2f}」≠ spec {el:.2f}")
            else:
                res.error(f"構造物「{st.label}」：圖面缺少「{label} EL={el:.2f}」高程文字")
        if st.kind == "detention":
            for m in st.marks:
                if m.kind in WATER_LEVEL_KINDS and not (
                    st.bottom_el - EL_TOL <= m.el <= st.top_el + EL_TOL
                ):
                    res.error(
                        f"構造物「{st.label}」：{m.label} EL={m.el:.2f} 不在池底 "
                        f"{st.bottom_el:.2f} 與池頂 {st.top_el:.2f} 之間"
                    )
    res.metrics["structures"] = [f"{st.kind}:{st.label}" for st in spec.structures]


# ---------------------------------------------------------------- DIMENSION（實際顯示文字）


def _check_dimensions(res, doc) -> None:
    """縱斷面為 1:1 圖紙 mm：dimlfac 必須 1；dimrnd / dimpost / dimalt 一律禁止；
    DIMENSION 實際顯示文字（geometry block）須含與量測值相等的完整數字。"""
    for layout in doc.layouts:
        for dim in layout.query("DIMENSION"):
            h = dim.dxf.handle
            lfac = float(dim.override().get("dimlfac", 1.0))
            if abs(lfac - 1.0) > 1e-9:
                res.error(f"DIMENSION {h} dimlfac={lfac:g}，mm 圖面必須為 1")
            for bad in forbidden_vars(dim):
                res.error(f"DIMENSION {h} {bad}")
            shown = displayed_text(dim)
            if not shown.text:
                res.error(f"DIMENSION {h} 沒有 geometry block 文字（未 render，無法確認顯示值）")
                continue
            try:
                meas = float(dim.get_measurement()) * lfac
            except Exception as ex:  # noqa: BLE001
                res.error(f"DIMENSION {h} 無法量測：{ex}")
                continue
            if not matches_number(shown.text, meas, DIM_TOL):
                res.error(f"DIMENSION {h} 顯示「{shown.text}」與量測值 {meas:.1f} 不符")


# ---------------------------------------------------------------- 物理合理性（#8）


def _invert_at(spec: ProfileSpec, station: float) -> float:
    nodes = spec.nodes
    if station <= nodes[0].station_m:
        return nodes[0].invert_el
    for a, b in itertools.pairwise(nodes):
        if a.station_m <= station <= b.station_m:
            t = (station - a.station_m) / (b.station_m - a.station_m)
            return a.invert_el + t * (b.invert_el - a.invert_el)
    return nodes[-1].invert_el


def _is_open_ditch(section: str) -> bool:
    """U 型溝 / 明溝斷面（管、箱涵除外）。"""
    return (
        ("U" in section.upper() or "溝" in section)
        and "管" not in section
        and not re.search(r"\bD\d", section)
    )


def _check_physical(res, spec: ProfileSpec) -> None:
    for n in spec.nodes:
        if n.top_el is not None and n.top_el > n.ground_el + TOP_ABOVE_GROUND_TOL:
            res.error(
                f"節點 {n.label}：溝頂 EL={n.top_el:.2f} 高於地面 EL={n.ground_el:.2f}"
                f"（容許 {TOP_ABOVE_GROUND_TOL:g} m）"
            )
    seen: set[tuple[str, str]] = set()
    for i, seg in enumerate(spec.segments):
        if not _is_open_ditch(seg.section):
            continue
        for n in (spec.nodes[i], spec.nodes[i + 1]):
            if n.top_el is None or (n.label, seg.section) in seen:
                continue
            seen.add((n.label, seg.section))
            h = n.top_el - n.invert_el
            if abs(h - seg.size_m) > SECTION_H_TOL:
                res.error(
                    f"節點 {n.label}：溝頂 − 溝底 = {h:.2f} m 與斷面「{seg.section}」"
                    f"溝高 size_m={seg.size_m:g} m 不符（±{SECTION_H_TOL:g}）"
                )
    ditches = [st for st in spec.structures if st.kind == "ditch"]
    last = spec.nodes[-1]
    for lv in spec.levels:
        if "進水管" in lv.label:
            inv = _invert_at(spec, lv.station_m)
            if lv.el < inv - EL_TOL:
                res.error(
                    f"「{lv.label}」EL={lv.el:.2f} 低於樁號 {lv.station_m:g} 處溝底 EL={inv:.2f}"
                )
        if "放流管" in lv.label:
            if ditches:
                for d in ditches:
                    if lv.el < d.bottom_el - EL_TOL:
                        res.error(
                            f"「{lv.label}」EL={lv.el:.2f} 低於受納點「{d.label}」溝底 "
                            f"EL={d.bottom_el:.2f}（倒灌）"
                        )
                    for m in d.marks:
                        if m.kind == "design_wl" and lv.el < m.el - EL_TOL:
                            res.error(
                                f"「{lv.label}」EL={lv.el:.2f} 低於受納點「{m.label}」"
                                f"EL={m.el:.2f}（放流口應設於設計水位以上）"
                            )
            elif last.kind == "outfall" and lv.el < last.invert_el - EL_TOL:
                res.error(
                    f"「{lv.label}」EL={lv.el:.2f} 低於受納點 {last.label} 溝底 "
                    f"EL={last.invert_el:.2f}（倒灌）"
                )


# ---------------------------------------------------------------- 幾何（#5）


def _segments(msp, layer: str) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    out = []
    for e in msp.query(f"LWPOLYLINE[layer=='{layer}']"):
        pts = [(p[0], p[1]) for p in e.get_points("xy")]
        if e.closed and len(pts) > 2:
            pts.append(pts[0])
        out += list(itertools.pairwise(pts))
    for e in msp.query(f"LINE[layer=='{layer}']"):
        s, t = e.dxf.start, e.dxf.end
        out.append(((s.x, s.y), (t.x, t.y)))
    return out


def _has_seg(segs, a, b, tol: float = GEOM_TOL) -> bool:
    def near(p, q):
        return abs(p[0] - q[0]) <= tol and abs(p[1] - q[1]) <= tol

    return any((near(p, a) and near(q, b)) or (near(p, b) and near(q, a)) for p, q in segs)


def _ruler(msp) -> list[tuple[float, float]]:
    """高程尺：(刻度 y, EL)。刻度數字（H-GRID TEXT）右側 5 mm 內須有同 y 的短刻度線。"""
    ticks = []
    for e in msp.query("LINE[layer=='H-GRID']"):
        s, t = e.dxf.start, e.dxf.end
        if abs(s.y - t.y) < 1e-6 and 0 < abs(s.x - t.x) <= 5.0:
            ticks.append((min(s.x, t.x), s.y))
    out = []
    for e in msp.query("TEXT[layer=='H-GRID']"):
        if not _RE_NUM.match(e.dxf.text):
            continue
        _, p, _ = e.get_placement()
        if any(abs(ty - p[1]) <= 0.3 and 0 <= tx - p[0] <= 5.0 for tx, ty in ticks):
            out.append((p[1], float(e.dxf.text)))
    return out


def _table_top(msp) -> float | None:
    best = None
    for e in msp.query("LWPOLYLINE[layer=='H-TABL']"):
        if not e.closed:
            continue
        pts = list(e.get_points("xy"))
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        area = (max(xs) - min(xs)) * (max(ys) - min(ys))
        if best is None or area > best[0]:
            best = (area, max(ys))
    return None if best is None else best[1]


def _text_xrange(e) -> tuple[float, float]:
    # 估算文字框（與平台字型無關）；fast=False 會依本機字型量字寬，跨平台結果不同
    box = est_text_box(e)
    if box is not None:
        return box[0], box[2]
    _, p, _ = e.get_placement()
    return p[0], p[0]


def _check_geometry(res, doc, spec: ProfileSpec | None, rows, texts, stations) -> None:
    msp = doc.modelspace()
    # ---- 高程尺 y ↔ EL
    ruler = _ruler(msp)
    if len(ruler) < 2:
        res.error("高程尺：找不到 2 個以上「刻度數字 + 刻度線」（H-GRID）")
        y_of = None
    else:
        ruler.sort(key=lambda p: p[1])
        k = (ruler[-1][0] - ruler[0][0]) / (ruler[-1][1] - ruler[0][1])
        if spec is not None:
            k = 1000.0 / spec.v_scale
        offs = sorted(y - el * k for y, el in ruler)
        a = offs[len(offs) // 2]
        bad = [(el, y) for y, el in ruler if abs(y - (a + el * k)) > GEOM_TOL]
        if bad:
            res.error(f"高程尺刻度間距不一致：{[f'{el:.2f}@y{y:.1f}' for el, y in bad]}")

        def y_of(el: float, a=a, k=k) -> float:
            return a + el * k

        if spec is not None:
            datum = spec.resolved_datum()
            ty1 = _table_top(msp)
            if ty1 is None:
                res.error("找不到表格帶外框（H-TABL 封閉線），無法確認高程尺基準")
            else:
                ok = [(el, y) for y, el in ruler if abs(y - (ty1 + (el - datum) * k)) <= GEOM_TOL]
                if len(ok) < 2 or len(ok) != len(ruler):
                    res.error(
                        f"高程尺絕對值與基準高程 EL={datum:.2f}（表格帶頂）不一致：刻度 "
                        f"{[f'{el:.2f}' for y, el in ruler if (el, y) not in ok][:4]} 位置錯誤"
                    )
            dtxt = [
                float(m.group(1))
                for t in texts
                if t.text.startswith("基準高程") and (m := _RE_EL.search(t.text))
            ]
            if not any(abs(v - datum) <= EL_TOL for v in dtxt):
                res.error(f"圖面缺少或錯誤「基準高程 EL={datum:.2f}」文字（圖面 {dtxt}）")

    # ---- 樁號欄 x ↔ 樁號、節點 x
    node_x: list[float] = []
    st_rows = rows.get("station", [])
    if st_rows and stations and len(stations) == len(st_rows):
        verts = []
        for e in msp.query("LINE[layer=='H-GRID']"):
            s, t = e.dxf.start, e.dxf.end
            if abs(s.x - t.x) < 1e-6:
                verts.append((s.x, min(s.y, t.y), max(s.y, t.y)))
        for t in st_rows:
            cand = [x for x, y0, y1 in verts if 0 <= x - t.x <= 6.0 and y0 <= t.y <= y1]
            node_x.append(min(cand) if cand else float("nan"))
        missing = [stations[i] for i, x in enumerate(node_x) if math.isnan(x)]
        if missing:
            res.error(f"樁號欄文字旁（右側 6 mm 內）找不到樁號格線：樁號 {missing}")
            node_x = []
        else:
            hk = 1000.0 / spec.h_scale if spec is not None else None
            if hk is None and len(stations) >= 2:
                hk = (node_x[-1] - node_x[0]) / (stations[-1] - stations[0])
            offs = [x - s * hk for x, s in zip(node_x, stations, strict=True)]
            med = sorted(offs)[len(offs) // 2]
            bad = [s for s, o in zip(stations, offs, strict=True) if abs(o - med) > GEOM_TOL]
            if bad:
                res.error(f"樁號欄 x 位置與水平比例換算不符（樁號 {bad}；±{GEOM_TOL} mm）")
        # 其他節點列（地面 / 溝頂 / 溝底 / 道路中心樁）的值須與樁號欄同 x
        sx = [t.x for t in st_rows]
        for key in NODE_ROWS[1:]:
            for t in rows.get(key, []):
                if not any(abs(t.x - x) <= GEOM_TOL for x in sx):
                    res.error(
                        f"表格帶「{PROFILE_TABLE_ROWS[key]}」值 {t.text} 的 x={t.x:.1f} 未對齊任何樁號欄"
                    )
    if spec is None or not node_x or len(node_x) != len(spec.nodes):
        if spec is not None and not node_x:
            res.error("無法由樁號欄定出節點 x，未能檢查縱斷面幾何")
        return
    hk = 1000.0 / spec.h_scale
    x0 = sorted(x - n.station_m * hk for x, n in zip(node_x, spec.nodes, strict=True))[
        len(node_x) // 2
    ]

    def x_of(st: float) -> float:
        return x0 + st * hk

    # ---- 節點名
    htext = list(msp.query("TEXT[layer=='H-TEXT']"))
    for n, x in zip(spec.nodes, node_x, strict=True):
        hits = [e for e in htext if e.dxf.text == n.label]
        if not any(abs(e.get_placement()[1][0] - x) <= GEOM_TOL for e in hits):
            where = [round(e.get_placement()[1][0], 1) for e in hits]
            res.error(f"節點名「{n.label}」不在樁號 {n.station_m:g} 的 x={x:.1f}（圖面 x={where}）")

    # ---- 流向箭頭（H-TEXT 封閉三角形）：須指向樁號遞增（+x）
    arrows = []
    for e in msp.query("LWPOLYLINE[layer=='H-TEXT']"):
        pts = [(p[0], p[1]) for p in e.get_points("xy")]
        if e.closed and len(pts) == 3:
            xs = sorted(pts, key=lambda p: p[0])
            if abs(xs[1][0] - xs[2][0]) < 0.05:  # 底邊在右 → 尖端在左
                arrows.append(-1)
            elif abs(xs[0][0] - xs[1][0]) < 0.05:
                arrows.append(1)
    if not arrows:
        res.error("找不到流向箭頭（H-TEXT 封閉三角形）")
    elif any(d < 0 for d in arrows):
        res.error("流向箭頭指向樁號遞減方向；流向應為樁號遞增")

    # ---- H-PROF 地面 / 溝底 / 溝頂折線（各節點 x 處 y 回推 EL）
    if y_of is not None:
        segs = _segments(msp, "H-PROF")
        nodes = spec.nodes
        for i, seg in enumerate(spec.segments):
            a, b = nodes[i], nodes[i + 1]
            xa, xb = node_x[i], node_x[i + 1]
            if a.top_el is not None and b.top_el is not None:
                ta, tb = a.top_el, b.top_el
            else:
                ta, tb = a.invert_el + seg.size_m, b.invert_el + seg.size_m
            for name, ea, eb in (
                ("地面線", a.ground_el, b.ground_el),
                ("溝底線", a.invert_el, b.invert_el),
                ("溝頂線", ta, tb),
            ):
                if not _has_seg(segs, (xa, y_of(ea)), (xb, y_of(eb))):
                    res.error(
                        f"H-PROF {name} {a.label}→{b.label}：找不到 ({xa:.1f},{y_of(ea):.1f})→"
                        f"({xb:.1f},{y_of(eb):.1f}) 的線段（EL {ea:.2f}→{eb:.2f}；±{GEOM_TOL} mm）"
                    )

    # ---- 特殊高程文字位置
    for lv in spec.levels:
        xs = x_of(lv.station_m)
        cands = [
            e
            for e in htext
            if e.dxf.text.startswith(f"{lv.label} EL=")
            and (m := _RE_EL.search(e.dxf.text))
            and abs(float(m.group(1)) - lv.el) <= EL_TOL
        ]
        if not cands:
            continue  # 缺文字已在 _compare_spec 回報
        dists = []
        for e in cands:
            lo, hi = _text_xrange(e)
            dists.append(max(0.0, lo - (xs + LEVEL_MARK_HALF), (xs - LEVEL_MARK_HALF) - hi))
        if min(dists) > LEVEL_X_TOL:
            res.error(
                f"「{lv.label} EL={lv.el:.2f}」文字離樁號 {lv.station_m:g}（x={xs:.1f}）"
                f"{min(dists):.1f} mm，超過 {LEVEL_X_TOL:g} mm"
            )


def check_text_anchors(res, msp, tol: float = 1.0) -> None:
    """非預設對齊的 TEXT：插入點（insert）須落在文字框（依對齊點量）擴大 1 字高的範圍內。

    ezdxf 產出時 insert = 對齊點；CAD 存檔後 insert = 文字左下角，兩者都在文字框附近。
    相距很遠代表檔案被改過一半——CAD（看對齊點）與部分檢視器（看 insert）會顯示在不同位置，
    位置類檢查無從判斷，一律 error。
    """
    for e in msp.query("TEXT"):
        if e.dxf.get("halign", 0) == 0 and e.dxf.get("valign", 0) == 0:
            continue
        if not e.dxf.hasattr("align_point"):
            continue
        ins = e.dxf.insert
        box = est_text_box(e)  # 依 align_point 估算，與平台字型無關
        if box is None:
            continue
        h = float(e.dxf.get("height", 1.0)) + tol
        if not (box[0] - h <= ins.x <= box[2] + h and box[1] - h <= ins.y <= box[3] + h):
            ap = e.dxf.align_point
            res.error(
                f"TEXT {e.dxf.handle}「{e.dxf.text}」插入點 ({ins.x:.1f},{ins.y:.1f}) 與對齊點 "
                f"({ap.x:.1f},{ap.y:.1f}) 不一致：CAD 與檢視器顯示位置不同"
            )


def _check_legibility(res, doc) -> None:
    """字高 ≥ 2.0 mm（modelspace = 1:1 圖紙 mm）、文字重疊 → error；文字壓 H-PROF 線 → warning。"""
    from draftagent.verify.legibility_check import check_text_heights, check_text_overlaps

    for msg in check_text_heights(doc, "Model", min_mm=MIN_TEXT_MM):
        res.error(f"字高：{msg}")
    errs, warns = check_text_overlaps(doc, "Model", line_layers=("H-PROF",))
    for msg in errs:
        res.error(msg)
    for msg in warns:
        res.warn(msg)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        prog="python -m draftagent.verify.profile_check",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("dxf")
    ap.add_argument("--spec", help="ProfileSpec JSON，用來比對表格帶數值、坡度、特殊高程")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    emit(check_profile(args.dxf, args.spec), args.json)


if __name__ == "__main__":
    main()
