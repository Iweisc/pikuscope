"""Optional static-analysis integration: run available linters on changed files.

Results are fed to the finder as hints (and can be posted directly). Mirrors
CodeRabbit's linter integrations, degrading gracefully when tools are absent.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

LINTERS = {
    ".py": [
        ("ruff", ["ruff", "check", "--output-format", "json", "--no-cache"]),
    ],
    ".ts": [("biome", ["biome", "lint", "--reporter=json"])],
    ".tsx": [("biome", ["biome", "lint", "--reporter=json"])],
    ".js": [("biome", ["biome", "lint", "--reporter=json"])],
    ".jsx": [("biome", ["biome", "lint", "--reporter=json"])],
    ".sh": [("shellcheck", ["shellcheck", "--format", "json"])],
}


def run_linters(root: str | Path, changed_paths: list[str], timeout: int = 120) -> list[dict]:
    """Run whichever supported linters exist on PATH against changed files.

    Returns [{tool, path, line, code, message}].
    """
    root = Path(root)
    by_tool: dict[str, list[str]] = {}
    tool_cmds: dict[str, list[str]] = {}
    for p in changed_paths:
        ext = Path(p).suffix
        for tool, cmd in LINTERS.get(ext, []):
            if shutil.which(cmd[0]):
                by_tool.setdefault(tool, []).append(p)
                tool_cmds[tool] = cmd
    findings: list[dict] = []
    for tool, paths in by_tool.items():
        cmd = tool_cmds[tool] + [str(root / p) for p in paths]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=root)
        except (subprocess.TimeoutExpired, OSError):
            continue
        findings.extend(_parse(tool, proc.stdout, root))
    return findings


def _parse(tool: str, output: str, root: Path) -> list[dict]:
    out: list[dict] = []
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return out
    if tool == "ruff":
        for item in data:
            out.append(
                {"tool": "ruff", "path": _rel(item.get("filename", ""), root),
                 "line": (item.get("location") or {}).get("row"),
                 "code": item.get("code"), "message": item.get("message", "")}
            )
    elif tool == "shellcheck":
        for item in data:
            out.append(
                {"tool": "shellcheck", "path": _rel(item.get("file", ""), root),
                 "line": item.get("line"), "code": f"SC{item.get('code')}",
                 "message": item.get("message", "")}
            )
    elif tool == "biome":
        for diag in (data.get("diagnostics") or []):
            loc = diag.get("location") or {}
            out.append(
                {"tool": "biome", "path": _rel((loc.get("path") or {}).get("file", ""), root),
                 "line": None, "code": diag.get("category"),
                 "message": (diag.get("description") or "")[:300]}
            )
    return out


def _rel(p: str, root: Path) -> str:
    try:
        return str(Path(p).resolve().relative_to(root.resolve()))
    except ValueError:
        return p
