"""Render review results to GitHub-flavored markdown (CodeRabbit-style layout)."""

from __future__ import annotations

from .review import Finding, ReviewResult

SEVERITY_BADGE = {
    "critical": "🛑 Critical",
    "major": "⚠️ Major",
    "minor": "🔧 Minor",
    "nit": "🧹 Nit",
}

MARKER = "<!-- pikuscope-summary -->"


def render_summary_comment(result: ReviewResult, incremental_note: str = "") -> str:
    parts = [MARKER]
    if incremental_note:
        parts.append(f"> {incremental_note}\n")
    if result.summary:
        parts.append("## Summary by pikuscope\n")
        parts.append(result.summary)
    if result.walkthrough:
        parts.append("\n<details>\n<summary>📝 Walkthrough</summary>\n")
        parts.append("| File(s) | Change summary |")
        parts.append("|---|---|")
        for row in result.walkthrough:
            files = str(row.get("files", "")).replace("|", "\\|")
            summary = str(row.get("summary", "")).replace("|", "\\|").replace("\n", " ")
            parts.append(f"| `{files}` | {summary} |")
        parts.append("\n</details>")
    if result.diagram:
        parts.append("\n<details>\n<summary>🔀 Sequence diagram</summary>\n")
        parts.append("```mermaid")
        parts.append(result.diagram.strip())
        parts.append("```")
        parts.append("\n</details>")
    if result.effort_estimate:
        parts.append(f"\n**Estimated review effort:** {result.effort_estimate}")
    if result.suggested_labels:
        parts.append("**Suggested labels:** " + ", ".join(f"`{l}`" for l in result.suggested_labels))
    if result.skipped_files:
        parts.append(
            "\n<details>\n<summary>⏭️ Files skipped (filters)</summary>\n\n"
            + "\n".join(f"- `{p}`" for p in result.skipped_files)
            + "\n</details>"
        )
    if result.poem:
        parts.append("\n<details>\n<summary>🐇 Poem</summary>\n\n" + result.poem + "\n</details>")
    parts.append(
        "\n---\n<sub>🔬 Review by **pikuscope** · comment `@pikuscope help` for commands</sub>"
    )
    return "\n".join(parts)


def render_finding_comment(f: Finding) -> str:
    badge = SEVERITY_BADGE.get(f.severity, f.severity)
    parts = [f"**{badge}** · `{f.category}` · **{f.title}**\n"]
    parts.append(f.body)
    if f.suggestion:
        parts.append("\n```suggestion")
        parts.append(f.suggestion.rstrip("\n"))
        parts.append("```")
    parts.append("\n<sub>🔬 pikuscope</sub>")
    return "\n".join(parts)


def render_report(result: ReviewResult, pr_ref: str = "") -> str:
    """Full markdown report for CLI/dry-run output."""
    parts = [f"# pikuscope review{' — ' + pr_ref if pr_ref else ''}\n"]
    parts.append(render_summary_comment(result))
    parts.append(f"\n## Findings ({len(result.findings)})\n")
    if not result.findings:
        parts.append("_No issues found — change looks good._")
    for f in result.findings:
        parts.append(f"### `{f.path}:{f.start_line}`" + (f"-{f.end_line}" if f.end_line != f.start_line else ""))
        parts.append(render_finding_comment(f))
        parts.append("")
    if result.dropped:
        parts.append(f"\n<details><summary>Dropped by verifier ({len(result.dropped)})</summary>\n")
        for f in result.dropped:
            parts.append(f"- `{f.path}:{f.start_line}` {f.title} — {f.verify_reason[:300]}")
        parts.append("</details>")
    return "\n".join(parts)
