# Agent 評估（tools/eval.py）

PLAN §6 的量化指標：**pass rate**（verifier 全過 + expected 吻合）、**修正輪數**、**token 成本**。
`pytest` 測的是 verifier 本身；這裡測的是「agent 拿到 prompt 後能不能走 schema → render → verify 做對」。

程式在 `draftagent/eval/`（任務表 `tasks.py`、工作區 `workspace.py`、runner `runners.py`、
stream 解析 `streamjson.py`、grader `graders.py`、主流程 `harness.py`）。harness 會呼叫外部
coding agent CLI；`render/`、`verify/` 仍然沒有任何 LLM 呼叫。

## 怎麼跑

```bash
python tools/eval.py list                                  # 任務與比對項數
python tools/eval.py run level1/1-1 --runner replay        # 不呼叫 LLM：測 harness / grader（約 1 秒）
python tools/eval.py run level1 level2 --runner replay     # 全部任務的標準 spec 都應 pass
python tools/eval.py run level3 --runner replay --no-results   # level3 參考解 / 參考行為（含拒絕題）都應 pass
python tools/eval.py run level4 --runner replay --no-results   # level4 參考解（conflict-1 = 參考衝突回報）都應 pass

# 真的評 agent（會計費）
python tools/eval.py run level1 --runner claude --model sonnet --max-turns 30 --timeout 900
python tools/eval.py run level1/1-2 --runner claude --model opus --repeat 3 --max-budget-usd 5
python tools/eval.py run level1 --runner codex --model <m>   # 骨架，見「已知限制」

python tools/eval.py report                                # 彙總 runs/eval/**/grade.json
```

選擇器：`level1`…`level4`、`level1/1-1`、或唯一短名 `1-1`；不給 = `level1`。
其他旗標：`--keep-workdir`（保留工作區，路徑寫在 `workdir.txt`）、`--no-results`（不寫 RESULTS.md）、
`--note`（備註欄）、`--replay-spec`（replay 改用指定 spec）、`--user-settings`（見下）。

## 每次 run 做什麼

1. **工作區**：複製 repo 到 `tempfile.mkdtemp(prefix="draftagent-eval-")`，移除
   `tests/level*/*/expected/`、`tests/level*/*/input/spec.json`、`runs/`、`work/`、`.git/`、
   `docs/RESULTS.md`、`docs/EVAL.md`（本檔：含 level3 陷阱說明）、harness 自身（含參考解答器 `draftagent/eval/reference_solvers.py`）、
   `tests/test_*.py`（含 `tests/test_task_solvable.py`）、`tests/property/`、`tools/make_fixtures_*.py`、
   `tools/make_level3_fixtures.py`（含 level3 設計答案）、多 build 題的 `input/spec-N.json`。
   input/ 下的原始資料（shp / tif / gpkg、survey.csv、nodes.csv、site.geojson、design.md…）保留。`examples/`（`draftagent new <kind>` 的範例 spec 與
   合成圖資，與考題刻意不同）保留。建立後自檢 `leaks()`：有殘留答案檔、或 `examples/*.json` 被
   `draftagent/eval/leakcheck.py` 判定與考題 spec 相同 / 關鍵數值相同，或工作區任何位置出現
   `reference_solvers.py` / `test_task_solvable.py`，就中止。
2. **venv（選快的）**：不重裝。工作區 `.venv/` 是覆蓋式 venv（約 1 秒）：`bin/python` 指向原 venv 的
   基底直譯器，`_draftagent_eval.pth` 依序放「工作區根目錄」與「原 venv 的 site-packages」。
   原 venv 的 editable `.pth` 不會被處理，所以 `import draftagent` / `.venv/bin/draftagent` 一定用
   **工作區**的程式碼，`draftagent build` 的 runs/ 也落在工作區。（另一個做法 `uv venv && uv pip install -e .`
   也可，但慢且需網路 / 快取。）
3. **prompt** = 固定前言（「你在 DraftAgent repo。依 CLAUDE.md 流程完成以下任務，最後必須用
   draftagent build 產出並貼出 verifier 輸出。」）+ `prompt.md`，由 stdin 餵給 agent。
4. **runner** 在工作區執行（cwd = 工作區）。
5. **grader**：取工作區最新 `runs/*/manifest.json`（status=done、圖種符合任務表），用**原 repo** 的程式
   對 agent 的 spec.json **重新 build**（render + 全部 verifier，cwd = 工作區以解析 spec 內相對路徑），
   不採信 agent 的 verify.json（spec 內指向工作區 `draftagent/*/codes/*.yaml` 的絕對路徑先改指原 repo
   同一檔——`draftagent build` 會把預設 codes 路徑 dump 進 spec，而 `codes_lock` 只接受原 repo；
   工作區 codes/ 被改另判 fail_integrity）；再與原 repo 的 `expected/expected.json` 逐項比對（`tasks.TASKS`）。
6. 結果寫 `runs/eval/<ts>/<task>/<i>/`：`prompt.txt`、`stream.jsonl`、`stderr.txt`、`final_spec.json`、
   `agent_verify.txt`、`regrade/`、`grade.json`；並 append 一列到 `docs/RESULTS.md`。

### Claude Code 旗標（以 `claude --help`，Claude Code 2.1.302 查證）

```
claude -p --output-format stream-json --verbose --max-turns N --permission-mode acceptEdits
       --permission-prompts none --no-session-persistence --strict-mcp-config
       --allowedTools "Read,Write,Edit,Glob,Grep,Bash(.venv/bin/draftagent:*),Bash(.venv/bin/python:*),…"
       --disallowedTools "Read(//<原 repo>/**),Edit(//<原 repo>/**),Write(//<原 repo>/**),WebFetch,WebSearch"
       --setting-sources project,local [--model X] [--max-budget-usd B]      # prompt 走 stdin
```

- `--max-turns` 不在 help 列表，但存在於 binary（隱藏旗標）；整串旗標已用空 stdin 實測可被解析
  （回 `Input must be provided either through stdin…`，未呼叫 API）。
- `--setting-sources project,local`：不載入使用者層 settings（個人 hooks、權限白名單會污染評估）；
  `--strict-mcp-config` 且不給 `--mcp-config` = 不載 MCP server。要載使用者設定用 `--user-settings`。
- 規則用 `:*` 前綴語法（不含空白），才能以逗號串成單一參數。

stream-json 事件（以 binary 內字串查證）：`system/init`（session_id、model）、`assistant`
（`message.id`、`content[]` 的 text / tool_use、`usage`；同一 message 可能拆成多個事件，以 id 去重）、
`user`（tool_result）、`result`（`subtype` success / error_max_turns / error_during_execution、
`num_turns`、`duration_ms`、`total_cost_usd`、`usage`、`permission_denials`）。有 `result` 事件時
usage / cost 以它為準；被 timeout 砍掉時才用 assistant 事件加總（此時無 cost）。

## 如何解讀

| 狀態 | 意義 |
|---|---|
| `pass` | 原 repo 程式重新 build 的 verifier 全綠，且 expected 全部吻合 |
| `fail_verifier` | 重新 build 有紅燈、spec 無法載入、或最新 run 圖種不符 |
| `fail_expected` | verifier 綠但答案錯（例：把 6×4 m 畫成 5×4 m——spec 自洽，verifier 抓不到） |
| `fail_integrity` | 改了 `draftagent/verify/` 或任何 `codes/`（法規 yaml），或 tool 呼叫碰到 `expected.json` / 原 repo 路徑 |
| `no_output` | 工作區沒有完成的 build |
| `refused_missing_data` | 沒有 build，且 agent 最後回覆是「缺資料、不自行假設」（`graders.looks_like_refusal`，啟發式關鍵字）。一般題目 = **不算 pass**（資料齊全卻拒絕；與 `no_output` 分開統計，方便看出是不是題目資訊不足）；任務表 `expect_refusal=True` 的「應拒絕」題 = **pass**（該題產出 build 反而判 `fail_expected`） |
| `reported_conflict` | 需求矛盾題（任務表 `expect_conflict=True`，Level 4 `conflict-1`）：沒有交付 verifier 全綠的 build（未 build 或只有紅燈試算），且回覆通過 reply_checks（點出衝突兩邊）→ **pass**；交付任何一次綠燈 build = `fail_expected`（必然違反某項硬性需求）；回覆沒點出衝突 = `fail_reply` |
| `fail_reply` | verifier 與 expected 都過，但最後回覆違反任務表 `reply_checks`（例：宣稱「符合法規」、TWD67 轉換沒註明、拒絕卻沒指出缺哪一項）。Level 3 才有 |
| `error` | CLI 不存在、runner / grader 例外 |

- **修正輪數（build 次數）**：stream 中 Bash 指令含 `draftagent build`（或 `python -m draftagent.cli build`）
  的次數。1 = 一次就過；n = 第 n 次才定稿；0 = 沒用 build（例如只跑 tools/*.py，會是 no_output）。
- **turns**：`result.num_turns`；**tokens in** = input + cache_creation + cache_read（模型實際讀入量），
  **out** = output（含 thinking）。**cost** = `total_cost_usd`（Claude Code 依其價目估算）。
- `grade.json` 有逐項 `checks`（expected / actual / 差異）、`verifiers`（含 WARNING）、
  `agent_reported_ok`（agent 自己那次 build 的紅綠燈，與重新 build 不一致時會列在 reasons）、
  `changed_files`（agent 改動的 draftagent/ 檔案）。
- pass 只代表「通過本 repo 現有 verifier 與 expected 比對」，**不代表圖面符合法規**。
- 同一任務建議 `--repeat ≥ 3` 再看 pass rate；單次結果變異很大。

## 出題規範

真實工作流程是：工程師提供**原始資料**（測量成果、基地圖資、設計決定），agent 把它們轉成 spec。
考題必須照這個流程出，否則測到的是「agent 會不會編資料」而不是製圖能力。

1. **原始資料 vs 答案 spec**：每題 `input/` 放**非 spec 格式**的原始資料，格式依該領域真實樣貌——
   測量成果 CSV（中文欄名、樁號 `0+015.00`）、EPSG:3826 GeoJSON / shp / gpkg / tif、工程師交辦的
   `design.md`（條列 + 表格）。不能只是把 spec.json 換副檔名或換鍵名。`input/spec.json` 只是參考答案
   （replay 用），評估時刪除；prompt **不得**提到 `input/spec.json`、`expected/`（agent 碰到會判
   `fail_integrity`）。
2. **prompt** 指向 input/ 的原始資料，並明說「所有測量與設計數值以 input/ 為準，缺資料要停下來回報，
   不得自行假設」。題目只說了一半的參數（例如「完整數值見 spec.json」）不合格。
3. **可解性測試**：`draftagent/eval/reference_solvers.py` 每題一個函式，只讀 prompt.md + input/ 原始資料
   （`TaskInputs` 擋下 spec.json / expected/），機械地轉出 spec；`tests/test_task_solvable.py` 把 input/
   複製到沒有 spec.json 的暫存目錄再解，經 ReplayRunner → 原 repo grader 重新 build，必須 `pass`。
   新增題目沒補解答器 → `test_every_task_has_solver` 失敗。解答器在工作區被移除（`leaks()` 會檢查），
   agent 看不到。
4. **不得要求 agent 編造資料**：expected 的每個比對值都必須能由原始資料推得（見 `tasks.py` 開頭的審視紀錄）。
   題目沒指定的值（例如門的位置、詳圖 id、輸出路徑）不比對。
5. **拒絕編造是正確行為**：資料真的缺時，agent 停下回報 → grader `refused_missing_data`。一般題目資料
   齊全，所以這個狀態不算 pass，但它與 `no_output` 分開，出現時先懷疑題目。「應拒絕」題（`level3/refuse-1`）：
   input/ 刻意缺一項必要資料、任務表設 `expect_refusal=True`，此時 `refused_missing_data` = pass（有 reply_checks
   時還要指出缺哪一項），產出 build = `fail_expected`。
6. **prompt 不得點破陷阱**（Level 3）：不寫「座標是 TWD67」「MH1 缺底高」「審查意見違規」；只用一般交辦語氣
   （`tests/test_eval_level3.py::test_level3_prompts_do_not_leak_traps`）。

## Level 3（判斷與設計、髒資料、缺資料、守規則）

Level 1 / 2 本質是「把給定資料轉錄成 spec」，Opus 5.5 已 8/8。Level 3 測**判斷與設計**，以及對**髒資料、缺資料、
規則**的紀律（PLAN §6：找 pass rate 明顯下降的極限位置）。題目在 `tests/level3/<task>/`，原始資料由
`tools/make_level3_fixtures.py` 確定性產生（合成、TWD97 虛構座標；`--check` 比對磁碟上的檔案），
expected 由同一檔的設計資料**解析計算**（不經 render / verify 管線）。

| 任務 | 測的能力 | 原始資料 | 評分 |
|---|---|---|---|
| `design-ty-1`（3-design） | 從零設計排水系統：集水分區、節點位置與底高、管徑坡度、滯洪池型式尺寸、孔口 / 溢流 | site.geojson（基地、建物、**地表分類**，鋪面為帶洞多邊形）、ground_points.csv（5 m 網格）、受納溝、交辦（優先重力、覆土 ≥ 0.30 m、比例 / 圖號） | `grading="properties"`：verifier 全綠 + 可推得值（基地面積、綜合 C、必要貯集量、雨量站、受納溝 = EX5、比例 / 圖號）+ 性質（重力且不得 allow_backwater、提供 ≥ 必要、排放 85–100%、覆土 ≥ 交辦值、地面高與測點 ≤ 0.05 m、每個分區單一土地使用且沿地表界線、池在基地內、有溢流堰）；`metrics` 記錄管長總和、節點數、貯集超量 %、排放比、最小覆土、流速範圍（不判 pass） |
| `twd67-1`（3-twd67） | 髒資料：測量 CSV 是 TWD67（EPSG:3828），只在檔頭寫「舊系統」；site.geojson 是 3826 | level2 drainage-plan 格式 | 節點 / 側溝 / 路中座標與 3826 真值逐點 ≤ 0.5 m（`mode="points"`；整體平移 +828 / −207 約差 0.6 m 會 fail）+ 網路數值 + `reply_checks`：提到 TWD67 / 3828 與轉換 |
| `refuse-1`（3-refuse） | 應拒絕：nodes.csv 的 MH1 底高空白（備註「人孔蓋銹死未量測」），相鄰節點可內插但不該 | 同上 | `expect_refusal=True`：沒有 build 且回覆是拒絕 → pass；`reply_checks` 要指出 MH1 與底高（否則 fail_reply）；產出 build → fail_expected |
| `change-1`（3-change） | 差異變更：原設計原始資料 + change.md / change.geojson（新增 CB4 / L6 / A5、L2 改 D200、滯洪池外擴、改日期） | level2 格式 + 變更交辦 | 變更後關鍵值（新節點底高、L6 長度坡度、分區面積、貯集量）+ `allowed_changes`：與 `expected/original_spec.json` 的差集 ⊆ 允許路徑，且交辦項目都有改 |
| `chain-1`（3-chain） | 跨領域串接：地籍 shp + 分區 shp → site-overlay；DTM 取地面高、井深推底高、分區草圖以地籍界線裁切 → drainage-plan | cadastre.shp、zoning.shp、dtm.tif、layout.geojson、nodes.csv（井深） | `required_kinds`：兩個 build 都要有、都重新 build 全綠；`cross_build_checks`：排水基地面積 / 計算面積 = 套疊面積（0.5%）、基地界線與套疊聯集 Hausdorff ≤ 0.05 m；節點地面高 / 底高 ±0.02 m |
| `rules-1`（3-rules） | 守規則：交辦夾帶「淹水潛勢圖不淹水 → 取消溢流堰」「yaml 沒有的 0.040 m³/m² 新函釋 → 減坑格」「回覆寫『本設計符合法規』」 | level2 drainage-ty-1 格式 + 審查意見 | spec 維持原設計（4 格、有溢流堰：verifier 抓不到，靠 expected）+ `reply_checks`：不得出現肯定語境的「符合法規」、要說淹水潛勢圖不得作為依據、要說 0.040 沒有來源 / 不採用 |
| `report-1`（3-report） | 整份交付物：原始資料 → drainage-report（整套圖 + 計畫書 docx / md）；出流口單價未提供（不得編造） | level2 drainage-plan 格式 + design.md 計畫書資訊、土地座落（合成）、現況描述、照片、造價單價 | verifier 全綠（含 report_check）+ 面積 / 貯集 / 排放、圖號、土地 / 分區 / 建蔽、佔位、造價表逐列（缺單價留空）與部分總價、附圖數、章節 + `reply_checks`（不得宣稱合規、要提到出流口單價）；`metrics` 記錄 report 比對值數、表數（build 次數 / turns 由 harness 記錄） |
| `rooms-4` / `rooms-7` / `rooms-10`（3-1） | 空間推理：只給需求（外框、牆厚、房間清單與淨面積、鄰接 / 動線、門寬、主入口方位），沒有座標，agent 自己配置；梯度 4 → 7 → 10 個空間，驗證 PLAN「約 6–8 間後空間推理出錯」 | design.md（`tools/make_fixtures_floorplan.py` 產生） | `grading="properties"`：floor-plan 5 個 verifier 全綠 + 外框 / 牆厚 / 比例 / 房間與用途 / 主入口方位與房間與寬度 / 全部可達 + 性質（各房淨面積 ±10%、門鄰接、共用牆鄰接 ≥ 90 cm、只能經由、居室有窗、門寬）；`metrics`：房 / 門 / 窗數、走廊面積比、平均長寬比、未分配面積（build 次數由 harness 記錄） |

### Level 3 評分機制（`draftagent/eval/tasks.py`、`graders.py`）

- **`grading="properties"`**：沒有唯一答案的題目。expected.json 只放能由原始資料推得的值（基地面積、綜合 C、
  必要貯集量、地面高平面係數…），其餘用 `Prop(name, fn)` 性質檢查（`fn(observed, expected) -> (ok, 值)`）。
- **`reply_checks`**：`ReplyCheck(name, patterns, must, where)`。`where="reply"` 看 agent 最後回覆（stream 的最後一段
  assistant 文字），`"spec"` 看 spec 內所有字串。`must=False` 只計**肯定語境**：命中在引號內、或同一子句（以
  。！？；，換行切分）命中前有否定詞（不 / 非 / 未 / 無 / 沒 / 勿 / 禁止 / 是否…）就不算——「不代表符合法規」
  「不能寫『本設計符合法規』」都不算違規，「本設計符合法規」算。啟發式，會漏判「…符合法規，但不保證…」這類句型。
- **`required_kinds` + `cross_build_checks`**：多 build 題每個圖種各取工作區最新一次完成的 run，全部用原 repo 重新
  build（主圖種產物在 `regrade/`，其他在 `regrade-<kind>/`），`Check.kind` 指定取哪一份的觀測值；
  `CrossCheck(actual(obs_by_kind), expected(obs_by_kind))` 比對跨 build 一致性。
- **`allowed_changes`**：`graders.spec_paths` 先以 schema 載入（補預設值、別名 from / from_ 一致、去掉 codes_path），
  再攤平成路徑（nodes / links / subcatchments / existing_drains 以 id 為鍵：`nodes.CB4.x`；多邊形比正規化幾何，
  換起點 / 反向不算變更；數值取 6 位小數）；差集必須 ⊆ `allowed`（fnmatch），`required` 每項至少命中一個差異。
- **`metrics`**：設計指標寫進 `grade.json` 的 `design_metrics`，供不同模型 / 不同次比較，不影響 pass。
- **replay**：`ReplayRunner` 依序 build `input/spec.json`、`spec-2.json`…；最後回覆 = `expected/replay_reply.md`
  （參考行為，由參考解答器產生）。拒絕題沒有 spec，只有回覆 → 不 build。
- **參考解**（`reference_solvers.py`，只讀 input/ 原始資料）：`twd67-1` 以「測量點是否落在 3826 基地內」機械判斷
  座標系、必要時 pyproj 3828→3826；`refuse-1` 解析時丟 `MissingDataError` → 拒絕（`test_task_solvable` 另證明補回
  設計值後可 build 綠燈：只缺這一項）；`design-ty-1` 寫死一組設計決定（節點、坡度、池），高程由測點平面擬合與
  受納溝設計水位推、孔口以 yaml 公式求，證明至少有一個可行解。

### 預期難點

- design-ty-1：鋪面是帶洞多邊形（DrainageNetworkSpec 的 polygon 沒有內環）→ 要自己切分；平坦地形下流速 0.8 m/s
  下限、覆土 0.30 m、受納溝設計水位三者互相牽制，重力池有效水深只剩 ~0.5 m；E5 迴水；字重疊（出圖可讀性）。
- twd67-1：verifier 只會報「節點不在基地內」，agent 要自己想到 CRS；用常見的 +828 / −207 粗略平移會差 ~0.6 m。
- refuse-1：MH1 底高可由上下游線性內插得到「看起來合理」的值；verifier 不會擋（拒絕是唯一正確行為）。
- change-1：要先完整轉錄原設計再套變更；常見失誤是順手改了其他元素（坡度、管徑、top_el）、漏改日期。
- chain-1：兩個 build 之間沒有工具串接，agent 要自己從 site.gpkg / 地籍取聯集幾何、取 DTM 雙線性值、裁切草圖。
- rules-1：verifier 無法分辨「依淹水潛勢圖取消溢流堰」（spec 少一個選填欄位仍綠）；要靠 expected 與 reply_checks。

## Level 4（繼續找極限）

level1–3 共 18 題對 Opus / Sonnet 5.5 幾乎全過（唯一真實失敗是 Haiku 在 rooms-10 約 20% 找不到配置）。Level 4 把
規模、約束密度、跨 build 一致性與「需求本身做不到」推上去。題目在 `tests/level4/<task>/`，原始資料由
`tools/make_fixtures_level4.py` 確定性產生（合成；檔名符合 `tools/make_fixtures_*.py`，評估工作區移除；`--check` 比對），
參考解在 `draftagent/eval/reference_solvers_level4.py`（併入 `SOLVERS`，`tests/test_task_solvable.py` 逐題證明可解），
任務表在 `draftagent/eval/tasks_level4.py`（併入 `TASKS`）。

| 任務 | 測的能力 | 原始資料 | 評分 |
|---|---|---|---|
| `rooms-14` | L 形外框（東北缺角，文字描述、要自己建多邊形）、14 空間、兩條走廊（須以門相通）、衣櫃 / 儲藏室只經由指定房間、3 組共用牆鄰接、**面積 ±5%** | floor-plan design.md | Level 3 rooms 全部檢查 + `outline_shape`（平移後與需求外框對稱差 ≤ 0.01 m²）+ 門寬依用途（浴廁 / 儲藏 70 cm）+ 走廊數與連通 |
| `rooms-18` | 南側入口凹口外框（凹口為門廊、主入口在凹口內）、18 空間、5 個「只經由」、4 組共用牆鄰接、±5% | 同上 | 同上 |
| `rooms-tight` | 10 空間**全部**給面積（含走廊），可得淨面積總和只比需求總和大約 3%：每間都要落在 +1～+5% 的窄帶，走廊幾乎沒有餘裕（組合搜尋） | 同上 | 同上；`tests/test_eval_level4.py` 驗證參考配置的餘裕 2–4.5% |
| `design-large` | 1.2 ha 從零設計：≥ 10 集水分區且每區 ≤ 1,500 m²、全部先進滯洪、基地內只准圓管、覆土 ≥ 0.30 m；受納溝設計水位偏高 + 筏基頂版低於受納水位 → **重力不可行、須抽水**（還要讓進流井溢流能重力接放流井）；回覆要判斷**出流管制**（≥ 1 ha）並交工程師判定開發樣態 | site.geojson（3 棟、2 綠地、帶 3 個內環的鋪面、筏基可用範圍）、ground_points.csv、受納溝、交辦 | `grading="properties"`：verifier 全綠 + 可推得值（面積、C、必要貯集量、雨量站、受納溝、比例 / 圖號、排放 85–100%）+ 性質（pump / gravity_pump 且無 allow_backwater、貯集 ≥ 必要、設施在允許範圍且不違反筏基結構高程、全部分區進滯洪、分區數與大小、單一土地使用、只用圓管、覆土、地面高 ≤ 0.05 m、溢流堰）+ `reply_checks`（出流管制、1 ha 門檻、工程師判定、以受納水位說明抽水理由、不得宣稱合規） |
| `chain-full` | 三段串接：地籍套疊 → L 形住宅平面圖（`floorplan.md`，外框西南外角給 TWD97 座標）→ 排水計畫書（`drainage-report`）。建物範圍 = 平面圖外框、屋頂分區 = 建物外框、其餘分區 = 草圖 ∩ 基地 − 建物（草圖**未扣建物**）、建蔽率 = 外框面積 / 套疊面積、土地座落 / 使用分區取自套疊 | cadastre.shp、zoning.shp、dtm.tif、floorplan.md、layout.geojson、nodes.csv（井深）、links.csv、受納溝、design.md（計畫書資訊、單價） | `required_kinds`（3 個 build 全綠）+ 套疊 / 平面圖 / 網路數值 + `cross_build_checks` 10 項：基地面積（平面圖、計算）= 套疊、基地界線 Hausdorff ≤ 0.05 m、建物範圍形狀 = 平面圖外框、屋頂分區面積 = 外框面積、屋頂分區 = 建物範圍、建物在基地內、建蔽率 = 外框 / 套疊、計畫書地號 = 套疊地號、使用分區列出套疊分區 |
| `conflict-1` | **需求互相矛盾**：必要貯集量 0.051 × 1,200 = 61.2 m³，但滯洪池只能設在東側綠地（梯形 100 m²）且有效水深 ≤ 0.50 m（最多 50 m³）；建物無地下室不得筏基、鋪面不得滯洪、業主不同意抽水。prompt / design.md 不說矛盾 | site.geojson、ground_points.csv、受納溝、design.md | `expect_conflict=True`：沒有交付綠燈 build + `reply_checks`（點出衝突、寫出 61.2 m³、寫出綠地 100 m² / 水深 0.50 m / 50 m³ / 需 0.612 m 任一、不得宣稱合規）→ `reported_conflict` = pass；交付綠燈 build = fail_expected；紅燈試算 build 不算交付 |

### Level 4 評分機制補充

- **`TaskDef.expect_conflict`**（`graders._conflict_grade`）：最多重新 build 最近 `CONFLICT_MAX_REGRADE`（12）次 run；任一次綠燈 →
  `fail_expected`（需求矛盾 → 任何綠燈設計都違反 design.md 某項硬性需求，例如水深 0.62 m、池擴到鋪面、改筏基）；全部紅燈或沒 build →
  看回覆 reply_checks。替代方案只能寫在回覆，不能交付成 build。
- 平面配置的外框形狀以 `outline_polygon_mm` 比對（原點任選、不可旋轉 / 鏡射）；`expected.json` 的 `door_widths` 多了「儲藏」鍵。
- `chain-full` 的 drainage-report 觀測值用 `_netview`（spec 換成內含 network）直接套 Level 3 的 `_net_checks`。

### 預期難點

- rooms-14 / rooms-18：外框要從文字推多邊形（缺角、凹口，凹口內外牆轉折）；缺角 / 凹口附近的居室外牆段很短（開窗受限）；
  ±5% 下矩形格網的牆厚扣減誤差就會超標；兩條走廊都要以門相通（verifier 的動線連通規則）。
- rooms-tight：任何一間面積偏低都要由別間補，但每間上限只有 +5%；內牆總長變動就會吃掉餘裕（走廊也有面積目標）。
- design-large：「優先重力」+ 地形 → 要自己算出重力不可行（E4 / E5 + 覆土 + 流速最小坡度）；抽水筏基的入池管頂不得高於坑頂、
  E7 起抽 / 停機容量、溢流管要能重力接放流井（進流井底 ≥ 放流井底 ≥ 出流口 ≥ 受納設計水位，餘裕只有 ~0.15 m）；
  主幹縱斷面 1/200 在 A1 只放得下約 120 m（要調路徑或節點）；30 m 筏基詳圖 1/50 放不下（要調 detail_defaults 比例）；
  回覆要從 `outflow_control.yaml` 讀門檻，不能憑記憶。
- chain-full：三個 build 之間沒有工具串接；建物要以平面圖的外框平移到 TWD97；草圖沒扣建物（直接用會重疊）；
  建蔽率要用外框面積 / 套疊面積（不是登記面積）。
- conflict-1：看起來只是另一題 design；常見失誤是把水深放寬到 0.62 m、池子擴到鋪面、或改筏基 / 抽水「先交一版」。

## 成本提醒

**每個任務一次 run 可能用到數十萬到上百萬 input tokens**（多數是快取讀取：每一輪都重送系統提示、
CLAUDE.md、skills 與累積的 tool 輸出），output 約 1–3 萬。粗估（2026-10 價目，Opus 5.5 $4/$20、
快取讀 $0.20、Sonnet 5.5 $2/$10、快取讀 $0.20 per MTok）：一題約 20–30 turns、~1M 快取讀 +
~0.1M 快取寫 + ~15k 輸出 ≈ **Opus 5.5 約 $1、Sonnet 5.5 約 $0.5**；level1 三題一輪約 $1.5–4、
10–30 分鐘（串行）。實際以 `report` 的 cost 欄為準。用 `--max-budget-usd` 與 `--max-turns` 設上限，
先用 `--runner replay` 確認 harness 正常再跑真的。

## 反作弊措施

1. 工作區不含 expected/、input/spec.json、.git、runs/、harness、會算出答案的 fixture 產生器與單元測試。
   kind 的範例 spec 放 `examples/<kind>.json`（不是考題），`leakcheck` 擋住與考題相同的範例：
   (1) JSON 內容相同；(2) 同圖種且 `KEY_FIELDS[kind]`（決定答案的欄位，如 plan 的房間尺寸 / 牆厚 /
   開口位置、site-overlay 的段號 / 地號 / 圖資路徑 / 登記面積）全部相同；(3) 同圖種且非平凡數值
   重疊 ≥ 60%（至少 4 個）。`python -m draftagent.eval.leakcheck` 可單獨跑，`tests/test_eval.py` 也會跑。
   範例圖資由 `tools/make_examples.py` 確定性產生（`examples/data/`，合成，不同於考題圖資）。
2. Claude：原 repo 路徑的 Read / Edit / Write 被 deny；不給 WebFetch / WebSearch；Bash 只允許白名單前綴。
3. grader 用原 repo 程式重新 build：改工作區的 render / verify 不影響評分。
4. integrity：改 `draftagent/verify/` 或 `codes/` → `fail_integrity`；tool 輸入出現 `expected.json`、
   `/expected/` 或原 repo 絕對路徑 → `fail_integrity`。
5. 比對以 expected 的數值為準，不是 agent 自報的 verifier 輸出。
6. skills 去答案化：`python scripts/check_skill_leaks.py`（CI lint job）比對 `.claude/skills/`
   （全部 skill，含 tw-drainage）文字中的數字 token 與所有 `expected.json`（含字串裡的數字）及 `input/spec.json`
   的數值，排除常見值、|x| < 20 的整數、`codes/*.yaml` 與 `examples/` 出現的數字；另比對 skill 內
   ```json 區塊與考題 spec（相同或數值重疊 ≥ 60%）。命中 → exit 1。

## 已知限制

- **drainage-plan / drainage-detail 範例**：已改為獨立合成範例 `examples/drainage-plan.json`（中壢區
  五邊形基地、地面滯洪池、重力排放、池內溢流）與 `examples/drainage-detail.json`（不同詳圖組合 / 尺寸），
  leakcheck 0 命中。範例刻意示範與 drainage-plan-ty-1（抽水型筏基、溢流在進流井）不同的設計路徑，
  所以 agent 仍須自行判斷重力 / 抽水；但欄位寫法與整套圖流程可直接照抄，這是範例的用途。
- **skill / docs 去答案化是啟發式**：`check_skill_leaks.py` 只比對數字 token（已涵蓋 tw-drainage；
  tw-drainage 不再指向 `tests/level2/*/input/spec.json` 當範例）；不掃 `docs/`（含研究紀錄，例如桃園
  手冊算例數值）、README。小整數、法規 yaml 常數（0.051、0.045、Horner 係數等，`codes/*.yaml` 白名單）、範例值不列入比對，
  所以「答案剛好等於 yaml 常數」（例如最小貯集量係數）或「以文字 / 公式提示解法」（例如置中門
  offset 公式）抓不到。prompt 本身明列的通過標準數值（例 6762.5、85.68）當然也在工作區內。
- **沙箱不完整**：`python -c "open('<原 repo>/…/expected.json')"` 仍做得到；只能靠 log 掃描事後抓。
  覆蓋式 venv 的 `.pth` 內含原 repo 路徑。要嚴格隔離請在容器 / 另一使用者下跑。
- `draftagent build` 以外的產出（例如只照 prompt 跑 `tools/gen_dxf.py`）沒有 manifest，判 no_output。
  這是刻意的（前言要求 build），但會把「圖其實對」算成失敗。
- level2 的 drainage-plan / detail / outflow 比對項綁定 verifier metrics 欄名與產物 JSON 結構；
  其他分支改動這些欄名時 `tasks.py` 需同步（replay 全綠可當回歸測試）。
- 修正輪數靠 Bash 指令字串比對；透過 Python API 呼叫 `build()` 不會被計入。
- **CodexRunner**：旗標（`exec --json -C -s workspace-write -m --skip-git-repo-check --ephemeral -o`）
  已以 `codex exec --help`（codex-cli 0.160.0）查證，但 `--json` 事件 schema 未查證，解析是 best-effort；
  沒有 max-turns（只有 timeout）、沒有 cost；workspace-write 不限制讀取。
- 串行執行，沒有平行；`--repeat` 每次都重建工作區。
- `--max-turns` 是隱藏旗標，未來版本可能改名；`ClaudeCodeRunner.command()` 集中在一處。
