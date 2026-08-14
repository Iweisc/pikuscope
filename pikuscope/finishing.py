"""Finishing touches: unit test generation and autofix (commit suggestions).

CodeRabbit parity: `generate unit tests`, `autofix`.
"""

from __future__ import annotations

import base64
from typing import Any

from .context import RepoContext
from .diff import parse_unified_diff
from .gh import Repo
from .llm import LLMClient
from .review import Finding

UNIT_TEST_SYSTEM = """You generate unit tests for the code a pull request adds or changes. \
Match the repository's existing test framework, file layout, naming, and assertion style \
(inspect existing tests first if provided). Only test the changed behavior; cover the main \
path plus the edge cases the diff introduces. Tests must be complete and runnable — real \
imports, no placeholders.

Output ONLY JSON:
{"tests": [{"path": str (test file path, following repo conventions; may be existing or new), \
"mode": "append"|"create", "content": str (the complete test code to append or the full new file)}]}
If the change is untestable (pure UI markup, config), output {"tests": []}.
"""


def generate_unit_tests(llm: LLMClient, ctx: RepoContext, diff_text: str) -> list[dict[str, Any]]:
    fds = [f for f in parse_unified_diff(diff_text) if not f.is_binary and f.status != "removed"]
    blocks = []
    test_examples: list[str] = []
    for fd in fds:
        content = ctx.file_content(fd.path)
        if content is None or len(content) > 50_000:
            continue
        blocks.append(f"## {fd.path}\n### Diff\n{fd.annotated(max_chars=8000)}\n### Full file\n{content[:30_000]}")
    # find existing tests for style reference
    for f in ctx.list_files():
        if (".test." in f or ".spec." in f or "/tests/" in f or f.startswith("tests/")) and len(test_examples) < 2:
            c = ctx.file_content(f)
            if c and len(c) < 8000:
                test_examples.append(f"## Existing test for style reference: {f}\n{c}")
    if not blocks:
        return []
    user = "\n\n".join(test_examples + blocks)[:180_000]
    data = llm.chat_json(
        [{"role": "system", "content": UNIT_TEST_SYSTEM}, {"role": "user", "content": user}],
        reasoning_effort="high",
    )
    return data.get("tests", [])


def autofix(repo: Repo, pr: dict[str, Any], findings: list[Finding]) -> list[str]:
    """Commit committable suggestions directly to the PR head branch. Returns commit messages."""
    head_ref = pr["head"]["ref"]
    committed: list[str] = []
    by_path: dict[str, list[Finding]] = {}
    for f in findings:
        if f.suggestion is not None:
            by_path.setdefault(f.path, []).append(f)
    for path, fs in by_path.items():
        data = repo.gh.get(f"repos/{repo.full}/contents/{path}", params={"ref": head_ref}, cache=False)
        if not isinstance(data, dict) or data.get("encoding") != "base64":
            continue
        content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        lines = content.splitlines(keepends=True)
        # apply bottom-up so line numbers stay valid
        for f in sorted(fs, key=lambda x: -x.start_line):
            start, end = f.start_line - 1, f.end_line
            if start < 0 or end > len(lines):
                continue
            repl = f.suggestion if f.suggestion.endswith("\n") else f.suggestion + "\n"
            lines[start:end] = [repl]
        new_content = "".join(lines)
        if new_content == content:
            continue
        msg = f"fix: apply pikuscope suggestions in {path}"
        repo.gh._request(  # noqa: SLF001 — contents API PUT
            "PUT",
            f"repos/{repo.full}/contents/{path}",
            body={
                "message": msg,
                "content": base64.b64encode(new_content.encode()).decode(),
                "sha": data["sha"],
                "branch": head_ref,
            },
        )
        committed.append(msg)
    return committed
