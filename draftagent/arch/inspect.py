"""建築師 DXF 體檢：`inspect_dxf(path) -> dict`（可 JSON 化）。純函式，不得呼叫 LLM。

只「看懂」不「決定」：單位推測、圖層統計、候選標記（hints）都只是提示，
哪個圖層是外牆、圖上單位到底是什麼，由 agent / 工程師確認後寫進 ArchImportSpec。

報告欄位（頂層）：
- ok / path / error
- read：{mode: "normal"|"recover", dxfversion, release, encoding, auditor_fixes, auditor_errors, messages}
- units：{insunits, insunits_name, extents, core_extents, size, guess, confidence, why, twd97_like, to_m}
- layers：[{name, on, frozen, locked, color, linetype, lineweight, entities{type: n}, n_entities,
            closed{count, area_max_m2, area_sum_m2, areas_top_m2}, hatch{count, paths, area_max_m2},
            open_length_m, double_line_ratio, bbox, hints[{role, score, why}]}]
- candidates：{role: [{layer, score, why}]}（各 role 依 score 排序）；top：前 5 名（非註記類）
- blocks：{definitions[...], anonymous, xrefs[...], inserts{count, scale_min, scale_max, non_uniform,
           mirrored, rotations}}
- texts：樓層線索 [{text, tags, layer, pos, height}]；clusters：空間群聚的圖面區塊與附近圖名
- layouts：[{name, viewports}]
- warnings：[str]
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import ezdxf
from ezdxf.document import Drawing

INSUNITS_NAMES = {
    0: "unitless", 1: "in", 2: "ft", 3: "mi", 4: "mm", 5: "cm", 6: "m", 7: "km",
    8: "microinch", 9: "mil", 10: "yd", 11: "angstrom", 12: "nm", 13: "micron", 14: "dm",
    15: "dam", 16: "hm", 17: "Gm", 18: "AU", 19: "ly", 20: "pc",
}
TO_M = {"mm": 0.001, "cm": 0.01, "dm": 0.1, "m": 1.0, "km": 1000.0, "in": 0.0254, "ft": 0.3048}

# 典型建物 / 基地尺度（m）
BLDG_SIZE_M = (10.0, 200.0)
BLDG_AREA_M2 = (50.0, 5000.0)
SITE_AREA_M2 = (50.0, 50000.0)
WALL_THICK_M = (0.08, 0.6)
# TWD97 TM2（m）範圍
TWD97_E = (1.5e5, 3.5e5)
TWD97_N = (2.4e6, 2.8e6)

MAX_SEGMENTS = 3000  # 雙平行線分析每圖層上限（取最長的）
MAX_DEPTH = 8  # 巢狀 INSERT 展開深度上限

# ---------------------------------------------------------------- 名稱規則

CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _cn2int(s: str) -> int | None:
    if s.isdigit():
        return int(s)
    if not s or any(c not in CN_NUM for c in s):
        return None
    if s == "十":
        return 10
    if s.startswith("十"):
        return 10 + CN_NUM[s[1]]
    if len(s) == 1:
        return CN_NUM[s]
    if len(s) == 2 and s[1] == "十":
        return CN_NUM[s[0]] * 10
    if len(s) == 3 and s[1] == "十":
        return CN_NUM[s[0]] * 10 + CN_NUM[s[2]]
    return None


_NUMS = r"\d{1,2}|[一二三四五六七八九十]{1,3}"
_RE_B_CN = re.compile(rf"地下\s*({_NUMS})\s*[層樓F]", re.I)
_RE_B = re.compile(r"(?<![A-Z0-9])B\s*(\d{1,2})\s*F?(?![0-9])", re.I)
_RE_BASEMENT = re.compile(r"地下室|地下層|BASEMENT|(?<![A-Z])BSMT", re.I)
_RE_ROOF = re.compile(r"(?<![A-Z0-9])(?:RF|R\d?F|ROOF|PRF?\d?)(?![A-Z])|屋頂|屋突", re.I)
_RE_F = re.compile(r"(?<![A-Z0-9])(\d{1,2})\s*(?:F|FL)(?![A-Z])", re.I)
_RE_FL = re.compile(r"(?<![A-Z])FL?\s*(\d{1,2})(?![0-9])", re.I)
_RE_F_CN = re.compile(rf"(?<!地下)(?<!地下\s)({_NUMS})\s*[層樓](?!梯|板|高)")


def floor_tags(s: str) -> list[str]:
    """從字串抽樓層標記：'1F' 'B1' 'RF'，以及泛稱 'basement'。"""
    tags: list[str] = []

    def add(t: str) -> None:
        if t not in tags:
            tags.append(t)

    rest = s
    for m in _RE_B_CN.finditer(s):
        n = _cn2int(m.group(1))
        if n:
            add(f"B{n}")
    rest = _RE_B_CN.sub(" ", rest)
    for m in _RE_B.finditer(rest):
        add(f"B{int(m.group(1))}")
    if _RE_BASEMENT.search(rest) and not any(t.startswith("B") for t in tags):
        add("basement")
    if _RE_ROOF.search(rest):
        add("RF")
    rest2 = _RE_B.sub(" ", rest)
    for rx in (_RE_F, _RE_F_CN):
        for m in rx.finditer(rest2):
            n = _cn2int(m.group(1))
            if n:
                add(f"{n}F")
    if not any(t.endswith("F") and t[0].isdigit() for t in tags):
        for m in _RE_FL.finditer(rest2):
            add(f"{int(m.group(1))}F")
    return tags


# (role, regex, 分數, 說明)；分數取同 role 最大者
NAME_RULES: list[tuple[str, re.Pattern[str], float, str]] = [
    ("exterior_wall", re.compile(r"外牆|外框|外輪廓|EXT[-_ ]?WALL|WALL[-_ ]?EXT|OUTLINE|FOOTPRINT", re.I),
     0.6, "名稱含外牆 / 外框 / outline"),
    ("exterior_wall", re.compile(r"A[-_ ]?WALL|WALL|牆", re.I), 0.4, "名稱含 wall / 牆"),
    ("exterior_wall", re.compile(r"BLDG|BUILDING|建築|建物", re.I), 0.35, "名稱含 bldg / 建築"),
    ("site_boundary", re.compile(r"地界|基地|境界|地籍|界線|建築線|PROP(?:ERTY)?|SITE|BOUNDARY|LOT|"
                                 r"(?<![A-Z])PL(?![A-Z])", re.I), 0.6, "名稱含地界 / 基地 / PL"),
    ("annotation", re.compile(r"DIM|標註|尺寸|TEXT|TXT|文字|字|NOTE|說明|HATCH|填充|AXIS|GRID|軸線|"
                              r"家具|FURN|設備|EQUIP|SYMB|符號|DEFPOINTS", re.I), 0.6,
     "名稱像標註 / 文字 / 軸線 / 家具（通常不是外框）"),
    ("title_frame", re.compile(r"圖框|圖簽|FRAME|BORDER|TITLE|TTLB|X[-_ ]?FRAME", re.I), 0.6,
     "名稱像圖框 / 圖簽"),
]
NEG_WALL = re.compile(r"內牆|隔間|PARTITION|INT[-_ ]?WALL|WALL[-_ ]?INT|欄杆|RAIL|女兒牆|PARAPET|擋土|RETAIN",
                      re.I)
NON_GEOM_ROLES = {"annotation", "title_frame"}


# ---------------------------------------------------------------- 幾何


def _shoelace(pts: list[tuple[float, float]]) -> float:
    a = 0.0
    n = len(pts)
    for i in range(n):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % n]
        a += x0 * y1 - x1 * y0
    return abs(a) / 2


def _plen(pts: list[tuple[float, float]], closed: bool) -> float:
    segs = list(zip(pts, pts[1:] + (pts[:1] if closed else []), strict=False))
    return sum(math.dist(a, b) for a, b in segs)


def _poly_points(e) -> tuple[list[tuple[float, float]], bool] | None:
    """(頂點, 是否封閉)；bulge 忽略（面積為近似）。非 2D 線類回傳 None。"""
    t = e.dxftype()
    if t == "LWPOLYLINE":
        pts = [(float(x), float(y)) for x, y in e.get_points("xy")]
        closed = bool(e.closed)
    elif t == "POLYLINE":
        if e.is_poly_face_mesh or e.is_polygon_mesh:
            return None
        pts = [(float(p[0]), float(p[1])) for p in e.points()]
        closed = bool(e.is_closed)
    else:
        return None
    if len(pts) >= 3 and not closed and math.dist(pts[0], pts[-1]) < 1e-6 * max(1.0, _diag(pts)):
        pts, closed = pts[:-1], True  # 首尾重合視為封閉
    if closed and len(pts) >= 2 and pts[0] == pts[-1]:
        pts = pts[:-1]
    return pts, closed


def _diag(pts) -> float:
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return math.hypot(max(xs) - min(xs), max(ys) - min(ys)) if pts else 0.0


def _segments(e) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    t = e.dxftype()
    if t == "LINE":
        s, en = e.dxf.start, e.dxf.end
        return [((s.x, s.y), (en.x, en.y))]
    pp = _poly_points(e)
    if pp is None:
        return []
    pts, closed = pp
    ring = pts + pts[:1] if closed else pts
    return [(a, b) for a, b in zip(ring, ring[1:], strict=False) if a != b]


def double_line_ratio(segs, thick: tuple[float, float]) -> float:
    """有平行夥伴（間距在牆厚範圍、投影重疊 ≥ 30%）的線段長度比例。"""
    segs = sorted(segs, key=lambda s: -math.dist(*s))[:MAX_SEGMENTS]
    lo, hi = thick
    buckets: dict[int, list[int]] = defaultdict(list)
    info = []
    for i, (a, b) in enumerate(segs):
        dx, dy = b[0] - a[0], b[1] - a[1]
        L = math.hypot(dx, dy)
        if L <= 0:
            info.append(None)
            continue
        ang = math.degrees(math.atan2(dy, dx)) % 180
        info.append((a, (dx / L, dy / L), L))
        buckets[int(round(ang)) % 180].append(i)
    total = sum(x[2] for x in info if x)
    if total <= 0:
        return 0.0
    paired = [False] * len(segs)
    for k, idx in buckets.items():
        cand = idx + buckets.get((k + 1) % 180, []) + buckets.get((k - 1) % 180, [])
        for i in idx:
            if paired[i]:
                continue
            a, u, L = info[i]
            for j in cand:
                if j == i:
                    continue
                b, _, Lj = info[j]
                vx, vy = b[0] - a[0], b[1] - a[1]
                d = abs(vx * u[1] - vy * u[0])
                if not lo <= d <= hi:
                    continue
                t0 = vx * u[0] + vy * u[1]
                bend = segs[j][1]
                t1 = (bend[0] - a[0]) * u[0] + (bend[1] - a[1]) * u[1]
                ov = min(L, max(t0, t1)) - max(0.0, min(t0, t1))
                if ov >= 0.3 * min(L, Lj):
                    paired[i] = paired[j] = True
                    break
    return round(sum(info[i][2] for i in range(len(segs)) if paired[i] and info[i]) / total, 3)


def _bbox_of(e) -> tuple[float, float, float, float] | None:
    from ezdxf import bbox

    try:
        ext = bbox.extents([e], fast=True)
    except Exception:  # noqa: BLE001
        return None
    if not ext.has_data:
        return None
    return (ext.extmin.x, ext.extmin.y, ext.extmax.x, ext.extmax.y)


def _union(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _r(v: float, nd: int = 3) -> float:
    return float(round(v, nd))


def _rb(b) -> list[float] | None:
    return None if b is None else [_r(x) for x in b]


# ---------------------------------------------------------------- 讀檔


def _read(path: Path) -> tuple[Drawing | None, dict[str, Any]]:
    info: dict[str, Any] = {"mode": "normal", "auditor_fixes": 0, "auditor_errors": 0, "messages": []}
    if not path.exists():
        info["error"] = f"找不到檔案 {path}"
        return None, info
    head = path.read_bytes()[:6]
    if head.startswith(b"AC10"):
        info["error"] = f"{path.name} 是 DWG（{head.decode(errors='replace')}），請先用 ODA File Converter 轉 DXF"
        return None, info
    try:
        return ezdxf.readfile(str(path)), info
    except ezdxf.DXFStructureError as e:
        info["messages"].append(f"一般讀檔失敗：{e}")
    except Exception as e:  # noqa: BLE001
        info["messages"].append(f"一般讀檔失敗：{type(e).__name__}: {e}")
    from ezdxf import recover

    try:
        doc, auditor = recover.readfile(str(path))
    except Exception as e:  # noqa: BLE001
        info["mode"] = "failed"
        info["error"] = f"recover 也無法讀取：{type(e).__name__}: {e}"
        return None, info
    info["mode"] = "recover"
    info["auditor_fixes"] = len(auditor.fixes)
    info["auditor_errors"] = len(auditor.errors)
    info["messages"] += [f"fix: {f.message}" for f in auditor.fixes[:10]]
    info["messages"] += [f"error: {f.message}" for f in auditor.errors[:10]]
    return doc, info


# ---------------------------------------------------------------- 展開 INSERT


def _iter_flat(doc: Drawing, warnings: list[str]):
    """modelspace 實體（INSERT 遞迴展開成虛擬實體；layer 0 繼承 INSERT 圖層）。

    yield (entity, top_level_entity)
    """

    def expand(ins, layer: str, depth: int, top):
        if depth > MAX_DEPTH:
            warnings.append(f"INSERT {ins.dxf.name} 巢狀超過 {MAX_DEPTH} 層，停止展開")
            return
        try:
            subs = list(ins.virtual_entities())
        except Exception as ex:  # noqa: BLE001
            warnings.append(f"INSERT {ins.dxf.get('name', '?')} 無法展開：{type(ex).__name__}: {ex}")
            return
        for s in subs:
            if s.dxf.get("layer", "0") == "0":
                s.dxf.layer = layer
            if s.dxftype() == "INSERT":
                yield from expand(s, s.dxf.layer, depth + 1, top)
            else:
                yield s, top

    for e in doc.modelspace():
        if e.dxftype() == "INSERT":
            yield from expand(e, e.dxf.get("layer", "0"), 1, e)
        else:
            yield e, e


# ---------------------------------------------------------------- 單位


def _in(v: float, rng: tuple[float, float]) -> bool:
    return rng[0] <= v <= rng[1]


def guess_units(core: tuple[float, float, float, float] | None, size: float) -> dict[str, Any]:
    """依範圍大小與座標量級推測圖上單位（只是推測）。"""
    if core is None or size <= 0:
        return {"guess": None, "confidence": 0.0, "why": "沒有可量測的幾何", "twd97_like": None}
    cx, cy = (core[0] + core[2]) / 2, (core[1] + core[3]) / 2
    if _in(cx, TWD97_E) and _in(cy, TWD97_N):
        return {"guess": "m", "confidence": 0.9, "twd97_like": "m",
                "why": f"中心 ({cx:.0f}, {cy:.0f}) 落在 TWD97 TM2 範圍（m）"}
    if _in(cx / 1000, TWD97_E) and _in(cy / 1000, TWD97_N):
        return {"guess": "mm", "confidence": 0.9, "twd97_like": "mm",
                "why": f"中心 ({cx:.0f}, {cy:.0f}) ÷1000 落在 TWD97 TM2 範圍（mm 座標）"}
    lo, hi = BLDG_SIZE_M
    if _in(size / 1000, (lo, hi)):
        return {"guess": "mm", "confidence": 0.8, "twd97_like": None,
                "why": f"主體範圍 {size:.0f}；以 mm 計為 {size / 1000:.1f} m，屬典型建物尺度"}
    if _in(size, (lo, hi)):
        return {"guess": "m", "confidence": 0.8, "twd97_like": None,
                "why": f"主體範圍 {size:.1f}；以 m 計屬典型建物尺度（以 mm 計僅 {size / 1000:.3f} m）"}
    if size >= 2000:
        return {"guess": "mm", "confidence": 0.5, "twd97_like": None,
                "why": f"主體範圍 {size:.0f}，以 m 計過大；推測 mm（{size / 1000:.1f} m）"}
    if size >= 1:
        return {"guess": "m", "confidence": 0.5, "twd97_like": None,
                "why": f"主體範圍 {size:.1f}，以 mm 計過小；推測 m"}
    return {"guess": None, "confidence": 0.0, "twd97_like": None, "why": f"主體範圍 {size:g} 太小，無法推測"}


def _core_extents(boxes: list[tuple[float, float, float, float]]):
    """去掉離群實體後的範圍：實體中心的 2–98 百分位。"""
    if not boxes:
        return None
    cxs = sorted((b[0] + b[2]) / 2 for b in boxes)
    cys = sorted((b[1] + b[3]) / 2 for b in boxes)
    n = len(boxes)
    lo, hi = int(n * 0.02), max(int(math.ceil(n * 0.98)) - 1, 0)
    x0, x1, y0, y1 = cxs[lo], cxs[hi], cys[lo], cys[hi]
    sel = [b for b in boxes if x0 <= (b[0] + b[2]) / 2 <= x1 and y0 <= (b[1] + b[3]) / 2 <= y1]
    out = None
    for b in sel:
        out = _union(out, b)
    return out


# ---------------------------------------------------------------- 群聚


def _clusters(items: list[tuple[tuple[float, float, float, float], int]], gap: float):
    """bbox 擴張 gap/2 後相交者合併。items: [(bbox, weight)] → [(bbox, n)]"""
    cl: list[list[Any]] = []
    for b, w in sorted(items, key=lambda t: (t[0][0], t[0][1])):
        merged = [b, w]
        keep = []
        for c in cl:
            cb = c[0]
            if (merged[0][0] - gap <= cb[2] and cb[0] - gap <= merged[0][2]
                    and merged[0][1] - gap <= cb[3] and cb[1] - gap <= merged[0][3]):
                merged = [_union(merged[0], cb), merged[1] + c[1]]
            else:
                keep.append(c)
        keep.append(merged)
        cl = keep
    # 多輪合併直到穩定
    changed = True
    while changed:
        changed = False
        out: list[list[Any]] = []
        for c in cl:
            for o in out:
                ob = o[0]
                if (c[0][0] - gap <= ob[2] and ob[0] - gap <= c[0][2]
                        and c[0][1] - gap <= ob[3] and ob[1] - gap <= c[0][3]):
                    o[0], o[1] = _union(ob, c[0]), o[1] + c[1]
                    changed = True
                    break
            else:
                out.append(c)
        cl = out
    return sorted(((c[0], c[1]) for c in cl), key=lambda t: -t[1])


def _box_dist(p, b) -> float:
    dx = max(b[0] - p[0], 0, p[0] - b[2])
    dy = max(b[1] - p[1], 0, p[1] - b[3])
    return math.hypot(dx, dy)


# ---------------------------------------------------------------- 主函式

_MTEXT_CODES = re.compile(r"\\[A-Za-z][^;\\]*;|\\P|[{}]")


def _plain_text(e) -> str:
    try:
        if e.dxftype() == "MTEXT":
            return e.plain_text().replace("\n", " ").strip()
        return e.plain_text().strip()
    except Exception:  # noqa: BLE001
        return _MTEXT_CODES.sub(" ", e.dxf.get("text", "")).strip()


def _text_height(e) -> float:
    key = "char_height" if e.dxftype() == "MTEXT" else "height"
    try:
        return float(e.dxf.get(key, 0) or 0)
    except Exception:  # noqa: BLE001
        return 0.0


def inspect_dxf(path: str | Path) -> dict[str, Any]:
    """讀 DXF 並回傳體檢報告（dict，可 JSON 化）。讀不到時 ok=False、error 說明原因。"""
    report, _ = inspect_doc(path)
    return report


def inspect_doc(path: str | Path) -> tuple[dict[str, Any], Drawing | None]:
    """同 inspect_dxf，另回傳讀入的 Drawing（給 PNG 輸出重用）。"""
    path = Path(path)
    warnings: list[str] = []
    doc, rinfo = _read(path)
    report: dict[str, Any] = {"ok": doc is not None, "path": str(path), "read": rinfo, "warnings": warnings}
    if doc is None:
        report["error"] = rinfo.get("error", "無法讀取")
        return report, None
    rinfo.update(dxfversion=doc.dxfversion, release=doc.acad_release, encoding=doc.encoding)
    if rinfo["mode"] == "recover":
        warnings.append(f"一般讀檔失敗，以 recover 模式讀入（修復 {rinfo['auditor_fixes']}、"
                        f"錯誤 {rinfo['auditor_errors']}）；幾何可能不完整")

    # ---- 逐實體收集
    per_layer: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "types": Counter(), "closed": [], "open_len": 0.0, "segs": [], "hatch": 0, "hatch_paths": 0,
        "hatch_areas": [], "bbox": None,
        "styles": defaultdict(lambda: {"n": 0, "len": 0.0, "bbox": None})})
    top_boxes: dict[int, tuple[Any, Any]] = {}  # id(top) -> (top entity, bbox)
    texts: list[dict[str, Any]] = []
    all_texts_pos: list[tuple[str, tuple[float, float], list[str]]] = []
    for e, top in _iter_flat(doc, warnings):
        t = e.dxftype()
        lay = e.dxf.get("layer", "0")
        L = per_layer[lay]
        L["types"][t] += 1
        b = _bbox_of(e)
        if b is not None and t not in ("TEXT", "MTEXT", "ATTRIB", "ATTDEF"):
            L["bbox"] = _union(L["bbox"], b)
            k = id(top)
            prev = top_boxes.get(k)
            top_boxes[k] = (top, _union(prev[1] if prev else None, b))
        if t in ("TEXT", "MTEXT", "ATTRIB"):
            s = _plain_text(e)
            if s:
                ins = e.dxf.get("insert")
                pos = (float(ins.x), float(ins.y)) if ins is not None else (0.0, 0.0)
                tags = floor_tags(s)
                is_title = bool(tags) or bool(re.search(r"平面圖|PLAN|配置圖|立面|剖面", s, re.I))
                if is_title:
                    all_texts_pos.append((s, pos, tags))
                    if len(texts) < 200:
                        texts.append({"text": s[:80], "tags": tags, "layer": lay, "pos": [_r(pos[0]), _r(pos[1])],
                                      "height": _r(_text_height(e))})
            continue
        if t in ("LINE", "LWPOLYLINE", "POLYLINE", "ARC") and b is not None:
            g = L["styles"][(int(e.dxf.get("color", 256)), int(e.dxf.get("lineweight", -1)))]
            g["n"] += 1
            g["bbox"] = _union(g["bbox"], b)
            if t == "LINE":
                g["len"] += math.dist(*_segments(e)[0]) if _segments(e) else 0.0
            elif t == "ARC":
                g["len"] += math.radians((e.dxf.end_angle - e.dxf.start_angle) % 360 or 360) * e.dxf.radius
            elif (pp := _poly_points(e)) is not None:
                g["len"] += _plen(pp[0], pp[1])
        if t == "LINE":
            L["open_len"] += math.dist(*_segments(e)[0]) if _segments(e) else 0.0
            L["segs"] += _segments(e)
        elif t in ("LWPOLYLINE", "POLYLINE"):
            pp = _poly_points(e)
            if pp is None:
                continue
            pts, closed = pp
            if closed and len(pts) >= 3:
                L["closed"].append((_shoelace(pts), pts))
            else:
                L["open_len"] += _plen(pts, False)
            L["segs"] += _segments(e)
        elif t == "ARC":
            span = (e.dxf.end_angle - e.dxf.start_angle) % 360 or 360
            L["open_len"] += math.radians(span) * e.dxf.radius
        elif t == "HATCH":
            L["hatch"] += 1
            for p in e.paths:
                L["hatch_paths"] += 1
                verts = getattr(p, "vertices", None)
                if verts:
                    L["hatch_areas"].append(_shoelace([(float(v[0]), float(v[1])) for v in verts]))

    # ---- 範圍與單位
    boxes = [bb for _, bb in top_boxes.values() if bb is not None]
    extents = None
    for bb in boxes:
        extents = _union(extents, bb)
    core = _core_extents(boxes)
    size = max(core[2] - core[0], core[3] - core[1]) if core else 0.0
    insunits = int(doc.header.get("$INSUNITS", 0))
    hname = INSUNITS_NAMES.get(insunits, f"?({insunits})")
    ug = guess_units(core, size)
    guess = ug["guess"]
    unit_used = guess or (hname if hname in TO_M else None)
    to_m = TO_M.get(unit_used, 1.0) if unit_used else 1.0
    units = {"insunits": insunits, "insunits_name": hname, "extents": _rb(extents),
             "core_extents": _rb(core), "size": _r(size), **ug,
             "unit_used_for_stats": unit_used, "to_m": to_m}
    if guess is not None:
        if insunits == 0:
            warnings.append(f"$INSUNITS 未設定（0 = unitless）；推測圖上單位為 {guess}（{ug['why']}）")
        elif hname != guess:
            warnings.append(f"$INSUNITS 可能錯誤：header 為 {insunits}（{hname}），但推測圖上單位為 "
                            f"{guess}（{ug['why']}）")
    if unit_used is None:
        warnings.append("無法判斷單位：面積 / 長度統計以圖面單位計（未換算 m）")
    if extents and core and extents != core:
        ew = max(extents[2] - extents[0], extents[3] - extents[1])
        if size > 0 and ew > 3 * size:
            warnings.append(f"有離群實體：全範圍 {ew:.0f} 遠大於主體範圍 {size:.0f}（可能是散落在遠處的雜物或原點附近的點）")

    # ---- 圖層表
    thick = (WALL_THICK_M[0] / to_m, WALL_THICK_M[1] / to_m)
    a2 = to_m * to_m
    all_closed_max = max((a for L in per_layer.values() for a, _ in L["closed"]), default=0.0)
    layers_out: list[dict[str, Any]] = []
    names = [ly.dxf.name for ly in doc.layers]
    names += [n for n in per_layer if n not in names]
    for name in names:
        L = per_layer.get(name)
        try:
            ly = doc.layers.get(name)
        except Exception:  # noqa: BLE001
            ly = None
        n_ent = sum(L["types"].values()) if L else 0
        closed_areas = sorted((a for a, _ in L["closed"]), reverse=True) if L else []
        big = max(L["closed"], key=lambda t: t[0])[1] if L and L["closed"] else None
        big_bbox = (min(p[0] for p in big), min(p[1] for p in big), max(p[0] for p in big),
                    max(p[1] for p in big)) if big else None
        dl = double_line_ratio(L["segs"], thick) if L and L["segs"] else 0.0
        row: dict[str, Any] = {
            "name": name,
            "on": bool(ly.is_on()) if ly is not None else True,
            "frozen": bool(ly.is_frozen()) if ly is not None else False,
            "locked": bool(ly.is_locked()) if ly is not None else False,
            "color": int(abs(ly.dxf.get("color", 7))) if ly is not None else None,
            "linetype": ly.dxf.get("linetype", "Continuous") if ly is not None else None,
            "lineweight": int(ly.dxf.get("lineweight", -3)) if ly is not None else None,
            "defined": ly is not None,
            "entities": dict(L["types"]) if L else {},
            "n_entities": n_ent,
            "closed": {
                "count": len(closed_areas),
                "area_max_m2": _r(closed_areas[0] * a2, 2) if closed_areas else 0.0,
                "area_sum_m2": _r(sum(closed_areas) * a2, 2),
                "areas_top_m2": [_r(a * a2, 2) for a in closed_areas[:5]],
                "bbox_max": _rb(big_bbox),
            },
            "hatch": {"count": L["hatch"] if L else 0, "paths": L["hatch_paths"] if L else 0,
                      "area_max_m2": _r(max(L["hatch_areas"]) * a2, 2) if L and L["hatch_areas"] else 0.0},
            "open_length_m": _r((L["open_len"] if L else 0.0) * to_m, 2),
            "double_line_ratio": dl,
            "bbox": _rb(L["bbox"]) if L else None,
            "styles": _styles(L["styles"], to_m) if L else [],
        }
        row["hints"] = _hints(row, all_closed_max * a2, to_m) if n_ent else _name_hints(name)
        layers_out.append(row)
    if all(r["n_entities"] == 0 or r["name"] == "0" for r in layers_out) and per_layer.get("0"):
        warnings.append("所有幾何都在 0 層")

    _relational(layers_out, to_m)

    # ---- 候選
    cand: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in layers_out:
        for h in r["hints"]:
            cand[h["role"]].append({"layer": r["name"], "score": h["score"], "why": h["why"]})
    for v in cand.values():
        v.sort(key=lambda d: -d["score"])
    top = sorted(({"layer": r["name"], **h} for r in layers_out for h in r["hints"]
                  if h["role"] not in NON_GEOM_ROLES), key=lambda d: -d["score"])[:5]

    # ---- 圖塊 / xref / INSERT
    blocks = _blocks(doc, path)
    for x in blocks["xrefs"]:
        if not x["found"]:
            warnings.append(f"外部參照 {x['name']} 找不到檔案：{x['path']}")

    # ---- 圖面群聚
    gap = 3.0 / to_m if unit_used else (size * 0.02 if size else 1.0)
    items = [(bb, 1) for _, bb in top_boxes.values() if bb is not None]
    cls = _clusters(items, gap) if items else []
    clusters_out = []
    for i, (bb, n) in enumerate(cls[:15]):
        diag = math.hypot(bb[2] - bb[0], bb[3] - bb[1])
        near = [(s, tags) for s, p, tags in all_texts_pos if _box_dist(p, bb) <= max(0.25 * diag, gap)]
        ftags = []
        for _, tags in near:
            ftags += [t for t in tags if t not in ftags]
        clusters_out.append({"id": i, "bbox": _rb(bb), "n_entities": n,
                             "size_m": [_r((bb[2] - bb[0]) * to_m, 2), _r((bb[3] - bb[1]) * to_m, 2)],
                             "titles": [s[:60] for s, _ in near][:5], "floor_tags": ftags})
    if len(cls) > 15:
        warnings.append(f"群聚共 {len(cls)} 區，只列前 15（依實體數）")

    # ---- paperspace
    layouts = []
    for lo in doc.layouts:
        if lo.name == "Model":
            continue
        vps = [v for v in lo.query("VIEWPORT") if v.dxf.get("id", 0) != 1]
        layouts.append({"name": lo.name, "viewports": len(vps), "entities": len(lo)})

    report.update(units=units, layers=layers_out, candidates=dict(cand), top=top, blocks=blocks,
                  texts=texts, clusters=clusters_out, layouts=layouts)
    return report, doc


def _name_hints(name: str) -> list[dict[str, Any]]:
    best: dict[str, tuple[float, str]] = {}
    for role, rx, sc, why in NAME_RULES:
        if rx.search(name) and sc > best.get(role, (0, ""))[0]:
            best[role] = (sc, why)
    if "exterior_wall" in best and NEG_WALL.search(name):
        best["exterior_wall"] = (0.1, "名稱含 wall 但像內牆 / 欄杆 / 女兒牆")
    # 註記類命中時，外牆分數降低（例如 WALL-DIM）
    if "annotation" in best and "exterior_wall" in best:
        best["exterior_wall"] = (best["exterior_wall"][0] * 0.4, best["exterior_wall"][1] + "（但也像標註）")
    tags = floor_tags(name)
    for t in tags:
        if t.startswith("B") or t == "basement":
            best["basement"] = (0.6, f"名稱含地下層標記 {t}")
            if t != "basement":
                best[f"floor:{t}"] = (0.6, f"名稱含樓層 {t}")
        elif t == "RF":
            best["roof"] = (0.6, "名稱含屋頂 / RF")
            best["floor:RF"] = (0.6, "名稱含 RF")
        else:
            best[f"floor:{t}"] = (0.6, f"名稱含樓層 {t}")
    return [{"role": r, "score": _r(s, 2), "why": w} for r, (s, w) in best.items()]


def _hints(row: dict[str, Any], global_max_m2: float, to_m: float) -> list[dict[str, Any]]:
    hs = {h["role"]: h for h in _name_hints(row["name"])}

    def bump(role: str, add: float, why: str) -> None:
        h = hs.setdefault(role, {"role": role, "score": 0.0, "why": ""})
        h["score"] = _r(min(1.0, h["score"] + add), 2)
        h["why"] = (h["why"] + "；" if h["why"] else "") + why

    amax = row["closed"]["area_max_m2"]
    ncl = row["closed"]["count"]
    dl = row["double_line_ratio"]
    annot = "annotation" in hs or "title_frame" in hs
    if not annot:
        geo_wall = 0.0
        whys = []
        ev_bbox = row["bbox"]
        if _in(amax, BLDG_AREA_M2):
            ev_bbox = row["closed"]["bbox_max"]
            geo_wall += 0.25
            whys.append(f"最大封閉多邊形 {amax:.0f} m² 屬建物尺度")
        elif ncl == 0 and row["bbox"]:
            b = row["bbox"]
            ba = (b[2] - b[0]) * (b[3] - b[1]) * to_m * to_m
            per = 2 * ((b[2] - b[0]) + (b[3] - b[1])) * to_m
            if _in(ba, BLDG_AREA_M2) and row["open_length_m"] >= 0.5 * per:
                geo_wall += 0.1
                whys.append(f"無封閉多邊形，但線段範圍 {ba:.0f} m² 屬建物尺度")
        if dl >= 0.3:
            geo_wall += 0.25 * min(1.0, dl / 0.6)
            whys.append(f"雙平行線比例 {dl:.0%}（像牆）")
        env = [g for g in row["styles"] if 3 <= g["n"] <= 300 and _in(g["bbox_area_m2"], BLDG_AREA_M2)
               and 0.9 <= g["envelope"] <= 1.6]
        if env:
            g = max(env, key=lambda g: g["bbox_area_m2"])
            sub = "" if len(row["styles"]) == 1 else f"（color={g['color']} lw={g['lineweight']} 的 {g['n']} 條）"
            geo_wall += 0.3
            ev_bbox = g["bbox"]
            whys.append(f"線段{sub}總長 ≈ 範圍周長 ×{g['envelope']:.2f}，圍成 {g['bbox_area_m2']:.0f} m² 外圍")
        if geo_wall and "site_boundary" in hs:
            geo_wall *= 0.3
            whys.append("但名稱像基地線")
        if geo_wall and (whys or hs.get("exterior_wall")):
            if amax > 0 and global_max_m2 > 0 and amax >= 0.999 * global_max_m2 and dl < 0.3:
                geo_wall *= 0.5
                whys.append("但其封閉多邊形是全圖最大（也可能是地界 / 圖框）")
            if "exterior_wall" in hs and hs["exterior_wall"]["score"] <= 0.1:
                geo_wall *= 0.3
            bump("exterior_wall", geo_wall, "；".join(whys))
            hs["exterior_wall"]["bbox"] = ev_bbox
        elif "exterior_wall" in hs and hs["exterior_wall"]["score"] > 0.1:
            h = hs["exterior_wall"]
            h["score"] = _r(h["score"] * 0.7, 2)
            h["why"] += "；但沒有幾何佐證（無建物尺度封閉多邊形、非雙線、線段繞不成外圍）"
        # 地界：少量大型封閉多邊形、不像雙線牆
        if 1 <= ncl <= 3 and _in(amax, SITE_AREA_M2) and dl < 0.2:
            add = 0.2 if amax >= 0.999 * global_max_m2 else 0.1
            bump("site_boundary", add, f"{ncl} 個封閉多邊形、最大 {amax:.0f} m²、非雙線")
    # footprint 指一樓外牆：其他樓層的外牆候選略降、地下層的牆加 basement 分數
    tags = floor_tags(row["name"])
    h = hs.get("exterior_wall")
    if h is not None and h["score"] > 0.1:
        if "1F" in tags:
            h["score"] = _r(min(1.0, h["score"] + 0.05), 2)
            h["why"] += "；屬 1F"
        elif tags:
            h["score"] = _r(h["score"] * 0.75, 2)
            h["why"] += f"；屬 {'/'.join(tags)} 層（footprint 通常取 1F）"
        if "basement" in hs:
            hb = hs["basement"]
            hb["score"] = _r(min(1.0, hb["score"] + 0.3 * h["score"] / 0.8), 2)
            hb["why"] += "；且本身像外牆"
    return sorted(hs.values(), key=lambda h: -h["score"])


def _styles(groups: dict, to_m: float) -> list[dict[str, Any]]:
    """依 (實體顏色, 線寬) 分組的線類統計；envelope = 總長 / bbox 周長（≈1 表示線段圍成外圍）。"""
    out = []
    for (color, lw), g in sorted(groups.items(), key=lambda kv: -kv[1]["n"])[:6]:
        b = g["bbox"]
        per = 2 * ((b[2] - b[0]) + (b[3] - b[1]))
        out.append({"color": color, "lineweight": lw, "n": g["n"], "length_m": _r(g["len"] * to_m, 2),
                    "bbox": _rb(b), "bbox_area_m2": _r((b[2] - b[0]) * (b[3] - b[1]) * to_m * to_m, 1),
                    "envelope": _r(g["len"] / per, 3) if per > 0 else 0.0})
    return out


def _contains(outer, inner, margin: float) -> bool:
    return (outer[0] + margin <= inner[0] and outer[1] + margin <= inner[1]
            and inner[2] <= outer[2] - margin and inner[3] <= outer[3] - margin)


def _relational(rows: list[dict[str, Any]], to_m: float) -> None:
    """圖層之間的關係修正外牆分數：
    - 範圍包住基地線候選 → 較像圖框（×0.3）
    - 範圍完全落在另一個外牆候選之內（內縮 ≥ 5 cm）→ 較像內牆 / 舊版較小外框（×0.6）
    """

    def ew(r):
        return next((h for h in r["hints"] if h["role"] == "exterior_wall"), None)

    sites = [r for r in rows if r["bbox"] and any(h["role"] == "site_boundary" and h["score"] >= 0.5
                                                    for h in r["hints"])]
    margin = 0.05 / to_m
    frames = set()
    for r in rows:
        h = ew(r)
        if h is None or not r["bbox"] or h["score"] <= 0.1:
            continue
        eb = h.get("bbox") or r["bbox"]
        s = next((x for x in sites if x["name"] != r["name"]
                  and _contains(eb, x["closed"]["bbox_max"] or x["bbox"], 0)), None)
        if s is not None:
            frames.add(r["name"])
            h["score"] = _r(h["score"] * 0.3, 2)
            h["why"] += f"；但範圍包住基地線候選 {s['name']}（較像圖框）"
    walls = [r for r in rows if r["bbox"] and r["name"] not in frames and (h := ew(r)) and h["score"] >= 0.3]
    for r in walls:
        h = ew(r)
        eb = h.get("bbox") or r["bbox"]
        outer = next((o for o in walls if o is not r
                      and _contains(ew(o).get("bbox") or o["bbox"], eb, margin)), None)
        if outer is not None:
            h["score"] = _r(h["score"] * 0.6, 2)
            h["why"] += f"；但範圍完全在 {outer['name']} 之內（較像內牆 / 舊版外框）"
    for r in rows:
        r["hints"].sort(key=lambda x: -x["score"])


def _blocks(doc: Drawing, path: Path) -> dict[str, Any]:
    ins_count: Counter[str] = Counter()
    scales: list[float] = []
    non_uniform = mirrored = 0
    rotations: Counter[float] = Counter()
    for c in doc.blocks:  # 含 *Model_Space / *Paper_Space 等 layout block
        for e in c.query("INSERT"):
            ins_count[e.dxf.name] += 1
            sx, sy = e.dxf.get("xscale", 1.0), e.dxf.get("yscale", 1.0)
            scales += [abs(sx), abs(sy)]
            if not math.isclose(abs(sx), abs(sy), rel_tol=1e-6):
                non_uniform += 1
            if sx * sy < 0:
                mirrored += 1
            rotations[round(e.dxf.get("rotation", 0.0) % 360, 2)] += 1
    defs, xrefs = [], []
    anon = 0
    for blk in doc.blocks:
        name = blk.name
        if blk.is_any_layout:
            continue
        if blk.is_alive and blk.block is not None and blk.block.is_xref:
            p = blk.block.dxf.get("xref_path", "")
            cand = [Path(p), path.parent / p, path.parent / Path(p.replace("\\", "/")).name] if p else []
            xrefs.append({"name": name, "path": p, "found": any(c.exists() for c in cand),
                          "inserted": ins_count[name]})
            continue
        if name.startswith("*"):
            anon += 1
            continue
        types = Counter(e.dxftype() for e in blk)
        defs.append({"name": name, "entities": sum(types.values()), "types": dict(types),
                     "inserted": ins_count[name], "nested": types.get("INSERT", 0) > 0})
    defs.sort(key=lambda d: (-d["inserted"], d["name"]))
    return {
        "definitions": defs, "anonymous": anon, "xrefs": xrefs,
        "inserts": {"count": sum(ins_count.values()),
                    "scale_min": _r(min(scales), 6) if scales else None,
                    "scale_max": _r(max(scales), 6) if scales else None,
                    "non_uniform": non_uniform, "mirrored": mirrored,
                    "rotations": {str(k): v for k, v in rotations.most_common(8)}},
    }


# ---------------------------------------------------------------- PNG


def layer_pngs(doc: Drawing, report: dict[str, Any], out_dir: str | Path, *, max_layers: int = 6,
               min_score: float = 0.3, dpi: int = 150, max_px: int = 1600) -> list[str]:
    """每個候選圖層單獨上色（紅），其他圖層淡灰。會改動傳入 doc 的顏色（只在記憶體）。

    不用 render_png：它對 mm 實尺圖的 dpi 下限 10 會產生數萬像素的圖；這裡固定長邊 max_px。
    """

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    seen: list[str] = []
    for d in sorted(({"layer": r["name"], "score": h["score"]} for r in report["layers"]
                     for h in r["hints"] if h["role"] not in NON_GEOM_ROLES and h["score"] >= min_score),
                    key=lambda d: -d["score"]):
        if d["layer"] not in seen:
            seen.append(d["layer"])
    seen = seen[:max_layers]
    if not seen:
        return []
    # 實體顏色改 BYLAYER，才能用圖層顏色控制
    for c in [doc.modelspace(), *doc.blocks]:
        for e in c:
            try:
                e.dxf.color = 256
                if e.dxf.hasattr("true_color"):
                    e.dxf.discard("true_color")
            except Exception:  # noqa: BLE001
                pass
    out: list[str] = []
    for i, name in enumerate(seen):
        for ly in doc.layers:
            ly.color = 1 if ly.dxf.name == name else 253
            if ly.dxf.name == name:
                ly.on()
                ly.thaw()
        safe = re.sub(r"[^\w\-]+", "_", name)
        out.append(str(_render_small(doc, out_dir / f"{i:02d}_{safe}.png", max_px=max_px)))
    return out


def _render_small(doc: Drawing, png_path: Path, *, max_px: int = 1600) -> Path:
    """modelspace → 長邊 max_px 的 PNG（白底、線寬固定細線）。"""
    from ezdxf.addons.drawing import Frontend, RenderContext
    from ezdxf.addons.drawing.config import BackgroundPolicy, Configuration, LineweightPolicy
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

    from draftagent.render.preview import _setup_matplotlib, substitute_missing_fonts

    plt = _setup_matplotlib()
    substitute_missing_fonts(doc)
    fig = plt.figure()
    ax = fig.add_axes((0, 0, 1, 1))
    ax.set_axis_off()
    cfg = Configuration(background_policy=BackgroundPolicy.WHITE,
                        lineweight_policy=LineweightPolicy.RELATIVE, lineweight_scaling=0.6)
    Frontend(RenderContext(doc), MatplotlibBackend(ax), config=cfg).draw_layout(doc.modelspace(), finalize=True)
    x0, x1 = ax.get_xlim()
    y0, y1 = ax.get_ylim()
    w, h = max(x1 - x0, 1e-9), max(y1 - y0, 1e-9)
    inch = 10.0
    fig.set_size_inches(inch if w >= h else inch * w / h, inch if h > w else inch * h / w)
    fig.savefig(png_path, dpi=max_px / inch, facecolor="#FFFFFF")
    plt.close(fig)
    return png_path


# ---------------------------------------------------------------- 人類可讀


def format_report(rep: dict[str, Any]) -> str:
    if not rep["ok"]:
        return f"讀取失敗：{rep.get('error')}\n" + "\n".join(rep["read"].get("messages", []))
    u, rd = rep["units"], rep["read"]
    lines = [f"{rep['path']}  DXF {rd['dxfversion']}（{rd['release']}）encoding={rd['encoding']}  "
             f"讀檔={rd['mode']}"]
    lines.append(f"[單位] $INSUNITS={u['insunits']}（{u['insunits_name']}）  推測={u['guess']} "
                 f"(信心 {u['confidence']:.1f})：{u['why']}")
    lines.append(f"       主體範圍 {u['core_extents']}  全範圍 {u['extents']}")
    lines.append("[前 5 名候選圖層]")
    for d in rep["top"]:
        lines.append(f"  {d['score']:.2f}  {d['role']:<14} {d['layer']}  — {d['why']}")
    if not rep["top"]:
        lines.append("  （無）")
    lines.append("[圖層]")
    rows = [r for r in rep["layers"] if r["n_entities"]]
    w = min(max((len(r["name"]) for r in rows), default=4), 28)
    lines.append(f"  {'name':<{w}} st  ents  closed  max_m2      open_m   dbl   hints")
    for r in rows:
        st = ("-" if not r["on"] else "o") + ("F" if r["frozen"] else " ")
        hs = ", ".join(f"{h['role']}:{h['score']:.2f}" for h in r["hints"][:3])
        lines.append(f"  {r['name'][:w]:<{w}} {st}  {r['n_entities']:>4}  {r['closed']['count']:>6}  "
                     f"{r['closed']['area_max_m2']:>10.1f}  {r['open_length_m']:>8.1f}  "
                     f"{r['double_line_ratio']:>4.2f}  {hs}")
    empty = [r["name"] for r in rep["layers"] if not r["n_entities"]]
    if empty:
        lines.append(f"  空圖層 {len(empty)}：{', '.join(empty[:12])}{' …' if len(empty) > 12 else ''}")
    b = rep["blocks"]
    lines.append(f"[圖塊] 定義 {len(b['definitions'])}（匿名 {b['anonymous']}）  INSERT {b['inserts']['count']}  "
                 f"縮放 {b['inserts']['scale_min']}–{b['inserts']['scale_max']}  非等比 "
                 f"{b['inserts']['non_uniform']}  鏡射 {b['inserts']['mirrored']}")
    for d in b["definitions"][:8]:
        lines.append(f"  {d['name']}: {d['entities']} 實體, 插入 {d['inserted']}{', 巢狀' if d['nested'] else ''}")
    for x in b["xrefs"]:
        lines.append(f"  XREF {x['name']} → {x['path']} {'OK' if x['found'] else '找不到'}")
    lines.append(f"[圖面群聚] {len(rep['clusters'])} 區")
    for c in rep["clusters"][:8]:
        lines.append(f"  #{c['id']} {c['n_entities']} 實體 {c['size_m'][0]}×{c['size_m'][1]} m  "
                     f"樓層 {c['floor_tags'] or '-'}  圖名 {c['titles'][:2] or '-'}")
    if rep["layouts"]:
        lines.append("[paperspace] " + ", ".join(f"{lo['name']}({lo['viewports']} vp)" for lo in rep["layouts"]))
    if rep["warnings"]:
        lines.append("[warnings]")
        lines += [f"  ! {w}" for w in rep["warnings"]]
    lines.append("（候選只是提示；圖層角色與單位請確認後寫進 ArchImportSpec）")
    return "\n".join(lines)
