"""面積對帳：GeoPackage 基地面積 vs 預期面積，誤差門檻預設 0.5%。

用法：python -m draftagent.verify.area_check <gpkg> --expected <m2>
        [--layer parcels] [--tolerance 0.005] [--json]

檢查項目：
1. layer 幾何有效、非空，CRS 為公尺制 TWD97（3826 / 3825）
2. layer 面積總和 vs --expected，相對誤差 ≤ tolerance
3. 若有 parcel_zoning：各分區面積加總 = parcels 面積（同門檻）；有 parcel_id 時逐筆核對
面積一律由幾何重算，不採信 area_m2 屬性欄。純函式，不得呼叫 LLM。

check_overlay（site-overlay build 用，獨立重新疊圖對帳）
4. expected_area_m2 必填（缺 → ERROR）
5. 由 spec 的 cadastre / zoning 來源以 shapely 直接 intersection 重新疊圖（不呼叫 site.overlay）：
   - parcels：地號集合相同；每筆幾何與來源地籍（轉到目標 CRS 後）Hausdorff ≤ 0.01 m
   - parcel_zoning：（地號, 分區）集合相同；每組面積 ±0.5%、幾何 Hausdorff ≤ 0.01 m
   - area_m2 / area_ping 屬性欄 = 幾何面積 / × 0.3025
6. 面積表 CSV、summary.json 逐列與重算對帳（四捨五入到 0.01）
7. parcel_id 比對時段號 / 地號正規化（「0102:101」與「102:101」視為同一筆）
8. 與已選地號共邊、同段同母號但未選取、也未列在 spec.confirmed_excluded_parcels 的地號 → WARNING（疑似漏列子號地號；實務上可能合法不納入）
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

import geopandas as gpd
import pyogrio
from shapely.ops import unary_union

from draftagent.verify.base import VerifyResult, emit

PING_PER_M2 = 0.3025
METRIC_EPSG = {3826, 3825}
ZONING_LAYER = "parcel_zoning"
MAX_TOLERANCE = 0.005  # 門檻只能收緊不能放寬（CLAUDE.md：不得為了過關放寬 verifier）
HAUSDORFF_MAX_M = 0.01
SLIVER_M2 = 1e-3  # 與 site.overlay 相同：更小的套疊碎片視為數值誤差
ROUND_TOL = 0.011  # 報表四捨五入到 0.01


def _geom_ok(res: VerifyResult, gdf: gpd.GeoDataFrame, layer: str) -> bool:
    ok = True
    if len(gdf) == 0:
        res.error(f"{layer}：沒有任何圖徵")
        return False
    epsg = gdf.crs.to_epsg() if gdf.crs else None
    if epsg not in METRIC_EPSG:
        res.error(f"{layer}：CRS 為 {epsg}，不是 TWD97 公尺制（3826/3825），面積無意義；先跑 crs_check")
        ok = False
    n_empty = int((gdf.geometry.is_empty | gdf.geometry.isna()).sum())
    if n_empty:
        res.error(f"{layer}：{n_empty} 個空幾何")
        ok = False
    n_invalid = int((~gdf.geometry.is_valid).sum())
    if n_invalid:
        res.error(f"{layer}：{n_invalid} 個無效幾何（自相交等）")
        ok = False
    if not set(gdf.geom_type.unique()) <= {"Polygon", "MultiPolygon"}:
        res.error(f"{layer}：含非面幾何 {sorted(set(gdf.geom_type))}")
        ok = False
    return ok


def _rel(a: float, b: float) -> float:
    return abs(a - b) / b if b else float("inf")


def check_area(
    path: str | Path, expected: float, layer: str = "parcels", tolerance: float = 0.005
) -> VerifyResult:
    path = Path(path)
    res = VerifyResult(name="area_check")
    res.metrics.update(file=str(path), layer=layer, tolerance=tolerance)
    if tolerance > MAX_TOLERANCE:
        res.error(f"tolerance {tolerance} 超過上限 {MAX_TOLERANCE}（0.5%）：只能收緊，不得放寬門檻")
        return res
    if not path.exists():
        res.error(f"檔案不存在：{path}")
        return res
    layers = [row[0] for row in pyogrio.list_layers(path)]
    if layer not in layers:
        res.error(f"找不到 layer {layer!r}，現有：{layers}")
        return res

    gdf = gpd.read_file(path, layer=layer)
    if not _geom_ok(res, gdf, layer):
        return res
    actual = float(gdf.geometry.area.sum())
    rel = _rel(actual, expected)
    res.metrics.update(
        expected_m2=round(expected, 3),
        actual_m2=round(actual, 3),
        actual_ping=round(actual * PING_PER_M2, 3),
        rel_error=round(rel, 6),
    )
    if rel > tolerance:
        res.error(f"{layer} 面積 {actual:.2f} m² 與預期 {expected:.2f} m² 相差 {rel:.3%}，超過 {tolerance:.2%}")

    if ZONING_LAYER == layer:
        return res
    if ZONING_LAYER not in layers:
        res.warn(f"沒有 {ZONING_LAYER} layer，略過分區加總檢查")
        return res
    pz = gpd.read_file(path, layer=ZONING_LAYER)
    if not _geom_ok(res, pz, ZONING_LAYER):
        return res
    pz_area = pz.geometry.area
    zoned = float(pz_area.sum())
    rel_z = _rel(zoned, actual)
    res.metrics.update(zoning_sum_m2=round(zoned, 3), zoning_rel_error=round(rel_z, 6))
    if "分區" in pz.columns:
        by_zone = pz_area.groupby(pz["分區"]).sum()
        res.metrics["zones_m2"] = {str(k): round(float(v), 3) for k, v in by_zone.items()}
    if rel_z > tolerance:
        res.error(
            f"{ZONING_LAYER} 各分區加總 {zoned:.2f} m² ≠ {layer} {actual:.2f} m²（差 {rel_z:.3%}）："
            "可能有地號未落在分區內或分區重疊"
        )
    if "parcel_id" in gdf.columns and "parcel_id" in pz.columns:
        per_parcel = gdf.geometry.area.groupby(gdf["parcel_id"]).sum()
        per_pz = pz_area.groupby(pz["parcel_id"]).sum()
        for pid, a in per_parcel.items():
            b = float(per_pz.get(pid, 0.0))
            if _rel(b, float(a)) > tolerance:
                res.error(f"地號 {pid}：分區加總 {b:.2f} m² ≠ 地號面積 {float(a):.2f} m²")
        extra = sorted(set(per_pz.index) - set(per_parcel.index))
        if extra:
            res.error(f"{ZONING_LAYER} 有 {layer} 沒有的 parcel_id：{extra}")
    return res


def _pid_key(pid: Any) -> tuple[str, str]:
    """parcel_id「段號或段名:地號」→ 正規化鍵（段號去前導 0、地號補成八位數字）。"""
    from draftagent.site.overlay import normalize_land_number, normalize_section_code

    sec, _, land = str(pid).rpartition(":")
    try:
        land_n = normalize_land_number(land)
    except ValueError:
        land_n = land
    return normalize_section_code(sec), land_n


def _pick_col(cols: Any, cands: tuple[str, ...]) -> str | None:
    return next((c for c in cands if c in cols), None)


def recompute_overlay(spec: Any) -> dict[str, Any]:
    """由 spec 的來源圖資獨立重新疊圖。回傳 {parcels: {key: geom}, pairs: {(key, 分區): geom}, siblings}。"""
    from draftagent.site.crs import read_vector
    from draftagent.site.overlay import (
        LAND_NO_COLS,
        SECTION_CODE_COLS,
        SECTION_NAME_COLS,
        normalize_land_number,
        normalize_section_code,
    )

    cad, _ = read_vector(spec.cadastre_path, spec.target_crs, label="地籍")
    zon, _ = read_vector(spec.zoning_path, spec.target_crs, label="分區")
    code_col = _pick_col(cad.columns, SECTION_CODE_COLS)
    name_col = _pick_col(cad.columns, SECTION_NAME_COLS)
    land_col = _pick_col(cad.columns, LAND_NO_COLS)
    if land_col is None:
        raise ValueError(f"地籍圖找不到地號欄：{list(cad.columns)}")
    rows = []
    for i, r in cad.iterrows():
        rows.append({
            "i": i,
            "raw_code": str(r[code_col]).strip() if code_col else None,
            "code": normalize_section_code(r[code_col]) if code_col else None,
            "name": str(r[name_col]).strip() if name_col else None,
            "land": normalize_land_number(r[land_col]),
            "geom": r.geometry,
        })
    parcels: dict[tuple[str, str], Any] = {}
    picked: set[int] = set()
    for grp in spec.parcels:
        for ln in grp.land_numbers:
            want = normalize_land_number(ln)
            hit = [
                r for r in rows
                if r["land"] == want
                and (not grp.section_code or r["code"] == normalize_section_code(grp.section_code))
                and (not grp.section_name or r["name"] == grp.section_name.strip())
            ]
            if len(hit) != 1:
                raise ValueError(f"地籍圖中 {grp.section_name or grp.section_code} 地號 {ln} 有 {len(hit)} 筆")
            h = hit[0]
            key = (h["code"] if h["code"] is not None else h["name"], h["land"])
            parcels[key] = h["geom"]
            picked.add(h["i"])
    zones: dict[str, Any] = {}
    for z, g in zip(zon[spec.zoning_field].astype(str), zon.geometry, strict=True):
        zones.setdefault(z, []).append(g)
    zones = {z: unary_union(gs) for z, gs in zones.items()}
    pairs: dict[tuple[tuple[str, str], str], Any] = {}
    for key, pg in parcels.items():
        for z, zg in zones.items():
            inter = pg.intersection(zg)
            if not inter.is_empty and inter.area >= SLIVER_M2:
                pairs[(key, z)] = inter
    # 可能漏列：未選、與已選共邊、同段同母號
    siblings = []
    sel_rows = [r for r in rows if r["i"] in picked]
    for r in rows:
        if r["i"] in picked:
            continue
        for s in sel_rows:
            if (r["code"], r["name"]) == (s["code"], s["name"]) and r["land"][:4] == s["land"][:4] \
                    and r["geom"].intersection(s["geom"]).length > 0:
                siblings.append((r["raw_code"] or r["name"], r["land"], s["land"]))
                break
    return {"parcels": parcels, "pairs": pairs, "zones": zones, "siblings": siblings}


def _close_round(a: Any, b: float) -> bool:
    try:
        return abs(float(a) - b) <= ROUND_TOL + 1e-6 * abs(b)
    except (TypeError, ValueError):
        return False


def check_overlay(
    spec: Any,
    gpkg: str | Path,
    csv_path: str | Path | None = None,
    summary_path: str | Path | None = None,
    tolerance: float = 0.005,
) -> VerifyResult:
    """site-overlay：從 spec 來源獨立重新疊圖，與 gpkg / CSV / summary.json 逐筆對帳。"""
    from draftagent.site.overlay import display_land_number

    res = VerifyResult(name="overlay_check")
    if tolerance > MAX_TOLERANCE:
        res.error(f"tolerance {tolerance} 超過上限 {MAX_TOLERANCE}：只能收緊，不得放寬門檻")
        return res
    if getattr(spec, "expected_area_m2", None) is None:
        res.error("spec 未提供 expected_area_m2（地政登記面積）：必填，不得省略面積對帳")
    try:
        ref = recompute_overlay(spec)
    except Exception as e:  # noqa: BLE001
        res.error(f"無法由來源圖資重新疊圖：{type(e).__name__}: {e}")
        return res
    confirmed = {_pid_key(x if ":" in x else f":{x}")[1] for x in getattr(spec, "confirmed_excluded_parcels", [])}
    for code, land, sib in ref["siblings"]:
        msg = f"地號 {code}:{display_land_number(land)} 與已選 {display_land_number(sib)} 同段同母號且共邊但未納入"
        if land in confirmed:
            res.warn(f"{msg}（spec.confirmed_excluded_parcels 已確認不納入）")
        else:
            # 只警告不擋：實務上業主常只取得部分子號地號，硬擋會誤判正確案件
            res.warn(f"{msg}：疑似漏列子號地號，請與使用者確認；確定不納入請列入 spec.confirmed_excluded_parcels")
    r_parcels, r_pairs = ref["parcels"], ref["pairs"]
    total = sum(g.area for g in r_parcels.values())
    res.metrics.update(recomputed_total_m2=round(total, 3), recomputed_pairs=len(r_pairs))
    exp = getattr(spec, "expected_area_m2", None)
    if exp is not None and _rel(total, exp) > tolerance:
        res.error(f"重新疊圖的基地面積 {total:.2f} m² 與 expected_area_m2 {exp:.2f} 相差 {_rel(total, exp):.3%}")

    gpkg = Path(gpkg)
    try:
        layers = [row[0] for row in pyogrio.list_layers(gpkg)]
        out_p = gpd.read_file(gpkg, layer="parcels")
        out_z = gpd.read_file(gpkg, layer=ZONING_LAYER) if ZONING_LAYER in layers else None
    except Exception as e:  # noqa: BLE001
        res.error(f"無法讀取 {gpkg}：{e}")
        return res
    epsg = int(spec.target_crs.split(":")[1])
    for name, g in (("parcels", out_p), (ZONING_LAYER, out_z)):
        if g is None:
            res.error(f"gpkg 缺 {name} layer")
            continue
        e = g.crs.to_epsg() if g.crs else None
        if e != epsg:
            res.error(f"{name} CRS 為 EPSG:{e}，spec target_crs 為 EPSG:{epsg}")
        if "parcel_id" not in g.columns:
            res.error(f"{name} 缺 parcel_id 欄")
            return res

    def attr_area(layer: str, label: str, row: Any) -> None:
        a = float(row.geometry.area)
        if "area_m2" in row and not abs(float(row["area_m2"]) - a) <= max(0.01, 1e-6 * a):
            res.error(f"{layer} {label} area_m2 屬性 {row['area_m2']} ≠ 幾何面積 {a:.3f}")
        if "area_ping" in row and not abs(float(row["area_ping"]) - float(row["area_m2"]) * PING_PER_M2) <= 0.01:
            res.error(f"{layer} {label} area_ping {row['area_ping']} ≠ area_m2 × {PING_PER_M2}")

    # parcels
    seen = {}
    for _, row in out_p.iterrows():
        k = _pid_key(row["parcel_id"])
        if k in seen:
            res.error(f"parcels 地號 {row['parcel_id']} 重複")
        seen[k] = row
    if set(seen) != set(r_parcels):
        miss = sorted(set(r_parcels) - set(seen))
        extra = sorted(set(seen) - set(r_parcels))
        res.error(f"parcels 地號集合與 spec 重新選取不同：缺 {miss}、多 {extra}")
    worst = 0.0
    for k, row in seen.items():
        if k not in r_parcels or row.geometry is None:
            continue
        hd = row.geometry.hausdorff_distance(r_parcels[k])
        worst = max(worst, hd)
        if hd > HAUSDORFF_MAX_M:
            res.error(f"parcels {row['parcel_id']} 幾何與來源地籍 Hausdorff {hd:.3f} m > {HAUSDORFF_MAX_M} m")
        attr_area("parcels", str(row["parcel_id"]), row)
    res.metrics["parcels_hausdorff_max_m"] = round(worst, 6)

    # parcel_zoning
    if out_z is not None:
        zseen = {}
        for _, row in out_z.iterrows():
            k = (_pid_key(row["parcel_id"]), str(row.get("分區")))
            if k in zseen:
                res.error(f"{ZONING_LAYER} ({row['parcel_id']}, {row.get('分區')}) 重複")
            zseen[k] = row
        if set(zseen) != set(r_pairs):
            res.error(
                f"{ZONING_LAYER}（地號, 分區）集合與重新疊圖不同：缺 {sorted(set(r_pairs) - set(zseen))}、"
                f"多 {sorted(set(zseen) - set(r_pairs))}"
            )
        worst = 0.0
        for k, row in zseen.items():
            if k not in r_pairs or row.geometry is None:
                continue
            want = r_pairs[k]
            a = float(row.geometry.area)
            label = f"({row['parcel_id']}, {k[1]})"
            if _rel(a, want.area) > tolerance:
                res.error(f"{ZONING_LAYER} {label} 面積 {a:.2f} m² ≠ 重新疊圖 {want.area:.2f} m²（> {tolerance:.1%}）")
            hd = row.geometry.hausdorff_distance(want)
            worst = max(worst, hd)
            if hd > HAUSDORFF_MAX_M:
                res.error(f"{ZONING_LAYER} {label} 幾何與重新疊圖 Hausdorff {hd:.3f} m > {HAUSDORFF_MAX_M} m")
            attr_area(ZONING_LAYER, label, row)
        res.metrics["pairs_hausdorff_max_m"] = round(worst, 6)

    by_zone: dict[str, float] = {}
    for (_, z), g in r_pairs.items():
        by_zone[z] = by_zone.get(z, 0.0) + g.area
    zoned = sum(by_zone.values())
    res.metrics["recomputed_zones_m2"] = {z: round(a, 3) for z, a in sorted(by_zone.items())}

    # CSV
    if csv_path is not None:
        cp = Path(csv_path)
        if not cp.exists():
            res.error(f"面積表 CSV 不存在：{cp}")
        else:
            with cp.open(encoding="utf-8-sig", newline="") as fh:
                rows = list(csv.reader(fh))
            body = {r[0]: r[1:] for r in rows[1:] if r}
            want_rows = {**by_zone, "合計": zoned}
            if set(body) != set(want_rows):
                res.error(f"面積表 CSV 列 {sorted(body)} ≠ 重算 {sorted(want_rows)}")
            for z, a in want_rows.items():
                v = body.get(z)
                if v is None:
                    continue
                if len(v) < 2 or not _close_round(v[0], a) or not _close_round(v[1], a * PING_PER_M2):
                    res.error(f"面積表 CSV「{z}」{v} ≠ 重算 {a:.2f} m² / {a * PING_PER_M2:.2f} 坪")

    # summary.json
    if summary_path is not None:
        sp = Path(summary_path)
        if not sp.exists():
            res.error(f"summary.json 不存在：{sp}")
        else:
            s = json.loads(sp.read_text(encoding="utf-8"))
            _check_summary(res, s, spec, r_parcels, r_pairs, by_zone, total)
    return res


def _check_summary(
    res: VerifyResult, s: dict[str, Any], spec: Any, r_parcels: dict, r_pairs: dict,
    by_zone: dict[str, float], total: float,
) -> None:
    def num(key: str, want: float) -> None:
        if not _close_round(s.get(key), want):
            res.error(f"summary.json {key} = {s.get(key)!r} ≠ 重算 {want:.2f}")

    num("total_area_m2", total)
    num("total_area_ping", total * PING_PER_M2)
    if s.get("expected_area_m2") != spec.expected_area_m2:
        res.error(f"summary.json expected_area_m2 = {s.get('expected_area_m2')!r} ≠ spec {spec.expected_area_m2}")
    if s.get("crs") != spec.target_crs:
        res.error(f"summary.json crs = {s.get('crs')!r} ≠ spec {spec.target_crs}")
    zs = {z.get("分區"): z for z in s.get("zones", [])}
    if set(zs) != set(by_zone):
        res.error(f"summary.json zones {sorted(zs, key=str)} ≠ 重算 {sorted(by_zone)}")
    for z, a in by_zone.items():
        if z in zs and not (_close_round(zs[z].get("area_m2"), a)
                            and _close_round(zs[z].get("area_ping"), a * PING_PER_M2)):
            res.error(f"summary.json zones「{z}」{zs[z]} ≠ 重算 {a:.2f} m²")
    ps = {_pid_key(p.get("parcel_id")): p for p in s.get("parcels", [])}
    if set(ps) != set(r_parcels):
        res.error(f"summary.json parcels 地號集合 ≠ 重算（{len(ps)} vs {len(r_parcels)} 筆）")
    for k, g in r_parcels.items():
        if k in ps and not (_close_round(ps[k].get("area_m2"), g.area)
                            and _close_round(ps[k].get("area_ping"), g.area * PING_PER_M2)):
            res.error(f"summary.json parcels {ps[k].get('parcel_id')} {ps[k]} ≠ 重算 {g.area:.2f} m²")
    pz = {(_pid_key(p.get("parcel_id")), str(p.get("分區"))): p for p in s.get("parcel_zoning", [])}
    if set(pz) != set(r_pairs):
        res.error(f"summary.json parcel_zoning（地號, 分區）集合 ≠ 重算（{len(pz)} vs {len(r_pairs)} 組）")
    for k, g in r_pairs.items():
        if k in pz and not _close_round(pz[k].get("area_m2"), g.area):
            res.error(f"summary.json parcel_zoning {pz[k]} ≠ 重算 {g.area:.2f} m²")


def main(argv: list[str] | None = None) -> VerifyResult:
    p = argparse.ArgumentParser(prog="python -m draftagent.verify.area_check", description=__doc__)
    p.add_argument("gpkg")
    p.add_argument("--expected", type=float, required=True, help="預期面積（m²）")
    p.add_argument("--layer", default="parcels")
    p.add_argument("--tolerance", type=float, default=0.005, help="相對誤差門檻，預設 0.005 = 0.5%%")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    res = check_area(a.gpkg, a.expected, a.layer, a.tolerance)
    emit(res, a.json)
    return res


if __name__ == "__main__":
    main()
