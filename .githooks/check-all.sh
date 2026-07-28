#!/bin/sh
# Run the same deterministic checks used by CI.
#
# Bootstrap once in a virtual environment:
#
#     python3 -m pip install -r requirements-dev.txt
#     sh .githooks/check-all.sh

set -eu

if ! root=$(git rev-parse --show-toplevel 2>/dev/null); then
  echo "Not inside a Git repository." >&2
  exit 1
fi
cd "$root"

sh .githooks/check-documents.sh

python3 .githooks/check_ingress.py --history HEAD
python3 .githooks/check_ingress.py --staged
python3 .githooks/check_ingress.py --worktree

ruff check .
ruff format --check .

# CI additionally runs the autoclave-empty merge gate (ci.yml), which a loaded
# tray legitimately fails mid-review — deliberately not repeated here, because
# this script must pass on a branch that is still carrying drafts.
# These two lists are written out rather than globbed, so that a script is
# checked because somebody decided it should be. The cost is that a new script
# escapes silently — which it did: operations/codex/seat.sh spent money through
# an external API for a whole session before anything linted it. The guard
# against a repeat is a test, not a glob: test_seat.py asserts that every shell
# script under .githooks/ and operations/ appears on both lines below.
shellcheck .githooks/check-all.sh .githooks/check-documents.sh \
           .githooks/commit-msg .githooks/doc-allowlist.sh \
           .githooks/install.sh .githooks/pre-commit .githooks/pre-push \
           .githooks/record-audit.sh operations/notify/notify.sh \
           operations/codex/seat.sh

sh -n .githooks/check-all.sh .githooks/check-documents.sh .githooks/commit-msg \
      .githooks/doc-allowlist.sh .githooks/install.sh .githooks/pre-commit \
      .githooks/pre-push .githooks/record-audit.sh operations/notify/notify.sh \
      operations/codex/seat.sh

# Tach currently has a real module to inspect only after shared implementation
# enters common/. Numbered pipeline stages are file-isolated and require their
# own boundary tests when their first code is imported.
if find common -type f -name '*.py' ! -name '__init__.py' | grep -q .; then
  tach check
else
  echo "no shared implementation yet — Tach boundary check deferred"
fi

python3 -m pytest -q

python3 .githooks/build_wheel.py
