"""DTM 坡度分級與各級面積統計（Level 1-3）。

純函式、確定性，不得呼叫 LLM。

計算方法
--------
- 坡度百分比 = 100 × sqrt((dz/dx)² + (dz/dy)²)，梯度用 Horn (1981) 3×3 加權差分：
      a b c
      d e f        dz/dx = ((c + 2f + i) − (a + 2d + g)) / (8 · Δx)
      g h i        dz/dy = ((g + 2h + i) − (a + 2b + c)) / (8 · Δy)
- 邊界處理：DTM 最外圈一列 / 一行沒有完整 3×3 鄰域，一律設為 NaN（不外插、不複製邊緣），
  3×3 視窗內只要有任一 nodata，中心像元也是 NaN。這些像元分級為 nodata_class（預設 0），
  面積另計 nodata_area_m2，不歸入任何法定級別。
- 基地遮罩：以「像元中心落在基地多邊形內」判定（rasterio all_touched=False）。
  基地面積同時記錄像元遮罩面積與向量多邊形面積，兩者差異寫入 metrics。
- 分級門檻與邊界歸屬一律從 classes yaml 讀，程式內不寫任何法規數值。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import rasterio
import yaml
from rasterio import features
from shapely.geometry import shape
from shapely.ops import unary_union

from draftagent.schema.slope import SlopeSpec

REQUIRED_EPSG = 3826
PING_PER_M2 = 0.3025
GPKG_LAYER = "slope_classes"
CSV_FIELDS = ["class", "name", "min_pct", "max_pct", "pixel_count", "area_m2", "area_ping"]


class CRSMismatchError(ValueError):
    """輸入圖資不是 EPSG:3826。不自動猜測或轉換。"""


# ---------------------------------------------------------------- 分級表


@dataclass(frozen=True)
class SlopeClass:
    cls: int
    name: str
    min_pct: float
    max_pct: float | None
    source: str
    as_of: str


@dataclass(frozen=True)
class SlopeClassTable:
    classes: tuple[SlopeClass, ...]
    boundary: str
    verified: bool
    note: str
    round_decimals: int
    nodata_class: int
    path: Path

    def by_id(self) -> dict[int, SlopeClass]:
        return {c.cls: c for c in self.classes}


def load_slope_classes(path: str | Path) -> SlopeClassTable:
    """讀取並檢查分級 yaml：每級需有 source / as_of，門檻需由 0 起連續遞增。"""
    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if "verified" not in raw:
        raise ValueError(f"{path}: 缺少頂層 verified 欄位")
    boundary = raw.get("boundary")
    if boundary not in ("upper_inclusive", "lower_inclusive"):
        raise ValueError(
            f"{path}: boundary 必須為 upper_inclusive 或 lower_inclusive，得到 {boundary!r}"
        )
    params = raw.get("params") or {}
    if "slope_round_decimals" not in params or "nodata_class" not in params:
        raise ValueError(f"{path}: params 需含 slope_round_decimals 與 nodata_class")

    classes: list[SlopeClass] = []
    for i, c in enumerate(raw.get("classes") or []):
        for key in ("class", "name", "min_pct", "max_pct", "source", "as_of"):
            if key not in c:
                raise ValueError(f"{path}: classes[{i}] 缺少 {key}")
        if not c["source"] or not c["as_of"]:
            raise ValueError(f"{path}: classes[{i}] 的 source / as_of 不得為空")
        classes.append(
            SlopeClass(
                cls=int(c["class"]),
                name=str(c["name"]),
                min_pct=float(c["min_pct"]),
                max_pct=None if c["max_pct"] is None else float(c["max_pct"]),
                source=str(c["source"]),
                as_of=str(c["as_of"]),
            )
        )
    if not classes:
        raise ValueError(f"{path}: classes 為空")
    if classes[0].min_pct != 0:
        raise ValueError(f"{path}: 第一級 min_pct 必須為 0")
    for prev, cur in pairwise(classes):
        if prev.max_pct is None or prev.max_pct != cur.min_pct:
            raise ValueError(f"{path}: 分級不連續：{prev.name} 與 {cur.name}")
        if cur.max_pct is not None and cur.max_pct <= cur.min_pct:
            raise ValueError(f"{path}: {cur.name} 的 max_pct 必須大於 min_pct")
    if classes[-1].max_pct is not None:
        raise ValueError(f"{path}: 最後一級 max_pct 必須為 null（無上限）")
    nodata_class = int(params["nodata_class"])
    if nodata_class in {c.cls for c in classes}:
        raise ValueError(f"{path}: nodata_class 與法定級別代碼重複")

    return SlopeClassTable(
        classes=tuple(classes),
        boundary=boundary,
        verified=bool(raw["verified"]),
        note=str(raw.get("note", "")),
        round_decimals=int(params["slope_round_decimals"]),
        nodata_class=nodata_class,
        path=path,
    )


# ---------------------------------------------------------------- 讀檔與 CRS


def _epsg_of(crs: Any) -> int | None:
    if crs is None:
        return None
    try:
        return crs.to_epsg()
    except Exception:  # noqa: BLE001
        return None


def read_dtm(path: str | Path) -> tuple[np.ndarray, rasterio.Affine, Any]:
    """讀 DTM 第一波段為 float64，nodata → NaN。CRS 非 EPSG:3826 直接拋錯。"""
    with rasterio.open(path) as src:
        epsg = _epsg_of(src.crs)
        if epsg != REQUIRED_EPSG:
            raise CRSMismatchError(
                f"DTM {path} 的 CRS 為 {src.crs}（EPSG:{epsg}），必須為 EPSG:{REQUIRED_EPSG}。"
                "請先以 crs_check 確認並明確轉換，本工具不自動猜測。"
            )
        t = src.transform
        if t.b != 0 or t.d != 0:
            raise ValueError(f"DTM {path} 有旋轉的 geotransform，不支援")
        z = src.read(1).astype(np.float64)
        if src.nodata is not None:
            z[z == src.nodata] = np.nan
        return z, t, src.crs


def read_site(path: str | Path, layer: str | None = None) -> gpd.GeoDataFrame:
    gdf = gpd.read_file(path, layer=layer)
    epsg = _epsg_of(gdf.crs)
    if epsg != REQUIRED_EPSG:
        raise CRSMismatchError(
            f"基地範圍 {path} 的 CRS 為 {gdf.crs}（EPSG:{epsg}），必須為 EPSG:{REQUIRED_EPSG}。"
        )
    if gdf.empty:
        raise ValueError(f"基地範圍 {path} 沒有任何圖徵")
    return gdf


def site_geometry(gdf: gpd.GeoDataFrame):
    geom = unary_union(list(gdf.geometry))
    if geom.is_empty or geom.area <= 0:
        raise ValueError("基地範圍面積為 0")
    return geom


# ---------------------------------------------------------------- 計算


def slope_percent(z: np.ndarray, cell_x: float, cell_y: float) -> np.ndarray:
    """Horn 法坡度百分比。最外圈與任何 3×3 內含 NaN 的像元為 NaN。"""
    z = np.asarray(z, dtype=np.float64)
    out = np.full(z.shape, np.nan)
    if z.shape[0] < 3 or z.shape[1] < 3:
        return out
    a, b, c = z[:-2, :-2], z[:-2, 1:-1], z[:-2, 2:]
    d, f = z[1:-1, :-2], z[1:-1, 2:]
    g, h, i = z[2:, :-2], z[2:, 1:-1], z[2:, 2:]
    dzdx = ((c + 2 * f + i) - (a + 2 * d + g)) / (8.0 * abs(cell_x))
    dzdy = ((g + 2 * h + i) - (a + 2 * b + c)) / (8.0 * abs(cell_y))
    out[1:-1, 1:-1] = 100.0 * np.hypot(dzdx, dzdy)
    # 中心像元本身為 NaN 時，Horn 公式不用 e，需另外遮掉
    out[np.isnan(z)] = np.nan
    return out


def classify(slope: np.ndarray, table: SlopeClassTable) -> np.ndarray:
    """依 yaml 門檻分級，回傳 uint8；NaN → nodata_class。"""
    s = np.round(slope, table.round_decimals)
    inner_edges = np.array([c.max_pct for c in table.classes[:-1]], dtype=np.float64)
    # upper_inclusive: (min, max] → digitize right=True；lower_inclusive: [min, max) → right=False
    idx = np.digitize(s, inner_edges, right=(table.boundary == "upper_inclusive"))
    ids = np.array([c.cls for c in table.classes], dtype=np.uint8)
    out = ids[np.clip(idx, 0, len(ids) - 1)]
    out[np.isnan(s)] = table.nodata_class
    return out.astype(np.uint8)


def site_mask(geom, transform: rasterio.Affine, shape_: tuple[int, int]) -> np.ndarray:
    """像元中心**嚴格**落在多邊形內部 → True（中心恰在邊線上 = 不算）。

    不用 rasterio `geometry_mask`：它走 GDAL 點陣化，各平台 wheel 內建的 GDAL 版本不同，
    中心落在邊線上的像元判斷會不一樣（CI 上 Linux / Windows 比 macOS 多 1–3 個像元）。
    shapely `contains_xy` 是確定的點在多邊形內判斷，跨平台一致。
    """
    import shapely

    rows, cols = np.indices(shape_)
    xs, ys = rasterio.transform.xy(transform, rows.ravel(), cols.ravel(), offset="center")
    return shapely.contains_xy(geom, np.asarray(xs), np.asarray(ys)).reshape(shape_)


def class_stats(
    classes_arr: np.ndarray, mask: np.ndarray, cell_area: float, table: SlopeClassTable
) -> list[dict[str, Any]]:
    """各級（含 nodata_class）在基地遮罩內的像元數與面積。每一級都會列出，面積可為 0。"""
    rows = []
    vals = classes_arr[mask]
    entries = [(table.nodata_class, "nodata", None, None)] + [
        (c.cls, c.name, c.min_pct, c.max_pct) for c in table.classes
    ]
    for cid, name, lo, hi in entries:
        n = int(np.count_nonzero(vals == cid))
        area = n * cell_area
        rows.append(
            {
                "class": cid,
                "name": name,
                "min_pct": lo,
                "max_pct": hi,
                "pixel_count": n,
                "area_m2": round(area, 4),
                "area_ping": round(area * PING_PER_M2, 4),
            }
        )
    return rows


def class_polygons(
    classes_arr: np.ndarray, mask: np.ndarray, transform: rasterio.Affine
) -> dict[int, Any]:
    """像元向量化，每級合併為一個 (Multi)Polygon。"""
    polys: dict[int, list] = {}
    for geom, val in features.shapes(classes_arr, mask=mask, transform=transform, connectivity=4):
        polys.setdefault(int(val), []).append(shape(geom))
    return {k: unary_union(v) for k, v in polys.items()}


# ---------------------------------------------------------------- 管線


def run_slope(spec: SlopeSpec) -> dict[str, Any]:
    table = load_slope_classes(spec.classes_path)
    z, transform, crs = read_dtm(spec.dtm_path)
    gdf = read_site(spec.site_path, spec.site_layer)
    geom = site_geometry(gdf)

    cell_x, cell_y = abs(transform.a), abs(transform.e)
    cell_area = cell_x * cell_y
    slope = slope_percent(z, cell_x, cell_y)
    cls_arr = classify(slope, table)
    mask = site_mask(geom, transform, z.shape)
    if not mask.any():
        raise ValueError("基地範圍與 DTM 沒有重疊的像元中心")

    rows = class_stats(cls_arr, mask, cell_area, table)
    polys = class_polygons(cls_arr, mask, transform)

    # --- 輸出 GeoTIFF（全幅分級；0 = DTM 邊界 / nodata）
    for p in (spec.output_raster, spec.output_gpkg, spec.output_csv):
        Path(p).parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        spec.output_raster,
        "w",
        driver="GTiff",
        height=cls_arr.shape[0],
        width=cls_arr.shape[1],
        count=1,
        dtype="uint8",
        crs=crs,
        transform=transform,
        nodata=table.nodata_class,
        compress="deflate",
    ) as dst:
        dst.write(cls_arr, 1)

    # --- 輸出 GeoPackage：每級一列（含面積為 0 的級別，幾何為 None）
    records = []
    for r in rows:
        records.append(
            {
                "class": r["class"],
                "name": r["name"],
                "pixel_count": r["pixel_count"],
                "area_m2": r["area_m2"],
                "area_ping": r["area_ping"],
                "geometry": polys.get(r["class"]),
            }
        )
    out_gdf = gpd.GeoDataFrame(records, geometry="geometry", crs=f"EPSG:{REQUIRED_EPSG}")
    out_path = Path(spec.output_gpkg)
    if out_path.exists():
        out_path.unlink()
    out_gdf.to_file(out_path, layer=GPKG_LAYER, driver="GPKG")

    # --- 輸出 CSV
    # 帶 BOM：Windows Excel 直接開中文不亂碼
    with open(spec.output_csv, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r[k] is None else r[k]) for k in CSV_FIELDS})

    # --- 摘要
    vec_area = float(geom.area)
    pix_area = float(mask.sum()) * cell_area
    nodata_area = next(r["area_m2"] for r in rows if r["class"] == table.nodata_class)
    classified_area = sum(r["area_m2"] for r in rows if r["class"] != table.nodata_class)
    in_site = slope[mask & ~np.isnan(slope)]
    return {
        "dtm": str(spec.dtm_path),
        "site": str(spec.site_path),
        "crs": f"EPSG:{REQUIRED_EPSG}",
        "cell_size_m": [cell_x, cell_y],
        "method": "Horn 3x3; DTM 最外圈為 nodata; 基地遮罩=像元中心在內",
        "boundary": table.boundary,
        "classes_verified": table.verified,
        "classes_note": table.note,
        "site_area_vector_m2": round(vec_area, 4),
        "site_area_pixel_m2": round(pix_area, 4),
        "site_area_diff_m2": round(pix_area - vec_area, 4),
        "site_area_diff_pct": round(100.0 * (pix_area - vec_area) / vec_area, 4),
        "site_area_vector_ping": round(vec_area * PING_PER_M2, 4),
        "site_area_pixel_ping": round(pix_area * PING_PER_M2, 4),
        "classified_area_m2": round(classified_area, 4),
        "nodata_area_m2": nodata_area,
        "slope_pct_min": round(float(in_site.min()), 4) if in_site.size else None,
        "slope_pct_max": round(float(in_site.max()), 4) if in_site.size else None,
        "slope_pct_mean": round(float(in_site.mean()), 4) if in_site.size else None,
        "classes": rows,
        "outputs": {
            "raster": str(spec.output_raster),
            "gpkg": str(spec.output_gpkg),
            "csv": str(spec.output_csv),
        },
    }
