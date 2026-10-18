# Level 1-2：基地套疊

> 圖資為合成資料（SYNTHETIC），見 `input/SYNTHETIC.txt`。

測試段（段號 0001）有 3 筆地號：12、12-1、13。
地籍圖在 `tests/level1/1-2/input/cadastre.shp`，使用分區圖在 `tests/level1/1-2/input/zoning.shp`（分區欄位 `分區`）。

請把這 3 筆地號和使用分區套疊，算出各分區面積（m² 與坪）與合計，產出統一 EPSG:3826 的 GeoPackage。
地政資料上這 3 筆的登記面積合計為 6762.5 m²，請對帳。

通過標準：`crs_check` 綠燈（輸出全為 3826）、`area_check --expected 6762.5` 綠燈（誤差 < 0.5%，分區加總 = 基地面積）。
