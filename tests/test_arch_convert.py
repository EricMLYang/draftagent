"""DWG → DXF 轉檔：平台判斷、工具優先序、外部工具呼叫（以假的 dwg2dxf 模擬）、無工具時的提示。"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from draftagent.arch import convert as cv
from draftagent.cli import app

FIXTURE = Path(__file__).parent / "fixtures" / "archdxf" / "clean_mm" / "drawing.dxf"


def _fake_dwg2dxf(bindir: Path, *, fail: bool = False) -> None:
    """模擬 LibreDWG：`dwg2dxf -y -o out.dxf in.dwg` → 把 fixture DXF 複製到 out。"""
    script = bindir / "dwg2dxf"
    body = "exit 3" if fail else f'cp "{FIXTURE}" "$3"'
    script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def no_oda(monkeypatch):
    monkeypatch.setattr(cv, "_detect_oda", lambda system: cv.Converter("oda", False))


@pytest.mark.parametrize(
    ("system", "first"), [("Windows", "oda"), ("Darwin", "libredwg"), ("Linux", "libredwg")]
)
def test_platform_preference(system, first, no_oda):
    assert cv.detect(system)[0].name == first


def test_current_platform_is_known():
    assert cv.current_platform() in cv.PREFERENCE


def test_no_converter_gives_platform_hints(tmp_path, monkeypatch, no_oda):
    monkeypatch.setenv("PATH", str(tmp_path))  # 找不到 dwg2dxf
    src = tmp_path / "a.dwg"
    src.write_bytes(b"AC1032")
    for system in ("Windows", "Darwin", "Linux"):
        res = cv.convert_dwg(src, system=system)
        assert not res.ok and res.hints == cv.INSTALL_HINTS[system]
        assert any("DXF" in h for h in res.hints)  # 一律提示「另存 DXF」這條路


@pytest.mark.skipif(os.name == "nt", reason="假 dwg2dxf 是 sh 腳本")
def test_libredwg_conversion_roundtrip(tmp_path, monkeypatch, no_oda):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _fake_dwg2dxf(bindir)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    src = tmp_path / "plan.dwg"
    src.write_bytes(b"AC1032")
    res = cv.convert_dwg(src, system="Darwin")
    assert res.ok, res.message
    assert res.converter == "libredwg" and Path(res.output).exists()


@pytest.mark.skipif(os.name == "nt", reason="假 dwg2dxf 是 sh 腳本")
def test_converter_failure_reported(tmp_path, monkeypatch, no_oda):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _fake_dwg2dxf(bindir, fail=True)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    src = tmp_path / "plan.dwg"
    src.write_bytes(b"AC1032")
    res = cv.convert_dwg(src, system="Linux")
    assert not res.ok and "libredwg" in res.message and res.hints


def test_dxf_input_passthrough():
    res = cv.convert_dwg(FIXTURE)
    assert res.ok and res.output == str(FIXTURE) and res.converter is None


def test_cli_convert_dwg_no_tool_exit_1(tmp_path, monkeypatch, no_oda):
    monkeypatch.setenv("PATH", str(tmp_path))
    src = tmp_path / "a.dwg"
    src.write_bytes(b"AC1032")
    r = CliRunner().invoke(app, ["convert-dwg", str(src), "--json"])
    assert r.exit_code == 1
    assert '"hints"' in r.output and '"converters"' in r.output


# ---------------------------------------------------------------- Windows ODA（版本號資料夾）


def test_windows_oda_glob_versioned_folder(monkeypatch):
    """Windows：glob Program Files\\ODA\\ODAFileConverter <ver>\\ODAFileConverter.exe，新版優先，
    並把找到的路徑設給 ezdxf odafc 的 win_exec_path。"""
    import ezdxf

    pf, pf86 = r"C:\Program Files", r"C:\Program Files (x86)"
    monkeypatch.setenv("ProgramFiles", pf)
    monkeypatch.setenv("ProgramFiles(x86)", pf86)
    seen: list[str] = []
    found = {
        pf: [
            rf"{pf}\ODA\ODAFileConverter 9.8.0\ODAFileConverter.exe",
            rf"{pf}\ODA\ODAFileConverter 25.12.0\ODAFileConverter.exe",
            rf"{pf}\ODA\ODAFileConverter 24.4.0\ODAFileConverter.exe",
        ],
        pf86: [rf"{pf86}\ODA\ODAFileConverter 21.1.0\ODAFileConverter.exe"],
    }

    def fake_glob(pattern):
        seen.append(pattern)
        root = max((k for k in found if pattern.startswith(k)), key=len, default=None)
        return list(found[root]) if root else []

    monkeypatch.setattr(cv.glob, "glob", fake_glob)
    monkeypatch.setattr(cv.Path, "exists", lambda self: "ODAFileConverter" in str(self))
    old = ezdxf.options.get("odafc-addon", "win_exec_path")
    try:
        cands = cv._windows_oda_candidates()
        assert cands[0].endswith(r"25.12.0\ODAFileConverter.exe")  # 新版在前（數字比較）
        assert cands[2].endswith(r"9.8.0\ODAFileConverter.exe")
        assert cands[-1].startswith(pf86)  # Program Files (x86) 也找
        assert all(p.endswith("ODAFileConverter.exe") and "ODAFileConverter*" in p for p in seen)
        conv = cv._detect_oda("Windows")
        assert conv.available and conv.path == cands[0]
        assert ezdxf.options.get("odafc-addon", "win_exec_path").strip('"') == cands[0]
        assert cv.detect("Windows")[0].name == "oda"
    finally:
        ezdxf.options.set("odafc-addon", "win_exec_path", old)


def test_windows_oda_not_installed(monkeypatch):
    monkeypatch.setattr(cv.glob, "glob", lambda pattern: [])
    monkeypatch.setattr(cv, "ODA_CANDIDATES", {})
    monkeypatch.setattr(cv.shutil, "which", lambda name: None)
    from ezdxf.addons import odafc

    monkeypatch.setattr(odafc, "is_installed", lambda: False)
    assert cv._windows_oda_candidates() == []
    assert not cv._detect_oda("Windows").available
