"""確定性產生 `examples/data/` 的【合成】圖資（SYNTHETIC — 非真實圖資，不連網）。

給 `examples/site-overlay.json`、`examples/slope.json`（`draftagent new <kind>` 的範例）使用。
刻意與 tests/level1/1-2、1-3 的考題圖資**不同**（不同段名 / 段號 / 地號 / 分區 / 座標 / 網格 / 地形），
範例只示範 spec 寫法，不能拿來抄考題答案。

python tools/make_examples.py [--out examples/data]

輸出：
  <out>/site/cadastre.shp   假「範例段（段號 0102）」地籍，中文欄名，EPSG:3826，4 筆地號（1 筆干擾項）
  <out>/site/zoning.shp     住宅區 / 工業區（以水平線分界），存成 EPSG:4326
  <out>/slope/dtm.tif       合成 DTM，EPSG:3826，10 m 網格，48 列 × 40 欄，高程只隨 N 方向變化
  <out>/slope/site.gpkg     基地範圍（layer site，四邊形）
並印出範例 spec 需要的 expected_area_m2（地號面積解析解）。
"""

from __future__ import annotations

import argparse
import json
from fractions import Fraction
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import Polygon

ROOT = Path(__file__).resolve().parent.parent

NOTE = """本目錄所有圖資皆為【合成資料 SYNTHETIC】，由 tools/make_examples.py 以固定座標產生，
不是國土測繪中心或任何機關的真實地籍 / 使用分區 / DTM，僅供 `draftagent new` 範例使用。
與 tests/level*/ 的評估考題圖資刻意不同。
"""

# ---------------------------------------------------------------- site-overlay

SITE_E0, SITE_N0 = 245_000, 2_700_000
ZONE_SPLIT_Y = 30  # 住宅區（北）/ 工業區（南）分界：y = N0 + 30
SITE_PTS = {
    "A": (0, 0),
    "B": (40, 0),
    "C": (0, 25),
    "D": (40, 35),
    "E": (0, 60),
    "F": (30, 60),
    "G": (70, 0),
    "H": (70, 50),
    "I": (40, 60),
}
# 地號欄位為 8 碼（母號 4 碼 + 子號 4 碼）
SITE_PARCELS = [
    ("01010000", "建", "ABDC"),  # 101    跨兩區
    ("01010003", "建", "CDIFE"),  # 101-3  大部分在住宅區
    ("01020000", "建", "BGHD"),  # 102    跨兩區
    ("01030000", "田", "DHI"),  # 103    不在範例 spec 內（干擾項）
]
SITE_ZONES = [
    ("住宅區", "R", [(-20, ZONE_SPLIT_Y), (100, ZONE_SPLIT_Y), (100, 90), (-20, 90)]),
    ("工業區", "I", [(-20, -20), (100, -20), (100, ZONE_SPLIT_Y), (-20, ZONE_SPLIT_Y)]),
]
SITE_SELECTED = 3  # 前 3 筆


def _shoelace(pts) -> Fraction:
    s = Fraction(0)
    for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1], strict=True):
        s += Fraction(x1) * Fraction(y2) - Fraction(x2) * Fraction(y1)
    return abs(s) / 2


def site_area_m2() -> float:
    return float(sum(_shoelace([SITE_PTS[k] for k in keys]) for _, _, keys in SITE_PARCELS[:3]))


def make_site(out: Path) -> dict[str, Path]:
    out.mkdir(parents=True, exist_ok=True)

    def ab(pts):
        return [(SITE_E0 + x, SITE_N0 + y) for x, y in pts]

    n = len(SITE_PARCELS)
    cad = gpd.GeoDataFrame(
        {
            "段名": ["範例段"] * n,
            "段號": ["0102"] * n,
            "地號": [no for no, _, _ in SITE_PARCELS],
            "地目": [lu for _, lu, _ in SITE_PARCELS],
            "SOURCE": ["SYNTHETIC"] * n,
        },
        geometry=[Polygon(ab([SITE_PTS[k] for k in keys])) for _, _, keys in SITE_PARCELS],
        crs="EPSG:3826",
    )
    zon = gpd.GeoDataFrame(
        {
            "分區": [z for z, _, _ in SITE_ZONES],
            "代碼": [c for _, c, _ in SITE_ZONES],
            "SOURCE": ["SYNTHETIC"] * len(SITE_ZONES),
        },
        geometry=[Polygon(ab(pts)) for _, _, pts in SITE_ZONES],
        crs="EPSG:3826",
    ).to_crs("EPSG:4326")
    paths = {"cadastre": out / "cadastre.shp", "zoning": out / "zoning.shp"}
    cad.to_file(paths["cadastre"], encoding="UTF-8")
    zon.to_file(paths["zoning"], encoding="UTF-8")
    (out / "SYNTHETIC.txt").write_text(NOTE, encoding="utf-8")
    return paths


# ---------------------------------------------------------------- slope

DTM_ROWS, DTM_COLS = 48, 40
DTM_CELL = 10.0
DTM_WEST, DTM_NORTH = 212_000.0, 2_655_000.0
DTM_Z0 = 350.0
# 由南（最後一列）往北的各段坡度與列間距數（合計 DTM_ROWS - 1）
DTM_BAND_SLOPES_PCT = [2.0, 12.0, 25.0, 36.0, 48.0, 62.0]
DTM_BAND_GAPS = [8, 8, 8, 8, 8, 7]
DTM_SITE = [
    (212_045.0, 2_654_545.0),
    (212_355.0, 2_654_565.0),
    (212_335.0, 2_654_955.0),
    (212_065.0, 2_654_935.0),
]


def make_dtm() -> np.ndarray:
    g = np.concatenate(
        [np.full(n, s / 100.0) for s, n in zip(DTM_BAND_SLOPES_PCT, DTM_BAND_GAPS, strict=True)]
    )
    assert g.size == DTM_ROWS - 1
    z_south_to_north = DTM_Z0 + np.concatenate([[0.0], np.cumsum(g * DTM_CELL)])
    col = z_south_to_north[::-1]  # 第 0 列是最北
    return np.tile(col[:, None], (1, DTM_COLS)).astype(np.float32)


def make_slope(out: Path) -> dict[str, Path]:
    out.mkdir(parents=True, exist_ok=True)
    dtm = out / "dtm.tif"
    with rasterio.open(
        dtm,
        "w",
        driver="GTiff",
        height=DTM_ROWS,
        width=DTM_COLS,
        count=1,
        dtype="float32",
        crs="EPSG:3826",
        transform=from_origin(DTM_WEST, DTM_NORTH, DTM_CELL, DTM_CELL),
        nodata=-9999.0,
        compress="deflate",
    ) as dst:
        dst.write(make_dtm(), 1)
    site = out / "site.gpkg"
    if site.exists():
        site.unlink()
    gpd.GeoDataFrame({"name": ["範例基地"]}, geometry=[Polygon(DTM_SITE)], crs="EPSG:3826").to_file(
        site, layer="site", driver="GPKG"
    )
    (out / "SYNTHETIC.txt").write_text(NOTE, encoding="utf-8")
    return {"dtm": dtm, "site": site}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "examples" / "data"))
    a = ap.parse_args()
    out = Path(a.out)
    paths = {**make_site(out / "site"), **make_slope(out / "slope")}
    print(json.dumps({k: str(v) for k, v in paths.items()}, ensure_ascii=False, indent=2))
    print(f"site-overlay expected_area_m2 = {site_area_m2()}")


if __name__ == "__main__":
    main()
