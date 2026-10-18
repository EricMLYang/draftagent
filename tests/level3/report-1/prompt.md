# Level 3 report-1：集合住宅排水計畫書（整份交付物）

桃園區某集合住宅基地（**合成資料**，座標為 TWD97 TM2 虛構位置，非真實基地；不含任何真實案名、地號、公司名）。
工程師交辦：把原始資料做成**整份排水計畫書**——計畫書本文（report.docx 與 report.md）加上整套附圖與水理計算。

- 原始資料：`tests/level3/report-1/input/` 的 `site.geojson`、`nodes.csv`、`links.csv`、`existing_drain.csv`、
  `road_center.csv`、`photo-1.png`，以及工程師交辦 `design.md`（專案、滯洪設施、圖面、計畫書資訊、土地座落、
  基地附近排水系統現況、照片、工程造價單價）

請寫 `DrainageReportSpec`（`draftagent/report/model.py`，圖種 `drainage-report`：內含一份 `DrainageNetworkSpec`），
用 `draftagent build` 一次產出整套圖、計算與計畫書，並貼出完整 verifier 輸出。
**不得填長度、坡度、面積、流量、數量、總價**（一律由程式計算）；章節補充文字只能是描述性文字。

**所有測量、設計與造價數值以 input/ 為準**；input/ 沒有的資料不得自行假設或編造——schema 允許留空的欄位就留空，
並在回覆列出還需要使用者補充的項目；其他缺漏要停下來回報缺什麼。

通過標準：verifier 全部綠燈（允許 yaml verified: false 等 WARNING）。
回覆只能說「通過本 repo 現有 verifier 的檢查項目」，不得宣稱符合法規。
