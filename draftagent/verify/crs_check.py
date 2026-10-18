"""CRS 檢查：任何向量 / 栅格進來先驗 CRS，非 EPSG:3826 報錯並建議轉換指令。

用法：python -m draftagent.verify.crs_check <file> [--allow 3825] [--json]

- 向量：.gpkg（逐 layer 檢查）、.shp、.geojson
- 栅格：.tif / .tiff（rasterio）
- TWD67（EPSG:3827 / 3828）一律紅燈並特別點名
- EPSG:3825（澎湖）預設紅燈；加 --allow 3825 且範圍落在澎湖時降為 warning
純函式，不得呼叫 LLM。
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import pyogrio
from pyproj import CRS

from draftagent.verify.base import VerifyResult, emit

TARGET = 3826
TWD67 = {3827: "TWD67 / TM2 119", 3828: "TWD67 / TM2 121"}
RASTER_EXT = {".tif", ".tiff"}

# 合理範圍（公尺）。超出代表 CRS 標籤可能錯（例如經緯度資料被標成 3826）
BOUNDS_3826 = (-50_000.0, 2_300_000.0, 500_000.0, 3_000_000.0)  # 台灣本島 + 外島寬鬆框
BOUNDS_PENGHU_3825 = (270_000.0, 2_550_000.0, 340_000.0, 2_645_000.0)  # 澎湖群島


def _epsg(crs_input) -> tuple[int | None, str]:
    if not crs_input:
        return None, ""
    crs = CRS.from_user_input(crs_input)
    code = crs.to_epsg()
    if code is None:
        for known in (3826, 3825, 3827, 3828, 4326, 3857):
            if crs.equals(CRS.from_epsg(known), ignore_axis_order=True):
                code = known
                break
    return code, crs.name


def _within(b, box) -> bool:
    return b[0] >= box[0] and b[1] >= box[1] and b[2] <= box[2] and b[3] <= box[3]


def _suggest(path: Path, raster: bool, src: str) -> str:
    out = path.with_name(path.stem + "_3826" + (".tif" if raster else ".gpkg"))
    if raster:
        return f"gdalwarp -s_srs {src} -t_srs EPSG:3826 {path} {out}"
    return (
        f"ogr2ogr -s_srs {src} -t_srs EPSG:3826 {out} {path}  "
        f"或 Python：gpd.read_file('{path}').to_crs(3826).to_file('{out}')"
    )


def _check_one(
    res: VerifyResult, path: Path, label: str, crs_input, bounds, raster: bool, allow: set[int]
) -> None:
    code, name = _epsg(crs_input)
    bounds_ok = bounds is not None and all(math.isfinite(v) for v in bounds)
    res.metrics[f"{label}.epsg"] = code if code is not None else (name or None)
    if bounds_ok:
        res.metrics[f"{label}.bounds"] = [round(float(v), 3) for v in bounds]

    if not crs_input:
        res.error(f"{label}：沒有 CRS（SHP 缺 .prj？）。不得猜測，請向資料提供者確認座標系統")
        return
    if code is None:
        res.error(f"{label}：無法辨識的 CRS {name!r}，請確認後轉成 EPSG:3826")
        return
    if code in TWD67:
        res.error(
            f"{label}：EPSG:{code}（{TWD67[code]}）是 TWD67 舊座標，與 TWD97 相差約 800 m，"
            f"必須先轉換並在回覆註明。建議：{_suggest(path, raster, f'EPSG:{code}')}"
        )
        return
    if code == 3825:
        if 3825 not in allow:
            res.error(
                f"{label}：EPSG:3825（TWD97 TM2 119，澎湖）。本島基地請轉 3826："
                f"{_suggest(path, raster, 'EPSG:3825')}；若確為澎湖資料請加 --allow 3825"
            )
        elif bounds_ok and not _within(bounds, BOUNDS_PENGHU_3825):
            res.error(f"{label}：標成 EPSG:3825 但範圍 {bounds} 不在澎湖，--allow 3825 不適用")
        else:
            res.warn(f"{label}：EPSG:3825（澎湖），依 --allow 3825 放行")
        return
    if code != TARGET:
        res.error(f"{label}：EPSG:{code}（{name}）非 EPSG:3826。建議：{_suggest(path, raster, f'EPSG:{code}')}")
        return
    if bounds_ok and not _within(bounds, BOUNDS_3826):
        res.error(
            f"{label}：標成 EPSG:3826 但範圍 {[round(float(v), 3) for v in bounds]} 不在台灣，"
            "CRS 標籤可能錯誤（例如經緯度資料被標成 3826）"
        )


def check_crs(path: str | Path, allow: set[int] | None = None) -> VerifyResult:
    path = Path(path)
    allow = set(allow or ())
    res = VerifyResult(name="crs_check")
    res.metrics["file"] = str(path)
    if not path.exists():
        res.error(f"檔案不存在：{path}")
        return res

    if path.suffix.lower() in RASTER_EXT:
        import rasterio

        with rasterio.open(path) as src:
            crs_input = src.crs.to_wkt() if src.crs else None
            _check_one(res, path, "raster", crs_input, tuple(src.bounds), True, allow)
        return res

    try:
        layers = [row[0] for row in pyogrio.list_layers(path)]
    except Exception as e:  # noqa: BLE001 - 讀檔失敗就是紅燈
        res.error(f"無法讀取向量檔：{e}")
        return res
    if not layers:
        res.error("檔案中沒有任何 layer")
        return res
    res.metrics["layers"] = layers
    for layer in layers:
        info = pyogrio.read_info(path, layer=layer)
        _check_one(res, path, layer, info.get("crs"), info.get("total_bounds"), False, allow)
    return res


def main(argv: list[str] | None = None) -> VerifyResult:
    p = argparse.ArgumentParser(prog="python -m draftagent.verify.crs_check", description=__doc__)
    p.add_argument("file")
    p.add_argument("--allow", type=int, action="append", default=[], choices=[3825],
                   help="額外允許的 EPSG（目前只接受 3825，澎湖）")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    res = check_crs(a.file, set(a.allow))
    emit(res, a.json)
    return res


if __name__ == "__main__":
    main()
