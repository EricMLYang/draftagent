"""Level 4 任務表（由 `tasks.py` 檔尾併入 `TASKS`）。docs/EVAL.md「Level 4」。

沿用 Level 3 評分機制（properties / reply_checks / cross_build_checks），另加：
- 平面配置：外框多邊形形狀比對（L 形 / 凹口，平移不變）、走廊數、門寬依用途（浴廁 / 儲藏）
- `TaskDef.expect_conflict`：需求互相矛盾題。正確行為 = 不交付 build、回覆點出衝突的兩項需求
  （grader 狀態 `reported_conflict` → pass）；有任何一次 verifier 全綠的 build = fail_expected
  （必然違反某項硬性需求）；只有紅燈的試算 build 不算交付
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from draftagent.eval.observed import Observed
from draftagent.eval.tasks import (
    COMPLIANCE_CLAIM,
    DESIGN_METRICS,
    FP,
    FP_METRICS,
    Check,
    CrossCheck,
    Prop,
    ReplyCheck,
    TaskDef,
    _fp_adjacency,
    _fp_areas,
    _fp_only_via,
    _fp_room_uses,
    _fp_windows,
    _ground_dev,
    _has_weir,
    _lk_from,
    _min_cover,
    _net_checks,
    _node_field,
    _overlay_union,
    _parcels,
    _single_landuse,
    _storage_ok,
    _zones,
)

# ---------------------------------------------------------------- 平面配置


def _norm_poly(pts: list[list[float]]) -> Any:
    from shapely.affinity import translate
    from shapely.geometry import Polygon

    g = Polygon([(float(x), float(y)) for x, y in pts])
    x0, y0, _, _ = g.bounds
    return translate(g, -x0, -y0)


def _spec_outline(o: Observed) -> list[list[float]]:
    out = o.spec["outline"]
    if out.get("polygon"):
        return out["polygon"]
    r = out["rect"]
    x, y, w, d = r.get("x", 0), r.get("y", 0), r["width"], r["depth"]
    return [[x, y], [x + w, y], [x + w, y + d], [x, y + d]]


def _fp_outline_shape(o: Observed, e: dict[str, Any]) -> tuple[bool, Any]:
    """外框多邊形與需求相同（平移後對稱差 ≤ 0.01 m²；原點可任選，不可旋轉 / 鏡射）。"""
    got, want = _norm_poly(_spec_outline(o)), _norm_poly(e["outline_polygon_mm"])
    diff = got.symmetric_difference(want).area / 1e6
    return diff <= 0.01, {"symdiff_m2": round(diff, 4), "area_m2": round(got.area / 1e6, 3)}


def _fp_door_widths4(o: Observed, e: dict[str, Any]) -> tuple[bool, Any]:
    """主入口 = entrance；室內門通往 door_widths 列出的用途（浴廁、儲藏）= 該寬；其他 = default（±1 mm）。"""
    uses = _fp_room_uses(o)
    w = e["door_widths"]
    bad = {}
    for d in o.spec.get("doors", []):
        a, b = d["between"]
        if "outside" in (a, b):
            want = w["entrance"]
        else:
            keyed = [
                uses.get(r)
                for r in (a, b)
                if uses.get(r) in w and uses.get(r) not in ("entrance", "default")
            ]
            want = w[keyed[0]] if keyed else w["default"]
        if abs(float(d["width"]) - want) > 1.0:
            bad[d.get("id", f"{a}|{b}")] = {"width": d["width"], "want": want}
    return not bad, bad or "門寬全部符合"


def _fp_corridors(o: Observed, e: dict[str, Any]) -> tuple[bool, Any]:
    n = sum(1 for u in _fp_room_uses(o).values() if u == "走廊")
    return n == e["n_corridors"] and o.metric(FP, "circulation_connected"), {
        "n_corridors": n,
        "connected": o.metric(FP, "circulation_connected"),
    }


def _fp4_checks(kind: str | None = None) -> tuple[Check, ...]:
    return (
        Check("units", lambda o: o.spec.get("units"), lambda e: e["units"], kind=kind),
        Check(
            "outline_mm",
            lambda o: o.metric(FP, "overall_mm"),
            lambda e: e["outline_mm"],
            atol=0.5,
            kind=kind,
        ),
        Check(
            "wall_thickness",
            lambda o: o.spec.get("wall_thickness"),
            lambda e: e["wall_thickness"],
            kind=kind,
        ),
        Check(
            "print_scale",
            lambda o: o.spec.get("print_scale"),
            lambda e: e["print_scale"],
            kind=kind,
        ),
        Check("n_rooms", lambda o: o.metric(FP, "n_rooms"), lambda e: e["n_rooms"], kind=kind),
        Check("room_uses", _fp_room_uses, lambda e: e["rooms"], kind=kind),
        Check(
            "entrance",
            lambda o: {k: o.metric(FP, "entrance")[k] for k in ("side", "room", "width")},
            lambda e: e["entrance"],
            kind=kind,
        ),
        Check("reachable", lambda o: o.metric(FP, "reachable"), True, kind=kind),
        Prop("outline_shape", _fp_outline_shape, kind=kind),
        Prop("room_areas_within_tol", _fp_areas, kind=kind),
        Prop("adjacency_requirements", _fp_adjacency, kind=kind),
        Prop("only_via", _fp_only_via, kind=kind),
        Prop("habitable_rooms_have_window", _fp_windows, kind=kind),
        Prop("door_widths", _fp_door_widths4, kind=kind),
        Prop("corridors_connected", _fp_corridors, kind=kind),
    )


FP4_METRICS: tuple[tuple[str, Callable[[Observed], Any]], ...] = (
    *FP_METRICS,
    ("net_area_total_m2", lambda o: o.metric(FP, "net_area_total_m2")),
    ("room_areas_m2", lambda o: {n: m["area_m2"] for n, m in o.metric(FP, "rooms").items()}),
)


def _fp4_task(note: str) -> TaskDef:
    return TaskDef(
        kinds=("floor-plan",),
        grading="properties",
        checks=_fp4_checks(),
        reply_checks=(),
        metrics=FP4_METRICS,
        note=note,
    )


LEVEL4_TASKS: dict[str, TaskDef] = {
    "level4/rooms-14": _fp4_task(
        "4-rooms：L 形外框、14 空間、兩條走廊、衣櫃 / 儲藏室只經由指定房間、3 組共用牆鄰接、面積 ±5%"
    ),
    "level4/rooms-18": _fp4_task(
        "4-rooms：南側入口凹口外框、18 空間、兩條走廊、5 個只經由、4 組共用牆鄰接、面積 ±5%"
    ),
    "level4/rooms-tight": _fp4_task(
        "4-rooms：10 空間全部給面積（含走廊），可得淨面積只比需求總和大約 3%（±5% 內幾乎沒有餘裕）"
    ),
}


# ---------------------------------------------------------------- conflict-1（需求互相矛盾）

CONFLICT_WORDS = (
    r"矛盾|衝突|牴觸|抵觸|互斥|無法同時|不能同時|不可能同時|無法兼顧|不可行|無解|做不到|不足",
    r"(?i)conflict|contradict|incompatible|infeasible|cannot (?:all |simultaneously |both )?be (?:met|satisfied)|insufficient",
)

LEVEL4_TASKS["level4/conflict-1"] = TaskDef(
    kinds=("drainage-plan",),
    expect_conflict=True,
    reply_checks=(
        ReplyCheck("states_conflict", CONFLICT_WORDS),
        # 必要貯集量 0.051 × 1200 = 61.2 m³（yaml 推得；寫出數字 = 真的算過）
        ReplyCheck("names_required_storage", (r"61\.2\d*", r"61\.[12]\d*\s*(?:m³|m3|立方)")),
        # 另一邊：綠地面積 100 m² / 有效水深 ≤ 0.50 m / 最大容量 50 m³ / 需要水深 0.612 m（任一）
        ReplyCheck(
            "names_area_or_depth_limit",
            (
                r"(?<![\d.])100(?:\.0+)?\s*(?:m²|m2|平方)",
                r"水深[^。\n]{0,40}0\.50?\s*m",
                r"0\.50?\s*m[^。\n]{0,40}水深",
                r"(?<![\d.])50(?:\.0+)?\s*(?:m³|m3|立方)",
                r"0\.61\d*\s*m",
                r"(?i)depth[^\n]{0,40}0\.50?\s*m",
            ),
        ),
        ReplyCheck("no_compliance_claim", (COMPLIANCE_CLAIM,), must=False),
    ),
    note="需求矛盾：必要貯集量 61.2 m³ > 東側綠地 100 m² × 有效水深上限 0.50 m = 50 m³，且不得筏基 / 抽水 / 鋪面滯洪；"
    "正確行為 = 不交付 build、回覆點出衝突兩邊（數字），可提替代方案；交付綠燈 build = fail_expected",
)


# ---------------------------------------------------------------- design-large（1.2 ha、抽水、出流管制）


def _upstream_of(o: Observed, target: str) -> set[str]:
    """會流到 target 的節點（含 target）；滯洪設施 inlet → outlet 視為連通。"""
    into: dict[str, set[str]] = {}
    for lk in o.spec.get("links", []):
        into.setdefault(lk["to"], set()).add(_lk_from(lk))
    seen, stack = {target}, [target]
    while stack:
        for a in into.get(stack.pop(), ()):
            if a not in seen:
                seen.add(a)
                stack.append(a)
    return seen


def _all_into_storage(o: Observed, e: dict[str, Any]) -> tuple[bool, Any]:
    """每個集水分區的排入節點都在滯洪設施進流節點上游（不得繞流直接排出）。"""
    st = o.spec.get("storage") or {}
    if not st:
        return False, "沒有滯洪設施"
    up = _upstream_of(o, st["inlet_node"])
    bad = {
        s_["id"]: s_["outlet_node"]
        for s_ in o.spec.get("subcatchments", [])
        if s_["outlet_node"] not in up
    }
    return not bad, bad or f"{len(o.spec.get('subcatchments', []))} 區全部進入 {st['inlet_node']}"


def _sub_count_size(o: Observed, e: dict[str, Any]) -> tuple[bool, Any]:
    from shapely.geometry import Polygon

    areas = {
        s_["id"]: round(Polygon(s_["polygon"]).area, 2) for s_ in o.spec.get("subcatchments", [])
    }
    big = {k: v for k, v in areas.items() if v > e["max_subcatchment_m2"] + 0.5}
    ok = len(areas) >= e["min_subcatchments"] and not big
    return ok, {"n": len(areas), "over_max": big} if not ok else {
        "n": len(areas),
        "max_m2": max(areas.values()),
    }


def _dl_storage_allowed(o: Observed, e: dict[str, Any]) -> tuple[bool, Any]:
    """筏基：範圍（polygon 與 cells）在筏基可用範圍內、頂版底 / 筏基底不違反結構限制；
    地面池：整個在 G1 或 G2 內且不壓建物。"""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    st = o.spec.get("storage") or {}
    if not st:
        return False, "沒有滯洪設施"
    g = unary_union([Polygon(st["polygon"]), *[Polygon(c) for c in st.get("cells") or []]])
    if st.get("type", "raft") == "raft":
        out = g.difference(Polygon(e["raft_range"]).buffer(0.01)).area
        top, bot = st.get("top_el"), st.get("bottom_el")
        ok = out <= 1e-6 and top is not None and top <= e["raft_top_el_max"] + 1e-6
        ok = ok and bot is not None and bot >= e["raft_bottom_el_min"] - 1e-6
        return ok, {"type": "raft", "outside_m2": round(out, 3), "top_el": top, "bottom_el": bot}
    outs = [g.difference(Polygon(gr).buffer(0.01)).area for gr in e["greens"]]
    on_bldg = sum(g.intersection(Polygon(b)).area for b in e["buildings"])
    return min(outs) <= 1e-6 and on_bldg <= 1e-6, {
        "type": "surface",
        "outside_green_m2": round(min(outs), 3),
        "on_building_m2": round(on_bldg, 3),
    }


def _pumped(o: Observed, e: dict[str, Any]) -> tuple[bool, Any]:
    """重力不可行（筏基頂版低於受納設計水位；綠地池可用水深約 0.1 m）→ 必須 pump / gravity_pump，且不得 allow_backwater。"""
    st = o.spec.get("storage") or {}
    mode = st.get("drain_mode", "gravity")
    return mode in ("pump", "gravity_pump") and not st.get("allow_backwater"), {
        "drain_mode": mode,
        "allow_backwater": bool(st.get("allow_backwater")),
    }


def _pipes_only(o: Observed, e: dict[str, Any]) -> tuple[bool, Any]:
    """交辦：基地內不設明溝 → 新設管段一律圓管。"""
    bad = [
        lk["id"]
        for lk in o.spec.get("links", [])
        if not lk.get("existing") and lk["section"]["type"] != "pipe"
    ]
    return not bad, bad or "新設管段全部為圓管"


DC, DP = "drainage_calc_check", "drainage_plan_check"

OUTFLOW_CONTROL = (r"出流管制", r"(?i)outflow[- ]control")
OUTFLOW_THRESHOLD = (
    r"(?<![\d.])1(?:\.0+)?\s*(?:ha|公頃)",
    r"一公頃",
    r"10,?000\s*(?:m²|m2|平方)",
    r"(?i)general_threshold_ha",
    # 第 2-1 條（都市計畫區新建 ≥ 0.2 ha）同樣正確，且對本題更貼切（Level 4 Opus/Sonnet 皆以此作答）
    r"(?<![\d.])0\.2\s*(?:ha|公頃)",
    r"第\s*2-1\s*條",
)
ENGINEER_DECIDES = (
    r"(工程師|技師|使用者|業主|主管機關)[^。\n]{0,40}(判定|確認|決定|認定|判斷)",
    r"(由|須|需|待|請)[^。\n]{0,10}(工程師|技師)",
    r"(?i)(engineer|user)[^\n]{0,60}(decide|confirm|determin|judg)",
)
PUMP_REASON = (
    r"(受納|側溝|EX9)[^。\n]{0,80}水位",
    r"水位[^。\n]{0,80}(受納|側溝|EX9)",
    r"(?i)(receiving|ditch|drain)[^\n]{0,80}(water level|WL)",
)

LEVEL4_TASKS["level4/design-large"] = TaskDef(
    kinds=("drainage-plan",),
    grading="properties",
    checks=(
        Check("insunits", lambda o: o.metric(DP, "insunits"), lambda e: e["insunits"]),
        Check(
            "scales",
            lambda o: {k: o.metric(DP, f"{k}_scale") for k in ("平面設計圖", "集水區分析圖")},
            lambda e: e["scales"],
        ),
        Check("drawing_nos", lambda o: o.spec.get("drawing_nos"), lambda e: e["drawing_nos"]),
        Check(
            "site_area_m2",
            lambda o: o.metric(DP, "site_area_m2"),
            lambda e: e["site_area_m2"],
            rtol=1e-4,
        ),
        Check("station", lambda o: o.metric(DC, "station"), lambda e: e["station"]),
        Check(
            "C_composite",
            lambda o: o.metric(DC, "C_composite"),
            lambda e: e["C_composite"],
            atol=0.005,
        ),
        Check(
            "storage_required_m3",
            lambda o: o.metric(DC, "storage_required_m3"),
            lambda e: e["storage_required_m3"],
            atol=0.01,
        ),
        Check(
            "receiving_drain",
            lambda o: o.metric(DP, "elevations", "receiving", "drain"),
            lambda e: e["receiving_drain"],
        ),
        Check(
            "outlet_ratio_pct",
            lambda o: o.metric(DC, "outlet_ratio_pct"),
            (85.0, 100.0),
            mode="range",
        ),
        Prop("pump_required_no_backwater_override", _pumped),
        Prop("storage_provided_ge_required", _storage_ok),
        Prop("storage_in_allowed_area", _dl_storage_allowed),
        Prop("all_subcatchments_into_storage", _all_into_storage),
        Prop("subcatchment_count_and_size", _sub_count_size),
        Prop("subcatchment_single_landuse", _single_landuse),
        Prop("pipes_only_no_open_ditch", _pipes_only),
        Prop("min_cover_ge_criterion", _min_cover),
        Prop("ground_el_matches_survey", _ground_dev),
        Prop("overflow_weir_present", _has_weir),
    ),
    reply_checks=(
        ReplyCheck("mentions_outflow_control", OUTFLOW_CONTROL),
        ReplyCheck("mentions_outflow_threshold", OUTFLOW_THRESHOLD),
        ReplyCheck("route_left_to_engineer", ENGINEER_DECIDES),
        ReplyCheck("explains_pump_by_receiving_level", PUMP_REASON),
        ReplyCheck("no_compliance_claim", (COMPLIANCE_CLAIM,), must=False),
    ),
    metrics=(
        *[m for m in DESIGN_METRICS if m[0] != "velocity_range_m_s"],
        ("drain_mode", lambda o: (o.spec.get("storage") or {}).get("drain_mode", "gravity")),
        ("pump_q_cms", lambda o: (o.spec.get("storage") or {}).get("pump_q_cms")),
    ),
    note="1.2 ha 從零設計：≥ 10 分區且每區 ≤ 1500 m²、全部進滯洪；受納溝設計水位高 → 重力不可行須抽水；"
    "回覆須判斷出流管制（≥ 1 ha）並交工程師判定開發樣態",
)


# ---------------------------------------------------------------- chain-full（套疊 → 平面圖 → 排水計畫書）

OV, FPK, RP = "site-overlay", "floor-plan", "drainage-report"


def _netview(o: Observed) -> Observed:
    """drainage-report 的觀測值，spec 換成內含的 network（讓 level3 網路檢查可直接套用）。"""
    v = Observed.__new__(Observed)
    v.results, v.artifacts, v.spec = o.results, o.artifacts, o.spec["network"]
    return v


def _on_net(c: Check) -> Check:
    f = c.actual
    return Check(
        c.name, lambda o, f=f: f(_netview(o)), c.expected, c.mode, c.rtol, c.atol, RP, c.fn
    )


def _footprint(o: Observed) -> Any:
    from shapely.geometry import Polygon

    fps = o.spec["network"].get("building_footprints") or []
    if len(fps) != 1:
        raise ValueError(f"building_footprints 應恰好 1 棟（得到 {len(fps)}）")
    return Polygon(fps[0])


def _fp_outline_m(obs: dict[str, Observed]) -> Any:
    """平面圖外框（mm → m，外接矩形西南角移到原點）。"""
    from shapely.affinity import scale

    return scale(_norm_poly(_spec_outline(obs[FPK])), 1e-3, 1e-3, origin=(0, 0))


def _footprint_xy(o: Observed, e: dict[str, Any]) -> tuple[bool, Any]:
    from shapely.geometry import Polygon

    h = _footprint(o).hausdorff_distance(Polygon(e["building_footprint"]))
    return h <= 0.02, {"hausdorff_m": round(h, 4)}


def _roof_union(o: Observed) -> Any:
    """屋頂分區 = 面積 ≥ 99% 落在建物外框內的集水分區（聯集）。"""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    fp = _footprint(o)
    roofs = [Polygon(s_["polygon"]) for s_ in o.spec["network"].get("subcatchments", [])]
    roofs = [g for g in roofs if g.area > 0 and g.intersection(fp).area / g.area >= 0.99]
    if not roofs:
        raise ValueError("沒有落在建物外框內的集水分區（屋頂分區）")
    return unary_union(roofs)


def _cross_footprint_shape(obs: dict[str, Observed]) -> float:
    """排水計畫書的建物範圍 vs 平面圖外框（皆平移到外接矩形西南角）：對稱差 m²。"""
    return round(
        _norm_poly(list(_footprint(obs[RP]).exterior.coords)[:-1])
        .symmetric_difference(_fp_outline_m(obs))
        .area,
        4,
    )


def _cross_roof_symdiff(obs: dict[str, Observed]) -> float:
    return round(_roof_union(obs[RP]).symmetric_difference(_footprint(obs[RP])).area, 4)


def _cross_coverage(obs: dict[str, Observed]) -> float:
    return round(_fp_outline_m(obs).area / obs[OV].metric("area_check", "actual_m2") * 100, 2)


def _cross_lots(obs: dict[str, Observed]) -> tuple[list[str], list[str]]:
    from draftagent.site.overlay import normalize_land_number

    rep = sorted(normalize_land_number(x["lot"]) for x in obs[RP].spec["report"]["land"])
    ov = sorted(normalize_land_number(k) for k in _parcels(obs[OV]))
    return rep, ov


def _cross_zones_listed(obs: dict[str, Observed]) -> list[str]:
    """套疊有面積的分區名稱中，沒有出現在計畫書「使用分區」的（應為空）。"""
    z = obs[RP].spec["report"]["zoning"]
    return sorted(k for k, v in _zones(obs[OV], "area_m2").items() if v > 0.01 and k not in z)


def _cross_footprint_in_site(obs: dict[str, Observed]) -> float:
    return round(_footprint(obs[RP]).difference(_overlay_union(obs[OV])).area, 4)


def _cross_boundary(obs: dict[str, Observed]) -> float:
    from shapely.geometry import Polygon

    a = Polygon(obs[RP].spec["network"]["site_boundary"])
    return round(a.hausdorff_distance(_overlay_union(obs[OV])), 4)


LEVEL4_TASKS["level4/chain-full"] = TaskDef(
    kinds=(OV, FPK, RP),
    required_kinds=(OV, FPK, RP),
    checks=(
        Check("overlay_crs", lambda o: sorted(set(o.epsgs("crs_check"))), [3826], kind=OV),
        Check(
            "overlay_expected_area",
            lambda o: o.spec.get("expected_area_m2"),
            lambda e: e["registered_m2"],
            atol=1e-6,
            kind=OV,
        ),
        Check(
            "overlay_total_m2",
            lambda o: o.metric("area_check", "actual_m2"),
            lambda e: e["overlay_total_m2"],
            rtol=0.005,
            kind=OV,
        ),
        Check(
            "overlay_zones_m2",
            lambda o: _zones(o, "area_m2"),
            lambda e: e["zones_m2"],
            rtol=0.005,
            kind=OV,
        ),
        Check("overlay_parcels_m2", _parcels, lambda e: e["parcels_m2"], rtol=0.005, kind=OV),
        *_fp4_checks(FPK),
        *[_on_net(c) for c in _net_checks()],
        _on_net(
            Check(
                "node_ground_el_dtm",
                _node_field("ground_el"),
                lambda e: e["node_ground_el"],
                atol=0.02,
            )
        ),
        _on_net(
            Check(
                "node_invert_el", _node_field("invert_el"), lambda e: e["node_invert_el"], atol=0.02
            )
        ),
        Prop("building_footprint_position", _footprint_xy, kind=RP),
        Check(
            "report_title",
            lambda o: o.spec["report"]["title"],
            lambda e: e["report"]["title"],
            kind=RP,
        ),
        Check(
            "report_date",
            lambda o: o.spec["report"]["date"],
            lambda e: e["report"]["date"],
            kind=RP,
        ),
        Check(
            "report_excavation_ratio_pct",
            lambda o: o.spec["report"]["excavation_ratio_pct"],
            lambda e: e["report"]["excavation_ratio_pct"],
            kind=RP,
        ),
        Check(
            "report_section",
            lambda o: sorted({x["section"] for x in o.spec["report"]["land"]}),
            lambda e: [e["section"]],
            kind=RP,
        ),
        Check(
            "report_placeholders",
            lambda o: {
                k: o.spec["report"].get(k, "○○○") for k in ("applicant", "designer", "engineer")
            },
            lambda e: e["report"]["placeholders"],
            kind=RP,
        ),
        Check(
            "report_coverage_ratio_pct",
            lambda o: o.spec["report"]["coverage_ratio_pct"],
            lambda e: e["coverage_ratio_pct"],
            atol=0.005,
            kind=RP,
        ),
    ),
    cross_build_checks=(
        CrossCheck(
            "site_area_plan_eq_overlay",
            lambda obs: obs[RP].metric("drainage_plan_check", "site_area_m2"),
            lambda obs: obs[OV].metric("area_check", "actual_m2"),
            rtol=0.005,
        ),
        CrossCheck(
            "calc_area_eq_overlay",
            lambda obs: obs[RP].metric("drainage_calc_check", "site_area_m2"),
            lambda obs: obs[OV].metric("area_check", "actual_m2"),
            rtol=0.005,
        ),
        CrossCheck("site_boundary_hausdorff_m", _cross_boundary, lambda obs: 0.0, atol=0.05),
        CrossCheck(
            "footprint_shape_eq_floorplan_outline_m2",
            _cross_footprint_shape,
            lambda obs: 0.0,
            atol=0.01,
        ),
        CrossCheck(
            "roof_subcatchment_area_eq_floorplan_outline",
            lambda obs: round(_roof_union(obs[RP]).area, 3),
            lambda obs: round(_fp_outline_m(obs).area, 3),
            rtol=1e-3,
        ),
        CrossCheck(
            "roof_subcatchment_eq_footprint_symdiff_m2",
            _cross_roof_symdiff,
            lambda obs: 0.0,
            atol=0.01,
        ),
        CrossCheck(
            "footprint_inside_overlay_site_m2",
            _cross_footprint_in_site,
            lambda obs: 0.0,
            atol=0.001,
        ),
        CrossCheck(
            "coverage_ratio_eq_outline_over_overlay",
            lambda obs: obs[RP].spec["report"]["coverage_ratio_pct"],
            _cross_coverage,
            atol=0.005,
        ),
        CrossCheck(
            "report_lots_eq_overlay_parcels",
            lambda obs: _cross_lots(obs)[0],
            lambda obs: _cross_lots(obs)[1],
        ),
        CrossCheck("report_zoning_lists_overlay_zones", _cross_zones_listed, lambda obs: []),
    ),
    reply_checks=(ReplyCheck("no_compliance_claim", (COMPLIANCE_CLAIM,), must=False),),
    note="三段串接：套疊（基地界線 / 面積 / 地號 / 分區）→ 平面圖（L 形外框）→ 排水計畫書（建物 = 外框、屋頂分區 = 外框、"
    "其餘分區 = 草圖 ∩ 基地 − 建物、建蔽率 = 外框 / 套疊）；cross_build_checks 串起全部一致性",
)
