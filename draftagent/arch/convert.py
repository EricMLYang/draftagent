"""DWG → DXF 轉檔：依作業系統挑選本機可用的外部轉檔工具。純函式（呼叫外部程式），不得呼叫 LLM。

轉檔是整條流程裡唯一與平台綁定的一段；核心（inspect-dxf / arch-import）只吃 DXF。

各平台的優先順序（先找到的先用）：
- Windows：ODA File Converter（相容性最好、可背景執行）→ LibreDWG
- macOS  ：LibreDWG（`brew install libredwg`，純指令列）→ ODA（會跳出視窗）
- Linux  ：LibreDWG（套件管理器）→ ODA（需 xvfb-run 才能背景執行）

實務上最可靠的仍是請建築師在 AutoCAD / BricsCAD「另存 DXF」——都找不到工具時會提示這條路。
授權：ODA 需同意其使用條款；LibreDWG 為 GPL（本 repo 只以外部指令呼叫，不連結）。
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

# macOS / Linux 上 ODA 的常見安裝位置（ezdxf odafc 也會自己找；這裡列出以便偵測與提示）
ODA_CANDIDATES = {
    "Darwin": ["/Applications/ODAFileConverter.app/Contents/MacOS/ODAFileConverter"],
    "Linux": ["/usr/bin/ODAFileConverter", "/usr/local/bin/ODAFileConverter"],
}

INSTALL_HINTS = {
    "Windows": [
        "安裝 ODA File Converter（opendesign.com/guestfiles/oda_file_converter，需同意使用條款）",
        "或請建築師 / 在 AutoCAD、BricsCAD 中「另存新檔 → DXF」",
    ],
    "Darwin": [
        "brew install libredwg（提供 dwg2dxf，純指令列）",
        "或安裝 ODA File Converter 到 /Applications（轉檔時會跳出視窗）",
        "或請建築師在 AutoCAD / BricsCAD「另存新檔 → DXF」",
    ],
    "Linux": [
        "sudo apt install libredwg-tools（或發行版對應套件，提供 dwg2dxf）",
        "或安裝 ODA File Converter（.deb / .rpm）並安裝 xvfb 以背景執行",
        "或請建築師在 AutoCAD / BricsCAD「另存新檔 → DXF」",
    ],
}

PREFERENCE = {
    "Windows": ("oda", "libredwg"),
    "Darwin": ("libredwg", "oda"),
    "Linux": ("libredwg", "oda"),
}


@dataclass
class Converter:
    name: str  # "oda" | "libredwg"
    available: bool
    path: str | None = None
    note: str = ""


@dataclass
class ConvertResult:
    ok: bool
    platform: str
    source: str
    output: str | None = None
    converter: str | None = None
    tried: list[str] = field(default_factory=list)
    message: str = ""
    hints: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def current_platform() -> str:
    """'Windows' | 'Darwin' | 'Linux'（其他系統當 Linux 處理）。"""
    s = platform.system()
    return s if s in PREFERENCE else "Linux"


def _detect_oda(system: str) -> Converter:
    try:
        from ezdxf.addons import odafc

        if odafc.is_installed():
            return Converter("oda", True, None, "ezdxf.addons.odafc 偵測到")
    except Exception:  # noqa: BLE001 - odafc 不存在或設定錯誤都視為不可用
        pass
    for p in [shutil.which("ODAFileConverter"), *ODA_CANDIDATES.get(system, [])]:
        if p and Path(p).exists():
            note = ""
            if system == "Linux" and not shutil.which("xvfb-run"):
                note = "未找到 xvfb-run：ODA 會嘗試開視窗，無螢幕環境可能失敗"
            if system == "Darwin":
                note = "macOS 上轉檔時會跳出視窗"
            return Converter("oda", True, p, note)
    return Converter("oda", False)


def _detect_libredwg() -> Converter:
    p = shutil.which("dwg2dxf")
    return Converter("libredwg", bool(p), p)


def detect(system: str | None = None) -> list[Converter]:
    """依該平台偏好順序回傳兩種轉檔工具的偵測結果。"""
    system = system or current_platform()
    found = {"oda": _detect_oda(system), "libredwg": _detect_libredwg()}
    return [found[n] for n in PREFERENCE[system]]


def _run_oda(conv: Converter, src: Path, out: Path, system: str) -> None:
    from ezdxf.addons import odafc

    if conv.path:  # 自訂位置：告訴 ezdxf 去哪找
        import ezdxf

        key = "win_exec_path" if system == "Windows" else "unix_exec_path"
        ezdxf.options.set("odafc-addon", key, conv.path)
    odafc.convert(str(src), str(out), version="R2018", replace=True)


def _run_libredwg(conv: Converter, src: Path, out: Path, timeout: float) -> None:
    proc = subprocess.run(
        [conv.path or "dwg2dxf", "-y", "-o", str(out), str(src)],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0 or not out.exists():
        raise RuntimeError(f"dwg2dxf exit {proc.returncode}：{(proc.stderr or proc.stdout)[-400:]}")


def _readable(dxf: Path) -> str | None:
    """轉出的 DXF 能否被 ezdxf 讀（含 recover）；回傳錯誤訊息或 None。"""
    import ezdxf
    from ezdxf import recover

    try:
        ezdxf.readfile(str(dxf))
        return None
    except Exception:  # noqa: BLE001
        try:
            recover.readfile(str(dxf))
            return None
        except Exception as e:  # noqa: BLE001
            return f"{type(e).__name__}: {e}"


def convert_dwg(
    src: str | Path,
    out: str | Path | None = None,
    *,
    prefer: str | None = None,
    system: str | None = None,
    timeout: float = 300,
) -> ConvertResult:
    """DWG → DXF。依平台偏好（或 `prefer` 指定）逐一嘗試可用的轉檔工具；全部失敗回傳安裝提示。"""
    system = system or current_platform()
    src = Path(src)
    res = ConvertResult(ok=False, platform=system, source=str(src))
    if not src.exists():
        res.message = f"找不到 {src}"
        return res
    if src.suffix.lower() == ".dxf":
        res.ok, res.output, res.message = True, str(src), "已是 DXF，不需轉檔"
        return res
    out = Path(out) if out else src.with_suffix(".dxf")
    out.parent.mkdir(parents=True, exist_ok=True)

    convs = detect(system)
    if prefer:
        convs.sort(key=lambda c: c.name != prefer)
    usable = [c for c in convs if c.available]
    if not usable:
        res.message = f"{system} 上沒有可用的 DWG 轉檔工具"
        res.hints = INSTALL_HINTS[system]
        return res

    errors: list[str] = []
    for conv in usable:
        res.tried.append(conv.name)
        with tempfile.TemporaryDirectory(prefix="draftagent-dwg-") as tmp:
            tmp_out = Path(tmp) / out.name
            try:
                if conv.name == "oda":
                    _run_oda(conv, src, tmp_out, system)
                else:
                    _run_libredwg(conv, src, tmp_out, timeout)
                bad = _readable(tmp_out)
                if bad:
                    raise RuntimeError(f"轉出的 DXF 無法讀取：{bad}")
                shutil.copyfile(tmp_out, out)
            except Exception as e:  # noqa: BLE001 - 換下一個轉檔工具
                errors.append(f"{conv.name}: {type(e).__name__}: {e}")
                continue
        res.ok, res.output, res.converter = True, str(out), conv.name
        res.message = f"以 {conv.name} 轉檔完成" + (f"（{conv.note}）" if conv.note else "")
        if errors:
            res.message += "；先前失敗：" + " / ".join(errors)
        return res
    res.message = "所有轉檔工具都失敗：" + " / ".join(errors)
    res.hints = INSTALL_HINTS[system]
    return res
