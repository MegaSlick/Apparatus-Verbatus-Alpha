#!/bin/sh
# Everyday gate: current-tree ingress, static checks, and focused test outcomes.

set -eu
root=$(git rev-parse --show-toplevel 2>/dev/null) ||
  { echo "check-fast: not inside a Git repository" >&2; exit 1; }
cd "$root"

# Use the project environment when it exists.
python=python3
if [ -x .venv/bin/python ]; then
  PATH="$root/.venv/bin:$PATH"
  export PATH
  python=.venv/bin/python
fi

python3 .githooks/check_ingress.py --staged
python3 .githooks/check_ingress.py --worktree
sh .githooks/check-static.sh
# `scanner` keeps the credential-scanner tests in the quick run.
"$python" -m pytest -m "not full or scanner"
