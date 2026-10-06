#!/bin/sh
# Prints the Python the repository's checks run under: the checkout's .venv first, then a
# python3 on PATH, whichever is 3.11 or later (the checks need tomllib). With neither it
# says how to fix that and exits 2, so no caller reads it as a finding of its own.

root=$(git rev-parse --show-toplevel 2>/dev/null) || root=.
for candidate in "$root/.venv/bin/python" python3; do
  case $candidate in
    /*) [ -x "$candidate" ] || continue ;;
    *) command -v "$candidate" >/dev/null 2>&1 || continue ;;
  esac
  if "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    printf '%s\n' "$candidate"
    exit 0
  fi
done
{
  echo ""
  echo "  BLOCKED: no Python 3.11 or later was found for the repository's checks."
  echo "  This checkout has no usable .venv, and python3 on PATH is missing or older"
  echo "  (macOS's own is 3.9). Nothing was checked, so nothing is claimed either way."
  echo "  Fix: run  uv sync --frozen --group test --group audit  in the checkout,"
  echo "  which creates .venv, then try again."
  echo ""
} >&2
exit 2
