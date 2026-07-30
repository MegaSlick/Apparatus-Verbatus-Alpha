#!/bin/sh
# Shared deterministic checks that do not scan history or run the test suite.

set -eu
root=$(git rev-parse --show-toplevel 2>/dev/null) ||
  { echo "check-static: not inside a Git repository" >&2; exit 1; }
cd "$root"

sh .githooks/check-documents.sh
git diff --check HEAD --
ruff check .
ruff format --check .

scripts=".githooks/applypatch-msg
.githooks/check-all.sh
.githooks/check-documents.sh
.githooks/check-fast.sh
.githooks/check-static.sh
.githooks/commit-msg
.githooks/doc-allowlist.sh
.githooks/install.sh
.githooks/pre-applypatch
.githooks/pre-commit
.githooks/pre-merge-commit
.githooks/pre-push
operations/codex/capture-seat-report.sh
operations/codex/seat.sh
operations/notify/notify.sh"

# Repository ingress rejects control characters in paths, so this intentional
# word split cannot turn one tracked path into several accepted paths.
# shellcheck disable=SC2086
shellcheck $scripts
# `sh -n` parses only its first operand and turns the rest into positional
# parameters, so passing the whole list checked the first file and silently
# ignored the rest. Walk them one at a time; `set -e` makes the first syntax
# error the exit status of this check.
#
# Prefer dash where it exists. On macOS `/bin/sh` is bash in POSIX mode, which
# parses `a=(1 2)`, `function f() {}` and `for ((;;))` without complaint; CI runs
# dash, which refuses all three. Checking with the local `sh` therefore passed
# things that fail only when the hook actually runs on another machine. This
# narrows that gap rather than closing it — `${x^^}` parses cleanly in both, and
# a bashism that is merely a command word rather than a syntax error, `[[` among
# them, is invisible to any `-n` parse. shellcheck above is what catches those.
syntax_shell="sh"
if command -v dash >/dev/null 2>&1; then
  syntax_shell="dash"
fi
for script in $scripts; do
  "$syntax_shell" -n "$script"
done
