"""產生「雜亂建築師 DXF」的【合成】fixture（工作台待辦 #94，契約見 draftagent/arch/__init__.py）。

    python tools/make_fixtures_archdxf.py            # 重寫 tests/fixtures/archdxf/<case>/{drawing.dxf,truth.json}
    python tools/make_fixtures_archdxf.py --check    # 只比對：與磁碟上檔案不同 → exit 1

每個 case 一份 drawing.dxf（模擬建築師給的平面圖，各有一種陷阱）與 truth.json（真值）。
確定性：固定亂數種子、固定 ezdxf meta、header 時間戳 / GUID 改寫成常數；--check 比對時再忽略這些欄位。
基地座標為 TWD97 TM2（EPSG:3826）範圍內的合成位置，不含任何真實案名、地號、公司名。
"""

from __future__ import annotations

import argparse
import io
import json
import math
import random
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import ezdxf
from ezdxf.enums import TextEntityAlignment
from shapely.geometry import LineString, Point, Polygon

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "tests" / "fixtures" / "archdxf"

ezdxf.options.write_fixed_meta_data_for_testing = True

Pt = tuple[float, float]

# --------------------------------------------------------------------------- 幾何工具


def ccw(poly: list[Pt]) -> list[Pt]:
    a = sum(
        x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1], strict=False)
    )
    return poly if a > 0 else poly[::-1]


def shift(poly: list[Pt], dx: float, dy: float) -> list[Pt]:
    return [(x + dx, y + dy) for x, y in poly]


def offset_ortho(poly: list[Pt], d: float) -> list[Pt]:
    """CCW 正交多邊形往內偏移 d（左法向）。回傳頂點與原多邊形一一對應。"""
    n = len(poly)
    lines = []
    for i in range(n):
        (x1, y1), (x2, y2) = poly[i], poly[(i + 1) % n]
        L = math.hypot(x2 - x1, y2 - y1)
        nx, ny = -(y2 - y1) / L, (x2 - x1) / L
        lines.append(((x1 + nx * d, y1 + ny * d), (x2 + nx * d, y2 + ny * d)))
    out = []
    for i in range(n):
        (p1, p2), (p3, p4) = lines[i - 1], lines[i]
        out.append(_intersect(p1, p2, p3, p4))
    return out


def _intersect(p1: Pt, p2: Pt, p3: Pt, p4: Pt) -> Pt:
    x1, y1 = p1
    x2, y2 = p2
    x3, y3 = p3
    x4, y4 = p4
    den = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    a = x1 * y2 - y1 * x2
    b = x3 * y4 - y3 * x4
    return ((a * (x3 - x4) - (x1 - x2) * b) / den, (a * (y3 - y4) - (y1 - y2) * b) / den)


class Sim:
    """相似變換 drawing → TWD97：twd = s·R(θ)·p + t。"""

    def __init__(self, s: float, theta_deg: float, tx: float, ty: float):
        self.s, self.th, self.tx, self.ty = s, math.radians(theta_deg), tx, ty

    def __call__(self, p: Pt) -> list[float]:
        c, sn = math.cos(self.th), math.sin(self.th)
        x, y = p
        return [
            round(self.s * (c * x - sn * y) + self.tx, 4),
            round(self.s * (sn * x + c * y) + self.ty, 4),
        ]

    def poly(self, pts: list[Pt]) -> list[list[float]]:
        return [self(p) for p in pts]


def rot(p: Pt, deg: float) -> Pt:
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    return (c * p[0] - s * p[1], s * p[0] + c * p[1])


def to_block_local(p: Pt, ins: Pt, deg: float, scale: float) -> Pt:
    q = rot((p[0] - ins[0], p[1] - ins[1]), -deg)
    return (q[0] / scale, q[1] / scale)


# --------------------------------------------------------------------------- 外框形狀（mm，CCW，局部座標）

SHAPE_L = [
    (0, 0),
    (32000, 0),
    (32000, 12000),
    (26000, 12000),
    (26000, 18000),
    (6000, 18000),
    (6000, 14000),
    (0, 14000),
]
SHAPE_U = [
    (0, 0),
    (36000, 0),
    (36000, 20000),
    (25000, 20000),
    (25000, 13000),
    (11000, 13000),
    (11000, 20000),
    (0, 20000),
]
SHAPE_STEP = [
    (0, 0),
    (24000, 0),
    (24000, 3000),
    (28000, 3000),
    (28000, 16000),
    (4000, 16000),
    (4000, 12500),
    (0, 12500),
]
SHAPE_T = [
    (0, 0),
    (22000, 0),
    (22000, 9000),
    (30000, 9000),
    (30000, 19000),
    (-6000, 19000),
    (-6000, 9000),
    (0, 9000),
]


def site_around(
    fp: list[Pt], front: float = 6000, side: float = 3500, back: float = 4000, skew: float = 2500
) -> list[Pt]:
    xs = [p[0] for p in fp]
    ys = [p[1] for p in fp]
    x0, x1, y0, y1 = min(xs) - side, max(xs) + side, min(ys) - front, max(ys) + back
    # 非矩形基地：右後角斜切
    return [(x0, y0), (x1, y0), (x1, y1 - skew), (x1 - skew * 2, y1), (x0, y1)]


# --------------------------------------------------------------------------- DXF 雜項繪製


def new_doc(insunits: int) -> ezdxf.document.Drawing:
    doc = ezdxf.new("R2018", setup=True)
    doc.header["$INSUNITS"] = insunits
    doc.header["$MEASUREMENT"] = 1
    return doc


def ensure_layer(doc, name: str, color: int = 7, lw: int | None = None) -> None:
    if name not in doc.layers:
        attrs: dict[str, Any] = {"color": color}
        if lw is not None:
            attrs["lineweight"] = lw
        doc.layers.add(name, **attrs)


def define_furniture(doc) -> None:
    if "FN_BED" in doc.blocks:
        return
    b = doc.blocks.new("FN_BED")
    b.add_lwpolyline([(0, 0), (1500, 0), (1500, 2000), (0, 2000)], close=True)
    b.add_lwpolyline([(100, 1550), (700, 1550), (700, 1900), (100, 1900)], close=True)
    b.add_lwpolyline([(800, 1550), (1400, 1550), (1400, 1900), (800, 1900)], close=True)
    b.add_line((0, 1400), (1500, 1400))
    b = doc.blocks.new("FN_SOFA")
    b.add_lwpolyline([(0, 0), (2100, 0), (2100, 850), (0, 850)], close=True)
    b.add_lwpolyline([(150, 150), (1950, 150), (1950, 700), (150, 700)], close=True)
    for x in (750, 1350):
        b.add_line((x, 150), (x, 700))
    b = doc.blocks.new("FN_TABLE")
    b.add_lwpolyline([(0, 0), (1600, 0), (1600, 900), (0, 900)], close=True)
    for cx, cy in ((400, -250), (1200, -250), (400, 1150), (1200, 1150)):
        b.add_circle((cx, cy), 220)
    b = doc.blocks.new("FN_WC")
    b.add_ellipse((0, 250), major_axis=(0, 260), ratio=0.65)
    b.add_lwpolyline([(-250, -150), (250, -150), (250, 0), (-250, 0)], close=True)
    b = doc.blocks.new("FN_KITCHEN")
    b.add_lwpolyline([(0, 0), (2400, 0), (2400, 600), (0, 600)], close=True)
    b.add_circle((600, 300), 180)
    b.add_circle((1050, 300), 180)
    b.add_lwpolyline([(1500, 100), (2200, 100), (2200, 500), (1500, 500)], close=True)


def furniture(doc, msp, inner: Polygon, rng: random.Random, n: int, layer: str) -> None:
    define_furniture(doc)
    names = ["FN_BED", "FN_SOFA", "FN_TABLE", "FN_WC", "FN_KITCHEN"]
    minx, miny, maxx, maxy = inner.bounds
    placed = 0
    tries = 0
    while placed < n and tries < n * 50:
        tries += 1
        p = (
            round(rng.uniform(minx + 800, maxx - 3000)),
            round(rng.uniform(miny + 800, maxy - 3000)),
        )
        if not inner.buffer(-2600).contains(Point(p)):
            continue
        msp.add_blockref(
            rng.choice(names),
            p,
            dxfattribs={"layer": layer, "rotation": rng.choice([0, 90, 180, 270])},
        )
        placed += 1


def interior_walls(
    fp: list[Pt], rng: random.Random, step: tuple[int, int] = (4200, 3600)
) -> list[tuple[Pt, Pt]]:
    """內牆中心線（裁進外框內 300 mm），隨機刪一些、留門洞。"""
    inner = Polygon(fp).buffer(-300, join_style=2)
    minx, miny, maxx, maxy = Polygon(fp).bounds
    segs: list[tuple[Pt, Pt]] = []
    x = minx + step[0]
    while x < maxx - 1000:
        segs.append(((x, miny - 10), (x, maxy + 10)))
        x += step[0] + rng.choice([-600, 0, 600])
    y = miny + step[1]
    while y < maxy - 1000:
        segs.append(((minx - 10, y), (maxx + 10, y)))
        y += step[1] + rng.choice([-300, 0, 900])
    out: list[tuple[Pt, Pt]] = []
    for a, b in segs:
        g = inner.intersection(LineString([a, b]))
        parts = getattr(g, "geoms", [g])
        for part in parts:
            if part.is_empty or part.length < 1500:
                continue
            c = list(part.coords)
            p0, p1 = c[0], c[-1]
            L = part.length
            # 門洞
            t = rng.uniform(0.15, 0.6)
            w = 900 / L
            for s0, s1 in ((0, t), (t + w, 1)):
                if rng.random() < 0.15:
                    continue
                q0 = (round(p0[0] + (p1[0] - p0[0]) * s0), round(p0[1] + (p1[1] - p0[1]) * s0))
                q1 = (round(p0[0] + (p1[0] - p0[0]) * s1), round(p0[1] + (p1[1] - p0[1]) * s1))
                out.append((q0, q1))
    return out


def draw_interior(
    msp, segs, layer: str, thick: float = 120, double: bool = True, attrs: dict | None = None
) -> None:
    attrs = {"layer": layer, **(attrs or {})}
    for (x0, y0), (x1, y1) in segs:
        if not double:
            msp.add_line((x0, y0), (x1, y1), dxfattribs=attrs)
            continue
        L = math.hypot(x1 - x0, y1 - y0)
        nx, ny = -(y1 - y0) / L * thick / 2, (x1 - x0) / L * thick / 2
        msp.add_line((x0 + nx, y0 + ny), (x1 + nx, y1 + ny), dxfattribs=attrs)
        msp.add_line((x0 - nx, y0 - ny), (x1 - nx, y1 - ny), dxfattribs=attrs)


def doors(msp, segs, rng: random.Random, layer: str) -> None:
    for (x0, y0), (x1, y1) in segs[::2]:
        L = math.hypot(x1 - x0, y1 - y0)
        ux, uy = (x1 - x0) / L, (y1 - y0) / L
        # 門扇：在段的終點後 900 洞口處
        hx, hy = x1, y1
        ang = math.degrees(math.atan2(uy, ux))
        msp.add_line((hx, hy), (hx - uy * 900, hy + ux * 900), dxfattribs={"layer": layer})
        msp.add_arc((hx, hy), 900, ang, ang + 90, dxfattribs={"layer": layer})


def room_labels(
    msp, fp: list[Pt], rng: random.Random, layer: str, n: int = 8, height: float = 300
) -> None:
    names = [
        "客廳",
        "餐廳",
        "主臥室",
        "臥室",
        "廚房",
        "浴室",
        "陽台",
        "儲藏室",
        "梯廳",
        "管委會",
        "機房",
        "玄關",
    ]
    inner = Polygon(fp).buffer(-1500)
    minx, miny, maxx, maxy = inner.bounds
    k = 0
    tries = 0
    while k < n and tries < 400:
        tries += 1
        p = (round(rng.uniform(minx, maxx)), round(rng.uniform(miny, maxy)))
        if inner.contains(Point(p)):
            nm = names[k % len(names)]
            msp.add_text(
                nm, height=height, dxfattribs={"layer": layer, "style": "OpenSans"}
            ).set_placement(p, align=TextEntityAlignment.MIDDLE_CENTER)
            area = rng.choice([8.6, 10.2, 12.4, 15.1, 18.3, 22.7])
            msp.add_text(
                f"{area:.1f} m²", height=height * 0.6, dxfattribs={"layer": layer}
            ).set_placement((p[0], p[1] - height * 1.4), align=TextEntityAlignment.MIDDLE_CENTER)
            k += 1


def grid(
    msp, fp: list[Pt], layer: str, label_layer: str, spacing: tuple[int, int] = (8000, 6000)
) -> list[Pt]:
    xs = [p[0] for p in fp]
    ys = [p[1] for p in fp]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    nodes: list[Pt] = []
    gx = [x0 + 400 + i * spacing[0] for i in range(int((x1 - x0) // spacing[0]) + 1)]
    gy = [y0 + 400 + j * spacing[1] for j in range(int((y1 - y0) // spacing[1]) + 1)]
    for i, x in enumerate(gx):
        msp.add_line(
            (x, y0 - 3000), (x, y1 + 3000), dxfattribs={"layer": layer, "linetype": "CENTER"}
        )
        msp.add_circle((x, y0 - 3600), 600, dxfattribs={"layer": label_layer})
        msp.add_text(str(i + 1), height=500, dxfattribs={"layer": label_layer}).set_placement(
            (x, y0 - 3600), align=TextEntityAlignment.MIDDLE_CENTER
        )
    for j, y in enumerate(gy):
        msp.add_line(
            (x0 - 3000, y), (x1 + 3000, y), dxfattribs={"layer": layer, "linetype": "CENTER"}
        )
        msp.add_circle((x0 - 3600, y), 600, dxfattribs={"layer": label_layer})
        msp.add_text(chr(65 + j), height=500, dxfattribs={"layer": label_layer}).set_placement(
            (x0 - 3600, y), align=TextEntityAlignment.MIDDLE_CENTER
        )
    for x in gx:
        for y in gy:
            nodes.append((x, y))
    return nodes


def columns(
    msp, fp: list[Pt], nodes: list[Pt], layer: str, size: float = 700, hatch: bool = True
) -> None:
    poly = Polygon(fp).buffer(-50)
    for x, y in nodes:
        sq = [
            (x - size / 2, y - size / 2),
            (x + size / 2, y - size / 2),
            (x + size / 2, y + size / 2),
            (x - size / 2, y + size / 2),
        ]
        if not poly.contains(Polygon(sq)):
            continue
        msp.add_lwpolyline(sq, close=True, dxfattribs={"layer": layer})
        if hatch:
            h = msp.add_hatch(color=8, dxfattribs={"layer": layer})
            h.paths.add_polyline_path(sq, is_closed=True)


DIMOV = {"dimtxt": 250, "dimasz": 200, "dimexo": 150, "dimexe": 200, "dimgap": 80, "dimtsz": 0}


def dims(msp, fp: list[Pt], layer: str, off: float = 2500) -> None:
    xs = [p[0] for p in fp]
    ys = [p[1] for p in fp]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    d = msp.add_linear_dim(
        base=(x0, y0 - off),
        p1=(x0, y0),
        p2=(x1, y0),
        dimstyle="EZDXF",
        override=DIMOV,
        dxfattribs={"layer": layer},
    )
    d.render()
    d = msp.add_linear_dim(
        base=(x1 + off, y0),
        p1=(x1, y0),
        p2=(x1, y1),
        angle=90,
        dimstyle="EZDXF",
        override=DIMOV,
        dxfattribs={"layer": layer},
    )
    d.render()
    # 分段尺寸
    pts = sorted({p[0] for p in fp})
    for a, b in zip(pts, pts[1:], strict=False):
        d = msp.add_linear_dim(
            base=(a, y1 + off),
            p1=(a, y1),
            p2=(b, y1),
            dimstyle="EZDXF",
            override=DIMOV,
            dxfattribs={"layer": layer},
        )
        d.render()


def site_and_cps(
    msp, site: list[Pt], cps_idx: list[int], T: Sim, site_layer: str, cp_layer: str, h: float = 350
):
    msp.add_lwpolyline(site, close=True, dxfattribs={"layer": site_layer, "linetype": "PHANTOM"})
    cps = []
    for k, i in enumerate(cps_idx):
        p = site[i]
        e, n = T(p)
        cid = f"CP{k + 1}"
        msp.add_circle(p, h * 0.8, dxfattribs={"layer": cp_layer})
        msp.add_line((p[0] - h * 1.5, p[1]), (p[0] + h * 1.5, p[1]), dxfattribs={"layer": cp_layer})
        msp.add_line((p[0], p[1] - h * 1.5), (p[0], p[1] + h * 1.5), dxfattribs={"layer": cp_layer})
        msp.add_text(
            f"{cid} E={e:.3f} N={n:.3f}", height=h, dxfattribs={"layer": cp_layer}
        ).set_placement((p[0] + h * 2, p[1] + h))
        cps.append({"id": cid, "drawing": [round(p[0], 4), round(p[1], 4)], "twd97": [e, n]})
    return cps


def frame(
    layout, x0: float, y0: float, w: float, h: float, layer: str, title: str, th: float
) -> None:
    layout.add_lwpolyline(
        [(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)],
        close=True,
        dxfattribs={"layer": layer},
    )
    m = th * 2
    layout.add_lwpolyline(
        [(x0 + m, y0 + m), (x0 + w - m, y0 + m), (x0 + w - m, y0 + h - m), (x0 + m, y0 + h - m)],
        close=True,
        dxfattribs={"layer": layer},
    )
    tb = x0 + w - m - th * 30
    layout.add_lwpolyline(
        [(tb, y0 + m), (x0 + w - m, y0 + m), (x0 + w - m, y0 + m + th * 8), (tb, y0 + m + th * 8)],
        close=True,
        dxfattribs={"layer": layer},
    )
    layout.add_text(title, height=th * 1.5, dxfattribs={"layer": layer}).set_placement(
        (tb + th, y0 + m + th * 5)
    )
    layout.add_text(
        "合成測試圖 / SYNTHETIC — 比例 1:100", height=th * 0.8, dxfattribs={"layer": layer}
    ).set_placement((tb + th, y0 + m + th * 2))


def north_arrow(msp, p: Pt, deg: float, layer: str, r: float = 1200) -> None:
    """指北針：deg = 圖面 +Y 相對真北的偏角（建物旋轉時指北針跟著轉）。"""
    tip = rot((0, r), deg)
    l = rot((-r * 0.4, -r * 0.6), deg)
    rr = rot((r * 0.4, -r * 0.6), deg)
    msp.add_circle(p, r, dxfattribs={"layer": layer})
    msp.add_lwpolyline(
        [
            (p[0] + tip[0], p[1] + tip[1]),
            (p[0] + l[0], p[1] + l[1]),
            (p[0], p[1]),
            (p[0] + rr[0], p[1] + rr[1]),
        ],
        close=True,
        dxfattribs={"layer": layer},
    )
    t = rot((0, r * 1.4), deg)
    msp.add_text("N", height=r * 0.5, dxfattribs={"layer": layer}).set_placement(
        (p[0] + t[0], p[1] + t[1]), align=TextEntityAlignment.MIDDLE_CENTER
    )


def windows_on(msp, fp: list[Pt], rng: random.Random, layer: str, d: float = 200) -> None:
    """窗符號：外牆內側畫兩條短平行線（不斷開外牆；乾淨 case 用）。"""
    n = len(fp)
    inner = offset_ortho(fp, d)
    for i in range(n):
        a, b = fp[i], fp[(i + 1) % n]
        L = math.hypot(b[0] - a[0], b[1] - a[1])
        if L < 4000:
            continue
        ux, uy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
        nx, ny = -uy, ux
        t = rng.uniform(1200, L - 3000)
        for k in (0.33, 0.66):
            p0 = (a[0] + ux * t + nx * d * k, a[1] + uy * t + ny * d * k)
            msp.add_line(p0, (p0[0] + ux * 1800, p0[1] + uy * 1800), dxfattribs={"layer": layer})
    _ = inner


def common_noise(
    doc, msp, fp: list[Pt], rng: random.Random, L: dict[str, str], furn: int = 8, labels: int = 8
) -> None:
    segs = interior_walls(fp, rng)
    draw_interior(msp, segs, L["int"])
    doors(msp, segs, rng, L["door"])
    windows_on(msp, fp, rng, L["wind"])
    furniture(doc, msp, Polygon(fp), rng, furn, L["furn"])
    room_labels(msp, fp, rng, L["text"], n=labels)
    dims(msp, fp, L["dims"])


STD = {
    "int": "A-WALL-INT",
    "door": "A-DOOR",
    "wind": "A-WIND",
    "furn": "A-FURN",
    "text": "A-TEXT",
    "dims": "A-DIMS",
    "grid": "S-GRID",
    "gridt": "S-GRID-IDEN",
    "col": "S-COLS",
    "site": "C-PROP",
    "cp": "C-CTRL",
    "frame": "X-FRAME",
}


def setup_layers(doc, names: dict[str, str]) -> None:
    colors = {
        "int": 3,
        "door": 4,
        "wind": 5,
        "furn": 8,
        "text": 7,
        "dims": 2,
        "grid": 1,
        "gridt": 1,
        "col": 8,
        "site": 6,
        "cp": 6,
        "frame": 7,
    }
    for k, v in names.items():
        ensure_layer(doc, v, colors.get(k, 7))


# --------------------------------------------------------------------------- case 建構


def truth_base(
    case: str,
    units: str,
    insunits: int | None,
    T: Sim,
    fp_draw: list[Pt],
    site_draw: list[Pt],
    cps,
    layers,
    notes: str,
    **kw,
) -> dict[str, Any]:
    t: dict[str, Any] = {
        "case": case,
        "units": units,
        "insunits_header": insunits,
        "layers": layers,
        "control_points": cps,
        "footprint_twd97": T.poly(ccw(fp_draw)),
        "basement_twd97": None,
        "floor_filter": None,
        "site_twd97": T.poly(ccw(site_draw)),
        "gap_mm": 0.0,
        "notes": notes,
    }
    t.update(kw)
    t["footprint_area_m2"] = round(Polygon(t["footprint_twd97"]).area, 3)
    return t


def case_clean_mm():
    rng = random.Random(101)
    doc = new_doc(4)
    msp = doc.modelspace()
    setup_layers(doc, STD)
    ensure_layer(doc, "A-WALL", 7, 50)
    fp = shift(SHAPE_L, 5000, 7000)
    site = site_around(fp)
    T = Sim(0.001, 0.0, 281340.0, 2763120.0)
    msp.add_lwpolyline(fp, close=True, dxfattribs={"layer": "A-WALL"})
    common_noise(doc, msp, fp, rng, STD)
    nodes = grid(msp, fp, STD["grid"], STD["gridt"])
    columns(msp, fp, nodes, STD["col"])
    cps = site_and_cps(msp, site, [0, 1, 4], T, STD["site"], STD["cp"])
    north_arrow(msp, (fp[1][0] + 9000, fp[1][1] + 20000), 0.0, STD["text"])
    frame(doc.paperspace(), 0, 0, 420, 297, STD["frame"], "一層平面圖", 5)
    truth = truth_base(
        "clean_mm",
        "mm",
        4,
        T,
        fp,
        site,
        cps,
        {
            "exterior_wall": "A-WALL",
            "basement": None,
            "interior_wall": "A-WALL-INT",
            "site": "C-PROP",
        },
        "乾淨基準：mm、$INSUNITS=4 正確、局部原點、A-WALL 只有一條封閉 LWPOLYLINE（外牆外緣），圖上無旋轉。"
        "其他圖層照 AIA 慣例（A-WALL-INT 內牆雙線、A-DOOR、A-WIND、A-FURN、S-GRID、C-PROP 基地線、C-CTRL 控制點）。",
    )
    return doc, truth


def case_layer_names_cn():
    rng = random.Random(202)
    doc = new_doc(4)
    msp = doc.modelspace()
    L = {
        "int": "牆",
        "door": "門",
        "wind": "窗",
        "furn": "傢俱",
        "text": "文字",
        "dims": "尺寸",
        "grid": "軸線",
        "gridt": "軸線號",
        "col": "柱",
        "site": "基地線",
        "cp": "座標點",
        "frame": "建築-外框",
    }
    setup_layers(doc, L)
    for nm, c in (("WALL-1", 7), ("外牆線", 7), ("陽台", 3), ("A-WALL$0$舊", 7)):
        ensure_layer(doc, nm, c)
    fp = shift(SHAPE_U, 12000, -4000)
    site = site_around(fp, front=7000)
    T = Sim(0.001, 8.0, 296815.5, 2741730.25)
    # 真正外牆：WALL-1（封閉 LWPOLYLINE）
    msp.add_lwpolyline(fp, close=True, dxfattribs={"layer": "WALL-1"})
    # 「外牆線」：陽台欄杆外緣（開放 polyline，貼在凹口與北側外面）——名字最像但不是
    bx = list(fp)
    y_top = max(p[1] for p in bx)
    x_min, x_max = min(p[0] for p in bx), max(p[0] for p in bx)
    msp.add_lwpolyline(
        [
            (x_min + 1000, y_top),
            (x_min + 1000, y_top + 1500),
            (x_min + 9000, y_top + 1500),
            (x_min + 9000, y_top),
        ],
        dxfattribs={"layer": "外牆線"},
    )
    msp.add_lwpolyline(
        [
            (x_max - 9000, y_top),
            (x_max - 9000, y_top + 1500),
            (x_max - 1000, y_top + 1500),
            (x_max - 1000, y_top),
        ],
        dxfattribs={"layer": "外牆線"},
    )
    msp.add_lwpolyline(
        [(fp[0][0] - 1200, fp[0][1] + 3000), (fp[0][0] - 1200, fp[0][1] + 15000)],
        dxfattribs={"layer": "外牆線"},
    )
    # 「陽台」：封閉矩形（在外框外）——封閉但不是外牆
    msp.add_lwpolyline(
        [
            (x_min + 2000, fp[0][1] - 1800),
            (x_min + 14000, fp[0][1] - 1800),
            (x_min + 14000, fp[0][1]),
            (x_min + 2000, fp[0][1]),
        ],
        close=True,
        dxfattribs={"layer": "陽台"},
    )
    # 舊版外框（較小，前一版設計）留在奇怪圖層
    old = shift([(0, 0), (30000, 0), (30000, 18000), (0, 18000)], fp[0][0] + 3000, fp[0][1] + 1000)
    msp.add_lwpolyline(old, close=True, dxfattribs={"layer": "A-WALL$0$舊", "color": 8})
    common_noise(doc, msp, fp, rng, L, furn=10)
    nodes = grid(msp, fp, L["grid"], L["gridt"])
    columns(msp, fp, nodes, L["col"])
    cps = site_and_cps(msp, site, [0, 1, 2, 4], T, L["site"], L["cp"])
    # 「建築-外框」其實是 modelspace 的圖框
    xs = [p[0] for p in site]
    ys = [p[1] for p in site]
    frame(msp, min(xs) - 12000, min(ys) - 12000, 72000, 52000, "建築-外框", "標準層平面圖", 600)
    north_arrow(msp, (max(xs) + 4000, max(ys) - 2000), 8.0, L["text"])
    truth = truth_base(
        "layer_names_cn",
        "mm",
        4,
        T,
        fp,
        site,
        cps,
        {
            "exterior_wall": "WALL-1",
            "basement": None,
            "interior_wall": "牆",
            "site": "基地線",
            "decoys": ["外牆線", "陽台", "建築-外框", "A-WALL$0$舊"],
        },
        "中文 / 亂取圖層名：真正外牆在 WALL-1（封閉 LWPOLYLINE）。"
        "陷阱：「外牆線」其實是陽台欄杆（開放線）、「陽台」是外框外的封閉矩形、「建築-外框」是 modelspace 圖框、"
        "「A-WALL$0$舊」是綁定 xref 殘留的舊版較小外框（名字最像 A-WALL）、「牆」是內牆雙線。圖面相對正北旋轉 8°。",
    )
    return doc, truth


def case_rotated():
    rng = random.Random(303)
    doc = new_doc(4)
    msp = doc.modelspace()
    setup_layers(doc, STD)
    ensure_layer(doc, "A-WALL", 7, 50)
    fp = shift(SHAPE_STEP, 3000, 4000)
    site = site_around(fp, skew=3000)
    theta = 23.5
    T = Sim(0.001, theta, 263508.8, 2775444.6)
    msp.add_lwpolyline(fp, close=True, dxfattribs={"layer": "A-WALL"})
    common_noise(doc, msp, fp, rng, STD)
    nodes = grid(msp, fp, STD["grid"], STD["gridt"], (7000, 5000))
    columns(msp, fp, nodes, STD["col"])
    cps = site_and_cps(msp, site, [0, 1, 3], T, STD["site"], STD["cp"])
    # 指北針：圖面 +Y 偏離真北 23.5°（圖紙上真北方向 = 往左轉 → 指北針畫成 -23.5°）
    north_arrow(msp, (fp[1][0] + 9000, fp[1][1] + 18000), -theta, STD["text"])
    msp.add_text(
        "真北 N23.5°W（圖面軸線）", height=300, dxfattribs={"layer": STD["text"]}
    ).set_placement((fp[1][0] + 7000, fp[1][1] + 15500))
    frame(doc.paperspace(), 0, 0, 420, 297, STD["frame"], "一層平面圖", 5)
    truth = truth_base(
        "rotated",
        "mm",
        4,
        T,
        fp,
        site,
        cps,
        {"exterior_wall": "A-WALL", "basement": None, "site": "C-PROP"},
        "建物軸線相對正北旋轉 23.5°：圖上外框仍正交（軸對齊），控制點的 twd97 反映 +23.5° 旋轉（drawing→TWD97 逆時針）。"
        "測試：相似變換要解出旋轉角；只做平移+縮放會整個歪掉。指北針畫成偏 23.5°。",
        rotation_deg=theta,
    )
    return doc, truth


def case_double_line_walls():
    rng = random.Random(404)
    doc = new_doc(4)
    msp = doc.modelspace()
    setup_layers(doc, STD)
    ensure_layer(doc, "A-WALL", 7, 50)
    fp = shift(SHAPE_T, 8000, 6000)
    site = site_around(fp)
    T = Sim(0.001, -12.0, 305210.0, 2718640.0)
    d = 200
    inner = offset_ortho(fp, d)
    n = len(fp)
    openings: dict[int, list[tuple[float, float]]] = {}
    max_open = 0.0
    for i in range(n):
        a, b = fp[i], fp[(i + 1) % n]
        Lg = math.hypot(b[0] - a[0], b[1] - a[1])
        ops = []
        t = 1500.0
        while True:
            w = float(rng.choice([900, 1200, 1500, 1800, 2400]))
            t += rng.uniform(1200, 3500)
            if t + w > Lg - 1500:
                break
            ops.append((t, t + w))
            max_open = max(max_open, w)
            t += w
        openings[i] = ops
    for i in range(n):
        a, b = fp[i], fp[(i + 1) % n]
        qa, qb = inner[i], inner[(i + 1) % n]
        Lg = math.hypot(b[0] - a[0], b[1] - a[1])
        ux, uy = (b[0] - a[0]) / Lg, (b[1] - a[1]) / Lg
        # 外緣
        cuts = [0.0] + [v for o in openings[i] for v in o] + [Lg]
        for s0, s1 in zip(cuts[::2], cuts[1::2], strict=False):
            msp.add_line(
                (a[0] + ux * s0, a[1] + uy * s0),
                (a[0] + ux * s1, a[1] + uy * s1),
                dxfattribs={"layer": "A-WALL"},
            )
        # 內緣：開口投影到內線（同一個沿邊參數，內線起點相對外線起點的投影偏移）
        off = (qa[0] - a[0]) * ux + (qa[1] - a[1]) * uy
        Li = math.hypot(qb[0] - qa[0], qb[1] - qa[1])
        icuts = [0.0] + [v - off for o in openings[i] for v in o] + [Li]
        for s0, s1 in zip(icuts[::2], icuts[1::2], strict=False):
            msp.add_line(
                (qa[0] + ux * s0, qa[1] + uy * s0),
                (qa[0] + ux * s1, qa[1] + uy * s1),
                dxfattribs={"layer": "A-WALL"},
            )
        # 開口：窗（三線）或門（弧）；洞口端部牆封口線
        nx, ny = -uy, ux
        for k, (s0, s1) in enumerate(openings[i]):
            for s in (s0, s1):
                p = (a[0] + ux * s, a[1] + uy * s)
                msp.add_line(p, (p[0] + nx * d, p[1] + ny * d), dxfattribs={"layer": "A-WALL"})
            if (s1 - s0) <= 1200 and k % 2 == 0:
                h = (a[0] + ux * s0 + nx * d, a[1] + uy * s0 + ny * d)
                ang = math.degrees(math.atan2(uy, ux))
                msp.add_line(
                    h,
                    (h[0] + nx * (s1 - s0), h[1] + ny * (s1 - s0)),
                    dxfattribs={"layer": "A-DOOR"},
                )
                msp.add_arc(h, s1 - s0, ang, ang + 90, dxfattribs={"layer": "A-DOOR"})
            else:
                for f in (0.0, 0.5, 1.0):
                    p0 = (a[0] + ux * s0 + nx * d * f, a[1] + uy * s0 + ny * d * f)
                    msp.add_line(
                        p0,
                        (p0[0] + ux * (s1 - s0), p0[1] + uy * (s1 - s0)),
                        dxfattribs={"layer": "A-WIND"},
                    )
    segs = interior_walls(fp, rng)
    draw_interior(msp, segs, STD["int"])
    doors(msp, segs, rng, STD["door"])
    furniture(doc, msp, Polygon(fp), rng, 8, STD["furn"])
    room_labels(msp, fp, rng, STD["text"])
    dims(msp, fp, STD["dims"])
    nodes = grid(msp, fp, STD["grid"], STD["gridt"])
    columns(msp, fp, nodes, STD["col"])
    cps = site_and_cps(msp, site, [0, 1, 2], T, STD["site"], STD["cp"])
    frame(doc.paperspace(), 0, 0, 594, 420, STD["frame"], "一層平面圖", 6)
    truth = truth_base(
        "double_line_walls",
        "mm",
        4,
        T,
        fp,
        site,
        cps,
        {"exterior_wall": "A-WALL", "basement": None, "site": "C-PROP"},
        f"外牆以雙線 LINE 表示（外緣 + 內緣，牆厚 {d} mm），門窗處兩線都斷開（開口 900–2400 mm），"
        "洞口端有 200 mm 封口短線、窗在 A-WIND（三線）、門在 A-DOOR（弧）。真值 footprint = 外緣線；"
        "陷阱：取到內緣會少一圈牆厚面積、把封口短線當外框會產生鋸齒；gap_mm 記開口最大寬（外緣線需跨過的縫）。",
        wall_thickness_mm=d,
        gap_mm=max_open,
    )
    return doc, truth


def case_gaps_unjoined():
    rng = random.Random(505)
    doc = new_doc(4)
    msp = doc.modelspace()
    setup_layers(doc, STD)
    ensure_layer(doc, "外牆", 7, 50)
    fp = shift(SHAPE_L, 40000, 25000)
    site = site_around(fp, side=4000)
    T = Sim(0.001, 3.25, 274005.0, 2752890.0)
    n = len(fp)
    segs: list[tuple[Pt, Pt]] = []
    max_gap = 0.0
    for i in range(n):
        a, b = fp[i], fp[(i + 1) % n]
        Lg = math.hypot(b[0] - a[0], b[1] - a[1])
        ux, uy = (b[0] - a[0]) / Lg, (b[1] - a[1]) / Lg
        k = rng.randint(2, 6)
        cuts = sorted(rng.uniform(0.1, 0.9) * Lg for _ in range(k - 1))
        cuts = [0.0] + cuts + [Lg]
        for j, (s0, s1) in enumerate(zip(cuts, cuts[1:], strict=False)):
            g0 = 0.0 if j == 0 and rng.random() < 0.3 else rng.uniform(0.5, 2.5)
            g1 = rng.uniform(0.5, 2.5)
            max_gap = max(max_gap, g0 + g1)
            e0, e1 = s0 + g0, s1 - g1
            # 少數片段過頭重疊
            if rng.random() < 0.15:
                e1 = s1 + rng.uniform(50, 300)
            p0 = (round(a[0] + ux * e0, 3), round(a[1] + uy * e0, 3))
            p1 = (round(a[0] + ux * e1, 3), round(a[1] + uy * e1, 3))
            segs.append((p1, p0) if rng.random() < 0.5 else (p0, p1))
        # 重複畫一段（完全疊在既有線上）
        if rng.random() < 0.4:
            s = rng.uniform(0.2, 0.6) * Lg
            segs.append(
                ((a[0] + ux * s, a[1] + uy * s), (a[0] + ux * (s + 1500), a[1] + uy * (s + 1500)))
            )
    # 角落縫：相鄰兩邊端點都退縮後，角落最大縫 = 兩向退縮的斜距；保守記錄
    rng.shuffle(segs)
    for p0, p1 in segs:
        msp.add_line(p0, p1, dxfattribs={"layer": "外牆"})
    common_noise(doc, msp, fp, rng, STD)
    nodes = grid(msp, fp, STD["grid"], STD["gridt"])
    columns(msp, fp, nodes, STD["col"])
    cps = site_and_cps(msp, site, [0, 1, 2, 4], T, STD["site"], STD["cp"])
    frame(doc.paperspace(), 0, 0, 420, 297, STD["frame"], "一層平面圖", 5)
    truth = truth_base(
        "gaps_unjoined",
        "mm",
        4,
        T,
        fp,
        site,
        cps,
        {"exterior_wall": "外牆", "basement": None, "site": "C-PROP"},
        "外牆由大量 LINE 片段組成（每邊 2–6 段、隨機順序、方向不一），接點留 1–5 mm 的縫（含角落），"
        "約 15% 片段過頭 50–300 mm 重疊、少數片段重複畫。測試：容差接線（snap ≤ 5 mm）、去重、截斷過頭後能還原單一封閉外框。",
        gap_mm=5.0,
    )
    return doc, truth


def case_blocks_xref():
    rng = random.Random(606)
    doc = new_doc(4)
    msp = doc.modelspace()
    setup_layers(doc, STD)
    ensure_layer(doc, "A-WALL", 7, 50)
    fp = shift(SHAPE_U, -18000, -10000)  # 原點在建物中間
    site = site_around(fp)
    T = Sim(0.001, 0.0, 289777.7, 2731222.2)
    # (邊數不需要)
    # 邊 0..2 → modelspace LWPOLYLINE（開放）
    msp.add_lwpolyline([fp[0], fp[1], fp[2], fp[3]], dxfattribs={"layer": "A-WALL"})
    # 邊 3..5（凹口三邊）→ 圖塊 WING_NOTCH，旋轉 90°、縮放 2（實體寫在 1/2 縮放的區域座標）
    ins1, r1, s1 = (fp[3][0] + 777.0, fp[3][1] - 333.0), 90.0, 2.0
    b1 = doc.blocks.new("WING_NOTCH")
    loc = [to_block_local(p, ins1, r1, s1) for p in (fp[3], fp[4], fp[5], fp[6])]
    b1.add_lwpolyline(loc, dxfattribs={"layer": "A-WALL"})
    b1.add_text("NOTCH", height=150, dxfattribs={"layer": "A-TEXT"}).set_placement(loc[1])
    msp.add_blockref(
        "WING_NOTCH",
        ins1,
        dxfattribs={"layer": "A-WALL", "rotation": r1, "xscale": s1, "yscale": s1, "zscale": s1},
    )
    # 邊 6,7 → 圖塊 WEST_WING（旋轉 -30°、縮放 1），內含巢狀圖塊 CORNER_W（旋轉 45°、縮放 0.5；實體在 0 層 → 繼承 INSERT 圖層）
    ins2, r2, s2 = (fp[7][0] - 2500.0, fp[7][1] - 4000.0), -30.0, 1.0
    ins3_world, r3_world, s3_world = (
        (fp[7][0] + 100.0, fp[7][1] + 100.0),
        15.0,
        0.5,
    )  # 巢狀圖塊的總變換
    b3 = doc.blocks.new("CORNER_W")
    b3.add_line(
        to_block_local(fp[6], ins3_world, r3_world, s3_world),
        to_block_local(fp[7], ins3_world, r3_world, s3_world),
        dxfattribs={"layer": "0"},
    )
    b3.add_line(
        to_block_local(fp[7], ins3_world, r3_world, s3_world),
        to_block_local(fp[0], ins3_world, r3_world, s3_world),
        dxfattribs={"layer": "0"},
    )
    b3.add_circle((0, 0), 200, dxfattribs={"layer": "0"})
    b2 = doc.blocks.new("WEST_WING")
    # 巢狀插入點與旋轉：在 WEST_WING 區域座標內，使總變換 = (ins3_world, r3_world, s3_world)
    ins3_local = to_block_local(ins3_world, ins2, r2, s2)
    b2.add_blockref(
        "CORNER_W",
        ins3_local,
        dxfattribs={
            "layer": "A-WALL",
            "rotation": r3_world - r2,
            "xscale": s3_world / s2,
            "yscale": s3_world / s2,
        },
    )
    b2.add_text("西翼", height=400, dxfattribs={"layer": "A-TEXT"}).set_placement((0, 0))
    msp.add_blockref("WEST_WING", ins2, dxfattribs={"layer": "A-WALL-INT", "rotation": r2})
    # 外部參照：指向不存在的檔
    doc.add_xref_def("../../_missing_/site_survey_twd97.dwg", "XREF_SITE_SURVEY")
    msp.add_blockref("XREF_SITE_SURVEY", (0, 0), dxfattribs={"layer": "C-PROP"})
    doc.add_xref_def("C:\\Projects\\SYNTH\\furniture_lib.dxf", "XREF_FURN_LIB")
    msp.add_blockref("XREF_FURN_LIB", (5000, 2000), dxfattribs={"layer": "A-FURN", "rotation": 90})
    common_noise(doc, msp, fp, rng, STD)
    nodes = grid(msp, fp, STD["grid"], STD["gridt"])
    columns(msp, fp, nodes, STD["col"])
    cps = site_and_cps(msp, site, [0, 1, 2], T, STD["site"], STD["cp"])
    frame(doc.paperspace(), 0, 0, 420, 297, STD["frame"], "一層平面圖", 5)
    truth = truth_base(
        "blocks_xref",
        "mm",
        4,
        T,
        fp,
        site,
        cps,
        {"exterior_wall": "A-WALL", "basement": None, "site": "C-PROP"},
        "外牆分散在三處：modelspace 開放 LWPOLYLINE（3 邊）＋ INSERT WING_NOTCH（旋轉 90°、縮放 2，凹口 3 邊）"
        "＋ INSERT WEST_WING（旋轉 -30°，INSERT 自己在 A-WALL-INT 層）內巢狀 INSERT CORNER_W（相對旋轉 45°、縮放 0.5，"
        "實體在 0 層、巢狀 INSERT 在 A-WALL 層 → 依 DXF 規則繼承為 A-WALL）。須展開圖塊（virtual_entities / 遞迴變換）才拿得到完整外框。"
        "另有兩個 xref 定義（XREF_SITE_SURVEY、XREF_FURN_LIB）指向不存在的外部檔：工具要報告「缺外部參照」而不崩潰。",
        xrefs=["XREF_SITE_SURVEY", "XREF_FURN_LIB"],
    )
    return doc, truth


def case_multi_floor():
    rng = random.Random(707)
    doc = new_doc(4)
    msp = doc.modelspace()
    fp = shift(SHAPE_L, 0, 0)
    site = site_around(fp, front=5000, side=3000, back=3500)
    xs = [p[0] for p in site]
    ys = [p[1] for p in site]
    b1 = [
        (min(xs) + 1000, min(ys) + 1000),
        (max(xs) - 1000, min(ys) + 1000),
        (max(xs) - 1000, max(ys) - 4500),
        (min(xs) + 1000, max(ys) - 4500),
    ]
    b1 = [(round(x), round(y)) for x, y in b1]
    rf = [
        (fp[3][0] - 8000, fp[3][1] - 2000),
        (fp[3][0] - 2000, fp[3][1] - 2000),
        (fp[3][0] - 2000, fp[3][1] + 4000),
        (fp[3][0] - 8000, fp[3][1] + 4000),
    ]
    T = Sim(0.001, 15.0, 258880.0, 2789010.0)
    offsets = {"1F": (0.0, 0.0), "B1": (60000.0, 0.0), "RF": (120000.0, 0.0)}
    titles = {"1F": "一層平面圖", "B1": "地下一層平面圖", "RF": "屋頂層平面圖"}
    for fl, (ox, oy) in offsets.items():
        Lf = {
            k: f"{fl}-{v}"
            for k, v in {
                "int": "WALL-INT",
                "door": "DOOR",
                "wind": "WIND",
                "furn": "FURN",
                "text": "TEXT",
                "dims": "DIMS",
            }.items()
        }
        Lf.update({"grid": f"{fl}-GRID", "gridt": f"{fl}-GRID-T", "col": f"{fl}-COL"})
        setup_layers(doc, Lf)
        ensure_layer(doc, f"{fl}-WALL", 7, 50)
        sub = rng
        if fl == "1F":
            outline = fp
        elif fl == "B1":
            outline = b1
        else:
            outline = fp  # 女兒牆 = 1F 外框
        o = shift(outline, ox, oy)
        msp.add_lwpolyline(o, close=True, dxfattribs={"layer": f"{fl}-WALL"})
        if fl == "RF":
            msp.add_lwpolyline(offset_ortho(o, 150), close=True, dxfattribs={"layer": f"{fl}-WALL"})
            msp.add_lwpolyline(shift(rf, ox, oy), close=True, dxfattribs={"layer": f"{fl}-WALL"})
            msp.add_text(
                "突出物（梯間）", height=400, dxfattribs={"layer": Lf["text"]}
            ).set_placement((rf[0][0] + ox + 500, rf[0][1] + oy + 2500))
            dims(msp, o, Lf["dims"])
        elif fl == "B1":
            segs = interior_walls(o, sub, (8000, 6000))
            draw_interior(msp, segs, Lf["int"], 250)
            for k in range(14):  # 停車格
                x = o[0][0] + 1500 + k * 2500
                if x + 2500 > o[1][0] - 1500:
                    break
                msp.add_lwpolyline(
                    [
                        (x, o[0][1] + 1500),
                        (x + 2500, o[0][1] + 1500),
                        (x + 2500, o[0][1] + 6500),
                        (x, o[0][1] + 6500),
                    ],
                    dxfattribs={"layer": Lf["furn"]},
                )
            msp.add_text("車道 1:6", height=500, dxfattribs={"layer": Lf["text"]}).set_placement(
                (o[1][0] - 6000, o[2][1] - 3000)
            )
            dims(msp, o, Lf["dims"])
        else:
            common_noise(doc, msp, o, sub, Lf)
        nodes = grid(msp, shift(fp, ox, oy), Lf["grid"], Lf["gridt"])
        columns(msp, o, nodes, Lf["col"])
        tx = min(p[0] for p in o) + ox * 0
        ensure_layer(doc, "TITLE", 7)
        msp.add_text(titles[fl], height=900, dxfattribs={"layer": "TITLE"}).set_placement(
            (min(p[0] for p in shift(site, ox, oy)), min(ys) - 9000)
        )
        msp.add_text("S:1/100", height=450, dxfattribs={"layer": "TITLE"}).set_placement(
            (min(p[0] for p in shift(site, ox, oy)), min(ys) - 10500)
        )
        _ = tx
    setup_layers(doc, {"site": "C-PROP", "cp": "C-CTRL", "frame": "X-FRAME"})
    cps = site_and_cps(msp, site, [0, 1, 2, 4], T, "C-PROP", "C-CTRL")
    msp.add_lwpolyline(
        shift(site, *offsets["B1"]),
        close=True,
        dxfattribs={"layer": "C-PROP", "linetype": "PHANTOM"},
    )
    frame(doc.paperspace(), 0, 0, 841, 594, "X-FRAME", "各層平面圖", 7)
    truth = truth_base(
        "multi_floor",
        "mm",
        4,
        T,
        fp,
        site,
        cps,
        {"exterior_wall": "1F-WALL", "basement": "B1-WALL", "roof": "RF-WALL", "site": "C-PROP"},
        "同一 modelspace 並排三層：1F 在原位、B1 右移 60000 mm、RF 右移 120000 mm；圖層前綴 1F-/B1-/RF-，"
        "下方圖名文字「一層平面圖」「地下一層平面圖」「屋頂層平面圖」（TITLE 層）。控制點只對 1F 原位（基地線 C-PROP 也只在 1F 有控制點）。"
        "真值 footprint = 1F-WALL；basement = B1-WALL 平移回原位（-60000, 0）後轉 TWD97（地下室大於 1F、在基地內）。"
        "陷阱：不分層全抓會把三層外框混在一起；RF-WALL 有女兒牆內線與梯間突出物。各層軸線編號相同可用來對位。",
        floor_filter="1F-",
        basement_twd97=T.poly(ccw([(float(x), float(y)) for x, y in b1])),
        floor_offsets_drawing={k: list(v) for k, v in offsets.items()},
        floor_titles=titles,
    )
    return doc, truth


def case_wrong_insunits():
    rng = random.Random(808)
    doc = new_doc(6)
    msp = doc.modelspace()
    setup_layers(doc, STD)
    ensure_layer(doc, "A-WALL", 7, 50)
    fp = shift(SHAPE_STEP, 1000, 1000)
    site = site_around(fp, front=8000)
    T = Sim(0.001, -5.0, 312450.0, 2709930.0)
    msp.add_lwpolyline(fp, close=True, dxfattribs={"layer": "A-WALL"})
    common_noise(doc, msp, fp, rng, STD)
    nodes = grid(msp, fp, STD["grid"], STD["gridt"])
    columns(msp, fp, nodes, STD["col"])
    cps = site_and_cps(msp, site, [0, 1, 4], T, STD["site"], STD["cp"])
    frame(doc.paperspace(), 0, 0, 420, 297, STD["frame"], "一層平面圖", 5)
    doc.header["$LUNITS"] = 2
    doc.header["$AUNITS"] = 0
    truth = truth_base(
        "wrong_insunits",
        "mm",
        6,
        T,
        fp,
        site,
        cps,
        {"exterior_wall": "A-WALL", "basement": None, "site": "C-PROP"},
        "圖其實是 mm（外框 28000 × 16000、文字高 300、門弧半徑 900），但 $INSUNITS 寫成 6（公尺）。"
        "信 header 會得到 28 km 的建物；正確做法是用控制點擬合出縮放比 ≈ 0.001 或由尺寸 / 文字高推斷單位，並在報告中標出 header 與實際不符。",
    )
    return doc, truth


def case_noise_heavy():
    rng = random.Random(909)
    doc = new_doc(4)
    msp = doc.modelspace()
    setup_layers(doc, STD)
    fp = shift(SHAPE_T, 30000, 15000)
    site = site_around(fp, front=6500, side=4000)
    T = Sim(0.001, 31.0, 267700.0, 2746600.0)
    # 外牆：0 層、紅色（ACI 1）、線寬 0.50，拆成 LINE 混在 0 層其他線中
    n = len(fp)
    for i in range(n):
        msp.add_line(
            fp[i], fp[(i + 1) % n], dxfattribs={"layer": "0", "color": 1, "lineweight": 50}
        )

    segs = interior_walls(fp, rng, (3600, 3000))
    draw_interior(msp, segs, "0", 120)
    doors(msp, segs, rng, "0")
    windows_on(msp, fp, rng, "0")
    # 0 層的其他閉合多邊形（陽台、樓梯間、雨遮）——顏色各異但不是紅
    xs = [p[0] for p in fp]
    ys = [p[1] for p in fp]
    msp.add_lwpolyline(
        [
            (min(xs) + 2000, min(ys) - 1500),
            (min(xs) + 12000, min(ys) - 1500),
            (min(xs) + 12000, min(ys)),
            (min(xs) + 2000, min(ys)),
        ],
        close=True,
        dxfattribs={"layer": "0", "color": 3},
    )
    msp.add_lwpolyline(
        [
            (fp[1][0] - 6000, fp[1][1] - 2500),
            (fp[1][0] + 1500, fp[1][1] - 2500),
            (fp[1][0] + 1500, fp[1][1]),
            (fp[1][0] - 6000, fp[1][1]),
        ],
        close=True,
        dxfattribs={"layer": "0", "color": 1, "lineweight": 25},
    )  # 雨遮：紅色但細線
    for k in range(12):  # 樓梯
        x = fp[0][0] + 3000 + k * 280
        msp.add_line(
            (x, fp[0][1] + 1000), (x, fp[0][1] + 3500), dxfattribs={"layer": "0", "color": 8}
        )
    furniture(doc, msp, Polygon(fp), rng, 30, "0")
    room_labels(msp, fp, rng, "0", n=14)
    dims(msp, fp, "0")
    setup_layers(
        doc, {k: STD[k] for k in ("grid", "gridt", "col", "site", "cp", "frame", "dims", "text")}
    )
    nodes = grid(msp, fp, STD["grid"], STD["gridt"], (6000, 5000))
    columns(msp, fp, nodes, STD["col"])
    # 剖面線 hatch：地坪磁磚（浴廁）與 RC 牆填實
    inner = Polygon(fp).buffer(-500)
    minx, miny, maxx, maxy = inner.bounds
    for k in range(10):
        x = rng.uniform(minx, maxx - 3000)
        y = rng.uniform(miny, maxy - 3000)
        sq = [(x, y), (x + 2400, y), (x + 2400, y + 1800), (x, y + 1800)]
        if not inner.contains(Polygon(sq)):
            continue
        h = msp.add_hatch(color=9, dxfattribs={"layer": "0"})
        h.set_pattern_fill("ANSI37" if k % 2 else "ANSI31", scale=40)
        h.paths.add_polyline_path(sq, is_closed=True)
    # 外牆內側填實 hatch（RC 牆），讓「封閉邊界」多一層干擾
    inn = offset_ortho(fp, 200)
    for i in range(n):
        h = msp.add_hatch(color=8, dxfattribs={"layer": "0"})
        h.paths.add_polyline_path(
            [fp[i], fp[(i + 1) % n], inn[(i + 1) % n], inn[i]], is_closed=True
        )
    # 大量文字與標註
    for _k in range(40):
        p = (rng.uniform(minx - 8000, maxx + 8000), rng.uniform(miny - 8000, maxy + 8000))
        msp.add_text(
            rng.choice(
                [
                    "FL+0.00",
                    "GL-0.45",
                    "RC牆 t=20cm",
                    "見詳圖 D-12",
                    "排水坡度 1/100",
                    "AC",
                    "EV",
                    "DS",
                ]
            ),
            height=250,
            dxfattribs={"layer": "0", "color": rng.choice([2, 4, 6, 7])},
        ).set_placement(p)
    for _k in range(8):
        a = (rng.uniform(minx, maxx), rng.uniform(miny, maxy))
        b = (a[0] + rng.uniform(1500, 5000), a[1])
        d = msp.add_linear_dim(
            base=(a[0], a[1] + 600),
            p1=a,
            p2=b,
            dimstyle="EZDXF",
            override=DIMOV,
            dxfattribs={"layer": "0"},
        )
        d.render()
    cps = site_and_cps(msp, site, [0, 1, 2, 4], T, STD["site"], STD["cp"])
    sx = [p[0] for p in site]
    sy = [p[1] for p in site]
    frame(msp, min(sx) - 15000, min(sy) - 15000, 84100 * 1.0, 59400 * 1.0, "0", "一層平面圖", 700)
    frame(doc.paperspace(), 0, 0, 841, 594, STD["frame"], "一層平面圖", 7)
    ps = doc.paperspace()
    ps.add_text("核章欄", height=8, dxfattribs={"layer": STD["frame"]}).set_placement((700, 60))
    for k in range(6):
        ps.add_line((20, 100 + k * 12), (200, 100 + k * 12), dxfattribs={"layer": STD["frame"]})
    truth = truth_base(
        "noise_heavy",
        "mm",
        4,
        T,
        fp,
        site,
        cps,
        {"exterior_wall": "0", "basement": None, "site": "C-PROP"},
        "大量干擾：幾乎所有東西都在 0 層（內牆、門窗、30 個家具圖塊、40 個文字、尺寸標註、浴廁磁磚 hatch、RC 牆 hatch、樓梯、陽台、雨遮），"
        "modelspace 與 paperspace 各有一個圖框。外牆是 0 層上 8 條 LINE，唯一特徵是 color=1（紅）且 lineweight=50（0.50 mm）；"
        "陷阱：雨遮也是紅色但 lineweight=25、陽台是封閉矩形、RC 牆 SOLID hatch（每面牆一塊 200 mm 帶狀）外邊界與外框重合（hatch 邊界不是線條實體）。"
        "工具要能用 layer+color+lineweight 過濾。",
        exterior_wall_filter={"layer": "0", "color": 1, "lineweight": 50},
    )
    return doc, truth


def case_twd97_native():
    rng = random.Random(1010)
    doc = new_doc(6)
    msp = doc.modelspace()
    names = {
        "int": "A-WALL-INT",
        "door": "A-DOOR",
        "wind": "A-WIND",
        "text": "A-TEXT",
        "site": "C-PROP",
        "cp": "C-CTRL",
        "frame": "X-FRAME",
    }
    setup_layers(doc, names)
    ensure_layer(doc, "A-WALL", 7, 50)
    E0, N0, th = 277123.45, 2768456.78, 17.0
    T_loc = Sim(0.001, th, E0, N0)  # 局部 mm → TWD97 m（只用來產生公尺座標）
    fp_mm = shift(SHAPE_U, 0, 0)
    site_mm = site_around(fp_mm)
    fp = [tuple(T_loc(p)) for p in fp_mm]
    site = [tuple(T_loc(p)) for p in site_mm]
    msp.add_lwpolyline(fp, close=True, dxfattribs={"layer": "A-WALL"})
    segs_mm = interior_walls(fp_mm, rng)
    segs = [(tuple(T_loc(a)), tuple(T_loc(b))) for a, b in segs_mm]
    draw_interior(msp, segs, names["int"], 0.12)
    for _a, b in segs[::3]:
        msp.add_arc(
            b,
            0.9,
            rng.choice([0, 90, 180, 270]) + th,
            rng.choice([0, 90, 180, 270]) + th + 90,
            dxfattribs={"layer": names["door"]},
        )
    poly = Polygon(fp).buffer(-1.5)
    minx, miny, maxx, maxy = poly.bounds
    for k in range(8):
        p = (rng.uniform(minx, maxx), rng.uniform(miny, maxy))
        if poly.contains(Point(p)):
            msp.add_text(
                ["客廳", "臥室", "廚房", "浴室", "梯廳"][k % 5],
                height=0.3,
                dxfattribs={"layer": names["text"], "rotation": th},
            ).set_placement(p)
    I = Sim(1.0, 0.0, 0.0, 0.0)
    cps = site_and_cps(msp, site, [0, 1, 2, 4], I, names["site"], names["cp"], h=0.35)
    frame(doc.paperspace(), 0, 0, 420, 297, names["frame"], "一層平面圖（TWD97）", 5)
    truth = truth_base(
        "twd97_native",
        "m",
        6,
        I,
        fp,
        site,
        cps,
        {"exterior_wall": "A-WALL", "basement": None, "site": "C-PROP"},
        "建築師已直接用 TWD97 TM2 公尺座標畫（$INSUNITS=6 正確、units=m），建物軸線偏 17°。控制點 drawing == twd97，"
        "擬合結果應為縮放 1、旋轉 0、平移 0。陷阱：工具若一律假設 mm 而 × 0.001 會把建物縮成 3 cm；"
        "大座標（E≈277000、N≈2768000）下 float 精度與文字高 0.3 m。",
        gap_mm=0.0,
    )
    return doc, truth


CASES: dict[str, Callable[[], tuple[Any, dict[str, Any]]]] = {
    "clean_mm": case_clean_mm,
    "layer_names_cn": case_layer_names_cn,
    "rotated": case_rotated,
    "double_line_walls": case_double_line_walls,
    "gaps_unjoined": case_gaps_unjoined,
    "blocks_xref": case_blocks_xref,
    "multi_floor": case_multi_floor,
    "wrong_insunits": case_wrong_insunits,
    "noise_heavy": case_noise_heavy,
    "twd97_native": case_twd97_native,
}

# --------------------------------------------------------------------------- 輸出（確定性）

_VOLATILE_HEADER = (
    "$TDCREATE",
    "$TDUCREATE",
    "$TDUPDATE",
    "$TDUUPDATE",
    "$TDINDWG",
    "$TDUSRTIMER",
    "$FINGERPRINTGUID",
    "$VERSIONGUID",
)
_FIXED = {
    "$TDCREATE": "2460000.5",
    "$TDUCREATE": "2460000.5",
    "$TDUPDATE": "2460000.5",
    "$TDUUPDATE": "2460000.5",
    "$TDINDWG": "0.0",
    "$TDUSRTIMER": "0.0",
    "$FINGERPRINTGUID": "{00000000-0000-4000-8000-000000000094}",
    "$VERSIONGUID": "{00000000-0000-4000-8000-000000000095}",
}


def _fix_header(text: str) -> str:
    """把 header 的時間戳 / GUID 改寫為常數（DXF tag pair：'  9\\n$NAME\\n<code>\\n<value>'）。"""
    lines = text.split("\n")
    for i in range(len(lines) - 3):
        if lines[i].strip() == "9" and lines[i + 1].strip() in _FIXED:
            lines[i + 3] = _FIXED[lines[i + 1].strip()]
    return "\n".join(lines)


def _sort_classes(text: str) -> str:
    """ezdxf 的 CLASSES 區段順序來自 set（依記憶體位址），改成依類別名排序。"""
    head = "  2\nCLASSES\n"
    i = text.find(head)
    if i < 0:
        return text
    i += len(head)
    j = text.find("  0\nENDSEC\n", i)
    sep = "  0\nCLASS\n"
    chunks = [c for c in text[i:j].split(sep) if c]
    chunks.sort(key=lambda c: c.split("\n")[1])
    return text[:i] + "".join(sep + c for c in chunks) + text[j:]


def _fix_all(text: str) -> str:
    return _sort_classes(_fix_header(text))


def normalize_dxf(text: str) -> str:
    """--check 比對用：去掉非決定性欄位（header 時間戳 / GUID、CLASSES 順序）。"""
    return _fix_all(text.replace("\r\n", "\n"))


def build(case: str) -> tuple[str, str]:
    doc, truth = CASES[case]()
    buf = io.StringIO()
    doc.write(buf)
    dxf = _fix_all(buf.getvalue())
    return dxf, json.dumps(truth, ensure_ascii=False, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="只比對，不寫檔")
    ap.add_argument("--out", type=Path, default=OUT)
    a = ap.parse_args(argv)
    bad = []
    for case in CASES:
        dxf, truth = build(case)
        d = a.out / case
        files = {"drawing.dxf": dxf, "truth.json": truth}
        if a.check:
            for fn, content in files.items():
                p = d / fn
                if not p.exists():
                    bad.append(f"{case}/{fn}: 不存在")
                    continue
                disk = p.read_text(encoding="utf-8")
                if fn.endswith(".dxf"):
                    same = normalize_dxf(disk) == normalize_dxf(content)
                else:
                    same = disk == content
                if not same:
                    bad.append(f"{case}/{fn}: 與產生器輸出不同")
        else:
            d.mkdir(parents=True, exist_ok=True)
            for fn, content in files.items():
                (d / fn).write_text(content, encoding="utf-8", newline="\n")
            print(f"wrote {case}: {len(dxf.encode()) / 1024:.0f} KB")
    if a.check:
        if bad:
            print("\n".join(bad))
            return 1
        print(f"OK：{len(CASES)} 個 case 與磁碟一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
