"""附圖：讀 drainage-plan 產出的 DXF 圖簽（圖號 / 圖名 / 比例），各出一張 PNG 供計畫書嵌入。

圖簽值**以 DXF 為準**（不從 spec 另算），report_check 再獨立讀 DXF 並與 spec 的圖號 / 比例對帳。
照片（spec.report.photos）複製到同一資料夾。純函式，不得呼叫 LLM。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

FIG_DIR = "report_figures"
PNG_DPI = 150
PNG_MAX_PX = 2400

# (artifact key, paperspace layout 或 None = modelspace, vocab produced_by)
SHEET_SOURCES: tuple[tuple[str, str | None, str], ...] = (
    ("dxf", "平面設計圖", "plan"),
    ("profile_dxf", None, "profile"),
    ("profile_outlet_dxf", None, "profile"),
    ("detail_dxf", None, "detail"),
    ("dxf", "集水區分析圖", "catchment"),
)


@dataclass(frozen=True)
class SheetInfo:
    key: str  # artifact key
    layout: str | None
    produced_by: str
    dwg_no: str
    title: str
    scale: str
    dxf: Path


@dataclass(frozen=True)
class Figure:
    path: Path
    sheet_name: str  # 統一圖名
    caption: str
    dwg_no: str | None = None
    scale: str | None = None


def read_title_block(dxf: str | Path, layout: str | None) -> dict[str, str] | None:
    import ezdxf

    doc = ezdxf.readfile(str(dxf))
    lay = doc.modelspace() if layout is None else doc.layouts.get(layout)
    for ins in lay.query("INSERT"):
        if ins.attribs and ins.dxf.name.upper().startswith("TTLB"):
            return {a.dxf.tag.upper(): a.dxf.text for a in ins.attribs}
    return None


def sheet_infos(artifacts: dict[str, Path]) -> list[SheetInfo]:
    out = []
    for key, layout, by in SHEET_SOURCES:
        p = artifacts.get(key)
        if p is None or not Path(p).exists():
            continue
        tb = read_title_block(p, layout)
        if tb is None:
            raise ValueError(f"{Path(p).name}{f'［{layout}］' if layout else ''} 找不到圖簽")
        out.append(
            SheetInfo(
                key=key,
                layout=layout,
                produced_by=by,
                dwg_no=tb.get("DWG_NO", "").strip(),
                title=tb.get("TITLE", "").strip(),
                scale=tb.get("SCALE", "").strip(),
                dxf=Path(p),
            )
        )
    return out


def render_figures(
    sheets: list[SheetInfo], photos: list, out_dir: Path, sheet_order: list[str]
) -> list[Figure]:
    """依統一圖名順序（sheet_order = vocab 的 produced_by 序）產生附圖檔。"""
    import logging

    from draftagent.render.preview import render_png

    logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)
    d = Path(out_dir) / FIG_DIR
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    figs: list[Figure] = []
    n = 0
    for by in sheet_order:
        if by == "photos":
            for ph in photos:
                src = Path(ph.path)
                if not src.exists():
                    raise FileNotFoundError(f"照片不存在：{src}")
                n += 1
                dst = d / f"fig-{n:02d}{src.suffix.lower()}"
                shutil.copy2(src, dst)
                figs.append(Figure(dst, "基地現況照片", ph.caption))
            continue
        for s in [s for s in sheets if s.produced_by == by]:
            n += 1
            png = d / f"fig-{n:02d}.png"
            render_png(s.dxf, png, dpi=PNG_DPI, max_px=PNG_MAX_PX, layout=s.layout)
            figs.append(Figure(png, s.title, "", s.dwg_no, s.scale))
    return figs
