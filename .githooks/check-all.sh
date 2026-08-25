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

# The gate defines its Python and uv environment. Inherited overrides can
# remove assertions, inject import roots or pytest plugins, redirect uv to a
# different environment, or turn its exact sync inexact while every command
# below still names the checkout-local interpreter.
unset PYTHONHOME PYTHONOPTIMIZE PYTHONPATH PYTEST_ADDOPTS PYTEST_PLUGINS
unset UV_CONFIG_FILE UV_INEXACT UV_PYTHON
PYTHONNOUSERSITE=1
PYTHONSAFEPATH=1
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
export PYTHONNOUSERSITE PYTHONSAFEPATH PYTEST_DISABLE_PLUGIN_AUTOLOAD

chamber_environment_followup() {
  [ -f /opt/autoclave/CLAUDE.md ] || return 0
  echo "check-all: this chamber image cannot construct the required checkout-local .venv." >&2
  echo "check-all: follow-up: install pinned uv==0.12.1 in operations/autoclave/Dockerfile; in cmd_new, after checkout, run '/opt/venv/bin/uv sync --frozen --group test --group audit'; add operations/autoclave/autoclave.sh to operations/autoclave/fingerprint.py INPUTS; then rebuild the image." >&2
  echo "check-all: do not link .venv to /opt/venv: that image environment is resolved from requirements-dev.txt and does not freeze uv.lock's transitive versions." >&2
}

# The interpreter must both import from `.venv` and match the current lock. The
# prefix check rejects a PATH shadow; the offline sync rejects a real but stale
# `.venv`.
frozen_python="$root/.venv/bin/python"
UV_PROJECT_ENVIRONMENT="$root/.venv"
export UV_PROJECT_ENVIRONMENT
[ -x "$frozen_python" ] || {
  echo "check-all: frozen interpreter is missing at $frozen_python; run 'uv sync --frozen --group test --group audit'" >&2
  chamber_environment_followup
  exit 1
}

# `sys.executable` can report the symlink used to invoke a PATH interpreter;
# `sys.prefix` identifies the environment supplying imports. Resolve both sides
# so a `.venv` symlink to the same frozen environment remains valid.
[ "$("$frozen_python" -c 'import os, sys; print(os.path.realpath(sys.prefix))')" \
  = "$(CDPATH='' cd -- "$root/.venv" && pwd -P)" ] || {
  echo "check-all: $frozen_python does not import from the frozen environment at $root/.venv; run 'uv sync --frozen --group test --group audit'" >&2
  chamber_environment_followup
  exit 1
}

# `uv sync` is exact by default: it reconciles the selected groups to uv.lock
# and removes undeclared packages. `--offline` keeps this verification from
# turning the checks before the final advisory audit into network-dependent
# steps. A stale environment is repaired when every needed artifact is already
# cached; otherwise the refusal names the online recovery command.
command -v uv >/dev/null 2>&1 || {
  echo "check-all: the frozen environment cannot be verified because uv is missing from PATH" >&2
  echo "check-all: recovery: install pinned uv==0.12.1, then run 'uv sync --frozen --group test --group audit'" >&2
  chamber_environment_followup
  exit 1
}
uv_version=$(uv --version 2>/dev/null) || {
  echo "check-all: uv is on PATH but could not report its version, so the frozen environment is unverified" >&2
  echo "check-all: recovery: install pinned uv==0.12.1, then run 'uv sync --frozen --group test --group audit'" >&2
  chamber_environment_followup
  exit 1
}
case "$uv_version" in
  "uv 0.12.1"|"uv 0.12.1 "*) : ;;
  *)
    echo "check-all: the frozen environment cannot be verified with $uv_version; this gate requires uv 0.12.1" >&2
    echo "check-all: recovery: install pinned uv==0.12.1, then run 'uv sync --frozen --group test --group audit'" >&2
    chamber_environment_followup
    exit 1
    ;;
esac
uv sync --frozen --offline --group test --group audit --no-config || {
  echo "check-all: uv could not reconcile $root/.venv to uv.lock from the local cache" >&2
  echo "check-all: recovery: run 'uv sync --frozen --group test --group audit' with network access, then retry" >&2
  chamber_environment_followup
  exit 1
}

# PATH may select `.venv` tools only after the environment passes both checks.
PATH="$root/.venv/bin:$PATH"
export PATH

sh .githooks/check-static.sh

if [ "$mode" = local ]; then
  python3 .githooks/check_ingress.py --history HEAD
  python3 .githooks/check_ingress.py --staged
  python3 .githooks/check_ingress.py --worktree
fi

"$frozen_python" -m pytest

# `--strict` makes an unreachable advisory service or unresolvable requirement
# fail; an audit that could not run is not evidence of clean dependencies.
# Audit the exact installed inventory, not requirements-dev.txt. That file pins
# every direct dependency, but `pip_audit --requirement` asks pip to resolve the
# transitive closure again. A later compatible transitive release can therefore
# be audited even though uv.lock installed an older one. The helper projects the
# distributions this already-proved interpreter imports into exact pins; the
# project itself is omitted because it is an editable local distribution with no
# PyPI advisory identity. `--no-deps --disable-pip` makes pip-audit consume those
# pins without resolving or installing anything.
# Keep this network-dependent step last so advisory-service availability cannot
# prevent credential scans or the test suite from running; it still fails the gate.
audit_inventory=$(mktemp "${TMPDIR:-/tmp}/verbatus-frozen-audit.XXXXXX") || {
  echo "check-all: could not create the frozen audit inventory" >&2
  exit 1
}
cleanup_audit_inventory() { rm -f -- "$audit_inventory"; }
trap cleanup_audit_inventory 0 1 2 15
"$frozen_python" .githooks/frozen_audit_requirements.py > "$audit_inventory"
"$frozen_python" -m pip_audit --strict --no-deps --disable-pip \
  --requirement "$audit_inventory"
cleanup_audit_inventory
trap - 0 1 2 15
