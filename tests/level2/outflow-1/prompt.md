# Level 2：outflow-1 出流管制計畫書水文水理計算（合成案例）

桃園某都市計畫區內 0.8 ha 新建集合住宅（**合成案例**，無真實案名 / 地號）。基地全部位於都市計畫區、重力排水、
工程師已確認聯外排水路通洪能力達 10 年重現期距。業主決定提送出流管制計畫書（第 2-1 條路徑由使用者決定）。

工程師提供的資料（示範站 Horner 係數、開發前 / 後土地利用與流路、滯洪池 H-A、出流工、外水位）見
`tests/level2/outflow-1/input/design.md`。**所有數值以 input/ 為準**；input/ 沒有的資料要停下來回報缺什麼，
不得自行假設或編造；不得自行選站或補 Horner 係數。

請寫 `OutflowControlSpec`，依 CLAUDE.md 流程以 `outflow-control` 圖種 `draftagent build` 產出
result.json、附圖 16 / 18 / 19（PNG + PDF）與 SWMM inp，並貼出完整 verifier 輸出。

通過標準：依檢核基準處理 CN 與集流時間下限；開發後出流 Q2/Q5/Q10 皆 ≤ 開發前；
提供體積 ≥ 10 年演算需求 × 安全係數；水量與單位歷線體積守恆；
verifier 綠燈（允許 verified: false、示範 Horner、第 2-1 條路徑、簡易檢核未選用等 WARNING）。
回覆須說明：第 2-1 條的其他可行路徑，以及若改用簡易檢核是否會通過。
回覆只能說「通過本 repo 現有 verifier 的檢查項目」，不得宣稱符合法規。
