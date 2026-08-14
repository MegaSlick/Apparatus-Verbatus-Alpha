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

# Every route ends in one exit-and-wait tail below, so a double-clicked window
# cannot close on an error message before it has been read.
# The checkout's own code always outranks an ambient `verbatus` on PATH: a
# stale or unrelated install must never be what a double-click in this folder
# runs, least of all for a verb that can upload to a network volume.
if [ -x "$ROOT/.venv/bin/python" ]; then
    set -- "$ROOT/.venv/bin/python" -m operations.operator.entry "$@"
elif command -v python3 >/dev/null 2>&1; then
    set -- "$(command -v python3)" -m operations.operator.entry "$@"
elif command -v verbatus >/dev/null 2>&1; then
    set -- verbatus "$@"
else
    echo "What happened: Verbatus could not find the Python it needs to open this rehearsal."
    echo "What it means: Nothing was changed or billed."
    echo "Next step: Install the approved project setup, then double-click this file again; this is safe."
    read -r -p "Press Return to close this window. "
    exit 1
fi

"$@"
STATUS=$?
if [ -t 0 ]; then
    read -r -p "Press Return to close this window. "
fi
exit "$STATUS"
