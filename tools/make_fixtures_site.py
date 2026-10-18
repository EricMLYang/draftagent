"""產生 Level 1-2 基地套疊的【合成】fixture（SYNTHETIC — 非真實圖資，不連網）。

輸出到 tests/level1/1-2/input/（可用第一個參數改目錄）：
- cadastre.shp        假「測試段（段號 0001）」地籍，中文欄名，EPSG:3826，4 筆地號
- zoning.shp          使用分區（住宅區 / 商業區），故意以 EPSG:4326 存，讓 CRS 正規化有事做
- cadastre_twd67.shp  同一份地籍轉成 TWD67（EPSG:3828）的「壞檔」，crs_check 必須紅燈
並把解析解面積寫到 tests/level1/1-2/expected/expected.json。

座標為固定整數（以 E=300000, N=2770000 為原點的公尺偏移，約在台北市），確定性可重現。
"""

from __future__ import annotations

import json
import sys
from fractions import Fraction
from pathlib import Path

import geopandas as gpd
from shapely.geometry import Polygon

E0, N0 = 300_000, 2_770_000
ZONE_SPLIT_X = 50  # 住宅區 / 商業區分界：x = E0 + 50

# 共用頂點（相對原點，公尺）
P = {
    "A": (0, 0), "B": (50, 0), "C": (95, 0), "D": (0, 35), "E": (50, 40), "F": (95, 45),
    "G": (10, 75), "H": (70, 80), "I": (120, 0), "J": (125, 50), "K": (110, 85),
}
# 地號用 8 碼（母號4碼+子號4碼），測試地號正規化
PARCELS = [
    ("00120000", "建", "ABED"),   # 12    全在住宅區
    ("00120001", "建", "BCFE"),   # 12-1  全在商業區
    ("00130000", "建", "DEFHG"),  # 13    跨兩區
    ("00140000", "田", "CIJKHF"),  # 14   不在 spec 內（干擾項）
]
ZONES = [
    ("住宅區", "R", [(-30, -30), (ZONE_SPLIT_X, -30), (ZONE_SPLIT_X, 120), (-30, 120)]),
    ("商業區", "C", [(ZONE_SPLIT_X, -30), (150, -30), (150, 120), (ZONE_SPLIT_X, 120)]),
]
SELECTED = ["12", "12-1", "13"]


def _abs(pts):
    return [(E0 + x, N0 + y) for x, y in pts]


def _shoelace(pts) -> Fraction:
    s = Fraction(0)
    for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1], strict=True):
        s += Fraction(x1) * Fraction(y2) - Fraction(x2) * Fraction(y1)
    return abs(s) / 2


def _clip_x(pts, x0, keep_left: bool):
    """Sutherland–Hodgman 以垂直線 x=x0 裁切，用 Fraction 求解析解。"""
    def inside(p):
        return p[0] <= x0 if keep_left else p[0] >= x0

    out = []
    for p, q in zip(pts, pts[1:] + pts[:1], strict=True):
        p = (Fraction(p[0]), Fraction(p[1]))
        q = (Fraction(q[0]), Fraction(q[1]))
        if inside(p):
            out.append(p)
        if inside(p) != inside(q):
            t = (x0 - p[0]) / (q[0] - p[0])
            out.append((Fraction(x0), p[1] + t * (q[1] - p[1])))
    return out


def analytic_expected() -> dict:
    zones = {"住宅區": Fraction(0), "商業區": Fraction(0)}
    parcels = {}
    for no, _, keys in PARCELS[:3]:
        pts = [P[k] for k in keys]
        label = f"{int(no[:4])}" + (f"-{int(no[4:])}" if int(no[4:]) else "")
        res = _shoelace(_clip_x(pts, ZONE_SPLIT_X, True))
        com = _shoelace(_clip_x(pts, ZONE_SPLIT_X, False))
        assert res + com == _shoelace(pts)
        parcels[label] = {"住宅區": float(res), "商業區": float(com), "total": float(res + com)}
        zones["住宅區"] += res
        zones["商業區"] += com
    total = sum(zones.values())
    return {
        "_note": "SYNTHETIC 合成資料的解析解（Fraction 精確計算，Sutherland–Hodgman 裁切 + shoelace）",
        "crs": "EPSG:3826",
        "parcels_m2": parcels,
        "zones_m2": {k: float(v) for k, v in zones.items()},
        "zones_ping": {k: round(float(v) * 0.3025, 4) for k, v in zones.items()},
        "total_m2": float(total),
        "total_ping": round(float(total) * 0.3025, 4),
        "tolerance": 0.005,
    }


def make_fixtures(out_dir: str | Path) -> dict[str, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    cad = gpd.GeoDataFrame(
        {
            "段名": ["測試段"] * len(PARCELS),
            "段號": ["0001"] * len(PARCELS),
            "地號": [no for no, _, _ in PARCELS],
            "地目": [lu for _, lu, _ in PARCELS],
            "SOURCE": ["SYNTHETIC"] * len(PARCELS),
        },
        geometry=[Polygon(_abs([P[k] for k in keys])) for _, _, keys in PARCELS],
        crs="EPSG:3826",
    )
    zon = gpd.GeoDataFrame(
        {
            "分區": [z for z, _, _ in ZONES],
            "代碼": [c for _, c, _ in ZONES],
            "SOURCE": ["SYNTHETIC"] * len(ZONES),
        },
        geometry=[Polygon(_abs(pts)) for _, _, pts in ZONES],
        crs="EPSG:3826",
    ).to_crs("EPSG:4326")  # 故意存成經緯度

    paths = {
        "cadastre": out / "cadastre.shp",
        "zoning": out / "zoning.shp",
        "cadastre_twd67": out / "cadastre_twd67.shp",
    }
    cad.to_file(paths["cadastre"], encoding="UTF-8")
    zon.to_file(paths["zoning"], encoding="UTF-8")
    cad.to_crs("EPSG:3828").to_file(paths["cadastre_twd67"], encoding="UTF-8")
    return paths


def main() -> None:
    repo = Path(__file__).resolve().parents[1]
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else repo / "tests/level1/1-2/input"
    paths = make_fixtures(out_dir)
    exp_path = out_dir.parent / "expected" / "expected.json"
    exp_path.parent.mkdir(parents=True, exist_ok=True)
    exp_path.write_text(json.dumps(analytic_expected(), ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    for k, p in paths.items():
        print(f"{k}: {p}")
    print(f"expected: {exp_path}")


if __name__ == "__main__":
    main()
