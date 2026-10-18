"""DXF → PNG 預覽（ezdxf drawing addon + matplotlib）。純函式，不得呼叫 LLM。

預覽只供人眼檢查，不是出圖。matplotlib 為選用相依，函式內才 import。

中文字型：ezdxf 以文字樣式的字型檔繪字。DXF 指定的 CJK 字型（如 Noto Sans TC）本機沒有時，
ezdxf 會退回不含中文的預設字型而畫成方塊，因此預覽前把「本機找不到」的 TrueType 樣式
暫時換成 `CJK_FALLBACK_FILES` 裡第一個存在的字型（只改記憶體中的 doc，不回寫 DXF）。
macOS 另把 Hiragino Sans GB 註冊進 matplotlib，family 以 ["Hiragino Sans GB", "DejaVu Sans"]
fallback；Linux 找不到則略過。
"""

from __future__ import annotations

from pathlib import Path

import ezdxf
from ezdxf.document import Drawing

MAC_CJK_FONT = Path("/System/Library/Fonts/Hiragino Sans GB.ttc")
MPL_FAMILY = ["Hiragino Sans GB", "DejaVu Sans"]
# ezdxf 依序取第一個本機存在者。macOS 先用 STHeiti Light：實測 ezdxf 以 Hiragino Sans GB
# 繪「進」等字時，字形內部封閉輪廓被填成實心方塊（填色規則問題），STHeiti 無此問題。
CJK_FALLBACK_FILES = (
    "STHeiti Light.ttc",  # macOS
    "Hiragino Sans GB.ttc",  # macOS
    "NotoSansTC-Regular.ttf",
    "NotoSansCJK-Regular.ttc",  # Linux（fonts-noto-cjk）
    "msjh.ttc",  # Windows 微軟正黑體
)


def _setup_matplotlib():
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import font_manager
    from matplotlib import pyplot as plt

    if MAC_CJK_FONT.exists():
        font_manager.fontManager.addfont(str(MAC_CJK_FONT))
        plt.rcParams["font.family"] = MPL_FAMILY
    plt.rcParams["axes.unicode_minus"] = False
    return plt


def substitute_missing_fonts(doc: Drawing) -> dict[str, str]:
    """把本機找不到字型檔的 TrueType 樣式換成可用的 CJK 字型。回傳 {style: 新字型}。"""
    from ezdxf.fonts import fonts

    fm = fonts.font_manager
    fallback = next((f for f in CJK_FALLBACK_FILES if fm.has_font(f)), None)
    changed: dict[str, str] = {}
    if fallback is None:
        return changed
    for style in doc.styles:
        font = style.dxf.get("font", "")
        if font.lower().endswith((".ttf", ".ttc", ".otf")) and not fm.has_font(font):
            style.dxf.font = fallback
            changed[style.dxf.name] = fallback
    return changed


# ezdxf 1.4 的 matplotlib backend 把線寬（mm）直接當 matplotlib 的 point 用 → 細 2.83 倍
MM_TO_PT = 72 / 25.4


def render_png(
    dxf: str | Path | Drawing,
    png_path: str | Path,
    *,
    dpi: int = 200,
    bg: str = "#FFFFFF",
    layout: str | None = None,
    max_px: int = 4000,
) -> Path:
    """把 modelspace（或指定名稱的 paperspace layout）畫成 PNG（白底）。回傳輸出路徑。

    layout 為 paperspace 名稱時依 viewport 出圖（座標 = 圖紙 mm，圖片尺寸依紙張 1:1）。
    """
    from ezdxf.addons.drawing import Frontend, RenderContext
    from ezdxf.addons.drawing.config import BackgroundPolicy, Configuration, LineweightPolicy
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

    plt = _setup_matplotlib()
    doc = dxf if isinstance(dxf, Drawing) else ezdxf.readfile(str(dxf))
    substitute_missing_fonts(doc)

    png_path = Path(png_path)
    png_path.parent.mkdir(parents=True, exist_ok=True)
    fig = plt.figure()
    ax = fig.add_axes((0, 0, 1, 1))
    target = doc.modelspace() if layout is None else doc.layouts.get(layout)

    def _draw(lineweight_scaling: float) -> None:
        ax.clear()
        ax.set_axis_off()
        cfg = Configuration(
            background_policy=BackgroundPolicy.WHITE,
            lineweight_policy=LineweightPolicy.ABSOLUTE,
            lineweight_scaling=lineweight_scaling,
        )
        Frontend(RenderContext(doc), MatplotlibBackend(ax), config=cfg).draw_layout(
            target, finalize=True
        )

    _draw(MM_TO_PT)
    # 依實際範圍設定圖片尺寸（1 inch = 25.4 mm）
    x0, x1 = ax.get_xlim()
    y0, y1 = ax.get_ylim()
    w_in, h_in = max((x1 - x0) / 25.4, 1), max((y1 - y0) / 25.4, 1)
    # 非圖紙 mm 的圖（mm 實尺建築圖、m 座標圖）會大到數十億像素：長邊上限 max_px。
    # 降 dpi 時線寬也等比例變細到看不見 → 以相同倍率加粗線條
    eff_dpi = max(min(dpi, max_px / max(w_in, h_in)), 10)
    if eff_dpi < dpi:
        _draw(MM_TO_PT * dpi / eff_dpi)
    fig.set_size_inches(w_in, h_in)  # 重畫會重設 figure 尺寸，必須在最後設定
    fig.savefig(png_path, dpi=eff_dpi, facecolor=bg)
    plt.close(fig)
    return png_path
