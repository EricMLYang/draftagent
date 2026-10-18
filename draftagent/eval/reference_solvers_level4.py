"""Level 4 參考解答器（只讀 prompt.md + input/ 原始資料；見 `reference_solvers.py` 開頭的規則）。

由 `reference_solvers.py` 末尾併入 `SOLVERS`。本檔在 `draftagent/eval/` 下，評估工作區整個移除。

- rooms-14 / rooms-18 / rooms-tight：手排的**一個**可行配置（矩形房間，外框可為 L 形 / 凹口多邊形）；
  外框多邊形由 design.md 的文字描述解析（L 形缺角 / 入口凹口），與參考配置不符 → ValueError
- design-large：1.2 ha、11 個集水分區、筏基全抽水（受納溝設計水位高於重力可行範圍）的一個可行設計
- chain-full：地籍套疊 → 平面圖（建物外框）→ 排水計畫書（建物投影 = 屋頂分區、建蔽率 = 外框面積 / 套疊面積）
- conflict-1：需求互相矛盾（必要貯集量 > 可用綠地面積 × 最大水深，且不得筏基 / 抽水）→ 不 build，回覆指出衝突
"""

from __future__ import annotations

import math
import re
from typing import Any

from draftagent.eval import reference_solvers as rs
from draftagent.eval.reference_solvers import Solution, TaskInputs

# ---------------------------------------------------------------- 平面配置（rooms-14 / rooms-18 / rooms-tight）

# 房名 → (用途, x0, x1, y0, y1) mm；doors：(A, B[, side])；windows：(房名, 方位, 寬 mm)
FP4_LAYOUTS: dict[str, dict[str, Any]] = {
    "level4/rooms-14": {
        "rooms": {
            "客廳": ("客廳", 200, 5000, 200, 3800),
            "玄關": ("玄關", 5000, 6800, 200, 3800),
            "主臥室": ("臥室", 6800, 10600, 200, 3800),
            "主臥浴廁": ("浴廁", 10600, 12800, 200, 2200),
            "主臥衣櫃": ("儲藏", 10600, 12800, 2200, 3800),
            "走廊": ("走廊", 5000, 12800, 3800, 5000),
            "次臥室": ("臥室", 6200, 9600, 5000, 8200),
            "次臥衣櫃": ("儲藏", 9600, 10800, 5000, 8200),
            "公用浴廁": ("浴廁", 10800, 12800, 5000, 8200),
            "餐廳": ("餐廳", 200, 5000, 3800, 7400),
            "廚房": ("廚房", 200, 3600, 7400, 11800),
            "儲藏室": ("儲藏", 3600, 5000, 7400, 11800),
            "後走廊": ("走廊", 5000, 6200, 5000, 11800),
            "書房": ("書房", 6200, 8400, 8200, 11800),
        },
        "doors": [("玄關", "outside", "S"), ("玄關", "客廳"), ("玄關", "走廊"), ("客廳", "餐廳"),
                  ("餐廳", "廚房"), ("廚房", "儲藏室"), ("主臥室", "走廊"), ("主臥室", "主臥浴廁"),
                  ("主臥室", "主臥衣櫃"), ("後走廊", "走廊"), ("書房", "後走廊"), ("次臥室", "走廊"),
                  ("次臥室", "次臥衣櫃"), ("公用浴廁", "走廊")],
        "windows": [("客廳", "S", 1800), ("客廳", "W", 1500), ("主臥室", "S", 1500), ("次臥室", "N", 800),
                    ("書房", "N", 1200), ("廚房", "W", 1200), ("公用浴廁", "E", 600), ("主臥浴廁", "E", 600)],
    },
    "level4/rooms-18": {
        "rooms": {
            "客廳": ("客廳", 200, 6800, 200, 4600),
            "玄關": ("玄關", 6800, 9000, 1700, 4600),
            "客用浴廁": ("浴廁", 9000, 10200, 1700, 4600),
            "主臥室": ("臥室", 10200, 15000, 200, 4600),
            "主臥浴廁": ("浴廁", 15000, 17800, 200, 2600),
            "主臥衣櫃": ("儲藏", 15000, 17800, 2600, 4600),
            "走廊": ("走廊", 6800, 17800, 4600, 5800),
            "餐廳": ("餐廳", 200, 6800, 4600, 8000),
            "廚房": ("廚房", 200, 4400, 8000, 11800),
            "儲藏室": ("儲藏", 4400, 6800, 10000, 11800),
            "公用浴廁": ("浴廁", 4400, 6800, 8000, 10000),
            "後走廊": ("走廊", 6800, 8000, 5800, 11800),
            "洗衣間": ("其他", 8000, 11000, 5800, 7600),
            "書房": ("書房", 8000, 11000, 7600, 11800),
            "兒童房": ("臥室", 11000, 14000, 5800, 11800),
            "兒童房衣櫃": ("儲藏", 14000, 15200, 8000, 11800),
            "次臥衣櫃": ("儲藏", 14000, 15200, 5800, 8000),
            "次臥室": ("臥室", 15200, 17800, 5800, 11800),
        },
        "doors": [("玄關", "outside", "S"), ("玄關", "客廳"), ("玄關", "走廊"), ("客廳", "餐廳"),
                  ("餐廳", "廚房"), ("廚房", "儲藏室"), ("公用浴廁", "後走廊"), ("後走廊", "走廊"),
                  ("書房", "後走廊"), ("洗衣間", "走廊"), ("客用浴廁", "走廊"), ("主臥室", "走廊"),
                  ("主臥室", "主臥浴廁"), ("主臥室", "主臥衣櫃"), ("兒童房", "走廊"),
                  ("兒童房", "兒童房衣櫃"), ("次臥室", "走廊"), ("次臥室", "次臥衣櫃")],
        "windows": [("客廳", "S", 1800), ("客廳", "W", 1500), ("主臥室", "S", 1800), ("書房", "N", 1500),
                    ("兒童房", "N", 1500), ("次臥室", "E", 1500), ("次臥室", "N", 1200), ("廚房", "N", 1500),
                    ("主臥浴廁", "E", 600)],
    },
    "level4/rooms-tight": {
        "rooms": {
            "客廳": ("客廳", 200, 5000, 200, 4200),
            "玄關": ("玄關", 5000, 6600, 200, 4200),
            "主臥室": ("臥室", 6600, 10200, 200, 4200),
            "主臥浴廁": ("浴廁", 10200, 12200, 200, 2400),
            "儲藏室": ("儲藏", 10200, 12200, 2400, 4200),
            "走廊": ("走廊", 5000, 12200, 4200, 5300),
            "餐廳": ("餐廳", 200, 5000, 4200, 8200),
            "廚房": ("廚房", 5000, 8000, 5300, 8200),
            "公用浴廁": ("浴廁", 8000, 9800, 5300, 8200),
            "次臥室": ("臥室", 9800, 12200, 5300, 8200),
        },
        "doors": [("玄關", "outside", "S"), ("玄關", "客廳"), ("玄關", "走廊"), ("客廳", "餐廳"),
                  ("餐廳", "廚房"), ("主臥室", "走廊"), ("主臥室", "主臥浴廁"), ("儲藏室", "走廊"),
                  ("公用浴廁", "走廊"), ("次臥室", "走廊")],
        "windows": [("客廳", "S", 1800), ("主臥室", "S", 1500), ("次臥室", "N", 1200), ("次臥室", "E", 1200),
                    ("廚房", "N", 1200), ("主臥浴廁", "E", 600)],
    },
    "level4/chain-full": {
        "rooms": {
            "客廳": ("客廳", 200, 5200, 200, 4400),
            "玄關": ("玄關", 5200, 7000, 200, 4400),
            "主臥室": ("臥室", 7000, 11400, 200, 4400),
            "次臥室": ("臥室", 11400, 15800, 200, 4400),
            "走廊": ("走廊", 5200, 15800, 4400, 5600),
            "浴廁": ("浴廁", 9800, 12200, 5600, 7800),
            "書房": ("書房", 12200, 15800, 5600, 7800),
            "餐廳": ("餐廳", 200, 5200, 4400, 8000),
            "廚房": ("廚房", 200, 5200, 8000, 11800),
            "儲藏室": ("儲藏", 5200, 6400, 5600, 11800),
            "兒童房": ("臥室", 6400, 9800, 5600, 11800),
        },
        "doors": [("玄關", "outside", "S"), ("玄關", "客廳"), ("玄關", "走廊"), ("客廳", "餐廳"),
                  ("餐廳", "廚房"), ("主臥室", "走廊"), ("次臥室", "走廊"), ("兒童房", "走廊"),
                  ("書房", "走廊"), ("浴廁", "走廊"), ("儲藏室", "走廊")],
        "windows": [("客廳", "S", 1800), ("主臥室", "S", 1500), ("次臥室", "S", 1500), ("次臥室", "E", 1200),
                    ("書房", "N", 1200), ("兒童房", "N", 1500), ("廚房", "W", 1200)],
    },
}  # fmt: skip

_SIDE_FROM_ZH = {"南": "S", "北": "N", "東": "E", "西": "W"}
_CORNER = {"東北": ("E", "N"), "西北": ("W", "N"), "東南": ("E", "S"), "西南": ("W", "S")}


def _m(s: str) -> float:
    return round(float(s) * 1000, 3)


def parse_outline(text: str) -> list[tuple[float, float]]:
    """design.md「外框（外牆外緣）」文字 → 外框多邊形（mm，西南外角 = 原點，逆時針）。

    - 矩形：「東西 a m × 南北 b m，矩形」
    - L 形：「…外接矩形東西 a m × 南北 b m；東北角缺角（東西 c m × 南北 d m…」
    - 入口凹口（南側）：「…凹口西緣距外框西緣 e m、寬 f m、向北凹入 g m」
    """
    m = re.search(r"東西 ([\d.]+) m × 南北 ([\d.]+) m", text)
    if not m:
        raise ValueError(f"外框描述無法解析：{text!r}")
    W, D = _m(m.group(1)), _m(m.group(2))
    if "缺角" in text:
        c = re.search(r"(東北|西北|東南|西南)角缺角（東西 ([\d.]+) m × 南北 ([\d.]+) m", text)
        if not c:
            raise ValueError(f"缺角描述無法解析：{text!r}")
        ew, ns = _CORNER[c.group(1)]
        cw, cd = _m(c.group(2)), _m(c.group(3))
        if (ew, ns) != ("E", "N"):
            raise ValueError("參考解只支援東北角缺角")
        return [(0, 0), (W, 0), (W, D - cd), (W - cw, D - cd), (W - cw, D), (0, D)]
    if "凹口" in text:
        c = re.search(r"凹口西緣距外框西緣 ([\d.]+) m、寬 ([\d.]+) m、向北凹入 ([\d.]+) m", text)
        if not c or "南側" not in text:
            raise ValueError(f"凹口描述無法解析：{text!r}")
        x0, w, dep = _m(c.group(1)), _m(c.group(2)), _m(c.group(3))
        return [(0, 0), (x0, 0), (x0, dep), (x0 + w, dep), (x0 + w, 0), (W, 0), (W, D), (0, D)]
    if "矩形" not in text:
        raise ValueError(f"外框描述無法解析：{text!r}")
    return [(0, 0), (W, 0), (W, D), (0, D)]


def door_width_rules(sec_bullets: dict[str, str]) -> dict[str, float]:
    """門寬條列 → {entrance, default, <用途>: mm}。「通往浴廁或儲藏的門」→ 浴廁、儲藏同寬。"""
    out: dict[str, float] = {}
    for k, v in sec_bullets.items():
        w = rs._after(v, "") * 10
        if k == "主入口":
            out["entrance"] = w
        elif k == "其他室內門":
            out["default"] = w
        else:
            m = re.fullmatch(r"通往(.+)的門", k)
            if not m:
                raise ValueError(f"門寬條列無法解析：{k}")
            for use in re.split(r"或|、", m.group(1)):
                out[use.strip()] = w
    return out


def interior_door_width(uses: tuple[str, str], w: dict[str, float]) -> float:
    for u in uses:
        if u in w and u not in ("entrance", "default"):
            return w[u]
    return w["default"]


def fp_spec_from_layout(
    lay: dict[str, Any], doc: rs.MdDoc, *, outline: list[tuple[float, float]] | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """design.md（基本條件 / 房間清單 / 門寬）+ 參考配置 → FloorPlanSpec dict；回傳 (spec, 解析資訊)。"""
    base = doc.section("基本條件").bullets
    poly = outline if outline is not None else parse_outline(base["外框（外牆外緣）"])
    te, ti = rs._after(base["牆厚"], "外牆") * 10, rs._after(base["牆厚"], "內牆") * 10
    ent = base["主入口"]
    ent_side = _SIDE_FROM_ZH[re.search(r"設於(.)側", ent).group(1)]
    ent_room = re.search(r"「(.+?)」", ent).group(1)
    table = doc.section("房間清單").table()
    uses = {r["房名"]: r["用途"] for r in table}
    if set(uses) != set(lay["rooms"]) or any(uses[n] != v[0] for n, v in lay["rooms"].items()):
        raise ValueError("design.md 房間清單與參考配置不符，請同步更新 FP4_LAYOUTS")
    if (te, ti) != (200.0, 120.0):
        raise ValueError("參考配置以外牆 20 cm / 內牆 12 cm 排定")
    ent_door = lay["doors"][0]
    if ent_door[0] != ent_room or ent_door[2] != ent_side:
        raise ValueError("主入口與參考配置不符")
    w = door_width_rules(doc.section("門寬").bullets)
    rooms = [
        {"name": n, "use": uses[n], "rect": {"x": x0, "y": y0, "width": x1 - x0, "depth": y1 - y0}}
        for n, (_, x0, x1, y0, y1) in lay["rooms"].items()
    ]
    doors = []
    for i, d in enumerate(lay["doors"]):
        a, b = d[0], d[1]
        width = w["entrance"] if b == "outside" else interior_door_width((uses[a], uses[b]), w)
        door: dict[str, Any] = {"id": f"D{i + 1}", "between": [a, b], "width": width}
        if len(d) > 2:
            door["side"] = d[2]
        if b != "outside" and uses[b] in ("浴廁", "儲藏"):
            door["swing_into"] = b
        doors.append(door)
    windows = [
        {"id": f"W{i + 1}", "room": r, "side": s, "width": wd}
        for i, (r, s, wd) in enumerate(lay["windows"])
    ]
    spec = {
        "kind": "floor-plan",
        "units": "mm",
        "title": base["圖名"],
        "print_scale": rs._scale(base["出圖比例"]),
        "outline": {"polygon": [list(p) for p in poly]},
        "wall_thickness": {"exterior": te, "interior": ti},
        "rooms": rooms,
        "doors": doors,
        "windows": windows,
        "entrance": "D1",
    }
    return spec, {"entrance_side": ent_side, "entrance_room": ent_room, "n": len(rooms)}


def solve_rooms4(inp: TaskInputs, task_id: str) -> Solution:
    spec, info = fp_spec_from_layout(FP4_LAYOUTS[task_id], inp.md("design.md"))
    side = {v: k for k, v in _SIDE_FROM_ZH.items()}[info["entrance_side"]]
    reply = (
        f"依 design.md 配置 {info['n']} 個空間（外框依文字描述建成多邊形）：主入口在"
        f"{side}側，進入「{info['entrance_room']}」；"
        "房間以外牆內緣與內牆中心線為界、鋪滿室內；兩條走廊以門相通，衣櫃 / 儲藏室只經由指定房間進出，"
        "居室皆有對外窗。已用 floor-plan 圖種 build 產出 DXF 與 IFC，通過本 repo 現有 verifier 的檢查項目"
        "（不代表符合建築法規）。"
    )
    return Solution([spec], reply)


# ---------------------------------------------------------------- conflict-1（需求互相矛盾 → 不 build）


def _required_storage(area_m2: float) -> float:
    y = rs._runoff_yaml()
    return (
        max(
            y["runoff_standard"]["min_storage_m3_per_m2"],
            y["building_code_4_3"]["min_storage_m3_per_m2"],
        )
        * area_m2
    )


def solve_conflict_1(inp: TaskInputs, task_id: str) -> Solution:
    from shapely.geometry import Polygon

    feats = rs._geo_feats(inp.json("site.geojson"))
    site = Polygon(rs._poly(feats["基地界線"][0]))
    (green,) = [f for f in feats["地表分類"] if f["properties"]["說明"] == "東側綠地"]
    A_green = Polygon(rs._poly(green)).area
    cond = inp.md("design.md").section("設計條件（工程師交辦；每一項都是硬性需求）").text
    depth = float(re.search(r"有效水深 ≤ ([\d.]+) m", cond).group(1))
    if "無地下室" not in cond or "不同意設置抽水" not in cond or "不得設置任何滯洪設施" not in cond:
        raise ValueError("conflict-1 交辦條件與參考解假設不符（筏基 / 抽水 / 鋪面限制）")
    req = _required_storage(site.area)
    cap = A_green * depth
    if cap >= req:
        raise ValueError("參考解預期需求矛盾，但綠地容量足夠：請檢查 fixture")
    y = rs._runoff_yaml()
    k = max(
        y["runoff_standard"]["min_storage_m3_per_m2"],
        y["building_code_4_3"]["min_storage_m3_per_m2"],
    )
    reply = (
        "未出圖：design.md 的設計條件互相矛盾，無法同時滿足。\n"
        f"1. 必要貯集量：taoyuan_runoff.yaml 取 max(桃園逕流量標準, 建技 4-3) = {k:g} m³/m² × 基地 "
        f"{site.area:.2f} m² = {req:.2f} m³（verifier 檢核，不得調低）。\n"
        f"2. 可設滯洪池的範圍只有東側綠地（{A_green:.2f} m²），有效水深上限 {depth:.2f} m → 最多 "
        f"{cap:.2f} m³ < {req:.2f} m³；要達到必要貯集量，綠地內有效水深須 ≥ {req / A_green:.3f} m。\n"
        "3. 其他途徑都被條件排除：建物無地下室不得設筏基、鋪面不得設滯洪、業主不同意抽水。\n"
        "替代方案（需工程師 / 業主決定其一後再設計）：放寬綠地滯洪池有效水深；或允許部分鋪面 / 地下設貯留設施；"
        "或改用筏基 / 地下貯留槽。在決定之前不交付任何違反條件的圖。未宣稱符合法規。"
    )
    return Solution([], reply)


# ---------------------------------------------------------------- design-large（1.2 ha、筏基全抽水）
#
# 設計決定寫死（相對基地 bbox 西南角，m）；高程由測點平面擬合、受納溝設計水位、覆土準則推。
# 重力不可行：筏基頂版低於受納設計水位；綠地池的「進流井底 − 受納設計水位」只剩約 0.1 m（需 > 5000 m² 池面）。

_DL_NODES = {
    "CB1": ("catch_basin", 78, 92),
    "CB2": ("catch_basin", 95, 92),
    "CB3": ("catch_basin", 95, 62),
    "CB4": ("catch_basin", 95, 52),
    "CB5": ("catch_basin", 15, 80),
    "CB6": ("catch_basin", 15, 52),
    "MH1": ("manhole", 57, 52),
    "CB7": ("catch_basin", 5, 8),
    "CB8": ("catch_basin", 115, 8),
    "MH2": ("manhole", 57, 8),
    "SI1": ("storage_inlet", 56, 30),
    "OW1": ("outlet_well", 30, 9),
    "OF1": ("outfall", 30, -1.0),
}
_DL_LINKS = (  # id, from, to, D (m), 設計坡度
    ("L1", "CB1", "CB2", 0.25, 0.008),
    ("L2", "CB2", "CB3", 0.30, 0.006),
    ("L3", "CB3", "CB4", 0.40, 0.006),
    ("L4", "CB4", "MH1", 0.40, 0.006),
    ("L5", "CB5", "CB6", 0.20, 0.010),
    ("L6", "CB6", "MH1", 0.30, 0.006),
    ("L7", "MH1", "SI1", 0.50, 0.005),
    ("L8", "CB7", "MH2", 0.30, 0.006),
    ("L9", "CB8", "MH2", 0.35, 0.006),
    ("L10", "MH2", "SI1", 0.40, 0.006),
)
_DL_OUT_LINK = ("L11", "OW1", "OF1", 0.30)
_DL_PARTS = (  # 分區、相對多邊形、排入
    ("R1", [(10, 15), (50, 15), (50, 45), (10, 45)], "SI1"),
    ("R2", [(70, 15), (110, 15), (110, 45), (70, 45)], "CB8"),
    ("R3", [(30, 65), (90, 65), (90, 85), (30, 85)], "CB3"),
    ("G1", [(0, 60), (30, 60), (30, 100), (0, 100)], "CB5"),
    ("G2", [(90, 60), (120, 60), (120, 100), (90, 100)], "CB2"),
    ("P1", [(0, 0), (60, 0), (60, 45), (50, 45), (50, 15), (10, 15), (10, 45), (0, 45)], "CB7"),
    (
        "P2",
        [(60, 0), (120, 0), (120, 45), (110, 45), (110, 15), (70, 15), (70, 45), (60, 45)],
        "CB8",
    ),
    ("P3", [(0, 45), (60, 45), (60, 60), (0, 60)], "CB6"),
    ("P4", [(60, 45), (120, 45), (120, 60), (60, 60)], "CB4"),
    ("P5", [(30, 60), (90, 60), (90, 65), (30, 65)], "CB3"),
    ("P6", [(30, 85), (90, 85), (90, 100), (30, 100)], "CB1"),
)
_DL_RAFT = [(14, 18), (46, 18), (46, 42), (14, 42)]
_DL_RAFT_CELLS = ((14, 30, 18, 30), (30, 46, 18, 30), (14, 30, 30, 42), (30, 46, 30, 42))
_DL_SUMP = [(44.0, 18.5), (45.5, 18.5), (45.5, 20.0), (44.0, 20.0)]  # 抽水集水坑（自貯集量扣除）
_DL_RAFT_BOTTOM = 116.20
_DL_PUMP_STOP_ABOVE_BOTTOM = 0.0
_DL_WEIR_HEAD = 0.25


def _floor2(v: float) -> float:
    return math.floor(round(v * 100, 6)) / 100


def _drain_from_csv(inp: TaskInputs) -> dict[str, Any]:
    raw_d = inp.csv("existing_drain.csv")
    return {
        "id": raw_d[0]["編號"],
        "name": raw_d[0]["名稱"],
        "points": [
            {"x": float(r["E(m)"]), "y": float(r["N(m)"]), "top_el": float(r["溝頂高(m)"]),
             "invert_el": float(r["溝底高(m)"])}
            for r in raw_d
        ],
        "section": rs._section(raw_d[0]["斷面"]),
        "design_water_depth_m": rs._req(raw_d[0], "設計水深(m)", "existing_drain.csv"),
    }  # fmt: skip


def _road_from_csv(inp: TaskInputs) -> list[dict[str, Any]]:
    return [
        {"x": float(r["E(m)"]), "y": float(r["N(m)"]), "el": float(r["道路中心樁高(m)"])}
        for r in inp.csv("road_center.csv")
    ]


def solve_design_large(inp: TaskInputs, task_id: str) -> Solution:
    from shapely.geometry import Polygon, shape

    gj = inp.json("site.geojson")
    feats = rs._geo_feats(gj)
    site = rs._poly(feats["基地界線"][0])
    surfaces = [
        (shape(f["geometry"]), rs._landuse_key(f["properties"]["土地使用"]))
        for f in feats["地表分類"]
    ]
    raft_range = Polygon(rs._poly(feats["筏基可用範圍"][0]))
    ox, oy = min(p[0] for p in site), min(p[1] for p in site)
    ground = rs._plane_fit(inp.csv("ground_points.csv"))
    doc = inp.md("design.md")
    cond = doc.section("設計條件（工程師交辦）").text
    min_cover = rs._after(cond, "管頂覆土 ≥")
    raft_top = rs._after(cond, "筏基頂版底 EL")
    raft_bot_min = rs._after(cond, "筏基底不得低於 EL")
    y = rs._runoff_yaml()

    def P(x: float, yy: float) -> tuple[float, float]:
        return (round(ox + x, 3), round(oy + yy, 3))

    drain = _drain_from_csv(inp)
    pos = {k: P(x, yy) for k, (_, x, yy) in _DL_NODES.items()}
    gr = {k: round(ground(*pos[k]), 2) for k in pos}
    _, wl = rs._receiving_wl(drain, *pos["OF1"])
    inv = {"OF1": rs._ceil2(wl + 0.02)}
    inv["OW1"] = round(inv["OF1"] + 0.02, 2)

    # 上游節點底高 = 下游 + 長度 × 設計坡度；SI1 底高取「所有節點覆土 ≥ 準則」的最高值
    dmax: dict[str, float] = {}
    for _, a, b, d, _ in _DL_LINKS:
        dmax[a] = max(dmax.get(a, 0), d)
        dmax[b] = max(dmax.get(b, 0), d)
    down = {a: (b, sl) for _, a, b, _, sl in _DL_LINKS}

    def offset(n: str) -> float:
        if n == "SI1":
            return 0.0
        b, sl = down[n]
        return offset(b) + math.dist(pos[n], pos[b]) * sl

    si = _floor2(min(gr[n] - min_cover - dmax[n] - offset(n) for n in dmax))
    while True:
        inv.update({"SI1": si})
        for n in sorted(dmax, key=offset):
            if n != "SI1":
                b, sl = down[n]
                inv[n] = rs._ceil2(inv[b] + math.dist(pos[n], pos[b]) * sl)
        if all(gr[n] - inv[n] - dmax[n] >= min_cover - 1e-9 for n in dmax):
            break
        si = round(si - 0.01, 2)
    if inv["SI1"] < inv["OW1"] + 0.03:
        raise ValueError("進流井底低於放流井底：溢流無法以重力接放流井，請改設計")
    d_out = _DL_OUT_LINK[3]
    for n in ("OW1", "OF1"):
        if gr[n] - inv[n] - d_out < min_cover - 1e-9:
            raise ValueError(f"放流管 {n} 端覆土不足")

    # 筏基（全抽水）：有效水深依必要貯集量；起抽 → 停機容量運轉 ≥ yaml pump_min_run_min（取 1.2 倍）
    A_site = Polygon(site).area
    req = _required_storage(A_site)
    cells = [
        [list(P(x0, y0)), list(P(x1, y0)), list(P(x1, y1)), list(P(x0, y1))]
        for x0, x1, y0, y1 in _DL_RAFT_CELLS
    ]
    raft_poly = [list(P(x, yy)) for x, yy in _DL_RAFT]
    if Polygon(raft_poly).difference(raft_range.buffer(1e-6)).area > 1e-6:
        raise ValueError("參考筏基超出筏基可用範圍")
    A_well = Polygon(_DL_SUMP).area
    A_raft = Polygon(_DL_RAFT).area
    depth = rs._ceil2(req / (A_raft - A_well)) + 0.01
    bottom = _DL_RAFT_BOTTOM
    if bottom < raft_bot_min:
        raise ValueError("參考筏基底低於結構限制")
    max_wl = round(bottom + depth, 2)
    d_in = max(d for _, _, b, d, _ in _DL_LINKS if b == "SI1")
    if (
        max_wl + d_in > raft_top
    ):  # 入池進流管底 = 設計滿水位（E5），管頂不得高於坑頂（detail_check）
        raise ValueError("參考筏基：進流管頂高於頂版底")
    allowed = A_site * y["runoff_standard"]["max_release_cms_per_m2"]
    pump_q = math.floor(allowed * 0.95 * 1000) / 1000
    run_s = y["design_criteria"]["pump_min_run_min"]["value"] * 60 * 1.2
    stop = round(bottom + _DL_PUMP_STOP_ABOVE_BOTTOM, 2)
    start = round(stop + math.ceil(pump_q * run_s / (A_raft - A_well) * 20) / 20, 2)
    # 溢流堰：Q ≥ Q20（全基地綜合 C × 雨量站 20 年 tc = yaml 建議值）
    cls = y["runoff_coefficients"]["classes"]
    C = sum(cls[lu]["median"] * g.area for g, lu in surfaces) / A_site
    station = next(
        k
        for k, v in y["horner"]["stations"].items()
        if doc.section("專案").bullets["行政區"] in v["districts"]
    )
    h20 = y["horner"]["stations"][station]["T20"]
    tc = 5.0
    q20 = C * h20["a"] / (tc + h20["b"]) ** h20["c"] * A_site / 3.6e6
    k_w = y["orifice_weir"]["rect_weir_coeff"]
    weir_w = math.ceil(q20 * 1.1 / (k_w * _DL_WEIR_HEAD**1.5) * 10) / 10

    nodes = [
        {
            "id": k,
            "kind": kind,
            "x": pos[k][0],
            "y": pos[k][1],
            "ground_el": gr[k],
            "invert_el": inv[k],
        }
        for k, (kind, _, _) in _DL_NODES.items()
    ]
    links = [
        {"id": lid, "from": a, "to": b, "section": {"type": "pipe", "d": d},
         "material": "plastic_frp_pipe", "n_override": 0.013}
        for lid, a, b, d, _ in (*_DL_LINKS, (*_DL_OUT_LINK, None))
    ]  # fmt: skip
    subs = []
    for sid, part, out in _DL_PARTS:
        g = Polygon([P(x, yy) for x, yy in part])
        lus = {lu for sg, lu in surfaces if sg.intersection(g).area > 1e-6}
        if len(lus) != 1:
            raise ValueError(f"參考分區 {sid} 跨 {lus} 種土地使用")
        subs.append({"id": sid, "polygon": [list(p) for p in g.exterior.coords[:-1]], "landuse": lus.pop(),
                     "outlet_node": out})  # fmt: skip
    if abs(sum(Polygon(sc["polygon"]).area for sc in subs) - A_site) > 0.01:
        raise ValueError("參考分區未涵蓋整個基地")
    proj = doc.section("專案").bullets
    scales, nos, sheet = rs._sheet_from_md(doc)
    spec = {
        "kind": "drainage-plan",
        "units": "m",
        "crs": "EPSG:3826",
        "title": proj["圖面總標題"],
        "project": {"name": proj["工程名稱"], "district": proj["行政區"], "location": proj["位置"]},
        "site_boundary": site,
        "building_footprints": [rs._poly(f) for f in feats["建物"]],
        "nodes": nodes,
        "links": links,
        "subcatchments": subs,
        "storage": {
            "id": "B1筏基滯洪",
            "type": "raft",
            "polygon": raft_poly,
            "cells": cells,
            "effective_depth": depth,
            "inlet_node": "SI1",
            "outlet_node": "OW1",
            "outlet_well_polygon": [list(P(x, yy)) for x, yy in _DL_SUMP],
            "drain_mode": "pump",
            "bottom_el": bottom,
            "max_water_el": max_wl,
            "top_el": raft_top,
            "overflow_el": round(inv["SI1"] - 0.01, 2),
            "overflow_at": "inlet_well",
            "pump_start_el": start,
            "pump_stop_el": stop,
            "pump_q_cms": pump_q,
            "inflow_invert_el": max_wl,
            "pump_outlet_invert_el": round(gr["OW1"] - 0.50, 2),
            "overflow_pipe_invert_el": round(inv["OW1"] + 0.01, 2),
            "beam_width": 0.4,
        },
        "existing_drains": [drain],
        "road_centerline_els": _road_from_csv(inp),
        "calc": {"overflow_weir": {"width_m": weir_w, "head_m": _DL_WEIR_HEAD}},
        "detail_defaults": {"raft": {"scale": 100}},  # 30 m 筏基 1/50 放不進 A1
        "scales": scales,
        "sheet": sheet,
        "drawing_nos": nos,
    }
    oc = rs.yaml.safe_load(
        (rs.REPO_ROOT / "draftagent/hydro/codes/outflow_control.yaml").read_text(encoding="utf-8")
    )
    th = oc["applicability"]["general_threshold_ha"]
    reply = (
        f"設計說明：基地 {A_site:,.0f} m²（{A_site / 1e4:.2f} ha），11 個集水分區（每區 ≤ 1,500 m²、單一土地使用），"
        f"全部經進流井 SI1 入 B1 地下室筏基（4 格、有效水深 {depth:.2f} m，提供 ≥ 必要貯集量 {req:.2f} m³）。\n"
        f"排放方式：受納溝 {drain['id']} 出流口處設計水位約 EL {wl:.2f}，筏基頂版底 EL {raft_top:.2f} 低於受納溝設計水位，"
        f"且進流井底 EL {inv['SI1']:.2f} 與受納設計水位只差約 {inv['SI1'] - wl:.2f} m，綠地重力池需數千 m² 池面，"
        f"重力排放不可行 → 改全抽水（抽水量 {pump_q:.3f} m³/s，約容許排放量 {pump_q / allowed:.0%}），"
        "溢流設於進流井、以溢流管重力接放流井。\n"
        f"法規路徑：(1) 桃園市建築基地逕流量標準與建技規則 4-3（取大者）——本次 drainage-plan 計算；"
        f"(2) 基地 {A_site / 1e4:.2f} ha ≥ outflow_control.yaml applicability.general_threshold_ha = {th:g} ha，"
        "依出流管制計畫書與規劃書審核監督及免辦認定辦法第 2 條應另提出流管制計畫書（且已達 1 ha，不適用第 2-1 條備查免提）；"
        "開發樣態是否屬第 2 條所列、有無免辦例外須由工程師判定；本案為抽水排放，不符第十四點簡易檢核的重力排水條件。\n"
        "須工程師確認：法規路徑與開發樣態、筏基有效水深與起抽 / 停機水位、抽水量、溢流堰尺寸、管徑坡度；"
        "集水面積 > 1,000 m² 管段的最小管徑 yaml 未收錄（verifier 僅 WARNING）；yaml 為 verified: false。\n"
        "通過本 repo 現有 verifier 的檢查項目（不代表符合法規）。"
    )
    return Solution([spec], reply)


# ---------------------------------------------------------------- chain-full（套疊 → 平面圖 → 排水計畫書）


def _unit_prices(doc: rs.MdDoc) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """design.md「工程造價單價」表 → (unit_prices, extra_costs, 缺單價項目)；規則同 level3 report-1。"""
    prices, extra, missing = [], [], []
    for r in doc.section("工程造價單價").table():
        price = rs._opt(r["單價（元）"]) if re.fullmatch(rs._NUM, r["單價（元）"].strip()) else None
        m = rs._REPORT_EXTRA.match(r["項目"])
        if m:
            extra.append({"item": m.group("item"), "spec": r["規格"], "unit": m.group("unit"),
                          "qty": float(m.group("qty")), "unit_price": price})  # fmt: skip
            continue
        if price is None:
            missing.append(r["項目"])
            continue
        prices.append(
            {
                "item": r["項目"],
                "spec": None if r["規格"] == "—" else r["規格"],
                "unit_price": price,
            }
        )
    return prices, extra, missing


def solve_chain_full(inp: TaskInputs, task_id: str) -> Solution:
    import geopandas as gpd
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    from draftagent.site.overlay import display_land_number, normalize_land_number

    p = inp.prompt()
    m = re.search(r"([^\s為：，。]+段)（段號 (\d+)）的 \d+ 筆地號：([^。]+)。", p)
    sec_name, sec_code = m.group(1), m.group(2)
    lands = [x.strip() for x in m.group(3).split("、")]
    task_rel = f"tests/{task_id}/input"
    site_spec = {
        "parcels": [{"section_name": sec_name, "section_code": sec_code, "land_numbers": lands}],
        "cadastre_path": f"{task_rel}/cadastre.shp",
        "zoning_path": f"{task_rel}/zoning.shp",
        "zoning_field": "分區",
        "target_crs": "EPSG:3826",
        "output_path": "runs/level4-chain-full/site.gpkg",
        "expected_area_m2": rs._after(p, "登記面積合計為"),
        "expected_area_source": "prompt（地政資料登記面積合計）",
    }
    cad = gpd.read_file(inp.path("cadastre.shp"))
    want = {normalize_land_number(x) for x in lands}
    sel = cad[cad["地號"].map(normalize_land_number).isin(want)]
    if len(sel) != len(want):
        raise ValueError(f"地籍圖找不到全部地號 {lands}")
    union = unary_union(list(sel.to_crs(3826).geometry)).simplify(0)
    if union.geom_type != "Polygon":
        raise ValueError("選取地號聯集不是單一多邊形")
    ring = [[round(x, 3), round(y, 3)] for x, y in union.exterior.coords[:-1]]
    site_poly = Polygon(ring)
    zon = gpd.read_file(inp.path("zoning.shp")).to_crs(3826)
    zone_names = [
        z
        for z, g in zip(zon["分區"], zon.geometry, strict=True)
        if g.intersection(site_poly).area > 0.01
    ]

    # 平面圖：外框依 floorplan.md 文字；建物位置 = 外框西南外角 TWD97 座標（不旋轉）
    fdoc = inp.md("floorplan.md")
    fp_spec, _ = fp_spec_from_layout(FP4_LAYOUTS[task_id], fdoc)
    pos = re.search(r"E=([\d.]+)、N=([\d.]+)", fdoc.section("基本條件").bullets["建物位置"])
    ax, ay = float(pos.group(1)), float(pos.group(2))
    outline = fp_spec["outline"]["polygon"]
    x0, y0 = min(q[0] for q in outline), min(q[1] for q in outline)
    bld = [[round(ax + (q[0] - x0) / 1000, 3), round(ay + (q[1] - y0) / 1000, 3)] for q in outline]
    bld_poly = Polygon(bld)

    # 排水：基地 = 套疊聯集；屋頂分區 = 建物外框；其餘 = 草圖 ∩ 基地 − 建物
    doc = inp.md("design.md")
    raw = rs._read_net_raw(
        inp, geo="layout.geojson", site_ring=ring, ground=rs._dtm_sampler(inp.path("dtm.tif"))
    )
    roof = re.search(
        r"屋頂分區 (\S+) = 建物外框（土地使用「(.+?)」，落水管排入 (\S+?)）",
        doc.section("原始資料清單").text,
    )
    subs = [
        {
            "id": roof.group(1),
            "polygon": bld,
            "landuse": rs._landuse_key(roof.group(2)),
            "outlet_node": roof.group(3),
        }
    ]
    for f in raw.feats["集水分區草圖"]:
        g = Polygon(rs._poly(f)).intersection(site_poly).difference(bld_poly).simplify(0)
        if g.geom_type != "Polygon" or list(g.interiors):
            raise ValueError(
                f"草圖 {f['properties']['分區編號']} 裁切 / 扣除建物後不是單一無洞多邊形"
            )
        pr = f["properties"]
        subs.append({"id": pr["分區編號"], "polygon": [[round(x, 3), round(y, 3)] for x, y in g.exterior.coords[:-1]],
                     "landuse": rs._landuse_key(pr["土地使用"]), "outlet_node": pr["排入"]})  # fmt: skip
    net = rs._net_spec(raw, doc, site=ring, subcatchments=subs)
    net["building_footprints"] = [bld]

    info = doc.section("計畫書資訊").bullets
    prices, extra, missing = _unit_prices(doc)
    report = {
        "title": info["計畫書名稱"],
        "date": info["日期"],
        "land": [
            {"section": sec_name, "lot": display_land_number(normalize_land_number(x)).zfill(4)}
            for x in lands
        ],
        "zoning": "、".join(zone_names),
        "coverage_ratio_pct": round(bld_poly.area / site_poly.area * 100, 2),
        "excavation_ratio_pct": rs._num(info["開挖率"]),
        "existing_drainage": " ".join(
            ln.strip() for ln in doc.section("基地附近排水系統現況").lines if ln.strip()
        ),
        "unit_prices": prices,
        "extra_costs": extra,
    }
    reply = (
        f"三份成果串接一致：基地 = {sec_name}地號 {'、'.join(lands)} 聯集（套疊面積 {site_poly.area:.2f} m²），"
        f"排水計畫的基地界線與計算面積取自同一份地籍幾何；建物外框取自平面圖（{bld_poly.area:.2f} m²，"
        f"依建物位置平移至 TWD97），屋頂分區 = 建物外框、其餘分區 = 草圖 ∩ 基地 − 建物；"
        f"建蔽率 = {report['coverage_ratio_pct']:.2f} %；使用分區 {report['zoning']}。"
        + (f"{'、'.join(missing)}單價未提供，造價表留空。" if missing else "")
        + "申請人、設計單位、簽證技師以 ○○○ 佔位。三次 build 皆通過本 repo 現有 verifier 的檢查項目（不代表符合法規）。"
    )
    return Solution(
        [site_spec, fp_spec, {"kind": "drainage-report", "network": net, "report": report}], reply
    )


LEVEL4_SOLVERS: dict[str, Any] = {
    "level4/chain-full": solve_chain_full,
    "level4/design-large": solve_design_large,
    "level4/conflict-1": solve_conflict_1,
    "level4/rooms-14": solve_rooms4,
    "level4/rooms-18": solve_rooms4,
    "level4/rooms-tight": solve_rooms4,
}
