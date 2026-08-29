"""Only this confined process may append an advance decision record."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Sequence

from common.contracts.errors import ApprovalRefusal, ContractError
from common.runtree.store import RunTree

from .advance import (
    MAX_ADVANCE_REQUEST_CHARACTERS,
    WORKER_REPORT_FAILED_EXIT,
    receipt_directory_identity,
    record_advance,
    require_directory_identity,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="verbatus-advance-worker")
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-device", required=True, type=int)
    parser.add_argument("--run-inode", required=True, type=int)
    parser.add_argument("--receipt-device", required=True, type=int)
    parser.add_argument("--receipt-inode", required=True, type=int)
    args = parser.parse_args(argv)
    try:
        raw_request = sys.stdin.read(MAX_ADVANCE_REQUEST_CHARACTERS + 1)
        if len(raw_request) > MAX_ADVANCE_REQUEST_CHARACTERS:
            raise ApprovalRefusal(
                f"advance request exceeds {MAX_ADVANCE_REQUEST_CHARACTERS} characters"
            )
        request = json.loads(raw_request)
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
        tree = RunTree(args.run_root, args.run_id)
        run_identity = (args.run_device, args.run_inode)
        receipt_identity = (args.receipt_device, args.receipt_inode)
        require_directory_identity(tree.root, run_identity, "the reviewed run tree")
        if receipt_directory_identity(tree.root, run_identity, create=False) != receipt_identity:
            raise ApprovalRefusal(
                "the advance receipt directory changed device or inode before worker use"
            )
        reference = record_advance(
            tree,
            stage,
            reason=reason,
            expected_digest=expected_digest,
        )
        require_directory_identity(tree.root, run_identity, "the reviewed run tree")
        if receipt_directory_identity(tree.root, run_identity, create=False) != receipt_identity:
            raise ApprovalRefusal(
                "the advance receipt directory changed device or inode during worker use"
            )
    except (ContractError, OSError, ValueError, RecursionError) as error:
        print(str(error), file=sys.stderr)
        return 2
    # The record above is on disk and permanent. Reporting it can still fail --
    # a closed or broken stdout pipe raises `OSError` -- and an uncaught one
    # here would exit nonzero with a traceback, which the parent reads as "the
    # advance was refused" about a boundary that was in fact advanced. The
    # write gets its own status so the parent can say which of the two happened.
    try:
        sys.stdout.write(json.dumps(reference.to_record(), sort_keys=True) + "\n")
        sys.stdout.flush()
    except OSError as error:
        # The buffer still holds the unsent report, and the interpreter's own
        # flush at shutdown would raise again and replace this status with 120.
        # Pointing the descriptor at the null device discards that second
        # attempt without discarding what this process is trying to say.
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except OSError:  # pragma: no cover - the null device is not optional
            pass
        print(
            f"the advance record was written and could not be reported: {error}",
            file=sys.stderr,
        )
        return WORKER_REPORT_FAILED_EXIT
    return 0


if __name__ == "__main__":  # pragma: no cover - run in a custody subprocess
    raise SystemExit(main())
