"""從排水網路自動衍生其他圖說 / 計算的輸入，讓所有圖面與計算書數字同源。

純函式、確定性，不得呼叫 LLM。

- `derive_profile(spec)` → `ProfileSpec`：縱斷面的樁號（座標算）、高程（節點）、斷面與平面圖同一來源，
  消除「平面圖與縱斷面數字對不上」的審查缺失。levels 另含滯洪設施明確給定的水位
  （設計滿水位、溢流口、溢流時最高水位（池內溢流）、孔口底、起抽 / 停機水位；`hydraulics.storage_profile_levels`）與
  路徑終點為出流口時的受納溝設計水位。
- `derive_calc` 依 storage.drain_mode：pump → release_mode=pump（排放量 = pump_q_cms），其餘用孔口。
- `derive_calc(spec)` → `TaoyuanSiteDrainageSpec`：基地 / 分區面積由 polygon（shapely）算，
  C 由 landuse（yaml），管渠坡度由 invert、承接面積由網路上游累加。
  每條管渠的 `design_c` = 上游集水分區面積加權 C（`hydro.taoyuan.compute` 用它算管渠設計流量，
  不用全基地綜合 C）。
- `pipe_design_c(spec)`：納入管渠檢核的 link → 上游集水分區面積加權 C（與 derive_calc 寫入的 design_c 相同）。
- `quantities(spec)`：設施尺寸數量一覽表的列（數量由模型算）。
- `catchment_rows(spec)`：集水分區面積表的列。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from shapely.geometry import LineString, Point, Polygon

from draftagent.drainage import sheet_vocab as V
from draftagent.drainage.hydraulics import (
    RECEIVE_MAX_DIST_M,
    receiving_point,
    resolve_storage,
    storage_profile_levels,
)
from draftagent.drainage.model import NODE_KIND_NAME, DrainageNetworkSpec, Link
from draftagent.drainage.network import (
    build_graph,
    check_network,
    default_profile_path,
    link_length,
    link_slope,
    path_links,
    stationing,
    subcatchment_areas,
)
from draftagent.hydro.drawing_codes import load_drawing_codes
from draftagent.hydro.taoyuan import PING_PER_M2, load_codes, runoff_c
from draftagent.schema.detail import DetailSheetSpec
from draftagent.schema.drainage import Pipe, Storage, StorageCell, Subarea, TaoyuanSiteDrainageSpec
from draftagent.schema.profile import (
    ProfileLevel,
    ProfileNode,
    ProfileSegment,
    ProfileSpec,
    ProfileStructure,
    StructureMark,
)

M2_PER_HA = 10_000.0
PROFILE_KIND = {
    "catch_basin": "catch_pit",
    "manhole": "manhole",
    "outlet_well": "outlet_pit",
    "storage_inlet": "inlet",
    "outfall": "outfall",
    "existing_mh": "other",
}
SECTION_ITEM = {"u_ditch": "U型溝", "pipe": "排水管", "box": "箱涵"}
STORAGE_ITEM = {"raft": "筏基滯洪槽", "surface": "地面滯洪池"}
NODE_UNIT = {"outfall": "處"}


# ---------------------------------------------------------------- 縱斷面


def road_el_at(spec: DrainageNetworkSpec, x: float, y: float) -> float | None:
    """節點投影到道路中心線（road_centerline_els 折線）上的內插高程；不足 2 點 → None。"""
    pts = spec.road_centerline_els
    if len(pts) < 2:
        return None
    line = LineString([(p.x, p.y) for p in pts])
    d = line.project(Point(x, y))
    cum = [0.0]
    for a, b in zip(pts, pts[1:], strict=False):
        cum.append(cum[-1] + LineString([(a.x, a.y), (b.x, b.y)]).length)
    for i in range(1, len(cum)):
        if d <= cum[i] or i == len(cum) - 1:
            seg = cum[i] - cum[i - 1]
            t = 0.0 if seg == 0 else min(max((d - cum[i - 1]) / seg, 0.0), 1.0)
            return pts[i - 1].el + t * (pts[i].el - pts[i - 1].el)
    return None  # pragma: no cover


def link_n(spec: DrainageNetworkSpec, lk: Link) -> float:
    m = load_codes(spec.codes_path)["manning_n"]["materials"][lk.material]
    return float(lk.n_override) if "n_range" in m else float(m["n"])


def profile_path(spec: DrainageNetworkSpec) -> list[str]:
    return list(spec.profile_path) if spec.profile_path else default_profile_path(spec)


def _project_range(
    spec: DrainageNetworkSpec, path: list[str], st: list[float], anchor: str, coords
) -> tuple[float, float]:
    """polygon 投影到路徑在 anchor 節點處的流向，回傳樁號範圍 (min, max)。"""
    i = path.index(anchor)
    a, b = (path[i - 1], path[i]) if i > 0 else (path[0], path[1])
    na, nb = spec.node(a), spec.node(b)
    L = math.hypot(nb.x - na.x, nb.y - na.y)
    ux, uy = (nb.x - na.x) / L, (nb.y - na.y) / L
    an = spec.node(anchor)
    vals = [st[i] + (x - an.x) * ux + (y - an.y) * uy for x, y in coords]
    return min(vals), max(vals)


def _storage_structure(
    spec: DrainageNetworkSpec,
    path: list[str],
    st: list[float],
    anchor: str,
    *,
    riser_to_outlet: bool = False,
) -> ProfileStructure | None:
    """滯洪設施構造物（池底 / 池頂須明確給定；推導值不上圖）。"""
    sto = spec.storage
    lv, _, _ = resolve_storage(spec)
    if sto is None or lv is None or "bottom_el" in lv.assumed or "top_el" in lv.assumed:
        return None
    x0, x1 = _project_range(spec, path, st, anchor, sto.polygon)
    marks: list[StructureMark] = []
    for label, el, kind in storage_profile_levels(spec):
        if label == "溢流口" and sto.overflow_at == "inlet_well":
            continue  # 溢流口在進流井，另以 level 標在進流井
        if label == "孔口底":
            kind = "other"
        marks.append(StructureMark(label=label, el=el, kind=kind))
    if sto.inflow_invert_el is not None:
        marks.append(StructureMark(label="入池進流管底", el=sto.inflow_invert_el, kind="pipe_in"))
    pump_st = pump_el = None
    if sto.drain_mode in ("pump", "gravity_pump"):
        sump = sto.outlet_well_polygon or sto.polygon
        c = Polygon(sump).centroid
        pump_st = _project_range(spec, path, st, anchor, [(c.x, c.y)])[0]
        pump_el = lv.pump_outlet_invert_el if riser_to_outlet else None
    return ProfileStructure(
        kind="detention",
        label=sto.id,
        x0_m=x0,
        x1_m=x1,
        bottom_el=lv.bottom_el,
        top_el=lv.top_el,
        marks=marks,
        pump_station_m=pump_st,
        pump_outlet_el=pump_el,
    )


def _profile_nodes(spec: DrainageNetworkSpec, path: list[str], st: list[float]):
    nodes = []
    for nid, s in zip(path, st, strict=True):
        n = spec.node(nid)
        nodes.append(
            ProfileNode(
                station_m=s,
                label=n.id,
                kind=PROFILE_KIND[n.kind],
                ground_el=n.ground_el,
                invert_el=n.invert_el,
                top_el=n.top_el,
                road_center_el=road_el_at(spec, n.x, n.y),
            )
        )
    segments = [
        ProfileSegment(
            section=lk.section.text(), size_m=lk.section.size_m, n=link_n(spec, lk), pump=lk.pump
        )
        for lk in path_links(spec, path)
    ]
    return nodes, segments


def _profile_sheet(spec: DrainageNetworkSpec, which: str):
    rule = load_drawing_codes().scale("drainage_profile")
    tb = spec.sheet.title_block.model_copy(
        update={"drawing_title": rule.name, "drawing_no": spec.drawing_no(which)}
    )
    return rule, spec.sheet.model_copy(update={"title_block": tb, "north_arrow": False})


def derive_profile(spec: DrainageNetworkSpec, path: list[str] | None = None) -> ProfileSpec:
    """主幹線縱斷面（上游 → 進流井）。

    滯洪設施池底 / 池頂明確給定時，以構造物（池體矩形 + 有效水深陰影 + 水位線）畫在進流井（或放流井）
    樁號處，範圍 = 滯洪設施 polygon 投影到路徑流向；否則沿用舊行為（水位以 levels 標在該樁號）。
    """
    path = path or profile_path(spec)
    st = stationing(spec, path)
    on_path = set(path)
    nodes, segments = _profile_nodes(spec, path, st)

    levels = []
    for nid, s in zip(path[1:], st[1:], strict=True):  # 支線進水管
        for lk in spec.links:
            if lk.to == nid and lk.from_ not in on_path:
                levels.append(
                    ProfileLevel(
                        station_m=s,
                        el=spec.node(nid).invert_el,
                        label=f"{V.INLET_PIPE_PREFIX}{lk.id}",
                        kind="pipe_in",
                    )
                )
    last = spec.node(path[-1])
    if last.kind == "outfall":
        levels.append(
            ProfileLevel(station_m=st[-1], el=last.invert_el, label="放流管", kind="pipe_out")
        )
        rcv = receiving_point(spec, last.x, last.y)
        if rcv is not None and rcv.design_wl is not None and rcv.distance_m <= RECEIVE_MAX_DIST_M:
            levels.append(
                ProfileLevel(
                    station_m=st[-1], el=rcv.design_wl, label=V.RECEIVING_WL_LABEL, kind="design_wl"
                )
            )
    structures = []
    if spec.storage is not None:  # 滯洪設施：標在進流井（或放流井）樁號
        sto = spec.storage
        s_node = next((n for n in (sto.inlet_node, sto.outlet_node) if n in on_path), None)
        if s_node is not None:
            s_st = st[path.index(s_node)]
            struct = _storage_structure(spec, path, st, s_node)
            if struct is None:
                levels += [
                    ProfileLevel(station_m=s_st, el=el, label=label, kind=kind)
                    for label, el, kind in storage_profile_levels(spec)
                ]
            else:
                structures.append(struct)
                lv, _, _ = resolve_storage(spec)
                if (
                    sto.overflow_at == "inlet_well"
                    and s_node == sto.inlet_node
                    and "overflow_el" not in lv.assumed
                ):
                    levels.append(
                        ProfileLevel(
                            station_m=s_st,
                            el=lv.overflow_el,
                            label=V.OVERFLOW_AT_INLET_LABEL,
                            kind="overflow",
                        )
                    )

    rule, sheet = _profile_sheet(spec, "profile")
    return ProfileSpec(
        title=f"{rule.name}（{path[0]}→{path[-1]}）",
        line_name=f"{path[0]}→{path[-1]} 排水幹線",
        h_scale=spec.scales.profile_h,
        v_scale=spec.scales.profile_v,
        nodes=nodes,
        segments=segments,
        levels=levels,
        structures=structures,
        sheet=sheet,
    )


def outlet_path(spec: DrainageNetworkSpec) -> list[str] | None:
    """出水側路徑：滯洪設施放流井 → … → 出流口（只走管段）；無 storage / 無路徑 → None。"""
    import networkx as nx

    if spec.storage is None:
        return None
    g = nx.DiGraph()
    for lk in spec.links:
        g.add_edge(lk.from_, lk.to)
    start = spec.storage.outlet_node
    outfalls = [n.id for n in spec.nodes if n.kind == "outfall"]
    if start not in g or len(outfalls) != 1 or not nx.has_path(g, start, outfalls[0]):
        return None
    path = nx.shortest_path(g, start, outfalls[0])
    return path if len(path) >= 2 else None


def outlet_well_outer_m(spec: DrainageNetworkSpec) -> float:
    ow = spec.detail_defaults.outlet_well
    return (2 * ow.wall_t + ow.chamber_a_length + ow.partition_t + ow.chamber_b_length) / 1000.0


def derive_outlet_profile(spec: DrainageNetworkSpec) -> ProfileSpec | None:
    """出水側縱斷面：滯洪設施（池體 / 水位 / 抽水機）→ 放流井 → 出流口 → 受納側溝斷面。

    放流井以構造物畫實際外尺寸（= 詳圖預設外長），受納溝以 U 形斷面 + 設計水位畫在出流口樁號；
    放流井處標「抽水出水管」「溢流管」「放流管<link>」入 / 出井管底。
    """
    path = outlet_path(spec)
    if path is None:
        return None
    sto = spec.storage
    lv, _, _ = resolve_storage(spec)
    st = stationing(spec, path)
    nodes, segments = _profile_nodes(spec, path, st)
    ow = spec.node(path[0])
    levels: list[ProfileLevel] = []
    if lv.pump_outlet_invert_el is not None:
        levels.append(
            ProfileLevel(
                station_m=st[0], el=lv.pump_outlet_invert_el, label=V.PUMP_OUTLET_LABEL, kind="pipe_in"
            )
        )
    if lv.overflow_pipe_invert_el is not None:
        levels.append(
            ProfileLevel(
                station_m=st[0], el=lv.overflow_pipe_invert_el, label=V.OVERFLOW_PIPE_LABEL, kind="pipe_in"
            )
        )
    first = path_links(spec, path)[0]
    levels.append(
        ProfileLevel(
            station_m=st[0], el=ow.invert_el, label=f"{V.OUTLET_PIPE_PREFIX}{first.id}", kind="pipe_out"
        )
    )
    last = spec.node(path[-1])
    levels.append(ProfileLevel(station_m=st[-1], el=last.invert_el, label="放流管", kind="pipe_out"))

    structures: list[ProfileStructure] = []
    struct = _storage_structure(spec, path, st, path[0], riser_to_outlet=True)
    if struct is not None:
        structures.append(struct)
    half = outlet_well_outer_m(spec) / 2
    if ow.kind == "outlet_well":
        structures.append(
            ProfileStructure(
                kind="well",
                label=f"{V.OUTLET_WELL_PREFIX}{ow.id}",
                x0_m=st[0] - half,
                x1_m=st[0] + half,
                bottom_el=ow.invert_el,
                top_el=ow.top,
            )
        )
    rcv = receiving_point(spec, last.x, last.y)
    if rcv is not None and rcv.distance_m <= RECEIVE_MAX_DIST_M:
        drain = next(d for d in spec.existing_drains if d.id == rcv.drain_id)
        w = drain.section.w if hasattr(drain.section, "w") else drain.section.size_m
        marks = []
        if rcv.design_wl is not None:
            marks.append(StructureMark(label=V.RECEIVING_WL_LABEL, el=rcv.design_wl, kind="design_wl"))
        structures.append(
            ProfileStructure(
                kind="ditch",
                label=V.RECEIVING_LABEL,
                x0_m=st[-1],
                x1_m=st[-1] + w,
                bottom_el=rcv.invert_el,
                top_el=rcv.top_el,
                marks=marks,
            )
        )
    rule, sheet = _profile_sheet(spec, "profile_outlet")
    return ProfileSpec(
        title=f"{rule.name}（{sto.id}→{path[-1]} 出水側）",
        line_name=f"{path[0]}→{path[-1]} 出水側",
        h_scale=spec.scales.profile_outlet_h or spec.scales.profile_h,
        v_scale=spec.scales.profile_outlet_v or spec.scales.profile_v,
        nodes=nodes,
        segments=segments,
        levels=levels,
        structures=structures,
        sheet=sheet,
    )


# ---------------------------------------------------------------- 詳圖


def _el2(v: float) -> float:
    """詳圖 EL 欄位：m、兩位小數。"""
    return round(v + 0.0, 2)


def _section_codes():
    for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        yield f"{c}-{c}"


def _cm_txt(v: float) -> str:
    return f"{v * 100:g}"


def raft_columns(spec: DrainageNetworkSpec) -> list[tuple[float, float]]:
    """貯集格依 x 分欄：[(x0, x1)]（剖面沿 E 向切；同一欄的格 x 範圍相同）。"""
    st = spec.storage
    cols: dict[tuple[float, float], None] = {}
    for c in st.cells or [st.polygon]:
        xs = [x for x, _ in c]
        cols[(round(min(xs), 3), round(max(xs), 3))] = None
    return sorted(cols)


def derive_details(spec: DrainageNetworkSpec, result: dict | None = None) -> DetailSheetSpec:
    """由排水網路衍生「排水系統各部詳圖」DetailSheetSpec：**所有高程與容量從模型 / 計算結果帶入**。

    - 新設 U 型溝：依斷面 (w, h) 去重，每種一個 `u_ditch`（標題列出使用的 link id）
    - storage（raft）：`raft_detention`；坑底 / 坑頂 / 設計滿水位 / 溢流口 / 溢流時最高水位 / 孔口 / 起抽 / 停機 /
      入池進流管底 = `hydraulics.resolve_storage`；坑格數 = storage.raft_cells 或 cells 欄數；
      坑格淨寬 = 貯集格 E 向寬（與計算書貯集格同一定義）；地梁寬 = storage.beam_width 或示意值
    - storage.outlet_node（kind=outlet_well）：`outlet_well`；井頂 / 井底 = 節點；放流管 = 出流 link
      （井端 = 上游 invert、出口 = 下游 invert、管長 = link 長 − 井外長 / 2）；側溝底 / 溝緣 / 設計水位 =
      出流口處受納溝內插值；抽水出水管 / 溢流管入井管底 = resolve_storage
    - outfall：detail_defaults.outfall.detail（auto：2 m 內有受納溝 → ditch_joint，否則 protection）
    - 銘牌：容量 = 計算書 storage_provided.provided_m3（未給 result → 模型重算同一公式）
    - calc.outlet 為孔口（非 pump）：`orifice_plate` 孔口板詳圖，孔徑 / 尺寸 = calc.outlet，
      孔心 EL = 孔口底 + 孔高 / 2（列在最後，不影響其他詳圖剖面代號）
    構造尺寸（壁厚、版厚…）一律取 spec.detail_defaults（示意值，工程師確認）。
    """
    from draftagent.schema import detail as D

    dd = spec.detail_defaults
    rule = load_drawing_codes().scale("drainage_details")
    codes = _section_codes()
    items: list = []
    T = V.DETAIL_TITLE

    # ---- U 型溝
    groups: dict[tuple[float, float], list[str]] = {}
    for lk in spec.links:
        if not lk.existing and lk.section.type == "u_ditch":
            groups.setdefault((lk.section.w, lk.section.h), []).append(lk.id)
    ud = dd.u_ditch
    for i, ((w, h), ids) in enumerate(groups.items(), start=1):
        items.append(
            D.UDitchSection(
                id=f"U{i}",
                title=f"{T['u_ditch']} W{_cm_txt(w)}×H{_cm_txt(h)}（{'、'.join(ids)}）",
                section_code=next(codes),
                scale=ud.scale,
                inner_width=round(w * 1000.0, 1),
                inner_depth=round(h * 1000.0, 1),
                wall_t=ud.wall_t,
                base_t=ud.base_t,
                gravel_t=ud.gravel_t,
                cover=D.Cover(thickness=ud.cover_t, type_text=ud.cover_text) if ud.cover_t else None,
                rebar=D.RebarNote(bar=ud.rebar_bar, spacing=ud.rebar_spacing) if ud.rebar_bar else None,
            )
        )

    sto = spec.storage
    lv = resolve_storage(spec)[0] if sto is not None else None
    raft = None
    if sto is not None and sto.type == "raft":
        rd = dd.raft
        cols = raft_columns(spec)
        n = sto.raft_cells or len(cols)
        cw = round((cols[0][1] - cols[0][0]) * 1000.0, 1)
        bw = round(sto.beam_width * 1000.0, 1) if sto.beam_width else rd.beam_width
        into = [lk for lk in spec.links if lk.to == sto.inlet_node]
        d_in = max((lk.section.size_m for lk in into), default=0.2) * 1000.0
        orifice = None
        center = orifice_center_el(spec, lv)
        if center is not None:
            o = spec.calc.outlet
            orifice = (
                D.Orifice(shape="circle", diameter=round(o.diameter_m * 1000, 1), center_el=center)
                if o.type == "circ_orifice"
                else D.Orifice(
                    shape="rect",
                    width=round(o.width_m * 1000, 1),
                    height=round(o.height_m * 1000, 1),
                    center_el=center,
                )
            )
        pump = None
        if sto.drain_mode in ("pump", "gravity_pump"):
            sump_w = 1000.0
            if sto.outlet_well_polygon is not None:
                xs = [x for x, _ in sto.outlet_well_polygon]
                sump_w = round((max(xs) - min(xs)) * 1000.0, 1)
                cxs = sum(xs) / len(xs)
                hits = [i for i, (a, b) in enumerate(cols) if a <= cxs <= b]
                cell = max(hits) if hits else n - 1  # 跨欄界 → 取下游（右）側坑格
            else:
                cell = n - 1
            pump = D.RaftPump(
                cell=min(cell, n - 1),
                sump_width=min(sump_w, cw - 300.0),
                sump_depth=rd.sump_depth,
                start_el=_el2(lv.pump_start_el),
                stop_el=_el2(lv.pump_stop_el),
                discharge_dia=rd.pump_discharge_dia,
                q_cms=lv.pump_q_cms,
                discharge_to=f"放流井 {sto.outlet_node}",
            )
        overflow_here = sto.overflow_at == "storage"
        raft = D.RaftDetentionSection(
            id="RAFT",
            title=f"{T['raft_detention']}（{sto.id}）",
            section_code=next(codes),
            scale=rd.scale,
            n_cells=n,
            cell_width=cw,
            beam_width=bw,
            pit_depth=round((lv.top_el - lv.bottom_el) * 1000.0, 1),
            base_slab_t=rd.base_slab_t,
            top_slab_t=rd.top_slab_t,
            gravel_t=rd.gravel_t,
            bottom_el=_el2(lv.bottom_el),
            effective_depth=round(sto.effective_depth * 1000.0, 1),
            storage_level_el=_el2(lv.max_water_el),
            overflow_el=_el2(lv.overflow_el) if overflow_here else None,
            overflow_flood_el=(
                _el2(lv.overflow_flood_el)
                if overflow_here and lv.overflow_flood_el is not None
                else None
            ),
            overflow_dia=rd.overflow_dia,
            orifice=orifice,
            outflow_pipe_dia=rd.outflow_pipe_dia,
            overflow_note=f"溢流口設於進流井 {sto.inlet_node}（溢流管重力接放流井 {sto.outlet_node}）",
            inflow_pipe=D.Pipe(diameter=d_in, invert_el=_el2(lv.inflow_invert_el)),
            manhole_size=rd.manhole_size,
            ladder=rd.ladder,
            vent_cell=(n - 1) if rd.vent else None,
            pump=pump,
        )
        items.append(raft)

    # ---- 放流井 / 出流口
    ow_node = spec.node(sto.outlet_node) if sto is not None else None
    out_links = [lk for lk in spec.links if ow_node is not None and lk.from_ == ow_node.id]
    owd = dd.outlet_well
    if ow_node is not None and ow_node.kind == "outlet_well" and out_links:
        lk = out_links[0]
        to = spec.node(lk.to)
        rcv = receiving_point(spec, to.x, to.y)
        if rcv is not None and rcv.distance_m <= RECEIVE_MAX_DIST_M:
            ditch = _side_ditch(spec, rcv, owd.ditch_wall_t)
            lw = outlet_well_outer_m(spec) * 1000.0
            inflow = overflow_in = pump_in = None
            if raft is not None and raft.orifice is not None:
                inflow = D.Pipe(diameter=raft.outflow_pipe_dia, invert_el=_el2(raft.outflow_invert_el()))
            if sto.overflow_at == "storage":
                overflow_in = D.Pipe(diameter=dd.raft.overflow_dia, invert_el=_el2(lv.overflow_el))
                ov_from = f"滯洪設施 {sto.id}"
            else:
                overflow_in = D.Pipe(
                    diameter=owd.overflow_pipe_dia, invert_el=_el2(lv.overflow_pipe_invert_el)
                )
                ov_from = f"進流井 {sto.inlet_node}"
            if lv.pump_outlet_invert_el is not None:
                pump_in = D.Pipe(
                    diameter=dd.raft.pump_discharge_dia, invert_el=_el2(lv.pump_outlet_invert_el)
                )
            items.append(
                D.OutletWellDetail(
                    id="OW",
                    title=f"{T['outlet_well']}（{ow_node.id}）",
                    section_code=next(codes),
                    scale=owd.scale,
                    inner_width=owd.inner_width,
                    chamber_a_length=owd.chamber_a_length,
                    chamber_b_length=owd.chamber_b_length,
                    wall_t=owd.wall_t,
                    partition_t=owd.partition_t,
                    base_t=owd.base_t,
                    gravel_t=owd.gravel_t,
                    top_el=_el2(ow_node.top),
                    bottom_el=_el2(ow_node.invert_el),
                    cover_text=owd.cover_text,
                    inflow_pipe=inflow,
                    pump_inflow=pump_in,
                    overflow_inflow=overflow_in,
                    overflow_inflow_from=ov_from,
                    outlet_pipe=D.Pipe(
                        diameter=round(lk.section.size_m * 1000.0, 1),
                        invert_el=_el2(ow_node.invert_el),
                    ),
                    outlet_pipe_end_el=_el2(to.invert_el),
                    outlet_pipe_length=max(round(link_length(spec, lk) * 1000.0 - lw / 2), 300.0),
                    ditch=ditch,
                    cleanout_size=owd.cleanout_size,
                )
            )

    outfalls = [nd for nd in spec.nodes if nd.kind == "outfall"]
    if len(outfalls) == 1:
        of = outfalls[0]
        od = dd.outfall
        rcv = receiving_point(spec, of.x, of.y)
        near = rcv is not None and rcv.distance_m <= RECEIVE_MAX_DIST_M
        mode = od.detail if od.detail != "auto" else ("ditch_joint" if near else "protection")
        into = [lk for lk in spec.links if lk.to == of.id]
        d_of = max((lk.section.size_m for lk in into), default=0.2) * 1000.0
        pipe = D.Pipe(diameter=round(d_of, 1), invert_el=_el2(of.invert_el))
        if mode == "ditch_joint" and near:
            items.append(
                D.DitchJointDetail(
                    id="OF",
                    title=f"{T['ditch_joint']}（{of.id}）",
                    section_code=next(codes),
                    scale=od.scale,
                    pipe=pipe,
                    ditch=_side_ditch(spec, rcv, owd.ditch_wall_t),
                    ground_el=_el2(of.ground_el),
                    seal_text=od.seal_text,
                )
            )
        elif mode in ("protection", "ditch_joint"):
            items.append(
                D.OutfallProtectionDetail(
                    id="OF",
                    title=f"{T['outfall_protection']}（{of.id}）",
                    section_code=next(codes),
                    scale=od.scale,
                    pipe=pipe,
                    headwall_height=od.headwall_height,
                    headwall_t=od.headwall_t,
                    apron_el=_el2(of.invert_el - od.apron_drop / 1000.0),
                    apron_length=od.apron_length,
                    apron_t=od.apron_t,
                    cutoff_depth=od.cutoff_depth,
                    riprap_length=od.riprap_length,
                )
            )

    if sto is not None:
        nd = dd.nameplate
        cap = (
            float(result["storage_provided"]["provided_m3"]["value"])
            if result is not None
            else storage_volume_m3(spec)
        )
        items.append(
            D.NameplateDetail(
                id="NP",
                title=f"{T['nameplate']}（{sto.id}）",
                section_code=next(codes),
                scale=nd.scale,
                plate_width=nd.plate_width,
                plate_height=nd.plate_height,
                material_text=nd.material_text,
                fixing_text=nd.fixing_text,
                facility_name=f"{sto.id}（{STORAGE_ITEM[sto.type]}）",
                capacity_m3=round(cap, 2),
                maintainer=nd.maintainer,
                phone=nd.phone,
                warnings=nd.warnings,
            )
        )
    plate = _orifice_plate(spec, lv, codes)
    if plate is not None:
        items.append(plate)
    if not items:
        raise ValueError("排水網路沒有可衍生的詳圖（無新設 U 型溝、滯洪設施、放流井或出流口）")

    tb = spec.sheet.title_block.model_copy(
        update={"drawing_title": rule.name, "drawing_no": spec.drawing_no("detail")}
    )
    sheet = spec.sheet.model_copy(
        update={"title_block": tb, "size": dd.sheet_size, "north_arrow": False}
    )
    return D.DetailSheetSpec(title=rule.name, sheet=sheet, details=items)


def orifice_center_el(spec: DrainageNetworkSpec, lv) -> float | None:
    """孔口中心 EL（兩位小數，無條件進位：避免「中心 − 孔高 / 2」低於孔口底 / 坑底）。"""
    sto = spec.storage
    if (
        sto is None
        or sto.drain_mode == "pump"
        or spec.calc is None
        or spec.calc.outlet is None
        or lv is None
        or lv.orifice_invert_el is None
    ):
        return None
    hgt = spec.calc.outlet.opening_height_m
    return math.ceil(round((lv.orifice_invert_el + hgt / 2) * 100.0, 6)) / 100.0


def orifice_size_text(outlet) -> str:
    """孔口尺寸文字（mm），與詳圖 Orifice.size_text() 相同格式：圓 D135、矩形 寬×高。"""
    if outlet.type == "circ_orifice":
        return f"D{round(outlet.diameter_m * 1000, 1):g}"
    return f"{round(outlet.width_m * 1000, 1):g}×{round(outlet.height_m * 1000, 1):g}"


def _orifice_plate(spec: DrainageNetworkSpec, lv, codes):
    """calc.outlet 為孔口（重力排放）時的孔口板詳圖：孔徑 / 尺寸 = calc.outlet，孔心 EL = 孔口底 + 孔高 / 2。"""
    from draftagent.schema import detail as D

    center = orifice_center_el(spec, lv)
    if center is None:
        return None
    o = spec.calc.outlet
    od = spec.detail_defaults.orifice_plate
    orifice = (
        D.Orifice(shape="circle", diameter=round(o.diameter_m * 1000, 1), center_el=center)
        if o.type == "circ_orifice"
        else D.Orifice(
            shape="rect",
            width=round(o.width_m * 1000, 1),
            height=round(o.height_m * 1000, 1),
            center_el=center,
        )
    )
    w = math.ceil((orifice.size_w() + 2 * od.margin) / 10.0) * 10.0  # 板尺寸取 10 mm 整數倍
    h = math.ceil((orifice.size_h() + 2 * od.margin) / 10.0) * 10.0
    return D.OrificePlateDetail(
        id="OP",
        title=f"{V.DETAIL_TITLE['orifice_plate']} {orifice.size_text()}（{spec.storage.id}）",
        section_code=next(codes),
        scale=od.scale,
        plate_width=round(w, 1),
        plate_height=round(h, 1),
        plate_t=od.plate_t,
        orifice=orifice,
        orifice_center_height=round(h / 2, 1),
        material_text=od.material_text,
        fixing_text=od.fixing_text,
        bolt_text=od.bolt_text,
    )


def _side_ditch(spec: DrainageNetworkSpec, rcv, wall_t: float):
    from draftagent.schema.detail import SideDitch

    drain = next(d for d in spec.existing_drains if d.id == rcv.drain_id)
    w = getattr(drain.section, "w", None) or drain.section.size_m
    return SideDitch(
        inner_width=round(w * 1000.0, 1),
        wall_t=wall_t,
        bottom_el=_el2(rcv.invert_el),
        top_el=_el2(rcv.top_el),
        design_wl_el=_el2(rcv.design_wl) if rcv.design_wl is not None else None,
    )


# ---------------------------------------------------------------- 計算


def _rect_sides(coords) -> tuple[float, float]:
    p = Polygon(coords)
    mrr = p.minimum_rotated_rectangle
    if abs(mrr.area - p.area) > max(1e-6, 1e-6 * p.area):
        raise ValueError("貯集格必須是矩形 polygon（才能換成 length × width）")
    xs = list(mrr.exterior.coords)
    a = LineString(xs[0:2]).length
    b = LineString(xs[1:3]).length
    return max(a, b), min(a, b)


def storage_cells(spec: DrainageNetworkSpec) -> list[tuple[float, float]]:
    st = spec.storage
    if st is None:
        return []
    return [_rect_sides(c) for c in (st.cells or [st.polygon])]


def outlet_well_area(spec: DrainageNetworkSpec) -> float:
    st = spec.storage
    if st is None or st.outlet_well_polygon is None:
        return 0.0
    return Polygon(st.outlet_well_polygon).area


def storage_volume_m3(spec: DrainageNetworkSpec) -> float:
    st = spec.storage
    if st is None:
        return 0.0
    plan = sum(a * b for a, b in storage_cells(spec))
    return (plan - outlet_well_area(spec)) * st.effective_depth


def calc_links(spec: DrainageNetworkSpec) -> tuple[list[Link], dict[str, str]]:
    """納入管渠水理檢核的 link 與排除原因。

    排除：既有設施；滯洪設施出流端以下（流量受孔口控制，不以合理化公式檢核）；坡度 ≤ 0（抽水段）。
    """
    import networkx as nx

    g = build_graph(spec)
    below: set[str] = set()
    if spec.storage is not None and spec.storage.outlet_node in g:
        below = nx.descendants(g, spec.storage.outlet_node) | {spec.storage.outlet_node}
    keep, skipped = [], {}
    for lk in spec.links:
        if lk.existing:
            skipped[lk.id] = "既有設施"
        elif lk.from_ in below:
            skipped[lk.id] = "滯洪設施出流端以下，流量受孔口控制"
        elif link_slope(spec, lk) <= 0:
            skipped[lk.id] = "坡度 ≤ 0（抽水段），不以曼寧公式檢核"
        else:
            keep.append(lk)
    return keep, skipped


def pipe_design_c(spec: DrainageNetworkSpec) -> dict[str, float]:
    """calc_links 的 link id → 上游集水分區面積加權 C（network.check_network 上游累加 Σ C·A / Σ A）。"""
    rep = check_network(spec)
    if not rep.ok:
        raise ValueError("排水網路檢查未通過：" + "；".join(rep.errors))
    out = {}
    for lk in calc_links(spec)[0]:
        if lk.id not in rep.link_c:
            raise ValueError(f"管段 {lk.id} 上游沒有任何集水分區，無法決定設計逕流係數")
        out[lk.id] = rep.link_c[lk.id]
    return out


def derive_calc(spec: DrainageNetworkSpec) -> TaoyuanSiteDrainageSpec:
    if spec.storage is None or spec.calc is None:
        raise ValueError("衍生流出抑制計算需要 storage 與 calc（孔口 / 溢流堰由工程師決定）")
    rep = check_network(spec)
    if not rep.ok:
        raise ValueError("排水網路檢查未通過：" + "；".join(rep.errors))
    areas = subcatchment_areas(spec)
    st = spec.storage
    pipes = []
    for lk in calc_links(spec)[0]:
        if lk.id not in rep.link_c:
            raise ValueError(f"管段 {lk.id} 上游沒有任何集水分區，無法決定設計逕流係數")
        sec = lk.section
        dims = {"diameter_m": sec.d} if sec.type == "pipe" else {"width_m": sec.w, "depth_m": sec.h}
        pipes.append(
            Pipe(
                name=lk.id,
                material=lk.material,
                n_override=lk.n_override,
                slope=link_slope(spec, lk),
                design_area_m2=rep.link_area_m2[lk.id],
                design_c=rep.link_c[lk.id],
                **dims,
            )
        )
    return TaoyuanSiteDrainageSpec(
        site_area_m2=Polygon(spec.site_boundary).area,
        district=spec.project.district,
        station=spec.calc.station,
        tc_min=spec.calc.tc_min,
        subareas=[
            Subarea(name=s.id, area_m2=areas[s.id], landuse=s.landuse, c_override=s.c_override)
            for s in spec.subcatchments
        ],
        storage=Storage(
            type=st.type,
            cells=[StorageCell(length_m=a, width_m=b) for a, b in storage_cells(spec)],
            effective_depth_m=st.effective_depth,
            outlet_well_area_m2=outlet_well_area(spec),
        ),
        release_mode="pump" if st.drain_mode == "pump" else "orifice",
        outlet=None if st.drain_mode == "pump" else spec.calc.outlet,
        pump_q_cms=st.pump_q_cms if st.drain_mode == "pump" else None,
        overflow_weir=spec.calc.overflow_weir,
        pipes=pipes,
        codes_path=spec.codes_path,
    )


# ---------------------------------------------------------------- 表格


@dataclass(frozen=True)
class QtyRow:
    item: str
    spec: str
    qty: float
    unit: str
    integer: bool = False

    def qty_text(self) -> str:
        return f"{int(self.qty)}" if self.integer else f"{self.qty:.2f}"

    def cells(self) -> tuple[str, str, str, str]:
        return self.item, self.spec, self.qty_text(), self.unit


def quantities(spec: DrainageNetworkSpec) -> list[QtyRow]:
    """設施尺寸數量一覽表（只計新設；既有設施不列）。"""
    mats = load_codes(spec.codes_path)["manning_n"]["materials"]
    rows: list[QtyRow] = []
    groups: dict[tuple[str, str], float] = {}
    order: list[tuple[str, str]] = []
    for lk in spec.links:
        if lk.existing:
            continue
        key = (
            SECTION_ITEM[lk.section.type],
            f"{lk.section.dims_text()}，{mats[lk.material]['name']}",
        )
        if key not in groups:
            order.append(key)
            groups[key] = 0.0
        groups[key] += link_length(spec, lk)
    rows += [QtyRow(item, sp, round(groups[(item, sp)], 2), "m") for item, sp in order]
    for kind, name in NODE_KIND_NAME.items():
        if kind == "existing_mh":
            continue
        n = sum(1 for nd in spec.nodes if nd.kind == kind)
        if n:
            rows.append(QtyRow(name, "—", n, NODE_UNIT.get(kind, "座"), integer=True))
    if spec.storage is not None:
        st = spec.storage
        cells = len(st.cells or [st.polygon])
        rows.append(
            QtyRow(
                STORAGE_ITEM[st.type],
                f"有效水深 {st.effective_depth:.2f} m，{cells} 格",
                round(storage_volume_m3(spec), 2),
                "m³",
            )
        )
    return rows


@dataclass(frozen=True)
class CatchmentRow:
    id: str
    landuse: str
    c: float
    area_m2: float
    outlet: str

    @property
    def area_ha(self) -> float:
        return self.area_m2 / M2_PER_HA

    @property
    def area_ping(self) -> float:
        return self.area_m2 * PING_PER_M2

    def cells(self) -> tuple[str, ...]:
        return (
            self.id,
            self.landuse,
            f"{self.c:.2f}",
            f"{self.area_m2:.2f}",
            f"{self.area_ha:.4f}",
            f"{self.area_ping:.2f}",
            self.outlet,
        )


def catchment_rows(spec: DrainageNetworkSpec) -> list[CatchmentRow]:
    codes = load_codes(spec.codes_path)
    cls = codes["runoff_coefficients"]["classes"]
    areas = subcatchment_areas(spec)
    return [
        CatchmentRow(
            id=s.id,
            landuse=cls[s.landuse]["name"],
            c=runoff_c(s.landuse, s.c_override, codes),
            area_m2=areas[s.id],
            outlet=s.outlet_node,
        )
        for s in spec.subcatchments
    ]
