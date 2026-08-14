"""Post-process dataset.jsonl: extract bot severity markers, flag CI-dependent findings."""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data" / "dataset.jsonl"

SEV_RE = re.compile(r"(🔴|🟠|🟡|⚪)\s*(Critical|Major|Minor|Trivial|Nitpick)", re.IGNORECASE)
KIND_RE = re.compile(r"_(⚠️ Potential issue|🛠️ Refactor suggestion|🧹 Nitpick(?: \(assertive\))?|💡 Verification agent|📝 Committable suggestion)_")
CI_RE = re.compile(r"(formatter failure in ci|ci reports|pipeline failure|check run|build failure|lint failure)", re.IGNORECASE)


def enrich() -> None:
    entries = [json.loads(l) for l in DATA.read_text().splitlines()]
    sev_counts: Counter = Counter()
    kind_counts: Counter = Counter()
    for e in entries:
        for f in e["findings"]:
            body = f.get("body", "")
            m = SEV_RE.search(body)
            f["bot_severity"] = m.group(2).lower() if m else None
            k = KIND_RE.search(body)
            if k:
                marker = k.group(1)
                if "Potential issue" in marker:
                    f["kind"] = "actionable"
                elif "Refactor" in marker:
                    f["kind"] = "refactor"
                elif "Nitpick" in marker:
                    f["kind"] = "nitpick"
                elif "Verification" in marker:
                    f["kind"] = "verification"
            f["ci_dependent"] = bool(CI_RE.search(body))
            sev_counts[f"{f['bot']}:{f.get('bot_severity')}"] += 1
            kind_counts[f"{f['bot']}:{f.get('kind')}"] += 1
    with DATA.open("w") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")
    print("severities:", dict(sev_counts), file=sys.stderr)
    print("kinds:", dict(kind_counts), file=sys.stderr)


if __name__ == "__main__":
    enrich()
