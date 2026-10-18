"""統一 CLI：`draftagent kinds | schema | new | build | verify | preview | check`。

所有指令支援 `--json`（機器可讀）；exit code 0 = 成功 / 全綠燈，1 = 失敗 / 紅燈 / 錯誤。
Pydantic ValidationError 轉成「欄位路徑 + 錯誤 + 允許值 / 範圍」，不印 traceback。
"""

from __future__ import annotations

import json
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import ValidationError

from draftagent import registry
from draftagent.errors import SpecLoadError, format_validation_error, validation_details

app = typer.Typer(
    name="draftagent",
    help="DraftAgent：spec（Python / JSON / YAML）→ render → verify → runs/。",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)

JsonOpt = Annotated[bool, typer.Option("--json", help="機器可讀 JSON 輸出")]


def _out(obj: Any) -> None:
    typer.echo(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _fail(as_json: bool, msg: str, details: list[dict[str, Any]] | None = None) -> None:
    if as_json:
        _out({"ok": False, "error": msg, "details": details or []})
    else:
        typer.echo(msg, err=True)
    raise typer.Exit(1)


def _guard(as_json: bool, fn: Callable[[], None]) -> None:
    """把預期內的錯誤轉成友善訊息 + exit 1。"""
    try:
        fn()
    except typer.Exit:
        raise
    except ValidationError as e:
        _fail(as_json, format_validation_error(e), validation_details(e))
    except (SpecLoadError, KeyError, FileNotFoundError, FileExistsError) as e:
        msg = e.args[0] if isinstance(e, KeyError) and e.args else str(e)
        _fail(as_json, str(msg))


def _rel(p: Path | None) -> str | None:
    from draftagent.build import _rel as rel

    return None if p is None else rel(p)


# ---------------------------------------------------------------- kinds / schema


@app.command()
def kinds(as_json: JsonOpt = False) -> None:
    """列出已註冊圖種、一行說明、範例 spec 路徑。"""
    ks = registry.all_kinds()
    rows = [
        {
            "name": k.name,
            "summary": k.summary,
            "spec": f"{k.spec.__module__}.{k.spec.__name__}",
            "example": _rel(k.example),
            "tags": list(k.tags),
        }
        for k in ks
    ]
    if as_json:
        _out(rows)
        return
    w = max(len(r["name"]) for r in rows)
    for r in rows:
        typer.echo(f"{r['name']:<{w}}  {r['summary']}")
        typer.echo(f"{'':<{w}}  spec: {r['spec'].rsplit('.', 1)[1]}　example: {r['example']}")
    typer.echo("\n下一步：draftagent new <kind> -o work/<name>.py → draftagent build work/<name>.py")


@app.command()
def schema(kind: Annotated[str, typer.Argument(help="圖種名稱（draftagent kinds）")],
           as_json: JsonOpt = False) -> None:
    """印出圖種 spec 的 JSON Schema（欄位、型別、允許值、範圍、說明）。"""

    def run() -> None:
        _out(registry.get(kind).spec.model_json_schema())

    _guard(as_json, run)


# ---------------------------------------------------------------- new


@app.command()
def new(
    kind: Annotated[str, typer.Argument(help="圖種名稱")],
    output: Annotated[Path | None, typer.Option("-o", "--output", help="輸出 .py（省略印到 stdout）")] = None,
    force: Annotated[bool, typer.Option("--force", help="覆寫既有檔案")] = False,
    as_json: JsonOpt = False,
) -> None:
    """由圖種範例產生一個可直接執行 / build 的 Python spec 檔。"""

    def run() -> None:
        from draftagent.build import load_spec
        from draftagent.codegen import spec_to_python

        k = registry.get(kind)
        if k.example is None:
            raise SpecLoadError(f"圖種 {kind} 沒有範例 spec")
        code = spec_to_python(
            load_spec(k.example, kind=k.name), kind=k.name, summary=k.summary,
            source=_rel(k.example),
        )
        if output is None:
            if as_json:
                _out({"ok": True, "kind": k.name, "code": code})
            else:
                typer.echo(code, nl=False)
            return
        if output.exists() and not force:
            raise FileExistsError(f"{output} 已存在；加 --force 覆寫")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(code, encoding="utf-8")
        if as_json:
            _out({"ok": True, "kind": k.name, "path": str(output)})
        else:
            typer.echo(f"wrote {output}（{k.name}）\n下一步：draftagent build {output}")

    _guard(as_json, run)


# ---------------------------------------------------------------- build / verify


@app.command()
def build(
    spec: Annotated[Path, typer.Argument(help="spec 檔 .py / .json / .yaml")],
    kind: Annotated[str | None, typer.Option("--kind", help="指定圖種（JSON 沒有 kind 欄時）")] = None,
    name: Annotated[str | None, typer.Option("--name", help="run 目錄後綴")] = None,
    out: Annotated[Path | None, typer.Option("--out", help="runs 根目錄（預設 repo 的 runs/）")] = None,
    no_preview: Annotated[bool, typer.Option("--no-preview", help="不輸出 DXF 的 PNG 預覽")] = False,
    as_json: JsonOpt = False,
) -> None:
    """spec → render → 全部 verifier → runs/<ts>-<kind>[-name]/，印完整 verifier 輸出。"""
    from draftagent.build import build as do_build
    from draftagent.build import load_spec

    def run() -> None:
        s = load_spec(spec, kind=kind)
        k = registry.for_spec(s)
        run_name = name
        if run_name is None and spec.stem not in ("spec", k.name) and spec.suffix == ".py":
            run_name = spec.stem
        res = do_build(s, name=run_name, out=out, preview=not no_preview)
        if as_json:
            _out(res.to_dict())
        else:
            typer.echo(str(res))
        raise typer.Exit(0 if res.ok else 1)

    _guard(as_json, run)


@app.command()
def verify(
    kind: Annotated[str, typer.Argument(help="圖種名稱")],
    spec: Annotated[Path, typer.Argument(help="spec 檔 .py / .json / .yaml")],
    artifacts: Annotated[Path, typer.Option("--artifacts", "-a", help="產物目錄（通常是 runs/<…>/）")],
    as_json: JsonOpt = False,
) -> None:
    """對既有產物重跑該圖種的全部 verifier（不重新產圖）。"""
    from draftagent.build import collect_artifacts, load_spec, verify_artifacts

    def run() -> None:
        if not artifacts.is_dir():
            raise FileNotFoundError(f"--artifacts {artifacts} 不是目錄")
        k = registry.get(kind)
        s = load_spec(spec, kind=k.name)
        arts = collect_artifacts(artifacts)
        if spec.suffix.lower() == ".json":
            arts["spec"] = spec  # 用指定的 spec，不用目錄裡的舊 spec.json
        else:
            arts.pop("spec", None)  # 由 adapter 寫暫存 spec.json
        results = verify_artifacts(s, arts, k)
        ok = all(r.ok for r in results)
        if as_json:
            _out({"ok": ok, "kind": k.name, "artifacts": arts,
                  "results": [r.to_dict() for r in results]})
        else:
            typer.echo("\n\n".join(r.render() for r in results))
            typer.echo(f"RESULT: {'PASS' if ok else 'FAIL'}（{sum(r.ok for r in results)}"
                       f"/{len(results)} verifier 綠燈）")
        raise typer.Exit(0 if ok else 1)

    _guard(as_json, run)


@app.command()
def preview(
    dxf: Annotated[Path, typer.Argument(help="DXF 檔")],
    output: Annotated[Path | None, typer.Option("-o", "--output", help="PNG 路徑（預設同名 .png）")] = None,
    dpi: Annotated[int, typer.Option("--dpi")] = 200,
    as_json: JsonOpt = False,
) -> None:
    """DXF → PNG 預覽（只供人眼檢查，不是出圖）。"""

    def run() -> None:
        if not dxf.exists():
            raise FileNotFoundError(f"找不到 {dxf}")
        from draftagent.render.preview import render_png

        png = render_png(dxf, output or dxf.with_suffix(".png"), dpi=dpi)
        if as_json:
            _out({"ok": True, "png": str(png)})
        else:
            typer.echo(f"wrote {png}")

    _guard(as_json, run)


# ---------------------------------------------------------------- check


REQUIRED_PKGS = ("ezdxf", "geopandas", "shapely", "pyproj", "rasterio", "numpy", "pydantic",
                 "pyyaml", "networkx", "pint", "matplotlib", "typer")
# (顯示名, import 敘述)：在子行程 import，避免把主行程弄掛（pyswmm 在部分 macOS 會被 SIGKILL）
OPTIONAL_IMPORTS = (
    ("pyswmm", "import pyswmm; print(getattr(pyswmm, '__version__', '?'))"),
    ("swmm-toolkit", "import swmm.toolkit as t; print(getattr(t, '__version__', '?'))"),
    ("ifcopenshell", "import ifcopenshell; print(ifcopenshell.version)"),
)


def _probe(code: str, timeout: float = 30.0) -> dict[str, Any]:
    try:
        r = subprocess.run([sys.executable, "-c", code], check=False, capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "detail": f"import 超過 {timeout:g}s 未回應"}
    if r.returncode == 0:
        return {"ok": True, "detail": r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""}
    if r.returncode < 0:
        try:
            sig = signal.Signals(-r.returncode).name
        except ValueError:
            sig = str(-r.returncode)
        return {"ok": False, "detail": f"子行程被訊號 {sig} 終止（import 時崩潰）"}
    last = (r.stderr.strip().splitlines() or ["?"])[-1]
    return {"ok": False, "detail": last}


def _environment() -> dict[str, Any]:
    from importlib.metadata import PackageNotFoundError, version

    import draftagent

    pkgs: dict[str, Any] = {}
    for p in REQUIRED_PKGS:
        try:
            pkgs[p] = {"ok": True, "version": version(p)}
        except PackageNotFoundError:
            pkgs[p] = {"ok": False, "version": None}

    fonts: dict[str, Any] = {}
    try:
        from ezdxf.fonts import fonts as ezfonts

        from draftagent.render.preview import CJK_FALLBACK_FILES, MAC_CJK_FONT
        from draftagent.render.sheet import CJK_FONT

        fm = ezfonts.font_manager
        avail = [f for f in CJK_FALLBACK_FILES if fm.has_font(f)]
        fonts = {
            "dxf_cjk_font": {"name": CJK_FONT, "ok": fm.has_font(CJK_FONT)},
            "preview_cjk_fallback": {"ok": bool(avail), "using": avail[0] if avail else None},
            "matplotlib_cjk": {"ok": MAC_CJK_FONT.exists(), "path": str(MAC_CJK_FONT)},
        }
    except Exception as e:  # noqa: BLE001
        fonts = {"error": f"{type(e).__name__}: {e}"}

    optional = {n: _probe(c) for n, c in OPTIONAL_IMPORTS}
    try:
        from ezdxf.addons import odafc

        oda = bool(odafc.is_installed())
    except Exception:  # noqa: BLE001
        oda = False
    optional["ODA File Converter"] = {
        "ok": oda or bool(shutil.which("ODAFileConverter")),
        "detail": "DWG 轉換（ezdxf.addons.odafc）",
    }

    kinds_info: dict[str, Any] = {}
    for k in registry.all_kinds():
        info: dict[str, Any] = {"example": _rel(k.example), "ok": True}
        try:
            from draftagent.build import load_spec

            if k.example is None:
                raise SpecLoadError("沒有範例")
            load_spec(k.example, kind=k.name)
        except Exception as e:  # noqa: BLE001
            info.update(ok=False, detail=f"{type(e).__name__}: {str(e).splitlines()[0]}")
        kinds_info[k.name] = info

    ok = all(v["ok"] for v in pkgs.values()) and all(v["ok"] for v in kinds_info.values())
    return {
        "ok": ok,
        "draftagent": draftagent.__version__,
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "packages": pkgs,
        "fonts": fonts,
        "optional": optional,
        "kinds": kinds_info,
    }


@app.command()
def check(as_json: JsonOpt = False) -> None:
    """環境自檢：必要套件版本、CJK 字型、選用相依（pyswmm / swmm-toolkit / ODA）、各圖種範例。"""
    env = _environment()
    if as_json:
        _out(env)
        raise typer.Exit(0 if env["ok"] else 1)

    def mark(ok: bool, required: bool = True) -> str:
        return "OK  " if ok else ("FAIL" if required else "--  ")

    typer.echo(f"draftagent {env['draftagent']}  python {env['python']}  ({env['executable']})")
    typer.echo("\n[必要套件]")
    for n, v in env["packages"].items():
        typer.echo(f"  {mark(v['ok'])} {n} {v['version'] or '未安裝'}")
    typer.echo("\n[字型]")
    for n, v in env["fonts"].items():
        if isinstance(v, dict):
            extra = v.get("name") or v.get("using") or v.get("path") or ""
            typer.echo(f"  {mark(v['ok'], False)} {n} {extra}")
        else:
            typer.echo(f"  FAIL {n}: {v}")
    typer.echo("\n[選用相依]（缺少不影響核心圖種）")
    for n, v in env["optional"].items():
        typer.echo(f"  {mark(v['ok'], False)} {n} {v.get('detail', '')}")
    typer.echo("\n[圖種範例]")
    for n, v in env["kinds"].items():
        typer.echo(f"  {mark(v['ok'])} {n} {v.get('detail') or v['example']}")
    typer.echo(f"\nRESULT: {'PASS' if env['ok'] else 'FAIL'}")
    raise typer.Exit(0 if env["ok"] else 1)


def main() -> None:  # python -m draftagent.cli
    app()


if __name__ == "__main__":
    main()
