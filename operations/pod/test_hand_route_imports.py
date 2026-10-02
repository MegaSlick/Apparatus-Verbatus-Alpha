"""The hand route's entry points load none of the managed route or the operator tool."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

FORBIDDEN = (
    "operations.pod.provider_runpod",
    "operations.pod.launch",
    "operations.pod.lease",
    "operations.pod.staged",
    "operations.operator",
)


def test_pod_run_and_bootstrap_main_load_no_managed_route_module() -> None:
    script = (
        "import json, sys\n"
        "import operations.pod.pod_run, operations.pod.bootstrap_main\n"
        "print(json.dumps(sorted(sys.modules)))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, check=True
    )
    loaded = json.loads(result.stdout)

    assert "operations.pod.pod_run" in loaded
    assert [
        name for name in loaded if any(name == f or name.startswith(f + ".") for f in FORBIDDEN)
    ] == []
