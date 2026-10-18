"""DetailSheetSpec JSON → 排水系統各部詳圖 DXF（可另出 PNG 預覽）。薄 CLI，邏輯在 draftagent.render。

用法：python tools/gen_detail.py <spec.json> -o <out.dxf> [--png <preview.png>] [--dpi 200]
"""

from __future__ import annotations

import argparse

from draftagent.render.detail import write_detail_sheet
from draftagent.schema.detail import DetailSheetSpec


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("spec")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--png", help="另輸出 PNG 預覽（需 matplotlib）")
    ap.add_argument("--dpi", type=int, default=200)
    args = ap.parse_args(argv)
    out = write_detail_sheet(DetailSheetSpec.from_json(args.spec), args.out)
    print(f"wrote {out}")
    if args.png:
        from draftagent.render.preview import render_png

        print(f"wrote {render_png(out, args.png, dpi=args.dpi)}")


if __name__ == "__main__":
    main()
