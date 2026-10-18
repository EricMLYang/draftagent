# Verifier 清單與已知限制

> `draftagent build <spec>` 會依圖種跑下表的 verifier，全部綠燈才算完成。
> 通過只代表「通過本 repo 現有 verifier 的檢查項目」，**不代表符合法規**。
> 法規 / 手冊數值 yaml 多數仍為 `verified: false`（verifier 會 WARNING），需人工對照原文。
> 各 verifier 的完整檢查項目以模組 docstring 為準：`python -m draftagent.verify.<name> --help`。

## 共用元件

| 模組 | 作用 |
|---|---|
| `verify/codes_lock.py` | 法規 yaml 路徑鎖定：schema 層只接受 `draftagent/<pkg>/codes/*.yaml`；verifier 層要求 spec 給的路徑 = repo 預設檔，否則 error，且一律用預設檔重算 |
| `verify/dimtext.py` | 讀 DIMENSION geometry block 內實際畫出的文字（不以量測 × dimlfac 推算）；`dimrnd` / `dimpost` / `dimalt` 非預設 → error；完整數字比對（「45000」≠ 4500） |
| `verify/legibility_check.py` | 印出字高 ≥ `render/codes/layers.yaml` `text.min_print_height_mm`（2.0 mm，製圖慣例）；文字框重疊 > 小框 10% → error；文字壓指定圖層線 → warning；paperspace 依 viewport 比例換算、考慮 viewport 凍結圖層 |
| `verify/standards_check.py` | 圖層表（layers.yaml）：未知圖層 warning；線寬 / 線型不符 error；顏色覆寫比例過高 warning；`$LWDISPLAY=1`；印出字高 |
| `kinds/_util.insunits_verifier` | `$INSUNITS` 與 spec.units 一致 |

## 各圖種

### `plan`（PlanSpec → 平面圖 DXF）

verifier：`dxf_check`、`insunits`

| 檢查什麼 | 不檢查什麼（已知限制） |
|---|---|
| INSUNITS=mm、必要圖層、0 層無實體、A-WALL 封閉線 | 不跑 `standards_check`（圖層線寬 / 線型表），只有 drainage-plan 會跑 |
| DIMENSION 量測值（defpoints 重算）與**實際顯示文字**完整數字相符；dimlfac=1；dimrnd / dimpost / dimalt 禁用 | 只標總長 / 總寬，不檢查分段尺寸鏈 |
| 由 spec 獨立重建：牆多邊形對稱差 ≤ 0.5%、門弧圓心 = 鉸點 / 半徑 = 門寬 / 90° / hinge / swing、窗框落在開口且面積 ≥ 90%、房名在房內 | v0 每房各自一圈牆，不處理共用牆；不檢查家具、軸線、樓梯 |
| 門數、窗數、房名、外尺寸 | 不檢查任何建築法規（走道寬、採光、防火） |
| 印出字高（`print_scale` 換算）、文字重疊 → error；文字壓牆 / 門 / 窗 → warning | |
| schema：房名不重複、房間不重疊、同牆段開口不重疊、開口離牆端 ≥ 牆厚 | |

### `floor-plan`（FloorPlanSpec → 多房間平面 DXF + IFC4）

verifier：`floorplan_check`、`floorplan_dxf_check`、`floorplan_ifc_check`、`standards_check`、`insunits`
（IFC 需 `.[ifc]`：ifcopenshell）

| 檢查什麼 | 不檢查什麼（已知限制） |
|---|---|
| spec 幾何：房間在外牆內緣內、不重疊、鋪滿室內範圍（未分配 ≤ 0.5%）、淨空單一區塊 | 不檢查任何建築法規（走道寬、採光比例、通風、防火、無障礙、樓梯） |
| 門窗：依合約找共用牆段，另**獨立**驗證開口在兩空間共用邊界上（室外 = 外牆內緣）；門寬 ≤ 段長 − 2 × 牆厚；同牆段開口不重疊；窗在外牆且屬於該房 | 門扇開啟範圍彼此碰撞、門扇撞家具不檢查 |
| 拓樸：門連通圖，全部空間可由主入口經室內門到達；走廊類（plan_rules.yaml `uses.circulation`）若有多個需以門連通；居室類（`uses.window_required`）至少一扇窗 | 「至少一扇窗」只是結構規則，不是採光面積比例（沒有附來源的 yaml） |
| 慣例 WARNING（`render/codes/plan_rules.yaml`，kind: convention，無法規意義）：浴廁只能經由臥室進入、淨寬過窄、長寬比過大 | 這些數值沒有外部來源，只提示 |
| DXF 讀回：牆由 spec 獨立重建（對稱差 ≤ 0.5%）、外牆線寬 > 內牆線寬、門弧、窗框、房名與淨面積文字（0.01 m²）在該房淨空內；DIMENSION 實際顯示文字、外總尺寸、尺寸鏈端點在外緣 / 內牆中心線上且連續；字高、重疊 | 尺寸鏈只標南、西兩側；太短的段會被 renderer 併掉（verifier 只要求端點合法、連續） |
| IFC 讀回：IFC4、mm；Project / Site / Building / Storey 各一且聚合；IfcSpace 數 / 名稱 / 用途 / NetFloorArea / 輪廓面積；IfcWall 數 = 外框邊數 + 內牆段數、IsExternal 數；IfcDoor / IfcWindow 數與 id、寬度、FillsVoids → 開口 → VoidsElements → IfcWall（外 / 內牆別）、開口放置點；全部包含於樓層 | 不產生 / 不檢查 IfcRelSpaceBoundary、材料、型別、樓板；IFC 高度（spec.ifc）為示意值 |
| schema：房名 / 門窗 id 不重複、參照存在、主入口必為外門、swing_into 必為 between 中的房間 | 幾何錯誤不在 schema 擋（錯誤 spec 仍可出圖給人看），由 floorplan_check 報 |

### `drainage-profile`（ProfileSpec → 縱斷面 DXF）

verifier：`profile_check`、`insunits`

| 檢查什麼 | 不檢查什麼（已知限制） |
|---|---|
| 比例文字、實際幾何比例；水平比例分母 ≤ `taoyuan_drawing.yaml` 門檻 | **垂直比例未檢查誇大方向**：v_scale 分母大於 h_scale（垂直反而縮小）仍綠 |
| 表格帶：樁號遞增、各節點高程文字、坡度由溝底重算、斷面文字 | **斷面沿流向縮小**（例：上游 U 溝、下游管徑較小）不檢查 |
| 幾何：高程尺 y↔EL（含基準高程絕對值）、每個樁號欄 x、節點名 x、流向箭頭方向、H-PROF 地面 / 溝底 / 溝頂線在節點處的 y | 不檢查水理（縱斷面不算流量 / 水面線） |
| 「進水管」「放流管」等 level 文字存在、數值與 x 位置；構造物頂 / 底 / mark EL | 構造物（池、井）輪廓幾何只驗文字，不驗外框尺寸 |
| 物理：溝頂 ≤ 地面、U 溝頂 − 底 = 斷面高、進水管 ≥ 溝底、放流管 ≥ 受納溝底 / 設計水位；順坡（pump 除外） | |
| DIMENSION 實際顯示文字、TEXT 插入點與對齊點一致、字高 / 重疊 / 壓 H-PROF 線 | 不跑 `standards_check` |

### `drainage-detail`（DetailSheetSpec → 詳圖 DXF）

verifier：`detail_check`、`insunits`

| 檢查什麼 | 不檢查什麼（已知限制） |
|---|---|
| 每個詳圖 GROUP、標題（剖面代號 + 圖名）、`S=1/N`；比例分母 ≤ 門檻 | 不算結構（配筋、厚度、承載力）；鋼筋只是示意 |
| DIMENSION：dimlfac = 比例分母、實際顯示文字 = 量測 × dimlfac（含覆寫）、顯示值多重集合 = spec 推導尺寸 | 材質、螺栓、拋石粒徑等文字只檢查「有沒有」，不檢查內容是否合理 |
| 高程以「<名稱>… EL=」標籤比對；▽ 尖端 y 與 EL 比例一致；同欄 EL 文字上下順序 = 高程順序；H-WATR 水位線 y | 單獨的 drainage-detail 不知道排水網路：高程彼此銜接只檢查同張圖內規則（sheet_issues），跨圖由 drainage-plan 的 sheetset_check 負責 |
| 必要註記（否定詞不算）、銘牌容量完整格式、剖面字母 = section_code | |
| 本體外框 bbox = spec 推導尺寸；hatch pattern_scale 在範圍表內 | 範圍表以外的 hatch pattern 只 warning |
| 幾何合理性（水位鏈、管底 / 溝底 / 護坦次序）；詳圖 bbox 不重疊、不壓圖簽 | 不跑 `standards_check` |

### `drainage-calc-ty`（TaoyuanSiteDrainageSpec → result.json + calc.md）

verifier：`drainage_calc_check`

| 檢查什麼 | 不檢查什麼（已知限制） |
|---|---|
| 一律由 spec 重算（讀預設 yaml）：Σ分區 = 基地（0.5%）、提供貯集量 ≥ 必要量、孔口 / 抽水排放量在容許量 85–100%、溢流堰 Q ≥ Q20 | 法規路徑（桃園標準 / 建技 4-3 / 出流管制）由工程師決定，verifier 不判斷 |
| 管渠：Q ≤ 容量、流速、U 溝出水高、最小管徑、材料與管徑；Q 的 C = `pipes[].design_c` 或全基地綜合 C | **無網路拓樸**：`design_c` 的上游分區來源無法驗證，只在低於綜合 C 時 WARNING；`design_area_m2` 也由 spec 給 |
| `--result` 完整 flatten 逐值比對（缺鍵 / 多鍵 error）；`--calc` calc.md 與 `render_table(重算)` 逐字相同 | 增改建 As 換算、抽水排放上限的規則原文未收錄（WARNING） |
| `codes_path` ≠ repo 預設 → error | yaml `verified: false` |

### `drainage-plan`（DrainageNetworkSpec → 整套排水圖 + 計算）

verifier：`drainage_plan_check`、`profile_check`（主幹）、`profile_check[outlet]`、`drainage_calc_check`、`detail_check`、
`sheetset_check`、`standards_check`、`insunits`

| 檢查什麼 | 不檢查什麼（已知限制） |
|---|---|
| **不信任中間檔**：profile / outlet profile / calc / detail spec 一律由 spec 重新衍生（預設 yaml），與產物 JSON 逐鍵相等，各圖 verifier 對重新衍生的 spec 執行 | 衍生邏輯（`drainage/derive.py`）與 renderer 同源；verifier 對「衍生規則本身錯誤」只有部分獨立實作（管渠 C / Q、面積） |
| 網路拓樸、管段標籤 S / L、節點 ATTRIB、座標刻度、viewport 比例、圖簽、分區面積、數量表 | 平面圖文字壓 C-STRM 線不檢查（只檢查文字互相重疊） |
| 平面拓樸幾何：C-STRM 線兩端 = 節點、流向箭頭方向與位置、標籤離自己管段最近、C-CTCH / C-PROP / 既有排水 Hausdorff < 0.01 m | |
| viewport 可見性：必要圖層未凍結 / on / plot、基地在視窗內 | |
| 高程相容性 E1–E9；物理合理性：覆土 ≥ 0、top_el ≤ ground_el、節點在基地內、地面池不壓建物、受納溝不逆坡、出流口未接受納溝 → error | 受納溝設計水深、管網可否承受迴水由工程師提供，verifier 只檢查一致性 |
| 計算：drainage_calc_check 直接對衍生 calc spec（管渠 `design_c` = 上游加權 C）判定並逐值 / 逐字比對 result.json、calc.md；`independent_pipe_flows` 獨立重算 C / 承接面積 / Q 與 calc spec、result.json 比對 | 滯洪設施出流端以下、逆坡（抽水）、既有管段不做合理化公式檢核 |
| 跨圖一致性（sheetset）：同一元素在平面 / 縱斷面 / 詳圖 / result.json 的數值一致；modelspace 標註（V=、有效水深、既有排水頂 / 底 EL、路中 EL）；孔口尺寸文字與孔口板孔徑標註 = calc；圖號唯一、統一圖名、比例門檻 | sheetset 只解析 TEXT / ATTRIB / DIMENSION 文字，數值對錯由各圖 verifier 對 spec 檢查 |
| 製圖標準（四個 DXF）、可讀性（字高、重疊） | |

### `drainage-report`（DrainageReportSpec → 整套排水圖 + 計畫書 report.docx / report.md）

verifier：drainage-plan 全部 8 個（對 `spec.network` 執行）+ `report_check`

| 檢查什麼 | 不檢查什麼（已知限制） |
|---|---|
| 章節（Heading 1）依序 = `report/codes/report_format.yaml` chapters + 附圖；必要表格都在、不重複、表名須在詞彙表 | 章節名依任務交辦，格式說明原檔未核（yaml verified: false）；版面數值為 repo 暫定 |
| 表格逐列逐欄對重算值（不信任 result.json）：基地 / 建物面積與坪、設計基準（yaml 路徑值）、Horner a / b / c 與 I（另以公式獨立重算）、逕流係數、既有排水、集水分區（shapely 面積）、管渠（座標重算 L / S）、貯集 / 排放量、造價（數量 × 單價、合計）；顯示值必須是正確四捨五入 | 只追溯表格與「=」後的數字，段落中其他數字（條號、日期）不檢查 |
| 「標籤 = 數值」：標籤須在詞彙表（否則「無法追溯」）、值 = 重算；法規數值必附〔yaml：檔：路徑；出處；verified〕且路徑存在、旗標與 yaml 一致；yaml 未核對時須有 verified: false 提醒段 | 補充文字的數字由 schema 擋（國字數字擋不到） |
| 附圖：14 項統一圖名依序、本 repo 產出的圖號 / 比例 = DXF 圖簽 = spec、檢核表比例 = taoyuan_drawing.yaml；圖說 = 圖簽；嵌入數 = DXF 圖紙數 + 照片數；PNG / 照片非空白 | 未產出的圖（地理位置、地籍、橫斷面…）只列「使用者另附」，不檢查內容 |
| docx ↔ md 逐項一致（標題 / 段落 / 條列 / 表格 / 圖片雜湊）；A4、邊界、字型（= report_meta.json 且在 yaml 允許清單）、TOC 功能變數 | 不檢查 Word 實際排版（分頁、圖是否跨頁） |
| 固定結論句；不得有肯定式合規宣稱（否定語境 / 引號除外，獨立實作）；提到淹水潛勢須為風險提示；不得含 `check_no_client_ids.py` 識別資訊樣式 | 合規宣稱判斷為啟發式 |

### `outflow-control`（OutflowControlSpec → 出流管制水文水理）

verifier：`outflow_check`

| 檢查什麼 | 不檢查什麼（已知限制） |
|---|---|
| 判定一律用重算值：開發後 Qp ≤ 開發前（T = 2/5/10）、最高水位 ≤ 池頂 − 出水高、體積 ≥ 需求 × 安全係數、簡易檢核條件 | 堰潛沒折減、Ogee 堰係數（查圖）未收錄；聯外排水通洪替代規則未實作 |
| 水量守恆、單位歷線體積守恆（1%） | Horner 係數、測站由工程師提供；verifier 不判斷測站是否合適 |
| result.json 完整 flatten 逐鍵比對 | SWMM 只檢查 .inp 內容，本機 SWMM 子行程可能無法執行（測試 skip），不比對 SWMM 演算結果 |
| SWMM .inp 各段落與 spec / 重算比對，且與 `build_inp(重算)` 逐字相同；附圖 PNG 非空白、PDF 有頁面 | 附圖只檢查「非空白」，不檢查曲線內容 |
| 開發前後面積差 > 0.5% → error（有說明 → warning）；`codes_path` 鎖定 | |

### `site-overlay`（SiteSpec → 地號 × 分區 GeoPackage）

verifier：`crs_check`、`area_check`、`overlay_check`

| 檢查什麼 | 不檢查什麼（已知限制） |
|---|---|
| 輸出 CRS = 3826（澎湖 3825 需明確允許）；TWD67 紅燈 | TWD67 → TWD97 的轉換精度（pyproj 預設約數公尺）不驗 |
| `expected_area_m2` 必填；幾何面積 vs 登記面積 0.5% | `expected_area_source` 只記錄，不驗證來源真偽；登記面積若由 agent 自填，verifier 無從分辨 |
| 獨立重新疊圖：地號集合、幾何 Hausdorff ≤ 0.01 m、（地號, 分區）面積 ±0.5%、area_m2 / area_ping 欄、CSV / summary.json 對帳 | 只檢查與輸入圖資一致，輸入圖資本身（分區圖版本、地籍正確性）不驗 |
| parcel_id 段號正規化比對（保留前導 0 輸出） | 疑似漏列子號地號只 WARNING（實務上可能合法不納入，需與使用者確認） |
| | 不做任何法規判定（建蔽、容積、淹水潛勢不得作為管制依據） |

### `slope`（SlopeSpec → 坡度分級 tif / gpkg / csv）

verifier：`slope_check`、`crs_check`

| 檢查什麼 | 不檢查什麼（已知限制） |
|---|---|
| 分級 yaml 路徑鎖定；各級都在、像元數 × 像元面積、坪換算、面積對帳（半個邊界像元帶） | 坡度演算法以 DTM 重算比對；與另一種差分法差異只記 metrics / warning |
| gpkg 與 csv 都跑，且逐級相同；tif 與 DTM 同網格、遮罩內像元數 = gpkg、整幅與重算逐像元相同 | DTM 解析度相對基地偏粗時統計僅供參考（差異 > 1% WARNING） |
| gpkg 多邊形 ⊂ 基地.buffer(像元)，柵格化後 = 重算像元集合 | 不判斷山坡地可開發性（只做分級統計） |
| gpkg、tif CRS = 3826 | |

## 稽核修補紀錄

2026-10 對 verifier 做過一次 false-negative 稽核（故意竄改產物或 spec 後重跑，約 95 個情境中約 80 個原本綠燈）。

### 已補

| 項目 | 修補 |
|---|---|
| spec 可覆寫法規 yaml 路徑 | `codes_lock`：schema 只收 `draftagent/*/codes/`，verifier 要求 = 預設檔並以預設檔重算（calc-ty、drainage-plan、outflow、slope） |
| DIMENSION 顯示值用推算 | `dimtext`：讀實際顯示文字、禁用 dimrnd / dimpost / dimalt、完整數字比對（plan、profile、detail、sheetset） |
| outflow 判定用 result.json | 判定一律用重算值；result.json 完整 flatten 比對 |
| drainage-plan 信任中間檔 | 中間檔由 spec 重新衍生並逐鍵比對；各圖 verifier 對重新衍生的 spec 執行 |
| 管渠設計流量用全基地綜合 C | 上游集水分區加權 C 寫入 calc spec `pipes[].design_c`；`independent_pipe_flows` 獨立實作交叉驗證 |
| 縱斷面幾何沒驗 | 高程尺、樁號欄、節點名、流向箭頭、H-PROF 線、level 文字位置、TEXT 插入點 / 對齊點 |
| 排水平面圖拓樸沒驗 | C-STRM 端點、流向箭頭、標籤歸屬、C-CTCH / C-PROP / 既有排水 Hausdorff |
| viewport / 圖層可見性 | 必要圖層未凍結 / on / plot、基地在 viewport 視窗內 |
| 物理合理性 | 覆土、節點在基地內、地面池壓建物、受納溝逆坡、出流口未接受納溝 → error；縱斷面溝頂 > 地面、斷面高不符、進水管 / 放流管高程 |
| 無字高檢查、文字重疊 | `legibility_check`：印出字高 ≥ 2.0 mm、重疊 > 10%（考慮 viewport 凍結）；PlanSpec 新增 `print_scale` |
| site-overlay 未獨立重算 | `overlay_check` 獨立重新疊圖、CSV / summary 對帳；`expected_area_m2` 必填；parcel_id 保留段號前導 0；疑似漏列子號 WARNING |
| slope tif / csv / 多邊形位置未驗 | csv 也跑完整檢查；tif 與 gpkg 對帳；多邊形柵格化比對 |
| 詳圖 EL 只比數值集合 | 標籤式 EL、▽ 比例、文字順序、H-WATR、否定詞、銘牌完整格式、剖面字母、本體尺寸、hatch 比例範圍 |
| 整套圖缺孔口資訊、modelspace 標註未比對 | 孔口為孔口型時必出孔口板詳圖並比對孔徑；V=、有效水深、既有排水 EL、路中 EL 納入 sheetset |
| outflow SWMM / 附圖 / 面積差 | .inp 解析比對 + 逐字比對；附圖非空白；面積差 > 0.5% → error |
| calc.md 數字被改 | `render_table(重算)` 逐字比對 |
| plan 只驗數量 | 牆對稱差、門弧、窗、房名；schema 擋房間重疊、開口重疊 |

### 仍缺

| 項目 | 現況 |
|---|---|
| 縱斷面垂直比例小於水平比例（v_scale 分母 > h_scale 分母） | 綠燈；尚未規定垂直誇大方向 |
| 縱斷面斷面沿流向縮小（例：上游 U 溝、下游小管徑） | 綠燈；未檢查下游斷面不得小於上游 |
| 平面設計圖文字壓 C-STRM 線 | 未檢查（縱斷面有壓 H-PROF 線 warning、詳圖有壓 H-STRC 線 warning） |
| plan / drainage-profile / drainage-detail 不跑 `standards_check` | 圖層線寬 / 線型表只在 drainage-plan 檢查 |
| drainage-calc-ty 單獨使用時的 `design_c` / `design_area_m2` | 無網路拓樸可驗，只在 design_c < 綜合 C 時 WARNING |
| 衍生規則本身 | `drainage/derive.py` 由 verifier 重新執行比對，除管渠 C / Q 與面積外沒有獨立實作 |
| 法規 yaml `verified: false` | 數值待人工對照原文；verifier 只能保證「與 yaml 一致」 |
