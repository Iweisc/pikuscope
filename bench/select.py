"""Select the benchmark PR sets.

- crgr: every PR with coderabbit/greptile findings (the parity targets), size-capped.
- aux: cursor/macroscope PRs rich in valid_fixed / false_positive labels, for FP-catch
  and recall statistics, size-capped.
Prints comma-separated PR lists.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data" / "dataset.jsonl"

MAX_FILES = 60
AUX_MAX_FILES = 25
AUX_COUNT = 40


def main() -> None:
    entries = [json.loads(l) for l in DATA.read_text().splitlines()]
    crgr = []
    aux_scored = []
    for e in entries:
        bots = {f["bot"] for f in e["findings"]}
        nf = e.get("changed_files") or 999
        if ("coderabbit" in bots or "greptile" in bots) and nf <= MAX_FILES:
            crgr.append(e["pr"])
        elif nf <= AUX_MAX_FILES:
            n_fp = sum(1 for f in e["findings"] if f.get("reception") == "false_positive")
            n_valid = sum(1 for f in e["findings"] if f.get("reception") in ("valid_fixed", "valid_acknowledged"))
            if n_fp or n_valid:
                # prioritize FP-rich, then valid-rich, prefer small PRs
                aux_scored.append((-(n_fp * 3 + n_valid), nf, e["pr"]))
    aux_scored.sort()
    aux = [pr for _, _, pr in aux_scored[:AUX_COUNT]]
    print("CRGR=" + ",".join(map(str, sorted(crgr))))
    print("AUX=" + ",".join(map(str, sorted(aux))))
    print(f"# crgr={len(crgr)} aux={len(aux)}", file=sys.stderr)


if __name__ == "__main__":
    main()
