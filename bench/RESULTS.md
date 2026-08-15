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
