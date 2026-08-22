"""The credential-free child that writes a Mac backup store."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .backup import BackupRefusal, sync_run_tree


def main() -> int:
    try:
        request = json.loads(sys.stdin.read())
        if not isinstance(request, dict) or set(request) != {"run_root", "run_id", "mac_directory"}:
            raise BackupRefusal("backup request has an invalid shape")
        report = sync_run_tree(
            Path(request["run_root"]), request["run_id"], Path(request["mac_directory"])
        )
    except (BackupRefusal, OSError, TypeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(report.to_record(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
