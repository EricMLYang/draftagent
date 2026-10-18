"""圖種 `drainage-plan`：排水網路 spec → 整套排水圖（同一模型、數字同源）。

一份 DrainageNetworkSpec 同時產出：
- drainage_plan.dxf：modelspace TWD97 m；paperspace「平面設計圖」「集水區分析圖」
- profile_spec.json + profile.dxf：主幹線縱斷面（上游 → 進流井；滯洪池構造 + 水位）
- profile_outlet_spec.json + profile_outlet.dxf：出水側縱斷面（滯洪池 / 放流井 → 出流口 → 受納溝）
- calc_spec.json + result.json + calc.md：桃園流出抑制計算（需 storage + calc）
- detail_spec.json + detail.dxf：排水系統各部詳圖（derive_details；高程 / 容量由模型與計算結果帶入）
verifier：平面 / 兩張縱斷面 / 計算 / 詳圖 / 跨圖一致性（sheetset_check）/ 製圖標準 / 單位。

verifier 不信任中間檔：profile_spec.json / profile_outlet_spec.json / calc_spec.json / detail_spec.json
一律由 spec 重新衍生（法規 yaml 一律用 repo 預設路徑），與產物 JSON 完全相等，否則 error「中間檔與模型不符」；
各圖 verifier 一律對**重新衍生**的 spec 執行。管渠設計流量用上游集水分區面積加權 C
（derive_calc 寫入 calc spec 的 pipes[].design_c；verifier 另有獨立實作重算比對）。
"""

from __future__ import annotations

import json
import tempfile
from contextlib import contextmanager
from pathlib import Path

from draftagent.drainage.model import DrainageNetworkSpec
from draftagent.kinds._util import example, insunits_verifier, need
from draftagent.registry import Kind, register
from draftagent.verify.base import VerifyResult

DXF_KEYS = ("dxf", "profile_dxf", "profile_outlet_dxf", "detail_dxf")


def render(spec: DrainageNetworkSpec, out_dir: Path) -> dict[str, Path]:
    from draftagent.drainage.derive import (
        derive_calc,
        derive_details,
        derive_outlet_profile,
        derive_profile,
    )
    from draftagent.hydro.taoyuan import compute
    from draftagent.hydro.taoyuan_report import render_table
    from draftagent.render.detail import write_detail_sheet
    from draftagent.render.drainage_plan import write_drainage_plan
    from draftagent.render.profile import write_profile

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    arts = {"dxf": write_drainage_plan(spec, out / "drainage_plan.dxf")}

    def dump(name: str, model) -> Path:
        p = out / name
        p.write_text(model.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return p

    prof = derive_profile(spec)
    arts["profile_spec"] = dump("profile_spec.json", prof)
    arts["profile_dxf"] = write_profile(prof, out / "profile.dxf")
    oprof = derive_outlet_profile(spec)
    if oprof is not None:
        arts["profile_outlet_spec"] = dump("profile_outlet_spec.json", oprof)
        arts["profile_outlet_dxf"] = write_profile(oprof, out / "profile_outlet.dxf")

    r = None
    if spec.storage is not None and spec.calc is not None:
        calc = derive_calc(spec)
        arts["calc_spec"] = dump("calc_spec.json", calc)
        r = compute(calc)
        rp = out / "result.json"
        rp.write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
        md = out / "calc.md"
        md.write_text(render_table(r) + "\n", encoding="utf-8")
        arts.update(result=rp, calc=md)

    det = derive_details(spec, r)
    arts["detail_spec"] = dump("detail_spec.json", det)
    arts["detail_dxf"] = write_detail_sheet(det, out / "detail.dxf")
    return arts


def verify_plan(spec: DrainageNetworkSpec, artifacts: dict[str, Path]) -> VerifyResult:
    from draftagent.verify.drainage_plan_check import NAME, check_drainage_plan

    p = need(artifacts, "dxf", NAME)
    if isinstance(p, VerifyResult):
        return p
    return check_drainage_plan(p, spec)


MISMATCH = "中間檔與模型不符"


def locked(spec: DrainageNetworkSpec) -> DrainageNetworkSpec:
    """verifier 用：法規 yaml 一律換成 repo 預設路徑（spec 指定的路徑由 drainage_plan_check 報 error）。"""
    from draftagent.schema.drainage import DEFAULT_CODES_PATH

    return spec.model_copy(update={"codes_path": DEFAULT_CODES_PATH})


def compare_intermediate(res: VerifyResult, label: str, derived, path: Path) -> None:
    """產物 JSON 必須與由 spec 重新衍生的模型完全相等（model_dump(mode="json") 逐鍵比對）。"""
    try:
        got = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        res.error(f"{MISMATCH}：{Path(path).name} 無法讀取（{e}）")
        return
    want = json.loads(derived.model_dump_json())
    if got == want:
        return
    diffs = _json_diffs(want, got)
    head = "；".join(diffs[:4]) + ("…" if len(diffs) > 4 else "")
    res.error(f"{MISMATCH}：{Path(path).name} 與由 spec 重新衍生的 {label} 不同（{len(diffs)} 處：{head}）")


def _json_diffs(a, b, prefix: str = "") -> list[str]:
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in sorted(set(a) | set(b), key=str):
            p = f"{prefix}.{k}" if prefix else str(k)
            if k not in a:
                out.append(f"{p} 多出")
            elif k not in b:
                out.append(f"{p} 缺少")
            else:
                out += _json_diffs(a[k], b[k], p)
        return out
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return [f"{prefix} 長度 {len(b)} ≠ 重新衍生 {len(a)}"]
        out = []
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            out += _json_diffs(x, y, f"{prefix}[{i}]")
        return out
    return [] if a == b else [f"{prefix} = {b!r}（重新衍生 {a!r}）"]


@contextmanager
def _tmp_json(name: str, model):
    with tempfile.TemporaryDirectory(prefix="draftagent-derive-") as d:
        p = Path(d) / name
        p.write_text(model.model_dump_json(indent=2) + "\n", encoding="utf-8")
        yield p


def _verify_profile(spec, artifacts, dxf_key: str, spec_key: str, name: str, derived):
    from draftagent.verify.profile_check import check_profile

    p = need(artifacts, dxf_key, name)
    if isinstance(p, VerifyResult):
        return p
    s = need(artifacts, spec_key, name)
    if isinstance(s, VerifyResult):
        return s
    with _tmp_json(Path(s).name, derived) as ds:
        res = check_profile(p, ds)
    res.name = name
    compare_intermediate(res, "縱斷面 ProfileSpec", derived, s)
    return res


def verify_profile(spec: DrainageNetworkSpec, artifacts: dict[str, Path]) -> VerifyResult:
    from draftagent.drainage.derive import derive_profile

    name = "profile_check"
    try:
        derived = derive_profile(locked(spec))
    except ValueError as e:
        res = VerifyResult(name=name)
        res.error(f"無法由 spec 衍生縱斷面：{e}")
        return res
    return _verify_profile(spec, artifacts, "profile_dxf", "profile_spec", name, derived)


def verify_profile_outlet(spec: DrainageNetworkSpec, artifacts: dict[str, Path]) -> VerifyResult:
    from draftagent.drainage.derive import derive_outlet_profile, outlet_path

    name = "profile_check[outlet]"
    if outlet_path(spec) is None:
        res = VerifyResult(name=name)
        res.warn("沒有滯洪設施放流井 → 出流口的管段路徑，未產出水側縱斷面")
        for key in ("profile_outlet_spec", "profile_outlet_dxf"):
            if artifacts.get(key) is not None and Path(artifacts[key]).exists():
                res.error(f"{MISMATCH}：模型沒有出水側路徑，卻有產物 {Path(artifacts[key]).name}")
        return res
    derived = derive_outlet_profile(locked(spec))
    return _verify_profile(
        spec, artifacts, "profile_outlet_dxf", "profile_outlet_spec", name, derived
    )


def _recompute(spec: DrainageNetworkSpec):
    """(重新衍生的 calc spec, 重算結果)；沒有 storage / calc → (None, None)。"""
    from draftagent.drainage.derive import derive_calc
    from draftagent.hydro.taoyuan import compute

    if spec.storage is None or spec.calc is None:
        return None, None
    sp = locked(spec)
    calc = derive_calc(sp)
    return calc, compute(calc)


def verify_calc(spec: DrainageNetworkSpec, artifacts: dict[str, Path]) -> VerifyResult:
    from draftagent.verify.drainage_calc_check import NAME, check
    from draftagent.verify.drainage_plan_check import check_network_calc

    if spec.storage is None or spec.calc is None:
        res = VerifyResult(name=NAME)
        res.warn("spec 未提供 storage / calc，未衍生流出抑制計算，略過")
        return res
    s = need(artifacts, "calc_spec", NAME)
    if isinstance(s, VerifyResult):
        return s
    r = need(artifacts, "result", NAME)
    if isinstance(r, VerifyResult):
        return r
    try:
        calc, _ = _recompute(spec)
    except (ValueError, KeyError) as e:
        res = VerifyResult(name=NAME)
        res.error(f"無法由 spec 衍生流出抑制計算：{e}")
        return res
    # drainage_calc_check 直接對「重新衍生」的 calc spec 判定（管渠用 design_c = 上游集水分區加權 C 重算），
    # 並以重算結果逐值比對 result.json、逐字比對 calc.md；
    # check_network_calc 另做網路獨立不變量與管渠設計流量的獨立實作交叉驗證
    with _tmp_json("calc_spec.json", calc) as ds:
        res = check(ds, r, artifacts.get("calc"))
    res.metrics["spec"] = str(s)
    compare_intermediate(res, "流出抑制計算 TaoyuanSiteDrainageSpec", calc, s)
    check_network_calc(res, spec, calc, r)
    return res


def verify_detail(spec: DrainageNetworkSpec, artifacts: dict[str, Path]) -> VerifyResult:
    from draftagent.drainage.derive import derive_details
    from draftagent.verify.detail_check import check_detail

    p = need(artifacts, "detail_dxf", "detail_check")
    if isinstance(p, VerifyResult):
        return p
    s = need(artifacts, "detail_spec", "detail_check")
    if isinstance(s, VerifyResult):
        return s
    try:
        _, recomputed = _recompute(spec)
        derived = derive_details(locked(spec), recomputed)
    except (ValueError, KeyError) as e:
        res = VerifyResult(name="detail_check")
        res.error(f"無法由 spec 衍生詳圖：{e}")
        return res
    with _tmp_json("detail_spec.json", derived) as ds:
        res = check_detail(p, ds)
    compare_intermediate(res, "詳圖 DetailSheetSpec", derived, s)
    return res


def verify_sheetset(spec: DrainageNetworkSpec, artifacts: dict[str, Path]) -> VerifyResult:
    from draftagent.verify.sheetset_check import check_sheetset

    return check_sheetset(artifacts, spec)


def verify_standards(spec: DrainageNetworkSpec, artifacts: dict[str, Path]) -> VerifyResult:
    """layers.yaml 製圖標準：整套圖每個 DXF 都檢查，合併成一個結果。"""
    from draftagent.verify.standards_check import check_standards

    res = VerifyResult(name="standards_check")
    for key in DXF_KEYS:
        p = artifacts.get(key)
        if p is None:
            continue
        r = check_standards(p)
        tag = Path(p).name
        for e in r.errors:
            res.error(f"{tag}：{e}")
        for w in r.warnings:
            res.warn(f"{tag}：{w}")
        res.metrics[tag] = {k: r.metrics[k] for k in ("layers_checked", "unknown_layers", "color_override")}
    return res


KIND = register(
    Kind(
        name="drainage-plan",
        spec=DrainageNetworkSpec,
        render=render,
        verifiers=(
            verify_plan,
            verify_profile,
            verify_profile_outlet,
            verify_calc,
            verify_detail,
            verify_sheetset,
            verify_standards,
            insunits_verifier,
        ),
        summary="排水網路 → 整套排水圖：平面設計圖 + 集水區分析圖（TWD97 m）+ 主幹 / 出水側縱斷面"
        " + 各部詳圖 + 流出抑制計算（同一模型、跨圖一致性檢查）",
        example=example("examples/drainage-plan.json"),
        tags=("dxf", "m", "gis", "hydro", "taoyuan", "sheet", "level2"),
    )
)
