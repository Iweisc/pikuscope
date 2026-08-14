# pikuscope

An AI pull-request review bot with feature parity with CodeRabbit and Greptile, powered by any
OpenAI-compatible model endpoint (default: `gpt-5.6-sol` at `xhigh` reasoning effort).

pikuscope reviews PRs the way a staff engineer does: it explores the repository with tools
(read/search/list at the PR head commit), drafts candidate findings, then **adversarially
verifies every finding before posting** — so what survives is real.

## Quick start

```bash
pip install -e .
cp .env.example .env   # set PIKUSCOPE_BASE_URL / PIKUSCOPE_API_KEY / PIKUSCOPE_MODEL

# one-shot CLI review (dry run prints markdown; --post posts to the PR)
pikuscope review --repo owner/name --pr 123 --clone-dir /tmp/clones --post

# webhook server (GitHub App / repo webhook mode)
pikuscope serve --port 8080 --secret $WEBHOOK_SECRET

# ask anything about a PR
pikuscope ask --repo owner/name --pr 123 "why was the cache key changed?"
```

GitHub Action mode: copy `examples/github-action.yml` into `.github/workflows/`.
Repo configuration: copy `examples/pikuscope.example.yaml` to `.pikuscope.yaml`.

## Feature parity matrix

| Feature | CodeRabbit | Greptile | pikuscope |
|---|---|---|---|
| PR summary comment | ✅ | ✅ | ✅ `Summary by pikuscope` |
| File-by-file walkthrough table | ✅ | ✅ | ✅ |
| Sequence diagrams (mermaid) | ✅ | ➖ | ✅ |
| Line-level review comments | ✅ | ✅ | ✅ |
| Severity / category tagging | ✅ | ✅ | ✅ critical/major/minor/nit × 14 categories |
| Committable suggestion blocks | ✅ | ✅ | ✅ ```suggestion``` blocks |
| Codebase-aware context (beyond the diff) | ✅ | ✅ (graph) | ✅ agentic tools: read_file / search_code / list_files at head SHA |
| Cross-file & missed-call-site detection | ✅ | ✅ | ✅ finder is prompted + tooled for it |
| Anti-false-positive verification pass | ➖ | ➖ | ✅ adversarial verifier refutes weak findings before posting |
| Incremental review on new commits | ✅ | ✅ | ✅ reviews only the delta, tracks last-reviewed SHA |
| Review status / skipped-files reporting | ✅ | ➖ | ✅ |
| Chat: free-form Q&A on the PR | ✅ | ✅ | ✅ `@pikuscope <question>` (tool-using) |
| Commands: `review`, `full review`, `summary` | ✅ | ✅ | ✅ |
| Commands: `pause` / `resume` / `ignore` | ✅ | ➖ | ✅ |
| `resolve` command | ✅ | ➖ | ✅ |
| Learnings / memory (`remember ...`) | ✅ | ✅ | ✅ path-scoped JSONL store, injected into future reviews |
| Docstring generation | ✅ | ➖ | ✅ `@pikuscope generate docstrings` |
| PR title & description generation | ✅ | ✅ | ✅ |
| Label suggestions | ✅ | ✅ | ✅ |
| Review effort estimation | ✅ | ➖ | ✅ |
| Configurable review profile (chill/assertive) | ✅ | strictness | ✅ |
| Path filters | ✅ | ✅ | ✅ gitignore-style, sane defaults for lockfiles/generated |
| Path-specific instructions | ✅ | ✅ | ✅ |
| Custom tone instructions | ✅ | ➖ | ✅ |
| YAML repo config | ✅ `.coderabbit.yaml` | ✅ `greptile.json` | ✅ `.pikuscope.yaml` |
| GitHub App / webhook mode | ✅ | ✅ | ✅ `pikuscope serve` |
| GitHub Action mode | ➖ | ➖ | ✅ |
| CLI mode | ✅ | ➖ | ✅ |
| Draft-PR skipping | ✅ | ✅ | ✅ |
| Fail-CI-on-severity gate | ➖ | ✅ | ✅ `fail_on: [critical]` |
| Repo guideline ingestion (CLAUDE.md, .cursorrules, AGENTS.md, …) | ✅ | ✅ | ✅ auto-scanned into review context |
| Auto-review gating (title keywords, drafts, base branches, authors) | ✅ | ✅ | ✅ `reviews.auto_review.*` |
| REQUEST_CHANGES workflow | ✅ | ➖ | ✅ `request_changes_workflow` |
| Commit status reporting | ✅ | ➖ | ✅ `commit_status` |
| "Prompt for AI agents" blocks on findings | ✅ | ✅ (IDE handoff) | ✅ per-finding collapsible prompt |
| Unit test generation | ✅ | ✅ (TREX) | ✅ `@pikuscope generate unit tests` |
| Autofix (commit suggestions to the branch) | ✅ | ➖ | ✅ `@pikuscope autofix` |
| AI-slop detection | ✅ | ➖ | ✅ summary warning |
| Duplicate-root-cause merging (editor stage) | ➖ | ➖ | ✅ |
| Poem 🐇 | ✅ | ➖ | ✅ (opt-in, off by default) |

Platform-level items (SaaS dashboards, Slack/Discord agents, IDE extensions, Jira/Linear,
hosted security scanning, sandboxed test execution) are out of scope for this repo; the
review-bot surface above is the parity target.

## Benchmark: t3code re-review

`bench/` contains a harness that measures pikuscope against the review bots active on
[pingdotgg/t3code](https://github.com/pingdotgg/t3code) (CodeRabbit, Greptile, Cursor Bugbot,
Macroscope) on already-merged PRs:

1. **collect** — extracts every inline bot finding + the developer interactions that followed,
   and labels each finding's reception (`valid_fixed` / `false_positive` / ...) from those
   interactions.
2. **run** — re-reviews each PR at the exact commit the bot reviewed, with zero visibility into
   the bots' comments.
3. **score** — an LLM judge matches pikuscope findings to bot findings by root cause and reports:
   - **recall** of bot findings (overall / per-bot / valid-only),
   - **FP avoidance**: bot findings the team rejected that pikuscope did not repeat,
   - **novel findings**: verified issues pikuscope found that every bot missed.

```bash
python bench/collect.py            # build dataset from GitHub
python bench/collect.py --label-fps
python bench/run.py --run-name baseline
python bench/score.py --run-name baseline
```

## Architecture

```
pikuscope/
  llm.py        OpenAI-compatible client: retries, JSON mode, agentic tool loop
  gh.py         GitHub REST client with on-disk caching
  diff.py       unified diff parser with new/old line annotation
  context.py    repo context: blobless clone + worktree per head SHA, ripgrep search
  config.py     .pikuscope.yaml (profiles, path filters, path instructions)
  review.py     plan → find (tool-using) → adversarial verify → results
  render.py     CodeRabbit-style markdown rendering
  commands.py   @pikuscope chat commands
  learnings.py  path-scoped learnings store
  docstrings.py docstring generation
  app.py        webhook server: auto/incremental reviews, chat handling
  cli.py        pikuscope review|serve|ask|docstrings
```
