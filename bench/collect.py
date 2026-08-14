"""Collect ground truth from t3code: bot inline findings + dev interactions + FP labels.

Output: bench/data/dataset.jsonl — one entry per PR with bot findings.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pikuscope.gh import GitHub, Repo  # noqa: E402
from pikuscope.llm import LLMClient  # noqa: E402

BOT_USERS = {
    "coderabbitai[bot]": "coderabbit",
    "greptile-apps[bot]": "greptile",
    "cursor[bot]": "cursor",
    "macroscopeapp[bot]": "macroscope",
    "chatgpt-codex-connector[bot]": "codex",
    "t3-code[bot]": "t3code-bot",
}

DATA_DIR = Path(__file__).resolve().parent / "data"
CACHE_DIR = Path(__file__).resolve().parent / "cache" / "gh"


def classify_coderabbit_kind(body: str) -> str:
    b = body.lower()
    if "nitpick" in b or "🧹" in body:
        return "nitpick"
    if "potential issue" in b or "⚠️" in body or "critical" in b:
        return "actionable"
    if "refactor suggestion" in b or "🛠" in body:
        return "refactor"
    if "verification" in b:
        return "verification"
    return "comment"


def clean_bot_body(body: str) -> str:
    """Strip html comments, tips, and boilerplate from bot comment bodies."""
    body = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
    # strip coderabbit committable suggestion blocks' footer noise
    body = re.sub(r"(?s)<details>\s*<summary>🤖 Prompt for AI Agents</summary>.*?</details>", "", body)
    body = re.sub(r"(?s)<details>\s*<summary>📝 Committable suggestion</summary>.*?</details>", "", body)
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip()


def collect(repo_full: str, max_prs: int | None = None) -> None:
    gh = GitHub(cache_dir=CACHE_DIR)
    repo = Repo(gh, repo_full)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    print("paginating all PR review comments...", file=sys.stderr)
    all_comments = gh.get_paginated(
        f"repos/{repo_full}/pulls/comments",
        params={"sort": "created", "direction": "asc"},
        max_pages=200,
    )
    print(f"{len(all_comments)} review comments total", file=sys.stderr)

    by_pr: dict[int, list[dict]] = defaultdict(list)
    for c in all_comments:
        m = re.search(r"/pulls/(\d+)$", c.get("pull_request_url", ""))
        if m:
            by_pr[int(m.group(1))].append(c)

    candidates = []
    for pr_num, comments in sorted(by_pr.items()):
        bot_comments = [c for c in comments if c["user"]["login"] in BOT_USERS]
        if bot_comments:
            candidates.append((pr_num, comments, bot_comments))
    print(f"{len(candidates)} PRs with bot inline comments", file=sys.stderr)

    out_path = DATA_DIR / "dataset.jsonl"
    entries = []
    for pr_num, comments, bot_comments in candidates:
        try:
            pr = repo.pr(pr_num)
        except Exception as e:  # noqa: BLE001
            print(f"PR {pr_num}: fetch failed {e}", file=sys.stderr)
            continue
        if not pr.get("merged_at"):
            continue  # merged PRs only, per benchmark design

        # threads: root comments + replies
        roots = {c["id"]: c for c in comments if not c.get("in_reply_to_id")}
        replies: dict[int, list[dict]] = defaultdict(list)
        for c in comments:
            if c.get("in_reply_to_id"):
                # replies chain to the root id
                rid = c["in_reply_to_id"]
                while rid not in roots:
                    parent = next((x for x in comments if x["id"] == rid), None)
                    if parent is None or not parent.get("in_reply_to_id"):
                        break
                    rid = parent["in_reply_to_id"]
                replies[rid].append(c)

        findings = []
        for c in bot_comments:
            if c.get("in_reply_to_id"):
                continue  # bot replies inside threads are not findings
            bot = BOT_USERS[c["user"]["login"]]
            body = clean_bot_body(c.get("body") or "")
            if not body or len(body) < 20:
                continue
            thread = [
                {
                    "user": r["user"]["login"],
                    "is_bot": r["user"]["login"] in BOT_USERS or r["user"]["type"] == "Bot",
                    "body": clean_bot_body(r.get("body") or "")[:2000],
                }
                for r in sorted(replies.get(c["id"], []), key=lambda x: x["created_at"])
            ]
            reactions = c.get("reactions") or {}
            findings.append(
                {
                    "id": c["id"],
                    "bot": bot,
                    "path": c.get("path"),
                    "line": c.get("line") or c.get("original_line"),
                    "start_line": c.get("start_line") or c.get("original_start_line"),
                    "commit_id": c.get("original_commit_id") or c.get("commit_id"),
                    "kind": classify_coderabbit_kind(c.get("body") or "") if bot == "coderabbit" else "actionable",
                    "body": body[:4000],
                    "diff_hunk": (c.get("diff_hunk") or "")[-1500:],
                    "thread": thread,
                    "reactions": {k: v for k, v in reactions.items() if k not in ("url", "total_count") and v},
                    "created_at": c.get("created_at"),
                }
            )
        if not findings:
            continue

        # earliest bot-reviewed commit = the state to re-review
        commit_ids = [f["commit_id"] for f in findings if f["commit_id"]]
        entries.append(
            {
                "pr": pr_num,
                "title": pr.get("title"),
                "body": (pr.get("body") or "")[:4000],
                "author": pr["user"]["login"],
                "merged_at": pr.get("merged_at"),
                "base_ref": pr["base"]["ref"],
                "base_sha": pr["base"]["sha"],
                "head_sha": pr["head"]["sha"],
                "review_sha": commit_ids[0] if commit_ids else pr["head"]["sha"],
                "changed_files": pr.get("changed_files"),
                "additions": pr.get("additions"),
                "deletions": pr.get("deletions"),
                "findings": findings,
            }
        )
        print(f"PR {pr_num}: {len(findings)} bot findings ({pr.get('title', '')[:60]})", file=sys.stderr)
        if max_prs and len(entries) >= max_prs:
            break

    with out_path.open("w") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")
    n_findings = sum(len(e["findings"]) for e in entries)
    print(f"wrote {len(entries)} PRs, {n_findings} findings -> {out_path}", file=sys.stderr)


FP_LABEL_SYSTEM = """You label code-review-bot findings using the developer interactions that \
followed. Given a bot's review comment and the humans' replies/reactions, classify the TEAM'S \
RECEPTION of the finding.

Categories:
- "valid_fixed": a human agreed / said done / the thread shows the issue was fixed.
- "valid_acknowledged": humans agreed it is real but chose not to fix (or deferred).
- "false_positive": a human explicitly or implicitly disputed correctness (said it's intentional, \
not an issue, wrong, doesn't apply, works as designed) OR thumbs-down reaction with no agreement.
- "unaddressed": no human engagement at all (no replies, no reactions).
- "unclear": engagement exists but reception cannot be determined.

Output ONLY JSON: {"labels": [{"id": <finding id>, "label": str, "confidence": 0.0-1.0, "evidence": "short quote or 'none'"}]}
"""


def label_fps(batch_size: int = 25, workers: int = 8) -> None:
    """LLM-label each finding's reception from dev interactions."""
    from concurrent.futures import ThreadPoolExecutor

    llm = LLMClient.from_env()
    ds_path = DATA_DIR / "dataset.jsonl"
    entries = [json.loads(l) for l in ds_path.read_text().splitlines()]
    todo = []
    for e in entries:
        for f in e["findings"]:
            if "reception" not in f:
                todo.append((e, f))
    print(f"labeling {len(todo)} findings", file=sys.stderr)
    chunks = [todo[i : i + batch_size] for i in range(0, len(todo), batch_size)]

    def do_chunk(chunk):
        blocks = []
        for e, f in chunk:
            thread_txt = "\n".join(
                f"  - {'BOT' if r['is_bot'] else 'HUMAN'} {r['user']}: {r['body'][:800]}" for r in f["thread"]
            ) or "  (no replies)"
            reactions = json.dumps(f.get("reactions") or {})
            blocks.append(
                f"### Finding id={f['id']} (bot={f['bot']}, PR #{e['pr']}: {e['title']})\n"
                f"Comment on {f['path']}:{f['line']}:\n{f['body'][:1500]}\n"
                f"Reactions: {reactions}\nReplies:\n{thread_txt}"
            )
        try:
            data = llm.chat_json(
                [
                    {"role": "system", "content": FP_LABEL_SYSTEM},
                    {"role": "user", "content": "\n\n".join(blocks)},
                ],
                reasoning_effort="medium",
            )
            return {l["id"]: l for l in data.get("labels", [])}
        except Exception as ex:  # noqa: BLE001
            print(f"label batch failed: {ex}", file=sys.stderr)
            return {}

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for chunk, labels in zip(chunks, pool.map(do_chunk, chunks)):
            for e, f in chunk:
                lab = labels.get(f["id"])
                if lab:
                    f["reception"] = lab.get("label", "unclear")
                    f["reception_confidence"] = lab.get("confidence", 0.5)
                    f["reception_evidence"] = lab.get("evidence", "")
                else:
                    f["reception"] = "unclear"
                    f["reception_confidence"] = 0.0
            done += len(chunk)
            print(f"labeled {done}/{len(todo)}", file=sys.stderr, flush=True)

    with ds_path.open("w") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")
    from collections import Counter

    print(Counter(f["reception"] for e in entries for f in e["findings"]), file=sys.stderr)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="pingdotgg/t3code")
    ap.add_argument("--max-prs", type=int, default=None)
    ap.add_argument("--label-fps", action="store_true")
    args = ap.parse_args()
    if args.label_fps:
        label_fps()
    else:
        collect(args.repo, args.max_prs)
