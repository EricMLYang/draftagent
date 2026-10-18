"""坡度分級統計 verifier（Level 1-3）。純函式，不得呼叫 LLM。

用法：
    python -m draftagent.verify.slope_check <output.gpkg|output.csv> --site <site.gpkg>
        [--site-layer NAME] [--dtm <dtm.tif>] [--classes <yaml>] [--json]

檢查項目
--------
1. 分級 yaml：verified: false → WARNING（數值未經人工對照法規原文）
2. 輸出檔：GPKG 的 CRS 必須為 EPSG:3826；欄位齊全；class 代碼都在 yaml 定義內
   （另允許 nodata_class）；yaml 每一級都要出現（面積可為 0）；不得重複；不得有負值；
   area_m2 = pixel_count × 像元面積；area_ping = area_m2 × 0.3025；
   GPKG 幾何面積 = area_m2；CSV 的 min_pct / max_pct 必須與 yaml 一致
3. 面積對帳：Σ 各級面積（含 nodata）與基地向量面積比較。
   門檻 = 半個邊界像元帶 = 0.5 × 基地周長 × 像元邊長。
   理由：以「像元中心在內」柵格化時，誤差只發生在跨越邊界的像元，
   這些像元落在寬度約一個像元的邊界帶內，淨誤差正負相抵後一般遠小於半個帶寬面積。
   差異 > 1% 另發 WARNING（代表 DTM 解析度相對基地偏粗，統計僅供參考）。
   基地內有 nodata 像元 → WARNING。
4. --dtm：用 DTM 重算一次，各級像元數必須與輸出完全相同（可抓到少一級、竄改、過期輸出）；
   另以獨立的中央差分法抽樣 200 個基地內像元比對坡度（僅記 metrics，平面上兩法相同，
   曲面上本來就有差異，差異過大只發 WARNING）。
5. build 產物（check_artifacts）：分級 yaml 只能是 repo 預設（codes_lock）；gpkg 與 csv 都跑 1–4，
   且兩者逐級 pixel_count / area_m2 相同；GeoTIFF 與 DTM 同網格，基地遮罩內各級像元數 = gpkg
   pixel_count，有 DTM 時整幅分級與重算逐像元相同；gpkg 各級多邊形 ⊂ 基地.buffer(像元邊長)，
   且以像元中心柵格化後 = 重算該級像元集合（抓多邊形平移 / 錯置）。
"""

from __future__ import annotations

import argparse
import csv
import math
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np

from draftagent.hydro.slope import (
    GPKG_LAYER,
    PING_PER_M2,
    REQUIRED_EPSG,
    CRSMismatchError,
    _epsg_of,
    class_stats,
    classify,
    load_slope_classes,
    read_dtm,
    read_site,
    site_geometry,
    site_mask,
    slope_percent,
)
from draftagent.schema.slope import DEFAULT_CLASSES_PATH
from draftagent.verify.base import VerifyResult, emit
from draftagent.verify.codes_lock import LOCK_MSG, assert_default_codes, same_path

NAME = "slope_check"
WARN_DIFF_PCT = 1.0
SAMPLE_N = 200
SAMPLE_WARN_PCT = 1.0  # Horn 與中央差分抽樣差異超過此值（坡度百分點）→ WARNING


def _f(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) else x


def _load_rows(
    path: Path, res: VerifyResult
) -> tuple[list[dict[str, Any]], gpd.GeoDataFrame | None]:
    if path.suffix.lower() == ".csv":
        with open(path, encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        return rows, None
    if path.suffix.lower() == ".gpkg":
        gdf = gpd.read_file(path, layer=GPKG_LAYER)
        epsg = _epsg_of(gdf.crs)
        if epsg != REQUIRED_EPSG:
            res.error(f"{path} 的 CRS 為 EPSG:{epsg}，必須為 EPSG:{REQUIRED_EPSG}")
        rows = [dict(r) for r in gdf.drop(columns="geometry").to_dict("records")]
        return rows, gdf
    raise ValueError(f"不支援的輸出格式：{path.suffix}（只接受 .gpkg / .csv）")


def check(
    output: str | Path,
    site: str | Path,
    site_layer: str | None = None,
    dtm: str | Path | None = None,
    classes_path: str | Path = DEFAULT_CLASSES_PATH,
) -> VerifyResult:
    res = VerifyResult(name=NAME)
    output = Path(output)

    # 1. 分級表
    table = load_slope_classes(classes_path)
    yaml_ids = {c.cls for c in table.classes}
    by_id = table.by_id()
    res.metrics["classes_yaml"] = str(table.path)
    res.metrics["boundary"] = table.boundary
    if not table.verified:
        res.warn(f"分級 yaml verified: false — {table.note}（{table.path.name}）")

    # 2. 輸出檔
    try:
        rows, gdf = _load_rows(output, res)
    except Exception as e:  # noqa: BLE001
        res.error(f"無法讀取輸出 {output}：{e}")
        return res
    required = {"class", "pixel_count", "area_m2"}
    if not rows:
        res.error("輸出沒有任何分級列")
        return res
    missing_fields = required - set(rows[0])
    if missing_fields:
        res.error(f"輸出缺少欄位：{sorted(missing_fields)}")
        return res

    seen: dict[int, dict[str, Any]] = {}
    for i, r in enumerate(rows):
        cid_f = _f(r.get("class"))
        if cid_f is None or cid_f != int(cid_f):
            res.error(f"第 {i} 列 class 值無效：{r.get('class')!r}")
            continue
        cid = int(cid_f)
        if cid not in yaml_ids and cid != table.nodata_class:
            res.error(
                f"class {cid} 不在 yaml 定義內（允許：{sorted(yaml_ids)} 與 nodata {table.nodata_class}）"
            )
        if cid in seen:
            res.error(f"class {cid} 重複出現")
        area, n = _f(r.get("area_m2")), _f(r.get("pixel_count"))
        if area is None or n is None:
            res.error(f"class {cid} 的 area_m2 / pixel_count 缺值")
            continue
        if area < 0 or n < 0:
            res.error(f"class {cid} 出現負值（area_m2={area}, pixel_count={n}）")
        ping = _f(r.get("area_ping"))
        if ping is not None and abs(ping - area * PING_PER_M2) > 0.01:
            res.error(f"class {cid} area_ping={ping} ≠ area_m2 × {PING_PER_M2}")
        if output.suffix.lower() == ".csv" and cid in by_id:
            c = by_id[cid]
            lo, hi = _f(r.get("min_pct")), _f(r.get("max_pct"))
            if lo != c.min_pct or hi != c.max_pct:
                res.error(
                    f"class {cid} 門檻 ({lo}, {hi}) 與 yaml ({c.min_pct}, {c.max_pct}) 不一致"
                )
        if n == 0 and area != 0:
            res.error(f"class {cid} pixel_count=0 但 area_m2={area}")
        seen[cid] = {"area": area, "n": n}

    absent = sorted(yaml_ids - set(seen))
    if absent:
        res.error(f"輸出缺少 yaml 定義的分級：{absent}（面積為 0 也必須列出）")

    # 像元面積一致性
    cell_areas = {round(v["area"] / v["n"], 6) for v in seen.values() if v["n"] > 0}
    if len(cell_areas) != 1:
        res.error(f"各級 area_m2 / pixel_count 不一致，無法推得單一像元面積：{sorted(cell_areas)}")
        return res
    cell_area = cell_areas.pop()
    cell_size = math.sqrt(cell_area)
    res.metrics["cell_area_m2"] = cell_area

    if gdf is not None:
        for _, g in gdf.iterrows():
            geom_area = 0.0 if g.geometry is None or g.geometry.is_empty else g.geometry.area
            if abs(geom_area - float(g["area_m2"])) > max(1e-6 * cell_area, 1e-3):
                res.error(f"class {g['class']} 幾何面積 {geom_area:.2f} ≠ area_m2 {g['area_m2']}")
        geoms = [g for g in gdf.geometry if g is not None and not g.is_empty]
        if geoms:
            union_area = gpd.GeoSeries(geoms).union_all().area
            if abs(union_area - sum(g.area for g in geoms)) > 1e-3:
                res.error("各級多邊形互相重疊")

    total = sum(v["area"] for v in seen.values())
    nodata_area = seen.get(table.nodata_class, {"area": 0.0})["area"]
    res.metrics["sum_class_area_m2"] = round(total, 4)
    res.metrics["sum_class_area_ping"] = round(total * PING_PER_M2, 4)
    res.metrics["nodata_area_m2"] = nodata_area
    if nodata_area > 0:
        res.warn(f"基地內有 {nodata_area} m² 無坡度值（DTM 邊界或 nodata），未歸入任何級別")

    # 3. 基地面積對帳
    try:
        site_gdf = read_site(site, site_layer)
    except CRSMismatchError as e:
        res.error(str(e))
        return res
    geom = site_geometry(site_gdf)
    a_vec, perim = float(geom.area), float(geom.length)
    tol = 0.5 * perim * cell_size
    diff = total - a_vec
    res.metrics["site_area_vector_m2"] = round(a_vec, 4)
    res.metrics["site_area_vector_ping"] = round(a_vec * PING_PER_M2, 4)
    res.metrics["area_diff_m2"] = round(diff, 4)
    res.metrics["area_diff_pct"] = round(100.0 * diff / a_vec, 4)
    res.metrics["area_tolerance_m2"] = round(tol, 4)
    res.metrics["area_tolerance_rule"] = "0.5 × 基地周長 × 像元邊長（半個邊界像元帶）"
    if abs(diff) > tol:
        res.error(
            f"各級面積加總 {total:.2f} m² 與基地面積 {a_vec:.2f} m² 差 {diff:.2f} m²，"
            f"超過半個邊界像元帶 {tol:.2f} m²"
        )
    elif abs(diff) / a_vec * 100 > WARN_DIFF_PCT:
        res.warn(
            f"各級面積加總與基地向量面積差 {100 * diff / a_vec:.2f}%（> {WARN_DIFF_PCT}%），"
            "像元化誤差偏大，DTM 解析度相對基地偏粗"
        )

    # 4. 以 DTM 重算
    if dtm is not None:
        try:
            z, transform, _ = read_dtm(dtm)
        except CRSMismatchError as e:
            res.error(str(e))
            return res
        cx, cy = abs(transform.a), abs(transform.e)
        if abs(cx * cy - cell_area) > 1e-6:
            res.error(f"DTM 像元面積 {cx * cy} 與輸出推得的 {cell_area} 不同")
            return res
        slope = slope_percent(z, cx, cy)
        cls_arr = classify(slope, table)
        mask = site_mask(geom, transform, z.shape)
        recomputed = {
            r["class"]: r["pixel_count"] for r in class_stats(cls_arr, mask, cx * cy, table)
        }
        res.metrics["site_area_pixel_m2"] = round(float(mask.sum()) * cx * cy, 4)
        for cid, n_re in recomputed.items():
            n_out = seen.get(cid, {"n": 0})["n"]
            if int(n_out) != n_re:
                res.error(f"重算 class {cid} 像元數 {n_re} ≠ 輸出 {int(n_out)}")

        # 獨立抽樣：中央差分 vs Horn
        rr, cc = np.nonzero(mask & ~np.isnan(slope))
        inner = (rr > 0) & (rr < z.shape[0] - 1) & (cc > 0) & (cc < z.shape[1] - 1)
        rr, cc = rr[inner], cc[inner]
        if rr.size:
            rng = np.random.default_rng(0)
            pick = rng.choice(rr.size, size=min(SAMPLE_N, rr.size), replace=False)
            r_, c_ = rr[pick], cc[pick]
            dzdx = (z[r_, c_ + 1] - z[r_, c_ - 1]) / (2 * cx)
            dzdy = (z[r_ + 1, c_] - z[r_ - 1, c_]) / (2 * cy)
            s_cd = 100.0 * np.hypot(dzdx, dzdy)
            d = np.abs(s_cd - slope[r_, c_])
            res.metrics["sample_n"] = int(r_.size)
            res.metrics["sample_max_abs_diff_pct"] = round(float(np.nanmax(d)), 6)
            res.metrics["sample_class_agreement"] = round(
                float(np.mean(classify(s_cd, table) == cls_arr[r_, c_])), 4
            )
            if np.nanmax(d) > SAMPLE_WARN_PCT:
                res.warn(
                    f"抽樣中央差分與 Horn 坡度最大差 {np.nanmax(d):.3f} 百分點"
                    f"（> {SAMPLE_WARN_PCT}），地形曲率大，分級對方法敏感"
                )
    return res


def _merge(res: VerifyResult, sub: VerifyResult, prefix: str) -> None:
    for e in sub.errors:
        res.error(f"{prefix}: {e}")
    res.warnings += [f"{prefix}: {w}" for w in sub.warnings if w not in res.warnings]


def check_artifacts(
    gpkg: str | Path,
    csv_path: str | Path | None,
    tif: str | Path | None,
    site: str | Path,
    site_layer: str | None = None,
    dtm: str | Path | None = None,
    classes_path: str | Path | None = None,
) -> VerifyResult:
    """build 用：gpkg（主結果，metrics 以此為準）+ csv + tif + 多邊形位置。"""
    lock = VerifyResult(name=NAME)
    assert_default_codes(classes_path, DEFAULT_CLASSES_PATH, lock, "classes_path")
    res = check(gpkg, site, site_layer, dtm, DEFAULT_CLASSES_PATH)
    _merge(res, lock, "classes_path")
    table = load_slope_classes(DEFAULT_CLASSES_PATH)

    try:
        gdf = gpd.read_file(gpkg, layer=GPKG_LAYER)
        g_rows = {int(r["class"]): r for r in gdf.drop(columns="geometry").to_dict("records")}
    except Exception as e:  # noqa: BLE001
        res.error(f"無法讀取 {gpkg}：{e}")
        return res

    # csv：同樣完整 check()，且與 gpkg 逐級相同
    if csv_path is None or not Path(csv_path).exists():
        res.error(f"缺 CSV 產物：{csv_path}")
    else:
        _merge(res, check(csv_path, site, site_layer, dtm, DEFAULT_CLASSES_PATH), "csv")
        with open(csv_path, encoding="utf-8-sig", newline="") as fh:
            c_rows = {}
            for r in csv.DictReader(fh):
                cf = _f(r.get("class"))
                if cf is not None:
                    c_rows[int(cf)] = r
        if set(c_rows) != set(g_rows):
            res.error(f"csv 分級 {sorted(c_rows)} ≠ gpkg {sorted(g_rows)}")
        for cid in sorted(set(c_rows) & set(g_rows)):
            for k in ("pixel_count", "area_m2"):
                a, b = _f(c_rows[cid].get(k)), _f(g_rows[cid].get(k))
                if a is None or b is None or abs(a - b) > 1e-6 * max(1.0, abs(b)):
                    res.error(f"class {cid} {k}：csv {a} ≠ gpkg {b}")

    try:
        geom = site_geometry(read_site(site, site_layer))
    except (CRSMismatchError, ValueError) as e:
        res.error(str(e))
        return res
    cell = math.sqrt(float(res.metrics.get("cell_area_m2") or 0.0)) or None

    # 多邊形位置：union ⊂ 基地.buffer(像元邊長)
    geoms = [g for g in gdf.geometry if g is not None and not g.is_empty]
    if geoms and cell:
        u = gpd.GeoSeries(geoms).union_all()
        outside = u.difference(geom.buffer(cell)).area
        if outside > 1e-6:
            res.error(f"分級多邊形有 {outside:.2f} m² 落在基地 buffer({cell:g} m) 之外（多邊形位置錯誤）")

    # GeoTIFF
    if tif is None or not Path(tif).exists():
        res.error(f"缺分級 GeoTIFF 產物：{tif}")
        return res
    import rasterio

    with rasterio.open(tif) as src:
        arr = src.read(1)
        t_tr, t_epsg = src.transform, _epsg_of(src.crs)
    if t_epsg != REQUIRED_EPSG:
        res.error(f"{Path(tif).name} CRS 為 EPSG:{t_epsg}，必須為 EPSG:{REQUIRED_EPSG}")
    mask = site_mask(geom, t_tr, arr.shape)
    vals = arr[mask]
    for cid, r in g_rows.items():
        n_t = int(np.count_nonzero(vals == cid))
        n_g = _f(r.get("pixel_count"))
        if n_g is None or int(n_g) != n_t:
            res.error(f"GeoTIFF 基地遮罩內 class {cid} 像元數 {n_t} ≠ gpkg pixel_count {n_g}")
    extra = sorted(set(np.unique(vals).tolist()) - set(g_rows))
    if extra:
        res.error(f"GeoTIFF 基地遮罩內有 gpkg 沒有的分級 {extra}")

    ref = None
    if dtm is not None:
        try:
            z, d_tr, _ = read_dtm(dtm)
        except CRSMismatchError as e:
            res.error(str(e))
            return res
        if z.shape != arr.shape or not d_tr.almost_equals(t_tr):
            res.error(f"GeoTIFF 網格 {arr.shape} / {tuple(t_tr)[:6]} 與 DTM {z.shape} / {tuple(d_tr)[:6]} 不同")
        else:
            ref = classify(slope_percent(z, abs(d_tr.a), abs(d_tr.e)), table)
            n_bad = int(np.count_nonzero(ref != arr))
            res.metrics["tif_pixel_mismatch"] = n_bad
            if n_bad:
                res.error(f"GeoTIFF 有 {n_bad} 個像元的分級與 DTM 重算不同")

    # 各級多邊形以像元中心柵格化 = 該級像元集合（以重算為準，無 DTM 時以 tif）
    truth = ref if ref is not None else arr
    for _, g in gdf.iterrows():
        cid = int(g["class"])
        want = mask & (truth == cid)
        if g.geometry is None or g.geometry.is_empty:
            got = np.zeros_like(mask)
        else:
            got = site_mask(g.geometry, t_tr, arr.shape)  # 像元中心判斷，跨平台一致
        n_bad = int(np.count_nonzero(got != want))
        if n_bad:
            res.error(f"class {cid} 多邊形柵格化後與分級像元有 {n_bad} 格不符（位置 / 形狀錯誤）")
    return res


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m draftagent.verify.slope_check")
    ap.add_argument("output", help="slope 管線輸出 .gpkg 或 .csv")
    ap.add_argument("--site", required=True, help="基地範圍向量")
    ap.add_argument("--site-layer", default=None)
    ap.add_argument("--dtm", default=None, help="給定則以 DTM 重算比對")
    ap.add_argument("--classes", default=str(DEFAULT_CLASSES_PATH))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not same_path(a.classes, DEFAULT_CLASSES_PATH):
        res = VerifyResult(name=NAME)
        res.error(f"--classes {a.classes} ≠ repo 預設 {DEFAULT_CLASSES_PATH}：{LOCK_MSG}")
        emit(res, a.json)
    try:
        res = check(a.output, a.site, a.site_layer, a.dtm, a.classes)
    except Exception as e:  # noqa: BLE001
        res = VerifyResult(name=NAME)
        res.error(f"{type(e).__name__}: {e}")
    emit(res, a.json)


if __name__ == "__main__":
    main()
