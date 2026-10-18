"""圖框 / 圖簽 / 比例尺 / 指北針（ezdxf）。純函式、確定性，不得呼叫 LLM。

全部畫在 modelspace，座標 = 圖紙 mm（1:1 出圖），原點為紙張左下角。
圖層：X-FRAME（外框、內框、比例尺、指北針）、X-TTLB（圖簽）。

圖簽為 block `TTLB-<size>`，屬性 tag：PROJECT、TITLE、DWG_NO、SCALE、DATE、DESIGNER、CHECKER。
CJK 文字一律用 TrueType 文字樣式 `CJK_STYLE`（字型檔 `CJK_FONT`）；接收端沒有該字型時
CAD 會自行替換，預覽（render/preview.py）會改用本機可用的 CJK 字型。
"""

from __future__ import annotations

import ezdxf
from ezdxf import units
from ezdxf.document import Drawing
from ezdxf.enums import TextEntityAlignment

from draftagent.render.standards import CJK_STYLE, DEFAULT_CJK_FONT, apply_layers, text_style
from draftagent.schema.sheet import SheetSpec

# 向後相容：樣式名 / 預設字型檔 / 圖層名稱。屬性（顏色、線寬…）以 render/codes/layers.yaml 為準；
# 實際寫入 DXF 的字型可由環境變數 DRAFTAGENT_CAD_FONT 覆寫（見 render/standards.py）。
CJK_FONT = DEFAULT_CJK_FONT
SHEET_LAYERS: dict[str, int] = {"X-FRAME": 8, "X-TTLB": 7}
ATTR_TAGS = ("PROJECT", "TITLE", "DWG_NO", "SCALE", "DATE", "DESIGNER", "CHECKER")
REQUIRED_ATTR_TAGS = ("PROJECT", "TITLE", "DWG_NO", "SCALE")
NORTH_BLOCK = "NORTH-ARROW"
LW_THIN, LW_MED, LW_THICK = 18, 35, 70  # 1/100 mm

# 圖簽尺寸（A3 基準，mm）；A1 依 _TB_FACTOR 放大
_TB_W, _TB_H, _TB_ROW = 170.0, 40.0, 10.0
_TB_FACTOR = {"A3": 1.0, "A1": 1.4}

Align = TextEntityAlignment


def new_sheet_doc() -> Drawing:
    """建立 mm 圖面：$INSUNITS=4、$LWDISPLAY=1、CJK TrueType 樣式、X-FRAME / X-TTLB 圖層。

    圖層屬性依 render/codes/layers.yaml（standards.apply_layers）。
    """
    doc = ezdxf.new("R2010", setup=True)
    doc.units = units.MM
    doc.header["$MEASUREMENT"] = 1
    doc.header["$LUNITS"] = 2
    text_style(doc)
    apply_layers(doc, SHEET_LAYERS)
    return doc


def est_text_width(text: str, height: float) -> float:
    """粗估單行文字寬 mm（偏保守）。用於排版避讓，非精確量測。

    TEXT 高度 = 大寫字高，TrueType 字身比它大：實測 Hiragino Sans GB 預覽 CJK ≈ 1.3 h、
    半形 ≈ 0.75 h，取 1.4 / 0.8 留餘裕。
    """
    return sum(1.4 if ord(c) >= 0x2E80 else 0.8 for c in text) * height


def add_text(
    layout,
    text: str,
    pos: tuple[float, float],
    height: float,
    layer: str,
    align: TextEntityAlignment = Align.MIDDLE_CENTER,
    rotation: float = 0.0,
    max_width: float | None = None,
):
    """CJK 樣式 TEXT。給 max_width 時以寬度因子壓縮（下限 0.5）塞進格子。"""
    attribs = {"layer": layer, "style": CJK_STYLE, "rotation": rotation}
    if max_width is not None:
        w = est_text_width(text, height)
        if w > max_width:
            attribs["width"] = max(0.5, max_width / w)
    return layout.add_text(text, height=height, dxfattribs=attribs).set_placement(pos, align=align)


def _rect(layout, x0, y0, x1, y1, layer, lw=LW_THIN):
    return layout.add_lwpolyline(
        [(x0, y0), (x1, y0), (x1, y1), (x0, y1)],
        close=True,
        dxfattribs={"layer": layer, "lineweight": lw},
    )


# ---------------------------------------------------------------- frame


def draw_frame(doc: Drawing, sheet: SheetSpec, layout=None) -> tuple[float, float, float, float]:
    """外框（紙邊，細線）+ 內框（粗線）。回傳內框 bbox。layout 省略 = modelspace。"""
    msp = layout if layout is not None else doc.modelspace()
    w, h = sheet.paper()
    _rect(msp, 0, 0, w, h, "X-FRAME", LW_THIN)
    x0, y0, x1, y1 = sheet.inner()
    _rect(msp, x0, y0, x1, y1, "X-FRAME", LW_THICK)
    return x0, y0, x1, y1


def title_block_size(sheet: SheetSpec) -> tuple[float, float]:
    f = _TB_FACTOR[sheet.size]
    return _TB_W * f, _TB_H * f


def _define_title_block(doc: Drawing, sheet: SheetSpec) -> str:
    name = f"TTLB-{sheet.size}"
    if name in doc.blocks:
        return name
    f = _TB_FACTOR[sheet.size]
    blk = doc.blocks.new(name, base_point=(0, 0))
    W, H, R = _TB_W * f, _TB_H * f, _TB_ROW * f
    lay = {"layer": "X-TTLB", "lineweight": LW_THIN}
    _rect(blk, 0, 0, W, H, "X-TTLB", LW_MED)
    for k in (1, 2, 3):
        blk.add_line((0, R * k), (W, R * k), dxfattribs=lay)
    lab_h, val_h = 2.5 * f, 3.0 * f
    # 每列：[(label, x0, x1, tag, x2)]；label 格 x0..x1、值格 x1..x2
    rows = [
        (3, [("工程名稱", 0, 22, "PROJECT", 170)]),
        (2, [("圖　　名", 0, 22, "TITLE", 170)]),
        (1, [("圖　　號", 0, 22, "DWG_NO", 75), ("比　　例", 75, 97, "SCALE", 170)]),
        (
            0,
            [
                ("日期", 0, 15, "DATE", 60),
                ("設計", 60, 75, "DESIGNER", 115),
                ("校核", 115, 130, "CHECKER", 170),
            ],
        ),
    ]
    for row, cells in rows:
        ym = R * row + R / 2
        for label, a, b, tag, c in cells:
            a, b, c = a * f, b * f, c * f
            if a > 0:
                blk.add_line((a, R * row), (a, R * (row + 1)), dxfattribs=lay)
            blk.add_line((b, R * row), (b, R * (row + 1)), dxfattribs=lay)
            add_text(blk, label, ((a + b) / 2, ym), lab_h, "X-TTLB", max_width=(b - a) - 2)
            blk.add_attdef(
                tag,
                insert=(b + 2 * f, ym),
                height=val_h,
                dxfattribs={"layer": "X-TTLB", "style": CJK_STYLE},
            ).set_placement((b + 2 * f, ym), align=Align.MIDDLE_LEFT)
    return name


def draw_title_block(
    doc: Drawing, sheet: SheetSpec, *, title: str, scale_text: str, layout=None
) -> tuple[float, float, float, float]:
    """圖簽放在內框右下角。回傳圖簽 bbox。layout 省略 = modelspace。"""
    name = _define_title_block(doc, sheet)
    tb = sheet.title_block
    values = {
        "PROJECT": tb.project,
        "TITLE": tb.drawing_title or title,
        "DWG_NO": tb.drawing_no,
        "SCALE": scale_text,
        "DATE": tb.date,
        "DESIGNER": tb.designer,
        "CHECKER": tb.checker,
    }
    _, y0, x1, _ = sheet.inner()
    W, H = title_block_size(sheet)
    ins = (x1 - W, y0)
    target = layout if layout is not None else doc.modelspace()
    ref = target.add_blockref(name, ins, dxfattribs={"layer": "X-TTLB"})
    ref.add_auto_attribs(values)
    # 值格寬（A3 基準）：依 tag 壓縮過長文字
    f = _TB_FACTOR[sheet.size]
    cell_w = {
        "PROJECT": 148,
        "TITLE": 148,
        "DWG_NO": 53,
        "SCALE": 73,
        "DATE": 45,
        "DESIGNER": 40,
        "CHECKER": 40,
    }
    for att in ref.attribs:
        att.dxf.layer = "X-TTLB"
        avail = (cell_w[att.dxf.tag] - 4) * f
        w = est_text_width(att.dxf.text, att.dxf.height)
        if w > avail:
            att.dxf.width = max(0.5, avail / w)
    return ins[0], ins[1], ins[0] + W, ins[1] + H


# ---------------------------------------------------------------- scale bar / north


def _nice_length(scale: int, target_mm: float) -> float:
    """選一個「整數好讀」的實長 m，使圖上長度接近 target_mm。"""
    for m in (0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000):
        if m * 1000 / scale >= target_mm:
            return float(m)
    return 1000.0


def _fmt_m(v: float) -> str:
    return f"{v:g}"


def draw_scale_bar(
    doc: Drawing,
    origin: tuple[float, float],
    scale: int,
    *,
    label: str,
    vertical: bool = False,
    target_mm: float = 40.0,
    text_h: float = 2.5,
    layout=None,
) -> tuple[float, float, float, float]:
    """比例尺 bar：4 等分黑白相間，標 0 / 中點 / 全長 m，旁註 `label`（例：水平 1/200）。

    回傳 bbox。vertical=True 時 bar 直立（用於縱斷面垂直比例）。layout 省略 = modelspace。
    """
    msp = layout if layout is not None else doc.modelspace()
    length_m = _nice_length(scale, target_mm)
    L = length_m * 1000 / scale
    t = 2.0  # bar 厚 mm
    x, y = origin
    n = 4
    for i in range(n):
        a, b = L * i / n, L * (i + 1) / n
        if vertical:
            pts = [(x, y + a), (x + t, y + a), (x + t, y + b), (x, y + b)]
        else:
            pts = [(x + a, y), (x + b, y), (x + b, y + t), (x + a, y + t)]
        msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": "X-FRAME", "lineweight": LW_THIN})
        if i % 2 == 0:
            hatch = msp.add_hatch(color=7, dxfattribs={"layer": "X-FRAME"})
            hatch.paths.add_polyline_path(pts, is_closed=True)
    ticks = [(0.0, "0"), (L / 2, _fmt_m(length_m / 2)), (L, f"{_fmt_m(length_m)} m")]
    if vertical:
        for d, s in ticks:
            add_text(msp, s, (x + t + 1.5, y + d), text_h, "X-FRAME", Align.MIDDLE_LEFT)
        add_text(msp, label, (x + t / 2, y - 2.5), text_h, "X-FRAME", Align.TOP_CENTER)
        tw = max(est_text_width(s, text_h) for _, s in ticks)
        return x, y - 2.5 - text_h, x + t + 1.5 + max(tw, est_text_width(label, text_h)), y + L
    for d, s in ticks:
        add_text(msp, s, (x + d, y + t + 1.0), text_h, "X-FRAME", Align.BOTTOM_CENTER)
    add_text(msp, label, (x + L / 2, y - 1.5), text_h, "X-FRAME", Align.TOP_CENTER)
    return x - 2, y - 1.5 - text_h, x + L + est_text_width(ticks[-1][1], text_h), y + t + 1 + text_h


def _define_north(doc: Drawing) -> str:
    if NORTH_BLOCK in doc.blocks:
        return NORTH_BLOCK
    blk = doc.blocks.new(NORTH_BLOCK, base_point=(0, 0))
    lay = {"layer": "X-FRAME", "lineweight": LW_THIN}
    blk.add_circle((0, 0), 8, dxfattribs=lay)
    pts = [(0, 10), (-3.5, -6), (0, -3), (3.5, -6)]
    blk.add_lwpolyline(pts, close=True, dxfattribs=lay)
    hatch = blk.add_hatch(color=7, dxfattribs={"layer": "X-FRAME"})
    hatch.paths.add_polyline_path([(0, 10), (0, -3), (3.5, -6)], is_closed=True)
    add_text(blk, "N", (0, 11), 4.0, "X-FRAME", Align.BOTTOM_CENTER)
    return NORTH_BLOCK


def draw_north_arrow(
    doc: Drawing, pos: tuple[float, float], rotation: float = 0.0, layout=None
) -> None:
    """指北針 block。rotation：圖面北向相對 +y 的逆時針角度。layout 省略 = modelspace。"""
    (layout if layout is not None else doc.modelspace()).add_blockref(
        _define_north(doc), pos, dxfattribs={"layer": "X-FRAME", "rotation": rotation}
    )
