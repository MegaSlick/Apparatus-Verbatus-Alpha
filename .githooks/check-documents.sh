#!/bin/sh
# The core documents exist and carry no dated state; dated notes belong in workbench/.
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

[ "$failed" -eq 0 ] && echo "Document check passed."
exit "$failed"
