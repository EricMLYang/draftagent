---
name: tw-building-code
description: 台灣法規數值（坡度分級、建蔽率、容積率、退縮等）的查表規則。任何要用到法規門檻或寫進報告的法規數字時先讀：數值只能從 draftagent/*/codes/*.yaml 讀，yaml 格式（source / as_of / verified）、缺值時怎麼辦、淹水潛勢圖不得作為管制依據、目前已有與缺少的 yaml 清單。
---

# tw-building-code：法規數值查表規則

## 鐵律

1. **法規數值只能從 `draftagent/*/codes/*.yaml` 讀**。程式裡不得寫死門檻；agent 不得憑記憶填數字。
2. yaml 裡沒有的數值（某縣市分區、容積獎勵、退縮距離…）→ **停下來告訴使用者缺什麼**，不要補。
3. 報告只能說「通過本 repo 現有 verifier 的檢查項目」，**不得**宣稱「符合法規」。
4. **淹水潛勢圖依法僅供防救災，不得作為土地使用管制或可建性判定依據**，只能列為風險提示。

## yaml 格式

```yaml
verified: false          # 頂層：是否已由人工對照法規原文
note: "…"                # verified: false 時說明狀況
verified_by: null        # 人工確認後填
verified_on: null
<條目>:
  - …數值欄位…
    source: 法規名稱（含條號）   # 每個數值條目必填
    as_of: "YYYY-MM-DD"         # 法規版本日期；必填
```

- `verified: false` → 對應 verifier 一律輸出 **WARNING**（不是 error），回覆時必須把這個 warning 轉告使用者。
- 邊界歸屬（例如剛好 5% 屬哪一級）寫成 yaml 參數，不寫在程式裡。
- 修改 yaml 數值必須附原文出處；不得為了讓 verifier 過而改 yaml。

## 目前已有

| yaml | 內容 | 狀態 |
|---|---|---|
| `draftagent/hydro/codes/slope_classes.yaml` | 山坡地土地可利用限度分類標準 坡度分級（一～六級坡，%），`boundary` 參數 | **verified: false**（agent 草擬，待對照原文；邊界歸屬待確認） |

注意：法定坡度計算為「坵塊圖法」，`hydro/slope.py` 是 DTM 逐像元 Horn 法，兩者結果不同；
目前的坡度統計僅供規劃參考。

## 目前缺少（遇到就停下來問）

- `draftagent/urban/codes/zoning_tw.yaml`：各縣市都市計畫土地使用分區管制要點（建蔽率、容積率）— M3
- 容積獎勵、退縮距離、建築線：無
- 山坡地開發相關規範（水土保持技術規範等）：無
- 道路縱坡上限：無（Level 2-3 的 8% 為測試題目設定，非法規查表結果）
