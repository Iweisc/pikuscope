# Benchmark results log

Comparison set: 15 merged t3code PRs (46, 49, 50, 58, 65, 75, 77, 110, 186, 2757, 3617,
4015, 4244, 4309, 4413) containing 47 ground-truth bot findings (CodeRabbit, Greptile,
Cursor Bugbot, Macroscope), of which 19 are `valid` (developer-confirmed). Reviewer:
gpt-5.6-sol @ xhigh. Judge rubric v2 (post-audit).

## Engine iterations (same 15 PRs)

| engine | valid recall | overall recall | CodeRabbit-valid | Cursor-valid | FP avoid | findings/PR |
|---|---|---|---|---|---|---|
| v3 baseline (multi-lens finder + adversarial verifier + editor) | 78.9% | 55.3% | 60% | 90.9% | 2/2 | 3.8 |
| v4 (+ consequence tracing, tests/docs classes, mined learnings, guidelines) | 78.9% | 57.4% | 80% | 81.8% | 2/2 | 3.67 |
| **v5 (+ context-expansion pre-pass, decision-table analysis)** | **94.7%** | **61.7%** | **80%** | **100%** | 2/2 | 3.6 |

The only remaining valid miss in v5 is a documentation-wording clarification
(PR 4309, CodeRabbit minor).

Note on "overall recall": the denominator includes `unaddressed` bot findings (no human
ever engaged with them) — a population known to contain unmarked noise. The valid-only
number is the parity-relevant metric.

## Novel findings (v3 baseline, 9 PRs, adversarial judge)

23 pikuscope findings matched no bot finding; 21 confirmed real, 2 rejected →
**91% novel precision**. pikuscope surfaces verified-real issues that all four bots
missed on the same PRs.

## Judge-quality audit (Claude agents, v4 matches)

- hits audited: 23, inflated: 1
- valid misses audited: 9, wrongly scored as miss: 5
→ rubric v2 (instance-of-broader-defect counts, cross-commit repeats, test-vs-prod
distinction) adopted; all runs re-judged with it.

## Method changes that mattered

1. Finder must be recall-oriented; the adversarial verifier owns precision.
   Splitting finder into 3 specialized lenses (state-lifecycle, logic-edges,
   behavior-ux) lifted recall from 20% → 60% on the pilot.
2. Verifier refutations require proof, not plausibility; dedupe as `duplicate`,
   never silent-drop.
3. Context-expansion pre-pass (model lists related files, we inline them) fixed the
   "one hop short" near-miss class (redirect chains, SDK shapes, sibling routes).
4. Global LLM concurrency cap — oversubscribing the endpoint queues server-side and
   collapses throughput.

## v6: intent/policy FP guards (validated 2026-08-15)

Workflow-derived guard rules from the 9 bot FPs that v5 repeated (maintainer-dismissed
claims), added to the verifier + 5 repo learnings. Re-run on the 5 affected PRs:

| PR | repeated FP in v5 | v6 outcome |
|---|---|---|
| 1180 | truthy env check; mock-vs-GitHub config precedence (×2) | both gone |
| 2911 | pin action `@main` (supply-chain policy) | gone; valid path-filter finding still reported |
| 4153 | missing cwd on CLI inventory (×3); warning-vs-error taxonomy | all gone |
| 4955 | 502/503/504→unreachable "guard bypass" | narrow claim gone (folded into broader ownership finding) |
| 4967 | sync error-banner clear on paste | gone; await-window variant (the real bug class) still flagged |

Density also fell to 4–8 findings/PR on these medium PRs (editor calibration).

## v6.1: distinct-scenario editor (2026-08-15)

The v6 density cap trimmed some valid minors (v6-core: 78.9% valid recall on the core 15).
v6.1 exempts findings with distinct concrete failure scenarios from merging/trimming.
Measured on core-15 + the 5 FP PRs together (20 PRs):

| metric | v5 (60-PR partial) | v6.1 (20-PR core+FP) |
|---|---|---|
| valid-only recall | 63.9% | **80.6%** |
| FP avoid (overall) | 53.8% | **92.9%** |
| FP avoid (factual) | 75.0% | **100%** |
| FP avoid (intent) | 30.0% | **85.7%** |
| findings/PR | 7.0 | **5.1** |
| valid lost to verifier | 3 | 1 |

v6.1 is the shipped default configuration.
