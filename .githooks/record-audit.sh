#!/bin/sh
# Record an operator assertion that an agent reviewed the work about to be
# pushed. This is local evidence, not proof of reviewer identity or coverage.
#
#   .githooks/record-audit.sh <auditor> '<what it found>'
#
# e.g.  .githooks/record-audit.sh 'Claude Opus 5' 'no findings'
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
# What this is: a discipline. Anything with a shell can write a receipt, and
# nothing here can tell an author from an auditor. `pre-push` does not refuse a
# push over what it finds — it prints the coverage as a checklist and lets Tyrel
# judge it. What that catches is the unconsidered push: the one where nobody
# meant to skip the review, it just never happened, and now the empty boxes are
# in front of him at the moment he pushes.

set -eu

auditor=${1-}
finding=${2-}

if [ -z "$auditor" ] || [ -z "$finding" ]; then
  echo "usage: .githooks/record-audit.sh <auditor> '<what it found>'" >&2
  echo "" >&2
  echo "  Both are required. An audit with no finding recorded is not an" >&2
  echo "  audit — 'no findings' is itself a finding and should be written." >&2
  exit 1
fi

# The receipt is line-oriented and pre-push counts `auditor:` records. Without
# this check one accidental newline in either free-text argument can create a
# second record, inflate the reviewer count, or corrupt the receipt's shape.
case "$auditor$finding" in
  *"
"*|*""*)
    echo "auditor and finding must each be one line; no receipt was written." >&2
    exit 1 ;;
esac

# Receipts are files too. Scan both free-text fields before locating or creating
# their destination, so a pasted credential cannot persist under .git where the
# staged, worktree, and history scans would never see it. The NUL-delimited
# protocol makes a truncated producer a malformed input rather than an
# innocent-looking partial scan.
ingress="$(dirname "$0")/check_ingress.py"
if [ ! -f "$ingress" ]; then
  echo "  Cannot check the receipt text because $ingress is missing." >&2
  echo "  No receipt was written." >&2
  exit 1
fi
if ! printf '%s\0%s\0' "$auditor" "$finding" |
     python3 "$ingress" --audit-fields >/dev/null; then
  echo "  Audit receipt text failed the credential check; no receipt was written." >&2
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

# Serialize writers for this commit. Two reviewers can finish together; without
# an atomic lock both can observe a missing receipt, both create it, and the
# second truncation can erase the first finding before either reports success.
lock="$receipts/$sha.lock"
if ! mkdir "$lock" 2>/dev/null; then
  echo "  The audit lock already exists for ${sha%"${sha#????????}"}." >&2
  echo "  Another writer may be active, or an earlier writer may have stopped." >&2
  echo "  No receipt was written. Verify no recorder is active, then remove" >&2
  echo "  this exact stale lock and retry: $lock" >&2
  exit 1
fi
temporary=""
cleanup_lock() {
  if [ -n "$temporary" ] && [ -f "$temporary" ]; then
    rm -f "$temporary"
  fi
  rmdir "$lock" 2>/dev/null || true
}
trap cleanup_lock EXIT
trap 'exit 1' HUP INT TERM

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

# Build the entire next receipt beside the old one, validate it, then replace
# the old file atomically. Appending three lines in place can leave only the
# `auditor:` line after a crash; pre-push must never count that as a completed
# review. The input text was credential-scanned before any temporary file is
# created.
if ! temporary=$(mktemp "$receipts/$sha.tmp.XXXXXX"); then
  echo "  Could not create a temporary receipt; no receipt was changed." >&2
  exit 1
fi
if [ -f "$receipts/$sha" ]; then
  if ! cp "$receipts/$sha" "$temporary"; then
    echo "  Could not copy the existing receipt; it was not changed." >&2
    exit 1
  fi
else
  {
    printf 'commit:  %s\n' "$sha"
    printf 'branch:  %s\n' "$branch"
    printf 'audited: %s (%s commit(s))\n' "$range" "$count"
  } > "$temporary"
fi

{
  printf '\nauditor: %s\n' "$auditor"
  printf 'when:    %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  printf 'finding: %s\n' "$finding"
} >> "$temporary"

if ! python3 "$ingress" --audit-receipt "$temporary" >/dev/null; then
  echo "  The complete receipt failed validation; the old receipt was not changed." >&2
  exit 1
fi
if ! mv "$temporary" "$receipts/$sha"; then
  echo "  Could not install the complete receipt; the old receipt was not changed." >&2
  exit 1
fi
temporary=""

recorded=$(grep -c '^auditor:' "$receipts/$sha")
printf '%s recorded for %s. %s reviewer(s) on record.\n' \
  "$auditor" "$(echo "$sha" | cut -c1-8)" "$recorded"
printf 'It covers %s — %s commit(s) — and that state only.\n' "$range" "$count"
