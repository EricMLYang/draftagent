# Level 3 change-1：集合住宅排水系統變更設計

桃園區某集合住宅基地（**合成資料**，座標為 TWD97 TM2 虛構位置，非真實基地；不含任何真實案名、地號、公司名）。
原設計已核定（地面滯洪池、重力排放、池內溢流），現在工程師交辦第 1 次變更設計。

- 原設計的原始資料：`tests/level3/change-1/input/` 的 `site.geojson`、`nodes.csv`、`links.csv`、
  `existing_drain.csv`、`road_center.csv`、`design.md`
- 變更交辦：`tests/level3/change-1/input/change.md`（文字）與 `change.geojson`（新範圍）

請產出**變更後**的整套圖：寫 `DrainageNetworkSpec`（`draftagent/drainage/model.py`），**不得填長度、坡度、面積、流量**，
以 `drainage-plan` 圖種一次產出平面設計圖、集水區分析圖、衍生縱斷面與衍生流出抑制計算，並貼出完整 verifier 輸出。
**變更交辦沒有列出的項目一律維持原設計**（不要順手「優化」其他元素）；回覆請列出相對原設計改了哪些元素。

**所有測量與設計數值以 input/ 為準**；input/ 沒有的資料要停下來回報缺什麼，不得自行假設或編造。

通過標準：verifier 全部綠燈（允許 yaml verified: false WARNING）。
回覆只能說「通過本 repo 現有 verifier 的檢查項目」，不得宣稱符合法規。
