"""排水計畫書本文（drainage-report）：重用 drainage-plan 整套圖與計算，組成 report.md + report.docx。

- `model.py`：DrainageReportSpec（DrainageNetworkSpec + 計畫書專屬欄位）
- `vocab.py`：章節 / 統一圖名（讀 codes/report_format.yaml）、表名、「=」標籤、固定句
- `content.py`：spec + result.json + 附圖 → 區塊序列（所有數字由程式帶入）
- `figures.py`：DXF 圖簽 → 附圖 PNG
- `writers.py`：區塊 → report.md / report.docx

verifier：`draftagent/verify/report_check.py`。render / verify 不得呼叫 LLM。
"""
