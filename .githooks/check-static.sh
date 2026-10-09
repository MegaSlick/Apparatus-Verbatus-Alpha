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
# Whitespace errors in every tracked file as it stands in the working tree, so a clean
# CI checkout checks the committed content and a local run checks edits as well.
git diff --check "$(git hash-object -t tree /dev/null)" --
ruff check .
ruff format --check .

scripts=".githooks/check-all.sh
.githooks/check-documents.sh
.githooks/check-fast.sh
.githooks/check-static.sh
.githooks/find-python.sh
.githooks/commit-msg
.githooks/install.sh
.githooks/pre-commit
.githooks/pre-merge-commit
.githooks/pre-push
operations/notify/notify.sh
operations/pod/pod_delete.sh
operations/pod/pod_guard.sh
operations/pod/pod_start_command.sh
operations/pod/session_end_pod_check.sh"

# Repository ingress rejects control characters in paths, so this intentional
# word split cannot turn one tracked path into several accepted paths.
# shellcheck disable=SC2086
shellcheck $scripts
# `sh -n` parses only its first operand, so walk the list. Prefer dash: macOS /bin/sh
# is bash in POSIX mode and accepts bashisms dash refuses.
syntax_shell="sh"
if command -v dash >/dev/null 2>&1; then
  syntax_shell="dash"
fi
for script in $scripts; do
  "$syntax_shell" -n "$script"
done
