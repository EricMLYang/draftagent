# Level 3 chain-1：地籍套疊 → 排水系統設計圖（跨領域串接）

桃園區某住商大樓新建工程（**合成資料**，座標為 TWD97 TM2 虛構位置，非真實基地；不含任何真實案名、地號、公司名）。
基地為測試二段（段號 0042）的 2 筆地號：105、106。地政資料上這 2 筆的登記面積合計為 1764.58 m²。

原始資料在 `tests/level3/chain-1/input/`（清單與說明見該目錄 `design.md`）：地籍圖 `cadastre.shp`、
使用分區圖 `zoning.shp`（分區欄位 `分區`）、地面高程 `dtm.tif`、配置 `layout.geojson`（含集水分區草圖）、
`nodes.csv`（節點位置與井深）、`links.csv`、`existing_drain.csv`、`road_center.csv`、`design.md`。

請依序完成兩份成果（各自一次 `draftagent build`）：

1. **基地套疊**（`site-overlay`）：兩筆地號與使用分區套疊，各分區面積（m² 與坪）與合計，輸出 EPSG:3826 GeoPackage，並與登記面積對帳。
2. **排水系統設計圖**（`drainage-plan`，`DrainageNetworkSpec`）：一次產出平面設計圖、集水區分析圖、縱斷面與流出抑制計算。
   - 基地界線**必須取自第 1 步的地籍套疊結果**（兩筆地號聯集），排水計算的基地面積要與套疊面積一致
   - 集水分區 = `layout.geojson` 草圖以基地界線裁切
   - 節點地面高取 `dtm.tif`（雙線性內插、取至 0.01 m），底高 = 地面高 − 井深

**所有測量與設計數值以 input/ 為準**；input/ 沒有的資料要停下來回報缺什麼，不得自行假設或編造。
spec 不得填長度、坡度、面積、流量。

通過標準：兩次 build 的 verifier 全部綠燈（允許 yaml verified: false WARNING），並貼出完整 verifier 輸出。
回覆只能說「通過本 repo 現有 verifier 的檢查項目」，不得宣稱符合法規。
