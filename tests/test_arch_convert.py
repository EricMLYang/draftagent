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
