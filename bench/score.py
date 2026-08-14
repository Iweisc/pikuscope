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

A MATCH means the same root cause at the same place: both reviewers would be satisfied by the \
same fix. Same file but different issue = no match. Same general topic but different defect = \
no match. Wording/severity may differ; a match is about the defect, not the prose. A match may \
also count if reviewer B covers the issue as part of a broader finding, as long as B explicitly \
mentions the specific problem A raised.

For each GROUND finding, check every CANDIDATE finding (a match candidate list is provided).

Output ONLY JSON:
{"matches": [{"ground_id": <id>, "candidate_index": <int or null>, "match_strength": "exact"|"partial"|"none", "reason": "one sentence"}]}
- "exact": same defect, same fix.
- "partial": candidate identifies the problem but incompletely or as a side note.
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
    # dropped candidates count for FP analysis, not for recall
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
            f"### CANDIDATE index={i}\nfile: {f['path']} lines {f['start_line']}-{f['end_line']}\n"
            f"severity={f['severity']} category={f['category']}\ntitle: {f['title']}\n"
            f"body:\n{f['body'][:2000]}"
        )
    user = (
        f"# PR #{entry['pr']}: {entry['title']}\n\n# GROUND findings (reviewer A)\n"
        + "\n\n".join(g_blocks)
        + "\n\n# CANDIDATE findings (reviewer B)\n"
        + ("\n\n".join(c_blocks) if c_blocks else "(reviewer B reported no findings)")
    )
    data = llm.chat_json(
        [{"role": "system", "content": MATCH_SYSTEM}, {"role": "user", "content": user}],
        reasoning_effort="high",
    )
    return data.get("matches", [])


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
    by_label: dict[str, Counter] = defaultdict(Counter)
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
            label = g.get("reception", "unclear")
            strength = m.get("match_strength", "none")
            hit = strength in ("exact", "partial")
            bot = g["bot"]
            kind = g.get("kind", "actionable")
            is_substantive = kind in ("actionable", "comment") or bot != "coderabbit"
            stats["ground_total"] += 1
            by_bot[bot]["total"] += 1
            by_label[label]["total"] += 1
            if hit:
                stats["ground_matched"] += 1
                by_bot[bot]["matched"] += 1
                by_label[label]["matched"] += 1
                if m.get("candidate_index") is not None:
                    matched_candidate_keys.add((pr, int(m["candidate_index"])))
            elif is_substantive and label in VALID_LABELS:
                missed_examples.append(
                    {"pr": pr, "bot": bot, "kind": kind, "label": label,
                     "path": g["path"], "line": g.get("line"),
                     "body": g["body"][:400]}
                )
            # FP catches: bot finding was a false positive; did we avoid repeating it?
            if label in FP_LABELS:
                fp_analysis["fp_total"] += 1
                if not hit:
                    fp_analysis["fp_not_repeated"] += 1
                # strong catch: we generated it as a candidate and refuted it
                refuted = any(
                    d.get("path") == g["path"] and d.get("verify_verdict") == "refuted"
                    for d in rr.get("dropped", [])
                )
                if refuted:
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
        "recall_by_reception": {
            l: {"recall": pct(c["matched"], c["total"]), "n": c["total"]} for l, c in sorted(by_label.items())
        },
        "recall_valid_only": pct(
            sum(by_label[l]["matched"] for l in VALID_LABELS),
            sum(by_label[l]["total"] for l in VALID_LABELS),
        ),
        "fp_analysis": dict(fp_analysis),
        "fp_avoid_rate": pct(fp_analysis["fp_not_repeated"], fp_analysis["fp_total"]),
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
