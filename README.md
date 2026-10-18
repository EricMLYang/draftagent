# draftagent
Agent-operable CAD drafting for Taiwan land development, urban renewal, cadastral and hydraulic planning documents

用 typed Python（或 JSON / YAML）寫 spec → `draftagent build` 一次完成產圖與全部 verifier，產物與 spec.json 自動存到 `runs/`。

```bash
uv venv && uv pip install -e ".[dev]"     # 安裝後 .venv/bin/draftagent 可用
draftagent check                           # 環境自檢（套件、CJK 字型、選用相依）
draftagent kinds                           # 有哪些圖種
draftagent new plan -o work/plan.py        # 產生可執行的 Python 範例 spec，照需求修改
draftagent build work/plan.py              # render → verifier → runs/<ts>-plan-…/
open runs/*-plan*/plan.png                 # 看預覽；verifier 完整輸出在 verify.txt
```

```python
from draftagent import build, PlanSpec, Room, Rect
print(build(PlanSpec(wall_thickness=200, rooms=[Room(name="房間", rect=Rect(width=6000, depth=4000))])))
```

| 圖種 | Spec | 產出 | 單位 |
|---|---|---|---|
| `plan` | `PlanSpec` | 建築平面圖 DXF + PNG | mm |
| `drainage-profile` | `ProfileSpec` | 排水縱斷面圖 DXF（含圖框圖簽）+ PNG | 圖紙 mm |
| `drainage-detail` | `DetailSheetSpec` | 排水系統各部詳圖 DXF（U 溝 / 筏基滯洪 / 放流井 / 孔口板 / 出水口 / 銘牌） | 圖紙 mm |
| `drainage-plan` | `DrainageNetworkSpec` | 整套排水圖：平面設計圖 + 集水區分析圖 + 主幹 / 出水側縱斷面 + 詳圖 + 流出抑制計算（同一模型、跨圖一致性檢查） | m（TWD97） |
| `drainage-calc-ty` | `TaoyuanSiteDrainageSpec` | 桃園流出抑制計算 result.json + 計算表 md | m / m² |
| `outflow-control` | `OutflowControlSpec` | 出流管制水文水理 result.json + 附圖 16/18/19 + SWMM inp | m / m³/s |
| `site-overlay` | `SiteSpec` | 地號 × 分區套疊 GeoPackage + 面積表 | m（EPSG:3826） |
| `slope` | `SlopeSpec` | 坡度分級 GeoTIFF / GPKG / CSV | m（EPSG:3826） |

欄位查詢：`draftagent schema <kind>`。Agent 作業規則見 [CLAUDE.md](CLAUDE.md)，完整計畫見 [docs/PLAN.md](docs/PLAN.md)、開發方式見 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)。
每個圖種跑哪些 verifier、檢查什麼與已知限制見 [docs/VERIFIERS.md](docs/VERIFIERS.md)；agent 評估方式見 [docs/EVAL.md](docs/EVAL.md)。
綠燈只代表「通過本 repo 現有 verifier 的檢查項目」，不代表符合法規。

## 評估結果

2026-10-18 以 Opus 5.5 / Sonnet 5.5 / Haiku 5.5 跑 18 題（Level 1–3），最新有效結果 70/72 pass；15 次非 pass 中 14 次是題目、harness 或評分的問題，只有 1 次（Haiku 10 房配置）是模型能力。矩陣、失敗根因與出題原則見 [docs/RESULTS.md](docs/RESULTS.md)。

## 圖庫

由 `examples/*.json` build 出的預覽（全部為合成範例資料；重新產生：`.venv/bin/python scripts/make_gallery.py`）。

| | |
|---|---|
| **plan** 建築平面圖<br>[![plan](docs/gallery/plan.png)](docs/gallery/plan.png) | **drainage-profile** 排水縱斷面圖<br>[![drainage-profile](docs/gallery/drainage-profile.png)](docs/gallery/drainage-profile.png) |
| **drainage-plan** 平面設計圖<br>[![drainage-plan 平面設計圖](docs/gallery/drainage-plan-design.png)](docs/gallery/drainage-plan-design.png) | **drainage-plan** 集水區分析圖<br>[![drainage-plan 集水區分析圖](docs/gallery/drainage-plan-catchment.png)](docs/gallery/drainage-plan-catchment.png) |
| **drainage-detail** 排水系統各部詳圖<br>[![drainage-detail](docs/gallery/drainage-detail.png)](docs/gallery/drainage-detail.png) | **site-overlay** 地號 × 使用分區（GeoPackage）<br>[![site-overlay](docs/gallery/site-overlay.png)](docs/gallery/site-overlay.png) |
| **slope** 坡度分級（GeoPackage）<br>[![slope](docs/gallery/slope.png)](docs/gallery/slope.png) | |
