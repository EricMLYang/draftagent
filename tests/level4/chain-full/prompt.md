# Level 4 chain-full：地籍套疊 → 平面圖 → 排水計畫書（跨領域串接）

桃園區某住宅新建工程（**合成資料**，座標為 TWD97 TM2 虛構位置，非真實基地；不含任何真實案名、地號、公司名）。
基地為測試五段（段號 0057）的 2 筆地號：213、214。地政資料上這 2 筆的登記面積合計為 1504.17 m²。

原始資料在 `tests/level4/chain-full/input/`（清單與說明見該目錄 `design.md`）：地籍圖 `cadastre.shp`、
使用分區圖 `zoning.shp`、地面高程 `dtm.tif`、平面需求 `floorplan.md`、配置 `layout.geojson`（滯洪池、集水分區草圖）、
`nodes.csv`（節點位置與井深）、`links.csv`、`existing_drain.csv`、`road_center.csv`、`design.md`。

請依序完成三份成果（各自一次 `draftagent build`）：

1. **基地套疊**（`site-overlay`）：兩筆地號與使用分區套疊，各分區面積（m² 與坪）與合計，輸出 EPSG:3826 GeoPackage，並與登記面積對帳。
2. **住宅棟一層平面圖**（`floor-plan`，`FloorPlanSpec`）：依 `floorplan.md` 配置，產出 DXF 與 IFC。
3. **排水計畫書**（`drainage-report`，`DrainageReportSpec` 內含 `DrainageNetworkSpec`）：整套排水圖、水理計算與計畫書本文。
   - 基地界線與基地面積**取自第 1 步的套疊結果**；建物範圍**取自第 2 步的平面圖外框**（依 floorplan.md 的建物位置）
   - 集水分區、計畫書的土地座落 / 使用分區 / 建蔽率依 design.md 說明，由前面成果推得

**所有測量與設計數值以 input/ 為準**；input/ 沒有的資料要停下來回報缺什麼，不得自行假設或編造。
spec 不得填長度、坡度、面積、流量、數量、總價。

通過標準：三次 build 的 verifier 全部綠燈（允許 yaml verified: false 等 WARNING），且三份成果彼此一致，並貼出完整 verifier 輸出。
回覆只能說「通過本 repo 現有 verifier 的檢查項目」，不得宣稱符合法規。
