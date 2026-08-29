"""Runs without provider credentials; success stdout is a closed record."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .backup import BackupRefusal, sync_run_tree

MAX_REQUEST_BYTES = 64 * 1024


def _identity(value: object, *, field: str) -> tuple[int, int]:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(
            not isinstance(member, int) or isinstance(member, bool) or member < 0
            for member in value
        )
    ):
        raise BackupRefusal(f"backup request field {field!r} has no filesystem identity")
    return (value[0], value[1])


def _destination_identities(value: object) -> tuple[tuple[int, int], ...]:
    if not isinstance(value, list) or len(value) != 5:
        raise BackupRefusal(
            "backup request field 'destination_identities' has no complete layout identity"
        )
    return tuple(
        _identity(member, field=f"destination_identities[{index}]")
        for index, member in enumerate(value)
    )


def main() -> int:
    try:
        data = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(data) > MAX_REQUEST_BYTES:
            raise BackupRefusal(
                f"backup request is larger than {MAX_REQUEST_BYTES} bytes and was not read"
            )
        request = json.loads(data)
        fields = {
            "run_root",
            "run_id",
            "mac_directory",
            "source_identity",
            "destination_identities",
        }
        if not isinstance(request, dict) or set(request) != fields:
            raise BackupRefusal("backup request has an invalid shape")
        report = sync_run_tree(
            Path(request["run_root"]),
            request["run_id"],
            Path(request["mac_directory"]),
            expected_source_identity=_identity(request["source_identity"], field="source_identity"),
            expected_destination_identities=_destination_identities(
                request["destination_identities"]
            ),
        )
    # `RecursionError` is listed for the same reason `advance_worker` lists it:
    # `json.loads` raises it, not `ValueError`, on a deeply nested request, and
    # the 64 KiB bound still admits tens of thousands of nesting levels. Left
    # out, the worker dies with a traceback and the operator's saved detail is
    # a stack trace instead of the one-line refusal every other failure prints.
    except (BackupRefusal, OSError, RecursionError, TypeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(report.to_record(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
