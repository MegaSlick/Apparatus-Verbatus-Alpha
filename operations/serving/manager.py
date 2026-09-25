"""Start one configured vLLM chair, prove it answers, then publish its receipt.

It never ranks chairs, retries with another recipe, or falls back from an adapter
to its base. Pre-launch validation errors (such as a discoverable local environment
file) propagate as they are; after that, every start failure becomes a refusal naming
the requested chair; a refusal the registry raised is re-raised unchanged so its reason survives. An
interrupt is not a chair refusal, because the operator caused it.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import json
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Final, Mapping, Protocol

from common.chairs.errors import ChairRefusal, UnresolvedChairRefusal
from common.chairs.models import (
    AbsentChair,
    ChairIdentity,
    ServingDetails,
    ServingReceipt,
    VerifiedSnapshot,
    is_sha256,
)
from operations.pod.models import looks_like_credential_field, looks_like_credential_value

from .config import (
    FixtureProfile,
    ServingConfigInputs,
    ServingProfile,
    ServingRecipes,
    UnsupportedProfile,
    chair_preflight_identity_digest,
    model_and_tokenizer_pins,
    seal_json_object,
)
from .errors import (
    AdapterActivityError,
    EndpointOccupiedError,
    ProcessLaunchError,
    ReadinessError,
    ReceiptPublicationError,
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

# Hybrid Mamba/attention checkpoints: prefix caching over recurrent state
# costs extra memory for no measured benefit (vLLM leaves it opt-in for
# hybrids), so a row for one of these with it on is refused at launch. Keyed
# by repository, not role, since tests reuse role names for fixture chairs.
_HYBRID_ATTENTION_REPOSITORIES = frozenset({"datalab-to/chandra-ocr-2", "Qwen/Qwen3.8-27B"})

# Parses the status out of `parse_openai_answer`'s probe error message. If that
# wording changes the match stops firing and probe rejections fall back to
# retrying until the watchdog, which is safe.
_PROBE_HTTP_STATUS = re.compile(r"HTTP (\d{3})$")

# Only the serving smoke assembly holds this token. It lets an unproven row run
# start, fixture read and verified stop without a general bypass in ``start``.
_PREFLIGHT_QUALIFICATION_PURPOSE: Final = object()
MECHANICS_QUALIFICATION_PURPOSE: Final = object()
_NORMAL_LAUNCH = "normal"
_PREFLIGHT_QUALIFICATION_LAUNCH = "preflight-qualification"
_MECHANICS_QUALIFICATION_LAUNCH = "mechanics-qualification"
_READINESS_PROBE_TIMEOUT_SECONDS = 2.0
"""Per-request budget for one /health or /v1/models poll.

Named, not repeated: the readiness loop takes the smaller of this and what is
left of the watchdog deadline, so a drifted literal would widen the overrun."""

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

    The launch audit is separate because pid, argv, packages, readiness and
    adapter proof do not belong in the closed receipt schema.
    """

    receipt_reference: Mapping[str, str]
    audit_reference: Mapping[str, str]
    evidence_reference: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "receipt_reference", _immutable_reference(self.receipt_reference, "receipt")
        )
        object.__setattr__(
            self, "audit_reference", _immutable_reference(self.audit_reference, "launch-audit")
        )
        object.__setattr__(
            self,
            "evidence_reference",
            _immutable_reference(self.evidence_reference, "serving-evidence"),
        )


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


@dataclass(frozen=True, slots=True)
class AdapterCalibration:
    """A declared deterministic base-versus-adapter activation check.

    A vision adapter must set ``requires_image`` so a text-only probe cannot
    count as evidence for its visual path.
    """

    kind: str
    payload: Mapping[str, object]
    fixture_sha256: str
    requires_image: bool = False
    _canonical_payload: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.kind not in {"chat-completions", "completions"}:
            raise ServingConfigurationError(
                "adapter calibration kind must be chat-completions or completions"
            )
        if not is_sha256(self.fixture_sha256):
            raise ServingConfigurationError(
                "adapter calibration fixture_sha256 must be a lowercase SHA-256"
            )
        normalized_payload, canonical_payload = seal_json_object(
            self.payload, label="adapter calibration payload"
        )
        # Requests are rebuilt from `_canonical_payload`, so mutating this
        # projection cannot change the validated request.
        object.__setattr__(self, "payload", MappingProxyType(normalized_payload))
        object.__setattr__(self, "_canonical_payload", canonical_payload)
        if not isinstance(self.requires_image, bool):
            raise ServingConfigurationError("adapter calibration requires_image must be boolean")
        if self.requires_image:
            if self.kind != "chat-completions":
                raise ServingConfigurationError(
                    "image adapter calibration must use the chat-completions endpoint"
                )
            image_bytes = _active_chat_image_bytes(
                normalized_payload, label="image adapter calibration"
            )
            if hashlib.sha256(image_bytes).hexdigest() != self.fixture_sha256:
                raise ServingConfigurationError(
                    "adapter calibration image bytes do not match fixture_sha256"
                )

    def request_payload(self) -> Mapping[str, object]:
        """Return a fresh, revalidated request from the sealed calibration bytes."""

        payload = json.loads(self._canonical_payload)
        if self.requires_image:
            image_bytes = _active_chat_image_bytes(payload, label="image adapter calibration")
            if hashlib.sha256(image_bytes).hexdigest() != self.fixture_sha256:
                raise ServingConfigurationError(
                    "sealed adapter calibration image bytes no longer match fixture_sha256"
                )
        return payload

    @classmethod
    def from_image_fixture(
        cls,
        *,
        fixture: str | Path,
        prompt: str,
        mime_type: str,
    ) -> "AdapterCalibration":
        """Build an image chat probe from a local fixture, embedded as a data URI.

        Remote or file URLs are never accepted: vLLM could resolve them
        differently on the pod.
        """

        if not isinstance(prompt, str) or not prompt.strip():
            raise ServingConfigurationError("image calibration prompt must be non-blank")
        if not isinstance(mime_type, str) or not mime_type.startswith("image/"):
            raise ServingConfigurationError("image calibration mime_type must begin with 'image/'")
        source = Path(fixture)
        try:
            data = source.read_bytes()
        except OSError as error:
            raise ServingConfigurationError(
                f"cannot read local adapter calibration fixture {source}: {error}"
            ) from error
        if not data:
            raise ServingConfigurationError("adapter calibration fixture must not be empty")
        encoded = base64.b64encode(data).decode("ascii")
        return cls(
            kind="chat-completions",
            payload={
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:{mime_type};base64,{encoded}"},
                            },
                            {"type": "text", "text": prompt},
                        ],
                    }
                ]
            },
            fixture_sha256=hashlib.sha256(data).hexdigest(),
            requires_image=True,
        )


@dataclass(frozen=True, slots=True)
class AdapterActivationEvidence:
    """Digest-only proof that the configured adapter affected a deterministic probe."""

    fixture_sha256: str
    kind: str
    requires_image: bool
    base_model_id: str
    adapter_model_id: str
    base_output_sha256: str
    adapter_output_sha256: str

    def to_record(self) -> dict[str, object]:
        return {
            "fixture_sha256": self.fixture_sha256,
            "kind": self.kind,
            "requires_image": self.requires_image,
            "base_model_id": self.base_model_id,
            "adapter_model_id": self.adapter_model_id,
            "base_output_sha256": self.base_output_sha256,
            "adapter_output_sha256": self.adapter_output_sha256,
            "different": self.base_output_sha256 != self.adapter_output_sha256,
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
    _requests_completed: int = field(default=0, init=False, repr=False)
    _fixture_requests_completed: int = field(default=0, init=False, repr=False)
    _last_fixture_request_sha256: str | None = field(default=None, init=False, repr=False)
    _last_fixture_response: OpenAIResult | None = field(default=None, init=False, repr=False)
    _last_request_was_fixture: bool = field(default=False, init=False, repr=False)

    @property
    def endpoint(self) -> str:
        return self.profile.endpoint

    def request(self, kind: str, payload: Mapping[str, object]) -> OpenAIResult:
        """Issue one exact-model, non-streaming OpenAI-compatible request."""

        return self._manager.request(self, kind, payload)

    def request_reading(self, kind: str, body_bytes: bytes, timeout_seconds: float) -> HttpResponse:
        """POST one already-built reading request and return the raw response.

        The caller retains and parses the bytes. ``request`` does not fit a
        reading: it imposes its own payload shape and the probe parser.
        """

        return self._manager.request_reading(self, kind, body_bytes, timeout_seconds)

    @property
    def requests_completed(self) -> int:
        """Successful page-specific requests, excluding manager readiness probes."""

        return self._requests_completed

    @property
    def fixture_requests_completed(self) -> int:
        """Successful requests whose embedded image bytes matched a local fixture."""

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

    @property
    def last_request_was_fixture(self) -> bool:
        """Whether the final successful page request was the bound image request."""

        return self._last_request_was_fixture

    def request_fixture_image(
        self,
        kind: str,
        payload: Mapping[str, object],
        *,
        fixture: str | Path,
        exchange_observer: Callable[[bytes, HttpResponse], None] | None = None,
    ) -> OpenAIResult:
        """Request this service with the actual chat image from ``fixture``.

        The pod smoke uses this so a passing page read cannot be a text-only
        request. The image must sit in a ``role=user`` content block; a stray
        field merely named ``image_url`` is refused.
        """

        if kind != "chat-completions":
            raise ServingConfigurationError(
                "golden-page fixture requests must use the chat-completions endpoint"
            )
        # Validate and send one snapshot, so a mutable Mapping cannot show an
        # image here and serialize without it.
        sealed_payload, _ = seal_json_object(payload, label="golden-page request")
        fixture_digest = _fixture_sha256(fixture)
        image_digest = hashlib.sha256(
            _active_chat_image_bytes(sealed_payload, label="golden-page request")
        ).hexdigest()
        if image_digest != fixture_digest:
            raise ServingConfigurationError(
                "golden-page request image bytes do not match its supplied local fixture"
            )
        result = self._manager.request(
            self, kind, sealed_payload, exchange_observer=exchange_observer
        )
        self._fixture_requests_completed += 1
        self._last_fixture_request_sha256 = fixture_digest
        self._last_fixture_response = result
        self._last_request_was_fixture = True
        return result

    def stop(self) -> None:
        """Stop exactly this owned process; no pattern search is ever used."""

        self._manager.stop(self)


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
        receipt_reference = self.context.write_serving_receipt(receipt.identity, receipt.details)
        if not isinstance(receipt_reference, Mapping):
            raise ReceiptPublicationError("StageContext returned a non-object receipt reference")
        write_audit = getattr(self.context, "write_serving_launch_audit", None)
        if not callable(write_audit):
            raise ReceiptPublicationError(
                "StageContext has no serving launch-audit publication seam"
            )
        audit_reference = write_audit(dict(launch_audit))
        if not isinstance(audit_reference, Mapping):
            raise ReceiptPublicationError(
                "StageContext returned a non-object launch-audit reference"
            )
        write_evidence = getattr(self.context, "write_serving_evidence_manifest", None)
        if not callable(write_evidence):
            raise ReceiptPublicationError(
                "StageContext has no serving evidence-manifest publication seam"
            )
        evidence_reference = write_evidence(dict(receipt_reference), dict(audit_reference))
        if not isinstance(evidence_reference, Mapping):
            raise ReceiptPublicationError(
                "StageContext returned a non-object serving evidence-manifest reference"
            )
        return ReceiptPublication(
            dict(receipt_reference), dict(audit_reference), dict(evidence_reference)
        )


class ServingManager:
    """A sequential vLLM lifecycle manager.

    :meth:`start` verifies the snapshot just before launch, and an adapter's base
    too, because the base shapes the answer. The base is never a fallback.
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
        _launch_purpose: object | None = None,
    ) -> None:
        supplied_command_prefix = command_prefix is not None
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
        if (
            supplied_command_prefix
            and package_inspector is None
            and command_prefix[0] != sys.executable
        ):
            # The default inspector reads this interpreter's packages, so the pin
            # check only means something if the child is this interpreter --
            # compared as exact strings, since two venvs can symlink one
            # interpreter with different site-packages.
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
        self.registry = registry
        self.recipes = recipes
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
        self._qualification_launch = _launch_purpose in (
            _PREFLIGHT_QUALIFICATION_PURPOSE,
            MECHANICS_QUALIFICATION_PURPOSE,
        )
        self.launch_purpose = {
            _PREFLIGHT_QUALIFICATION_PURPOSE: _PREFLIGHT_QUALIFICATION_LAUNCH,
            MECHANICS_QUALIFICATION_PURPOSE: _MECHANICS_QUALIFICATION_LAUNCH,
        }.get(_launch_purpose, _NORMAL_LAUNCH)
        self._active: ServiceHandle | None = None
        self._residency_handle: ResidencyHandle | None = None
        self._unready_process: ServerProcess | None = None
        self._unready_endpoint = ""

    def start(
        self,
        identity: ChairIdentity,
        tier: str,
        *,
        adapter_calibration: AdapterCalibration | None = None,
    ) -> ServiceHandle:
        """Start one configured chair and publish its receipt after a real answer.

        A handle returns only after health, the exact model id, a bounded probe,
        adapter evidence and receipt publication have all succeeded.
        """

        # An invented value names no chair to refuse, so check it first.
        if not isinstance(identity, ChairIdentity):
            raise ServingConfigurationError("serving start requires one resolved ChairIdentity")
        if not isinstance(tier, str) or not tier:
            raise ServingConfigurationError("serving start requires one non-blank placement tier")
        # Checked here because every launch passes through ``start``.
        assert_no_discoverable_local_env()
        if self._active is not None or self._residency_handle is not None:
            # Not failed-launch cleanup: the held lease records an unverified shutdown.
            self._refuse(
                identity,
                ServingConfigurationError(
                    "a serving process is still resident or its shutdown is not verified; "
                    "stop and verify it before starting another chair"
                ),
            )
            raise AssertionError("registry refusal returned unexpectedly")  # pragma: no cover

        process: ServerProcess | None = None
        endpoint = ""
        try:
            # Both the chair's and an adapter base's profiles pass the recipe
            # check before any snapshot is verified.
            profile = _launchable(
                self.recipes.for_identity(identity, tier),
                identity,
                qualification=self._qualification_launch,
            )
            self._assert_runtime(profile)
            base_identity, base_profile = self._base_profile(identity, tier, profile)
            primary_snapshot = self.registry.ensure(identity)
            base_snapshot = (
                primary_snapshot
                if identity.adapter_of is None
                else self.registry.ensure(base_identity)
            )
            assert_processor_geometry(base_snapshot, profile)
            # Before launch: a bad generation_config.json is knowable offline,
            # so finding it after boot would waste GPU time.
            generation_config_digest = _generation_config_digest(profile, base_snapshot)
            endpoint = profile.endpoint
            # Held from endpoint probing through failed-launch cleanup, or two
            # assemblers can race from an empty endpoint into GPU co-residency.
            self._residency_handle = self.residency_lease.acquire(identity)
            self._assert_endpoint_unoccupied(endpoint)
            argv = render_vllm_argv(
                command_prefix=self.command_prefix,
                profile=profile,
                base_identity=base_identity,
                base_snapshot=base_snapshot,
                adapter_snapshot=primary_snapshot if identity.adapter_of is not None else None,
                base_profile=base_profile,
            )
            process = self.launcher.launch(
                argv,
                self._next_log_path(identity),
                inheritable_fds=(self._residency_fd(),),
            )
            started_at = _utc_stamp(self.now())
            readiness = self._wait_until_ready(process, profile)
            observed_packages = self._assert_runtime(profile)
            activation = self._prove_adapter_active(
                identity,
                profile,
                base_profile,
                adapter_calibration,
            )
            self._assert_process_live(process)
            details = ServingDetails(
                tokenizer_revision=base_identity.receipt_revision,
                seed=profile.seed,
                context_cap=profile.max_model_len,
                # A total pixel count. `pixel_cap` in config/pod_placement.toml
                # is a longest edge; the two are not comparable directly.
                pixel_cap=profile.max_pixels,
                engine="vllm",
                engine_version=observed_packages["vllm"],
                dtype=profile.dtype,
                adapter_identity=base_identity if identity.adapter_of is not None else None,
                endpoint=endpoint,
                started_at=started_at,
            )
            receipt = self.registry.receipt(identity, details)
            audit = self._launch_audit(
                identity=identity,
                profile=profile,
                process=process,
                argv=argv,
                readiness=readiness,
                primary_snapshot=primary_snapshot,
                base_snapshot=base_snapshot,
                base_profile=base_profile,
                activation=activation,
                runtime_packages=observed_packages,
                started_at=started_at,
                generation_config_digest=generation_config_digest,
            )
            sealed_audit = _immutable_json_value(audit)
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
            self._refuse(identity, error, also=self._attempt_cleanup(process, endpoint))
            raise AssertionError(
                "registry refusal returned unexpectedly"
            ) from error  # pragma: no cover
        except Exception as error:
            self._refuse(
                identity,
                ProcessLaunchError(
                    f"unexpected serving start failure: {type(error).__name__}: {error}"
                ),
                also=self._attempt_cleanup(process, endpoint),
            )
            raise AssertionError(
                "registry refusal returned unexpectedly"
            ) from error  # pragma: no cover
        except BaseException as error:
            # An interrupt must not strand a child. If cleanup is verified, the
            # interrupt is re-raised unchanged. If not, a possibly resident child
            # matters more, so the interrupt becomes a ServiceStopError; callers
            # that catch Exception then record it instead of unwinding, and the
            # retained lease makes later starts refuse (principle 2).
            cleanup_error = self._attempt_cleanup(process, endpoint)
            if cleanup_error is not None:
                raise ServiceStopError(
                    "serving start was interrupted and cleanup could not be verified: "
                    f"start={type(error).__name__}: {error}; stop={cleanup_error}"
                ) from error
            raise

    def request(
        self,
        handle: ServiceHandle,
        kind: str,
        payload: Mapping[str, object],
        *,
        exchange_observer: Callable[[bytes, HttpResponse], None] | None = None,
    ) -> OpenAIResult:
        """Send a regular non-streaming request to the handle's exact served alias."""

        self._require_active(handle)
        self._assert_process_live(handle.process)
        body = request_body(
            payload,
            model_id=handle.profile.served_model_id,
            seed=handle.profile.seed,
            deterministic=False,
        )
        response = self.http.request(
            "POST",
            endpoint_for_probe(handle.endpoint, kind),
            body=body,
            timeout_seconds=_INFERENCE_TIMEOUT_SECONDS,
        )
        if exchange_observer is not None:
            exchange_observer(body, response)
        result = parse_openai_answer(
            response, kind=kind, expected_model_id=handle.profile.served_model_id
        )
        handle._requests_completed += 1
        handle._last_request_was_fixture = False
        return result

    def request_reading(
        self, handle: ServiceHandle, kind: str, body_bytes: bytes, timeout_seconds: float
    ) -> HttpResponse:
        """POST a caller-built request body and return the unparsed response.

        Nothing is parsed here, so a malformed body reaches the caller's parser.
        """

        self._require_active(handle)
        self._assert_process_live(handle.process)
        return self.http.request(
            "POST",
            endpoint_for_probe(handle.endpoint, kind),
            body=body_bytes,
            timeout_seconds=timeout_seconds,
        )

    def stop(self, handle: ServiceHandle) -> None:
        """Stop one exact owned process and verify its endpoint no longer responds."""

        self._require_active(handle)
        try:
            self._stop_process(handle.process)
            self._assert_endpoint_absent(handle.endpoint)
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

    def recover_failed_start(self) -> None:
        """Retry cleanup after a failed start kept the pod lease; never a launch path.

        Once the child and endpoint are proved gone, the lease is released.
        """

        if self._active is not None:
            raise ServiceStopError("active service must be stopped through its ServiceHandle")
        if self._residency_handle is None:
            return
        error = self._attempt_cleanup(self._unready_process, self._unready_endpoint)
        if error is not None:
            raise error

    def _base_profile(
        self,
        identity: ChairIdentity,
        tier: str,
        profile: ServingProfile,
    ) -> tuple[ChairIdentity, ServingProfile]:
        """Resolve the base chair and check its profile, without touching snapshots."""

        if identity.adapter_of is None:
            return identity, profile
        configured_base = self.registry.resolve(identity.adapter_of)
        if isinstance(configured_base, AbsentChair):
            raise ServingConfigurationError(
                f"adapter chair {identity.role!r} names explicitly absent base {identity.adapter_of!r}"
            )
        if not isinstance(configured_base, ChairIdentity):
            raise ServingConfigurationError(
                f"adapter chair {identity.role!r} has no resolved base identity"
            )
        return (
            configured_base,
            _launchable(
                self.recipes.for_identity(configured_base, tier),
                configured_base,
                qualification=self._qualification_launch,
            ),
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
            response = self.http.request(
                "GET",
                health_url(endpoint),
                body=None,
                timeout_seconds=_READINESS_PROBE_TIMEOUT_SECONDS,
            )
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
        while True:
            # Each probe is capped by the watchdog time left, recomputed per
            # call, so no probe overruns the deadline.
            def probe_timeout() -> float:
                return min(_READINESS_PROBE_TIMEOUT_SECONDS, max(0.0, deadline - self.monotonic()))

            # A dead process or a fatal log line is a better reason than a timeout,
            # so check them before the watchdog.
            self._assert_process_live(process)
            launch_tail = process.read_tail()
            if launch_tail.startswith("VLLM_LOG_UNREADABLE:"):
                raise ReadinessError(
                    "VLLM_LOG_UNREADABLE",
                    launch_tail.removeprefix("VLLM_LOG_UNREADABLE:").strip(),
                )
            signature = _fatal_log_signature(launch_tail)
            if signature is not None:
                raise ReadinessError(signature, "fatal vLLM signature appeared in this launch log")
            current_progress = _progress_log_line(launch_tail)
            if current_progress is not None:
                if progress_line is not None and current_progress != progress_line:
                    progress_advanced = True
                progress_line = current_progress
            # No time left: a request could not succeed.
            if deadline - self.monotonic() <= 0:
                raise _watchdog_timeout(
                    process,
                    last=last,
                    endpoint_state=endpoint_state,
                    progress_advanced=progress_advanced,
                    budget_seconds=float(profile.startup_timeout_seconds),
                )
            try:
                health = self.http.request(
                    "GET",
                    health_url(profile.endpoint),
                    body=None,
                    timeout_seconds=probe_timeout(),
                )
                if health.status != 200:
                    raise ReadinessError(
                        "VLLM_HEALTH_UNAVAILABLE", f"/health returned HTTP {health.status}"
                    )
                models = self.http.request(
                    "GET",
                    models_url(profile.endpoint),
                    body=None,
                    timeout_seconds=probe_timeout(),
                )
                model_ids = require_exact_model_id(models, profile.served_model_id)
                probe = self._post_probe(
                    endpoint=profile.endpoint,
                    kind=profile.readiness_probe.kind,
                    payload=profile.readiness_probe.request_payload(),
                    model_id=profile.served_model_id,
                    seed=profile.seed,
                    deterministic=True,
                    timeout_seconds=min(
                        _INFERENCE_TIMEOUT_SECONDS, max(0.0, deadline - self.monotonic())
                    ),
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
                raise _watchdog_timeout(
                    process,
                    last=last,
                    endpoint_state=endpoint_state,
                    progress_advanced=progress_advanced,
                    budget_seconds=float(profile.startup_timeout_seconds),
                )
            self.sleep(
                min(float(profile.poll_interval_seconds), max(0.0, deadline - self.monotonic()))
            )

    @staticmethod
    def _assert_process_live(process: ServerProcess) -> None:
        """Refuse a readiness/receipt observation once its owned child has exited."""

        exit_code = process.poll()
        if exit_code is not None:
            raise ReadinessError(
                "VLLM_PROCESS_EXITED",
                f"owned process pid={process.pid} exited with {exit_code}",
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
        response = self.http.request(
            "POST",
            endpoint_for_probe(endpoint, kind),
            body=request_body(payload, model_id=model_id, seed=seed, deterministic=deterministic),
            timeout_seconds=timeout_seconds,
        )
        return parse_openai_answer(response, kind=kind, expected_model_id=model_id)

    def _prove_adapter_active(
        self,
        identity: ChairIdentity,
        profile: ServingProfile,
        base_profile: ServingProfile,
        calibration: AdapterCalibration | None,
    ) -> AdapterActivationEvidence | None:
        if identity.adapter_of is None:
            if calibration is not None:
                raise ServingConfigurationError(
                    "an unadapted chair cannot carry an adapter calibration"
                )
            return None
        if calibration is None:
            raise AdapterActivityError(
                "adapter chair has no calibration; /v1/models registration is not proof an adapter contributed"
            )
        if profile.enable_tower_connector_lora and not calibration.requires_image:
            raise AdapterActivityError(
                "tower/connector LoRA requires an image-bearing adapter calibration"
            )
        calibration_payload = calibration.request_payload()
        response = self.http.request(
            "GET",
            models_url(profile.endpoint),
            body=None,
            timeout_seconds=_READINESS_PROBE_TIMEOUT_SECONDS,
        )
        ids = require_exact_model_id(response, profile.served_model_id)
        if base_profile.served_model_id not in ids:
            raise AdapterActivityError(
                f"adapter endpoint does not advertise configured base id {base_profile.served_model_id!r}"
            )
        base = self._post_probe(
            endpoint=profile.endpoint,
            kind=calibration.kind,
            payload=calibration_payload,
            model_id=base_profile.served_model_id,
            seed=profile.seed,
            deterministic=True,
        )
        adapted = self._post_probe(
            endpoint=profile.endpoint,
            kind=calibration.kind,
            payload=calibration_payload,
            model_id=profile.served_model_id,
            seed=profile.seed,
            deterministic=True,
        )
        base_digest = outputs_sha256(base)
        adapted_digest = outputs_sha256(adapted)
        if base_digest == adapted_digest:
            raise AdapterActivityError(
                "base and adapter produced identical deterministic calibration output; adapter is unproven"
            )
        return AdapterActivationEvidence(
            fixture_sha256=calibration.fixture_sha256,
            kind=calibration.kind,
            requires_image=calibration.requires_image,
            base_model_id=base_profile.served_model_id,
            adapter_model_id=profile.served_model_id,
            base_output_sha256=base_digest,
            adapter_output_sha256=adapted_digest,
        )

    def _launch_audit(
        self,
        *,
        identity: ChairIdentity,
        profile: ServingProfile,
        process: ServerProcess,
        argv: tuple[str, ...],
        readiness: ReadinessEvidence,
        primary_snapshot: VerifiedSnapshot,
        base_snapshot: VerifiedSnapshot,
        base_profile: ServingProfile,
        activation: AdapterActivationEvidence | None,
        runtime_packages: Mapping[str, str],
        started_at: str,
        generation_config_digest: str | None,
    ) -> Mapping[str, object]:
        """Return operational evidence kept outside the receipt schema."""

        argv_digest = hashlib.sha256(
            json.dumps(list(argv), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        pins = model_and_tokenizer_pins(base_snapshot.identity)
        # A local-repository chair has no commit; `revision_kind` says which pin
        # bound the launch instead of leaving nulls.
        model_revision, tokenizer_revision = (
            pins if pins is not None else (base_snapshot.identity.receipt_revision,) * 2
        )
        return MappingProxyType(
            {
                "schema": "serving-launch-audit.v1",
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
                    "max_lora_rank": profile.max_lora_rank,
                    "enable_tower_connector_lora": profile.enable_tower_connector_lora,
                    "enable_prefix_caching": profile.enable_prefix_caching,
                    "enforce_eager": profile.enforce_eager,
                    "trust_remote_code": profile.trust_remote_code,
                    "generation_config": profile.generation_config,
                    # Only for 'auto': pins the generation_config.json vLLM will
                    # read, so 'auto' cannot change silently under the row.
                    "generation_config_digest": generation_config_digest,
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
                    "revision_kind": base_snapshot.identity.receipt_revision_kind,
                    "served_model_name": base_profile.served_model_id,
                },
                "runtime_packages": {
                    "required": dict(profile.required_packages),
                    "observed": dict(runtime_packages),
                },
                "primary_identity": primary_snapshot.identity.to_record(),
                "base_identity": base_snapshot.identity.to_record(),
                "primary_manifest_digest": primary_snapshot.manifest_digest,
                "base_manifest_digest": base_snapshot.manifest_digest,
                "readiness": readiness.to_record(),
                "adapter_activation": activation.to_record() if activation is not None else None,
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
        process and endpoint are kept for :meth:`recover_failed_start`. The error
        is returned, not raised, so it joins the start failure in one refusal
        (principle 2).
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
        if process.poll() is None:
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
            # Cap each probe by the time left, as in `_wait_until_ready`.
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                raise ServiceStopError(last)
            try:
                response = self.http.request(
                    "GET",
                    health_url(endpoint),
                    body=None,
                    timeout_seconds=min(_READINESS_PROBE_TIMEOUT_SECONDS, remaining),
                )
            except EndpointUnavailable as error:
                if error.definitively_absent:
                    return
                last = f"endpoint {endpoint!r} was unreachable but absence was unproven: {error}"
            else:
                last = f"endpoint {endpoint!r} still answered HTTP {response.status} after owned process exit"
            if self.monotonic() >= deadline:
                raise ServiceStopError(last)
            self.sleep(min(0.25, max(0.0, deadline - self.monotonic())))

    def _require_active(self, handle: ServiceHandle) -> None:
        if self._active is not handle:
            raise ServiceStopError("service handle is not this manager's active owned service")

    def _refuse(
        self,
        identity: ChairIdentity,
        error: BaseException,
        *,
        also: BaseException | None = None,
    ) -> None:
        """Report this chair unavailable, carrying every reason it is.

        `also` carries a cleanup failure beside the start failure: the registry
        raises one refusal, and a reason left out of it is lost (principle 2).
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
            continue
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


# Filenames the repository already treats as credential-bearing (`.env`,
# `.env.*` except the tracked example), plus `local.env` as an operator habit.
_ENV_OVERRIDE_EXACT_NAMES: Final = frozenset({"local.env", ".env"})
_ENV_OVERRIDE_EXCLUDED_NAMES: Final = frozenset({".env.example"})


def assert_no_discoverable_local_env(*, directory: str | Path | None = None) -> None:
    """Refuse a live launch next to an undeclared env-override file.

    ``directory`` defaults to the cwd, which the vLLM child inherits. A value
    injected from such a file (a Hub token, a proxy, an engine flag) is invisible
    to the sealed configuration and the launch audit (principle 6).
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
    profile: "ServingProfile | FixtureProfile | UnsupportedProfile",
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
    if (
        identity.source == "huggingface"
        and identity.repo in _HYBRID_ATTENTION_REPOSITORIES
        and profile.enable_prefix_caching
    ):
        raise ServingConfigurationError(
            f"chair {identity.role!r} serves {identity.repo!r}, a hybrid Mamba/attention "
            "(qwen3_5) checkpoint; vLLM keeps prefix caching over recurrent state opt-in "
            f"for hybrid models, and it only costs recurrent-state memory here -- "
            f"enable_prefix_caching must be false for this chair"
        )
    return profile


def _generation_config_digest(
    profile: ServingProfile, base_snapshot: VerifiedSnapshot
) -> str | None:
    """Digest the generation_config.json an 'auto' row will resolve to.

    ``None`` for a 'vllm' row, although vLLM still reads the file's
    ``eos_token_id`` under 'vllm' (v0.27.1,
    ``ModelConfig.try_get_generation_config``); only its sampling parameters
    are ignored. A
    missing file on an 'auto' row is first caught here, at launch.
    """

    if profile.generation_config != "auto":
        return None
    path = base_snapshot.root / "generation_config.json"
    try:
        data = path.read_bytes()
    except OSError as error:
        raise ServingConfigurationError(
            f"chair {profile.chair!r} row is generation_config='auto' but its verified "
            f"snapshot has no readable {path}: {error}"
        ) from error
    return hashlib.sha256(data).hexdigest()


def render_vllm_argv(
    *,
    command_prefix: tuple[str, ...],
    profile: ServingProfile,
    base_identity: ChairIdentity,
    base_snapshot: VerifiedSnapshot,
    adapter_snapshot: VerifiedSnapshot | None,
    base_profile: ServingProfile,
) -> tuple[str, ...]:
    """Render the exact argv from verified identities and one typed profile.

    Model and tokenizer point at the verified snapshot; ``model_and_tokenizer_pins``
    decides whether revision flags follow.
    """

    pins = model_and_tokenizer_pins(base_identity)
    argv = [
        *command_prefix,
        "serve",
        str(base_snapshot.root),
        "--tokenizer",
        str(base_snapshot.root),
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
    # An adapter registers its alias via `--lora-modules`, so the served name
    # is the base's.
    argv += [
        "--served-model-name",
        base_profile.served_model_id if adapter_snapshot is not None else profile.served_model_id,
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
    ]
    if adapter_snapshot is not None:
        argv.extend(
            [
                "--enable-lora",
                "--max-loras",
                "1",
                "--max-lora-rank",
                str(profile.max_lora_rank),
                "--lora-modules",
                json.dumps(
                    {
                        "name": profile.served_model_id,
                        "path": str(adapter_snapshot.root),
                        # The configured source ref, not a process-local alias.
                        "base_model_name": base_identity.source_reference,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ]
        )
        argv.append(
            "--enable-tower-connector-lora"
            if profile.enable_tower_connector_lora
            else "--no-enable-tower-connector-lora"
        )
    return tuple(argv)


def _fatal_log_signature(tail: str) -> str | None:
    """Name a fatal startup failure rather than waiting out the whole watchdog.

    The list stays narrow: the whole tail is re-read every poll, so one benign
    match aborts a good start every time. `RuntimeError`, `ValueError` and bare
    `traceback` are excluded because vLLM logs harmless ones at startup
    (vllm-project/vllm#12513); a process they kill is caught as exited.
    `VLLM_ERROR` has no producer in vLLM itself; it is reserved for a launch
    wrapper that writes into this log.
    """

    normalized = tail.lower()
    if "enginedeaderror" in normalized:
        return "EngineDeadError"
    if "cuda out of memory" in normalized:
        return "CUDA out of memory"
    if "does not support lora" in normalized:
        return "LORA_UNSUPPORTED"
    if "unknown model:" in normalized:
        return "UNKNOWN_MODEL"
    if "vllm_error" in normalized:
        return "VLLM_ERROR"
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
#: Splits a token so the shape test also reaches the parts of `key=value`.
_TOKEN_PARTS: Final = re.compile(r"""[^\s"'{}\[\],;:=]+""")


def _redact_value(match: re.Match[str]) -> str:
    """Replace only the `value` group inside what this match covered."""

    whole = match.group(0)
    start = match.start("value") - match.start(0)
    end = match.end("value") - match.start(0)
    return f"{whole[:start]}{_REDACTED}{whole[end:]}"


def _redacted(text: str) -> str:
    """Blank out credential-shaped values before a launch log leaves the machine.

    The tail travels to journals and notifications. Three passes, since the shared
    shape test skips tokens with path or URL punctuation: values of secret-named
    fields (a JWT looks like a dotted path), anything after `Bearer`, then the
    shape test over each token and its parts. Only values are replaced.
    """

    def redact_named(match: re.Match[str]) -> str:
        if not looks_like_credential_field(match.group("name")):
            return match.group(0)
        return _redact_value(match)

    def redact_by_shape(token: str) -> str:
        if looks_like_credential_value(token):
            return _REDACTED
        return _TOKEN_PARTS.sub(
            lambda part: _REDACTED if looks_like_credential_value(part.group(0)) else part.group(0),
            token,
        )

    lines = []
    for raw in text.splitlines():
        line = _LOG_ASSIGNMENT.sub(redact_named, raw)
        line = _LOG_BEARER.sub(_redact_value, line)
        # Split on all whitespace but keep it, so tabs separate fields too.
        lines.append(
            "".join(
                part if index % 2 else redact_by_shape(part)
                for index, part in enumerate(re.split(r"(\s+)", line))
            )
        )
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
    if tail.startswith("VLLM_LOG_UNREADABLE:"):
        return ReadinessError(
            "VLLM_WATCHDOG_TIMEOUT",
            f"{last} -- and after {budget_seconds:.0f}s this launch log could not be read "
            f"({tail.removeprefix('VLLM_LOG_UNREADABLE:').strip()}), so nothing here can say "
            "whether the engine was still loading or never started",
        )
    progress = _progress_log_line(tail)
    if progress is not None and progress_advanced:
        diagnosis = (
            f"still loading, not refused: {last} -- but this launch log's most recent "
            f"progress line is {progress!r} and it advanced while this start was waited "
            f"on, so the engine was still starting when the {budget_seconds:.0f}s "
            "startup_timeout_seconds bound expired. That bound is a "
            "budget, not a measurement of this row's load time; size it from the chair's "
            "weight bytes over the volume's measured read rate plus graph capture "
            "(config/serving_recipes_real.toml records the derivation per row) rather than "
            "reading this as a failure to start"
        )
    elif progress is not None:
        diagnosis = (
            f"loading was observed, and nothing since: {last} -- this launch log's most "
            f"recent progress line is {progress!r}, and it did not change while the "
            f"{budget_seconds:.0f}s startup_timeout_seconds bound ran out, so nothing here "
            "establishes the engine was still starting rather than stuck at that point. "
            "Read the whole log on the pod before raising the bound and paying for another "
            "wait"
        )
    elif endpoint_state == _ENDPOINT_REFUSED:
        diagnosis = (
            f"connection refused, not still loading: {last} -- and after "
            f"{budget_seconds:.0f}s nothing in this launch log shows the engine loading "
            "weights, capturing graphs or initializing, so raising "
            "startup_timeout_seconds is unlikely to help"
        )
    elif endpoint_state == _ENDPOINT_UNREACHABLE:
        diagnosis = (
            f"the endpoint did not answer, and nothing proved it empty: {last} -- after "
            f"{budget_seconds:.0f}s no probe produced a response and no connection was "
            "refused, so this says nothing about whether the engine is listening, and the "
            "log shows no loading either"
        )
    elif endpoint_state == _ENDPOINT_ANSWERED_UNREADY:
        diagnosis = (
            f"answered but never ready: {last} -- after {budget_seconds:.0f}s the endpoint "
            "was reachable and nothing in this launch log shows the engine still loading"
        )
    else:
        diagnosis = (
            f"no readiness probe was ever answered: {last} -- the "
            f"{budget_seconds:.0f}s startup_timeout_seconds bound was gone before the "
            "first round completed, so nothing here observed the endpoint at all"
        )
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

    if error.code != "VLLM_PROBE_HTTP_ERROR":
        return False
    match = _PROBE_HTTP_STATUS.search(error.detail)
    return match is not None and 400 <= int(match.group(1)) < 500


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


def _fixture_sha256(fixture: str | Path) -> str:
    """Hash one non-empty local golden-page fixture without transmitting a path."""

    source = Path(fixture)
    try:
        data = source.read_bytes()
    except OSError as error:
        raise ServingConfigurationError(
            f"cannot read local golden-page fixture {source}: {error}"
        ) from error
    if not data:
        raise ServingConfigurationError("golden-page fixture must not be empty")
    return hashlib.sha256(data).hexdigest()


def _utc_stamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ServingConfigurationError("serving manager clock must return an aware UTC timestamp")
    return value.isoformat().replace("+00:00", "Z")


def _active_chat_image_bytes(payload: Mapping[str, object], *, label: str) -> bytes:
    """Extract the one image vLLM will receive; refuse anything but exactly one.

    The image rules live in :func:`chat_image_bytes_all`.
    """

    try:
        images = chat_image_bytes_all(payload, label=label)
    except ServingConfigurationError as error:
        # Reword a stray image_url refusal as this caller's single refusal.
        if "outside a role=user content list" in str(error):
            raise ServingConfigurationError(
                f"{label} must contain exactly one active image_url "
                "content block and no ignored image_url fields"
            ) from error
        raise
    if len(images) != 1:
        raise ServingConfigurationError(
            f"{label} must contain exactly one active image_url content block and no ignored image_url fields"
        )
    return images[0]


def _immutable_json_value(value: object) -> object:
    """Deep-freeze one already-validated JSON value exposed on a live handle."""

    if isinstance(value, Mapping):
        return MappingProxyType({key: _immutable_json_value(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_immutable_json_value(item) for item in value)
    return value
