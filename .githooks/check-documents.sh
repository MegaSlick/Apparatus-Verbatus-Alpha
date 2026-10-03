#!/bin/sh
# The core documents exist and carry no dated state; dated notes belong in workbench/.
# The separation inventory classifies every tracked path.
set -u

root=$(git rev-parse --show-toplevel 2>/dev/null) || {
  echo "check-documents: not inside a Git repository." >&2
  exit 2
}
cd "$root" || exit 2

documents="README.md PRINCIPLES.md ARCHITECTURE.md GLOSSARY.md CONTRIBUTING.md AGENTS.md CLAUDE.md"

failed=0
for file in $documents; do
  if [ ! -f "$file" ]; then
    echo "missing core document: $file" >&2
    failed=1
    continue
  fi
  # Print only line numbers and dates, never the line: this runs before the credential
  # scan in CI, and a whole line could carry a secret.
  dates=$(grep -nEo '20[0-9]{2}-[0-9]{2}-[0-9]{2}' "$file")
  case $? in
    0)
      printf '%s\n' "$dates"
      echo "$file carries a date; dated state belongs in workbench/." >&2
      failed=1
      ;;
    1) ;;
    *)
      echo "could not read $file." >&2
      failed=1
      ;;
  esac
done

# Paths with control characters could split one record into two for later checks.
python3 .githooks/check_ingress.py --paths || failed=1

# Every tracked path has a row in the separation inventory, and every row still matches
# a tracked path. A row ending in `/` covers everything under it.
separation=docs/SEPARATION.md
if [ ! -f "$separation" ]; then
  echo "missing separation inventory: $separation" >&2
  failed=1
elif ! tracked=$(git -c core.quotepath=off ls-files); then
  echo "could not list tracked paths." >&2
  failed=1
elif ! printf '%s\n' "$tracked" | awk -v table="$separation" '
  BEGIN {
    row = "^[|] `[^`]+` [|] (PRODUCT|HARNESS|HISTORY|PRIVATE|AMBIGUOUS) [|]"
    while ((status = (getline line < table)) > 0) {
      if (line ~ row) { split(line, part, "`"); entry[part[2]] = 1 }
      else if (line ~ /^[|] `/) { print "separation row has no known class: " line > "/dev/stderr"; bad = 1 }
    }
    if (status < 0) { print "could not read " table > "/dev/stderr"; bad = 1 }
  }
  $0 == "" { next }
  {
    covered = 0
    for (e in entry)
      if ($0 == e || (e ~ /\/$/ && index($0, e) == 1)) { covered = 1; used[e] = 1 }
    if (covered) next
    # Name the shortest prefix of the path that no row reaches, once.
    n = split($0, piece, "/"); key = ""
    for (i = 1; i <= n; i++) {
      key = key piece[i] (i < n ? "/" : "")
      reached = 0
      for (e in entry) if (index(e, key) == 1) reached = 1
      if (!reached) break
    }
    if (!(key in told)) { told[key] = 1; print "unclassified path: " key > "/dev/stderr" }
    bad = 1
  }
  END {
    for (e in entry)
      if (!(e in used)) { print "separation row matches no tracked path: " e > "/dev/stderr"; bad = 1 }
    exit bad
  }'; then
  echo "$separation must give every tracked path a row, and every row a tracked path." >&2
  failed=1
fi

[ "$failed" -eq 0 ] && echo "Document check passed."
exit "$failed"
