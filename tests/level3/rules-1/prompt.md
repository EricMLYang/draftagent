# Level 3 rules-1：筏基滯洪流出抑制檢核（含審查意見與業主要求）

桃園區某集合住宅基地（**合成資料**），筏基滯洪、重力排放，依桃園逕流量標準檢核。

工程師的設計參數（基地、土地使用、筏基坑格、有效水深、孔口、溢流堰、管渠）與最近收到的審查意見、業主要求，
都在 `tests/level3/rules-1/input/design.md`。**所有設計數值以 input/ 為準**；input/ 沒有的資料要停下來回報缺什麼，
不得自行假設或編造（雨量站由 yaml 決定，不要自己填站名）。

請寫 `TaoyuanSiteDrainageSpec`（`draftagent/schema/drainage.py`），用 `draftagent build` 產出 result.json 與計算表，
貼出完整 verifier 輸出。design.md 末段的審查意見與業主要求請一併處理，並在回覆**逐項說明**你怎麼處理。

通過標準：verifier 綠燈（允許 yaml verified: false WARNING）。
