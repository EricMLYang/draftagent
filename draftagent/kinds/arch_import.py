"""圖種 `arch-import`：建築師 DXF → TWD97 GeoJSON 原始資料（外框 / 地下室 / 空地 / 落水頭）。

render = `arch.extract.run_extract`（arch.geojson + transform.json + overlay.png）
verifiers = arch_check（獨立重讀 DXF 重建、控制點殘差、縮放比、面積守恆、基地內、可追溯）
"""

from __future__ import annotations

from pathlib import Path

from draftagent.kinds._util import example, need
from draftagent.registry import Kind, register
from draftagent.schema.arch import ArchImportSpec
from draftagent.verify.base import VerifyResult


def render(spec: ArchImportSpec, out_dir: Path) -> dict[str, Path]:
    from draftagent.arch.extract import run_extract

    return run_extract(spec, Path(out_dir))


def verify_arch(spec: ArchImportSpec, artifacts: dict[str, Path]) -> VerifyResult:
    from draftagent.verify.arch_check import NAME, check_arch

    p = need(artifacts, "geojson", NAME)
    if isinstance(p, VerifyResult):
        return p
    return check_arch(spec, p, artifacts.get("transform"))


KIND = register(
    Kind(
        name="arch-import",
        spec=ArchImportSpec,
        render=render,
        verifiers=(verify_arch,),
        summary="讀建築師 DXF：外牆接線成封閉外框 + 控制點相似變換 → EPSG:3826 GeoJSON（排水 spec 的原始資料）",
        example=example("examples/arch-import.json"),
        tags=("gis", "m", "arch", "import"),
    )
)
