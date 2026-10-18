"""圖種 `slope`：Level 1-3 坡度分級（SlopeSpec → GeoTIFF + GeoPackage + CSV，m）。

build 時三個輸出一律寫進 run 目錄（檔名沿用 spec），存檔的 spec.json 保留原路徑。
"""

from __future__ import annotations

import json
from pathlib import Path

from draftagent.kinds._util import example, need
from draftagent.registry import Kind, register
from draftagent.schema.slope import SlopeSpec
from draftagent.verify.base import VerifyResult


def render(spec: SlopeSpec, out_dir: Path) -> dict[str, Path]:
    from draftagent.hydro.slope import run_slope

    out_dir = Path(out_dir)
    local = spec.model_copy(
        update={
            "output_raster": out_dir / Path(spec.output_raster).name,
            "output_gpkg": out_dir / Path(spec.output_gpkg).name,
            "output_csv": out_dir / Path(spec.output_csv).name,
        }
    )
    summary = run_slope(local)
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )
    return {
        "tif": local.output_raster,
        "gpkg": local.output_gpkg,
        "csv": local.output_csv,
        "summary": out_dir / "summary.json",
    }


def verify_slope(spec: SlopeSpec, artifacts: dict[str, Path]) -> VerifyResult:
    from draftagent.verify.slope_check import NAME, check_artifacts

    p = need(artifacts, "gpkg", NAME)
    if isinstance(p, VerifyResult):
        return p
    return check_artifacts(
        p, artifacts.get("csv"), artifacts.get("tif"), spec.site_path, spec.site_layer,
        spec.dtm_path, spec.classes_path,
    )


def verify_crs(spec: SlopeSpec, artifacts: dict[str, Path]) -> VerifyResult:
    from draftagent.verify.crs_check import check_crs

    res = VerifyResult(name="crs_check")
    for key in ("gpkg", "tif"):
        p = need(artifacts, key, "crs_check")
        sub = p if isinstance(p, VerifyResult) else check_crs(p)
        res.errors += [f"{key}: {e}" for e in sub.errors]
        res.warnings += [f"{key}: {w}" for w in sub.warnings]
        res.ok = res.ok and sub.ok
        res.metrics[key] = sub.metrics
    return res


KIND = register(
    Kind(
        name="slope",
        spec=SlopeSpec,
        render=render,
        verifiers=(verify_slope, verify_crs),
        summary="坡度分析：DTM + 基地範圍 → 坡度分級 GeoTIFF / 多邊形 GPKG / 面積 CSV（依 yaml）",
        example=example("examples/slope.json"),
        tags=("gis", "m", "hydro", "level1"),
    )
)
