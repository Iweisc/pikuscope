#!/usr/bin/env bash
# One full benchmark iteration: run -> score -> verify-novel -> analyze.
# Usage: bench/iterate.sh <run-name> [pr-list] [limit]
set -euo pipefail
cd "$(dirname "$0")/.."
RUN="${1:?run name}"
PRS="${2:-}"
LIMIT="${3:-}"
PY=.venv/bin/python

ARGS=(--run-name "$RUN")
[ -n "$PRS" ] && ARGS+=(--prs "$PRS")
[ -n "$LIMIT" ] && ARGS+=(--limit "$LIMIT")

echo "=== run ==="
$PY bench/run.py "${ARGS[@]}"
echo "=== score ==="
$PY bench/score.py --run-name "$RUN"
echo "=== verify novel ==="
$PY bench/verify_novel.py --run-name "$RUN"
echo "=== analyze misses ==="
$PY bench/analyze.py --run-name "$RUN" || true
echo "=== done: bench/runs/$RUN/score.json ==="
