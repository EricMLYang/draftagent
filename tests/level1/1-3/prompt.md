# Level 1-3：坡度分析

DTM + 基地範圍，做坡度分級與各級面積統計。

- DTM：`tests/level1/1-3/input/dtm.tif`（20 m 網格）
- 基地範圍：`tests/level1/1-3/input/site.gpkg`（layer `site`）

請產出坡度分級圖（GeoTIFF）、各級多邊形（GeoPackage）與各級面積統計表（m²，另附坪），
依 CLAUDE.md 流程以 `slope` 圖種 `draftagent build` 產出並貼出完整 verifier 輸出。

通過標準：坡度分級依 `draftagent/hydro/codes/slope_classes.yaml`、各級面積加總 = 基地面積
（像元化誤差在 verifier 門檻內）、CRS 為 EPSG:3826。
