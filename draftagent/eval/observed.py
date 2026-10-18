"""Grader 看到的「觀測值」：重新 build 後的 verifier metrics、產物 JSON、DXF 文字。

只讀檔，不呼叫 LLM。任何取不到的值丟 KeyError（grader 會記成該檢查項 missing / fail）。
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any


class Observed:
    def __init__(
        self, results: list[dict[str, Any]], artifacts: dict[str, Path], spec: dict[str, Any]
    ) -> None:
        self.results = {r["name"]: r for r in results}
        self.artifacts = {k: Path(v) for k, v in artifacts.items()}
        self.spec = spec

    # ------------------------------------------------------------ verifier metrics

    def metric(self, verifier: str, *keys: str) -> Any:
        if verifier not in self.results:
            raise KeyError(f"沒有 verifier {verifier!r}（有：{sorted(self.results)}）")
        cur: Any = self.results[verifier].get("metrics") or {}
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                raise KeyError(f"{verifier}.metrics 缺 {'.'.join(keys)}")
            cur = cur[k]
        return cur

    def epsgs(self, verifier: str) -> list[Any]:
        """metrics 內所有以 epsg 結尾的欄位值（遞迴）。"""
        out: list[Any] = []

        def walk(x: Any) -> None:
            if isinstance(x, dict):
                for k, v in x.items():
                    if isinstance(k, str) and k.lower().endswith("epsg"):
                        out.append(v)
                    else:
                        walk(v)

        walk(self.metric(verifier))
        if not out:
            raise KeyError(f"{verifier}.metrics 沒有任何 epsg 欄位")
        return out

    # ------------------------------------------------------------ artifacts

    def path(self, key: str) -> Path:
        p = self.artifacts.get(key)
        if p is None or not p.exists():
            raise KeyError(f"沒有產物 {key!r}（有：{sorted(self.artifacts)}）")
        return p

    def json(self, key: str) -> Any:
        return json.loads(self.path(key).read_text(encoding="utf-8"))

    def texts(self, key: str = "dxf") -> list[str]:
        return list(_dxf_texts(str(self.path(key))))

    def insunits(self, key: str = "dxf") -> int:
        import ezdxf

        return int(ezdxf.readfile(str(self.path(key))).header.get("$INSUNITS", 0))


@cache
def _dxf_texts(path: str) -> tuple[str, ...]:
    """DXF 所有區塊（含 model / paper space 與尺寸匿名區塊）的 TEXT / MTEXT / ATTRIB 文字。"""
    import ezdxf

    doc = ezdxf.readfile(path)
    out: list[str] = []
    for blk in doc.blocks:
        for e in blk:
            t = e.dxftype()
            if t in ("TEXT", "ATTRIB", "ATTDEF"):
                out.append(e.dxf.get("text", ""))
            elif t == "MTEXT":
                out.append(e.plain_text())
            elif t == "DIMENSION":
                out.append(e.dxf.get("text", ""))
            elif t == "INSERT":
                out += [a.dxf.get("text", "") for a in e.attribs]
    return tuple(s for s in out if s)
