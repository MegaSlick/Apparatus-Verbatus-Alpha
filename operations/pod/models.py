"""Data-only values used by the pod runtime.

The values deliberately describe observations rather than making claims a
provider cannot support.  In particular, a close records charges captured
through a cutoff; it never claims that no future charge can appear.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Mapping

UTC = timezone.utc


class PodRuntimeError(RuntimeError):
    """Base error for a closed, named pod-runtime refusal."""


class ProviderFailure(PodRuntimeError):
    """The provider failed a requested observation or action."""


class LeaseOwnershipError(PodRuntimeError):
    """A lease owner attempted to overwrite another controller's record."""


class LeaseFormatError(PodRuntimeError):
    """A durable lease cannot be parsed safely."""


class SpendRefusal(PodRuntimeError):
    """A paid action is outside the configured spend guard."""


class BillingState(StrEnum):
    """What the billing endpoint actually made observable at the cutoff."""

    CAPTURED = "captured"
    PENDING_RECONCILIATION = "pending-reconciliation"
    UNAVAILABLE = "unavailable"


class Presence(StrEnum):
    """A provider observation, including the important unknown state."""

    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


class CloseState(StrEnum):
    """Terminal results of a shutdown attempt; only VERIFIED is green."""

    VERIFIED = "verified"
    PENDING_RECONCILIATION = "pending-reconciliation"
    UNVERIFIED_BILLING = "unverified-billing"
    FAILED_SHUTDOWN = "failed-shutdown"


def utc_now() -> datetime:
    """Return an aware UTC timestamp, kept injectable at callers for tests."""

    return datetime.now(UTC)


def require_utc(value: datetime, label: str) -> datetime:
    """Reject ambiguous timestamps instead of silently assuming a timezone."""

    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{label} must be an aware UTC timestamp")
    if value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"{label} must be in UTC")
    return value


def as_decimal(value: Decimal | str | int | float, label: str) -> Decimal:
    """Parse a non-negative monetary value without binary-float arithmetic."""

    try:
        parsed = Decimal(str(value))
    except Exception as error:  # pragma: no cover - Decimal's exact subclasses vary
        raise ValueError(f"{label} is not decimal money") from error
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"{label} must be finite and non-negative")
    return parsed


@dataclass(frozen=True, slots=True)
class PodCreateRequest:
    """The complete requested pod shape, before a provider sees it.

    ``interruptible`` is typed and checked here because allowing a spot reclaim
    is a silent-loss path.  A volume is supplied at creation time; there is no
    later attach operation in this runtime.
    """

    name: str
    gpu_type: str
    image: str
    volume_id: str
    volume_mount_path: str
    docker_start_cmd: tuple[str, ...]
    hard_deadline: datetime
    repository_commit: str
    template: str | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)
    interruptible: bool = False
    recovery_only: bool = False

    def __post_init__(self) -> None:
        for label, value in (
            ("name", self.name),
            ("gpu_type", self.gpu_type),
            ("image", self.image),
            ("volume_id", self.volume_id),
            ("volume_mount_path", self.volume_mount_path),
            ("repository_commit", self.repository_commit),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{label} must be a non-blank string")
        if self.template is not None and (
            not isinstance(self.template, str) or not self.template.strip()
        ):
            raise ValueError("template must be a non-blank string when supplied")
        if not self.docker_start_cmd or not all(
            isinstance(part, str) and part for part in self.docker_start_cmd
        ):
            raise ValueError("docker_start_cmd must contain non-blank command parts")
        if self.docker_start_cmd[:3] != ("python", "-m", "operations.pod.pod_timer"):
            raise ValueError(
                "docker_start_cmd must make operations.pod.pod_timer the primary process"
            )
        _required_timer_arguments(self.docker_start_cmd, self.volume_mount_path)
        if self.interruptible is not False:
            raise ValueError("pod runtime requires interruptible=false")
        if not isinstance(self.recovery_only, bool):
            raise ValueError("recovery_only must be boolean")
        require_utc(self.hard_deadline, "hard_deadline")
        if re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", self.image) is None:
            raise ValueError("image must be pinned by immutable sha256 digest")
        if len(self.repository_commit) != 40 or any(
            character not in "0123456789abcdef" for character in self.repository_commit
        ):
            raise ValueError("repository_commit must be a full lowercase Git SHA-1")
        if not isinstance(self.metadata, Mapping) or not all(
            isinstance(key, str) and key and isinstance(value, str) and value
            for key, value in self.metadata.items()
        ):
            raise ValueError("metadata must contain non-blank string keys and values")
        for key in self.metadata:
            if key != "VERBATUS_LAUNCH_TOKEN" and _looks_like_credential_field(key):
                raise ValueError(
                    "pod metadata cannot carry a credential-like field; supply runtime capability out of tree"
                )

    def recovery_request(self) -> "PodCreateRequest":
        """Return a non-creating exact-token lookup request for crash reconciliation."""

        return replace(self, recovery_only=True)


def _required_timer_arguments(command: tuple[str, ...], volume_mount_path: str) -> None:
    """Require timer, bootstrap, factory, and durable-report wiring before billing.

    The provider can only start the primary command.  If the command does not
    carry all four facts, a successful provider create would leave a pod that
    cannot prove its bootstrap or its independent shutdown evidence.
    """

    values: dict[str, str] = {}
    for flag in ("--timer-factory", "--bootstrap-command-json", "--report-path"):
        indexes = [index for index, item in enumerate(command) if item == flag]
        if len(indexes) != 1 or indexes[0] + 1 >= len(command):
            raise ValueError(f"docker_start_cmd must contain exactly one {flag} value")
        value = command[indexes[0] + 1]
        if not value:
            raise ValueError(f"docker_start_cmd {flag} value must be non-blank")
        values[flag] = value
    if values["--timer-factory"].count(":") != 1:
        raise ValueError("pod timer factory must be module:callable")
    try:
        bootstrap = json.loads(values["--bootstrap-command-json"])
    except json.JSONDecodeError as error:
        raise ValueError("pod bootstrap command must be JSON argv") from error
    if (
        not isinstance(bootstrap, list)
        or not bootstrap
        or not all(isinstance(item, str) and item for item in bootstrap)
    ):
        raise ValueError("pod bootstrap command must be a non-empty string argv")
    raw_report_path = values["--report-path"]
    report_path = PurePosixPath(raw_report_path)
    volume_path = PurePosixPath(volume_mount_path)
    if (
        ".." in raw_report_path.split("/")
        or not volume_path.is_absolute()
        or not report_path.is_absolute()
        or report_path == volume_path
        or not report_path.is_relative_to(volume_path)
    ):
        raise ValueError("pod timer report path must be inside the attached volume mount")


_CREDENTIAL_MARKERS = ("key", "secret", "password", "credential", "bearer", "token")


def _looks_like_credential_field(value: str) -> bool:
    normalized = value.lower().replace("-", "_")
    return any(marker in normalized for marker in _CREDENTIAL_MARKERS)


def assert_nonsecret_receipt(value: Mapping[str, object]) -> None:
    """Controller receipts are durable evidence; capability material is forbidden.

    Shared by arming.py (checked when a receipt is first constructed) and
    lease.py (checked again when a persisted lease is reloaded, e.g. after a
    controller restart) — one marker list for both triggers.

    Nested lists are walked as well as nested mappings.  A receipt comes from a
    supplied controller harness and travels to both the durable lease and the
    operator's terminal, so a scrubber that stopped at the first list would pass
    ``{"controllers": [{"api_key": ...}]}`` — a shape a harness reaches by
    describing two controllers rather than by trying to evade anything.
    """

    for item_key, item_value in value.items():
        if not isinstance(item_key, str):
            raise ValueError("controller receipt keys must be strings")
        if _looks_like_credential_field(item_key):
            raise ValueError("controller receipt must not retain credential or token material")
        _assert_nonsecret_value(item_value)


def _assert_nonsecret_value(value: object) -> None:
    if isinstance(value, Mapping):
        assert_nonsecret_receipt(value)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_nonsecret_value(item)


@dataclass(frozen=True, slots=True)
class PendingCreateIntent:
    """Non-secret facts needed to recover one pre-armed create after a crash.

    Recovery is lookup-only: it carries the exact launch token but never any
    user-supplied environment values or provider capability.  A provider seam
    must not issue another POST for this request.
    """

    name: str
    gpu_type: str
    image: str
    volume_id: str
    volume_mount_path: str
    docker_start_cmd: tuple[str, ...]
    hard_deadline: datetime
    repository_commit: str
    launch_token: str
    pod_hourly_usd: Decimal
    volume_hourly_usd: Decimal
    template: str | None = None

    @classmethod
    def from_request(cls, request: PodCreateRequest, *, launch_token: str) -> "PendingCreateIntent":
        return cls(
            name=request.name,
            gpu_type=request.gpu_type,
            image=request.image,
            volume_id=request.volume_id,
            volume_mount_path=request.volume_mount_path,
            docker_start_cmd=request.docker_start_cmd,
            hard_deadline=request.hard_deadline,
            repository_commit=request.repository_commit,
            launch_token=launch_token,
            pod_hourly_usd=as_decimal(
                request.metadata["VERBATUS_POD_HOURLY_USD"], "pending create pod rate"
            ),
            volume_hourly_usd=as_decimal(
                request.metadata["VERBATUS_VOLUME_ONGOING_HOURLY_USD"], "pending create volume rate"
            ),
            template=request.template,
        )

    def __post_init__(self) -> None:
        # Reuse the request's closed validation surface rather than maintaining
        # a second, looser serialization format for recovery.
        object.__setattr__(
            self, "pod_hourly_usd", as_decimal(self.pod_hourly_usd, "pending create pod rate")
        )
        object.__setattr__(
            self,
            "volume_hourly_usd",
            as_decimal(self.volume_hourly_usd, "pending create volume rate"),
        )
        self.recovery_request()

    def recovery_request(self) -> PodCreateRequest:
        return PodCreateRequest(
            name=self.name,
            gpu_type=self.gpu_type,
            image=self.image,
            volume_id=self.volume_id,
            volume_mount_path=self.volume_mount_path,
            docker_start_cmd=self.docker_start_cmd,
            hard_deadline=self.hard_deadline,
            repository_commit=self.repository_commit,
            template=self.template,
            metadata={
                "VERBATUS_LAUNCH_TOKEN": self.launch_token,
                "VERBATUS_POD_HOURLY_USD": str(self.pod_hourly_usd),
                "VERBATUS_VOLUME_ONGOING_HOURLY_USD": str(self.volume_hourly_usd),
            },
            recovery_only=True,
        )

    def to_record(self) -> dict[str, object]:
        return {
            "name": self.name,
            "gpu_type": self.gpu_type,
            "image": self.image,
            "volume_id": self.volume_id,
            "volume_mount_path": self.volume_mount_path,
            "docker_start_cmd": list(self.docker_start_cmd),
            "hard_deadline": self.hard_deadline.isoformat().replace("+00:00", "Z"),
            "repository_commit": self.repository_commit,
            "launch_token": self.launch_token,
            "pod_hourly_usd": str(self.pod_hourly_usd),
            "volume_hourly_usd": str(self.volume_hourly_usd),
            "template": self.template,
        }

    @classmethod
    def from_record(cls, value: object) -> "PendingCreateIntent":
        if not isinstance(value, dict):
            raise LeaseFormatError("pending create intent must be an object")
        required = {
            "name",
            "gpu_type",
            "image",
            "volume_id",
            "volume_mount_path",
            "docker_start_cmd",
            "hard_deadline",
            "repository_commit",
            "launch_token",
            "pod_hourly_usd",
            "volume_hourly_usd",
            "template",
        }
        if set(value) != required:
            raise LeaseFormatError("pending create intent has missing or unknown fields")
        command = value["docker_start_cmd"]
        if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
            raise LeaseFormatError("pending create command must be a string list")
        deadline = value["hard_deadline"]
        if not isinstance(deadline, str):
            raise LeaseFormatError("pending create deadline must be RFC3339 UTC")
        try:
            parsed = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
            return cls(
                name=value["name"],
                gpu_type=value["gpu_type"],
                image=value["image"],
                volume_id=value["volume_id"],
                volume_mount_path=value["volume_mount_path"],
                docker_start_cmd=tuple(command),
                hard_deadline=require_utc(parsed, "pending create hard_deadline"),
                repository_commit=value["repository_commit"],
                launch_token=value["launch_token"],
                pod_hourly_usd=as_decimal(value["pod_hourly_usd"], "pending create pod rate"),
                volume_hourly_usd=as_decimal(
                    value["volume_hourly_usd"], "pending create volume rate"
                ),
                template=value["template"],
            )
        except (TypeError, ValueError) as error:
            raise LeaseFormatError(f"pending create intent is invalid: {error}") from error


@dataclass(frozen=True, slots=True)
class PodRuntimeContract:
    """Provider-observed immutable runtime facts required for a safe paid pod."""

    interruptible: bool
    gpu_type: str
    image: str
    volume_id: str
    volume_mount_path: str
    docker_start_cmd: tuple[str, ...]
    template: str | None = None

    def __post_init__(self) -> None:
        if self.interruptible is not False:
            raise ValueError("observed pod must be non-interruptible")
        for label, value in (
            ("observed gpu_type", self.gpu_type),
            ("observed image", self.image),
            ("observed volume_id", self.volume_id),
            ("observed volume_mount_path", self.volume_mount_path),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(f"{label} must be non-blank")
        if self.template is not None and (not isinstance(self.template, str) or not self.template):
            raise ValueError("observed template must be non-blank when supplied")
        if not self.docker_start_cmd or not all(
            isinstance(item, str) and item for item in self.docker_start_cmd
        ):
            raise ValueError("observed docker_start_cmd must be non-empty string argv")

    def matches(self, request: PodCreateRequest) -> bool:
        return (
            self.interruptible is False
            and self.gpu_type == request.gpu_type
            and self.image == request.image
            and self.volume_id == request.volume_id
            and self.volume_mount_path == request.volume_mount_path
            and self.docker_start_cmd == request.docker_start_cmd
            and self.template == request.template
        )


@dataclass(frozen=True, slots=True)
class PodEstimate:
    """A provider price observation used by the launch and adopt gate."""

    pod_hourly_usd: Decimal
    volume_hourly_usd: Decimal
    source: str
    observed_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "pod_hourly_usd", as_decimal(self.pod_hourly_usd, "pod hourly"))
        object.__setattr__(
            self, "volume_hourly_usd", as_decimal(self.volume_hourly_usd, "volume hourly")
        )
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("estimate source must be non-blank")
        require_utc(self.observed_at, "estimate observed_at")


@dataclass(frozen=True, slots=True)
class PodRecord:
    """The exact pod a provider created or exposed for adoption."""

    pod_id: str
    name: str
    estimate: PodEstimate
    volume_id: str
    created_at: datetime
    state: str = "running"
    runtime_contract: PodRuntimeContract | None = None

    def __post_init__(self) -> None:
        for label, value in (
            ("pod_id", self.pod_id),
            ("name", self.name),
            ("volume_id", self.volume_id),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{label} must be a non-blank string")
        require_utc(self.created_at, "pod created_at")


@dataclass(frozen=True, slots=True)
class ProviderStatus:
    """The result of the exact-pod GET observation."""

    pod_id: str
    presence: Presence
    observed_at: datetime
    detail: str = ""
    http_status: int | None = None

    def __post_init__(self) -> None:
        require_utc(self.observed_at, "provider status observed_at")


@dataclass(frozen=True, slots=True)
class AbsenceObservation:
    """The independent list-membership observation used at close."""

    pod_id: str
    presence: Presence
    observed_at: datetime
    detail: str = ""

    def __post_init__(self) -> None:
        require_utc(self.observed_at, "absence observation observed_at")


@dataclass(frozen=True, slots=True)
class CostLine:
    """One provider-computed billing record, never a local timestamp estimate."""

    amount_usd: Decimal
    description: str
    recorded_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount_usd", as_decimal(self.amount_usd, "billing amount"))
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError("billing description must be non-blank")
        if self.recorded_at is not None:
            require_utc(self.recorded_at, "billing recorded_at")


@dataclass(frozen=True, slots=True)
class CostCapture:
    """Actual billing records captured through a named cutoff."""

    pod_id: str
    state: BillingState
    cutoff_at: datetime
    lines: tuple[CostLine, ...] = ()
    reason: str = ""
    source: str = "provider billing"
    window_start_at: datetime | None = None

    def __post_init__(self) -> None:
        require_utc(self.cutoff_at, "billing cutoff_at")
        if self.window_start_at is not None:
            require_utc(self.window_start_at, "billing window start")
            if self.window_start_at >= self.cutoff_at:
                raise ValueError("billing window start must precede billing cutoff")
        if self.state is BillingState.CAPTURED and not self.lines:
            raise ValueError("captured billing must contain at least one provider record")
        if self.state is not BillingState.CAPTURED and self.lines:
            raise ValueError("non-captured billing cannot contain cost lines")

    @property
    def total_usd(self) -> Decimal | None:
        if self.state is not BillingState.CAPTURED:
            return None
        return sum((line.amount_usd for line in self.lines), Decimal("0"))
