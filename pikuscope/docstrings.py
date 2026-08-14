"""Docstring generation for functions changed in a PR (CodeRabbit 'generate docstrings' parity)."""

from __future__ import annotations

from typing import Any

from .context import RepoContext
from .diff import parse_unified_diff
from .llm import LLMClient, extract_json

DOCSTRING_SYSTEM = """You generate docstrings/doc-comments for functions, methods, classes, and \
components that a pull request adds or modifies. Match the language's idiom (JSDoc/TSDoc for \
TS/JS, PEP 257 for Python, doc comments for Rust/Go, etc.) and the repository's existing style.

Rules:
- Only document symbols the diff touches that lack adequate documentation.
- Describe behavior, parameters, return values, thrown errors, and side effects — from the code, \
never invented.
- Keep each docstring tight; no restating the obvious.

Output ONLY JSON:
{"docstrings": [{"path": str, "insert_before_line": int (1-based line of the symbol's first line at head), "indent": str (exact leading whitespace), "text": str (the full comment block, WITHOUT the indent applied)}]}
"""


def generate_docstrings(llm: LLMClient, ctx: RepoContext, diff_text: str) -> list[dict[str, Any]]:
    fds = [f for f in parse_unified_diff(diff_text) if not f.is_binary and f.status != "removed"]
    blocks = []
    for fd in fds:
        content = ctx.file_content(fd.path)
        if content is None or len(content) > 60_000:
            continue
        numbered = "\n".join(f"{i + 1:>5}\t{l}" for i, l in enumerate(content.splitlines()))
        blocks.append(f"## {fd.path}\n### Diff\n{fd.annotated(max_chars=8000)}\n### Full file\n{numbered}")
    if not blocks:
        return []
    data = llm.chat_json(
        [
            {"role": "system", "content": DOCSTRING_SYSTEM},
            {"role": "user", "content": "\n\n".join(blocks)[:180_000]},
        ],
        reasoning_effort="medium",
    )
    return data.get("docstrings", [])
