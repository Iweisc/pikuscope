"""Knowledge base: repo guideline scanning (CLAUDE.md, AGENTS.md, .cursorrules, ...).

CodeRabbit `knowledge_base.code_guidelines` parity: these files carry the team's own
rules and are injected into review prompts.
"""

from __future__ import annotations

from pathlib import Path

from .context import RepoContext

GUIDELINE_FILES = [
    "CLAUDE.md", "AGENTS.md", "AGENT.md", ".cursorrules", ".cursor/rules",
    ".github/copilot-instructions.md", "CONTRIBUTING.md", ".windsurfrules",
    "docs/CONTRIBUTING.md", ".pikuscope/guidelines.md",
]

MAX_GUIDELINE_CHARS = 12_000


def collect_guidelines(ctx: RepoContext, extra_patterns: list[str] | None = None) -> str:
    """Concatenate whatever guideline docs exist in the repo, capped."""
    chunks: list[str] = []
    seen: set[str] = set()
    budget = MAX_GUIDELINE_CHARS

    candidates = list(GUIDELINE_FILES)
    # .cursor/rules is a directory of .mdc files
    for f in ctx.list_files(".cursor/rules"):
        candidates.append(f)
    for pat in extra_patterns or []:
        for f in ctx.list_files():
            if f == pat or f.endswith("/" + pat):
                candidates.append(f)

    for path in candidates:
        if path in seen or budget <= 0:
            continue
        seen.add(path)
        content = ctx.file_content(path)
        if not content or not content.strip():
            continue
        take = content.strip()[: min(budget, 6000)]
        chunks.append(f"## {path}\n{take}")
        budget -= len(take)
    return "\n\n".join(chunks)
