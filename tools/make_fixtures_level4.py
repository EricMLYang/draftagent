"""產生 Level 4 評估題（tests/level4/*）的【合成】fixture（SYNTHETIC — 非真實圖資、不連網、確定性）。

    python tools/make_fixtures_level4.py            # 重寫 tests/level4/*/prompt.md、input/、expected/
    python tools/make_fixtures_level4.py --check    # 只比對：與磁碟上檔案不同 → exit 1

Level 4 = 繼續找極限（Level 3 對 Opus / Sonnet 太簡單）：

| 任務 | 測的能力 |
|---|---|
| rooms-14 | L 形外框、14 空間、兩條走廊、衣櫃 / 儲藏室只經由指定房間、多組鄰接、面積 ±5% |
| rooms-18 | 南側入口凹口外框、18 空間、同上約束更多 |
| rooms-tight | 10 空間全部給面積（含走廊），室內淨面積只比需求總和大約 3%：組合搜尋 |
| design-large | 1.2 ha 從零設計：≥ 10 集水分區、受納溝設計水位偏高 → 重力不可行要抽水；回覆要判斷出流管制適用路徑 |
| chain-full | 地籍套疊 → 平面圖（建物外框）→ 排水計畫書（建物投影 = 屋頂分區、建蔽率 = 外框 / 套疊面積） |
| conflict-1 | 需求互相矛盾（必要貯集量 > 可用綠地 × 最大水深，且不得筏基 / 抽水）：應指出衝突並停下 |

每題輸出 `prompt.md`、`input/`（原始資料：design.md、geojson、csv、shp、tif…）、`expected/expected.json`
（由本檔的需求 / 設計資料**解析計算**，不經 render / verify）、`input/spec*.json` 與
`expected/replay_reply.md`（參考解：`draftagent/eval/reference_solvers_level4.py` 只讀 input/ 解出；replay 用）。

座標：TWD97 TM2（EPSG:3826）虛構位置；不含任何真實案名、地號、公司名。檔名符合 `tools/make_fixtures_*.py`，
評估工作區會移除（含設計答案）。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

L4 = REPO / "tests" / "level4"
SIDE_ZH = {"S": "南", "N": "北", "E": "東", "W": "西"}
SYN_FP = "> 合成需求，非真實案件；不含任何真實案名、地號、公司名。"


def _write_json(p: Path, obj: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ================================================================ floor-plan（rooms-14 / rooms-18 / rooms-tight）

DOOR_W = {"entrance": 900, "浴廁": 700, "儲藏": 700, "default": 800}
AREA_TOL = 0.05
MIN_SHARED_WALL_MM = 900
WINDOW_REQUIRED = ("客廳", "臥室", "書房")  # plan_rules.yaml window_required 的子集（本題有的用途）

# outline：rect (W, D) | L (W, D, 東北缺角 cw, cd) | notch (W, D, 南側凹口 x0, w, dep)；mm
FP_BRIEFS: dict[str, dict[str, Any]] = {
    "rooms-14": {
        "title": "三房書房住宅平面圖",
        "outline": ("L", 13000, 12000, 4400, 3600),
        "walls": (200, 120),
        "scale": 100,
        "entrance": ("S", "玄關"),
        "rooms": [
            ("玄關", "玄關", 6.0),
            ("客廳", "客廳", 16.5),
            ("餐廳", "餐廳", 16.5),
            ("廚房", "廚房", 14.5),
            ("儲藏室", "儲藏", 5.5),
            ("走廊", "走廊", None),
            ("後走廊", "走廊", None),
            ("主臥室", "臥室", 13.0),
            ("主臥浴廁", "浴廁", 4.2),
            ("主臥衣櫃", "儲藏", 3.2),
            ("次臥室", "臥室", 10.0),
            ("次臥衣櫃", "儲藏", 3.4),
            ("公用浴廁", "浴廁", 6.0),
            ("書房", "書房", 7.5),
        ],
        "adjacency": [
            ("玄關", "客廳", "door"),
            ("玄關", "走廊", "door"),
            ("餐廳", "客廳", "door"),
            ("廚房", "餐廳", "door"),
            ("儲藏室", "廚房", "door"),
            ("後走廊", "走廊", "door"),
            ("主臥室", "走廊", "door"),
            ("次臥室", "走廊", "door"),
            ("公用浴廁", "走廊", "door"),
            ("書房", "後走廊", "door"),
            ("主臥室", "主臥浴廁", "door"),
            ("主臥室", "主臥衣櫃", "door"),
            ("次臥室", "次臥衣櫃", "door"),
            ("主臥浴廁", "主臥衣櫃", "adjacent"),
            ("次臥衣櫃", "公用浴廁", "adjacent"),
            ("書房", "次臥室", "adjacent"),
        ],
        "only_via": {
            "儲藏室": ["廚房"],
            "主臥浴廁": ["主臥室"],
            "主臥衣櫃": ["主臥室"],
            "次臥衣櫃": ["次臥室"],
        },
    },
    "rooms-18": {
        "title": "四房住宅平面圖",
        "outline": ("notch", 18000, 12000, 7000, 3000, 1500),
        "walls": (200, 120),
        "scale": 100,
        "entrance": ("S", "玄關"),
        "rooms": [
            ("玄關", "玄關", 6.0),
            ("客用浴廁", "浴廁", 3.0),
            ("客廳", "客廳", 28.5),
            ("餐廳", "餐廳", 21.5),
            ("廚房", "廚房", 15.5),
            ("儲藏室", "儲藏", 4.0),
            ("公用浴廁", "浴廁", 4.3),
            ("洗衣間", "其他", 4.8),
            ("走廊", "走廊", None),
            ("後走廊", "走廊", None),
            ("書房", "書房", 12.0),
            ("主臥室", "臥室", 20.5),
            ("主臥浴廁", "浴廁", 6.5),
            ("主臥衣櫃", "儲藏", 5.0),
            ("次臥室", "臥室", 15.0),
            ("次臥衣櫃", "儲藏", 2.3),
            ("兒童房", "臥室", 17.0),
            ("兒童房衣櫃", "儲藏", 4.0),
        ],
        "adjacency": [
            ("玄關", "客廳", "door"),
            ("玄關", "走廊", "door"),
            ("餐廳", "客廳", "door"),
            ("廚房", "餐廳", "door"),
            ("儲藏室", "廚房", "door"),
            ("後走廊", "走廊", "door"),
            ("公用浴廁", "後走廊", "door"),
            ("書房", "後走廊", "door"),
            ("客用浴廁", "走廊", "door"),
            ("洗衣間", "走廊", "door"),
            ("主臥室", "走廊", "door"),
            ("次臥室", "走廊", "door"),
            ("兒童房", "走廊", "door"),
            ("主臥室", "主臥浴廁", "door"),
            ("主臥室", "主臥衣櫃", "door"),
            ("次臥室", "次臥衣櫃", "door"),
            ("兒童房", "兒童房衣櫃", "door"),
            ("主臥浴廁", "主臥衣櫃", "adjacent"),
            ("廚房", "公用浴廁", "adjacent"),
            ("客用浴廁", "玄關", "adjacent"),
            ("次臥衣櫃", "兒童房衣櫃", "adjacent"),
        ],
        "only_via": {
            "儲藏室": ["廚房"],
            "主臥浴廁": ["主臥室"],
            "主臥衣櫃": ["主臥室"],
            "次臥衣櫃": ["次臥室"],
            "兒童房衣櫃": ["兒童房"],
        },
    },
    "rooms-tight": {
        "title": "二房緊湊住宅平面圖",
        "outline": ("rect", 12400, 8400),
        "walls": (200, 120),
        "scale": 100,
        "entrance": ("S", "玄關"),
        "rooms": [
            ("玄關", "玄關", 5.7),
            ("客廳", "客廳", 18.1),
            ("餐廳", "餐廳", 18.1),
            ("廚房", "廚房", 7.9),
            ("走廊", "走廊", 6.7),
            ("主臥室", "臥室", 13.2),
            ("主臥浴廁", "浴廁", 4.0),
            ("儲藏室", "儲藏", 3.2),
            ("次臥室", "臥室", 6.5),
            ("公用浴廁", "浴廁", 4.7),
        ],
        "adjacency": [
            ("玄關", "客廳", "door"),
            ("玄關", "走廊", "door"),
            ("餐廳", "客廳", "door"),
            ("廚房", "餐廳", "door"),
            ("主臥室", "走廊", "door"),
            ("次臥室", "走廊", "door"),
            ("公用浴廁", "走廊", "door"),
            ("儲藏室", "走廊", "door"),
            ("主臥室", "主臥浴廁", "door"),
            ("廚房", "公用浴廁", "adjacent"),
        ],
        "only_via": {"主臥浴廁": ["主臥室"], "儲藏室": ["走廊"]},
    },
}


def outline_polygon(o: tuple) -> list[list[float]]:
    """外框多邊形（mm；西南外角 = 原點，逆時針）。"""
    if o[0] == "rect":
        _, W, D = o
        return [[0, 0], [W, 0], [W, D], [0, D]]
    if o[0] == "L":
        _, W, D, cw, cd = o
        return [[0, 0], [W, 0], [W, D - cd], [W - cw, D - cd], [W - cw, D], [0, D]]
    _, W, D, x0, w, dep = o
    return [[0, 0], [x0, 0], [x0, dep], [x0 + w, dep], [x0 + w, 0], [W, 0], [W, D], [0, D]]


def outline_text(o: tuple) -> str:
    def m(v: float) -> str:
        return f"{v / 1000:g}"

    if o[0] == "rect":
        return f"東西 {m(o[1])} m × 南北 {m(o[2])} m，矩形"
    if o[0] == "L":
        _, W, D, cw, cd = o
        return (
            f"L 形，外接矩形東西 {m(W)} m × 南北 {m(D)} m；東北角缺角（東西 {m(cw)} m × 南北 {m(cd)} m，"
            "缺角範圍為室外）"
        )
    _, W, D, x0, w, dep = o
    return (
        f"外接矩形東西 {m(W)} m × 南北 {m(D)} m；南側外牆有入口凹口：凹口西緣距外框西緣 {m(x0)} m、"
        f"寬 {m(w)} m、向北凹入 {m(dep)} m（凹口範圍為室外門廊，外牆沿凹口轉折）"
    )


def fp_design_md(name: str, b: dict[str, Any]) -> str:
    te, ti = b["walls"]
    side, ent_room = b["entrance"]
    rows = "\n".join(
        f"| {n} | {u} | {'依配置' if a is None else f'{a:g}'} |" for n, u, a in b["rooms"]
    )
    adj = []
    for a, c, kind in b["adjacency"]:
        if kind == "door":
            adj.append(f"- {a} 與 {c} 之間有門直接相通")
        else:
            adj.append(
                f"- {a} 與 {c} 相鄰（共用一道牆，共用長度 ≥ {MIN_SHARED_WALL_MM / 10:g} cm）"
            )
    for a, via in b["only_via"].items():
        adj.append(f"- {a} 只能經由 {'、'.join(via)} 進出（{a} 的門只通往 {'、'.join(via)}）")
    habit = [n for n, u, _ in b["rooms"] if u in WINDOW_REQUIRED]
    corr = [n for n, u, _ in b["rooms"] if u == "走廊"]
    corr_line = f"- 動線空間（{'、'.join(corr)}）彼此以門直接相通\n" if len(corr) > 1 else ""
    return f"""# 設計需求：{b["title"]}（{name}）

{SYN_FP}

## 基本條件

- 圖名：{b["title"]}
- 外框（外牆外緣）：{outline_text(b["outline"])}
- 牆厚：外牆 {te / 10:g} cm、內牆 {ti / 10:g} cm
- 出圖比例：1/{b["scale"]}
- 主入口：設於{SIDE_ZH[side]}側外牆，由室外直接進入「{ent_room}」

## 房間清單

房名與用途請照下表填寫（用途為 FloorPlanSpec 的 use）。面積為**淨面積**（扣除牆體後，
即圖面房名下方的面積文字），容許 ±{AREA_TOL:.0%}；「依配置」表示不限。

| 房名 | 用途 | 淨面積 (m²) |
|---|---|---|
{rows}

## 鄰接與動線

{chr(10).join(adj)}
{corr_line}- 所有空間都必須能由主入口經室內的門到達
- 居室（{"、".join(habit)}）每間至少一扇對外窗

## 門寬

- 主入口：{DOOR_W["entrance"] / 10:g} cm
- 通往浴廁或儲藏的門：{DOOR_W["浴廁"] / 10:g} cm
- 其他室內門：{DOOR_W["default"] / 10:g} cm
"""


def fp_prompt_md(name: str, b: dict[str, Any]) -> str:
    n = len(b["rooms"])
    return f"""# Level 4 {name}：多房間平面配置（{n} 個空間）

住宅單層平面（**合成需求**，非真實案件；不含任何真實案名、地號、公司名）。
工程師只給設計需求 `tests/level4/{name}/input/design.md`（外框、牆厚、房間清單與淨面積需求、鄰接與動線需求、
門寬、主入口方位），**沒有座標**：各空間的位置與形狀、門窗位置由你配置。

請寫 `FloorPlanSpec`（`draftagent/schema/floorplan.py`），以 `floor-plan` 圖種 `draftagent build`
一次產出 DXF 平面圖與 IFC，並貼出完整 verifier 輸出。房名與用途照 design.md 表格；需求數值以 input/ 為準，
資料不足以配置時要停下來回報缺什麼，不得自行假設。回覆請簡述配置（各空間位置、動線、門窗）。

通過標準：verifier 全部綠燈，且符合 design.md 的全部需求（面積容許 ±{AREA_TOL:.0%}）。
回覆只能說「通過本 repo 現有 verifier 的檢查項目」，不得宣稱符合法規。
"""


def fp_expected(b: dict[str, Any]) -> dict[str, Any]:
    poly = outline_polygon(b["outline"])
    xs, ys = [p[0] for p in poly], [p[1] for p in poly]
    te, ti = b["walls"]
    side, ent_room = b["entrance"]
    return {
        "units": "mm",
        "outline_mm": [max(xs) - min(xs), max(ys) - min(ys)],
        "outline_polygon_mm": poly,
        "wall_thickness": {"exterior": te, "interior": ti},
        "print_scale": b["scale"],
        "n_rooms": len(b["rooms"]),
        "rooms": {n: u for n, u, _ in b["rooms"]},
        "area_targets_m2": {n: a for n, _, a in b["rooms"] if a is not None},
        "area_tol": AREA_TOL,
        "adjacency": [{"a": a, "b": c, "type": k} for a, c, k in b["adjacency"]],
        "min_shared_wall_mm": MIN_SHARED_WALL_MM,
        "only_via": b["only_via"],
        "entrance": {"side": side, "room": ent_room, "width": DOOR_W["entrance"]},
        "door_widths": DOOR_W,
        "window_required": [n for n, u, _ in b["rooms"] if u in WINDOW_REQUIRED],
        "n_corridors": sum(1 for _, u, _ in b["rooms"] if u == "走廊"),
    }


def task_rooms(root: Path, name: str) -> None:
    b = FP_BRIEFS[name]
    d = root / name
    (d / "input").mkdir(parents=True, exist_ok=True)
    (d / "prompt.md").write_text(fp_prompt_md(name, b), encoding="utf-8")
    (d / "input" / "design.md").write_text(fp_design_md(name, b), encoding="utf-8")
    _write_json(d / "expected" / "expected.json", fp_expected(b))


# ================================================================ 排水從零設計共用（design-large / conflict-1）


def _l3():
    import make_level3_fixtures as l3  # tools/（同為答案檔，評估工作區移除）

    return l3


def write_design_inputs(
    inp: Path,
    *,
    origin: tuple[float, float],
    site: list[tuple[float, float]],
    buildings: list[tuple[str, list[tuple[float, float]]]],
    surfaces: list[tuple[str, str, list[list[tuple[float, float]]]]],
    ground,
    grid: tuple[range, range],
    drain: dict[str, Any],
    road: list[tuple[str, float, float, float]],
    extra: list[tuple[dict[str, Any], list[tuple[float, float]]]] = (),
) -> None:
    """site.geojson（基地、建物、地表分類〔可帶內環〕、其他範圍）+ ground_points.csv + existing_drain.csv + road_center.csv。"""
    l3 = _l3()
    e0, n0 = origin

    def A(ps):
        return [[l3.r3(e0 + x), l3.r3(n0 + y)] for x, y in ps]

    feats = [l3._feature({"類別": "基地界線", "名稱": "基地範圍"}, A(site))]
    feats += [l3._feature({"類別": "建物", "名稱": nm}, A(p)) for nm, p in buildings]
    for lu, desc, rings in surfaces:
        f = l3._feature(
            {"類別": "地表分類", "土地使用": l3.LANDUSE_ZH[lu], "說明": desc}, A(rings[0])
        )
        for hole in rings[1:]:
            h = A(hole)
            f["geometry"]["coordinates"].append([*h, h[0]])
        feats.append(f)
    feats += [l3._feature(props, A(p)) for props, p in extra]
    inp.mkdir(parents=True, exist_ok=True)
    (inp / "site.geojson").write_text(l3._geojson("site", feats), encoding="utf-8")
    rows: list[list[Any]] = [["點號", "E(m)", "N(m)", "地面高(m)"]]
    i = 0
    for y in grid[1]:
        for x in grid[0]:
            i += 1
            rows.append([f"G{i:03d}", f"{e0 + x:.3f}", f"{n0 + y:.3f}", l3._fmt(ground(x, y))])
    (inp / "ground_points.csv").write_text(l3._csv(rows), encoding="utf-8")
    fake = l3.Net(
        name="design", origin=origin, ground=ground, site=site, buildings=[], subcatchments=[], pond=[],
        nodes=[], links=[], drain=drain, road=road, storage={}, project={}, sheet={},
    )  # fmt: skip
    (inp / "existing_drain.csv").write_text(l3._csv(l3.drain_rows(fake)), encoding="utf-8")
    (inp / "road_center.csv").write_text(l3._csv(l3.road_rows(fake)), encoding="utf-8")


def _req_storage(area: float) -> float:
    y = _l3()._yaml()
    return (
        max(
            y["runoff_standard"]["min_storage_m3_per_m2"],
            y["building_code_4_3"]["min_storage_m3_per_m2"],
        )
        * area
    )


def _abs(origin, ps):
    l3 = _l3()
    return [[l3.r3(origin[0] + x), l3.r3(origin[1] + y)] for x, y in ps]


def _surface_expected(origin, surfaces):
    return [{"landuse": lu, "rings": [_abs(origin, r) for r in rings]} for lu, _, rings in surfaces]


DESIGN_RAW_TABLE = """## 原始資料清單（同目錄）

| 檔案 | 內容 |
|---|---|
| `site.geojson` | EPSG:3826：基地界線、建物、**地表分類**（屬性 土地使用 = `taoyuan_runoff.yaml` `runoff_coefficients.classes.*.name`；鋪面為帶內環的多邊形，建物在其中）{extra} |
| `ground_points.csv` | 地面高程測點（點號、E、N、地面高；{grid} m 網格，涵蓋基地與南側道路邊） |
| `existing_drain.csv` | 南側既有道路側溝 {drain} 兩端測點（溝頂高、溝底高、斷面、設計水深；工程師提供） |
| `road_center.csv` | 道路中心樁高程 |
"""

DESIGN_SHEET_MD = """## 圖面

| 圖 | 比例 | 圖號 |
|---|---|---|
| 平面設計圖 | 1/{s[plan]} | {nos[plan]} |
| 集水區分析圖 | 1/{s[catchment]} | {nos[catchment]} |
| 縱斷面（主線） | 水平 1/{s[profile_h]}、垂直 1/{s[profile_v]} | {nos[profile]} |
| 縱斷面（出水側） | 水平 1/{s[outlet_h]}、垂直 1/{s[outlet_v]} | {nos[profile_outlet]} |
| 排水系統各部詳圖 | 依詳圖 | {nos[detail]} |

- 圖紙 {s[size]}；圖框工程名稱「{s[tb_project]}」、圖框圖號 {s[tb_no]}、日期 {s[date]}；設計、校核留白
- 要畫指北針與比例尺
"""


def _project_md(p: dict[str, str]) -> str:
    return f"""## 專案

- 工程名稱：{p["name"]}
- 行政區：{p["district"]}
- 位置：{p["location"]}
- 圖面總標題：{p["title"]}
"""


# ================================================================ conflict-1（需求互相矛盾）

CONFLICT_ORIGIN = (279650.0, 2766210.0)
CONFLICT_SITE = [(0, 0), (40, 0), (40, 30), (0, 30)]
CONFLICT_BUILDING = [(4, 6), (24, 6), (24, 26), (4, 26)]
CONFLICT_GREEN = [(30, 8), (40, 6), (40, 18), (30, 16)]  # 梯形，面積 100 m²
CONFLICT_MAX_DEPTH = 0.50
CONFLICT_MIN_COVER = 0.30
CONFLICT_DRAIN = {
    "id": "EX7",
    "name": "既有道路側溝",
    "section": "U型溝 W50×H100",
    "depth": 0.40,
    "pts": [("上游端", 44, -1.2, 118.32, 117.32), ("下游端", -4, -1.2, 118.26, 117.26)],
}
CONFLICT_ROAD = [("RC1", -4, -5.0, 118.36), ("RC2", 44, -5.0, 118.44)]
CONFLICT_PROJECT = {
    "name": "中壢區某廠辦新建工程（合成範例）",
    "district": "中壢區",
    "location": "桃園市中壢區（合成座標，非真實基地）",
    "title": "廠辦新建工程排水系統（合成範例）",
}


def conflict_ground(x: float, y: float) -> float:
    return 118.40 + 0.012 * y + 0.002 * (40 - x)


def conflict_surfaces():
    pave = [(0, 0), (40, 0), (40, 6), (30, 8), (30, 16), (40, 18), (40, 30), (0, 30)]
    return [
        ("mixed_residential", "廠辦屋頂", [CONFLICT_BUILDING]),
        ("park_green", "東側綠地", [CONFLICT_GREEN]),
        ("concrete_asphalt", "消防車道及停車場鋪面", [pave, CONFLICT_BUILDING[::-1]]),
    ]


CONFLICT_MD = """# 廠辦排水系統設計：設計交辦（合成資料）

{syn}

本案**沒有既有排水設計**：集水分區、節點、管線、滯洪設施都要由你設計。下列是工程師與業主已確定的條件與原始資料。

{raw}
{project}
## 設計條件（工程師交辦；每一項都是硬性需求）

- 雨水流出抑制依 `draftagent/hydro/codes/taoyuan_runoff.yaml`（貯集量、容許排放量、Horner、逕流係數、曼寧 n、流速、最小管徑，全部讀 yaml，不得另填）
- 建物為鋼構廠辦、**無地下室**，不得設筏基滯洪
- 鋪面為消防車道及停車場（消防單位要求淨空），不得設置任何滯洪設施；滯洪設施只能是設在 `site.geojson`「東側綠地」範圍內的地面滯洪池（池體不得超出綠地）
- 地面滯洪池**有效水深 ≤ {depth:.2f} m**（業主安全要求，工程師已同意）
- 業主不同意設置抽水設備：只能重力排放（排放口為圓形孔口，溢流設溢流堰、須能通過 Q20）
- 唯一的放流點：南側既有道路側溝 {drain}（出流口須接到 {drain}，放流口高程依受納溝設計水位檢核）
- 管材：新設管一律「塑膠管 / 纖維強化管」，曼寧 n 取 0.013；也可用「U 型溝（RC）」明溝
- **管頂覆土 ≥ {cover:.2f} m**（所有新設圓管 / 箱涵兩端；明溝不適用）
- 節點地面高依 `ground_points.csv` 內插，取至 0.01 m
- 集水分區：須涵蓋整個基地、互不重疊；同一集水分區只能有一種土地使用，分區界線須沿地表分類界線
- 不得使用 `allow_backwater`

{sheet}"""

CONFLICT_SHEET = {
    "plan": 200, "catchment": 250, "profile_h": 200, "profile_v": 50, "outlet_h": 50, "outlet_v": 50,
    "size": "A1", "tb_project": "中壢區某廠辦新建工程排水計畫（合成範例）", "tb_no": "CF", "date": "115.11.02",
    "nos": {"plan": "CF-01", "catchment": "CF-02", "profile": "CF-03", "profile_outlet": "CF-04", "detail": "CF-05"},
}  # fmt: skip


CONFLICT_PROMPT = """# Level 4 conflict-1：廠辦排水系統設計（從零設計）

中壢區某廠辦基地（**合成資料**，座標為 TWD97 TM2 虛構位置，非真實基地；不含任何真實案名、地號、公司名）。
本案沒有既有排水設計：你要**自己設計**集水分區、節點位置與底高、管徑與坡度、滯洪設施與孔口 / 溢流，並出整套排水圖。

原始資料與工程師的設計條件在 `tests/level4/conflict-1/input/`（`design.md` 有清單與設計條件）：
`site.geojson`（基地、建物、地表分類）、`ground_points.csv`（地面高程測點）、`existing_drain.csv`（受納溝）、
`road_center.csv`、`design.md`。

**測量值、受納溝資料與工程師條件以 input/ 為準**；design.md「設計條件」每一項都是硬性需求，不得為了出圖自行放寬。
資料不足以設計時要停下來回報缺什麼，不得自行假設或編造測量值。

請寫 `DrainageNetworkSpec`（`draftagent/drainage/model.py`），**不得填長度、坡度、面積、流量**；
以 `drainage-plan` 圖種一次產出平面設計圖、集水區分析圖、衍生縱斷面與衍生流出抑制計算，並貼出完整 verifier 輸出。
回覆請簡述設計決定與理由。

通過標準：verifier 全部綠燈（允許 yaml verified: false WARNING），且符合 design.md 的全部設計條件。
回覆只能說「通過本 repo 現有 verifier 的檢查項目」，不得宣稱符合法規。
"""


def task_conflict(root: Path) -> None:
    from shapely.geometry import Polygon

    l3 = _l3()
    d = root / "conflict-1"
    inp = d / "input"
    write_design_inputs(
        inp,
        origin=CONFLICT_ORIGIN,
        site=CONFLICT_SITE,
        buildings=[("廠辦棟", CONFLICT_BUILDING)],
        surfaces=conflict_surfaces(),
        ground=conflict_ground,
        grid=(range(-2, 43, 5), range(-3, 33, 5)),
        drain=CONFLICT_DRAIN,
        road=CONFLICT_ROAD,
    )
    s = CONFLICT_SHEET
    (inp / "design.md").write_text(
        CONFLICT_MD.format(
            syn=l3.SYN,
            raw=DESIGN_RAW_TABLE.format(extra="", grid=5, drain=CONFLICT_DRAIN["id"]),
            project=_project_md(CONFLICT_PROJECT),
            depth=CONFLICT_MAX_DEPTH,
            drain=CONFLICT_DRAIN["id"],
            cover=CONFLICT_MIN_COVER,
            sheet=DESIGN_SHEET_MD.format(s=s, nos=s["nos"]),
        ),
        encoding="utf-8",
    )
    (d / "prompt.md").write_text(CONFLICT_PROMPT, encoding="utf-8")
    A_site = Polygon(CONFLICT_SITE).area
    A_green = Polygon(CONFLICT_GREEN).area
    req = _req_storage(A_site)
    cap = A_green * CONFLICT_MAX_DEPTH
    if cap >= req:
        raise ValueError("conflict-1 必須矛盾：綠地 × 最大水深 < 必要貯集量")
    _write_json(
        d / "expected" / "expected.json",
        {
            "_note": "需求矛盾題：必要貯集量（yaml）> 可設滯洪池的綠地面積 × 最大有效水深；不得筏基、不得抽水、"
            "鋪面不得設滯洪。正確行為：不交付 build，回覆點出衝突（貯集量 vs 綠地面積 / 水深上限）。",
            "site_area_m2": A_site,
            "storage_required_m3": req,
            "green_area_m2": A_green,
            "max_depth_m": CONFLICT_MAX_DEPTH,
            "max_capacity_m3": cap,
            "depth_needed_m": req / A_green,
        },
    )


# ================================================================ design-large（1.2 ha 從零設計、抽水、出流管制判斷）

DL_ORIGIN = (281300.0, 2765400.0)
DL_SITE = [(0, 0), (120, 0), (120, 100), (0, 100)]
DL_BUILDINGS = [
    ("B1 住宅棟（設地下室）", [(10, 15), (50, 15), (50, 45), (10, 45)]),
    ("B2 住宅棟", [(70, 15), (110, 15), (110, 45), (70, 45)]),
    ("B3 住宅棟", [(30, 65), (90, 65), (90, 85), (30, 85)]),
]
DL_GREENS = [("西北綠地 G1", [(0, 60), (30, 60), (30, 100), (0, 100)]),
             ("東北綠地 G2", [(90, 60), (120, 60), (120, 100), (90, 100)])]  # fmt: skip
DL_RAFT_RANGE = [(12, 17), (48, 17), (48, 43), (12, 43)]  # B1 地下室筏基可用範圍
DL_RAFT_TOP = 117.60  # 筏基頂版底（結構已定）
DL_RAFT_BOTTOM_MIN = 116.20  # 筏基底不得低於（結構已定）
DL_MIN_COVER = 0.30
DL_MAX_SUB_M2 = 1500.0
DL_MIN_SUBS = 10
DL_DRAIN = {
    "id": "EX9",
    "name": "既有道路側溝",
    "section": "U型溝 W80×H120",
    "depth": 0.75,
    "pts": [("上游端", 125, -1.5, 120.25, 119.05), ("下游端", -5, -1.5, 120.19, 118.99)],
}
DL_ROAD = [("RC1", -5, -6.0, 120.96), ("RC2", 125, -6.0, 121.04)]
DL_PROJECT = {
    "name": "桃園區某集合住宅社區新建工程（合成範例）",
    "district": "桃園區",
    "location": "桃園市桃園區（合成座標，非真實基地；都市計畫區內住宅區）",
    "title": "集合住宅社區新建工程排水系統（合成範例）",
}
DL_SHEET = {
    "plan": 300, "catchment": 400, "profile_h": 200, "profile_v": 50, "outlet_h": 100, "outlet_v": 50,
    "size": "A1", "tb_project": "桃園區某集合住宅社區新建工程排水計畫（合成範例）", "tb_no": "DL",
    "date": "115.11.05",
    "nos": {"plan": "DL-01", "catchment": "DL-02", "profile": "DL-03", "profile_outlet": "DL-04", "detail": "DL-05"},
}  # fmt: skip


def dl_ground(x: float, y: float) -> float:
    return 121.00 + 0.003 * y + 0.0005 * x


def dl_surfaces():
    pave = [(0, 0), (120, 0), (120, 60), (90, 60), (90, 100), (30, 100), (30, 60), (0, 60)]
    roofs = [("mixed_residential", f"{nm.split()[0]} 屋頂", [p]) for nm, p in DL_BUILDINGS]
    greens = [("park_green", nm, [p]) for nm, p in DL_GREENS]
    return [
        *roofs,
        *greens,
        ("concrete_asphalt", "車道及鋪面", [pave, *[p[::-1] for _, p in DL_BUILDINGS]]),
    ]


DL_MD = """# 集合住宅社區排水系統設計：設計交辦（合成資料）

{syn}

本案**沒有既有排水設計**：集水分區、節點、管線、滯洪設施都要由你設計。下列是工程師已確定的條件與原始資料。

{raw}
{project}
## 開發概要（申請人提供）

- 開發內容：新建集合住宅社區（3 棟住宅），基地全部位於都市計畫區住宅區內
- 基地面積以 `site.geojson` 基地界線計算

## 設計條件（工程師交辦）

- 雨水流出抑制依 `draftagent/hydro/codes/taoyuan_runoff.yaml`（貯集量、容許排放量、Horner、逕流係數、曼寧 n、流速、最小管徑，全部讀 yaml，不得另填）
- **優先重力排放**：重力可行就不得用抽水；重力不可行時可改抽水（`pump` 或 `gravity_pump`），抽水量由你決定，須在容許排放量 85–100%；溢流設溢流堰（須能通過 Q20）
- 滯洪設施（擇一）：B1 地下室筏基（範圍不得超出 `site.geojson`「筏基可用範圍」；筏基頂版底 EL {raft_top:.2f}、筏基底不得低於 EL {raft_bot:.2f}，結構已定），或綠地 G1 / G2 範圍內的地面滯洪池（不得壓到建物）
- **全部集水分區的逕流都要先進入滯洪設施**（不得繞流直接排出）
- 唯一的放流點：南側既有道路側溝 {drain}（出流口須接到 {drain}，放流口高程依受納溝設計水位檢核）
- 管材：新設管一律圓管「塑膠管 / 纖維強化管」，曼寧 n 取 0.013（社區管委會要求：基地內不設明溝）
- **管頂覆土 ≥ {cover:.2f} m**（所有新設圓管兩端）
- 節點地面高依 `ground_points.csv` 內插，取至 0.01 m
- 集水分區：須涵蓋整個基地、互不重疊；同一集水分區只能有一種土地使用，分區界線須沿地表分類界線；
  **至少 {nsub} 區、每區 ≤ {maxsub:,.0f} m²**（工程師訂的分區準則）；建物屋頂以落水管直接排入你指定的節點
- 不得使用 `allow_backwater`（未經工程師書面判斷）

## 回覆要求

- 說明本案依 repo 的法規 yaml 應走哪些法規路徑（含門檻與判斷依據），以及哪些數值 / 判斷必須由工程師確認
- 說明滯洪設施型式與排放方式的理由

{sheet}"""

DL_PROMPT = """# Level 4 design-large：集合住宅社區排水系統設計（1.2 ha，從零設計）

桃園區某集合住宅社區基地（**合成資料**，座標為 TWD97 TM2 虛構位置，非真實基地；不含任何真實案名、地號、公司名）。
本案沒有既有排水設計：你要**自己設計**集水分區、集水井 / 人孔 / 進流井 / 放流井 / 出流口的位置與底高、
管徑與坡度、滯洪設施型式與尺寸、排放（孔口或抽水）與溢流，並出整套排水圖。

原始資料與工程師的設計條件在 `tests/level4/design-large/input/`（`design.md` 有清單、開發概要與設計條件）：
`site.geojson`（基地、建物、地表分類、筏基可用範圍）、`ground_points.csv`（地面高程測點）、`existing_drain.csv`（受納溝）、
`road_center.csv`、`design.md`。

**測量值、受納溝資料與工程師條件以 input/ 為準**；設計決定（位置、底高、尺寸）由你決定，但必須滿足 design.md 的
設計條件與本 repo verifier。資料不足以設計時要停下來回報缺什麼，不得自行假設或編造測量值。

請寫 `DrainageNetworkSpec`（`draftagent/drainage/model.py`），**不得填長度、坡度、面積、流量**；
以 `drainage-plan` 圖種一次產出平面設計圖、集水區分析圖、衍生縱斷面與衍生流出抑制計算，並貼出完整 verifier 輸出。
回覆依 design.md「回覆要求」說明。

通過標準：verifier 全部綠燈（允許 yaml verified: false 等 WARNING），且符合 design.md 的設計條件。
回覆只能說「通過本 repo 現有 verifier 的檢查項目」，不得宣稱符合法規。
"""


def task_design_large(root: Path) -> None:
    from shapely.geometry import Polygon

    l3 = _l3()
    d = root / "design-large"
    inp = d / "input"
    extra = [({"類別": "筏基可用範圍", "名稱": "B1 地下室筏基可用範圍"}, DL_RAFT_RANGE)]
    write_design_inputs(
        inp,
        origin=DL_ORIGIN,
        site=DL_SITE,
        buildings=DL_BUILDINGS,
        surfaces=dl_surfaces(),
        ground=dl_ground,
        grid=(range(-5, 126, 10), range(-5, 106, 10)),
        drain=DL_DRAIN,
        road=DL_ROAD,
        extra=extra,
    )
    s = DL_SHEET
    (inp / "design.md").write_text(
        DL_MD.format(
            syn=l3.SYN,
            raw=DESIGN_RAW_TABLE.format(extra="、**筏基可用範圍**", grid=10, drain=DL_DRAIN["id"]),
            project=_project_md(DL_PROJECT),
            raft_top=DL_RAFT_TOP,
            raft_bot=DL_RAFT_BOTTOM_MIN,
            drain=DL_DRAIN["id"],
            cover=DL_MIN_COVER,
            nsub=DL_MIN_SUBS,
            maxsub=DL_MAX_SUB_M2,
            sheet=DESIGN_SHEET_MD.format(s=s, nos=s["nos"]),
        ),
        encoding="utf-8",
    )
    (d / "prompt.md").write_text(DL_PROMPT, encoding="utf-8")
    y = l3._yaml()
    cls = y["runoff_coefficients"]["classes"]
    A_site = Polygon(DL_SITE).area
    ca = sum(cls[lu]["median"] * Polygon(r[0], r[1:]).area for lu, _, r in dl_surfaces())
    oc = __import__("yaml").safe_load(
        (REPO / "draftagent/hydro/codes/outflow_control.yaml").read_text(encoding="utf-8")
    )["applicability"]
    _write_json(
        d / "expected" / "expected.json",
        {
            "_note": "properties 題：沒有唯一答案。受納溝設計水位高於「進流井底 − 有效水深」可行範圍，且筏基頂版低於受納水位"
            " → 重力不可行、須抽水；基地 1.2 ha ≥ outflow_control.yaml applicability.general_threshold_ha → 回覆須指出"
            "出流管制適用（開發樣態由工程師判定）。ground_plane：地面高 = a + b·dE + c·dN（相對 origin）。",
            "insunits": 6,
            "site_area_m2": A_site,
            "site_area_ha": A_site / 1e4,
            "outflow_threshold_ha": oc["general_threshold_ha"],
            "C_composite": ca / A_site,
            "storage_required_m3": _req_storage(A_site),
            "release_allowed_cms": A_site * y["runoff_standard"]["max_release_cms_per_m2"],
            "receiving_drain": DL_DRAIN["id"],
            "min_cover_m": DL_MIN_COVER,
            "min_subcatchments": DL_MIN_SUBS,
            "max_subcatchment_m2": DL_MAX_SUB_M2,
            "station": "桃園",
            "origin": list(DL_ORIGIN),
            "ground_plane": {"a": 121.00, "b": 0.0005, "c": 0.003},
            "surfaces": _surface_expected(DL_ORIGIN, dl_surfaces()),
            "site_boundary": _abs(DL_ORIGIN, DL_SITE),
            "buildings": [_abs(DL_ORIGIN, p) for _, p in DL_BUILDINGS],
            "raft_range": _abs(DL_ORIGIN, DL_RAFT_RANGE),
            "raft_top_el_max": DL_RAFT_TOP,
            "raft_bottom_el_min": DL_RAFT_BOTTOM_MIN,
            "greens": [_abs(DL_ORIGIN, p) for _, p in DL_GREENS],
            "scales": {"平面設計圖": f"1/{s['plan']}", "集水區分析圖": f"1/{s['catchment']}"},
            "drawing_nos": dict(s["nos"]),
        },
    )


# ================================================================ chain-full（套疊 → 平面圖 → 排水計畫書）

CF_ORIGIN = (282050.0, 2764120.0)
CF_W, CF_H = 46.0, 34.0
CF_SECTION = ("測試五段", "0057")
CF_PARCELS = [  # 地號 8 碼、地目、相對座標、登記面積
    ("02120000", "建", [(-20, 0), (0, 0), (0, 34), (-20, 34)], None),
    ("02130000", "建", [(0, 0), (22, 0), (22, 34), (0, 34)], 748.36),
    ("02140000", "建", [(22, 0), (46, 0), (46, 24), (34, 34), (22, 34)], 755.81),
    ("02150000", "田", [(46, 0), (64, 0), (64, 24), (46, 24)], None),
    ("02160000", "建", [(22, 34), (34, 34), (46, 24), (64, 24), (64, 48), (-20, 48), (-20, 34), (0, 34)], None),
]  # fmt: skip
CF_SITE_LOTS = ("213", "214")
CF_SITE_REL = [
    (0, 0),
    (CF_W, 0),
    (CF_W, CF_H - 10),
    (CF_W - 12, CF_H),
    (0, CF_H),
]  # = template_net 基地
CF_ZONE_Y = 17.0  # 住宅區（南）/ 商業區（北）
CF_DZ = -4.75  # template_net 高程平移：地面 = 116.80 + 0.0105·dN − 0.002·dE
CF_BUILDING_SW = (12.0, 10.0)  # 建物外框西南外角（相對原點 m）
CF_SKETCH = {  # 集水分區草圖（外緣畫到基地外，且未扣除建物）
    "A2": ("concrete_asphalt", "西側車道", "CB1", [(-3, -3), (16, -3), (16, 37), (-3, 37)]),
    "A3": ("park_green", "北側及東側綠地", "CB3", [(16, 10), (49, 10), (49, 37), (16, 37)]),
    "A4": ("concrete_asphalt", "前庭及滯洪池", "SI1", [(16, -3), (49, -3), (49, 10), (16, 10)]),
}
CF_ROOF = ("A1", "mixed_residential", "建物屋頂", "SI1")
CF_FLOORPLAN = {
    "title": "住宅棟一層平面圖",
    "outline": ("L", 16000, 12000, 6000, 4000),
    "walls": (200, 120),
    "scale": 100,
    "entrance": ("S", "玄關"),
    "rooms": [
        ("玄關", "玄關", 7.0),
        ("客廳", "客廳", 20.5),
        ("餐廳", "餐廳", 17.0),
        ("廚房", "廚房", 18.5),
        ("走廊", "走廊", None),
        ("主臥室", "臥室", 17.5),
        ("次臥室", "臥室", 18.0),
        ("兒童房", "臥室", 20.5),
        ("書房", "書房", 7.5),
        ("浴廁", "浴廁", 4.9),
        ("儲藏室", "儲藏", 6.5),
    ],
    "adjacency": [
        ("玄關", "客廳", "door"),
        ("玄關", "走廊", "door"),
        ("餐廳", "客廳", "door"),
        ("廚房", "餐廳", "door"),
        ("主臥室", "走廊", "door"),
        ("次臥室", "走廊", "door"),
        ("兒童房", "走廊", "door"),
        ("書房", "走廊", "door"),
        ("浴廁", "走廊", "door"),
        ("儲藏室", "走廊", "door"),
    ],
    "only_via": {},
}
CF_REPORT = {
    "title": "桃園區某住宅新建工程排水計畫書（合成範例）",
    "date": "115.11.08",
    "excavation_ratio_pct": 12.0,
    "existing": "基地南側為既有道路，道路北側設有既有道路側溝，由東向西流；基地雨水經地面滯洪池調節後，由出流口接入該側溝。",
    "prices": [
        ("排水管", "D200 mm，塑膠管 / 纖維強化管", 2400),
        ("集水井", "—", 16000),
        ("人孔", "—", 26000),
        ("進流井", "—", 21000),
        ("放流井", "—", 28000),
        ("出流口", "—", 12000),
        ("地面滯洪池", "—", 3200),
    ],
}


def cf_ground(x: float, y: float) -> float:
    return 116.80 + 0.0105 * y - 0.002 * x


def cf_building() -> list[tuple[float, float]]:
    x0, y0 = CF_BUILDING_SW
    return [(x0 + p[0] / 1000, y0 + p[1] / 1000) for p in outline_polygon(CF_FLOORPLAN["outline"])]


def cf_subcatchments() -> list[tuple[str, str, str, str, list[tuple[float, float]]]]:
    """A1 = 建物外框；其餘 = 草圖 ∩ 基地 − 建物外框（相對座標）。"""
    from shapely.geometry import Polygon

    site = Polygon(CF_SITE_REL)
    bld = Polygon(cf_building())
    out = [(CF_ROOF[0], CF_ROOF[1], CF_ROOF[2], CF_ROOF[3], cf_building())]
    for sid, (lu, desc, to, sk) in CF_SKETCH.items():
        g = Polygon(sk).intersection(site).difference(bld).simplify(0)
        if g.geom_type != "Polygon" or list(g.interiors):
            raise ValueError(f"{sid} 裁切後不是單一無洞多邊形")
        out.append(
            (sid, lu, desc, to, [(round(x, 3), round(y, 3)) for x, y in g.exterior.coords[:-1]])
        )
    return out


def net_chain_full():
    l3 = _l3()
    net = l3.template_net(
        "chain-full",
        CF_ORIGIN,
        W=CF_W,
        H=CF_H,
        dz=CF_DZ,
        mirror=False,
        pond_w=13.0,
        drain_ids=("EX6", "既有道路側溝"),
        project={
            "name": "桃園區某住宅新建工程（合成範例）",
            "district": "桃園區",
            "location": "桃園市桃園區（合成座標，非真實基地）",
            "title": "住宅新建工程排水系統（合成範例）",
        },
        sheet=l3._std_sheet("CH", "115.11.08", "桃園區某住宅新建工程排水計畫書（合成範例）"),
    )
    for lk in net.links:  # 西側車道分區比 template 大（草圖含建物西側鋪面）→ L1 / L2 加大
        if lk.id in ("L1", "L2"):
            lk.section = "管 D200"
    net.buildings = [("住宅棟", cf_building())]
    net.subcatchments = cf_subcatchments()
    for n in net.nodes:
        n.ground = l3.r2(cf_ground(n.x, n.y))
    return net


CF_MD = """# 住宅新建工程：基地套疊 → 平面圖 → 排水計畫書（合成資料）

{syn}

## 原始資料清單（同目錄）

| 檔案 | 內容 |
|---|---|
| `cadastre.shp` | 地籍圖（EPSG:3826；欄位 段名、段號、地號、地目） |
| `zoning.shp` | 使用分區圖（EPSG:3826；分區欄位 `分區`） |
| `dtm.tif` | 地面高程 DTM（EPSG:3826，1 m 網格） |
| `floorplan.md` | 住宅棟一層平面的設計需求（外框、房間清單、鄰接、門寬）與建物位置 |
| `layout.geojson` | 配置：滯洪池範圍、**集水分區草圖**（外緣畫到地籍界線外，且**沒有扣除建物**） |
| `nodes.csv` | 節點平面位置與**井深**（地面高取 `dtm.tif`，雙線性內插、取至 0.01 m；底高 = 地面高 − 井深）；出流口直接給底高 |
| `links.csv` | 管溝連接、斷面、材質、曼寧 n |
| `existing_drain.csv` | 既有道路側溝兩端測點高程、斷面、設計水深 |
| `road_center.csv` | 道路中心樁高程 |

- 基地 = 地號 {lots}（見 prompt）；排水計畫的基地界線**必須與地籍套疊結果一致**
- 建物 = `floorplan.md` 的住宅棟外框（外牆外緣），依該檔「建物位置」放到 TWD97 座標；排水計畫的建物範圍以它為準
- 集水分區：屋頂分區 {roof} = 建物外框（土地使用「{roof_lu}」，落水管排入 {roof_to}）；其餘分區 = 草圖 ∩ 基地界線 − 建物外框
- 集水分區的「土地使用」為 `taoyuan_runoff.yaml` `runoff_coefficients.classes.*.name`；逕流係數取 yaml 中值
- 出流口在基地外道路側，地面高同樣取 DTM

{project}
{storage}
{sheet}
## 計畫書資訊

- 計畫書名稱：{r[title]}
- 申請人、設計單位、簽證技師：尚未提供（以 ○○○ 佔位）
- 日期：{r[date]}
- 土地座落：依地籍套疊結果（段名、地號；地號寫法同地政資料，如 0213）
- 使用分區：依地籍套疊結果，列出基地內所有分區名稱（以「、」分隔）
- 建蔽率：建物外框（外牆外緣）面積 ÷ 基地面積（地籍套疊結果）× 100 %，取至小數 2 位
- 開挖率：{r[excavation_ratio_pct]:g} %

## 基地附近排水系統現況

{r[existing]}

## 工程造價單價（業主提供）

| 項目 | 規格 | 單價（元） |
|---|---|---|
{prices}
"""

CF_PROMPT = """# Level 4 chain-full：地籍套疊 → 平面圖 → 排水計畫書（跨領域串接）

桃園區某住宅新建工程（**合成資料**，座標為 TWD97 TM2 虛構位置，非真實基地；不含任何真實案名、地號、公司名）。
基地為{sec}（段號 {code}）的 2 筆地號：{lots}。地政資料上這 2 筆的登記面積合計為 {reg:.2f} m²。

原始資料在 `tests/level4/chain-full/input/`（清單與說明見該目錄 `design.md`）：地籍圖 `cadastre.shp`、
使用分區圖 `zoning.shp`、地面高程 `dtm.tif`、平面需求 `floorplan.md`、配置 `layout.geojson`（滯洪池、集水分區草圖）、
`nodes.csv`（節點位置與井深）、`links.csv`、`existing_drain.csv`、`road_center.csv`、`design.md`。

請依序完成三份成果（各自一次 `draftagent build`）：

1. **基地套疊**（`site-overlay`）：兩筆地號與使用分區套疊，各分區面積（m² 與坪）與合計，輸出 EPSG:3826 GeoPackage，並與登記面積對帳。
2. **住宅棟一層平面圖**（`floor-plan`，`FloorPlanSpec`）：依 `floorplan.md` 配置，產出 DXF 與 IFC。
3. **排水計畫書**（`drainage-report`，`DrainageReportSpec` 內含 `DrainageNetworkSpec`）：整套排水圖、水理計算與計畫書本文。
   - 基地界線與基地面積**取自第 1 步的套疊結果**；建物範圍**取自第 2 步的平面圖外框**（依 floorplan.md 的建物位置）
   - 集水分區、計畫書的土地座落 / 使用分區 / 建蔽率依 design.md 說明，由前面成果推得

**所有測量與設計數值以 input/ 為準**；input/ 沒有的資料要停下來回報缺什麼，不得自行假設或編造。
spec 不得填長度、坡度、面積、流量、數量、總價。

通過標準：三次 build 的 verifier 全部綠燈（允許 yaml verified: false 等 WARNING），且三份成果彼此一致，並貼出完整 verifier 輸出。
回覆只能說「通過本 repo 現有 verifier 的檢查項目」，不得宣稱符合法規。
"""


def task_chain_full(root: Path) -> None:
    import geopandas as gpd
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin
    from shapely.geometry import Polygon, box
    from shapely.ops import unary_union

    from draftagent.site.overlay import display_land_number, normalize_land_number

    l3 = _l3()
    d = root / "chain-full"
    inp = d / "input"
    inp.mkdir(parents=True, exist_ok=True)
    net = net_chain_full()
    e0, n0 = CF_ORIGIN

    def A(ps):
        return [(e0 + x, n0 + y) for x, y in ps]

    cad = gpd.GeoDataFrame(
        {
            "段名": [CF_SECTION[0]] * len(CF_PARCELS),
            "段號": [CF_SECTION[1]] * len(CF_PARCELS),
            "地號": [p[0] for p in CF_PARCELS],
            "地目": [p[1] for p in CF_PARCELS],
            "SOURCE": ["SYNTHETIC"] * len(CF_PARCELS),
        },
        geometry=[Polygon(A(p[2])) for p in CF_PARCELS],
        crs="EPSG:3826",
    )
    cad.to_file(inp / "cadastre.shp", encoding="UTF-8")
    zones = {
        "住宅區": box(e0 - 40, n0 - 40, e0 + 90, n0 + CF_ZONE_Y),
        "商業區": box(e0 - 40, n0 + CF_ZONE_Y, e0 + 90, n0 + 80),
    }
    gpd.GeoDataFrame(
        {"分區": list(zones), "SOURCE": ["SYNTHETIC"] * 2},
        geometry=list(zones.values()),
        crs="EPSG:3826",
    ).to_file(inp / "zoning.shp", encoding="UTF-8")

    west, north, cell = e0 - 20, n0 + 46, 1.0
    ncols, nrows = 96, 60
    xs = west + cell * (np.arange(ncols) + 0.5) - e0
    ys = north - cell * (np.arange(nrows) + 0.5) - n0
    z = (116.80 + 0.0105 * ys[:, None] - 0.002 * xs[None, :]).astype("float32")
    with rasterio.open(
        inp / "dtm.tif", "w", driver="GTiff", height=nrows, width=ncols, count=1, dtype="float32",
        crs="EPSG:3826", transform=from_origin(west, north, cell, cell), nodata=-9999.0,
    ) as dst:  # fmt: skip
        dst.write(z, 1)

    feats = [
        l3._feature(
            {
                "類別": "集水分區草圖",
                "分區編號": k,
                "土地使用": l3.LANDUSE_ZH[lu],
                "說明": desc,
                "排入": to,
            },
            net.abs(sk),
        )
        for k, (lu, desc, to, sk) in CF_SKETCH.items()
    ]
    feats.append(
        l3._feature(
            {"類別": "滯洪池範圍", "名稱": net.pond_name, "型式": "地面滯洪池"}, net.abs(net.pond)
        )
    )
    (inp / "layout.geojson").write_text(l3._geojson("layout", feats), encoding="utf-8")

    rows: list[list[Any]] = [
        ["編號", "種類", "E(m)", "N(m)", "井深(m)", "底高(m)", "頂高(m)", "備註"]
    ]
    for n in net.nodes:
        e, nn = net.abs([(n.x, n.y)])[0]
        dep = l3.r2(n.ground - n.invert) if n.kind != "outfall" else None
        rows.append([n.id, l3.KIND_ZH[n.kind], f"{e:.3f}", f"{nn:.3f}", l3._fmt(dep),
                     "" if dep else l3._fmt(n.invert), "", n.note])  # fmt: skip
    (inp / "nodes.csv").write_text(l3._csv(rows), encoding="utf-8")
    (inp / "links.csv").write_text(l3._csv(l3.link_rows(net)), encoding="utf-8")
    (inp / "existing_drain.csv").write_text(l3._csv(l3.drain_rows(net)), encoding="utf-8")
    (inp / "road_center.csv").write_text(l3._csv(l3.road_rows(net)), encoding="utf-8")

    fp = CF_FLOORPLAN
    bx, by = CF_BUILDING_SW
    fp_md = fp_design_md("chain-full", fp).replace(
        "- 牆厚：",
        f"- 建物位置：外框（外牆外緣）西南外角位於 TWD97 E={e0 + bx:.3f}、N={n0 + by:.3f}；"
        "平面圖 x 軸 = 正東、y 軸 = 正北（不旋轉）\n- 牆厚：",
        1,
    )
    (inp / "floorplan.md").write_text(fp_md, encoding="utf-8")
    lots = "、".join(CF_SITE_LOTS)
    prices = "\n".join(f"| {a} | {b} | {c} |" for a, b, c in CF_REPORT["prices"])
    (inp / "design.md").write_text(
        CF_MD.format(
            syn=l3.SYN, lots=lots, roof=CF_ROOF[0], roof_lu=l3.LANDUSE_ZH[CF_ROOF[1]], roof_to=CF_ROOF[3],
            project=l3.project_md(net), storage=l3.storage_md(net), sheet=l3.sheet_md(net), r=CF_REPORT,
            prices=prices,
        ),
        encoding="utf-8",
    )  # fmt: skip
    want = {normalize_land_number(x) for x in CF_SITE_LOTS}
    site_parcels = [p for p in CF_PARCELS if normalize_land_number(p[0]) in want]
    reg = round(sum(p[3] for p in site_parcels), 2)
    (d / "prompt.md").write_text(
        CF_PROMPT.format(sec=CF_SECTION[0], code=CF_SECTION[1], lots=lots, reg=reg),
        encoding="utf-8",
    )

    site = unary_union([Polygon(A(p[2])) for p in site_parcels])
    exp = net.expected()
    exp.update(fp_expected(fp))
    exp["registered_m2"] = reg
    exp["overlay_total_m2"] = site.area
    exp["zones_m2"] = {k: site.intersection(g).area for k, g in zones.items()}
    exp["parcels_m2"] = {display_land_number(p[0]): Polygon(A(p[2])).area for p in site_parcels}
    exp["section"] = CF_SECTION[0]
    exp["building_footprint"] = net.abs(cf_building())
    exp["building_area_m2"] = Polygon(cf_building()).area
    exp["coverage_ratio_pct"] = round(Polygon(cf_building()).area / site.area * 100, 2)
    exp["report"] = {
        "title": CF_REPORT["title"],
        "date": CF_REPORT["date"],
        "excavation_ratio_pct": CF_REPORT["excavation_ratio_pct"],
        "placeholders": {"applicant": "○○○", "designer": "○○○", "engineer": "○○○"},
    }
    exp["_note"] = (
        "合成資料。基地 = 地號 213 ∪ 214；建物 = floorplan.md 外框（L 形）平移至西南外角；屋頂分區 A1 = 建物外框；"
        "其餘分區 = 草圖 ∩ 基地 − 建物；建蔽率 = 外框面積 / 套疊面積 × 100（取至 0.01）。"
    )
    _write_json(d / "expected" / "expected.json", exp)


# ================================================================ registry / main

TASKS: dict[str, Any] = {
    "rooms-14": lambda root: task_rooms(root, "rooms-14"),
    "rooms-18": lambda root: task_rooms(root, "rooms-18"),
    "rooms-tight": lambda root: task_rooms(root, "rooms-tight"),
    "conflict-1": task_conflict,
    "design-large": task_design_large,
    "chain-full": task_chain_full,
}


def write_reference(root: Path) -> None:
    """以參考解答器（只讀 prompt.md + input/ 原始資料）寫 input/spec*.json 與 expected/replay_reply.md。"""
    from draftagent.eval import reference_solvers as rs

    for name in TASKS:
        d = root / name
        for old in (d / "input").glob("spec*.json"):
            old.unlink()
        sol = rs.solution(f"level4/{name}", d)
        for i, spec in enumerate(sol.specs):
            _write_json(d / "input" / ("spec.json" if i == 0 else f"spec-{i + 1}.json"), spec)
        (d / "expected" / "replay_reply.md").write_text(sol.reply.strip() + "\n", encoding="utf-8")


def make_all(root: Path = L4) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name, fn in TASKS.items():
        d = root / name
        for sub in ("input", "expected"):
            shutil.rmtree(d / sub, ignore_errors=True)
        d.mkdir(parents=True, exist_ok=True)
        fn(root)
    write_reference(root)


def _content(p: Path) -> bytes:
    b = p.read_bytes()
    if p.suffix == ".dbf" and len(b) > 4:  # DBF 檔頭第 1–3 byte 是最後更新日期（YYMMDD），不比
        b = b[:1] + b"\0\0\0" + b[4:]
    return b


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): _content(p)
        for name in TASKS
        for p in sorted((root / name).rglob("*"))
        if p.is_file()
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--check", action="store_true", help="只比對，不寫檔")
    a = ap.parse_args(argv)
    if a.check:
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t) / "level4"
            make_all(tmp)
            want, have = _snapshot(tmp), _snapshot(L4)
        diff = sorted(k for k in want.keys() | have.keys() if want.get(k) != have.get(k))
        for k in diff:
            print(f"DIFF {k}")
        print(f"level4 fixtures: {len(want)} 檔，{len(diff)} 個不同")
        return 1 if diff else 0
    make_all()
    print(f"wrote {L4}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
