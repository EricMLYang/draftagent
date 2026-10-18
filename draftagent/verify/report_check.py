"""排水計畫書本文 verifier（drainage-report）：report.md / report.docx 對重算結果與 DXF 圖簽。

    python -m draftagent.verify.report_check <run_dir> [--spec <run_dir>/spec.json] [--json]

檢查（不信任 result.json / report_meta.json：數值一律由 spec 重算，法規數值讀 repo 預設 yaml）：
1. 章節：Heading 1 依序 = `report/codes/report_format.yaml` chapters + 附圖，不得缺 / 多 / 換序
2. 表格：每張表的表名（表上一段）必須是已知表名；必要表都在、不重複；逐列逐欄與重算值比對
   （數值容差 = 顯示位數的半個單位：顯示值必須是正確的四捨五入）
3. 「標籤 = 數值」：段落 / 條列中每個「= 數字」的標籤都要在詞彙表內（否則 error「無法追溯」），
   數值 = 重算值；法規數值標籤必須附〔yaml：檔名：路徑；出處；verified: …〕且路徑值 = 數值
4. yaml 來源標註：每個〔yaml：…〕的檔名 / 路徑都要存在於 repo 預設 yaml；yaml verified: false 時
   標註必須寫 verified: false；且本文要有 verified: false 提醒段（尚未人工核對原文）
5. 附圖：統一圖名對照表 14 項依序；本 repo 產出的圖號 / 比例 = DXF 圖簽 = spec（drawing_no / scales），
   檢核表比例 = taoyuan_drawing.yaml；附圖圖說的圖名 / 圖號 / 比例 = DXF 圖簽；
   嵌入圖片數 = 附圖數 = DXF 圖紙數 + 照片數；每張圖存在、非空白（PIL 灰階 ≥ 0.1% 非白像素）
6. docx 與 md 內容一致：依序比對（標題 / 章 / 節 / 段落 / 條列 / 表格 / 圖片雜湊）
7. 固定結論句必須出現；不得有肯定語境的合規宣稱（引號內、同子句前有否定詞不算）；
   提到淹水潛勢的句子必須是風險提示語氣
8. 不得含疑似真實案件識別資訊（`scripts/check_no_client_ids.py` 的 pattern）
9. docx 版面：A4、邊界 = report_format.yaml；Normal 字型（西文 / 東亞）= report_meta.json 且屬 yaml 允許清單；
   有目錄（TOC）功能變數

已知限制：只追溯表格與「=」後的數字，段落中其他數字（條號、日期）不檢查；補充文字的數字由 schema 擋
（國字數字擋不到）；合規宣稱為啟發式（同 eval 的否定語境判斷，獨立實作）。純函式，不得呼叫 LLM。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

from draftagent.verify.base import VerifyResult, emit

NAME = "report_check"
PING = 0.3025

# ---------------------------------------------------------------- 合規宣稱 / 淹水潛勢（獨立實作）

# pattern 抄自 draftagent/eval/tasks.py COMPLIANCE_CLAIM（verifier 不得 import eval；兩者要同步）
COMPLIANCE_CLAIM = (
    r"(符合|合乎|滿足|達到)(相關|所有|全部)?(法規|法令|規定|規範要求|審查標準)"
    r"|(compl(?:y|ies|iant|iance)\s+with|conforms?\s+to|meets?|satisf(?:y|ies))\s+"
    r"(?:the\s+|all\s+|relevant\s+|applicable\s+)*(?:regulations?|codes?|building\s+codes?|legal\s+requirements)"
)
_QUOTES = {"「": "」", "『": "』", "“": "”", '"': '"', "《": "》"}
_CLAUSE = re.compile(r"[。！？!?；;\n，,]|\.(?=\s)")
_NEGATION = re.compile(
    r"(不|非|未|無|沒|勿|別|禁止|是否|能否|避免|拒絕|並非)"
    r"|(?i:\b(?:not|never|no|without|cannot|whether)\b|n't)"
)
FLOOD = "淹水潛勢"
FLOOD_OK = re.compile(r"風險提示|僅供|只供|防救災|不得作為|不作為|不能作為|不可作為")

# pattern 抄自 scripts/check_no_client_ids.py PATTERNS（scripts/ 不是套件；tests/test_report.py 檢查兩者相同）
_CJK = r"[一-鿿]"
CLIENT_ID_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("地號（數字+地號）", re.compile(r"\d+(?:-\d+)?\s*地號")),
    ("地號（段+數字+地號）", re.compile(r"段\s*\d+(?:-\d*)?\s*地號")),
    ("地號（地號+數字）", re.compile(r"地號\s*[:：]?\s*\d+(?:-\d+)?")),
    ("顧問公司", re.compile(_CJK + r"{2,}(?:工程)?(?:技術)?顧問(?:股份)?有限公司")),
    ("顧問公司（簡稱）", re.compile(_CJK + r"{2,}工程技術顧問(?!(?:股份)?有限公司)")),
    ("建築師事務所", re.compile(_CJK + r"{2,}建築師事務所")),
    ("營造 / 測量 / 工程公司", re.compile(_CJK + r"{2,}(?:營造|測量|工程)(?:股份)?有限公司")),
    ("案件編號", re.compile(r"(?<![A-Za-z0-9])C\d{5}[A-Z]{2}(?![A-Za-z0-9])")),
]


def _in_quotes(text: str, pos: int) -> bool:
    stack: list[str] = []
    for ch in text[text.rfind("\n", 0, pos) + 1 : pos]:
        if stack and ch == _QUOTES[stack[-1]]:
            stack.pop()
        elif ch in _QUOTES:
            stack.append(ch)
    return bool(stack)


def compliance_claims(text: str) -> list[str]:
    out = []
    for m in re.finditer(COMPLIANCE_CLAIM, text, re.I):
        if _in_quotes(text, m.start()):
            continue
        starts = [x.end() for x in _CLAUSE.finditer(text, 0, m.start())]
        cs = starts[-1] if starts else 0
        if _NEGATION.search(text[cs : m.start()]):
            continue
        end = _CLAUSE.search(text, m.end())
        out.append(text[cs : end.start() if end else len(text)].strip()[:120])
    return out


# ---------------------------------------------------------------- 數字

_NUM = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def _nums(s: str) -> list[tuple[float, int]]:
    """[(值, 小數位數)]；千分位逗號去掉。"""
    out = []
    for m in _NUM.finditer(s):
        t = m.group().replace(",", "")
        if t in ("-", ""):
            continue
        out.append((float(t), len(t.split(".")[1]) if "." in t else 0))
    return out


def _tol(decimals: int) -> float:
    return 0.5 * 10 ** (-decimals) + 1e-9


class _Ctx:
    def __init__(self, res: VerifyResult) -> None:
        self.res = res
        self.compared = 0

    def num(self, where: str, cell: str, want: float, idx: int = 0) -> None:
        ns = _nums(cell)
        if len(ns) <= idx:
            self.res.error(f"{where}：「{cell}」找不到數值（應為 {want:.6g}）")
            return
        got, dp = ns[idx]
        self.compared += 1
        if not math.isfinite(want) or abs(got - want) > _tol(dp) + 1e-9 * abs(want):
            self.res.error(
                f"{where}：「{cell}」= {got:g}，重算值 {want:.6g}（差 {got - want:+.6g}）"
            )

    def nums(self, where: str, cell: str, want: list[float]) -> None:
        ns = _nums(cell)
        if len(ns) != len(want):
            self.res.error(f"{where}：「{cell}」數值個數 {len(ns)} ≠ {len(want)}（應為 {want}）")
            return
        for i, w in enumerate(want):
            self.num(where, cell, w, i)

    def text(self, where: str, got: str, want: str) -> None:
        self.compared += 1
        if " ".join(str(got).split()) != " ".join(str(want).split()):
            self.res.error(f"{where}：「{got}」≠ 應為「{want}」")


# ---------------------------------------------------------------- md / docx 解析


def parse_md(path: Path) -> list[tuple]:
    items: list[tuple] = []
    rows: list[list[str]] = []

    def flush() -> None:
        if rows:
            body = [r for r in rows if not set("".join(r)) <= set("-: ")]
            items.append(("table", tuple(tuple(r) for r in body)))
            rows.clear()

    for ln in Path(path).read_text(encoding="utf-8").splitlines():
        s = ln.strip()
        if s.startswith("|"):
            rows.append([c.strip() for c in s.strip("|").split("|")])
            continue
        flush()
        if not s:
            continue
        m = re.fullmatch(r"!\[[^\]]*\]\(([^)]+)\)", s)
        if m:
            p = (Path(path).parent / m.group(1)).resolve()
            items.append(("img", _sha(p) if p.exists() else f"missing:{m.group(1)}", str(p)))
        elif s.startswith("### "):
            items.append(("h2", s[4:].strip()))
        elif s.startswith("## "):
            items.append(("h1", s[3:].strip()))
        elif s.startswith("# "):
            items.append(("title", s[2:].strip()))
        elif s.startswith("- "):
            items.append(("li", s[2:].strip()))
        else:
            items.append(("p", s))
    flush()
    return items


def _sha(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def parse_docx(path: Path) -> tuple[list[tuple], Any]:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    d = docx.Document(str(path))
    items: list[tuple] = []
    style_type = {"Title": "title", "Heading 1": "h1", "Heading 2": "h2", "List Bullet": "li"}
    for el in d.element.body.iterchildren():
        tag = el.tag.split("}")[-1]
        if tag == "tbl":
            t = Table(el, d)
            items.append(
                ("table", tuple(tuple(" ".join(c.text.split()) for c in r.cells) for r in t.rows))
            )
        elif tag == "p":
            p = Paragraph(el, d)
            blips = el.findall(".//{http://schemas.openxmlformats.org/drawingml/2006/main}blip")
            for b in blips:
                rid = b.get(
                    "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"
                )
                blob = d.part.related_parts[rid].blob
                items.append(("img", hashlib.sha256(blob).hexdigest(), rid))
            text = " ".join(p.text.split())
            if text:
                items.append((style_type.get(p.style.name, "p"), text))
    return items, d


def _cmp_key(it: tuple) -> tuple:
    return it[:2]


# ---------------------------------------------------------------- DXF 圖簽（獨立讀取）

DXF_SHEETS = (
    ("dxf", "平面設計圖", "plan"),
    ("profile_dxf", None, "profile"),
    ("profile_outlet_dxf", None, "profile_outlet"),
    ("detail_dxf", None, "detail"),
    ("dxf", "集水區分析圖", "catchment"),
)


def dxf_title_blocks(artifacts: dict[str, Path]) -> list[dict[str, str]]:
    import ezdxf

    out = []
    for key, layout, which in DXF_SHEETS:
        p = artifacts.get(key)
        if p is None or not Path(p).exists():
            continue
        doc = ezdxf.readfile(str(p))
        lay = doc.modelspace() if layout is None else doc.layouts.get(layout)
        tb = None
        for ins in lay.query("INSERT"):
            if ins.attribs and ins.dxf.name.upper().startswith("TTLB"):
                tb = {a.dxf.tag.upper(): " ".join(a.dxf.text.split()) for a in ins.attribs}
                break
        out.append({"which": which, "file": Path(p).name, **(tb or {})})
    return out


def _png_blank(p: Path) -> str | None:
    from PIL import Image

    try:
        with Image.open(p) as im:
            g = im.convert("L")
            w, h = g.size
            dark = sum(g.point(lambda v: 255 if v < 250 else 0).histogram()[255:])
    except OSError as e:
        return f"無法讀取（{e}）"
    if w < 100 or h < 100:
        return f"尺寸過小 {w}×{h}"
    if dark < 0.001 * w * h:
        return f"空白（非白像素 {dark}/{w * h}）"
    return None


# ---------------------------------------------------------------- 主檢查


def check_report(spec, artifacts: dict[str, Path]) -> VerifyResult:
    from draftagent.drainage.derive import quantities
    from draftagent.hydro.drawing_codes import load_drawing_codes
    from draftagent.hydro.taoyuan import load_codes
    from draftagent.kinds.drainage_plan import _recompute, locked
    from draftagent.report import vocab as V
    from draftagent.report.model import DrainageReportSpec
    from draftagent.report.vocab import load_format

    res = VerifyResult(name=NAME)
    if not isinstance(spec, DrainageReportSpec):
        spec = DrainageReportSpec.from_json(spec)
    for key in ("report_md", "report_docx"):
        p = artifacts.get(key)
        if p is None or not Path(p).exists():
            res.error(f"找不到產物 {key}（artifacts: {sorted(artifacts)}）")
    if not res.ok:
        return res
    md_path, docx_path = Path(artifacts["report_md"]), Path(artifacts["report_docx"])
    fmt = load_format()
    net = locked(spec.network)
    codes = load_codes(net.codes_path)
    dcodes = load_drawing_codes()
    try:
        _, r = _recompute(spec.network)
    except (ValueError, KeyError) as e:
        res.error(f"無法由 spec 重算流出抑制計算：{e}")
        return res
    ctx = _Ctx(res)

    md = parse_md(md_path)
    try:
        dx, ddoc = parse_docx(docx_path)
    except Exception as e:  # noqa: BLE001 - 損毀的 docx 要變紅燈
        res.error(f"report.docx 無法讀取：{type(e).__name__}: {e}")
        return res

    # ------------------------------------------------ 6. md ↔ docx
    a, b = [_cmp_key(x) for x in md], [_cmp_key(x) for x in dx]
    if a != b:
        diffs = [
            i
            for i in range(max(len(a), len(b)))
            if (a[i] if i < len(a) else None) != (b[i] if i < len(b) else None)
        ]
        i = diffs[0]
        show = lambda L: str(L[i])[:100] if i < len(L) else "（無）"  # noqa: E731
        res.error(
            f"report.md 與 report.docx 內容不一致（{len(diffs)} 處；md {len(a)} 項 / docx {len(b)} 項）："
            f"第 {i + 1} 項 md={show(a)} vs docx={show(b)}"
        )

    texts = [x[1] for x in md if x[0] in ("title", "h1", "h2", "p", "li")]
    tables: list[tuple[str, tuple]] = []
    for i, x in enumerate(md):
        if x[0] == "table":
            cap = md[i - 1][1] if i > 0 and md[i - 1][0] == "p" else ""
            tables.append((cap, x[1]))
    all_text = "\n".join(texts + [" ".join(c) for _, t in tables for row in t for c in [row]])

    # ------------------------------------------------ 1. 章節
    want_h1 = [t for _, t in fmt.chapters] + [fmt.appendix_title]
    got_h1 = [x[1] for x in md if x[0] == "h1"]
    if got_h1 != want_h1:
        missing = [t for t in want_h1 if t not in got_h1]
        extra = [t for t in got_h1 if t not in want_h1]
        res.error(
            f"章節不符 report_format.yaml：缺 {missing}、多 {extra}"
            + ("、順序不同" if not missing and not extra else "")
            + f"（應為 {want_h1}）"
        )
    res.metrics["chapters"] = got_h1

    # ------------------------------------------------ 7. 固定句 / 合規宣稱 / 淹水潛勢
    if V.CONCLUSION not in texts:
        res.error(f"缺固定結論句：「{V.CONCLUSION}」")
    claims = compliance_claims(all_text)
    for c in claims:
        res.error(f"肯定式合規宣稱（只能說通過本 repo 現有 verifier 的檢查項目）：「{c}」")
    for sent in re.split(r"[。\n]", all_text):
        if FLOOD in sent and not FLOOD_OK.search(sent):
            res.error(
                f"提到淹水潛勢但不是風險提示（淹水潛勢圖不得作為管制 / 設計依據）：「{sent.strip()[:80]}」"
            )

    # ------------------------------------------------ 8. 識別資訊
    for t in texts + [c for _, tb in tables for row in tb for c in row]:
        for kind, rx in CLIENT_ID_PATTERNS:
            m = rx.search(t)
            if m:
                res.error(f"疑似真實案件識別資訊（{kind}）：「{m.group(0)}」｜{t[:60]}")

    # ------------------------------------------------ 4. yaml 來源標註 / verified 提醒
    yaml_files = {"taoyuan_runoff.yaml": (codes, bool(codes.get("verified"))),
                  "taoyuan_drawing.yaml": (None, dcodes.verified)}  # fmt: skip
    tag_rx = re.compile(
        re.escape(V.SRC_OPEN + V.SRC_PREFIX)
        + r"([^：〕]+)：([^；〕]+)；([^〕]*)"
        + re.escape(V.SRC_CLOSE)
    )
    n_tags = 0
    for t in texts:
        for m in tag_rx.finditer(t):
            n_tags += 1
            _check_tag(res, m.group(1), m.group(2), m.group(3), yaml_files, dcodes, V)
    unverified = [f for f, (_, ok) in yaml_files.items() if not ok]
    if unverified and not any(
        V.VERIFIED_NOTICE_HEAD in t and V.VERIFIED_FALSE in t and "尚未人工核對" in t for t in texts
    ):
        res.error(
            f"缺 verified: false 提醒段（{V.VERIFIED_NOTICE_HEAD}… {', '.join(unverified)} 為 {V.VERIFIED_FALSE}、尚未人工核對原文）"
        )
    res.metrics["yaml_tags"] = n_tags

    # ------------------------------------------------ 3. 「標籤 = 數值」
    exprs = _expressions(res, ctx, texts, spec, net, r, codes, V)
    res.metrics["expressions_checked"] = exprs

    # ------------------------------------------------ 2. 表格
    seen: dict[str, int] = {}
    for cap, _t in tables:
        if cap not in V.TABLES:
            res.error(f"未知表格（表名「{cap}」不在詞彙表，數值無法追溯）")
            continue
        seen[cap] = seen.get(cap, 0) + 1
    for cap, n in seen.items():
        if n > 1:
            res.error(f"表格「{cap}」重複 {n} 次")
    required = [V.T_LAND, V.T_SITE, V.T_CODES, V.T_HORNER, V.T_RUNOFF_C, V.T_CATCH,
                V.T_STORAGE, V.T_RELEASE, V.T_COST, V.T_SHEETS]  # fmt: skip
    if r["pipes"]:
        required.append(V.T_PIPES)
    if net.existing_drains:
        required.append(V.T_EXISTING)
    if spec.report.photos:
        required.append(V.T_PHOTOS)
    for cap in required:
        if cap not in seen:
            res.error(f"缺表格「{cap}」")
    tb = dict(tables)
    rv = bool(codes.get("verified"))
    flag = V.VERIFIED_TRUE if rv else V.VERIFIED_FALSE
    checkers = {
        V.T_LAND: lambda t: _t_land(ctx, t, spec),
        V.T_SITE: lambda t: _t_site(ctx, t, spec, net),
        V.T_CODES: lambda t: _t_codes(ctx, t, codes, flag),
        V.T_HORNER: lambda t: _t_horner(ctx, t, r, codes, flag),
        V.T_RUNOFF_C: lambda t: _t_runoff_c(ctx, t, r, codes, flag),
        V.T_EXISTING: lambda t: _t_existing(ctx, t, net),
        V.T_PHOTOS: lambda t: _t_photos(ctx, t, spec),
        V.T_CATCH: lambda t: _t_catch(ctx, t, net, codes),
        V.T_PIPES: lambda t: _t_pipes(ctx, t, net, r),
        V.T_STORAGE: lambda t: _t_storage(ctx, t, net, r, codes),
        V.T_RELEASE: lambda t: _t_release(ctx, t, net, r),
        V.T_COST: lambda t: _t_cost(ctx, t, spec, quantities(net)),
    }
    for cap, fn in checkers.items():
        if cap in tb:
            try:
                fn(tb[cap])
            except (KeyError, IndexError, ValueError) as e:
                res.error(f"{cap}：格式無法解析（{type(e).__name__}: {e}）")

    # ------------------------------------------------ 5. 附圖
    tbs = dxf_title_blocks(artifacts)
    _figures(res, ctx, md, dx, tb.get(V.T_SHEETS), tbs, spec, net, fmt, dcodes, V, md_path)

    # ------------------------------------------------ 9. docx 版面 / 字型 / 目錄
    _docx_layout(res, ddoc, fmt, artifacts)

    # ------------------------------------------------ warnings
    if not fmt.verified:
        res.warn(f"計畫書格式 yaml verified: false — {fmt.note}（{fmt.path.name}）")
    if not rv:
        res.warn(
            f"法規 yaml verified: false — {str(codes.get('note', '')).strip()}（taoyuan_runoff.yaml）"
        )
    for label, v in (
        ("建蔽率", spec.report.coverage_ratio_pct),
        ("開挖率", spec.report.excavation_ratio_pct),
    ):
        if v is None:
            res.warn(f"{label}未提供（使用者須提供）")
    res.metrics["values_compared"] = ctx.compared
    res.metrics["tables"] = len(tables)
    return res


# ---------------------------------------------------------------- 標註


def _check_tag(res, file: str, path: str, rest: str, yaml_files, dcodes, V) -> None:
    if file not in yaml_files:
        res.error(f"yaml 來源標註檔名「{file}」不是 repo 法規 yaml（{sorted(yaml_files)}）")
        return
    data, ok = yaml_files[file]
    if file == "taoyuan_drawing.yaml":
        if path != "scales" and not (
            path.startswith("scales.") and path.split(".", 1)[1] in dcodes.scales
        ):
            res.error(f"yaml 來源標註 {file}：{path} 不存在")
    else:
        try:
            _get(data, path)
        except (KeyError, TypeError):
            res.error(f"yaml 來源標註 {file}：{path} 不存在")
            return
    want = V.VERIFIED_TRUE if ok else V.VERIFIED_FALSE
    if want not in rest:
        res.error(
            f"yaml 來源標註 {file}：{path} 未註明「{want}」（yaml 實際 verified: {str(ok).lower()}）"
        )


def _get(d: Any, path: str) -> Any:
    for k in path.split("."):
        d = d[k]
    return d


# ---------------------------------------------------------------- 「=」


def _expressions(res, ctx: _Ctx, texts, spec, net, r, codes, V) -> int:
    from shapely.geometry import Polygon

    from draftagent.drainage.derive import quantities

    A = Polygon(net.site_boundary).area
    As = r["As"]["As_m2"]
    cls = codes["runoff_coefficients"]["classes"]
    ca = [(Polygon(s.polygon).area, s.c_override if s.c_override is not None else cls[s.landuse]["median"])
          for s in net.subcatchments]  # fmt: skip
    c_comp = sum(a * c for a, c in ca) / sum(a for a, _ in ca)
    k_ty = codes["runoff_standard"]["min_storage_m3_per_m2"]
    k_bc = codes["building_code_4_3"]["min_storage_m3_per_m2"]
    total, missing = _cost_total(spec, quantities(net))
    want: dict[str, tuple[float, float | None]] = {
        V.L_SITE_AREA: (A, A * PING),
        V.L_AS: (As, As * PING),
        V.L_C: (c_comp, None),
        V.L_V_REQ: (max(k_ty, k_bc) * As, None),
        V.L_V_PROV: (r["storage_provided"]["provided_m3"]["value"], None),
        V.L_Q_ALLOW: (codes["runoff_standard"]["max_release_cms_per_m2"] * As, None),
        V.L_Q_OUT: (r["outlet"]["Q_cms"]["value"], None),
        V.L_RATIO: (
            100 * r["outlet"]["Q_cms"]["value"] / r["release"]["allowed_cms"]["value"],
            None,
        ),
        V.L_Q20: (r["rainfall"]["storage"]["Q_site_cms"]["value"], None),
    }
    if r.get("overflow_weir"):
        want[V.L_Q_WEIR] = (r["overflow_weir"]["Q_cms"]["value"], None)
    want[V.L_COST_PARTIAL if missing else V.L_COST_TOTAL] = (total, None)
    for label, (_, path) in V.REGULATORY_LABELS.items():
        want[label] = (float(_get(codes, path)), None)
    rx = re.compile(r"([^，。；：、\n=]+?)\s*=\s*(-?\d[\d,]*(?:\.\d+)?)([^，。；\n]*)")
    n = 0
    found: set[str] = set()
    for t in texts:
        for m in rx.finditer(t):
            label = m.group(1).strip()
            label = (
                re.sub(r"^.*[（(]", "", label) if label.count("（") > label.count("）") else label
            )
            n += 1
            if label not in want:
                res.error(
                    f"「{label} = {m.group(2)}」：標籤不在詞彙表，數值無法追溯（只能用程式產生的數值標籤）"
                )
                continue
            found.add(label)
            val, ping = want[label]
            ctx.num(f"「{label}」", m.group(2), val)
            tail = m.group(3)
            if ping is not None:
                pm = re.search(r"（\s*(" + _NUM.pattern + r")\s*坪）", tail)
                if pm is None:
                    res.error(f"「{label}」面積未附坪（× {PING}）")
                else:
                    ctx.num(f"「{label}」坪", pm.group(1), ping)
            if label in V.REGULATORY_LABELS:
                file, path = V.REGULATORY_LABELS[label]
                if (
                    not t[m.end(2) :]
                    .lstrip()
                    .split(V.SRC_OPEN, 1)[-1]
                    .startswith(f"{V.SRC_PREFIX}{file}：{path}；")
                ):
                    res.error(
                        f"法規數值「{label}」旁缺 yaml 來源標註〔{V.SRC_PREFIX}{file}：{path}；…〕"
                    )
    for label in (V.L_SITE_AREA, V.L_V_REQ, V.L_V_PROV, V.L_Q_ALLOW, V.L_Q_OUT):
        if label not in found:
            res.error(f"摘要缺「{label} = …」")
    for label in V.REGULATORY_LABELS:
        if label not in found:
            res.error(f"缺法規數值「{label} = …」（附 yaml 來源）")
    if V.L_COST_TOTAL in found and missing:
        res.error(f"有未提供單價項目（{'、'.join(missing)}），不得寫「{V.L_COST_TOTAL}」")
    return n


def _cost_total(spec, qrows) -> tuple[float, list[str]]:
    total, missing = 0.0, []
    for q in qrows:
        ps = [
            u
            for u in spec.report.unit_prices
            if u.item == q.item and (u.spec is None or u.spec == q.spec)
        ]
        if ps:
            total += round(q.qty * ps[0].unit_price)
        else:
            missing.append(f"{q.item}（{q.spec}）")
    for x in spec.report.extra_costs:
        if x.unit_price is None:
            missing.append(f"{x.item}（{x.spec}）")
        else:
            total += round(x.qty * x.unit_price)
    return total, missing


# ---------------------------------------------------------------- 表格檢查器（header 在 t[0]）


def _body(ctx: _Ctx, cap: str, t, header: list[str]) -> list[tuple[str, ...]]:
    if list(t[0]) != header:
        ctx.res.error(f"{cap}：表頭 {list(t[0])} ≠ {header}")
    return list(t[1:])


def _by_item(t) -> dict[str, tuple[str, ...]]:
    return {row[0]: row for row in t[1:]}


def _t_land(ctx, t, spec) -> None:
    from draftagent.report import vocab as V

    rows = _body(ctx, V.T_LAND, t, ["項次", "段名", "地號"])
    want = [(str(i), x.section, x.lot) for i, x in enumerate(spec.report.land, 1)]
    if [tuple(r) for r in rows] != want:
        ctx.res.error(f"{V.T_LAND}：{[tuple(r) for r in rows]} ≠ spec {want}")
    ctx.compared += 1


def _t_site(ctx, t, spec, net) -> None:
    from shapely.geometry import Polygon

    from draftagent.report import vocab as V

    cap = V.T_SITE
    _body(ctx, cap, t, ["項目", "內容", "來源"])
    d = _by_item(t)
    info = spec.report
    for k, v in (
        ("行政區", net.project.district),
        ("基地位置", net.project.location or "—"),
        ("使用分區", info.zoning),
    ):
        ctx.text(f"{cap} {k}", d[k][1], v)
    for k, v in (("建蔽率", info.coverage_ratio_pct), ("開挖率", info.excavation_ratio_pct)):
        if v is None:
            ctx.text(f"{cap} {k}", d[k][1], V.NOT_GIVEN)
        else:
            ctx.num(f"{cap} {k}", d[k][1], v)
    A = Polygon(net.site_boundary).area
    ctx.nums(f"{cap} 基地面積", d["基地面積"][1], [A, A * PING])
    if net.building_footprints:
        bf = sum(Polygon(p).area for p in net.building_footprints)
        ctx.nums(f"{cap} 建物投影面積", d["建物投影面積"][1], [bf, bf * PING])


def _t_codes(ctx, t, codes, flag) -> None:
    from draftagent.report import vocab as V

    cap = V.T_CODES
    rows = _body(ctx, cap, t, ["項目", "數值", "單位", "yaml 來源", "出處", "核對狀態"])
    paths = set()
    for row in rows:
        file, _, path = row[3].partition("：")
        if file != "taoyuan_runoff.yaml":
            ctx.res.error(f"{cap} {row[0]}：yaml 來源「{row[3]}」不是 taoyuan_runoff.yaml")
            continue
        try:
            v = _get(codes, path)
        except (KeyError, TypeError):
            ctx.res.error(f"{cap} {row[0]}：yaml 路徑 {path} 不存在")
            continue
        paths.add(path)
        ctx.nums(
            f"{cap} {row[0]}", row[1], [float(x) for x in v] if isinstance(v, list) else [float(v)]
        )
        ctx.text(f"{cap} {row[0]} 出處", row[4], str(codes[path.split(".")[0]]["source"]))
        ctx.text(f"{cap} {row[0]} 核對狀態", row[5], flag)
    for p in ("runoff_standard.min_storage_m3_per_m2", "runoff_standard.max_release_cms_per_m2",
              "runoff_standard.design_release_ratio", "building_code_4_3.min_storage_m3_per_m2"):  # fmt: skip
        if p not in paths:
            ctx.res.error(f"{cap}：缺 {p}")


def _t_horner(ctx, t, r, codes, flag) -> None:
    from draftagent.report import vocab as V

    cap = V.T_HORNER
    rows = _body(
        ctx,
        cap,
        t,
        [
            "重現期",
            "a",
            "b",
            "c",
            "集流時間 t（min）",
            "降雨強度 I（mm/hr）",
            "用途",
            "yaml 來源",
            "核對狀態",
        ],
    )
    station = r["station"]["value"]
    tc = r["tc_min"]["value"]
    want_T = {int(d["T_yr"]) for d in r["rainfall"].values()}
    got_T = set()
    for row in rows:
        T = int(_nums(row[0])[0][0])
        got_T.add(T)
        ctx.text(
            f"{cap} T{T} yaml 來源", row[7], f"taoyuan_runoff.yaml：horner.stations.{station}.T{T}"
        )
        p = codes["horner"]["stations"][station][f"T{T}"]
        for i, k in enumerate("abc", 1):
            ctx.num(f"{cap} T{T} {k}", row[i], float(p[k]))
        ctx.num(f"{cap} T{T} t", row[4], tc)
        I_indep = p["a"] / (tc + p["b"]) ** p["c"]
        ctx.num(f"{cap} T{T} I（Horner 重算）", row[5], I_indep)
        I_r = next(
            (d["I_mm_hr"]["value"] for d in r["rainfall"].values() if int(d["T_yr"]) == T), None
        )
        if I_r is not None:
            ctx.num(f"{cap} T{T} I（計算結果）", row[5], I_r)
        ctx.text(f"{cap} T{T} 核對狀態", row[8], flag)
    if got_T != want_T:
        ctx.res.error(f"{cap}：重現期 {sorted(got_T)} ≠ 計算使用 {sorted(want_T)}")


def _t_runoff_c(ctx, t, r, codes, flag) -> None:
    from draftagent.report import vocab as V

    cap = V.T_RUNOFF_C
    rows = _body(ctx, cap, t, ["土地使用", "手冊範圍", "採用 C", "C 來源", "yaml 來源", "核對狀態"])
    cls = codes["runoff_coefficients"]["classes"]
    used = {(s["landuse"], round(s["c"], 2)) for s in r["subareas"]}
    got = set()
    for row in rows:
        key = row[4].rsplit(".", 1)[-1]
        if key not in cls:
            ctx.res.error(f"{cap} {row[0]}：yaml 來源 {row[4]} 不存在")
            continue
        ctx.text(f"{cap} {key} 名稱", row[0], cls[key]["name"])
        ctx.nums(f"{cap} {key} 範圍", row[1], [cls[key]["min"], cls[key]["max"]])
        c = _nums(row[2])[0][0]
        got.add((key, round(c, 2)))
        ctx.text(f"{cap} {key} 核對狀態", row[5], flag)
    if got != used:
        ctx.res.error(f"{cap}：（土地使用, C）{sorted(got)} ≠ 計算使用 {sorted(used)}")


def _t_existing(ctx, t, net) -> None:
    from draftagent.report import vocab as V

    cap = V.T_EXISTING
    rows = _body(
        ctx,
        cap,
        t,
        [
            "編號",
            "名稱",
            "斷面",
            "測點（依流向）",
            "E（m）",
            "N（m）",
            "溝頂 EL（m）",
            "溝底 EL（m）",
            "設計水深（m）",
        ],
    )
    want = [(d, i, pt) for d in net.existing_drains for i, pt in enumerate(d.points, 1)]
    if len(rows) != len(want):
        ctx.res.error(f"{cap}：{len(rows)} 列 ≠ 既有排水測點 {len(want)}")
        return
    for row, (d, i, pt) in zip(rows, want, strict=True):
        w = f"{cap} {d.id} 測點 {i}"
        ctx.text(w, row[0], d.id)
        ctx.text(w + " 斷面", row[2], d.section.dims_text())
        ctx.num(w + " E", row[4], pt.x)
        ctx.num(w + " N", row[5], pt.y)
        for col, v in ((6, pt.top_el), (7, pt.invert_el), (8, d.design_water_depth_m)):
            if v is None:
                ctx.text(w, row[col], "—")
            else:
                ctx.num(w + f" {t[0][col]}", row[col], v)


def _t_photos(ctx, t, spec) -> None:
    from draftagent.report import vocab as V

    rows = _body(ctx, V.T_PHOTOS, t, ["附圖", "檔案", "說明"])
    want = [(Path(p.path).name, p.caption) for p in spec.report.photos]
    if [(r[1], r[2]) for r in rows] != want:
        ctx.res.error(f"{V.T_PHOTOS}：{[(r[1], r[2]) for r in rows]} ≠ spec {want}")


def _t_catch(ctx, t, net, codes) -> None:
    from shapely.geometry import Polygon

    from draftagent.report import vocab as V

    cap = V.T_CATCH
    rows = _body(ctx, cap, t, ["分區", "土地使用", "C", "面積（m²）", "面積（坪）", "排入節點"])
    cls = codes["runoff_coefficients"]["classes"]
    subs = {s.id: s for s in net.subcatchments}
    ids = [row[0] for row in rows if row[0] != "合計"]
    if ids != [s.id for s in net.subcatchments]:
        ctx.res.error(f"{cap}：分區 {ids} ≠ spec {[s.id for s in net.subcatchments]}")
    tot = 0.0
    for row in rows:
        if row[0] == "合計":
            ctx.num(
                f"{cap} 合計面積", row[3], sum(Polygon(s.polygon).area for s in net.subcatchments)
            )
            ctx.num(
                f"{cap} 合計坪",
                row[4],
                sum(Polygon(s.polygon).area for s in net.subcatchments) * PING,
            )
            continue
        s = subs.get(row[0])
        if s is None:
            continue
        a = Polygon(s.polygon).area
        tot += a
        c = s.c_override if s.c_override is not None else cls[s.landuse]["median"]
        ctx.text(f"{cap} {s.id} 土地使用", row[1], cls[s.landuse]["name"])
        ctx.num(f"{cap} {s.id} C", row[2], c)
        ctx.num(f"{cap} {s.id} 面積", row[3], a)
        ctx.num(f"{cap} {s.id} 坪", row[4], a * PING)
        ctx.text(f"{cap} {s.id} 排入", row[5], s.outlet_node)
    if not any(row[0] == "合計" for row in rows):
        ctx.res.error(f"{cap}：缺合計列")


def _t_pipes(ctx, t, net, r) -> None:
    from draftagent.report import vocab as V

    cap = V.T_PIPES
    rows = _body(ctx, cap, t, ["管渠", "斷面", "材料", "n", "坡度 S（%）", "長度 L（m）", "集水面積（m²）", "C",
                              "設計流量 Q（m³/s）", "通水容量（m³/s）", "流速 V（m/s）"])  # fmt: skip
    pipes = {p["name"]: p for p in r["pipes"]}
    if [row[0] for row in rows] != list(pipes):
        ctx.res.error(f"{cap}：管渠 {[row[0] for row in rows]} ≠ 計算 {list(pipes)}")
    nodes = {n.id: n for n in net.nodes}
    links = {lk.id: lk for lk in net.links}
    for row in rows:
        p, lk = pipes.get(row[0]), links.get(row[0])
        if p is None or lk is None:
            continue
        a, b = nodes[lk.from_], nodes[lk.to]
        L = math.hypot(b.x - a.x, b.y - a.y)
        w = f"{cap} {row[0]}"
        ctx.text(w + " 斷面", row[1], lk.section.text())
        ctx.num(w + " n", row[3], p["n"]["value"])
        ctx.num(w + " 坡度", row[4], (a.invert_el - b.invert_el) / L * 100)
        ctx.num(w + " 長度", row[5], L)
        ctx.num(w + " 集水面積", row[6], p["design_area_m2"])
        c = p["C_design"]["value"] if "C_design" in p else r["C_composite"]["value"]
        ctx.num(w + " C", row[7], c)
        ctx.num(w + " Q", row[8], p["Q_design_cms"]["value"])
        ctx.num(w + " 容量", row[9], p["capacity_cms"]["value"])
        if p["velocity_m_s"] is None:
            ctx.text(w + " 流速", row[10], "—")
        else:
            ctx.num(w + " 流速", row[10], p["velocity_m_s"]["value"])
        if p["Q_design_cms"]["value"] > p["capacity_cms"]["value"]:
            ctx.res.error(f"{w}：設計流量 > 通水容量（應由 drainage_calc_check 擋下）")


def _t_storage(ctx, t, net, r, codes) -> None:
    from draftagent.report import vocab as V

    cap = V.T_STORAGE
    _body(ctx, cap, t, ["項目", "數值", "單位", "說明"])
    d = _by_item(t)
    As = r["As"]["As_m2"]
    sr, sp = r["storage_required"], r["storage_provided"]
    k_ty = codes["runoff_standard"]["min_storage_m3_per_m2"]
    k_bc = codes["building_code_4_3"]["min_storage_m3_per_m2"]
    ctx.nums(f"{cap} 計算用面積", d["計算用面積 As"][1], [As, As * PING])
    ctx.num(f"{cap} 必要（桃園）", d["必要貯集量（桃園逕流量標準）"][1], k_ty * As)
    ctx.num(f"{cap} 必要（4-3）", d["必要貯集量（建技 4-3）"][1], k_bc * As)
    ctx.num(f"{cap} 必要（採用）", d["必要貯集量（採用）"][1], max(k_ty, k_bc) * As)
    ctx.num(f"{cap} 淨平面積", d["貯集設施淨平面積"][1], sp["net_plan_area_m2"]["value"])
    ctx.num(f"{cap} 有效水深", d["有效水深"][1], net.storage.effective_depth)
    ctx.num(f"{cap} 提供", d["提供貯集量"][1], sp["provided_m3"]["value"])
    if sp["provided_m3"]["value"] + 1e-9 < sr["required_m3"]["value"]:
        ctx.res.error(f"{cap}：提供貯集量 < 必要貯集量（應由 drainage_calc_check 擋下）")


def _t_release(ctx, t, net, r) -> None:
    from draftagent.report import vocab as V

    cap = V.T_RELEASE
    _body(ctx, cap, t, ["項目", "數值", "單位", "說明"])
    d = _by_item(t)
    rel, out = r["release"], r["outlet"]
    for k, v in (
        ("容許排放量", rel["allowed_cms"]["value"]),
        ("設計排放量下限", rel["design_min_cms"]["value"]),
        ("設計排放量上限", rel["design_max_cms"]["value"]),
        ("設計排放量", out["Q_cms"]["value"]),
        ("設計排放量 / 容許排放量", 100 * out["Q_cms"]["value"] / rel["allowed_cms"]["value"]),
        ("全基地洪峰流量 Q20", r["rainfall"]["storage"]["Q_site_cms"]["value"]),
    ):
        ctx.num(f"{cap} {k}", d[k][1], v)
    o = net.calc.outlet
    if out["type"] != "pump":
        want = [o.diameter_m] if o.type == "circ_orifice" else [o.height_m, o.width_m]
        ctx.nums(f"{cap} 出流型式", d["出流型式"][1], want)
        ctx.num(f"{cap} 孔口設計水深", d["孔口設計水深"][1], o.design_head_m)
    if r.get("overflow_weir"):
        ctx.num(f"{cap} 溢流堰排洪量", d["溢流堰排洪量"][1], r["overflow_weir"]["Q_cms"]["value"])


def _t_cost(ctx, t, spec, qrows) -> None:
    from draftagent.report import vocab as V

    cap = V.T_COST
    rows = _body(
        ctx,
        cap,
        t,
        ["項次", "項目", "規格", "單位", "數量", "單價（元）", "複價（元）", "數量來源"],
    )
    want: list[tuple[str, str, str, float, float | None, str]] = []
    for q in qrows:
        ps = [
            u
            for u in spec.report.unit_prices
            if u.item == q.item and (u.spec is None or u.spec == q.spec)
        ]
        want.append((q.item, q.spec, q.unit, q.qty, ps[0].unit_price if ps else None, V.MODEL_SRC))
    for x in spec.report.extra_costs:
        want.append((x.item, x.spec, x.unit, x.qty, x.unit_price, V.USER_SRC))
    body = [row for row in rows if row[1] != "合計"]
    if len(body) != len(want):
        ctx.res.error(f"{cap}：{len(body)} 列 ≠ 數量表 + 使用者項目 {len(want)} 列")
        return
    total = 0.0
    for row, (item, sp, unit, qty, price, src) in zip(body, want, strict=True):
        w = f"{cap} {item}（{sp}）"
        ctx.text(w + " 項目", row[1], item)
        ctx.text(w + " 規格", row[2], sp)
        ctx.text(w + " 單位", row[3], unit)
        ctx.num(w + " 數量", row[4], qty)
        ctx.text(w + " 數量來源", row[7], src)
        if price is None:
            ctx.text(w + " 單價", row[5], "")
            ctx.text(w + " 複價", row[6], "")
        else:
            amt = round(qty * price)
            total += amt
            ctx.num(w + " 單價", row[5], price)
            ctx.num(w + " 複價", row[6], amt)
    tot = [row for row in rows if row[1] == "合計"]
    if len(tot) != 1:
        ctx.res.error(f"{cap}：缺合計列")
    else:
        ctx.num(f"{cap} 合計", tot[0][6], total)


# ---------------------------------------------------------------- 附圖


def _figures(res, ctx, md, dx, sheets_tb, tbs, spec, net, fmt, dcodes, V, md_path) -> None:
    # DXF 圖簽 vs spec
    spec_scale = {
        "plan": f"1/{net.scales.plan}",
        "catchment": f"1/{net.scales.catchment}",
    }
    for tb in tbs:
        which = tb["which"]
        if "DWG_NO" not in tb:
            res.error(f"{tb['file']}［{which}］找不到圖簽")
            continue
        if tb["DWG_NO"] != net.drawing_no(which):
            res.error(f"{tb['file']}［{which}］圖號 {tb['DWG_NO']} ≠ spec {net.drawing_no(which)}")
        if which in spec_scale and tb.get("SCALE", "").replace(" ", "") != spec_scale[which]:
            res.error(f"{tb['file']}［{which}］比例 {tb.get('SCALE')} ≠ spec {spec_scale[which]}")
    names = [s.name for s in fmt.sheets]
    for tb in tbs:
        if tb.get("TITLE") not in names:
            res.error(f"{tb['file']}［{tb['which']}］圖名「{tb.get('TITLE')}」不是統一圖名")

    # 附圖清單（統一圖名對照）
    by_title: dict[str, list[dict]] = {}
    for tb in tbs:
        by_title.setdefault(tb.get("TITLE", ""), []).append(tb)
    if sheets_tb is not None:
        rows = _body(
            ctx,
            V.T_SHEETS,
            sheets_tb,
            ["項次", "統一圖名", "檢核表比例", "圖號", "比例", "本計畫書處理"],
        )
        got = [row[1] for row in rows]
        if got != names:
            res.error(f"{V.T_SHEETS}：統一圖名 {got} ≠ {names}（{fmt.path.name}，研究文件 §2a）")
        for row, sh in zip(rows, fmt.sheets, strict=False):
            w = f"{V.T_SHEETS} {sh.no} {sh.name}"
            thr = f"S≧1/{dcodes.scale(sh.scale_key).max_denominator}" if sh.scale_key else "—"
            ctx.text(w + " 檢核表比例", row[2], thr)
            dx_tbs = by_title.get(sh.name, [])
            if dx_tbs:
                ctx.text(w + " 圖號", row[3], "、".join(t["DWG_NO"] for t in dx_tbs))
                ctx.text(w + " 比例", row[4], "；".join(t.get("SCALE", "") for t in dx_tbs))
            elif sh.produced_by in ("plan", "profile", "detail", "catchment"):
                res.error(f"{w}：本 repo 應產出此圖，但找不到 DXF")
            elif row[3] != "—":
                res.error(f"{w}：沒有對應 DXF，圖號應為「—」，實際「{row[3]}」")

    # 嵌入圖片與圖說
    imgs = [i for i, x in enumerate(md) if x[0] == "img"]
    n_photos = len(spec.report.photos)
    want_n = len(tbs) + n_photos
    n_docx = sum(1 for x in dx if x[0] == "img")
    res.metrics["figures"] = {"md": len(imgs), "docx": n_docx, "expected": want_n}
    if len(imgs) != want_n or n_docx != want_n:
        res.error(
            f"附圖數不符：md 嵌入 {len(imgs)}、docx 嵌入 {n_docx}，應為 DXF 圖紙 {len(tbs)} + 照片 {n_photos} = {want_n}"
        )
    caps = []
    for k, i in enumerate(imgs, 1):
        it = md[i]
        if it[1].startswith("missing:"):
            res.error(f"附圖 {k} 檔案不存在：{it[1][8:]}")
        else:
            bad = _png_blank(Path(it[2]))
            if bad:
                res.error(f"附圖 {k}（{Path(it[2]).name}）{bad}")
        cap = md[i + 1][1] if i + 1 < len(md) and md[i + 1][0] == "p" else ""
        if not cap.startswith(f"{V.FIG_PREFIX} {k} "):
            res.error(f"附圖 {k} 缺圖說（「{V.FIG_PREFIX} {k} …」），實際「{cap[:40]}」")
        caps.append(cap)
    # 照片在前（統一圖名順序：基地現況照片 → 平面 → 縱斷面 → 詳圖 → 集水區），其餘依 DXF 圖簽
    order = [s.produced_by for s in fmt.sheets if s.produced_by]
    expect_caps: list[str] = []
    k = 0
    for by in order:
        if by == "photos":
            for ph in spec.report.photos:
                k += 1
                expect_caps.append(f"{V.FIG_PREFIX} {k} 基地現況照片：{ph.caption}")
            continue
        whiches = {"plan": ["plan"], "profile": ["profile", "profile_outlet"], "detail": ["detail"],
                   "catchment": ["catchment"]}.get(by, [])  # fmt: skip
        for w in whiches:
            for tb in [t for t in tbs if t["which"] == w]:
                k += 1
                expect_caps.append(
                    f"{V.FIG_PREFIX} {k} {tb.get('TITLE')}（圖號 {tb.get('DWG_NO')}，比例 {tb.get('SCALE')}）"
                )
    for i, (got, want) in enumerate(zip(caps, expect_caps, strict=False), 1):
        if " ".join(got.split()) != " ".join(want.split()):
            res.error(f"附圖 {i} 圖說「{got}」≠ DXF 圖簽 / 照片「{want}」")


def _docx_layout(res, d, fmt, artifacts) -> None:
    from docx.oxml.ns import qn
    from docx.shared import Mm

    lay = fmt.layout
    sec = d.sections[0]
    mm = lambda v: v / Mm(1)  # noqa: E731
    pg, mg = lay["page"], lay["margins_mm"]
    for label, got, want in (
        ("頁寬", sec.page_width, pg["width_mm"]),
        ("頁高", sec.page_height, pg["height_mm"]),
        ("上邊界", sec.top_margin, mg["top"]),
        ("下邊界", sec.bottom_margin, mg["bottom"]),
        ("左邊界", sec.left_margin, mg["left"]),
        ("右邊界", sec.right_margin, mg["right"]),
    ):
        if got is None or abs(mm(got) - want) > 0.5:
            res.error(
                f"docx {label} {None if got is None else round(mm(got), 1)} mm ≠ {want} mm（{fmt.path.name}）"
            )
    meta: dict[str, Any] = {}
    mp = artifacts.get("report_meta")
    if mp is not None and Path(mp).exists():
        meta = json.loads(Path(mp).read_text(encoding="utf-8"))
        for w in meta.get("warnings", []):
            res.warn(w)
    fonts = lay["fonts"]
    allowed = {fonts["east_asia"], *fonts.get("east_asia_fallbacks", [])}
    rf = (
        d.styles["Normal"].element.rPr.find(qn("w:rFonts"))
        if d.styles["Normal"].element.rPr is not None
        else None
    )
    east = rf.get(qn("w:eastAsia")) if rf is not None else None
    latin = rf.get(qn("w:ascii")) if rf is not None else None
    if east not in allowed:
        res.error(f"docx 東亞字型「{east}」不在 {sorted(allowed)}")
    elif meta and east != meta.get("east_asia_font"):
        res.error(f"docx 東亞字型「{east}」≠ report_meta.json「{meta.get('east_asia_font')}」")
    if east != fonts["east_asia"]:
        res.warn(f"docx 東亞字型為 fallback「{east}」（應為{fonts['east_asia']}）")
    if latin != fonts["latin"]:
        res.error(f"docx 西文字型「{latin}」≠ {fonts['latin']}")
    instr = [e.text or "" for e in d.element.body.iter(qn("w:instrText"))]
    if not any(s.strip().startswith("TOC") for s in instr):
        res.error("docx 缺目錄（TOC 功能變數）")
    res.metrics["docx_font"] = {"east_asia": east, "latin": latin}


# ---------------------------------------------------------------- CLI


def collect(run_dir: str | Path) -> dict[str, Path]:
    from draftagent.build import collect_artifacts

    return collect_artifacts(run_dir)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description="排水計畫書本文 verifier",
        epilog=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("run_dir", help="draftagent build 的 run 目錄（含 report.md / report.docx）")
    ap.add_argument("--spec", help="DrainageReportSpec JSON；省略 → <run_dir>/spec.json")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    spec = a.spec or str(Path(a.run_dir) / "spec.json")
    emit(check_report(spec, collect(a.run_dir)), a.json)


if __name__ == "__main__":
    main()
