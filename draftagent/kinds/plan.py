"""圖種 `plan`：Level 1-1 建築平面圖（PlanSpec → DXF，mm）。"""

from __future__ import annotations

from pathlib import Path

from draftagent.kinds._util import example, insunits_verifier, need, spec_file
from draftagent.registry import Kind, register
from draftagent.schema.plan import PlanSpec
from draftagent.verify.base import VerifyResult


def render(spec: PlanSpec, out_dir: Path) -> dict[str, Path]:
    from draftagent.render.dxf import write_plan

    return {"dxf": write_plan(spec, Path(out_dir) / "plan.dxf")}


def verify_dxf(spec: PlanSpec, artifacts: dict[str, Path]) -> VerifyResult:
    from draftagent.verify.dxf_check import check_dxf

    p = need(artifacts, "dxf", "dxf_check")
    if isinstance(p, VerifyResult):
        return p
    with spec_file(spec, artifacts) as sp:
        return check_dxf(p, spec_path=sp)


KIND = register(
    Kind(
        name="plan",
        spec=PlanSpec,
        render=render,
        verifiers=(verify_dxf, insunits_verifier),
        summary="建築平面圖：房間 / 牆 / 門窗 / 總尺寸 → DXF（mm）",
        example=example("examples/plan.json"),
        tags=("dxf", "mm", "architecture", "level1"),
    )
)
