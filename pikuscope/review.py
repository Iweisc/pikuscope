"""The review engine: plan → find → verify → render-ready result."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict
from typing import Any

from .config import Config
from .context import RepoContext, file_tree_summary
from .diff import FileDiff, parse_unified_diff
from .llm import LLMClient, extract_json

SEVERITIES = ["critical", "major", "minor", "nit"]
CATEGORIES = [
    "bug", "security", "performance", "correctness", "data-loss", "race-condition",
    "error-handling", "maintainability", "style", "docs", "tests", "a11y", "i18n", "typo",
]


@dataclass
class Finding:
    path: str
    start_line: int
    end_line: int
    severity: str
    category: str
    title: str
    body: str
    suggestion: str | None = None
    confidence: float = 0.7
    failure_scenario: str = ""
    verify_verdict: str = ""  # confirmed | refuted | downgraded
    verify_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReviewResult:
    summary: str = ""
    walkthrough: list[dict[str, str]] = field(default_factory=list)
    diagram: str = ""
    poem: str = ""
    findings: list[Finding] = field(default_factory=list)
    dropped: list[Finding] = field(default_factory=list)
    suggested_title: str = ""
    suggested_description: str = ""
    suggested_labels: list[str] = field(default_factory=list)
    effort_estimate: str = ""
    skipped_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


TOOLS_SPEC = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the repository at the PR head commit. Returns numbered lines.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "repo-relative file path"},
                    "start_line": {"type": "integer", "description": "1-based, optional"},
                    "end_line": {"type": "integer", "description": "inclusive, optional"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "Regex search across the repository (ripgrep syntax). Use to find definitions, callers, and usages of symbols.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "regex pattern"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List repository file paths, optionally under a directory prefix.",
            "parameters": {
                "type": "object",
                "properties": {"prefix": {"type": "string", "description": "path prefix, optional"}},
            },
        },
    },
]


def make_tool_handler(ctx: RepoContext):
    def handler(name: str, args: dict[str, Any]) -> str:
        if name == "read_file":
            content = ctx.file_content(args.get("path", ""))
            if content is None:
                return f"file not found or unreadable: {args.get('path')}"
            lines = content.splitlines()
            start = max(1, int(args.get("start_line") or 1))
            end = min(len(lines), int(args.get("end_line") or len(lines)))
            if end - start > 600:
                end = start + 600
            body = "\n".join(f"{i:>5}\t{lines[i - 1]}" for i in range(start, end + 1))
            return f"{args.get('path')} lines {start}-{end} of {len(lines)}:\n{body}"
        if name == "search_code":
            hits = ctx.search(args.get("pattern", ""), max_results=40)
            if not hits:
                return "no matches"
            return "\n".join(f"{p}:{ln}: {text[:200]}" for p, ln, text in hits)
        if name == "list_files":
            files = ctx.list_files(args.get("prefix", ""))
            if len(files) > 300:
                return "\n".join(files[:300]) + f"\n... ({len(files) - 300} more)"
            return "\n".join(files) if files else "no files"
        return f"unknown tool {name}"

    return handler


FINDER_SYSTEM = """You are pikuscope, a principal-level code reviewer. You review pull requests \
with the judgment of a staff engineer who knows this codebase: precise, evidence-driven, and \
allergic to false positives.

## Objective
Find REAL problems INTRODUCED BY THIS CHANGE. A real problem is one where you can describe a \
concrete scenario in which the code misbehaves: wrong runtime behavior, broken edge case, \
security hole, race condition, leak, perf regression on a hot path, breaking an API contract \
that existing callers rely on, state-management bugs, incorrect cleanup, off-by-one, \
wrong dependency arrays / stale closures (React), unhandled promise rejections, incorrect \
platform-specific behavior, data loss, a11y regressions in UI code.

## Method — investigate before you claim
You have tools over the repository at the PR's head commit. Before reporting a finding, use them to:
- Read the FULL modified function/component, not just the hunk, so you see guards and cleanup.
- Find callers/usages of changed functions to check contracts (`search_code`).
- Check sibling implementations for conventions the change may violate.
- Verify the claimed symbol/type/helper actually behaves as you assume (read its definition).
Also actively look for what the diff does NOT change but should have: other call sites needing \
the same fix, copies of duplicated logic, related platform variants (e.g. ios/android/web \
versions of the same file).

## Do NOT report
- Pre-existing issues the diff doesn't touch or make worse (unless the PR's purpose is to fix exactly that and fails to).
- Style/formatting/naming taste, import ordering, comment wording.
- Anything the project's compiler, typechecker, or linter obviously catches.
- Speculative "could be undefined" claims without a real path where it is.
- "Consider adding tests/docs" boilerplate.
- Refactor suggestions that don't fix a defect (unless profile is assertive, then max 2, marked minor/nit).
- Duplicates: one finding per root cause; mention other affected lines inside that finding.

## Line anchoring
`path`, `start_line`, `end_line` must reference NEW-file line numbers visible in the annotated \
diff (added `+` or context lines). Anchor to the smallest range that shows the defect. \
If you provide `suggestion`, it must be the complete replacement for exactly lines \
start_line..end_line (same indentation style, no markdown fences) so it can be committed as-is.

## Output
After your investigation, output ONLY a JSON object:
{"findings": [{
  "path": str,
  "start_line": int, "end_line": int,
  "severity": "critical" | "major" | "minor" | "nit",
  "category": one of %s,
  "title": short imperative summary (<= 80 chars),
  "body": markdown; the defect, the evidence (quote the exact code), the concrete failure scenario, and the fix. Concise but complete.,
  "suggestion": str | null,
  "confidence": 0.0-1.0 (probability a staff engineer would agree this is a real, introduced defect worth fixing),
  "failure_scenario": one sentence: concrete inputs/state -> wrong outcome
}]}
Return {"findings": []} if the change is clean. An empty review of a clean PR is a GOOD review. \
Severity calibration: critical = data loss/security/crash on common path; major = incorrect \
behavior a user will hit; minor = real but edge-case or low-impact defect; nit = polish.
""" % (json.dumps(CATEGORIES))


SUMMARY_SYSTEM = """You are pikuscope, an AI code review assistant. Produce PR-level artifacts \
from the diff. Be accurate and specific to THIS change; never pad or speculate.

Output ONLY JSON:
{
  "summary": "2-6 bullet markdown summary of WHAT changed and WHY (from the diff/description), grouped by New Features / Bug Fixes / Refactor / Chores as applicable",
  "walkthrough": [{"files": "comma-separated file paths (group related files)", "summary": "1-2 sentence change summary"}],
  "sequence_diagram": "mermaid sequenceDiagram source (no fences) if the change involves a multi-step interaction between components, else \\"\\"",
  "suggested_title": "conventional-commit style title accurately describing the change",
  "suggested_description": "markdown PR description: what/why/how, testing notes",
  "suggested_labels": ["bug"|"enhancement"|"refactor"|"docs"|"chore"|"performance"|"security"|...],
  "effort_estimate": "review effort 1-5 with one-line justification, e.g. '2 (small, focused change in one file)'",
  "poem": ""
}
"""


VERIFIER_SYSTEM = """You are an adversarial code-review verifier. Your job is to REFUTE weak \
review findings so only real, introduced defects survive. You have tools over the repo at the \
PR head commit.

For EACH candidate finding, investigate the actual code (read the full function, check callers, \
check guards elsewhere, check whether the issue is pre-existing rather than introduced by this \
diff) and decide:
- "confirmed": the defect is real, introduced/worsened by this diff, and worth a comment. \
Check the anchor lines and suggestion: if the suggestion is wrong or would not compile, fix it or null it.
- "downgraded": real but overstated -> give revised severity/confidence.
- "refuted": not a real defect (code is actually correct, guarded elsewhere, pre-existing, \
purely stylistic, or the scenario cannot occur). Explain the disproof.

Be strict: a finding that merely "could be worth considering" is refuted. A finding whose \
failure scenario you cannot concretely reproduce from the code is refuted.

Output ONLY JSON:
{"verdicts": [{"index": int, "verdict": "confirmed"|"downgraded"|"refuted", \
"revised_severity": str|null, "revised_confidence": 0.0-1.0, "reason": "one-paragraph disproof or confirmation with evidence"}]}
"""


class Reviewer:
    def __init__(self, llm: LLMClient, ctx: RepoContext, cfg: Config | None = None):
        self.llm = llm
        self.ctx = ctx
        self.cfg = cfg or Config()

    # ---------- prompt building ----------

    def _pr_header(self, pr: dict[str, Any]) -> str:
        title = pr.get("title", "")
        body = (pr.get("body") or "").strip()
        if len(body) > 4000:
            body = body[:4000] + "\n... (truncated)"
        base = pr.get("base", {}).get("ref", "main")
        return f"# Pull request\nTitle: {title}\nTarget branch: {base}\n\nDescription:\n{body or '(none)'}"

    def _file_block(self, fd: FileDiff, full_content: bool = True) -> str:
        parts = [f"## File: {fd.path} ({fd.status}, +{fd.additions}/-{fd.deletions})"]
        extra = self.cfg.instructions_for(fd.path)
        if extra:
            parts.append("Path-specific review instructions: " + " | ".join(extra))
        parts.append("### Annotated diff (new-side line numbers on + and context lines)")
        parts.append(fd.annotated())
        if full_content and fd.status != "removed":
            content = self.ctx.file_content(fd.path)
            if content is not None and len(content) < 48_000:
                lines = content.splitlines()
                numbered = "\n".join(f"{i + 1:>5}\t{l}" for i, l in enumerate(lines))
                parts.append(f"### Full file content at head ({len(lines)} lines)")
                parts.append(numbered)
            elif content is not None:
                parts.append("### Full file too large; use read_file tool for specific ranges")
        return "\n".join(parts)

    def _batch_files(self, fds: list[FileDiff], budget: int = 90_000) -> list[list[FileDiff]]:
        batches: list[list[FileDiff]] = []
        cur: list[FileDiff] = []
        size = 0
        for fd in sorted(fds, key=lambda f: f.path):
            est = len(fd.annotated()) + 4000
            content = None if fd.status == "removed" else self.ctx.file_content(fd.path)
            if content is not None and len(content) < 48_000:
                est += len(content)
            if cur and size + est > budget:
                batches.append(cur)
                cur, size = [], 0
            cur.append(fd)
            size += est
        if cur:
            batches.append(cur)
        return batches

    # ---------- stages ----------

    def review(self, pr: dict[str, Any], diff_text: str,
               learnings: list[str] | None = None,
               progress: Any = None) -> ReviewResult:
        def note(msg: str) -> None:
            if progress:
                progress(msg)

        result = ReviewResult()
        fds = parse_unified_diff(diff_text)
        reviewable = [f for f in fds if self.cfg.is_reviewable(f.path) and not f.is_binary]
        result.skipped_files = [f.path for f in fds if f not in reviewable]

        with ThreadPoolExecutor(max_workers=4) as pool:
            fut_summary = pool.submit(self._summarize, pr, fds)
            note(f"finding pass over {len(reviewable)} files")
            candidates = self._find(pr, reviewable, learnings or [])
            note(f"{len(candidates)} candidate findings; verifying")
            confirmed, dropped = self._verify(pr, reviewable, candidates)
            summary = fut_summary.result()

        result.summary = summary.get("summary", "")
        result.walkthrough = summary.get("walkthrough", []) or []
        result.diagram = summary.get("sequence_diagram", "") or ""
        result.suggested_title = summary.get("suggested_title", "")
        result.suggested_description = summary.get("suggested_description", "")
        result.suggested_labels = summary.get("suggested_labels", []) or []
        result.effort_estimate = summary.get("effort_estimate", "")
        result.poem = summary.get("poem", "") if self.cfg.poem else ""

        # thresholds + anchoring validation
        by_path = {f.path: f for f in reviewable}
        final: list[Finding] = []
        for f in confirmed:
            if f.confidence < self.cfg.confidence_threshold:
                f.verify_verdict = "refuted"
                f.verify_reason = f.verify_reason or "below confidence threshold"
                dropped.append(f)
                continue
            fd = by_path.get(f.path)
            if fd is not None:
                valid = fd.new_line_numbers()
                if valid and f.start_line not in valid and f.end_line not in valid:
                    # Snap to nearest commentable line rather than dropping.
                    nearest = min(valid, key=lambda x: abs(x - f.start_line))
                    f.start_line = f.end_line = nearest
                    f.suggestion = None
            final.append(f)
        order = {s: i for i, s in enumerate(SEVERITIES)}
        final.sort(key=lambda f: (order.get(f.severity, 9), -f.confidence))
        result.findings = final[: self.cfg.max_findings]
        result.dropped = dropped
        return result

    def _summarize(self, pr: dict[str, Any], fds: list[FileDiff]) -> dict[str, Any]:
        diff_parts = []
        budget = 120_000
        for fd in fds:
            block = f"## {fd.path} ({fd.status})\n{fd.annotated(max_chars=8000)}"
            if budget - len(block) < 0:
                diff_parts.append(f"## {fd.path} ({fd.status}) — omitted for length")
                continue
            budget -= len(block)
            diff_parts.append(block)
        user = f"{self._pr_header(pr)}\n\n# Diff\n" + "\n\n".join(diff_parts)
        try:
            return self.llm.chat_json(
                [{"role": "system", "content": SUMMARY_SYSTEM}, {"role": "user", "content": user}],
                reasoning_effort="medium",
            )
        except Exception:  # noqa: BLE001
            return {}

    def _find(self, pr: dict[str, Any], fds: list[FileDiff],
              learnings: list[str]) -> list[Finding]:
        batches = self._batch_files(fds)
        tree = file_tree_summary(self.ctx)
        handler = make_tool_handler(self.ctx)

        def run_batch(batch: list[FileDiff]) -> list[Finding]:
            profile_note = (
                "Profile: assertive — you may include up to 2 maintainability/style findings if clearly worthwhile."
                if self.cfg.profile == "assertive"
                else "Profile: chill — only report defects that matter; skip style entirely."
            )
            learn_note = (
                "\n# Reviewer learnings for this repository (apply them)\n" + "\n".join(f"- {l}" for l in learnings)
                if learnings
                else ""
            )
            other_files = [f.path for f in fds if f not in batch]
            other_note = (
                "\nOther files changed in this PR (reviewed separately, listed for context): "
                + ", ".join(other_files)
                if other_files
                else ""
            )
            user = (
                f"{self._pr_header(pr)}\n\n{profile_note}{learn_note}\n\n"
                f"# Repository layout\n{tree}\n{other_note}\n\n# Files to review\n\n"
                + "\n\n".join(self._file_block(fd) for fd in batch)
            )
            text = self.llm.chat_with_tools(
                [{"role": "system", "content": FINDER_SYSTEM}, {"role": "user", "content": user}],
                TOOLS_SPEC,
                handler,
            )
            data = extract_json(text)
            out = []
            for raw in data.get("findings", []):
                try:
                    out.append(
                        Finding(
                            path=str(raw.get("path", "")),
                            start_line=int(raw.get("start_line", 0) or 0),
                            end_line=int(raw.get("end_line", raw.get("start_line", 0)) or 0),
                            severity=str(raw.get("severity", "minor")),
                            category=str(raw.get("category", "bug")),
                            title=str(raw.get("title", ""))[:200],
                            body=str(raw.get("body", "")),
                            suggestion=raw.get("suggestion") or None,
                            confidence=float(raw.get("confidence", 0.7) or 0.7),
                            failure_scenario=str(raw.get("failure_scenario", "")),
                        )
                    )
                except (TypeError, ValueError):
                    continue
            return out

        findings: list[Finding] = []
        with ThreadPoolExecutor(max_workers=3) as pool:
            for batch_result in pool.map(run_batch, batches):
                findings.extend(batch_result)
        return findings

    def _verify(self, pr: dict[str, Any], fds: list[FileDiff],
                candidates: list[Finding]) -> tuple[list[Finding], list[Finding]]:
        if not candidates:
            return [], []
        handler = make_tool_handler(self.ctx)
        by_path = {f.path: f for f in fds}
        blocks = []
        for i, f in enumerate(candidates):
            blocks.append(
                f"### Finding {i}\npath: {f.path}\nlines: {f.start_line}-{f.end_line}\n"
                f"severity: {f.severity} | category: {f.category} | confidence: {f.confidence}\n"
                f"title: {f.title}\nfailure_scenario: {f.failure_scenario}\nbody:\n{f.body}\n"
                f"suggestion:\n{f.suggestion or '(none)'}"
            )
        diff_blocks = []
        for path in sorted({f.path for f in candidates}):
            fd = by_path.get(path)
            if fd:
                diff_blocks.append(f"## {path}\n{fd.annotated(max_chars=20_000)}")
        user = (
            f"{self._pr_header(pr)}\n\n# Diff of files with candidate findings\n"
            + "\n\n".join(diff_blocks)
            + "\n\n# Candidate findings to verify\n"
            + "\n\n".join(blocks)
        )
        try:
            text = self.llm.chat_with_tools(
                [{"role": "system", "content": VERIFIER_SYSTEM}, {"role": "user", "content": user}],
                TOOLS_SPEC,
                handler,
            )
            data = extract_json(text)
        except Exception:  # noqa: BLE001 — verification is best-effort; keep candidates
            return candidates, []
        verdicts = {int(v.get("index", -1)): v for v in data.get("verdicts", [])}
        confirmed: list[Finding] = []
        dropped: list[Finding] = []
        for i, f in enumerate(candidates):
            v = verdicts.get(i)
            if v is None:
                confirmed.append(f)
                continue
            f.verify_verdict = str(v.get("verdict", ""))
            f.verify_reason = str(v.get("reason", ""))
            if f.verify_verdict == "refuted":
                dropped.append(f)
                continue
            if f.verify_verdict == "downgraded":
                if v.get("revised_severity") in SEVERITIES:
                    f.severity = v["revised_severity"]
            rc = v.get("revised_confidence")
            if isinstance(rc, (int, float)):
                f.confidence = float(rc)
            confirmed.append(f)
        return confirmed, dropped
