"""整套排水圖（平面 / 縱斷面 / 詳圖）共用的圖面文字詞彙。純常數，不得呼叫 LLM。

`drainage/derive.py` 依這裡的標籤產生縱斷面 levels / structures 與詳圖標題，
`verify/sheetset_check.py` 依同一份詞彙把各圖 TEXT 解析回「元素 id → 屬性」再互相比對。
只放**文字契約**，不放任何數值。
"""

from __future__ import annotations

# 元素種類（sheetset 對照表的 key 第一欄）
STORAGE = "storage"
RECEIVING = "receiving"  # 受納溝（出流口處內插）

# 滯洪設施水位名詞（兩者不同）：
# 設計滿水位（貯留水位）= 池底 + 有效水深，≤ 溢流口；溢流時最高水位 = 溢流口 + 堰頂水頭，≤ 池頂
STORAGE_LEVEL_LABEL = "設計滿水位"
OVERFLOW_FLOOD_LABEL = "溢流時最高水位"

# 滯洪設施水位 / 銜接高程：圖面標籤 → StorageLevels 屬性
STORAGE_MARKS: dict[str, str] = {
    STORAGE_LEVEL_LABEL: "max_water_el",
    OVERFLOW_FLOOD_LABEL: "overflow_flood_el",
    "起抽水位": "pump_start_el",
    "停機水位": "pump_stop_el",
    "溢流口": "overflow_el",
    "孔口底": "orifice_invert_el",
    "入池進流管底": "inflow_invert_el",
}
OVERFLOW_AT_INLET_LABEL = "溢流口（進流井）"
PUMP_OUTLET_LABEL = "抽水出水管"  # 縱斷面 level（放流井處）
OVERFLOW_PIPE_LABEL = "溢流管"  # 縱斷面 level（放流井處；進流井溢流管入井管底）
OUTLET_PIPE_PREFIX = "放流管"  # 「放流管<link id>」= 該 link 上游端管底；單獨「放流管」= 出流口管底
INLET_PIPE_PREFIX = "進水管"  # 「進水管<link id>」= 該 link 下游端管底
RECEIVING_LABEL = "受納溝"  # 縱斷面構造物 → 「受納溝頂 / 受納溝底」
RECEIVING_WL_LABEL = "受納溝設計水位"
OUTLET_WELL_PREFIX = "放流井"  # 縱斷面構造物「放流井<node id>」

# 結構物頂 / 底（縱斷面 structure 文字「<label>頂 EL=」「<label>底 EL=」）
TOP_SUFFIX = "頂"
BOTTOM_SUFFIX = "底"

# 詳圖標題：「<圖名>（<元素 id>[、<元素 id>…]）」
DETAIL_TITLE = {
    "u_ditch": "U型溝斷面詳圖",
    "raft_detention": "筏基滯洪坑剖面詳圖",
    "outlet_well": "放流井詳圖",
    "ditch_joint": "出流口接既有側溝詳圖",
    "outfall_protection": "出水口保護工詳圖",
    "nameplate": "滯洪設施銘牌詳圖",
    "orifice_plate": "孔口板詳圖",  # 標題「孔口板詳圖 <孔口尺寸>（<滯洪設施 id>）」
}
ORIFICE_PLATE_CENTER_LABEL = "孔心"  # 孔口板詳圖「孔心 EL=」= 滯洪設施孔口中心


# 屬性的中文名（錯誤訊息用）
ATTR_NAME = {
    "invert_el": "底 EL",
    "top_el": "頂 EL",
    "bottom_el": "池底 EL",
    "max_water_el": "設計滿水位 EL",
    "overflow_flood_el": "溢流時最高水位 EL",
    "pump_start_el": "起抽水位 EL",
    "pump_stop_el": "停機水位 EL",
    "overflow_el": "溢流口 EL",
    "orifice_invert_el": "孔口底 EL",
    "inflow_invert_el": "入池進流管底 EL",
    "pump_outlet_invert_el": "抽水出水管入井管底 EL",
    "overflow_pipe_invert_el": "溢流管入井管底 EL",
    "design_wl": "設計水位 EL",
    "slope_pct": "坡度 S %",
    "length_m": "長度 L m",
    "section": "斷面",
    "volume_m3": "貯集量 m³",
    "effective_depth_m": "有效水深 m",
    "orifice_size": "孔口尺寸 mm",
    "orifice_center_el": "孔口中心 EL",
    "road_el": "路中 EL",
}
