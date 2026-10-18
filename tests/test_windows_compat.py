"""Windows 相容性：檔案讀寫必須指定 encoding、cp950 console 不崩潰、venv 路徑、ODA 偵測、字型候選。"""

from __future__ import annotations

import ast
import io
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = ("draftagent", "tools", "scripts")
# `<owner>.open(...)` 不是文字檔 open：影像 / 柵格 / IFC / 壓縮檔 / os 低階 fd
_NON_TEXT_OPENERS = {
    "os",
    "zipfile",
    "tarfile",
    "gzip",
    "rasterio",
    "fiona",
    "Image",
    "ifcopenshell",
}


def _mode_of(call: ast.Call, pos: int) -> str | None:
    """open() 的 mode：第 pos 個位置參數或 mode= 關鍵字（非字串常數回傳 None）。"""
    node = None
    if len(call.args) > pos:
        node = call.args[pos]
    for kw in call.keywords:
        if kw.arg == "mode":
            node = kw.value
    if node is None:
        return "r"
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _missing_encoding(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    bad: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
        if name not in ("open", "read_text", "write_text"):
            continue
        if any(kw.arg == "encoding" or kw.arg is None for kw in node.keywords):
            continue  # 有 encoding=，或 **kwargs 轉傳
        if name == "open":
            # builtin open(file, mode)；Path.open(mode)；os.open / 其他 .open 物件（zipfile、tarfile…）略過
            if isinstance(f, ast.Attribute):
                owner = f.value
                if isinstance(owner, ast.Name) and owner.id in _NON_TEXT_OPENERS:
                    continue
                mode = _mode_of(node, 0)
            else:
                mode = _mode_of(node, 1)
            if mode is not None and "b" in mode:
                continue
        if name == "write_text" and len(node.args) >= 2:
            continue  # 位置參數給了 encoding
        if name == "read_text" and len(node.args) >= 1:
            continue
        bad.append(f"{path.relative_to(ROOT).as_posix()}:{node.lineno} {name}()")
    return bad


def test_text_io_specifies_encoding():
    """Windows 預設 locale 編碼是 cp950：文字檔讀寫一律明確指定 encoding（二進位模式除外）。"""
    bad: list[str] = []
    for d in SCAN_DIRS:
        for p in sorted((ROOT / d).rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            bad += _missing_encoding(p)
    assert not bad, "未指定 encoding：\n" + "\n".join(bad)


# ---------------------------------------------------------------- cp950 console


def _cp950_stream() -> io.TextIOWrapper:
    return io.TextIOWrapper(io.BytesIO(), encoding="cp950", errors="strict")


def test_cp950_stream_would_crash_without_fix():
    s = _cp950_stream()
    with pytest.raises(UnicodeEncodeError):
        s.write("🟢 PASS")


def test_ensure_utf8_stdio_reconfigures_cp950(monkeypatch):
    from draftagent._console import ensure_utf8_stdio

    out, err = _cp950_stream(), _cp950_stream()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    assert ensure_utf8_stdio(force=True) == ["stdout", "stderr"]
    print("🟢 綠燈 🔴 紅燈 坵塊 㙟")  # emoji + 罕用字（cp950 沒有）
    print("🔴 錯誤", file=sys.stderr)
    sys.stdout.flush()
    assert out.buffer.getvalue().decode("utf-8") == "🟢 綠燈 🔴 紅燈 坵塊 㙟\n"
    assert ensure_utf8_stdio(force=True) == []  # 已是 UTF-8：不再動


def test_ensure_utf8_stdio_noop_off_windows(monkeypatch):
    from draftagent import _console

    monkeypatch.setattr(_console.sys, "platform", "darwin")
    monkeypatch.setattr(sys, "stdout", _cp950_stream())
    assert _console.ensure_utf8_stdio() == []


def test_cli_on_cp950_console_does_not_crash(monkeypatch, capfd):
    """模擬 Windows：sys.platform=win32、stdout 為 cp950 → CLI 印 🟢🔴 / 中文不 UnicodeEncodeError。"""
    from draftagent import _console, cli

    monkeypatch.setattr(_console.sys, "platform", "win32")
    out = _cp950_stream()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "argv", ["draftagent", "kinds"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code in (0, None)
    sys.stdout.flush()
    text = out.buffer.getvalue().decode("utf-8")
    assert "下一步" in text
    # verifier 的燈號也要能印
    print("RESULT: 🟢 PASS / 🔴 FAIL")
    sys.stdout.flush()
    assert "🟢" in out.buffer.getvalue().decode("utf-8")


# ---------------------------------------------------------------- venv / 路徑


def test_venv_bin_per_platform(tmp_path):
    from draftagent._paths import venv_bin, venv_bin_dir

    assert (
        venv_bin(tmp_path, "draftagent", windows=False) == tmp_path / ".venv" / "bin" / "draftagent"
    )
    assert (
        venv_bin(tmp_path, "python", windows=True) == tmp_path / ".venv" / "Scripts" / "python.exe"
    )
    assert venv_bin(tmp_path, "pytest.exe", windows=True).name == "pytest.exe"
    assert venv_bin_dir(tmp_path, windows=True) == tmp_path / "Scripts"


def test_is_within(tmp_path):
    from draftagent._paths import is_within

    (tmp_path / "repo" / "work").mkdir(parents=True)
    spec = tmp_path / "repo" / "work" / "a.py"
    spec.write_text("", encoding="utf-8")
    assert is_within(spec, tmp_path / "repo")
    assert is_within(tmp_path / "repo", tmp_path / "repo")
    assert not is_within(tmp_path / "repo2" / "a.py", tmp_path / "repo")  # 前綴相同但不是子目錄
    assert not is_within(tmp_path / "a.py", tmp_path / "repo")


def test_is_within_windows_case_and_drive(monkeypatch):
    """Windows 語意：大小寫不敏感、不同磁碟代號 False（以 ntpath 模擬，不碰實際檔案系統）。"""
    import ntpath
    from pathlib import PureWindowsPath

    from draftagent import _paths

    monkeypatch.setattr(_paths, "is_windows", lambda: True)
    monkeypatch.setattr(_paths.os.path, "normcase", ntpath.normcase)
    monkeypatch.setattr(_paths.os, "sep", "\\")

    class FakePath(PureWindowsPath):
        def resolve(self):
            return self

    monkeypatch.setattr(_paths, "Path", FakePath)
    assert _paths.is_within(r"c:\users\Eric\Repo\work\a.py", r"C:\Users\eric\repo")
    assert _paths.is_within(r"C:\Users\eric\repo", "C:\\Users\\eric\\repo\\")
    assert not _paths.is_within(r"D:\Users\eric\repo\a.py", r"C:\Users\eric\repo")
    assert not _paths.is_within(r"C:\Users\eric\repo2\a.py", r"C:\Users\eric\repo")


def test_load_spec_py_outside_repo_rejected(tmp_path):
    from draftagent.build import load_spec
    from draftagent.errors import SpecLoadError

    p = tmp_path / "outside.py"
    p.write_text("spec = None\n", encoding="utf-8")
    with pytest.raises(SpecLoadError, match="repo 內"):
        load_spec(p, kind="plan")


def test_workspace_exe_names(monkeypatch, tmp_path):
    from draftagent.eval import workspace as wsmod

    ws = wsmod.Workspace(root=tmp_path, source=tmp_path)
    monkeypatch.setattr(wsmod, "is_windows", lambda: True)
    monkeypatch.setattr(wsmod, "venv_bin_dir", lambda v: v / "Scripts")
    assert ws.exe("python") == tmp_path / ".venv" / "Scripts" / "python.exe"
    assert ws.exe("draftagent") == tmp_path / ".venv" / "Scripts" / "draftagent.cmd"
    assert wsmod.is_protected("draftagent\\verify\\dxf_check.py")
    assert wsmod.is_protected("draftagent/hydro/codes/taoyuan_runoff.yaml")
    assert not wsmod.is_protected("draftagent/render/dxf.py")


def test_claude_rule_path_windows_drive():
    from pathlib import PureWindowsPath

    from draftagent.eval.runners import abs_rule_path

    assert abs_rule_path(PureWindowsPath(r"C:\Users\eric\draftagent")) == "c/Users/eric/draftagent"
    assert abs_rule_path(Path("/Users/eric/draftagent")) == "Users/eric/draftagent"


# ---------------------------------------------------------------- 字型


def test_cad_font_default_is_windows_builtin(monkeypatch):
    from draftagent.render import standards

    monkeypatch.delenv(standards.FONT_ENV, raising=False)
    assert standards.cad_font() == "msjh.ttc"
    monkeypatch.setenv(standards.FONT_ENV, "kaiu.ttf")
    assert standards.cad_font() == "kaiu.ttf"


def test_preview_font_candidates_include_windows(monkeypatch):
    from draftagent.render import preview

    monkeypatch.setenv("WINDIR", r"D:\Win")
    names = [p.name for p in preview.cjk_font_paths()]
    for f in ("msjh.ttc", "msjhl.ttc", "mingliu.ttc", "kaiu.ttf", "NotoSansCJK-Regular.ttc"):
        assert f in names
    assert preview.windows_font_dir() == Path(r"D:\Win") / "Fonts"
    assert set(preview.WINDOWS_CJK_FILES) <= set(preview.CJK_FALLBACK_FILES)


def test_preview_substitutes_msjh_when_missing():
    """Mac / Linux 沒有 msjh.ttc：預覽前換成本機 CJK 字型；有 msjh（Windows）則不動。"""
    import ezdxf
    from ezdxf.fonts import fonts

    from draftagent.render import preview, standards

    doc = ezdxf.new("R2010", setup=True)
    standards.text_style(doc)
    changed = preview.substitute_missing_fonts(doc)
    if fonts.font_manager.has_font("msjh.ttc"):
        assert "CJK" not in changed
    elif any(fonts.font_manager.has_font(f) for f in preview.CJK_FALLBACK_FILES):
        assert changed.get("CJK") in preview.CJK_FALLBACK_FILES
