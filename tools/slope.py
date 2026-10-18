"""薄 CLI（向後相容）：python tools/slope.py <spec.json>

建議改用統一 CLI（輸出寫進 runs/、自動跑 slope_check + crs_check）：
    draftagent build <spec.json> --kind slope

讀 SlopeSpec → draftagent.hydro.slope.run_slope → 印出摘要 JSON。
產出後請另跑 verifier：
    python -m draftagent.verify.slope_check <output_gpkg> --site <site_path> --dtm <dtm_path>
"""

from __future__ import annotations

import json
import sys

from draftagent.hydro.slope import run_slope
from draftagent.schema.slope import SlopeSpec


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    spec = SlopeSpec.from_json(argv[0])
    summary = run_slope(spec)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
