#!/bin/sh
# Full local/CI gate. CI supplies its own ref-aware history scan once.

set -eu
root=$(git rev-parse --show-toplevel 2>/dev/null) ||
  { echo "check-all: not inside a Git repository" >&2; exit 1; }
cd "$root"

mode=local
if [ "${1:-}" = "--ci" ] && [ "$#" -eq 1 ]; then
  mode=ci
elif [ "$#" -ne 0 ]; then
  echo "usage: sh .githooks/check-all.sh [--ci]" >&2
  exit 2
fi

sh .githooks/check-static.sh

if [ "$mode" = local ]; then
  python3 .githooks/check_ingress.py --history HEAD
  python3 .githooks/check_ingress.py --staged
  python3 .githooks/check_ingress.py --worktree
fi

python3 -m pytest

# Dependency vulnerability audit, fired by the deferred-tooling trigger the
# moment dependencies stopped being an empty list. `--strict` makes an
# unreachable advisory service or an unresolvable requirement a failing gate:
# a check that cannot run is a failure, not a pass.
#
# It audits requirements-dev.txt, which carries every runtime dependency as well
# as the tools this gate installs. That is only true because it is kept true —
# `test_every_runtime_dependency_is_inside_what_the_audit_reads` in
# .githooks/test_ci_workflow.py fails if a dependency is added to pyproject.toml
# and not here, which would otherwise leave it silently unaudited.
#
# **Last, and that is the point.** `set -e` makes every step a barrier to the ones
# after it, and this is the only step that needs a network and a tool the gate does
# not itself install. Run earlier, an offline developer or a missing `pip_audit`
# stopped the credential scans and the whole suite from running at all — the check
# that keeps credentials out of outgoing history was sitting downstream of an
# advisory service. It still fails the gate; it no longer decides whether the rest
# of the gate happens.
python3 -m pip_audit --strict --requirement requirements-dev.txt
