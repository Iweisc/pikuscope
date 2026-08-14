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

    sv = sub.add_parser("serve", help="run webhook server (GitHub App / webhook mode)")
    sv.add_argument("--port", type=int, default=8080)
    sv.add_argument("--secret", default=None, help="webhook secret")
    sv.add_argument("--clone-root", default="/tmp/pikuscope-clones")
    sv.set_defaults(func=cmd_serve)

    dc = sub.add_parser("docstrings", help="suggest docstrings for a PR's changed functions")
    dc.add_argument("--repo", required=True)
    dc.add_argument("--pr", type=int, required=True)
    dc.add_argument("--clone-dir", default=None)
    dc.add_argument("--cache-dir", default=".pikuscope-cache")
    dc.add_argument("--effort", default=None)
    dc.add_argument("--profile", default=None)
    dc.set_defaults(func=cmd_docstrings)

    au = sub.add_parser("audit", help="audit other bots' review comments for false positives")
    au.add_argument("--repo", required=True)
    au.add_argument("--pr", type=int, required=True)
    au.add_argument("--clone-dir", default=None)
    au.add_argument("--cache-dir", default=".pikuscope-cache")
    au.add_argument("--effort", default=None)
    au.add_argument("--profile", default=None)
    au.add_argument("--post", action="store_true", help="reply to suspected false positives")
    au.add_argument("--json-out", default=None)
    au.set_defaults(func=cmd_audit)

    ch = sub.add_parser("ask", help="ask a question about a PR")
    ch.add_argument("--repo", required=True)
    ch.add_argument("--pr", type=int, required=True)
    ch.add_argument("--clone-dir", default=None)
    ch.add_argument("--cache-dir", default=".pikuscope-cache")
    ch.add_argument("--effort", default=None)
    ch.add_argument("--profile", default=None)
    ch.add_argument("question")
    ch.set_defaults(func=cmd_ask)

    args = ap.parse_args(argv)
    return args.func(args)


def cmd_serve(args: argparse.Namespace) -> int:
    from .app import serve

    serve(args.port, args.secret, args.clone_root)
    return 0


def cmd_docstrings(args: argparse.Namespace) -> int:
    from .docstrings import generate_docstrings

    gh = GitHub(cache_dir=args.cache_dir)
    repo = Repo(gh, args.repo)
    pr = repo.pr(args.pr)
    reviewer = build_reviewer(args, repo, pr)
    docs = generate_docstrings(reviewer.llm, reviewer.ctx, repo.pr_diff(args.pr))
    for d in docs:
        print(f"--- {d['path']}:{d['insert_before_line']}\n{d['text']}\n")
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    from .audit import audit_bot_comments, render_audit_reply

    gh = GitHub(cache_dir=args.cache_dir)
    repo = Repo(gh, args.repo)
    pr = repo.pr(args.pr)
    reviewer = build_reviewer(args, repo, pr)
    audits = audit_bot_comments(reviewer.llm, reviewer.ctx, repo, pr)
    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(json.dumps(audits, indent=2))
    for a in audits:
        print(f"[{a['verdict']} {a.get('confidence', 0):.0%}] {a['bot']} {a['path']}:{a['line']}")
        print(f"  claim: {a['claim'][:150]}")
        print(f"  audit: {a.get('explanation', '')[:300]}\n")
    if args.post:
        for a in audits:
            if a["verdict"] == "false_positive" and a.get("confidence", 0) >= 0.7:
                repo.gh.post(
                    f"repos/{args.repo}/pulls/{args.pr}/comments/{a['comment_id']}/replies",
                    {"body": render_audit_reply(a)},
                )
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    from .commands import answer_chat

    gh = GitHub(cache_dir=args.cache_dir)
    repo = Repo(gh, args.repo)
    pr = repo.pr(args.pr)
    reviewer = build_reviewer(args, repo, pr)
    print(answer_chat(reviewer.llm, reviewer.ctx, repo, pr, args.question))
    return 0


if __name__ == "__main__":
    sys.exit(main())
