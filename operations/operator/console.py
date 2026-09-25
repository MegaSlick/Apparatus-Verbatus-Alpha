"""A local, read-only JSON console process.

It has no provider, subprocess, approval-builder, or run-tree-writer import.
It accepts only an already-checked projection, never a path or writer capability.
"""

from __future__ import annotations

import json
import sys
from typing import Sequence

# A distinct status, so the parent never reports this process's own broken
# input pipe as a claim about the run tree.
PROJECTION_UNREADABLE_EXIT = 3


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments:
        raise SystemExit(
            "verbatus-review receives its already-checked projection on standard input"
        )
    try:
        projection = json.load(sys.stdin)
    # `json.load` on `sys.stdin` raises `JSONDecodeError` for malformed text,
    # `UnicodeDecodeError` for non-UTF-8 bytes, and `OSError` when the read
    # itself fails. This is deliberately not an OperatorError: the custody
    # parent only hands the child bytes it just serialized, and a malformed
    # pipe cannot be a claim about the run tree; the parent turns a nonzero
    # exit into the ordinary three-part failure contract instead.
    except (ValueError, OSError) as error:
        print(
            "the projection on standard input could not be read as complete JSON "
            f"({type(error).__name__}); the run tree itself was never read by this process",
            file=sys.stderr,
        )
        return PROJECTION_UNREADABLE_EXIT
    print(json.dumps(projection, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the CLI subprocess
    raise SystemExit(main())
