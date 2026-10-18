---
name: ifc-building
description: 多房間平面圖 → IFC4（floor-plan 圖種）：FloorPlanSpec 寫法、IFC 實體對應（IfcSpace / IfcWall / IfcDoor / IfcWindow / 開口關係）、ifcopenshell 讀回驗證要點。要產出或檢查 IFC、BIM 空間、多房間配置時先讀。
---

# ifc-building：floor-plan → DXF + IFC4

流程固定：寫 `FloorPlanSpec`（`draftagent/schema/floorplan.py`）→ `draftagent build spec.json`
→ 一次產出 `floorplan.dxf`（mm）與 `floorplan.ifc`（IFC4），跑 5 個 verifier。
**不要**用 IfcOpenShell / ezdxf 手寫一次性腳本建模；IFC 由 `render/ifc.py` 從同一份 spec 產生。
需要選用相依：`uv pip install -e ".[ifc]"`（ifcopenshell）。範例：`draftagent new floor-plan`。

## 幾何約定（最容易錯的地方）

- `outline` = **外牆外緣**（總尺寸）。外牆由外緣往內 `wall_thickness.exterior`，內緣圍出「室內範圍」。
- **房間多邊形以外牆內緣、內牆中心線為界**，必須**鋪滿**室內範圍、互不重疊。
  例：外框 W × D、外牆厚 te → 室內範圍 x ∈ [te, W − te]、y ∈ [te, D − te]；兩房相鄰的那條邊就是內牆中心線。
  走廊、玄關也是房間（要列出來），否則會報「未分配」。
- 共用牆自動產生、只畫一次：任兩房共用的邊界 → 一道內牆（中心線兩側各半內牆厚）。
- 面積一律**淨面積** = 房間多邊形 − 半道內牆（貼外牆的邊不扣，因為房間本來就從外牆內緣算起）。
  例：矩形房間兩邊貼外牆、兩邊是內牆 → 淨面積 = (w − ti/2) × (d − ti/2)。DXF 面積文字、IFC NetFloorArea、
  verifier metrics 都用這個數。設計時先用淨面積反推多邊形尺寸。
- 單位：mm；面積 m²（報表另附坪 × 0.3025，verifier metrics 有 `area_ping`）。

## FloorPlanSpec 欄位

| 欄位 | 說明 |
|---|---|
| `outline` | `{rect: {x, y, width, depth}}` 或 `{polygon: [[x, y], …]}`（外牆外緣） |
| `wall_thickness` | `{exterior, interior}` mm |
| `rooms[]` | `name`（唯一）、`use`（居室 / 臥室 / 客廳 / 餐廳 / 書房 / 廚房 / 浴廁 / 走廊 / 玄關 / 儲藏 / 其他）、`rect` 或 `polygon` |
| `doors[]` | `id`、`between: [房, 房 或 "outside"]`、`width`；選填 `side`（N/E/S/W：由第一個房往另一側的方位）、`segment`、`offset`（省略 = 置中）、`swing_into`（預設第一個房）、`hinge` |
| `windows[]` | `id`、`room`、`width`；選填 `side`、`segment`、`offset`（只能在外牆） |
| `entrance` | 主入口門 id（必須是通往 `"outside"` 的門） |
| `dimensions` | `auto_overall`、`room_chains`（尺寸鏈到內牆中心線）、`offset`、`text_height` |
| `print_scale` | 出圖比例分母（預設 100）；字高依此換算 |
| `ifc` | `wall_height` / `door_height` / `window_sill` / `window_height` / `storey_name`——**示意高度**，不是法規數值 |

門窗定位：程式自動找兩空間的共用直線牆段；`side` 篩方位，候選依起點 (x, y) 排序、`segment` 選索引、省略取最長段；
牆段起點 = 非垂直段的西端、垂直段的南端；`offset` 自起點量。開口須離段端 ≥ 該牆厚（門寬 ≤ 段長 − 2 × 牆厚）。
找不到共用牆 / 超出範圍的訊息會寫出牆段端點與可用範圍，照著改 spec。

## IFC 實體對應（render/ifc.py）

| spec | IFC4 |
|---|---|
| 檔案 | IfcProject（Name = title）→ IfcSite → IfcBuilding → IfcBuildingStorey（IfcRelAggregates）；長度單位 IfcSIUnit MILLI METRE、面積 SQUARE_METRE |
| 外框每一邊 | IfcWall `EW-<i>`（轉角斜接梯形擠出），`Pset_WallCommon.IsExternal = True` |
| 每段內牆中心線直線段 | IfcWall `IW-<j>`，`IsExternal = False`（T 字交會處分段） |
| room | IfcSpace：Name = 房名、LongName = 用途；淨空輪廓擠出；`Qto_SpaceBaseQuantities.NetFloorArea`（淨面積）/ `GrossFloorArea`（多邊形面積）；聚合於樓層 |
| door / window | IfcOpeningElement（放置點 = 開口起點）—IfcRelVoidsElement→ 所在 IfcWall；IfcDoor / IfcWindow（Name = id、OverallWidth / OverallHeight）—IfcRelFillsElement→ 開口 |
| 牆 / 門 / 窗 | IfcRelContainedInSpatialStructure 於樓層 |

不產生 IfcRelSpaceBoundary、材料、型別（IfcWallType 等）、樓板、屋頂。

## ifcopenshell 讀回驗證要點（verify/floorplan_check.py `check_floorplan_ifc`）

```python
import ifcopenshell, ifcopenshell.util.element as uel, ifcopenshell.util.placement as upl
f = ifcopenshell.open("floorplan.ifc")
f.schema                                   # "IFC4"
f.by_type("IfcSpace"); sp.Name; sp.LongName
uel.get_psets(sp, qtos_only=True)["Qto_SpaceBaseQuantities"]["NetFloorArea"]
uel.get_psets(wall)["Pset_WallCommon"]["IsExternal"]
door.FillsVoids[0].RelatingOpeningElement.VoidsElements[0].RelatingBuildingElement  # 所在牆
upl.get_local_placement(opening.ObjectPlacement)[:2, 3]                              # 放置點 x, y
uel.get_container(door); uel.get_aggregate(space)
```

verifier 檢查：IFC4、mm；空間結構各一且聚合；IfcSpace 數 / 名稱 / 用途 / NetFloorArea / 輪廓面積 = spec；
IfcWall 數 = 外框邊數 + 內牆直線段數、外牆數 = 外框邊數；IfcDoor / IfcWindow 數、id、寬度一致，
每個都有 FillsVoids → 開口 → VoidsElements → IfcWall（外 / 內牆別一致），開口放置點 = spec 開口起點（1 mm）；
全部包含於樓層。

## 其他 verifier

- `floorplan_check`（spec）：房間在室內範圍內、不重疊、鋪滿；門在兩空間共用牆上、門寬 ≤ 段長 − 2 × 牆厚、
  同牆段開口不重疊、窗在外牆且屬於該房；**全部空間可由主入口經室內門到達**；走廊類空間若有多個需以門連通；
  居室類（`render/codes/plan_rules.yaml` `uses.window_required`）至少一扇窗——只檢查「有沒有窗」，
  **不檢查採光面積比例**（沒有附來源的 yaml）。WARNING（慣例，無法規意義）：浴廁只能經由臥室進入、
  房間淨寬過窄、長寬比過大。metrics：各房淨面積 / 坪 / 長寬比、`shared_walls_mm`、`door_links`、`reachable`、
  `entrance`、`circulation_area_ratio`、`mean_aspect_ratio`。
- `floorplan_dxf_check`（DXF 讀回）、`standards_check`、`units_check`：見 dxf-arch skill「floor-plan」一節。

## 配置要訣

- 先在紙上排矩形格網：決定外框 → 室內範圍 → 走廊帶 → 兩側房間；每條格線就是一道內牆中心線。
- 每加一個房間就檢查：是否碰到需要開門的鄰房（共用邊長 ≥ 門寬 + 2 × 內牆厚）、居室是否有一邊貼外牆。
- 小房間（浴廁）的共用牆可能很短；門放不下時調整格線，而不是縮門寬（門寬照交辦）。
- 回覆只能說「通過本 repo 現有 verifier 的檢查項目」，不要說符合建築法規（走道寬、採光、防火、無障礙都沒檢查）。
