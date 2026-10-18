"""桃園建築基地流出抑制計算 CLI（向後相容的薄 shim）。

建議改用統一 CLI（自動存 spec.json、跑 verifier、寫 runs/<ts>-drainage-calc-ty/）：
    draftagent build examples/drainage-calc-ty.json --kind drainage-calc-ty

舊用法仍可用：python tools/drainage_calc.py <spec.json> -o <result.json>
寫出 result.json，並印出 markdown 計算表（面積附坪）。之後請跑：
    python -m draftagent.verify.drainage_calc_check <spec.json> --result <result.json>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from draftagent.hydro.taoyuan import compute
from draftagent.hydro.taoyuan_report import render_table
from draftagent.schema.drainage import TaoyuanSiteDrainageSpec

__all__ = ["main", "render_table"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("spec")
    ap.add_argument("-o", "--out", required=True, help="result.json 輸出路徑")
    a = ap.parse_args(argv)
    r = compute(TaoyuanSiteDrainageSpec.from_json(a.spec))
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    print(render_table(r))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
