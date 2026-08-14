# Benchmark methodology

Goal: measure whether pikuscope re-discovers, without any visibility into other bots'
comments, the same problems that CodeRabbit, Greptile, Cursor Bugbot, and Macroscope found
on merged PRs of [pingdotgg/t3code](https://github.com/pingdotgg/t3code) — and whether it
goes further (novel real findings) while avoiding the bots' false positives.

## Ground truth

`collect.py` paginates every inline PR review comment in the repo, keeps those authored by
review bots, groups them into threads, and stores per-PR entries in `data/dataset.jsonl`
(657 merged PRs, 5658 findings: cursor 2852, macroscope 2517, coderabbit 212, greptile 51,
codex 26).

Each finding's **reception** is labeled by an LLM from the developer interactions that
followed (replies in the thread, emoji reactions):

| label | meaning |
|---|---|
| `valid_fixed` | a human agreed/fixed it |
| `valid_acknowledged` | agreed but deferred |
| `false_positive` | a human disputed it (intentional / doesn't apply / wrong) |
| `unaddressed` | zero human engagement |
| `unclear` | engagement but ambiguous |

`enrich.py` additionally extracts the bot's own severity markers and flags CI-dependent
comments (formatter/pipeline piggybacks we cannot reproduce without CI logs; excluded from
scoring).

## Re-review protocol

For each benchmark PR, `run.py`:

1. checks out **the exact commit the bot reviewed** (`original_commit_id` of the earliest
   bot comment) in a worktree — reviewing the merge head would hide findings the author
   fixed in later commits;
2. computes the diff the bot saw (`merge-base(base, review_sha)..review_sha`);
3. runs the pikuscope engine (multi-lens finder + adversarial verifier, gpt-5.6-sol at
   xhigh) with **no access to any PR comments**;
4. stores findings + internally-dropped candidates.

## Scoring

`score.py` has an LLM judge decide, per ground finding, whether any pikuscope finding
identifies the same root cause (`exact` / `partial` / `none`). Metrics:

- **recall_overall / by bot / by reception** — matched ÷ ground findings;
  `recall_valid_only` (receptions `valid_fixed`+`valid_acknowledged`) is the headline
  parity number: those are the findings that demonstrably mattered to the team.
- **fp_avoid_rate** — of ground findings labeled `false_positive`, the fraction pikuscope
  did *not* repeat. `fp_explicitly_refuted` counts the stronger case where pikuscope
  generated the same claim as a candidate and its verifier struck it down with a reason.
- **novel findings** — pikuscope findings matching no ground finding. `verify_novel.py`
  sends each to an independent adversarial judge (with repo tools, at the same commit)
  that confirms or rejects it; `novel_precision` is the fraction confirmed real.

`analyze.py` diagnoses every missed valid finding (`found_but_dropped` / `near_miss` /
`not_generated` / `ground_truth_dubious`) to drive the improvement loop.

## Benchmark sets (`select.py`)

- **CRGR** (64 PRs): every PR with CodeRabbit or Greptile findings, ≤60 changed files.
- **AUX** (40 PRs): cursor/macroscope PRs ≤25 files, ranked by FP-label and valid-label
  density — the FP-catch statistics come mostly from here.

## Contamination control

Repo-specific learnings fed to the reviewer are mined **only from PRs outside both
benchmark sets** (553 remaining PRs). The reviewer never sees bot comments, PR outcomes,
or post-review commits for benchmarked PRs.
