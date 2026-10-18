"""排水計畫書本文的內容模型：spec + result.json + 附圖 → 區塊序列（md / docx 共用）。

**所有數字由程式帶入**：面積（m² + 坪）、貯集量、排放量、孔口、管渠表、Horner 係數與降雨強度、
逕流係數表一律取自模型（`drainage/network.py`、`drainage/derive.py`）與計算結果（result.json）；
法規 / 手冊數值只從 `codes/*.yaml` 讀，並在旁標註 yaml 來源與核對狀態（verified: false）。
spec 的補充文字只能是描述（schema 擋數字）。純函式，不得呼叫 LLM。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from draftagent.report import vocab as V
from draftagent.report.figures import Figure, SheetInfo
from draftagent.report.model import PLACEHOLDER, DrainageReportSpec

PING = 0.3025  # 坪 / m²（CLAUDE.md：面積 m²，報表另附坪 × 0.3025）

RUNOFF_FILE = "taoyuan_runoff.yaml"
DRAWING_FILE = "taoyuan_drawing.yaml"


# ---------------------------------------------------------------- blocks


@dataclass
class Block:
    type: str  # title | h1 | h2 | p | li | caption | toc | table | image
    text: str = ""
    header: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    path: Path | None = None
    page_break: bool = False


@dataclass
class Content:
    blocks: list[Block]
    warnings: list[str]
    figures: list[Figure]


class _B:
    def __init__(self) -> None:
        self.blocks: list[Block] = []

    def add(self, type_: str, text: str = "", **kw: Any) -> None:
        self.blocks.append(Block(type_, " ".join(text.split()) if text else "", **kw))

    def table(self, caption: str, header: list[str], rows: list[list[str]]) -> None:
        self.add("caption", caption)
        clean = lambda s: " ".join(str(s).replace("|", "｜").split())  # noqa: E731
        self.blocks.append(
            Block(
                "table",
                header=[clean(h) for h in header],
                rows=[[clean(c) for c in r] for r in rows],
            )
        )


# ---------------------------------------------------------------- formatting


def f2(v: float) -> str:
    return f"{v:.2f}"


def area_txt(m2: float) -> str:
    return f"{m2:.2f} m²（{m2 * PING:.2f} 坪）"


def dec(v: Any) -> str:
    """yaml 數值原樣（不用科學記號）：0.000014 → '0.000014'。"""
    if isinstance(v, list | tuple):
        return " – ".join(dec(x) for x in v)
    d = Decimal(str(v))
    s = format(d, "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


def money(v: float) -> str:
    return f"{v:,.0f}" if float(v).is_integer() else f"{v:,.2f}"


def yaml_get(codes: dict[str, Any], path: str) -> Any:
    cur: Any = codes
    for k in path.split("."):
        cur = cur[k]
    return cur


def src_tag(file: str, path: str, source: str, verified: bool) -> str:
    flag = V.VERIFIED_TRUE if verified else V.VERIFIED_FALSE
    return f"{V.SRC_OPEN}{V.SRC_PREFIX}{file}：{path}；{source}；{flag}{V.SRC_CLOSE}"


def _ver(verified: bool) -> str:
    return V.VERIFIED_TRUE if verified else V.VERIFIED_FALSE


# ---------------------------------------------------------------- builder


def build_content(
    spec: DrainageReportSpec,
    result: dict[str, Any],
    sheets: list[SheetInfo],
    figures: list[Figure],
) -> Content:
    from draftagent.drainage.derive import STORAGE_ITEM, catchment_rows, quantities
    from draftagent.drainage.model import NODE_KIND_NAME
    from draftagent.drainage.network import link_length, site_area
    from draftagent.hydro.drawing_codes import load_drawing_codes
    from draftagent.hydro.taoyuan import load_codes
    from draftagent.render.drainage_plan import DRAIN_MODE_NAME
    from draftagent.report.vocab import load_format

    fmt = load_format()
    net = spec.network
    info = spec.report
    codes = load_codes(net.codes_path)
    dcodes = load_drawing_codes()
    rv = bool(codes.get("verified"))
    dv = dcodes.verified
    warnings: list[str] = []
    b = _B()
    st = net.storage
    r = result
    A = site_area(net)

    # ------------------------------------------------ 封面 / 目錄
    b.add("title", info.title)
    b.add("p", f"工程名稱：{net.project.name}")
    b.add("p", f"申請人：{info.applicant}")
    b.add("p", f"設計單位：{info.designer}")
    b.add("p", f"簽證技師：{info.engineer}")
    b.add("p", f"日期：中華民國 {info.date}")
    if PLACEHOLDER in (info.applicant, info.designer, info.engineer):
        b.add("p", V.PLACEHOLDER_NOTE)
        warnings.append("申請人 / 設計單位 / 簽證技師 未提供（以 ○○○ 佔位），送審前須由使用者填寫")
    b.add("p", V.TOC_TITLE, page_break=True)
    b.add("toc", V.TOC_PLACEHOLDER)

    def chapter(key: str) -> None:
        b.add("h1", fmt.chapter(key), page_break=True)

    def notes(key: str) -> None:
        for t in info.notes.get(key, []):
            b.add("p", t)

    mode = DRAIN_MODE_NAME[st.drain_mode]
    st_name = STORAGE_ITEM[st.type]
    outlet = r["outlet"]
    rel = r["release"]
    sr = r["storage_required"]
    sp = r["storage_provided"]

    cost_rows, total, missing = _cost_rows(spec, quantities(net))
    for item in missing:
        warnings.append(f"工程造價：{item} 未提供單價，單價 / 複價欄留空")

    # ------------------------------------------------ 摘要
    chapter("summary")
    where = net.project.location or net.project.district
    b.add(
        "p",
        f"本計畫為「{net.project.name}」之排水計畫書，基地位於{where}。排水方式為{mode}，"
        f"以{st_name}（{st.id}）抑制雨水流出，經出流口排入基地附近既有排水。主要成果如下（數值由模型與計算程式產出）：",
    )
    b.add("li", f"{V.L_SITE_AREA} = {area_txt(A)}")
    b.add("li", f"{V.L_AS} = {area_txt(r['As']['As_m2'])}")
    b.add("li", f"{V.L_C} = {r['C_composite']['value']:.4f}")
    b.add("li", f"{V.L_V_REQ} = {sr['required_m3']['value']:.2f} m³")
    b.add("li", f"{V.L_V_PROV} = {sp['provided_m3']['value']:.2f} m³")
    b.add("li", f"{V.L_Q_ALLOW} = {rel['allowed_cms']['value']:.6f} m³/s")
    b.add("li", f"{V.L_Q_OUT} = {outlet['Q_cms']['value']:.6f} m³/s")
    b.add("li", f"{V.L_RATIO} = {100 * outlet['ratio_to_allowed']:.2f} %")
    if r.get("overflow_weir"):
        b.add("li", f"{V.L_Q_WEIR} = {r['overflow_weir']['Q_cms']['value']:.6f} m³/s")
    if missing:
        b.add("li", f"{V.L_COST_PARTIAL} = {money(total)} 元")
    else:
        b.add("li", f"{V.L_COST_TOTAL} = {money(total)} 元")
    notes("summary")
    b.add("p", V.CONCLUSION)
    unver = [f for f, ok in ((RUNOFF_FILE, rv), (DRAWING_FILE, dv)) if not ok]
    if unver:
        b.add(
            "p",
            f"{V.VERIFIED_NOTICE_HEAD}本計畫書引用之 {'、'.join(unver)} 目前為 {V.VERIFIED_FALSE}"
            "（尚未人工核對原文），各法規數值旁已標註 yaml 來源與核對狀態；送審前須逐項對照原文。",
        )

    # ------------------------------------------------ 一、目的
    chapter("purpose")
    b.add(
        "p",
        "依桃園市建築基地開發排入雨水下水道逕流量標準及建築技術規則建築設計施工編第 4-3 條，"
        "檢討基地開發後雨水流出抑制設施之貯集量與排放量，並說明基地內排水系統規劃，"
        "供雨水下水道相關設施送審使用。適用法規路徑由工程師判定。",
    )
    notes("purpose")

    # ------------------------------------------------ 二、計畫範圍
    chapter("scope")
    b.add("h2", "2.1 基地位置")
    b.add(
        "p",
        f"基地位於{where}（行政區：{net.project.district}），圖資平面座標系統為 TWD97 TM2（EPSG:3826）。",
    )
    b.add("h2", "2.2 土地座落")
    b.table(
        V.T_LAND,
        ["項次", "段名", "地號"],
        [[str(i), lot.section, lot.lot] for i, lot in enumerate(info.land, 1)],
    )
    b.add("h2", "2.3 基地概要")
    site_rows = [
        ["行政區", net.project.district, V.USER_SRC],
        ["基地位置", net.project.location or "—", V.USER_SRC],
        ["使用分區", info.zoning, V.USER_SRC],
    ]
    for label, v in (("建蔽率", info.coverage_ratio_pct), ("開挖率", info.excavation_ratio_pct)):
        if v is None:
            warnings.append(f"{label}未提供（基地概要表填「未提供」）")
        site_rows.append([label, V.NOT_GIVEN if v is None else f"{dec(v)} %", V.USER_SRC])
    site_rows.append(["基地面積", area_txt(A), f"{V.MODEL_SRC}（基地界線多邊形）"])
    if net.building_footprints:
        from shapely.geometry import Polygon

        bf = sum(Polygon(p).area for p in net.building_footprints)
        site_rows.append(["建物投影面積", area_txt(bf), f"{V.MODEL_SRC}（建物外框多邊形）"])
    b.table(V.T_SITE, ["項目", "內容", "來源"], site_rows)
    notes("scope")

    # ------------------------------------------------ 三、內容概要
    chapter("overview")
    b.add("h2", "3.1 開發內容")
    b.add(
        "p",
        "建物配置依建築師提供之建築物一樓平面圖（本 repo 未產出，由使用者另附）；"
        "基地範圍、建物外框、排水設施與集水分區見附圖。",
    )
    b.add("h2", "3.2 排水規劃概要")
    b.add("li", f"排水方式：{mode}")
    b.add(
        "li",
        f"流出抑制設施：{st_name}（{st.id}），自進流井 {st.inlet_node} 入流、經放流井 {st.outlet_node} 出流",
    )
    b.add("li", f"出流控制：{_outlet_kind(net)}")
    if r.get("overflow_weir"):
        b.add(
            "li", "溢流：" + ("池內溢流至放流井" if st.overflow_at == "storage" else "進流井溢流")
        )
    outfalls = [n.id for n in net.nodes if n.kind == "outfall"]
    if net.existing_drains:
        names = "、".join(f"{d.name}（{d.id}）" for d in net.existing_drains)
        b.add("li", f"受納排水：出流口 {'、'.join(outfalls)} 排入{names}")
    kinds = sorted({n.kind for n in net.nodes if n.kind != "existing_mh"})
    b.add("li", "排水設施：" + "、".join(NODE_KIND_NAME[k] for k in kinds) + "，以管渠串接")
    notes("overview")

    # ------------------------------------------------ 四、基本資料
    chapter("basic_data")
    b.add("h2", "4.1 設計基準與法規數值")
    b.add(
        "p",
        "下表數值一律讀自 repo 內 codes/*.yaml（不得憑記憶填寫）；yaml 來源欄為檔名與鍵路徑，"
        f"核對狀態為 {V.VERIFIED_FALSE} 者尚未人工核對原文。",
    )
    b.table(
        V.T_CODES,
        ["項目", "數值", "單位", "yaml 來源", "出處", "核對狀態"],
        [
            [label, dec(yaml_get(codes, path)), unit, f"{RUNOFF_FILE}：{path}",
             str(yaml_get(codes, path.split(".")[0])["source"]), _ver(rv)]
            for label, path, unit in _code_rows(net, r)
        ],
    )  # fmt: skip
    b.add("h2", "4.2 降雨強度")
    station = r["station"]["value"]
    st_src = r["station"]["source"]
    how = (
        f"由 yaml 依行政區{net.project.district}決定"
        if st_src.endswith(".districts")
        else "由工程師指定"
    )
    b.add(
        "p",
        f"降雨強度採 Horner 公式 I = a / (t + b)^c（I：mm/hr；t：min），雨量站「{station}」{how}"
        + src_tag(RUNOFF_FILE, f"horner.stations.{station}", codes["horner"]["source"], rv)
        + "。集流時間 t 見下表。",
    )
    use_name = {"storage": "貯集設施（溢流堰）", "release": "排放量", "collection": "集排水管渠"}
    by_T: dict[int, list[str]] = {}
    for label, d in r["rainfall"].items():
        by_T.setdefault(int(d["T_yr"]), []).append(use_name.get(label, label))
    tc = r["tc_min"]["value"]
    horner_rows = []
    for T in sorted(by_T, reverse=True):
        p = codes["horner"]["stations"][station][f"T{T}"]
        I = next(d["I_mm_hr"]["value"] for d in r["rainfall"].values() if int(d["T_yr"]) == T)
        horner_rows.append(
            [f"{T} 年", dec(p["a"]), dec(p["b"]), dec(p["c"]), dec(tc), f"{I:.2f}",
             "、".join(by_T[T]), f"{RUNOFF_FILE}：horner.stations.{station}.T{T}", _ver(rv)]
        )  # fmt: skip
    b.table(
        V.T_HORNER,
        [
            "重現期",
            "a",
            "b",
            "c",
            "集流時間 t（min）",
            "降雨強度 I（mm/hr）",
            "用途",
            "yaml 來源",
            "核對狀態",
        ],
        horner_rows,
    )
    b.add("h2", "4.3 逕流係數")
    cls = codes["runoff_coefficients"]["classes"]
    seen: dict[tuple[str, float], list[str]] = {}
    for s in r["subareas"]:
        seen.setdefault((s["landuse"], round(s["c"], 6)), []).append(s["c_source"])
    c_rows = []
    for (lu, c), srcs in seen.items():
        k = cls[lu]
        how_c = "手冊中值" if all(x.endswith(".median") for x in srcs) else "工程師指定（範圍內）"
        c_rows.append(
            [k["name"], f"{dec(k['min'])} – {dec(k['max'])}", f"{c:.2f}", how_c,
             f"{RUNOFF_FILE}：runoff_coefficients.classes.{lu}", _ver(rv)]
        )  # fmt: skip
    b.table(
        V.T_RUNOFF_C, ["土地使用", "手冊範圍", "採用 C", "C 來源", "yaml 來源", "核對狀態"], c_rows
    )
    b.add("h2", "4.4 基地附近排水系統現況")
    b.add("p", info.existing_drainage)
    if net.existing_drains:
        ex_rows = []
        for d in net.existing_drains:
            for i, pt in enumerate(d.points, 1):
                ex_rows.append(
                    [d.id, d.name, d.section.dims_text(), f"測點 {i}", f2(pt.x), f2(pt.y),
                     "—" if pt.top_el is None else f2(pt.top_el),
                     "—" if pt.invert_el is None else f2(pt.invert_el),
                     "—" if d.design_water_depth_m is None else f2(d.design_water_depth_m)]
                )  # fmt: skip
        b.table(
            V.T_EXISTING,
            [
                "編號",
                "名稱",
                "斷面",
                "測點（依流向）",
                "E（m）",
                "N（m）",
                "溝頂 EL（m）",
                "溝底 EL（m）",
                "設計水深（m）",
            ],
            ex_rows,
        )
    else:
        warnings.append("network 未提供既有排水（existing_drains），現況表未列")
        b.add("p", "（模型未提供既有排水測量資料，由使用者另附）")
    b.add("h2", "4.5 基地現況照片")
    photo_figs = [f for f in figures if f.sheet_name == "基地現況照片"]
    if photo_figs:
        idx = {id(f): i for i, f in enumerate(figures, 1)}
        b.table(
            V.T_PHOTOS,
            ["附圖", "檔案", "說明"],
            [[f"{V.FIG_PREFIX} {idx[id(f)]}", Path(ph.path).name, ph.caption]
             for f, ph in zip(photo_figs, info.photos, strict=True)],
        )  # fmt: skip
    else:
        warnings.append("未提供基地現況照片（由使用者另附）")
        b.add("p", "未提供基地現況照片（由使用者另附）。")
    notes("basic_data")

    # ------------------------------------------------ 五、雨水下水道相關設施
    chapter("facilities")
    b.add("h2", "5.1 集水分區")
    b.add("p", "集水分區面積由分區多邊形計算，逕流係數依表 4-3。")
    crs = catchment_rows(net)
    rows = [
        [c.id, c.landuse, f"{c.c:.2f}", f"{c.area_m2:.2f}", f"{c.area_ping:.2f}", c.outlet]
        for c in crs
    ]
    tot = sum(c.area_m2 for c in crs)
    rows.append(["合計", "—", "—", f"{tot:.2f}", f"{tot * PING:.2f}", "—"])
    b.table(V.T_CATCH, ["分區", "土地使用", "C", "面積（m²）", "面積（坪）", "排入節點"], rows)
    b.add("h2", "5.2 排水管渠及水理計算")
    b.add(
        "p",
        "管渠設計流量以合理化公式 Q = C·I·A / 360（A：ha）計算，C 為上游集水分區面積加權逕流係數，"
        "I 為集排水管渠重現期之降雨強度（表 4-2）；通水容量以曼寧公式計算（滿管 / 滿渠）。"
        "滯洪設施出流端以下受孔口控制，不列入本表。",
    )
    mats = codes["manning_n"]["materials"]
    links = {lk.id: lk for lk in net.links}
    p_rows = []
    for p in r["pipes"]:
        lk = links[p["name"]]
        c = p["C_design"]["value"] if "C_design" in p else r["C_composite"]["value"]
        vel = "—" if p["velocity_m_s"] is None else f"{p['velocity_m_s']['value']:.3f}"
        p_rows.append(
            [p["name"], lk.section.text(), mats[lk.material]["name"], f"{p['n']['value']:.3f}",
             f"{p['slope'] * 100:.2f}", f"{link_length(net, lk):.2f}", f"{p['design_area_m2']:.2f}",
             f"{c:.2f}", f"{p['Q_design_cms']['value']:.5f}", f"{p['capacity_cms']['value']:.5f}", vel]
        )  # fmt: skip
    b.table(
        V.T_PIPES,
        ["管渠", "斷面", "材料", "n", "坡度 S（%）", "長度 L（m）", "集水面積（m²）", "C",
         "設計流量 Q（m³/s）", "通水容量（m³/s）", "流速 V（m/s）"],
        p_rows,
    )  # fmt: skip
    b.add("h2", "5.3 流出抑制設施")
    b.add("p", "貯集量與排放量依下列法規數值計算（數值與來源讀自 yaml）：")
    for label, (file, path) in V.REGULATORY_LABELS.items():
        sec = path.split(".")[0]
        b.add(
            "li",
            f"{label} = {dec(yaml_get(codes, path))} "
            + {"min_storage_m3_per_m2": "m³/m²", "max_release_cms_per_m2": "m³/s/m²"}[
                path.split(".")[-1]
            ]
            + src_tag(file, path, codes[sec]["source"], rv),
        )
    b.table(
        V.T_STORAGE,
        ["項目", "數值", "單位", "說明"],
        [
            [
                "計算用面積 As",
                f"{r['As']['As_m2']:.2f}（{r['As']['As_ping']:.2f} 坪）",
                "m²",
                r["As"]["rule"],
            ],
            ["必要貯集量（桃園逕流量標準）", f"{sr['taoyuan_m3']['value']:.2f}", "m³", "As × k_TY"],
            ["必要貯集量（建技 4-3）", f"{sr['building_code_m3']['value']:.2f}", "m³", "As × k_BC"],
            ["必要貯集量（採用）", f"{sr['required_m3']['value']:.2f}", "m³", "兩者取大"],
            [
                "貯集設施淨平面積",
                f"{sp['net_plan_area_m2']['value']:.2f}",
                "m²",
                "貯集格面積扣除出流井",
            ],
            ["有效水深", f"{st.effective_depth:.2f}", "m", "工程師決定"],
            ["提供貯集量", f"{sp['provided_m3']['value']:.2f}", "m³", "淨平面積 × 有效水深"],
        ],
    )
    rel_rows = [
        ["容許排放量", f"{rel['allowed_cms']['value']:.6f}", "m³/s", "As × q"],
        [
            "設計排放量下限",
            f"{rel['design_min_cms']['value']:.6f}",
            "m³/s",
            "容許排放量 × 設計排放量比下限",
        ],
        [
            "設計排放量上限",
            f"{rel['design_max_cms']['value']:.6f}",
            "m³/s",
            "容許排放量 × 設計排放量比上限",
        ],
    ]
    calc_out = net.calc.outlet
    if outlet["type"] == "pump":
        rel_rows.append(["出流型式", "抽水機", "—", "工程師決定"])
    else:
        rel_rows.append(["出流型式", _outlet_kind(net), "—", "工程師決定"])
        rel_rows.append(["孔口設計水深", f"{calc_out.design_head_m:.2f}", "m", "工程師決定"])
    rel_rows += [
        ["設計排放量", f"{outlet['Q_cms']['value']:.6f}", "m³/s",
         "抽水機設計抽水量" if outlet["type"] == "pump" else "孔口公式（表 4-1 孔口係數）"],
        ["設計排放量 / 容許排放量", f"{100 * outlet['ratio_to_allowed']:.2f}", "%", "須介於設計排放量比範圍"],
        ["全基地洪峰流量 Q20", f"{r['rainfall']['storage']['Q_site_cms']['value']:.6f}", "m³/s", "合理化公式（重現期見表 4-2）"],
    ]  # fmt: skip
    if r.get("overflow_weir"):
        rel_rows.append(
            [
                "溢流堰排洪量",
                f"{r['overflow_weir']['Q_cms']['value']:.6f}",
                "m³/s",
                "矩形堰公式，須不小於 Q20",
            ]
        )
    b.table(V.T_RELEASE, ["項目", "數值", "單位", "說明"], rel_rows)
    notes("facilities")

    # ------------------------------------------------ 六、設施項目數量及總工程造價
    chapter("cost")
    b.add(
        "p",
        "數量由排水網路模型計算（管渠長度為節點座標距離、設施數量為節點數、滯洪設施為提供貯集量）；"
        "單價由使用者提供，未提供者單價與複價欄留空。",
    )
    b.table(
        V.T_COST,
        ["項次", "項目", "規格", "單位", "數量", "單價（元）", "複價（元）", "數量來源"],
        cost_rows,
    )
    if missing:
        b.add("li", f"{V.L_COST_PARTIAL} = {money(total)} 元")
        b.add("p", "未提供單價項目：" + "、".join(missing) + "。")
    else:
        b.add("li", f"{V.L_COST_TOTAL} = {money(total)} 元")
    notes("cost")

    # ------------------------------------------------ 附圖
    b.add("h1", fmt.appendix_title, page_break=True)
    fig_no = {id(f): i for i, f in enumerate(figures, 1)}
    sheet_rows = []
    for sh in fmt.sheets:
        thr = "—"
        if sh.scale_key:
            rule = dcodes.scale(sh.scale_key)
            thr = f"S≧1/{rule.max_denominator}"
        figs = [f for f in figures if f.sheet_name == sh.name]
        if figs:
            refs = "、".join(f"{V.FIG_PREFIX} {fig_no[id(f)]}" for f in figs)
            nos = "、".join(f.dwg_no for f in figs if f.dwg_no) or "—"
            scs = "；".join(f.scale for f in figs if f.scale) or "—"
            how = f"本 repo 產出（{refs}）"
        else:
            nos = scs = "—"
            how = {
                "text_hydraulics": "本文 5.2（表 5-2）",
                "text_storage": "本文 5.3（表 5-3、表 5-4）",
                "photos": "未提供（使用者另附）",
            }.get(
                sh.produced_by or "",
                f"{sh.provided_by}提供（本 repo 未產出）"
                if sh.provided_by
                else "本 repo 未產出（使用者另附）",
            )
        sheet_rows.append([str(sh.no), sh.name, thr, nos, scs, how])
    b.table(
        V.T_SHEETS, ["項次", "統一圖名", "檢核表比例", "圖號", "比例", "本計畫書處理"], sheet_rows
    )
    if not dv:
        b.add(
            "p",
            "檢核表比例門檻讀自 "
            + src_tag(DRAWING_FILE, "scales", dcodes.scales["drainage_plan"].source, dv)
            + "。",
        )
    photo_iter = iter(info.photos)
    for f in figures:
        n = fig_no[id(f)]
        b.add("image", f"{V.FIG_PREFIX} {n}", path=f.path)
        if f.sheet_name == "基地現況照片":
            ph = next(photo_iter)
            b.add("caption", f"{V.FIG_PREFIX} {n} 基地現況照片：{ph.caption}")
        else:
            b.add(
                "caption", f"{V.FIG_PREFIX} {n} {f.sheet_name}（圖號 {f.dwg_no}，比例 {f.scale}）"
            )
    return Content(b.blocks, warnings, figures)


def _outlet_kind(net) -> str:
    o = net.calc.outlet
    if o is None:
        return "抽水機排放"
    if o.type == "circ_orifice":
        return f"圓形孔口，孔徑 {o.diameter_m:.3f} m"
    return f"矩形孔口，高 {o.height_m:.3f} m × 寬 {o.width_m:.3f} m"


def _code_rows(net, r) -> list[tuple[str, str, str]]:
    rows = [
        ("最小貯集量（桃園逕流量標準）", "runoff_standard.min_storage_m3_per_m2", "m³/m²"),
        ("最大容許排放量（單位面積）", "runoff_standard.max_release_cms_per_m2", "m³/s/m²"),
        ("設計排放量比（對容許排放量）", "runoff_standard.design_release_ratio", "—"),
        ("最小貯集量（建技 4-3）", "building_code_4_3.min_storage_m3_per_m2", "m³/m²"),
        ("貯集設施設計重現期", "design_criteria.return_period_yr.storage", "年"),
        ("排放量重現期", "design_criteria.return_period_yr.release", "年"),
        ("集排水管渠重現期", "design_criteria.return_period_yr.collection", "年"),
        ("建議集流時間", "design_criteria.tc_min_recommended", "min"),
        ("管渠設計流速範圍", "design_criteria.velocity_m_s", "m/s"),
    ]
    o = net.calc.outlet
    if o is not None:
        rows.append(
            ("圓形孔口係數" if o.type == "circ_orifice" else "矩形孔口係數",
             f"orifice_weir.{'circ' if o.type == 'circ_orifice' else 'rect'}_orifice_coeff", "—")
        )  # fmt: skip
    if r.get("overflow_weir"):
        rows.append(("矩形堰係數", "orifice_weir.rect_weir_coeff", "—"))
    return rows


def _cost_rows(spec: DrainageReportSpec, qrows) -> tuple[list[list[str]], float, list[str]]:
    info = spec.report
    rows: list[list[str]] = []
    total = 0.0
    missing: list[str] = []
    i = 0
    for q in qrows:
        prices = [
            u for u in info.unit_prices if u.item == q.item and (u.spec is None or u.spec == q.spec)
        ]
        if len(prices) > 1:
            raise ValueError(f"數量表 {q.item}｜{q.spec} 對應多個單價")
        i += 1
        if prices:
            amt = round(q.qty * prices[0].unit_price)
            total += amt
            rows.append(
                [
                    str(i),
                    q.item,
                    q.spec,
                    q.unit,
                    q.qty_text(),
                    money(prices[0].unit_price),
                    money(amt),
                    V.MODEL_SRC,
                ]
            )
        else:
            missing.append(f"{q.item}（{q.spec}）")
            rows.append([str(i), q.item, q.spec, q.unit, q.qty_text(), "", "", V.MODEL_SRC])
    for x in info.extra_costs:
        i += 1
        qty = dec(x.qty)
        if x.unit_price is None:
            missing.append(f"{x.item}（{x.spec}）")
            rows.append([str(i), x.item, x.spec, x.unit, qty, "", "", V.USER_SRC])
        else:
            amt = round(x.qty * x.unit_price)
            total += amt
            rows.append(
                [str(i), x.item, x.spec, x.unit, qty, money(x.unit_price), money(amt), V.USER_SRC]
            )
    rows.append(
        ["", "合計", "不含未提供單價項目" if missing else "—", "", "", "", money(total), ""]
    )
    return rows, total, missing
