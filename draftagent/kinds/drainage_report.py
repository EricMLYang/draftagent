"""圖種 `drainage-report`：排水計畫書本文（DrainageReportSpec → 整套排水圖 + report.docx / report.md）。

render：先以 `drainage-plan` 的 render 產出整套圖與計算（重用，不複製邏輯），再讀 DXF 圖簽出附圖 PNG、
由 `draftagent.report` 組成計畫書（數字全部由程式帶入）。
verifier：drainage-plan 的全部 verifier（對 spec.network 執行）+ `report_check`。
"""

from __future__ import annotations

import functools
import json
from pathlib import Path

from draftagent.kinds import drainage_plan as dp
from draftagent.kinds._util import example
from draftagent.registry import Kind, register
from draftagent.report.model import DrainageReportSpec
from draftagent.verify.base import VerifyResult


def render(spec: DrainageReportSpec, out_dir: Path) -> dict[str, Path]:
    from draftagent.report.content import build_content
    from draftagent.report.figures import render_figures, sheet_infos
    from draftagent.report.vocab import load_format
    from draftagent.report.writers import write_docx, write_md

    out = Path(out_dir)
    arts = dp.render(spec.network, out)
    result = json.loads(Path(arts["result"]).read_text(encoding="utf-8"))
    fmt = load_format()
    sheets = sheet_infos(arts)
    order = [s.produced_by for s in fmt.sheets if s.produced_by]
    figs = render_figures(sheets, spec.report.photos, out, order)
    content = build_content(spec, result, sheets, figs)
    md = write_md(content.blocks, out / "report.md")
    docx_path, meta = write_docx(content.blocks, out / "report.docx", fmt.layout)
    meta["warnings"] = content.warnings + meta["warnings"]
    meta["figures"] = [
        {
            "path": f.path.relative_to(out).as_posix(),
            "sheet": f.sheet_name,
            "dwg_no": f.dwg_no,
            "scale": f.scale,
        }
        for f in figs
    ]
    mp = out / "report_meta.json"
    mp.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    arts.update(report_md=md, report_docx=docx_path, report_meta=mp)
    return arts


def _on_network(fn):
    @functools.wraps(fn)
    def wrapped(spec: DrainageReportSpec, artifacts: dict[str, Path]) -> VerifyResult:
        return fn(spec.network, artifacts)

    return wrapped


def verify_report(spec: DrainageReportSpec, artifacts: dict[str, Path]) -> VerifyResult:
    from draftagent.verify.report_check import check_report

    return check_report(spec, artifacts)


KIND = register(
    Kind(
        name="drainage-report",
        spec=DrainageReportSpec,
        render=render,
        verifiers=(*(_on_network(f) for f in dp.KIND.verifiers), verify_report),
        summary="排水計畫書本文：drainage-plan 整套圖與計算 + report.docx / report.md"
        "（§2a 章節、統一圖名附圖清單、數值由程式帶入並可追溯、法規數值標 yaml 來源）",
        example=example("examples/drainage-report.json"),
        tags=("docx", "md", "m", "hydro", "taoyuan", "report", "level3"),
    )
)
