# Level 4 design-large：集合住宅社區排水系統設計（1.2 ha，從零設計）

桃園區某集合住宅社區基地（**合成資料**，座標為 TWD97 TM2 虛構位置，非真實基地；不含任何真實案名、地號、公司名）。
本案沒有既有排水設計：你要**自己設計**集水分區、集水井 / 人孔 / 進流井 / 放流井 / 出流口的位置與底高、
管徑與坡度、滯洪設施型式與尺寸、排放（孔口或抽水）與溢流，並出整套排水圖。

原始資料與工程師的設計條件在 `tests/level4/design-large/input/`（`design.md` 有清單、開發概要與設計條件）：
`site.geojson`（基地、建物、地表分類、筏基可用範圍）、`ground_points.csv`（地面高程測點）、`existing_drain.csv`（受納溝）、
`road_center.csv`、`design.md`。

**測量值、受納溝資料與工程師條件以 input/ 為準**；設計決定（位置、底高、尺寸）由你決定，但必須滿足 design.md 的
設計條件與本 repo verifier。資料不足以設計時要停下來回報缺什麼，不得自行假設或編造測量值。

請寫 `DrainageNetworkSpec`（`draftagent/drainage/model.py`），**不得填長度、坡度、面積、流量**；
以 `drainage-plan` 圖種一次產出平面設計圖、集水區分析圖、衍生縱斷面與衍生流出抑制計算，並貼出完整 verifier 輸出。
回覆依 design.md「回覆要求」說明。

通過標準：verifier 全部綠燈（允許 yaml verified: false 等 WARNING），且符合 design.md 的設計條件。
回覆只能說「通過本 repo 現有 verifier 的檢查項目」，不得宣稱符合法規。
