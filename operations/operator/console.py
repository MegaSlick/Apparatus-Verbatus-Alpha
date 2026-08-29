"""A local, read-only JSON console process.

It has no provider, subprocess, approval-builder, or run-tree-writer import.
It accepts only an already-checked projection, never a path or writer capability.
"""

from __future__ import annotations

import json
import sys
from typing import Sequence

# A distinct status, so the parent never reports this process's own broken
# input pipe as a claim about the run tree. Exit 2 was indistinguishable from
# "the console read the tree and could not make sense of it", and the operator
# was told to freeze a parish run tree and open an evidence investigation
# because two of this tool's own processes had mishandled a pipe.
PROJECTION_UNREADABLE_EXIT = 3


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
        #
        # It still has to say which of the two happened. Exiting silently left
        # the parent with an empty detail, so the operator read "investigate
        # the named evidence problem" with no problem named, and was sent to
        # preserve and investigate register evidence that was never touched.
        print(
            "the projection on standard input was not complete JSON; the run tree "
            "itself was never read by this process",
            file=sys.stderr,
        )
        return PROJECTION_UNREADABLE_EXIT
    print(json.dumps(projection, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the CLI subprocess
    raise SystemExit(main())
