"""基地套疊 CLI（向後相容的薄封裝）：python tools/gis_overlay.py <spec.json>

建議改用統一 CLI（輸出寫進 runs/、自動跑 crs_check + area_check）：
    draftagent build <spec.json> --kind site-overlay

讀 SiteSpec → draftagent.site.overlay.run_overlay → 印出 JSON 摘要。
之後請接著跑 crs_check 與 area_check。
"""

from __future__ import annotations

import argparse
import json
import sys

from draftagent.schema.site import SiteSpec
from draftagent.site.overlay import run_overlay


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("spec")
    a = p.parse_args(argv)
    summary = run_overlay(SiteSpec.from_json(a.spec))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
