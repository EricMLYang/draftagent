"""多房間平面圖（floor-plan）verifier。純函式，不得呼叫 LLM。

三個 verifier（`draftagent build` 依序執行；CLI 一次跑三個）：

`check_floorplan(spec)` → `floorplan_check`（spec 幾何 + 拓樸）
- 幾何（ERROR）：外框 / 房間多邊形有效；房間全部在室內範圍（外牆內緣）內；兩兩不重疊；
  房間聯集鋪滿室內範圍（未分配面積 ≤ `COVER_TOL` × 室內面積——房間以外牆內緣 / 內牆中心線為界，
  兩者差只來自牆厚的幾何取整）；每間淨空非空且為單一區塊
- 門窗（ERROR）：依合約（schema/floorplan.py）找到牆段；**獨立**驗證開口位於兩空間共用邊界上
  （室外 = 外牆內緣）；門寬 ≤ 牆段長 − 2 × 牆厚；同一牆段開口不重疊；窗在外牆上且屬於該房間邊界
- 拓樸（ERROR）：門 = 邊的連通圖，所有房間都可由主入口（不經室外）到達；動線空間
  （plan_rules.yaml `uses.circulation`）若有，彼此以門連通；居室類（`uses.window_required`）
  至少一扇窗（結構規則，**不檢查採光比例**）
- 慣例（WARNING，plan_rules.yaml kind: convention，無法規意義）：浴廁只能經由臥室進入、
  房間淨寬過窄、長寬比過大
- metrics：每房淨面積（m²、坪）/ 外接尺寸 / 長寬比、共用牆長度、門連接、可達性、窗數、
  動線面積比、平均長寬比、主入口方位

`check_floorplan_dxf(dxf, spec)` → `floorplan_dxf_check`（DXF 讀回）
- INSUNITS=mm、必要圖層、0 層無實體、A-WALL 只有封閉 polyline
- 牆：由 spec 獨立重建（外牆環 + 共用邊界兩側各半內牆厚 − 開口）與 A-WALL（奇偶規則）對稱差
  ≤ 0.5%；外牆線寬 > 內牆線寬
- 門：每扇一條 A-DOOR ARC（圓心 = 鉸點、半徑 = 門寬、90°、開向）；窗：A-WIND 封閉框落在開口內
- 房名 TEXT 在該房淨空內；面積 TEXT「x.xx m²」在該房淨空內且 = spec 淨面積（0.01 m²）
- DIMENSION：讀實際顯示文字（verify/dimtext.py；dimrnd / dimpost / dimalt 禁用、dimlfac = 1）；
  外總尺寸存在；尺寸鏈端點都在外緣或內牆中心線上、相接且總和 = 總尺寸
- 印出字高 ≥ layers.yaml 下限、文字不重疊（legibility_check）；文字壓牆 / 門 / 窗 → warning

`check_floorplan_ifc(ifc, spec)` → `floorplan_ifc_check`（ifcopenshell 讀回）
- IFC4、長度單位 mm（IfcSIUnit MILLI METRE）；Project → Site → Building → Storey 各一且聚合
- IfcSpace 數 = 房間數、Name / LongName（用途）一致、NetFloorArea 與 spec 淨面積一致、
  輪廓多邊形面積一致
- IfcWall 數 = 外框邊數 + 內牆直線段數，Pset_WallCommon.IsExternal 數 = 外框邊數
- IfcDoor / IfcWindow 數與 id 一致、寬度一致；每個都有 IfcRelFillsElement → IfcOpeningElement
  → IfcRelVoidsElement → IfcWall（外 / 內牆別與 spec 一致）；開口放置點 = spec 開口起點（1 mm）
- 牆 / 門 / 窗都包含在樓層（IfcRelContainedInSpatialStructure）

通過只代表「通過本 repo 現有 verifier 的檢查項目」，不代表符合建築法規（不檢查走道寬、採光比例、
防火、無障礙等）。

CLI：python -m draftagent.verify.floorplan_check <spec.json> [--dxf plan.dxf] [--ifc plan.ifc] [--json]
"""

from __future__ import annotations

import argparse
import math
import re
from collections import deque
from functools import cache
from pathlib import Path

import yaml
from shapely.geometry import LineString, MultiPolygon, Point, Polygon
from shapely.ops import unary_union

from draftagent.schema.floorplan import (
    OUTSIDE,
    FloorPlanSpec,
    LocateError,
    Placement,
    door_placement,
    exterior_edges,
    interior_wall_lines,
    side_of,
    window_placement,
)
from draftagent.verify.base import VerifyResult

RULES_YAML = Path(__file__).resolve().parents[1] / "render" / "codes" / "plan_rules.yaml"
NAME = "floorplan_check"
NAME_DXF = "floorplan_dxf_check"
NAME_IFC = "floorplan_ifc_check"
COVER_TOL = 0.005  # 未分配面積 / 室內面積
AREA_TOL_MM2 = 1.0  # 重疊 / 超出容差 mm²
ON_LINE_TOL = 1.0  # 開口在共用邊界上的容差 mm
WALL_SYMDIFF_MAX = 0.005
POS_TOL = 1.0
ANGLE_TOL = 1.0
AREA_TEXT_TOL = 0.01  # m²
LINE_LAYERS = ("A-WALL", "A-DOOR", "A-WIND")
REQUIRED_LAYERS = ("A-WALL", "A-DOOR", "A-WIND", "A-DIMS", "A-TEXT")
PING = 0.3025


# ---------------------------------------------------------------- rules yaml


@cache
def _rules_cached(path: str) -> dict:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if data.get("kind") != "convention":
        raise ValueError(f"{path}: kind 必須為 'convention'（配置慣例，非法規數值）")
    uses = data.get("uses") or {}
    for k in ("window_required", "circulation", "bedroom", "bathroom"):
        if not isinstance(uses.get(k), list):
            raise ValueError(f"{path}: 缺少 uses.{k}（list）")
    return data


def load_rules(path: str | Path = RULES_YAML) -> dict:
    return _rules_cached(str(Path(path).resolve()))


# ---------------------------------------------------------------- 獨立幾何（verifier 自用）


def _poly(pts) -> Polygon:
    return Polygon([(float(x), float(y)) for x, y in pts])


def _inner(spec: FloorPlanSpec):
    return _poly(spec.outline.outline()).buffer(-spec.wall_thickness.exterior, join_style="mitre")


def _interior_band(spec: FloorPlanSpec):
    """內牆區域：所有房間兩兩共用邊界（線狀部分）往兩側各半內牆厚（端點方形延伸），裁到室內範圍。"""
    half = spec.wall_thickness.interior / 2
    polys = [_poly(r.outline()) for r in spec.rooms]
    lines = []
    for i in range(len(polys)):
        for j in range(i + 1, len(polys)):
            g = polys[i].boundary.intersection(polys[j].boundary)
            for part in getattr(g, "geoms", [g]):
                if isinstance(part, LineString) and part.length > 1e-3:
                    lines.append(part)
                elif hasattr(part, "geoms"):
                    lines += [x for x in part.geoms if isinstance(x, LineString) and x.length > 1e-3]
    if not lines:
        return Polygon()
    band = unary_union([ln.buffer(half, cap_style="square", join_style="mitre") for ln in lines])
    return band.intersection(_inner(spec))


def net_polygons(spec: FloorPlanSpec) -> dict[str, object]:
    band = _interior_band(spec)
    return {r.name: _poly(r.outline()).difference(band) for r in spec.rooms}


def _mv(p, v, s):
    return (p[0] + v[0] * s, p[1] + v[1] * s)


def _opening_poly(pl: Placement, eps: float = 0.0) -> Polygon:
    n = pl.n_out
    lo, hi = (-eps, pl.thickness + eps) if pl.exterior else (-pl.thickness / 2 - eps, pl.thickness / 2 + eps)
    return Polygon([_mv(pl.a, n, lo), _mv(pl.b, n, lo), _mv(pl.b, n, hi), _mv(pl.a, n, hi)])


def _placements(spec: FloorPlanSpec) -> tuple[dict[str, Placement], dict[str, str]]:
    ok: dict[str, Placement] = {}
    bad: dict[str, str] = {}
    for d in spec.doors:
        try:
            ok[d.id] = door_placement(spec, d)
        except (LocateError, KeyError) as e:
            bad[d.id] = str(e)
    for w in spec.windows:
        try:
            ok[w.id] = window_placement(spec, w)
        except (LocateError, KeyError) as e:
            bad[w.id] = str(e)
    return ok, bad


def expected_walls(spec: FloorPlanSpec):
    """(外牆區域, 內牆區域) 扣開口（法向各多 1 mm）。"""
    outer = _poly(spec.outline.outline())
    inner = _inner(spec)
    ext = outer.difference(inner)
    intr = _interior_band(spec)
    pls, _ = _placements(spec)
    if pls:
        cut = unary_union([_opening_poly(p, 1.0) for p in pls.values()])
        ext, intr = ext.difference(cut), intr.difference(cut)
    return ext, intr


def _rect_dims(g) -> tuple[float, float]:
    """最小外接矩形 (長邊, 短邊) mm。"""
    mrr = g.minimum_rotated_rectangle
    pts = list(mrr.exterior.coords)[:3]
    a, b = math.dist(pts[0], pts[1]), math.dist(pts[1], pts[2])
    return max(a, b), min(a, b)


def _key(a: str, b: str) -> str:
    return "|".join(sorted((a, b)))


# ---------------------------------------------------------------- spec 幾何 + 拓樸


def check_floorplan(spec: FloorPlanSpec, rules_path: str | Path = RULES_YAML) -> VerifyResult:
    res = VerifyResult(name=NAME)
    try:
        rules = load_rules(rules_path)
    except (OSError, ValueError) as e:
        res.error(f"plan_rules.yaml 讀取失敗：{e}")
        return res
    uses = rules["uses"]
    t_ext, t_int = spec.wall_thickness.exterior, spec.wall_thickness.interior

    outer = _poly(spec.outline.outline())
    if not outer.is_valid:
        res.error("outline 多邊形自相交或無效")
        return res
    inner = _inner(spec)
    if inner.is_empty or isinstance(inner, MultiPolygon):
        res.error(f"外牆厚 {t_ext:g} 使室內範圍為空或分裂，outline 太小")
        return res
    W, D = spec.overall_dims()
    res.metrics["overall_mm"] = [round(W, 3), round(D, 3)]
    res.metrics["inner_area_m2"] = round(inner.area / 1e6, 3)

    polys: dict[str, Polygon] = {}
    for r in spec.rooms:
        p = _poly(r.outline())
        if not p.is_valid:
            res.error(f"房間「{r.name}」多邊形自相交或無效")
            continue
        polys[r.name] = p
        out = p.difference(inner).area
        if out > AREA_TOL_MM2:
            res.error(
                f"房間「{r.name}」有 {out / 1e6:.3f} m² 超出室內範圍（外牆內緣）；房間應以外牆內緣"
                f"（外框往內 {t_ext:g}）與內牆中心線為界"
            )
    names = list(polys)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            ov = polys[names[i]].intersection(polys[names[j]]).area
            if ov > AREA_TOL_MM2:
                res.error(f"房間「{names[i]}」與「{names[j]}」重疊 {ov / 1e6:.3f} m²")
    if polys:
        union = unary_union(list(polys.values()))
        gap = inner.difference(union).area
        res.metrics["unassigned_m2"] = round(gap / 1e6, 3)
        if gap > COVER_TOL * inner.area:
            res.error(
                f"室內範圍有 {gap / 1e6:.3f} m² 未分配給任何房間（> {COVER_TOL:.1%}）；"
                "房間必須鋪滿外牆內緣圍出的範圍（走廊 / 玄關也要列為房間）"
            )

    # 淨空
    nets = net_polygons(spec)
    room_metrics: dict[str, dict] = {}
    for r in spec.rooms:
        net = nets[r.name]
        if net.is_empty:
            res.error(f"房間「{r.name}」扣除內牆後沒有淨空")
            continue
        if isinstance(net, MultiPolygon) and len([g for g in net.geoms if g.area > 1.0]) > 1:
            res.error(f"房間「{r.name}」被內牆切成多塊（多邊形太窄或形狀異常）")
        L, S = _rect_dims(net)
        room_metrics[r.name] = {
            "use": r.use,
            "area_m2": round(net.area / 1e6, 2),
            "area_ping": round(net.area / 1e6 * PING, 2),
            "long_mm": round(L, 1),
            "short_mm": round(S, 1),
            "aspect": round(L / S, 3) if S > 0 else None,
        }
    res.metrics["rooms"] = room_metrics
    res.metrics["n_rooms"] = len(spec.rooms)

    # 共用牆
    shared: dict[str, float] = {}
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            g = polys[names[i]].boundary.intersection(polys[names[j]].boundary)
            if g.length > 1e-3:
                shared[_key(names[i], names[j])] = round(g.length, 1)
    res.metrics["shared_walls_mm"] = dict(sorted(shared.items()))
    ext_len = {
        n: round(p.boundary.intersection(inner.boundary).length, 1) for n, p in polys.items()
    }
    res.metrics["exterior_wall_mm"] = ext_len

    # 門窗
    pls, bad = _placements(spec)
    for msg in bad.values():
        res.error(msg)
    inner_b = inner.boundary
    spans_by_wall: dict[tuple, list[tuple[float, float, str]]] = {}
    for d in spec.doors:
        pl = pls.get(d.id)
        if pl is None:
            continue
        seg = LineString([pl.a, pl.b])
        rooms_ = d.rooms()
        bounds = [polys[r].boundary for r in rooms_ if r in polys]
        if OUTSIDE in d.between:
            bounds.append(inner_b)
        for bnd in bounds:
            if seg.difference(bnd.buffer(ON_LINE_TOL)).length > ON_LINE_TOL:
                res.error(f"door {d.id}：開口不在 {d.between[0]}–{d.between[1]} 的共用牆上")
                break
        t = t_ext if pl.exterior else t_int
        if d.width > pl.seg.length - 2 * t + 1e-6:
            res.error(f"door {d.id}：門寬 {d.width:g} > 牆段長 {pl.seg.length:g} − 2 × 牆厚 {t:g}")
        spans_by_wall.setdefault((pl.seg.start, pl.seg.end), []).append(
            (pl.offset, pl.offset + pl.width, d.id)
        )
    windows_by_room: dict[str, list[str]] = {r.name: [] for r in spec.rooms}
    for w in spec.windows:
        pl = pls.get(w.id)
        if pl is None:
            continue
        seg = LineString([pl.a, pl.b])
        if not pl.exterior or seg.difference(inner_b.buffer(ON_LINE_TOL)).length > ON_LINE_TOL:
            res.error(f"window {w.id}：不在外牆上")
        elif w.room in polys and seg.difference(polys[w.room].boundary.buffer(ON_LINE_TOL)).length > ON_LINE_TOL:
            res.error(f"window {w.id}：不在房間「{w.room}」的邊界上")
        else:
            windows_by_room[w.room].append(w.id)
        spans_by_wall.setdefault((pl.seg.start, pl.seg.end), []).append(
            (pl.offset, pl.offset + pl.width, w.id)
        )
    for spans in spans_by_wall.values():
        spans.sort()
        for (a0, a1, i), (b0, b1, j) in zip(spans, spans[1:], strict=False):
            if b0 < a1 - 1e-6:
                res.error(f"{i} [{a0:g}, {a1:g}] 與 {j} [{b0:g}, {b1:g}] 在同一牆段重疊")
    res.metrics["n_doors"] = len(spec.doors)
    res.metrics["n_windows"] = len(spec.windows)
    res.metrics["windows_by_room"] = {k: len(v) for k, v in windows_by_room.items()}

    # 拓樸
    graph: dict[str, set[str]] = {r.name: set() for r in spec.rooms}
    door_links: dict[str, list[str]] = {}
    for d in spec.doors:
        if d.id not in pls:
            continue
        a, b = d.between
        door_links.setdefault(_key(a, b), []).append(d.id)
        if OUTSIDE not in (a, b):
            graph[a].add(b)
            graph[b].add(a)
    res.metrics["door_links"] = dict(sorted(door_links.items()))
    ent = spec.door(spec.entrance)
    start = ent.rooms()[0]
    seen = {start} if ent.id in pls else set()
    q = deque(seen)
    while q:
        cur = q.popleft()
        for nb in graph[cur]:
            if nb not in seen:
                seen.add(nb)
                q.append(nb)
    unreachable = [r.name for r in spec.rooms if r.name not in seen]
    res.metrics["entrance"] = {
        "door": ent.id,
        "room": start,
        "side": side_of(pls[ent.id].n_out) if ent.id in pls else None,
        "width": ent.width,
    }
    res.metrics["reachable"] = not unreachable
    res.metrics["unreachable"] = unreachable
    if unreachable:
        res.error(f"由主入口 {ent.id}（{start}）經室內門無法到達：{unreachable}")

    circ = [r.name for r in spec.rooms if r.use in uses["circulation"]]
    if len(circ) > 1:
        sub = set(circ)
        seen_c = {circ[0]}
        q = deque(seen_c)
        while q:
            cur = q.popleft()
            for nb in graph[cur] & sub:
                if nb not in seen_c:
                    seen_c.add(nb)
                    q.append(nb)
        if seen_c != sub:
            res.error(f"動線空間（{'/'.join(uses['circulation'])}）彼此未以門連通：{sorted(sub - seen_c)}")
    res.metrics["circulation_connected"] = len(circ) <= 1 or seen_c == set(circ)

    for r in spec.rooms:
        if r.use in uses["window_required"] and not windows_by_room[r.name]:
            res.error(f"居室「{r.name}」（{r.use}）沒有任何對外窗（至少一扇；結構規則，非採光比例）")

    # 慣例 warning
    warn = rules.get("warnings") or {}
    if warn.get("bathroom_only_via_bedroom"):
        for r in spec.rooms:
            if r.use in uses["bathroom"] and graph[r.name]:
                nbs = graph[r.name]
                if all(spec.room(n).use in uses["bedroom"] for n in nbs):
                    res.warn(
                        f"浴廁「{r.name}」只能經由臥室（{sorted(nbs)}）進入（慣例提示；主臥套房可忽略）"
                    )
    mw = (warn.get("min_clear_width_mm") or {}).get("value")
    ma = (warn.get("max_aspect_ratio") or {}).get("value")
    for name, m in room_metrics.items():
        if mw and m["short_mm"] < mw:
            res.warn(f"房間「{name}」淨寬 {m['short_mm']:.0f} mm < {mw:g}（plan_rules.yaml 慣例，無法規意義）")
        if ma and m["use"] not in uses["circulation"] and m["aspect"] and m["aspect"] > ma:
            res.warn(f"房間「{name}」長寬比 {m['aspect']:.2f} > {ma:g}（慣例提示）")

    net_total = sum(m["area_m2"] for m in room_metrics.values())
    circ_area = sum(m["area_m2"] for n, m in room_metrics.items() if m["use"] in uses["circulation"])
    res.metrics["net_area_total_m2"] = round(net_total, 2)
    res.metrics["circulation_area_ratio"] = round(circ_area / net_total, 4) if net_total else None
    asp = [m["aspect"] for m in room_metrics.values() if m["aspect"] and m["use"] not in uses["circulation"]]
    res.metrics["mean_aspect_ratio"] = round(sum(asp) / len(asp), 3) if asp else None
    return res


# ---------------------------------------------------------------- DXF 讀回


def _door_expect(spec: FloorPlanSpec, d, pl: Placement):
    first = d.rooms()[0]
    toward_first = (-pl.n_out[0], -pl.n_out[1])
    n_in = toward_first if d.swing_room() == first else pl.n_out
    face = 0.0 if pl.exterior else pl.thickness / 2
    hinge = _mv(pl.a if d.hinge == "start" else pl.b, n_in, face)
    u = pl.seg.u
    closed = u if d.hinge == "start" else (-u[0], -u[1])
    return hinge, closed, n_in


def _angle(v) -> float:
    return math.degrees(math.atan2(v[1], v[0])) % 360.0


def _ang_eq(a: float, b: float) -> bool:
    return abs((a - b + 180.0) % 360.0 - 180.0) <= ANGLE_TOL


def _pts(e) -> list[tuple[float, float]]:
    if e.dxftype() == "POLYLINE":
        return [(p[0], p[1]) for p in e.points()]
    return [(p[0], p[1]) for p in e.get_points("xy")]


def _layer_lw(doc, name: str) -> int:
    if name in doc.layers:
        return int(doc.layers.get(name).dxf.get("lineweight", -3))
    return -3


AREA_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*m²\s*$")


def check_floorplan_dxf(path: str | Path, spec: FloorPlanSpec) -> VerifyResult:
    import ezdxf

    from draftagent.verify.dimtext import displayed_text, forbidden_vars, matches_number
    from draftagent.verify.legibility_check import check_text_heights, check_text_overlaps
    from draftagent.verify.standards_check import DEFAULT_MIN_TEXT_MM, min_print_height_mm

    res = VerifyResult(name=NAME_DXF)
    try:
        doc = ezdxf.readfile(str(path))
    except (OSError, ezdxf.DXFStructureError) as e:
        res.error(f"無法讀取 DXF：{e}")
        return res
    ins = doc.header.get("$INSUNITS", 0)
    res.metrics["insunits"] = ins
    if ins != 4:
        res.error(f"$INSUNITS = {ins}，應為 4（mm）")
    layers = {lay.dxf.name.upper() for lay in doc.layers}
    for req in REQUIRED_LAYERS:
        if req not in layers:
            res.error(f"缺少必要圖層 {req}")
    on_zero = [e.dxftype() for lay in doc.layouts for e in lay if e.dxf.get("layer", "0") == "0"]
    if on_zero:
        res.error(f"0 層有 {len(on_zero)} 個實體")
    msp = doc.modelspace()

    def on(layer: str):
        return [e for e in msp if e.dxf.get("layer", "").upper() == layer]

    # 牆
    walls = on("A-WALL")
    geom = Polygon()
    rings: list[tuple[LineString, int]] = []
    layer_lw = _layer_lw(doc, "A-WALL")
    for e in walls:
        if e.dxftype() not in ("LWPOLYLINE", "POLYLINE") or not e.is_closed:
            res.error(f"A-WALL 只能放封閉 polyline，發現 {e.dxftype()}（{e.dxf.handle}）")
            continue
        pts = _pts(e)
        if len(pts) < 3:
            continue
        p = Polygon(pts)
        if not p.is_valid:
            p = p.buffer(0)
        geom = geom.symmetric_difference(p)
        lw = int(e.dxf.get("lineweight", -1))
        rings.append((LineString([*pts, pts[0]]), layer_lw if lw < 0 else lw))
    ext_w, int_w = expected_walls(spec)
    want = unary_union([ext_w, int_w])
    try:
        ratio = want.symmetric_difference(geom).area / want.area if want.area else float("inf")
    except Exception as e:  # noqa: BLE001 - 幾何無效
        res.error(f"A-WALL 牆幾何無法比對：{e}")
        ratio = None
    res.metrics["wall_symdiff_ratio"] = None if ratio is None else round(ratio, 6)
    if ratio is not None and ratio > WALL_SYMDIFF_MAX:
        res.error(
            f"A-WALL 牆幾何與 spec 不符：對稱差 = spec 牆面積 {ratio:.2%}（上限 {WALL_SYMDIFF_MAX:.1%}）"
        )
    ext_buf, int_buf = ext_w.buffer(POS_TOL), int_w.buffer(POS_TOL)
    ext_lws = {lw for ring, lw in rings if ext_buf.contains(ring)}
    int_lws = {lw for ring, lw in rings if not ext_buf.contains(ring) and int_buf.contains(ring)}
    res.metrics["wall_lineweights"] = {"exterior": sorted(ext_lws), "interior": sorted(int_lws)}
    if ext_lws and int_lws and min(ext_lws) <= max(int_lws):
        res.error(f"外牆線寬 {sorted(ext_lws)} 應大於內牆線寬 {sorted(int_lws)}（1/100 mm）")
    if not int_w.is_empty and not int_lws:
        res.error("找不到內牆的 A-WALL 輪廓")

    # 門
    pls, _ = _placements(spec)
    arcs = [e for e in on("A-DOOR") if e.dxftype() == "ARC"]
    res.metrics["doors"] = f"{len(arcs)}/{len(spec.doors)}"
    if len(arcs) != len(spec.doors):
        res.error(f"門數不符：A-DOOR 開啟弧 {len(arcs)}，spec {len(spec.doors)}")
    used: set[int] = set()
    for d in spec.doors:
        pl = pls.get(d.id)
        if pl is None:
            res.error(f"door {d.id}：spec 找不到牆段，無法比對 DXF")
            continue
        hinge, closed, opened = _door_expect(spec, d, pl)
        want_a = {_angle(closed), _angle(opened)}
        hit = None
        for k, arc in enumerate(arcs):
            if k in used:
                continue
            c = arc.dxf.center
            if math.hypot(c.x - hinge[0], c.y - hinge[1]) > POS_TOL or abs(arc.dxf.radius - d.width) > POS_TOL:
                continue
            s0, s1 = arc.dxf.start_angle % 360.0, arc.dxf.end_angle % 360.0
            if abs((s1 - s0) % 360.0 - 90.0) > ANGLE_TOL:
                continue
            if all(any(_ang_eq(x, w) for x in (s0, s1)) for w in want_a):
                hit = k
                break
        if hit is None:
            res.error(
                f"door {d.id}：找不到圓心 = 鉸點 ({hinge[0]:.0f}, {hinge[1]:.0f})、半徑 {d.width:g}、"
                f"90° 開向 {d.swing_room()} 的 A-DOOR ARC"
            )
        else:
            used.add(hit)

    # 窗
    frames = [e for e in on("A-WIND") if e.dxftype() in ("LWPOLYLINE", "POLYLINE") and e.is_closed]
    fpolys = [Polygon(_pts(e)) for e in frames]
    res.metrics["windows"] = f"{len(frames)}/{len(spec.windows)}"
    matched: set[int] = set()
    for w in spec.windows:
        pl = pls.get(w.id)
        if pl is None:
            res.error(f"window {w.id}：spec 找不到牆段，無法比對 DXF")
            continue
        rect = _opening_poly(pl)
        zone = rect.buffer(POS_TOL, join_style="mitre")
        hit = next(
            (k for k, fp in enumerate(fpolys) if k not in matched and fp.is_valid and zone.contains(fp) and fp.area >= 0.9 * rect.area),
            None,
        )
        if hit is None:
            res.error(f"window {w.id}（{w.room}）：A-WIND 沒有落在開口範圍內的封閉框")
        else:
            matched.add(hit)
    for k, e in enumerate(frames):
        if k not in matched:
            res.error(f"A-WIND 封閉框 {e.dxf.handle} 不在任何窗開口範圍內")

    # 房名 / 面積
    nets = net_polygons(spec)
    texts = [e for e in on("A-TEXT") if e.dxftype() in ("TEXT", "MTEXT")]

    def content(e) -> str:
        return (e.dxf.text if e.dxftype() == "TEXT" else e.plain_text()).strip()

    def anchor(e) -> list[tuple[float, float]]:
        pts = [(e.dxf.insert.x, e.dxf.insert.y)]
        if e.dxftype() == "TEXT" and (e.dxf.get("halign", 0) or e.dxf.get("valign", 0)):
            ap = e.dxf.get("align_point")
            if ap is not None:
                pts.append((ap[0], ap[1]))
        return pts

    names_found = sorted(content(e) for e in texts if not AREA_RE.match(content(e)))
    want_names = sorted(r.name for r in spec.rooms)
    res.metrics["room_names"] = f"{len(names_found)}/{len(want_names)}"
    if names_found != want_names:
        res.error(f"房名不符：DXF A-TEXT {names_found}，spec {want_names}")
    area_texts = [(e, float(AREA_RE.match(content(e)).group(1))) for e in texts if AREA_RE.match(content(e))]
    if len(area_texts) != len(spec.rooms):
        res.error(f"面積文字數 {len(area_texts)} ≠ 房間數 {len(spec.rooms)}")
    for r in spec.rooms:
        net = nets[r.name]
        want_a = round(net.area / 1e6, 2)
        for e in texts:
            if content(e) == r.name and not all(net.contains(Point(p)) for p in anchor(e)):
                res.error(f"房名「{r.name}」插入點不在該房淨空內")
        inside = [v for e, v in area_texts if all(net.contains(Point(p)) for p in anchor(e))]
        if len(inside) != 1:
            res.error(f"房間「{r.name}」淨空內應有 1 個面積文字，實際 {len(inside)}")
        elif abs(inside[0] - want_a) > AREA_TEXT_TOL + 1e-9:
            res.error(f"房間「{r.name}」面積文字 {inside[0]:.2f} m² ≠ 淨面積 {want_a:.2f} m²")

    # 尺寸
    _check_dims(res, msp, spec, displayed_text, forbidden_vars, matches_number)

    # 可讀性
    try:
        min_mm = min_print_height_mm()
    except ValueError as e:
        res.error(str(e))
        min_mm = DEFAULT_MIN_TEXT_MM
    bad = check_text_heights(doc, "Model", min_mm, model_scale=spec.print_scale)
    res.metrics["text_too_small"] = len(bad)
    for m in bad[:20]:
        res.error(f"文字太小：{m}")
    errs, warns = check_text_overlaps(doc, "Model", line_layers=LINE_LAYERS)
    for e in errs:
        res.error(e)
    for w in warns:
        res.warn(w)
    res.metrics["print_scale"] = spec.print_scale
    return res


def _check_dims(res, msp, spec: FloorPlanSpec, displayed_text, forbidden_vars, matches_number) -> None:
    minx, miny, maxx, maxy = spec.bbox()
    W, D = maxx - minx, maxy - miny
    # 合法站點：外緣、內牆中心線（與該軸垂直者）
    st = {0: {round(minx, 3), round(maxx, 3)}, 1: {round(miny, 3), round(maxy, 3)}}
    for s in interior_wall_lines(spec):
        for ax in (0, 1):
            if abs(s.start[ax] - s.end[ax]) < 1e-3:
                st[ax].add(round(s.start[ax], 3))
    rows: dict[tuple[int, float], list[tuple[float, float]]] = {}
    shown: list[float] = []
    dims = [e for e in msp if e.dxftype() == "DIMENSION"]
    if not dims:
        res.error("沒有任何 DIMENSION")
    for dim in dims:
        h = dim.dxf.handle
        if dim.dxf.get("layer", "").upper() != "A-DIMS":
            res.error(f"DIMENSION {h} 不在 A-DIMS 層")
        try:
            m = float(dim.get_measurement())
        except Exception as e:  # noqa: BLE001
            res.error(f"DIMENSION {h} 無法量測：{e}")
            continue
        for bad in forbidden_vars(dim):
            res.error(f"DIMENSION {h} {bad}")
        lfac = float(dim.override().get("dimlfac", 1.0))
        if abs(lfac - 1.0) > 1e-9:
            res.error(f"DIMENSION {h} dimlfac={lfac:g}，mm 平面圖必須為 1")
        override = dim.dxf.get("text", "")
        if override not in ("", "<>") and not matches_number(override.replace("<>", f"{m:.0f}"), m, 0.5):
            res.error(f"DIMENSION {h} 文字覆寫 '{override}' 與量測值 {m:.0f} 不符")
        dt = displayed_text(dim)
        if not dt.text or not matches_number(dt.text, m, 0.5):
            res.error(f"DIMENSION {h} 實際顯示 '{dt.text}'，與量測值 {m:.0f} 不符")
            continue
        shown.append(round(m, 1))
        p1, p2, base = dim.dxf.defpoint2, dim.dxf.defpoint3, dim.dxf.defpoint
        ang = float(dim.dxf.get("angle", 0.0)) % 180.0
        ax = 0 if abs(ang) < 1e-6 else (1 if abs(ang - 90.0) < 1e-6 else None)
        if ax is None:
            res.warn(f"DIMENSION {h} 角度 {ang:g}° 非水平 / 垂直，未檢查站點")
            continue
        a, b = sorted((p1[ax], p2[ax]))
        rows.setdefault((ax, round(base[1 - ax], 1)), []).append((a, b))
    res.metrics["dimensions_displayed"] = sorted(shown)
    d = spec.dimensions
    for ax, total, label in ((0, W, "總長"), (1, D, "總寬")):
        lo, hi = (minx, maxx) if ax == 0 else (miny, maxy)
        ax_rows = {k: v for k, v in rows.items() if k[0] == ax}
        if d.auto_overall and not any(
            any(abs(a - lo) < 0.5 and abs(b - hi) < 0.5 for a, b in v) for v in ax_rows.values()
        ):
            res.error(f"找不到外{label} {total:g} 的 DIMENSION")
        for (_, pos), spans in ax_rows.items():
            if len(spans) == 1 and abs(spans[0][0] - lo) < 0.5 and abs(spans[0][1] - hi) < 0.5:
                continue  # 總尺寸
            spans.sort()
            for a, b in spans:
                for v in (a, b):
                    if not any(abs(v - s) < 0.5 for s in st[ax]):
                        res.error(f"尺寸鏈（{label}向，位置 {pos:g}）端點 {v:g} 不在外緣或內牆中心線上")
            if abs(spans[0][0] - lo) > 0.5 or abs(spans[-1][1] - hi) > 0.5 or any(
                abs(x[1] - y[0]) > 0.5 for x, y in zip(spans, spans[1:], strict=False)
            ):
                res.error(f"尺寸鏈（{label}向，位置 {pos:g}）不連續或未涵蓋外緣到外緣：{spans}")


# ---------------------------------------------------------------- IFC 讀回


def check_floorplan_ifc(path: str | Path, spec: FloorPlanSpec) -> VerifyResult:
    res = VerifyResult(name=NAME_IFC)
    try:
        import ifcopenshell
        import ifcopenshell.util.element as uel
        import ifcopenshell.util.placement as upl
    except ImportError:
        res.error("未安裝 ifcopenshell（uv pip install -e '.[ifc]'），無法讀回 IFC")
        return res
    try:
        f = ifcopenshell.open(str(path))
    except Exception as e:  # noqa: BLE001
        res.error(f"無法讀取 IFC：{e}")
        return res
    res.metrics["schema"] = f.schema
    if f.schema != "IFC4":
        res.error(f"IFC schema = {f.schema}，應為 IFC4")

    # 單位
    length_units = [
        u for ua in f.by_type("IfcUnitAssignment") for u in ua.Units
        if u.is_a("IfcSIUnit") and u.UnitType == "LENGTHUNIT"
    ]  # fmt: skip
    lu = length_units[0] if length_units else None
    res.metrics["length_unit"] = f"{lu.Prefix or ''}{lu.Name}" if lu else None
    if lu is None or lu.Prefix != "MILLI" or lu.Name != "METRE":
        res.error(f"長度單位應為 MILLI METRE（mm），實際 {res.metrics['length_unit']}")

    # 空間結構
    counts = {c: len(f.by_type(c)) for c in ("IfcProject", "IfcSite", "IfcBuilding", "IfcBuildingStorey")}
    res.metrics["spatial"] = counts
    for c, n in counts.items():
        if n != 1:
            res.error(f"{c} 應為 1 個，實際 {n}")
    if all(n == 1 for n in counts.values()):
        chain = [f.by_type(c)[0] for c in ("IfcProject", "IfcSite", "IfcBuilding", "IfcBuildingStorey")]
        for parent, child in zip(chain, chain[1:], strict=False):
            if uel.get_aggregate(child) != parent:
                res.error(f"{child.is_a()} 未聚合於 {parent.is_a()}")
        storey = chain[-1]
    else:
        storey = None

    # 空間
    nets = net_polygons(spec)
    spaces = f.by_type("IfcSpace")
    res.metrics["spaces"] = f"{len(spaces)}/{len(spec.rooms)}"
    if len(spaces) != len(spec.rooms):
        res.error(f"IfcSpace 數 {len(spaces)} ≠ 房間數 {len(spec.rooms)}")
    by_name = {s.Name: s for s in spaces}
    if sorted(by_name) != sorted(r.name for r in spec.rooms):
        res.error(f"IfcSpace 名稱 {sorted(by_name)} ≠ spec {sorted(r.name for r in spec.rooms)}")
    for r in spec.rooms:
        s = by_name.get(r.name)
        if s is None:
            continue
        if s.LongName != r.use:
            res.error(f"IfcSpace「{r.name}」LongName {s.LongName!r} ≠ 用途 {r.use!r}")
        if storey is not None and uel.get_aggregate(s) != storey:
            res.error(f"IfcSpace「{r.name}」未聚合於樓層")
        want = nets[r.name].area / 1e6
        q = (uel.get_psets(s, qtos_only=True).get("Qto_SpaceBaseQuantities") or {}).get("NetFloorArea")
        if q is None or abs(float(q) - round(want, 2)) > AREA_TEXT_TOL + 1e-9:
            res.error(f"IfcSpace「{r.name}」NetFloorArea {q} ≠ 淨面積 {want:.2f} m²")
        prof_area = _space_profile_area(s)
        if prof_area is None or abs(prof_area - nets[r.name].area) > max(0.005 * nets[r.name].area, 1.0):
            res.error(f"IfcSpace「{r.name}」輪廓面積 {prof_area} mm² ≠ 淨空 {nets[r.name].area:.0f} mm²")

    # 牆
    walls = f.by_type("IfcWall")
    n_ext_edges = len(exterior_edges(spec))
    n_int = len(interior_wall_lines(spec))
    ext_walls = [w for w in walls if (uel.get_psets(w).get("Pset_WallCommon") or {}).get("IsExternal") is True]
    res.metrics["walls"] = {"total": len(walls), "external": len(ext_walls), "expected": n_ext_edges + n_int}
    if len(walls) != n_ext_edges + n_int:
        res.error(f"IfcWall 數 {len(walls)} ≠ 外框邊數 {n_ext_edges} + 內牆直線段 {n_int}")
    if len(ext_walls) != n_ext_edges:
        res.error(f"IsExternal 牆數 {len(ext_walls)} ≠ 外框邊數 {n_ext_edges}")

    # 門窗
    pls, _ = _placements(spec)
    for cls, items, label in (("IfcDoor", spec.doors, "door"), ("IfcWindow", spec.windows, "window")):
        els = f.by_type(cls)
        res.metrics[cls] = f"{len(els)}/{len(items)}"
        if len(els) != len(items):
            res.error(f"{cls} 數 {len(els)} ≠ spec {label} 數 {len(items)}")
        by_id = {e.Name: e for e in els}
        for it in items:
            e = by_id.get(it.id)
            if e is None:
                res.error(f"{label} {it.id}：IFC 沒有同名 {cls}")
                continue
            if e.OverallWidth is None or abs(float(e.OverallWidth) - it.width) > POS_TOL:
                res.error(f"{cls} {it.id} OverallWidth {e.OverallWidth} ≠ {it.width:g}")
            fills = list(getattr(e, "FillsVoids", []) or [])
            if len(fills) != 1:
                res.error(f"{cls} {it.id} 應有 1 個 IfcRelFillsElement，實際 {len(fills)}")
                continue
            opening = fills[0].RelatingOpeningElement
            voids = list(getattr(opening, "VoidsElements", []) or [])
            if len(voids) != 1 or not voids[0].RelatingBuildingElement.is_a("IfcWall"):
                res.error(f"{cls} {it.id} 的開口未以 IfcRelVoidsElement 開在 IfcWall 上")
                continue
            wall = voids[0].RelatingBuildingElement
            pl = pls.get(it.id)
            if pl is not None:
                is_ext = (uel.get_psets(wall).get("Pset_WallCommon") or {}).get("IsExternal") is True
                if is_ext != pl.exterior:
                    res.error(f"{cls} {it.id} 所在牆 {wall.Name} 外 / 內牆別與 spec 不符")
                m = upl.get_local_placement(opening.ObjectPlacement)
                if math.hypot(m[0][3] - pl.a[0], m[1][3] - pl.a[1]) > POS_TOL:
                    res.error(
                        f"{cls} {it.id} 開口放置點 ({m[0][3]:.0f}, {m[1][3]:.0f}) ≠ spec 開口起點 "
                        f"({pl.a[0]:.0f}, {pl.a[1]:.0f})"
                    )
            if storey is not None and uel.get_container(e) != storey:
                res.error(f"{cls} {it.id} 未包含於樓層")
    if storey is not None:
        for w in walls:
            if uel.get_container(w) != storey:
                res.error(f"IfcWall {w.Name} 未包含於樓層")
    return res


def _space_profile_area(space) -> float | None:
    rep = space.Representation
    if rep is None:
        return None
    for r in rep.Representations:
        for item in r.Items:
            if item.is_a("IfcExtrudedAreaSolid") and item.SweptArea.is_a("IfcArbitraryClosedProfileDef"):
                pts = [tuple(p.Coordinates[:2]) for p in item.SweptArea.OuterCurve.Points]
                if pts and pts[0] == pts[-1]:
                    pts = pts[:-1]
                return abs(Polygon(pts).area) if len(pts) >= 3 else None
    return None


# ---------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> None:
    import sys

    ap = argparse.ArgumentParser(
        prog="python -m draftagent.verify.floorplan_check",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("spec")
    ap.add_argument("--dxf")
    ap.add_argument("--ifc")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    spec = FloorPlanSpec.from_json(a.spec)
    results = [check_floorplan(spec)]
    if a.dxf:
        results.append(check_floorplan_dxf(a.dxf, spec))
    if a.ifc:
        results.append(check_floorplan_ifc(a.ifc, spec))
    if a.json:
        import json

        print(json.dumps([r.to_dict() for r in results], ensure_ascii=False, indent=2))
    else:
        print("\n\n".join(r.render() for r in results))
    sys.exit(0 if all(r.ok for r in results) else 1)


if __name__ == "__main__":
    main()
