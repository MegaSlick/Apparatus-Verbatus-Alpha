#!/bin/sh
# Record that an agent has read the commit about to be pushed.
#
#   .githooks/record-audit.sh <auditor> '<what it found>'
#
# e.g.  .githooks/record-audit.sh opus 'no findings; verified all four routes'
#
# The receipt names exactly one commit — HEAD as it stands right now. Amend it,
# or add another commit, and the receipt no longer applies and the push is
# refused again. That is deliberate: an audit is of a specific state, not of a
# branch in general.
#
# Receipts live in the git common directory. They are never committed. They are
# evidence about a working session, not a document, and by the repository's own
# rule that means they do not belong in the tree.
#
# This is a discipline, not a vault. Anything with a shell can write a receipt
# or set ALLOW_UNAUDITED_PUSH=1. What it stops is the unconsidered push — the
# one where nobody ever meant to skip the review, it just never happened.

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

sha=$(git rev-parse HEAD)
receipts="$(git rev-parse --path-format=absolute --git-common-dir)/audit-receipts"
mkdir -p "$receipts"

{
  echo "commit:  $sha"
  echo "branch:  $(git rev-parse --abbrev-ref HEAD)"
  echo "auditor: $auditor"
  echo "when:    $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  echo "finding: $finding"
} > "$receipts/$sha"

echo "Audit recorded for $(echo "$sha" | cut -c1-8) by $auditor."
echo "This receipt covers that commit only."
