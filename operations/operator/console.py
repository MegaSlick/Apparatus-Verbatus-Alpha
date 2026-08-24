"""A local, read-only JSON console process.

The initial console deliberately has no provider, subprocess, approval-builder,
or run-tree-writer import.  It renders a review projection to stdout so a local
web wrapper can consume the same shape later without expanding this process's
authority today.
"""

from __future__ import annotations

import json
import sys
from typing import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments:
        raise SystemExit(
            "verbatus-review receives its already-checked projection on standard input"
        )
    try:
        projection = json.load(sys.stdin)
    except json.JSONDecodeError:
        # This is deliberately not an OperatorError: the custody parent only
        # hands the child bytes it just serialized, and a malformed pipe cannot
        # be a claim about the run tree.  The parent turns a nonzero child exit
        # into the ordinary three-part operator failure contract.
        return 2
    print(json.dumps(projection, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the CLI subprocess
    raise SystemExit(main())
