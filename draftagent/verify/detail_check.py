"""排水系統各部詳圖 DXF verifier（DetailSheetSpec）。純函式，不得呼叫 LLM。

從 DXF 讀回，對照 spec（必填）檢查：
- 檔案可讀；$INSUNITS = 4（mm）；任何 layout 的 0 層無實體
- 圖簽 INSERT 的 ATTRIB PROJECT / TITLE / DWG_NO / SCALE 不可空
- 每個 spec 詳圖都有 DXF GROUP `DETAIL-<id>`；其中須有：含「剖面代號」與「圖名」的標題文字、
  「S=1/N」比例文字（N = spec.scale）。N ≤ `hydro/codes/taoyuan_drawing.yaml`
  drainage_details 上限（verified: false → WARNING）
- **尺寸標註顯示值 = 實尺**：每個 DIMENSION 的 dimlfac 必須 = 該詳圖比例分母；
  顯示值 = get_measurement()（圖紙 mm，由 defpoints 重算）× dimlfac，若有文字覆寫則以覆寫數字為準
  且必須與量測 × dimlfac 一致；顯示值多重集合須與 verifier 由 spec **獨立推導**的應有尺寸一致
  （容差 0.5 mm）——缺標註、多出 spec 沒有的值都是 error
- spec 中所有高程（含由坑底 + 坑深推得的坑頂）都以「EL=xx.xx」出現在該詳圖
- 必要註記（審查常見缺失）：筏基滯洪 → 逆止閥、溢流口、人孔（有爬梯時「爬梯」）；
  放流井 → 切齊溝緣、清掃孔、溢流堰；銘牌 → 容量（有給值時另須數值）；出水口保護工 → 護坦
  （有拋石 / 截水牆時亦須）；孔口板 → 材質、固定方式文字
- 幾何合理性（由 spec 重算，違反為 error）：有效水深 ≤ 坑深；筏基水位
  孔口中心 EL < 設計滿水位（貯留水位 = 坑底 + 有效水深）EL ≤ 溢流口 EL < 溢流時最高水位
  （溢流口 + 堰頂水頭）EL ≤ 坑頂 EL（溢流時最高水位未給 → 跳過該段並 WARNING；池內無溢流 → 設計滿水位 < 坑頂）；
  孔口底 ≥ 坑底；放流井 孔口 < 堰頂 < 井頂、放流管底 ≥ 側溝底、
  放流管頂 ≤ 溝緣；出水管底 ≥ 護坦頂；抽水型筏基 停機 < 起抽 ≤ 設計滿水位、停機 ≥ 集水坑底；
  放流井出口管底 ≥ 受納溝設計水位（有給時）、不逆坡；接側溝詳圖 管底 ≥ 溝底 / 設計水位、管頂 ≤ 溝緣
- 同張圖銜接：重力筏基標示「接放流井」時，放流管底 ≥ 放流井井底（sheet_issues）
- 各詳圖 bbox 互不重疊、不壓圖簽、皆在內框內

用法：python -m draftagent.verify.detail_check <detail.dxf> --spec spec.json [--json]
"""

from __future__ import annotations

import argparse
import itertools
import math
import re
import warnings
from collections import Counter
from pathlib import Path

import ezdxf
from ezdxf import bbox as ezbbox

from draftagent.hydro.drawing_codes import DEFAULT_PATH, load_drawing_codes
from draftagent.schema.detail import (
    DETAIL_SCALE_KEY,
    DetailSheetSpec,
    DitchJointDetail,
    LegacyFieldWarning,
    NameplateDetail,
    OrificePlateDetail,
    OutfallProtectionDetail,
    OutletWellDetail,
    RaftDetentionSection,
    UDitchSection,
)
from draftagent.verify.base import VerifyResult, emit
from draftagent.verify.dimtext import displayed_text, forbidden_vars
from draftagent.verify.profile_check import check_text_anchors

NAME = "detail_check"
INSUNITS_MM = 4
DIM_TOL = 0.5  # mm（實尺）
REQUIRED_ATTRIBS = ("PROJECT", "TITLE", "DWG_NO", "SCALE")
GROUP_PREFIX = "DETAIL-"
_RE_EL = re.compile(r"EL\s*=\s*(-?\d+\.\d{2})(?!\d)")
_RE_SCALE = re.compile(r"S\s*=\s*1\s*/\s*(\d+)")
_RE_NUM = re.compile(r"-?\d+(?:\.\d+)?")
_RE_CAPACITY = re.compile(r"^(\d+(?:\.\d+)?)\s*m³$")
EL_TOL = 0.005  # m
GEOM_TOL = 0.5  # mm（圖紙）：標示點 / 水位線與 EL 換算位置
BODY_RTOL = 0.01  # 本體外框尺寸相對容差
MIN_TEXT_MM = 2.0
# 必要註記前的否定詞：同一段文字中出現在關鍵字之前 → 該段不算有標示
_NEGATIONS = ("不設", "未設", "免設", "不需", "無需", "免", "無", "取消", "省略")
# Hatch pattern_scale 合理範圍（圖紙 mm 出圖，詳圖比例無關）。
# convention：本 repo 詳圖 renderer 預設 AR-CONC 0.035、EARTH 0.6、GRAVEL 0.09/0.2、ANSI31 0.6，
# 經 PNG 預覽確認可讀；範圍取約 ×1/3～×3。超出 → 材料符號過密成實心或過疏看不出（業界慣例，非法規）。
HATCH_SCALE_RANGE: dict[str, tuple[float, float]] = {
    "AR-CONC": (0.012, 0.11),
    "EARTH": (0.2, 1.8),
    "GRAVEL": (0.03, 0.6),
    "ANSI31": (0.2, 1.8),
}
# expected_els 名稱 → 圖面文字「<標籤> EL=」的標籤樣式（標籤可帶管徑、括號說明）
EL_LABEL_PATTERNS: dict[str, str] = {
    "溝底": r"溝底$",
    "坑底": r"坑底$",
    "坑頂": r"坑頂(（[^）]*）)?$",
    "設計滿水位": r"設計滿水位$",
    "溢流口": r"溢流口( D\d+(\.\d+)?)?$",
    "溢流時最高水位": r"溢流時最高水位$",
    "放流孔口中心": r"放流孔口 \S+ 中心$",
    "進流管底": r"進流管 D.+?\s*管底$",
    "起抽水位": r"起抽水位$",
    "停機水位": r"停機水位$",
    "井頂": r"井頂$",
    "井底": r"井底$",
    "溢流堰頂": r"溢流堰頂(（[^）]*）)?$",
    "孔口中心": r"孔口 \S+ 中心$",
    "放流管底": r"放流管 D.+?\s*管底$",
    "側溝底": r"側溝底$",
    "側溝溝緣": r"側溝溝緣$",
    "放流管出口管底": r"放流管出口管底$",
    "受納溝設計水位": r"受納溝設計水位$",
    "抽水出水管底": r"抽水出水管 D.+?\s*管底$",
    "溢流管底": r"溢流管 D.+?\s*管底$",
    "出流口管底": r"出流口 D.+?\s*管底$",
    "地面": r"地面$",
    "孔心": r"孔心$",
    "護坦頂": r"護坦頂$",
    "出水管底": r"出水管 D.+?\s*管底$",
}


# ---------------------------------------------------------------- spec → 應有內容（獨立推導）


def expected_dims(d) -> list[tuple[str, float]]:
    """該詳圖應標註的實尺尺寸 (名稱, mm)。與 renderer 分開維護，作為交叉檢核。"""
    out: list[tuple[str, float]] = []
    if isinstance(d, UDitchSection):
        B = d.inner_width + 2 * d.wall_t
        out += [("壁厚", d.wall_t), ("內寬 W", d.inner_width), ("壁厚", d.wall_t), ("外寬", B)]
        if d.gravel_t > 0:
            out.append(("碎石厚", d.gravel_t))
        out += [("底厚", d.base_t), ("內深 H", d.inner_depth)]
        if d.cover:
            out.append(("蓋板厚", d.cover.thickness))
    elif isinstance(d, RaftDetentionSection):
        for _ in range(d.n_cells):
            out += [("地梁寬", d.beam_width), ("坑格寬", d.cell_width)]
        out.append(("地梁寬", d.beam_width))
        out.append(("總寬", d.n_cells * d.cell_width + (d.n_cells + 1) * d.beam_width))
        if d.gravel_t > 0:
            out.append(("碎石厚", d.gravel_t))
        out += [("底板厚", d.base_slab_t), ("坑深", d.pit_depth), ("頂版厚", d.top_slab_t)]
        out += [("有效水深", d.effective_depth), ("人孔", d.manhole_size)]
        if d.pump is not None:
            out += [("集水坑寬", d.pump.sump_width), ("集水坑深", d.pump.sump_depth)]
    elif isinstance(d, OutletWellDetail):
        t = d.wall_t
        inner = d.chamber_a_length + d.partition_t + d.chamber_b_length
        if d.single_chamber:
            out += [("井壁", t), ("井內長", inner), ("井壁", t)]
        else:
            out += [("井壁", t), ("A 室", d.chamber_a_length), ("隔牆", d.partition_t)]
            out += [("B 室", d.chamber_b_length), ("井壁", t)]
        out.append(("井外長", 2 * t + inner))
        out += [("放流管長", d.outlet_pipe_length), ("側溝內寬", d.ditch.inner_width)]
        if d.gravel_t > 0:
            out.append(("碎石厚", d.gravel_t))
        out.append(("底板厚", d.base_t))
        out.append(("井內寬", d.inner_width))
        if d.single_chamber:
            out.append(("井內長", inner))
        else:
            out += [("A 室", d.chamber_a_length), ("B 室", d.chamber_b_length)]
        if d.weir is not None:
            out.append(("堰寬", d.weir.width))
    elif isinstance(d, DitchJointDetail):
        dt = d.ditch
        out += [("側溝壁厚", dt.wall_t), ("側溝內寬", dt.inner_width), ("側溝壁厚", dt.wall_t)]
        out += [("側溝內深", round((dt.top_el - dt.bottom_el) * 1000.0, 1)), ("底板厚", d.base_t)]
    elif isinstance(d, OrificePlateDetail):
        o = d.orifice
        out += [("板寬", d.plate_width), ("孔寬/孔徑", o.size_w()), ("螺栓邊距", d.bolt_edge)]
        out += [("板高", d.plate_height), ("孔心高", d.orifice_center_height)]
        if o.shape == "rect":
            out.append(("孔高", o.height))  # type: ignore[arg-type]
    elif isinstance(d, OutfallProtectionDetail):
        out += [("端牆厚", d.headwall_t), ("護坦長", d.apron_length)]
        if d.riprap_length > 0:
            out.append(("拋石長", d.riprap_length))
        out += [("護坦厚", d.apron_t), ("端牆高", d.headwall_height)]
        if d.riprap_length > 0:
            out.append(("拋石厚", d.riprap_t))
        if d.cutoff_depth > 0:
            out.append(("截水牆深", d.cutoff_depth))
    elif isinstance(d, NameplateDetail):
        out += [("牌寬", d.plate_width), ("牌高", d.plate_height)]
    return out


def expected_els(d) -> list[tuple[str, float]]:
    if isinstance(d, UDitchSection):
        return [("溝底", d.invert_el)] if d.invert_el is not None else []
    if isinstance(d, RaftDetentionSection):
        out = [("坑底", d.bottom_el), ("坑頂", d.top_el()), ("設計滿水位", d.storage_level_el)]
        if d.overflow_el is not None:
            out.append(("溢流口", d.overflow_el))
            if d.overflow_flood_el is not None:
                out.append(("溢流時最高水位", d.overflow_flood_el))
        if d.orifice is not None:
            out.append(("放流孔口中心", d.orifice.center_el))
        out.append(("進流管底", d.inflow_pipe.invert_el))
        if d.pump is not None:
            out += [("起抽水位", d.pump.start_el), ("停機水位", d.pump.stop_el)]
        return out
    if isinstance(d, OutletWellDetail):
        out = [("井頂", d.top_el), ("井底", d.bottom_el)]
        if d.weir is not None:
            out.append(("溢流堰頂", d.weir.crest_el))
        if d.orifice is not None:
            out.append(("孔口中心", d.orifice.center_el))
        out += [
            ("放流管底", d.outlet_pipe.invert_el),
            ("側溝底", d.ditch.bottom_el),
            ("側溝溝緣", d.ditch.top_el),
        ]
        if d.outlet_pipe_end_el is not None:
            out.append(("放流管出口管底", d.outlet_pipe_end_el))
        if d.ditch.design_wl_el is not None:
            out.append(("受納溝設計水位", d.ditch.design_wl_el))
        if d.inflow_pipe:
            out.append(("進流管底", d.inflow_pipe.invert_el))
        if d.pump_inflow:
            out.append(("抽水出水管底", d.pump_inflow.invert_el))
        if d.overflow_inflow:
            out.append(("溢流管底", d.overflow_inflow.invert_el))
        return out
    if isinstance(d, DitchJointDetail):
        out = [
            ("出流口管底", d.pipe.invert_el),
            ("側溝底", d.ditch.bottom_el),
            ("側溝溝緣", d.ditch.top_el),
        ]
        if d.ditch.design_wl_el is not None:
            out.append(("受納溝設計水位", d.ditch.design_wl_el))
        if d.ground_el is not None:
            out.append(("地面", d.ground_el))
        return out
    if isinstance(d, OrificePlateDetail):
        return [("孔心", d.orifice.center_el)]
    if isinstance(d, OutfallProtectionDetail):
        return [("護坦頂", d.apron_el), ("出水管底", d.pipe.invert_el)]
    return []


def required_words(d) -> list[str]:
    if isinstance(d, RaftDetentionSection):
        words = ["逆止閥", "溢流口", "人孔"] + (["爬梯"] if d.ladder else [])
        if d.pump is not None:
            words += ["抽水機", "集水坑", "抽水出水管"]
        return words
    if isinstance(d, OutletWellDetail):
        # 溢流：井內溢流堰，或（抽水型）進流井溢流管入井；兩者皆無 → 缺「溢流管」error
        return ["切齊溝緣", "清掃孔", "溢流堰" if d.weir is not None else "溢流管"]
    if isinstance(d, DitchJointDetail):
        return ["切齊溝緣"]
    if isinstance(d, NameplateDetail):
        return ["容量"] + ([f"{d.capacity_m3:.2f}"] if d.capacity_m3 is not None else [])
    if isinstance(d, OutfallProtectionDetail):
        words = ["護坦"]
        if d.riprap_length > 0:
            words.append("拋石")
        if d.cutoff_depth > 0:
            words.append("截水牆")
        return words
    if isinstance(d, OrificePlateDetail):
        return [d.material_text, d.fixing_text]
    if isinstance(d, UDitchSection):
        words = []
        if d.cover:
            words.append(d.cover.type_text)
        if d.rebar:
            words.append(f"{d.rebar.bar}{d.rebar.spacing}")
        return words
    return []


def sheet_issues(spec: DetailSheetSpec) -> list[str]:
    """同一張詳圖之間的銜接（違反為 error）。

    raft_detention 的放流管標示「接放流井」（outflow_to_outlet_well）且為重力孔口（非抽水）時，
    放流管底（孔口中心 − 放流管徑 / 2）不得低於同張圖上任一放流井（outlet_well）的井底
    ——否則水要往上流（審查：銜接處各高程、圖說呈現須一致）。
    """
    bad: list[str] = []
    rafts = [d for d in spec.details if isinstance(d, RaftDetentionSection)]
    wells = [d for d in spec.details if isinstance(d, OutletWellDetail)]
    for r in rafts:
        if r.pump is not None or not r.outflow_to_outlet_well or r.orifice is None:
            continue
        out_el = r.outflow_invert_el()
        for w in wells:
            if out_el < w.bottom_el - 1e-9:
                bad.append(
                    f"詳圖 {r.id}（{r.section_code}）放流管底 EL={out_el:.2f}（孔口中心 "
                    f"{r.orifice.center_el:.2f} − D{r.outflow_pipe_dia:g}/2）低於 詳圖 {w.id}"
                    f"（{w.section_code}）放流井井底 EL={w.bottom_el:.2f}：重力無法排入放流井，"
                    "請改高程或改標示抽水（pump）"
                )
    return bad


def geometry_issues(d) -> list[str]:
    """幾何合理性（違反為 error）。高程比較一律用 m、尺寸用 mm。"""
    bad: list[str] = []

    def order(chain: list[tuple[str, float]]) -> None:
        for (na, a), (nb, b) in zip(chain, chain[1:], strict=False):
            if not a < b:
                bad.append(f"{na} EL={a:.2f} 必須低於 {nb} EL={b:.2f}")

    if isinstance(d, RaftDetentionSection):
        if d.effective_depth > d.pit_depth:
            bad.append(f"有效水深 {d.effective_depth:g} mm 大於坑深 {d.pit_depth:g} mm")
        o = d.orifice
        top = d.top_el()
        sl = d.storage_level_el
        if o is not None:
            order([("放流孔口中心", o.center_el), ("設計滿水位", sl)])
        if d.overflow_el is None:  # 池內不設溢流（溢流在進流井）
            order([("設計滿水位", sl), ("坑頂", top)])
        else:
            ov = d.overflow_el
            if sl > ov + 1e-9:
                bad.append(
                    f"設計滿水位 EL={sl:.2f} 高於溢流口 EL={ov:.2f}：未達設計滿水位即溢流，有效貯集量不成立"
                )
            if d.overflow_flood_el is None:
                order([("溢流口", ov), ("坑頂", top)])
            else:
                fl = d.overflow_flood_el
                order([("溢流口", ov), ("溢流時最高水位", fl)])
                if fl > top + 1e-9:
                    bad.append(f"溢流時最高水位 EL={fl:.2f} 高於坑頂 EL={top:.2f}：溢流期間溢頂")
        if d.overflow_flood_el is not None and d.overflow_el is None:
            bad.append("給了溢流時最高水位 overflow_flood_el 但池內無溢流口（overflow_el）")
        if o is not None and o.center_el - o.size_h() / 2000.0 < d.bottom_el - 1e-9:
            bad.append(f"放流孔口底低於坑底 EL={d.bottom_el:.2f}")
        if d.pump is not None:
            pm = d.pump
            sump_bot = round(d.bottom_el - pm.sump_depth / 1000.0, 3)
            order([("停機水位", pm.stop_el), ("起抽水位", pm.start_el)])
            if pm.start_el > d.storage_level_el + 1e-9:
                bad.append(
                    f"起抽水位 EL={pm.start_el:.2f} 高於設計滿水位 EL={d.storage_level_el:.2f}"
                )
            if pm.stop_el < sump_bot - 1e-9:
                bad.append(f"停機水位 EL={pm.stop_el:.2f} 低於集水坑底 EL={sump_bot:.2f}")
        top_in = d.inflow_pipe.invert_el + d.inflow_pipe.diameter / 1000.0
        if top_in > d.top_el() + 1e-9:
            bad.append(f"進流管頂 EL={top_in:.2f} 高於坑頂 EL={d.top_el():.2f}")
    elif isinstance(d, OutletWellDetail):
        o = d.orifice
        chain = []
        if o is not None:
            chain.append(("孔口中心", o.center_el))
        if d.weir is not None:
            chain.append(("溢流堰頂", d.weir.crest_el))
        order([*chain, ("井頂", d.top_el)])
        if o is not None and o.center_el - o.size_h() / 2000.0 < d.bottom_el - 1e-9:
            bad.append(f"孔口底低於井底 EL={d.bottom_el:.2f}")
        exit_el = d.outlet_exit_el()  # 側溝端管底（未給出口高程 = 井端）
        if exit_el < d.ditch.bottom_el - 1e-9:
            bad.append(
                f"放流管底 EL={exit_el:.2f} 低於接入側溝底 "
                f"EL={d.ditch.bottom_el:.2f}（會倒灌 / 淤積）"
            )
        top_out = exit_el + d.outlet_pipe.diameter / 1000.0
        if top_out > d.ditch.top_el + 1e-9:
            bad.append(f"放流管頂 EL={top_out:.2f} 高於側溝溝緣 EL={d.ditch.top_el:.2f}")
        if d.ditch.design_wl_el is not None and exit_el < d.ditch.design_wl_el - 1e-9:
            bad.append(
                f"放流管出口底 EL={exit_el:.2f} 低於受納溝設計水位 EL={d.ditch.design_wl_el:.2f}"
                "（放流口應設於設計水深以上）"
            )
        if exit_el > d.outlet_pipe.invert_el + 1e-9:
            bad.append(
                f"放流管逆坡：井端管底 EL={d.outlet_pipe.invert_el:.2f} < 出口管底 EL={exit_el:.2f}"
            )
        if d.outlet_pipe.invert_el < d.bottom_el - 1e-9:
            bad.append("放流管底低於井底")
        for name, pp in (("抽水出水管", d.pump_inflow), ("溢流管", d.overflow_inflow)):
            if pp is None:
                continue
            if pp.invert_el < d.bottom_el - 1e-9:
                bad.append(f"{name}底 EL={pp.invert_el:.2f} 低於井底 EL={d.bottom_el:.2f}")
            if pp.invert_el + pp.diameter / 1000.0 > d.top_el + 1e-9:
                bad.append(f"{name}頂高於井頂 EL={d.top_el:.2f}")
        if not d.ditch.bottom_el < d.ditch.top_el:
            bad.append("側溝底必須低於溝緣")
    elif isinstance(d, DitchJointDetail):
        dt, pp = d.ditch, d.pipe
        if not dt.bottom_el < dt.top_el:
            bad.append("側溝底必須低於溝緣")
        if pp.invert_el < dt.bottom_el - 1e-9:
            bad.append(f"出流口管底 EL={pp.invert_el:.2f} 低於側溝底 EL={dt.bottom_el:.2f}")
        if dt.design_wl_el is not None and pp.invert_el < dt.design_wl_el - 1e-9:
            bad.append(
                f"出流口管底 EL={pp.invert_el:.2f} 低於受納溝設計水位 EL={dt.design_wl_el:.2f}"
                "（放流口應設於設計水深以上）"
            )
        if pp.invert_el + pp.diameter / 1000.0 > dt.top_el + 1e-9:
            bad.append(f"出流口管頂高於側溝溝緣 EL={dt.top_el:.2f}")
    elif isinstance(d, OutfallProtectionDetail):
        if d.pipe.invert_el < d.apron_el - 1e-9:
            bad.append(f"出水管底 EL={d.pipe.invert_el:.2f} 低於護坦頂 EL={d.apron_el:.2f}")
    return bad


# ---------------------------------------------------------------- DXF 讀取


def _dim_display(dim) -> tuple[float, float, float | None, str]:
    """回傳 (圖紙量測值, dimlfac, 實際顯示數字 or None, 實際顯示文字)。

    顯示值讀 DIMENSION geometry block 內實際畫出的文字（dimtext.displayed_text），
    不再以「量測 × dimlfac」推算——後者漏掉 dimrnd / dimpost / 文字覆寫。
    """
    meas = float(dim.get_measurement())
    lfac = float(dim.override().get("dimlfac", 1.0))
    shown = displayed_text(dim)
    val = shown.numbers[0] if len(shown.numbers) == 1 else None
    return meas, lfac, val, shown.text


def _match_multiset(expected: list[float], shown: list[float], tol: float):
    pool = list(shown)
    missing = []
    for v in expected:
        hit = next((i for i, s in enumerate(pool) if abs(s - v) <= tol), None)
        if hit is None:
            missing.append(v)
        else:
            pool.pop(hit)
    return missing, pool


def check_detail(
    path: str | Path, spec_path: str | Path, codes_path: str | Path = DEFAULT_PATH
) -> VerifyResult:
    res = VerifyResult(name=NAME)
    try:
        doc = ezdxf.readfile(str(path))
    except (OSError, ezdxf.DXFStructureError) as e:
        res.error(f"無法讀取 DXF：{e}")
        return res
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", LegacyFieldWarning)
        spec = DetailSheetSpec.from_json(spec_path)
    for w in caught:
        if issubclass(w.category, LegacyFieldWarning):
            res.warn(f"舊欄位：{w.message}")

    insunits = doc.header.get("$INSUNITS", 0)
    res.metrics["insunits"] = insunits
    if insunits != INSUNITS_MM:
        res.error(f"$INSUNITS = {insunits}，應為 4（mm，圖紙座標）")
    on_zero = Counter(
        e.dxftype() for layout in doc.layouts for e in layout if e.dxf.get("layer", "0") == "0"
    )
    if on_zero:
        res.error(f"0 層有 {sum(on_zero.values())} 個實體：{dict(on_zero)}")

    msp = doc.modelspace()
    tb_box = None
    for ins in msp.query("INSERT"):
        if not ins.attribs:
            continue
        att = {a.dxf.tag.upper(): a.dxf.text for a in ins.attribs}
        for tag in REQUIRED_ATTRIBS:
            if tag in att and not att[tag].strip():
                res.error(f"圖簽 {tag} 為空")
        if ins.dxf.name.upper().startswith("TTLB"):
            e = ezbbox.extents([ins])
            tb_box = (e.extmin.x, e.extmin.y, e.extmax.x, e.extmax.y)

    codes = load_drawing_codes(codes_path)
    rule = codes.scale(DETAIL_SCALE_KEY)
    if not codes.verified:
        res.warn(f"比例尺門檻 yaml verified: false — {codes.note}（{codes.path.name}）")
    res.metrics["scale_limit"] = f"S≧1/{rule.max_denominator}（{rule.source}）"

    ix0, iy0, ix1, iy1 = spec.sheet.inner()
    boxes: dict[str, tuple[float, float, float, float]] = {}
    dims_metric: dict[str, list[float]] = {}
    for d in spec.details:
        tag = f"詳圖 {d.id}（{d.section_code} {d.title}）"
        grp = (
            doc.groups.get(f"{GROUP_PREFIX}{d.id}")
            if f"{GROUP_PREFIX}{d.id}" in doc.groups
            else None
        )
        if grp is None:
            res.error(f"{tag}：DXF 找不到 GROUP {GROUP_PREFIX}{d.id}")
            continue
        ents = [e for e in grp if e.is_alive]
        texts = [e.dxf.text for e in ents if e.dxftype() == "TEXT"]

        # 標題 / 剖面代號 / 比例
        if not any(d.section_code in s and d.title in s for s in texts):
            res.error(f"{tag}：找不到含剖面代號「{d.section_code}」與圖名「{d.title}」的標題文字")
        found_scales = {int(m.group(1)) for s in texts for m in _RE_SCALE.finditer(s)}
        if found_scales != {d.scale}:
            res.error(f"{tag}：比例文字應為 S=1/{d.scale}，圖上為 {sorted(found_scales)}")
        if d.scale > rule.max_denominator:
            res.error(
                f"{tag}：比例 1/{d.scale} 小於{rule.name}要求 S≧1/{rule.max_denominator}"
                f"（{rule.source}）"
            )

        # 尺寸
        shown_vals: list[float] = []
        for dim in (e for e in ents if e.dxftype() == "DIMENSION"):
            h = dim.dxf.handle
            try:
                meas, lfac, shown, txt = _dim_display(dim)
            except Exception as ex:  # noqa: BLE001
                res.error(f"{tag}：DIMENSION {h} 無法量測：{ex}")
                continue
            for bad in forbidden_vars(dim):
                res.error(f"{tag}：DIMENSION {h} {bad}")
            if abs(lfac - d.scale) > 1e-6:
                res.error(
                    f"{tag}：DIMENSION {h} dimlfac={lfac:g} ≠ 比例分母 {d.scale}"
                    f"（顯示值 {meas * lfac:.0f} 不是實尺）"
                )
            if shown is None:
                res.error(
                    f"{tag}：DIMENSION {h} 顯示文字「{txt}」不是單一數字（未 render 或含前後綴）"
                )
                continue
            # CAD 重生（regen）後的顯示值：有文字覆寫 → 覆寫數字；否則 量測 × dimlfac
            regen = meas * lfac
            raw = dim.dxf.get("text", "")
            if raw not in ("", "<>"):
                m = _RE_NUM.search(raw.replace("<>", ""))
                ov = float(m.group()) if m else float("nan")
                if not abs(ov - regen) <= DIM_TOL:
                    res.error(
                        f"{tag}：DIMENSION {h} 文字覆寫「{raw}」與量測 × dimlfac = {regen:.1f} 不符"
                    )
                regen = ov
            if not abs(shown - regen) <= DIM_TOL + 1e-6:
                res.error(
                    f"{tag}：DIMENSION {h} 顯示「{txt}」與量測 × dimlfac（含覆寫）"
                    f"= {regen:.1f} 不符（block 未重生或被改）"
                )
            # 對照 spec 用 regen 值（block 文字與之不符已另報 error）
            shown_vals.append(round(shown if math.isnan(regen) else regen, 2))
        dims_metric[d.id] = sorted(shown_vals)
        exp = expected_dims(d)
        missing, extra = _match_multiset([v for _, v in exp], shown_vals, DIM_TOL)
        for v in missing:
            names = "/".join(sorted({n for n, x in exp if abs(x - v) <= DIM_TOL}))
            res.error(f"{tag}：缺尺寸標註 {names} = {v:g} mm（或顯示值不符）")
        for v in extra:
            res.error(f"{tag}：尺寸標註顯示 {v:g} mm，spec 無此尺寸")

        # 高程（依「<名稱>… EL=」前綴比對，不只比數值集合）
        el_texts = [e for e in ents if e.dxftype() == "TEXT" and _RE_EL.search(e.dxf.text)]
        for name, v in expected_els(d):
            pat = _el_label_re(name)
            vals = [
                float(_RE_EL.search(e.dxf.text).group(1))
                for e in el_texts
                if pat.match(_el_label(e.dxf.text))
            ]
            if any(abs(x - v) <= EL_TOL for x in vals):
                continue
            if vals:
                res.error(f"{tag}：{name} 圖面 EL={vals[0]:.2f} ≠ spec EL={v:.2f}")
            else:
                res.error(f"{tag}：圖上找不到 {name} EL={v:.2f}")
        _check_el_geometry(res, tag, d, ents, el_texts)

        # 必要註記（否定句不算：「不設逆止閥」「無溢流口」「免設…」）
        for w in required_words(d):
            if not any(_affirms(s_, w) for s_ in texts):
                res.error(
                    f"{tag}：缺必要註記「{w}」"
                    + ("（僅見否定句）" if w in "\n".join(texts) else "")
                )
        if isinstance(d, NameplateDetail) and d.capacity_m3 is not None:
            caps = [float(m.group(1)) for s_ in texts if (m := _RE_CAPACITY.match(s_.strip()))]
            if not any(abs(c - d.capacity_m3) <= 0.005 for c in caps):
                res.error(
                    f"{tag}：銘牌容量應為「{d.capacity_m3:.2f} m³」，圖面為 "
                    f"{[f'{c:g} m³' for c in caps] or '無'}"
                )

        # 剖面代號
        _check_section_letters(res, tag, d, texts)
        # 本體幾何尺寸、hatch 比例
        _check_body(res, tag, d, ents)
        _check_hatch_scale(res, tag, ents)

        # 幾何合理性
        for msg in geometry_issues(d):
            res.error(f"{tag}：{msg}")
        if isinstance(d, RaftDetentionSection):
            sl_depth = (d.storage_level_el - d.bottom_el) * 1000.0
            if abs(d.effective_depth - sl_depth) > 0.5 + 1e-6:
                res.warn(
                    f"{tag}：有效水深 {d.effective_depth:g} mm ≠ 設計滿水位 − 坑底 "
                    f"= {sl_depth:.0f} mm，請確認定義（設計滿水位 = 坑底 + 有效水深）"
                )
            if d.overflow_el is not None and d.overflow_flood_el is None:
                res.warn(
                    f"{tag}：池內溢流未給溢流時最高水位 overflow_flood_el（溢流口 + 堰頂水頭），"
                    "未檢查溢流時水位 ≤ 坑頂"
                )

        # bbox
        ext = ezbbox.extents(ents)
        if not ext.has_data:
            res.error(f"{tag}：GROUP 無可量測實體")
            continue
        b = (ext.extmin.x, ext.extmin.y, ext.extmax.x, ext.extmax.y)
        boxes[d.id] = b
        if b[0] < ix0 or b[1] < iy0 or b[2] > ix1 or b[3] > iy1:
            res.error(
                f"{tag}：超出圖框內框（bbox {b[0]:.1f},{b[1]:.1f}–{b[2]:.1f},{b[3]:.1f}；"
                f"內框 {ix0:g},{iy0:g}–{ix1:g},{iy1:g}）"
            )
        if tb_box and _overlap(b, tb_box):
            res.error(f"{tag}：與圖簽重疊")

    for msg in sheet_issues(spec):
        res.error(msg)
    check_text_anchors(res, msp)
    _check_legibility(res, doc)

    ids = list(boxes)
    for i, a in enumerate(ids):
        for bid in ids[i + 1 :]:
            if _overlap(boxes[a], boxes[bid]):
                res.error(f"詳圖 {a} 與 {bid} 的範圍重疊")

    res.metrics["details"] = [
        f"{d.id}:{d.template}:{d.section_code}:1/{d.scale}" for d in spec.details
    ]
    res.metrics["dimensions_shown"] = dims_metric
    return res


# ---------------------------------------------------------------- 文字 / 幾何（#13）


def _el_label(text: str) -> str:
    return text.split(" EL=", maxsplit=1)[0].split("EL=", maxsplit=1)[0].strip()


def _el_label_re(name: str) -> re.Pattern:
    pat = EL_LABEL_PATTERNS.get(name, re.escape(name) + "$")
    return re.compile("^" + pat)


def _affirms(text: str, word: str) -> bool:
    """text 含 word，且 word 之前沒有否定詞。"""
    start = 0
    while (i := text.find(word, start)) >= 0:
        if not any(n in text[:i] for n in _NEGATIONS):
            return True
        start = i + 1
    return False


def water_els(d) -> list[tuple[str, float]]:
    """該詳圖應畫水位線（H-WATR）的高程。"""
    out: list[tuple[str, float]] = []
    if isinstance(d, RaftDetentionSection):
        out.append(("設計滿水位", d.storage_level_el))
        if d.overflow_el is not None and d.overflow_flood_el is not None:
            out.append(("溢流時最高水位", d.overflow_flood_el))
        if d.pump is not None:
            out += [("起抽水位", d.pump.start_el), ("停機水位", d.pump.stop_el)]
    elif isinstance(d, OutletWellDetail | DitchJointDetail):
        if d.ditch.design_wl_el is not None:
            out.append(("受納溝設計水位", d.ditch.design_wl_el))
    return out


def _triangles(ents) -> list[tuple[float, float]]:
    """▽ 高程標示（H-TEXT 封閉三角形，頂點朝下）的尖端座標。"""
    out = []
    for e in ents:
        if e.dxftype() != "LWPOLYLINE" or e.dxf.layer != "H-TEXT" or not e.closed:
            continue
        pts = [(p[0], p[1]) for p in e.get_points("xy")]
        if len(pts) != 3:
            continue
        pts.sort(key=lambda p: p[1])
        if pts[1][1] - pts[0][1] > 1.0 and abs(pts[1][1] - pts[2][1]) < 0.05:
            out.append(pts[0])
    return out


def _el_marker(text_e, tris, leaders) -> tuple[float, float] | None:
    """EL 文字 → 標示點（▽ 尖端）。文字緊貼 ▽（inline）或以引線拉到註記欄。"""
    _, p, _ = text_e.get_placement()
    for ax, ay in tris:  # inline：文字底在尖端上方、左右 ≤ 3 mm
        if abs(p[1] - ay) <= 0.6 and abs(p[0] - ax) <= 3.0:
            return ax, ay
    for pts in leaders:  # 引線：最後一段水平線在文字底下 0.6 mm 並涵蓋文字插入點
        (x0, y0), (x1, y1), (x2, y2) = pts[0], pts[1], pts[2]
        if abs(y1 - y2) > 0.05 or abs(p[1] - 0.6 - y1) > 0.3:
            continue
        if not (min(x1, x2) - 0.3 <= p[0] <= max(x1, x2) + 0.3):
            continue
        if any(abs(x0 - ax) <= 0.05 and abs(y0 - ay) <= 0.05 for ax, ay in tris):
            return x0, y0
    return None


def _check_el_geometry(res, tag, d, ents, el_texts) -> None:
    """EL 文字的 ▽ 標示點 y 須與高程一致（同一詳圖內 y = a + EL × 1000 / scale）；
    文字 y 順序 = 高程順序；H-WATR 水位線 y 與水位 EL 一致。"""
    k = 1000.0 / d.scale
    tris = _triangles(ents)
    leaders = [
        [(p[0], p[1]) for p in e.get_points("xy")]
        for e in ents
        if e.dxftype() == "LWPOLYLINE" and e.dxf.layer == "H-TEXT" and not e.closed and len(e) == 3
    ]
    marks: list[tuple[str, float, float, float, tuple]] = []  # (文字, EL, 標示點 y, 文字 y, 欄)
    for e in el_texts:
        if e.dxf.get("rotation", 0.0):
            continue
        el = float(_RE_EL.search(e.dxf.text).group(1))
        m = _el_marker(e, tris, leaders)
        if m is None:
            res.error(f"{tag}：「{e.dxf.text}」找不到對應的 ▽ 高程標示點（或引線）")
            continue
        al, p, _ = e.get_placement()
        marks.append((e.dxf.text, el, m[1], p[1], (al, round(p[0], 1))))
    a = None
    if marks:
        offs = sorted(my - el * k for _, el, my, _, _ in marks)
        a = offs[len(offs) // 2]
        if len({round(el, 3) for _, el, _, _, _ in marks}) >= 2:
            for txt, el, my, _, _ in marks:
                if abs(my - (a + el * k)) > GEOM_TOL:
                    res.error(
                        f"{tag}：「{txt}」的標示點 y 與其他高程不成比例（差 "
                        f"{(my - (a + el * k)) * d.scale:.0f} mm 實尺）——標示位置與 EL 不符"
                    )
        # 同一註記欄（同 x、同對齊）內的 EL 文字，上下順序必須與高程一致
        for (ta, ea, _, ya, ca), (tb, eb, _, yb, cb) in itertools.combinations(marks, 2):
            if ca == cb and (ea - eb) * (ya - yb) < 0:
                res.error(f"{tag}：EL 文字上下順序與高程相反：「{ta}」vs「{tb}」")
    wl = water_els(d)
    lines = [e for e in ents if e.dxftype() == "LINE" and e.dxf.layer == "H-WATR"]
    if a is None:
        if wl and lines:
            res.error(f"{tag}：沒有可用的高程標示點，無法核對水位線位置")
        return
    for ln in lines:
        y = ln.dxf.start.y
        if abs(ln.dxf.end.y - y) > 1e-6:
            res.error(f"{tag}：水位線 {ln.dxf.handle} 非水平")
            continue
        if not any(abs(y - (a + el * k)) <= GEOM_TOL for _, el in wl):
            res.error(
                f"{tag}：水位線 {ln.dxf.handle} 位置相當於 EL={(y - a) / k:.2f}，"
                f"不是任何水位（{', '.join(f'{n} {v:.2f}' for n, v in wl) or '無'}）"
            )
    for n, el in wl:
        if not any(abs(ln.dxf.start.y - (a + el * k)) <= GEOM_TOL for ln in lines):
            res.error(f"{tag}：缺 {n} EL={el:.2f} 的水位線（H-WATR）")


def _check_section_letters(res, tag, d, texts) -> None:
    letters = set(d.section_code.split("-"))
    for s_ in texts:
        m = re.match(r"^([A-Z]{1,2}-[A-Z]{1,2}) 剖面$", s_.strip())
        if m and m.group(1) != d.section_code:
            res.error(f"{tag}：剖面標示「{s_}」與剖面代號 {d.section_code} 不符")
    if isinstance(d, OutletWellDetail):  # 平面圖上的剖切線字母
        marks = [s_.strip() for s_ in texts if re.fullmatch(r"[A-Z]{1,2}", s_.strip())]
        if len(marks) < 2:
            res.error(f"{tag}：平面圖缺剖切線字母（應為 {sorted(letters)}）")
        bad = [m for m in marks if m not in letters]
        if bad:
            res.error(f"{tag}：剖切線字母 {bad} 與剖面代號 {d.section_code} 不符")


def expected_body(d) -> tuple[str, float, float] | None:
    """本體外框（實尺 mm）：(量測對象, 寬, 高)。

    量測對象 "conc" = 最大的 AR-CONC hatch 邊界 bbox；"outline" = 最大的 H-STRC 封閉線 bbox。
    """
    if isinstance(d, UDitchSection):
        h = d.base_t + d.inner_depth
        if d.cover and ("RC" in d.cover.type_text or "混凝土" in d.cover.type_text):
            h += d.cover.thickness  # 混凝土蓋板與溝體同為 AR-CONC、相接
        return "conc", d.inner_width + 2 * d.wall_t, h
    if isinstance(d, RaftDetentionSection):
        L = d.n_cells * d.cell_width + (d.n_cells + 1) * d.beam_width
        return "conc", L, d.base_slab_t + d.pit_depth + d.top_slab_t
    if isinstance(d, OutletWellDetail):
        inner = d.chamber_a_length + d.partition_t + d.chamber_b_length
        return "conc", 2 * d.wall_t + inner, d.base_t + (d.top_el - d.bottom_el) * 1000.0
    if isinstance(d, DitchJointDetail):
        dt = d.ditch
        return "conc", dt.inner_width + 2 * dt.wall_t, d.base_t + (dt.top_el - dt.bottom_el) * 1000
    if isinstance(d, OutfallProtectionDetail):
        return (
            "conc",
            d.headwall_t + d.apron_length,
            d.apron_t + d.headwall_height + max(d.gravel_t, d.cutoff_depth),
        )
    if isinstance(d, OrificePlateDetail):
        return "outline", d.plate_width, d.plate_height
    if isinstance(d, NameplateDetail):
        return "outline", d.plate_width, d.plate_height
    return None


def _hatch_bbox(h) -> tuple[float, float, float, float] | None:
    xs, ys = [], []
    for path in h.paths:
        verts = getattr(path, "vertices", None)
        if verts:
            xs += [v[0] for v in verts]
            ys += [v[1] for v in verts]
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def _merge_touching(boxes, tol: float = 0.05):
    """相接 / 重疊的 bbox 合併（被管孔切開的混凝土本體視為一體）。"""
    boxes = list(boxes)
    merged = True
    while merged:
        merged = False
        for i, j in itertools.combinations(range(len(boxes)), 2):
            a, b = boxes[i], boxes[j]
            if (
                a[0] <= b[2] + tol
                and b[0] <= a[2] + tol
                and a[1] <= b[3] + tol
                and b[1] <= a[3] + tol
            ):
                boxes[i] = (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))
                del boxes[j]
                merged = True
                break
    return boxes


def _hole_gap(d) -> float:
    """本體被管孔 / 孔口切開時，碎塊間的最大縫（實尺 mm）= 最大管徑或孔口尺寸。"""
    sizes = [0.0]
    for name in ("inflow_pipe", "outlet_pipe", "pump_inflow", "overflow_inflow", "pipe"):
        p = getattr(d, name, None)
        if p is not None:
            sizes.append(p.diameter)
    for name in ("overflow_dia", "outflow_pipe_dia", "vent_diameter", "manhole_size"):
        v = getattr(d, name, None)
        if v:
            sizes.append(float(v))
    o = getattr(d, "orifice", None)
    if o is not None:
        sizes.append(max(o.size_h(), o.size_w()))
    return max(sizes)


def _check_body(res, tag, d, ents) -> None:
    exp = expected_body(d)
    if exp is None:
        return
    what, w, h = exp
    boxes = []
    if what == "conc":
        for e in ents:
            if e.dxftype() == "HATCH" and e.dxf.get("pattern_name", "").upper() == "AR-CONC":
                b = _hatch_bbox(e)
                if b:
                    boxes.append(b)
    else:
        for e in ents:
            if e.dxftype() == "LWPOLYLINE" and e.dxf.layer == "H-STRC" and e.closed:
                pts = list(e.get_points("xy"))
                boxes.append(
                    (
                        min(p[0] for p in pts),
                        min(p[1] for p in pts),
                        max(p[0] for p in pts),
                        max(p[1] for p in pts),
                    )
                )
    if not boxes:
        res.error(f"{tag}：找不到本體（{'混凝土 hatch' if what == 'conc' else 'H-STRC 外框'}）")
        return
    b = max(
        _merge_touching(boxes, _hole_gap(d) / d.scale + 0.2),
        key=lambda b: (b[2] - b[0]) * (b[3] - b[1]),
    )
    gw, gh = (b[2] - b[0]) * d.scale, (b[3] - b[1]) * d.scale
    if abs(gw - w) > w * BODY_RTOL or abs(gh - h) > h * BODY_RTOL:
        res.error(
            f"{tag}：本體幾何 {gw:.0f}×{gh:.0f} mm（實尺）與 spec {w:.0f}×{h:.0f} mm 不符"
            f"（±{BODY_RTOL:.0%}）——圖形與尺寸標註不一致"
        )


def _check_hatch_scale(res, tag, ents) -> None:
    for e in ents:
        if e.dxftype() != "HATCH" or e.dxf.get("solid_fill", 0):
            continue
        name = e.dxf.get("pattern_name", "").upper()
        if name in ("", "SOLID"):
            continue
        sc = float(e.dxf.get("pattern_scale", 1.0))
        rng = HATCH_SCALE_RANGE.get(name)
        if rng is None:
            res.warn(f"{tag}：hatch {e.dxf.handle} pattern {name} 無比例範圍表，未檢查")
        elif not rng[0] <= sc <= rng[1]:
            res.error(
                f"{tag}：hatch {e.dxf.handle} {name} pattern_scale={sc:g} 超出合理範圍 "
                f"{rng[0]:g}～{rng[1]:g}（出圖會成實心或看不出材料）"
            )


def _check_legibility(res, doc) -> None:
    from draftagent.verify.legibility_check import check_text_heights, check_text_overlaps

    for msg in check_text_heights(doc, "Model", min_mm=MIN_TEXT_MM):
        res.error(f"字高：{msg}")
    errs, warns = check_text_overlaps(doc, "Model", line_layers=("H-STRC",))
    for msg in errs:
        res.error(msg)
    for msg in warns:
        res.warn(msg)


def _overlap(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description="排水系統各部詳圖 DXF verifier",
        epilog=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("dxf")
    ap.add_argument("--spec", required=True, help="DetailSheetSpec JSON")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    emit(check_detail(args.dxf, args.spec), args.json)


if __name__ == "__main__":
    main()
