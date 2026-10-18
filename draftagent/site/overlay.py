"""基地套疊（Level 1-2）：地籍篩地號 → CRS 正規化 → 與使用分區 intersection → GeoPackage + 面積表。

純函式、確定性、不呼叫 LLM。面積一律由幾何在目標 CRS（公尺）下計算，不採信屬性欄。
"""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd

from draftagent.schema.site import SiteSpec
from draftagent.site.crs import read_vector

PING_PER_M2 = 0.3025
SLIVER_M2 = 1e-3  # 小於此面積的 intersection 碎片視為 CRS 轉換造成的數值誤差並捨棄

# 地籍常見欄名（第一個找到的為準）
SECTION_CODE_COLS = ("段號", "段代碼", "SECTNO", "SECT_NO", "SECTION")
SECTION_NAME_COLS = ("段名", "段名稱", "SECTNAME", "SECT_NAME")
LAND_NO_COLS = ("地號", "LANDNO", "LAND_NO", "PARCELNO")
LAND_USE_COLS = ("地目",)


class OverlayError(ValueError):
    pass


def normalize_land_number(s: str) -> str:
    """地號正規化為 8 碼「母號4碼+子號4碼」：'12'、'12-1'、'0012-0001'、'00120001' 皆可。"""
    s = str(s).strip()
    m = re.fullmatch(r"(\d{1,4})(?:-(\d{1,4}))?", s)
    if m:
        return f"{int(m.group(1)):04d}{int(m.group(2) or 0):04d}"
    if re.fullmatch(r"\d{8}", s):
        return s
    raise OverlayError(f"無法解析地號：{s!r}")


def display_land_number(norm: str) -> str:
    mother, child = int(norm[:4]), int(norm[4:])
    return f"{mother}-{child}" if child else f"{mother}"


def normalize_section_code(s: Any) -> str:
    s = str(s).strip()
    return str(int(s)) if s.isdigit() else s


def _pick(cols: Any, candidates: tuple[str, ...]) -> str | None:
    for c in candidates:
        if c in cols:
            return c
    return None


def select_parcels(cadastre: gpd.GeoDataFrame, spec: SiteSpec) -> gpd.GeoDataFrame:
    code_col = _pick(cadastre.columns, SECTION_CODE_COLS)
    name_col = _pick(cadastre.columns, SECTION_NAME_COLS)
    land_col = _pick(cadastre.columns, LAND_NO_COLS)
    if land_col is None:
        raise OverlayError(f"地籍圖找不到地號欄（試過 {LAND_NO_COLS}），現有欄位：{list(cadastre.columns)}")

    cad = cadastre.copy()
    cad["_land"] = cad[land_col].map(normalize_land_number)
    cad["_code"] = cad[code_col].map(normalize_section_code) if code_col else None
    cad["_name"] = cad[name_col].astype(str).str.strip() if name_col else None

    picked = []
    for group in spec.parcels:
        mask = pd.Series(True, index=cad.index)
        if group.section_code:
            if code_col is None:
                raise OverlayError("spec 指定了段號，但地籍圖沒有段號欄")
            mask &= cad["_code"] == normalize_section_code(group.section_code)
        if group.section_name:
            if name_col is None:
                raise OverlayError("spec 指定了段名，但地籍圖沒有段名欄")
            mask &= cad["_name"] == group.section_name.strip()
        section = group.section_name or group.section_code
        for ln in group.land_numbers:
            hit = cad[mask & (cad["_land"] == normalize_land_number(ln))]
            if len(hit) == 0:
                raise OverlayError(f"地籍圖中找不到 {section} 地號 {ln}")
            if len(hit) > 1:
                raise OverlayError(f"{section} 地號 {ln} 在地籍圖中有 {len(hit)} 筆，請先確認資料")
            picked.append(hit)

    sel = pd.concat(picked)
    if sel.index.duplicated().any():
        raise OverlayError("spec 中有重複的地號")
    sel = sel.reset_index(drop=True)
    use_col = _pick(sel.columns, LAND_USE_COLS)
    # parcel_id 的段號保留地籍原字串（例「0102」，不吃掉前導 0）；比對時才正規化
    raw_code = sel[code_col].astype(str).str.strip() if code_col else [None] * len(sel)
    out = gpd.GeoDataFrame(
        {
            "parcel_id": [
                f"{c or n}:{display_land_number(x)}"
                for c, n, x in zip(raw_code, sel["_name"], sel["_land"], strict=True)
            ],
            "段名": sel[name_col].astype(str) if name_col else None,
            "段號": sel[code_col].astype(str) if code_col else None,
            "地號": sel["_land"].map(display_land_number),
            "地目": sel[use_col].astype(str) if use_col else None,
        },
        geometry=sel.geometry.values,
        crs=cadastre.crs,
    )
    return out


def run_overlay(spec: SiteSpec) -> dict[str, Any]:
    """執行套疊並寫出 GeoPackage 與面積表 CSV；回傳摘要 dict。"""
    warnings: list[str] = []
    cadastre, cad_notes = read_vector(spec.cadastre_path, spec.target_crs, label="地籍")
    zoning, zon_notes = read_vector(spec.zoning_path, spec.target_crs, label="分區")

    if spec.zoning_field not in zoning.columns:
        raise OverlayError(
            f"分區圖找不到欄位 {spec.zoning_field!r}，現有欄位：{list(zoning.columns)}"
        )
    invalid = int((~cadastre.geometry.is_valid).sum()) + int((~zoning.geometry.is_valid).sum())
    if invalid:
        raise OverlayError(f"輸入有 {invalid} 個無效幾何，請先修正（不自動 make_valid 以免改動面積）")

    parcels = select_parcels(cadastre, spec)
    parcels["area_m2"] = parcels.geometry.area

    zon = gpd.GeoDataFrame(
        {"分區": zoning[spec.zoning_field].astype(str)}, geometry=zoning.geometry, crs=zoning.crs
    )
    pz = gpd.overlay(parcels[["parcel_id", "段號", "地號", "geometry"]], zon, how="intersection",
                     keep_geom_type=True)
    pz["area_m2"] = pz.geometry.area
    n_sliver = int((pz["area_m2"] < SLIVER_M2).sum())
    pz = pz[pz["area_m2"] >= SLIVER_M2]
    # 同一地號同一分區若被切成多塊，合併成一筆
    pz = pz.dissolve(by=["parcel_id", "分區"], as_index=False, aggfunc="first")
    pz["area_m2"] = pz.geometry.area
    pz["area_ping"] = pz["area_m2"] * PING_PER_M2
    pz = pz.sort_values(["parcel_id", "分區"]).reset_index(drop=True)
    pz = pz[["parcel_id", "段號", "地號", "分區", "area_m2", "area_ping", "geometry"]]
    if n_sliver:
        warnings.append(f"捨棄 {n_sliver} 個 < {SLIVER_M2} m² 的套疊碎片（CRS 轉換數值誤差）")

    total = float(parcels["area_m2"].sum())
    zoned = float(pz["area_m2"].sum())
    if total > 0 and abs(total - zoned) / total > 1e-4:
        warnings.append(f"有 {total - zoned:.2f} m² 未落在任何分區內（或分區重疊），請檢查分區圖")

    out = Path(spec.output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    parcels.to_file(out, layer="parcels", driver="GPKG")
    pz.to_file(out, layer="parcel_zoning", driver="GPKG")

    by_zone = pz.groupby("分區", sort=True)["area_m2"].sum()
    zones = [
        {"分區": z, "area_m2": round(float(a), 2), "area_ping": round(float(a) * PING_PER_M2, 2)}
        for z, a in by_zone.items()
    ]
    csv_path = out.with_name(out.stem + "_areas.csv")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["分區", "面積_m2", "面積_坪"])
        for z in zones:
            w.writerow([z["分區"], f"{z['area_m2']:.2f}", f"{z['area_ping']:.2f}"])
        w.writerow(["合計", f"{zoned:.2f}", f"{zoned * PING_PER_M2:.2f}"])

    return {
        "output_gpkg": str(out),
        "area_table_csv": str(csv_path),
        "crs": spec.target_crs,
        "crs_notes": cad_notes + zon_notes,
        "parcels": [
            {"parcel_id": r.parcel_id, "地號": r.地號, "area_m2": round(float(r.area_m2), 2),
             "area_ping": round(float(r.area_m2) * PING_PER_M2, 2)}
            for r in parcels.itertuples()
        ],
        "parcel_zoning": [
            {"parcel_id": r.parcel_id, "分區": r.分區, "area_m2": round(float(r.area_m2), 2)}
            for r in pz.itertuples()
        ],
        "zones": zones,
        "total_area_m2": round(total, 2),
        "total_area_ping": round(total * PING_PER_M2, 2),
        "expected_area_m2": spec.expected_area_m2,
        "warnings": warnings,
    }
