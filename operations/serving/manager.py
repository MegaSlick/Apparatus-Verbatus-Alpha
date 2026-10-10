"""Start one configured vLLM chair, prove it answers, then publish its receipt.

It never ranks chairs or retries with another recipe. Pre-launch validation errors (such as a discoverable local environment
file) propagate as they are; after that, every start failure becomes a refusal naming
the requested chair; a refusal the registry raised is re-raised unchanged so its reason
survives, unless cleanup could not be verified, in which case both reasons travel in one new
refusal. An interrupt is not a chair refusal, because the operator caused it.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Final, Mapping, NoReturn, Protocol

from common.chairs.errors import ChairRefusal, UnresolvedChairRefusal
from common.chairs.models import (
    ChairIdentity,
    ServingDetails,
    ServingReceipt,
    VerifiedSnapshot,
    is_sha256,
)
from common.contracts.canonical import canonical_bytes
from common.contracts.serving import SERVING_LAUNCH_AUDIT_SCHEMA
from common.credentials import log_word_carries_credential, looks_like_credential_field

from .capacity import CapacityPlan, planned_ceiling
from .config import (
    QUANTIZATION_CHECKPOINT_DECLARATIONS,
    FixtureProfile,
    InProcessProfile,
    ServingConfigInputs,
    ServingProfile,
    ServingRecipes,
    SubprocessProfile,
    UnsupportedProfile,
    chair_preflight_identity_digest,
    frozen_json,
    model_and_tokenizer_pins,
    seal_json_object,
    thawed_json,
)
from .errors import (
    EndpointOccupiedError,
    ProcessLaunchError,
    ReadinessError,
    ReceiptPublicationError,
    ResidencyError,
    RuntimePinError,
    ServiceStopError,
    ServingConfigurationError,
    ServingError,
)
from .http import (
    EndpointUnavailable,
    HttpResponse,
    HttpTransport,
    OpenAIResult,
    chat_image_bytes_all,
    endpoint_for_probe,
    health_url,
    models_url,
    outputs_sha256,
    parse_openai_answer,
    request_body,
    require_exact_model_id,
)
from .process import ProcessLauncher, ServerProcess
from .residency import ResidencyHandle, ResidencyLease

# Hybrid Mamba/attention checkpoints: with prefix caching on, the pinned vLLM
# reserves two recurrent-state pages per sequence instead of one
# (`MambaSpec.max_memory_usage_bytes`, "align" mode), memory these rows need
# for KV and concurrency. It turns prefix caching on by default for hybrids as
# for any model, so only the row's explicit `--no-enable-prefix-caching` keeps
# it off. A row for one of these with it on is refused at launch. Keyed by
# repository, not role, since tests reuse role names for fixture chairs.
_HYBRID_ATTENTION_REPOSITORIES = frozenset(
    {
        "datalab-to/chandra-ocr-2",
        "Qwen/Qwen3.8-27B",
        "Qwen/Qwen3.8-27B-FP8",
        "nvidia/Qwen3.8-27B-NVFP4",
    }
)

# Two launch-purpose tokens admit an `unproven` row; every other check still
# runs. The private one is held only by the serving smoke assembly (preflight
# qualification). The public one is passed by stage assembly for a run under
# --mechanics-qualification. The launch audit's `launch_purpose` records which
# token started the service, and `start` has no other bypass.
_PREFLIGHT_QUALIFICATION_PURPOSE: Final = object()
MECHANICS_QUALIFICATION_PURPOSE: Final = object()
_NORMAL_LAUNCH = "normal"
# The launch purpose of a service this manager took over rather than started
# (`ServingManager.start`, "A shared service" in the README).
ADOPTED_LAUNCH: Final = "adopted"
HAND_OFF_SCHEMA: Final = "serving-hand-off.v1"
_HAND_OFF_FIELDS: Final = frozenset(
    {
        "schema",
        "chair",
        "service_scope",
        "pid",
        "start_marker",
        "log_path",
        "endpoint",
        "receipt_reference",
        "audit_reference",
        "evidence_reference",
        "launch_audit",
    }
)
# What two identities serving one service may differ in: the role, and the
# catalogue key the role looks its row up by.
_ROLE_FIELDS: Final = frozenset({"role", "serving_recipe"})
_PREFLIGHT_QUALIFICATION_LAUNCH = "preflight-qualification"
_MECHANICS_QUALIFICATION_LAUNCH = "mechanics-qualification"
_LOG_UNREADABLE: Final = "VLLM_LOG_UNREADABLE:"
_READINESS_PROBE_TIMEOUT_SECONDS = 2.0
"""Per-request budget for one /health or /v1/models poll."""

_INFERENCE_TIMEOUT_SECONDS = 10.0
"""Per-request budget for one chat/completions call: a real answer, not a poll."""


class ReceiptPublisher(Protocol):
    """Durably publish receipt and operational evidence after service proof exists."""

    def publish(
        self, receipt: ServingReceipt, launch_audit: Mapping[str, object]
    ) -> "ReceiptPublication":
        """Return immutable references accepted by downstream stages."""


class PackageInspector(Protocol):
    """Small runtime-version seam; tests never import or install vLLM."""

    def version(self, package: str) -> str:
        """Return the installed distribution version or raise a concrete error."""


class InstalledPackages:
    """Production package inspector, deliberately lazy and side-effect free."""

    def version(self, package: str) -> str:
        try:
            return importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError as error:
            raise RuntimePinError(f"required package {package!r} is not installed") from error


@dataclass(frozen=True, slots=True)
class ReceiptPublication:
    """Three immutable evidence references for one observed serving moment.

    The launch audit is separate because pid, argv, packages and readiness do
    not belong in the closed receipt schema.
    """

    receipt_reference: Mapping[str, str]
    audit_reference: Mapping[str, str]
    evidence_reference: Mapping[str, str]

    def __post_init__(self) -> None:
        for name, label in (
            ("receipt_reference", "receipt"),
            ("audit_reference", "launch-audit"),
            ("evidence_reference", "serving-evidence"),
        ):
            object.__setattr__(self, name, _immutable_reference(getattr(self, name), label))


@dataclass(frozen=True, slots=True)
class ReadinessEvidence:
    """The observations that prove this process, ID, and endpoint answered once."""

    health_status: int
    model_ids: tuple[str, ...]
    probe: OpenAIResult
    ready_at: str

    def to_record(self) -> dict[str, object]:
        return {
            "health_status": self.health_status,
            "model_ids": list(self.model_ids),
            "probe_response_sha256": self.probe.response_sha256,
            "probe_output_sha256": outputs_sha256(self.probe),
            "ready_at": self.ready_at,
        }


@dataclass(slots=True)
class ServiceHandle:
    """The one live service owned by a :class:`ServingManager` instance."""

    _manager: "ServingManager" = field(repr=False)
    identity: ChairIdentity
    profile: ServingProfile
    process: ServerProcess
    receipt: ServingReceipt
    receipt_reference: Mapping[str, str]
    launch_audit: Mapping[str, object]
    audit_reference: Mapping[str, str]
    evidence_reference: Mapping[str, str]
    # The launch log, named in a hand-off so the next manager can read its tail.
    log_path: Path | None = None
    _fixture_requests_completed: int = field(default=0, init=False, repr=False)
    _last_fixture_request_sha256: str | None = field(default=None, init=False, repr=False)
    _last_fixture_response: OpenAIResult | None = field(default=None, init=False, repr=False)

    @property
    def endpoint(self) -> str:
        return self.profile.endpoint

    def request_reading(self, kind: str, body_bytes: bytes, timeout_seconds: float) -> HttpResponse:
        """POST one already-built reading request and return the raw response.

        The caller retains and parses the bytes.
        """

        return self._manager.request_reading(self, kind, body_bytes, timeout_seconds)

    def stream_reading(
        self,
        kind: str,
        body_bytes: bytes,
        timeout_seconds: float,
        on_chunk: Callable[[bytes], bool],
    ) -> HttpResponse:
        """POST one already-built streamed request, handing its reply to ``on_chunk``
        as it arrives, and return the bytes received (``StreamingHttpTransport``).

        The caller retains and parses the bytes.
        """

        return self._manager.stream_reading(self, kind, body_bytes, timeout_seconds, on_chunk)

    @property
    def fixture_requests_completed(self) -> int:
        """Successful requests whose embedded image bytes matched a local fixture.

        Readiness probes and readings sent through :meth:`request_reading` are
        not counted; nothing else can send a request on this handle.
        """

        return self._fixture_requests_completed

    @property
    def last_fixture_request_sha256(self) -> str | None:
        """Digest of the exact local fixture bound to the latest successful request."""

        return self._last_fixture_request_sha256

    @property
    def last_fixture_response_sha256(self) -> str | None:
        """Opaque response token for the latest successful fixture request."""

        response = self._last_fixture_response
        return response.response_sha256 if response is not None else None

    @property
    def last_fixture_output_sha256(self) -> str | None:
        """Digest of semantic output for the latest successful fixture request."""

        response = self._last_fixture_response
        return outputs_sha256(response) if response is not None else None

    def request_fixture_image(
        self,
        kind: str,
        payload: Mapping[str, object],
        *,
        fixture: str | Path,
        sampling: Mapping[str, int | float],
        exchange_observer: Callable[[bytes, HttpResponse], None] | None = None,
    ) -> OpenAIResult:
        """Request this service with the actual chat image from ``fixture``.

        The pod smoke uses this so a passing page read cannot be a text-only
        request. The image must sit in a ``role=user`` content block; a stray
        field merely named ``image_url`` is refused. ``sampling`` is the chair's
        sealed decoding row, so the smoke reads the page as the run will.
        """

        if kind != "chat-completions":
            raise ServingConfigurationError(
                "golden-page fixture requests must use the chat-completions endpoint"
            )
        # Validate and send one snapshot, so a mutable Mapping cannot show an
        # image here and serialize without it.
        sealed_payload, _ = seal_json_object(payload, label="golden-page request")
        fixture_digest = hashlib.sha256(
            _local_fixture_bytes(fixture, "golden-page fixture")
        ).hexdigest()
        image_digest = hashlib.sha256(
            _active_chat_image_bytes(sealed_payload, label="golden-page request")
        ).hexdigest()
        if image_digest != fixture_digest:
            raise ServingConfigurationError(
                "golden-page request image bytes do not match its supplied local fixture"
            )
        result = self._manager._request(
            self, kind, sealed_payload, sampling=sampling, exchange_observer=exchange_observer
        )
        self._fixture_requests_completed += 1
        self._last_fixture_request_sha256 = fixture_digest
        self._last_fixture_response = result
        return result

    def stop(self) -> None:
        """Stop exactly this owned process; no pattern search is ever used."""

        self._manager.stop(self)

    def hand_off(self) -> bool:
        """Leave this service running for the next stage's process; see `ServingManager.hand_off`."""

        return self._manager.hand_off(self)


class StageContextReceiptPublisher:
    """Publishes through ``StageContext``: receipt, launch audit and evidence manifest.

    The launch audit is its own content-addressed blob, outside the closed
    receipt schema.
    """

    def __init__(self, context: Any) -> None:
        self.context = context

    def publish(
        self, receipt: ServingReceipt, launch_audit: Mapping[str, object]
    ) -> ReceiptPublication:
        receipt_reference = _object_reference(
            self.context.write_serving_receipt(receipt.identity, receipt.details), "receipt"
        )
        write_audit = self._seam("write_serving_launch_audit", "launch-audit")
        audit_reference = _object_reference(write_audit(dict(launch_audit)), "launch-audit")
        write_evidence = self._seam("write_serving_evidence_manifest", "evidence-manifest")
        evidence_reference = _object_reference(
            write_evidence(dict(receipt_reference), dict(audit_reference)),
            "serving evidence-manifest",
        )
        return ReceiptPublication(
            dict(receipt_reference), dict(audit_reference), dict(evidence_reference)
        )

    def _seam(self, name: str, label: str) -> Callable[..., object]:
        write = getattr(self.context, name, None)
        if not callable(write):
            raise ReceiptPublicationError(f"StageContext has no serving {label} publication seam")
        return write


def _object_reference(reference: object, label: str) -> Mapping[str, str]:
    if not isinstance(reference, Mapping):
        raise ReceiptPublicationError(f"StageContext returned a non-object {label} reference")
    return reference


class ServingManager:
    """A sequential vLLM lifecycle manager.

    :meth:`start` verifies the chair's snapshot just before launch. Only full
    checkpoints are served; the roster refuses an adapter chair when it is parsed.
    """

    def __init__(
        self,
        *,
        registry: Any,
        recipes: ServingRecipes,
        config_inputs: ServingConfigInputs,
        launcher: ProcessLauncher,
        http: HttpTransport,
        receipt_publisher: ReceiptPublisher,
        log_root: str | Path,
        package_inspector: PackageInspector | None = None,
        command_prefix: tuple[str, ...] | None = None,
        residency_lease: ResidencyLease,
        producer: str = "operations.serving.manager",
        now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
        shutdown_timeout_seconds: float = 10.0,
        capacity_plan: CapacityPlan | None = None,
        placement_table: Any | None = None,
        hand_off_path: str | Path | None = None,
        service_scope: str | None = None,
        _launch_purpose: object | None = None,
    ) -> None:
        if command_prefix is None:
            # This interpreter, so the launched vLLM is the one whose version
            # was inspected; a PATH console script could belong to another venv.
            # `vllm.entrypoints.cli.main`, not `vllm`: the vLLM wheel ships no
            # `vllm/__main__.py`, so `-m vllm` has no launch target and fails.
            command_prefix = (sys.executable, "-m", "vllm.entrypoints.cli.main")
        if (
            not isinstance(command_prefix, tuple)
            or not command_prefix
            or any(not isinstance(item, str) or not item for item in command_prefix)
            or not Path(command_prefix[0]).is_absolute()
        ):
            raise ValueError("vLLM command_prefix must start with an absolute interpreter path")
        if package_inspector is None and command_prefix[0] != sys.executable:
            # Exact strings: two venvs can symlink one interpreter with
            # different site-packages.
            raise ValueError(
                "the default package inspector reads this interpreter's installed "
                "distributions, so a supplied vLLM command_prefix must launch "
                f"{sys.executable!r}; to launch another interpreter, supply the "
                "PackageInspector for the environment it launches"
            )
        if not isinstance(producer, str) or not producer.strip():
            raise ValueError("serving audit producer must be a non-blank string")
        if shutdown_timeout_seconds <= 0:
            raise ValueError("shutdown timeout must be positive")
        if residency_lease is None:
            raise ValueError("serving manager requires an explicit pod/GPU-scoped residency lease")
        if not isinstance(config_inputs, ServingConfigInputs):
            raise ValueError("serving manager requires exact sealed serving configuration inputs")
        if _launch_purpose not in (
            None,
            _PREFLIGHT_QUALIFICATION_PURPOSE,
            MECHANICS_QUALIFICATION_PURPOSE,
        ):
            raise ValueError("serving launch purpose is not a recognized qualification purpose")
        if capacity_plan is not None:
            capacity_plan.require_inputs(config_inputs)
            if placement_table is None:
                raise ValueError(
                    "a capacity plan needs the sealed placement table, whose ceilings bound it"
                )
        self.registry = registry
        self.recipes = recipes
        # The measured card's plan, or None: then every row launches exactly as written.
        self.capacity_plan = capacity_plan
        self.placement_table = placement_table
        self.config_inputs = config_inputs
        self.launcher = launcher
        self.http = http
        self.receipt_publisher = receipt_publisher
        self.log_root = Path(log_root)
        self.package_inspector = package_inspector or InstalledPackages()
        self.command_prefix = command_prefix
        self.residency_lease = residency_lease
        self.producer = producer
        self.now = now or (lambda: datetime.now(UTC))
        self.monotonic = monotonic or time.monotonic
        self.sleep = sleep or time.sleep
        self.shutdown_timeout_seconds = shutdown_timeout_seconds
        self.launch_purpose = {
            _PREFLIGHT_QUALIFICATION_PURPOSE: _PREFLIGHT_QUALIFICATION_LAUNCH,
            MECHANICS_QUALIFICATION_PURPOSE: _MECHANICS_QUALIFICATION_LAUNCH,
        }.get(_launch_purpose, _NORMAL_LAUNCH)
        self._qualification_launch = self.launch_purpose != _NORMAL_LAUNCH
        # Where a running service is handed to the next process, and which run
        # may take it over; with either unset this manager neither hands a
        # service off nor takes one over.
        self.hand_off_path = Path(hand_off_path) if hand_off_path is not None else None
        self.service_scope = service_scope
        # True while the active service was taken over: its lease is held by the
        # service's own processes, so stopping it leaves the lease to them
        # instead of releasing it (`_leave_adopted_lease`).
        self._adopted = False
        self._active: ServiceHandle | None = None
        self._residency_handle: ResidencyHandle | None = None
        self._unready_process: ServerProcess | None = None
        self._unready_endpoint = ""

    def start(self, identity: ChairIdentity, tier: str) -> ServiceHandle:
        """Start one configured chair and publish its receipt after a real answer.

        A handle returns only after health, the exact model id, a bounded probe
        and receipt publication have all succeeded.
        """

        # An invented value names no chair to refuse, so check it first.
        if not isinstance(identity, ChairIdentity):
            raise ServingConfigurationError("serving start requires one resolved ChairIdentity")
        if not isinstance(tier, str) or not tier:
            raise ServingConfigurationError("serving start requires one non-blank placement tier")
        assert_no_discoverable_local_env()
        if self._active is not None or self._residency_handle is not None:
            # Not failed-launch cleanup: the held lease records an unverified shutdown.
            self._refuse_start(
                identity,
                ServingConfigurationError(
                    "a serving process is still resident or its shutdown is not verified; "
                    "stop and verify it before starting another chair"
                ),
            )

        process: ServerProcess | None = None
        endpoint = ""
        service_events: dict[str, object] = {}
        try:
            # The recipe check comes before any snapshot is verified.
            row = self._launchable_profile(identity, tier)
            # Proof marks and digests are checked on the row; the plan only widens it.
            profile = (
                self.capacity_plan.launch_profile(row, identity.role)
                if self.capacity_plan is not None
                else row
            )
            if self.capacity_plan is not None:
                self._plan_ceilings(profile)
            self._assert_runtime(profile)
            adopted = self._take_over_or_clear(identity, profile, service_events, row)
            if adopted is not None:
                return adopted
            snapshot = self.registry.ensure(identity)
            assert_processor_geometry(snapshot, profile)
            assert_quantization(snapshot, profile)
            endpoint = profile.endpoint
            # Held from endpoint probing through failed-launch cleanup, or two
            # assemblers can race from an empty endpoint into GPU co-residency.
            self._residency_handle = self.residency_lease.acquire(identity)
            self._assert_endpoint_unoccupied(endpoint)
            argv = render_vllm_argv(
                command_prefix=self.command_prefix, profile=profile, snapshot=snapshot
            )
            log_path = self._next_log_path(identity)
            process = self.launcher.launch(
                argv,
                log_path,
                inheritable_fds=(self._residency_fd(),),
            )
            started_at = _utc_stamp(self.now())
            readiness = self._wait_until_ready(process, profile)
            observed_packages = self._assert_runtime(profile)
            self._assert_process_live(process)
            details = ServingDetails(
                tokenizer_revision=identity.receipt_revision,
                seed=profile.seed,
                context_cap=profile.max_model_len,
                # A total pixel count. `pixel_cap` in config/pod_placement.toml
                # is a longest edge; the two are not comparable directly.
                pixel_cap=profile.max_pixels,
                engine="vllm",
                engine_version=observed_packages["vllm"],
                dtype=profile.dtype,
                adapter_identity=None,
                endpoint=endpoint,
                started_at=started_at,
            )
            receipt = self.registry.receipt(identity, details)
            audit = {
                **self._launch_audit(
                    identity=identity,
                    profile=profile,
                    process=process,
                    argv=argv,
                    readiness=readiness.to_record(),
                    snapshot=snapshot,
                    runtime_packages=observed_packages,
                    started_at=started_at,
                    row=row,
                ),
                **service_events,
            }
            sealed_audit = frozen_json(audit)
            publication = self._publish(receipt, audit)
            handle = ServiceHandle(
                self,
                identity,
                profile,
                process,
                receipt,
                publication.receipt_reference,
                sealed_audit,
                publication.audit_reference,
                publication.evidence_reference,
                log_path=log_path,
            )
            self._active = handle
            return handle
        except ChairRefusal as error:
            # Already a refusal. An unverified cleanup rides along in one refusal;
            # a second refusal would mask the first.
            cleanup_error = self._attempt_cleanup(process, endpoint)
            if cleanup_error is not None:
                self._refuse(identity, error, also=cleanup_error)
            raise
        except ServingError as error:
            self._refuse_start(identity, error, also=self._attempt_cleanup(process, endpoint))
        except Exception as error:
            self._refuse_start(
                identity,
                ProcessLaunchError(
                    f"unexpected serving start failure: {type(error).__name__}: {error}"
                ),
                also=self._attempt_cleanup(process, endpoint),
            )
        except BaseException as error:
            # An interrupt must not strand a child. If cleanup is verified, the
            # interrupt is re-raised unchanged. If not, a possibly resident child
            # matters more, so the interrupt becomes a ServiceStopError; callers
            # that catch Exception then record it instead of unwinding, and the
            # retained lease makes later starts refuse.
            cleanup_error = self._attempt_cleanup(process, endpoint)
            if cleanup_error is not None:
                raise ServiceStopError(
                    "serving start was interrupted and cleanup could not be verified: "
                    f"start={type(error).__name__}: {error}; stop={cleanup_error}"
                ) from error
            raise

    def _take_over_or_clear(
        self,
        identity: ChairIdentity,
        profile: ServingProfile,
        events: dict[str, object],
        row: ServingProfile | None = None,
    ) -> ServiceHandle | None:
        """Take over a handed-off service this row shares, or stop whatever was handed off.

        A refused take-over, and a handed-off service stopped to make room, are
        noted in `events`, which the launch audit of the start that follows carries.
        """

        try:
            record = self._read_hand_off()
        except _HandOffUnreadable as error:
            self._remove_hand_off()
            events["hand_off_discarded"] = str(error)
            return None
        if record is None:
            return None
        if profile.shares_service_with is not None:
            try:
                return self._adopt(identity, profile, record, row)
            except _AdoptionRefused as refused:
                events["adoption_refused"] = str(refused)
        events["displaced_service"] = self._displace(record, identity)
        return None

    def _adopt(
        self,
        identity: ChairIdentity,
        profile: ServingProfile,
        record: Mapping[str, Any],
        row: ServingProfile | None = None,
    ) -> ServiceHandle:
        """Take over the handed-off service, publishing a receipt and audit for `identity`.

        Every check runs before anything is published, and any failure is an
        `_AdoptionRefused` naming why; the caller then stops that service and
        starts this row itself.
        """

        def refuse(reason: str) -> NoReturn:
            raise _AdoptionRefused(reason)

        if record["chair"] != profile.shares_service_with:
            refuse(
                f"the handed-off service is chair {record['chair']!r}'s, and this row shares "
                f"{profile.shares_service_with!r}'s"
            )
        if self.service_scope is None or record["service_scope"] != self.service_scope:
            refuse("the service was handed off by another run")
        audit = record["launch_audit"]
        if hashlib.sha256(canonical_bytes(audit)).hexdigest() != record["audit_reference"].get(
            "sha256"
        ):
            refuse("the hand-off's launch audit is not the one its reference names")
        if audit.get("configuration_inputs") != self.config_inputs.to_record():
            refuse("the service was launched under other sealed serving configuration")
        served_identity = audit.get("chair_identity")
        if not isinstance(served_identity, Mapping) or _without_role(
            served_identity
        ) != _without_role(identity.to_record()):
            refuse(
                f"chair {record['chair']!r} serves another checkpoint than {identity.role!r} "
                "is configured with"
            )
        launched = audit.get("profile")
        if not isinstance(launched, Mapping) or launched.get("tier") != profile.tier:
            refuse("the service was launched at another placement tier")
        try:
            snapshot = self.registry.ensure(identity)
            assert_processor_geometry(snapshot, profile)
            assert_quantization(snapshot, profile)
            argv = render_vllm_argv(
                command_prefix=self.command_prefix, profile=profile, snapshot=snapshot
            )
            observed_packages = self._assert_runtime(profile)
        except (ChairRefusal, ServingError) as error:
            refuse(f"this chair could not be checked against it: {error}")
        command = audit.get("command")
        if not isinstance(command, Mapping) or command.get("argv_sha256") != _argv_sha256(argv):
            refuse(
                "this row and snapshot render another vLLM command than the one the service "
                "was launched with"
            )
        runtime = audit.get("runtime_packages")
        if not isinstance(runtime, Mapping) or runtime.get("observed") != observed_packages:
            refuse("the installed packages differ from those the service was launched under")
        try:
            process = self.launcher.attach(
                record["pid"], record["start_marker"], Path(record["log_path"])
            )
        except ProcessLaunchError as error:
            refuse(f"the handed-off process is gone: {error}")
        if process.poll() is not None:
            # Its group still runs (an engine process outlived the server), so it
            # is stopped, not taken over.
            refuse("the handed-off server has exited, though its process group still runs")
        endpoint = profile.endpoint
        try:
            health = self._get(health_url(endpoint), _READINESS_PROBE_TIMEOUT_SECONDS)
            if health.status != 200:
                refuse(f"/health answered HTTP {health.status}")
            models = self._get(models_url(endpoint), _READINESS_PROBE_TIMEOUT_SECONDS)
            model_ids = require_exact_model_id(models, profile.served_model_id)
        except (EndpointUnavailable, ServingError) as error:
            refuse(f"the handed-off service did not answer: {error}")
        # Consumed before publishing, so no later start stops a service this
        # manager now holds; from here a failure stops it before refusing.
        self._remove_hand_off()
        adopted_at = _utc_stamp(self.now())
        try:
            details = ServingDetails(
                tokenizer_revision=identity.receipt_revision,
                seed=profile.seed,
                context_cap=profile.max_model_len,
                pixel_cap=profile.max_pixels,
                engine="vllm",
                engine_version=observed_packages["vllm"],
                dtype=profile.dtype,
                adapter_identity=None,
                endpoint=endpoint,
                started_at=audit["started_at"],
            )
            receipt = self.registry.receipt(identity, details)
            adopted_audit = {
                **self._launch_audit(
                    identity=identity,
                    profile=profile,
                    process=process,
                    argv=argv,
                    readiness=audit["readiness"],
                    snapshot=snapshot,
                    runtime_packages=observed_packages,
                    started_at=audit["started_at"],
                    row=row,
                ),
                "launch_purpose": ADOPTED_LAUNCH,
                "adoption": {
                    "from_chair": record["chair"],
                    "receipt_reference": dict(record["receipt_reference"]),
                    "audit_reference": dict(record["audit_reference"]),
                    "evidence_reference": dict(record["evidence_reference"]),
                    "launched_for": audit.get("launch_purpose"),
                    "adopted_for": self.launch_purpose,
                    "adopted_at": adopted_at,
                    "health_status": health.status,
                    "model_ids": list(model_ids),
                },
            }
            publication = self._publish(receipt, adopted_audit)
        except BaseException as error:
            stop_failure = self._attempt_cleanup_adopted(process, endpoint, identity)
            if stop_failure is not None:
                raise stop_failure from error
            if isinstance(error, Exception):
                refuse(f"the take-over could not be recorded: {type(error).__name__}: {error}")
            raise
        handle = ServiceHandle(
            self,
            identity,
            profile,
            process,
            receipt,
            publication.receipt_reference,
            frozen_json(adopted_audit),
            publication.audit_reference,
            publication.evidence_reference,
            log_path=Path(record["log_path"]),
        )
        self._active = handle
        self._adopted = True
        return handle

    def _displace(
        self, record: Mapping[str, Any], identity: ChairIdentity | None
    ) -> Mapping[str, object]:
        """Stop a handed-off service and consume its record; what was done, for the audit.

        The record is removed only once the card's lease is proved free: a process
        still holding it (an engine outliving its server) keeps the record, so a
        later start can still find and stop that group.
        """

        noted = {"chair": record["chair"], "pid": record["pid"]}
        try:
            process = self.launcher.attach(
                record["pid"], record["start_marker"], Path(record["log_path"])
            )
        except ProcessLaunchError:
            self._prove_lease_free(identity)
            self._remove_hand_off()
            return {**noted, "outcome": "already-exited"}
        self._stop_process(process)
        self._assert_endpoint_absent(str(record["endpoint"]))
        self._prove_lease_free(identity)
        self._remove_hand_off()
        return {**noted, "outcome": "stopped"}

    def _attempt_cleanup_adopted(
        self, process: ServerProcess, endpoint: str, identity: ChairIdentity
    ) -> ServiceStopError | None:
        """Stop a service taken over moments ago, before anything names it."""

        try:
            self._stop_process(process)
            self._assert_endpoint_absent(endpoint)
            self._prove_lease_free(identity)
        except BaseException as error:
            if isinstance(error, ServiceStopError):
                return error
            return ServiceStopError(
                f"a taken-over service could not be stopped: {type(error).__name__}: {error}"
            )
        return None

    def _prove_lease_free(self, identity: ChairIdentity | None) -> None:
        """After a handed-off service stopped, show its lease went with it."""

        held = self._await_lease_free(identity)
        if held is not None:
            raise ServiceStopError(
                f"the handed-off service is stopped or gone, but the card's lease is still "
                f"held: {held}"
            ) from held

    def _await_lease_free(self, identity: ChairIdentity | None) -> ResidencyError | None:
        """Wait up to the shutdown timeout for the card's lease to come free.

        Returns `None` once it was acquired and released, or the last refusal
        when it is still held. The lease a handed-off service carries is the
        launching manager's descriptor, inherited by the service's processes;
        one of them that left the service's process group can keep it a little
        past the group's end.
        """

        deadline = self.monotonic() + self.shutdown_timeout_seconds
        while True:
            try:
                self.residency_lease.acquire(identity).release()  # type: ignore[arg-type]
            except ResidencyError as error:
                held = error
            else:
                return None
            if self.monotonic() >= deadline:
                return held
            self.sleep(self._time_left(deadline, 0.25))

    def _read_hand_off(self) -> Mapping[str, Any] | None:
        """The hand-off record beside the lease, `None` when there is none."""

        if self.hand_off_path is None:
            return None
        try:
            descriptor = os.open(self.hand_off_path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise _HandOffUnreadable(f"the hand-off record could not be opened: {error}") from error
        try:
            with os.fdopen(descriptor, "rb") as stream:
                record = json.loads(stream.read())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise _HandOffUnreadable(f"the hand-off record could not be read: {error}") from error
        if (
            not isinstance(record, dict)
            or set(record) != _HAND_OFF_FIELDS
            or record["schema"] != HAND_OFF_SCHEMA
            or type(record["pid"]) is not int
            or record["pid"] <= 1
            or not all(
                isinstance(record[name], str) and record[name]
                for name in ("chair", "service_scope", "start_marker", "log_path", "endpoint")
            )
            or not all(
                isinstance(record[name], dict)
                for name in (
                    "receipt_reference",
                    "audit_reference",
                    "evidence_reference",
                    "launch_audit",
                )
            )
        ):
            raise _HandOffUnreadable("the hand-off record is not its closed schema")
        return record

    def _remove_hand_off(self) -> None:
        if self.hand_off_path is None:
            return
        try:
            self.hand_off_path.unlink(missing_ok=True)
        except OSError as error:
            raise ServiceStopError(
                f"the hand-off record {self.hand_off_path} could not be removed: {error}"
            ) from error

    def _publish(self, receipt: ServingReceipt, audit: Mapping[str, object]) -> ReceiptPublication:
        publication_audit, _ = seal_json_object(audit, label="serving launch audit")
        try:
            publication = self.receipt_publisher.publish(receipt, publication_audit)
        except Exception as error:
            raise ReceiptPublicationError(
                f"receipt publisher refused ready service: {error}"
            ) from error
        if not isinstance(publication, ReceiptPublication):
            raise ReceiptPublicationError(
                "receipt publisher must return receipt, durable launch-audit, "
                "and combined evidence references"
            )
        return publication

    def _request(
        self,
        handle: ServiceHandle,
        kind: str,
        payload: Mapping[str, object],
        *,
        sampling: Mapping[str, int | float],
        exchange_observer: Callable[[bytes, HttpResponse], None] | None = None,
    ) -> OpenAIResult:
        """Send a non-streaming request to the handle's exact served alias, under
        the chair's sealed sampling values and the profile seed. Only the golden-page
        fixture request comes through here."""

        self._require_active(handle)
        self._assert_process_live(handle.process)
        body = request_body(
            payload,
            model_id=handle.profile.served_model_id,
            seed=handle.profile.seed,
            deterministic=False,
            sampling=sampling,
        )
        response = self._post(handle.endpoint, kind, body, _INFERENCE_TIMEOUT_SECONDS)
        if exchange_observer is not None:
            exchange_observer(body, response)
        return parse_openai_answer(
            response, kind=kind, expected_model_id=handle.profile.served_model_id
        )

    def request_reading(
        self, handle: ServiceHandle, kind: str, body_bytes: bytes, timeout_seconds: float
    ) -> HttpResponse:
        """POST a caller-built request body and return the unparsed response.

        Nothing is parsed here, so a malformed body reaches the caller's parser.
        """

        self._require_active(handle)
        self._assert_process_live(handle.process)
        return self._post(handle.endpoint, kind, body_bytes, timeout_seconds)

    def stream_reading(
        self,
        handle: ServiceHandle,
        kind: str,
        body_bytes: bytes,
        timeout_seconds: float,
        on_chunk: Callable[[bytes], bool],
    ) -> HttpResponse:
        """POST a caller-built streamed request and return the unparsed bytes received.

        A transport that cannot stream is refused before anything is sent.
        """

        self._require_active(handle)
        self._assert_process_live(handle.process)
        stream = getattr(self.http, "stream", None)
        if not callable(stream):
            raise ServingConfigurationError(
                f"the serving transport {type(self.http).__name__} cannot stream a reply"
            )
        return stream(
            "POST",
            endpoint_for_probe(handle.endpoint, kind),
            body=body_bytes,
            timeout_seconds=timeout_seconds,
            on_chunk=on_chunk,
        )

    def stop(self, handle: ServiceHandle) -> None:
        """Stop one exact owned process and verify its endpoint no longer responds."""

        self._require_active(handle)
        try:
            self._stop_process(handle.process)
            self._assert_endpoint_absent(handle.endpoint)
            if self._adopted:
                self._leave_adopted_lease(handle)
            else:
                self._release_residency()
        except BaseException as error:
            # Keep the handle and the lease, so a failed shutdown cannot lead to
            # a second resident GPU process; `stop()` may be called again.
            if isinstance(error, ServiceStopError):
                raise
            # Include the type: an exception raised with no arguments has an
            # empty message.
            raise ServiceStopError(f"{type(error).__name__}: {error}") from error
        else:
            self._active = None
            self._adopted = False

    def _leave_adopted_lease(self, handle: ServiceHandle) -> None:
        """After a taken-over service stopped, leave its lease to whoever still holds it.

        This manager never held that lease: the launching manager's descriptor
        went to the service's processes, and only they can let it go. On
        2026-10-08 one of them still held it after the
        whole process group had exited and the endpoint was gone, and failing
        here cost the Coniector its seal although its service was stopped. The
        group and the endpoint are what this stop verifies; the lease stays
        the gate every later start must pass, so while such a process lives
        no server can start on the card. Its holders are named on stderr.
        """

        held = self._await_lease_free(handle.identity)
        if held is not None:
            print(
                f"serving: chair {handle.identity.role!r}'s taken-over service is stopped "
                f"and its endpoint is gone, but the card's lease is still held, so no "
                f"chair can start until it is free: {held}",
                file=sys.stderr,
            )

    def hand_off(self, handle: ServiceHandle) -> bool:
        """Leave this running service for the next stage's process to take over.

        Writes the hand-off record beside the lease and gives up this manager's
        lease descriptor without unlocking it: the service's own process
        inherited the lease, so the card stays leased while it lives. The next
        manager whose row shares this service takes it over
        (`_take_over_or_clear`); any other start stops it first. Returns
        `False`, with nothing changed, when the service cannot be handed over
        (no hand-off path or run scope, an exited process, a process whose start
        cannot be told apart from a later one, or a record already there); the
        caller then stops it as usual.
        """

        self._require_active(handle)
        process = handle.process
        if (
            self.hand_off_path is None
            or self.service_scope is None
            or self._adopted
            or self._residency_handle is None
            or handle.log_path is None
            or process.poll() is not None
            or process.start_marker is None
        ):
            return False
        record = {
            "schema": HAND_OFF_SCHEMA,
            "chair": handle.identity.role,
            "service_scope": self.service_scope,
            "pid": process.pid,
            "start_marker": process.start_marker,
            "log_path": str(handle.log_path),
            "endpoint": handle.endpoint,
            "receipt_reference": dict(handle.receipt_reference),
            "audit_reference": dict(handle.audit_reference),
            "evidence_reference": dict(handle.evidence_reference),
            "launch_audit": thawed_json(handle.launch_audit),
        }
        try:
            descriptor = os.open(
                self.hand_off_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
        except OSError:
            return False
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(canonical_bytes(record))
                stream.flush()
                os.fsync(stream.fileno())
        except OSError:
            self._remove_hand_off()
            return False
        self._residency_handle.relinquish()
        self._residency_handle = None
        self._active = None
        return True

    def reclaim_hand_off(self) -> Mapping[str, object] | None:
        """Stop a service handed off by an earlier process that nobody took over.

        Returns what was found and done, or `None` when there was no hand-off.
        An unverified stop raises `ServiceStopError`, and the service keeps the
        card's lease.
        """

        if self._active is not None or self._residency_handle is not None:
            raise ServiceStopError(
                "this manager holds a service of its own; a handed-off one is reclaimed only "
                "before a start"
            )
        try:
            record = self._read_hand_off()
        except _HandOffUnreadable as error:
            self._remove_hand_off()
            return {"discarded": str(error)}
        if record is None:
            return None
        return self._displace(record, None)

    def recover(self) -> None:
        """Retry the cleanup a failed start or a failed stop left; never a launch path.

        The manager keeps the process it launched, so recovery does not depend
        on a caller still holding the service handle. Once the child and
        endpoint are proved gone, the lease is released.
        """

        if self._active is not None:
            self.stop(self._active)
            return
        if self._residency_handle is None:
            return
        error = self._attempt_cleanup(self._unready_process, self._unready_endpoint)
        if error is not None:
            raise error

    def _launchable_profile(self, identity: ChairIdentity, tier: str) -> ServingProfile:
        return _launchable(
            self.recipes.for_identity(identity, tier),
            identity,
            qualification=self._qualification_launch,
        )

    def _assert_runtime(self, profile: ServingProfile) -> dict[str, str]:
        observed = self._observed_packages(profile)
        mismatches = [
            f"{package}={observed[package]!r}, expected {expected!r}"
            for package, expected in profile.required_packages.items()
            if observed[package] != expected
        ]
        if mismatches:
            raise RuntimePinError("runtime package pin mismatch: " + "; ".join(mismatches))
        return observed

    def _observed_packages(self, profile: ServingProfile) -> dict[str, str]:
        observed: dict[str, str] = {}
        for package in profile.required_packages:
            try:
                observed[package] = self.package_inspector.version(package)
            except ServingError:
                raise
            except Exception as error:
                raise RuntimePinError(f"could not inspect package {package!r}: {error}") from error
        return observed

    def _assert_endpoint_unoccupied(self, endpoint: str) -> None:
        try:
            response = self._get(health_url(endpoint), _READINESS_PROBE_TIMEOUT_SECONDS)
        except EndpointUnavailable as error:
            if error.definitively_absent:
                return
            raise EndpointOccupiedError(
                f"loopback endpoint {endpoint!r} did not prove absent before launch: {error}"
            ) from error
        raise EndpointOccupiedError(
            f"loopback endpoint {endpoint!r} already answered HTTP {response.status}; refusing an unknown service"
        )

    def _wait_until_ready(
        self, process: ServerProcess, profile: ServingProfile
    ) -> ReadinessEvidence:
        deadline = self.monotonic() + profile.startup_timeout_seconds
        last = "service did not become ready"
        # The kind of not-ready last observed: refused, unreachable (timeout,
        # reset) or answered-but-unready. They call for different timeout
        # advice. None until a probe returns.
        endpoint_state: str | None = None
        # Only a loading marker that changed shows the engine still progressing;
        # that is what justifies advising a longer, billed timeout.
        progress_line: str | None = None
        progress_advanced = False

        def watchdog_timeout() -> ReadinessError:
            return _watchdog_timeout(
                process,
                last=last,
                endpoint_state=endpoint_state,
                progress_advanced=progress_advanced,
                budget_seconds=float(profile.startup_timeout_seconds),
            )

        while True:
            # A dead process or a fatal log line is a better reason than a timeout,
            # so check them before the watchdog.
            self._assert_process_live(process)
            launch_tail = process.read_tail()
            if launch_tail.startswith(_LOG_UNREADABLE):
                raise ReadinessError(
                    "VLLM_LOG_UNREADABLE", launch_tail.removeprefix(_LOG_UNREADABLE).strip()
                )
            signature = _fatal_log_signature(launch_tail)
            if signature is not None:
                raise ReadinessError(signature, "fatal vLLM signature appeared in this launch log")
            current_progress = _progress_log_line(launch_tail)
            if current_progress is not None:
                if progress_line is not None and current_progress != progress_line:
                    progress_advanced = True
                progress_line = current_progress
            if deadline - self.monotonic() <= 0:
                raise watchdog_timeout()
            try:
                health = self._get(
                    health_url(profile.endpoint),
                    self._time_left(deadline, _READINESS_PROBE_TIMEOUT_SECONDS),
                )
                if health.status != 200:
                    raise ReadinessError(
                        "VLLM_HEALTH_UNAVAILABLE", f"/health returned HTTP {health.status}"
                    )
                models = self._get(
                    models_url(profile.endpoint),
                    self._time_left(deadline, _READINESS_PROBE_TIMEOUT_SECONDS),
                )
                model_ids = require_exact_model_id(models, profile.served_model_id)
                probe = self._post_probe(
                    endpoint=profile.endpoint,
                    kind=profile.readiness_probe.kind,
                    payload=profile.readiness_probe.request_payload(),
                    model_id=profile.served_model_id,
                    seed=profile.seed,
                    deterministic=True,
                    timeout_seconds=self._time_left(deadline, _INFERENCE_TIMEOUT_SECONDS),
                )
                return ReadinessEvidence(
                    health_status=health.status,
                    model_ids=model_ids,
                    probe=probe,
                    ready_at=_utc_stamp(self.now()),
                )
            except EndpointUnavailable as error:
                last = f"loopback endpoint unavailable: {error}"
                endpoint_state = (
                    _ENDPOINT_REFUSED if error.definitively_absent else _ENDPOINT_UNREACHABLE
                )
            except ReadinessError as error:
                if _is_deterministic_probe_rejection(error):
                    # A 4xx rejects the probe's shape, not warm-up; retrying the
                    # same body would only burn GPU time until the watchdog.
                    raise
                last = str(error)
                endpoint_state = _ENDPOINT_ANSWERED_UNREADY
            if self.monotonic() >= deadline:
                raise watchdog_timeout()
            self.sleep(self._time_left(deadline, float(profile.poll_interval_seconds)))

    def _time_left(self, deadline: float, cap: float) -> float:
        """Cap one wait by the time left before ``deadline``, so no wait overruns it."""

        return min(cap, max(0.0, deadline - self.monotonic()))

    @staticmethod
    def _assert_process_live(process: ServerProcess) -> None:
        """Refuse a readiness/receipt observation once its owned child has exited."""

        exit_code = process.poll()
        if exit_code is not None:
            raise ReadinessError(
                "VLLM_PROCESS_EXITED",
                f"owned process pid={process.pid} exited with {exit_code}",
            )

    def _get(self, url: str, timeout_seconds: float) -> HttpResponse:
        return self.http.request("GET", url, body=None, timeout_seconds=timeout_seconds)

    def _post(self, endpoint: str, kind: str, body: bytes, timeout_seconds: float) -> HttpResponse:
        return self.http.request(
            "POST", endpoint_for_probe(endpoint, kind), body=body, timeout_seconds=timeout_seconds
        )

    def _post_probe(
        self,
        *,
        endpoint: str,
        kind: str,
        payload: Mapping[str, object],
        model_id: str,
        seed: int,
        deterministic: bool,
        timeout_seconds: float = _INFERENCE_TIMEOUT_SECONDS,
    ) -> OpenAIResult:
        response = self._post(
            endpoint,
            kind,
            request_body(payload, model_id=model_id, seed=seed, deterministic=deterministic),
            timeout_seconds,
        )
        return parse_openai_answer(response, kind=kind, expected_model_id=model_id)

    def _plan_ceilings(self, profile: ServingProfile) -> dict[str, int]:
        """The tier's two configured ceilings, refusing a plan that is not bound by them.

        ``batch_size`` bounds the row (PREFLIGHT checks it); ``planned_batch_ceiling``
        bounds the width a plan launches it with. A plan derived under another
        ceiling than the sealed placement table's is refused too.
        """

        plan = self.capacity_plan
        if plan is None:
            return {}
        try:
            tier = self.placement_table.tier_named(profile.tier)
        except ValueError as error:
            raise ServingConfigurationError(str(error)) from error
        ceiling = planned_ceiling(tier)
        if plan.max_num_seqs_cap != ceiling:
            raise ServingConfigurationError(
                f"capacity plan was derived under a ceiling of "
                f"{plan.max_num_seqs_cap} sequences, but tier {profile.tier!r} "
                f"sets planned_batch_ceiling {ceiling}"
            )
        if profile.max_num_seqs > ceiling:
            raise ServingConfigurationError(
                f"capacity plan would launch chair {profile.chair!r} with {profile.max_num_seqs} "
                f"sequences, above tier {profile.tier!r}'s planned_batch_ceiling of {ceiling}"
            )
        return {"row_ceiling": tier.recipe.batch_size, "planned_ceiling": ceiling}

    def _launch_audit(
        self,
        *,
        identity: ChairIdentity,
        profile: ServingProfile,
        process: ServerProcess,
        argv: tuple[str, ...],
        readiness: Mapping[str, object],
        snapshot: VerifiedSnapshot,
        runtime_packages: Mapping[str, str],
        started_at: str,
        row: ServingProfile | None = None,
    ) -> Mapping[str, object]:
        """Return operational evidence kept outside the receipt schema.

        ``profile`` is the shape launched. Under a capacity plan, ``capacity``
        records the row's width beside it, the card and the plan's digest.
        """

        argv_digest = _argv_sha256(argv)
        pins = model_and_tokenizer_pins(snapshot.identity)
        # A local-repository chair has no commit; `revision_kind` says which pin
        # bound the launch instead of leaving nulls.
        model_revision, tokenizer_revision = (
            pins if pins is not None else (snapshot.identity.receipt_revision,) * 2
        )
        capacity: dict[str, object] = {}
        if self.capacity_plan is not None:
            capacity["capacity"] = {
                "plan_sha256": self.capacity_plan.digest,
                "card": self.capacity_plan.card_record(),
                "row_max_num_seqs": (row or profile).max_num_seqs,
                "max_num_seqs": profile.max_num_seqs,
                **self._plan_ceilings(profile),
            }
        return MappingProxyType(
            {
                **capacity,
                "schema": SERVING_LAUNCH_AUDIT_SCHEMA,
                "chair": identity.role,
                "producer": self.producer,
                "launch_purpose": self.launch_purpose,
                "configuration_inputs": self.config_inputs.to_record(),
                "started_at": started_at,
                "endpoint": profile.endpoint,
                "profile": {
                    "recipe": profile.recipe,
                    "tier": profile.tier,
                    "preflight_state": profile.preflight_state,
                    "served_model_id": profile.served_model_id,
                    "host": profile.host,
                    "port": profile.port,
                    "dtype": profile.dtype,
                    "max_model_len": profile.max_model_len,
                    "max_num_seqs": profile.max_num_seqs,
                    "max_num_batched_tokens": profile.max_num_batched_tokens,
                    "gpu_memory_utilization": str(profile.gpu_memory_utilization),
                    "min_pixels": profile.min_pixels,
                    "max_pixels": profile.max_pixels,
                    "enable_prefix_caching": profile.enable_prefix_caching,
                    "enforce_eager": profile.enforce_eager,
                    "trust_remote_code": profile.trust_remote_code,
                    "generation_config": profile.generation_config,
                    **_engine_option_record(profile),
                    "request_logging": False,
                    "startup_timeout_seconds": profile.startup_timeout_seconds,
                    "poll_interval_seconds": profile.poll_interval_seconds,
                    "readiness_probe": {
                        "kind": profile.readiness_probe.kind,
                        "request_payload_sha256": _canonical_object_sha256(
                            profile.readiness_probe.request_payload()
                        ),
                        "temperature": 0,
                        "seed": profile.seed,
                        "stream": False,
                    },
                },
                "process": {"pid": process.pid},
                "command": {
                    "argv_sha256": argv_digest,
                    "model_revision": model_revision,
                    "tokenizer_revision": tokenizer_revision,
                    "revision_kind": snapshot.identity.receipt_revision_kind,
                    "served_model_name": profile.served_model_id,
                },
                "runtime_packages": {
                    "required": dict(profile.required_packages),
                    "observed": dict(runtime_packages),
                },
                "chair_identity": snapshot.identity.to_record(),
                "manifest_digest": snapshot.manifest_digest,
                "readiness": dict(readiness),
            }
        )

    def _next_log_path(self, identity: ChairIdentity) -> Path:
        # One log per launch: a previous launch's fatal line in a reused log
        # would abort this readiness poll.
        return self.log_root / f"vllm-{identity.role}-{uuid.uuid4().hex}.log"

    def _residency_fd(self) -> int:
        """Pass the held OS lock to the exact owned child across manager failure."""

        handle = self._residency_handle
        if handle is None:  # pragma: no cover - invoked immediately after acquire
            raise ProcessLaunchError("serving launch has no held residency lease")
        try:
            descriptor = handle.inheritable_fd()
        except ServiceStopError:
            raise
        except Exception as error:
            raise ProcessLaunchError(
                f"could not pass serving residency lease to owned process: {error}"
            ) from error
        if not isinstance(descriptor, int) or isinstance(descriptor, bool) or descriptor < 0:
            raise ProcessLaunchError("serving residency lease returned an invalid child descriptor")
        return descriptor

    def _attempt_cleanup(
        self, process: ServerProcess | None, endpoint: str
    ) -> ServiceStopError | None:
        """Cleanup shared by a failed start and its retry.

        Stop, verify the endpoint is absent, then release the lease; releasing
        first would let another start run beside a live process. On failure the
        process and endpoint are kept for :meth:`recover`. The error
        is returned, not raised, so it joins the start failure in one refusal.
        """

        try:
            if process is not None:
                self._stop_process(process)
                if endpoint:
                    self._assert_endpoint_absent(endpoint)
            self._release_residency()
        except BaseException as error:
            self._unready_process = process
            self._unready_endpoint = endpoint
            if isinstance(error, ServiceStopError):
                return error
            return ServiceStopError(
                "cleanup after failed serving launch could not complete: "
                f"{type(error).__name__}: {error}"
            )
        self._unready_process = None
        self._unready_endpoint = ""
        return None

    def _release_residency(self) -> None:
        handle = self._residency_handle
        if handle is None:
            return
        try:
            handle.release()
        except ServiceStopError:
            raise
        except Exception as error:
            raise ServiceStopError(f"could not release serving residency lease: {error}") from error
        self._residency_handle = None

    def _stop_process(self, process: ServerProcess) -> None:
        # Signalled even when the direct child has already exited: another
        # member of its process group may still hold the card.
        try:
            process.terminate()
            process.wait(self.shutdown_timeout_seconds)
        except TimeoutError:
            process.kill()
            try:
                process.wait(self.shutdown_timeout_seconds)
            except TimeoutError as error:
                raise ServiceStopError(
                    f"owned process pid={process.pid} did not exit after TERM and KILL"
                ) from error
        except Exception as error:
            raise ServiceStopError(
                f"could not stop owned process pid={process.pid}: {error}"
            ) from error
        if process.poll() is None:
            raise ServiceStopError(f"owned process pid={process.pid} remains live after stop")

    def _assert_endpoint_absent(self, endpoint: str) -> None:
        deadline = self.monotonic() + self.shutdown_timeout_seconds
        last = "endpoint absence has not been observed"
        while True:
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                raise ServiceStopError(last)
            try:
                response = self._get(
                    health_url(endpoint), min(_READINESS_PROBE_TIMEOUT_SECONDS, remaining)
                )
            except EndpointUnavailable as error:
                if error.definitively_absent:
                    return
                last = f"endpoint {endpoint!r} was unreachable but absence was unproven: {error}"
            else:
                last = f"endpoint {endpoint!r} still answered HTTP {response.status} after owned process exit"
            if self.monotonic() >= deadline:
                raise ServiceStopError(last)
            self.sleep(self._time_left(deadline, 0.25))

    def _require_active(self, handle: ServiceHandle) -> None:
        if self._active is not handle:
            raise ServiceStopError("service handle is not this manager's active owned service")

    def _refuse_start(
        self,
        identity: ChairIdentity,
        error: BaseException,
        *,
        also: BaseException | None = None,
    ) -> NoReturn:
        self._refuse(identity, error, also=also)
        raise AssertionError("registry refusal returned unexpectedly")  # pragma: no cover

    def _refuse(
        self,
        identity: ChairIdentity,
        error: BaseException,
        *,
        also: BaseException | None = None,
    ) -> None:
        """Report this chair unavailable, carrying every reason it is.

        `also` carries a cleanup failure beside the start failure: the registry
        raises one refusal, and a reason left out of it is lost.
        """

        error_code = getattr(error, "code", type(error).__name__)
        detail = f"{error_code}: {error}"
        if also is not None:
            also_code = getattr(also, "code", type(also).__name__)
            detail += (
                f"; additionally, cleanup after this failure could not be verified and the "
                f"single-resident lease is retained: {also_code}: {also}"
            )
        try:
            self.registry.refuse_recipe_start(identity, detail)
        except UnresolvedChairRefusal as refusal:
            raise UnresolvedChairRefusal(
                refusal.chair,
                f"{refusal.difference}; additionally, the serving start failed: {detail}",
            ) from refusal


#: Where a Qwen-VL repository states its vision geometry, in lookup order. The
#: pinned repositories ship one or both; ``processor_config.json`` nests the
#: values under ``image_processor``.
PROCESSOR_CONFIG_FILENAMES: Final = ("preprocessor_config.json", "processor_config.json")


def assert_processor_geometry(snapshot: VerifiedSnapshot, profile: ServingProfile) -> None:
    """Check the row's declared ``patch_size``/``merge_size`` against the model's file.

    They set every image's prompt-token cost, so a wrong declaration mis-counts
    every request; the check needs the weights, so it runs at every start.
    A row that does not declare both values (fixtures) is skipped. If neither
    config file is present, this check also returns without cross-checking the
    declared values. Snapshot completeness is checked separately against the
    manifest.
    """

    declared = {field: getattr(profile, field, None) for field in ("patch_size", "merge_size")}
    if any(value is None for value in declared.values()):
        return
    for filename in PROCESSOR_CONFIG_FILENAMES:
        path = snapshot.root / filename
        if not path.is_file():
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ServingConfigurationError(
                f"chair {profile.chair!r} declares patch_size/merge_size on its serving row, "
                f"and {filename} in its verified snapshot could not be read to check them: "
                f"{error}"
            ) from error
        if not isinstance(document, dict):
            raise ServingConfigurationError(
                f"chair {profile.chair!r} declares {declared} on its serving row, and "
                f"{filename} in its verified snapshot is not a JSON object; every image's "
                "prompt-token cost is computed from the row's numbers, so nothing here "
                "could confirm them"
            )
        nested = document.get("image_processor")
        sections = [document] + ([nested] if isinstance(nested, dict) else [])
        observed = {
            field: next(
                (section[field] for section in sections if section.get(field) is not None),
                None,
            )
            for field in declared
        }
        if any(value is None for value in observed.values()):
            raise ServingConfigurationError(
                f"chair {profile.chair!r} declares {declared} on its serving row, and "
                f"{filename} in its verified snapshot does not provide both values at the "
                "top level or under 'image_processor'; every image's prompt-token cost is "
                "computed from the row's numbers, so nothing here could confirm them"
            )
        if observed != declared:
            raise ServingConfigurationError(
                f"chair {profile.chair!r} serving row (recipe={profile.recipe!r}, "
                f"tier={profile.tier!r}) declares {declared}, but {filename} at the pinned "
                f"revision states {observed}; every image's prompt-token cost is computed "
                "from the row's numbers, so serving under them would mis-count every request "
                "this chair is sent"
            )
        return


def assert_quantization(snapshot: VerifiedSnapshot, profile: ServingProfile) -> None:
    """Refuse a row whose ``quantization`` the verified checkpoint does not declare.

    vLLM refuses a checkpoint quantized another way, but given an unquantized
    checkpoint it would quantize the weights itself at load: a different model
    served under the row's name. So a row that names a method needs the
    snapshot's ``config.json`` ``quantization_config`` to declare it: the
    ``quant_method`` (and, for a ModelOpt method, the ``quant_algo``) that
    ``QUANTIZATION_CHECKPOINT_DECLARATIONS`` lists for the row's value. A row
    that names none is not checked, as before.
    """

    declared = getattr(profile, "quantization", None)
    if declared is None:
        return
    path = snapshot.root / "config.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ServingConfigurationError(
            f"chair {profile.chair!r} serving row (recipe={profile.recipe!r}) declares "
            f"quantization={declared!r}, and config.json in its verified snapshot could not "
            f"be read to confirm it: {error}"
        ) from error
    sections = [document] if isinstance(document, dict) else []
    text = document.get("text_config") if isinstance(document, dict) else None
    if isinstance(text, dict):
        sections.append(text)
    quantization_config = next(
        (
            section["quantization_config"]
            for section in sections
            if isinstance(section.get("quantization_config"), dict)
        ),
        {},
    )
    observed = quantization_config.get("quant_method")
    expected_method, expected_algo = QUANTIZATION_CHECKPOINT_DECLARATIONS[declared]
    observed_algo = quantization_config.get("quant_algo")
    if expected_algo is not None and isinstance(observed_algo, str):
        observed_algo = observed_algo.upper()  # vLLM upper-cases it before matching
    if observed != expected_method or (
        expected_algo is not None and observed_algo != expected_algo
    ):
        algo = f", quant_algo={observed_algo!r}" if expected_algo is not None else ""
        wanted = f"quant_method={expected_method!r}" + (
            f", quant_algo={expected_algo!r}" if expected_algo is not None else ""
        )
        raise ServingConfigurationError(
            f"chair {profile.chair!r} serving row (recipe={profile.recipe!r}, "
            f"tier={profile.tier!r}) declares quantization={declared!r}, but config.json at "
            f"the pinned revision declares quant_method={observed!r}{algo}, not {wanted}; "
            "vLLM would quantize an unquantized checkpoint itself, serving weights nobody "
            "pinned, or read a differently quantized one another way"
        )


def _engine_option_record(profile: ServingProfile) -> dict[str, object]:
    """The row's optional engine options for the launch audit, only those it states."""

    record: dict[str, object] = {}
    if profile.quantization is not None:
        record["quantization"] = profile.quantization
    if profile.kv_cache_dtype is not None:
        record["kv_cache_dtype"] = profile.kv_cache_dtype
    if profile.speculative_config is not None:
        record["speculative_config"] = dict(profile.speculative_config)
    return record


# Filenames the repository already treats as credential-bearing (`.env`,
# `.env.*` except the tracked example), plus `local.env` as an operator habit.
_ENV_OVERRIDE_EXACT_NAMES: Final = frozenset({"local.env", ".env"})
_ENV_OVERRIDE_EXCLUDED_NAMES: Final = frozenset({".env.example"})


def assert_no_discoverable_local_env(*, directory: str | Path | None = None) -> None:
    """Refuse a live launch next to an undeclared env-override file.

    ``directory`` defaults to the cwd, which the vLLM child inherits. A value
    injected from such a file (a Hub token, a proxy, an engine flag) is invisible
    to the sealed configuration and the launch audit.
    """

    target = Path(directory) if directory is not None else Path.cwd()
    try:
        candidates = sorted(
            entry.name
            for entry in target.iterdir()
            if entry.name in _ENV_OVERRIDE_EXACT_NAMES
            or (entry.name.startswith(".env.") and entry.name not in _ENV_OVERRIDE_EXCLUDED_NAMES)
        )
    except OSError as error:
        raise ServingConfigurationError(
            f"cannot check for a discoverable env-override file in {target}: {error}"
        ) from error
    if candidates:
        raise ServingConfigurationError(
            f"an env-override file ({', '.join(candidates)}) is discoverable at {target}; a "
            "real serving launch must not start next to an undeclared environment-override "
            "file, since nothing sealed by this package's configuration inputs or launch audit "
            "could ever see a value it injected into the launched subprocess's inherited "
            "environment. Remove or rename it before starting a real chair."
        )


def _launchable(
    profile: "ServingProfile | InProcessProfile | SubprocessProfile | FixtureProfile | UnsupportedProfile",
    identity: ChairIdentity,
    *,
    qualification: bool = False,
) -> ServingProfile:
    """Refuse non-launchable rows before snapshot and runtime checks.

    Refusing later would hide the configuration cause behind a pin or engine
    failure.
    """

    if isinstance(profile, FixtureProfile):
        raise ServingConfigurationError(
            f"chair {identity.role!r} resolves to fixture serving profile {profile.recipe!r} "
            f"at tier {profile.tier!r} ({profile.description}); a fixture profile is never "
            "launched, and the offline walking skeleton answers it from declared serving "
            "details instead"
        )
    if isinstance(profile, InProcessProfile):
        raise ServingConfigurationError(
            f"chair {identity.role!r} resolves to in-process profile {profile.recipe!r} at "
            f"tier {profile.tier!r}; its own stage loads and runs it on the "
            f"{profile.device}, and no serving process is ever started for it"
        )
    if isinstance(profile, SubprocessProfile):
        raise ServingConfigurationError(
            f"chair {identity.role!r} resolves to subprocess profile {profile.recipe!r} at "
            f"tier {profile.tier!r}; its own stage runs it on the {profile.device} in "
            f"{profile.environment}, and no serving process is ever started for it"
        )
    if isinstance(profile, UnsupportedProfile):
        raise ServingConfigurationError(
            f"chair {identity.role!r} resolves to unsupported serving profile "
            f"{profile.recipe!r} at tier {profile.tier!r}: {profile.reason}; no serving "
            "process was started; add and preflight a native serving implementation before "
            "launching this chair"
        )
    if profile.preflight_state != "proven" and not (
        qualification and profile.preflight_state == "unproven"
    ):
        raise ServingConfigurationError(
            f"chair {identity.role!r} serving profile is structurally marked "
            f"preflight_state={profile.preflight_state!r}; real-silicon preflight must "
            "prove this exact profile before launch"
        )
    # A preflight proves a profile with one checkpoint. Repointing the chair in
    # config/models.toml leaves the row and its digest unchanged, so the chair
    # identity is checked here too.
    observed_identity_digest = chair_preflight_identity_digest(identity)
    if (
        profile.preflight_state == "proven"
        and profile.preflight_identity_digest != observed_identity_digest
    ):
        raise ServingConfigurationError(
            f"chair {identity.role!r} serving profile was preflight-proven against chair "
            f"identity {profile.preflight_identity_digest!r}, but the configured identity "
            f"digests to {observed_identity_digest!r}; the checkpoint changed after this "
            "profile was proven, so it must be preflighted again before launch"
        )
    _refuse_hybrid_prefix_caching(identity, profile)
    return profile


def _refuse_hybrid_prefix_caching(identity: ChairIdentity, profile: ServingProfile) -> None:
    """Refuse prefix caching on a row that serves a hybrid Mamba/attention checkpoint."""

    if (
        identity.source == "huggingface"
        and identity.repo in _HYBRID_ATTENTION_REPOSITORIES
        and profile.enable_prefix_caching
    ):
        raise ServingConfigurationError(
            f"chair {identity.role!r} serves {identity.repo!r}, a hybrid Mamba/attention "
            "(qwen3_5) checkpoint; prefix caching over recurrent state only costs "
            "recurrent-state memory here, and vLLM would enable it by default -- "
            f"enable_prefix_caching must be false for this chair"
        )


def render_vllm_argv(
    *,
    command_prefix: tuple[str, ...],
    profile: ServingProfile,
    snapshot: VerifiedSnapshot,
) -> tuple[str, ...]:
    """Render the exact argv from verified identities and one typed profile.

    Model and tokenizer point at the verified snapshot; ``model_and_tokenizer_pins``
    decides whether revision flags follow.
    """

    pins = model_and_tokenizer_pins(snapshot.identity)
    argv = [
        *command_prefix,
        "serve",
        str(snapshot.root),
        "--tokenizer",
        str(snapshot.root),
        "--host",
        profile.host,
        "--port",
        str(profile.port),
    ]
    if pins is not None:
        model_revision, tokenizer_revision = pins
        argv += [
            "--revision",
            model_revision,
            "--tokenizer-revision",
            tokenizer_revision,
        ]
    argv += [
        "--served-model-name",
        profile.served_model_id,
        "--dtype",
        profile.dtype,
        "--seed",
        str(profile.seed),
        "--max-model-len",
        str(profile.max_model_len),
        "--max-num-seqs",
        str(profile.max_num_seqs),
        "--max-num-batched-tokens",
        str(profile.max_num_batched_tokens),
        "--gpu-memory-utilization",
        str(profile.gpu_memory_utilization),
        "--mm-processor-kwargs",
        json.dumps(
            {"min_pixels": profile.min_pixels, "max_pixels": profile.max_pixels},
            sort_keys=True,
            separators=(",", ":"),
        ),
        "--generation-config",
        profile.generation_config,
        # Page contents are not diagnostic data.
        "--no-enable-log-requests",
        # Token counts only, never text. The per-modality breakdown lets usage
        # reconciliation catch a silently dropped `mm_processor_kwargs`, which
        # would otherwise read a page at the wrong scale with no error
        # (vllm-project/vllm#49015).
        "--enable-prompt-tokens-details",
        # Not `auto`: under `string` format vLLM moves images ahead of text
        # (vllm-project/vllm#14047), so the image-before-text check could not
        # fail. `openai` keeps the caller's part order.
        "--chat-template-content-format",
        "openai",
        "--enable-prefix-caching"
        if profile.enable_prefix_caching
        else "--no-enable-prefix-caching",
        "--enforce-eager" if profile.enforce_eager else "--no-enforce-eager",
        "--trust-remote-code" if profile.trust_remote_code else "--no-trust-remote-code",
        # Optional engine options last, and only when the row states them, so a
        # row without them renders exactly the argv it always did.
        *profile.engine_option_argv(),
    ]
    return tuple(argv)


def _fatal_log_signature(tail: str) -> str | None:
    """Name a fatal startup failure rather than waiting out the whole watchdog.

    The list stays narrow: the whole tail is re-read every poll, so one benign
    match aborts a good start every time. `RuntimeError`, `ValueError` and bare
    `traceback` are excluded because vLLM logs harmless ones at startup
    (vllm-project/vllm#12513); a process they kill is caught as exited.
    """

    normalized = tail.lower()
    if "enginedeaderror" in normalized:
        return "EngineDeadError"
    if "cuda out of memory" in normalized:
        return "CUDA out of memory"
    # vLLM's registry refusal for an architecture it cannot serve.
    if "are not supported for now. supported architectures:" in normalized:
        return "UNKNOWN_MODEL"
    return None


_LOADING_LOG_MARKERS: Final = (
    "loading safetensors checkpoint shards",
    "loading weights took",
    "starting to load model",
    "model loading took",
    "capturing cuda graph",
    "graph capturing finished",
    "torch.compile",
    "compiling a graph",
    "memory profiling takes",
    "gpu kv cache size",
    "initializing a v1 llm engine",
    "init engine",
)
"""Launch-log strings that mean the engine is doing startup work, not hanging.

The mirror of `_fatal_log_signature`, generous where that one is strict: a
missed fatal signature costs a bounded wait, but a missed or false loading
marker only costs a vaguer message, since neither aborts a start or relaunches
anything. The refusal quotes the matched line rather than asserting a verdict.
"""

_WATCHDOG_TAIL_BYTES: Final = 1_200
"""How much launch log a watchdog refusal carries.

Enough to hold the progress lines and whatever preceded them, short enough that
a refusal stays readable in a journal entry and a notification.  The whole log
is on the pod at the path the launch audit names; this is the part that
travels with the refusal.
"""


_REDACTED: Final = "[redacted]"
#: `name=value`, `name: value`, `"name":"value"`; only the value is replaced.
_LOG_ASSIGNMENT: Final = re.compile(
    r"""(?P<lead>["']?)(?P<name>[A-Za-z_][A-Za-z0-9_.-]*)(?P=lead)\s*[:=]\s*"""
    r"""(?P<quote>["']?)(?P<value>[^\s"',;}\]]+)(?P=quote)"""
)
#: Whatever follows `Bearer` is a secret, whatever its shape.
_LOG_BEARER: Final = re.compile(r"""(?i)\bbearer\s+(?P<value>[^\s"',;]+)""")


def _redact_value(match: re.Match[str]) -> str:
    """Replace only the `value` group inside what this match covered."""

    whole = match.group(0)
    start = match.start("value") - match.start(0)
    end = match.end("value") - match.start(0)
    return f"{whole[:start]}{_REDACTED}{whole[end:]}"


def _redacted(text: str) -> str:
    """Blank out credential-shaped values before a launch log leaves the machine.

    The tail travels to journals and notifications. Values of secret-named fields and
    whatever follows `Bearer` are replaced whatever their shape; elsewhere, any word
    the shared shape test flags, whole, so no part of a key survives.
    """

    def redact_named(match: re.Match[str]) -> str:
        if not looks_like_credential_field(match.group("name")):
            return match.group(0)
        return _redact_value(match)

    def redact_word(match: re.Match[str]) -> str:
        if log_word_carries_credential(match.group(0)):
            return _REDACTED
        return match.group(0)

    lines = []
    for raw in text.splitlines():
        line = _LOG_ASSIGNMENT.sub(redact_named, raw)
        line = _LOG_BEARER.sub(_redact_value, line)
        lines.append(re.sub(r"\S+", redact_word, line))
    return "\n".join(lines)


def _progress_log_line(tail: str) -> str | None:
    """The most recent launch-log line showing engine startup progress, if any."""

    for line in reversed(tail.splitlines()):
        lowered = line.lower()
        if any(marker in lowered for marker in _LOADING_LOG_MARKERS):
            # One line: readers of the refusal treat its first line as the diagnosis.
            return " ".join(line.split())[:240]
    return None


_ENDPOINT_REFUSED: Final = "refused"
"""No listener owned that loopback port at that instant -- the one definite absence."""
_ENDPOINT_UNREACHABLE: Final = "unreachable"
"""The request produced no response and nothing proved the port empty: a
timeout, a reset, or a malformed local route. Retryable like a refusal, and not
evidence that nothing is listening."""
_ENDPOINT_ANSWERED_UNREADY: Final = "answered-unready"
"""The engine answered and was not ready."""


def _watchdog_timeout(
    process: ServerProcess,
    *,
    last: str,
    endpoint_state: str | None,
    budget_seconds: float,
    progress_advanced: bool = False,
) -> ReadinessError:
    """Say which kind of not-ready this was, and carry the evidence for it.

    Still loading and never started need opposite responses (raise the timeout,
    or investigate), and the launch log tells them apart. ``endpoint_state`` is
    ``None`` when no probe returned before the budget ran out. The diagnosis
    stays on the first line, ahead of the log tail.
    """

    tail = _redacted(process.read_tail())
    budget = f"{budget_seconds:.0f}s"
    if tail.startswith(_LOG_UNREADABLE):
        reason = tail.removeprefix(_LOG_UNREADABLE).strip()
        return ReadinessError(
            "VLLM_WATCHDOG_TIMEOUT",
            f"{last} -- log-unreadable: the launch log could not be read ({reason}), so "
            "this cannot say whether the engine was loading",
        )
    progress = _progress_log_line(tail)
    if progress is not None and progress_advanced:
        code, sentence = (
            "still-loading",
            f"the launch log's progress advanced to {progress!r} while this start waited; "
            f"the {budget} startup_timeout_seconds ran out mid-load (it is a budget, sized "
            "per row in config/serving_recipes_real.toml)",
        )
    elif progress is not None:
        code, sentence = (
            "stalled",
            f"the launch log shows loading at {progress!r}, and it did not move in {budget}",
        )
    elif endpoint_state == _ENDPOINT_REFUSED:
        code, sentence = (
            "refused",
            f"connections were refused and the log shows no loading after {budget}; a "
            "longer startup_timeout_seconds is unlikely to help",
        )
    elif endpoint_state == _ENDPOINT_UNREACHABLE:
        code, sentence = (
            "unreachable",
            f"no probe got an answer or a refusal in {budget}, and the log shows no loading",
        )
    elif endpoint_state == _ENDPOINT_ANSWERED_UNREADY:
        code, sentence = (
            "answered-unready",
            f"the endpoint answered but was never ready in {budget}, and the log shows no loading",
        )
    else:
        code, sentence = (
            "no-probe",
            f"the {budget} bound ran out before any readiness probe returned",
        )
    diagnosis = f"{last} -- {code}: {sentence}"
    # Cut in bytes, not characters; a cut mid-character decodes as a replacement.
    encoded = tail.encode("utf-8")
    excerpt = encoded[-_WATCHDOG_TAIL_BYTES:].decode("utf-8", errors="replace").strip()
    if not excerpt:
        return ReadinessError("VLLM_WATCHDOG_TIMEOUT", f"{diagnosis}. The launch log is empty")
    if len(encoded) > _WATCHDOG_TAIL_BYTES:
        excerpt = f"[last {_WATCHDOG_TAIL_BYTES} bytes] {excerpt}"
    return ReadinessError("VLLM_WATCHDOG_TIMEOUT", f"{diagnosis}. Launch log tail:\n{excerpt}")


def _is_deterministic_probe_rejection(error: ReadinessError) -> bool:
    """A readiness probe 4xx: the engine rejecting the request, which waiting cannot fix.

    A 5xx while booting is still worth retrying.
    """

    status = error.http_status
    return status is not None and 400 <= status < 500


def _immutable_reference(value: Mapping[str, str], label: str) -> Mapping[str, str]:
    if not isinstance(value, Mapping) or set(value) != {"relative_path", "sha256"}:
        raise ReceiptPublicationError(f"receipt publisher returned no {label} reference")
    relative_path = value["relative_path"]
    digest = value["sha256"]
    if (
        not isinstance(relative_path, str)
        or not relative_path
        or relative_path.startswith("/")
        or ".." in relative_path.split("/")
        or not is_sha256(digest)
    ):
        raise ReceiptPublicationError(f"receipt publisher returned a malformed {label} reference")
    result = {"relative_path": relative_path, "sha256": digest}
    return MappingProxyType(result)


def _canonical_object_sha256(value: Mapping[str, object]) -> str:
    """Digest a request shape without placing its potentially private text in audit."""

    _, canonical = seal_json_object(value, label="serving audit request payload")
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _local_fixture_bytes(fixture: str | Path, label: str) -> bytes:
    source = Path(fixture)
    try:
        data = source.read_bytes()
    except OSError as error:
        raise ServingConfigurationError(f"cannot read local {label} {source}: {error}") from error
    if not data:
        raise ServingConfigurationError(f"{label} must not be empty")
    return data


def _utc_stamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ServingConfigurationError("serving manager clock must return an aware UTC timestamp")
    return value.isoformat().replace("+00:00", "Z")


def _active_chat_image_bytes(payload: Mapping[str, object], *, label: str) -> bytes:
    """Extract the one image vLLM will receive; refuse anything but exactly one.

    The image rules live in :func:`chat_image_bytes_all`.
    """

    images = chat_image_bytes_all(payload, label=label)
    if len(images) != 1:
        raise ServingConfigurationError(
            f"{label} must contain exactly one active image_url content block and no ignored image_url fields"
        )
    return images[0]


class _AdoptionRefused(Exception):
    """Why a handed-off service was not taken over; the start goes on and records it."""


class _HandOffUnreadable(Exception):
    """A hand-off record that cannot be trusted to name a process."""


def _argv_sha256(argv: tuple[str, ...]) -> str:
    return hashlib.sha256(
        json.dumps(list(argv), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _without_role(identity: Mapping[str, object]) -> dict[str, object]:
    return {key: value for key, value in identity.items() if key not in _ROLE_FIELDS}
