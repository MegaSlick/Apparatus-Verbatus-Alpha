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
# Only the checkout's own code, never an ambient `verbatus` from PATH: a
# stale, unrelated, or live-capable install must never be what a double-click
# in this folder runs. With no usable Python the honest answer is the setup
# message below, not somebody else's executable.
if [ -x "$ROOT/.venv/bin/python" ]; then
    set -- "$ROOT/.venv/bin/python" -m operations.operator.entry "$@"
elif command -v python3 >/dev/null 2>&1; then
    set -- "$(command -v python3)" -m operations.operator.entry "$@"
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
