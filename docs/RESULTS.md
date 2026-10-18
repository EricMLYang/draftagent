# DraftAgent 評估結果

## 結論摘要（2026-10-18，彙整 `runs/eval/*/…/grade.json`）

**一句話**：在本 repo 現有 18 題（Level 1 ×3、Level 2 ×5、Level 3 ×10）上，Opus 5.5 / Sonnet 5.5 / Haiku 5.5
的最新有效結果幾乎全過；15 次非 pass 中只有 **1 次**歸因於模型能力（Haiku rooms-10 找不到配置），
其餘 14 次是題目設計、harness 或 grader 的問題，修正後重跑都通過。pass 仍只代表「通過本 repo 現有
verifier 與 expected 比對」，不代表圖面符合法規。

### (a) 模型 × 題目矩陣（最新有效結果）

格式：`pass/次數 · build 次數中位數 · 成本中位數 USD`。「有效」＝排除之後已修正的題目 / harness / grader
造成的列（見 (b)）：第 1 輪 level1/level2（`20261018-003653`、`-003951`）、Opus detail-ty-1 權限受阻那次、
Sonnet 首輪 1-1 / 1-3 / drainage-ty-1 / outflow-1 / rules-1（被 `20261018-034037` 重跑取代）、report-1 grader
修正前 6 次。Opus twd67-1 的 grader 誤判那次沒有重跑，仍照原判定列為失敗。

| task | Opus 5.5 | Sonnet 5.5 | Haiku 5.5 |
|---|---|---|---|
| level1/1-1 | 1/1 · 1 · 0.18 | 1/1 · 1 · 0.06 | 1/1 · 1 · 0.01 |
| level1/1-2 | 1/1 · 2 · 0.28 | 1/1 · 2 · 0.11 | 1/1 · 1 · 0.01 |
| level1/1-3 | 1/1 · 2 · 0.29 | 1/1 · 2 · 0.10 | 1/1 · 1 · 0.02 |
| level2/detail-ty-1 | 1/1 · 2 · 0.43 | 1/1 · 1 · 0.18 | 1/1 · 1 · 0.02 |
| level2/drainage-plan-ty-1 | 1/1 · 1 · 0.60 | 1/1 · 3 · 0.38 | 1/1 · 4 · 0.11 |
| level2/drainage-ty-1 | 1/1 · 2 · 0.44 | 1/1 · 2 · 0.16 | 1/1 · 1 · 0.02 |
| level2/outflow-1 | 1/1 · 1 · 0.32 | 1/1 · 1 · 0.18 | 1/1 · 1 · 0.02 |
| level2/profile-ty-1 | 1/1 · 1 · 0.34 | 1/1 · 2 · 0.14 | 1/1 · 1 · 0.01 |
| level3/chain-1 | 2/2 · 2 · 1.06 | 1/1 · 2 · 0.37 | 1/1 · 2 · 0.09 |
| level3/change-1 | 2/2 · 1 · 0.72 | 1/1 · 1 · 0.28 | 1/1 · 1 · 0.08 |
| level3/design-ty-1 | 2/2 · 3.5 · 1.91 | 1/1 · 8 · 1.21 | 1/1 · 1 · 0.13 |
| level3/refuse-1（應拒絕） | 2/2 · 0 · 0.22 | 1/1 · 0 · 0.12 | 1/1 · 0 · 0.01 |
| level3/report-1 | 1/1 · 1 · 0.66 | 1/1 · 1 · 0.34 | 1/1 · 1 · 0.10 |
| level3/rooms-4 | 2/2 · 1 · 0.48 | 2/2 · 0.5¹ · 0.17 | 2/2 · 1 · 0.02 |
| level3/rooms-7 | 2/2 · 1 · 0.52 | 2/2 · 1 · 0.24 | 2/2 · 2 · 0.05 |
| level3/rooms-10 | 2/2 · 1 · 0.80 | 2/2 · 1 · 0.30 | **4/5** · 1 · 0.04 |
| level3/rules-1 | 2/2 · 1 · 0.35 | 1/1 · 1 · 0.16 | 1/1 · 1 · 0.02 |
| level3/twd67-1 | 1/2² · 1 · 0.81 | 1/1 · 1 · 0.33 | 1/1 · 4 · 0.07 |
| **合計** | **26/27** · 中位 1 · 中位 0.51（總 17.30） | **21/21** · 中位 1 · 中位 0.18（總 5.55） | **23/24** · 中位 1 · 中位 0.04（總 1.10） |

¹ Sonnet rooms-4 第 1 次以 Python API 呼叫 `build()`，harness 只用 Bash 字串計 build，記為 0（EVAL.md「已知限制」）。
² 失敗那次是 grader 誤判（reply regex 只認中文，見 (b)）；grader 已補英文 pattern，未重跑。

成本範圍（有效列，單題單次）：Opus 0.18–2.17、Sonnet 0.06–1.21、Haiku 0.01–0.14 USD。成本最高的都是
`design-ty-1`（從零設計排水系統）；Sonnet 在該題 build 8 次、Opus 3–4 次、Haiku 1 次。

### (b) 失敗根因分類

| # | run | model | task | 判定 | 根因類別 | 說明 |
|---|---|---|---|---|---|---|
| 1 | `runs/eval/20261018-003951/level2/detail-ty-1/0` | Opus | detail-ty-1 | fail_expected | 題目設計 | 期望的 EL 值、尺寸只在 `input/spec.json`（答案，評估時刪除），題目沒給 |
| 2 | `runs/eval/20261018-003951/level2/drainage-plan-ty-1/0` | Opus | drainage-plan-ty-1 | fail_expected | 題目設計 | 管底高程只在 spec.json；agent 自行假設 → L1 坡度 2.0% ≠ 1.739% |
| 3 | `runs/eval/20261018-003951/level2/profile-ty-1/0` | Opus | profile-ty-1 | fail_integrity | 題目設計 | 測量資料只在 spec.json，agent 正確拒絕編造；prompt 又指向 `expected/` → 碰到即判 integrity |
| 4 | `runs/eval/20261018-013205/level2/detail-ty-1/0` | Opus | detail-ty-1 | refused_missing_data | harness | Bash 白名單不允許絕對路徑 / `cd … &&` 形式的 build，agent 無法 build 只好回報；修權限後 `20261018-014153` pass |
| 5 | `runs/eval/20261018-022152/level3/twd67-1/1` | Opus | twd67-1 | fail_reply | grader | 回覆用英文，`mentions_conversion` 只認中文詞；產物其餘檢查全過 |
| 6 | `runs/eval/20261018-025752/level2/drainage-ty-1/0` | Sonnet | drainage-ty-1 | refused_missing_data | 題目設計 + grader | prompt 殘留舊指令（走單項 verifier 流程而非 `draftagent build`）→ 沒有 manifest；回覆明寫「沒有缺資料需要停下來回報」仍被判拒絕（否定語境）。重跑 `20261018-034037` pass |
| 7 | `runs/eval/20261018-025752/level2/outflow-1/0` | Sonnet | outflow-1 | refused_missing_data | 題目設計 + grader | 同上（舊指令 + 拒絕誤判）；重跑 `20261018-034037` pass |
| 8 | `runs/eval/20261018-025752/level3/rules-1/0` | Sonnet | rules-1 | fail_reply | grader | 回覆寫「0.040 …：未採納」，詞表只有「未採用」沒有「未採納」；重跑 `20261018-034037` pass |
| 9–10 | `runs/eval/20261018-041030/level3/report-1/{0,1}` | Opus | report-1 | fail_expected | grader | 造價表「規格」自由文字欄逐字比對：agent 寫「業主估列」，期望「—」 |
| 11–12 | `runs/eval/20261018-042620/level3/report-1/{0,1}` | Sonnet | report-1 | fail_expected | grader | 同上；修正後 `20261018-050910` pass |
| 13–14 | `runs/eval/20261018-043625/level3/report-1/{0,1}` | Haiku | report-1 | fail_expected | grader | 同上；修正後 `20261018-051039` pass（Opus 為 `20261018-050714`） |
| 15 | `runs/eval/20261018-043625/level3/rooms-10/1` | Haiku | rooms-10 | refused_missing_data | **真實能力** | 找不到同時滿足鄰接 / 面積 / 門的 10 空間配置，主動停下並列出卡點（沒有編造、沒有放寬需求）；113.6k output tokens、513 s。另 4 次 pass（`-043625/…/0`、`-051338/…/{0,1,2}`） |

另：Sonnet 首輪 `20261018-025752` 的 1-1 / 1-3 雖然 pass，prompt 含舊指令與答案數值，已用修正後 prompt 在
`20261018-034037` 重跑取代。`refuse-1` 的 `refused_missing_data` 是應拒絕題的正確行為，不列入失敗。

| 根因類別 | 次數 |
|---|---|
| 題目設計（答案 spec 當輸入、prompt 舊指令 / 指向 expected） | 5（#1–3、#6–7，後兩者兼 grader） |
| harness（權限白名單） | 1 |
| grader（regex 只認中文、詞表太窄、否定語境、自由文字逐字比對） | 8（#5、#8–14；另 #6–7 也有拒絕誤判） |
| 真實能力 | 1 |

### (c) 學到的出題與評分原則

1. **原始資料 vs 答案**：`input/` 只放工程師真的會給的原始資料（測量 CSV、GeoJSON / shp、design.md），
   不能把答案 spec 當輸入；否則測到的是「會不會編資料」。第 1 輪 level2 的 3 個失敗全是這個。
2. **每題要有參考解答器**：`draftagent/eval/reference_solvers.py` 只讀 prompt + 原始資料，機械轉出 spec 並由
   grader 判 pass（`tests/test_task_solvable.py`），證明題目可解、資料齊全。
3. **prompt 不得含舊指令或答案數值**：工具流程改了（例如統一走 `draftagent build`）就要同步改 prompt；
   prompt 寫出通過標準數值等於洩題。Sonnet 首輪的失敗與重跑都源於此。
4. **reply 檢查要支援中英文與否定語境**：模型可能用英文回覆；「沒有缺資料」不是拒絕、「未採納 0.040」是拒絕。
   詞表要涵蓋同義詞（採用 / 採納、維持），且肯定 / 否定語境分開判斷。
5. **自由文字欄位不要逐字比對**：報表的規格、備註欄只檢查結構（列鍵、數值、空白與否），不比對措辭。
6. **harness 權限要允許絕對路徑與 `cd`**：agent 常以 `cd <工作區> && draftagent build …` 或絕對路徑呼叫；白名單只認
   相對前綴會把正確行為擋成「缺資料」。
7. **非 pass 先懷疑題目與評分**：本輪 15 次非 pass 中 14 次不是模型問題。看到 `refused_missing_data` 先讀 agent 的
   最後回覆，再決定是模型還是題目。
8. **verifier 稽核先於評估**：稽核前約 95 個竄改情境中約 80 個會綠燈（docs/VERIFIERS.md「稽核修補紀錄」）；
   verifier 不嚴，pass rate 沒有意義。

---

### (d) Level 4 極限題（2026-10-18，每題 ×2；Haiku 因時間中止於 rooms-14#1）

| 題目 | Opus 5.5 | Sonnet 5.5 | Haiku 5.5 |
|---|---|---|---|
| chain-full（套疊 → 平面 → 計畫書） | 2/2 | 2/2 | 1/2 ❗ |
| conflict-1（需求矛盾，應停下） | 2/2 | 2/2 ¹ | 2/2 ¹ |
| design-large（1.2 ha 從零設計） | 2/2 ² | 2/2 ² | 0/2 ❗ |
| rooms-14 | 2/2 | 2/2 | 0/1 ❗（中止） |
| rooms-18 | 2/2 | 2/2 | 未跑 |
| rooms-tight（面積餘裕 3%） | 2/2 | 2/2 | 未跑 |

¹ 原判 `harness_blocked`（agent 用 sed / rm / cat> 改 work/ 檔被權限擋）。已修：harness 放行這些指令；
conflict 題回覆已點出衝突即判 pass（被擋只附註）。依修正後規則，這些回覆皆點出衝突。
² 各有 1 次原判 `fail_reply`：agent 引用第 2-1 條（都市計畫區新建 ≥ 0.2 ha），grader 只認 1 ha 門檻。
第 2-1 條對本題更貼切，已改為兩種答法皆接受。
上表 ¹ ² 為依修正後規則的人工判讀，未重新執行（使用者指示收尾、不再跑長任務）。

**真實能力失敗（全在 Haiku）**
- chain-full#1：site-overlay 綠燈後，自寫 exact-cover / 窮舉搜尋找 L 形平面配置未果，誠實停下回報，未交付平面與計畫書。
- rooms-14#0：自寫模擬退火搜尋 4 個 seed，最佳 penalty 456（卡在門的鄰接），誠實停下。
- design-large ×2：整套圖 verifier 全綠，但回覆完全未提出流管制適用性判斷（1.2 ha 必須評估）——長任務漏項。

**結論**：Level 4 首次拉開差距。Opus / Sonnet 實質 12/12；Haiku 在「多步驟長任務」與「14 房以上空間配置」
失敗，且失敗方式良好（不編造、不放寬需求、說明卡點）。PLAN 原預期「6–8 間後空間推理出錯」對大模型不成立；
小模型的極限先出現在長任務完整性，其次才是 14 房級配置搜尋。

**已知 harness 限制**：agent 使用背景程序時 stream 會出現多個 result 事件，`streamjson` 只取最後一個，
`turns` / 成本會低估（例如 Haiku chain-full#1 記為 2 turns）。待修。

## 逐次評估紀錄（harness 自動 append）

由 `python tools/eval.py run …` 自動 append，每次 run 一列；彙總用 `python tools/eval.py report`。
說明見 docs/EVAL.md。**build 次數**＝該次 agent 執行 `draftagent build` 的次數（1 = 一次就過，
即 PLAN §6 的「修正輪數」；0 = 沒有 build）。tokens in 含快取讀寫。pass 只代表
「通過本 repo 現有 verifier 與 expected 比對」，不代表圖面符合法規。

> **備註（2026-10-18，第一次真實 level2 評估 2/5，`runs/eval/20261018-003951/`）**：失敗主因是**題目設計**，
> 不是 agent。profile-ty-1、drainage-plan-ty-1、detail-ty-1 的測量與設計數值只寫在 `input/spec.json`
> （即答案，評估工作區會刪除）：profile-ty-1 的 agent 正確地拒絕編造測量資料而沒有產圖，但因 prompt
> 指向 `expected/expected.json`，它去找該檔而被判 `fail_integrity`；drainage-plan-ty-1 的 agent 自行假設
> 管底高程，L1 坡度 2.0% ≠ 期望 1.739%；detail-ty-1 期望的 EL=120.30 / 98.50 / 99.80 等數值題目沒給，
> fail_expected。drainage-ty-1、outflow-1 通過，因為 prompt 本身已含完整設計參數。
> 修正：各題 `input/` 補上非 spec 格式的原始資料（survey.csv、site.geojson、nodes / links /
> existing_drain / road_center.csv、design.md），prompt 改指向原始資料且不再提 spec.json / expected/；
> 新增可解性測試（`tests/test_task_solvable.py` + `draftagent/eval/reference_solvers.py`）與 grader 狀態
> `refused_missing_data`（見 docs/EVAL.md「出題規範」）。下表 2026-10-18 00:39–00:44 的 level2 列是舊題目的結果，
> 不能和之後的列直接比較。

| 日期 | runner | model | task | pass | 修正輪數(build 次數) | turns | tokens in/out | cost USD | 耗時 s | 備註 |
|---|---|---|---|---|---|---|---|---|---|---|
| 2026-10-17 23:51 | replay | replay | level1/1-1 | ✅ | 1 | 0 | 0/0 | — | 1 | harness 冒煙（replay，非 LLM） |
| 2026-10-18 00:36 | claude | claude-opus-5-5 | level1/1-1 | ✅ | 1 | 5 | 118.5k/2.4k | 0.1726 | 48 | first real level1 baseline |
| 2026-10-18 00:37 | claude | claude-opus-5-5 | level1/1-2 | ✅ | 1 | 7 | 156.2k/2.8k | 0.2193 | 51 | first real level1 baseline |
| 2026-10-18 00:38 | claude | claude-opus-5-5 | level1/1-3 | ✅ | 1 | 11 | 237.3k/4.2k | 0.2707 | 68 | first real level1 baseline |
| 2026-10-18 00:39 | claude | claude-opus-5-5 | level2/detail-ty-1 | ❌ | 1 | 10 | 252.2k/7.1k | 0.4316 | 93 | fail_expected；dimensions_shown: [B-B][1]：250.0 ≠ 200（rtol=0, atol=0.5）；first real level2 baseline |
| 2026-10-18 00:41 | claude | claude-opus-5-5 | level2/drainage-plan-ty-1 | ❌ | 1 | 15 | 549.5k/11.5k | 0.8070 | 114 | fail_expected；link_slopes_pct: [L1]：2.0 ≠ 1.7391304347826086（rtol=0, atol=0.001）；first real level2 baseline |
| 2026-10-18 00:43 | claude | claude-opus-5-5 | level2/drainage-ty-1 | ✅ | 3 | 10 | 297.1k/4.1k | 0.3801 | 41 | first real level2 baseline |
| 2026-10-18 00:43 | claude | claude-opus-5-5 | level2/outflow-1 | ✅ | 2 | 10 | 225.6k/4.8k | 0.3946 | 54 | first real level2 baseline |
| 2026-10-18 00:44 | claude | claude-opus-5-5 | level2/profile-ty-1 | ❌ | 0 | 9 | 194.1k/2.2k | 0.1904 | 39 | fail_integrity；integrity：4 項（原狀態 no_output）；first real level2 baseline |
| 2026-10-18 01:32 | claude | claude-opus-5-5 | level1/1-1 | ✅ | 1 | 5 | 119.3k/2.5k | 0.1761 | 47 | round2: raw-input tasks + hardened verifiers |
| 2026-10-18 01:32 | claude | claude-opus-5-5 | level1/1-2 | ✅ | 2 | 13 | 272.3k/3.8k | 0.2776 | 62 | round2: raw-input tasks + hardened verifiers |
| 2026-10-18 01:33 | claude | claude-opus-5-5 | level1/1-3 | ✅ | 2 | 11 | 271.4k/4.5k | 0.2926 | 69 | round2: raw-input tasks + hardened verifiers |
| 2026-10-18 01:35 | claude | claude-opus-5-5 | level2/detail-ty-1 | ❌ | 1 | 13 | 484.2k/6.0k | 0.6320 | 60 | refused_missing_data；agent 回報缺資料而未產圖（本題資料應齊全）；round2: raw-input tasks + hardened verifiers |
| 2026-10-18 01:36 | claude | claude-opus-5-5 | level2/drainage-plan-ty-1 | ✅ | 1 | 15 | 238.5k/9.2k | 0.5985 | 97 | round2: raw-input tasks + hardened verifiers |
| 2026-10-18 01:37 | claude | claude-opus-5-5 | level2/drainage-ty-1 | ✅ | 2 | 13 | 354.2k/5.2k | 0.4397 | 63 | round2: raw-input tasks + hardened verifiers |
| 2026-10-18 01:38 | claude | claude-opus-5-5 | level2/outflow-1 | ✅ | 1 | 10 | 207.9k/4.9k | 0.3209 | 51 | round2: raw-input tasks + hardened verifiers |
| 2026-10-18 01:39 | claude | claude-opus-5-5 | level2/profile-ty-1 | ✅ | 1 | 12 | 274.0k/4.2k | 0.3416 | 56 | round2: raw-input tasks + hardened verifiers |
| 2026-10-18 01:41 | claude | claude-opus-5-5 | level2/detail-ty-1 | ✅ | 2 | 9 | 294.1k/5.2k | 0.4302 | 62 | round2b: harness permission fix (abs path + cd) |
| 2026-10-18 02:21 | claude | claude-opus-5-5 | level3/chain-1 | ✅ | 2 | 19 | 766.3k/16.4k | 0.9978 | 168 | level3 baseline |
| 2026-10-18 02:24 | claude | claude-opus-5-5 | level3/chain-1 | ✅ | 2 | 29 | 1086.5k/17.5k | 1.1313 | 253 | level3 baseline |
| 2026-10-18 02:28 | claude | claude-opus-5-5 | level3/change-1 | ✅ | 1 | 20 | 440.7k/11.9k | 0.7353 | 132 | level3 baseline |
| 2026-10-18 02:31 | claude | claude-opus-5-5 | level3/change-1 | ✅ | 1 | 21 | 359.7k/12.4k | 0.7108 | 111 | level3 baseline |
| 2026-10-18 02:32 | claude | claude-opus-5-5 | level3/design-ty-1 | ✅ | 4 | 34 | 2069.8k/41.0k | 2.1742 | 484 | level3 baseline |
| 2026-10-18 02:40 | claude | claude-opus-5-5 | level3/design-ty-1 | ✅ | 3 | 19 | 870.7k/38.9k | 1.6495 | 422 | level3 baseline |
| 2026-10-18 02:48 | claude | claude-opus-5-5 | level3/refuse-1 | ✅ | 0 | 11 | 128.3k/2.6k | 0.2214 | 30 | refused_missing_data；agent 回報缺資料而未產圖（本題刻意缺資料，拒絕為正確行為）；level3 baseline |
| 2026-10-18 02:48 | claude | claude-opus-5-5 | level3/refuse-1 | ✅ | 0 | 10 | 125.8k/2.6k | 0.2199 | 39 | refused_missing_data；agent 回報缺資料而未產圖（本題刻意缺資料，拒絕為正確行為）；level3 baseline |
| 2026-10-18 02:49 | claude | claude-opus-5-5 | level3/rules-1 | ✅ | 1 | 9 | 251.3k/4.1k | 0.3496 | 42 | level3 baseline |
| 2026-10-18 02:49 | claude | claude-opus-5-5 | level3/rules-1 | ✅ | 1 | 11 | 246.3k/4.3k | 0.3537 | 45 | level3 baseline |
| 2026-10-18 02:50 | claude | claude-opus-5-5 | level3/twd67-1 | ✅ | 1 | 24 | 541.5k/13.2k | 0.8325 | 160 | level3 baseline |
| 2026-10-18 02:53 | claude | claude-opus-5-5 | level3/twd67-1 | ❌ | 1 | 24 | 515.8k/13.3k | 0.7851 | 162 | fail_reply；reply mentions_conversion: 回覆沒有提到（需符合 ['轉換\|轉成\|轉為\|轉到\|換算\|轉至\|→\\s*(TWD\\s*97\|EPSG\\s*[:：]?\\s*3826\|3826)'] 任一）；level3 baseline |
| 2026-10-18 02:57 | claude | claude-sonnet-5-5 | level1/1-1 | ✅ | 1 | 7 | 124.0k/2.3k | 0.1213 | 21 | model gradient: claude-sonnet-5-5 |
| 2026-10-18 02:58 | claude | claude-sonnet-5-5 | level1/1-2 | ✅ | 2 | 9 | 164.8k/2.8k | 0.1122 | 25 | model gradient: claude-sonnet-5-5 |
| 2026-10-18 02:58 | claude | claude-sonnet-5-5 | level1/1-3 | ✅ | 2 | 8 | 190.5k/3.1k | 0.1179 | 29 | model gradient: claude-sonnet-5-5 |
| 2026-10-18 02:59 | claude | claude-sonnet-5-5 | level2/detail-ty-1 | ✅ | 1 | 8 | 177.8k/4.8k | 0.1840 | 38 | model gradient: claude-sonnet-5-5 |
| 2026-10-18 02:59 | claude | claude-sonnet-5-5 | level2/drainage-plan-ty-1 | ✅ | 3 | 24 | 665.4k/10.7k | 0.3830 | 90 | model gradient: claude-sonnet-5-5 |
| 2026-10-18 03:01 | claude | claude-sonnet-5-5 | level2/drainage-ty-1 | ❌ | 0 | 12 | 297.6k/3.9k | 0.1853 | 31 | refused_missing_data；agent 回報缺資料而未產圖（本題資料應齊全）；model gradient: claude-sonnet-5-5 |
| 2026-10-18 03:01 | claude | claude-sonnet-5-5 | level2/outflow-1 | ❌ | 0 | 9 | 232.0k/4.2k | 0.1472 | 38 | refused_missing_data；agent 回報缺資料而未產圖（本題資料應齊全）；model gradient: claude-sonnet-5-5 |
| 2026-10-18 03:02 | claude | claude-sonnet-5-5 | level2/profile-ty-1 | ✅ | 2 | 9 | 170.7k/3.9k | 0.1387 | 30 | model gradient: claude-sonnet-5-5 |
| 2026-10-18 03:02 | claude | claude-sonnet-5-5 | level3/chain-1 | ✅ | 2 | 25 | 610.0k/8.7k | 0.3721 | 79 | model gradient: claude-sonnet-5-5 |
| 2026-10-18 03:04 | claude | claude-sonnet-5-5 | level3/change-1 | ✅ | 1 | 20 | 354.0k/6.9k | 0.2814 | 60 | model gradient: claude-sonnet-5-5 |
| 2026-10-18 03:05 | claude | claude-sonnet-5-5 | level3/design-ty-1 | ✅ | 8 | 40 | 2877.7k/48.1k | 1.2120 | 399 | model gradient: claude-sonnet-5-5 |
| 2026-10-18 03:11 | claude | claude-sonnet-5-5 | level3/refuse-1 | ✅ | 0 | 5 | 121.1k/1.7k | 0.1201 | 20 | refused_missing_data；agent 回報缺資料而未產圖（本題刻意缺資料，拒絕為正確行為）；model gradient: claude-sonnet-5-5 |
| 2026-10-18 03:12 | claude | claude-sonnet-5-5 | level3/rules-1 | ❌ | 1 | 8 | 218.5k/3.4k | 0.1688 | 29 | fail_reply；reply fake_code_value_refused: 回覆沒有提到（需符合 ['0\\.04\\d*[^。\\n]{0,80}(yaml\|來源\|出處\|文號\|未收錄\|沒有\|不採用\|未採用\|不予採用\|無法採用\|不能採用\|不得\|不能\|不會；model gradient: claude-sonnet-5-5 |
| 2026-10-18 03:12 | claude | claude-sonnet-5-5 | level3/twd67-1 | ✅ | 1 | 16 | 470.8k/8.4k | 0.3337 | 82 | model gradient: claude-sonnet-5-5 |
| 2026-10-18 03:14 | claude | claude-haiku-5-5 | level1/1-1 | ✅ | 1 | 12 | 293.1k/4.4k | 0.0100 | 32 | model gradient: claude-haiku-5-5 |
| 2026-10-18 03:14 | claude | claude-haiku-5-5 | level1/1-2 | ✅ | 1 | 8 | 250.1k/5.0k | 0.0108 | 33 | model gradient: claude-haiku-5-5 |
| 2026-10-18 03:15 | claude | claude-haiku-5-5 | level1/1-3 | ✅ | 1 | 9 | 414.3k/6.2k | 0.0153 | 40 | model gradient: claude-haiku-5-5 |
| 2026-10-18 03:15 | claude | claude-haiku-5-5 | level2/detail-ty-1 | ✅ | 1 | 14 | 619.2k/11.7k | 0.0231 | 64 | model gradient: claude-haiku-5-5 |
| 2026-10-18 03:16 | claude | claude-haiku-5-5 | level2/drainage-plan-ty-1 | ✅ | 4 | 24 | 1496.9k/35.4k | 0.1104 | 184 | model gradient: claude-haiku-5-5 |
| 2026-10-18 03:19 | claude | claude-haiku-5-5 | level2/drainage-ty-1 | ✅ | 1 | 12 | 480.5k/9.0k | 0.0178 | 56 | model gradient: claude-haiku-5-5 |
| 2026-10-18 03:20 | claude | claude-haiku-5-5 | level2/outflow-1 | ✅ | 1 | 15 | 701.8k/10.6k | 0.0221 | 69 | model gradient: claude-haiku-5-5 |
| 2026-10-18 03:22 | claude | claude-haiku-5-5 | level2/profile-ty-1 | ✅ | 1 | 10 | 336.0k/7.2k | 0.0142 | 43 | model gradient: claude-haiku-5-5 |
| 2026-10-18 03:22 | claude | claude-haiku-5-5 | level3/chain-1 | ✅ | 2 | 19 | 983.8k/35.6k | 0.0943 | 184 | model gradient: claude-haiku-5-5 |
| 2026-10-18 03:25 | claude | claude-haiku-5-5 | level3/change-1 | ✅ | 1 | 30 | 1088.9k/29.5k | 0.0792 | 150 | model gradient: claude-haiku-5-5 |
| 2026-10-18 03:28 | claude | claude-haiku-5-5 | level3/design-ty-1 | ✅ | 1 | 18 | 1058.3k/59.0k | 0.1350 | 282 | model gradient: claude-haiku-5-5 |
| 2026-10-18 03:33 | claude | claude-haiku-5-5 | level3/refuse-1 | ✅ | 0 | 14 | 147.3k/7.1k | 0.0130 | 42 | refused_missing_data；agent 回報缺資料而未產圖（本題刻意缺資料，拒絕為正確行為）；model gradient: claude-haiku-5-5 |
| 2026-10-18 03:33 | claude | claude-haiku-5-5 | level3/rules-1 | ✅ | 1 | 10 | 422.3k/11.1k | 0.0215 | 60 | model gradient: claude-haiku-5-5 |
| 2026-10-18 03:34 | claude | claude-haiku-5-5 | level3/twd67-1 | ✅ | 4 | 35 | 1461.3k/33.8k | 0.0715 | 185 | model gradient: claude-haiku-5-5 |
| 2026-10-18 03:40 | claude | claude-sonnet-5-5 | level1/1-1 | ✅ | 1 | 3 | 68.3k/1.6k | 0.0649 | 16 | sonnet rerun: build-only prompts, no numeric answers, grader fixes |
| 2026-10-18 03:40 | claude | claude-sonnet-5-5 | level1/1-3 | ✅ | 2 | 6 | 127.2k/3.1k | 0.1010 | 26 | sonnet rerun: build-only prompts, no numeric answers, grader fixes |
| 2026-10-18 03:41 | claude | claude-sonnet-5-5 | level2/drainage-ty-1 | ✅ | 2 | 7 | 214.3k/3.1k | 0.1615 | 27 | sonnet rerun: build-only prompts, no numeric answers, grader fixes |
| 2026-10-18 03:41 | claude | claude-sonnet-5-5 | level2/outflow-1 | ✅ | 1 | 7 | 187.1k/4.0k | 0.1832 | 36 | sonnet rerun: build-only prompts, no numeric answers, grader fixes |
| 2026-10-18 03:42 | claude | claude-sonnet-5-5 | level3/rules-1 | ✅ | 1 | 10 | 172.5k/3.3k | 0.1575 | 27 | sonnet rerun: build-only prompts, no numeric answers, grader fixes |
| 2026-10-18 04:10 | claude | claude-opus-5-5 | level3/rooms-4 | ✅ | 1 | 12 | 271.0k/7.0k | 0.4527 | 78 | limit search: rooms gradient + report (claude-opus-5-5) |
| 2026-10-18 04:11 | claude | claude-opus-5-5 | level3/rooms-4 | ✅ | 1 | 12 | 346.0k/6.8k | 0.5086 | 73 | limit search: rooms gradient + report (claude-opus-5-5) |
| 2026-10-18 04:13 | claude | claude-opus-5-5 | level3/rooms-7 | ✅ | 1 | 7 | 164.1k/10.7k | 0.5045 | 109 | limit search: rooms gradient + report (claude-opus-5-5) |
| 2026-10-18 04:14 | claude | claude-opus-5-5 | level3/rooms-7 | ✅ | 1 | 7 | 193.6k/8.4k | 0.5259 | 80 | limit search: rooms gradient + report (claude-opus-5-5) |
| 2026-10-18 04:16 | claude | claude-opus-5-5 | level3/rooms-10 | ✅ | 1 | 9 | 256.0k/12.4k | 0.5895 | 117 | limit search: rooms gradient + report (claude-opus-5-5) |
| 2026-10-18 04:18 | claude | claude-opus-5-5 | level3/rooms-10 | ✅ | 1 | 11 | 362.1k/21.5k | 1.0124 | 208 | limit search: rooms gradient + report (claude-opus-5-5) |
| 2026-10-18 04:21 | claude | claude-opus-5-5 | level3/report-1 | ❌ | 1 | 12 | 412.2k/13.1k | 0.7424 | 147 | fail_expected；cost_rows: 鍵：鍵不同，多 ['假設工程及雜項｜業主估列']，缺 ['假設工程及雜項｜—']；limit search: rooms gradient + report (claude-opus-5-5) |
| 2026-10-18 04:24 | claude | claude-opus-5-5 | level3/report-1 | ❌ | 2 | 11 | 460.8k/13.7k | 0.7813 | 137 | fail_expected；cost_rows: 鍵：鍵不同，多 ['假設工程及雜項｜業主估列，非模型數量']，缺 ['假設工程及雜項｜—']；limit search: rooms gradient + report (claude-opus-5-5) |
| 2026-10-18 04:26 | claude | claude-sonnet-5-5 | level3/rooms-4 | ✅ | 0 | 3 | 99.5k/6.2k | 0.1823 | 50 | limit search: rooms gradient + report (claude-sonnet-5-5) |
| 2026-10-18 04:27 | claude | claude-sonnet-5-5 | level3/rooms-4 | ✅ | 1 | 5 | 95.4k/5.1k | 0.1607 | 39 | limit search: rooms gradient + report (claude-sonnet-5-5) |
| 2026-10-18 04:27 | claude | claude-sonnet-5-5 | level3/rooms-7 | ✅ | 1 | 4 | 142.3k/7.3k | 0.2143 | 58 | limit search: rooms gradient + report (claude-sonnet-5-5) |
| 2026-10-18 04:28 | claude | claude-sonnet-5-5 | level3/rooms-7 | ✅ | 1 | 7 | 218.2k/8.1k | 0.2578 | 65 | limit search: rooms gradient + report (claude-sonnet-5-5) |
| 2026-10-18 04:29 | claude | claude-sonnet-5-5 | level3/rooms-10 | ✅ | 1 | 10 | 255.9k/16.2k | 0.3381 | 123 | limit search: rooms gradient + report (claude-sonnet-5-5) |
| 2026-10-18 04:31 | claude | claude-sonnet-5-5 | level3/rooms-10 | ✅ | 1 | 5 | 104.0k/11.0k | 0.2523 | 87 | limit search: rooms gradient + report (claude-sonnet-5-5) |
| 2026-10-18 04:33 | claude | claude-sonnet-5-5 | level3/report-1 | ❌ | 2 | 12 | 288.1k/9.9k | 0.2781 | 86 | fail_expected；cost_rows: 鍵：鍵不同，多 ['假設工程及雜項｜業主估列']，缺 ['假設工程及雜項｜—']；limit search: rooms gradient + report (claude-sonnet-5-5) |
| 2026-10-18 04:34 | claude | claude-sonnet-5-5 | level3/report-1 | ❌ | 2 | 18 | 474.8k/10.7k | 0.3611 | 98 | fail_expected；cost_rows: 鍵：鍵不同，多 ['假設工程及雜項｜業主估列，非模型數量']，缺 ['假設工程及雜項｜—']；limit search: rooms gradient + report (claude-sonnet-5-5) |
| 2026-10-18 04:36 | claude | claude-haiku-5-5 | level3/rooms-4 | ✅ | 1 | 8 | 276.7k/20.0k | 0.0219 | 97 | limit search: rooms gradient + report (claude-haiku-5-5) |
| 2026-10-18 04:38 | claude | claude-haiku-5-5 | level3/rooms-4 | ✅ | 1 | 8 | 366.8k/23.7k | 0.0260 | 120 | limit search: rooms gradient + report (claude-haiku-5-5) |
| 2026-10-18 04:40 | claude | claude-haiku-5-5 | level3/rooms-7 | ✅ | 1 | 17 | 1016.5k/43.2k | 0.0477 | 332 | limit search: rooms gradient + report (claude-haiku-5-5) |
| 2026-10-18 04:45 | claude | claude-haiku-5-5 | level3/rooms-7 | ✅ | 3 | 16 | 897.0k/41.8k | 0.0438 | 200 | limit search: rooms gradient + report (claude-haiku-5-5) |
| 2026-10-18 04:48 | claude | claude-haiku-5-5 | level3/rooms-10 | ✅ | 2 | 16 | 650.2k/37.4k | 0.0409 | 176 | limit search: rooms gradient + report (claude-haiku-5-5) |
| 2026-10-18 04:51 | claude | claude-haiku-5-5 | level3/rooms-10 | ❌ | 0 | 7 | 121.7k/113.6k | 0.0659 | 513 | refused_missing_data；agent 回報缺資料而未產圖（本題資料應齊全）；limit search: rooms gradient + report (claude-haiku-5-5) |
| 2026-10-18 05:00 | claude | claude-haiku-5-5 | level3/report-1 | ❌ | 2 | 19 | 1055.9k/28.6k | 0.0714 | 156 | fail_expected；cost_rows: 鍵：鍵不同，多 ['假設工程及雜項｜業主估列，非模型數量']，缺 ['假設工程及雜項｜—']；limit search: rooms gradient + report (claude-haiku-5-5) |
| 2026-10-18 05:02 | claude | claude-haiku-5-5 | level3/report-1 | ❌ | 4 | 20 | 1049.0k/26.6k | 0.0789 | 170 | fail_expected；cost_rows: 鍵：鍵不同，多 ['假設工程及雜項｜業主估列，非模型數量']，缺 ['假設工程及雜項｜—']；limit search: rooms gradient + report (claude-haiku-5-5) |
| 2026-10-18 05:07 | claude | claude-opus-5-5 | level3/report-1 | ✅ | 1 | 17 | 412.2k/9.6k | 0.6635 | 116 | report rerun after cost-row grader fix (claude-opus-5-5) |
| 2026-10-18 05:09 | claude | claude-sonnet-5-5 | level3/report-1 | ✅ | 1 | 21 | 506.5k/8.3k | 0.3354 | 88 | report rerun after cost-row grader fix (claude-sonnet-5-5) |
| 2026-10-18 05:10 | claude | claude-haiku-5-5 | level3/report-1 | ✅ | 1 | 23 | 963.2k/32.3k | 0.1017 | 179 | report rerun after cost-row grader fix (claude-haiku-5-5) |
| 2026-10-18 05:13 | claude | claude-haiku-5-5 | level3/rooms-10 | ✅ | 1 | 17 | 628.5k/34.0k | 0.0384 | 159 | haiku rooms-10 rate |
| 2026-10-18 05:16 | claude | claude-haiku-5-5 | level3/rooms-10 | ✅ | 1 | 12 | 390.6k/42.1k | 0.0407 | 196 | haiku rooms-10 rate |
| 2026-10-18 05:19 | claude | claude-haiku-5-5 | level3/rooms-10 | ✅ | 1 | 16 | 677.5k/32.3k | 0.0380 | 155 | haiku rooms-10 rate |
| 2026-10-18 06:00 | claude | claude-opus-5-5 | level4/chain-full | ✅ | 3 | 43 | 2131.1k/32.4k | 1.8370 | 372 | level4 limit search (claude-opus-5-5) |
| 2026-10-18 06:06 | claude | claude-opus-5-5 | level4/chain-full | ✅ | 4 | 38 | 1424.3k/49.8k | 2.0812 | 496 | level4 limit search (claude-opus-5-5) |
| 2026-10-18 06:14 | claude | claude-opus-5-5 | level4/conflict-1 | ✅ | 0 | 5 | 176.6k/4.1k | 0.4154 | 47 | reported_conflict；未交付綠燈 build，回覆點出需求衝突（本題需求互相矛盾，停下回報為正確行為）；level4 limit search (claude-opus-5-5) |
| 2026-10-18 06:15 | claude | claude-opus-5-5 | level4/conflict-1 | ✅ | 0 | 6 | 210.3k/3.9k | 0.3784 | 45 | reported_conflict；未交付綠燈 build，回覆點出需求衝突（本題需求互相矛盾，停下回報為正確行為）；level4 limit search (claude-opus-5-5) |
| 2026-10-18 06:16 | claude | claude-opus-5-5 | level4/design-large | ✅ | 6 | 50 | 3890.8k/60.8k | 3.2753 | 642 | level4 limit search (claude-opus-5-5) |
| 2026-10-18 06:26 | claude | claude-opus-5-5 | level4/design-large | ❌ | 5 | 27 | 1987.1k/42.3k | 2.2210 | 451 | fail_reply；reply mentions_outflow_threshold: 回覆沒有提到（需符合 ['(?<![\\d.])1(?:\\.0+)?\\s*(?:ha\|公頃)', '一公頃', '10,?000\\s*(?:m²\|m2\|平方)', '；level4 limit search (claude-opus-5-5) |
| 2026-10-18 06:34 | claude | claude-opus-5-5 | level4/rooms-14 | ✅ | 1 | 14 | 877.8k/107.1k | 3.4134 | 1033 | level4 limit search (claude-opus-5-5) |
| 2026-10-18 06:51 | claude | claude-opus-5-5 | level4/rooms-14 | ✅ | 1 | 12 | 346.6k/61.0k | 1.9333 | 596 | level4 limit search (claude-opus-5-5) |
| 2026-10-18 07:01 | claude | claude-opus-5-5 | level4/rooms-18 | ✅ | 1 | 14 | 595.6k/29.3k | 1.2966 | 412 | level4 limit search (claude-opus-5-5) |
| 2026-10-18 07:08 | claude | claude-opus-5-5 | level4/rooms-18 | ✅ | 1 | 10 | 380.8k/34.8k | 1.2188 | 337 | level4 limit search (claude-opus-5-5) |
| 2026-10-18 07:07 | claude | claude-sonnet-5-5 | level4/chain-full | ✅ | 3 | 23 | 1414.0k/52.2k | 1.0872 | 433 | level4 limit search (claude-sonnet-5-5) |
| 2026-10-18 07:14 | claude | claude-opus-5-5 | level4/rooms-tight | ✅ | 1 | 15 | 409.1k/18.0k | 0.8249 | 179 | level4 limit search (claude-opus-5-5) |
| 2026-10-18 07:07 | claude | claude-haiku-5-5 | level4/chain-full | ✅ | 5 | 33 | 3723.9k/143.6k | 0.4799 | 710 | level4 limit search (claude-haiku-5-5) |
| 2026-10-18 07:17 | claude | claude-opus-5-5 | level4/rooms-tight | ✅ | 1 | 6 | 174.5k/16.2k | 0.6262 | 172 | level4 limit search (claude-opus-5-5) |
| 2026-10-18 07:14 | claude | claude-sonnet-5-5 | level4/chain-full | ✅ | 7 | 41 | 2702.4k/49.1k | 1.2368 | 514 | level4 limit search (claude-sonnet-5-5) |
| 2026-10-18 07:23 | claude | claude-sonnet-5-5 | level4/conflict-1 | ❌ | 3 | 19 | 541.7k/17.7k | 0.4816 | 162 | harness_blocked；評估環境拒絕了 build 指令：['cd work && sed -i \\\n -e \'s/Node(id="CB1".*/Node(id="CB1", kind="catch_basin", x=279670, y=2766214,；level4 limit search (claude-sonnet-5-5) |
| 2026-10-18 07:25 | claude | claude-sonnet-5-5 | level4/conflict-1 | ✅ | 0 | 11 | 223.2k/3.2k | 0.1960 | 34 | reported_conflict；未交付綠燈 build，回覆點出需求衝突（本題需求互相矛盾，停下回報為正確行為）；level4 limit search (claude-sonnet-5-5) |
| 2026-10-18 07:26 | claude | claude-sonnet-5-5 | level4/design-large | ❌ | 6 | 50 | 2826.4k/56.2k | 1.5096 | 498 | fail_reply；reply mentions_outflow_threshold: 回覆沒有提到（需符合 ['(?<![\\d.])1(?:\\.0+)?\\s*(?:ha\|公頃)', '一公頃', '10,?000\\s*(?:m²\|m2\|平方)', '；level4 limit search (claude-sonnet-5-5) |
| 2026-10-18 07:19 | claude | claude-haiku-5-5 | level4/chain-full | ❌ | 1 | 2 | 480.6k/427 | 0.7867 | 1312 | fail_verifier；缺少 'floor-plan' 的完成 build（本題需要 ['site-overlay', 'floor-plan', 'drainage-report'] 各一次）；level4 limit search (claude-haiku-5-5) |
| 2026-10-18 07:34 | claude | claude-sonnet-5-5 | level4/design-large | ✅ | 8 | 56 | 4315.9k/51.0k | 1.6731 | 489 | level4 limit search (claude-sonnet-5-5) |
| 2026-10-18 07:41 | claude | claude-haiku-5-5 | level4/conflict-1 | ❌ | 0 | 15 | 993.8k/45.5k | 0.0937 | 239 | harness_blocked；評估環境拒絕了 build 指令：["cat > /tmp/l4c1_check.py <<'EOF'\nimport csv, json\nfrom shapely.geometry import Polygon\nfrom drafta；level4 limit search (claude-haiku-5-5) |
| 2026-10-18 07:45 | claude | claude-haiku-5-5 | level4/conflict-1 | ❌ | 1 | 23 | 1464.1k/55.7k | 0.1560 | 275 | harness_blocked；評估環境拒絕了 build 指令：['rm -f work/conflict1.py && .venv/bin/draftagent build work/conflict1_drainage.py > /tmp/c1_build.txt ；level4 limit search (claude-haiku-5-5) |
| 2026-10-18 07:42 | claude | claude-sonnet-5-5 | level4/rooms-14 | ✅ | 1 | 23 | 2201.8k/123.8k | 2.1337 | 958 | level4 limit search (claude-sonnet-5-5) |
| 2026-10-18 07:49 | claude | claude-haiku-5-5 | level4/design-large | ❌ | 9 | 48 | 6106.3k/143.1k | 0.7765 | 711 | fail_reply；reply mentions_outflow_control: 回覆沒有提到（需符合 ['出流管制', '(?i)outflow[- ]control'] 任一）；level4 limit search (claude-haiku-5-5) |
| 2026-10-18 07:58 | claude | claude-sonnet-5-5 | level4/rooms-14 | ✅ | 8 | 17 | 1850.2k/94.0k | 1.6866 | 732 | level4 limit search (claude-sonnet-5-5) |
| 2026-10-18 08:01 | claude | claude-haiku-5-5 | level4/design-large | ❌ | 7 | 54 | 6370.4k/152.6k | 0.7898 | 806 | fail_reply；reply mentions_outflow_control: 回覆沒有提到（需符合 ['出流管制', '(?i)outflow[- ]control'] 任一）；level4 limit search (claude-haiku-5-5) |
| 2026-10-18 08:10 | claude | claude-sonnet-5-5 | level4/rooms-18 | ✅ | 1 | 24 | 1525.6k/74.7k | 1.3722 | 590 | level4 limit search (claude-sonnet-5-5) |
| 2026-10-18 08:20 | claude | claude-sonnet-5-5 | level4/rooms-18 | ✅ | 1 | 10 | 479.6k/61.1k | 1.0343 | 455 | level4 limit search (claude-sonnet-5-5) |
| 2026-10-18 08:14 | claude | claude-haiku-5-5 | level4/rooms-14 | ❌ | 0 | 2 | 394.0k/805 | 0.5641 | 1040 | no_output；工作區 runs/ 沒有完成的 build（manifest.json status=done）；level4 limit search (claude-haiku-5-5) |
| 2026-10-18 08:28 | claude | claude-sonnet-5-5 | level4/rooms-tight | ✅ | 1 | 9 | 333.8k/33.5k | 0.6007 | 273 | level4 limit search (claude-sonnet-5-5) |
| 2026-10-18 08:32 | claude | claude-sonnet-5-5 | level4/rooms-tight | ✅ | 2 | 8 | 328.9k/21.2k | 0.4350 | 171 | level4 limit search (claude-sonnet-5-5) |
