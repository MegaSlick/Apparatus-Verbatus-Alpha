#!/bin/sh
# Validate the repository's durable-document contract.

set -u

if ! root=$(git rev-parse --show-toplevel 2>/dev/null); then
  echo "Document check must run inside a Git repository." >&2
  exit 2
fi
cd "$root" || exit 2

missing=0
for file in README.md GOALS.md GOVERNANCE.md ARCHITECTURE.md GLOSSARY.md CLAUDE.md; do
  if [ ! -f "$file" ]; then
    echo "missing canonical document: $file" >&2
    missing=1
  fi
done
[ "$missing" -eq 0 ] || exit 1

dated=0
for file in GOALS.md GOVERNANCE.md ARCHITECTURE.md GLOSSARY.md CLAUDE.md; do
  date_lines=$(grep -nE '20[0-9]{2}-[0-9]{2}-[0-9]{2}' "$file")
  grep_status=$?
  if [ "$grep_status" -eq 0 ]; then
    printf '%s\n' "$date_lines"
    echo "dated state belongs in README.md, not $file" >&2
    dated=1
  elif [ "$grep_status" -ne 1 ]; then
    echo "could not inspect $file for dated state." >&2
    exit 2
  fi
done
[ "$dated" -eq 0 ] || exit 1

allowlist=.githooks/doc-allowlist.sh
if [ ! -f "$allowlist" ]; then
  echo "$allowlist is missing; documentation cannot be checked." >&2
  exit 2
fi

if ! repository_files=$(git -c core.quotePath=false ls-files --cached --others \
  --exclude-standard); then
  echo "could not list repository files; documentation cannot be checked." >&2
  exit 2
fi

stray=$(printf '%s\n' "$repository_files" | sh "$allowlist")
status=$?
if [ "$status" -ne 0 ] && [ "$status" -ne 1 ]; then
  echo "documentation allowlist failed with exit $status." >&2
  exit 2
fi
if [ -n "$stray" ]; then
  echo "stray documentation; notes belong in ignored workbench/:" >&2
  printf '%s\n' "$stray" | while IFS= read -r file; do
    printf '  %s\n' "$file" >&2
  done
  exit 1
fi

echo "Document check passed."
