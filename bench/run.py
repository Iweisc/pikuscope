"""Run pikuscope on benchmark PRs at the bot-reviewed commit, without bot visibility.

For each dataset entry: checkout review_sha, compute the diff the bot saw
(merge-base(base branch, review_sha)..review_sha), run the reviewer, save results.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pikuscope.config import Config  # noqa: E402
from pikuscope.context import ensure_checkout  # noqa: E402
from pikuscope.llm import LLMClient  # noqa: E402
from pikuscope.review import Reviewer  # noqa: E402

DATA = ROOT / "bench" / "data" / "dataset.jsonl"
RUNS = ROOT / "bench" / "runs"
CLONE = ROOT / "bench" / "repos" / "t3code"


def pr_diff_at(repo_dir: Path, base_sha: str, review_sha: str) -> str:
    """The diff the bot reviewed: merge-base(base_sha, review_sha)..review_sha."""
    mb = subprocess.run(
        ["git", "merge-base", base_sha, review_sha],
        cwd=repo_dir, capture_output=True, text=True,
    )
    base = mb.stdout.strip() if mb.returncode == 0 else base_sha
    proc = subprocess.run(
        ["git", "diff", "--no-color", f"{base}..{review_sha}"],
        cwd=repo_dir, capture_output=True, text=True,
    )
    return proc.stdout


def ensure_base(repo_dir: Path, base_sha: str) -> None:
    have = subprocess.run(["git", "cat-file", "-e", f"{base_sha}^{{commit}}"],
                          cwd=repo_dir, capture_output=True)
    if have.returncode != 0:
        subprocess.run(["git", "fetch", "origin", base_sha], cwd=repo_dir, capture_output=True)


def run_one(entry: dict, run_dir: Path, effort: str, learnings: list[str] | None = None) -> dict:
    pr_num = entry["pr"]
    out_path = run_dir / f"pr-{pr_num}.json"
    if out_path.exists():
        return json.loads(out_path.read_text())
    llm = LLMClient.from_env()
    llm.reasoning_effort = effort
    review_sha = entry["review_sha"]
    ctx = ensure_checkout(CLONE, "pingdotgg/t3code", review_sha, pr_number=pr_num)
    repo_dir = CLONE / "repo.git"
    ensure_base(repo_dir, entry["base_sha"])
    diff_text = pr_diff_at(repo_dir, entry["base_sha"], review_sha)
    if not diff_text.strip():
        result = {"pr": pr_num, "error": "empty diff"}
        out_path.write_text(json.dumps(result))
        return result

    pr_meta = {
        "title": entry["title"],
        "body": entry["body"],
        "number": pr_num,
        "base": {"ref": entry["base_ref"]},
        "head": {"sha": review_sha},
    }
    cfg = Config()
    reviewer = Reviewer(llm, ctx, cfg)
    try:
        rr = reviewer.review(pr_meta, diff_text, learnings=learnings)
        result = {
            "pr": pr_num,
            "review_sha": review_sha,
            "findings": [f.to_dict() for f in rr.findings],
            "dropped": [f.to_dict() for f in rr.dropped],
            "summary": rr.summary,
            "usage": {
                "calls": llm.usage.calls,
                "prompt_tokens": llm.usage.prompt_tokens,
                "completion_tokens": llm.usage.completion_tokens,
            },
        }
    except Exception as e:  # noqa: BLE001
        result = {"pr": pr_num, "error": f"{e}\n{traceback.format_exc()[-2000:]}"}
    out_path.write_text(json.dumps(result, indent=2))
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-name", required=True)
    ap.add_argument("--prs", default="", help="comma-separated PR numbers; default = all")
    ap.add_argument("--effort", default="xhigh")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--learnings", default=None,
                    help="path to learnings.jsonl to inject into reviews")
    args = ap.parse_args()

    learnings: list[str] | None = None
    if args.learnings:
        learnings = [json.loads(l)["text"] for l in Path(args.learnings).read_text().splitlines()]

    entries = [json.loads(l) for l in DATA.read_text().splitlines()]
    if args.prs:
        want = {int(x) for x in args.prs.split(",")}
        entries = [e for e in entries if e["pr"] in want]
    if args.limit:
        entries = entries[: args.limit]
    # smallest first: quick results land early, monsters run at the end
    entries.sort(key=lambda e: (e.get("changed_files") or 999, e.get("additions") or 0))

    run_dir = RUNS / args.run_name
    run_dir.mkdir(parents=True, exist_ok=True)

    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(run_one, e, run_dir, args.effort, learnings): e for e in entries}
        for fut in as_completed(futs):
            e = futs[fut]
            done += 1
            try:
                r = fut.result()
                n = len(r.get("findings", []))
                err = r.get("error", "")
                print(f"[{done}/{len(entries)}] PR {e['pr']}: "
                      + (f"ERROR {err[:120]}" if err else f"{n} findings"), file=sys.stderr, flush=True)
            except Exception as ex:  # noqa: BLE001
                print(f"[{done}/{len(entries)}] PR {e['pr']}: crashed {ex}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
