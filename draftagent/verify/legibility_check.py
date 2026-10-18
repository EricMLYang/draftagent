"""出圖可讀性檢查：印出字高、文字重疊 / 壓線（所有 DXF 圖種共用）。純函式，不得呼叫 LLM。

公開 API（其他 verifier 直接呼叫；語意穩定，擴充只加 keyword 參數）
------------------------------------------------------------------
- `check_text_heights(doc, layout_name=None, min_mm=2.0, *, model_scale=None) -> list[str]`
  回傳違規訊息（空 list = 通過）。印出字高換算：
  - paperspace 實體：圖紙 mm，直接用字高
  - modelspace 實體：
    * `layout_name` 為 paperspace layout → 只算該 layout 的 viewport 看得到的文字
      （插入點在 viewport 視窗內、圖層未在該 viewport 凍結），印出高 = 字高 × viewport 比例
      （`vp.height / vp.view_height`，圖紙 mm / model 單位）
    * `layout_name="Model"` → 印出高 = 字高 / `model_scale`（`model_scale` = 出圖比例分母，
      例如平面圖 1/50 → 50；None = 1，即 modelspace 本身就是圖紙 mm，如縱斷面 / 詳圖）
    * `layout_name=None` → 所有 paperspace layout 逐一檢查；modelspace 若有任何 viewport 顯示
      就只經由 viewport 換算（不在任何 viewport 內的 model 文字不出圖，不檢查），
      否則（純 modelspace 圖）用 `model_scale`。`model_scale` 有給時 modelspace 另以它直接檢查。
  - 文字種類：TEXT、MTEXT、ATTRIB、INSERT 內（含巢狀）的 TEXT / MTEXT（乘上插入比例）、
    DIMENSION（`dimtxt × dimscale`，dimscale = 0 視為 1）
  - 圖層關閉 / 凍結 / 不出圖的文字不檢查（印不出來；圖層可見性由其他檢查把關）
- `check_text_overlaps(doc, layout_name="Model", frozen_layers=(), overlap_ratio=0.10, *,
  line_layers=(), ignore_layers=()) -> tuple[list[str], list[str]]`
  回傳 (errors, warnings)。以 `ezdxf.bbox.extents(fast=False)` 取文字框（DIMENSION 取 geometry
  block 內的文字），一律換算到該 layout 的圖紙座標後兩兩比對：
  - 交集面積 > 較小框面積 × `overlap_ratio` → error
  - 文字框與 `line_layers` 上的線（LINE / LWPOLYLINE / POLYLINE / ARC / CIRCLE / SPLINE）相交
    → warning
  - paperspace layout：每個 viewport 只放入該 viewport 可見的 model 文字 / 線（扣除
    `vp.frozen_layers`），同一 model 文字在兩個 viewport 出現就是兩個框；在不同 viewport 凍結、
    實際出圖不會同時出現的兩個 model 文字因此不會被比對。
  - `frozen_layers`：額外視為不可見的圖層（例如在 modelspace 模擬某個 viewport 的凍結表）
  - `ignore_layers`：不參與比對的文字圖層
- `check_legibility(path_or_doc, *, model_scale=None, min_mm=2.0, line_layers=(), layouts=None)
  -> VerifyResult`：上面兩者包成 verifier（CLI 用）

CLI：python -m draftagent.verify.legibility_check <dxf> [--model-scale 50] [--min-mm 2.0]
     [--line-layer A-WALL ...] [--json]
"""

from __future__ import annotations

import argparse
import math
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

import ezdxf
from ezdxf import bbox as ezbbox
from ezdxf.document import Drawing
from shapely.geometry import LineString, Point, Polygon, box
from shapely.strtree import STRtree

from draftagent.verify.base import VerifyResult, emit

NAME = "legibility_check"
MODEL = "Model"
TEXT_TYPES = ("TEXT", "MTEXT", "ATTRIB")
LINE_TYPES = ("LINE", "LWPOLYLINE", "POLYLINE", "ARC", "CIRCLE", "SPLINE", "ELLIPSE")


@dataclass(frozen=True)
class TextItem:
    handle: str
    kind: str  # TEXT / MTEXT / ATTRIB / DIMENSION / INSERT>TEXT …
    layer: str
    text: str
    height: float  # 所在空間單位（model 或圖紙 mm）
    bbox: tuple[float, float, float, float] | None  # 所在空間座標


# ---------------------------------------------------------------- 小工具


def _layer_visible(doc: Drawing, name: str) -> bool:
    if name not in doc.layers:
        return True
    lay = doc.layers.get(name)
    return lay.is_on() and not lay.is_frozen() and bool(lay.dxf.get("plot", 1))


def _plain_text(e) -> str:
    t = e.dxftype()
    if t == "MTEXT":
        return e.plain_text().strip()
    return str(e.dxf.get("text", "")).strip()


def _extents(e) -> tuple[float, float, float, float] | None:
    try:
        ext = ezbbox.extents([e], fast=False)
    except Exception:  # noqa: BLE001 - 字型 / 實體無法量測
        return None
    if not ext.has_data:
        return None
    return (ext.extmin.x, ext.extmin.y, ext.extmax.x, ext.extmax.y)


def _dim_height(dim) -> float:
    ov = dim.override()
    txt = float(ov.get("dimtxt", 2.5) or 0.0)
    scale = float(ov.get("dimscale", 1.0) or 0.0)
    return txt * (scale if scale > 0 else 1.0)


def _text_height(e) -> float:
    if e.dxftype() == "MTEXT":
        return float(e.dxf.get("char_height", 0.0) or 0.0)
    return float(e.dxf.get("height", 0.0) or 0.0)


def _iter_texts(
    entities: Iterable, parent_layer: str | None = None, depth: int = 0
) -> Iterator[TextItem]:
    """layout 實體 → TextItem（INSERT 展開、DIMENSION 取 geometry block 文字框）。"""
    for e in entities:
        t = e.dxftype()
        layer = e.dxf.get("layer", "0")
        if parent_layer is not None and layer == "0":
            layer = parent_layer  # block 內 0 層實體繼承 INSERT 圖層
        if t in TEXT_TYPES:
            s = _plain_text(e)
            if not s:
                continue
            kind = t if depth == 0 else f"INSERT>{t}"
            yield TextItem(e.dxf.handle or "", kind, layer, s, _text_height(e), _extents(e))
        elif t == "DIMENSION":
            boxes = []
            label = ""
            try:
                for v in e.virtual_entities():
                    if v.dxftype() in ("TEXT", "MTEXT"):
                        label = label or _plain_text(v)
                        b = _extents(v)
                        if b:
                            boxes.append(b)
            except Exception:  # noqa: BLE001 - 沒有 geometry block
                pass
            bb = None
            if boxes:
                bb = (
                    min(b[0] for b in boxes),
                    min(b[1] for b in boxes),
                    max(b[2] for b in boxes),
                    max(b[3] for b in boxes),
                )
            yield TextItem(e.dxf.handle or "", "DIMENSION", layer, label, _dim_height(e), bb)
        elif t == "INSERT" and depth < 8:
            for a in e.attribs:
                if a.dxf.get("invisible", 0) & 1 or getattr(a, "is_invisible", False):
                    continue
                s = _plain_text(a)
                if s:
                    alay = a.dxf.get("layer", "0")
                    yield TextItem(
                        a.dxf.handle or "",
                        "ATTRIB",
                        layer if alay == "0" else alay,
                        s,
                        _text_height(a),
                        _extents(a),
                    )
            try:
                virt = [v for v in e.virtual_entities() if v.dxftype() != "ATTDEF"]
            except Exception:  # noqa: BLE001 - 非等比縮放等無法展開
                continue
            for item in _iter_texts(virt, layer, depth + 1):
                yield TextItem(
                    e.dxf.handle or "", item.kind, item.layer, item.text, item.height, item.bbox
                )


def _viewports(layout) -> list:
    """layout 內使用者 viewport（排除 paperspace 主視埠 id=1 與尺寸為 0 者）。"""
    out = []
    for e in layout.query("VIEWPORT"):
        if e.dxf.get("id", 0) == 1 or e.dxf.get("status", 1) == 0:
            continue
        if float(e.dxf.get("view_height", 0) or 0) <= 0 or float(e.dxf.get("height", 0) or 0) <= 0:
            continue
        out.append(e)
    return out


class _VpXform:
    """model → 圖紙座標（含 view_twist_angle）。"""

    def __init__(self, vp):
        self.scale = float(vp.dxf.height) / float(vp.dxf.view_height)
        self.cx, self.cy = vp.dxf.center.x, vp.dxf.center.y
        vc = vp.dxf.get("view_center_point", (0, 0))
        self.vx, self.vy = float(vc[0]), float(vc[1])
        tw = math.radians(float(vp.dxf.get("view_twist_angle", 0.0) or 0.0))
        self.cos, self.sin = math.cos(tw), math.sin(tw)
        w, h = float(vp.dxf.width) / 2, float(vp.dxf.height) / 2
        self.paper = box(self.cx - w, self.cy - h, self.cx + w, self.cy + h)
        self.frozen = {n.upper() for n in (vp.frozen_layers or [])}

    def pt(self, x: float, y: float) -> tuple[float, float]:
        dx, dy = (x - self.vx) * self.scale, (y - self.vy) * self.scale
        return (self.cx + dx * self.cos - dy * self.sin, self.cy + dx * self.sin + dy * self.cos)

    def bbox(self, b) -> Polygon:
        x0, y0, x1, y1 = b
        return Polygon([self.pt(x0, y0), self.pt(x1, y0), self.pt(x1, y1), self.pt(x0, y1)])

    def contains_model_point(self, x: float, y: float) -> bool:
        px, py = self.pt(x, y)
        return self.paper.buffer(1e-6).contains(Point(px, py))


def _center(b) -> tuple[float, float]:
    return ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)


def _layout(doc: Drawing, name: str):
    if name == MODEL or name.lower() == "model":
        return doc.modelspace()
    if name not in doc.layouts.names():
        raise KeyError(f"layout {name!r} 不存在（有：{doc.layouts.names()}）")
    return doc.layouts.get(name)


def _paper_layouts(doc: Drawing) -> list:
    return [lay for lay in doc.layouts if lay.name != MODEL and not lay.is_modelspace]


def _short(s: str, n: int = 20) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


# ---------------------------------------------------------------- 印出字高


def _height_msg(where: str, it: TextItem, printed: float, min_mm: float) -> str:
    return (
        f"{where}：{it.kind} {it.handle}（{it.layer}「{_short(it.text)}」）印出字高 "
        f"{printed:.2f} mm < {min_mm:g} mm"
    )


def _check_model_direct(doc, min_mm: float, model_scale: float | None) -> list[str]:
    denom = float(model_scale) if model_scale else 1.0
    out = []
    for it in _iter_texts(doc.modelspace()):
        if not _layer_visible(doc, it.layer):
            continue
        printed = it.height / denom
        if printed < min_mm - 1e-9:
            where = f"Model（1/{denom:g}）" if model_scale else "Model（1:1 圖紙 mm）"
            out.append(_height_msg(where, it, printed, min_mm))
    return out


def _check_paper(doc, layout, min_mm: float) -> list[str]:
    out = []
    for it in _iter_texts(e for e in layout if e.dxftype() != "VIEWPORT"):
        if not _layer_visible(doc, it.layer):
            continue
        if it.height < min_mm - 1e-9:
            out.append(_height_msg(layout.name, it, it.height, min_mm))
    vps = _viewports(layout)
    if not vps:
        return out
    model_items = [it for it in _iter_texts(doc.modelspace()) if _layer_visible(doc, it.layer)]
    for vp in vps:
        xf = _VpXform(vp)
        for it in model_items:
            if it.layer.upper() in xf.frozen:
                continue
            ref = _center(it.bbox) if it.bbox else None
            if ref is None or not xf.contains_model_point(*ref):
                continue
            printed = it.height * xf.scale
            if printed < min_mm - 1e-9:
                where = f"{layout.name} viewport {vp.dxf.handle}（比例 {xf.scale:g} 圖紙 mm / model 單位）"
                out.append(_height_msg(where, it, printed, min_mm))
    return out


def check_text_heights(
    doc: Drawing,
    layout_name: str | None = None,
    min_mm: float = 2.0,
    *,
    model_scale: float | None = None,
) -> list[str]:
    """印出字高 < min_mm 的文字清單（訊息）。見模組 docstring。"""
    if layout_name is not None:
        lay = _layout(doc, layout_name)
        if lay.is_modelspace:
            return _check_model_direct(doc, min_mm, model_scale)
        return _check_paper(doc, lay, min_mm)
    out: list[str] = []
    any_vp = False
    for lay in _paper_layouts(doc):
        any_vp = any_vp or bool(_viewports(lay))
        out += _check_paper(doc, lay, min_mm)
    if not any_vp or model_scale:
        out += _check_model_direct(doc, min_mm, model_scale)
    return list(dict.fromkeys(out))


# ---------------------------------------------------------------- 重疊 / 壓線


@dataclass
class _Box:
    poly: Polygon
    it: TextItem
    where: str


def _line_geoms(
    entities, skip_layers: set[str], line_layers: set[str]
) -> Iterator[tuple[str, object]]:
    for e in entities:
        lay = e.dxf.get("layer", "0").upper()
        if lay not in line_layers or lay in skip_layers or e.dxftype() not in LINE_TYPES:
            continue
        try:
            if e.dxftype() == "LINE":
                pts = [(e.dxf.start.x, e.dxf.start.y), (e.dxf.end.x, e.dxf.end.y)]
            elif e.dxftype() == "LWPOLYLINE":
                pts = [(p[0], p[1]) for p in e.get_points("xy")]
                if e.closed and pts:
                    pts.append(pts[0])
            else:
                from ezdxf import path as ezpath

                pts = [(v.x, v.y) for v in ezpath.make_path(e).flattening(1.0)]
        except Exception:  # noqa: BLE001
            continue
        if len(pts) >= 2:
            yield e.dxf.handle or "", LineString(pts)


def check_text_overlaps(
    doc: Drawing,
    layout_name: str = MODEL,
    frozen_layers: Iterable[str] = (),
    overlap_ratio: float = 0.10,
    *,
    line_layers: Iterable[str] = (),
    ignore_layers: Iterable[str] = (),
) -> tuple[list[str], list[str]]:
    """文字框兩兩交集 > 小框 × overlap_ratio → error；文字壓到 line_layers 的線 → warning。"""
    from shapely import affinity
    from shapely.ops import transform

    lay = _layout(doc, layout_name)
    frozen = {n.upper() for n in frozen_layers}
    ignore = {n.upper() for n in ignore_layers}
    lines_wanted = {n.upper() for n in line_layers}

    def visible(layer: str, extra: set[str]) -> bool:
        u = layer.upper()
        return u not in frozen and u not in extra and u not in ignore and _layer_visible(doc, layer)

    boxes: list[_Box] = []
    lines: list[tuple[str, str, object]] = []  # (where, handle, geom)
    own = [e for e in lay if e.dxftype() != "VIEWPORT"]
    for it in _iter_texts(own):
        if it.bbox and visible(it.layer, set()):
            boxes.append(_Box(box(*it.bbox), it, lay.name))
    for h, g in _line_geoms(own, frozen, lines_wanted):
        lines.append((lay.name, h, g))

    if not lay.is_modelspace:
        msp_items = list(_iter_texts(doc.modelspace()))
        msp_ents = list(doc.modelspace())
        for vp in _viewports(lay):
            xf = _VpXform(vp)
            where = f"{lay.name} viewport {vp.dxf.handle}"
            for it in msp_items:
                if not it.bbox or not visible(it.layer, xf.frozen):
                    continue
                if not xf.contains_model_point(*_center(it.bbox)):
                    continue
                boxes.append(_Box(xf.bbox(it.bbox), it, where))
            for h, g in _line_geoms(msp_ents, frozen | xf.frozen, lines_wanted):
                pg = transform(lambda x, y, z=None, _xf=xf: _xf.pt(x, y), g)
                pg = pg.intersection(xf.paper)
                if not pg.is_empty:
                    lines.append((where, h, pg))

    errors: list[str] = []
    warnings: list[str] = []
    polys = [b.poly for b in boxes]
    tree = STRtree(polys) if polys else None
    seen: set[tuple[int, int]] = set()
    for i, b in enumerate(boxes):
        if tree is None:
            break
        for j in tree.query(b.poly):
            j = int(j)
            if j <= i or (i, j) in seen:
                continue
            seen.add((i, j))
            o = boxes[j]
            if b.it.handle == o.it.handle and b.where == o.where:
                continue  # 同一實體（例如 INSERT 內多段文字）
            inter = b.poly.intersection(o.poly).area
            small = min(b.poly.area, o.poly.area)
            if small <= 0 or inter <= small * overlap_ratio:
                continue
            errors.append(
                f"{b.where}：文字重疊 {inter / small:.0%}（> {overlap_ratio:.0%}）："
                f"{b.it.kind} {b.it.handle}（{b.it.layer}「{_short(b.it.text)}」）× "
                f"{o.it.kind} {o.it.handle}（{o.it.layer}「{_short(o.it.text)}」）"
            )
    if lines:
        for b in boxes:
            core = affinity.scale(b.poly, 0.9, 0.9)  # 內縮 5%：只算真的壓到字身
            for _where, h, g in lines:  # 全部已在同一 layout 的圖紙座標
                if core.intersects(g):
                    warnings.append(
                        f"{b.where}：文字壓線：{b.it.kind} {b.it.handle}（{b.it.layer}"
                        f"「{_short(b.it.text)}」）與線 {h} 相交"
                    )
                    break
    return errors, warnings


# ---------------------------------------------------------------- verifier / CLI


def check_legibility(
    path_or_doc: str | Path | Drawing,
    *,
    model_scale: float | None = None,
    min_mm: float = 2.0,
    line_layers: Iterable[str] = (),
    layouts: Iterable[str] | None = None,
    overlap_ratio: float = 0.10,
) -> VerifyResult:
    res = VerifyResult(name=NAME)
    if isinstance(path_or_doc, Drawing):
        doc = path_or_doc
    else:
        try:
            doc = ezdxf.readfile(str(path_or_doc))
        except (OSError, ezdxf.DXFStructureError) as e:
            res.error(f"無法讀取 DXF：{e}")
            return res
    for m in check_text_heights(doc, None, min_mm, model_scale=model_scale):
        res.error(m)
    names = (
        list(layouts)
        if layouts is not None
        else ([lay.name for lay in _paper_layouts(doc) if _viewports(lay) or len(lay)] or [MODEL])
    )
    if MODEL not in names and not any(_viewports(_layout(doc, n)) for n in names if n != MODEL):
        names.append(MODEL)
    for n in names:
        errs, warns = check_text_overlaps(
            doc, n, overlap_ratio=overlap_ratio, line_layers=line_layers
        )
        for e in errs:
            res.error(e)
        for w in warns:
            res.warn(w)
    res.metrics["min_print_height_mm"] = min_mm
    res.metrics["model_scale"] = model_scale
    res.metrics["layouts"] = names
    return res


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        prog="python -m draftagent.verify.legibility_check",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("dxf")
    ap.add_argument(
        "--model-scale", type=float, default=None, help="modelspace 出圖比例分母（1/50 → 50）"
    )
    ap.add_argument("--min-mm", type=float, default=2.0)
    ap.add_argument("--line-layer", action="append", default=[], help="文字不應壓到的線圖層")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    emit(
        check_legibility(
            a.dxf, model_scale=a.model_scale, min_mm=a.min_mm, line_layers=a.line_layer
        ),
        a.json,
    )


if __name__ == "__main__":
    main()
