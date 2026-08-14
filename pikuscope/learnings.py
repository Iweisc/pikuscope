"""Learnings store: repo-specific reviewer knowledge, CodeRabbit-style.

Learnings come from chat ("@pikuscope remember ..."), from disputed findings, and
from benchmark analysis. Stored as JSONL; retrieved per review, filtered by path scope.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, asdict
from pathlib import Path

from .config import _match


@dataclass
class Learning:
    text: str
    scope: str = "**"  # glob of paths it applies to
    source: str = "chat"  # chat | dispute | bench | manual
    created_at: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


class LearningsStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def add(self, text: str, scope: str = "**", source: str = "chat") -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as fh:
            fh.write(json.dumps(Learning(text=text, scope=scope, source=source,
                                         created_at=time.time()).to_dict()) + "\n")

    def all(self) -> list[Learning]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text().splitlines():
            try:
                out.append(Learning(**json.loads(line)))
            except (json.JSONDecodeError, TypeError):
                continue
        return out

    def for_paths(self, paths: list[str], limit: int = 30) -> list[str]:
        out = []
        for l in self.all():
            if l.scope == "**" or any(_match(p, l.scope) for p in paths):
                out.append(l.text)
        return out[-limit:]
