"""Shared pod test support."""

import json

# An explicit no-op child: these requests are never booted, so the bootstrap
# argv only has to satisfy the request's shape checks.
NO_OP_BOOTSTRAP = json.dumps(["python", "-c", "pass"])


def timer_start_command(report_path: str) -> tuple[str, ...]:
    """The pod-timer `docker_start_cmd` a placeholder request carries."""
    return (
        "python",
        "-m",
        "operations.pod.pod_timer",
        "--timer-factory",
        "operations.pod.provider_runpod:timer_context_from_environment",
        "--bootstrap-command-json",
        NO_OP_BOOTSTRAP,
        "--report-path",
        report_path,
    )
