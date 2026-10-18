# DraftAgent 計畫書

> Agent-operable CAD drafting for Taiwan land development, urban renewal, cadastral and hydraulic planning documents.
>
> 建立日期：2026-10-17 ｜ 狀態：Planning → 2026-10-18 Level 1–3 第一輪評估完成（見 §7.1、§10.1）

## 1. 目的

驗證「coding agent（Claude Code / Codex 等）能否產出台灣建築、土地規劃、土木水利計劃書所需的 CAD 圖面」，並找出目前技術的極限位置。

目標產物：水利計劃書、土地開發基地規劃、都市更新計劃書、地政相關作業所需的附圖與計算成果。格式需與市面 CAD 軟體相容（DXF/DWG、IFC、GeoPackage）。

**不求涵蓋所有領域需求，只求測到可用的極限。**

## 2. 背景：為什麼不用 SMLib

2026-10 NVIDIA 以 Apache 2.0 開源 SMLib（NURBS solid modeling kernel，隨 `NVIDIA-Omniverse/usd-brep` 釋出），AWV Informatik 隨後釋出 Python 包裝 `smlibpy`（MIT，含 MCP server）。查證後結論：

| 項目 | 判定 |
|---|---|
| NVIDIA 開源 SMLib、允許商用 | 屬實 |
| 「今天」開源 | 實際是 2026-10-08 釋出 1.0.0 |
| Python 版由 NVIDIA 釋出 | 不精確：`smlibpy` 是第三方 AWV Informatik 製作 |
| 手繪草圖 → CAD 變得可行 | 過度樂觀，kernel 只解決幾何正確性，不解決圖面理解 |

SMLib 是**機械零件取向的 solid kernel**：有 Boolean / fillet / shell / STEP 匯出，但沒有圖層、線型、標註、hatch、block、IFC 語意。建築與土木圖面的核心是**製圖慣例 + GIS 空間分析 + 法規計算**，三者它都沒有。

**定位：smlibpy 列為選用模組**，只在需要精確 3D 實體（橋墩、箱涵、異形量體體積）時啟用。

## 3. 核心設計原則：Agent 不直接畫圖

```
自然語言需求 / 圖資
      │
      ▼
 ① Agent 產生「結構化中間表示」（JSON：基地、量體、房間、水路斷面…）
      │
      ▼
 ② 確定性 generator（Python）：JSON → DXF / IFC / GeoPackage / 報表
      │
      ▼
 ③ Verifier（Python）：法規檢核、幾何有效性、面積對帳、CRS 檢查
      │  fail → 回到 ① 讓 agent 修 JSON
      ▼
   輸出
```

- Agent 只負責 ①；② ③ 是寫死的 Python，agent 可呼叫但不能繞過
- 失敗時可精準區分「agent 理解錯」與「工具不夠」
- 法規檢核走**確定性計算引擎**，不走「讓 LLM 讀法條判斷」（文獻：MCP agent pipeline 在嚴謹性上優於 RAG 推理 pipeline）
- Verifier 用 code 檢查，不用 LLM 判斷（文獻：Self-Verification Text-to-BIM, EC3 2026）

## 4. 工具選型

| 領域 | 核心輸出 | 工具 | 備註 |
|---|---|---|---|
| 建築 | DXF 平面圖、IFC | **ezdxf** + **IfcOpenShell** | ArchiGen、MCP4IFC 已驗證此路線 |
| 土地規劃 | 套疊、面積、分區圖 | **GeoPandas + Shapely + pyproj + rasterio** | 純 Python、headless；不用 QGIS MCP |
| 土木水利 | 縱斷面、坡度、淹水 | **rasterio**（DTM）、**pyswmm**、**ras-commander**（HEC-RAS） | HEC-RAS 本體 Windows only |
| 精確 3D（選用） | STEP、體積 | **smlibpy** | 僅特殊構造物 |
| DXF → DWG | 轉檔 | ODA File Converter | 免費 |

## 5. 台灣圖資來源（全部免費開放）

| 圖資 | 來源 | 格式 / 座標 | 用途 |
|---|---|---|---|
| 20m DTM / DSM | 內政部地政司 | 網格點 N/E/H，TWD97 | 坡度、等高線、集水區、縱斷面 |
| 段籍圖 | 國土測繪中心 | WMTS 3857 / WMS 4326、3826、3857，圖層 `LANDSECT` | 基地定位 |
| 地籍圖（地號層級） | 國土測繪中心（購買） | SHP，欄位：段號、地號、地目（中文欄名） | 基地面積 |
| 通用版電子地圖 | 國土測繪中心 | WMTS `wmts.nlsc.gov.tw/wmts` | 底圖 |
| 村里 / 鄉鎮 / 縣市界 | 國土測繪中心 | SHP，TWD97 | 行政區套疊 |
| 山坡地範圍 | 農業部水保署 | SHP，欄位 ACAD_TEXT / new_area / CountyCode | 開發限制 |
| 淹水潛勢 | 水利署 / 縣市 | 依法僅供防救災，**不得作為土地使用管制依據** | 風險提示；verifier 測試案例 |
| 岩體滑動潛勢 | 地礦中心 | WMS | 地質風險 |

**座標系統陷阱（verifier 必測）**：本島 TWD97 TM2 = EPSG:3826，澎湖 119 分帶 = 3825，舊資料可能是 TWD67。未處理 CRS 直接套疊會偏約 800 m。

## 6. 測試計畫

### Level 1：確定性生成（基本能力）

| # | 任務 | 輸入 | 輸出 | 通過標準 |
|---|---|---|---|---|
| 1-1 | 平面圖 | 「6×4 m 單房，牆厚 20 cm，南面開 90 cm 門」 | DXF | ezdxf 讀回：圖層 ≥ 3（A-WALL / A-DOOR / A-DIMS）、牆線封閉、標註值 = 6000 |
| 1-2 | 基地套疊 | 地號 SHP + 分區 SHP | GeoPackage + 面積表 | 面積誤差 < 0.5%、CRS 統一 3826 |
| 1-3 | 坡度分析 | DTM + 基地範圍 | 坡度分級圖 + 統計 | 水保法坡度分級正確、各級面積加總 = 基地面積 |

### Level 2：法規與多步推理（中階）

| # | 任務 | 考點 |
|---|---|---|
| 2-1 | 建蔽率 / 容積率檢核 | 查分區管制 → 可建面積與樓地板上限 → 量體 DXF。驗：數字對、量體投影 ≤ 可建面積 |
| 2-2 | 山坡地開發可行性 | 基地跨山坡地範圍，分別算面積、套不同規範、出報告。驗：是否誤用淹水潛勢圖當法規依據 |
| 2-3 | 道路縱斷面 | LineString + DTM → 樁號 / 高程 / 坡度表 + ezdxf 縱斷面圖。驗：坡度計算、> 8% 路段標示 |

### Level 3：極限測試（找斷點）

| # | 任務 | 預期卡點 |
|---|---|---|
| 3-1 | 多房間平面 + IFC | 10 間房需走廊連通、每間有窗。預期 6-8 間後空間推理出錯 |
| 3-2 | 手繪草圖照片 → DXF | 預期拓樸對、尺寸錯。**每半年重測一次當 vision model 進步指標** |
| 3-3 | 小型排水設計 | 合理化公式 + pyswmm 模擬。預期參數靠工程判斷，agent 只能給合理範圍 |
| 3-4 | 跨領域串接 | 基地 → 分區 → 量體 → 坡度 → 排水 → 完整報告。預期錯誤累積，verifier 設計是重點 |

**量化指標**：pass rate（verifier 全過）、修正輪數、token 成本。Level 3 pass rate 明顯掉的那一題即為極限位置。

## 7. 可行性評估（2026-10）

| 等級 | 判定 | 理由 |
|---|---|---|
| Level 1、2-1、2-3 | 做得出來 | 本質是寫 Python + 查表計算，可驗證、agent 可自我修正 |
| 3-1、3-3 | 做得出來但品質有上限 | 可當草案產生器，不能當設計師；參數需工程師判斷 |
| 3-2 | 目前做不到可信賴程度 | vision model 拓樸約六七成，尺寸靠人補 |
| 3-4 | 可做但 verifier 工夫 > agent 工夫 | 錯誤累積，需跨步驟一致性檢查 |

真正的瓶頸不在 AI，在：**領域知識編碼**（台灣法規 → skill）、**verifier 設計**、**資料清理**。三者都不依賴模型進步。

### 7.1 2026-10-18 實測

證據：`docs/RESULTS.md`「結論摘要」與 `runs/eval/*/…/grade.json`。題目與原 §6 編號不完全對應（現有考題以
桃園排水為主），以下逐項對照原判定；pass 只代表通過本 repo 現有 verifier 與 expected 比對。

| 原項目 | 原判定 / 預期 | 實測（最新有效結果） | 對照 |
|---|---|---|---|
| Level 1（1-1 平面、1-2 套疊、1-3 坡度） | 做得出來 | Opus / Sonnet / Haiku 各 3/3，build 中位數 1–2 | 符合 |
| Level 2（實際考題：detail / drainage-plan / drainage-calc / outflow / profile） | 做得出來 | 三模型各 5/5；第 1 輪 Opus 2/5 的失敗全是題目把答案 spec 當輸入，修題後全過 | 符合；原 2-1 建蔽率、2-2 山坡地尚未出題（`draftagent/urban/codes/` 沒有 yaml） |
| 3-1 多房間平面 + IFC | 預期 6–8 間後空間推理出錯 | rooms-4 / 7 / 10：Opus 6/6、Sonnet 6/6；Haiku 4、7 間 4/4，10 間 4/5（約 80%，唯一失敗是找不到配置後主動停下，沒有編造） | **比預期好**：到 10 間還沒看到 Opus / Sonnet 的斷點；Haiku 在 10 間開始出現 |
| 3-2 手繪草圖 → DXF | 目前做不到可信賴程度 | 未測 | — |
| 3-3 小型排水設計 | 做得出來但品質有上限 | design-ty-1（從零設計集水分區、管網、滯洪池）三模型全過；build 次數 Sonnet 8、Opus 3–4、Haiku 1；成本最高的一題 | 可產出通過 verifier 的草案；「品質上限」需工程師審圖判斷，verifier 只檢查性質，不評設計優劣。pyswmm 動態演算未納入（見 §10） |
| 3-4 跨領域串接 | 可做但 verifier 工夫 > agent 工夫 | chain-1（地籍 + 分區 + DTM → 套疊 → 整套排水圖）、report-1（整份計畫書）三模型全過 | 符合後半句：要先補跨 build 一致性檢查（`cross_build_checks`）才有意義 |
| 判斷與紀律（新增：twd67-1 髒 CRS、refuse-1 應拒絕、rules-1 不採未收錄法規值 / 淹水潛勢、change-1 差異變更） | §7 未列 | 三模型全過；Opus twd67-1 一次失敗是 grader 只認中文回覆 | 模型會辨識 TWD67、拒絕內插缺值、拒絕未收錄數值 |

**真正的瓶頸觀察（修正 §7 結論的排序）**：

1. **verifier 設計**：稽核前約 95 個竄改情境中約 80 個會綠燈（docs/VERIFIERS.md「稽核修補紀錄」），分 4 路修補後
   才開始評估。verifier 不嚴，pass rate 沒有意義。
2. **題目與評分設計**：15 次非 pass 中 14 次是題目（答案 spec 當輸入、prompt 舊指令 / 答案數值）、harness（權限
   擋絕對路徑與 `cd`）或 grader（reply regex 只認中文、詞表太窄、否定語境、自由文字逐字比對）的問題；只有 1 次
   （Haiku rooms-10）是模型能力。
3. **領域知識編碼**仍是前提：法規 yaml 全部 `verified: false`，綠燈只保證「與 yaml 一致」。
4. **成本**不是瓶頸：單題單次 Haiku 約 USD 0.01–0.14、Sonnet 0.06–1.21、Opus 0.18–2.17。Haiku 在現有題目上
   pass rate 與 Opus 接近、成本約 1/15。

結論：現有題目還沒測到 Opus / Sonnet 的極限位置；要找斷點需要更難的題目（§10 Level 4）與真實圖資。

## 8. 競爭格局

| | 法規推理 | 台灣資料 | coding agent | 開源 |
|---|---|---|---|---|
| TestFit / Hypar / Forma / Archistar | ✅ | ❌ | ❌ | ❌ |
| Text2BIM、TUM ACC 等學術 | 部分 | ❌ | ✅ | ✅ |
| harnessbim / ArchiGen / ifc-coding-agent | 部分 | ❌ | ✅ | ✅ |
| 台灣都更顧問業 | ✅（人工） | ✅ | ❌ | ❌ |
| 新北 / 台北地政 AI 櫃台 | ❌ | ✅ | ❌ | ❌ |
| **DraftAgent** | ✅ | ✅ | ✅ | ✅ |

查無台灣團隊公開做「LLM agent + 容積檢討 + 地籍圖資」。技術路線國際上已被驗證多次，台灣法規與圖資是在地優勢，國際工具短期不會為台灣客製。

## 9. Repo 結構（規劃）

```
draftagent/
├── CLAUDE.md / AGENTS.md     # 專案規則：CRS 3826、單位 mm、圖層命名
├── .claude/skills/
│   ├── tw-gis/               # 台灣圖資：下載、欄位、座標陷阱
│   ├── dxf-arch/             # ezdxf 建築製圖慣例
│   ├── ifc-building/         # IfcOpenShell API + 常見錯誤
│   └── tw-building-code/     # 建蔽率容積率表、山坡地規定
├── draftagent/
│   ├── site/                 # 基地：地籍套疊、分區、面積
│   ├── hydro/                # 水利：集水區、縱斷面、斷面圖
│   ├── urban/                # 都更：量體、容積、建築線
│   ├── render/               # ezdxf / IfcOpenShell 輸出
│   └── verify/               # 所有 verifier
├── tools/                    # CLI 入口（gen_dxf, gen_ifc, gis_overlay…）
├── tests/                    # Level 1-3 fixtures 與 expected
└── docs/
```

走 **skills + Bash 執行 Python**，不包成 MCP：工具本來就是 Python 套件，少一層；skills 可放領域知識；Codex 吃 `AGENTS.md`，同一結構兩邊可跑。需要即時畫面的 demo 再接 Bonsai-mcp 或 FreeCAD MCP。

## 10. Roadmap

| 週次 | 工作 | 產出 |
|---|---|---|
| W1 | 下載一幅 DTM、一個段的地籍 SHP、該區使用分區圖；寫 `verify/crs_check.py`、`verify/area_check.py` | 只用 GeoPandas，先測 agent 對 CRS 的理解 |
| W2 | Level 1 三題全部跑通 | pass rate 基線 |
| W3 | Level 2-1 建蔽率檢核 | 最有在地價值的 demo |
| W4+ | Level 3 挑一題往下挖 | 找到斷點 = 測試結論 |
| 每半年 | 重測 3-2 | vision model 進步指標 |

### 10.1 完成狀態與下一步（2026-10-18）

| 原週次 | 狀態 | 備註 |
|---|---|---|
| W1 圖資 + crs / area verifier | ✅ 完成（合成圖資） | 尚未換成真實下載的 DTM / 地籍 / 分區 |
| W2 Level 1 跑通 | ✅ 完成 | 三模型 3/3 |
| W3 Level 2-1 建蔽率 | ⏳ 未做 | 改先做桃園排水 Level 2（5 題，三模型全過）；建蔽率 / 容積率 yaml 尚未建立 |
| W4+ Level 3 往下挖 | ✅ 第一輪完成 | 10 題、三模型；Opus / Sonnet 未見斷點，Haiku rooms-10 約 80% |
| 每半年 3-2 | ⏳ 未開始 | |
| （新增）verifier false-negative 稽核 | ✅ 完成 | ~80/95 誤判綠燈 → 修補；仍缺項目見 VERIFIERS.md |
| （新增）評估 harness + 可解性測試 | ✅ 完成 | `tools/eval.py`、`reference_solvers.py`、`test_task_solvable.py` |

下一步：

1. **Level 4 極限題**：Level 3 沒有找到 Opus / Sonnet 的斷點，需要更難的題目（更多空間 / 更複雜拓樸、多次變更累積、
   更長的跨領域串接、資料互相矛盾），目標是找到 pass rate 明顯下降的位置。
2. **真實圖資**：目前考題全為合成資料；改用真實下載的 DTM、段籍、使用分區（去識別化，不寫入真實案名 / 地號）。
3. **法規 yaml 人工核對**：所有 `codes/*.yaml` 仍是 `verified: false`，需人工逐條對照原文後改 `verified: true`；
   補 `urban/codes/` 建蔽率 / 容積率後才能出原 2-1。
4. **SWMM 本機簽章問題**：本機 SWMM 子行程可能因執行檔簽章無法執行（相關測試 skip），目前只比對 `.inp`；
   解決後才能把 pyswmm 演算結果納入 outflow / 3-3 的 verifier。
5. 每題 `--repeat ≥ 3` 補足樣本數（多數格子目前只有 1–2 次）。

## 11. 參考

- NVIDIA usd-brep / SMLib：github.com/NVIDIA-Omniverse/usd-brep
- smlibpy：github.com/awv-informatik/smlibpy
- Text2BIM (TUM, 2024)：arxiv.org/abs/2408.08054
- Self-Verification Text-to-BIM (EC3 2026)
- Automatic Building Code Review: A Case Study：arxiv.org/abs/2510.02634
- harnessbim：github.com/ReverseZoom2151/harnessbim
- ArchiGen：github.com/Dropio12/archigen
- ifc-coding-agent：github.com/rrdls/ifc-coding-agent
- ras-commander：ras-commander.readthedocs.io
- 政府資料開放平臺（DTM、段籍圖、山坡地範圍）：data.gov.tw
