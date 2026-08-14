"""Second-opinion mode: audit other review bots' comments on a PR for false positives.

`pikuscope audit --repo owner/name --pr N` fetches every inline review comment left by
other bots (CodeRabbit, Greptile, Cursor, Macroscope, ...), adversarially verifies each
claim against the code at the PR head, and reports verdicts. With --post, replies to
suspected false positives explaining the disproof.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .context import RepoContext
from .gh import Repo
from .llm import LLMClient, extract_json
from .review import TOOLS_SPEC, make_tool_handler

KNOWN_BOTS = {
    "coderabbitai[bot]", "greptile-apps[bot]", "cursor[bot]", "macroscopeapp[bot]",
    "chatgpt-codex-connector[bot]", "sourcery-ai[bot]", "codacy-production[bot]",
}

AUDIT_SYSTEM = """You are pikuscope's second-opinion auditor. Another AI review bot left a \
comment on this pull request. Your job: determine whether the bot's claim is actually TRUE, \
by investigating the code at the PR head commit with your tools. Review bots are frequently \
wrong: they misread guards, miss handling elsewhere, flag intentional behavior, or invent \
failure paths that cannot occur.

Verdicts:
- "valid": the bot's claim is correct and worth acting on.
- "valid_minor": technically correct but overstated/low-impact.
- "false_positive": the claim is wrong — prove it with specific code evidence (quote the guard, \
the type, the caller, or the intended behavior that disproves it).
- "unverifiable": cannot be decided from the code (needs runtime/CI data).

Output ONLY JSON:
{"verdict": str, "confidence": 0.0-1.0, "explanation": "2-4 sentences with concrete code evidence"}
"""


def audit_bot_comments(llm: LLMClient, ctx: RepoContext, repo: Repo, pr: dict[str, Any],
                       bots: set[str] | None = None, workers: int = 4) -> list[dict[str, Any]]:
    bots = bots or KNOWN_BOTS
    comments = [
        c for c in repo.pr_review_comments(pr["number"])
        if c["user"]["login"] in bots and not c.get("in_reply_to_id")
    ]
    handler = make_tool_handler(ctx)

    def one(c: dict) -> dict[str, Any]:
        user = (
            f"# PR #{pr['number']}: {pr.get('title')}\n\n"
            f"# Bot comment by {c['user']['login']} on {c.get('path')}:{c.get('line')}\n"
            f"Diff hunk:\n{(c.get('diff_hunk') or '')[-1500:]}\n\n"
            f"Claim:\n{(c.get('body') or '')[:3000]}\n\n"
            "Investigate with tools, then output your JSON verdict."
        )
        try:
            text = llm.chat_with_tools(
                [{"role": "system", "content": AUDIT_SYSTEM}, {"role": "user", "content": user}],
                TOOLS_SPEC, handler, max_rounds=8,
            )
            verdict = extract_json(text)
        except Exception as e:  # noqa: BLE001
            verdict = {"verdict": "unverifiable", "confidence": 0, "explanation": f"audit error: {e}"}
        return {
            "comment_id": c["id"], "bot": c["user"]["login"], "path": c.get("path"),
            "line": c.get("line"), "claim": (c.get("body") or "")[:300], **verdict,
        }

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, comments))


def render_audit_reply(audit: dict[str, Any]) -> str:
    return (
        f"🔍 **Second opinion from pikuscope: this looks like a false positive** "
        f"(confidence {audit.get('confidence', 0):.0%})\n\n{audit.get('explanation', '')}\n\n"
        "<sub>🔬 pikuscope bot-comment audit</sub>"
    )
