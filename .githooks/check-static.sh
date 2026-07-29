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
.githooks/record-audit.sh
operations/codex/seat.sh
operations/notify/notify.sh"

# Repository ingress rejects control characters in paths, so this intentional
# word split cannot turn one tracked path into several accepted paths.
# shellcheck disable=SC2086
shellcheck $scripts
# shellcheck disable=SC2086
sh -n $scripts
