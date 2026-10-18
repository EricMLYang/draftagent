"""整套排水圖跨圖一致性 verifier（sheet set）。純函式，不得呼叫 LLM。

讀同一次 build 產出的 drainage_plan.dxf、profile.dxf、profile_outlet.dxf、detail.dxf、result.json，
**只從各圖的 TEXT / ATTRIB 解析數值**，建立「元素 id → 屬性 → [(圖, 值)]」對照表，檢查同一元素在
每張圖上的數值一致。spec（DrainageNetworkSpec）只用來知道有哪些元素 id 與拓樸（link 的上下游節點），
不拿 spec 的數值當答案——數值錯誤由各圖自己的 verifier 對 spec 檢查，這裡抓「圖與圖之間對不上」
（審查常見缺失：圖說呈現須一致、銜接處各高程）。

解析契約（文字詞彙見 `drainage/sheet_vocab.py`）
- 平面設計圖：節點 INSERT ATTRIB ID / TOP / INV；管段標籤「<id> <斷面> S=x.xx% L=xx.xx m」；
  出流口「受納溝設計水位 EL=」；附註「池底EL=…、設計滿水位EL=…」；數量表滯洪設施列（貯集量）
- 縱斷面（主幹 / 出水側）：表格帶節點列（依節點名稱文字 x 對位）與管段列（坡度 / 距離 / 斷面）；
  「<label> EL=」高程文字（進水管<link>、放流管<link>、放流管、溢流口（進流井）、抽水出水管、溢流管、
  滯洪設施水位、<storage>頂 / 底、放流井<node>頂 / 底、受納溝頂 / 底 / 設計水位）
- 詳圖：GROUP DETAIL-<id>，標題「…（<元素 id>）」；各模板的「… EL=」文字與銘牌容量
- result.json：storage_provided.provided_m3、pipes[].slope

檢查
- 同一（元素, 屬性）在不同圖（或同圖不同處）數值不一致 → error，列出元素、屬性、圖A值、圖B值。
  容差：高程 / 長度 0.005 m；坡度 0.01 %（百分點）；貯集量 max(0.01 m³, 0.01 %)；斷面文字須相同
- 每張圖（含平面圖的兩個 layout）圖號唯一；圖名 = `hydro/codes/taoyuan_drawing.yaml` 統一圖名；
  比例分母 ≤ 該圖門檻（詳圖「如圖示」時檢查每個 S=1/N）
- 至少有 1 項跨圖比對；滯洪設施池底 / 受納溝設計水位等關鍵項目只出現在 1 張圖 → WARNING
- 平面設計圖 modelspace 標註納入比對（對其他圖與 spec）：滯洪設施「V=… m³」（= 計算書 / 數量表 / 銘牌）、
  「有效水深 … m」（= 數量表 = spec）、既有排水各點「頂EL= / 底EL=」（= spec 該點）、「路中EL=」（= spec 道路中心樁）；
  spec 有該點卻找不到標註 → error
- 孔口：calc.outlet 為孔口（非 pump）時，詳圖必須有孔口尺寸文字（孔口板詳圖標題「孔口板詳圖 D135（…）」、
  筏基 / 放流井「孔口 D135 中心」）且 = calc_spec.json 的孔口；孔口板孔徑標註（DIMENSION 實際顯示文字）= calc；
  「孔心 EL=」= 模型孔口中心（孔口底 + 孔高 / 2）

用法：python -m draftagent.verify.sheetset_check <run_dir> --spec <spec.json> [--json]
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import ezdxf

from draftagent.drainage import sheet_vocab as V
from draftagent.drainage.model import DrainageNetworkSpec
from draftagent.hydro.drawing_codes import DEFAULT_PATH, load_drawing_codes
from draftagent.render.drainage_plan import CATCH_LAYOUT, PLAN_LAYOUT
from draftagent.schema.profile import PROFILE_TABLE_ROWS
from draftagent.verify.base import VerifyResult, emit
from draftagent.verify.dimtext import displayed_text, matches_number

NAME = "sheetset_check"
EL_TOL = 0.005
LEN_TOL = 0.005
SLOPE_TOL = 0.01  # 百分點
VOL_ABS, VOL_REL = 0.01, 1e-4
ROW_YTOL = 0.5
NODE_XTOL = 5.0  # mm：表格帶數值與節點名稱的 x 對位

_RE_LINK = re.compile(r"^(\S+) (\S+) S=(-?\d+\.\d+)% L=(\d+\.\d+) m$")
_RE_LABEL_EL = re.compile(r"^(.+?)\s*EL=(-?\d+\.\d+)$")
_RE_NOTE_EL = re.compile(r"([^、\s]+?)EL=(-?\d+\.\d+)")
_RE_SEC = re.compile(r"(W\d+(?:\.\d+)?×H\d+(?:\.\d+)?|D\d+(?:\.\d+)?)")
_RE_IDS = re.compile(r"（([^（）]+)）\s*$")
_RE_SCALE_S = re.compile(r"S\s*=\s*1\s*/\s*(\d+)")
_RE_H = re.compile(r"水平\s*1\s*/\s*(\d+)")
_RE_M3 = re.compile(r"^(\d+(?:\.\d+)?)\s*m³$")
_RE_V = re.compile(r"^V=(\d+(?:\.\d+)?) m³$")
_RE_DEPTH = re.compile(r"有效水深 (\d+(?:\.\d+)?) m")
_RE_TOP_EL = re.compile(r"^頂EL=(-?\d+\.\d+)$")
_RE_INV_EL = re.compile(r"^底EL=(-?\d+\.\d+)$")
_RE_ROAD = re.compile(r"^路中EL=(-?\d+\.\d+)$")
_SIZE = r"(D\d+(?:\.\d+)?|\d+(?:\.\d+)?×\d+(?:\.\d+)?)"
_RE_ORI_PLATE = re.compile(V.DETAIL_TITLE["orifice_plate"] + r"\s+" + _SIZE + r"（")
_RE_ORI_LABEL = re.compile(r"孔口\s+" + _SIZE + r"\s*中心")
SPEC_SRC = "模型 spec"

# 屬性 → 容差類別
_KIND = {
    "slope_pct": "slope",
    "length_m": "len",
    "effective_depth_m": "len",
    "volume_m3": "vol",
    "section": "text",
    "orifice_size": "text",
}


@dataclass
class _Obs:
    source: str
    value: float | str
    text: str


class _Table:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], list[_Obs]] = defaultdict(list)

    def add(self, elem: str, attr: str, source: str, value, text: str) -> None:
        self.rows[(elem, attr)].append(_Obs(source, value, text))


def _section(text: str) -> str | None:
    m = _RE_SEC.search(text)
    return m.group(1) if m else None


def _ttlb(layout) -> dict[str, str] | None:
    for ins in layout.query("INSERT"):
        if ins.attribs and ins.dxf.name.upper().startswith("TTLB"):
            return {a.dxf.tag.upper(): a.dxf.text for a in ins.attribs}
    return None


def _texts(layout):
    yield from layout.query("TEXT")


# ---------------------------------------------------------------- 各圖解析


class _Ctx:
    def __init__(self, spec: DrainageNetworkSpec):
        self.spec = spec
        self.nodes = {n.id: n for n in spec.nodes}
        self.links = {lk.id: lk for lk in spec.links}
        self.sid = spec.storage.id if spec.storage is not None else None
        of = [n.id for n in spec.nodes if n.kind == "outfall"]
        self.outfall = of[0] if len(of) == 1 else None

    def link_between(self, a: str, b: str):
        return next((lk for lk in self.spec.links if lk.from_ == a and lk.to == b), None)

    def label_target(self, label: str) -> tuple[str, str] | None:
        """縱斷面 / 平面「<label> EL=」→ (元素, 屬性)。"""
        st = V.STORAGE
        if self.sid is not None:
            if label in V.STORAGE_MARKS:
                return st, V.STORAGE_MARKS[label]
            if label == V.OVERFLOW_AT_INLET_LABEL:
                return st, "overflow_el"
            if label == V.PUMP_OUTLET_LABEL:
                return st, "pump_outlet_invert_el"
            if label == V.OVERFLOW_PIPE_LABEL:
                return st, "overflow_pipe_invert_el"
            if label in (f"{self.sid}{V.TOP_SUFFIX}", "池頂"):
                return st, "top_el"
            if label in (f"{self.sid}{V.BOTTOM_SUFFIX}", "池底"):
                return st, "bottom_el"
        if label == V.RECEIVING_WL_LABEL:
            return V.RECEIVING, "design_wl"
        if label == f"{V.RECEIVING_LABEL}{V.TOP_SUFFIX}":
            return V.RECEIVING, "top_el"
        if label == f"{V.RECEIVING_LABEL}{V.BOTTOM_SUFFIX}":
            return V.RECEIVING, "invert_el"
        if label == "放流管" and self.outfall:
            return self.outfall, "invert_el"
        for prefix, end in ((V.INLET_PIPE_PREFIX, "to"), (V.OUTLET_PIPE_PREFIX, "from_")):
            if label.startswith(prefix) and label[len(prefix) :] in self.links:
                return getattr(self.links[label[len(prefix) :]], end), "invert_el"
        if label.startswith(V.OUTLET_WELL_PREFIX):
            rest = label[len(V.OUTLET_WELL_PREFIX) :]
            for suf, attr in ((V.TOP_SUFFIX, "top"), (V.BOTTOM_SUFFIX, "invert_el")):
                if rest.endswith(suf) and rest[: -len(suf)] in self.nodes:
                    return rest[: -len(suf)], attr
        return None


def _parse_plan(doc, c: _Ctx, t: _Table, sheets: list) -> None:
    msp = doc.modelspace()
    src = "平面設計圖"
    for ins in msp.query("INSERT"):
        if not ins.attribs:
            continue
        att = {a.dxf.tag.upper(): a.dxf.text for a in ins.attribs}
        nid = att.get("ID")
        if nid not in c.nodes:
            continue
        for tag, attr in (("TOP", "top"), ("INV", "invert_el")):
            try:
                t.add(nid, attr, src, float(att[tag]), f"{tag}={att[tag]}")
            except (KeyError, ValueError):
                pass
    for e in _texts(msp):
        s = e.dxf.text
        m = _RE_LINK.match(s)
        if m and m.group(1) in c.links:
            lid = m.group(1)
            t.add(lid, "slope_pct", src, float(m.group(3)), s)
            t.add(lid, "length_m", src, float(m.group(4)), s)
            sec = _section(m.group(2))
            if sec:
                t.add(lid, "section", src, sec, s)
            continue
        m = _RE_LABEL_EL.match(s)
        if m and m.group(1) == V.RECEIVING_WL_LABEL:
            t.add(V.RECEIVING, "design_wl", src, float(m.group(2)), s)
            continue
        if c.sid is not None and e.dxf.layer == "C-STRM-TEXT":
            if mv := _RE_V.match(s):
                t.add(V.STORAGE, "volume_m3", src, float(mv.group(1)), f"modelspace {s}")
            elif md := _RE_DEPTH.fullmatch(s):
                t.add(V.STORAGE, "effective_depth_m", src, float(md.group(1)), f"modelspace {s}")
    for name in (PLAN_LAYOUT, CATCH_LAYOUT):
        if name not in doc.layouts:
            continue
        lay = doc.layouts.get(name)
        tb = _ttlb(lay)
        key = "drainage_plan" if name == PLAN_LAYOUT else "catchment_map"
        sheets.append((f"{name}", key, tb))
        if name != PLAN_LAYOUT:
            continue
        texts = list(_texts(lay))
        for e in texts:
            if e.dxf.layer == "X-TEXT":
                for m in _RE_NOTE_EL.finditer(e.dxf.text):
                    tgt = c.label_target(m.group(1))
                    if tgt:
                        t.add(*tgt, src, float(m.group(2)), e.dxf.text.strip())
        if c.spec.storage is not None:  # 數量表：滯洪設施列的數量
            from draftagent.drainage.derive import STORAGE_ITEM

            item = STORAGE_ITEM[c.spec.storage.type]
            tabl = [e for e in texts if e.dxf.layer == "C-TABL"]
            for e in tabl:
                if e.dxf.text != item:
                    continue
                y = e.get_placement()[1][1]
                for o in tabl:
                    same_row = abs(o.get_placement()[1][1] - y) <= 0.3
                    if same_row and (md := _RE_DEPTH.search(o.dxf.text)):
                        t.add(
                            V.STORAGE,
                            "effective_depth_m",
                            src,
                            float(md.group(1)),
                            f"數量表 {o.dxf.text}",
                        )
                    if same_row and re.fullmatch(r"\d+\.\d{2}", o.dxf.text):
                        t.add(
                            V.STORAGE,
                            "volume_m3",
                            src,
                            float(o.dxf.text),
                            f"數量表 {item} {o.dxf.text}",
                        )


def _assign(points: list[tuple[str, float, float]], labels: list, max_d: float):
    """點 ↔ 標籤一對一（全域距離由近而遠貪婪配對）；回傳 {點 key: 標籤}。"""
    pairs = sorted(
        (math.hypot(lb[0] - x, lb[1] - y), i, j)
        for i, (_, x, y) in enumerate(points)
        for j, lb in enumerate(labels)
    )
    out, used_p, used_l = {}, set(), set()
    for d, i, j in pairs:
        if d > max_d or i in used_p or j in used_l:
            continue
        used_p.add(i)
        used_l.add(j)
        out[points[i][0]] = labels[j]
    return out


def _parse_plan_context(doc, c: _Ctx, t: _Table, res: VerifyResult) -> None:
    """平面設計圖 modelspace：既有排水各點「頂EL= / 底EL=」、道路中心樁「路中EL=」對 spec。"""
    from draftagent.drainage.model import NODE_KIND_NAME
    from draftagent.render.drainage_plan import (
        LABEL_GAP_MM,
        LABEL_RING_MM,
        LABEL_RINGS,
        LINE_GAP,
        TEXT_MM,
    )

    sp = c.spec
    k = sp.scales.plan / 1000.0
    max_d = (LABEL_GAP_MM + LABEL_RINGS * LABEL_RING_MM) * k + 2 * TEXT_MM * k
    src = "平面設計圖"
    msp = doc.modelspace()
    txt = [
        (e.get_placement()[1][0], e.get_placement()[1][1], e.dxf.text, e.dxf.layer.upper())
        for e in msp.query("TEXT")
    ]
    step = TEXT_MM * k * LINE_GAP
    heads = [
        (x, y)
        for x, y, s_, lay in txt
        if lay == "C-STRM-TEXT" and any(s_ == f"{n.id} {NODE_KIND_NAME[n.kind]}" for n in sp.nodes)
    ]
    groups = []  # 既有排水點標籤：「頂EL=」+ 正下方「底EL=」，上方沒有節點標題
    for x, y, s_, lay in txt:
        if lay != "C-STRM-TEXT" or not (mt := _RE_TOP_EL.match(s_)):
            continue
        if any(abs(hx - x) < 1e-3 and 0 < hy - y < 1.5 * step for hx, hy in heads):
            continue
        inv = [
            float(mi.group(1))
            for x2, y2, s2, lay2 in txt
            if lay2 == "C-STRM-TEXT"
            and abs(x2 - x) < 1e-3
            and 0 < y - y2 < 1.5 * step
            and (mi := _RE_INV_EL.match(s2))
        ]
        groups.append((x, y, float(mt.group(1)), inv[0] if inv else None, s_))
    pts = [
        (f"{d.id}[{i}]", p.x, p.y, p)
        for d in sp.existing_drains
        for i, p in enumerate(d.points)
        if p.top_el is not None and p.invert_el is not None
    ]
    hit = _assign([(key, x, y) for key, x, y, _ in pts], groups, max_d)
    for key, _x, _y, p in pts:
        t.add(key, "top_el", SPEC_SRC, p.top_el, f"spec top_el={p.top_el}")
        t.add(key, "invert_el", SPEC_SRC, p.invert_el, f"spec invert_el={p.invert_el}")
        g = hit.get(key)
        if g is None:
            res.error(f"平面設計圖缺既有排水 {key} 的「頂EL= / 底EL=」標註")
            continue
        t.add(key, "top_el", src, g[2], g[4])
        if g[3] is None:
            res.error(f"平面設計圖既有排水 {key} 標註缺「底EL=」")
        else:
            t.add(key, "invert_el", src, g[3], f"底EL={g[3]:.2f}")
    roads = [(x, y, float(m.group(1)), s_) for x, y, s_, lay in txt if lay == "C-ROAD-TEXT" and (m := _RE_ROAD.match(s_))]
    rpts = [(f"路中樁[{i}]", p.x, p.y) for i, p in enumerate(sp.road_centerline_els)]
    rhit = _assign(rpts, roads, max_d)
    for (key, _x, _y), p in zip(rpts, sp.road_centerline_els, strict=True):
        t.add(key, "road_el", SPEC_SRC, p.el, f"spec el={p.el}")
        g = rhit.get(key)
        if g is None:
            res.error(f"平面設計圖缺道路中心樁 {key} 的「路中EL=」標註")
        else:
            t.add(key, "road_el", src, g[2], g[3])
    if sp.storage is not None:
        t.add(
            V.STORAGE,
            "effective_depth_m",
            SPEC_SRC,
            sp.storage.effective_depth,
            f"spec effective_depth={sp.storage.effective_depth}",
        )


def _orifice_checks(
    c: _Ctx, t: _Table, res: VerifyResult, detail_doc, calc_spec_path: Path | None
) -> None:
    """calc.outlet 為孔口時：詳圖孔口尺寸文字 / 孔口板孔徑標註 = calc；孔心 EL = 模型。"""
    from draftagent.drainage.derive import orifice_center_el, orifice_size_text
    from draftagent.drainage.hydraulics import resolve_storage
    from draftagent.schema.drainage import TaoyuanSiteDrainageSpec

    sp = c.spec
    if sp.storage is None or sp.calc is None or sp.storage.drain_mode == "pump":
        return
    outlet, src = sp.calc.outlet, "模型 spec.calc"
    if calc_spec_path is not None and Path(calc_spec_path).exists():
        try:
            cs = TaoyuanSiteDrainageSpec.from_json(calc_spec_path)
            outlet, src = cs.outlet, "計算書 calc_spec.json"
        except Exception as e:  # noqa: BLE001 - 回報給使用者
            res.error(f"calc_spec.json 無法讀取：{e}")
    if outlet is None:
        res.error("drain_mode 非 pump，但計算書沒有孔口（calc.outlet）")
        return
    size = orifice_size_text(outlet)
    t.add(V.STORAGE, "orifice_size", src, size, f"{outlet.type} {size}")
    lv = resolve_storage(sp)[0]
    center = orifice_center_el(sp, lv)
    if center is not None:
        t.add(V.STORAGE, "orifice_center_el", "模型（孔口底 + 孔高 / 2）", center, f"{center:.2f}")
    if detail_doc is None:
        return
    n_size = 0
    plate_dims: list[str] | None = None
    for name, grp in detail_doc.groups:
        if not name.startswith("DETAIL-"):
            continue
        template = grp.dxf.get("description", "")
        for e in grp:
            if not e.is_alive:
                continue
            if e.dxftype() == "TEXT":
                s_ = e.dxf.text
                m = _RE_ORI_PLATE.search(s_) if template == "orifice_plate" else _RE_ORI_LABEL.search(s_)
                if m:
                    n_size += 1
                    t.add(V.STORAGE, "orifice_size", "詳圖", m.group(1), s_)
            elif e.dxftype() == "DIMENSION" and template == "orifice_plate":
                plate_dims = (plate_dims or []) + [displayed_text(e).text]
    if n_size == 0:
        res.error(f"詳圖缺孔口尺寸文字（計算書孔口 {size}；審查：圖說與計算書須一致）")
    if plate_dims is not None:
        w = (outlet.diameter_m if outlet.type == "circ_orifice" else outlet.width_m) * 1000.0
        if not any(matches_number(d, w, 0.5) for d in plate_dims):
            res.error(
                f"孔口板詳圖沒有顯示 {w:g} mm 的孔徑 / 孔寬標註（計算書 {size}；標註實際文字 {plate_dims}）"
            )


def _parse_profile(doc, c: _Ctx, t: _Table, src: str) -> None:
    msp = doc.modelspace()
    texts = list(_texts(msp))
    node_x = {}
    for e in texts:
        if e.dxf.layer == "H-TEXT" and e.dxf.text in c.nodes:
            node_x[e.dxf.text] = e.get_placement()[1][0]
    order = sorted(node_x, key=node_x.get)
    tabl = [e for e in texts if e.dxf.layer == "H-TABL"]
    label_of = {v: k for k, v in PROFILE_TABLE_ROWS.items()}
    labels = {label_of[e.dxf.text]: e.get_placement()[1][1] for e in tabl if e.dxf.text in label_of}

    def row(key):
        if key not in labels:
            return []
        y = labels[key]
        return [
            e
            for e in tabl
            if e.dxf.text not in label_of and abs(e.get_placement()[1][1] - y) <= ROW_YTOL
        ]

    for key, attr in (("ground", "ground_el"), ("top", "top"), ("invert", "invert_el")):
        for e in row(key):
            x = e.get_placement()[1][0]
            near = [n for n in order if abs(node_x[n] - x) <= NODE_XTOL]
            if near:
                try:
                    t.add(
                        near[0],
                        attr,
                        src,
                        float(e.dxf.text),
                        f"表格帶 {PROFILE_TABLE_ROWS[key]} {e.dxf.text}",
                    )
                except ValueError:
                    pass
    for key, attr in (("slope", "slope_pct"), ("length", "length_m"), ("section", "section")):
        for e in row(key):
            x = e.get_placement()[1][0]
            for a, b in zip(order, order[1:], strict=False):
                if node_x[a] < x < node_x[b]:
                    lk = c.link_between(a, b)
                    if lk is None:
                        break
                    s = e.dxf.text
                    if attr == "section":
                        v = _section(s)
                        if v:
                            t.add(lk.id, attr, src, v, f"表格帶 斷面 {s}")
                    else:
                        try:
                            t.add(lk.id, attr, src, float(s.rstrip("%")), f"表格帶 {s}")
                        except ValueError:
                            pass
                    break
    for e in texts:
        m = _RE_LABEL_EL.match(e.dxf.text)
        if not m:
            continue
        tgt = c.label_target(m.group(1))
        if tgt:
            t.add(*tgt, src, float(m.group(2)), e.dxf.text)


def _detail_target(c: _Ctx, template: str, ids: list[str], label: str):
    sid = V.STORAGE
    if template == "raft_detention":
        table = {
            "坑底": (sid, "bottom_el"),
            "坑頂（頂版底）": (sid, "top_el"),
            V.STORAGE_LEVEL_LABEL: (sid, "max_water_el"),
            V.OVERFLOW_FLOOD_LABEL: (sid, "overflow_flood_el"),
            "起抽水位": (sid, "pump_start_el"),
            "停機水位": (sid, "pump_stop_el"),
        }
        if label in table:
            return table[label]
        if label.startswith("溢流口"):
            return sid, "overflow_el"
        if label.startswith("進流管"):
            return sid, "inflow_invert_el"
        if label.startswith("放流孔口"):
            return sid, "orifice_center_el"
        return None
    if template == "orifice_plate":
        return (sid, "orifice_center_el") if label == V.ORIFICE_PLATE_CENTER_LABEL else None
    node = ids[0] if ids and ids[0] in c.nodes else None
    recv = {
        "側溝底": (V.RECEIVING, "invert_el"),
        "側溝溝緣": (V.RECEIVING, "top_el"),
        V.RECEIVING_WL_LABEL: (V.RECEIVING, "design_wl"),
    }
    if label in recv:
        return recv[label]
    if template == "outlet_well" and node:
        if label == "井頂":
            return node, "top"
        if label == "井底":
            return node, "invert_el"
        if label == "放流管出口管底":
            out = next((lk for lk in c.spec.links if lk.from_ == node), None)
            return (out.to, "invert_el") if out else None
        if label.startswith("放流管"):
            return node, "invert_el"  # 放流管井端管底 = 出流 link 上游 invert = 放流井底
        if label.startswith("抽水出水管"):
            return sid, "pump_outlet_invert_el"
        if label.startswith("溢流管"):
            st = c.spec.storage
            inlet = st is not None and st.overflow_at == "inlet_well"
            return sid, "overflow_pipe_invert_el" if inlet else "overflow_el"
        if label.startswith("溢流堰頂"):
            return sid, "overflow_el"
        if label.startswith("孔口"):
            return sid, "orifice_center_el"
        return None
    if template in ("ditch_joint", "outfall_protection") and node:
        if label.startswith(("出流口", "出水管")):
            return node, "invert_el"
        if label == "地面":
            return node, "ground_el"
    return None


def _parse_detail(doc, c: _Ctx, t: _Table, src: str, scales: list[int]) -> None:
    for name, grp in doc.groups:
        if not name.startswith("DETAIL-"):
            continue
        template = grp.dxf.get("description", "")
        ents = [e for e in grp if e.is_alive and e.dxftype() == "TEXT"]
        title = next(
            (e.dxf.text for e in ents if "　" in e.dxf.text and _RE_IDS.search(e.dxf.text)), ""
        )
        m = _RE_IDS.search(title)
        ids = m.group(1).split("、") if m else []
        for e in ents:
            s = e.dxf.text
            for ms in _RE_SCALE_S.finditer(s):
                scales.append(int(ms.group(1)))
            if template == "u_ditch" and s == title:
                sec = _section(s)
                for lid in ids:
                    if lid in c.links and sec:
                        t.add(lid, "section", src, sec, s)
                continue
            if template == "nameplate":
                mv = _RE_M3.match(s)
                if mv:
                    t.add(V.STORAGE, "volume_m3", src, float(mv.group(1)), f"銘牌 {s}")
                continue
            ml = _RE_LABEL_EL.match(s)
            if ml:
                tgt = _detail_target(c, template, ids, ml.group(1))
                if tgt:
                    t.add(*tgt, src, float(ml.group(2)), f"{title or name}：{s}")


def _parse_result(path: Path, c: _Ctx, t: _Table) -> None:
    r = json.loads(Path(path).read_text(encoding="utf-8"))
    src = "計算書 result.json"
    try:
        v = float(r["storage_provided"]["provided_m3"]["value"])
        t.add(V.STORAGE, "volume_m3", src, round(v, 4), f"provided_m3={v}")
    except (KeyError, TypeError, ValueError):
        pass
    for p in r.get("pipes") or []:
        if p.get("name") in c.links and "slope" in p:
            t.add(p["name"], "slope_pct", src, float(p["slope"]) * 100, f"slope={p['slope']}")


# ---------------------------------------------------------------- 主程式


def _same(attr: str, a, b) -> bool:
    k = _KIND.get(attr, "el")
    if k == "text":
        return a == b
    tol = {"slope": SLOPE_TOL, "len": LEN_TOL, "el": EL_TOL}.get(k)
    if k == "vol":
        tol = max(VOL_ABS, VOL_REL * abs(a))
    return abs(a - b) <= tol + 1e-9


def _fmt(attr: str, v) -> str:
    if isinstance(v, str):
        return v
    return f"{v:.2f}%" if attr == "slope_pct" else f"{v:.3f}"


def check_sheetset(
    artifacts: dict[str, Path],
    spec: DrainageNetworkSpec | str | Path,
    codes_path: str | Path = DEFAULT_PATH,
) -> VerifyResult:
    res = VerifyResult(name=NAME)
    if not isinstance(spec, DrainageNetworkSpec):
        spec = DrainageNetworkSpec.from_json(spec)
    c = _Ctx(spec)
    t = _Table()
    sheets: list[tuple[str, str, dict | None]] = []
    detail_scales: list[int] = []

    def read(key: str):
        p = artifacts.get(key)
        if p is None or not Path(p).exists():
            return None
        try:
            return ezdxf.readfile(str(p))
        except (OSError, ezdxf.DXFStructureError) as e:
            res.error(f"{Path(p).name} 無法讀取：{e}")
            return None

    doc = read("dxf")
    if doc is None:
        res.error("找不到平面設計圖 drainage_plan.dxf（artifacts['dxf']）")
    else:
        _parse_plan(doc, c, t, sheets)
        _parse_plan_context(doc, c, t, res)
    for key, label in (("profile_dxf", "縱斷面"), ("profile_outlet_dxf", "出水側縱斷面")):
        d = read(key)
        if d is None:
            if key == "profile_dxf":
                res.error("找不到縱斷面 profile.dxf")
            continue
        tb = _ttlb(d.modelspace())
        src = f"{label}（{(tb or {}).get('DWG_NO', '?')}）"
        sheets.append((src, "drainage_profile", tb))
        _parse_profile(d, c, t, src)
    d = read("detail_dxf")
    if d is not None:
        tb = _ttlb(d.modelspace())
        src = f"詳圖（{(tb or {}).get('DWG_NO', '?')}）"
        sheets.append((src, "drainage_details", tb))
        _parse_detail(d, c, t, src, detail_scales)
    _orifice_checks(c, t, res, d, artifacts.get("calc_spec"))
    if artifacts.get("result") and Path(artifacts["result"]).exists():
        _parse_result(artifacts["result"], c, t)

    # ---------------- 跨圖數值一致
    compared = 0
    single: list[str] = []
    for (elem, attr), obs in sorted(t.rows.items()):
        sources = {o.source for o in obs}
        if len(obs) < 2:
            single.append(f"{elem}.{attr}@{obs[0].source}")
            continue
        if len(sources) > 1:
            compared += 1
        ref = obs[0]
        seen = set()
        for o in obs[1:]:
            if _same(attr, ref.value, o.value):
                continue
            key = (o.source, str(o.value))
            if key in seen:
                continue
            seen.add(key)
            diff = "" if isinstance(o.value, str) else f"（差 {o.value - ref.value:+.3f}）"
            res.error(
                f"跨圖不一致：元素 {elem} {V.ATTR_NAME.get(attr, attr)}："
                f"{ref.source} = {_fmt(attr, ref.value)}「{ref.text}」 vs "
                f"{o.source} = {_fmt(attr, o.value)}「{o.text}」{diff}"
            )
    res.metrics["elements_compared"] = compared
    res.metrics["observations"] = sum(len(v) for v in t.rows.values())
    if compared == 0:
        res.error("沒有任何元素在兩張以上圖面出現，無法做跨圖一致性檢查（圖面文字契約可能被改動）")
    key_items = []
    if spec.storage is not None:
        key_items += [(V.STORAGE, "bottom_el"), (V.STORAGE, "volume_m3")]
    if any(d.design_water_depth_m for d in spec.existing_drains):
        key_items.append((V.RECEIVING, "design_wl"))
    for k in key_items:
        srcs = {o.source for o in t.rows.get(k, [])}
        if len(srcs) < 2:
            res.warn(
                f"關鍵項目 {k[0]} {V.ATTR_NAME.get(k[1], k[1])} 只出現在 {sorted(srcs) or '0 張圖'}"
            )

    # ---------------- 圖號 / 圖名 / 比例
    codes = load_drawing_codes(codes_path)
    nos: dict[str, list[str]] = defaultdict(list)
    for src, key, tb in sheets:
        rule = codes.scale(key)
        if tb is None:
            res.error(f"{src}：找不到圖簽")
            continue
        no = tb.get("DWG_NO", "").strip()
        nos[no].append(src)
        if tb.get("TITLE", "") != rule.name:
            res.error(
                f"{src}：圖簽圖名「{tb.get('TITLE', '')}」≠ 統一圖名「{rule.name}」（{rule.source}）"
            )
        sc = tb.get("SCALE", "").replace(" ", "")
        if key == "drainage_profile":
            m = _RE_H.search(tb.get("SCALE", ""))
            denoms = [int(m.group(1))] if m else []
        elif key == "drainage_details" and sc == "如圖示":
            denoms = detail_scales
        else:
            m = re.fullmatch(r"1/(\d+)", sc)
            denoms = [int(m.group(1))] if m else []
        if not denoms:
            res.error(f"{src}：圖簽比例「{tb.get('SCALE', '')}」無法解析")
        for dn in denoms:
            if dn > rule.max_denominator:
                res.error(
                    f"{src}：比例 1/{dn} 小於{rule.name} S≧1/{rule.max_denominator}（{rule.source}）"
                )
    for no, srcs in nos.items():
        if len(srcs) > 1:
            res.error(f"圖號「{no}」重複：{'、'.join(srcs)}")
    if not codes.verified:
        res.warn(f"比例尺門檻 yaml verified: false — {codes.note}（{codes.path.name}）")
    res.metrics["sheets"] = [f"{(tb or {}).get('DWG_NO', '?')}:{src}" for src, _, tb in sheets]
    return res


def collect(run_dir: str | Path) -> dict[str, Path]:
    d = Path(run_dir)
    names = {
        "dxf": "drainage_plan.dxf",
        "profile_dxf": "profile.dxf",
        "profile_outlet_dxf": "profile_outlet.dxf",
        "detail_dxf": "detail.dxf",
        "result": "result.json",
        "calc_spec": "calc_spec.json",
    }
    return {k: d / v for k, v in names.items() if (d / v).exists()}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description="整套排水圖跨圖一致性 verifier",
        epilog=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("run_dir", help="draftagent build 的 run 目錄（含 drainage_plan.dxf 等）")
    ap.add_argument("--spec", help="DrainageNetworkSpec JSON；省略 → <run_dir>/spec.json")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    spec = a.spec or str(Path(a.run_dir) / "spec.json")
    emit(check_sheetset(collect(a.run_dir), spec), a.json)


if __name__ == "__main__":
    main()
