#!/bin/sh
# Check the mechanically decidable parts of the durable-document contract.
#
# The ISO-date alarm below does not prove that prose contains no narrative
# dates, hashes, or status claims. Those concepts cannot be recognized safely
# by a broad text pattern; they remain review obligations.

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

# **README.md is in this list now, by Tyrel's ruling.** It used to be the one canonical
# document a date was allowed in, because status lived there and status is dated by
# nature. That exemption is what let the status line sit a full day behind the thing it
# described, in the document that calls itself the only place status lives — a claim
# nothing could contradict, because nothing was checking. An undated status line can only
# be wrong about its substance, and substance is what a reader notices.
#
# Dated state belongs in `history/` and in the standing ledgers under `workbench/`, which
# are read as records rather than as instructions. Provenance — when a ruling was made,
# when something was measured — lives in the documents that carry procedure, and none of
# those six is scanned for it here.
dated=0
for file in README.md GOALS.md GOVERNANCE.md ARCHITECTURE.md GLOSSARY.md CLAUDE.md; do
  # Print only the line number and matched date. This check runs before the
  # credential scanner in CI, so echoing the whole source line could expose a
  # credential that the next step correctly refuses.
  date_lines=$(grep -nEo '20[0-9]{2}-[0-9]{2}-[0-9]{2}' "$file")
  grep_status=$?
  if [ "$grep_status" -eq 0 ]; then
    printf '%s\n' "$date_lines"
    echo "dated state belongs in history/ or workbench/, not $file" >&2
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

# The allowlist protocol is newline-delimited. Refuse control characters in
# repository paths before using it, so one filename cannot split into two
# innocent-looking records and pass. The ingress scanner enforces the same
# precondition on staged, working and historical trees.
if ! python3 .githooks/check_ingress.py --paths; then
  echo "repository paths are unsafe to pass to the documentation allowlist." >&2
  exit 1
fi

if ! listed_files=$(git -c core.quotePath=false ls-files --cached --others \
  --exclude-standard); then
  echo "could not list repository files; documentation cannot be checked." >&2
  exit 2
fi

# Reflect the current working tree without hiding a staged addition. `ls-files
# --cached` includes a tracked file deleted only from the worktree; checking
# that stale path makes a removal fail until it is staged. Conversely, an
# absent staged addition is still what the index would commit and must remain
# visible here. A path that exists in HEAD is an unstaged deletion; a path that
# does not is an index addition and is retained for the allowlist.
repository_files=$(
  printf '%s\n' "$listed_files" |
    while IFS= read -r file; do
      [ -n "$file" ] || continue
      if [ -e "$file" ] || [ -L "$file" ] || \
         ! git cat-file -e "HEAD:$file" 2>/dev/null; then
        printf '%s\n' "$file"
      fi
    done
)

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
