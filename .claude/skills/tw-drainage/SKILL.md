---
name: tw-drainage
description: 桃園建築基地雨水流出抑制設施（筏基 / 地面滯洪、孔口、溢流堰、管渠）的確定性計算與檢核：何時適用桃園逕流量標準 / 建技 4-3 / 出流管制、TaoyuanSiteDrainageSpec 欄位、tools/drainage_calc.py、verify.drainage_calc_check、哪些值必須由工程師決定。要算貯集量、排放量、Horner 雨強、合理化公式、曼寧流速或寫排水計畫書水理計算時先讀。
---

# tw-drainage：桃園建築基地流出抑制計算

流程固定：寫 `TaoyuanSiteDrainageSpec`（`draftagent/schema/drainage.py`）存 `runs/<timestamp>/spec.json`
→ `tools/drainage_calc.py` 產 result.json + 計算表 → `verify.drainage_calc_check`，綠燈才算完成。
**不產圖**（縱斷面 / 平面圖是另外的模組）。

```bash
.venv/bin/python tools/drainage_calc.py runs/<ts>/spec.json -o runs/<ts>/result.json
.venv/bin/python -m draftagent.verify.drainage_calc_check runs/<ts>/spec.json --result runs/<ts>/result.json [--calc runs/<ts>/calc.md]
```

或直接 `draftagent build runs/<ts>/spec.json`（render + verifier 一次完成，產物含 result.json 與 calc.md）。

數值全部來自 `draftagent/hydro/codes/taoyuan_runoff.yaml`（`verified: false`，verifier 會 WARNING，
回覆必須轉告使用者）。設計參考手冊為 **108-04-01 草案**。研究來源：
`docs/research/2026-10-17-taoyuan-drainage.md`。

## 先判斷法規路徑（研究文件 §1）

| 條件 | 路徑 | 本模組 |
|---|---|---|
| 108-09-26 起，桃園建築基地 | 桃園市建築基地開發排入雨水下水道逕流量標準：0.051 m³/m²、0.000014 m³/s/m²（建管處 / 雨水下水道科），流出抑制 14 圖 | ✅ 本模組主體 |
| 都市計畫區、基地 > 300 m² | 建技規則 4-3：0.045 m³/m² | ✅ 同時計算，取大者 |
| 108-02-01 起 ≥ 2 ha（114-09-03 後都市計畫區新建 ≥ 0.2 ha、一般 ≥ 1 ha） | 出流管制計畫書（水利法 83-7），SCS / 單位歷線 / H-A-V | ✅ `hydro/outflow.py` + `verify/outflow_check.py`（見下方「出流管制計畫書」） |
| 108-02-01 前、≥ 1 ha | 舊制排水計畫書（水務局） | ❌ 未支援 |

路徑本身（尤其 108–114 年單棟集合住宅是否需出流管制）**沒有函釋**，由工程師 / 使用者決定，agent 不得自行斷定。

## spec 欄位

| 欄位 | 說明 |
|---|---|
| `site_area_m2` | 基地面積 m²（≤ 300 m² verifier 提示免設） |
| `existing_building` | 可選 `{new_building_area_m2, legal_coverage_ratio}`；As = min(基地, 新增建築面積 / 法定建蔽率)，規則原文待核 → WARNING |
| `district` | 行政區名（例「桃園區」）。雨量站由 yaml `horner.stations.*.districts` 決定；目前 yaml 只有 桃園區 / 中壢區 / 八德區 |
| `station` | 可選覆寫，必須是 yaml 站名（中壢 / 桃園 / 八德）；其他行政區**必須**由工程師指定 |
| `subareas[]` | `{name, area_m2, landuse, c_override?}`；landuse 為 yaml `runoff_coefficients.classes` key（`mixed_residential`、`commercial`、`concrete_asphalt`…）；不給 c_override 用中值，給了須在範圍內。Σ面積須 = 基地（0.5%） |
| `tc_min` | 可選，省略用 yaml 建議 5 min |
| `storage` | `{type: raft/surface, cells: [{length_m, width_m}], effective_depth_m, outlet_well_area_m2}`（出流井面積從貯集量扣除） |
| `outlet` | `{type: rect_orifice, height_m, width_m, design_head_m}` 或 `{type: circ_orifice, diameter_m, design_head_m}`（`release_mode: orifice`，預設） |
| `release_mode` / `pump_q_cms` | `pump` → 排放量 = 抽水機設計抽水量，不給 outlet；同樣檢核容許排放量 85–100%（「抽水排放量亦不得超過容許排放量」原文研究文件未收錄 → WARNING） |
| `overflow_weir` | 可選 `{width_m, head_m}`；verifier 要求 Q ≥ Q20 全基地 |
| `pipes[]` | `{name, diameter_m 或 width_m+depth_m, material, n_override?, slope, design_area_m2, design_c?}`；material 為 yaml `manning_n.materials` key；n 為範圍的材料（塑膠管、草溝）必須給 n_override |
| `pipes[].design_c` | 選填，0–1：此管渠的設計逕流係數（上游集水分區面積加權 Σ(C·A)/ΣA）。給了 → 管渠 Q = design_c × I × A，result.json 該管渠多列 `C_design`；省略 → 用全基地綜合 C（`C_composite`，舊行為）。`drainage-plan` 由網路自動算出並寫入 calc_spec.json，不要手填 |

spec 不得出現任何計算結果（`extra="forbid"`）；不要把貯集量、Q、I 寫進 spec。

## verifier 檢查

提供貯集量 ≥ max(0.051, 0.045) × As；孔口 Q 在容許排放量 85–100%；溢流堰 Q ≥ Q20；
管渠設計流量（Q5，集水面積 × 加權 C）≤ 容量、流速 0.8–3.0 m/s、U 溝（寬 ≤ 60 cm）出水高
≥ max(30% 水深, 0.20 m)、最小管徑（≤ 600 m² ≥ 150 mm、≤ 1000 m² ≥ 200 mm）、RC 管材料 key 與管徑相符；
`--result` 時逐值比對重算（缺鍵 / 多鍵皆 error）；`--calc` 時 calc.md 須與重算計算表逐字相同（管渠表含「C」欄）。
法規 yaml 路徑鎖定：`codes_path` 只能是 `draftagent/*/codes/*.yaml`（schema 擋），且 verifier 要求 = repo 預設檔，
否則 error；重算一律讀預設 yaml（`verify/codes_lock.py`）。不要為了換係數改 `codes_path`。
紅燈 → 改 spec（尺寸），不得改 yaml 或 verifier。

## 必須由工程師決定（agent 不得代選）

- 法規路徑、雨量站（yaml 未對應的行政區）、C 取值（範圍內）、n 取值（範圍材料）
- 筏基坑尺寸 / 有效水深、孔口型式與尺寸、設計水深、溢流堰尺寸、管渠斷面與坡度
- 增改建 As 換算是否適用
- yaml 缺的東西：2 年重現期 Horner 係數、其他 10 個行政區的雨量站、寬 > 60 cm U 溝出水高、
  > 1000 m² 最小管徑 → 停下來告訴使用者缺什麼

## 已知疑點

- 圓形孔口公式：手冊 7.7 `Q = 2.0862·L²·(y−L/2)^0.5`；研究彙整另見 `2.0862×W×L×…` 寫法，yaml 只收前者，待核
- 設計參考手冊 URL 未記錄、正式版未確認
- 回覆只能說「通過本 repo 現有 verifier 的檢查項目」，不得宣稱符合法規

## 出流管制計畫書（水文水理計算）

適用判定 `hydro.outflow.applicability()`（114-09-03 修正辦法）：第 2 條 ≥ 1 ha → 計畫書；第 2-1 條都市計畫區
新建 0.2–1 ha → 計畫書，或流出抑制 / 貯留 ≥ 675 m³/ha 於建照前備查免提（**路徑由使用者決定**）；≤ 5 ha 且重力排水、
聯外 Q10 通洪 → 可選第十四點簡易檢核（520 m³/ha、0.16 cms/ha）。開發樣態是否屬第 2 條由工程師判定。

流程：寫 `OutflowControlSpec`（`draftagent/schema/outflow.py`）存 `runs/<ts>/spec.json` →

```bash
.venv/bin/python -m draftagent.hydro.outflow runs/<ts>/spec.json -o runs/<ts> --charts --swmm
.venv/bin/python -m draftagent.verify.outflow_check runs/<ts>/spec.json --result runs/<ts>/result.json
```

產出 result.json、附圖 16（開發前後 2/5/10 年逕流歷線）/ 18（H-A-V）/ 19（滯洪演算成果）PNG + PDF、
SWMM 5 `outflow_T10.inp`（hydrograph 模式：本 repo 入流歷線灌入池節點）。數值讀
`draftagent/hydro/codes/outflow_control.yaml`（`verified: false`）。範例：`examples/outflow-control.json`（`draftagent new outflow-control`）。

方法（全部確定性）：Horner 24 hr、交替區塊 10 min（最大值置中、右大左小）→ SCS-CN 累積法（開發前 CN > 70 以 70 計、
多分區面積加權）→ Tc（< 10 min 以 10 計；漫地流 T1 公式或流入時間 + 曼寧流下時間）→ SCS 無因次單位歷線
（附表五 33 點；體積校正至 1 mm × A 並回報校正前比值）→ 卷積 → 修正 Puls 滯洪演算（孔口 + 堰 + 外水位歷線
Ht = hp/4 + (3/4)(Qt/Qp)hp + Hb）。孔口 / 堰 / H-A-V 依技術手冊附件一。

| spec 欄位 | 必須由工程師提供 |
|---|---|
| `horner` | 站名、各重現期 a/b/c、`source_note`（治理規劃報告或鄰近站分析）。**agent 不得選站或補係數**；示範值須寫明「示範用，非實際測站」 |
| `pre` / `post` | 分區 `cn` 或 `scs_class`+`soil_group`（yaml 附表二）；`tc_min` 或 `flow_path` |
| `detention` | `layers`（高程 + 水面積，由低至高）、`freeboard_m`（yaml 無法定出水高） |
| `outlet` | 孔口 `vertical_/horizontal_ circular/rect`、底高、尺寸、`cd`（預設 0.61）；堰頂高、長、`cd` 或 `crest_width_m`/`weir_height_m` |
| `tailwater` | `free` / `fixed` / `hydrograph`（Hb、各重現期 Hp） |
| `site` | `gravity_drainage`、`outfall_q10_capacity_ok`、`use_simple_check` |

verifier：開發後出流 Q2/Q5/Q10 ≤ 開發前；最高水位 ≤ 池頂 − 出水高（溢頂 ERROR）；提供體積 ≥ 10 年演算最大蓄量 × 1.2；
安全係數 ≥ 1.2；選用簡易檢核時 520 / 0.16；水量守恆與單位歷線體積誤差 ≤ 1%；`--result` 逐值比對。

已知限制：堰潛沒折減（手冊附圖 1-5）與 Ogee 堰係數（附圖 1-3）為查圖未收錄（外水位高於堰頂 → WARNING）；
520 m³/ha 是否含 1.2 原文未明；聯外排水通洪僅 5 年之替代規則未實作；**不要在主行程 import pyswmm / swmm.toolkit**，
用 `hydro.swmm_export.run_swmm_subprocess`（本機子行程目前被 SIGKILL → 測試 skip）。

## 排水網路 spec → 平面圖 / 集水區圖 / 縱斷面 / 計算 一次產出（`drainage-plan`）

寫一份 `DrainageNetworkSpec`（`draftagent/drainage/model.py`，範例 `examples/drainage-plan.json`：
地面滯洪池、重力排放、池內溢流；`draftagent new drainage-plan` 取得），一次產出且數字同源：

| 產物 | 內容 |
|---|---|
| `drainage_plan.dxf` | modelspace = **TWD97 TM2 座標原值 m**（$INSUNITS=6）；paperspace「平面設計圖」「集水區分析圖」（圖紙 mm，viewport 依比例） |
| `profile_spec.json` + `profile.dxf` | 主幹線縱斷面（`profile_path` 或最長新設管段路徑）；滯洪池以構造物畫出 |
| `profile_outlet_spec.json` + `profile_outlet.dxf` | 出水側縱斷面（滯洪池 / 抽水機 → 放流井 → 出流口 → 受納溝斷面 + 設計水位） |
| `calc_spec.json` + `result.json` + `calc.md` | 由網路衍生的 `TaoyuanSiteDrainageSpec` 與計算（需 `storage` + `calc`） |
| `detail_spec.json` + `detail.dxf` | 排水系統各部詳圖（`derive_details`：U 溝 / 筏基 / 放流井 / 出流口接側溝 / 銘牌） |

```bash
draftagent build runs/<ts>/spec.json   # 統一 CLI：render + 全部 verifier（8 項）
.venv/bin/python -m draftagent.verify.drainage_plan_check runs/<ts>/drainage_plan.dxf --spec runs/<ts>/spec.json
.venv/bin/python -m draftagent.verify.profile_check runs/<ts>/profile.dxf --spec runs/<ts>/profile_spec.json
.venv/bin/python -m draftagent.verify.drainage_calc_check runs/<ts>/calc_spec.json \
  --result runs/<ts>/result.json --calc runs/<ts>/calc.md
```

上面單獨執行的 verifier 讀的是 build 寫出的中間檔；`draftagent build` 內的 verifier 則**不信任中間檔**：
profile_spec.json / profile_outlet_spec.json / calc_spec.json / detail_spec.json 一律由 spec 重新衍生（法規 yaml
固定用 repo 預設），與產物 JSON 逐鍵比對，不同 → error「中間檔與模型不符」；各圖 verifier 對重新衍生的 spec 執行。
手改中間檔或產物 JSON 一定紅燈，要改就改 DrainageNetworkSpec 重新 build。

### 管渠設計流量：上游加權 C

管渠 Q 用該管段**上游集水分區面積加權 C**（Σ Cᵢ·Aᵢ / Σ Aᵢ，Cᵢ = c_override 或 yaml 中值），不用全基地綜合 C。
`derive_calc` 把它寫進 calc_spec.json 的 `pipes[].design_c`；`hydro.taoyuan.compute` 用 design_c 算 Q；
`drainage_calc_check` 直接對衍生的 calc spec 判定。verifier 另有**獨立實作**（`drainage_plan_check.independent_pipe_flows`：
自建上游集合、yaml 直讀 C 與 Horner，不經 network / hydro.taoyuan）重算每段 C、承接面積、Q，與 calc spec 的 design_c
及 result.json 比對；上游加權 Q > 通水容量 → error。納入檢核的管段：新設、順坡（S > 0）、不在滯洪設施出流端以下
（出流端以下受孔口控制，不以合理化公式檢核）。上游沒有任何集水分區的管段 → 無法決定 C，衍生失敗（error）。

spec 重點（`extra="forbid"`；**長度、坡度、面積、流量、貯集量一律不得填**）：
- `units: "m"`、`crs: "EPSG:3826"`；所有 x / y 為 TWD97 E / N 原值（經緯度、區域座標會被擋）
- `nodes[]`：`id, kind（catch_basin / manhole / outlet_well / storage_inlet / outfall / existing_mh）, x, y, ground_el, invert_el, top_el?`
  （圖面「頂EL」= top_el，未給則 ground_el）
- `links[]`：`id, from, to, section（{type:u_ditch,w,h} / {type:pipe,d} / {type:box,w,h}，m）, material（yaml 曼寧 key）, n_override?, existing, pump`
- `subcatchments[]`：`id, polygon, landuse, c_override?, outlet_node`；Σ面積須 = 基地（0.5%）、互不重疊
- `storage`：`polygon`、`cells`（矩形筏基坑；省略則 polygon 須為矩形）、`effective_depth`、`inlet_node`、`outlet_node`、`outlet_well_polygon?`
  （拓樸上 inlet→outlet 視為連通；出流端以下管段不納入合理化公式管渠檢核）；高程欄位（工程師決定，全部選填）：
  `drain_mode`（gravity / gravity_pump / pump）、`bottom_el`、`max_water_el`（= bottom_el + effective_depth）、`top_el`、
  `orifice_invert_el` 或 `orifice_center_el`、`overflow_el`、`overflow_at`（storage / inlet_well）、
  `pump_start_el`、`pump_stop_el`（pump 型必填）、`pump_q_cms`、`allow_backwater` + `backwater_note`
- `existing_drains[]`（基地附近既有排水，點依流向排序，≥ 2 點需同時有 top_el / invert_el；`design_water_depth_m` 受納溝設計水深，
  **工程師提供**，agent 不得自填）、`road_centerline_els[]`
- `calc`：`outlet`（pump 型可省略，排放量 = storage.pump_q_cms）、`overflow_weir?`、`station?`、`tc_min?`（工程師決定）；`project.district` 決定雨量站
- `scales`：`plan`（≤ 300）、`catchment`（≤ 500）、`profile_h`（≤ 200）、`profile_v`；門檻讀 `taoyuan_drawing.yaml`
- 圖面放不下 → renderer raise，改 A1 或調比例（不得超過門檻），不要改 renderer 版面去硬塞

`drainage_plan_check` 檢查：INSUNITS=6、0 層無實體、拓樸（單一出流口 / 無迴圈 / 可達 / 順坡）、
每條管段標籤 S / L 與重算一致（±0.01）、節點 ATTRIB TOP / INV 與標籤頂 / 底 EL、縱斷面路徑樁號、
每張圖的 viewport 比例（≤ 門檻）、指北針、比例文字、圖例、TWD97 座標刻度（E / N 各 ≥ 2、位置與 viewport 換算一致）、
圖簽（TITLE = 統一圖名）、分區面積文字、數量一覽表與分區面積表。另外：

- **平面拓樸幾何**：每條 link 有 C-STRM（新設）/ C-STRM-EXST（既有）線，兩端點 = from / to 節點（±0.01 m）；
  C-STRM-FLOW 流向箭頭與 (to − from) 同向、落在管段緩衝帶內，每條 link 至少 1 個且不得有反向；管段標籤離自己的管段最近；
  C-CTCH 分區界線、C-PROP 基地界線、既有排水折線與 spec 多邊形 / 折線 Hausdorff < 0.01 m
- **viewport 可見性**：兩個 layout 的必要圖層不得在 viewport 凍結表內，圖層須 on / 未凍結 / plot；基地 bbox 須在 viewport 視窗內
- **物理合理性**（error）：管 / 箱涵兩端覆土 = 地面 − (管底 + 管高) ≥ 0；節點 top_el ≤ ground_el；節點在基地內
  （outfall、existing_mh 除外）；surface 型滯洪池不得與建物（building_footprints）重疊；既有排水溝底沿點序不得上升（受納溝逆坡）；
  有既有排水但出流口距受納溝 > `hydraulics.RECEIVE_MAX_DIST_M` → error「出流口未接到既有排水」（E1–E4 無法檢查，不再只是 WARNING）
- **出圖可讀性**：兩個 layout 印出字高 ≥ layers.yaml `text.min_print_height_mm`、文字框重疊 → error（viewport 凍結圖層的 model 文字不計）
- 法規 yaml：spec.codes_path ≠ repo 預設 → error

紅燈 → 改 spec，不得改 verifier 或 yaml。

### 高程相容性檢查 E1–E8（`drainage/hydraulics.py`，由 `drainage_plan_check` 呼叫，error 全部照算）

受納點：出流口投影到最近既有排水，沿線（依流向）線性內插溝底 / 溝頂 EL；距離 > `RECEIVE_MAX_DIST_M` → **error**「出流口未接到既有排水」（E1–E4 無法檢查）。
設計水位 = 內插溝底 + `design_water_depth_m`。每項 metrics 列兩個比較值與差值。

| 代碼 | 檢查 | 結果 |
|---|---|---|
| E1 | 出流口底 ≤ 受納溝底（+1 cm 視為同高） | error（倒灌） |
| E2 | 出流口底 < 受納溝設計水位；未給設計水深 | error；WARNING |
| E3 | 出流口底 > 溝頂 + 0.05 m | error（應切齊溝緣） |
| E4 | gravity / gravity_pump：孔口底 < 設計水位（無設計水深時 ≤ 溝底） | error（重力排放不可行） |
| E5 | 滿水靜水位 max(設計滿水位, 溢流口) > 進流管下游端底；溢流時最高水位（溢流口 + 堰頂水頭）> 進流管底 | error（allow_backwater + note → WARNING）；WARNING |
| E6 | 溢流口 ≥ 設計滿水位、≤ 池頂（inlet_well 為進流井地面）、≥ 放流井底；池內溢流時溢流時最高水位 ≤ 池頂；設計滿水位 ≤ 池頂；池底 ≤ 孔口 < 溢流；孔口設計水深 = 設計滿水位 − 孔口底 | error |
| E7 | pump：停機 < 起抽 ≤ 設計滿水位；起抽→停機容量 / pump_q ≥ yaml `design_criteria.pump_min_run_min`（15 min，verified: false）；停機高於池底 | error；呆水 WARNING |
| E8 | 隱含邊：進流井底 ≥ 池底；重力時孔口底 ≥ 放流井底（管段順坡由 check_network 檢查） | error |

高程未給時預設：池底 ≈ 放流井底、設計滿水位 = 池底 + 有效水深、孔口 = 池底、溢流 = 設計滿水位、池頂 = 進流井地面，並 WARNING
「高程未明確給定」。推導值不畫上圖；明確給定的水位才標在縱斷面（設計滿水位 / 溢流口 / 溢流時最高水位 / 孔口底 / 起抽 / 停機）與平面圖附註。

### 水位名詞（兩個不同的水位，不要混用）

| 名詞 | 定義 | 欄位 | 限制 |
|---|---|---|---|
| **設計滿水位**（貯留水位） | 池底 + 有效水深（有效貯集頂） | 網路 `storage.max_water_el`；詳圖 `raft_detention.storage_level_el` | ≤ 溢流口底（否則未滿即溢流，有效貯集量不成立） |
| **溢流時最高水位** | 溢流口底 + 溢流堰水頭（`calc.overflow_weir.head_m`，Q20 過堰） | 由程式推導；詳圖 `raft_detention.overflow_flood_el` | > 溢流口底、≤ 池頂 / 坑頂 |

詳圖筏基的水位鏈：孔口中心 < 設計滿水位 ≤ 溢流口 < 溢流時最高水位 ≤ 坑頂（`overflow_flood_el` 未給 → 該段跳過並 WARNING）。
溢流設在進流井（`overflow_at: inlet_well`）時，溢流時最高水位發生在進流井，不畫在池體 / 坑內。
舊詳圖欄位 `design_hwl_el` 自動映射為 `storage_level_el` 並 WARNING；若舊 spec 把它當「溢流時水位」填（高於溢流口），映射後會紅燈 → 改填正確語意。
平面圖出流口標籤在有設計水深時加「受納溝設計水位 EL=」，verifier 會比對。

### 重力 vs 抽水的判斷

1. 先要工程師給受納溝設計水深 → 算受納設計水位 WL。
2. 出流口底須介於 WL 與溝頂之間（E2 / E3）；放流井、孔口底再往上游逐段順坡（E8）。
3. 重力型要同時滿足：孔口底 ≥ WL（E4）**且** 池設計滿水位（及溢流口）≤ 進流管底（E5）。即「進流管底 − 受納設計水位」必須 ≥ 有效水深。
   這個落差不夠（典型：地下室筏基、平坦基地）→ 只能 `pump`（或下層抽水的 `gravity_pump`），並給起抽 / 停機水位、抽水量；
   溢流設在進流井（`overflow_at: inlet_well`，溢流管重力接放流井）。
4. 抽水量 ≤ 容許排放量（WARNING 原文待核），起抽→停機容量要夠運轉 15 min（E7）。
5. 不要為了過 E5 自己加 `allow_backwater`；那是工程師對管網可承受迴水的書面判斷。
範例：`examples/drainage-plan.json`（重力型、池內溢流）。常見錯誤：出流口設在受納溝底（E1）、筏基滿水位高於進流管底（E5）。

## 一個模型 → 整套圖（`drainage-plan` 全套）

**不要再手寫詳圖或縱斷面的高程。** 審查最常見缺失之一是「圖說要求呈現方式務必一致」「銜接處各高程」：
舊做法每張詳圖由 agent 獨立填數字，曾出現筏基孔口「接放流井」卻比放流井底低約 2 m（水往上爬）。
現在 `DrainageNetworkSpec` 是唯一來源，`drainage/derive.py` 衍生所有圖：

| 衍生 | 來源（agent 不填） |
|---|---|
| `derive_profile` 主幹（上游 → 進流井） | 節點 / 管段；滯洪池池底 / 池頂明確給定時畫成構造物（池體 + 有效水深陰影 + 水位線 + 抽水機），範圍 = storage polygon 投影到路徑流向 |
| `derive_outlet_profile` 出水側 | 放流井 → 出流口路徑；放流井外長 = `detail_defaults.outlet_well`；受納溝底 / 頂 / 設計水位 = 出流口處內插 |
| `derive_details` 詳圖 | calc.outlet 為孔口（非 pump）時**必出孔口板詳圖**（`orifice_plate`，孔徑 / 尺寸 = calc.outlet、孔心 EL = 孔口底 + 孔高 / 2）；U 溝依斷面去重；筏基 = `hydraulics.resolve_storage`（池底、池頂、設計滿水位、池內溢流時的溢流口 + 溢流時最高水位、起抽 / 停機、入池管底）；放流井 = 節點 + 出流 link（井端 / 出口管底、管長 = link 長 − 井外長 / 2）+ 抽水出水管 / 溢流管入井管底；出流口 = 接既有側溝（2 m 內有受納溝）或保護工；銘牌容量 = result.json provided_m3 |

spec 新欄位（全部選填，工程師決定；未給者推導並 WARNING「銜接管高程未給定」）：
`storage.inflow_invert_el`（入池進流管底；筏基在地下室時進流井底遠高於池頂，**必須給**，否則詳圖進流管頂高於坑頂 → detail_check 紅）、
`storage.pump_outlet_invert_el`（抽水出水管入放流井管底）、`storage.overflow_pipe_invert_el`（進流井溢流管入放流井管底）、
`storage.raft_cells` / `storage.beam_width`（詳圖坑格數 / 地梁寬）、`scales.profile_outlet_h/v`、`drawing_nos.profile_outlet/detail`。
`detail_defaults`：壁厚、版厚、蓋板、集水坑深、管徑等**構造示意值（工程師確認）**，不是法規數值。
hydraulics 新增 **E9**：入池管底介於池底與進流井底、抽水出水管入井管底介於放流井底與井頂、溢流管入井管底介於放流井底與溢流口。

詳圖模板擴充（向後相容）：`raft_detention.pump`（集水坑 + 抽水機 + 出水管 + 起抽 / 停機 ▽，`orifice` / `overflow_el` 可省略）；
`outlet_well` 可無孔口 / 溢流堰（單室）、`pump_inflow`、`overflow_inflow`、`outlet_pipe_end_el`、`ditch.design_wl_el`；
新模板 `ditch_joint`（出流口接既有側溝：管口切齊側溝內壁、設計水位）。detail_check 新增同圖銜接規則：
重力筏基標示「接放流井」時，放流管底（孔口中心 − 管徑 / 2）≥ 放流井井底，否則 error。

## 跨圖一致性（`verify/sheetset_check.py`）

```bash
.venv/bin/python -m draftagent.verify.sheetset_check runs/<ts> [--spec runs/<ts>/spec.json] [--json]
```

只從各圖 TEXT / ATTRIB 解析（詞彙在 `drainage/sheet_vocab.py`），建「元素 id → 屬性 → [(圖, 值)]」，同一元素
在平面圖 / 兩張縱斷面 / 詳圖 / result.json 的值必須一致（高程 0.005 m、坡度 0.01 %、貯集量 0.01 m³）。
紅燈訊息列「元素、屬性、圖A值、圖B值」。另檢查：每張圖圖號唯一、圖名 = 統一圖名、比例 ≤ taoyuan_drawing.yaml 門檻。
detail_check 以「<名稱>… EL=」前綴比對高程（不只比數值集合），跨圖數值一致性則是 sheetset_check 的工作。
sheetset_check 另把平面設計圖 modelspace 標註納入比對：滯洪設施「V=… m³」、「有效水深 … m」、既有排水各點「頂EL= / 底EL=」、
「路中EL=」；calc.outlet 為孔口時，詳圖必須有孔口尺寸文字且 = calc_spec.json 的孔口，孔口板孔徑標註（DIMENSION 實際顯示文字）= calc。
紅燈 → 改 spec（或修 renderer 的文字契約），不得改 verifier 容差。

## 排水計畫書本文（`drainage-report`）

一份 `DrainageReportSpec`（`draftagent/report/model.py`；範例 `examples/drainage-report.json`，`draftagent new drainage-report`）
= `network`（一份完整 `DrainageNetworkSpec`，需 storage + calc）+ `report`（計畫書專屬欄位）。`draftagent build` 先跑
drainage-plan 的 render（整套圖 + 計算），再產出 `report.docx` / `report.md`（+ `report_figures/`、`report_meta.json`）。

| `report` 欄位 | 說明 |
|---|---|
| `title`、`date` | 計畫書名稱、民國日期 |
| `applicant` / `designer` / `engineer` | 申請人 / 設計單位 / 簽證技師；未提供就省略（預設「○○○」佔位，WARNING）。不得寫真實公司名 |
| `land[]` | `{section, lot}` 段名 / 地號（使用者提供；範例與測試一律合成） |
| `zoning`、`coverage_ratio_pct`、`excavation_ratio_pct` | 使用分區、建蔽率 / 開挖率（使用者提供；未提供 → null，表內「未提供」+ WARNING） |
| `existing_drainage` | 基地附近排水系統現況**描述**（測量值放 `network.existing_drains`） |
| `photos[]` | `{path, caption}`（png / jpg；path 相對於執行目錄）；可空 |
| `unit_prices[]` | `{item, spec?, unit_price}`：item / spec 必須對到數量表（`derive.quantities`）恰好一列；項目只有一種規格時 spec 可省略。**數量由模型算，不得填** |
| `extra_costs[]` | 模型無法計量的項目（`{item, spec, unit, qty, unit_price?}`，數量由使用者提供） |
| `notes` | 各章補充文字（key：summary / purpose / scope / overview / basic_data / facilities / cost）；**不得含數字** |

單價沒給 → 不要編，省略該列：造價表單價 / 複價留空，總價寫「總工程造價（不含未提供單價項目）」並 WARNING；回覆要列出缺哪些單價。

本文由程式組成（`draftagent/report/content.py`）：章節依 `report/codes/report_format.yaml`（摘要、一～六章、附圖），
面積附坪、Horner 係數 / 逕流係數 / 設計基準表一律讀 yaml 並標 yaml 來源與 `verified: false`，附圖清單列 14 項統一圖名，
本 repo 產出的圖（平面 / 縱斷面 / 詳圖 / 集水區）以 DXF 圖簽的圖號 / 比例嵌入 PNG。結論句固定：
「本計畫書圖說通過本 repo 現有 verifier 之檢查項目；法規數值尚待人工核對原文，並不代表符合法規。」

`report_check`（另加 drainage-plan 全部 8 個 verifier）：章節完整且依序、14 項統一圖名與 DXF 圖簽 / spec 圖號比例一致、
表格與「標籤 = 數值」逐值對重算（顯示值須是正確四捨五入）、未知「=」標籤 → error、法規數值必附〔yaml：…；verified: …〕、
附圖嵌入數 = DXF 圖紙數 + 照片數且非空白、docx 與 md 段落 / 表格 / 圖片逐項相同、不得有肯定式合規宣稱、
淹水潛勢只能作風險提示、不得有真實識別資訊樣式、A4 / 邊界 / 字型（標楷體 + Times New Roman，本機無標楷體 → fallback + WARNING）/ 目錄。
紅燈 → 改 spec 重新 build；**不要手改 report.md / report.docx**（會與重算或另一份不一致）。
版面數值（邊界、字號）研究文件未收錄，`report_format.yaml` 為 repo 暫定值（verified: false）。
