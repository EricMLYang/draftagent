# Level 2 detail-ty-1：桃園集合住宅排水系統各部詳圖（合成資料）

桃園某集合住宅基地的排水計畫書附圖「排水系統各部詳圖」，畫在一張 A1，共 6 個詳圖：
A-A U 型溝斷面、B-B 筏基滯洪坑剖面、C-C 放流井（平面＋剖面）、D-D 孔口板、E-E 出水口保護工、F-F 滯洪設施銘牌。

每個詳圖的尺寸、高程、比例、閥件與文字，以及圖框資料，見工程師交辦表
`tests/level2/detail-ty-1/input/design.md`（尺寸 mm、高程 m）。本題為合成資料，不對應任何真實案件。

**所有尺寸與高程以 input/ 為準**；表內沒有的值要停下來回報缺什麼，不得自行假設或編造
（業主未提供的銘牌資料照表填 ○○○）。

請寫 `DetailSheetSpec`（`draftagent/schema/detail.py`），用 `drainage-detail` 圖種 `draftagent build`
產出 DXF，貼出 `detail_check` 的完整輸出。

完成標準：`detail_check` 綠燈：每個尺寸標註顯示實尺 mm、所有高程以 EL=xx.xx 出現、
逆止閥 / 溢流口 / 切齊溝緣 / 清掃孔 / 容量註記齊全、詳圖不重疊。回報只能說「通過本 repo 現有 verifier 的檢查項目」。
