"""Render a run's artifacts (score.json, novel_summary.json) into a markdown report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

RUNS = Path(__file__).resolve().parent / "runs"


def report(run_name: str) -> str:
    d = json.loads((RUNS / run_name / "score.json").read_text())
    novel_path = RUNS / run_name / "novel_summary.json"
    novel = json.loads(novel_path.read_text()) if novel_path.exists() else None
    lines = [f"## Run `{run_name}`", ""]
    lines.append(f"- PRs scored: **{d['prs_scored']}** (errors: {d.get('pr_errors', 0)})")
    lines.append(f"- Ground findings: **{d['ground_total']}** · matched: {d['ground_matched']}")
    lines.append(f"- **Valid-only recall: {d['recall_valid_only']}** · overall: {d['recall_overall']}")
    lines.append("")
    lines.append("| bot | recall (all) | n | recall (valid) | n |")
    lines.append("|---|---|---|---|---|")
    for bot in sorted(set(d["recall_by_bot"]) | set(d.get("recall_by_bot_valid_only", {}))):
        a = d["recall_by_bot"].get(bot, {})
        v = d.get("recall_by_bot_valid_only", {}).get(bot, {})
        lines.append(
            f"| {bot} | {a.get('recall', '—')} | {a.get('n', 0)} | {v.get('recall', '—')} | {v.get('n', 0)} |"
        )
    lines.append("")
    lines.append("| reception | recall | n |")
    lines.append("|---|---|---|")
    for label, c in sorted(d.get("recall_by_reception", {}).items()):
        lines.append(f"| {label} | {c['recall']} | {c['n']} |")
    lines.append("")
    fa = d.get("fp_analysis", {})
    lines.append(
        f"- FP catches: avoided {fa.get('fp_not_repeated', 0)}/{fa.get('fp_total', 0)} "
        f"({d.get('fp_avoid_rate')}), explicitly refuted: {fa.get('fp_explicitly_refuted', 0)}"
    )
    lines.append(
        f"- pikuscope findings: {d['pikuscope_findings_total']} total · {d['findings_per_pr']}/PR · "
        f"{d['pikuscope_findings_matched']} matched · {d['pikuscope_findings_novel']} novel"
    )
    if novel:
        lines.append(
            f"- Novel verification: **{novel['novel_real']}/{novel['novel_total']} real "
            f"({novel['novel_precision']})** — verified issues every bot missed"
        )
    lines.append(f"- Verifier losses (valid findings killed): {d.get('lost_to_verifier_valid', 0)}")
    return "\n".join(lines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-name", required=True)
    args = ap.parse_args()
    print(report(args.run_name))
