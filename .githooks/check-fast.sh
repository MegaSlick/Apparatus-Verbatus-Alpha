#!/bin/sh
# Everyday gate: current-tree ingress, static checks, and focused test outcomes.

set -eu
root=$(git rev-parse --show-toplevel 2>/dev/null) ||
  { echo "check-fast: not inside a Git repository" >&2; exit 1; }
cd "$root"

python3 .githooks/check_ingress.py --staged
python3 .githooks/check_ingress.py --worktree
sh .githooks/check-static.sh
# `scanner` re-includes the credential-scanner cases that `full` would deselect.
# Running the scanner on every commit without ever testing it is how an edit to
# it goes green here and fails only in CI. One invocation, not two: pytest's
# marker language composes, and a second run would pay another interpreter
# start and another collection pass for the same set.
python3 -m pytest -m "not full or scanner"
