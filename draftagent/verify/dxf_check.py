"""DXF 平面圖 verifier（Level 1-1）。純函式，不得呼叫 LLM。

檢查項目
- 檔案可被 ezdxf 讀取
- $INSUNITS = 4（mm）
- 必要圖層存在：A-WALL、A-DOOR、A-DIMS
- 任何 layout（model / paper）中沒有實體在 0 層
- A-WALL 只有封閉的 LWPOLYLINE / POLYLINE，且至少一條
- 至少一個 DIMENSION；量測值以 defpoints 重算（不信任文字覆寫）
- DIMENSION **實際顯示文字**（geometry block 內的 TEXT / MTEXT，verify/dimtext.py）必須以
  完整數字等於量測值（"45000" 不算 4500）；dimrnd / dimpost / dimalt 非預設 → error；
  dimlfac ≠ 1 → error；文字覆寫以完整數字比對（"<>" 以量測值代入）
- 可選 --expect-dim：每個期望值都要在「量測值且顯示值相符」的 DIMENSION 中找到（容差 --tol mm）
- 可選 --spec：比對門數（A-DOOR 上的 ARC）、窗數（A-WIND 上的封閉 polyline）、
  房數與房名（A-TEXT 上的 TEXT / MTEXT）、spec 的外尺寸；另外由 spec 獨立重建幾何：
  * 牆：每房 外框 − 內縮牆厚 − 開口 的聯集，與 A-WALL 封閉多段線（奇偶規則）組成的多邊形
    對稱差面積 ≤ 0.5%
  * 門：每扇門有一條 A-DOOR ARC，圓心 = 鉸點（±1 mm）、半徑 = 門寬（±1 mm）、開啟角 90°，
    起訖角 = 關閉方向 / 開啟方向（驗 hinge、swing）
  * 窗：每個 A-WIND 封閉框落在對應開口範圍內（外擴 1 mm）且面積 ≥ 開口 90%
  * 房名：插入點（與對齊點）在該房間淨空多邊形（外框內縮牆厚）內
  * 印出字高 ≥ layers.yaml `text.min_print_height_mm`（spec.print_scale 換算）、文字互不重疊
    （verify/legibility_check.py）；文字壓到 A-WALL / A-DOOR / A-WIND → warning
- 不給 --spec 時可用 --print-scale 做字高檢查；兩者都沒有 → 略過字高檢查（warning）

用法：python -m draftagent.verify.dxf_check <file.dxf> [--expect-dim 6000 ...] [--spec spec.json]
      [--print-scale 50] [--json]
"""

from __future__ import annotations

import argparse
import math
from collections import Counter
from pathlib import Path

import ezdxf
from shapely.geometry import MultiPolygon, Point, Polygon
from shapely.ops import unary_union

from draftagent.verify.base import VerifyResult, emit
from draftagent.verify.dimtext import displayed_text, forbidden_vars, matches_number
from draftagent.verify.legibility_check import check_text_heights, check_text_overlaps

NAME = "dxf_check"
REQUIRED_LAYERS = ("A-WALL", "A-DOOR", "A-DIMS")
INSUNITS_MM = 4
WALL_SYMDIFF_MAX = 0.005  # 牆多邊形對稱差 / spec 牆面積
POS_TOL = 1.0  # 門鉸點、半徑、窗框位置容差 mm
ANGLE_TOL = 1.0  # 度
LINE_LAYERS = ("A-WALL", "A-DOOR", "A-WIND")


def check_dxf(
    path: str | Path,
    expect_dims: list[float] | None = None,
    spec_path: str | Path | None = None,
    tol: float = 0.5,
    print_scale: float | None = None,
) -> VerifyResult:
    res = VerifyResult(name=NAME)
    try:
        doc = ezdxf.readfile(str(path))
    except (OSError, ezdxf.DXFStructureError) as e:
        res.error(f"無法讀取 DXF：{e}")
        return res

    # 單位
    insunits = doc.header.get("$INSUNITS", 0)
    res.metrics["insunits"] = insunits
    if insunits != INSUNITS_MM:
        res.error(f"$INSUNITS = {insunits}，應為 4（mm）")

    # 圖層
    layer_names = sorted(layer.dxf.name for layer in doc.layers)
    res.metrics["layers"] = layer_names
    upper = {n.upper() for n in layer_names}
    for req in REQUIRED_LAYERS:
        if req not in upper:
            res.error(f"缺少必要圖層 {req}")

    # 0 層
    msp = doc.modelspace()
    on_zero = Counter(
        e.dxftype() for layout in doc.layouts for e in layout if e.dxf.get("layer", "0") == "0"
    )
    if on_zero:
        res.error(f"0 層有 {sum(on_zero.values())} 個實體：{dict(on_zero)}")

    by_layer = Counter(e.dxf.get("layer", "0").upper() for e in msp)
    res.metrics["entities_by_layer"] = dict(sorted(by_layer.items()))

    # 牆
    walls = [e for e in msp if e.dxf.get("layer", "").upper() == "A-WALL"]
    closed = 0
    for e in walls:
        kind = e.dxftype()
        if kind in ("LWPOLYLINE", "POLYLINE"):
            if e.is_closed:
                closed += 1
            else:
                res.error(f"A-WALL 有未封閉的 {kind}（handle {e.dxf.handle}）")
        else:
            res.error(f"A-WALL 只能放封閉 polyline，發現 {kind}（handle {e.dxf.handle}）")
    res.metrics["wall_closed_polylines"] = closed
    if closed == 0:
        res.error("A-WALL 沒有任何封閉 polyline")

    # 尺寸
    dims = [e for e in msp if e.dxftype() == "DIMENSION"]
    measurements: list[float] = []
    displayed: list[float] = []
    consistent: list[float] = []  # 量測值且顯示文字相符者
    for dim in dims:
        h = dim.dxf.handle
        try:
            m = float(dim.get_measurement())
        except Exception as e:  # noqa: BLE001 - 非線性尺寸等無法量測者
            res.warn(f"DIMENSION {h} 無法量測：{e}")
            continue
        measurements.append(round(m, 3))
        ok = True
        if dim.dxf.get("layer", "").upper() != "A-DIMS":
            res.error(f"DIMENSION {h} 在 {dim.dxf.layer} 層，應在 A-DIMS")
        override = dim.dxf.get("text", "")
        if override not in ("", "<>"):
            probe = override.replace("<>", f"{m:.0f}")
            if not matches_number(probe, m, tol):
                ok = False
                res.error(f"DIMENSION {h} 文字覆寫為 '{override}'，與量測值 {m:.0f} 不符")
        for bad in forbidden_vars(dim):
            ok = False
            res.error(f"DIMENSION {h} {bad}")
        # 顯示值 = 量測值 × dimlfac；mm 圖面兩者必須一致
        lfac = float(dim.override().get("dimlfac", 1.0))
        if abs(lfac - 1.0) > 1e-9:
            ok = False
            res.error(f"DIMENSION {h} dimlfac={lfac:g}，顯示值 {m * lfac:.0f} ≠ 量測值 {m:.0f}")
        # 實際畫出來的文字（geometry block）
        dt = displayed_text(dim)
        if not dt.text:
            ok = False
            displayed.append(round(m * lfac, 3))
            res.error(
                f"DIMENSION {h} 讀不到顯示文字（geometry block 不存在或無文字），無法確認顯示值"
            )
        else:
            displayed.append(round(dt.numbers[0], 3) if dt.numbers else float("nan"))
            if not matches_number(dt.text, m, tol):
                ok = False
                res.error(f"DIMENSION {h} 實際顯示 '{dt.text}'，與量測值 {m:.0f} 不符")
        if ok:
            consistent.append(round(m, 3))
    res.metrics["dimensions"] = measurements
    res.metrics["dimensions_displayed"] = displayed
    if not dims:
        res.error("沒有任何 DIMENSION 實體")

    expected = list(expect_dims or [])

    # spec 比對
    scale = print_scale
    if spec_path is not None:
        from draftagent.schema.plan import PlanSpec

        try:
            spec = PlanSpec.from_json(spec_path)
        except Exception as e:  # noqa: BLE001 - 回報給使用者即可
            res.error(f"spec 讀取失敗：{e}")
        else:
            _compare_spec(res, msp, spec)
            _check_geometry(res, msp, spec)
            scale = spec.print_scale
            if spec.dimensions.auto_overall:
                expected += [v for v in spec.overall_dims() if v not in expected]

    _check_legibility(res, doc, scale)

    for want in expected:
        if not any(abs(m - want) <= tol for m in measurements):
            res.error(f"找不到量測值 {want:g} mm 的 DIMENSION（實際：{measurements}）")
        elif not any(abs(m - want) <= tol for m in consistent):
            res.error(f"量測值 {want:g} mm 的 DIMENSION 顯示值不符（見上方 DIMENSION 錯誤）")
    if expected:
        res.metrics["expected_dims"] = expected
    return res


def _compare_spec(res: VerifyResult, msp, spec) -> None:
    def on(layer: str):
        return [e for e in msp if e.dxf.get("layer", "").upper() == layer]

    want_doors = sum(1 for o in spec.openings if o.type == "door")
    want_wins = sum(1 for o in spec.openings if o.type == "window")
    got_doors = sum(1 for e in on("A-DOOR") if e.dxftype() == "ARC")
    got_wins = sum(
        1 for e in on("A-WIND") if e.dxftype() in ("LWPOLYLINE", "POLYLINE") and e.is_closed
    )
    texts = [
        e.dxf.text if e.dxftype() == "TEXT" else e.text
        for e in on("A-TEXT")
        if e.dxftype() in ("TEXT", "MTEXT")
    ]
    want_rooms = sorted(r.name for r in spec.rooms)

    res.metrics["doors"] = f"{got_doors}/{want_doors}"
    res.metrics["windows"] = f"{got_wins}/{want_wins}"
    res.metrics["rooms"] = f"{len(texts)}/{len(want_rooms)}"
    if got_doors != want_doors:
        res.error(f"門數不符：DXF A-DOOR 開啟弧 {got_doors}，spec {want_doors}")
    if got_wins != want_wins:
        res.error(f"窗數不符：DXF A-WIND 封閉框 {got_wins}，spec {want_wins}")
    if sorted(texts) != want_rooms:
        res.error(f"房間不符：DXF A-TEXT {sorted(texts)}，spec {want_rooms}")


def _check_legibility(res: VerifyResult, doc, scale: float | None) -> None:
    from draftagent.verify.standards_check import DEFAULT_MIN_TEXT_MM, min_print_height_mm

    try:
        min_mm = min_print_height_mm()
    except ValueError as e:
        res.error(str(e))
        min_mm = DEFAULT_MIN_TEXT_MM
    res.metrics["print_scale"] = scale
    res.metrics["min_print_height_mm"] = min_mm
    if scale is None:
        res.warn("未提供出圖比例（--spec 或 --print-scale），略過印出字高檢查")
    else:
        bad = check_text_heights(doc, "Model", min_mm, model_scale=scale)
        res.metrics["text_too_small"] = len(bad)
        for m in bad[:20]:
            res.error(f"文字太小：{m}")
        if len(bad) > 20:
            res.error(f"文字太小：另有 {len(bad) - 20} 筆未列出")
    errs, warns = check_text_overlaps(doc, "Model", line_layers=LINE_LAYERS)
    for e in errs:
        res.error(e)
    for w in warns:
        res.warn(w)


# ---------------------------------------------------------------- 幾何（由 spec 獨立重建）


def _unit(p0, p1):
    length = math.hypot(p1[0] - p0[0], p1[1] - p0[1])
    return (p1[0] - p0[0]) / length, (p1[1] - p0[1]) / length


def _opening_geom(spec, op):
    """回傳 (a, b, u, n, outer)：開口在外牆外緣的兩端 a、b，沿牆單位向量 u，指向室內的 n。"""
    room = spec.room_of(op)
    outer = Polygon(room.outline())
    p0, p1 = room.segment(op.side, op.edge)
    u = _unit(p0, p1)
    n = (-u[1], u[0])
    mid = ((p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2)
    eps = min(spec.wall_thickness / 2, 1.0)
    if not outer.contains(Point(mid[0] + n[0] * eps, mid[1] + n[1] * eps)):
        n = (-n[0], -n[1])
    a = (p0[0] + u[0] * op.offset, p0[1] + u[1] * op.offset)
    b = (a[0] + u[0] * op.width, a[1] + u[1] * op.width)
    return a, b, u, n


def _mv(p, v, s):
    return (p[0] + v[0] * s, p[1] + v[1] * s)


def _opening_poly(spec, op) -> Polygon:
    a, b, _, n = _opening_geom(spec, op)
    t = spec.wall_thickness
    return Polygon([a, b, _mv(b, n, t), _mv(a, n, t)])


def _expected_walls(spec):
    t = spec.wall_thickness
    parts = []
    for room in spec.rooms:
        outer = Polygon(room.outline())
        wall = outer.difference(outer.buffer(-t, join_style="mitre"))
        cuts = []
        for op in spec.openings:
            if spec.room_of(op).name != room.name:
                continue
            a, b, _, n = _opening_geom(spec, op)  # 法向各多切 1 mm，避免殘留薄片
            cuts.append(Polygon([_mv(a, n, -1), _mv(b, n, -1), _mv(b, n, t + 1), _mv(a, n, t + 1)]))
        if cuts:
            wall = wall.difference(unary_union(cuts))
        parts.append(wall)
    return unary_union(parts)


def _actual_walls(msp):
    """A-WALL 封閉多段線 → 多邊形（奇偶規則：內圈當洞）。"""
    geom = Polygon()
    for e in msp:
        if e.dxf.get("layer", "").upper() != "A-WALL" or e.dxftype() not in (
            "LWPOLYLINE",
            "POLYLINE",
        ):
            continue
        if not e.is_closed:
            continue
        pts = (
            [(p[0], p[1]) for p in e.points()]
            if e.dxftype() == "POLYLINE"
            else [(p[0], p[1]) for p in e.get_points("xy")]
        )
        if len(pts) < 3:
            continue
        poly = Polygon(pts)
        if not poly.is_valid:
            poly = poly.buffer(0)
        geom = geom.symmetric_difference(poly)
    return geom


def _angle(v) -> float:
    return math.degrees(math.atan2(v[1], v[0])) % 360.0


def _ang_eq(a: float, b: float) -> bool:
    d = abs((a - b + 180.0) % 360.0 - 180.0)
    return d <= ANGLE_TOL


def _insert_points(e) -> list[tuple[float, float]]:
    pts = [(e.dxf.insert.x, e.dxf.insert.y)]
    if e.dxftype() == "TEXT" and (e.dxf.get("halign", 0) or e.dxf.get("valign", 0)):
        ap = e.dxf.get("align_point")
        if ap is not None:
            pts.append((ap[0], ap[1]))
    return pts


def _check_geometry(res: VerifyResult, msp, spec) -> None:
    t = spec.wall_thickness

    # 牆
    try:
        want = _expected_walls(spec)
        got = _actual_walls(msp)
        diff = want.symmetric_difference(got).area
        ratio = diff / want.area if want.area > 0 else float("inf")
        res.metrics["wall_symdiff_ratio"] = round(ratio, 6)
        if ratio > WALL_SYMDIFF_MAX:
            res.error(
                f"A-WALL 牆幾何與 spec 不符：對稱差 {diff:,.0f} mm²＝spec 牆面積 {ratio:.2%}"
                f"（上限 {WALL_SYMDIFF_MAX:.1%}；外框 / 牆厚 {t:g} / 開口位置）"
            )
    except Exception as e:  # noqa: BLE001 - 幾何無效
        res.error(f"A-WALL 牆幾何無法比對：{e}")

    # 門
    arcs = [e for e in msp if e.dxf.get("layer", "").upper() == "A-DOOR" and e.dxftype() == "ARC"]
    used: set[int] = set()
    for i, op in enumerate(spec.openings):
        if op.type != "door":
            continue
        a, b, u, n = _opening_geom(spec, op)
        hinge = _mv(a if op.hinge == "start" else b, n, t if op.swing == "in" else 0.0)
        closed = u if op.hinge == "start" else (-u[0], -u[1])
        opened = n if op.swing == "in" else (-n[0], -n[1])
        want_angles = {_angle(closed), _angle(opened)}
        hit = None
        for k, arc in enumerate(arcs):
            if k in used:
                continue
            c = arc.dxf.center
            if math.hypot(c.x - hinge[0], c.y - hinge[1]) > POS_TOL:
                continue
            if abs(arc.dxf.radius - op.width) > POS_TOL:
                continue
            s0, s1 = arc.dxf.start_angle % 360.0, arc.dxf.end_angle % 360.0
            span = (s1 - s0) % 360.0
            if abs(span - 90.0) > ANGLE_TOL:
                continue
            if not all(any(_ang_eq(x, w) for x in (s0, s1)) for w in want_angles):
                continue
            hit = k
            break
        if hit is None:
            res.error(
                f"openings[{i}] 門（{spec.room_of(op).name}）：找不到圓心 = 鉸點 "
                f"({hinge[0]:.0f}, {hinge[1]:.0f})、半徑 = 門寬 {op.width:g}、90° 開向 "
                f"{op.swing}/{op.hinge} 的 A-DOOR ARC"
            )
        else:
            used.add(hit)

    # 窗
    frames = [
        e
        for e in msp
        if e.dxf.get("layer", "").upper() == "A-WIND"
        and e.dxftype() == "LWPOLYLINE"
        and e.is_closed
    ]
    frame_polys = [Polygon([(p[0], p[1]) for p in e.get_points("xy")]) for e in frames]
    matched: set[int] = set()
    for i, op in enumerate(spec.openings):
        if op.type != "window":
            continue
        rect = _opening_poly(spec, op)
        zone = rect.buffer(POS_TOL, join_style="mitre")
        hit = None
        for k, fp in enumerate(frame_polys):
            if k in matched or not fp.is_valid:
                continue
            if zone.contains(fp) and fp.area >= 0.9 * rect.area:
                hit = k
                break
        if hit is None:
            res.error(
                f"openings[{i}] 窗（{spec.room_of(op).name}）：A-WIND 沒有落在開口範圍 "
                f"（{op.side or f'edge {op.edge}'} offset {op.offset:g} 寬 {op.width:g}）的封閉框"
            )
        else:
            matched.add(hit)
    for k, e in enumerate(frames):
        if k not in matched:
            res.error(f"A-WIND 封閉框 {e.dxf.handle} 不在任何窗開口範圍內")

    # 房名
    rooms = {r.name: r for r in spec.rooms}
    for e in msp:
        if e.dxf.get("layer", "").upper() != "A-TEXT" or e.dxftype() not in ("TEXT", "MTEXT"):
            continue
        name = e.dxf.text if e.dxftype() == "TEXT" else e.text
        room = rooms.get(name)
        if room is None:
            continue  # 名稱不符由 _compare_spec 報
        inner = Polygon(room.outline()).buffer(-t, join_style="mitre")
        if isinstance(inner, MultiPolygon) or inner.is_empty:
            inner = Polygon(room.outline())
        for x, y in _insert_points(e):
            if not inner.contains(Point(x, y)):
                res.error(
                    f"房名「{name}」（{e.dxf.handle}）插入點 ({x:.0f}, {y:.0f}) 不在房間淨空範圍內"
                )
                break


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        prog="python -m draftagent.verify.dxf_check",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("dxf")
    ap.add_argument("--expect-dim", type=float, action="append", default=[], metavar="MM")
    ap.add_argument("--spec", help="PlanSpec JSON，用來比對門窗數、房數與外尺寸")
    ap.add_argument("--tol", type=float, default=0.5, help="尺寸容差 mm（預設 0.5）")
    ap.add_argument(
        "--print-scale",
        type=float,
        default=None,
        help="出圖比例分母（無 --spec 時用於印出字高檢查；有 --spec 以 spec.print_scale 為準）",
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    emit(check_dxf(args.dxf, args.expect_dim, args.spec, args.tol, args.print_scale), args.json)


if __name__ == "__main__":
    main()
