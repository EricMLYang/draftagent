"""一鍵 build：spec → render → 全部 verifier → runs/<ts>-<kind>[-name]/。

    from draftagent import build, PlanSpec
    result = build(PlanSpec(...), name="demo")
    print(result)            # 完整 verifier 輸出（CLAUDE.md 要求貼在回覆裡）
    result.raise_for_status()

產物目錄
--------
runs/<YYYYmmdd-HHMMSS>-<kind>[-name]/
  spec.json       spec 的完整 dump（唯一真相，可 replay：draftagent build spec.json）
  manifest.json   kind、spec 類別、schema hash、draftagent 版本、git commit、產物清單、紅綠燈
  verify.json     所有 VerifyResult（機器可讀）
  verify.txt      所有 VerifyResult.render()（貼給人看）
  <render 產物>   例 plan.dxf、profile.dxf、site.gpkg、result.json …
  <stem>.png      有 DXF 且 preview=True 時的預覽（失敗只記 warning）

render / verify 本身仍是純函式，不得呼叫 LLM。
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import os
import platform
import re
import runpy
import subprocess
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from draftagent import registry
from draftagent._paths import is_within
from draftagent.errors import SpecLoadError
from draftagent.registry import Kind
from draftagent.verify.base import VerifyResult

REPO_ROOT = Path(__file__).resolve().parent.parent
RESERVED_FILES = {"spec.json", "manifest.json", "verify.json", "verify.txt", "render_error.txt"}


class BuildFailed(RuntimeError):
    """raise_for_status()：有 verifier 紅燈。訊息為完整 verifier 輸出。"""


def default_runs_dir() -> Path:
    if (REPO_ROOT / "pyproject.toml").exists():
        return REPO_ROOT / "runs"
    return Path.cwd() / "runs"


def _rel(p: Path) -> str:
    p = Path(p)
    for base in (Path.cwd(), REPO_ROOT):
        try:
            return p.resolve().relative_to(base.resolve()).as_posix()
        except ValueError:
            continue
    return str(p)


# ---------------------------------------------------------------- result


@dataclass
class BuildResult:
    ok: bool
    kind: str
    run_dir: Path
    artifacts: dict[str, Path]
    results: list[VerifyResult]
    warnings: list[str] = field(default_factory=list)

    def report(self) -> str:
        """所有 verifier 的 render() 文字（= verify.txt）。"""
        return "\n\n".join(r.render() for r in self.results)

    def __str__(self) -> str:
        n_ok = sum(r.ok for r in self.results)
        lines = [f"== draftagent build [{self.kind}] → {_rel(self.run_dir)}", "", self.report(), ""]
        lines.append("artifacts:")
        lines += [f"  {k}: {_rel(v)}" for k, v in self.artifacts.items()]
        if self.warnings:
            lines.append("build warnings:")
            lines += [f"  WARN   {w}" for w in self.warnings]
        verdict = "PASS" if self.ok else "FAIL"
        lines.append(f"RESULT: {verdict}（{n_ok}/{len(self.results)} verifier 綠燈）")
        return "\n".join(lines)

    def raise_for_status(self) -> BuildResult:
        if not self.ok:
            raise BuildFailed(str(self))
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "kind": self.kind,
            "run_dir": str(self.run_dir),
            "artifacts": {k: str(v) for k, v in self.artifacts.items()},
            "results": [r.to_dict() for r in self.results],
            "warnings": self.warnings,
        }


# ---------------------------------------------------------------- load


def _kind_aliases(k: Kind) -> set[str]:
    names = {k.name}
    f = k.spec.model_fields.get("kind")
    if f is not None and isinstance(f.default, str):
        names.add(f.default)
    return names


def _match_kind(name: str) -> Kind:
    for k in registry.all_kinds():
        if name in _kind_aliases(k):
            return k
    return registry.get(name)  # 丟出含可用清單的 KeyError


def _validate_as(k: Kind, data: dict[str, Any]) -> BaseModel:
    data = dict(data)
    if "kind" in data and "kind" not in k.spec.model_fields:
        data.pop("kind")  # 沒有 kind 欄位的 spec（SiteSpec 等）允許在檔案裡標註 kind
    elif "kind" in k.spec.model_fields and data.get("kind") == k.name:
        data["kind"] = k.spec.model_fields["kind"].default
    return k.spec.model_validate(data)


def _guess(data: dict[str, Any]) -> BaseModel:
    hits: list[tuple[Kind, BaseModel]] = []
    fails: list[tuple[int, Kind, ValidationError]] = []
    for k in registry.all_kinds():
        try:
            hits.append((k, _validate_as(k, data)))
        except ValidationError as e:
            fails.append((e.error_count(), k, e))
    if len(hits) == 1:
        return hits[0][1]
    if hits:
        raise SpecLoadError(
            f"spec 同時符合多個圖種 {[k.name for k, _ in hits]}；請加 \"kind\" 欄位或 --kind"
        )
    from draftagent.errors import format_validation_error

    fails.sort(key=lambda t: t[0])
    n, best, err = fails[0]
    runner_up = fails[1][0] if len(fails) > 1 else None
    if runner_up is None or n * 3 <= runner_up:
        # 明顯就是這個圖種，只是 spec 不合法：先講拒絕原因，不要讓人以為是圖種判斷問題
        raise SpecLoadError(
            f"spec 看起來是 {best.name} 圖種，但未通過驗證（{n} 個錯誤；"
            f"若不是這個圖種請加 \"kind\" 或 --kind）：\n{format_validation_error(err, best.spec)}"
        )
    raise SpecLoadError(
        "無法判斷圖種（spec 沒有 kind 欄位，也不符合任何已註冊圖種）；請加 \"kind\" 或用 --kind。\n"
        f"最接近的是 {best.name}（{n} 個錯誤）：\n{format_validation_error(err, best.spec)}"
    )


def _from_data(data: Any, kind: str | None, src: Path) -> BaseModel:
    if not isinstance(data, dict):
        raise SpecLoadError(f"{src}：最外層必須是物件（mapping），實際是 {type(data).__name__}")
    if kind is not None:
        return _validate_as(_match_kind(kind), data)
    if isinstance(data.get("kind"), str):
        return _validate_as(_match_kind(data["kind"]), data)
    return _guess(data)


def load_spec(path: str | Path, kind: str | None = None) -> BaseModel:
    """讀 spec：.json / .yaml / .yml / .py。

    - JSON / YAML：圖種依 `kind` 參數 → 檔內 `kind` 欄 → 逐一試驗所有已註冊 spec 型別。
    - .py：以 runpy 執行（檔案必須在 repo 內），取模組層 `spec` 變數，
      或 `def spec() -> BaseModel`。`if __name__ == "__main__":` 區塊不會被執行。
    ValidationError 原樣丟出（CLI 會轉成友善訊息）；其他問題丟 SpecLoadError。
    """
    p = Path(path)
    if not p.exists():
        raise SpecLoadError(f"找不到 spec 檔：{p}")
    suffix = p.suffix.lower()
    if suffix == ".json":
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise SpecLoadError(f"{p}：JSON 格式錯誤（第 {e.lineno} 行第 {e.colno} 欄）：{e.msg}") from None
        return _from_data(data, kind, p)
    if suffix in (".yaml", ".yml"):
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            raise SpecLoadError(f"{p}：YAML 格式錯誤：{e}") from None
        return _from_data(data, kind, p)
    if suffix == ".py":
        rp = p.resolve()
        if not is_within(rp, REPO_ROOT):  # Windows：大小寫 / 磁碟代號不敏感
            raise SpecLoadError(f"{p}：.py spec 必須放在 repo 內（建議 {REPO_ROOT / 'work'}）")
        ns = runpy.run_path(str(rp), run_name="__draftagent_spec__")
        obj = ns.get("spec")
        if callable(obj) and not isinstance(obj, BaseModel):
            obj = obj()
        if not isinstance(obj, BaseModel):
            raise SpecLoadError(
                f"{p}：需定義模組層變數 `spec = <Spec>(...)` 或 `def spec() -> <Spec>`"
                f"（實際 {type(obj).__name__}）"
            )
        k = registry.for_spec(obj)
        if kind is not None and kind not in _kind_aliases(k):
            raise SpecLoadError(f"{p}：spec 是 {k.name}（{type(obj).__name__}），與 --kind {kind} 不符")
        return obj
    raise SpecLoadError(f"{p}：不支援的副檔名 {suffix!r}（可用 .json / .yaml / .yml / .py）")


# ---------------------------------------------------------------- build


def _git() -> dict[str, Any]:
    def run(*args: str) -> str | None:
        try:
            r = subprocess.run(
                ["git", *args], check=False, cwd=REPO_ROOT, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=5
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return r.stdout.strip() if r.returncode == 0 else None

    commit = run("rev-parse", "HEAD")
    if commit is None:
        return {"commit": None}
    status = run("status", "--porcelain")
    return {"commit": commit, "dirty": bool(status)}


def _version() -> str:
    import draftagent

    return draftagent.__version__


def _slug(s: str) -> str:
    s = re.sub(r"[\s/\\:]+", "-", s.strip())
    return re.sub(r"[^\w\-.]", "", s).strip("-.") or "run"


def _new_run_dir(root: Path, kind: str, name: str | None) -> Path:
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"{stamp}-{kind}" + (f"-{_slug(name)}" if name else "")
    d, i = root / base, 2
    while d.exists():
        d, i = root / f"{base}-{i}", i + 1
    d.mkdir(parents=True)
    return d


def _safe_verify(fn: Any, spec: BaseModel, artifacts: dict[str, Path]) -> VerifyResult:
    try:
        res = fn(spec, artifacts)
    except Exception as e:  # noqa: BLE001 - verifier 崩潰也要變成紅燈，不能變成「沒輸出」
        res = VerifyResult(name=getattr(fn, "__name__", "verifier"))
        res.error(f"verifier 例外 {type(e).__name__}: {e}")
        return res
    if not isinstance(res, VerifyResult):
        bad = VerifyResult(name=getattr(fn, "__name__", "verifier"))
        bad.error(f"verifier 回傳 {type(res).__name__}，應為 VerifyResult")
        return bad
    return res


def verify_artifacts(
    spec: BaseModel, artifacts: dict[str, Path], kind: Kind | str | None = None
) -> list[VerifyResult]:
    """對既有產物跑該圖種的全部 verifier。"""
    k = registry.get(kind) if isinstance(kind, str) else (kind or registry.for_spec(spec))
    return [_safe_verify(fn, spec, artifacts) for fn in k.verifiers]


def collect_artifacts(run_dir: str | Path) -> dict[str, Path]:
    """由 run 目錄還原 artifacts：優先讀 manifest.json，否則依副檔名 / 檔名猜 key。"""
    d = Path(run_dir)
    man = d / "manifest.json"
    if man.exists():
        arts = json.loads(man.read_text(encoding="utf-8")).get("artifacts") or {}
        if arts:
            return {k: d / v for k, v in arts.items()}
    out: dict[str, Path] = {}
    for p in sorted(d.iterdir()):
        if not p.is_file() or p.name in RESERVED_FILES:
            continue
        key = p.stem if p.suffix.lower() in (".json", ".md") else p.suffix.lower().lstrip(".")
        out.setdefault(key, p)
    if (d / "spec.json").exists():
        out["spec"] = d / "spec.json"
    return out


PREVIEW_DPI = 200
PREVIEW_MAX_PX = 4000


def preview_dpi(dxf: str | Path, dpi: int = PREVIEW_DPI, max_px: int = PREVIEW_MAX_PX) -> int:
    """render_png 以 1 繪圖單位 = 1 mm 決定圖片尺寸；1:1 實尺 mm 平面（或 m 座標）會變成
    數十億像素。依 modelspace 範圍把 DPI 壓到長邊 ≤ max_px（圖紙 mm 圖面維持 dpi）。"""
    import ezdxf
    from ezdxf import bbox

    doc = ezdxf.readfile(str(dxf))
    ext = bbox.extents(doc.modelspace(), fast=True)
    if not ext.has_data:
        return dpi
    long_in = max(ext.size.x, ext.size.y) / 25.4
    return max(1, min(dpi, int(max_px / long_in))) if long_in > 0 else dpi


def _previews(artifacts: dict[str, Path], warnings: list[str]) -> dict[str, Path]:
    """每個 DXF 產物各出一張 PNG；含 paperspace layout 的再依 layout 各出一張。

    key：主 DXF（"dxf"）→ "png"；其他 → "<key>_png"、"<key>_png[<layout>]"。
    預覽只供人眼檢查，失敗只記 warning。
    """
    import ezdxf

    from draftagent.render.preview import render_png

    # matplotlib 找字重的 findfont 訊息只是雜訊，不要混進 verifier 輸出
    logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)
    out: dict[str, Path] = {}
    for key, dxf in list(artifacts.items()):
        if Path(dxf).suffix.lower() != ".dxf":
            continue
        base = "png" if key == "dxf" else f"{key}_png"
        try:
            # 縮圖與線寬補償交給 render_png（max_px）；先在這裡降 dpi 會讓線寬補償失效
            out[base] = render_png(dxf, Path(dxf).with_suffix(".png"), dpi=PREVIEW_DPI, max_px=PREVIEW_MAX_PX)
            layouts = [n for n in ezdxf.readfile(str(dxf)).layout_names() if n != "Model"]
            for name in layouts:
                if not ezdxf.readfile(str(dxf)).layouts.get(name).query("VIEWPORT"):
                    continue  # 空白預設 Layout1 之類
                png = Path(dxf).with_name(f"{Path(dxf).stem}-{name}.png")
                out[f"{base}[{name}]"] = render_png(
                    dxf, png, dpi=PREVIEW_DPI, max_px=PREVIEW_MAX_PX, layout=name
                )
        except Exception as e:  # noqa: BLE001 - 預覽只供人眼檢查，失敗不致命
            warnings.append(f"PNG 預覽失敗（{key}）：{type(e).__name__}: {e}")
    return out


def _write_json(p: Path, obj: Any) -> None:
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def build(
    spec: BaseModel | str | Path,
    *,
    name: str | None = None,
    out: str | Path | None = None,
    preview: bool = True,
) -> BuildResult:
    """spec → render → verifiers → runs/<ts>-<kind>[-name]/。

    spec 可以是 Spec 實例，或 spec 檔路徑（.json / .yaml / .py，交給 load_spec）。
    out 是 runs 根目錄（預設 repo 的 runs/），run 目錄建在它底下。
    """
    source: Path | None = None
    if isinstance(spec, str | Path):
        source = Path(spec)
        spec = load_spec(source)
    k = registry.for_spec(spec)
    run_dir = _new_run_dir(Path(out) if out is not None else default_runs_dir(), k.name, name)

    spec_path = run_dir / "spec.json"
    _write_json(spec_path, spec.model_dump(mode="json"))  # = model_dump_json，indent 2、不轉義中文

    schema_json = json.dumps(type(spec).model_json_schema(), sort_keys=True, ensure_ascii=False)
    manifest: dict[str, Any] = {
        "kind": k.name,
        "name": name,
        "created_at": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "spec_model": f"{type(spec).__module__}.{type(spec).__qualname__}",
        "schema_version": getattr(spec, "schema_version", None),
        "schema_sha256": hashlib.sha256(schema_json.encode()).hexdigest(),
        "draftagent_version": _version(),
        "git": _git(),
        "python": platform.python_version(),
        "source": str(source) if source else None,
        "cwd": os.getcwd(),
        "status": "rendering",
    }
    _write_json(run_dir / "manifest.json", manifest)

    warnings: list[str] = []
    artifacts: dict[str, Path] = {}
    try:
        artifacts = {key: Path(v) for key, v in k.render(spec, run_dir).items()}
    except Exception as e:  # noqa: BLE001 - render 失敗要寫進 verify.json，回報給 agent 修 spec
        (run_dir / "render_error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        failed = VerifyResult(name="render")
        failed.error(f"{type(e).__name__}: {e}")
        failed.metrics["traceback"] = _rel(run_dir / "render_error.txt")
        results = [failed]
    else:
        artifacts["spec"] = spec_path
        results = verify_artifacts(spec, artifacts, k)
        if preview:
            artifacts.update(_previews(artifacts, warnings))
    artifacts.setdefault("spec", spec_path)

    ok = all(r.ok for r in results)
    _write_json(
        run_dir / "verify.json",
        {"ok": ok, "kind": k.name, "results": [r.to_dict() for r in results]},
    )
    result = BuildResult(ok, k.name, run_dir, artifacts, results, warnings)
    (run_dir / "verify.txt").write_text(result.report() + "\n", encoding="utf-8")

    def relto(p: Path) -> str:
        try:
            return str(p.resolve().relative_to(run_dir.resolve()))
        except ValueError:
            return str(p)

    manifest.update(
        status="done",
        ok=ok,
        artifacts={key: relto(v) for key, v in artifacts.items()},
        verifiers=[{"name": r.name, "ok": r.ok} for r in results],
        warnings=warnings,
    )
    _write_json(run_dir / "manifest.json", manifest)
    return result
