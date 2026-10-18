"""桃園送審附圖比例尺門檻（`hydro/codes/taoyuan_drawing.yaml`）讀取。純函式，不得呼叫 LLM。

程式內不寫任何門檻數值；缺 key / 缺 source 直接 raise，讓呼叫端停下來回報缺什麼。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

DEFAULT_PATH = Path(__file__).parent / "codes" / "taoyuan_drawing.yaml"


@dataclass(frozen=True)
class ScaleRule:
    key: str
    name: str
    max_denominator: int  # S ≥ 1/max_denominator ⇒ 比例分母 ≤ max_denominator
    source: str
    source_url: str | None
    as_of: str | None


@dataclass(frozen=True)
class DrawingCodes:
    path: Path
    verified: bool
    note: str
    scales: dict[str, ScaleRule]

    def scale(self, key: str) -> ScaleRule:
        if key not in self.scales:
            raise KeyError(
                f"{self.path.name} 沒有 '{key}' 的比例尺門檻（已有：{sorted(self.scales)}）"
            )
        return self.scales[key]


@lru_cache(maxsize=8)
def load_drawing_codes(path: str | Path = DEFAULT_PATH) -> DrawingCodes:
    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if "verified" not in raw:
        raise ValueError(f"{path}: 缺少頂層 verified 欄位")
    scales: dict[str, ScaleRule] = {}
    for key, item in (raw.get("scales") or {}).items():
        for f in ("name", "max_denominator", "source", "as_of"):
            if f not in item:
                raise ValueError(f"{path}: scales.{key} 缺少 {f}")
        scales[key] = ScaleRule(
            key=key,
            name=str(item["name"]),
            max_denominator=int(item["max_denominator"]),
            source=str(item["source"]),
            source_url=item.get("source_url"),
            as_of=item["as_of"],
        )
    return DrawingCodes(
        path=path, verified=bool(raw["verified"]), note=str(raw.get("note", "")), scales=scales
    )
