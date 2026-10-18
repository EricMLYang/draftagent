"""圖種 `drainage-profile`：排水系統縱斷面圖 + 圖框（ProfileSpec → DXF，圖紙 mm）。"""

from __future__ import annotations

from pathlib import Path

from draftagent.kinds._util import example, insunits_verifier, need, spec_file
from draftagent.registry import Kind, register
from draftagent.schema.profile import ProfileSpec
from draftagent.verify.base import VerifyResult


def render(spec: ProfileSpec, out_dir: Path) -> dict[str, Path]:
    from draftagent.render.profile import write_profile

    return {"dxf": write_profile(spec, Path(out_dir) / "profile.dxf")}


def verify_profile(spec: ProfileSpec, artifacts: dict[str, Path]) -> VerifyResult:
    from draftagent.verify.profile_check import check_profile

    p = need(artifacts, "dxf", "profile_check")
    if isinstance(p, VerifyResult):
        return p
    with spec_file(spec, artifacts) as sp:
        return check_profile(p, sp)


KIND = register(
    Kind(
        name="drainage-profile",
        spec=ProfileSpec,
        render=render,
        verifiers=(verify_profile, insunits_verifier),
        summary="排水縱斷面圖：節點樁號 / 高程 / 溝段 + 表格帶 + 圖框圖簽 → DXF（圖紙 mm）",
        example=example("examples/drainage-profile.json"),
        tags=("dxf", "mm", "hydro", "sheet", "level2"),
    )
)
