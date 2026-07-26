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
# It also records the range the auditor was supposed to have read, so a receipt
# on the tip cannot quietly stand in for a glance at the last commit while five
# unread ones travel with it.
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
  # Older git has no --path-format. Resolve the relative answer by hand rather
  # than letting an empty value silently become the filesystem root.
  common=$(cd "$(git rev-parse --git-common-dir)" 2>/dev/null && pwd) || common=""
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
else
  range="$sha"
  count=1
fi

# printf, not echo: /bin/sh is dash on Linux and bash-with-xpg_echo on macOS,
# and both expand backslashes in echo. A finding must be recorded as written.
{
  printf 'commit:  %s\n' "$sha"
  printf 'branch:  %s\n' "$branch"
  printf 'audited: %s (%s commit(s))\n' "$range" "$count"
  printf 'auditor: %s\n' "$auditor"
  printf 'when:    %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  printf 'finding: %s\n' "$finding"
} > "$receipts/$sha"

printf 'Audit recorded for %s by %s.\n' "$(echo "$sha" | cut -c1-8)" "$auditor"
printf 'It covers %s — %s commit(s) — and that state only.\n' "$range" "$count"
