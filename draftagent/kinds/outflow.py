"""圖種 `outflow-control`：出流管制計畫書水文水理（SCS 歷線 + 滯洪演算）→ result.json + 附圖 16/18/19 + SWMM inp。"""

from __future__ import annotations

from pathlib import Path

from draftagent.kinds._util import example
from draftagent.registry import Kind, register
from draftagent.schema.outflow import OutflowControlSpec
from draftagent.verify.base import VerifyResult


def render(spec: OutflowControlSpec, out_dir: Path) -> dict[str, Path]:
    from draftagent.render.charts import render_outflow

    return render_outflow(spec, Path(out_dir))


def verify_outflow(spec: OutflowControlSpec, artifacts: dict[str, Path]) -> VerifyResult:
    from draftagent.verify.outflow_check import verify_artifacts

    return verify_artifacts(spec, artifacts)


KIND = register(
    Kind(
        name="outflow-control",
        spec=OutflowControlSpec,
        render=render,
        verifiers=(verify_outflow,),
        summary="出流管制：交替區塊雨型 + SCS-CN + 單位歷線 + 滯洪演算 → result.json、附圖16/18/19、SWMM inp",
        example=example("examples/outflow-control.json"),
        tags=("hydro", "calc", "chart", "swmm", "level2"),
    )
)
