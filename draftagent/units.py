"""單位：pint UnitRegistry 單例 + DXF $INSUNITS 對照與檢查。純函式，不得呼叫 LLM。

單位規則（spec 宣告）
--------------------
每個圖種在 spec 宣告 `units`：
- 建築平面 / 詳圖 / 縱斷面圖紙 = **mm**（$INSUNITS = 4）
- GIS 對位的平面圖（集水區、排水平面、地籍套繪）= **m**（$INSUNITS = 6，TWD97 TM2 座標）
verifier 必須檢查 DXF 的 $INSUNITS 與 spec 宣告一致（`check_insunits`）。
"""

from __future__ import annotations

from functools import cache
from typing import TYPE_CHECKING, Any, Literal

from draftagent.verify.base import VerifyResult

if TYPE_CHECKING:
    import pint
    from ezdxf.document import Drawing

DrawingUnits = Literal["mm", "m"]

# DXF $INSUNITS 代碼（AutoCAD DXF Reference：4 = Millimeters、6 = Meters）
DXF_INSUNITS: dict[str, int] = {"mm": 4, "m": 6}
INSUNITS_NAME: dict[int, str] = {v: k for k, v in DXF_INSUNITS.items()}


@cache
def get_ureg() -> pint.UnitRegistry:
    """全 repo 共用的 pint UnitRegistry（延遲建立；不同 registry 的量不能互相運算）。"""
    import pint

    return pint.UnitRegistry()


def __getattr__(name: str) -> Any:  # `from draftagent.units import ureg`
    if name == "ureg":
        return get_ureg()
    raise AttributeError(name)


def Q_(value: float, unit: str) -> pint.Quantity:  # pint 慣用名
    return get_ureg().Quantity(value, unit)


def check_insunits(doc: Drawing, expected: str, name: str = "units_check") -> VerifyResult:
    """DXF $INSUNITS 必須等於 spec 宣告的 `units`（mm → 4、m → 6）。"""
    res = VerifyResult(name=name)
    if expected not in DXF_INSUNITS:
        res.error(f"spec units {expected!r} 不支援；允許值 {sorted(DXF_INSUNITS)}")
        return res
    want = DXF_INSUNITS[expected]
    got = doc.header.get("$INSUNITS", 0)
    res.metrics.update(spec_units=expected, insunits=got, expected_insunits=want)
    if got != want:
        label = INSUNITS_NAME.get(got, "未知 / 未設定")
        res.error(f"$INSUNITS = {got}（{label}），spec 宣告 units={expected!r} 應為 {want}")
    return res
