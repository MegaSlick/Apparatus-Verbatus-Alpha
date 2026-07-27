#!/bin/sh
# Record that an agent has read the work about to be pushed.
#
#   .githooks/record-audit.sh <auditor> '<what it found>'
#
# e.g.  .githooks/record-audit.sh opus 'verified all four routes; no findings'
#
# The receipt names exactly one commit — HEAD as it stands now. Add a commit or
# amend one and the receipt no longer applies and the push is refused again.
# That is deliberate: an audit is of a state, not of a branch.
#
# It also writes down the range the auditor was meant to read. Nothing checks
# that they did — pre-push only verifies the receipt names the right commit —
# but a receipt saying "6 commits" next to a finding about one of them is at
# least visible afterwards.
#
# Receipts live in the git common directory, shared by every worktree, and are
# never committed. A receipt is evidence about a working session, not a
# document, and by this repository's own rule that keeps it out of the tree.
#
# What this is: a discipline. Anything with a shell can write a receipt or set
# ALLOW_UNAUDITED_PUSH=1, and nothing here can tell an author from an auditor.
# What it stops is the unconsidered push — the one where nobody meant to skip
# the review, it just never happened.

set -e

auditor=$1
finding=$2

if [ -z "$auditor" ] || [ -z "$finding" ]; then
  echo "usage: .githooks/record-audit.sh <auditor> '<what it found>'" >&2
  echo "" >&2
  echo "  Both are required. An audit with no finding recorded is not an" >&2
  echo "  audit — 'no findings' is itself a finding and should be written." >&2
  exit 1
fi

common=$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || common=""
if [ -z "$common" ]; then
  # Older git has no --path-format. Guard the inner command, not the cd:
  # `cd ""` succeeds and stays put, so the fallback would quietly return $PWD
  # and a receipt would be written into the working tree.
  relative=$(git rev-parse --git-common-dir 2>/dev/null) || relative=""
  if [ -n "$relative" ]; then
    common=$(cd "$relative" 2>/dev/null && pwd) || common=""
  fi
fi
if [ -z "$common" ]; then
  echo "  Cannot locate the git directory, so no receipt can be written." >&2
  echo "  Without one, pre-push will refuse the push — which is correct." >&2
  exit 1
fi

receipts="$common/audit-receipts"
if ! mkdir -p "$receipts" 2>/dev/null; then
  echo "  Cannot create $receipts, so no receipt was written." >&2
  echo "  The push will stay blocked until this is fixed." >&2
  exit 1
fi

sha=$(git rev-parse HEAD)
branch=$(git rev-parse --abbrev-ref HEAD)

# What should have been read: everything this branch adds to main.
base=$(git merge-base origin/main HEAD 2>/dev/null || echo "")
if [ -n "$base" ] && [ "$base" != "$sha" ]; then
  range="$base..$sha"
  count=$(git rev-list --count "$range" 2>/dev/null || echo "?")
elif [ -z "$base" ]; then
  # Reporting "1 commit" here would be a measurement nobody made — a fresh
  # clone with no origin/main would understate a whole branch. Say so instead.
  range="unknown (origin/main did not resolve — fetch first)"
  count="?"
else
  range="$sha"
  count=1
fi

# printf, not echo: /bin/sh is dash on Linux and bash-with-xpg_echo on macOS,
# and both expand backslashes in echo. A finding must be recorded as written.
# The header is written once; every reviewer is appended. Overwriting would
# mean three reviewers produced one surviving finding and the other two
# vanished without a word — which is the thing this repository refuses to do.
if [ ! -f "$receipts/$sha" ]; then
  {
    printf 'commit:  %s\n' "$sha"
    printf 'branch:  %s\n' "$branch"
    printf 'audited: %s (%s commit(s))\n' "$range" "$count"
  } > "$receipts/$sha"
fi

{
  printf '\nauditor: %s\n' "$auditor"
  printf 'when:    %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  printf 'finding: %s\n' "$finding"
} >> "$receipts/$sha"

recorded=$(grep -c '^auditor:' "$receipts/$sha")
printf '%s recorded for %s. %s reviewer(s) on record.\n' \
  "$auditor" "$(echo "$sha" | cut -c1-8)" "$recorded"
printf 'It covers %s — %s commit(s) — and that state only.\n' "$range" "$count"
