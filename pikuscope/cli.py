"""CLI: pikuscope review / summarize / post."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .config import Config
from .context import ApiRepoContext, ensure_checkout
from .gh import GitHub, Repo
from .llm import LLMClient
from .render import render_finding_comment, render_report, render_summary_comment, MARKER
from .review import Reviewer


def build_reviewer(args: argparse.Namespace, repo: Repo, pr: dict) -> Reviewer:
    llm = LLMClient.from_env()
    if args.effort:
        llm.reasoning_effort = args.effort
    head_sha = pr["head"]["sha"]
    if args.clone_dir:
        ctx = ensure_checkout(args.clone_dir, args.repo, head_sha, pr_number=pr["number"])
    else:
        ctx = ApiRepoContext(repo, head_sha)
    cfg_data = None
    cfg_root = None
    if hasattr(ctx, "root"):
        cfg_root = ctx.root  # type: ignore[attr-defined]
    cfg = Config.load(repo_root=cfg_root, data=cfg_data)
    if args.profile:
        cfg.profile = args.profile
    return Reviewer(llm, ctx, cfg)


def cmd_review(args: argparse.Namespace) -> int:
    gh = GitHub(cache_dir=args.cache_dir)
    repo = Repo(gh, args.repo)
    pr = repo.pr(args.pr)
    diff_text = repo.pr_diff(args.pr)
    reviewer = build_reviewer(args, repo, pr)

    t0 = time.time()
    result = reviewer.review(pr, diff_text, progress=lambda m: print(f"[pikuscope] {m}", file=sys.stderr))
    dt = time.time() - t0
    u = reviewer.llm.usage
    print(
        f"[pikuscope] done in {dt:.0f}s · {u.calls} llm calls · "
        f"{u.prompt_tokens} in / {u.completion_tokens} out tokens",
        file=sys.stderr,
    )

    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(json.dumps(result.to_dict(), indent=2))
    report = render_report(result, pr_ref=f"{args.repo}#{args.pr}")
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(report)
    else:
        print(report)

    if args.post:
        post_review(repo, args.pr, pr, result)
        print("[pikuscope] posted to GitHub", file=sys.stderr)
    return 0


def post_review(repo: Repo, number: int, pr: dict, result) -> None:
    """Post/update summary comment and create a PR review with inline comments."""
    existing = repo.issue_comments(number)
    summary_md = render_summary_comment(result)
    mine = [c for c in existing if MARKER in (c.get("body") or "")]
    if mine:
        repo.gh.patch(
            f"repos/{repo.full}/issues/comments/{mine[-1]['id']}", {"body": summary_md}
        )
    else:
        repo.gh.post(f"repos/{repo.full}/issues/{number}/comments", {"body": summary_md})

    comments = []
    for f in result.findings:
        c: dict = {"path": f.path, "body": render_finding_comment(f), "side": "RIGHT"}
        if f.end_line != f.start_line:
            c["start_line"] = f.start_line
            c["start_side"] = "RIGHT"
            c["line"] = f.end_line
        else:
            c["line"] = f.start_line
        comments.append(c)
    if comments:
        repo.gh.post(
            f"repos/{repo.full}/pulls/{number}/reviews",
            {
                "commit_id": pr["head"]["sha"],
                "event": "COMMENT",
                "body": "",
                "comments": comments,
            },
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pikuscope", description="AI PR review bot")
    sub = ap.add_subparsers(dest="cmd", required=True)

    rv = sub.add_parser("review", help="review a pull request")
    rv.add_argument("--repo", required=True, help="owner/name")
    rv.add_argument("--pr", type=int, required=True)
    rv.add_argument("--post", action="store_true", help="post results to the PR")
    rv.add_argument("--profile", choices=["chill", "assertive"], default=None)
    rv.add_argument("--effort", default=None, help="reasoning effort override")
    rv.add_argument("--clone-dir", default=None, help="dir for local clone (better context)")
    rv.add_argument("--cache-dir", default=".pikuscope-cache")
    rv.add_argument("--out", default=None, help="write markdown report to file")
    rv.add_argument("--json-out", default=None, help="write structured result to file")
    rv.set_defaults(func=cmd_review)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
