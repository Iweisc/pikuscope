"""Sub-classify false_positive ground findings: factual | intent | stale.

- factual: the bot's claim is provably wrong from the code (a guard/type/behavior disproves it).
- intent: technically plausible, but maintainers deliberately chose/accept the behavior.
- stale: the finding targeted an intermediate commit superseded within the same PR.

fp_avoid parity is judged on "factual" (catchable by verification); "intent" FPs are
handled by the learnings feature after first dismissal — same as every other bot.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pikuscope.llm import LLMClient  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent / "data"

SYSTEM = """Code-review-bot findings below were dismissed by maintainers as false positives. \
Classify each dismissal:
- "factual": the maintainer showed the claim was WRONG about the code/library behavior \
(a guard exists, the API works differently, the scenario cannot happen).
- "intent": the claim describes real behavior, but the maintainer says it is deliberate, \
acceptable, out of scope, or a conscious tradeoff.
- "stale": the finding was about an intermediate commit that later commits in the same PR \
already replaced.

Output ONLY JSON: {"labels": [{"id": <finding id>, "subtype": "factual"|"intent"|"stale"}]}
"""


def run(dataset: str) -> None:
    llm = LLMClient.from_env()
    path = DATA_DIR / dataset
    entries = [json.loads(l) for l in path.read_text().splitlines()]
    todo = []
    for e in entries:
        for f in e["findings"]:
            if f.get("reception") == "false_positive" and "fp_subtype" not in f:
                todo.append((e, f))
    print(f"{len(todo)} FP findings to subtype", file=sys.stderr)
    for i in range(0, len(todo), 20):
        chunk = todo[i : i + 20]
        blocks = []
        for e, f in chunk:
            thread = "\n".join(
                f"  {'BOT' if r['is_bot'] else 'HUMAN'} {r['user']}: {r['body'][:600]}" for r in f["thread"]
            )
            blocks.append(
                f"### id={f['id']} (PR #{e['pr']})\nClaim:\n{f['body'][:1200]}\n"
                f"Dev evidence: {f.get('reception_evidence', '')}\nThread:\n{thread or '(none)'}"
            )
        try:
            data = llm.chat_json(
                [{"role": "system", "content": SYSTEM},
                 {"role": "user", "content": "\n\n".join(blocks)}],
                reasoning_effort="medium",
            )
            labels = {l["id"]: l.get("subtype", "intent") for l in data.get("labels", [])}
        except Exception as ex:  # noqa: BLE001
            print(f"batch failed: {ex}", file=sys.stderr)
            labels = {}
        for e, f in chunk:
            f["fp_subtype"] = labels.get(f["id"], "intent")
    with path.open("w") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")
    print(Counter(f.get("fp_subtype") for e in entries for f in e["findings"]
                  if f.get("reception") == "false_positive"), file=sys.stderr)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="dataset.jsonl")
    args = ap.parse_args()
    run(args.dataset)
