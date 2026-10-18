"""所有 verifier 共用的結果型別與 CLI 輸出格式。純函式，不得呼叫 LLM。"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class VerifyResult:
    name: str
    ok: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def error(self, msg: str) -> None:
        self.ok = False
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def render(self) -> str:
        lamp = "🟢 PASS" if self.ok else "🔴 FAIL"
        lines = [f"[{self.name}] {lamp}"]
        lines += [f"  ERROR  {e}" for e in self.errors]
        lines += [f"  WARN   {w}" for w in self.warnings]
        for k, v in self.metrics.items():
            lines.append(f"  {k}: {v}")
        return "\n".join(lines)


def emit(result: VerifyResult, as_json: bool = False) -> None:
    """CLI 共用出口：印出結果，失敗時 exit code 1。"""
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2) if as_json else result.render())
    sys.exit(0 if result.ok else 1)
