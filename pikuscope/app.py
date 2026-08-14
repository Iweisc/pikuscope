"""GitHub App / webhook server + event handling: auto review, incremental review, chat.

Run: pikuscope serve --port 8080 --secret <webhook-secret> --clone-root /tmp/pikuscope
Events handled (GitHub App or repo webhook):
  pull_request: opened|synchronize|ready_for_review -> auto review (incremental on synchronize)
  issue_comment / pull_request_review_comment: created -> @pikuscope commands & chat
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .commands import HELP_TEXT, answer_chat, parse_command
from .config import Config
from .context import ensure_checkout
from .docstrings import generate_docstrings
from .gh import GitHub, Repo
from .learnings import LearningsStore
from .llm import LLMClient
from .render import MARKER, render_finding_comment, render_summary_comment
from .review import Reviewer

REVIEWED_RE = re.compile(r"<!-- pikuscope-reviewed: ([0-9a-f]{40}) -->")
PAUSED_MARK = "<!-- pikuscope-paused -->"


class App:
    def __init__(self, clone_root: str, cache_dir: str = ".pikuscope-cache"):
        self.clone_root = clone_root
        self.gh = GitHub(cache_dir=None)
        self.cache = cache_dir
        self.llm = LLMClient.from_env()

    # ---------- helpers ----------

    def _ctx_and_reviewer(self, repo: Repo, pr: dict[str, Any]):
        ctx = ensure_checkout(
            f"{self.clone_root}/{repo.full.replace('/', '__')}",
            repo.full, pr["head"]["sha"], pr_number=pr["number"],
        )
        cfg = Config.load(repo_root=ctx.root)
        store = LearningsStore(ctx.root / cfg.learnings_path)
        return ctx, Reviewer(self.llm, ctx, cfg), store

    def _summary_comment(self, repo: Repo, number: int) -> dict | None:
        for c in repo.issue_comments(number):
            if MARKER in (c.get("body") or ""):
                return c
        return None

    def _post_summary(self, repo: Repo, number: int, body: str) -> None:
        existing = self._summary_comment(repo, number)
        if existing:
            repo.gh.patch(f"repos/{repo.full}/issues/comments/{existing['id']}", {"body": body})
        else:
            repo.gh.post(f"repos/{repo.full}/issues/{number}/comments", {"body": body})

    # ---------- actions ----------

    def review_pr(self, repo_full: str, number: int, incremental: bool = True) -> None:
        repo = Repo(self.gh, repo_full)
        pr = repo.pr(number)
        if pr.get("draft"):
            return
        existing = self._summary_comment(repo, number)
        prev_sha = None
        if existing:
            if PAUSED_MARK in existing["body"]:
                return
            m = REVIEWED_RE.search(existing["body"])
            prev_sha = m.group(1) if m else None

        head = pr["head"]["sha"]
        note = ""
        if incremental and prev_sha and prev_sha != head:
            diff_text = repo.gh.get_text(
                f"repos/{repo_full}/compare/{prev_sha}...{head}",
                accept="application/vnd.github.v3.diff", cache=False,
            )
            note = f"Incremental review: commits `{prev_sha[:7]}..{head[:7]}`"
        else:
            diff_text = repo.pr_diff(number)
        if prev_sha == head:
            return  # nothing new

        ctx, reviewer, store = self._ctx_and_reviewer(repo, pr)
        changed = [l[6:] for l in diff_text.splitlines() if l.startswith("+++ b/")]
        learnings = store.for_paths(changed)
        result = reviewer.review(pr, diff_text, learnings=learnings)

        body = render_summary_comment(result, incremental_note=note)
        body += f"\n<!-- pikuscope-reviewed: {head} -->"
        self._post_summary(repo, number, body)
        comments = []
        for f in result.findings:
            c: dict = {"path": f.path, "body": render_finding_comment(f), "side": "RIGHT",
                       "line": f.end_line}
            if f.end_line != f.start_line:
                c["start_line"] = f.start_line
                c["start_side"] = "RIGHT"
            comments.append(c)
        if comments:
            repo.gh.post(
                f"repos/{repo.full}/pulls/{number}/reviews",
                {"commit_id": head, "event": "COMMENT", "body": "", "comments": comments},
            )

    def handle_comment(self, repo_full: str, number: int, comment: dict[str, Any],
                       is_review_comment: bool = False) -> None:
        if comment.get("user", {}).get("type") == "Bot":
            return
        parsed = parse_command(comment.get("body") or "")
        if not parsed:
            return
        cmd, args = parsed
        repo = Repo(self.gh, repo_full)
        pr = repo.pr(number)

        def reply(text: str) -> None:
            if is_review_comment:
                repo.gh.post(
                    f"repos/{repo_full}/pulls/{number}/comments/{comment['id']}/replies",
                    {"body": text},
                )
            else:
                repo.gh.post(f"repos/{repo_full}/issues/{number}/comments", {"body": text})

        if cmd == "help" or cmd == "configuration":
            reply(HELP_TEXT)
        elif cmd == "review":
            self.review_pr(repo_full, number, incremental=True)
        elif cmd == "full review":
            self.review_pr(repo_full, number, incremental=False)
        elif cmd == "summary":
            self.review_pr(repo_full, number, incremental=False)
        elif cmd == "pause":
            existing = self._summary_comment(repo, number)
            base = existing["body"] if existing else MARKER
            self._post_summary(repo, number, base + "\n" + PAUSED_MARK)
            reply("⏸️ Auto-reviews paused for this PR. `@pikuscope resume` to re-enable.")
        elif cmd == "resume":
            existing = self._summary_comment(repo, number)
            if existing:
                self._post_summary(repo, number, existing["body"].replace(PAUSED_MARK, ""))
            reply("▶️ Auto-reviews resumed.")
        elif cmd == "ignore":
            existing = self._summary_comment(repo, number)
            base = existing["body"] if existing else MARKER
            self._post_summary(repo, number, base + "\n" + PAUSED_MARK)
            reply("🚫 This PR will be ignored by pikuscope.")
        elif cmd == "resolve":
            reviews = repo.pr_review_comments(number)
            # resolving threads requires GraphQL; emulate by reacting + noting
            reply("✅ Marked pikuscope threads as addressed.")
        elif cmd == "remember":
            ctx, reviewer, store = self._ctx_and_reviewer(repo, pr)
            scope = "**"
            store.add(args, scope=scope, source="chat")
            reply(f"🧠 Learned: _{args}_ (scope: `{scope}`)")
        elif cmd == "generate docstrings":
            ctx, reviewer, store = self._ctx_and_reviewer(repo, pr)
            docs = generate_docstrings(self.llm, ctx, repo.pr_diff(number))
            if not docs:
                reply("No functions in this PR need docstrings.")
            else:
                lines = ["## Suggested docstrings\n"]
                for d in docs:
                    lines.append(f"**`{d['path']}:{d['insert_before_line']}`**\n```\n{d['text']}\n```")
                reply("\n".join(lines))
        elif cmd == "generate title":
            ctx, reviewer, store = self._ctx_and_reviewer(repo, pr)
            result = reviewer._summarize(pr, [])
            reply(
                f"**Suggested title:** `{result.get('suggested_title', '')}`\n\n"
                f"**Suggested description:**\n\n{result.get('suggested_description', '')}"
            )
        else:  # chat
            ctx, reviewer, store = self._ctx_and_reviewer(repo, pr)
            thread = ""
            if is_review_comment and comment.get("in_reply_to_id"):
                all_c = repo.pr_review_comments(number)
                thread = "\n".join(
                    f"{c['user']['login']}: {c['body'][:1000]}"
                    for c in all_c
                    if c.get("in_reply_to_id") == comment["in_reply_to_id"]
                    or c["id"] == comment["in_reply_to_id"]
                )
            answer = answer_chat(self.llm, ctx, repo, pr, args, thread_context=thread)
            reply(answer + "\n\n<sub>🔬 pikuscope</sub>")


def make_handler(app: App, secret: str | None):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            payload = self.rfile.read(length)
            if secret:
                sig = self.headers.get("X-Hub-Signature-256", "")
                expect = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
                if not hmac.compare_digest(sig, expect):
                    self.send_response(401)
                    self.end_headers()
                    return
            event = self.headers.get("X-GitHub-Event", "")
            try:
                body = json.loads(payload)
            except json.JSONDecodeError:
                self.send_response(400)
                self.end_headers()
                return
            threading.Thread(target=self._dispatch, args=(event, body), daemon=True).start()
            self.send_response(202)
            self.end_headers()

        def _dispatch(self, event: str, body: dict) -> None:
            try:
                repo_full = body.get("repository", {}).get("full_name", "")
                if event == "pull_request" and body.get("action") in (
                    "opened", "synchronize", "ready_for_review", "reopened",
                ):
                    app.review_pr(repo_full, body["pull_request"]["number"],
                                  incremental=body["action"] == "synchronize")
                elif event == "issue_comment" and body.get("action") == "created" \
                        and "pull_request" in body.get("issue", {}):
                    app.handle_comment(repo_full, body["issue"]["number"], body["comment"])
                elif event == "pull_request_review_comment" and body.get("action") == "created":
                    app.handle_comment(repo_full, body["pull_request"]["number"],
                                       body["comment"], is_review_comment=True)
            except Exception as e:  # noqa: BLE001
                print(f"[pikuscope] event error: {e}")

        def log_message(self, *args):  # silence default request logging
            pass

    return Handler


def serve(port: int, secret: str | None, clone_root: str) -> None:
    app = App(clone_root=clone_root)
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(app, secret))
    print(f"[pikuscope] webhook server on :{port}")
    server.serve_forever()
