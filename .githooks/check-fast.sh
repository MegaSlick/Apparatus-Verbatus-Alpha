#!/bin/sh
# Everyday gate: current-tree ingress, static checks, and focused test outcomes.

set -eu
root=$(git rev-parse --show-toplevel 2>/dev/null) ||
  { echo "check-fast: not inside a Git repository" >&2; exit 1; }
cd "$root"

python3 .githooks/check_ingress.py --staged
python3 .githooks/check_ingress.py --worktree
sh .githooks/check-static.sh
python3 -m pytest -m "not full"
