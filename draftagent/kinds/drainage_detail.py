"""圖種 `drainage-detail`：排水系統各部詳圖（U 型溝 / 筏基滯洪 / 放流井 / 孔口板 / 出水口保護工 / 銘牌）。"""

from __future__ import annotations

from pathlib import Path

from draftagent.kinds._util import example, insunits_verifier, need, spec_file
from draftagent.registry import Kind, register
from draftagent.schema.detail import DetailSheetSpec
from draftagent.verify.base import VerifyResult


def render(spec: DetailSheetSpec, out_dir: Path) -> dict[str, Path]:
    from draftagent.render.detail import write_detail_sheet

    return {"dxf": write_detail_sheet(spec, Path(out_dir) / "detail.dxf")}


def verify_detail(spec: DetailSheetSpec, artifacts: dict[str, Path]) -> VerifyResult:
    from draftagent.verify.detail_check import check_detail

    p = need(artifacts, "dxf", "detail_check")
    if isinstance(p, VerifyResult):
        return p
    with spec_file(spec, artifacts) as sp:
        return check_detail(p, sp)


KIND = register(
    Kind(
        name="drainage-detail",
        spec=DetailSheetSpec,
        render=render,
        # 詳圖以 dimlfac = 比例分母顯示實尺，不套用 dxf_check 的 dimlfac=1 規則；由 detail_check 驗證顯示值
        verifiers=(verify_detail, insunits_verifier),
        summary="排水系統各部詳圖：U型溝/筏基滯洪/放流井/孔口板/出水口保護工/銘牌 → DXF（圖紙 mm）",
        example=example("examples/drainage-detail.json"),
        tags=("dxf", "mm", "hydro", "detail", "level2"),
    )
)
