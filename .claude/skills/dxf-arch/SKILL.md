---
name: dxf-arch
description: 建築平面圖 DXF 製圖慣例與 PlanSpec / FloorPlanSpec（多房間 floor-plan）用法。要產出或檢查平面圖 DXF（圖層命名、mm 單位、牆/門/窗/尺寸畫法、tools/gen_dxf.py、verify.dxf_check、floorplan_dxf_check）時先讀。
---

# dxf-arch：平面圖 DXF

流程固定：寫 `PlanSpec` JSON → `tools/gen_dxf.py` → `draftagent.verify.dxf_check`。
不要直接用 ezdxf 手寫腳本畫圖。

## 單位與座標

- DXF 一律 **mm**，`$INSUNITS = 4`（renderer 會設定，verifier 會檢查）
- x 向東、y 向北；房間西南角通常放 (0, 0)
- 「4.5×3.3 m、牆厚 15 cm、門 80 cm」→ `width: 4500, depth: 3300, wall_thickness: 150, width: 800`

## 圖層（以 `draftagent/render/codes/layers.yaml` 為準）

圖層名稱、ACI 顏色、線型、線寬（1/100 mm）、是否出圖集中定義在 `render/codes/layers.yaml`，
renderer 以 `render/standards.apply_layers(doc, names)` 套用（同時設 `$LWDISPLAY=1`）；
`python -m draftagent.verify.standards_check <dxf>` 檢查使用中圖層都在表內（未知 → WARN）、
線寬 / 線型與表一致（不一致 → ERROR）。命名採 **AIA/NCS 風格前綴，非台灣官方標準**；
線寬是 **業界慣例（`kind: convention`），不是法規數值**。新增圖層先改 yaml，不要在 renderer 硬寫屬性。
中文字一律 `standards.text_style(doc)`（樣式 `CJK`）；字型檔預設 `msjh.ttc`（微軟正黑體，Windows 內建），可設 `DRAFTAGENT_CAD_FONT` 覆寫。

精簡表（完整欄位與說明見 yaml）：

| 圖層 | 內容 | 線寬 mm | 線型 |
|---|---|---|---|
| `A-WALL` | 牆輪廓（封閉 LWPOLYLINE；門窗處輪廓繞過開口） | **0.50** | 實線 |
| `A-DOOR` / `A-WIND` | 門扇線 + 90° 開啟弧（1 門 = 1 ARC）/ 窗框 + 中線（1 窗 = 1 封閉框） | 0.25 | 實線 |
| `A-DIMS` / `A-TEXT` | 尺寸標註（量測值 = 外尺寸）/ 房名（1 房 = 1 TEXT） | 0.18 | 實線 |
| `A-BLDG` | 建物外廓（排水平面底圖） | 0.25 | 實線 |
| `H-STRC` | 詳圖結構輪廓（剖面切到的混凝土） | 0.50 | 實線 |
| `H-PROF` / `H-PIPE` / `C-STRM` / `C-STRM-NODE` | 縱斷面線 / 管線閥件 / 新設管溝 / 節點符號 | 0.35 | 實線 |
| `C-PROP` | 基地境界線 | 0.35 | DA-CHAIN |
| `C-STRM-EXST` / `C-CTCH` | 既有排水設施 / 集水分區界 | 0.25 | DA-DASH |
| `H-PROF-WL` / `H-WATR` / `H-HIDN` | 水位線 / 隱藏線 | 0.25 | DASHED |
| `H-GRND` / `H-RBAR` / `*-FLOW` / `C-STRM-STOR` | 地面線 / 鋼筋 / 流向 / 滯洪範圍 | 0.25 | 實線 |
| `H-TEXT` / `H-DIMS` / `H-TABL` / `C-*-TEXT` / `C-LGND` / `C-TABL` / `X-TEXT` | 文字、標註、表格、圖例 | 0.18 | 實線 |
| `C-ROAD` | 道路中心線 | 0.18 | DA-CENTER |
| `H-GRID` / `H-HATC` / `X-GRID` / `C-TOPO` | 格線 / 材料 hatch / 座標格 / 等高線 | 0.13 | 實線 |
| `H-CL` / `S-GRID` | 中心線、剖切線 / 結構格線 | 0.13 | CENTER |
| `X-FRAME` | 圖框粗框、圖名、比例尺、指北針（紙邊細線由實體覆寫） | 0.70 | 實線 |
| `X-TTLB` | 圖簽框（內部格線由實體覆寫為細線） | 0.35 | 實線 |
| `X-VPORT` | 視埠框（**不出圖**） | 0.13 | 實線 |

個別實體可用 entity lineweight 覆寫（如縱斷面溝底 0.70、圖框紙邊 0.18），表中是圖層預設值。
`0` 層不得有任何實體。

## PlanSpec 欄位（`draftagent/schema/plan.py`，extra 欄位會被拒）

| 欄位 | 說明 |
|---|---|
| `title` | 圖名（畫在 X-FRAME） |
| `wall_thickness` | 牆厚 mm |
| `rooms[]` | `name` + `rect{x,y,width,depth}` 或 `polygon[[x,y],...]`（二擇一） |
| `openings[]` | `type: door/window`、`room`（多房必填）、`side: N/E/S/W`（僅 rect）或 `edge: i`（polygon 第 i 段 = v[i]→v[i+1]）、`offset`、`width`、門另有 `swing: in/out`、`hinge: start/end` |
| `dimensions` | `auto_overall`（預設 true，南側標總長、西側標總寬）、`offset`（尺寸線離牆 800）、`text_height`（model mm；**預設 None = 印出 2.5 mm × print_scale**，一般不要填） |
| `frame` | 是否畫圖框（預設 true） |
| `print_scale` | 出圖比例分母（**預設 50** = 1/50）。renderer 依此設定文字 / 標註字高，verifier 依此換算印出字高；改比例只改這裡 |

關鍵慣例：
- **房間外框 = 外牆外緣**，牆往內長。所以 `rect.width` 就是標註出來的外尺寸
- `offset`：N/S 牆自**西側外角**起算，E/W 牆自**南側外角**起算；polygon 自 `v[i]` 起算
- 開口兩端須離牆段端點 ≥ `wall_thickness`，否則 schema 驗證失敗
- 置中門：`offset = (牆長 - 門寬) / 2`
- v0 每房各自一圈牆，不處理共用牆
- schema 另擋：房名重複、房間外框無效或兩兩重疊（相鄰 / 共邊可）、同一牆段的開口區間重疊

範例：`examples/plan.json`（`draftagent new plan` 會由它產生可 build 的 .py spec）。
範例只示範欄位寫法，數值與任何評估考題無關，不要照抄尺寸：

```json
{
  "title": "範例 書房平面圖",
  "wall_thickness": 150,
  "rooms": [{"name": "書房", "rect": {"x": 0, "y": 0, "width": 4500, "depth": 3300}}],
  "openings": [
    {"type": "door", "side": "E", "offset": 600, "width": 800, "swing": "in", "hinge": "end"},
    {"type": "window", "side": "N", "offset": 1350, "width": 1800}
  ]
}
```

## 指令

```bash
TS=$(date +%Y%m%d-%H%M%S); mkdir -p runs/$TS   # spec 先存到 runs/$TS/spec.json
.venv/bin/python tools/gen_dxf.py runs/$TS/spec.json -o runs/$TS/plan.dxf
.venv/bin/python -m draftagent.verify.dxf_check runs/$TS/plan.dxf \
  --expect-dim 4500 --expect-dim 3300 --spec runs/$TS/spec.json [--json] [--tol 0.5]
```

`dxf_check` 檢查：INSUNITS=mm、必要圖層 A-WALL/A-DOOR/A-DIMS、0 層無實體、A-WALL 全為封閉
polyline、DIMENSION 量測值（由 defpoints 重算）、`--expect-dim` 值存在；`--spec` 另比對門數、窗數、房名與 spec 外尺寸。

**DIMENSION 讀實際顯示文字，不推算**（`verify/dimtext.py`）：讀 geometry block 裡實際畫出的 TEXT / MTEXT，
以**完整數字**比對量測值（「45000」不算 4500）；`dimrnd` / `dimpost` / `dimalt` 非預設一律 error（會改變顯示值）；
平面圖 `dimlfac ≠ 1` → error；文字覆寫也以完整數字比對（`<>` 以量測值代入）。不要用 dimpost 加單位、不要手寫覆寫文字。

`--spec` 時另由 spec **獨立重建幾何**比對（不是只數數量）：
- 牆：每房「外框 − 內縮牆厚 − 開口」的聯集 vs A-WALL 封閉線組成的多邊形，**對稱差面積 ≤ 0.5%**（牆縮放、牆厚畫錯會紅）
- 門弧：每扇門一條 A-DOOR ARC，圓心 = 鉸點、半徑 = 門寬（±1 mm）、開啟 90°，起訖角符合 hinge / swing
- 窗：每個 A-WIND 封閉框落在對應開口範圍內且面積 ≥ 開口 90%
- 房名：插入點（與對齊點）在該房淨空多邊形（外框內縮牆厚）內

**可讀性**（`verify/legibility_check.py`，所有 DXF 圖種共用）：
- 印出字高 ≥ `render/codes/layers.yaml` 的 `text.min_print_height_mm`（2.0 mm，製圖慣例非法規）；平面圖以 `print_scale`
  換算（model 字高 / print_scale）；DIMENSION 用 dimtxt × dimscale。不給 `--spec` 時用 `--print-scale`，都沒有 → 略過並 warning
- 文字框兩兩重疊 > 小框 10% → error；文字壓到 A-WALL / A-DOOR / A-WIND → warning

紅燈 → 改 spec 重跑，不得改 verifier。

回報時只能說「通過本 repo 現有 verifier 的檢查項目」，不得說「符合法規」。

## 多房間平面圖（floor-plan，FloorPlanSpec）

`plan` 是**單房**圖種（每房各自一圈牆，不處理共用牆）。多個相鄰房間、要走廊 / 門連通、要 IFC 時用
`floor-plan`：`draftagent new floor-plan` 取範例 → 改 spec → `draftagent build spec.json`
（一次產出 `floorplan.dxf` + `floorplan.ifc`）。spec 欄位、幾何約定（房間以**外牆內緣 / 內牆中心線**為界、
鋪滿室內、面積 = 淨面積）、門窗定位規則、IFC 對應見 **ifc-building** skill。

DXF 畫法（`render/floorplan.py`）：

| 圖層 | 內容 |
|---|---|
| `A-WALL` | 外牆（外框 − 室內範圍 − 開口）封閉線，線寬 BYLAYER 0.50；內牆（所有共用邊界合併、中心線兩側各半牆厚 − 開口）封閉線，**實體線寬覆寫 0.35**——外牆 / 內牆線寬不同；共用牆只畫一次 |
| `A-DOOR` | 門扇線 + 90° 開啟弧（1 門 = 1 ARC），往 `swing_into` 房間開；外門鉸點在外牆內緣 |
| `A-WIND` | 窗框（貫穿外牆厚）+ 中線 |
| `A-TEXT` | 房名 + 淨面積「x.xx m²」（各 1 TEXT，放在淨空內、避開門扇範圍） |
| `A-DIMS` | 第一排：尺寸鏈（外緣 → 每道與該向垂直的內牆中心線 → 外緣，太短的段併入鄰段避免數字重疊）；第二排：外總尺寸（南側總長、西側總寬） |
| `X-FRAME` | 圖框、圖名 |

`floorplan_dxf_check` 讀回檢查：INSUNITS=mm、必要圖層、0 層無實體、A-WALL 全為封閉 polyline；
牆由 spec **獨立重建**（外牆環 + 共用邊界兩側各半內牆厚 − 開口）與 A-WALL 對稱差 ≤ 0.5%、外牆線寬 > 內牆線寬；
門弧圓心 / 半徑 / 90° / 開向；窗框落在開口內；房名與面積文字在該房淨空內、面積數字 = spec 淨面積（0.01 m²）；
DIMENSION 讀**實際顯示文字**（dimrnd / dimpost / dimalt 禁用、dimlfac = 1），外總尺寸存在，尺寸鏈端點都在
外緣或內牆中心線上、相接且涵蓋外緣到外緣；印出字高與文字重疊（legibility_check，error）、文字壓牆 / 門 / 窗（warning）。
另跑 `standards_check`（圖層表線寬 / 線型）與 `units_check`。

常見紅燈：房間畫到外牆外緣（應從內緣算起）→「超出室內範圍」；忘了把走廊 / 玄關列成房間 →「未分配」；
兩房只碰到一個角 →「沒有共用牆」；小房間共用牆太短 →「超出牆段可用範圍」；居室全部被內牆包住 →「沒有任何對外窗」。

## 縱斷面圖（排水系統縱斷面，ProfileSpec）

桃園檢核表最常被點名漏附的圖。流程：寫 `ProfileSpec` JSON → `tools/gen_profile.py` → `draftagent.verify.profile_check`。

- **單位**：spec 內樁號、高程、尺寸一律 **m**；DXF 以**圖紙 mm** 為座標（$INSUNITS=4，1:1 出圖），
  renderer 換算 `x = Δ樁號 × 1000 / h_scale`、`y = Δ高程 × 1000 / v_scale`。不要自己換算。
- **流向 = 樁號遞增方向**；第一個節點是最上游。`segments[i]` 連接 `nodes[i]`→`nodes[i+1]`，數量 = 節點數 − 1。
- **坡度不准填**：程式由 `invert_el` 與樁號差算 `S = (上游溝底 − 下游溝底) / 距離 × 100 %`（順坡為正）。
  逆坡只有 `segments[i].pump: true` 才允許，否則 verifier 紅燈。
- `h_scale` 分母上限讀 `draftagent/hydro/codes/taoyuan_drawing.yaml`（`drainage_profile`，S≧1/200；verified: false）。
  schema 驗證與 verifier 都會擋 1/300。`v_scale` 可誇張（常用 1/50、1/100）。
- `levels[]`：特殊高程（`kind`: pipe_in / pipe_out / overflow / design_wl / pump_start / pump_stop / other）。
  label 含「進水管」「放流管」者圖上必須出現「<label> EL=xx.xx」（審查常見缺失）。水位類畫在 `H-PROF-WL`（DASHED）。
- `datum_el` 省略時 = floor(最低高程) − 0.5。
- A3 放不下會 raise（A3 @1/200 大約 60 m）；改 `sheet.size: "A1"` 或分張，**不要**放大 h_scale 分母。

| 圖層 | 內容 |
|---|---|
| `H-PROF` | 地面線、溝底線（粗）、溝頂/管內頂線、井框、管線高程短線 |
| `H-PROF-WL` | 水位線（DASHED）＋▽ |
| `H-TEXT` | 節點名、特殊高程文字＋引線、圖名、比例文字、流向、附註 |
| `H-GRID` | 高程尺、刻度數字、格線、樁號格線 |
| `H-TABL` | 表格帶列名與數值（線＋TEXT，不用 ACAD_TABLE / MLEADER） |

表格帶列（由上而下，列名字串是 verifier 契約，定義在 `schema.profile.PROFILE_TABLE_ROWS`）：
樁號（`0+012.00`）、地面高程 EL、溝頂高程 EL、溝底高程 EL、道路中心樁高程 EL、坡度 S (%)、距離 (m)、斷面。
節點值直書、放在樁號格線左側；高程一律兩位小數。

```bash
TS=$(date +%Y%m%d-%H%M%S)-profile; mkdir -p runs/$TS   # spec 先存到 runs/$TS/spec.json
.venv/bin/python tools/gen_profile.py runs/$TS/spec.json -o runs/$TS/profile.dxf --png runs/$TS/profile.png
.venv/bin/python -m draftagent.verify.profile_check runs/$TS/profile.dxf --spec runs/$TS/spec.json [--json]
```

`profile_check` 另檢查：INSUNITS=4、0 層無實體、DIMENSION dimlfac=1 且讀實際顯示文字（dimrnd / dimpost / dimalt 禁用）、
圖簽屬性不空、**實際幾何比例**（樁號文字 x 間距、高程尺數字 y 間距）與比例文字一致。

縱斷面**幾何**也會驗（不只表格帶文字），要點：
- **高程尺**：每個刻度數字（H-GRID TEXT）旁須有同 y 的短刻度線，刻度 y ↔ EL 間距一致，且以「基準高程 EL=」與表格帶頂對齊驗絕對值
  （數字整體 +1.00 會紅）
- **樁號欄**：每個樁號文字右側須有樁號格線，x 位置 = 水平比例換算（每一欄都驗，不只頭尾）；其他節點列的值須與樁號欄同 x
- **節點名**在該節點樁號的 x；流向箭頭（H-TEXT 封閉三角形）須指向樁號遞增
- **H-PROF 線**：每個 segment 的地面線、溝底線、溝頂線，在兩端節點 x 處的 y 回推 EL 須等於 spec（±0.5 mm）；線被刪、上移都會紅
- 「<label> EL=」特殊高程文字須在該 level 的樁號附近
- **TEXT 插入點與對齊點一致**：非預設對齊的 TEXT，insert 須落在依對齊點量的文字框（外擴 1 字高）內；
  兩者不一致代表檔案被改過一半（CAD 與檢視器顯示位置不同）→ error
- 物理合理性（由 spec）：溝頂不得高於地面；U 溝 / 明溝 溝頂 − 溝底 = 斷面溝高；「進水管」EL 不得低於該樁號溝底；
  「放流管」EL 不得低於受納點溝底 / 設計水位
- 字高 ≥ 2.0 mm、文字重疊 → error；文字壓 H-PROF 線 → warning範例見 `examples/drainage-profile.json`（`draftagent new drainage-profile`）。

## 圖框（SheetSpec，render/sheet.py）

- 紙張 `A3` / `A1`，橫式，尺寸依 **ISO 216**（`schema.sheet.ISO216_A`）。內框邊距預設左 25、其他 10 mm——
  **CNS 3 原文未取得，邊距暫定**，可用 `sheet.margins` 覆寫。
- `title_block`：`project`、`drawing_no` 必填；`drawing_title` 空白時用圖面 `title`；`date`、`designer`、
  `checker` 可空（renderer 不自動填日期，保持可重現）。**比例欄由程式依比例產生，spec 沒有 scale 欄位。**
- 圖簽是 block `TTLB-<size>`，ATTRIB tag：PROJECT、TITLE、DWG_NO、SCALE、DATE、DESIGNER、CHECKER（X-TTLB 層）。
- 外框（紙邊細線）、內框（粗線）、比例尺 bar（含「水平 1/200」等數值文字）、指北針 block `NORTH-ARROW` 在 X-FRAME 層。
  `north_arrow: null` 依圖別預設（縱斷面 = 不畫）。
- 中文字用 TrueType 文字樣式 `CJK`（字型檔常數 `CJK_FONT = msjh.ttc`，微軟正黑體；`DRAFTAGENT_CAD_FONT` 可覆寫）。接收端沒有該字型時 CAD 自行替換；
  PNG 預覽（`render/preview.py`）會把本機找不到的字型換成 STHeiti Light / Hiragino Sans GB / Noto CJK / 微軟正黑體
  中第一個存在者（Hiragino 在 ezdxf 下「進」字會出現實心方塊，所以 macOS 先用 STHeiti）。
- 預覽 PNG 一定要親自看：文字是否方塊、數字是否 ×100、表格帶是否對齊、標註是否壓線。

## 排水詳圖模板（DetailSheetSpec，render/detail.py）

「排水系統各部詳圖」（檢核表 S≧1/500）。流程：寫 `DetailSheetSpec` JSON → `tools/gen_detail.py` →
`draftagent.verify.detail_check --spec`。一張圖紙放多個參數化詳圖，自動 shelf packing（放不下會 raise，
改 `sheet.size: "A1"` 或調比例 / 分張）。

- **單位**：尺寸欄位一律**實尺 mm**；欄位名 `*_el` 一律 **m、最多兩位小數**（圖面 `EL=xx.xx`）。
  DXF 座標 = 圖紙 mm（$INSUNITS=4），每個詳圖依自己的 `scale` 縮放。
- **尺寸標註 = dimlfac 法**：DIMENSION 量圖紙距離，override `dimlfac = scale`，顯示值才是實尺 mm。
  所以詳圖的 dimlfac **不是 1**（與平面圖 / 縱斷面相反）；verifier 會檢查 dimlfac = 比例分母、
  **讀 geometry block 的實際顯示文字**（dimrnd / dimpost / dimalt 禁用）並與量測 × dimlfac（含覆寫）一致，
  顯示值多重集合與 spec 推導的應有尺寸逐一相符。不要手改標註文字。
- 共同欄位：`id`（唯一）、`template`、`title`、`section_code`（如 `A-A`，全圖唯一；審查常抓代號誤植）、`scale`。
- 模板（`template`）：

| template | 內容 | 重點欄位 |
|---|---|---|
| `u_ditch` | U 型溝斷面 | `inner_width` W、`inner_depth` H、`wall_t`、`base_t`、`gravel_t`、`cover{thickness,type_text}`、`rebar{bar,spacing}`（示意，不算配筋）、`invert_el` |
| `raft_detention` | 筏基滯洪坑剖面（`pump` 有值 = 抽水型：集水坑 / 抽水機 / 起抽停機 ▽，孔口與池內溢流可省略） | `n_cells`、`cell_width`、`beam_width`、`pit_depth`、`base_slab_t`、`top_slab_t`、`bottom_el`、`effective_depth`、`storage_level_el`（設計滿水位；舊名 `design_hwl_el` 自動映射並 WARNING）、`overflow_el`、`overflow_flood_el`（溢流時最高水位）、`orifice{shape,diameter/width,height,center_el}`、`inflow_pipe{diameter,invert_el}`、人孔 / 爬梯 / 通氣管、`valves[]`、`check_valve_at` |
| `outlet_well` | 放流井平面＋剖面 | A 室 / 隔牆（孔口＋溢流堰）/ B 室、`top_el`、`bottom_el`、`orifice`、`weir{width,crest_el}`、`outlet_pipe`、`ditch{inner_width,bottom_el,top_el}`、`cleanout_size` |
| `orifice_plate` | 孔口板正視＋側視 | `plate_width/height/t`、`orifice`、`orifice_center_height`、`material_text`、`fixing_text`（使用者提供） |
| `outfall_protection` | 出水口保護工剖面 | 端牆、`apron_el/length/t`、`cutoff_depth`、`sill_height`（消能檻）、`riprap_length/t/text` |
| `ditch_joint` | 出流口接既有側溝接頭 | `pipe`（管底）、`ditch{inner_width,bottom_el,top_el,design_wl_el}`、`ground_el`；管口切齊側溝內壁 |
| `nameplate` | 滯洪設施銘牌 | `plate_width/height`、`facility_name`、`capacity_m3`、`maintainer`、`phone`、`warnings[]`；未提供 → 「○○○」 |

- **不得自己補數值**：材質、螺栓、拋石粒徑、電話、容量等由使用者 / 計算書提供；沒有就保留預設文字或「○○○」。
- 幾何合理性**不在 schema 擋**（錯誤 spec 仍可出圖給人看），由 verifier 報 error：
  有效水深 ≤ 坑深；孔口中心 EL < 設計滿水位（坑底 + 有效水深）EL ≤ 溢流口 EL < 溢流時最高水位（溢流口 + 堰頂水頭）EL
  ≤ 坑頂 EL（坑頂 = 坑底 + 坑深，程式推得；名詞定義見 tw-drainage「水位名詞」）；
  放流井 孔口 < 堰頂 < 井頂、放流管底 ≥ 接入側溝底、管頂 ≤ 溝緣；出水管底 ≥ 護坦頂。
- 必要註記（審查常見缺失，verifier 檢查）：筏基 → 逆止閥、溢流口、人孔、爬梯；放流井 → 「切齊溝緣」、
  清掃孔、溢流堰；銘牌 → 容量；保護工 → 護坦 / 拋石 / 截水牆。

detail_check 檢查要點（除上述）：
- **標籤式 EL**：高程依「<名稱>… EL=」前綴比對（例「坑底 EL=」「井頂 EL=」），不只比數值集合；坑底 / 坑頂互換會紅
- **▽ 比例**：每個 EL 文字須對應一個 ▽ 標示點（或引線），同一詳圖內 ▽ 尖端 y = a + EL × 1000 / scale；
  同一註記欄內 EL 文字上下順序須與高程順序一致
- **H-WATR 水位線**：應畫水位線的高程（設計滿水位、溢流時最高水位、起抽 / 停機、受納溝設計水位等）須有 y 一致的 H-WATR 線
- **否定詞**：必要註記前面有否定詞（不設、未設、免設、不需、無需、免、無、取消、省略）不算有標示（「本案不設逆止閥」→ 紅）
- 銘牌容量以完整格式「<數值> m³」比對；剖面標示 / 剖切線字母須與 `section_code` 相符
- **本體幾何**：最大混凝土 hatch / H-STRC 外框 bbox = spec 推導的本體尺寸（整體縮放而標註不動會紅）
- **hatch 比例範圍**：`HATCH_SCALE_RANGE`（AR-CONC / EARTH / GRAVEL / ANSI31）外 → error；表外 pattern → warning
- 字高 ≥ 2.0 mm、文字重疊、TEXT 插入點與對齊點一致

| 圖層 | 內容 |
|---|---|
| `H-STRC` | 結構輪廓 |
| `H-HATC` | 材料 hatch：AR-CONC（混凝土）、EARTH（土壤）、GRAVEL（碎石 / 拋石）、ANSI31（鋼板）；密度以圖紙 mm 固定 |
| `H-HIDN` / `H-WATR` | 背後輪廓 / 水位線（DASHED） |
| `H-PIPE` / `H-RBAR` / `H-CL` / `H-GRND` | 管線與閥件符號 / 鋼筋示意 / 中心線、剖切線 / 地面線 |
| `H-DIMS` | DIMENSION（dimlfac = scale） |
| `H-TEXT` | 引線註記（LINE+TEXT，不用 MLEADER）、▽ 與 EL 文字、詳圖標題（`A-A　圖名`＋雙底線）、`S=1/N` |

每個詳圖的實體收在 DXF GROUP `DETAIL-<id>`，verifier 以此分辨詳圖並檢查 bbox 不重疊、不壓圖簽、在內框內。
圖簽比例欄：多種比例時為「如圖示」。

```bash
TS=$(date +%Y%m%d-%H%M%S)-detail; mkdir -p runs/$TS   # spec 先存到 runs/$TS/spec.json
.venv/bin/python tools/gen_detail.py runs/$TS/spec.json -o runs/$TS/detail.dxf --png runs/$TS/detail.png
.venv/bin/python -m draftagent.verify.detail_check runs/$TS/detail.dxf --spec runs/$TS/spec.json [--json]
```

範例用 `draftagent new drainage-detail` 產生。預覽 PNG 要親自看：hatch 不是一片黑、標註是實尺 mm
（例如內寬 30 cm 的 U 型溝標 300 而非 30）、▽ 文字不壓線。回報只能說「通過本 repo 現有 verifier 的檢查項目」。
