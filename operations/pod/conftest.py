"""Shared pod test support."""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from .models import BILLING_CUTOFF_MARGIN_ENV, PodCreateRequest
from .shutdown import VerifiedShutdown
from .spend import SpendPolicy

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


@dataclass
class SharedClock:
    start: datetime
    seconds: float = 0.0

    def now(self) -> datetime:
        return self.start + timedelta(seconds=self.seconds)

    def monotonic(self) -> float:
        return self.seconds

    def sleep(self, seconds: float) -> None:
        self.seconds += seconds


def standard_request(
    *,
    hard_deadline: datetime,
    name: str,
    report_path: str,
    gpu_type: str = "fake-48gb",
    volume_id: str = "test-volume",
    bootstrap_command_json: str = '["service"]',
    timer_factory: str = "untracked.timer:factory",
    cutoff_margin: int = 3600,
    docker_start_cmd: tuple[str, ...] | None = None,
) -> PodCreateRequest:
    return PodCreateRequest(
        name=name,
        gpu_type=gpu_type,
        image="registry.example/verbatus@sha256:" + "a" * 64,
        template="pinned-template",
        volume_id=volume_id,
        volume_mount_path="/workspace/private",
        docker_start_cmd=docker_start_cmd
        or (
            "python",
            "-m",
            "operations.pod.pod_timer",
            "--timer-factory",
            timer_factory,
            "--bootstrap-command-json",
            bootstrap_command_json,
            "--report-path",
            report_path,
        ),
        hard_deadline=hard_deadline,
        repository_commit="b" * 40,
        metadata={BILLING_CUTOFF_MARGIN_ENV: str(cutoff_margin)},
    )


def configured_policy(**overrides: object) -> SpendPolicy:
    values = {
        "state": "configured",
        "max_hourly_usd": Decimal("1.00"),
        "max_estimated_metered_cost_usd": Decimal("2.00"),
        "account_balance_floor_usd": Decimal("50.00"),
        "account_balance_alert_usd": Decimal("75.00"),
        "hard_lifetime_seconds": 3600,
        "laptop_heartbeat_timeout_seconds": 30,
        "shutdown_poll_interval_seconds": 1,
        "shutdown_deadline_seconds": 8,
        "billing_cutoff_margin_seconds": 3600,
        "soft_max_seconds": 86_400,
        "hard_max_seconds": 86_400,
        "soft_max_cost_usd": Decimal("1000.00"),
        "hard_max_cost_usd": Decimal("1000.00"),
    }
    values.update(overrides)
    return SpendPolicy(**values)  # type: ignore[arg-type]


def configured_spend_toml(
    *, max_estimated_metered_cost_usd: str = "2.00", shutdown_deadline_seconds: int = 8
) -> str:
    return "\n".join(
        (
            'schema = "pod-spend.v4"',
            'state = "configured"',
            'currency = "USD"',
            'max_hourly_usd = "1.00"',
            f'max_estimated_metered_cost_usd = "{max_estimated_metered_cost_usd}"',
            'account_balance_floor_usd = "50.00"',
            'account_balance_alert_usd = "75.00"',
            "hard_lifetime_seconds = 3600",
            "laptop_heartbeat_timeout_seconds = 30",
            "shutdown_poll_interval_seconds = 1",
            f"shutdown_deadline_seconds = {shutdown_deadline_seconds}",
            "billing_cutoff_margin_seconds = 3600",
            "soft_max_seconds = 86400",
            "hard_max_seconds = 86400",
            'soft_max_cost_usd = "1000.00"',
            'hard_max_cost_usd = "1000.00"',
            "",
        )
    )


def verified_shutdown(
    provider: object, clock: SharedClock, *, timeout: float = 8, cutoff_margin: int = 3600
) -> VerifiedShutdown:
    return VerifiedShutdown(
        provider,  # type: ignore[arg-type]
        timeout_seconds=timeout,
        poll_seconds=1,
        billing_cutoff_margin_seconds=cutoff_margin,
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
        now=clock.now,
    )
