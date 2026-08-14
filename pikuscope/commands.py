"""Chat commands: @pikuscope <command> in PR comments, CodeRabbit-parity surface.

Supported:
  @pikuscope review               — incremental review of new commits
  @pikuscope full review          — re-review everything from scratch
  @pikuscope summary              — regenerate the summary comment
  @pikuscope generate docstrings  — docstrings for changed functions
  @pikuscope generate title       — suggest PR title/description
  @pikuscope resolve              — resolve all pikuscope review threads
  @pikuscope pause / resume       — toggle auto-review for this PR
  @pikuscope ignore               — skip this PR entirely
  @pikuscope remember <text>      — store a learning
  @pikuscope help                 — usage
  @pikuscope <anything else>      — free-form Q&A about the PR/codebase
"""

from __future__ import annotations

import re
from typing import Any

from .gh import Repo
from .llm import LLMClient
from .review import TOOLS_SPEC, make_tool_handler
from .context import RepoContext

HELP_TEXT = """## pikuscope commands

| Command | Effect |
|---|---|
| `@pikuscope review` | Incremental review of commits pushed since the last review |
| `@pikuscope full review` | Full re-review of the PR from scratch |
| `@pikuscope summary` | Regenerate the PR summary comment |
| `@pikuscope generate docstrings` | Add docstrings for functions changed in this PR |
| `@pikuscope generate unit tests` | Generate unit tests for the changed code |
| `@pikuscope autofix` | Commit the committable suggestions to the PR branch |
| `@pikuscope generate title` | Suggest a PR title and description |
| `@pikuscope resolve` | Resolve all pikuscope review threads |
| `@pikuscope pause` / `@pikuscope resume` | Pause/resume automatic reviews on this PR |
| `@pikuscope ignore` | Permanently skip this PR |
| `@pikuscope remember <lesson>` | Teach pikuscope a repo-specific review rule |
| `@pikuscope <question>` | Ask anything about this PR or the codebase |
"""

CHAT_SYSTEM = """You are pikuscope, an AI code review assistant, replying inside a GitHub pull \
request conversation. You have tools to read the repository at the PR head commit. Answer the \
user's question precisely and concisely in GitHub markdown. If the question is about the PR's \
changes, ground every claim in the actual diff/code (read it first). If asked for code, produce \
complete, correct snippets. Do not pad; do not add sign-offs."""


def parse_command(body: str, bot_name: str = "pikuscope") -> tuple[str, str] | None:
    """Return (command, args) if the comment addresses the bot, else None."""
    m = re.search(rf"@{re.escape(bot_name)}\b\s*(.*)", body, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    rest = m.group(1).strip()
    low = rest.lower()
    for cmd in ("full review", "generate docstrings", "generate unit tests", "generate title",
                "review", "summary", "resolve", "pause", "resume", "ignore", "help",
                "remember", "autofix", "configuration"):
        if low == cmd or low.startswith(cmd + " ") or low.startswith(cmd + "\n"):
            return cmd, rest[len(cmd):].strip()
    return ("chat", rest) if rest else ("help", "")


def answer_chat(llm: LLMClient, ctx: RepoContext, repo: Repo, pr: dict[str, Any],
                question: str, thread_context: str = "") -> str:
    diff = repo.pr_diff(pr["number"])
    if len(diff) > 60_000:
        diff = diff[:60_000] + "\n... (truncated; use tools to read files)"
    user = (
        f"# PR #{pr['number']}: {pr.get('title')}\n{(pr.get('body') or '')[:2000]}\n\n"
        f"# Diff\n```diff\n{diff}\n```\n\n"
        + (f"# Thread context\n{thread_context}\n\n" if thread_context else "")
        + f"# User question\n{question}"
    )
    return llm.chat_with_tools(
        [{"role": "system", "content": CHAT_SYSTEM}, {"role": "user", "content": user}],
        TOOLS_SPEC,
        make_tool_handler(ctx),
        reasoning_effort="high",
    )
