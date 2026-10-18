# Level 2：drainage-ty-1 桃園建築基地流出抑制設施檢核

桃園區某 1,680 m² 集合住宅基地（合成資料），筏基滯洪，重力排放，依桃園逕流量標準檢核。

工程師的設計參數（基地、土地使用、筏基坑格、有效水深、孔口、溢流堰、管渠）見
`tests/level2/drainage-ty-1/input/design.md`。**所有設計數值以 input/ 為準**；input/ 沒有的資料要停下來
回報缺什麼，不得自行假設或編造（雨量站由 yaml 決定，不要自己填站名）。

請寫 `TaoyuanSiteDrainageSpec`，依 CLAUDE.md 流程以 `drainage-calc-ty` 圖種 `draftagent build` 產出
result.json 與計算表（m² 另附坪），並貼出完整 verifier 輸出。

通過標準：提供貯集量 ≥ 必要貯集量；孔口排放量在容許排放量的規定範圍內；verifier 綠燈（允許 verified: false WARNING）。
回覆只能說「通過本 repo 現有 verifier 的檢查項目」，不得宣稱符合法規。
