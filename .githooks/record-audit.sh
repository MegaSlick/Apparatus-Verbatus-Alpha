#!/bin/sh
# Record a local assertion that one reviewer read one exact commit.
#
#   .githooks/record-audit.sh [--commit <rev>] <reviewer> <finding>
#
# This is a checklist receipt, not proof of identity or review quality.

set -eu

usage() {
  echo "usage: .githooks/record-audit.sh [--commit <rev>] <reviewer> <finding>" >&2
  exit 1
}

reviewed=HEAD
case ${1:-} in
  --commit)
    [ "$#" -ge 2 ] || usage
    reviewed=$2
    shift 2 ;;
  --commit=*)
    reviewed=${1#--commit=}
    shift ;;
  -*) usage ;;
esac
[ "$#" -eq 2 ] || usage
reviewer=$1
finding=$2
[ -n "$reviewer" ] && [ -n "$finding" ] || usage

case "$reviewer$finding" in
  *"
"*|*""*)
    echo "record-audit: reviewer and finding must each be one line" >&2
    exit 1 ;;
esac

ingress="$(dirname "$0")/check_ingress.py"
[ -f "$ingress" ] ||
  { echo "record-audit: $ingress is missing" >&2; exit 1; }
printf '%s\0%s\0' "$reviewer" "$finding" |
  python3 "$ingress" --audit-fields >/dev/null ||
  { echo "record-audit: receipt text failed ingress scanning" >&2; exit 1; }

sha=$(git rev-parse --verify --quiet "$reviewed^{commit}" 2>/dev/null) || sha=""
[ -n "$sha" ] ||
  { echo "record-audit: '$reviewed' does not name a commit" >&2; exit 1; }
common=$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null) ||
  common=""
case $common in
  /*) : ;;
  *) echo "record-audit: cannot locate the git common directory" >&2; exit 1 ;;
esac

receipts="$common/audit-receipts"
mkdir -p "$receipts" ||
  { echo "record-audit: cannot create $receipts" >&2; exit 1; }
receipt="$receipts/$sha"
lock="$receipts/$sha.lock"
if ! mkdir "$lock" 2>/dev/null; then
  echo "record-audit: another writer is recording this commit; try again" >&2
  exit 1
fi
temporary=""
cleanup() {
  [ -z "$temporary" ] || rm -f "$temporary"
  rmdir "$lock" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

if [ -e "$receipt" ] || [ -L "$receipt" ]; then
  if ! [ -f "$receipt" ] || ! [ -r "$receipt" ] ||
    ! python3 "$ingress" --audit-receipt "$receipt" >/dev/null ||
    ! grep -Fqx "commit:  $sha" "$receipt"; then
    echo "record-audit: existing receipt is invalid; left unchanged" >&2
    exit 1
  fi
fi

umask 077
temporary=$(mktemp "$receipts/$sha.tmp.XXXXXX") ||
  { echo "record-audit: cannot create a temporary receipt" >&2; exit 1; }

if [ -f "$receipt" ]; then
  cp "$receipt" "$temporary"
else
  branch=$(git symbolic-ref --quiet --short HEAD 2>/dev/null || echo "(detached)")
  {
    printf 'commit:  %s\n' "$sha"
    printf 'branch:  %s\n' "$branch"
    printf 'audited: exact commit %s\n' "$sha"
  } >"$temporary"
fi
{
  printf '\nauditor: %s\n' "$reviewer"
  printf 'when:    %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  printf 'finding: %s\n' "$finding"
} >>"$temporary"

python3 "$ingress" --audit-receipt "$temporary" >/dev/null ||
  { echo "record-audit: complete receipt failed validation" >&2; exit 1; }
count=$(grep -c '^auditor: ' "$temporary")
mv "$temporary" "$receipt"
temporary=""
short=$(printf '%s' "$sha" | cut -c1-8)
echo "$reviewer recorded for $short ($count reviewer(s) total)."
