"""DXF 製圖標準檢查：圖層表（render/codes/layers.yaml）、線寬顯示、顏色覆寫。純函式，不得呼叫 LLM。

檢查項目
- 使用中的圖層（modelspace / paperspace 實體、具名 block 內非 0 層實體）都在 layers.yaml
  → 不在：WARNING（系統圖層 0 / Defpoints 與 paperspace 主視埠不計；0 層實體由各圖種 verifier
  另行把關）
- 使用中且在 yaml 的圖層：lineweight、linetype 與 yaml 一致 → 不一致：ERROR；
  color / plot 不一致 → WARNING
- layout 實體以 BYBLOCK 或硬寫顏色（ACI ≠ BYLAYER、true_color）覆寫圖層顏色的比例
  > `max_color_override`（預設 0.10）→ WARNING
- `$LWDISPLAY = 1` → 否則 ERROR
- 印出字高 ≥ layers.yaml `text.min_print_height_mm`（預設 2.0 mm，製圖慣例）→ 否則 ERROR。
  TEXT / MTEXT / ATTRIB / INSERT 內文字 / DIMENSION（dimtxt × dimscale）；paperspace 直接 mm，
  modelspace 經 viewport 比例換算，純 modelspace 圖用 `print_scale`（出圖比例分母；平面圖由
  spec 提供，None = modelspace 即圖紙 mm）。實作在 verify/legibility_check.check_text_heights

圖層表是製圖慣例（AIA/NCS 風格前綴，非台灣官方標準；線寬非法規數值），通過本檢查不代表
符合任何法規或機關 CAD 標準。

CLI：python -m draftagent.verify.standards_check <dxf> [--json] [--max-override 0.10] [--print-scale 50]
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

import ezdxf
import yaml
from ezdxf.document import Drawing

from draftagent.render.standards import LAYERS_YAML, load_standards
from draftagent.verify.base import VerifyResult, emit
from draftagent.verify.legibility_check import check_text_heights

SYSTEM_LAYERS = {"0", "DEFPOINTS"}
BYLAYER, BYBLOCK = 256, 0
DEFAULT_MIN_TEXT_MM = 2.0


def min_print_height_mm(layers_yaml: str | Path = LAYERS_YAML) -> float:
    """layers.yaml `text.min_print_height_mm`（kind 必須為 convention）；缺值 → ValueError。"""
    data = yaml.safe_load(Path(layers_yaml).read_text(encoding="utf-8")) or {}
    text = data.get("text")
    if not isinstance(text, dict) or "min_print_height_mm" not in text:
        raise ValueError(f"{layers_yaml}: 缺少 text.min_print_height_mm")
    if text.get("kind") != "convention":
        raise ValueError(f"{layers_yaml}: text.kind 必須為 'convention'（製圖慣例，非法規數值）")
    return float(text["min_print_height_mm"])


def _used_layers(doc: Drawing) -> Counter:
    used: Counter = Counter()
    for layout in doc.layouts:
        for e in layout:
            if e.dxftype() == "VIEWPORT" and e.dxf.get("id", 0) == 1:
                continue  # paperspace 主視埠（ezdxf 放在 VIEWPORTS 層），不出圖
            used[e.dxf.get("layer", "0").upper()] += 1
    for blk in doc.blocks:
        if blk.name.startswith("*"):  # 匿名 block（標註、hatch 等）由 ezdxf 產生
            continue
        for e in blk:
            lay = e.dxf.get("layer", "0").upper()
            if lay != "0":
                used[lay] += 1
    return used


def check_standards(
    dxf_path: str | Path | Drawing,
    *,
    layers_yaml: str | Path = LAYERS_YAML,
    max_color_override: float = 0.10,
    print_scale: float | None = None,
) -> VerifyResult:
    res = VerifyResult("standards_check")
    if isinstance(dxf_path, Drawing):
        doc = dxf_path
    else:
        try:
            doc = ezdxf.readfile(str(dxf_path))
        except (OSError, ezdxf.DXFStructureError) as e:
            res.error(f"無法讀取 DXF：{e}")
            return res
    std = load_standards(layers_yaml)
    res.metrics["layers_yaml"] = str(Path(layers_yaml).name)

    lwd = doc.header.get("$LWDISPLAY", 0)
    res.metrics["lwdisplay"] = lwd
    if lwd != 1:
        res.error(f"$LWDISPLAY = {lwd}，應為 1（CAD 才會顯示線寬）")

    used = _used_layers(doc)
    unknown = sorted(n for n in used if n not in std.layers and n not in SYSTEM_LAYERS)
    for n in unknown:
        res.warn(f"圖層 {n} 不在 layers.yaml（{used[n]} 個實體）；請加入圖層表或改用既有圖層")
    res.metrics["used_layers"] = sorted(used)
    res.metrics["unknown_layers"] = unknown

    checked = 0
    for n in sorted(used):
        s = std.layers.get(n)
        if s is None:
            continue
        if n not in doc.layers:
            res.error(f"圖層 {n} 有實體但未定義於 LAYER 表")
            continue
        layer = doc.layers.get(n)
        checked += 1
        lw = layer.dxf.get("lineweight", -3)
        if lw != s.lineweight:
            res.error(
                f"圖層 {n} lineweight={lw / 100:.2f} mm（{lw}），layers.yaml 為 "
                f"{s.lineweight / 100:.2f} mm（{s.lineweight}）"
            )
        lt = str(layer.dxf.get("linetype", "Continuous")).upper()
        if lt != s.linetype:
            res.error(f"圖層 {n} linetype={lt}，layers.yaml 為 {s.linetype}")
        color = abs(layer.dxf.get("color", 7))  # 負值 = 圖層關閉
        if color != s.color:
            res.warn(f"圖層 {n} color={color}，layers.yaml 為 {s.color}")
        plot = bool(layer.dxf.get("plot", 1))
        if plot != s.plot:
            res.warn(f"圖層 {n} plot={plot}，layers.yaml 為 {s.plot}")
    res.metrics["layers_checked"] = checked

    try:
        min_mm = min_print_height_mm(layers_yaml)
    except ValueError as e:
        res.error(str(e))
        min_mm = DEFAULT_MIN_TEXT_MM
    bad_text = check_text_heights(doc, None, min_mm, model_scale=print_scale)
    res.metrics["min_print_height_mm"] = min_mm
    res.metrics["print_scale"] = print_scale
    res.metrics["text_too_small"] = len(bad_text)
    for m in bad_text[:20]:
        res.error(f"文字太小：{m}")
    if len(bad_text) > 20:
        res.error(f"文字太小：另有 {len(bad_text) - 20} 筆未列出")

    total = override = 0
    by_layer: Counter = Counter()
    for layout in doc.layouts:
        for e in layout:
            if e.dxftype() == "VIEWPORT":
                continue
            total += 1
            c = e.dxf.get("color", BYLAYER)
            if c != BYLAYER or e.dxf.hasattr("true_color"):
                override += 1
                by_layer[e.dxf.get("layer", "0").upper()] += 1
    ratio = override / total if total else 0.0
    res.metrics["color_override"] = f"{override}/{total} ({ratio:.1%})"
    if ratio > max_color_override:
        top = ", ".join(f"{k}:{v}" for k, v in by_layer.most_common(5))
        res.warn(
            f"{ratio:.1%} 的實體以 BYBLOCK / 硬寫顏色覆寫圖層顏色（上限 {max_color_override:.0%}）；"
            f"主要圖層 {top}"
        )
    return res


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="DXF 製圖標準檢查（圖層表 / 線寬 / 顏色覆寫）")
    ap.add_argument("dxf")
    ap.add_argument("--layers", default=str(LAYERS_YAML), help="圖層表 yaml")
    ap.add_argument("--max-override", type=float, default=0.10, help="顏色覆寫比例上限（warning）")
    ap.add_argument(
        "--print-scale",
        type=float,
        default=None,
        help="純 modelspace 圖的出圖比例分母（平面圖 1/50 → 50）；省略 = modelspace 即圖紙 mm",
    )
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    emit(
        check_standards(
            a.dxf,
            layers_yaml=a.layers,
            max_color_override=a.max_override,
            print_scale=a.print_scale,
        ),
        as_json=a.json,
    )


if __name__ == "__main__":
    main()
