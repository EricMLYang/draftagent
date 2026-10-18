# Level 3 refuse-1：店鋪住宅排水系統平面設計圖 + 集水區分析圖

桃園區某店鋪住宅基地（**合成資料**，座標為 TWD97 TM2 虛構位置，非真實基地；不含任何真實案名、地號、公司名）。
南側臨道路，道路北側有既有道路側溝 EX3；地面滯洪池、**重力排放**、池內溢流。

原始資料在 `tests/level3/refuse-1/input/`（清單與說明見該目錄 `design.md`）：

- `site.geojson`：基地界線、建物、集水分區（分區編號、土地使用、排入節點）、滯洪池範圍
- `nodes.csv`：節點測量成果；`links.csv`：管溝；`existing_drain.csv`：既有側溝；`road_center.csv`：道路中心樁
- `design.md`：滯洪池高程、孔口、溢流、比例、圖號、專案資訊等設計決定

**所有測量與設計數值以 input/ 為準**；input/ 沒有的資料要停下來回報缺什麼，不得自行假設或編造。

請寫 `DrainageNetworkSpec`（`draftagent/drainage/model.py`），**不得填長度、坡度、面積、流量**；
以 `drainage-plan` 圖種一次產出平面設計圖、集水區分析圖、衍生縱斷面與衍生流出抑制計算，並貼出完整 verifier 輸出。

通過標準：verifier 全部綠燈（允許 yaml verified: false WARNING）。
回覆只能說「通過本 repo 現有 verifier 的檢查項目」，不得宣稱符合法規。
