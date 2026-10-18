# DraftAgent 開發計畫

> 對應 [PLAN.md](PLAN.md) 的執行層。PLAN 講「為什麼、做什麼」，這份講「怎麼做、做到什麼程度算完成」。

## 0. 開發環境

- Python ≥ 3.11，套件管理用 `uv`
- 核心相依：`ezdxf`、`geopandas`、`shapely`、`pyproj`、`rasterio`、`numpy`、`pydantic`
- 選用相依（extras）：`ifcopenshell`（`[ifc]`）、`pyswmm`（`[hydro]`）、`smlibpy`（`[solid]`）
- 測試：`pytest`；lint：`ruff`
- 兩種 agent 都要能跑：Claude Code 讀 `CLAUDE.md`，Codex 讀 `AGENTS.md`（內容相同，用 symlink）

```bash
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
pytest
```

## 1. 架構分層與模組職責

```
Agent（Claude Code / Codex）
   │  只做：讀需求 → 寫 spec（typed Python / JSON / YAML）→ draftagent build → 讀 verifier 結果 → 修 spec
   ▼
draftagent CLI        draftagent/cli.py（typer）：kinds / schema / new / build / verify / preview / check
 draftagent.build     build(spec) → runs/；load_spec(.json/.yaml/.py)
 registry + kinds/    圖種註冊：spec 型別 + render + verifiers + 範例（draftagent/registry.py、kinds/*.py）
tools/*.py            舊 CLI 入口（向後相容的薄 shim），不放邏輯
   ▼
draftagent/
  schema/             Pydantic model：所有 JSON spec 的唯一定義（SiteSpec, PlanSpec, ProfileSpec…）
  site/               地籍套疊、分區查詢、面積計算、CRS 正規化
  urban/              建蔽率 / 容積率 / 建築線 / 量體生成
  hydro/              DTM 讀取、坡度分級、集水區、縱斷面、斷面
  render/             spec → DXF（ezdxf）/ IFC（IfcOpenShell）/ GeoPackage
  verify/             所有 verifier；每個回傳 VerifyResult(ok, errors, warnings, metrics)
  data/               圖資下載與快取（data.gov.tw、NLSC WMTS）
```

**鐵律**
1. `render/` 和 `verify/` 不得 import LLM 相關程式碼，必須是純函式、可重現
2. 任何 generator 的輸入只能是 `schema/` 裡定義的 model，不接受自由文字
3. 每個 verifier 必須可以獨立執行：`python -m draftagent.verify.<name> <file>`
4. 法規數值（容積率表、坡度分級門檻）放在 `draftagent/urban/codes/*.yaml` 與 `hydro/codes/*.yaml`，附來源與日期，不寫死在程式裡

## 2. Milestones

### M0：骨架（本次）
- [x] 目錄結構、`pyproject.toml`、`CLAUDE.md` / `AGENTS.md`
- [x] skills 佔位檔
- [x] CI：ruff + pytest（GitHub Actions，`.github/workflows/ci.yml`）

### M1：資料與 verifier 基礎（對應 Roadmap W1）
- [ ] `data/`：下載一幅 20 m DTM、一個段的地籍 SHP、該區使用分區圖，寫快取（目前只有空模組；範例與考題圖資為 `tools/make_examples.py` / `tools/make_fixtures_*.py` 產生的合成資料）
- [x] `verify/crs_check.py`：任何向量 / 栅格進來先驗 CRS，非 3826 報錯並建議轉換
- [x] `verify/area_check.py`：面積對帳，誤差門檻 0.5%（另含 `check_overlay` 獨立重新疊圖對帳）
- [x] `schema/site.py`：`SiteSpec`（地號清單、CRS、分區）
- [x] 第一個 skill：`tw-gis`（圖資來源、欄位、CRS 陷阱）

**DoD**：agent 拿到一組地號，能產出統一 3826 的 GeoPackage，兩個 verifier 綠燈。

### M2：Level 1 全過（W2）
- [x] 1-1 `schema/plan.py` + `render/dxf.py` + `verify/dxf_check.py`
- [x] 1-2 `site/overlay.py`
- [x] 1-3 `hydro/slope.py` + `hydro/codes/slope_classes.yaml`（水保法分級；`verified: false`）
- [x] `tests/level1/` 三題 fixtures + expected
- [x] skill：`dxf-arch`（圖層命名、線型、標註樣式）
- [x] 三題 pass rate 已記錄於 `docs/RESULTS.md`

**DoD**：三題 pass rate 記錄進 `docs/RESULTS.md`，含修正輪數與 token 成本。

### M3：Level 2-1 建蔽率檢核（W3）
- [ ] `urban/codes/zoning_tw.yaml`：先放一個縣市的住宅區管制（來源：該縣市都市計畫土地使用分區管制要點）
- [ ] `urban/coverage.py`：建蔽率、容積率、可建面積、樓地板上限
- [ ] `urban/massing.py`：量體生成 → DXF
- [ ] `verify/zoning_check.py`：量體投影 ≤ 可建面積、樓地板 ≤ 容積上限
- [x] skill：`tw-building-code`（查表規則與缺值處理；`urban/codes/` 目前沒有任何 yaml）

**DoD**：給地號 + 分區，產出檢核報告 + 量體 DXF，verifier 綠燈。這是第一個可拿給 SME 看的 demo。

### M4：Level 2-2、2-3（W4）
- [ ] `hydro/profile.py`：LineString + DTM → 樁號表 + 縱斷面 DXF（部分：已有 `drainage-profile` 圖種——樁號 / 高程由 spec 或排水網路給定，尚未由 DTM 取地面線）
- [ ] `site/hillside.py`：山坡地範圍套疊、分區規範
- [ ] `verify/source_check.py`：擋掉把淹水潛勢圖當法規依據的推論

已完成（排水方向，Level 2 題組 `tests/level2/`）：
- [x] `drainage-calc-ty`：桃園建築基地流出抑制計算（`hydro/taoyuan.py` + `verify/drainage_calc_check.py`）
- [x] `outflow-control`：出流管制水文水理（`hydro/outflow.py` + SWMM inp + `verify/outflow_check.py`）
- [x] `drainage-profile` / `drainage-detail`：縱斷面與各部詳圖 DXF + verifier
- [x] `drainage-plan`：排水網路 → 整套圖 + 計算（`drainage/derive.py` 同源衍生、`sheetset_check` 跨圖一致性）
- [x] verifier false-negative 稽核與修補（見 [VERIFIERS.md](VERIFIERS.md)「稽核修補紀錄」）

### M5：Level 3 探索（W5+）
- 挑 3-1 或 3-4 其中一題往下挖，找到 pass rate 明顯下降的點
- 3-2 建 fixtures（手繪草圖照片 + 標準答案），每半年重測

## 3. 測試與評估

```
tests/
  level1/  level2/  level3/
    <task-id>/
      input/        # spec 或原始圖資
      expected/     # 標準答案（DXF 屬性、面積、分級統計）
      prompt.md     # 給 agent 的自然語言需求
```

- `pytest tests/` 跑的是 **verifier 本身** 的單元測試（確定性）
- `tools/eval.py <level>` 跑的是 **agent 評估**：餵 prompt → agent 產 spec → render → verify，記錄 pass / 輪數 / token
- 結果寫入 `docs/RESULTS.md`，每次評估一列，含模型名稱與日期

## 3.1 verifier 設計原則

各 verifier 的檢查項目與已知限制見 [VERIFIERS.md](VERIFIERS.md)。新增或修改 verifier 時遵守：

1. **不信任中間檔**：render 寫出的衍生 spec / result.json / calc.md / CSV / summary.json 都是待驗物，不是答案。
   verifier 由使用者 spec 重新衍生、重新計算，與產物逐鍵 / 逐字比對（例：drainage-plan 的 `compare_intermediate`、
   site-overlay 的 `check_overlay` 獨立重新疊圖）。判定一律用重算值，不用產物裡的數字
2. **讀實際顯示值，不推算**：圖面上人看到的是什麼就驗什麼。DIMENSION 讀 geometry block 的實際文字（`verify/dimtext.py`），
   禁用會改變顯示值的 dimrnd / dimpost / dimalt；數字以完整數字比對（「45000」不等於 4500）
3. **幾何與文字都要驗**：只驗標註文字會漏掉「標註對、線畫錯」。牆多邊形對稱差、門弧圓心 / 半徑、縱斷面 H-PROF 線 y 回推 EL、
   高程尺與樁號欄位置、排水平面 C-STRM 端點與流向箭頭、詳圖本體 bbox 與 ▽ 位置都由 spec 獨立推導後比對
4. **法規路徑鎖定**：法規數值只從 `draftagent/*/codes/*.yaml` 讀；spec 指定的 yaml 路徑 ≠ repo 預設 → error，
   verifier 一律以預設檔重算（`verify/codes_lock.py`）
5. **物理合理性**：數字自洽之外還要合乎物理——覆土 ≥ 0、水往低處流（順坡、受納溝不逆坡、放流管不低於受納水位）、
   構造物在基地內、滯洪池不壓建物、出流口須接到受納溝；關鍵檢查因資料不足而無法執行時（例：出流口未接受納溝 → E1–E4 無法檢查）
   報 error，不以 WARNING 放行
6. **可讀性**：印出字高 ≥ `layers.yaml` `text.min_print_height_mm`、文字不重疊（依 viewport 比例與凍結圖層換算到圖紙）、
   文字壓線 warning；預覽 PNG 仍要人看
7. **獨立實作交叉驗證**：關鍵計算（例：管渠上游加權 C 與 Q）在 verifier 另寫一份不經共用模組的實作
8. **每個修補都要有紅燈 fixture**：竄改產物或 spec 後確認 verifier 會紅；門檻只能收緊，不得為了讓測試過而放寬

## 4. 開發慣例

- Branch：`feat/<module>-<short>`、`fix/…`、`docs/…`；main 保護
- Commit：Conventional Commits（`feat:`、`fix:`、`docs:`、`test:`、`chore:`）
- 單位：每個圖種在 spec 宣告 `units`：建築平面 / 詳圖 / 縱斷面圖紙 = **mm**（$INSUNITS 4）；GIS 對位的平面圖（集水區、排水平面、地籍套繪）= **m**（$INSUNITS 6，TWD97 TM2）；verifier 以 `draftagent.units.check_insunits` 檢查一致。GIS 一律 **m**；面積報表用 m²，另附坪（× 0.3025）
- 圖層命名：AIA 風格前綴，`A-WALL`、`A-DOOR`、`A-DIMS`、`S-GRID`、`C-TOPO`、`C-ROAD`、`H-PROF`
- 法規檔案每個數值都要有 `source:` 與 `as_of:` 欄位
- spec 由 `draftagent build` 自動存檔到 `runs/<ts>-<kind>[-name]/spec.json`，方便事後 replay；agent 暫存的 spec 放 `work/`（gitignore）

## 4.1 如何新增一個圖種

1. **Spec**：`draftagent/schema/<x>.py` 定義 Pydantic model（`extra="forbid"`；有 DXF 的圖種加 `units: Literal["mm"]` 或 `Literal["m"]`；每個欄位寫 `description`，它會出現在 `draftagent schema` 與錯誤提示裡）。法規數值只從 `codes/*.yaml` 讀，在 validator 內檢查
2. **render**：`draftagent/render/<x>.py`（或領域模組）寫純函式 `write_<x>(spec, path)`；不得呼叫 LLM、不得有隨機性
3. **verifier**：`draftagent/verify/<x>_check.py` 回傳 `VerifyResult`，並提供 `python -m draftagent.verify.<x>_check` 入口；要有「故意錯的 fixture」證明它會抓到錯。有 DXF 時另檢查 `$INSUNITS` 與 spec.units 一致（可直接用 `kinds/_util.insunits_verifier`）
4. **註冊**：`draftagent/kinds/<x>.py`

   ```python
   from draftagent.kinds._util import example, insunits_verifier, need, spec_file
   from draftagent.registry import Kind, register

   def render(spec, out_dir):            # -> {"dxf": Path, ...}；產物一律寫進 out_dir
       return {"dxf": write_x(spec, out_dir / "x.dxf")}

   def verify_x(spec, artifacts):        # VerifyFn(spec, artifacts) -> VerifyResult
       p = need(artifacts, "dxf", "x_check")
       if not isinstance(p, Path):
           return p
       with spec_file(spec, artifacts) as sp:   # 既有 verifier 吃 spec 檔路徑時用
           return check_x(p, sp)

   KIND = register(Kind(name="x", spec=XSpec, render=render,
                        verifiers=(verify_x, insunits_verifier),
                        summary="一行說明", example=example("tests/levelN/x-1/input/spec.json")))
   ```

   `draftagent/kinds/` 下的模組會被 `registry` 自動 import；重型相依放在函式內 import
5. **example**：`tests/levelN/<task>/input/spec.json`（+ `prompt.md`、`expected/`）。`draftagent new x` 由它產生 Python 範例
6. **tests**：verifier 單元測試（含紅燈 fixture）；`tests/test_build_cli.py` 會自動對每個已註冊圖種跑 `new` round-trip 與 `build` 範例並要求全綠

## 4.2 build 產物目錄

```
runs/<YYYYmmdd-HHMMSS>-<kind>[-name]/
  spec.json        spec 完整 dump（model_dump，indent 2、不轉義中文）；可 replay
  manifest.json    kind、spec 類別、schema_sha256、draftagent 版本、git commit/dirty、artifacts、紅綠燈
  verify.json      所有 VerifyResult（機器可讀）
  verify.txt       所有 VerifyResult.render()（貼給人看）
  render_error.txt render 失敗時的 traceback（此時 verify.json 有一筆 name=render 的紅燈）
  <產物>           plan.dxf / profile.dxf / site.gpkg / slope_classes.tif / result.json / calc.md …
  <stem>.png       有 DXF 且未加 --no-preview 時的預覽（失敗只記 warning；DPI 自動壓到長邊 ≈ 4000 px）
```

GIS 類圖種（site-overlay、slope）的輸出路徑在 build 時一律改寫到 run 目錄（檔名沿用 spec），存檔的 spec.json 保留原值。

## 5. 風險與對策

| 風險 | 對策 |
|---|---|
| 圖資中文欄名、TWD67/97 混用吃掉大量時間 | M1 就把清理邏輯做進 `data/`，之後不再碰 |
| 法規表整理工作量大 | 先一個縣市、一種分區；結構對了再擴 |
| verifier 太鬆讓錯誤過關 | 每個 verifier 要有「故意錯的 fixture」，確認它會抓到 |
| agent 繞過 verifier 直接宣稱完成 | CLAUDE.md 明訂：沒有 verifier 輸出不算完成 |
