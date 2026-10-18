# archdxf — 合成「雜亂建築師 DXF」fixture

由 `tools/make_fixtures_archdxf.py` 產生（確定性、可重現），契約見 `draftagent/arch/__init__.py`。
全部是合成資料：無真實案名、地號、公司名；基地座標為 TWD97 TM2（EPSG:3826）範圍內的合成位置。

```bash
python tools/make_fixtures_archdxf.py           # 重寫
python tools/make_fixtures_archdxf.py --check   # 比對（忽略 header 時間戳 / GUID），不一致 exit 1
pytest tests/test_archdxf_fixtures.py
```

每個 case：`drawing.dxf`（< 300 KB）+ `truth.json`（契約欄位，另有少數 case 專屬的額外欄位）。

| case | 單位 / $INSUNITS | 外牆圖層 | 陷阱 | 額外 truth 欄位 |
|---|---|---|---|---|
| `clean_mm` | mm / 4 | `A-WALL` | 無：基準。局部原點、單一封閉 LWPOLYLINE、圖面無旋轉 | — |
| `layer_names_cn` | mm / 4 | `WALL-1` | 中文 / 亂取圖層名；「外牆線」是陽台欄杆、「陽台」是封閉矩形、「建築-外框」是圖框、`A-WALL$0$舊` 是舊版較小外框、「牆」是內牆；旋轉 8° | `layers.decoys` |
| `rotated` | mm / 4 | `A-WALL` | 建物軸線相對正北 23.5°，圖上正交；控制點帶旋轉 | `rotation_deg` |
| `double_line_walls` | mm / 4 | `A-WALL` | 外牆雙線 LINE（牆厚 200），門窗處斷開 900–2400 mm，洞口有封口短線；真值 = 外緣 | `wall_thickness_mm`、`gap_mm`=2400 |
| `gaps_unjoined` | mm / 4 | `外牆` | 32 段 LINE，順序打亂、方向不一，接縫 1–5 mm（含角落）、部分過頭 50–300 mm、少數重複 | `gap_mm`=5 |
| `blocks_xref` | mm / 4 | `A-WALL` | 外牆分散在 modelspace + INSERT（旋轉 90°、縮放 2）+ 巢狀 INSERT（總旋轉 15°、縮放 0.5，實體在 0 層繼承 A-WALL）；兩個 xref 指向不存在的檔 | `xrefs` |
| `multi_floor` | mm / 4 | `1F-WALL` | 1F / B1 / RF 並排在同一 modelspace（B1 右移 60 m、RF 右移 120 m），圖層前綴 + 圖名文字；控制點只對 1F；basement 要平移回原位 | `floor_filter`="1F-"、`basement_twd97`、`floor_offsets_drawing`、`floor_titles` |
| `wrong_insunits` | mm / **6（錯）** | `A-WALL` | header 說公尺、實際 mm；信 header 會得到 28 km 的建物 | — |
| `noise_heavy` | mm / 4 | `0` | 幾乎全部在 0 層；外牆 = 0 層 color 1 + lineweight 50 的 LINE；紅色細線雨遮、陽台、SOLID 牆 hatch、30 家具、40 文字、尺寸、modelspace 與 paperspace 各一圖框 | `exterior_wall_filter` |
| `twd97_native` | m / 6 | `A-WALL` | 已用 TWD97 公尺座標畫；控制點 drawing == twd97（縮放 1）；工具若假設 mm 會縮 1000 倍 | — |

共同：每份圖都有內牆、門弧、窗、家具圖塊、房名文字、尺寸、軸線 + 柱 hatch、基地線（`C-PROP` 等，非矩形）、
3–4 個控制點（在基地界線頂點，圖上標示「CPn E=… N=…」），paperspace 圖框。
`footprint_area_m2` 為真值外框面積（方便核對）。
