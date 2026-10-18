"""arch-import 驗證：建築師 DXF → TWD97 GeoJSON。純函式，不得呼叫 LLM。

用法：python -m draftagent.verify.arch_check <spec.json> <arch.geojson> [--transform transform.json] [--json]

不採信 extract 的中間值（transform.json 只拿來比對，不拿來算）：
1. 重新讀 DXF、依 spec 重建多邊形（共用 `arch.extract` 的接線演算法）並以本檔獨立實作的
   4 參數最小平方（numpy lstsq）重算相似變換 → 與 arch.geojson 逐角色比對，對稱差 ≤ 0.1%
2. 控制點：≥ 3 點時每點殘差 ≤ 0.05 m（error）；2 點 → warning（轉換無法驗證，請工程師確認或提供第 3 點）
3. 縮放比：drawing_units 換算後的等比縮放 s ∈ [0.995, 1.005]（否則 error：單位判斷錯或控制點錯）
4. 面積守恆：圖上面積（換算 m²）vs geojson 面積，差 ≤ 0.1%
5. 多邊形有效（is_valid）、型別 Polygon、外框（footprint）恰一個（spec.allow_multiple_footprints 才允許多個）、
   至少一個外框；接縫最大距離 ≤ join_tolerance
6. 有 site：所有面 ⊂ 基地.buffer(0.05 m)
7. 可追溯：每個 feature 的 source_handles 非空、handle 路徑存在於 DXF，且末端實體的有效圖層屬於該角色圖層
8. 屬性：area_m2 / perimeter_m = 幾何重算；CRS = EPSG:3826；transform.json 的縮放 / 旋轉 / 殘差與重算一致
9. $INSUNITS 與 drawing_units 不符 → warning（header 可能錯）
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
from shapely.affinity import affine_transform
from shapely.geometry import Point, Polygon, shape
from shapely.ops import unary_union

from draftagent.verify.base import VerifyResult, emit

RESIDUAL_MAX_M = 0.05
SCALE_RANGE = (0.995, 1.005)
AREA_REL_MAX = 0.001
SYMDIFF_REL_MAX = 0.001
SITE_TOL_M = 0.05
POINT_TOL_M = 0.01
EDGE_ON_LINES_MIN = 0.6  # 外緣落在原圖線上的比例下限（低於 → warning，提示目視）
INSUNITS_UNITS = {4: "mm", 5: "cm", 6: "m"}
ROLE_LAYER_FIELD = {"footprint": "exterior_wall", "basement": "basement", "open_space": "open_space",
                    "roof_drain": "roof_drains"}
NAME = "arch_check"


def load_site(site: Any) -> Any:
    """SiteRef → EPSG:3826 幾何（path 的 CRS 必須是 3826）。"""
    from draftagent.schema.arch import resolve_repo_path

    if site.coords is not None:
        return Polygon(site.coords)
    import geopandas as gpd

    gdf = gpd.read_file(resolve_repo_path(site.path), layer=site.layer) if site.layer else \
        gpd.read_file(resolve_repo_path(site.path))
    epsg = gdf.crs.to_epsg() if gdf.crs else None
    if epsg != 3826:
        raise ValueError(f"基地圖資 CRS 為 EPSG:{epsg}，必須是 EPSG:3826（先跑 crs_check / 轉換）")
    return unary_union(list(gdf.geometry))


def fit_lstsq(src: np.ndarray, dst: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """4 參數相似變換 [E, N] = [[a, -b], [b, a]]·[x, y] + [tx, ty]（獨立實作）。回傳 (params, residuals)。"""
    n = len(src)
    A = np.zeros((2 * n, 4))
    A[0::2] = np.column_stack([src[:, 0], -src[:, 1], np.ones(n), np.zeros(n)])
    A[1::2] = np.column_stack([src[:, 1], src[:, 0], np.zeros(n), np.ones(n)])
    y = dst.reshape(-1)
    prm, *_ = np.linalg.lstsq(A, y, rcond=None)
    res = np.hypot(*(A @ prm - y).reshape(-1, 2).T)
    return prm, res


def _valid_handle_paths(doc: Any) -> Any:
    """handle 路徑 → 末端實體有效圖層；None = 路徑不存在。"""
    db = doc.entitydb
    msp_handles = {e.dxf.handle for e in doc.modelspace()}

    def resolve(path: str) -> str | None:
        hs = path.split(">")
        if hs[0] not in msp_handles:
            return None
        e = db.get(hs[0])
        layer = e.dxf.get("layer", "0")
        for h in hs[1:]:
            if e.dxftype() != "INSERT":
                return None
            blk = doc.blocks.get(e.dxf.get("name", ""))
            if blk is None:
                return None
            child = next((c for c in blk if c.dxf.get("handle") == h), None)
            if child is None:
                return None
            cl = child.dxf.get("layer", "0")
            layer = layer if cl == "0" else cl
            e = child
        return layer

    return resolve


def _polys_of(f: dict) -> Any:
    try:
        return shape(f["geometry"])
    except Exception:  # noqa: BLE001
        return None


def check_arch(spec: Any, geojson: str | Path, transform: str | Path | None = None) -> VerifyResult:
    from draftagent.arch.extract import POLY_ROLES, collect, load_doc, role_polygons

    res = VerifyResult(name=NAME)
    k = spec.unit_to_m
    tol = spec.join_tol
    res.metrics.update(drawing_units=spec.drawing_units, join_tolerance=tol, control_points=len(spec.control_points))

    # ---- 控制點（獨立擬合）
    src = np.array([[c.drawing[0] * k, c.drawing[1] * k] for c in spec.control_points])
    dst = np.array([list(c.twd97) for c in spec.control_points], dtype=float)
    if np.ptp(src, axis=0).max() == 0:
        res.error("控制點圖上座標全部重合，無法擬合轉換")
        return res
    prm, resid = fit_lstsq(src, dst)
    a, b, tx, ty = (float(v) for v in prm)
    s = math.hypot(a, b)
    rot = math.degrees(math.atan2(b, a))
    res.metrics.update(scale=round(s, 6), rotation_deg=round(rot, 6),
                       residuals_m={c.id: round(float(r), 4) for c, r in zip(spec.control_points, resid, strict=True)})
    if len(spec.control_points) == 2:
        res.warn("只有 2 個控制點：相似變換唯一解、殘差恆為 0，無法驗證轉換，請工程師確認或提供第 3 點")
    else:
        for c, r in zip(spec.control_points, resid, strict=True):
            if r > RESIDUAL_MAX_M:
                res.error(f"控制點 {c.id} 殘差 {r:.3f} m > {RESIDUAL_MAX_M} m（座標抄錯或該點不是同一個位置）")
    if not (SCALE_RANGE[0] <= s <= SCALE_RANGE[1]):
        res.error(
            f"縮放比 s = {s:.6f} 不在 [{SCALE_RANGE[0]}, {SCALE_RANGE[1]}]：drawing_units={spec.drawing_units!r} "
            "判斷錯誤或控制點錯誤（建築圖與 TWD97 應為 1:1）"
        )
    M = [a * k, -b * k, b * k, a * k, tx, ty]

    # ---- 重讀 DXF、重建
    try:
        doc, _ = load_doc(spec.dxf_path())
        col = collect(spec)
        rebuilt = role_polygons(spec, col)
    except Exception as e:  # noqa: BLE001
        res.error(f"無法由 DXF 重建：{type(e).__name__}: {e}")
        return res
    for w in col.warnings:  # 讀圖警告（缺 xref、缺圖塊、圖層無實體…）一併列出
        res.warn(w)
    ins = col.insunits
    ins_name = INSUNITS_UNITS.get(ins)
    res.metrics["insunits_header"] = ins
    if ins_name != spec.drawing_units:
        res.warn(
            f"$INSUNITS = {ins}（{ins_name or '未設定 / 其他'}）與 drawing_units={spec.drawing_units!r} 不符："
            "header 可能是錯的，請確認圖上尺寸標註"
        )

    seam_max = max((pr.seam_max for pr, _ in rebuilt.values()), default=0.0)
    res.metrics["seam_max_drawing"] = round(seam_max, 6)
    if seam_max > tol * (1 + 1e-9):
        res.error(f"接縫最大距離 {seam_max:.4g} > join_tolerance {tol:.4g}（端點群聚串接過長）")

    # ---- 讀 geojson
    try:
        gj = json.loads(Path(geojson).read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        res.error(f"無法讀取 {geojson}：{e}")
        return res
    crs = (gj.get("crs") or {}).get("properties", {}).get("name", "")
    if not str(crs).endswith("3826"):
        res.error(f"arch.geojson CRS {crs!r} 不是 EPSG:3826")
    feats = gj.get("features", [])
    by_role: dict[str, list[dict]] = {}
    for f in feats:
        by_role.setdefault(str((f.get("properties") or {}).get("role")), []).append(f)
    unknown = set(by_role) - set(ROLE_LAYER_FIELD)
    if unknown:
        res.error(f"arch.geojson 有未知 role：{sorted(unknown)}")

    # ---- 面：有效性、數量、比對、面積守恆
    site = None
    if spec.site is not None:
        try:
            site = load_site(spec.site)
        except Exception as e:  # noqa: BLE001
            res.error(f"無法讀取基地：{type(e).__name__}: {e}")
    area_rows = {}
    for layer_role, role in POLY_ROLES.items():
        want = [affine_transform(p, M) for p in rebuilt[layer_role][0].polygons] if layer_role in rebuilt else []
        want_drawing_area = sum(p.area for p in rebuilt[layer_role][0].polygons) * k * k if layer_role in rebuilt else 0
        got_f = by_role.get(role, [])
        got = []
        for f in got_f:
            g = _polys_of(f)
            if g is None or g.geom_type != "Polygon":
                res.error(f"{role}：幾何型別 {None if g is None else g.geom_type}，應為 Polygon")
                continue
            if not g.is_valid:
                res.error(f"{role}：多邊形無效（自交等）")
            got.append(g)
            p = f.get("properties") or {}
            if not _close(p.get("area_m2"), g.area, 0.01):
                res.error(f"{role} area_m2 屬性 {p.get('area_m2')} ≠ 幾何面積 {g.area:.3f}")
            if not _close(p.get("perimeter_m"), g.length, 0.01):
                res.error(f"{role} perimeter_m 屬性 {p.get('perimeter_m')} ≠ 幾何周長 {g.length:.3f}")
            if site is not None:
                out = g.difference(site.buffer(SITE_TOL_M)).area
                if out > 1e-6:
                    res.error(f"{role} 有 {out:.3f} m² 落在基地外（容差 {SITE_TOL_M} m）")
        if got and layer_role in col.curves:
            from shapely.geometry import LineString

            src = unary_union([affine_transform(LineString(c.coords), M) for c in col.curves[layer_role]])
            near = src.buffer(tol * k + 1e-3)
            per = sum(g.exterior.length for g in got)
            on = sum(g.exterior.intersection(near).length for g in got)
            ratio = on / per if per else 0.0
            res.metrics[f"{role}_edge_on_source_lines"] = round(ratio, 4)
            if ratio < EDGE_ON_LINES_MIN:
                res.warn(
                    f"{role} 外緣只有 {ratio:.0%} 落在原圖線上（其餘為橋接的開口）：開口比例偏高，請目視 overlay.png 確認"
                )
        if role == "footprint":
            if not got:
                res.error("沒有抽出建物外框（footprint）：外牆未封閉（縫隙 > join_tolerance）或圖層 / floor_filter 錯")
            elif len(got) > 1 and not spec.allow_multiple_footprints:
                res.error(f"外牆抽出 {len(got)} 個分離外框；若確實是多棟，spec 設 allow_multiple_footprints=true")
        if not want and not got:
            continue
        W, G = unary_union(want) if want else Polygon(), unary_union(got) if got else Polygon()
        ref = max(W.area, 1e-9)
        sd = W.symmetric_difference(G).area / ref
        area_rows[role] = {"rebuilt_m2": round(W.area, 3), "geojson_m2": round(G.area, 3), "symdiff_rel": round(sd, 6)}
        if len(want) != len(got):
            res.error(f"{role}：geojson {len(got)} 個面，DXF 重建 {len(want)} 個")
        if sd > SYMDIFF_REL_MAX:
            res.error(f"{role}：geojson 與 DXF 重建對稱差 {sd:.3%} > {SYMDIFF_REL_MAX:.1%}（產物被改過或抽取不一致）")
        if want_drawing_area > 0:
            rel = abs(G.area - want_drawing_area) / want_drawing_area
            area_rows[role]["drawing_m2"] = round(want_drawing_area, 3)
            area_rows[role]["area_rel"] = round(rel, 6)
            if rel > AREA_REL_MAX:
                res.error(
                    f"{role} 面積不守恆：圖上 {want_drawing_area:.3f} m² → 轉換後 {G.area:.3f} m²"
                    f"（差 {rel:.3%} > {AREA_REL_MAX:.1%}）"
                )
    res.metrics["areas"] = area_rows

    # ---- 點
    want_pts = [affine_transform(Point(xy), M) for _, _, xy in col.points]
    got_pts = [_polys_of(f) for f in by_role.get("roof_drain", [])]
    if len(want_pts) != len(got_pts):
        res.error(f"roof_drain：geojson {len(got_pts)} 點，DXF 重建 {len(want_pts)} 點")
    for g in got_pts:
        if g is None or g.geom_type != "Point":
            res.error("roof_drain 幾何應為 Point")
            continue
        if not want_pts or min(g.distance(w) for w in want_pts) > POINT_TOL_M:
            res.error(f"roof_drain 點 ({g.x:.3f}, {g.y:.3f}) 在 DXF 重建中找不到（> {POINT_TOL_M} m）")
        if site is not None and not site.buffer(SITE_TOL_M).contains(g):
            res.error(f"roof_drain 點 ({g.x:.3f}, {g.y:.3f}) 在基地外")

    # ---- 可追溯
    resolve = _valid_handle_paths(doc)
    bad = 0
    for f in feats:
        p = f.get("properties") or {}
        role = p.get("role")
        layers = set(getattr(spec.layers, ROLE_LAYER_FIELD.get(role, ""), []) or [])
        hs = p.get("source_handles") or []
        if not hs:
            res.error(f"{role} feature 沒有 source_handles（無法追溯到 DXF 實體）")
            continue
        for h in hs:
            lay = resolve(str(h))
            if lay is None or lay not in layers:
                bad += 1
                if bad <= 10:
                    res.error(f"{role} source_handle {h!r} " + ("不存在於 DXF" if lay is None else
                              f"在圖層 {lay!r}，不屬於 spec 指定圖層 {sorted(layers)}"))
    if bad > 10:
        res.error(f"…另有 {bad - 10} 個 source_handle 無法追溯")

    # ---- transform.json 只比對
    if transform is not None:
        try:
            tr = json.loads(Path(transform).read_text(encoding="utf-8"))
            if not (_close(tr.get("scale"), s, 1e-6) and _close(tr.get("rotation_deg"), rot, 1e-5)):
                res.error(f"transform.json scale/rotation ({tr.get('scale')}, {tr.get('rotation_deg')}) ≠ 重算 ({s}, {rot})")
            for c, r in zip(tr.get("control_points", []), resid, strict=False):
                if not _close(c.get("residual_m"), float(r), 1e-4):
                    res.error(f"transform.json 控制點 {c.get('id')} 殘差 {c.get('residual_m')} ≠ 重算 {r:.4f}")
            if tr.get("drawing_units") != spec.drawing_units:
                res.error(f"transform.json drawing_units {tr.get('drawing_units')!r} ≠ spec {spec.drawing_units!r}")
        except Exception as e:  # noqa: BLE001
            res.error(f"無法讀取 transform.json：{type(e).__name__}: {e}")
    return res


def _close(a: Any, b: float, tol: float) -> bool:
    try:
        return abs(float(a) - b) <= tol + 1e-9 * abs(b)
    except (TypeError, ValueError):
        return False


def main(argv: list[str] | None = None) -> VerifyResult:
    from draftagent.schema.arch import ArchImportSpec

    p = argparse.ArgumentParser(prog="python -m draftagent.verify.arch_check", description=__doc__)
    p.add_argument("spec")
    p.add_argument("geojson")
    p.add_argument("--transform")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    res = check_arch(ArchImportSpec.from_json(a.spec), a.geojson, a.transform)
    emit(res, a.json)
    return res


if __name__ == "__main__":
    main()
