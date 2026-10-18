"""圖種 `drainage-calc-ty`：桃園建築基地雨水流出抑制計算（→ result.json + 計算表 calc.md）。

不產圖；係數全部由 `hydro/codes/taoyuan_runoff.yaml` 提供。
"""

from __future__ import annotations

import json
from pathlib import Path

from draftagent.kinds._util import example, need, spec_file
from draftagent.registry import Kind, register
from draftagent.schema.drainage import TaoyuanSiteDrainageSpec
from draftagent.verify.base import VerifyResult


def render(spec: TaoyuanSiteDrainageSpec, out_dir: Path) -> dict[str, Path]:
    from draftagent.hydro.taoyuan import compute
    from draftagent.hydro.taoyuan_report import render_table

    out_dir = Path(out_dir)
    r = compute(spec)
    result = out_dir / "result.json"
    result.write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    calc = out_dir / "calc.md"
    calc.write_text(render_table(r) + "\n", encoding="utf-8")
    return {"result": result, "calc": calc}


def verify_calc(spec: TaoyuanSiteDrainageSpec, artifacts: dict[str, Path]) -> VerifyResult:
    from draftagent.verify.drainage_calc_check import NAME, check

    p = need(artifacts, "result", NAME)
    if isinstance(p, VerifyResult):
        return p
    calc = artifacts.get("calc")
    with spec_file(spec, artifacts) as sp:
        return check(sp, p, calc)


KIND = register(
    Kind(
        name="drainage-calc-ty",
        spec=TaoyuanSiteDrainageSpec,
        render=render,
        verifiers=(verify_calc,),
        summary="桃園基地流出抑制計算：貯集量 / 孔口 / 溢流堰 / 管渠 → result.json + 計算表 md",
        example=example("examples/drainage-calc-ty.json"),
        tags=("calc", "hydro", "taoyuan", "level2"),
    )
)
