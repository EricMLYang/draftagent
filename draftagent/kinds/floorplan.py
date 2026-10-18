"""圖種 `floor-plan`：多房間平面圖（FloorPlanSpec → DXF（mm）+ IFC4）。Level 3-1。"""

from __future__ import annotations

from pathlib import Path

from draftagent.kinds._util import example, insunits_verifier, need
from draftagent.registry import Kind, register
from draftagent.schema.floorplan import FloorPlanSpec
from draftagent.verify.base import VerifyResult


def render(spec: FloorPlanSpec, out_dir: Path) -> dict[str, Path]:
    from draftagent.render.floorplan import write_floorplan
    from draftagent.render.ifc import write_ifc

    out = Path(out_dir)
    return {
        "dxf": write_floorplan(spec, out / "floorplan.dxf"),
        "ifc": write_ifc(spec, out / "floorplan.ifc"),
    }


def verify_layout(spec: FloorPlanSpec, artifacts: dict[str, Path]) -> VerifyResult:
    from draftagent.verify.floorplan_check import check_floorplan

    return check_floorplan(spec)


def verify_dxf(spec: FloorPlanSpec, artifacts: dict[str, Path]) -> VerifyResult:
    from draftagent.verify.floorplan_check import check_floorplan_dxf

    p = need(artifacts, "dxf", "floorplan_dxf_check")
    return p if isinstance(p, VerifyResult) else check_floorplan_dxf(p, spec)


def verify_ifc(spec: FloorPlanSpec, artifacts: dict[str, Path]) -> VerifyResult:
    from draftagent.verify.floorplan_check import check_floorplan_ifc

    p = need(artifacts, "ifc", "floorplan_ifc_check")
    return p if isinstance(p, VerifyResult) else check_floorplan_ifc(p, spec)


def verify_standards(spec: FloorPlanSpec, artifacts: dict[str, Path]) -> VerifyResult:
    from draftagent.verify.standards_check import check_standards

    p = need(artifacts, "dxf", "standards_check")
    return p if isinstance(p, VerifyResult) else check_standards(p, print_scale=spec.print_scale)


KIND = register(
    Kind(
        name="floor-plan",
        spec=FloorPlanSpec,
        render=render,
        verifiers=(verify_layout, verify_dxf, verify_ifc, verify_standards, insunits_verifier),
        summary="多房間平面圖：外牆 / 房間（共用牆自動合併）/ 門窗 / 尺寸鏈 → DXF（mm）+ IFC4",
        example=example("examples/floor-plan.json"),
        tags=("dxf", "ifc", "mm", "architecture", "level3"),
    )
)
