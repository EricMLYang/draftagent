"""確定性產生 Level 1-3 坡度分析 fixtures（不連網）。

python tools/make_fixtures_slope.py [--out tests/level1/1-3]

輸出：
  <out>/input/dtm.tif        合成 DTM，EPSG:3826，20 m 網格，100×100，西北角 E=299000 N=2771000
  <out>/input/site.gpkg      基地範圍（layer site，不規則五邊形）
  <out>/expected/expected.json 解析解：各級像元數 / 面積

地形設計：高程只隨 E 方向變化，由 6 段固定坡度的斜面串接（3、10、22、35、47、70 %），
每段約 16–17 個像元間距。設第 c 與 c+1 欄像元中心間的坡度為 g[c]，則 Horn 法在內部欄
c 的解析坡度為 (g[c-1] + g[c]) / 2（y 方向無變化時 Horn 退化為中央差分），
斜面內部即為該段坡度，段與段交界那一欄為兩段平均。expected 依此解析式計算，
不呼叫 draftagent.hydro.slope，以免拿實作驗證實作。

注意：expected 的分級門檻是從 slope_classes.yaml 讀出的（verified: false 草稿值）。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
import yaml
from rasterio.transform import from_origin
from shapely.geometry import Polygon

ROOT = Path(__file__).resolve().parent.parent
CLASSES_YAML = ROOT / "draftagent" / "hydro" / "codes" / "slope_classes.yaml"

NROWS = NCOLS = 100
CELL = 20.0
WEST, NORTH = 299000.0, 2771000.0
Z0 = 100.0
BAND_SLOPES_PCT = [3.0, 10.0, 22.0, 35.0, 47.0, 70.0]
BAND_GAPS = [16, 17, 17, 17, 16, 16]  # 合計 99 個欄間距
SITE_COORDS = [
    (299130.0, 2769170.0),
    (300870.0, 2769230.0),
    (300910.0, 2770650.0),
    (300100.0, 2770870.0),
    (299090.0, 2770760.0),
]


def gap_slopes() -> np.ndarray:
    g = np.concatenate(
        [np.full(n, s / 100.0) for s, n in zip(BAND_SLOPES_PCT, BAND_GAPS, strict=True)]
    )
    assert g.size == NCOLS - 1
    return g


def make_dtm() -> np.ndarray:
    g = gap_slopes()
    z_row = Z0 + np.concatenate([[0.0], np.cumsum(g * CELL)])
    return np.tile(z_row, (NROWS, 1)).astype(np.float32)


def analytic_slope_pct() -> np.ndarray:
    g = gap_slopes() * 100.0
    s = np.full((NROWS, NCOLS), np.nan)
    s[1:-1, 1:-1] = ((g[:-1] + g[1:]) / 2.0)[None, :]
    return s


def classify_from_yaml(s: np.ndarray, cfg: dict) -> np.ndarray:
    out = np.zeros(s.shape, dtype=np.uint8)
    upper_incl = cfg["boundary"] == "upper_inclusive"
    for c in cfg["classes"]:
        lo, hi = c["min_pct"], c["max_pct"]
        if upper_incl:
            sel = (s > lo) | ((s == lo) & (lo == 0))
            if hi is not None:
                sel &= s <= hi
        else:
            sel = s >= lo
            if hi is not None:
                sel &= s < hi
        out[sel & ~np.isnan(s)] = c["class"]
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "tests" / "level1" / "1-3"))
    a = ap.parse_args()
    out = Path(a.out)
    (out / "input").mkdir(parents=True, exist_ok=True)
    (out / "expected").mkdir(parents=True, exist_ok=True)

    transform = from_origin(WEST, NORTH, CELL, CELL)
    z = make_dtm()
    with rasterio.open(
        out / "input" / "dtm.tif",
        "w",
        driver="GTiff",
        height=NROWS,
        width=NCOLS,
        count=1,
        dtype="float32",
        crs="EPSG:3826",
        transform=transform,
        nodata=-9999.0,
    ) as dst:
        dst.write(z, 1)

    site = Polygon(SITE_COORDS)
    site_path = out / "input" / "site.gpkg"
    if site_path.exists():
        site_path.unlink()
    gpd.GeoDataFrame({"name": ["基地"]}, geometry=[site], crs="EPSG:3826").to_file(
        site_path, layer="site", driver="GPKG"
    )

    cfg = yaml.safe_load(CLASSES_YAML.read_text(encoding="utf-8"))
    from draftagent.hydro.slope import site_mask  # 與 renderer / verifier 同一判斷，跨平台一致

    mask = site_mask(site, transform, (NROWS, NCOLS))
    cls = classify_from_yaml(analytic_slope_pct(), cfg)
    per_class = {}
    for c in [0] + [c["class"] for c in cfg["classes"]]:
        n = int(np.count_nonzero(cls[mask] == c))
        per_class[str(c)] = {"pixel_count": n, "area_m2": n * CELL * CELL}
    expected = {
        "task": "1-3",
        "crs": "EPSG:3826",
        "cell_size_m": CELL,
        "band_slopes_pct": BAND_SLOPES_PCT,
        "classes_yaml_verified": cfg["verified"],
        "boundary": cfg["boundary"],
        "site_area_vector_m2": round(site.area, 4),
        "site_area_pixel_m2": float(mask.sum()) * CELL * CELL,
        "classes": per_class,
        "note": "解析解（不經 draftagent.hydro.slope）；門檻取自 slope_classes.yaml 草稿值",
    }
    (out / "expected" / "expected.json").write_text(
        json.dumps(expected, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(expected, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
