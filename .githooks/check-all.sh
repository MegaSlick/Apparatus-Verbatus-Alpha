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

check_all_usage() {
  echo "usage: sh .githooks/check-all.sh [--ci] [--parallel]" >&2
  exit 2
}

# Each flag at most once: a gate that ignored a misspelled flag would report green
# for a check it never ran.
mode=local
parallel=no
for check_all_argument in "$@"; do
  case "$check_all_argument" in
    --ci)
      [ "$mode" = local ] || check_all_usage
      mode=ci
      ;;
    --parallel)
      [ "$parallel" = no ] || check_all_usage
      parallel=yes
      ;;
    *)
      check_all_usage
      ;;
  esac
done

# Inherited overrides could remove assertions, inject imports or pytest plugins, or
# redirect or loosen uv's sync while every command still names the checkout interpreter.
unset PYTHONHOME PYTHONOPTIMIZE PYTHONPATH PYTEST_ADDOPTS PYTEST_PLUGINS
unset UV_CONFIG_FILE UV_INEXACT UV_PYTHON
PYTHONNOUSERSITE=1
PYTHONSAFEPATH=1
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
export PYTHONNOUSERSITE PYTHONSAFEPATH PYTEST_DISABLE_PLUGIN_AUTOLOAD

# `.venv`'s interpreter runs once before the offline sync, to read the uv version with
# stdlib tomllib only (no third-party import); the prefix check follows the sync.
frozen_python="$root/.venv/bin/python"
UV_PROJECT_ENVIRONMENT="$root/.venv"
export UV_PROJECT_ENVIRONMENT
[ -x "$frozen_python" ] || {
  echo "check-all: frozen interpreter is missing at $frozen_python; run 'uv sync --frozen --group test --group audit'" >&2
  exit 1
}

# One declaration of the uv version, pyproject.toml's; a second literal here once drifted.
required_uv_version=$("$frozen_python" -c '
import tomllib
with open("pyproject.toml", "rb") as handle:
    project = tomllib.load(handle)
print(project["tool"]["uv"]["required-version"].removeprefix("=="))
' 2>/dev/null) && [ -n "$required_uv_version" ] || {
  echo "check-all: pyproject.toml's [tool.uv] required-version could not be read" >&2
  exit 1
}

# The sync is exact (undeclared packages are removed) and offline, so the checks before
# the final audit never need the network; a cache miss names the online recovery.
uv_binary=$(command -v uv 2>/dev/null) || {
  echo "check-all: the frozen environment cannot be verified because uv is missing from PATH" >&2
  echo "check-all: recovery: install pinned uv==$required_uv_version, then run 'uv sync --frozen --group test --group audit'" >&2
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
  echo "check-all: recovery: install pinned uv==$required_uv_version, then run 'uv sync --frozen --group test --group audit'" >&2
  exit 1
}
case "$uv_version" in
  "uv $required_uv_version"|"uv $required_uv_version "*) : ;;
  *)
    echo "check-all: the frozen environment cannot be verified with $uv_version; this gate requires uv $required_uv_version" >&2
    echo "check-all: recovery: install pinned uv==$required_uv_version, then run 'uv sync --frozen --group test --group audit'" >&2
    exit 1
    ;;
esac
# `env -i` drops UV_CACHE_DIR and XDG_CACHE_HOME on purpose: a caller-named cache would
# be caller input to the step that decides whether `.venv` is trusted. The cost: a
# cache outside HOME must be refilled by the online recovery command.
/usr/bin/env -i HOME="$uv_home" PATH=/usr/bin:/bin \
  UV_PROJECT_ENVIRONMENT="$UV_PROJECT_ENVIRONMENT" \
  "$uv_binary" sync --frozen --offline --group test --group audit --no-config || {
  echo "check-all: uv could not reconcile $root/.venv to uv.lock from the local cache" >&2
  echo "check-all: recovery: run 'uv sync --frozen --group test --group audit' with network access, then retry" >&2
  exit 1
}

# Compare the resolved sys.prefix, not sys.executable (possibly a PATH symlink), so a
# `.venv` symlink to the same environment passes. Its first run past the stdlib-only
# version read, so no package imports before uv reconciled them.
[ "$("$frozen_python" -c 'import os, sys; print(os.path.realpath(sys.prefix))')" \
  = "$(CDPATH='' cd -- "$root/.venv" && pwd -P)" ] || {
  echo "check-all: $frozen_python does not import from the frozen environment at $root/.venv; run 'uv sync --frozen --group test --group audit'" >&2
  exit 1
}

# From here PATH holds only the verified environment and fixed system roots, so no
# inherited directory can supply a later shell, Python, Git or scanner.
PATH="$root/.venv/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PATH

/bin/sh .githooks/check-static.sh

if [ "$mode" = local ]; then
  "$frozen_python" .githooks/check_ingress.py --history HEAD
  "$frozen_python" .githooks/check_ingress.py --staged
  "$frozen_python" .githooks/check_ingress.py --worktree
fi

# The suites here run in the checkout holding the real private/ntfy.conf, so force the
# test-sink topic notify.sh never posts (conftest.py sets it too). Set beside pytest,
# not with the environment above, because the gate's own tests use repos without
# conftest.py. Read, not written: check_ingress.py refuses any literal topic
# assignment. Fails closed: an empty value would mean the real topic.
NTFY_TOPIC=$(sed -n 's/^NOTIFY_TEST_SINK_TOPIC = "\([A-Za-z0-9_-]\{1,64\}\)"$/\1/p' \
  "$root/conftest.py" | head -n 1)
[ -n "$NTFY_TOPIC" ] || {
  echo "check-all: could not read NOTIFY_TEST_SINK_TOPIC from conftest.py; refusing to run the" >&2
  echo "check-all: suites in a checkout that may hold the real notification topic" >&2
  exit 1
}
export NTFY_TOPIC

# Nothing about `--parallel` comes from the environment. Four workers, fixed, so the
# census never depends on core count; loadfile keeps each file's fixtures on one worker.
if [ "$parallel" = yes ]; then
  "$frozen_python" -m pytest -p xdist -n 4 --dist loadfile
else
  "$frozen_python" -m pytest
fi

# Audit the exact installed inventory: `--requirement` on requirements-dev.txt would
# re-resolve transitives and could audit a newer release than uv.lock installed. The
# editable local project has no PyPI identity. `--strict`: an audit that could not run
# is not evidence. `--no-deps --disable-pip`: consume the pins, resolve and install
# nothing. Last, so network trouble never stops the scans or suites.
audit_directory=$(mktemp -d "/tmp/verbatus-frozen-audit.XXXXXX") || {
  echo "check-all: could not create the private frozen audit directory" >&2
  exit 1
}
audit_inventory="${audit_directory}/requirements.txt"
cleanup_audit_inventory() {
  # Not rmdir: under `set -e` a stray file would fail the gate after every check passed.
  rm -rf -- "$audit_directory"
}
# A POSIX trap on HUP/INT/TERM resumes the script, so the interrupt handler must exit
# or pip_audit would run on a deleted inventory.
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
