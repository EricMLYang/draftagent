# Level 2 profile-ty-1：桃園集合住宅 U1 排水幹線縱斷面圖

桃園某集合住宅基地（合成資料），U1 排水幹線由集水井 CP1 經 MH1、MH2 至放流井，再接道路側溝；
畫排水縱斷面圖，水平 1/200 垂直 1/50。

原始資料在 `tests/level2/profile-ty-1/input/`：

- `survey.csv`：測量成果（點號、樁號、種類、地面高、溝底高、溝頂高、道路中心樁高）
- `design.md`：工程師的設計決定（各段斷面、進水管 / 放流管 / 溢流 / 設計水位高程、比例、圖號、圖框資料）

**所有測量與設計數值以 input/ 為準**；input/ 沒有的資料要停下來回報缺什麼，不得自行假設或編造。
請把原始資料轉成 `ProfileSpec`（`draftagent/schema/profile.py`），用 `drainage-profile` 圖種
`draftagent build` 產出 DXF，並貼出 `profile_check` 的完整輸出。坡度、距離不要填，由程式依溝底高程與樁號計算。

完成標準：`profile_check` 綠燈。回覆只能說「通過本 repo 現有 verifier 的檢查項目」。
