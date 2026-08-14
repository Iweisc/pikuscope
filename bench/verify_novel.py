"""Adversarially verify pikuscope findings that matched no bot finding ("novel").

A novel finding is only credited if an independent adversarial judge (with repo tools,
at the same commit) confirms it as a real, introduced defect. Everything else counts
against precision.
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pikuscope.context import ensure_checkout  # noqa: E402
from pikuscope.llm import LLMClient, extract_json  # noqa: E402
from pikuscope.review import TOOLS_SPEC, make_tool_handler  # noqa: E402

DATA = ROOT / "bench" / "data" / "dataset.jsonl"
RUNS = ROOT / "bench" / "runs"
CLONE = ROOT / "bench" / "repos" / "t3code"

JUDGE_SYSTEM = """You are an independent adversarial judge evaluating whether a code-review \
finding on a pull request is a REAL defect worth a review comment. You have tools over the \
repository at the reviewed commit. Investigate: read the full surrounding code, check callers, \
check guards. Default to NOT-real unless the evidence is concrete.

"real" requires ALL of:
1. The claimed behavior actually occurs in the code as written (verify by reading it).
2. It was introduced or made worse by this PR's diff (not pre-existing).
3. A competent team would want it flagged (it affects correctness, security, perf, UX,
   data integrity, or violates an explicit repo convention) — not a style taste.

Output ONLY JSON: {"real": true|false, "confidence": 0.0-1.0, "reason": "concise evidence-based justification"}
"""


def verify_run(run_name: str, workers: int = 6) -> None:
    llm = LLMClient.from_env()
    entries = {e["pr"]: e for e in (json.loads(l) for l in DATA.read_text().splitlines())}
    out_dir = RUNS / run_name / "novel"
    out_dir.mkdir(parents=True, exist_ok=True)

    jobs = []
    for p in (RUNS / run_name).glob("pr-*.json"):
        rr = json.loads(p.read_text())
        pr = rr.get("pr")
        if rr.get("error") or pr not in entries:
            continue
        match_path = RUNS / run_name / "matches" / f"pr-{pr}.json"
        matched_idx = set()
        if match_path.exists():
            for m in json.loads(match_path.read_text()):
                if m.get("candidate_index") is not None and m.get("match_strength") in ("exact", "partial"):
                    matched_idx.add(int(m["candidate_index"]))
        for i, f in enumerate(rr.get("findings", [])):
            if i not in matched_idx:
                jobs.append((pr, i, f, entries[pr]))

    print(f"{len(jobs)} novel findings to verify", file=sys.stderr)

    def one(job):
        pr, i, f, entry = job
        out_path = out_dir / f"pr-{pr}-f{i}.json"
        if out_path.exists():
            return json.loads(out_path.read_text())
        try:
            ctx = ensure_checkout(CLONE, "pingdotgg/t3code", entry["review_sha"], pr_number=pr)
            user = (
                f"# PR #{pr}: {entry['title']}\n{entry['body'][:1500]}\n\n"
                f"# Finding to judge\nfile: {f['path']} lines {f['start_line']}-{f['end_line']}\n"
                f"severity: {f['severity']} | category: {f['category']}\ntitle: {f['title']}\n"
                f"claim:\n{f['body'][:2500]}\n"
                f"failure_scenario: {f.get('failure_scenario', '')}\n\n"
                "Investigate with tools, then output your JSON verdict."
            )
            text = llm.chat_with_tools(
                [{"role": "system", "content": JUDGE_SYSTEM}, {"role": "user", "content": user}],
                TOOLS_SPEC, make_tool_handler(ctx), reasoning_effort="high",
            )
            verdict = extract_json(text)
        except Exception as e:  # noqa: BLE001
            verdict = {"real": None, "confidence": 0, "reason": f"judge error: {e}"}
        record = {"pr": pr, "index": i, "path": f["path"], "title": f["title"],
                  "severity": f["severity"], **verdict}
        out_path.write_text(json.dumps(record, indent=2))
        return record

    results = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for r in pool.map(one, jobs):
            results.append(r)
            print(f"PR {r['pr']} [{r['index']}] {r['title'][:60]}: real={r.get('real')} ({r.get('confidence')})",
                  file=sys.stderr, flush=True)

    real = [r for r in results if r.get("real") is True]
    fake = [r for r in results if r.get("real") is False]
    summary = {
        "novel_total": len(results),
        "novel_real": len(real),
        "novel_fake": len(fake),
        "novel_precision": f"{100 * len(real) / len(results):.1f}%" if results else "n/a",
        "real_findings": [
            {"pr": r["pr"], "path": r["path"], "title": r["title"], "severity": r["severity"],
             "confidence": r.get("confidence")} for r in real
        ],
    }
    (RUNS / run_name / "novel_summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-name", required=True)
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()
    verify_run(args.run_name, args.workers)
