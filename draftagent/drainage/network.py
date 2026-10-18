"""排水網路拓樸與幾何量（networkx DiGraph）。純函式、確定性，不得呼叫 LLM。

圖：節點 = spec.nodes；邊 = links（kind="link"）+ 滯洪設施 inlet→outlet（kind="storage"，
不是管溝，但水流經過，拓樸上必須連通）。

`check_network(spec)` 檢查
- 恰好 1 個 outfall 節點，且 outfall 無下游
- 無迴圈；無孤立節點；無重複管段（同一對 from→to）
- 除 outfall 外每個節點都有下游、都可達 outfall
- link 長度（座標算）> 0；坡度（invert 算）S = (上游底 − 下游底) / L：
  S < 0 且非 pump → error（逆坡）；S = 0 → warning
- 上游累加面積：節點承接面積 = Σ outlet_node 為該節點或其上游節點之分區面積；
  同時累加 Σ C·A，link 設計逕流係數 = 上游集水分區面積加權 C = Σ(Cᵢ·Aᵢ) / ΣAᵢ
  （Cᵢ = c_override 或 yaml 中值；管渠設計流量用此 C，不用全基地綜合 C）
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import networkx as nx
from shapely.geometry import Polygon

from draftagent.drainage.model import DrainageNetworkSpec, Link
from draftagent.hydro.taoyuan import load_codes, runoff_c
from draftagent.schema.profile import format_station

__all__ = [
    "NetworkReport",
    "build_graph",
    "check_network",
    "default_profile_path",
    "format_station",
    "link_length",
    "link_slope",
    "path_links",
    "site_area",
    "station_texts",
    "stationing",
    "subcatchment_areas",
]


@dataclass
class NetworkReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    outfall: str | None = None
    lengths: dict[str, float] = field(default_factory=dict)  # link id → m
    slopes: dict[str, float] = field(default_factory=dict)  # link id → m/m（順坡為正）
    node_area_m2: dict[str, float] = field(default_factory=dict)  # 上游累加面積
    link_area_m2: dict[str, float] = field(default_factory=dict)  # link 承接面積（= from 節點）
    node_ca_m2: dict[str, float] = field(default_factory=dict)  # 上游累加 Σ C·A
    link_c: dict[str, float] = field(default_factory=dict)  # link 上游面積加權 C（承接面積 0 → 不列）

    @property
    def ok(self) -> bool:
        return not self.errors


def link_length(spec: DrainageNetworkSpec, link: Link) -> float:
    a, b = spec.node(link.from_), spec.node(link.to)
    return math.hypot(b.x - a.x, b.y - a.y)


def link_slope(spec: DrainageNetworkSpec, link: Link) -> float:
    """S（m/m）= (上游 invert − 下游 invert) / 長度；順坡為正。"""
    a, b = spec.node(link.from_), spec.node(link.to)
    L = link_length(spec, link)
    return (a.invert_el - b.invert_el) / L if L > 0 else float("nan")


def subcatchment_areas(spec: DrainageNetworkSpec) -> dict[str, float]:
    return {s.id: Polygon(s.polygon).area for s in spec.subcatchments}


def site_area(spec: DrainageNetworkSpec) -> float:
    return Polygon(spec.site_boundary).area


def build_graph(spec: DrainageNetworkSpec) -> nx.DiGraph:
    g = nx.DiGraph()
    for n in spec.nodes:
        g.add_node(n.id, kind=n.kind)
    for lk in spec.links:
        g.add_edge(lk.from_, lk.to, kind="link", id=lk.id)
    if spec.storage is not None:
        st = spec.storage
        if not g.has_edge(st.inlet_node, st.outlet_node):
            g.add_edge(st.inlet_node, st.outlet_node, kind="storage", id=st.id)
    return g


def check_network(spec: DrainageNetworkSpec) -> NetworkReport:
    rep = NetworkReport()
    g = build_graph(spec)

    pairs = [(lk.from_, lk.to) for lk in spec.links]
    dup = sorted({p for p in pairs if pairs.count(p) > 1})
    if dup:
        rep.errors.append(f"重複管段（同一 from→to）：{dup}")

    outfalls = [n.id for n in spec.nodes if n.kind == "outfall"]
    if len(outfalls) != 1:
        rep.errors.append(
            f"出流口（kind=outfall）必須恰好 1 個，目前 {len(outfalls)} 個：{outfalls}"
        )
    if outfalls:
        rep.outfall = outfalls[0]
        for of in outfalls:
            if g.out_degree(of) > 0:
                rep.errors.append(f"出流口 {of} 不得有下游管段：{list(g.successors(of))}")

    for n in g.nodes:
        if g.degree(n) == 0:
            rep.errors.append(f"節點 {n} 沒有連接任何管段")

    cycles = list(nx.simple_cycles(g))
    for c in cycles:
        rep.errors.append(f"網路有迴圈：{' → '.join([*c, c[0]])}")

    for n in g.nodes:
        if n in outfalls:
            continue
        if g.out_degree(n) == 0 and g.degree(n) > 0:
            rep.errors.append(f"節點 {n} 沒有下游（不是出流口）")
        if rep.outfall is not None and not nx.has_path(g, n, rep.outfall):
            rep.errors.append(f"節點 {n} 無法流到出流口 {rep.outfall}")

    for lk in spec.links:
        L = link_length(spec, lk)
        rep.lengths[lk.id] = L
        if L <= 0:
            rep.errors.append(f"管段 {lk.id} 長度為 0（{lk.from_} 與 {lk.to} 座標相同）")
            continue
        s = link_slope(spec, lk)
        rep.slopes[lk.id] = s
        a, b = spec.node(lk.from_), spec.node(lk.to)
        if s < 0 and not lk.pump:
            rep.errors.append(
                f"逆坡：{lk.id} {lk.from_}→{lk.to} 底 {a.invert_el:.2f} → {b.invert_el:.2f}"
                f"（S={s * 100:.2f}%），非抽水段（pump=false）"
            )
        elif s == 0:
            rep.warnings.append(f"{lk.id} 坡度 0%")

    if spec.storage is not None:
        st = spec.storage
        if any((lk.from_, lk.to) == (st.inlet_node, st.outlet_node) for lk in spec.links):
            rep.warnings.append(f"滯洪設施 {st.id} inlet→outlet 另有管段，拓樸以管段為準")

    if not cycles:
        codes = load_codes(spec.codes_path)
        areas = subcatchment_areas(spec)
        by_node: dict[str, float] = {}
        ca_node: dict[str, float] = {}
        for s in spec.subcatchments:
            c = runoff_c(s.landuse, s.c_override, codes)
            by_node[s.outlet_node] = by_node.get(s.outlet_node, 0.0) + areas[s.id]
            ca_node[s.outlet_node] = ca_node.get(s.outlet_node, 0.0) + c * areas[s.id]
        for n in g.nodes:
            ups = nx.ancestors(g, n) | {n}
            rep.node_area_m2[n] = sum(by_node.get(u, 0.0) for u in ups)
            rep.node_ca_m2[n] = sum(ca_node.get(u, 0.0) for u in ups)
        for lk in spec.links:
            rep.link_area_m2[lk.id] = rep.node_area_m2[lk.from_]
            if rep.node_area_m2[lk.from_] > 0:
                rep.link_c[lk.id] = rep.node_ca_m2[lk.from_] / rep.node_area_m2[lk.from_]
    return rep


def _link_between(spec: DrainageNetworkSpec, a: str, b: str) -> Link | None:
    return next((lk for lk in spec.links if lk.from_ == a and lk.to == b), None)


def path_links(spec: DrainageNetworkSpec, path: list[str]) -> list[Link]:
    """路徑相鄰節點間的 link（依流向）；任一段沒有 link → ValueError。"""
    out = []
    for a, b in zip(path, path[1:], strict=False):
        lk = _link_between(spec, a, b)
        if lk is None:
            raise ValueError(f"路徑 {a}→{b} 之間沒有管段（滯洪設施不是管段，縱斷面須在其前後分開）")
        out.append(lk)
    return out


def stationing(spec: DrainageNetworkSpec, path: list[str], origin: str = "upstream") -> list[float]:
    """路徑各節點樁號 m。origin="upstream"：第一點 0 往下游遞增；"outfall"：最後一點 0 往上游反推。"""
    links = path_links(spec, path)
    cum = [0.0]
    for lk in links:
        cum.append(cum[-1] + link_length(spec, lk))
    if origin == "upstream":
        return cum
    if origin == "outfall":
        return [cum[-1] - c for c in cum]
    raise ValueError(f"origin 必須是 upstream 或 outfall，不是 {origin!r}")


def default_profile_path(spec: DrainageNetworkSpec) -> list[str]:
    """縱斷面預設路徑：只走新設管段（existing=false）的最長連續路徑（長度加權）。"""
    g = nx.DiGraph()
    for lk in spec.links:
        if not lk.existing:
            g.add_edge(lk.from_, lk.to, weight=link_length(spec, lk))
    if g.number_of_edges() == 0:
        raise ValueError("沒有新設管段可畫縱斷面")
    if not nx.is_directed_acyclic_graph(g):
        raise ValueError("管段網路有迴圈，無法決定縱斷面路徑")
    return list(nx.dag_longest_path(g, weight="weight"))


def station_texts(stations: list[float]) -> list[str]:
    return [format_station(s) for s in stations]
