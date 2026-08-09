#!/usr/bin/env bash
# Double-click on macOS: this opens the same Python flow as the `verbatus` command.
set -u

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
if ! cd "$ROOT"; then
    echo "What happened: Verbatus could not open its project folder."
    echo "What it means: Nothing was changed or billed."
    echo "Next step: Move this file back inside the Apparatus Verbatus folder, then try again; this is safe."
    read -r -p "Press Return to close this window. "
    exit 1
fi

PYTHON=""
if [ -x "$ROOT/.venv/bin/python" ]; then
    PYTHON="$ROOT/.venv/bin/python"
elif command -v verbatus >/dev/null 2>&1; then
    verbatus "$@"
    STATUS=$?
    if [ -t 0 ]; then
        read -r -p "Press Return to close this window. "
    fi
    exit "$STATUS"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON="$(command -v python3)"
else
    echo "What happened: Verbatus could not find the Python it needs to open this rehearsal."
    echo "What it means: Nothing was changed or billed."
    echo "Next step: Install the approved project setup, then double-click this file again; this is safe."
    read -r -p "Press Return to close this window. "
    exit 1
fi

"$PYTHON" -m operations.operator.entry "$@"
STATUS=$?
if [ -t 0 ]; then
    read -r -p "Press Return to close this window. "
fi
exit "$STATUS"
