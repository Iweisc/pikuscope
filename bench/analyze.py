"""Analyze benchmark misses: why did pikuscope not find what the bots found?

Classifies each missed valid finding and each false-positive-repeat so the
improvement loop knows what to fix (prompt gap, context gap, verifier over-drop,
batching, anchoring).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pikuscope.llm import LLMClient  # noqa: E402

RUNS = ROOT / "bench" / "runs"
DATA = ROOT / "bench" / "data" / "dataset.jsonl"

ANALYZE_SYSTEM = """You diagnose why an AI code reviewer (pikuscope) missed a finding that \
another review bot caught on the same PR. You get: the bot's finding, pikuscope's full list of \
reported findings AND its internally-dropped candidates for that PR.

Classify the miss:
- "found_but_dropped": pikuscope generated the same issue as a candidate but its verifier refuted it.
- "near_miss": pikuscope commented on the same lines/file but described a different issue.
- "not_generated": pikuscope never surfaced the issue at all.
- "ground_truth_dubious": the bot's finding looks wrong/trivial/noise, missing it is fine.

Also state the single most likely root cause in one sentence (e.g. "verifier too strict about \
platform-specific claims", "finder never reads sibling file X", "nitpick below reporting bar").

Output ONLY JSON:
{"diagnoses": [{"ground_id": int, "class": str, "root_cause": str}]}
"""


def analyze(run_name: str) -> None:
    llm = LLMClient.from_env()
    entries = {e["pr"]: e for e in (json.loads(l) for l in DATA.read_text().splitlines())}
    missed = json.loads((RUNS / run_name / "missed.json").read_text())
    if not missed:
        print("no misses to analyze")
        return
    by_pr: dict[int, list[dict]] = {}
    for m in missed:
        by_pr.setdefault(m["pr"], []).append(m)

    diagnoses = []
    for pr, misses in by_pr.items():
        rr = json.loads((RUNS / run_name / f"pr-{pr}.json").read_text())
        entry = entries[pr]
        ground_by_id = {f["id"]: f for f in entry["findings"]}
        g_blocks = []
        for m in misses:
            g = next((f for f in entry["findings"] if f["path"] == m["path"] and f["body"][:200] == m["body"][:200]), None)
            gid = g["id"] if g else -1
            g_blocks.append(
                f"### MISSED ground_id={gid} (bot={m['bot']})\n{m['path']}:{m.get('line')}\n{m['body'][:1200]}"
            )
        piku = "\n".join(
            f"- [{f['severity']}] {f['path']}:{f['start_line']} {f['title']}: {f['body'][:300]}"
            for f in rr.get("findings", [])
        ) or "(none)"
        dropped = "\n".join(
            f"- [dropped: {f.get('verify_reason', '')[:150]}] {f['path']}:{f['start_line']} {f['title']}: {f['body'][:200]}"
            for f in rr.get("dropped", [])
        ) or "(none)"
        user = (
            f"# PR #{pr}: {entry['title']}\n\n# Bot findings pikuscope missed\n"
            + "\n\n".join(g_blocks)
            + f"\n\n# pikuscope reported findings\n{piku}\n\n# pikuscope dropped candidates\n{dropped}"
        )
        try:
            data = llm.chat_json(
                [{"role": "system", "content": ANALYZE_SYSTEM}, {"role": "user", "content": user}],
                reasoning_effort="high",
            )
            for d in data.get("diagnoses", []):
                d["pr"] = pr
                g = ground_by_id.get(d.get("ground_id"))
                if g:
                    d["path"] = g["path"]
                    d["bot"] = g["bot"]
                    d["body_head"] = g["body"][:150]
                diagnoses.append(d)
        except Exception as e:  # noqa: BLE001
            print(f"PR {pr} analyze failed: {e}", file=sys.stderr)

    out = RUNS / run_name / "diagnoses.json"
    out.write_text(json.dumps(diagnoses, indent=2))
    print(Counter(d["class"] for d in diagnoses))
    print("\nRoot causes:")
    for d in diagnoses:
        print(f"- PR {d['pr']} [{d.get('bot')}] {d.get('class')}: {d.get('root_cause')}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-name", required=True)
    args = ap.parse_args()
    analyze(args.run_name)
