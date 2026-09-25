#!/bin/sh
# Shared deterministic checks that do not scan history or run the test suite.

set -eu
root=$(git rev-parse --show-toplevel 2>/dev/null) ||
  { echo "check-static: not inside a Git repository" >&2; exit 1; }
cd "$root"

if [ -x .venv/bin/ruff ]; then
  PATH="$root/.venv/bin:$PATH"
  export PATH
fi

sh .githooks/check-documents.sh
git diff --check HEAD --
ruff check .
ruff format --check .

scripts=".githooks/check-all.sh
.githooks/check-documents.sh
.githooks/check-fast.sh
.githooks/check-static.sh
.githooks/commit-msg
.githooks/install.sh
.githooks/pre-commit
.githooks/pre-merge-commit
operations/notify/notify.sh"

# Repository ingress rejects control characters in paths, so this intentional
# word split cannot turn one tracked path into several accepted paths.
# shellcheck disable=SC2086
shellcheck $scripts
# `sh -n` parses only its first operand, so walk the list. Prefer dash: macOS /bin/sh
# is bash in POSIX mode and accepts bashisms CI's dash refuses. shellcheck catches the
# ones no `-n` parse sees (`[[`, `${x^^}`).
syntax_shell="sh"
if command -v dash >/dev/null 2>&1; then
  syntax_shell="dash"
fi
for script in $scripts; do
  "$syntax_shell" -n "$script"
done
