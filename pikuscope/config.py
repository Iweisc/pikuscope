"""Configuration: .pikuscope.yaml — path filters, profiles, instructions.

Mirrors CodeRabbit's .coderabbit.yaml / Greptile's greptile.json surface.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Files that are noise for review by default (CodeRabbit-style default filters).
DEFAULT_EXCLUDES = [
    "**/*.lock", "**/package-lock.json", "**/yarn.lock", "**/pnpm-lock.yaml",
    "**/bun.lockb", "**/bun.lock", "**/Cargo.lock", "**/poetry.lock", "**/uv.lock",
    "**/composer.lock", "**/Gemfile.lock", "**/go.sum",
    "**/*.min.js", "**/*.min.css", "**/dist/**", "**/build/**", "**/out/**",
    "**/node_modules/**", "**/vendor/**", "**/*.svg", "**/*.png", "**/*.jpg",
    "**/*.jpeg", "**/*.gif", "**/*.ico", "**/*.webp", "**/*.woff", "**/*.woff2",
    "**/*.ttf", "**/*.otf", "**/*.eot", "**/*.pdf", "**/*.snap",
    "**/generated/**", "**/__generated__/**", "**/*.generated.*", "**/*.pb.go",
    "**/*_pb2.py", "**/*.d.ts.map", "**/*.js.map", "**/*.css.map",
]


@dataclass
class Config:
    # review behavior
    profile: str = "chill"  # chill | assertive
    path_filters: list[str] = field(default_factory=list)  # extra excludes (! prefix = include)
    path_instructions: list[dict[str, str]] = field(default_factory=list)  # {path, instructions}
    tone_instructions: str = ""
    # features
    high_level_summary: bool = True
    walkthrough: bool = True
    sequence_diagram: bool = True
    poem: bool = False
    review_status: bool = True
    collapse_walkthrough: bool = False
    # gates
    fail_on: list[str] = field(default_factory=list)  # e.g. ["critical"]
    max_findings: int = 25
    confidence_threshold: float = 0.6
    # chat
    bot_name: str = "pikuscope"
    # learnings
    learnings_path: str = ".pikuscope/learnings.jsonl"
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, repo_root: str | Path | None = None, data: dict[str, Any] | None = None) -> "Config":
        if data is None:
            data = {}
            if repo_root:
                for name in (".pikuscope.yaml", ".pikuscope.yml"):
                    p = Path(repo_root) / name
                    if p.exists():
                        data = yaml.safe_load(p.read_text()) or {}
                        break
        reviews = data.get("reviews", {}) if isinstance(data, dict) else {}
        cfg = cls(raw=data if isinstance(data, dict) else {})
        cfg.profile = reviews.get("profile", data.get("profile", cfg.profile))
        cfg.path_filters = reviews.get("path_filters", data.get("path_filters", []) or [])
        cfg.path_instructions = reviews.get("path_instructions", data.get("path_instructions", []) or [])
        cfg.tone_instructions = data.get("tone_instructions", "")
        cfg.high_level_summary = reviews.get("high_level_summary", True)
        cfg.sequence_diagram = reviews.get("sequence_diagrams", reviews.get("sequence_diagram", True))
        cfg.poem = reviews.get("poem", False)
        cfg.collapse_walkthrough = reviews.get("collapse_walkthrough", False)
        cfg.fail_on = data.get("fail_on", []) or []
        cfg.max_findings = int(data.get("max_findings", cfg.max_findings))
        cfg.confidence_threshold = float(data.get("confidence_threshold", cfg.confidence_threshold))
        return cfg

    def is_reviewable(self, path: str) -> bool:
        """Apply default excludes then user path_filters (gitignore-style, ! = re-include)."""
        excluded = any(_match(path, pat) for pat in DEFAULT_EXCLUDES)
        for pat in self.path_filters:
            neg = pat.startswith("!")
            p = pat[1:] if neg else pat
            if _match(path, p):
                excluded = not neg
        return not excluded

    def instructions_for(self, path: str) -> list[str]:
        out = []
        for item in self.path_instructions:
            if _match(path, item.get("path", "")):
                out.append(item.get("instructions", ""))
        return [i for i in out if i]


def _match(path: str, pattern: str) -> bool:
    """Match with ** support: try full path and basename, and dir-prefix semantics."""
    if not pattern:
        return False
    if fnmatch.fnmatch(path, pattern):
        return True
    # "**/x" should also match top-level "x"
    if pattern.startswith("**/") and fnmatch.fnmatch(path, pattern[3:]):
        return True
    # "dir/**" should match everything under dir
    if pattern.endswith("/**") and (path.startswith(pattern[:-3] + "/") or path == pattern[:-3]):
        return True
    return False
