"""圖種 `site-overlay`：Level 1-2 地籍 × 使用分區套疊（SiteSpec → GeoPackage + 面積表，m）。

build 時輸出一律寫進 run 目錄（覆寫 spec.output_path 的資料夾，檔名沿用），
存檔的 spec.json 保留 agent 原本寫的 output_path。
"""

from __future__ import annotations

import json
from pathlib import Path

from draftagent.kinds._util import example, need
from draftagent.registry import Kind, register
from draftagent.schema.site import SiteSpec
from draftagent.verify.base import VerifyResult


def render(spec: SiteSpec, out_dir: Path) -> dict[str, Path]:
    from draftagent.site.overlay import run_overlay

    out_dir = Path(out_dir)
    local = spec.model_copy(update={"output_path": out_dir / Path(spec.output_path).name})
    summary = run_overlay(local)
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {
        "gpkg": Path(summary["output_gpkg"]),
        "csv": Path(summary["area_table_csv"]),
        "summary": out_dir / "summary.json",
    }


def verify_crs(spec: SiteSpec, artifacts: dict[str, Path]) -> VerifyResult:
    from draftagent.verify.crs_check import check_crs

    p = need(artifacts, "gpkg", "crs_check")
    if isinstance(p, VerifyResult):
        return p
    allow = {3825} if spec.target_crs == "EPSG:3825" else None
    return check_crs(p, allow)


def verify_area(spec: SiteSpec, artifacts: dict[str, Path]) -> VerifyResult:
    from draftagent.verify.area_check import check_area

    p = need(artifacts, "gpkg", "area_check")
    if isinstance(p, VerifyResult):
        return p
    if spec.expected_area_m2 is None:
        res = VerifyResult(name="area_check")
        res.error("spec 未提供 expected_area_m2（地政登記面積）：必填，不得省略面積對帳")
        return res
    return check_area(p, spec.expected_area_m2)


def verify_overlay(spec: SiteSpec, artifacts: dict[str, Path]) -> VerifyResult:
    """由 spec 來源獨立重新疊圖，與 gpkg / CSV / summary.json 逐筆對帳。"""
    from draftagent.verify.area_check import check_overlay

    p = need(artifacts, "gpkg", "overlay_check")
    if isinstance(p, VerifyResult):
        return p
    res = check_overlay(spec, p, artifacts.get("csv"), artifacts.get("summary"))
    for key in ("csv", "summary"):
        if key not in artifacts:
            res.error(f"缺產物 {key!r}")
    return res


KIND = register(
    Kind(
        name="site-overlay",
        spec=SiteSpec,
        render=render,
        verifiers=(verify_crs, verify_area, verify_overlay),
        summary="基地套疊：地號 × 使用分區 → EPSG:3826 GeoPackage + 分區面積表（m²／坪）",
        example=example("examples/site-overlay.json"),
        tags=("gis", "m", "site", "level1"),
    )
)
