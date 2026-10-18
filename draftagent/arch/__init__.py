"""讀建築師 DXF（工作台待辦 #94）。純函式，不得呼叫 LLM。

原則：工具只「看懂」與「轉換」，判斷（哪個圖層是外牆、用哪些控制點）交給 agent / 工程師。

模組分工（三個並行開發者的契約）：
- `draftagent/arch/inspect.py`：`inspect_dxf(path) -> InspectReport`（dict 可 JSON 化）+ `draftagent inspect-dxf` CLI
- `draftagent/arch/extract.py`：`ArchImportSpec`（schema 在 `draftagent/schema/arch.py`）→ 抽圖層、接線成封閉多邊形、
  控制點相似變換 → TWD97 GeoJSON；`kinds/arch_import.py` 註冊圖種 `arch-import`
- `draftagent/verify/arch_check.py`：控制點殘差、縮放比、面積守恆、外框在基地內、多邊形有效、可追溯
- 合成 fixture：`tools/make_fixtures_archdxf.py` → `tests/fixtures/archdxf/<case>/`

Fixture 契約（每個 case 一個資料夾）：
- `drawing.dxf`：雜亂的建築師平面圖
- `truth.json`：
  {
    "case": "<名稱>",
    "units": "mm" | "m",                       # 圖上實際單位（可能與 $INSUNITS 不符）
    "insunits_header": <int|null>,              # 圖檔 header 寫的值（可能是錯的）
    "layers": {"exterior_wall": "<圖層名>", "basement": "<圖層名|null>", ...},
    "control_points": [{"id": "CP1", "drawing": [x, y], "twd97": [E, N]}, ...],   # ≥ 3 點
    "footprint_twd97": [[E, N], ...],           # 一樓建物外框（外牆外緣）真值，逆時針、不重複首點
    "basement_twd97": [[E, N], ...] | null,
    "floor_filter": "<1F 圖層前綴或圖框名稱|null>",
    "site_twd97": [[E, N], ...],                 # 基地界線（給 verifier 檢查外框在基地內）
    "gap_mm": <float>,                           # 故意留的接縫最大值
    "notes": "這個 case 在測什麼"
  }
"""
