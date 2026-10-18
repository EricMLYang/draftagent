"""產生 Level 3-1 多房間平面配置題（tests/level3/rooms-4、rooms-7、rooms-10）的【合成】fixture。

    python tools/make_fixtures_floorplan.py            # 重寫 prompt.md、input/design.md、expected/、input/spec.json
    python tools/make_fixtures_floorplan.py --check    # 只比對：與磁碟上檔案不同 → exit 1

PLAN §6 3-1 梯度：房間數 4 → 7 → 10（含走廊 / 玄關），只給**設計需求**（外框、牆厚、房間清單與
淨面積需求、鄰接需求、門寬、主入口方位），不給座標——agent 要自己做空間配置。

- `input/design.md`：工程師交辦的需求（條列 + 表格）
- `expected/expected.json`：需求本身（房間 / 用途 / 面積目標 / 鄰接 / 主入口），grading="properties" 用
- `input/spec.json`、`expected/replay_reply.md`：參考解（`draftagent/eval/reference_solvers.py` 只讀
  design.md 解出的**一個**可行配置；replay 用），評估工作區會刪除

合成需求，不含任何真實案名、地號、公司名。本檔名符合 `tools/make_fixtures_*.py`，評估工作區會移除。
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
L3 = REPO / "tests" / "level3"
SIDE_ZH = {"S": "南", "N": "北", "E": "東", "W": "西"}

# 需求資料（不是配置答案：沒有任何座標）
BRIEFS: dict[str, dict[str, Any]] = {
    "rooms-4": {
        "title": "小套房平面圖",
        "outline": (8000, 6000),
        "walls": (200, 120),
        "scale": 100,
        "entrance": ("S", "客廳"),
        "rooms": [
            ("客廳", "客廳", 14.0),
            ("臥室", "臥室", 11.0),
            ("廚房", "廚房", 12.0),
            ("浴廁", "浴廁", 4.0),
        ],
        "adjacency": [
            ("客廳", "廚房", "door"),
            ("客廳", "臥室", "door"),
            ("浴廁", "客廳", "door"),
        ],
        "only_via": {},
    },
    "rooms-7": {
        "title": "二房住宅平面圖",
        "outline": (10000, 8000),
        "walls": (200, 120),
        "scale": 100,
        "entrance": ("S", "玄關"),
        "rooms": [
            ("玄關", "玄關", 6.0),
            ("客廳", "客廳", 15.0),
            ("廚房", "廚房", 13.0),
            ("走廊", "走廊", None),
            ("主臥室", "臥室", 12.0),
            ("次臥室", "臥室", 11.0),
            ("浴廁", "浴廁", 5.5),
        ],
        "adjacency": [
            ("玄關", "客廳", "door"),
            ("廚房", "客廳", "door"),
            ("客廳", "走廊", "door"),
            ("主臥室", "走廊", "door"),
            ("次臥室", "走廊", "door"),
            ("浴廁", "走廊", "door"),
        ],
        "only_via": {},
    },
    "rooms-10": {
        "title": "三房住宅平面圖",
        "outline": (14000, 9000),
        "walls": (200, 120),
        "scale": 100,
        "entrance": ("S", "玄關"),
        "rooms": [
            ("玄關", "玄關", 6.5),
            ("客廳", "客廳", 17.0),
            ("餐廳", "餐廳", 12.0),
            ("廚房", "廚房", 11.0),
            ("走廊", "走廊", None),
            ("主臥室", "臥室", 13.0),
            ("主臥浴廁", "浴廁", 5.0),
            ("次臥室", "臥室", 16.0),
            ("兒童房", "臥室", 16.0),
            ("公用浴廁", "浴廁", 5.0),
        ],
        "adjacency": [
            ("玄關", "客廳", "door"),
            ("玄關", "走廊", "door"),
            ("餐廳", "客廳", "door"),
            ("廚房", "餐廳", "door"),
            ("主臥室", "走廊", "door"),
            ("次臥室", "走廊", "door"),
            ("兒童房", "走廊", "door"),
            ("公用浴廁", "走廊", "door"),
            ("主臥室", "主臥浴廁", "door"),
            ("公用浴廁", "主臥浴廁", "adjacent"),
        ],
        "only_via": {"主臥浴廁": ["主臥室"]},
    },
}
DOOR_W = {"entrance": 900, "浴廁": 700, "default": 800}
WINDOW_REQUIRED = ("客廳", "臥室")  # 本題「居室」= 這兩種用途（plan_rules.yaml window_required 的子集）
AREA_TOL = 0.10
MIN_SHARED_WALL_MM = 900  # 「相鄰」= 共用牆長 ≥ 此值（題目明文）


def design_md(name: str, b: dict[str, Any]) -> str:
    W, D = b["outline"]
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
            adj.append(f"- {a} 與 {c} 相鄰（共用一道牆，共用長度 ≥ {MIN_SHARED_WALL_MM / 10:g} cm）")
    for a, via in b["only_via"].items():
        adj.append(f"- {a} 只能經由 {'、'.join(via)} 進出（{a} 的門只通往 {'、'.join(via)}）")
    habit = [n for n, u, _ in b["rooms"] if u in WINDOW_REQUIRED]
    return f"""# 設計需求：{b["title"]}（{name}）

> 合成需求，非真實案件；不含任何真實案名、地號、公司名。

## 基本條件

- 圖名：{b["title"]}
- 外框（外牆外緣）：東西 {W / 1000:g} m × 南北 {D / 1000:g} m，矩形
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
- 所有空間都必須能由主入口經室內的門到達
- 居室（{"、".join(habit)}）每間至少一扇對外窗

## 門寬

- 主入口：{DOOR_W["entrance"] / 10:g} cm
- 通往浴廁的門：{DOOR_W["浴廁"] / 10:g} cm
- 其他室內門：{DOOR_W["default"] / 10:g} cm
"""


def prompt_md(name: str, b: dict[str, Any]) -> str:
    n = len(b["rooms"])
    return f"""# Level 3 {name}：多房間平面配置（{n} 個空間）

住宅單層平面（**合成需求**，非真實案件；不含任何真實案名、地號、公司名）。
工程師只給設計需求 `tests/level3/{name}/input/design.md`（外框尺寸、牆厚、房間清單與淨面積需求、鄰接與動線需求、
門寬、主入口方位），**沒有座標**：各空間的位置與形狀、門窗位置由你配置。

請寫 `FloorPlanSpec`（`draftagent/schema/floorplan.py`），以 `floor-plan` 圖種 `draftagent build`
一次產出 DXF 平面圖與 IFC，並貼出完整 verifier 輸出。房名與用途照 design.md 表格；需求數值以 input/ 為準，
資料不足以配置時要停下來回報缺什麼，不得自行假設。回覆請簡述配置（各空間位置、動線、門窗）。

通過標準：verifier 全部綠燈，且符合 design.md 的全部需求（面積容許 ±{AREA_TOL:.0%}）。
回覆只能說「通過本 repo 現有 verifier 的檢查項目」，不得宣稱符合法規。
"""


def expected(b: dict[str, Any]) -> dict[str, Any]:
    W, D = b["outline"]
    te, ti = b["walls"]
    side, ent_room = b["entrance"]
    return {
        "units": "mm",
        "outline_mm": [W, D],
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
    }


def make_all(root: Path = L3) -> None:
    from draftagent.eval import reference_solvers as rs

    for name, b in BRIEFS.items():
        d = root / name
        for sub in ("input", "expected"):
            shutil.rmtree(d / sub, ignore_errors=True)
            (d / sub).mkdir(parents=True)
        (d / "prompt.md").write_text(prompt_md(name, b), encoding="utf-8")
        (d / "input" / "design.md").write_text(design_md(name, b), encoding="utf-8")
        (d / "expected" / "expected.json").write_text(
            json.dumps(expected(b), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        sol = rs.solution(f"level3/{name}", d)
        (d / "input" / "spec.json").write_text(
            json.dumps(sol.specs[0], ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (d / "expected" / "replay_reply.md").write_text(sol.reply.strip() + "\n", encoding="utf-8")


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for name in BRIEFS
        for p in sorted((root / name).rglob("*"))
        if p.is_file()
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="只比對，不寫檔")
    a = ap.parse_args(argv)
    if a.check:
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t) / "level3"
            make_all(tmp)
            want, have = _snapshot(tmp), _snapshot(L3)
        diff = sorted(k for k in want.keys() | have.keys() if want.get(k) != have.get(k))
        for k in diff:
            print(f"DIFF {k}")
        print(f"floor-plan fixtures: {len(want)} 檔，{len(diff)} 個不同")
        return 1 if diff else 0
    make_all()
    print(f"wrote {', '.join(str(L3 / n) for n in BRIEFS)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
