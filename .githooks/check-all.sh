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

shellcheck .githooks/check-all.sh .githooks/check-documents.sh \
           .githooks/commit-msg .githooks/doc-allowlist.sh \
           .githooks/install.sh .githooks/pre-commit .githooks/pre-push \
           .githooks/record-audit.sh

sh -n .githooks/check-all.sh .githooks/check-documents.sh .githooks/commit-msg \
      .githooks/doc-allowlist.sh .githooks/install.sh .githooks/pre-commit \
      .githooks/pre-push .githooks/record-audit.sh

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
