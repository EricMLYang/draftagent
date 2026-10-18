# Level 3 rooms-4：多房間平面配置（4 個空間）

住宅單層平面（**合成需求**，非真實案件；不含任何真實案名、地號、公司名）。
工程師只給設計需求 `tests/level3/rooms-4/input/design.md`（外框尺寸、牆厚、房間清單與淨面積需求、鄰接與動線需求、
門寬、主入口方位），**沒有座標**：各空間的位置與形狀、門窗位置由你配置。

請寫 `FloorPlanSpec`（`draftagent/schema/floorplan.py`），以 `floor-plan` 圖種 `draftagent build`
一次產出 DXF 平面圖與 IFC，並貼出完整 verifier 輸出。房名與用途照 design.md 表格；需求數值以 input/ 為準，
資料不足以配置時要停下來回報缺什麼，不得自行假設。回覆請簡述配置（各空間位置、動線、門窗）。

通過標準：verifier 全部綠燈，且符合 design.md 的全部需求（面積容許 ±10%）。
回覆只能說「通過本 repo 現有 verifier 的檢查項目」，不得宣稱符合法規。
