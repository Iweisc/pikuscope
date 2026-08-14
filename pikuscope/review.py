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
    lens: str = ""
    verify_verdict: str = ""  # confirmed | refuted | downgraded | duplicate
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


FINDER_SYSTEM = """You are pikuscope, a principal-level code reviewer hunting for problems in a \
pull request. You are the RECALL stage of a two-stage pipeline: enumerate every defensible \
issue; a separate adversarial verifier will filter false positives. A real issue you fail to \
surface here is lost forever — when in doubt, REPORT it as a candidate.

## What to hunt (lens for this pass)
%LENS%

## Method — investigate before you claim
You have tools over the repository at the PR's head commit. Use them:
- Read the FULL modified function/component, not just the hunk, so you see guards and cleanup.
- Find callers/usages of changed symbols to check contracts (`search_code`).
- Check sibling implementations and platform variants (ios/android/web copies) for behavior the \
change may break or forget to update.
- Verify claimed helpers/types actually behave as you assume (read their definitions).
Also hunt for what the diff does NOT change but should have: other call sites needing the same \
fix, duplicated logic copies, related config.

## Report
- Issues INTRODUCED or WORSENED by this change (or the change fails its own stated purpose).
- Minor-but-real defects count: senior reviewers DO flag needless effect re-subscription, \
overbroad string matching, missing cleanup, silently swallowed errors, subtle navigation/history \
misbehavior. Report them with severity "minor".
- Pre-existing defects ONLY if the diff touches those exact lines (mark severity honestly).

## Do NOT report
- Formatting, import order, naming taste, comment wording.
- Things the compiler/typechecker trivially rejects.
- "Add tests/docs" boilerplate.
- Vague "could be undefined" without a concrete path.

## Line anchoring
`path`, `start_line`, `end_line` must reference NEW-file line numbers visible in the annotated \
diff (added `+` or context lines). Anchor to the smallest range that shows the defect. If you \
provide `suggestion`, it must be a complete replacement for exactly lines start_line..end_line \
(matching indentation, no fences) so it can be committed as-is.

## Output
After investigating, output ONLY a JSON object:
{"findings": [{
  "path": str,
  "start_line": int, "end_line": int,
  "severity": "critical" | "major" | "minor" | "nit",
  "category": one of %CATS%,
  "title": short imperative summary (<= 80 chars),
  "body": markdown; the defect, the evidence (quote the exact code), the concrete failure scenario, and the fix. Concise but complete.,
  "suggestion": str | null,
  "confidence": 0.0-1.0 (probability this is a real, introduced defect worth fixing),
  "failure_scenario": one sentence: concrete inputs/state -> wrong outcome
}]}
Return {"findings": []} only if you truly found nothing under this lens. \
Severity calibration: critical = data loss/security/crash on common path; major = incorrect \
behavior a user will hit; minor = real but edge-case, low-impact, or hygiene defect a senior \
reviewer would still flag; nit = polish.
""".replace("%CATS%", json.dumps(CATEGORIES))

LENSES = [
    (
        "state-lifecycle",
        """State, lifecycle, and concurrency defects:
- React/UI: wrong or missing dependency arrays, stale closures, effects that re-subscribe or \
re-run needlessly (listener/subscription churn), missing cleanup on unmount, setState-after-\
unmount, refs vs state misuse, memoization broken by unstable identities, event handlers \
capturing stale values.
- Async: race conditions, unawaited promises, unhandled rejections, out-of-order responses, \
missing AbortController/cancellation, double-fire on rapid input.
- Resources: leaks of listeners/timers/sockets/files/observers; cleanup paths that skip cases.
- Platform lifecycle: iOS/Android/desktop/web divergence in mount, focus, background, resume.""",
    ),
    (
        "logic-edges",
        """Logic and edge-case defects:
- String/path/URL matching that over- or under-matches (e.g. prefix checks like startsWith that \
match unintended siblings, missing boundary checks, case sensitivity, locale issues).
- Off-by-one, wrong comparison operators, inverted conditions, unreachable/dead branches, \
switch fallthrough, wrong precedence.
- Null/undefined/empty/NaN paths that concretely occur; default values that mask errors.
- Numeric: overflow, rounding, units, clamping, negative values, division by zero.
- Regex correctness; escaping (HTML, shell, SQL, markdown); encoding; timezone/date math.
- API contract misuse: wrong argument order, misunderstood return values, error codes ignored, \
misuse of library semantics (verify with search/read).
- Cross-file consistency: callers not updated, duplicated logic diverging, exhaustiveness of \
switches over enums/unions after adding a variant.""",
    ),
    (
        "behavior-ux",
        """User-visible behavior, data, and security defects:
- Navigation/history semantics (push vs replace, back-button traps, deep links, nested routes).
- Focus, scroll, selection, keyboard, IME handling; loading/error/empty states; race between \
user input and async updates; optimistic updates that desync.
- Persistence: data loss on reload/migration, cache invalidation misses, stale reads, \
localStorage/DB schema drift.
- Error handling users can hit: silent failures reported as success, missing user feedback, \
retries that duplicate side effects.
- Security: injection, XSS, path traversal, secrets exposure, permissive CORS/auth checks, \
unsafe HTML/markdown rendering.
- Performance on hot paths: N+1 calls, quadratic loops over unbounded data, sync work on the \
UI thread, unnecessary re-renders of large trees.
- Accessibility regressions in changed UI: focus traps, missing labels/roles, hover-only \
affordances unusable on touch devices.""",
    ),
]


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


VERIFIER_SYSTEM = """You are an adversarial code-review verifier — the PRECISION stage. \
Candidates come from recall-oriented finder passes and WILL contain false positives and \
duplicates. Your job: only real, introduced defects survive. You have tools over the repo at \
the PR head commit.

For EACH candidate finding, investigate the actual code (read the full function, check callers, \
check guards elsewhere, check whether the issue is pre-existing rather than introduced by this \
diff) and decide:
- "confirmed": the defect is real, introduced/worsened by this diff, and a competent reviewer \
would post it. Minor-but-real hygiene defects (needless resubscription, overbroad matching, \
missing cleanup, swallowed errors) ARE confirmable at severity minor. Check the anchor lines \
and suggestion: if the suggestion is wrong or would not compile, null it via revised_suggestion_invalid.
- "downgraded": real but overstated -> give revised severity/confidence.
- "refuted": not a real defect: the code is actually correct, the scenario cannot occur (prove \
it from code you read), it is guarded elsewhere, it is purely stylistic taste, or it is \
pre-existing and untouched by this diff.
- "duplicate": same root cause as a lower-indexed candidate; keep the best one confirmed and \
mark the rest duplicate.

Be strict about speculation: a finding whose failure scenario you cannot concretely trace \
through the code is refuted. But do NOT refute real minor defects merely for being minor.

Output ONLY JSON:
{"verdicts": [{"index": int, "verdict": "confirmed"|"downgraded"|"refuted"|"duplicate", \
"revised_severity": str|null, "revised_confidence": 0.0-1.0, "revised_suggestion_invalid": bool, \
"reason": "one-paragraph disproof or confirmation with evidence"}]}
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

        def run_batch(job: tuple[list[FileDiff], tuple[str, str]]) -> list[Finding]:
            batch, (lens_name, lens_text) = job
            profile_note = (
                "Profile: assertive — also surface maintainability/consistency candidates."
                if self.cfg.profile == "assertive"
                else "Profile: chill — surface real defects of any severity; skip pure style."
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
            system = FINDER_SYSTEM.replace("%LENS%", lens_text)
            try:
                text = self.llm.chat_with_tools(
                    [{"role": "system", "content": system}, {"role": "user", "content": user}],
                    TOOLS_SPEC,
                    handler,
                )
                data = extract_json(text)
            except Exception:  # noqa: BLE001 — a lens failing shouldn't kill the review
                return []
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
                            lens=lens_name,
                        )
                    )
                except (TypeError, ValueError):
                    continue
            return out

        jobs = [(batch, lens) for batch in batches for lens in LENSES]
        findings: list[Finding] = []
        with ThreadPoolExecutor(max_workers=min(6, len(jobs)) or 1) as pool:
            for batch_result in pool.map(run_batch, jobs):
                findings.extend(batch_result)
        return findings

    def _verify(self, pr: dict[str, Any], fds: list[FileDiff],
                candidates: list[Finding]) -> tuple[list[Finding], list[Finding]]:
        if not candidates:
            return [], []
        handler = make_tool_handler(self.ctx)
        by_path = {f.path: f for f in fds}
        # Group candidates by path so duplicates land in the same verifier call.
        candidates = sorted(candidates, key=lambda f: (f.path, f.start_line))
        chunks: list[list[tuple[int, Finding]]] = []
        cur: list[tuple[int, Finding]] = []
        cur_paths: set[str] = set()
        for i, f in enumerate(candidates):
            if cur and (len(cur) >= 14 or (f.path not in cur_paths and len(cur) >= 8)):
                chunks.append(cur)
                cur, cur_paths = [], set()
            cur.append((i, f))
            cur_paths.add(f.path)
        if cur:
            chunks.append(cur)

        def verify_chunk(chunk: list[tuple[int, Finding]]) -> dict[int, dict]:
            blocks = []
            for i, f in chunk:
                blocks.append(
                    f"### Finding {i}\npath: {f.path}\nlines: {f.start_line}-{f.end_line}\n"
                    f"severity: {f.severity} | category: {f.category} | confidence: {f.confidence}\n"
                    f"title: {f.title}\nfailure_scenario: {f.failure_scenario}\nbody:\n{f.body}\n"
                    f"suggestion:\n{f.suggestion or '(none)'}"
                )
            diff_blocks = []
            for path in sorted({f.path for _, f in chunk}):
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
                return {}
            return {int(v.get("index", -1)): v for v in data.get("verdicts", [])}

        verdicts: dict[int, dict] = {}
        with ThreadPoolExecutor(max_workers=min(4, len(chunks)) or 1) as pool:
            for chunk_verdicts in pool.map(verify_chunk, chunks):
                verdicts.update(chunk_verdicts)

        confirmed: list[Finding] = []
        dropped: list[Finding] = []
        for i, f in enumerate(candidates):
            v = verdicts.get(i)
            if v is None:
                confirmed.append(f)
                continue
            f.verify_verdict = str(v.get("verdict", ""))
            f.verify_reason = str(v.get("reason", ""))
            if f.verify_verdict in ("refuted", "duplicate"):
                dropped.append(f)
                continue
            if f.verify_verdict == "downgraded":
                if v.get("revised_severity") in SEVERITIES:
                    f.severity = v["revised_severity"]
            if v.get("revised_suggestion_invalid"):
                f.suggestion = None
            rc = v.get("revised_confidence")
            if isinstance(rc, (int, float)):
                f.confidence = float(rc)
            confirmed.append(f)
        return confirmed, dropped
