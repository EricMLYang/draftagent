#!/usr/bin/env python3
"""由 examples/*.json build 出 README 圖庫 PNG（docs/gallery/）。

每個 DXF 圖種 1–2 張（render.preview.render_png，max_px 控制寬度）；site-overlay / slope 以
matplotlib 簡單畫 GeoPackage。輸出 PNG 長邊 ≤ 1600 px，並以調色盤量化壓到 < 400 KB。
build 產物放暫存目錄，不寫入 runs/。只有 build 全綠才產圖。

用法：.venv/bin/python scripts/make_gallery.py [--out docs/gallery]
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_PX = 1600
MAX_BYTES = 400_000


def _shrink(png: Path) -> None:
    from PIL import Image

    im = Image.open(png).convert("RGB")
    if max(im.size) > MAX_PX:
        im.thumbnail((MAX_PX, MAX_PX), Image.LANCZOS)
    for colors in (64, 32, 16):
        im.quantize(colors=colors, method=Image.Quantize.MEDIANCUT).save(png, optimize=True)
        if png.stat().st_size < MAX_BYTES:
            return
    raise SystemExit(f"{png} 仍 ≥ {MAX_BYTES} bytes")


def _build(example: str, tmp: Path):
    from draftagent.build import build

    res = build(ROOT / "examples" / example, out=tmp, preview=False)
    if not res.ok:
        raise SystemExit(f"{example} build 未全綠，不產圖：\n{res.report()}")
    return res


def _arts(res) -> dict[str, Path]:
    return {k: Path(v) for k, v in res.artifacts.items()}


def _dxf_png(dxf: Path, out: Path, layout: str | None = None) -> None:
    from draftagent.render.preview import render_png

    render_png(dxf, out, layout=layout, max_px=MAX_PX)
    _shrink(out)


def _gpkg_png(gpkg: Path, layers: list[tuple[str, str]], out: Path, title: str) -> None:
    import geopandas as gpd
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from draftagent.render.preview import _setup_matplotlib

    _setup_matplotlib()
    fig, ax = plt.subplots(figsize=(8, 8), dpi=150)
    for layer, column in layers:
        g = gpd.read_file(gpkg, layer=layer)
        g.plot(
            ax=ax,
            column=column,
            categorical=True,
            legend=True,
            edgecolor="black",
            linewidth=0.6,
            cmap="Pastel1",
            legend_kwds={"loc": "upper left", "fontsize": 9},
        )
        if "parcel_id" in g.columns:
            for pid, geom in zip(g["parcel_id"], g.geometry, strict=True):
                c = geom.representative_point()
                ax.annotate(pid, (c.x, c.y), ha="center", va="center", fontsize=7)
    ax.set_title(title)
    ax.set_xlabel("E (m, EPSG:3826)")
    ax.set_ylabel("N (m, EPSG:3826)")
    ax.ticklabel_format(useOffset=False, style="plain")
    ax.set_aspect("equal")
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)
    _shrink(out)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--out", default=str(ROOT / "docs" / "gallery"))
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="draftagent-gallery-") as d:
        tmp = Path(d)
        a1 = _arts(_build("plan.json", tmp))
        _dxf_png(a1["dxf"], out / "plan.png")
        a1 = _arts(_build("drainage-profile.json", tmp))
        _dxf_png(a1["dxf"], out / "drainage-profile.png")
        a1 = _arts(_build("drainage-detail.json", tmp))
        _dxf_png(a1["dxf"], out / "drainage-detail.png")
        a1 = _arts(_build("drainage-plan.json", tmp))
        _dxf_png(a1["dxf"], out / "drainage-plan-design.png", layout="平面設計圖")
        _dxf_png(a1["dxf"], out / "drainage-plan-catchment.png", layout="集水區分析圖")
        a1 = _arts(_build("site-overlay.json", tmp))
        _gpkg_png(
            a1["gpkg"],
            [("parcel_zoning", "分區")],
            out / "site-overlay.png",
            "site-overlay：地號 × 使用分區",
        )
        a1 = _arts(_build("slope.json", tmp))
        _gpkg_png(a1["gpkg"], [("slope_classes", "name")], out / "slope.png", "slope：坡度分級")
    for p in sorted(out.glob("*.png")):
        print(f"{p.relative_to(ROOT)}  {p.stat().st_size // 1024} KB")


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    main()
