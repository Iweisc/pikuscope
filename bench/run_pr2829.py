"""Blind review of PR 2829's head-round scope (10 files) — one-off harness."""

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pikuscope.config import Config
from pikuscope.context import ensure_checkout
from pikuscope.llm import LLMClient
from pikuscope.review import Reviewer

HEAD = "993407dd9e57f1edf2f5681d70140bfefeca93cc"

llm = LLMClient.from_env()
ctx = ensure_checkout(ROOT / "bench/repos/t3code", "pingdotgg/t3code", HEAD, pr_number=2829)
diff_text = Path("/tmp/pr2829_scoped.diff").read_text()
learnings = [json.loads(l)["text"] for l in (ROOT / "bench/data/learnings.jsonl").read_text().splitlines()]

pr_meta = {
    "title": "feat(orchestrator): introduce new orchestrator",
    "body": "Introduces the orchestration-v2 subsystem: event-sourced orchestrator, provider "
            "adapters (Claude/Codex/Cursor/OpenCode/ACP), projections, context handoff, MCP service, "
            "replay testkit. (Scoped review of the adapter/service files.)",
    "number": 2829,
    "base": {"ref": "main"},
    "head": {"sha": HEAD},
}
cfg = Config()
reviewer = Reviewer(llm, ctx, cfg)
t0 = time.time()
rr = reviewer.review(pr_meta, diff_text, learnings=learnings,
                     progress=lambda m: print(f"[pikuscope] {m}", flush=True))
out = {
    "pr": 2829, "review_sha": HEAD,
    "findings": [f.to_dict() for f in rr.findings],
    "dropped": [f.to_dict() for f in rr.dropped],
    "summary": rr.summary,
    "usage": {"calls": llm.usage.calls, "prompt_tokens": llm.usage.prompt_tokens,
              "completion_tokens": llm.usage.completion_tokens},
    "wall_s": round(time.time() - t0),
}
outdir = ROOT / "bench/runs/pr2829-blind"
outdir.mkdir(parents=True, exist_ok=True)
(outdir / "pr-2829.json").write_text(json.dumps(out, indent=2))
print(f"done in {out['wall_s']}s: {len(rr.findings)} findings, {len(rr.dropped)} dropped")
