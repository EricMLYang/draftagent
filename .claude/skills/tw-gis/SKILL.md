---
name: tw-gis
description: 台灣圖資（地籍、使用分區、DTM）的 CRS 陷阱、中文欄名與基地套疊流程（SiteSpec、tools/gis_overlay.py、verify.crs_check、verify.area_check）。拿到任何 SHP / GPKG / TIF、要算基地或分區面積、看到 TWD67 / 3825 / 經緯度資料時先讀。
---

# tw-gis：台灣圖資與基地套疊

流程固定：寫 `SiteSpec` JSON（`draftagent/schema/site.py`）存 `runs/<timestamp>/spec.json`
→ `draftagent build`（或 `tools/gis_overlay.py` → `crs_check` + `area_check`）；build 的三個 verifier
（crs_check、area_check、overlay_check）全綠才算完成。

```bash
.venv/bin/python -m draftagent.verify.crs_check <input.shp>          # 拿到圖資先驗
.venv/bin/python tools/gis_overlay.py runs/<ts>/spec.json            # 套疊，印 JSON 摘要
.venv/bin/python -m draftagent.verify.crs_check runs/<ts>/site.gpkg
.venv/bin/python -m draftagent.verify.area_check runs/<ts>/site.gpkg --expected <m2>
```

輸入圖資紅燈不代表不能做：`gis_overlay` 會自動把已知 CRS 轉到 3826，並在 `crs_notes` 寫出轉了什麼；**輸出** gpkg 必須綠燈。

## CRS 陷阱

| EPSG | 是什麼 | 處理 |
|---|---|---|
| **3826** | TWD97 / TM2 121，本島 | 唯一標準。單位 m |
| 3825 | TWD97 / TM2 119，澎湖（金門也常用） | 本島基地轉 3826；澎湖基地 spec 設 `target_crs: "EPSG:3825"`，crs_check 加 `--allow 3825`（範圍不在澎湖仍紅燈） |
| 3828 / 3827 | TWD67 / TM2 121 / 119，舊地籍 | 與 TWD97 差約 **800 m**。pyproj 預設轉換精度約數公尺；**回覆必須註明做過 TWD67 轉換** |
| 4326 | WGS84 經緯度（WMS、GeoJSON、GPS） | 轉 3826；不得在 4326 下算面積 |
| 3857 | Web Mercator（NLSC WMTS、網路地圖） | 轉 3826；面積放大約 1/cos²(緯度) ≈ 1.2 倍，絕不可直接算 |
| 無 .prj | 不知道 | **不猜**，停下來問資料來源 |

其他：座標數值 < 360 卻標 3826 → 標籤錯（crs_check 會以「不在台灣」紅燈）。
TWD97 3826 合理範圍約 E 150k–360k、N 2.42M–2.80M（台北約 E 300k、N 2.77M）。

## 中文欄名

- 地籍 SHP（國土測繪中心）：`段號`、`段名`、`地號`、`地目`；DBF 欄名限 10 bytes（UTF-8 中文 3 bytes/字）
- 編碼：新資料有 `.cpg`（UTF-8）；舊資料常是 Big5 / CP950、沒有 `.cpg` → 欄位亂碼時用 `gpd.read_file(p, encoding="cp950")` 檢查
- 地號格式：`101`、`101-3`、`0101-0003`、`01010003`（母號 4 碼 + 子號 4 碼）都等價；`overlay.normalize_land_number` 會統一
- 段號 `0102` 與 `102` 比對時視為相同；但輸出的 `parcel_id`（`段號:地號`）**保留地籍原字串的段號前導 0**
  （例 `0102:101`，不會被吃成 `102:101`），比對時才正規化
- 分區欄位名稱各縣市不同，用 SiteSpec `zoning_field` 指定（預設 `分區`）
- 山坡地範圍（水保署）欄位：`ACAD_TEXT`、`new_area`、`CountyCode`

## SiteSpec 範例

即 `examples/site-overlay.json`（`draftagent new site-overlay`）；數值只示範寫法，與評估考題無關。

```json
{
  "parcels": [{"section_name": "範例段", "section_code": "0102", "land_numbers": ["101", "0101-0003", "102"]}],
  "cadastre_path": "examples/data/site/cadastre.shp",
  "zoning_path": "examples/data/site/zoning.shp",
  "zoning_field": "分區",
  "target_crs": "EPSG:3826",
  "output_path": "runs/<ts>/site.gpkg",
  "expected_area_m2": 3675.0,
  "expected_area_source": "使用者提供"
}
```

- `expected_area_m2`：**必填**（schema 擋缺值，build 的 area_check 也會 error），值是使用者 / 地政資料提供的登記面積合計，
  **不得由 agent 用幾何自己算出來填**（那等於拿答案對答案）。使用者沒給 → 停下來問
- `expected_area_source`：選填，記錄 expected_area_m2 的來源（謄本、使用者口述…）；目前 verifier 不檢查，但回覆時要能交代來源
- `confirmed_excluded_parcels`：使用者確認「不納入」的相鄰同母號地號（`101-3` 或 `0102:101-3`）。overlay_check 發現
  與已選地號共邊、同段同母號、卻沒選也沒列在這裡的地號 → **WARNING「疑似漏列子號地號」**。看到這個 warning 要問使用者，
  確認不納入才列進來；不要為了消 warning 自行加入或排除

輸出：`site.gpkg`（layers `parcels`、`parcel_zoning`，欄 `parcel_id`=`段號:地號`）＋ `site_areas.csv`（分區、m²、坪，含合計；坪 = m² × 0.3025）。

## overlay_check（build 時執行，獨立重新疊圖對帳）

不呼叫 `site.overlay`，直接由 spec 的 cadastre / zoning 來源用 shapely intersection 重新疊圖，再與產物逐筆對帳：
- parcels：地號集合相同；每筆幾何與來源地籍（轉到目標 CRS 後）Hausdorff ≤ 0.01 m（整體平移、選錯地號會紅）
- parcel_zoning：（地號, 分區）集合相同；每組面積 ±0.5%、幾何 Hausdorff ≤ 0.01 m（分區屬性對調、幾何互換會紅）
- `area_m2` / `area_ping` 屬性欄 = 幾何面積 / × 0.3025
- 面積表 CSV、summary.json 逐列與重算對帳（四捨五入到 0.01）
- 疑似漏列子號地號 → WARNING（見上）

手改 gpkg / CSV / summary.json 都會被抓到；要改結果就改 spec 重新 build。

## area_check 判讀

- 面積由幾何重算，不信屬性欄；門檻 0.5%，`--tolerance` 只能收緊不能放寬
- `parcel_zoning` 加總 ≠ `parcels` → 有地號落在分區圖外或分區重疊，回頭查分區圖，不要改 verifier
- 投影面積與登記面積本來就有微小差異（TM2 尺度比 0.9999，約 0.02%）；超過 0.5% 通常是選錯地號或 CRS 錯

## slope（坡度分級）產物對帳

`draftagent build` 的 slope_check（`check_artifacts`）對 tif / csv / gpkg 三個產物交叉對帳：
- 分級 yaml（`classes_path`）只能是 repo 預設（`verify/codes_lock`），否則 error；不要改門檻 yaml 路徑
- gpkg 與 csv 各自跑完整檢查（欄位、各級都在、area = 像元數 × 像元面積、坪換算、面積對帳、DTM 重算像元數），
  且兩者逐級 pixel_count / area_m2 相同
- GeoTIFF 與 DTM 同網格；基地遮罩內各級像元數 = gpkg pixel_count；有 DTM 時整幅分級與重算逐像元相同
- gpkg 各級多邊形 ⊂ 基地.buffer(像元邊長)，且以像元中心柵格化後 = 重算該級像元集合（抓多邊形平移 / 錯置）
- crs_check 對 gpkg 與 tif 都跑

## 圖資來源與限制

來源表見 `docs/PLAN.md` §5（DTM、段籍圖 LANDSECT、地籍 SHP、通用版電子地圖、行政界、山坡地、淹水潛勢、岩體滑動）。
- **淹水潛勢圖依法僅供防救災，不得作為土地使用管制或可建性依據**，只能列風險提示
- 使用分區只能說「套疊結果」，不得延伸成法規判定；建蔽 / 容積數值只能從 `draftagent/*/codes/*.yaml` 讀
- 範例圖資（`examples/data/`，`tools/make_examples.py` 產生）與評估考題圖資都是**合成資料**，不是真實地籍

## 讀建築師 DXF（arch-import）

建築師給的平面圖 DXF 要當排水 / 基地 spec 的原始資料時，**不要手寫 ezdxf 腳本抽線**，一律走：

1. `draftagent inspect-dxf <drawing.dxf>`：看圖層清單、各層實體數 / 顏色 / 線寬、範圍、$INSUNITS、文字（圖名、
   「CPn E=… N=…」控制點標註）、圖塊 / xref
2. 判斷並寫進 spec（`draftagent schema arch-import`、範例 `examples/arch-import.json`）：
   - `layers.exterior_wall`（必填）：真正的一樓外牆層。小心誘餌：名字像 A-WALL 的舊版綁定層（`A-WALL$0$…`）、
     陽台欄杆、圖框、內牆。外牆和雜線同層（例如全在 0 層）→ `exterior_wall_filter: {color, lineweight}`
   - 圖塊內 0 層實體繼承 INSERT 的圖層（巢狀時逐層繼承），以「展開後的有效圖層」判斷
   - `drawing_units`（必填，mm / cm / m）：看尺寸標註、文字高、門弧半徑判斷，**不得只信 $INSUNITS**
     （header 常是錯的；不符時 arch_check 會 warning）
   - `wall_representation`：外牆單線外框 = `outline`；雙線（外緣 + 內緣，門窗處斷開）= `double_line`
     （開口 ≤ `opening_max`〔預設 3 m〕會被橋接；外牆凹口比 opening_max 窄時會被填平 → 調小）
   - `floor_filter`：多層並排在同一 modelspace 時用 `bbox` 或 `title_text`（例「一層平面圖」，完全相符優先）＋
     `search_radius`；圖層已依樓層命名（`1F-WALL`）時直接指定圖層即可。地下室畫在旁邊、控制點只對 1F →
     `offsets.basement = [-dx, -dy]`（用各層軸線對位求得）
   - `control_points`：≥ 2，**建議 ≥ 3**（2 點時轉換無法驗證，會 warning）。來源：圖上標註的界址點、
     地籍圖套繪、工程師提供。**沒有控制點就停下來問使用者 / 工程師，不要自己猜座標或用圖上原點硬套**
   - `join_tolerance`：預設 10 mm（上限 50 mm）；縫更大 → 問建築師，不要放大容差硬接
   - `site`：有基地界線就給（coords 或 EPSG:3826 檔），外框出基地會紅燈
3. `draftagent build spec.json` → `arch.geojson`（EPSG:3826；footprint / basement / open_space / roof_drain，
   帶 source_layers、source_handles、area_m2、perimeter_m）、`transform.json`（縮放、旋轉、各控制點殘差、接縫）、
   `overlay.png`（原圖淡灰 + 外框紅）。**貼 arch_check 完整輸出**；紅燈修 spec，不改 verifier
4. arch_check：重讀 DXF 重建比對（對稱差 ≤ 0.1%）、殘差 ≤ 0.05 m（≥ 3 點）、縮放比 ∈ [0.995, 1.005]
   （超出 = 單位或控制點錯）、面積守恆 ≤ 0.1%、多邊形有效且外框唯一、接縫 ≤ join_tolerance、⊂ 基地（0.05 m）、
   source_handles 可追溯到指定圖層
5. 把 `arch.geojson` 當原始資料寫排水 spec（建物外框、落水頭位置）；轉換結果仍要請工程師目視 overlay.png 確認。
   只能說「通過 arch_check 的檢查項目」，不能說外框「正確」或「符合法規」

### 建築師給的是 DWG

先 `draftagent convert-dwg <file.dwg>`：自動判斷作業系統，依序嘗試本機可用的轉檔工具
（Windows：ODA File Converter → LibreDWG；macOS / Linux：LibreDWG → ODA），轉出後確認 ezdxf 讀得到。
都沒有可用工具時會列出該平台的安裝方式——**不要自己下載或安裝軟體**，把提示轉告使用者；
最可靠的替代是請建築師在 AutoCAD / BricsCAD「另存 DXF」。轉完接 `inspect-dxf`。
