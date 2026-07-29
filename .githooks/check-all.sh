#!/bin/sh
# Full local/CI gate. CI supplies its own ref-aware history scan once.

set -eu
root=$(git rev-parse --show-toplevel 2>/dev/null) ||
  { echo "check-all: not inside a Git repository" >&2; exit 1; }
cd "$root"

mode=local
if [ "${1:-}" = "--ci" ] && [ "$#" -eq 1 ]; then
  mode=ci
elif [ "$#" -ne 0 ]; then
  echo "usage: sh .githooks/check-all.sh [--ci]" >&2
  exit 2
fi

sh .githooks/check-static.sh

if [ "$mode" = local ]; then
  python3 .githooks/check_ingress.py --history HEAD
  python3 .githooks/check_ingress.py --staged
  python3 .githooks/check_ingress.py --worktree
fi

python3 -m pytest
