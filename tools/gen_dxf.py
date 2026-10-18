"""PlanSpec JSON → DXF。薄 CLI（向後相容），邏輯在 draftagent.render.dxf。

建議改用統一 CLI（自動存 spec.json、跑 dxf_check + units_check、出 PNG）：
    draftagent new plan -o work/plan.py && draftagent build work/plan.py

用法：python tools/gen_dxf.py <spec.json> -o <out.dxf>
"""

from __future__ import annotations

import argparse

from draftagent.render.dxf import write_plan
from draftagent.schema.plan import PlanSpec


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("spec")
    ap.add_argument("-o", "--out", required=True)
    args = ap.parse_args(argv)
    print(f"wrote {write_plan(PlanSpec.from_json(args.spec), args.out)}")


if __name__ == "__main__":
    main()
