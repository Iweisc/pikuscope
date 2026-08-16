"""Score a benchmark run: recall vs bot findings, FP catches, novel findings.

Matching is judged by the LLM: same root cause = match, not same wording.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pikuscope.llm import LLMClient  # noqa: E402

DATA = ROOT / "bench" / "data" / "dataset.jsonl"
RUNS = ROOT / "bench" / "runs"

VALID_LABELS = {"valid_fixed", "valid_acknowledged"}
FP_LABELS = {"false_positive"}

MATCH_SYSTEM = """You compare code-review findings from two reviewers on the SAME pull request \
and decide which ones identify the same underlying issue.

A MATCH means the same root cause: the fix a candidate demands would also resolve (or directly \
surface) the ground finding's defect. Apply these rules:
- Wording, severity, and exact line may differ; a match is about the defect, not the prose.
- If the ground finding is a CONCRETE INSTANCE of a broader defect a candidate describes \
(e.g. ground: "regex also matches serialized 500 bodies"; candidate: "non-404 errors are \
misclassified as missing sessions — only a confirmed 404 may trigger the fallback"), that is a \
match ("partial" at minimum): the candidate's fix eliminates the ground's failure mode.
- If SEVERAL candidates together describe the defect cluster and any one of them would drive \
the same fix, match the ground to the closest one.
- Ground findings from bots often repeat the same defect across fix-iteration commits; judge \
each against the candidates on its merits even if it anchors to a different commit's lines.
- Same file but genuinely different defect = no match. A candidate about test coverage does NOT \
match a ground finding about the production bug itself (and vice versa) — surfacing ≠ fixing \
counts only when the candidate also names the production defect.

For each GROUND finding, check every CANDIDATE finding (a match candidate list is provided).

Output ONLY JSON:
{"matches": [{"ground_id": <id>, "candidate_index": <int or null>, "match_strength": "exact"|"partial"|"none", "reason": "one sentence"}]}
- "exact": same defect, same fix.
- "partial": candidate covers the root cause incompletely, more broadly, or as an instance.
- "none": no candidate covers it (candidate_index null).
"""


def load_run(run_name: str) -> dict[int, dict]:
    out = {}
    for p in (RUNS / run_name).glob("pr-*.json"):
        d = json.loads(p.read_text())
        out[d["pr"]] = d
    return out


def match_pr(llm: LLMClient, entry: dict, run_result: dict) -> list[dict]:
    ground = entry["findings"]
    cands = run_result.get("findings", [])
    dropped = run_result.get("dropped", [])
    if not ground:
        return []
    g_blocks = []
    for f in ground:
        g_blocks.append(
            f"### GROUND id={f['id']} (bot={f['bot']}, kind={f.get('kind')})\n"
            f"file: {f['path']} line {f.get('start_line') or f.get('line')}-{f.get('line')}\n"
            f"hunk:\n{f.get('diff_hunk', '')[-800:]}\n"
            f"comment:\n{f['body'][:2000]}"
        )
    c_blocks = []
    for i, f in enumerate(cands):
        c_blocks.append(
            f"### CANDIDATE index={i} [REPORTED]\nfile: {f['path']} lines {f['start_line']}-{f['end_line']}\n"
            f"severity={f['severity']} category={f['category']}\ntitle: {f['title']}\n"
            f"body:\n{f['body'][:2000]}"
        )
    for j, f in enumerate(dropped):
        c_blocks.append(
            f"### CANDIDATE index={len(cands) + j} [DROPPED — generated but suppressed by verifier]\n"
            f"file: {f['path']} lines {f['start_line']}-{f['end_line']}\n"
            f"title: {f['title']}\nbody:\n{f['body'][:1200]}"
        )
    user = (
        f"# PR #{entry['pr']}: {entry['title']}\n\n# GROUND findings (reviewer A)\n"
        + "\n\n".join(g_blocks)
        + "\n\n# CANDIDATE findings (reviewer B; some marked DROPPED)\n"
        + ("\n\n".join(c_blocks) if c_blocks else "(reviewer B reported no findings)")
    )
    data = llm.chat_json(
        [{"role": "system", "content": MATCH_SYSTEM}, {"role": "user", "content": user}],
        reasoning_effort="high",
    )
    matches = data.get("matches", [])
    # annotate which candidate indexes were dropped so scoring can distinguish
    for m in matches:
        ci = m.get("candidate_index")
        m["candidate_dropped"] = ci is not None and int(ci) >= len(cands)
    return matches


def score(run_name: str, refresh: bool = False) -> None:
    llm = LLMClient.from_env()
    entries = {e["pr"]: e for e in (json.loads(l) for l in DATA.read_text().splitlines())}
    run = load_run(run_name)
    match_dir = RUNS / run_name / "matches"
    match_dir.mkdir(exist_ok=True)

    def one(pr: int):
        mp = match_dir / f"pr-{pr}.json"
        if mp.exists() and not refresh:
            return pr, json.loads(mp.read_text())
        entry = entries[pr]
        rr = run[pr]
        if rr.get("error"):
            return pr, None
        try:
            matches = match_pr(llm, entry, rr)
        except Exception as ex:  # noqa: BLE001
            print(f"match failed PR {pr}: {ex}", file=sys.stderr)
            return pr, None
        mp.write_text(json.dumps(matches, indent=2))
        return pr, matches

    prs = [pr for pr in run if pr in entries]
    results: dict[int, list[dict] | None] = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        for pr, matches in pool.map(one, prs):
            results[pr] = matches

    # aggregate
    stats = Counter()
    by_bot: dict[str, Counter] = defaultdict(Counter)
    by_bot_valid: dict[str, Counter] = defaultdict(Counter)
    by_label: dict[str, Counter] = defaultdict(Counter)
    by_era: dict[str, Counter] = defaultdict(Counter)  # early (<200) vs modern (>=200)
    fp_analysis = Counter()
    missed_examples = []
    total_piku_findings = 0
    matched_candidate_keys: set[tuple[int, int]] = set()
    pr_count = 0

    for pr, matches in results.items():
        if matches is None:
            stats["pr_errors"] += 1
            continue
        pr_count += 1
        entry = entries[pr]
        rr = run[pr]
        total_piku_findings += len(rr.get("findings", []))
        ground_by_id = {f["id"]: f for f in entry["findings"]}
        for m in matches:
            g = ground_by_id.get(m.get("ground_id"))
            if g is None:
                continue
            if g.get("ci_dependent"):
                stats["ci_dependent_skipped"] += 1
                continue
            label = g.get("reception", "unclear")
            strength = m.get("match_strength", "none")
            was_dropped = bool(m.get("candidate_dropped"))
            # A candidate dropped as "duplicate" means a reported finding shares its root
            # cause — matching the dropped twin still counts as a hit.
            dup_of_reported = False
            if was_dropped and m.get("candidate_index") is not None:
                di = int(m["candidate_index"]) - len(rr.get("findings", []))
                dropped_list = rr.get("dropped", [])
                if 0 <= di < len(dropped_list):
                    dup_of_reported = dropped_list[di].get("verify_verdict") == "duplicate"
            hit = strength in ("exact", "partial") and (not was_dropped or dup_of_reported)
            dropped_hit = strength in ("exact", "partial") and was_dropped and not dup_of_reported
            bot = g["bot"]
            kind = g.get("kind", "actionable")
            is_substantive = kind in ("actionable", "comment") or bot != "coderabbit"
            stats["ground_total"] += 1
            by_bot[bot]["total"] += 1
            by_label[label]["total"] += 1
            era = "early(<200)" if pr < 200 else "modern(>=200)"
            if label in VALID_LABELS:
                by_bot_valid[bot]["total"] += 1
                by_era[era]["total"] += 1
            if dropped_hit:
                stats["lost_to_verifier"] += 1
                if label in VALID_LABELS:
                    stats["lost_to_verifier_valid"] += 1
            if hit:
                stats["ground_matched"] += 1
                by_bot[bot]["matched"] += 1
                by_label[label]["matched"] += 1
                if label in VALID_LABELS:
                    by_bot_valid[bot]["matched"] += 1
                    by_era[era]["matched"] += 1
                if m.get("candidate_index") is not None:
                    matched_candidate_keys.add((pr, int(m["candidate_index"])))
            elif is_substantive and label in VALID_LABELS:
                missed_examples.append(
                    {"pr": pr, "id": g["id"], "bot": bot, "kind": kind, "label": label,
                     "path": g["path"], "line": g.get("line"),
                     "body": g["body"][:400]}
                )
            # FP catches: bot finding was a false positive; did we avoid repeating it?
            if label in FP_LABELS:
                subtype = g.get("fp_subtype", "unknown")
                fp_analysis["fp_total"] += 1
                fp_analysis[f"fp_{subtype}_total"] += 1
                if not hit:
                    fp_analysis["fp_not_repeated"] += 1
                    fp_analysis[f"fp_{subtype}_not_repeated"] += 1
                if dropped_hit:
                    # strongest catch: we generated the same claim and struck it down
                    fp_analysis["fp_explicitly_refuted"] += 1

    novel = total_piku_findings - len(matched_candidate_keys)

    def pct(a: int, b: int) -> str:
        return f"{100 * a / b:.1f}%" if b else "n/a"

    report = {
        "run": run_name,
        "prs_scored": pr_count,
        "pr_errors": stats["pr_errors"],
        "ground_total": stats["ground_total"],
        "ground_matched": stats["ground_matched"],
        "recall_overall": pct(stats["ground_matched"], stats["ground_total"]),
        "recall_by_bot": {
            b: {"recall": pct(c["matched"], c["total"]), "n": c["total"]} for b, c in sorted(by_bot.items())
        },
        "recall_by_bot_valid_only": {
            b: {"recall": pct(c["matched"], c["total"]), "n": c["total"]}
            for b, c in sorted(by_bot_valid.items())
        },
        "recall_by_reception": {
            l: {"recall": pct(c["matched"], c["total"]), "n": c["total"]} for l, c in sorted(by_label.items())
        },
        "recall_valid_only": pct(
            sum(by_label[l]["matched"] for l in VALID_LABELS),
            sum(by_label[l]["total"] for l in VALID_LABELS),
        ),
        "recall_valid_by_era": {
            e: {"recall": pct(c["matched"], c["total"]), "n": c["total"]}
            for e, c in sorted(by_era.items())
        },
        "fp_analysis": dict(fp_analysis),
        "fp_avoid_rate": pct(fp_analysis["fp_not_repeated"], fp_analysis["fp_total"]),
        "fp_factual_avoid_rate": pct(
            fp_analysis["fp_factual_not_repeated"], fp_analysis["fp_factual_total"]
        ),
        "fp_intent_avoid_rate": pct(
            fp_analysis["fp_intent_not_repeated"], fp_analysis["fp_intent_total"]
        ),
        "lost_to_verifier": stats["lost_to_verifier"],
        "lost_to_verifier_valid": stats["lost_to_verifier_valid"],
        "pikuscope_findings_total": total_piku_findings,
        "pikuscope_findings_matched": len(matched_candidate_keys),
        "pikuscope_findings_novel": novel,
        "findings_per_pr": round(total_piku_findings / pr_count, 2) if pr_count else 0,
    }
    out = RUNS / run_name / "score.json"
    out.write_text(json.dumps(report, indent=2))
    (RUNS / run_name / "missed.json").write_text(json.dumps(missed_examples, indent=2))
    print(json.dumps(report, indent=2))
    print(f"\nmissed valid findings: {len(missed_examples)} -> {RUNS / run_name / 'missed.json'}", file=sys.stderr)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-name", required=True)
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    score(args.run_name, refresh=args.refresh)
