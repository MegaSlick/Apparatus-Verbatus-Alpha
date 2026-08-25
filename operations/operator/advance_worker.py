"""Only this confined process may append an advance decision record."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from common.contracts.errors import ApprovalRefusal, ContractError
from common.runtree.store import RunTree

from .advance import record_advance


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="verbatus-advance-worker")
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    try:
        request = json.load(sys.stdin)
        if not isinstance(request, dict):
            raise ApprovalRefusal("advance request is not an object")
        stage, reason = request.get("stage"), request.get("reason")
        expected_digest = request.get("expected_digest")
        if (
            not isinstance(stage, str)
            or not isinstance(reason, str)
            or not isinstance(expected_digest, str)
        ):
            raise ApprovalRefusal(
                "advance request must name one stage, one reason, and the reviewed seal digest"
            )
        reference = record_advance(
            RunTree(args.run_root, args.run_id),
            stage,
            reason=reason,
            expected_digest=expected_digest,
        )
    except (ContractError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(reference.to_record(), sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - run in a custody subprocess
    raise SystemExit(main())
