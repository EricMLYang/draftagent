"""製圖標準集中管理：圖層表（`render/codes/layers.yaml`）、線寬顯示、CJK 文字樣式。

純函式、確定性，不得呼叫 LLM。

- 圖層命名採 AIA/NCS 風格前綴，**非台灣官方標準**；線寬為業界慣例（yaml `kind: convention`），
  **非法規數值**。
- `apply_layers(doc, names)`：依 yaml 建立 / 更新圖層的 color、linetype、lineweight、plot，
  缺少的自訂線型（DA-*）一併建立，並設 `$LWDISPLAY = 1` 讓 CAD 顯示線寬。
- `text_style(doc)`：建立 / 更新統一的 CJK TrueType 文字樣式。字型檔預設
  `DEFAULT_CJK_FONT` = `msjh.ttc`（微軟正黑體，Windows 內建；審查端以 Windows AutoCAD / BricsCAD
  為主，開圖即有字型）。可用環境變數 `DRAFTAGENT_CAD_FONT` 覆寫（例如 `NotoSansTC-Regular.ttf`）。
  Mac / Linux 的 PNG 預覽會由 `render/preview.substitute_missing_fonts` 換成本機 CJK 字型（不回寫 DXF）。
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml
from ezdxf.document import Drawing
from ezdxf.lldxf.const import VALID_DXF_LINEWEIGHTS

LAYERS_YAML = Path(__file__).resolve().parent / "codes" / "layers.yaml"

CJK_STYLE = "CJK"
WINDOWS_CJK_FONT = "msjh.ttc"  # 微軟正黑體（Windows 內建）
NOTO_CJK_FONT = "NotoSansTC-Regular.ttf"  # Noto Sans TC（SIL OFL）；跨平台開源，可用環境變數改用
DEFAULT_CJK_FONT = WINDOWS_CJK_FONT  # 同事以 Windows CAD 為主
FONT_ENV = "DRAFTAGENT_CAD_FONT"

DISCIPLINES = ("A", "C", "H", "S", "X")


@dataclass(frozen=True)
class LayerStd:
    name: str
    description: str
    color: int
    linetype: str
    lineweight: int  # 1/100 mm
    plot: bool
    discipline: str


@dataclass(frozen=True)
class Standards:
    kind: str
    source: str
    layers: dict[str, LayerStd]
    linetypes: dict[str, tuple[tuple[float, ...], str]]  # name → (pattern, description)


def load_standards(path: str | Path = LAYERS_YAML) -> Standards:
    """讀 layers.yaml 並驗證欄位。格式錯誤 raise ValueError。"""
    path = Path(path)
    if path == LAYERS_YAML:
        return _load_default()
    return _parse(path)


@lru_cache(maxsize=1)
def _load_default() -> Standards:
    return _parse(LAYERS_YAML)


def _parse(path: Path) -> Standards:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data.get("kind") != "convention":
        raise ValueError(f"{path}: kind 必須為 'convention'（製圖慣例，非法規數值）")
    lts: dict[str, tuple[tuple[float, ...], str]] = {}
    for name, d in (data.get("linetypes") or {}).items():
        lts[name.upper()] = (tuple(float(v) for v in d["pattern"]), str(d.get("description", "")))
    layers: dict[str, LayerStd] = {}
    for name, d in (data.get("layers") or {}).items():
        lw = int(d["lineweight"])
        if lw not in VALID_DXF_LINEWEIGHTS:
            raise ValueError(f"{path}: 圖層 {name} lineweight={lw} 不是 DXF 合法線寬")
        color = int(d["color"])
        if not 1 <= color <= 255:
            raise ValueError(f"{path}: 圖層 {name} color={color} 不在 ACI 1–255")
        disc = str(d["discipline"])
        if disc not in DISCIPLINES or not name.startswith(f"{disc}-"):
            raise ValueError(f"{path}: 圖層 {name} discipline={disc} 與前綴不符")
        layers[name.upper()] = LayerStd(
            name=name,
            description=str(d["description"]),
            color=color,
            linetype=str(d["linetype"]).upper(),
            lineweight=lw,
            plot=bool(d["plot"]),
            discipline=disc,
        )
    return Standards(
        kind=data["kind"], source=str(data.get("source", "")), layers=layers, linetypes=lts
    )


def load_layers(path: str | Path = LAYERS_YAML) -> dict[str, LayerStd]:
    """圖層表：大寫名稱 → LayerStd。"""
    return load_standards(path).layers


def get_layer(name: str) -> LayerStd | None:
    return load_layers().get(name.upper())


def _doc_linetype_name(doc: Drawing, lt: str) -> str:
    """把 yaml 的大寫線型名對應到 doc 內實際名稱（CONTINUOUS → Continuous）。"""
    for entry in doc.linetypes:
        if entry.dxf.name.upper() == lt:
            return entry.dxf.name
    return lt


def ensure_linetype(doc: Drawing, lt: str, std: Standards | None = None) -> str:
    """確保線型存在於 doc；yaml 自訂線型缺少時建立。回傳 doc 內名稱。"""
    std = std or load_standards()
    lt = lt.upper()
    name = _doc_linetype_name(doc, lt)
    if name in doc.linetypes:
        return name
    if lt in std.linetypes:
        pattern, desc = std.linetypes[lt]
        doc.linetypes.add(lt, pattern=list(pattern), description=desc)
        return lt
    raise ValueError(f"線型 {lt} 不在 doc 內，也不在 layers.yaml linetypes 區")


def apply_layers(doc: Drawing, names: Iterable[str] | None = None) -> list[str]:
    """依 layers.yaml 建立 / 更新圖層屬性（color、linetype、lineweight、plot），設 $LWDISPLAY=1。

    names 省略 = yaml 全部圖層；給了則只處理這些（不在 yaml 內 raise KeyError）。
    已存在的圖層屬性一律改成 yaml 值。回傳處理的圖層名稱。
    """
    std = load_standards()
    wanted = list(std.layers) if names is None else [n.upper() for n in names]
    out = []
    for key in wanted:
        if key not in std.layers:
            raise KeyError(f"圖層 {key} 不在 render/codes/layers.yaml；請先加入圖層表")
        s = std.layers[key]
        lt = ensure_linetype(doc, s.linetype, std)
        attribs = {
            "color": s.color,
            "linetype": lt,
            "lineweight": s.lineweight,
            "plot": int(s.plot),
        }
        layer = doc.layers.get(s.name) if s.name in doc.layers else doc.layers.add(s.name)
        for k, v in attribs.items():
            layer.dxf.set(k, v)
        out.append(s.name)
    doc.header["$LWDISPLAY"] = 1
    return out


def cad_font() -> str:
    """DXF 文字樣式使用的 CJK 字型檔：環境變數 DRAFTAGENT_CAD_FONT 覆寫，預設 DEFAULT_CJK_FONT。"""
    return os.environ.get(FONT_ENV, "").strip() or DEFAULT_CJK_FONT


def text_style(doc: Drawing, name: str = CJK_STYLE) -> str:
    """建立 / 更新 CJK TrueType 文字樣式，回傳樣式名。"""
    font = cad_font()
    if name in doc.styles:
        doc.styles.get(name).dxf.font = font
    else:
        doc.styles.add(name, font=font)
    return name


__all__ = [
    "CJK_STYLE",
    "DEFAULT_CJK_FONT",
    "FONT_ENV",
    "LAYERS_YAML",
    "NOTO_CJK_FONT",
    "WINDOWS_CJK_FONT",
    "LayerStd",
    "Standards",
    "apply_layers",
    "cad_font",
    "ensure_linetype",
    "get_layer",
    "load_layers",
    "load_standards",
    "text_style",
]
