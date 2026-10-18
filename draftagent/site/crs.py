"""CRS 正規化：讀向量 → 判斷 CRS → 轉到目標 CRS，並回報做了什麼轉換。

原則：無 CRS 直接報錯，不猜；不認得的 CRS 也報錯，不猜。
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
from pyproj import CRS

# 認得、可以自動轉換的來源 CRS（EPSG code → 說明）
KNOWN_CRS: dict[int, str] = {
    3826: "TWD97 / TM2 121（本島）",
    3825: "TWD97 / TM2 119（澎湖）",
    3828: "TWD67 / TM2 121（舊資料）",
    3827: "TWD67 / TM2 119（舊資料，澎湖）",
    4326: "WGS84 經緯度",
    3857: "Web Mercator（WMTS / 網路地圖）",
}
TWD67_CODES = {3827, 3828}


class CRSMissingError(ValueError):
    """檔案沒有 CRS（例如 SHP 缺 .prj）。"""


class CRSUnsupportedError(ValueError):
    """CRS 無法辨識為已知的 EPSG code。"""


def identify_epsg(crs: CRS | None) -> int | None:
    """回傳 EPSG code；無 CRS 回 None。認不出 EPSG 時拋 CRSUnsupportedError。"""
    if crs is None:
        return None
    crs = CRS.from_user_input(crs)
    code = crs.to_epsg()
    if code is None:
        # 某些 .prj 只有 WKT 沒有 authority：比對已知 CRS 是否等價
        for known in KNOWN_CRS:
            if crs.equals(CRS.from_epsg(known), ignore_axis_order=True):
                return known
        raise CRSUnsupportedError(f"無法辨識 CRS：{crs.name!r}（沒有 EPSG code）")
    return code


def normalize_crs(
    gdf: gpd.GeoDataFrame, target: str = "EPSG:3826", label: str = "data"
) -> tuple[gpd.GeoDataFrame, list[str]]:
    """把 gdf 轉到 target。回傳 (新 gdf, 轉換說明清單)。"""
    target_code = CRS.from_user_input(target).to_epsg()
    src = identify_epsg(gdf.crs)
    if src is None:
        raise CRSMissingError(
            f"{label}：沒有 CRS（SHP 可能缺 .prj）。不猜測座標系統，請向資料提供者確認後再指定。"
        )
    if src == target_code:
        return gdf, [f"{label}：已是 EPSG:{src}，未轉換"]
    if src not in KNOWN_CRS:
        raise CRSUnsupportedError(
            f"{label}：EPSG:{src}（{gdf.crs.name}）不在自動轉換清單 {sorted(KNOWN_CRS)}，請人工確認"
        )
    out = gdf.to_crs(epsg=target_code)
    notes = [f"{label}：EPSG:{src}（{KNOWN_CRS[src]}）→ EPSG:{target_code}"]
    if src in TWD67_CODES:
        notes.append(
            f"{label}：來源為 TWD67，已做基準轉換（pyproj 預設參數，精度約數公尺）；"
            "回覆中必須註明此轉換"
        )
    if target_code == 3826 and src == 3825:
        notes.append(f"{label}：來源為澎湖 3825，已轉到本島 3826；若基地在澎湖應改用 target_crs=3825")
    return out, notes


def read_vector(
    path: str | Path, target: str = "EPSG:3826", layer: str | None = None, label: str | None = None
) -> tuple[gpd.GeoDataFrame, list[str]]:
    """讀向量檔並正規化 CRS。"""
    path = Path(path)
    gdf = gpd.read_file(path, layer=layer) if layer else gpd.read_file(path)
    return normalize_crs(gdf, target=target, label=label or path.name)
