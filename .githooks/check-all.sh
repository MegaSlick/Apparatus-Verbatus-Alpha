#!/bin/sh
# The full gate: static checks, ingress scans, the test suite and the dependency audit.
# With --ci the workflow has already scanned the history the push or pull request adds.

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

mode=local
parallel=no
for check_all_argument in "$@"; do
  case "$check_all_argument" in
    --ci) mode=ci ;;
    --parallel) parallel=yes ;;
    *) check_all_usage ;;
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

recovery="run 'uv sync --frozen --group test --group audit'"
frozen_python="$root/.venv/bin/python"
[ -x "$frozen_python" ] || {
  echo "check-all: frozen interpreter is missing at $frozen_python; $recovery" >&2
  exit 1
}

# The uv version is declared once, in pyproject.toml. This first run of the interpreter
# imports only the standard library, because the environment is not yet reconciled.
required_uv_version=$("$frozen_python" -c '
import tomllib
with open("pyproject.toml", "rb") as handle:
    project = tomllib.load(handle)
print(project["tool"]["uv"]["required-version"].removeprefix("=="))
' 2>/dev/null) && [ -n "$required_uv_version" ] || {
  echo "check-all: pyproject.toml's [tool.uv] required-version could not be read" >&2
  exit 1
}

uv_binary=$(command -v uv 2>/dev/null) || {
  echo "check-all: uv is missing from PATH" >&2
  echo "check-all: recovery: install uv==$required_uv_version, then $recovery" >&2
  exit 1
}
# `env -i` below runs uv from the checkout root, where a relative path could name a
# file the checkout itself supplies.
case "$uv_binary" in
  /*) : ;;
  *)
    echo "check-all: uv resolved to the relative path '$uv_binary'; put an absolute uv directory on PATH" >&2
    exit 1
    ;;
esac
[ -x /usr/bin/env ] || {
  echo "check-all: /usr/bin/env is unavailable, so uv cannot run with a clean environment" >&2
  exit 1
}
uv_home=${HOME:-/tmp}
case "$uv_home" in
  /*) : ;;
  *) uv_home=/tmp ;;
esac
# uv runs with a clean environment, so no caller UV_* variable or cache location
# changes what it installs or exports.
run_uv() {
  /usr/bin/env -i HOME="$uv_home" PATH=/usr/bin:/bin \
    UV_PROJECT_ENVIRONMENT="$root/.venv" "$uv_binary" "$@"
}

uv_version=$(run_uv --version 2>/dev/null) || uv_version="no version"
case "$uv_version" in
  "uv $required_uv_version"|"uv $required_uv_version "*) : ;;
  *)
    echo "check-all: uv on PATH reports '$uv_version'; this gate requires uv $required_uv_version" >&2
    echo "check-all: recovery: install uv==$required_uv_version, then $recovery" >&2
    exit 1
    ;;
esac

# Exact (undeclared packages are removed) and offline, so nothing before the final
# audit needs the network.
run_uv sync --frozen --offline --group test --group audit --no-config || {
  echo "check-all: uv could not reconcile $root/.venv to uv.lock from the local cache" >&2
  echo "check-all: recovery: $recovery with network access, then retry" >&2
  exit 1
}

# The resolved sys.prefix, not sys.executable: a `.venv/bin/python` symlink to another
# interpreter would otherwise pass as the frozen environment.
[ "$("$frozen_python" -c 'import os, sys; print(os.path.realpath(sys.prefix))')" \
  = "$(CDPATH='' cd -- "$root/.venv" && pwd -P)" ] || {
  echo "check-all: $frozen_python does not import from the frozen environment at $root/.venv; $recovery" >&2
  exit 1
}

# From here PATH holds only the verified environment and fixed system directories.
PATH="$root/.venv/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PATH

/bin/sh .githooks/check-static.sh

if [ "$mode" = local ]; then
  "$frozen_python" .githooks/check_ingress.py --history HEAD
  "$frozen_python" .githooks/check_ingress.py --staged
  "$frozen_python" .githooks/check_ingress.py --worktree
fi

# This checkout may hold the real private/ntfy.conf, so the suites post only to the
# test-sink topic conftest.py declares. Read from conftest.py rather than written here,
# because check_ingress.py refuses a literal topic. An empty value would mean the real
# topic, so a failed read stops the gate.
NTFY_TOPIC=$(sed -n 's/^NOTIFY_TEST_SINK_TOPIC = "\([A-Za-z0-9_-]\{1,64\}\)"$/\1/p' \
  "$root/conftest.py" | head -n 1)
[ -n "$NTFY_TOPIC" ] || {
  echo "check-all: could not read NOTIFY_TEST_SINK_TOPIC from conftest.py; refusing to run the" >&2
  echo "check-all: suites in a checkout that may hold the real notification topic" >&2
  exit 1
}
export NTFY_TOPIC

# A fixed four workers, so the run never depends on core count; loadfile keeps each
# file's fixtures on one worker.
if [ "$parallel" = yes ]; then
  "$frozen_python" -m pytest -p xdist -n 4 --dist loadfile
else
  "$frozen_python" -m pytest
fi

# Audit exactly what uv.lock installs for the gate's groups. `--strict`: an audit that
# could not run (an unreachable advisory service included) fails the gate.
# `--no-deps --disable-pip`: audit the pins as given, resolving and installing nothing.
# Last, so network trouble never stops the scans or the suites.
audit_directory=$(mktemp -d "/tmp/verbatus-audit.XXXXXX") || {
  echo "check-all: could not create the audit directory" >&2
  exit 1
}
audit_inventory="$audit_directory/requirements.txt"
# Exiting from the signal trap fires the exit trap, which removes the directory.
trap 'rm -rf -- "$audit_directory"' 0
trap 'echo "check-all: interrupted before the dependency audit finished" >&2; exit 1' 1 2 15
run_uv export --frozen --offline --no-config --no-emit-project --no-hashes \
  --group test --group audit > "$audit_inventory"
"$frozen_python" -m pip_audit --strict --no-deps --disable-pip \
  --requirement "$audit_inventory"
