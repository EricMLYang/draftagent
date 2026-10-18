"""建築師 DXF → TWD97 GeoJSON（圖種 `arch-import` 的 render 核心）。純函式，不得呼叫 LLM。

流程
1. 讀 DXF（結構錯誤時改用 `ezdxf.recover`）；遞迴展開 INSERT（`virtual_entities()`，含巢狀、縮放、旋轉、
   MINSERT）；圖塊內 0 層實體繼承 INSERT 的圖層；外部參照 / 缺圖塊 → warning，不崩潰。
   每個實體的來源以「handle 路徑」記錄：`<模型空間 INSERT handle>><圖塊內實體 handle>…`。
2. floor_filter：只取完全落在範圍（bbox 或圖名文字周圍方框）內的實體。
3. 曲線（LINE / ARC / CIRCLE / ELLIPSE / SPLINE / LWPOLYLINE / POLYLINE）以 `ezdxf.path` 攤平成折線。
4. 接線（容差 join_tol，圖上單位）：
   - 端點群聚（STRtree dwithin + union-find）→ 合併到群心；每個接縫的實際距離記錄下來
   - 仍懸空的端點若在 join_tol 內碰到其他線 → 拉到最近點並微幅延伸（讓 noding 一定切得到）
   - `unary_union` 節點化（同時去除重複 / 重疊線）→ `polygonize`
5. 外框
   - outline（單線外框）：面聯集 → 取外環（填洞）。縫隙 > join_tol 時就不會封閉，直接回報，不硬接。
   - double_line（雙線取外緣）：
     a. polygonize 得到牆段面；若面聯集填洞後是單一外框且涵蓋所有牆線（外牆是封閉環）→ 直接取外環（精確）
     b. 否則（門窗開口把環切斷）→ 形態學閉運算（mitre 接角）：
        (牆段面 ∪ 牆線 0.01·join_tol 細帶).buffer(+r) → 填洞 → .buffer(-r)，r = opening_max / 2。
        寬 ≤ opening_max 的開口被橋接；mitre 外推再內縮對直邊與凸 / 凹轉角是精確反運算（不會磨圓），
        沒畫封口短線的開放雙線也能接起來。外緣誤差 ≤ 細帶寬（0.01·join_tol）。
        限制：外牆凹口寬度 < opening_max 時凹口會被填平（spec 調小 opening_max）；
        小於約 11° 的銳角會被 mitre 上限削角。
6. 單位換算（drawing_units → m）＋控制點相似變換（最小平方：平移、旋轉、等比縮放；無 shear / 非等比）。
7. 輸出 arch.geojson（EPSG:3826）、transform.json、overlay.png。
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from shapely import STRtree
from shapely.affinity import affine_transform
from shapely.geometry import LineString, MultiLineString, Point, Polygon, box, mapping
from shapely.geometry.polygon import orient
from shapely.ops import polygonize, unary_union

from draftagent.schema.arch import ArchImportSpec

PING_PER_M2 = 0.3025
MAX_INSERT_DEPTH = 16
MITRE_LIMIT = 10.0  # 閉運算 mitre 上限（約 11° 以下的銳角才會被削角）
CURVE_TYPES = {"LINE", "ARC", "CIRCLE", "ELLIPSE", "SPLINE", "LWPOLYLINE", "POLYLINE"}
POINT_TYPES = {"POINT", "CIRCLE", "INSERT"}
TEXT_TYPES = {"TEXT", "MTEXT"}
POLY_ROLES = {"exterior_wall": "footprint", "basement": "basement", "open_space": "open_space"}
INSUNITS_NAME = {1: "in", 2: "ft", 4: "mm", 5: "cm", 6: "m"}


# ---------------------------------------------------------------- 讀 DXF / 展開圖塊


def load_doc(path: str | Path) -> tuple[Any, list[str]]:
    """讀 DXF；結構錯誤時用 recover。回傳 (doc, warnings)。"""
    import ezdxf
    from ezdxf import recover

    warnings: list[str] = []
    try:
        doc = ezdxf.readfile(str(path))
    except ezdxf.DXFStructureError:
        doc, auditor = recover.readfile(str(path))
        warnings.append(f"DXF 結構有誤，已用 recover 讀取（修正 {len(auditor.fixes)} 處、錯誤 {len(auditor.errors)} 處）")
    return doc, warnings


def _handle(e: Any) -> str:
    h = e.dxf.get("handle")
    if h:
        return str(h)
    src = getattr(e, "source_of_copy", None)
    if src is not None and src.dxf.get("handle"):
        return str(src.dxf.handle)
    return "?"


def walk_entities(doc: Any, warnings: list[str]) -> Iterator[tuple[Any, str, tuple[str, ...]]]:
    """模型空間所有實體（INSERT 遞迴展開）→ (entity, 有效圖層, handle 路徑)。INSERT 本身也會產出。"""
    for e in doc.modelspace():
        yield from _walk(doc, e, None, (), 0, warnings)


def _walk(doc: Any, e: Any, parent_layer: str | None, path: tuple[str, ...], depth: int,
          warnings: list[str]) -> Iterator[tuple[Any, str, tuple[str, ...]]]:
    layer = e.dxf.get("layer", "0")
    if layer == "0" and parent_layer is not None:
        layer = parent_layer
    p = (*path, _handle(e))
    t = e.dxftype()
    yield e, layer, p
    if t != "INSERT":
        return
    name = e.dxf.get("name", "")
    blk = doc.blocks.get(name)
    if blk is None:
        warnings.append(f"INSERT {p[-1]} 參照的圖塊 {name!r} 不存在，略過")
        return
    if blk.block.is_xref or blk.block.is_xref_overlay:
        warnings.append(f"INSERT {p[-1]} 是外部參照 {name!r}（{blk.block.dxf.get('xref_path', '')}），未載入，略過")
        return
    if depth >= MAX_INSERT_DEPTH:
        warnings.append(f"INSERT {p[-1]} 巢狀超過 {MAX_INSERT_DEPTH} 層，略過")
        return
    try:
        inserts = list(e.multi_insert()) if e.mcount > 1 else [e]
        for ins in inserts:
            for ve in ins.virtual_entities():
                yield from _walk(doc, ve, layer, p, depth + 1, warnings)
    except Exception as ex:  # noqa: BLE001 - 單一圖塊壞掉不應讓整張圖失敗
        warnings.append(f"INSERT {p[-1]}（{name!r}）展開失敗：{type(ex).__name__}: {ex}")


def entity_lines(e: Any, flat: float) -> list[tuple[list[tuple[float, float]], bool]]:
    """曲線實體 → [(座標串, 是否封閉)]（WCS 2D）。"""
    from ezdxf import path as ezpath

    try:
        pth = ezpath.make_path(e)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for sp in pth.sub_paths():
        pts = [(float(v.x), float(v.y)) for v in sp.flattening(distance=flat)]
        dedup = [pts[0]] if pts else []
        for q in pts[1:]:
            if q != dedup[-1]:
                dedup.append(q)
        if len(dedup) < 2:
            continue
        closed = len(dedup) > 2 and math.dist(dedup[0], dedup[-1]) == 0.0
        out.append((dedup, closed))
    return out


def entity_point(e: Any) -> tuple[float, float] | None:
    t = e.dxftype()
    try:
        if t == "POINT":
            v = e.dxf.location
            return float(v.x), float(v.y)
        if t == "CIRCLE":
            v = e.ocs().to_wcs(e.dxf.center)
            return float(v.x), float(v.y)
        if t == "INSERT":
            v = e.ocs().to_wcs(e.dxf.insert)
            return float(v.x), float(v.y)
    except Exception:  # noqa: BLE001
        return None
    return None


def _text_of(e: Any) -> str:
    try:
        return e.plain_text()
    except Exception:  # noqa: BLE001
        return str(e.dxf.get("text", ""))


def _text_point(e: Any) -> tuple[float, float]:
    if e.dxftype() == "MTEXT":
        v = e.dxf.insert
    else:
        v = e.ocs().to_wcs(e.dxf.insert)
    return float(v.x), float(v.y)


# ---------------------------------------------------------------- 收集


@dataclass
class Curve:
    layer: str
    handles: str  # handle 路徑，"A>B"
    coords: list[tuple[float, float]]
    closed: bool


@dataclass
class Collected:
    curves: dict[str, list[Curve]] = field(default_factory=dict)  # role → curves
    points: list[tuple[str, str, tuple[float, float]]] = field(default_factory=list)  # (layer, handles, xy)
    background: list[list[tuple[float, float]]] = field(default_factory=list)  # 其他圖層（疊圖用）
    region: Polygon | None = None
    insunits: int = 0
    warnings: list[str] = field(default_factory=list)
    n_filtered_out: int = 0


def _layer_roles(spec: ArchImportSpec) -> dict[str, str]:
    out = {}
    for role in ("exterior_wall", "basement", "open_space", "roof_drains"):
        for name in getattr(spec.layers, role):
            out[name] = role
    return out


def floor_region(spec: ArchImportSpec, texts: list[tuple[str, tuple[float, float]]]) -> Polygon | None:
    ff = spec.floor_filter
    if ff is None:
        return None
    if ff.bbox is not None:
        return box(*ff.bbox)
    exact = [(t, xy) for t, xy in texts if t.strip() == ff.title_text.strip()]
    hits = exact or [(t, xy) for t, xy in texts if ff.title_text in t]
    if not hits:
        raise ValueError(f"floor_filter：圖面上找不到含 {ff.title_text!r} 的文字（TEXT / MTEXT）")
    if len({xy for _, xy in hits}) > 1:
        raise ValueError(
            f"floor_filter：含 {ff.title_text!r} 的文字有 {len(hits)} 處 {[t for t, _ in hits][:5]}，"
            "請改用更精確的 title_text 或 bbox"
        )
    x, y = hits[0][1]
    r = ff.search_radius
    return box(x - r, y - r, x + r, y + r)


def collect(spec: ArchImportSpec, keep_background: bool = False) -> Collected:
    """依 spec 讀 DXF、展開圖塊、套 floor_filter，按角色收集曲線與點。"""
    path = spec.dxf_path()
    if not path.exists():
        raise FileNotFoundError(f"source_dxf 不存在：{spec.source_dxf}")
    doc, warnings = load_doc(path)
    col = Collected(warnings=warnings, insunits=int(doc.header.get("$INSUNITS", 0) or 0))
    for blk in doc.blocks:
        if blk.block.is_xref or blk.block.is_xref_overlay:
            xp = str(blk.block.dxf.get("xref_path", "") or "")
            if not xp or not (path.parent / xp).exists():
                col.warnings.append(f"外部參照 {blk.name!r}（{xp or '無路徑'}）找不到檔案，未載入；若外牆畫在 xref 裡請先 bind")
    roles = _layer_roles(spec)
    flat = spec.join_tol / 4.0
    ents = list(walk_entities(doc, col.warnings))
    texts = [(_text_of(e), _text_point(e)) for e, _, _ in ents if e.dxftype() in TEXT_TYPES]
    col.region = floor_region(spec, texts)
    region = col.region.buffer(spec.join_tol) if col.region is not None else None
    present = {lay for _, lay, _ in ents}
    for name, role in roles.items():
        if name not in present:
            col.warnings.append(f"圖層 {name!r}（{role}）在 DXF 中沒有任何實體")
    wf = spec.exterior_wall_filter
    drain_inserts: set[tuple[str, ...]] = set()
    for e, layer, hp in ents:
        t = e.dxftype()
        role = roles.get(layer)
        handles = ">".join(hp)
        if role == "exterior_wall" and wf is not None and not _match_filter(e, wf):
            role = None
        dx, dy = spec.offsets.get(role, (0.0, 0.0)) if role else (0.0, 0.0)
        if any(hp[:n] in drain_inserts for n in range(1, len(hp))):
            continue  # 落水頭圖塊內的圖形不另算
        if role == "roof_drains":
            if t == "INSERT":
                drain_inserts.add(hp)
            if t in POINT_TYPES:
                xy = entity_point(e)
                if xy is not None and (region is None or region.contains(Point(xy))):
                    col.points.append((layer, handles, (xy[0] + dx, xy[1] + dy)))
                elif xy is not None:
                    col.n_filtered_out += 1
            continue
        if t not in CURVE_TYPES:
            continue
        for coords, closed in entity_lines(e, flat):
            if region is not None and not region.contains(LineString(coords)):
                if role is not None:
                    col.n_filtered_out += 1
                continue
            if role is None:
                if keep_background:
                    col.background.append(coords)
                continue
            if dx or dy:
                coords = [(x + dx, y + dy) for x, y in coords]
            col.curves.setdefault(role, []).append(Curve(layer, handles, coords, closed))
    return col


def _match_filter(e: Any, f: Any) -> bool:
    if f.color is not None and e.dxf.get("color", 256) != f.color:
        return False
    return not (f.lineweight is not None and e.dxf.get("lineweight", -1) != f.lineweight)


# ---------------------------------------------------------------- 接線 / 多邊形


@dataclass
class PolyResult:
    polygons: list[Polygon]
    method: str
    seams: list[dict[str, float]]  # 每個非零接縫 {d, x, y}
    dropped_small: int = 0

    @property
    def seam_max(self) -> float:
        return max((s["d"] for s in self.seams), default=0.0)


class _UF:
    def __init__(self, n: int) -> None:
        self.p = list(range(n))

    def find(self, i: int) -> int:
        while self.p[i] != i:
            self.p[i] = self.p[self.p[i]]
            i = self.p[i]
        return i

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def snap_lines(lines: list[tuple[list[tuple[float, float]], bool]], tol: float
               ) -> tuple[list[list[tuple[float, float]]], list[dict[str, float]]]:
    """端點在 tol 內群聚合併；懸空端點在 tol 內碰到其他線 → 拉過去並微幅延伸。回傳 (折線串, 接縫)。"""
    coords = [list(c) for c, _ in lines]
    closed = [cl for _, cl in lines]
    ep_idx = [(i, end) for i, cl in enumerate(closed) if not cl for end in (0, -1)]
    seams: list[dict[str, float]] = []
    if not ep_idx:
        return coords, seams
    pts = np.array([coords[i][end] for i, end in ep_idx], dtype=float)
    geoms = [Point(p) for p in pts]
    tree = STRtree(geoms)
    a, b = tree.query(geoms, predicate="dwithin", distance=tol)
    uf = _UF(len(pts))
    for i, j in zip(a, b, strict=True):
        if i < j:
            uf.union(int(i), int(j))
    clusters: dict[int, list[int]] = {}
    for k in range(len(pts)):
        clusters.setdefault(uf.find(k), []).append(k)
    single: list[int] = []
    for members in clusters.values():
        if len(members) == 1:
            single.append(members[0])
            continue
        cp = pts[members]
        c = cp.mean(axis=0)
        d = max(float(np.hypot(*(cp[u] - cp[v]))) for u in range(len(cp)) for v in range(u + 1, len(cp)))
        if d > 0:
            seams.append({"d": d, "x": float(c[0]), "y": float(c[1])})
        for k in members:
            i, end = ep_idx[k]
            coords[i][end] = (float(c[0]), float(c[1]))
    # 懸空端點 → 拉到最近的其他線上（T 接）
    line_geoms = [LineString(c) for c in coords]
    ltree = STRtree(line_geoms)
    ext = tol * 0.01
    for k in single:
        i, end = ep_idx[k]
        p = Point(coords[i][end])
        cand = [int(j) for j in ltree.query(p, predicate="dwithin", distance=tol) if int(j) != i]
        if not cand:
            continue
        j = min(cand, key=lambda jj: line_geoms[jj].distance(p))
        d = line_geoms[j].distance(p)
        q = line_geoms[j].interpolate(line_geoms[j].project(p))
        prev = np.array(coords[i][1] if end == 0 else coords[i][-2], dtype=float)
        cur = np.array([q.x, q.y])
        v = cur - prev
        n = float(np.hypot(*v))
        if n == 0:
            continue
        new = cur + v / n * ext
        coords[i][end] = (float(new[0]), float(new[1]))
        if d > 0:
            seams.append({"d": float(d), "x": float(q.x), "y": float(q.y)})
    return coords, seams


def _parts(g: Any) -> list[Polygon]:
    if g.is_empty:
        return []
    if g.geom_type == "Polygon":
        return [g]
    return [p for p in getattr(g, "geoms", []) if p.geom_type == "Polygon" and not p.is_empty]


def _fill(polys: list[Polygon]) -> list[Polygon]:
    """聯集後取外環（填洞），回傳分離的外框。"""
    u = unary_union(polys)
    return _parts(unary_union([Polygon(p.exterior) for p in _parts(u)]))


def _closing(pieces: list[Any], r: float) -> list[Polygon]:
    """形態學閉運算（mitre 接角）：外推 r → 填洞 → 內縮 r。寬 ≤ 2r 的開口被橋接，直邊與轉角原樣還原。"""
    kw = {"join_style": "mitre", "mitre_limit": MITRE_LIMIT}
    grown = unary_union(pieces).buffer(r, **kw)
    filled = unary_union([Polygon(p.exterior) for p in _parts(grown)])
    return _parts(filled.buffer(-r, **kw))


def build_polygons(curves: list[Curve], tol: float, mode: str, opening: float, min_area: float) -> PolyResult:
    """曲線（圖上座標）→ 封閉外框（圖上座標）。outline / double_line 見模組說明。"""
    if not curves:
        return PolyResult([], "none", [])
    lines, seams = snap_lines([(c.coords, c.closed) for c in curves], tol)
    noded = unary_union(MultiLineString([ln for ln in lines if len(ln) >= 2]))
    res = PolyResult([], mode, seams)
    faces = list(polygonize(getattr(noded, "geoms", [noded])))
    polys = _fill(faces)
    res.method = "polygonize"
    if mode == "double_line":
        covered = len(polys) == 1 and noded.difference(polys[0].buffer(tol)).length <= tol
        if not covered:
            # 牆段面（有封口線時）+ 牆線細帶（沒有封口線的開放雙線也能被外推接起來）
            # 只對不屬於任何牆段面的線（沒畫封口線的開放雙線）加細帶；已在面邊界上的線不加，避免外緣出現細小台階
            eps = tol * 0.01
            free = noded.difference(unary_union(faces).buffer(eps)) if faces else noded
            pieces = list(faces)
            if not free.is_empty:
                pieces.append(free.buffer(eps, cap_style="flat", join_style="mitre", mitre_limit=MITRE_LIMIT))
            polys = _closing(pieces, opening / 2.0)
            res.method = "closing"
    keep = [p for p in polys if p.area >= min_area]
    res.dropped_small = len(polys) - len(keep)
    res.polygons = [orient(p.simplify(0), 1.0) for p in sorted(keep, key=lambda p: -p.area)]
    return res


def role_polygons(spec: ArchImportSpec, col: Collected) -> dict[str, tuple[PolyResult, list[Curve]]]:
    """每個面角色 → (PolyResult, 該角色曲線)。"""
    min_area = spec.min_area_m2 / spec.unit_to_m**2
    out = {}
    for role in ("exterior_wall", "basement", "open_space"):
        curves = col.curves.get(role, [])
        if not curves:
            continue
        mode = "outline" if role == "open_space" else spec.wall_representation
        out[role] = (build_polygons(curves, spec.join_tol, mode, spec.opening_bridge, min_area), curves)
    return out


def contributing_handles(poly: Polygon, curves: list[Curve], tol: float) -> list[str]:
    zone = poly.buffer(tol)
    hs = {c.handles for c in curves if LineString(c.coords).intersects(zone)}
    return sorted(hs)


# ---------------------------------------------------------------- 相似變換


def fit_similarity(src: list[tuple[float, float]], dst: list[tuple[float, float]]) -> dict[str, Any]:
    """最小平方相似變換 w = a·z + b（複數；a = s·e^{iθ}）。src 已換算成 m。"""
    z = np.array([complex(*p) for p in src])
    w = np.array([complex(*p) for p in dst])
    zc, wc = z - z.mean(), w - w.mean()
    den = float(np.sum(np.abs(zc) ** 2))
    if den == 0:
        raise ValueError("控制點圖上座標全部重合，無法擬合轉換")
    a = complex(np.sum(wc * np.conj(zc)) / den)
    b = complex(w.mean() - a * z.mean())
    fitted = a * z + b
    resid = np.abs(fitted - w)
    return {
        "a": a.real, "b": a.imag, "tx": b.real, "ty": b.imag,
        "scale": abs(a), "rotation_deg": math.degrees(math.atan2(a.imag, a.real)),
        "fitted": [(f.real, f.imag) for f in fitted],
        "residuals_m": [float(r) for r in resid],
    }


def affine_params(spec: ArchImportSpec, fit: dict[str, Any]) -> list[float]:
    """圖上座標 → TWD97 的 shapely affine 參數 [a, b, d, e, xoff, yoff]。"""
    k = spec.unit_to_m
    a, b = fit["a"], fit["b"]
    return [a * k, -b * k, b * k, a * k, fit["tx"], fit["ty"]]


def fit_spec(spec: ArchImportSpec) -> dict[str, Any]:
    k = spec.unit_to_m
    src = [(c.drawing[0] * k, c.drawing[1] * k) for c in spec.control_points]
    dst = [tuple(c.twd97) for c in spec.control_points]
    return fit_similarity(src, dst)


# ---------------------------------------------------------------- 輸出


def run_extract(spec: ArchImportSpec, out_dir: Path) -> dict[str, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    col = collect(spec, keep_background=True)
    polys = role_polygons(spec, col)
    fit = fit_spec(spec)
    M = affine_params(spec, fit)
    k = spec.unit_to_m
    tol = spec.join_tol
    warnings = list(col.warnings)

    features = []
    for role, (pr, curves) in polys.items():
        if pr.dropped_small:
            warnings.append(f"{role}：{pr.dropped_small} 個小於 {spec.min_area_m2} m² 的封閉面視為雜線略過")
        for i, p in enumerate(pr.polygons):
            g = orient(affine_transform(p, M), 1.0)
            features.append({
                "type": "Feature",
                "geometry": mapping(g),
                "properties": {
                    "role": POLY_ROLES[role],
                    "index": i,
                    "source_layers": sorted({c.layer for c in curves}),
                    "source_handles": contributing_handles(p, curves, tol),
                    "area_m2": round(g.area, 3),
                    "area_ping": round(g.area * PING_PER_M2, 3),
                    "perimeter_m": round(g.length, 3),
                    "method": pr.method,
                },
            })
        if not pr.polygons:
            warnings.append(f"{role}：沒有抽出任何封閉外框（縫隙 > join_tolerance 或圖層 / floor_filter 選錯？）")
    for layer, handles, xy in col.points:
        g = affine_transform(Point(xy), M)
        features.append({
            "type": "Feature",
            "geometry": mapping(g),
            "properties": {"role": "roof_drain", "source_layers": [layer], "source_handles": [handles]},
        })
    gj = {
        "type": "FeatureCollection",
        "name": spec.title or "arch-import",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::3826"}},
        "features": features,
    }
    gj_path = out_dir / "arch.geojson"
    gj_path.write_text(json.dumps(gj, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    seams = {role: pr.seams for role, (pr, _) in polys.items()}
    seam_max = max((pr.seam_max for pr, _ in polys.values()), default=0.0)
    tr = {
        "crs": spec.crs,
        "drawing_units": spec.drawing_units,
        "unit_to_m": k,
        "insunits_header": col.insunits,
        "model": "similarity (least squares: translation + rotation + uniform scale)",
        "affine": M,
        "scale": fit["scale"],
        "rotation_deg": fit["rotation_deg"],
        "translation": [fit["tx"], fit["ty"]],
        "control_points": [
            {"id": c.id, "drawing": list(c.drawing), "twd97": list(c.twd97),
             "fitted": list(f), "residual_m": r}
            for c, f, r in zip(spec.control_points, fit["fitted"], fit["residuals_m"], strict=True)
        ],
        "max_residual_m": max(fit["residuals_m"]),
        "join_tolerance": tol,
        "seam_max_drawing": seam_max,
        "seam_max_m": seam_max * k,
        "seams": {r: sorted(s, key=lambda x: -x["d"])[:50] for r, s in seams.items()},
        "methods": {r: {"method": pr.method,
                        "polygons": len(pr.polygons)} for r, (pr, _) in polys.items()},
        "floor_region": list(col.region.bounds) if col.region is not None else None,
        "filtered_out": col.n_filtered_out,
        "warnings": warnings,
    }
    tr_path = out_dir / "transform.json"
    tr_path.write_text(json.dumps(tr, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    png = out_dir / "overlay.png"
    try:
        _overlay_png(png, col, polys, M, spec)
    except Exception as e:  # noqa: BLE001 - 疊圖只供人眼檢查
        tr["warnings"].append(f"疊圖 PNG 失敗：{type(e).__name__}: {e}")
        tr_path.write_text(json.dumps(tr, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return {"geojson": gj_path, "transform": tr_path}
    return {"geojson": gj_path, "transform": tr_path, "overlay_png": png}


def _overlay_png(png: Path, col: Collected, polys: dict, M: list[float], spec: ArchImportSpec) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import LineCollection

    def tf(coords: list[tuple[float, float]]) -> np.ndarray:
        a = np.asarray(coords, dtype=float)
        return np.column_stack([M[0] * a[:, 0] + M[1] * a[:, 1] + M[4], M[2] * a[:, 0] + M[3] * a[:, 1] + M[5]])

    fig, ax = plt.subplots(figsize=(8, 8), dpi=120)
    bg = [tf(c) for c in col.background] + [tf(c.coords) for cs in col.curves.values() for c in cs]
    if bg:
        ax.add_collection(LineCollection(bg, colors="#c8c8c8", linewidths=0.4))
    colors = {"exterior_wall": "#d62728", "basement": "#1f77b4", "open_space": "#2ca02c"}
    for role, (pr, _) in polys.items():
        for p in pr.polygons:
            xy = tf(list(p.exterior.coords))
            ax.plot(xy[:, 0], xy[:, 1], color=colors[role], lw=1.6)
    for _, _, xy in col.points:
        q = tf([xy])
        ax.plot(q[:, 0], q[:, 1], "o", color="#9467bd", ms=4)
    for c in spec.control_points:
        ax.plot(*c.twd97, "^", color="black", ms=6)
        ax.annotate(c.id, c.twd97, fontsize=7)
    if spec.site is not None:
        try:
            from draftagent.verify.arch_check import load_site

            s = load_site(spec.site)
            for p in _parts(s):
                xy = np.asarray(p.exterior.coords)
                ax.plot(xy[:, 0], xy[:, 1], "--", color="black", lw=0.8)
        except Exception:  # noqa: BLE001
            pass
    ax.set_aspect("equal")
    ax.autoscale_view()
    ax.ticklabel_format(useOffset=False, style="plain")
    ax.tick_params(labelsize=6)
    ax.set_title(f"arch-import  EPSG:3826  ({spec.source_dxf.name})", fontsize=8)
    fig.tight_layout()
    fig.savefig(png)
    plt.close(fig)
