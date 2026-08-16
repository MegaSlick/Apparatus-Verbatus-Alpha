"""Start one configured vLLM chair, prove it answers, then publish its receipt.

The manager receives one already-named chair.  It never ranks chairs, retries
with another recipe, or turns a failed adapter into its bare base.  Every
*observed* start failure leaves as a refusal naming that requested chair: one
this manager observed is routed through ``ChairRegistry.refuse_recipe_start``,
and one the registry itself raised is re-raised as it stands, because refusing
it a second time would replace the chair boundary's own reason with this
module's.

The one start failure that is deliberately *not* a chair refusal is an
interrupt — ``KeyboardInterrupt``/``SystemExit`` — because the operator, not
the chair, is the reason.  ``start``'s ``BaseException`` clause says what it
does with the two facts that case can produce.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import importlib.metadata
import json
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol

from common.chairs.errors import ChairRefusal, UnresolvedChairRefusal
from common.chairs.models import (
    AbsentChair,
    ChairIdentity,
    ServingDetails,
    ServingReceipt,
    VerifiedSnapshot,
    is_sha256,
)

from .config import (
    FixtureProfile,
    ServingConfigInputs,
    ServingProfile,
    ServingRecipes,
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
    HttpTransport,
    OpenAIResult,
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

    ``ServingReceipt`` remains the existing closed v1 identity/serving record.
    The launch audit is intentionally separate because pid, argv digest, package
    map, readiness evidence, and adapter proof do not belong in that schema.
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

    ``fixture_sha256`` names the calibration fixture without putting its image
    bytes or transcribed content into a receipt.  A vision/connector adapter
    must set ``requires_image`` so a text-only probe cannot be misreported as
    evidence for its visual path.
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
        # The public projection is useful for diagnostics/tests, but actual
        # requests are rebuilt from `_canonical_payload` below.  Mutating a
        # nested list/dict on this projection therefore cannot swap in a remote
        # image or change the already-validated calibration request.
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
        """Build an image-bearing deterministic chat probe from a local fixture.

        This is intentionally the only convenience builder: it reads one local
        proof fixture, embeds its exact bytes as a data URI, and binds their
        SHA-256 into the calibration.  It neither fetches media nor accepts a
        remote/file URL that vLLM could interpret differently on the pod.
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
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:{mime_type};base64,{encoded}"},
                            },
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
    ) -> OpenAIResult:
        """Request this service with the actual OpenAI chat image from ``fixture``.

        The pod smoke seam uses this rather than ordinary ``request`` so a
        passing page read cannot silently become a text-only health surrogate.
        The only accepted image lives in a ``role=user``
        ``messages[].content[]`` block of a chat-completions request; an
        ignored extension field merely named ``image_url`` is refused.  The
        returned response SHA-256 is the opaque token that the smoke result
        must carry as ``fixture_response_sha256``.
        """

        if kind != "chat-completions":
            raise ServingConfigurationError(
                "golden-page fixture requests must use the chat-completions endpoint"
            )
        # Validate and dispatch one ordinary-dict snapshot: a custom or
        # later-mutated Mapping must not present an image here and then
        # serialize as a text-only request inside ``request_body``.
        sealed_payload, _ = seal_json_object(payload, label="golden-page request")
        fixture_digest = _fixture_sha256(fixture)
        image_digest = hashlib.sha256(
            _active_chat_image_bytes(sealed_payload, label="golden-page request")
        ).hexdigest()
        if image_digest != fixture_digest:
            raise ServingConfigurationError(
                "golden-page request image bytes do not match its supplied local fixture"
            )
        result = self.request(kind, sealed_payload)
        self._fixture_requests_completed += 1
        self._last_fixture_request_sha256 = fixture_digest
        self._last_fixture_response = result
        self._last_request_was_fixture = True
        return result

    def stop(self) -> None:
        """Stop exactly this owned process; no pattern search is ever used."""

        self._manager.stop(self)


class StageContextReceiptPublisher:
    """Adapter for the repository's existing run-receipt publication seam.

    ``StageContext.write_serving_receipt`` deliberately re-verifies the chair
    and writes content-addressed run-receipt bytes.  The launch audit is written
    as a separate content-addressed stage blob, then returned with its reference;
    it is never smuggled into the closed receipt v1 schema or discarded.
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
    """A fake-first, sequential vLLM lifecycle manager.

    Each call to :meth:`start` verifies the requested snapshot immediately
    before launch.  For an adapter chair it verifies the configured base too,
    because that base genuinely participates in the answer; it is never used as
    a fallback if any adapter check fails.
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
    ) -> None:
        supplied_command_prefix = command_prefix is not None
        if command_prefix is None:
            # Invoking vLLM through this interpreter binds the command to the
            # exact environment inspected through importlib.metadata below.  A
            # PATH-resolved console script could name another virtualenv's vLLM.
            command_prefix = (sys.executable, "-m", "vllm")
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
            # `InstalledPackages` reads *this* interpreter's distributions, so
            # the whole point of `_assert_runtime` — that the pinned versions
            # are the ones the engine will import — holds only while the child
            # is this interpreter.  Launch another one under the default
            # inspector and the pin passes against an environment nothing ran
            # in, and `runtime_packages.observed` in the launch audit becomes a
            # measurement of the wrong Python (GOVERNANCE 6, GOVERNANCE 10).
            #
            # Compared as exact strings rather than resolved paths on purpose:
            # two virtualenvs routinely symlink the same real interpreter while
            # holding entirely different site-packages, so `realpath` equality
            # would accept precisely the substitution this refuses.  A caller
            # that really does launch elsewhere must supply the
            # `PackageInspector` for *that* environment and own the pairing.
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

        A returned handle is impossible unless the process was live, loopback
        health responded, `/v1/models` advertised its exact id, the endpoint
        completed a bounded probe, adapters supplied positive evidence, and the
        receipt publisher accepted the observed details.
        """

        # There is no named configured chair to refuse if this boundary is
        # called with an invented value.  Validate it before using the registry's
        # named-chair refusal path.
        if not isinstance(identity, ChairIdentity):
            raise ServingConfigurationError("serving start requires one resolved ChairIdentity")
        if not isinstance(tier, str) or not tier:
            raise ServingConfigurationError("serving start requires one non-blank placement tier")
        if self._active is not None or self._residency_handle is not None:
            # Do not route this through failed-launch cleanup: a held lease is
            # intentional evidence that a prior shutdown has not been verified.
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
            profile = _launchable(self.recipes.for_identity(identity, tier), identity)
            self._assert_runtime(profile)
            primary_snapshot = self.registry.ensure(identity)
            base_identity, base_snapshot, base_profile = self._base_material(
                identity, tier, primary_snapshot
            )
            endpoint = profile.endpoint
            # The lock spans endpoint probing and any failed launch cleanup, not
            # merely the successful `ServiceHandle` lifetime.  Otherwise two pod
            # assemblers can race from an apparently empty endpoint into GPU
            # co-residency.
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
                # `chair-serving-receipt.v1`'s `pixel_cap` is the **total pixel
                # count** that went to vLLM, because that is the only pixel
                # bound a serving moment actually had.  It is the third site of
                # the one word this branch already had to disentangle once:
                # `config/pod_placement.toml`'s `pixel_cap` is a longest edge,
                # and `ServingSmokeReader._assert_profile_within_placement`
                # holds the arithmetic that relates them.  A reader comparing a
                # receipt's `pixel_cap` with a placement plan's is comparing
                # 2359296 with 1792 and will conclude the wrong thing, exactly
                # as this package did before that check was corrected.
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
            # The chair boundary has already named this failure. If cleanup is
            # also unverified, both facts must leave through the one refusal
            # the registry raises; a second call would mask the first.
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
            # KeyboardInterrupt/SystemExit must not strand a child between
            # launch and handle publication.  When cleanup proves the child and
            # endpoint are gone, the control-flow signal is re-raised
            # unchanged: nothing is owed to anyone but the operator who sent it.
            #
            # When cleanup *cannot* prove that, the two facts cannot both be
            # this exception, and the possibly-resident child wins.  So the
            # interrupt is deliberately converted into a `ServiceStopError`
            # carrying both halves, and the control-flow signal is spent to say
            # so.  The cost is real and is accepted knowingly: an ordinary
            # `except Exception` above this frame — `PreflightRunner._smoke` is
            # the one in this repository — will now catch what was a Ctrl-C and
            # turn it into a red issue rather than unwinding. That is the
            # louder of the two outcomes, because the retained lease then makes
            # every following start refuse by name, whereas an interrupt that
            # unwound past a handler would leave the stop failure in a
            # traceback nobody stores (GOVERNANCE 2).
            cleanup_error = self._attempt_cleanup(process, endpoint)
            if cleanup_error is not None:
                raise ServiceStopError(
                    "serving start was interrupted and cleanup could not be verified: "
                    f"start={type(error).__name__}: {error}; stop={cleanup_error}"
                ) from error
            raise

    def request(
        self, handle: ServiceHandle, kind: str, payload: Mapping[str, object]
    ) -> OpenAIResult:
        """Send a regular non-streaming request to the handle's exact served alias."""

        self._require_active(handle)
        self._assert_process_live(handle.process)
        result = self._post_probe(
            endpoint=handle.endpoint,
            kind=kind,
            payload=payload,
            model_id=handle.profile.served_model_id,
            seed=handle.profile.seed,
            deterministic=False,
        )
        handle._requests_completed += 1
        handle._last_request_was_fixture = False
        return result

    def stop(self, handle: ServiceHandle) -> None:
        """Stop one exact owned process and verify its endpoint no longer responds."""

        self._require_active(handle)
        try:
            self._stop_process(handle.process)
            self._assert_endpoint_absent(handle.endpoint)
            self._release_residency()
        except BaseException as error:
            # Deliberately retain both the active handle and the pod lease.  A
            # following start must not convert a failed exact shutdown into a
            # second GPU resident process.  The caller may call `stop()` again
            # after repairing an endpoint/process problem.
            if isinstance(error, ServiceStopError):
                raise
            # Name the type as well as the message.  `_stop_process` observes
            # its child with `process.poll()` outside its own try, so a
            # `ServerProcess` implementation that raises there arrives here as
            # it stands — and `str()` of an exception raised with no arguments
            # is the empty string, which would report an unstopped child as
            # `VLLM_STOP_FAILED: ` and nothing else (GOVERNANCE 2).
            raise ServiceStopError(f"{type(error).__name__}: {error}") from error
        else:
            self._active = None

    def recover_failed_start(self) -> None:
        """Retry exact cleanup after a failed start retained the pod lease.

        This is deliberately not a new launch path.  It is the only recovery
        action available when a readiness/publisher failure also left this
        manager unsure whether its owned child or endpoint is gone.  Once it
        proves absence, the original lease is released and a later named start
        may proceed.
        """

        if self._active is not None:
            raise ServiceStopError("active service must be stopped through its ServiceHandle")
        if self._residency_handle is None:
            return
        error = self._attempt_cleanup(self._unready_process, self._unready_endpoint)
        if error is not None:
            raise error

    def _base_material(
        self,
        identity: ChairIdentity,
        tier: str,
        primary_snapshot: VerifiedSnapshot,
    ) -> tuple[ChairIdentity, VerifiedSnapshot, ServingProfile]:
        if identity.adapter_of is None:
            return (
                identity,
                primary_snapshot,
                _launchable(self.recipes.for_identity(identity, tier), identity),
            )
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
            self.registry.ensure(configured_base),
            _launchable(self.recipes.for_identity(configured_base, tier), configured_base),
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
                "GET", health_url(endpoint), body=None, timeout_seconds=2.0
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
        while True:
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
            try:
                health = self.http.request(
                    "GET", health_url(profile.endpoint), body=None, timeout_seconds=2.0
                )
                if health.status != 200:
                    raise ReadinessError(
                        "VLLM_HEALTH_UNAVAILABLE", f"/health returned HTTP {health.status}"
                    )
                models = self.http.request(
                    "GET", models_url(profile.endpoint), body=None, timeout_seconds=2.0
                )
                model_ids = require_exact_model_id(models, profile.served_model_id)
                probe = self._post_probe(
                    endpoint=profile.endpoint,
                    kind=profile.readiness_probe.kind,
                    payload=profile.readiness_probe.request_payload(),
                    model_id=profile.served_model_id,
                    seed=profile.seed,
                    deterministic=True,
                )
                return ReadinessEvidence(
                    health_status=health.status,
                    model_ids=model_ids,
                    probe=probe,
                    ready_at=_utc_stamp(self.now()),
                )
            except EndpointUnavailable as error:
                last = f"loopback endpoint unavailable: {error}"
            except ReadinessError as error:
                last = str(error)
            if self.monotonic() >= deadline:
                raise ReadinessError("VLLM_WATCHDOG_TIMEOUT", last)
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
    ) -> OpenAIResult:
        response = self.http.request(
            "POST",
            endpoint_for_probe(endpoint, kind),
            body=request_body(payload, model_id=model_id, seed=seed, deterministic=deterministic),
            timeout_seconds=10.0,
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
            "GET", models_url(profile.endpoint), body=None, timeout_seconds=2.0
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
    ) -> Mapping[str, object]:
        """Return operational evidence deliberately kept outside receipt schema v1."""

        argv_digest = hashlib.sha256(
            json.dumps(list(argv), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        pins = model_and_tokenizer_pins(base_snapshot.identity)
        # A local-repository chair has no commit to record. The audit says so
        # positively — `revision_kind` names which pin actually bound the
        # launch — rather than leaving two nulls a reader has to interpret.
        model_revision, tokenizer_revision = (
            pins if pins is not None else (base_snapshot.identity.receipt_revision,) * 2
        )
        return MappingProxyType(
            {
                "schema": "serving-launch-audit.v1",
                "chair": identity.role,
                "producer": self.producer,
                "configuration_inputs": self.config_inputs.to_record(),
                "started_at": started_at,
                "endpoint": profile.endpoint,
                "profile": {
                    "recipe": profile.recipe,
                    "tier": profile.tier,
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
        # One log per launch, never a per-chair log reopened: a previous
        # launch's fatal signature still in the tail would abort this one's
        # readiness poll, which is the trap the old pipeline worked around
        # instead of removing.
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
        """The one cleanup sequence shared by a failed start and its later retry.

        Stop the exact owned process, verify its endpoint is absent, then
        release the residency lease — in that order, because releasing first
        would let a second start proceed while this process might still be
        live.  On any failure, the process/endpoint are recorded as unready so
        :meth:`recover_failed_start` can retry exactly this same cleanup;
        recording them again with the same values on that retry's own failure
        is a no-op, not a second distinct effect.

        The stop failure is returned rather than raised or suppressed: an
        unknown still-live child is not secondary to the readiness or publisher
        problem that exposed it, and both facts have to reach the one refusal
        the registry raises (GOVERNANCE 2).
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
            try:
                response = self.http.request(
                    "GET", health_url(endpoint), body=None, timeout_seconds=2.0
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

        `also` exists because a failed launch can produce two independent
        facts, and reporting either alone loses the other. Refusing with only
        the stop failure tells an operator the process would not go away and
        never mentions that it died of CUDA out-of-memory; refusing with only
        the launch failure hides a child that may still hold the card. Both go
        in one message, because the registry raises one refusal and whatever is
        not in it is not anywhere (GOVERNANCE 2).
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


def _launchable(
    profile: "ServingProfile | FixtureProfile", identity: ChairIdentity
) -> ServingProfile:
    """Refuse a fixture profile by the reason it is a fixture, before anything else.

    The walking skeleton's chairs are answered by
    ``common.stage.fixture_serving_details`` — declared values that say
    ``fixture://`` out loud — and nothing in this package substitutes for that.
    Refusing here rather than letting an unsatisfiable package pin do it keeps
    the operator from chasing a runtime-version complaint that is true about the
    wrong thing, and keeps the refusal from being an accident of one field's
    value: edit that pin to match and the manager would try to serve a directory
    of fixture bytes to a real engine.
    """

    if isinstance(profile, FixtureProfile):
        raise ServingConfigurationError(
            f"chair {identity.role!r} resolves to fixture serving profile {profile.recipe!r} "
            f"at tier {profile.tier!r} ({profile.description}); a fixture profile is never "
            "launched, and the offline walking skeleton answers it from declared serving "
            "details instead"
        )
    if profile.preflight_state != "proven":
        raise ServingConfigurationError(
            f"chair {identity.role!r} serving profile is structurally marked "
            f"preflight_state={profile.preflight_state!r}; real-silicon preflight must "
            "prove this exact profile before launch"
        )
    return profile


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

    The model and tokenizer arguments point at the locally verified snapshot.
    Whether the two revision flags accompany them is
    ``model_and_tokenizer_pins``' decision, and its docstring holds the reason
    a local-repository chair correctly gets neither.
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
    # An adapted chair registers its own alias through `--lora-modules` below,
    # so the API identity of the served weights is the base chair's; an
    # unadapted chair is itself the API identity.
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
        # Page contents are not diagnostic data.  Keep vLLM's documented
        # request logging disabled independently of ordinary engine logging.
        "--no-enable-log-requests",
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
                        # vLLM receives the immutable configured source ref,
                        # not an API alias that happened to be registered for
                        # this process.
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

    Spec 04 requires a red preflight to carry useful remediation, and spec 12
    requires operator-facing errors to say what happened and what to do next.
    The two loud LoRA/model rejections therefore need named refusals rather than
    a generic watchdog timeout. The old pipeline's launch scripts, which grepped
    `does not support LoRA` and `Unknown model`, are historical evidence for the
    strings rather than the authority for this behavior.

    Those scripts also grepped `RuntimeError|ValueError`, and that is
    deliberately **not** carried.  This poll re-reads the whole launch tail
    every interval, so one benign line naming either word aborts a start that
    was going to succeed.  A missed signature costs a bounded watchdog wait; a
    false one costs a relaunch, and on the real path that is GPU-hours.

    `VLLM_ERROR` is kept, and is honestly a marker with no producer on this
    path.  vLLM prints no such string: it was the *wrapper script's* own echo
    in the old pipeline (`/window/remote/serve_chandra.sh` line 87 writes it to
    the wrapper's stdout after grepping the engine log), and this poll reads
    only the child's own launch log.  It stays because a future launch wrapper
    that does write into that log has one agreed word for "fatal, stop
    waiting", and because a signature that never fires costs nothing — the risk
    it carries is a false positive from some later vLLM string containing
    `vllm_error`, not a missed failure.  It is not evidence that vLLM says
    anything.

    A bare `traceback` is declined for exactly the same reason.  vLLM has
    printed a benign, logged-and-swallowed traceback at startup for an
    optional backend that failed to import (FlashInfer probing is the
    documented case: vllm-project/vllm#12513, #30240) while still going on to
    serve normally.  Because this poll re-reads the whole tail every interval,
    that one line would make the chair deterministically unstartable, not
    merely cost one relaunch.  `EngineDeadError` and `CUDA out of memory`
    already name the loud fatal cases, and `VLLM_PROCESS_EXITED`
    (`_assert_process_live`, checked earlier in the same loop) catches a
    process that a bad traceback actually killed.
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
    """Extract one image vLLM will receive from an OpenAI chat payload.

    A recursive ``image_url`` search is deliberately insufficient: vLLM may
    ignore unknown extension fields, which would let a text-only request claim
    fixture evidence.  The sole image must be a typed ``image_url`` part in a
    user message's ``content`` list, and no other ``image_url`` key may exist
    elsewhere in the payload.
    """

    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ServingConfigurationError(f"{label} must contain a non-empty chat messages list")
    active_candidates: list[object] = []
    for message in messages:
        if not isinstance(message, Mapping):
            raise ServingConfigurationError(f"{label} chat messages must be objects")
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, Mapping) or part.get("type") != "image_url":
                continue
            if message.get("role") != "user":
                raise ServingConfigurationError(
                    f"{label} image_url must be in a role=user content block"
                )
            if set(part) != {"type", "image_url"}:
                raise ServingConfigurationError(
                    f"{label} image_url content block must contain only type and image_url"
                )
            active_candidates.append(part["image_url"])
    all_candidates = _image_url_candidates(payload)
    if len(active_candidates) != 1 or len(all_candidates) != 1:
        raise ServingConfigurationError(
            f"{label} must contain exactly one active image_url content block and no ignored image_url fields"
        )
    candidate = active_candidates[0]
    if not isinstance(candidate, Mapping) or "url" not in candidate:
        raise ServingConfigurationError(
            f"{label} image_url must be an OpenAI image object with a non-blank URL"
        )
    if set(candidate) - {"url", "detail"}:
        raise ServingConfigurationError(f"{label} image_url has fields outside OpenAI url/detail")
    detail = candidate.get("detail")
    if detail is not None and not isinstance(detail, str):
        raise ServingConfigurationError(f"{label} image_url detail must be a string when supplied")
    candidate = candidate["url"]
    if not isinstance(candidate, str) or not candidate:
        raise ServingConfigurationError(f"{label} image_url must contain a non-blank URL")
    if not candidate.startswith("data:"):
        raise ServingConfigurationError(
            f"{label} image_url must be a local image data URI, not a remote/file URL"
        )
    try:
        header, encoded = candidate.split(",", 1)
    except ValueError as error:
        raise ServingConfigurationError(f"{label} data URI has no payload") from error
    if not header.startswith("data:image/") or ";base64" not in header:
        raise ServingConfigurationError(
            f"{label} data URI must declare an image MIME type and base64 payload"
        )
    if not encoded:
        raise ServingConfigurationError(f"{label} data URI payload must not be empty")
    try:
        data = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error) as error:
        raise ServingConfigurationError(f"{label} data URI payload is not valid base64") from error
    if not data:
        raise ServingConfigurationError(f"{label} data URI decoded to no bytes")
    return data


def _image_url_candidates(value: object) -> list[object]:
    if isinstance(value, Mapping):
        candidates: list[object] = []
        for key, item in value.items():
            if key == "image_url":
                candidates.append(item)
            else:
                candidates.extend(_image_url_candidates(item))
        return candidates
    if isinstance(value, list):
        return [candidate for item in value for candidate in _image_url_candidates(item)]
    return []


def _immutable_json_value(value: object) -> object:
    """Deep-freeze one already-validated JSON value exposed on a live handle."""

    if isinstance(value, Mapping):
        return MappingProxyType({key: _immutable_json_value(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_immutable_json_value(item) for item in value)
    return value
