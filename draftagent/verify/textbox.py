"""與平台無關的文字框估算（verifier 共用）。純函式，不得呼叫 LLM。

為什麼不用 `ezdxf.bbox.extents(fast=False)`：它依「本機安裝的字型」實際量字寬，同一張 DXF 在
macOS / Linux / Windows 量出不同的文字框（CI 上 Linux 的縱斷面標示因此偏 8–10 mm 而紅燈）。
verifier 必須跨平台確定，所以一律用 renderer 排版時同一套估算（`render.sheet.est_text_width`）。
"""

from __future__ import annotations

import math

from draftagent.render.sheet import est_text_width

LINE_SPACING = 1.4  # MTEXT 行距（× 字高），與 renderer 慣例一致

# TEXT halign（0 左 1 中 2 右 3 aligned 4 middle 5 fit）→ 水平錨點比例
_HALIGN = {0: 0.0, 1: 0.5, 2: 1.0, 3: 0.0, 4: 0.5, 5: 0.0}
# TEXT valign（0 baseline 1 bottom 2 middle 3 top）→ 垂直錨點比例（0 = 字底）
_VALIGN = {0: 0.0, 1: 0.0, 2: 0.5, 3: 1.0}
# MTEXT attachment_point 1..9（TL TC TR / ML MC MR / BL BC BR）
_MTEXT_ATTACH = {
    1: (0.0, 1.0), 2: (0.5, 1.0), 3: (1.0, 1.0),
    4: (0.0, 0.5), 5: (0.5, 0.5), 6: (1.0, 0.5),
    7: (0.0, 0.0), 8: (0.5, 0.0), 9: (1.0, 0.0),
}  # fmt: skip

Box = tuple[float, float, float, float]


def _rotated_box(x: float, y: float, w: float, h: float, fx: float, fy: float, deg: float) -> Box:
    """錨點 (x, y)、寬高 w×h、錨點在框內的比例 (fx, fy)、旋轉 deg → 軸向外接框。"""
    corners = [(-fx * w, -fy * h), ((1 - fx) * w, -fy * h), ((1 - fx) * w, (1 - fy) * h), (-fx * w, (1 - fy) * h)]
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    pts = [(x + dx * c - dy * s, y + dx * s + dy * c) for dx, dy in corners]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def est_text_box(e) -> Box | None:
    """TEXT / ATTRIB / MTEXT 的估算外接框（圖面座標）；其他實體回傳 None。"""
    t = e.dxftype()
    if t in ("TEXT", "ATTRIB", "ATTDEF"):
        text = str(e.dxf.get("text", ""))
        h = float(e.dxf.get("height", 0.0) or 0.0)
        if not text or h <= 0:
            return None
        width_factor = float(e.dxf.get("width", 1.0) or 1.0)
        w = est_text_width(text, h) * width_factor
        ha, va = int(e.dxf.get("halign", 0)), int(e.dxf.get("valign", 0))
        anchor = e.dxf.align_point if (ha or va) and e.dxf.hasattr("align_point") else e.dxf.insert
        return _rotated_box(
            anchor.x, anchor.y, w, h, _HALIGN.get(ha, 0.0), _VALIGN.get(va, 0.0),
            float(e.dxf.get("rotation", 0.0) or 0.0),
        )  # fmt: skip
    if t == "MTEXT":
        lines = [ln for ln in e.plain_text().split("\n") if ln.strip()]
        h = float(e.dxf.get("char_height", 0.0) or 0.0)
        if not lines or h <= 0:
            return None
        w = max(est_text_width(ln, h) for ln in lines)
        total_h = h + (len(lines) - 1) * h * LINE_SPACING
        fx, fy = _MTEXT_ATTACH.get(int(e.dxf.get("attachment_point", 1)), (0.0, 1.0))
        ins = e.dxf.insert
        return _rotated_box(ins.x, ins.y, w, total_h, fx, fy, float(e.get_rotation()))
    return None
