#!/bin/sh
# Full local/CI gate. CI supplies its own ref-aware history scan once.

set -eu
[ -x /usr/bin/git ] || {
  echo "check-all: trusted Git is unavailable at /usr/bin/git" >&2
  exit 1
}
root=$(/usr/bin/git rev-parse --show-toplevel 2>/dev/null) ||
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
# offline sync runs before that interpreter executes anything; only then does
# the prefix check reject a PATH shadow.
frozen_python="$root/.venv/bin/python"
UV_PROJECT_ENVIRONMENT="$root/.venv"
export UV_PROJECT_ENVIRONMENT
[ -x "$frozen_python" ] || {
  echo "check-all: frozen interpreter is missing at $frozen_python; run 'uv sync --frozen --group test --group audit'" >&2
  chamber_environment_followup
  exit 1
}

# `uv sync` is exact by default: it reconciles the selected groups to uv.lock
# and removes undeclared packages. `--offline` keeps this verification from
# turning the checks before the final advisory audit into network-dependent
# steps. A stale environment is repaired when every needed artifact is already
# cached; otherwise the refusal names the online recovery command.
uv_binary=$(command -v uv 2>/dev/null) || {
  echo "check-all: the frozen environment cannot be verified because uv is missing from PATH" >&2
  echo "check-all: recovery: install pinned uv==0.12.1, then run 'uv sync --frozen --group test --group audit'" >&2
  chamber_environment_followup
  exit 1
}
case "$uv_binary" in
  /*) : ;;
  *)
    echo "check-all: uv resolved to a non-absolute command '$uv_binary'; refusing PATH ambiguity" >&2
    exit 1
    ;;
esac
[ -x /usr/bin/readlink ] || {
  echo "check-all: /usr/bin/readlink is unavailable, so uv's executable path cannot be verified" >&2
  exit 1
}
uv_resolved=$uv_binary
uv_links=0
while [ -L "$uv_resolved" ]; do
  uv_links=$((uv_links + 1))
  [ "$uv_links" -le 40 ] || {
    echo "check-all: uv's symlink chain is too deep to verify" >&2
    exit 1
  }
  uv_target=$(/usr/bin/readlink "$uv_resolved") || {
    echo "check-all: uv's symlink target cannot be read" >&2
    exit 1
  }
  case "$uv_target" in
    /*) uv_resolved=$uv_target ;;
    *) uv_resolved=${uv_resolved%/*}/$uv_target ;;
  esac
done
uv_parent=${uv_resolved%/*}
[ -n "$uv_parent" ] || uv_parent=/
uv_parent=$(CDPATH='' cd -- "$uv_parent" && pwd -P) || {
  echo "check-all: uv's containing directory cannot be resolved safely" >&2
  exit 1
}
while :; do
  if [ "$uv_parent" -ef "$root" ]; then
    echo "check-all: uv resolves inside the checkout; refusing a repository-controlled verifier" >&2
    exit 1
  fi
  [ "$uv_parent" = / ] && break
  uv_parent=${uv_parent%/*}
  [ -n "$uv_parent" ] || uv_parent=/
done
[ -x /usr/bin/env ] || {
  echo "check-all: /usr/bin/env is unavailable, so uv cannot run with a clean environment" >&2
  exit 1
}
uv_home=${HOME:-/tmp}
case "$uv_home" in
  /*) : ;;
  *) uv_home=/tmp ;;
esac
uv_version=$(/usr/bin/env -i HOME="$uv_home" PATH=/usr/bin:/bin \
  "$uv_binary" --version 2>/dev/null) || {
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
# uv finds its cache through UV_CACHE_DIR, then XDG_CACHE_HOME, then HOME, and
# `env -i` drops the first two deliberately. That is the decision, not an
# oversight: this sync is the step that decides whether `.venv` may be trusted,
# and a caller-named cache directory is a caller-supplied input to the verifier.
# The cost is real and bounded -- someone whose populated cache lives outside
# HOME gets "could not reconcile" and must re-run the recovery command with
# network access. Losing that trade the other way would let the environment the
# gate vouches for be reconciled from bytes the caller chose.
/usr/bin/env -i HOME="$uv_home" PATH=/usr/bin:/bin \
  UV_PROJECT_ENVIRONMENT="$UV_PROJECT_ENVIRONMENT" \
  "$uv_binary" sync --frozen --offline --group test --group audit --no-config || {
  echo "check-all: uv could not reconcile $root/.venv to uv.lock from the local cache" >&2
  echo "check-all: recovery: run 'uv sync --frozen --group test --group audit' with network access, then retry" >&2
  chamber_environment_followup
  exit 1
}

# `sys.executable` can report the symlink used to invoke a PATH interpreter;
# `sys.prefix` identifies the environment supplying imports. Resolve both sides
# so a `.venv` symlink to the same frozen environment remains valid. This is the
# first execution through the checkout-local interpreter: uv has reconciled its
# packages before any of them can import.
[ "$("$frozen_python" -c 'import os, sys; print(os.path.realpath(sys.prefix))')" \
  = "$(CDPATH='' cd -- "$root/.venv" && pwd -P)" ] || {
  echo "check-all: $frozen_python does not import from the frozen environment at $root/.venv; run 'uv sync --frozen --group test --group audit'" >&2
  chamber_environment_followup
  exit 1
}

# PATH may select only the verified environment and fixed system tool roots
# after both checks. An inherited checkout-local or writable tools directory
# must not supply a later shell, Python, Git, or scanner.
PATH="$root/.venv/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PATH

/bin/sh .githooks/check-static.sh

if [ "$mode" = local ]; then
  "$frozen_python" .githooks/check_ingress.py --history HEAD
  "$frozen_python" .githooks/check_ingress.py --staged
  "$frozen_python" .githooks/check_ingress.py --worktree
fi

# The gate is the one place the suites run inside the checkout that holds the
# real `private/ntfy.conf`, and that is exactly where a test which forgot to
# inject its notification seam pages his phone: nine identical milestones
# arrived from a single gate run. `operations/notify/notify.sh` treats one
# reserved topic as "under test" -- it prints what it would have sent and exits
# 0 without posting -- and the root `conftest.py` sets that topic for any pytest
# session. This is belt to those braces, and it is set here rather than
# inherited, for the reason the environment block at the top of this file
# exists.
#
# It sits beside the pytest line rather than in that block because the gate's
# own tests run this script against synthetic repositories that have no
# `conftest.py`, and every one of them stops before pytest. Refusing up there
# would have failed seven of them for the absence of a file they have no reason
# to carry. The variable is needed exactly where it is now used.
#
# The value is *read* from `conftest.py`, not written out again. Writing it
# again would be a fourth copy of a constant whose whole job is to be identical
# everywhere, and `.githooks/check_ingress.py` refuses a literal
# `NTFY_TOPIC=<topic-shaped value>` anywhere in the tree -- correctly, and under
# a ruling that deliberately exempts no exact topic. Reading it satisfies both:
# one source of truth, and no topic-shaped assignment to exempt.
#
# It fails closed. An empty `NTFY_TOPIC` is not "no sink"; it is the real topic
# from `private/ntfy.conf`, which is the precise failure this guards against. So
# a renamed or reshaped constant stops the gate rather than quietly unsinking it.
NTFY_TOPIC=$(sed -n 's/^NOTIFY_TEST_SINK_TOPIC = "\([A-Za-z0-9_-]\{1,64\}\)"$/\1/p' \
  "$root/conftest.py" | head -n 1)
[ -n "$NTFY_TOPIC" ] || {
  echo "check-all: could not read NOTIFY_TEST_SINK_TOPIC from conftest.py; refusing to run the" >&2
  echo "check-all: suites in a checkout that may hold the real notification topic" >&2
  exit 1
}
export NTFY_TOPIC

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
audit_directory=$(mktemp -d "/tmp/verbatus-frozen-audit.XXXXXX") || {
  echo "check-all: could not create the private frozen audit directory" >&2
  exit 1
}
audit_inventory="${audit_directory}/requirements.txt"
cleanup_audit_inventory() {
  # The directory as a unit. `set -e` is in force, so `rmdir` returning non-zero
  # over one unexpected file left the whole gate exiting non-zero after every
  # check had already passed -- a directory-removal error the operator cannot
  # tell from a real audit failure. Cleanup may not outvote the result.
  rm -rf -- "$audit_directory"
}
# A POSIX sh trap for HUP/INT/TERM runs the handler and then *resumes* the
# script. With one shared trap, Ctrl-C deleted the inventory and the gate carried
# straight on to pip_audit, which then failed on a missing --requirement file:
# the operator read a missing-file error instead of "the run was interrupted".
interrupt_audit_inventory() {
  cleanup_audit_inventory
  echo "check-all: interrupted before the advisory audit finished" >&2
  exit 1
}
trap cleanup_audit_inventory 0
trap interrupt_audit_inventory 1 2 15
"$frozen_python" .githooks/frozen_audit_requirements.py > "$audit_inventory"
"$frozen_python" -m pip_audit --strict --no-deps --disable-pip \
  --requirement "$audit_inventory"
cleanup_audit_inventory
trap - 0 1 2 15
