# Level 2 drainage-plan-ty-1：桃園集合住宅排水系統平面設計圖 + 集水區分析圖

桃園區某 1,680 m² 集合住宅基地（**合成資料**，座標為 TWD97 TM2 虛構位置，非真實基地；
不含任何真實案名、地號、公司名）。南側臨道路，道路北側有既有道路側溝 EX1（往西流）；
地下室筏基滯洪、**抽水排放**，溢流口設於進流井。

原始資料在 `tests/level2/drainage-plan-ty-1/input/`（清單與說明見該目錄 `design.md`）：

- `site.geojson`（EPSG:3826）：基地界線、建物、集水分區（分區編號、土地使用、排入節點）、滯洪池範圍、筏基坑格、抽水集水坑
- `nodes.csv`：節點測量成果（編號、種類、E、N、地面高、底高、頂高）
- `links.csv`：管溝（編號、上游、下游、斷面、材質、曼寧 n）
- `existing_drain.csv`：既有側溝點位與高程、斷面、設計水深
- `road_center.csv`：道路中心樁高程
- `design.md`：滯洪型式、水位、抽水、溢流、比例、圖號、專案資訊等設計決定

**所有測量與設計數值以 input/ 為準**；input/ 沒有的資料要停下來回報缺什麼，不得自行假設或編造。
`bad/legacy_spec.json` 是舊版錯誤範例（淺池重力排放 → 迴水），不要照抄。

請寫 `DrainageNetworkSpec`（`draftagent/drainage/model.py`），**不得填長度、坡度、面積、流量**；
以 `drainage-plan` 圖種一次產出平面設計圖、集水區分析圖、衍生縱斷面與衍生流出抑制計算，
並貼出 `drainage_plan_check`、`profile_check`、`drainage_calc_check` 的完整輸出。

通過標準：三個 verifier 綠燈（drainage_plan_check 含高程相容性 E1–E8）（允許 yaml verified: false WARNING）。
回覆只能說「通過本 repo 現有 verifier 的檢查項目」，不得宣稱符合法規。
