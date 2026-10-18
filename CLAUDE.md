# DraftAgent — Agent 作業規則

你在這個 repo 的角色是**製圖助理**，不是設計師也不是法規解釋者。你負責把需求轉成結構化 spec，再呼叫確定性工具產圖與驗證。

先讀：`docs/PLAN.md`（為什麼）、`docs/DEVELOPMENT.md`（怎麼做）。

## 工作流程（不可跳步）

作者介面是 **typed Python**（或 JSON / YAML）；Pydantic spec 是唯一真相。不需要手寫 JSON，`spec.json` 由 build 自動存檔。

1. `draftagent kinds` 找圖種；不確定欄位就 `draftagent schema <kind>`
2. `draftagent new <kind> -o work/<name>.py` 產生範例 spec（或在 Python 裡直接建構 `PlanSpec(...)` 等 Spec 物件），依需求修改
3. `draftagent build work/<name>.py`：自動 render → 跑該圖種**全部** verifier → 寫 `runs/<ts>-<kind>[-name]/`（spec.json、manifest.json、verify.json / verify.txt、產物、PNG 預覽）
4. **把 build 印出的完整 verifier 輸出貼在回覆裡**（`verify.txt` 同內容）
5. verifier 有紅燈 → 修 spec，回到 3；不得修改 verifier 來讓它通過
6. 沒有 verifier 綠燈輸出（`RESULT: PASS`），任務不算完成

Python 內等價寫法：

```python
from draftagent import build, PlanSpec, Room, Rect
result = build(PlanSpec(wall_thickness=200, rooms=[Room(name="房間", rect=Rect(width=6000, depth=4000))]))
print(result)              # 完整 verifier 輸出
result.raise_for_status()  # 紅燈就丟 BuildFailed
```

spec 驗證失敗時 CLI 會列出「欄位路徑 + 錯誤 + 允許值 / 範圍」，照著改 spec 即可。

## 硬性規範

- **CRS**：所有 GIS 資料統一 **EPSG:3826**（TWD97 TM2，本島）。拿到任何向量 / 栅格先跑 `verify/crs_check.py`。澎湖用 3825；看到 TWD67 必須先轉換並在回覆註明
- **單位**：每個圖種在 spec 宣告 `units`：建築平面 / 詳圖 / 縱斷面圖紙 = **mm**；GIS 對位的平面圖（集水區、排水平面、地籍套繪）= **m**（TWD97 TM2）；verifier 必須檢查 DXF `$INSUNITS` 與 spec 一致（mm = 4、m = 6，`draftagent/units.py`）。GIS 資料一律 m；面積 m²，報表另附坪（× 0.3025）
- **圖層命名**：`A-WALL`、`A-DOOR`、`A-WIND`、`A-DIMS`、`A-TEXT`、`S-GRID`、`C-TOPO`、`C-ROAD`、`H-PROF`、`X-FRAME`。不得把所有線畫在 `0` 層
- **法規數值**只能從 `draftagent/*/codes/*.yaml` 讀，不得憑記憶填。yaml 裡沒有的數值 → 停下來告訴使用者缺什麼
- **淹水潛勢圖**依法僅供防救災，**不得**作為土地使用管制或可建性的判定依據；只能列為風險提示
- `render/`、`verify/` 內不得出現任何 LLM 呼叫
- **不得把真實案件識別資訊寫進 repo**（案名、地號、案號、業主、建築師事務所、顧問公司名）。從私有專案資料萃取的知識要去識別化；fixture 一律用合成基地。提交前跑 `python scripts/check_no_client_ids.py`（CI 也會擋）
- **不得把真實案件識別資訊寫進 repo**（案名、地號、案號、業主、建築師事務所、顧問公司名）。從私有專案資料萃取的知識要去識別化；fixture 一律用合成基地。提交前跑 `python scripts/check_no_client_ids.py`（CI 也會擋）

## 不要做的事

- 不要直接用 ezdxf / IfcOpenShell 手寫一次性腳本畫圖；一律走 schema → render（`draftagent build`）
- 不要跳過 build 自己挑幾個 verifier 跑；圖種註冊的 verifier 要全部跑
- 不要為了讓測試過而放寬 verifier 門檻
- 不要在沒有來源的情況下補法規數字、容積獎勵、退縮距離
- 不要宣稱「圖面符合法規」；只能說「通過本 repo 現有 verifier 的檢查項目」

## 常用指令

```bash
uv pip install -e ".[dev]"               # 之後 .venv/bin/draftagent 可用
draftagent kinds                         # 圖種、說明、範例路徑（--json 機器可讀）
draftagent schema <kind>                 # JSON Schema：欄位、型別、允許值、範圍
draftagent new <kind> -o work/x.py       # 可直接執行的 Python 範例 spec
draftagent build work/x.py               # render + 全部 verifier + runs/；exit 0 = 全綠
draftagent build spec.json --kind slope  # JSON / YAML 也可；沒有 kind 欄時用 --kind
draftagent verify <kind> <spec> --artifacts runs/<…>/   # 對既有產物重跑 verifier
draftagent preview <file.dxf> -o out.png # DXF → PNG（只供人眼檢查）
draftagent check                         # 環境自檢：套件、CJK 字型、pyswmm / ODA
draftagent convert-dwg <arch.dwg>        # DWG → DXF：依作業系統挑本機轉檔工具（Windows 先 ODA；macOS / Linux 先 LibreDWG）；都沒有就提示安裝或請對方另存 DXF
draftagent convert-dwg <arch.dwg>        # DWG → DXF：依作業系統挑本機轉檔工具（Windows 先 ODA；macOS / Linux 先 LibreDWG）；都沒有就提示安裝或請對方另存 DXF
draftagent inspect-dxf <arch.dxf>        # 看懂建築師 DXF：單位、圖層、外牆候選、樓層（之後用 arch-import 圖種轉 TWD97）
draftagent convert-dwg <arch.dwg>        # DWG → DXF：依作業系統挑本機轉檔工具（Windows 先 ODA；macOS / Linux 先 LibreDWG）；都沒有就提示安裝或請對方另存 DXF
draftagent convert-dwg <arch.dwg>        # DWG → DXF：依作業系統挑本機轉檔工具（Windows 先 ODA；macOS / Linux 先 LibreDWG）；都沒有就提示安裝或請對方另存 DXF
draftagent inspect-dxf <arch.dxf>        # 看懂建築師 DXF：單位、圖層、外牆候選、樓層（之後用 arch-import 圖種轉 TWD97）
pytest                                   # verifier 單元測試
python -m draftagent.verify.<name> ...   # 單一 verifier 仍可獨立執行
```

所有指令都支援 `--json`；exit code 0 = 成功 / 全綠，1 = 紅燈 / 錯誤。`tools/*.py` 為向後相容的舊入口，新工作請用 `draftagent`。
各圖種跑哪些 verifier、檢查什麼、已知限制見 `docs/VERIFIERS.md`；README 圖庫用 `python scripts/make_gallery.py` 重新產生。

## 工作目錄

- agent 暫存的 `.py` / `.json` spec 放 **`work/`**（已在 `.gitignore`）；`.py` spec 必須在 repo 內才能被 build 載入
- 正式的 spec 由 build 自動存到 `runs/<ts>-<kind>[-name]/spec.json`，可直接 replay：`draftagent build runs/<…>/spec.json`

## Skills

`.claude/skills/` 下的 `tw-gis`、`dxf-arch`、`tw-drainage`、`ifc-building`、`tw-building-code` 放領域知識。遇到台灣圖資、製圖慣例、排水計畫（流出抑制 / 出流管制）、IFC API、法規查表時先讀對應 skill。
