"""The safe, plain-language façade for the operator's seven words.

This module deliberately has no live provider or S3 adapter.  It joins the
existing fake-first seams into an offline rehearsal, persists what the operator
confirmed before an action, and leaves the provider-facing machinery behind the
surface.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import io
import json
import os
import secrets
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Final, Iterator, Protocol, Sequence

from common.chairs.config import load_models_toml
from common.contracts.canonical import canonical_bytes, digest_bytes
from common.contracts.errors import ContractError, SchemaRefusal
from common.contracts.identities import artifact_id, validate_run_id
from common.contracts.stages import ARMARIUM, WRITING_DIRECTORIES
from common.runtree.store import (
    DOOR_MANIFEST_FILE,
    MANIFEST_FILE,
    MAX_RECORD_READ_BYTES,
    RECEIPTS_DIR,
    RUN_FILE,
    SERVING_LOGS_DIR,
    RunTree,
)
from common.stage import load_fixture
from common.witness_context import validate_witness_context_configuration
from operations.pod.arming import ControllerArming, ControllerReadiness
from operations.pod.bootstrap import (
    CONFIGURATION_RECEIPT_SCHEMA,
    BootstrapJournal,
    Bootstrapper,
    BootstrapPlan,
    BootstrapStep,
    BootstrapStepFailure,
)
from operations.pod.launch import (
    LaunchResult,
    LaunchState,
    PaidActionPreview,
    PodRuntime,
    phraseless,
    price_move_note,
)
from operations.pod.lease import LeaseStore, PodLease
from operations.pod.models import (
    DEFAULT_CONTAINER_DISK_GB,
    PodCreateRequest,
    PodEstimate,
    PodRecord,
    PodRuntimeContract,
    ProviderFailure,
    require_billing_cutoff_margin_seconds,
    require_utc,
)
from operations.pod.notify_bridge import NotifyOutcome as PodNotifyOutcome
from operations.pod.pod_run import DEFAULT_RUNS_DIRECTORY
from operations.pod.preflight import (
    CacheMismatch,
    GpuProfile,
    PreflightRunner,
    SmokeResult,
    UtilizationSample,
    load_placement_table,
)
from operations.pod.shutdown import CloseReport, VerifiedShutdown
from operations.pod.spend import (
    PRICE_MOVE_MARKER,
    SpendPolicy,
    load_spend_policy,
)
from operations.pod.supervise import (
    identity_path as _supervisor_identity_path,
)
from operations.pod.supervise import (
    peek_running as _supervisor_peek_running,
)
from operations.pod.supervise import (
    read_identity as _read_supervisor_identity,
)
from operations.pod.transfer import (
    ChecksummedTransfer,
    TransferFailure,
    TransferTarget,
    normalize_transfer_prefix,
)
from operations.submit import submit as submission_door

from . import notify_bridge
from ._run_tree_paths import is_publication_temporary
from .custody import credential_free_environment
from .errors import ErrorCode, OperatorError, strip_control_bytes
from .fakes import LocalFixtureObjectStore, OperatorFakeProvider
from .notify_bridge import Notifier
from .records import (
    MAX_RECORD_BYTES,
    DescriptorStore,
    ReceiptStore,
    RecordError,
    sha256_file,
    utc_stamp,
)
from .volume_cost import volume_cost_lines
from .volume_s3 import (
    TRANSFER_CREDENTIAL_ENV,
    S3VolumeObjectReader,
    S3VolumeTarget,
    VolumeSpec,
    VolumeTransferRefusal,
)

UTC = timezone.utc
OPERATOR_CLOSE_PREFIX = "CLOSE"
DEFAULT_FIXTURE = "synthetic-two-page-v0"
MAX_SEALED_MANIFEST_BYTES = 4 * 1024 * 1024
# The Door's program path, named once. Two spellings of it — the fault drill's
# call site and the guard that decides the drill forwards real ingress — is how
# the drill could go on running while quietly stopping injecting: change one and
# the comparison fails silently, rehearsing a fixture door under a real
# submission's name.
DOOR_PROGRAM = "pipeline/1_exemplar/door.py"
# Read from the transfer that owns these names rather than spelled again here: a
# second literal copy would keep this stripper green while a newly added upload
# credential stayed in a stage's environment.
_TRANSFER_CREDENTIAL_ENV = TRANSFER_CREDENTIAL_ENV
_COPY_CHUNK_BYTES = 1024 * 1024
FETCH_RUN_PREFIX = DEFAULT_RUNS_DIRECTORY
"""Where `pod_run` writes run trees on the volume, relative to its mount:
`<volume>/runs/<run_id>` (`operations/pod/pod_run.py`, `DEFAULT_RUNS_DIRECTORY`)."""
FETCH_EVIDENCE_PREFIX = "preflight"
"""Where a launch's PREFLIGHT evidence sits on the volume, relative to its mount:
`<volume>/preflight/<report stem>/` (`operations/pod/bootstrap_main.py`,
`PREFLIGHT_DIRECTORY`) -- the golden page, the serving logs, and the
content-addressed serving receipts, launch audits and evidence manifests. Spelled
here rather than imported because `bootstrap_main` pulls the whole serving stack
in behind it, which this surface has no business loading to learn a directory
name; `operations/pod/test_pod_run.py` holds the two spellings together."""
EVIDENCE_DIRECTORY = "evidence"
"""Where fetched evidence lands under `--into`, beside `<run_id>/` rather than
inside it: the run tree must stay byte-for-byte what the volume holds under
`runs/<run_id>/`, and the tree's own inventory scope accounts for nothing else."""
MAX_FETCH_EVIDENCE_OBJECTS = 10_000
"""A launch's preflight directory holds a page, some logs and a handful of
receipts. Ten thousand is far past that and still bounds a listing that is not
what this verb thinks it is."""
MAX_FETCH_OBJECT_BYTES = 256 * 1024 * 1024
"""One object's bound. A whole-page blob is the largest thing a run tree holds;
the manifest walk already refuses an artifact above 64 MiB, and a quarter of a
gigabyte is past any page this project has rendered."""
# `FakeProvider.bill()` always stamps its cutoff exactly one hour **ahead** of
# its own clock -- `fake_provider.py`'s `cutoff_at=self.now() + timedelta(hours=1)`
# -- so a frozen test clock still opens a valid, non-empty billing window. This
# surface is fixture-only and always closes through that same `bill()`, so its
# billing-cutoff margin must reach forward far enough to cover that stamp --
# matching the margin `operations/pod/test_pod_runtime.py` pairs with `.bill()`
# throughout.
#
# **This comment said "backdates" until 2026-08-11, and the direction matters.**
# A reader who believes the cutoff is in the past concludes that a *smaller*
# margin is the safe direction. It is the opposite, and acting on that reversed
# belief is exactly the defect below at `_shutdown`.
FIXTURE_BILLING_CUTOFF_MARGIN_SECONDS = 3600


class _UploadManifestConflict(TransferFailure):
    """An occupied prefix seals another submission; no target write has occurred."""


class Presenter(Protocol):
    """Output seam kept small enough for transcript and CLI tests."""

    def __call__(self, line: str = "") -> None:
        """Present one line to the operator."""


@dataclass(slots=True)
class Faults:
    """One-shot failure injection used only by the hardening drills."""

    provider_timeout: bool = False
    provider_error: bool = False
    partial_upload: bool = False
    failed_close: bool = False
    laptop_crash: bool = False
    cache_failure: bool = False


@dataclass(frozen=True, slots=True)
class PreparedLaunch:
    """The exact preview the operator saw before typing a paid confirmation."""

    request: PodCreateRequest
    action: str
    adopted_pod_id: str | None
    result: LaunchResult
    policy: SpendPolicy
    runtime: PodRuntime

    @property
    def review_record(self) -> dict[str, object]:
        """The reviewed request and priced preview in the one record the UI keeps."""

        if self.result.preview is None:
            raise OperatorError(ErrorCode.CONFIRMATION_REQUIRED)
        return _review_record(self.request, self.action, self.adopted_pod_id, self.result.preview)

    @property
    def review_digest(self) -> str:
        """The digest of the exact request, price, and ceilings presented."""

        return digest_bytes(canonical_bytes(self.review_record))

    @property
    def confirmation_phrase(self) -> str:
        """The phrase this exact price screen requires, derived from that screen.

        `operations/pod/spend.py` builds it from the action, the subject and both
        hourly rates just displayed, so it cannot be typed from memory or pasted
        by someone who has not read what is about to bill. There is deliberately
        no constant here for a caller to reach for instead.
        """

        if self.result.preview is None:
            raise OperatorError(ErrorCode.CONFIRMATION_REQUIRED)
        return self.result.preview.confirmation_phrase


@dataclass(frozen=True, slots=True)
class PreparedClose:
    """The exact close notice the operator saw before typing its confirmation."""

    launch: dict[str, Any]
    record: PodRecord
    lease_store: LeaseStore
    lease: PodLease
    phrase: str


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """Existing Armarium truth, projected for the operator after the run finishes."""

    state: str
    run_root: Path
    run_id: str
    aggregate: dict[str, Any]
    export_payload: dict[str, Any]


class FixtureControllerArmer:
    """A visibly fixture-only two-controller acknowledgement for fake pod drills."""

    def __init__(self, now: Callable[[], datetime]) -> None:
        self.now = now

    def preflight(
        self, *, action: str, request: PodCreateRequest, policy: SpendPolicy
    ) -> ControllerReadiness:
        return ControllerReadiness(
            True,
            self.now(),
            "fixture controller handshake is available",
            {"action": action, "mode": "offline-fixture"},
        )

    def arm(self, *, action, request, record, lease, store, owner_token, policy):  # type: ignore[no-untyped-def]
        del action, store, owner_token, policy
        observed = self.now()
        stamp = utc_stamp(observed)
        # `request` is the sealed request: its `--report-path` has already been
        # bound to this exact launch token (`launch._bind_report_path_to_launch`).
        # `_validate_arming_binding` compares that exact value against what this
        # acknowledgement reports, so a fixture-only constant here can never match.
        command = request.docker_start_cmd
        report_path = command[command.index("--report-path") + 1]
        return ControllerArming(
            True,
            True,
            observed,
            "fixture laptop controller and fixture pod timer acknowledged",
            {
                "lease_id": lease.lease_id,
                "pod_id": record.pod_id,
                "hard_deadline": utc_stamp(lease.hard_deadline),
                "laptop_supervisor": {
                    "identity": "fixture-laptop-supervisor",
                    "started_at": stamp,
                },
                "pod_timer": {
                    "report_path": report_path,
                    "acknowledged_at": stamp,
                },
            },
        )


class FixtureCache:
    """A preflight cache seam that reports fixture verification only."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def verify(self, identity):  # type: ignore[no-untyped-def]
        if self.fail:
            raise CacheMismatch("injected fixture cache mismatch")
        return {"state": "fixture-verified", "chair": identity.role}

    def refetch_once(self, identity):  # type: ignore[no-untyped-def]
        del identity


class FixtureSmokeReader:
    """A proof-page seam that never claims to have reached a model service."""

    def read(self, identity, fixture, placement):  # type: ignore[no-untyped-def]
        del fixture, placement
        return SmokeResult(
            True,
            True,
            True,
            {"state": "fixture-smoke", "chair": identity.role},
            (UtilizationSample(Decimal("0"), Decimal("0")),),
        )


class FixtureBootstrapActions:
    """Runs the real Bootstrapper journal without checkout, download, or network effects."""

    def __init__(self, surface: "OperatorSurface", *, transfer_receipt: Path | None) -> None:
        self.surface = surface
        self.transfer_receipt = transfer_receipt

    def checkout_commit(self, commit: str) -> dict[str, object]:
        return {"commit": commit, "mode": "fixture-only; no network checkout"}

    def validate_configuration(self) -> dict[str, object]:
        root = self.surface.workspace
        config_root = root / "config"
        shipped_config_root = Path(__file__).resolve().parents[2] / "config"
        validation = validate_witness_context_configuration(
            load_models_toml(config_root / "models.toml"),
            config_root / "witness_context.toml",
            shipped_config_root=shipped_config_root,
        )
        return {
            "schema": CONFIGURATION_RECEIPT_SCHEMA,
            "bindings": {
                name: {
                    "path": str(config_root / filename),
                    "sha256": sha256_file(config_root / filename),
                }
                for name, filename in (
                    ("models_config", "models.toml"),
                    ("witness_context_config", "witness_context.toml"),
                    ("serving_recipes_config", "serving_recipes.toml"),
                    ("placement_config", "pod_placement.toml"),
                )
            },
            "witness_context_validation": validation.to_record(),
        }

    def sync_uv_environment(self, lockfile: Path) -> dict[str, object]:
        if not lockfile.is_file():
            raise BootstrapStepFailure(
                BootstrapStep.UV_ENVIRONMENT,
                "the pinned lockfile is missing",
                "Restore the repository lockfile, then run `verbatus boot` again.",
            )
        return {
            "lockfile": str(lockfile),
            "sha256": sha256_file(lockfile),
            "mode": "fixture-only; no environment was changed",
        }

    def resume_transfer(self) -> dict[str, object]:
        if self.transfer_receipt is None:
            return {"state": "no-upload-recorded", "mode": "fixture-only"}
        return {"state": "recorded-upload", "receipt": str(self.transfer_receipt)}

    def materialize_model_store(self) -> dict[str, object]:
        """Report the required step without fetching weights from this fake-only surface.

        Real acquisition belongs to pod launch; fixture bootstrap must still
        account for the step explicitly so a green journal cannot omit it.
        """
        return {"state": "no-materialization", "mode": "fixture-only; no weights are fetched"}

    def verify_chair_cache(self) -> dict[str, object]:
        return {"state": "fixture-cache-check", "mode": "no download"}

    def run_preflight(self) -> dict[str, object]:
        root = self.surface.workspace
        models = load_models_toml(root / "config" / "models.toml")
        injected_cache_failure = self.surface.faults.cache_failure
        self.surface.faults.cache_failure = False
        runner = PreflightRunner(
            models,
            load_placement_table(root / "config" / "pod_placement.toml"),
            FixtureCache(fail=injected_cache_failure),
            FixtureSmokeReader(),
            root / "proof" / "fixtures" / DEFAULT_FIXTURE / "page-1.png",
        )
        report = runner.run(
            GpuProfile(
                name="fixture 48 GiB GPU",
                cuda_version="fixture",
                driver_version="fixture",
                compute_capability=(9, 0),
                vram_gib=Decimal("48"),
                disk_gib=Decimal("100"),
                dtype="float16",
            )
        )
        record = report.to_record()
        if report.color != "green":
            raise BootstrapStepFailure(
                BootstrapStep.PREFLIGHT,
                "fixture preflight returned red",
                "Repair the named fixture check, then run `verbatus boot` again; this is safe.",
            )
        return record


class OperatorSurface:
    """One durable, fake-only surface over launch, boot, upload, run, export, close, and status."""

    def __init__(
        self,
        workspace: str | Path,
        state_root: str | Path,
        *,
        provider: OperatorFakeProvider | None = None,
        now: Callable[[], datetime] | None = None,
        present: Presenter | None = None,
        faults: Faults | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        notifier: Notifier | None = None,
        monotonic: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.state_root = Path(state_root).resolve()
        self.now = now or (lambda: datetime.now(UTC))
        self._present: Presenter = present or print
        candidate = provider or OperatorFakeProvider(now=self.now)
        # OperatorFakeProvider exactly, not the base FakeProvider: close() calls
        # clear_failures, _provider_for_record calls seed_existing, and
        # _shutdown's billing-margin floor is gated on this subclass — a plain
        # FakeProvider would pass construction, then die at close with a bare
        # AttributeError and silently skip the floor that keeps a rehearsal's
        # shutdown from reporting a spurious UNVERIFIED.
        if not isinstance(candidate, OperatorFakeProvider):
            raise OperatorError(ErrorCode.LIVE_PROVIDER_BLOCKED)
        self.provider = candidate
        self.faults = faults or Faults()
        self.runner = runner or _run_program
        # Silent unless asked: no test and no first rehearsal sends a ping.
        self.notifier: Notifier = notifier or notify_bridge.silent
        self.monotonic = monotonic or time.monotonic
        self.sleeper = sleeper or time.sleep
        self.receipts = ReceiptStore(self.state_root, now=self.now)
        self.descriptor = DescriptorStore(self.state_root)

    def present(self, line: str = "") -> None:
        """Show one line, with terminal control bytes removed on the way out.

        `errors.py` already makes this argument for an error detail. It holds
        for everything else this surface prints, and for more of it: a recorded
        pod id, a receipt summary, and a reconciliation row carrying a page
        census's own refusal reason all reach here from a file or a run tree
        rather than from a constant. Stripping belongs on the channel, not on
        one kind of string. The receipt keeps the bytes it was given —
        GOVERNANCE 4 — the terminal simply does not get to act on them.
        """

        self._present(strip_control_bytes(line))

    # -- launch ---------------------------------------------------------------

    def prepare_launch(
        self,
        request: PodCreateRequest,
        *,
        policy_path: str | Path,
        adopt_pod_id: str | None = None,
    ) -> PreparedLaunch:
        """Make the non-billable preview that must precede every paid action."""

        try:
            policy = _load_policy(policy_path)
        except OperatorError as error:
            if adopt_pod_id is not None and error.code is ErrorCode.SPEND_POLICY_REQUIRED:
                raise OperatorError(ErrorCode.ADOPTION_REFUSED, detail=error.detail) from error
            raise
        self._refuse_if_active_pod()
        self._inject_provider_preview_fault()
        runtime = self._runtime(policy)
        result = (
            runtime.preview_adopt(adopt_pod_id, expected=request)
            if adopt_pod_id is not None
            else runtime.preview_create(request)
        )
        action = "adopt" if adopt_pod_id is not None else "create"
        prepared = PreparedLaunch(request, action, adopt_pod_id, result, policy, runtime)
        self._show_paid_preview(prepared)
        self._record_spend_alert(prepared)
        if result.state is not LaunchState.PREVIEW or result.preview is None:
            self._record_failure("launch", result.state.value, result.detail)
            if adopt_pod_id is not None:
                raise OperatorError(ErrorCode.ADOPTION_REFUSED, detail=result.detail)
            raise self._launch_error(result)
        if not result.preview.assessment.allowed:
            assessment = result.preview.assessment
            if assessment.balance_unobservable_triggered:
                refusal_state = LaunchState.REFUSED_BALANCE_UNOBSERVABLE.value
                code = ErrorCode.BALANCE_UNOBSERVABLE
            elif assessment.hard_floor_triggered:
                refusal_state = LaunchState.REFUSED_BALANCE_FLOOR.value
                code = ErrorCode.BALANCE_FLOOR_REACHED
            else:
                refusal_state = LaunchState.REFUSED_CEILING.value
                code = (
                    ErrorCode.SPEND_POLICY_REQUIRED
                    if not policy.configured
                    else ErrorCode.PAID_ACTION_REFUSED
                )
            self._record_failure("launch", refusal_state, result.detail)
            if adopt_pod_id is not None:
                code = ErrorCode.ADOPTION_REFUSED
            raise OperatorError(code, detail=result.detail)
        return prepared

    def launch(self, prepared: PreparedLaunch, confirmation: str | None) -> LaunchResult:
        """Record the typed value, then let the spend gate validate it once."""

        with self._exclusive_paid_launch():
            # This re-check must remain inside the cross-process claim: the active
            # receipt does not exist until after the provider call returns.
            self._refuse_if_active_pod()
            # Read first, so a prepared launch carrying no preview leaves through
            # this property's own three-part refusal. Reached inside the payload
            # below it would instead be an attribute error on `None` — a raw
            # traceback on the money path, which this surface never shows.
            review = prepared.review_record
            # Only PodRuntime may validate and consume a challenge. This durable
            # receipt commits to the input bytes without retaining a spendable phrase.
            confirmation_receipt = self._write_action(
                "launch-confirmation",
                {
                    "summary": f"Paid {prepared.action} confirmation recorded before the provider call.",
                    "action": prepared.action,
                    "adopted_pod_id": prepared.adopted_pod_id,
                    "request": _request_record(prepared.request),
                    "preview": review["preview"],
                    "review": review,
                    "review_sha256": prepared.review_digest,
                    "confirmation_sha256": (
                        None if confirmation is None else digest_bytes(confirmation.encode("utf-8"))
                    ),
                },
                descriptor_action="launch-confirmation",
                failure_code=ErrorCode.CONFIRMATION_RECORD_FAILED,
            )
            result = (
                prepared.runtime.adopt(
                    prepared.adopted_pod_id or "",
                    expected=prepared.request,
                    confirmation=confirmation,
                )
                if prepared.adopted_pod_id is not None
                else prepared.runtime.create(prepared.request, confirmation=confirmation)
            )
            if not result.green or result.record is None:
                receipt = self._write_action(
                    "launch",
                    {
                        "summary": f"Paid {prepared.action} did not become ready: {result.state.value}.",
                        "state": result.state.value,
                        "detail": result.detail,
                        "confirmation_receipt": self._state_relative(confirmation_receipt),
                        "presented_review_sha256": prepared.review_digest,
                        "current_preview": (
                            None
                            if result.preview is None
                            else phraseless(result.preview).to_record()
                        ),
                        "current_review_sha256": (
                            None
                            if result.preview is None
                            else digest_bytes(
                                canonical_bytes(
                                    _review_record(
                                        prepared.request,
                                        prepared.action,
                                        prepared.adopted_pod_id,
                                        result.preview,
                                    )
                                )
                            )
                        ),
                    },
                    descriptor_action="launch",
                )
                if prepared.action == "adopt":
                    raise OperatorError(
                        ErrorCode.ADOPTION_REFUSED,
                        detail=f"{result.detail} Saved receipt: {receipt}",
                    )
                raise self._launch_error(result, receipt=receipt)
            receipt = self._write_action(
                "launch",
                {
                    "summary": (
                        "Fixture pod is created with both fixture safety timers recorded."
                        if prepared.action == "create"
                        else "Existing fixture pod is adopted with both fixture safety timers recorded."
                    ),
                    "state": result.state.value,
                    # Gate detail is the only durable evidence of a post-claim price move.
                    "detail": result.detail,
                    "action": prepared.action,
                    "pod": _pod_record(result.record),
                    "request": _request_record(prepared.request),
                    "confirmation_receipt": self._state_relative(confirmation_receipt),
                    # Relative to the state root, like every reference a receipt
                    # makes to a file under it: `close` rejoins it, so a moved
                    # or restored state directory still finds its own lease.
                    "lease": (
                        self._state_relative(result.lease_path)
                        if result.lease_path is not None
                        else None
                    ),
                    "controller_arming": (
                        result.controller_arming.to_record()
                        if result.controller_arming is not None
                        else None
                    ),
                },
                descriptor_action="active-launch",
                additional_descriptor_actions=("launch",),
            )
        moved = (
            ""
            if result.preview is None
            else price_move_note(prepared.result.preview.assessment, result.preview.assessment)
        )
        if moved:
            self.present(f"Price notice{moved}.")
        self.present("Launch rehearsal complete. Both fixture safety timers are recorded.")
        self.present(f"Saved receipt: {receipt}")
        self.present("This rehearsal contacted no cloud provider and created no bill.")
        return result

    # -- upload ---------------------------------------------------------------

    def submit_and_upload(
        self,
        source: str | Path,
        *,
        manifest_out: str | Path,
        policy_path: str | Path | None = None,
        prefix: str = "submission",
        volume: VolumeSpec | None = None,
    ) -> Path:
        """Run Spec 03's local door before transferring only a sealed manifest."""

        try:
            prefix = normalize_transfer_prefix(prefix)
        except ValueError as error:
            raise OperatorError(ErrorCode.INVALID_COMMAND, detail=str(error)) from error
        try:
            submission_door.submit(
                Path(source),
                Path(manifest_out),
                policy_path=(
                    Path(policy_path)
                    if policy_path is not None
                    else submission_door.gate.DEFAULT_POLICY_PATH
                ),
            )
        except Exception as error:
            self._record_failure("upload", "submission-refused", str(error))
            raise OperatorError(ErrorCode.UPLOAD_REFUSED, detail=str(error)) from error
        return self.upload(source, sealed_manifest=manifest_out, prefix=prefix, volume=volume)

    def upload(
        self,
        source: str | Path,
        *,
        sealed_manifest: str | Path,
        prefix: str = "submission",
        volume: VolumeSpec | None = None,
        target: TransferTarget | None = None,
    ) -> Path:
        """Transfer only what the sealed submission record names; no pod is queried.

        The default target is the local fixture volume, so a rehearsal sends
        nothing anywhere. `volume` names a real RunPod network volume instead —
        the one path in this surface that can leave this computer, which is why
        the operator has to name it and is told exactly what will be contacted
        before a byte moves. It is still zero GPU-hours either way: storage
        transfer needs no pod, which is the whole reason this verb runs first.
        """

        source_path = Path(source)
        manifest_path = Path(sealed_manifest)
        if not manifest_path.is_file():
            raise OperatorError(ErrorCode.UPLOAD_MANIFEST_MISSING)
        try:
            manifest_bytes = _read_sealed_manifest(manifest_path)
            manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        except OSError as error:
            raise OperatorError(
                ErrorCode.UPLOAD_MANIFEST_MISSING,
                detail="the sealed submission record could not be read",
            ) from error
        try:
            prefix = normalize_transfer_prefix(prefix)
        except ValueError as error:
            raise OperatorError(ErrorCode.INVALID_COMMAND, detail=str(error)) from error
        fixture_only = volume is None and target is None
        if fixture_only:
            self.present("Upload uses the sealed submission record and the fixture volume.")
        else:
            subject = volume.describe() if volume is not None else "the supplied transfer target"
            self.present(f"Upload will send the sealed submission record to {subject}.")
            self.present("Nothing outside that sealed record is read or sent.")
        self.present("No pod needs to be running. This step uses zero GPU-hours.")
        store: TransferTarget
        if target is not None:
            store = target
        elif volume is not None:
            try:
                store = S3VolumeTarget(volume)
            except Exception as error:
                self._record_failure("upload", "volume-unavailable", str(error))
                raise OperatorError(
                    ErrorCode.UPLOAD_VOLUME_UNAVAILABLE, detail=str(error)
                ) from error
        else:
            store = LocalFixtureObjectStore(
                self.state_root / "fixture-volume",
                fail_once_for=self._fault_upload_key(manifest_path, prefix),
            )
        try:
            snapshot_root = self.state_root / "transfer" / ".manifest-snapshots"
            snapshot_root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="manifest-", dir=snapshot_root) as temporary:
                manifest_snapshot = Path(temporary) / "sealed-manifest.json"
                manifest_snapshot.write_bytes(manifest_bytes)
                # Parse the immutable snapshot before consulting or changing the
                # target. A malformed ledger is a local refusal, not a partial
                # transfer, and must never cause a remote read or write.
                submission_door.load_manifest(manifest_snapshot)
                manifest_key = f"{prefix}-manifest.json"
                remote_manifest = store.inspect(manifest_key, expected_size=len(manifest_bytes))
                if remote_manifest is not None and (
                    remote_manifest.sha256 != manifest_sha256
                    or remote_manifest.size != len(manifest_bytes)
                ):
                    raise _UploadManifestConflict(
                        f"target {manifest_key!r} exists but differs from the sealed submission "
                        "manifest; it was not overwritten"
                    )
                report = ChecksummedTransfer(
                    source_root=source_path,
                    submission_manifest=manifest_snapshot,
                    target=store,
                    prefix=prefix,
                    journal_path=self.state_root / "transfer" / f"{manifest_sha256}.json",
                ).resume()
                # Recheck after the image transfer. A manifest that appeared
                # concurrently owns this prefix and must be compared before a
                # new one is published.
                remote_manifest = store.inspect(manifest_key, expected_size=len(manifest_bytes))
                if remote_manifest is not None and (
                    remote_manifest.sha256 != manifest_sha256
                    or remote_manifest.size != len(manifest_bytes)
                ):
                    raise TransferFailure(
                        f"target {manifest_key!r} exists but differs from the sealed submission "
                        "manifest; it was not overwritten"
                    )
                if remote_manifest is None:
                    store.put_file(
                        manifest_key, io.BytesIO(manifest_bytes), expected_sha=manifest_sha256
                    )
                    remote_manifest = store.inspect(manifest_key, expected_size=len(manifest_bytes))
                if remote_manifest is None or (
                    remote_manifest.sha256 != manifest_sha256
                    or remote_manifest.size != len(manifest_bytes)
                ):
                    raise TransferFailure(
                        f"target {manifest_key!r} did not verify after publication"
                    )
        except (_UploadManifestConflict, ContractError) as error:
            # The snapshot is read through `submission_door.load_manifest`
            # before the target is inspected or a transfer is constructed, and
            # a malformed or non-canonical manifest raises
            # `SubmitRefusal` (a `ContractError`), not one of the transfer
            # exceptions below. A conflicting target manifest is found by that
            # same preflight boundary. Nothing was sent, so this is never
            # `UPLOAD_PARTIAL`, which would tell an operator that some files
            # were verified when none were touched (G13).
            #
            # `UPLOAD_REFUSED`, not `UPLOAD_MANIFEST_MISSING`: the record is
            # present and readable at the path the operator named, and it was
            # refused for what it says. Sending them to "create or locate the
            # sealed submission record" would send them looking for a file
            # sitting where they left it, while `UPLOAD_REFUSED` says exactly
            # what happened -- nothing transferred, no pod started, correct the
            # named file -- and is what `submit_and_upload` already reports for
            # a door refusal of the same record.
            self._record_failure(
                "upload",
                (
                    "manifest-conflict"
                    if isinstance(error, _UploadManifestConflict)
                    else "manifest-refused"
                ),
                str(error),
                facts={
                    "submission_manifest_sha256": manifest_sha256,
                    "zero_gpu_hours": True,
                },
            )
            raise OperatorError(ErrorCode.UPLOAD_REFUSED, detail=str(error)) from error
        except (TransferFailure, VolumeTransferRefusal, OSError, ValueError) as error:
            receipt = self._write_action(
                "upload",
                {
                    "summary": "Upload is partial and can be resumed from its verified files.",
                    "state": "partial-transfer",
                    "submission_manifest_sha256": manifest_sha256,
                    "volume": _volume_record(volume),
                    "detail": str(error),
                    "zero_gpu_hours": True,
                },
                descriptor_action="upload",
            )
            # The cause on the screen as well as in the receipt, like every
            # sibling refusal here: a receipt path where the reason should be
            # sends the operator to open a JSON file to learn what to fix.
            raise OperatorError(
                ErrorCode.UPLOAD_PARTIAL, detail=f"{error} Saved receipt: {receipt}"
            ) from error
        receipt = self._write_action(
            "upload",
            {
                "summary": (
                    "Upload is complete; every recorded file was verified in the fixture volume."
                    if fixture_only
                    else "Upload is complete; every recorded file was verified at its target."
                ),
                "state": "complete",
                "submission_manifest_sha256": manifest_sha256,
                "volume": _volume_record(volume),
                "transfer": report.to_record(),
                "zero_gpu_hours": True,
            },
            descriptor_action="upload",
        )
        self.present("Upload complete. Every file in the sealed record was verified.")
        self.present(f"Saved receipt: {receipt}")
        return receipt

    # -- boot -----------------------------------------------------------------

    def boot(self) -> Path:
        """Run the real bootstrap journal with explicit fixture-only effects."""

        launch_receipt = self._active_launch_receipt()
        upload_receipt = self._descriptor_receipt("upload")
        commit = _repository_commit(self.workspace)
        plan = BootstrapPlan(commit, self.workspace / "uv.lock")
        journal = BootstrapJournal(self.state_root / "boot" / "bootstrap.json", plan, now=self.now)
        report = Bootstrapper(
            journal,
            FixtureBootstrapActions(self, transfer_receipt=upload_receipt),
        ).run()
        payload = {
            "summary": (
                "Boot report is green for the fixture-only environment. No real GPU or model service was measured."
                if report.green
                else "Boot report is red; the fixture-only environment is not ready."
            ),
            "report": report.to_record(),
            "bootstrap_journal": self._state_relative(journal.path),
            "launch_receipt": (
                self._state_relative(launch_receipt) if launch_receipt is not None else None
            ),
            "upload_receipt": (
                self._state_relative(upload_receipt) if upload_receipt is not None else None
            ),
        }
        receipt = self._write_action("boot", payload, descriptor_action="boot")
        if not report.green:
            self.present("Boot report: RED. The environment is not ready.")
            if report.remediation:
                self.present(f"Next step from the report: {report.remediation}")
            raise OperatorError(ErrorCode.BOOT_RED, detail=f"Saved report: {receipt}")
        self.present("Boot report: GREEN for the fixture-only rehearsal.")
        self.present("No real GPU or model service was measured or claimed ready.")
        if launch_receipt is None:
            self.present("No launch record was present; boot checked only the local fixture setup.")
        if upload_receipt is None:
            self.present("No upload record was present; no transfer was assumed complete.")
        self.present(f"Saved report: {receipt}")
        return receipt

    # -- fetch-run ------------------------------------------------------------

    def fetch_run(
        self,
        *,
        run_id: str,
        into: str | Path,
        volume: VolumeSpec | None = None,
        reader: RunObjectReader | None = None,
        run_prefix: str = FETCH_RUN_PREFIX,
        evidence_prefixes: Sequence[str] = (FETCH_EVIDENCE_PREFIX,),
        evidence_keys: Sequence[str] = (),
    ) -> Path:
        """Bring one run tree back from the volume, every object digest-checked.

        Lists every object under ``<run_prefix>/<run_id>/`` through the volume
        S3 seam and fetches each into ``<into>/<run_id>/``, then checks it the
        way the tree checks itself: a blob must hash to its own name, a receipt
        to its own name, an artifact to the digest its stage manifest recorded,
        ``run.json`` to its own self-hash, and every stage manifest must equal
        the manifest the local copy rebuilds from the artifacts that arrived.
        An object nobody accounts for -- a key outside the tree's own inventory
        scope -- is a refusal by name; a publication temporary is skipped and
        its name recorded. A local file that already exists is compared, never
        replaced: identical bytes are reused, different bytes refuse by name.

        **One class of object in the tree cannot be verified, and says so.** A
        stage that served a chair leaves the engine's launch log under
        ``<stage>/serving-logs/``. No manifest records it and nothing ever
        digested it, so it is fetched as side evidence: digested on arrival,
        listed in the receipt under ``unverified_serving_logs``, and never
        counted among what was checked. Refusing the whole tree for it -- which
        is what happened while the inventory scope did not name the path --
        brought home nothing at all from a run that had already billed a card.

        It is also the one file here that is still being written. A chair
        serving right now appends to its log, so an operator who fetches a held
        run mid-flight and again at the end meets bytes that have grown, and a
        debug-level log can outgrow ``MAX_FETCH_OBJECT_BYTES``. Neither is a
        reason to lose the run: a log that will not come home is refused **by
        itself**, named in ``refused_serving_logs`` and on the screen, and the
        verified tree still arrives. Every other object in the tree is immutable
        evidence, and a changed one still refuses the fetch as a whole.

        **The run tree is not the whole record, so the evidence comes too.** A
        launch's PREFLIGHT tree (``preflight/``) is written outside
        ``runs/<run_id>/`` and says which chairs were preflighted, against which
        catalogue digests, at what measured tier -- provenance that has to
        travel with the record (GOVERNANCE 6) off a volume the retention policy
        will destroy. Everything under ``evidence_prefixes`` is fetched into
        ``<into>/evidence/``, each object recorded in the receipt with its
        digest and the content-addressed ones checked against their own names.
        Six records lie under neither prefix and are named rather than
        fetched. Five carry this launch's token in names an operator chose --
        the bootstrap report, the pod-run report, that report's ``-hold``
        liveness sibling, the pod-timer runtime report, and the bootstrap
        journal -- at paths this verb has no way to derive; guessing at them
        would mean listing the whole volume, which holds the submission's own
        page images. The sixth, ``pod-transfer-journal.json``, sits at the
        volume root under a fixed name and is the only durable record of which
        submission rows were verified against target-observed bytes.
        ``evidence_keys`` takes their exact keys from the operator,
        ``operations/pod/README.md`` lists the complete set and how each key is
        derived, and the receipt names that
        derivation limit -- with how many keys this call named, never a blanket
        claim that they went unfetched -- rather than passing over it in silence.

        Zero GPU-hours: this reads storage and needs no pod. Nothing here has
        run against a real endpoint (``volume_s3.py``, note 3).
        """

        try:
            checked_id = validate_run_id(run_id)
        except ContractError as error:
            raise OperatorError(ErrorCode.FETCH_RUN_FAILED, detail=str(error)) from error
        if reader is None:
            if volume is None:
                raise OperatorError(
                    ErrorCode.FETCH_RUN_FAILED,
                    detail="fetch-run needs the network volume the run was written to "
                    "(--network-volume DATACENTER:VOLUME_ID); there is no local stand-in",
                )
            self.present(f"Fetch-run will read {volume.describe()}.")
            try:
                reader = S3VolumeObjectReader(volume)
            except Exception as error:
                self._record_failure("fetch-run", "volume-unavailable", str(error))
                # Not UPLOAD_VOLUME_UNAVAILABLE: that code's registered copy
                # ends "then run `verbatus upload` again", which tells an
                # operator who asked to bring a run tree home to send files
                # instead -- and following it brings nothing back. Every other
                # refusal on this verb is FETCH_RUN_FAILED, whose copy names
                # `verbatus fetch-run`; the volume detail the other code
                # carried is kept here in the detail line.
                raise OperatorError(
                    ErrorCode.FETCH_RUN_FAILED,
                    detail=(
                        f"the network volume could not be prepared for reading: {error}. "
                        "Check the volume id, its datacenter, and that both storage-key "
                        "environment variables are set on this computer"
                    ),
                ) from error
        self.present("No pod needs to be running. This step uses zero GPU-hours.")
        destination_root = Path(into).resolve()
        prefix = f"{run_prefix.strip('/')}/{checked_id}/"
        try:
            outcome = _fetch_run_tree(reader, prefix, destination_root, checked_id)
        except (
            FetchRunRefusal,
            VolumeTransferRefusal,
            ContractError,
            OSError,
            RecursionError,
            MemoryError,
        ) as error:
            # `RecursionError` because nesting, not length, defeats the JSON
            # parser (a hostile `manifest.json` a few thousand brackets deep),
            # and `MemoryError` because `RunTree.read_bytes` is otherwise
            # unbounded against a run tree that is itself untrusted input
            # (G13): both must land as `FETCH_RUN_FAILED`, never as an
            # unclassified crash.
            receipt = self._write_action(
                "fetch-run",
                {
                    "summary": "Fetch-run stopped before the whole run tree was verified.",
                    "state": "partial",
                    "run_id": checked_id,
                    "prefix": prefix,
                    "into": str(destination_root),
                    "volume": _volume_record(volume),
                    "detail": str(error),
                    "zero_gpu_hours": True,
                },
                descriptor_action="fetch-run",
            )
            raise OperatorError(
                ErrorCode.FETCH_RUN_FAILED, detail=f"{error} Saved receipt: {receipt}"
            ) from error
        evidence = _fetch_evidence(
            reader,
            tuple(evidence_prefixes),
            tuple(evidence_keys),
            destination_root / EVIDENCE_DIRECTORY,
        )
        partial = bool(outcome.unmanifested_stages)
        # What was actually checked, which is not what arrived: a serving log is
        # counted in `fetched`/`reused` like any other object, and nothing
        # checked it against anything (GOVERNANCE 10 -- claims are made only
        # about what was measured). Every count of "verified" below is this one,
        # and the arrival counts stay beside it rather than standing in for it.
        verified_objects = outcome.fetched + outcome.reused - len(outcome.unverified_serving_logs)
        checked_clause = (
            "every one checked"
            if not outcome.unverified_serving_logs
            else f"{verified_objects} of them checked"
        )
        summary = (
            f"Run {checked_id} was brought back: "
            f"{outcome.fetched} object(s) fetched, {outcome.reused} reused, "
            f"{verified_objects} verified against the run tree's own digests."
        )
        if outcome.unverified_serving_logs:
            summary += (
                f" {len(outcome.unverified_serving_logs)} object(s) in the tree are serving "
                "logs, which no manifest records: they came home as side evidence, digested "
                "but unverified, and are not in that count."
            )
        if outcome.refused_serving_logs:
            summary += (
                f" {len(outcome.refused_serving_logs)} serving log(s) did not come home and "
                "are named in refused_serving_logs; the verified run tree did."
            )
        if partial:
            summary += (
                f" {', '.join(outcome.unmanifested_stages)} reached no manifest.json -- its "
                f"artifact(s) were verified only by their own envelope, never by a stored "
                "manifest; this run tree is verified-partial, not verified."
            )
        receipt = self._write_action(
            "fetch-run",
            {
                "summary": summary,
                "state": "verified-partial" if partial else "verified",
                "run_id": checked_id,
                "prefix": prefix,
                "into": str(destination_root),
                # Which volume the tree came from: the one identifier needed
                # to go back for a missing object, and the only way to tell
                # two volumes' runs of the same id apart.
                "volume": _volume_record(volume),
                "fetched": outcome.fetched,
                "reused": outcome.reused,
                # `fetched + reused` minus the serving logs: the objects this
                # call actually checked against a digest the run tree recorded.
                "verified_objects": verified_objects,
                "bytes": outcome.bytes,
                "stages_verified": list(outcome.stages),
                "unmanifested_stages": list(outcome.unmanifested_stages),
                "envelope_only_artifacts": list(outcome.envelope_only_artifacts),
                "unverified_serving_logs": [
                    {"relative_path": relative, "sha256": digest}
                    for relative, digest in outcome.unverified_serving_logs
                ],
                "refused_serving_logs": list(outcome.refused_serving_logs),
                "excluded_publication_temporaries": list(outcome.excluded),
                "evidence": {
                    "into": str(destination_root / EVIDENCE_DIRECTORY),
                    "prefixes": list(evidence_prefixes),
                    "requested_keys": list(evidence_keys),
                    "fetched": evidence.fetched,
                    "reused": evidence.reused,
                    "bytes": evidence.bytes,
                    "objects": [{"key": key, "sha256": digest} for key, digest in evidence.objects],
                    "prefixes_with_nothing_stored": list(evidence.empty_prefixes),
                    "refusals": list(evidence.refusals),
                    "records_only_by_name": (
                        "the launch-bound bootstrap report, the pod-run report, that report's "
                        "'-hold' liveness sibling, the pod-timer runtime report, the bootstrap "
                        "journal, and 'pod-transfer-journal.json' at the volume root: five of "
                        "them carry this launch's token at paths an operator chose, which this "
                        "verb cannot derive and will not guess at by listing the whole volume, "
                        "and the transfer journal lies outside both prefixes, so all six come "
                        "home only when named as --evidence-key. operations/pod/README.md "
                        "lists the complete set and how each key is derived. "
                        + (
                            "No key was named this call, so none of them is in this tree."
                            if not evidence_keys
                            else f"{len(evidence_keys)} key(s) were named this call; "
                            "requested_keys, objects and refusals above say which of those "
                            "arrived. Any such record not named is not in this tree."
                        )
                    ),
                },
                "zero_gpu_hours": True,
            },
            descriptor_action="fetch-run",
        )
        if partial:
            self.present(
                f"Run {checked_id} is at {destination_root / checked_id}: "
                f"{outcome.fetched} object(s) fetched, {outcome.reused} already present and "
                f"identical, {checked_clause} -- but "
                f"{', '.join(outcome.unmanifested_stages)} never reached a manifest.json, so "
                "this run is verified-partial: its artifacts are trusted by envelope alone."
            )
        else:
            self.present(
                f"Run {checked_id} is at {destination_root / checked_id}: "
                f"{outcome.fetched} object(s) fetched, {outcome.reused} already present and "
                f"identical, {checked_clause} against the run tree's own digests."
            )
        if outcome.unverified_serving_logs:
            self.present(
                f"{len(outcome.unverified_serving_logs)} serving log(s) came home as side "
                "evidence, not as verified run-tree objects: no manifest records an engine "
                "log, so each is recorded in the receipt with the digest of the bytes that "
                "arrived and checked against nothing else."
            )
        for refusal in outcome.refused_serving_logs:
            self.present(
                f"A serving log did not come home: {refusal}. A log is the one object here "
                "refused by itself rather than fatally -- no manifest records it and a "
                "serving chair is still appending to it -- so the run tree was brought back "
                "and verified without it. If the local copy is an earlier, shorter fetch of "
                "the same log, fetch into a fresh --into; otherwise read it on the volume."
            )
        if outcome.excluded:
            self.present(
                f"{len(outcome.excluded)} publication temporar{'y' if len(outcome.excluded) == 1 else 'ies'} "
                "on the volume were not fetched; their names are in the receipt."
            )
        self.present(
            f"Launch evidence: {evidence.fetched} object(s) fetched, {evidence.reused} already "
            f"present and identical, into {destination_root / EVIDENCE_DIRECTORY}."
        )
        if evidence.refusals:
            self.present(
                f"{len(evidence.refusals)} evidence object(s) did not come home; their names "
                "and reasons are in the receipt. The run tree above is unaffected."
            )
        if evidence_keys:
            self.present(
                "The launch-bound reports, their '-hold' siblings, the bootstrap journal and "
                "the volume-root transfer journal come home only when named: "
                f"{len(evidence_keys)} key(s) were named this call, and the receipt says which "
                "of them arrived."
            )
        else:
            self.present(
                "The launch-bound reports, their '-hold' siblings, the bootstrap journal and "
                "the volume-root transfer journal lie under neither prefix -- none was named "
                "this call, so none came home; pass --evidence-key for each. "
                "operations/pod/README.md lists the complete set. The receipt says so too."
            )
        self.present(f"Saved receipt: {receipt}")
        return receipt

    # -- run ------------------------------------------------------------------

    def run(
        self,
        *,
        run_id: str,
        scenario: str = "happy",
        fixture: str = DEFAULT_FIXTURE,
        submission_folder: str | Path | None = None,
        submission_manifest: str | Path | None = None,
        data_gate_policy: str | Path | None = None,
        models_config: str | Path | None = None,
        serving_recipes_config: str | Path | None = None,
        witness_context_config: str | Path | None = None,
    ) -> RunOutcome:
        if submission_folder is None:
            if submission_manifest is not None:
                raise OperatorError(
                    ErrorCode.INVALID_COMMAND,
                    detail=("--submission-manifest is meaningful only with --submission-folder"),
                )
            if data_gate_policy is not None:
                raise OperatorError(
                    ErrorCode.INVALID_COMMAND,
                    detail="--data-gate-policy is meaningful only with --submission-folder",
                )
        # The roster's three halves travel together or not at all: the
        # orchestrator seals all of them into `config_digest`, and forwarding
        # one would let the real roster resolve against the fixture catalogue,
        # or be described to the Perlector by the fixture declaration.
        # Resolved here, before the fault drill or any child starts.
        roster_argv = _roster_argv(
            models_config=models_config,
            serving_recipes_config=serving_recipes_config,
            witness_context_config=witness_context_config,
        )

        run_root = self.state_root / "runs"
        ingress_mode = "real" if submission_folder is not None else "synthetic-fixture"
        started_at = utc_stamp(self.now())
        prior_state = self._prior_run_state(run_id)
        if submission_folder is None:
            pages, acts, declared_ok = _declared_work(self.workspace)
            if not declared_ok:
                # Not a naming inconvenience: the orchestrator reads the same
                # fixture from the same place, so a run started here would fail
                # a moment later for a reason the operator never sees. This is
                # the condition `NOT_A_CHECKOUT` was written for, said before
                # anything starts.
                raise OperatorError(
                    ErrorCode.NOT_A_CHECKOUT,
                    detail=(
                        f"the declared fixture could not be read from {self.workspace / 'proof'}; "
                        "a run reads its fixture, stage programs and configuration from the "
                        "checkout it is started in, so nothing was started"
                    ),
                )
            extent = f"Checking {', '.join(pages)}."
        else:
            # The operator must not read or name real material before the Door
            # applies its storage and logging policy.
            extent = (
                "Its extent is recorded by the submitted filename ledger, which the Door "
                "checks against the data-handling policy."
            )

        if prior_state in {"interrupted-recoverable", "started"}:
            # `started` with no later record is a run that never reported an
            # end state -- killed, or its terminal closed -- which is the same
            # resumable position the crash drill records explicitly.
            opening = f"Resuming run {run_id}. {extent}"
        elif prior_state is not None:
            opening = (
                f"Run {run_id} already has saved state {prior_state}; "
                f"checking its recorded work again. {extent}"
            )
        else:
            opening = f"Run started. {extent}"
        self.present(opening)

        if submission_folder is None:
            self.present(f"Working next: {', '.join(acts)}.")
            self.present(
                "This rehearsal uses declared synthetic pages, not an uploaded real submission."
            )
        else:
            self.present("This run sends the recorded real submission to the Door's data gate.")
        command = [
            sys.executable,
            str(self.workspace / "pipeline" / "orchestrator" / "run.py"),
            "--fixture",
            fixture,
            "--scenario",
            scenario,
            "--run-id",
            run_id,
            "--run-root",
            str(run_root),
        ]
        command.extend(
            _real_ingress_argv(
                submission_folder=submission_folder,
                submission_manifest=submission_manifest,
                data_gate_policy=data_gate_policy,
            )
        )
        command.extend(roster_argv)
        # Every receipt this run writes carries the same identity, whatever its
        # end state: which run, where its tree is, what was asked for, which
        # commit and which configuration files (by digest) it ran under, and
        # when it started. A held run's receipt was the one that most needed
        # these and had none of them; a later session diagnosing a run from
        # the state directory alone has nothing else to go on.
        commit, commit_unreadable = _repository_commit_or_reason(self.workspace)
        facts: dict[str, Any] = {
            "ingress": ingress_mode,
            "run_root": self._state_relative(run_root),
            "run_id": run_id,
            "scenario": scenario,
            "fixture": fixture,
            "started_at": started_at,
            "argv": list(command),
            "repository_commit": commit,
            "repository_commit_unreadable": commit_unreadable,
            "configuration": {
                "models_config": _config_binding(models_config),
                "serving_recipes_config": _config_binding(serving_recipes_config),
                "witness_context_config": _config_binding(witness_context_config),
                "submission_manifest": _config_binding(submission_manifest),
                "data_gate_policy": _config_binding(data_gate_policy),
            },
        }
        # Written before the child starts, so a run killed by a signal this
        # process cannot catch (SIGKILL, the pod timer, a closed laptop lid)
        # still leaves a record naming the run and its tree. `status` used to
        # report an empty machine over a half-written run tree.
        self._write_action(
            "run",
            {
                **facts,
                "summary": (
                    f"Run {run_id} started; this receipt records the start only, not an end state."
                ),
                "state": "started",
            },
            descriptor_action="run",
        )
        if self.faults.laptop_crash:
            self.faults.laptop_crash = False
            ingress_label = "real submission" if submission_folder is not None else "fixture"
            observed_work = (
                "The real submission's pages reached the Door."
                if submission_folder is not None
                else "The fixture pages reached the Door."
            )
            self._run_door_stage(
                run_root,
                run_id,
                scenario,
                submission_folder=submission_folder,
                submission_manifest=submission_manifest,
                data_gate_policy=data_gate_policy,
                roster_argv=roster_argv,
            )
            receipt = self._write_action(
                "run",
                {
                    **facts,
                    "summary": (
                        f"Run interrupted after the Door recorded the {ingress_label}'s page "
                        "evidence; it can resume."
                    ),
                    "state": "interrupted-recoverable",
                    "last_observed_work": observed_work,
                },
                descriptor_action="run",
            )
            self.present(
                f"The laptop-crash drill interrupted after the {ingress_label} reached the Door."
            )
            self._present_review_command(run_root, run_id)
            raise OperatorError(ErrorCode.RUN_INTERRUPTED, detail=f"Saved run receipt: {receipt}")
        try:
            completed = self._run_orchestrator(command)
        except KeyboardInterrupt:
            # Ctrl+C, or SIGTERM turned into the same thing by
            # `_run_orchestrator`. The child is gone; what it sealed is in the
            # tree, and the receipt says so instead of the run vanishing from
            # every screen.
            receipt = self._write_action(
                "run",
                {
                    **facts,
                    "summary": (
                        f"Run {run_id} was interrupted before the orchestrator reported an "
                        "end state; it can resume."
                    ),
                    "state": "interrupted-recoverable",
                    "interrupted_at": utc_stamp(self.now()),
                    "last_observed_work": (
                        "The orchestrator was interrupted by a signal before it reported an "
                        "end state; every stage it sealed remains in the run tree."
                    ),
                },
                descriptor_action="run",
            )
            self.present(f"Run {run_id} was interrupted before it reported an end state.")
            self._present_review_command(run_root, run_id)
            raise OperatorError(
                ErrorCode.RUN_INTERRUPTED, detail=f"Saved run receipt: {receipt}"
            ) from None
        ended: dict[str, Any] = {
            **facts,
            "ended_at": utc_stamp(self.now()),
            "exit_code": completed.returncode,
            "stderr_tail": bounded_tail(completed.stderr),
            "stdout_tail": bounded_tail(completed.stdout),
        }
        # The stages speak on stderr -- the Door's refusal count, a hold's
        # reason -- and only the crash drill used to show it. Shown here for
        # every exit, the way `_run_door_stage` already argues it must be.
        for line in completed.stderr.rstrip().splitlines():
            self.present(line)
        if completed.returncode not in {0, 3}:
            output = completed.stderr or completed.stdout
            reason = _last_line(output) or (
                f"the orchestrator exited {completed.returncode} without any output"
            )
            receipt = self._write_action(
                "run",
                {
                    **ended,
                    "summary": "Run ended before its Armarium record was available.",
                    "state": "failed",
                    "reason": reason,
                    "detail": bounded_tail(output),
                },
                descriptor_action="run",
            )
            self.present(f"Run {run_id} failed: {reason}")
            self._present_review_command(run_root, run_id)
            # The reason travels on the screen as well as in the receipt: a
            # path where the cause should be sends the operator to `status`,
            # which then has to show it -- and did not.
            raise OperatorError(
                ErrorCode.RUN_FAILED, detail=f"{reason} Saved run receipt: {receipt}"
            )
        try:
            export_payload = self._armarium_export(run_root, run_id)
            aggregate = export_payload["aggregate"]
            state = str(aggregate["status"])
            page_records = export_payload.get("pages", [])
            # A list, or the count is refused: `pages` arrives from an artifact
            # on disk. Keep this validation inside the read/validation block so
            # a malformed record cannot publish the happy-path receipt first.
            if not isinstance(page_records, list):
                raise ValueError("the Armarium export's page record is not a list")
        except Exception as error:
            if completed.returncode == 3:
                # Held before the Armarium -- the Attestatores' hold, or a
                # staged-mode boundary -- so no export record exists to carry
                # the reason; the orchestrator named it on stderr and that is
                # what the receipt keeps. This was filed as a failed run with
                # no run id and the reason discarded.
                reason = (
                    _last_line(completed.stderr)
                    or _last_line(completed.stdout)
                    or "the orchestrator reported a hold, and no Armarium export record "
                    "exists to name the reason"
                )
                receipt = self._write_action(
                    "run",
                    {
                        **ended,
                        "summary": (
                            f"Run {run_id} is held before the Armarium; the hold is recorded "
                            "in the run tree and no export record exists yet."
                        ),
                        "state": "held",
                        "reason": reason,
                        "reasons": [reason],
                        "armarium_export": None,
                        "armarium_export_unreadable": str(error),
                    },
                    descriptor_action="run",
                )
                self.present("Run is held. It was not called complete.")
                self.present(f"Hold reason: {reason}")
                self._present_review_command(run_root, run_id)
                self._notify(
                    "decision", f"Verbatus run {run_id} is held and needs a decision: {reason}"
                )
                raise OperatorError(
                    ErrorCode.RUN_HELD, detail=f"{reason} Saved run receipt: {receipt}"
                ) from error
            reason = f"the Armarium export record could not be read: {error}"
            receipt = self._write_action(
                "run",
                {
                    **ended,
                    "summary": "Run ended before its Armarium record was available.",
                    "state": "armarium-record-unreadable",
                    "reason": reason,
                    "detail": reason,
                    "armarium_export_unreadable": str(error),
                },
                descriptor_action="run",
            )
            self._present_review_command(run_root, run_id)
            raise OperatorError(
                ErrorCode.RUN_FAILED, detail=f"{reason} Saved run receipt: {receipt}"
            ) from error
        # `reasons` is external data: only a list may feed decision output, or a
        # string would become one hold reason per character and a mapping its keys.
        reasons = aggregate.get("reasons")
        if isinstance(reasons, list):
            malformed: str | None = None
            notification_reasons = reasons
        else:
            malformed = (
                "the Armarium record's hold reasons were not a list and were not read; "
                "the run is still held"
            )
            notification_reasons = [malformed]
            reasons = []
        expected = export_payload.get("expected_acts")
        receipt = self._write_action(
            "run",
            {
                **ended,
                "summary": f"Run finished with recorded state: {state}.",
                "state": state,
                "armarium_export": self._state_relative(
                    Path(_armarium_reference(run_root, run_id))
                ),
                # The aggregate's own reasons, kept where `status` can show
                # them before any export exists.
                "reasons": [
                    reason if isinstance(reason, str) else str(reason) for reason in reasons
                ],
                "reasons_unreadable": malformed,
                "expected_acts": expected if isinstance(expected, int) else None,
                "pages_accounted_for": len(page_records),
            },
            descriptor_action="run",
        )
        expected_on_screen = f"{expected} total" if expected is not None else "total not recorded"
        expected_in_notice = (
            f"{expected} act(s) accounted for" if expected is not None else "act total not recorded"
        )
        if submission_folder is None:
            pages, acts, _declared_ok = _declared_work(self.workspace)
            self.present(
                f"Pages accounted for: {', '.join(pages)} ({len(page_records)} total). "
                f"Acts accounted for: {', '.join(acts)} ({expected_on_screen})."
            )
        else:
            # The data-handling policy permits counts here, not real names.
            self.present(
                f"Pages accounted for: {len(page_records)} total. "
                f"Acts accounted for: {expected_on_screen}."
            )
        if state == "complete":
            self.present("Run complete. Its pages and acts reached the Armarium record.")
            self.present(f"Saved run receipt: {receipt}")
            self._present_review_command(run_root, run_id)
            self._notify(
                "milestone",
                f"Verbatus run {run_id} finished: {len(page_records)} page(s), "
                f"{expected_in_notice}.",
            )
            return RunOutcome(state, run_root, run_id, aggregate, export_payload)
        self.present("Run is held. It was not called complete.")
        if malformed is not None:
            self.present(f"Hold reason: UNREADABLE. {malformed}")
        for reason in reasons:
            self.present(f"Hold reason: {reason}")
        self._present_review_command(run_root, run_id)
        # A hold is the pipeline asking a person to decide: the `decision`
        # moment, sent when the hold happens rather than when someone looks.
        # `notification_reasons` carries the UNREADABLE marker even when the
        # display loop above was given an empty list, so the phone hears the
        # same truth the console showed.
        self._notify(
            "decision",
            f"Verbatus run {run_id} is held and needs a decision: "
            f"{'; '.join(str(reason) for reason in notification_reasons) or 'no reason recorded'}",
        )
        raise OperatorError(
            ErrorCode.RUN_HELD,
            detail=(
                f"{len(notification_reasons)} hold reason(s) are recorded above and in the "
                f"receipt. Saved run receipt: {receipt}"
            ),
        )

    def _run_orchestrator(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        """Run the orchestrator child, with SIGTERM meaning what SIGINT already means.

        `subprocess.run` kills its child and re-raises on `KeyboardInterrupt`,
        so a Ctrl+C reaches `run` as an interruption it can record. A SIGTERM
        -- the pod timer, a closed session, `kill` -- ended this process with
        no output and no receipt at all, leaving a half-written run tree that
        `status` reported as an empty machine. For the child's lifetime it is
        turned into the same interruption. SIGKILL cannot be caught by anyone;
        the receipt `run` writes before this starts covers that case.
        """

        def interrupted(signum: int, frame: object) -> None:
            del signum, frame
            raise KeyboardInterrupt

        installed = False
        previous: Any = None
        try:
            previous = signal.signal(signal.SIGTERM, interrupted)
            installed = True
        except ValueError:
            # Not the main thread: a handler cannot be installed here, and the
            # receipt written before the child started still names the run.
            pass
        try:
            return self.runner(
                command,
                cwd=self.workspace,
                capture_output=True,
                text=True,
                check=False,
                env=_stage_environment(),
            )
        finally:
            if installed:
                # `signal.signal` reports `None` for a handler not installed
                # from Python; restoring that spelling is a TypeError, and the
                # default disposition is what such a process had.
                signal.signal(signal.SIGTERM, signal.SIG_DFL if previous is None else previous)

    def _present_review_command(self, run_root: Path, run_id: str) -> None:
        """The one thing `review`, `backup` and `advance` all need and no verb printed."""

        self.present(f"Review it read-only with: {review_command(run_root, run_id)}")

    # -- export ---------------------------------------------------------------

    def export(self, *, run_id: str | None = None) -> Path:
        """Make a local evidence bundle from the base-tree Armarium artifact.

        The run is named first, before any work: with no `run_id` the most
        recently recorded run is chosen and said so, rather than discovered
        from a bundle filename after the fact. With one, the latest receipt for
        that run is used even when another run was recorded since.
        """

        run_records = self._run_receipts()
        if not run_records:
            raise OperatorError(ErrorCode.EXPORT_MISSING)
        if run_id is None:
            latest = run_records[-1][1].get("run_id")
            if not isinstance(latest, str):
                raise OperatorError(ErrorCode.EXPORT_MISSING)
            recorded_id = latest
            self.present(
                f"No run was named, so this exports run {recorded_id}, the run recorded most "
                "recently in this operator state."
            )
        else:
            recorded_id = run_id
        matching = [payload for _, payload in run_records if payload.get("run_id") == recorded_id]
        if not matching:
            raise OperatorError(
                ErrorCode.EXPORT_MISSING,
                detail=f"run {recorded_id} is not a run recorded in this operator state",
            )
        run_record = matching[-1]
        try:
            run_root = self._state_path(str(run_record["run_root"]))
            self.present(f"Exporting run {recorded_id} from run root {run_root}.")
            export_payload = self._armarium_export(run_root, recorded_id)
        except Exception as error:
            raise OperatorError(ErrorCode.EXPORT_MISSING, detail=str(error)) from error
        exports_dir = self.state_root / "exports"
        staged = exports_dir / (f".{recorded_id}-armarium-base-{secrets.token_hex(16)}.staged")
        try:
            exports_dir.mkdir(parents=True, exist_ok=True)
            self._write_base_armarium_bundle(run_root, recorded_id, staged)
            digest = sha256_file(staged)
            # Named by content, not only by run id: an earlier export's receipt
            # is immutable and names both this path pattern and its own digest,
            # so a second export of the same run — after the run tree changed —
            # must land beside the first rather than overwrite bytes an earlier
            # receipt still vouches for (GOVERNANCE 4's argument, applied to the
            # bundle this stage itself produces).
            destination = exports_dir / f"{recorded_id}-armarium-base-{digest}.zip"
            try:
                os.link(staged, destination, follow_symlinks=False)
            except FileExistsError:
                if _sha256_regular_file_nofollow(destination) != digest:
                    raise OSError(
                        "the existing content-addressed export does not contain "
                        "the bytes its name claims"
                    ) from None
            staged.unlink()
        except OperatorError as error:
            # `_write_base_armarium_bundle` refuses an incomplete or unsafe run
            # tree with an already-shaped OperatorError. Without this arm it
            # travelled past the handler below: no receipt was written and the
            # staged file was left behind, so `verbatus status` after a failed
            # export showed no export at all and the only account of it was one
            # terminal line.
            staged.unlink(missing_ok=True)
            self._record_failure(
                "export",
                "local-copy-failed",
                str(error.detail or error),
                facts={"run_id": recorded_id, "run_root": self._state_relative(run_root)},
            )
            raise
        except (OSError, zipfile.BadZipFile) as error:
            staged.unlink(missing_ok=True)
            self._record_failure(
                "export",
                "local-copy-failed",
                str(error),
                facts={"run_id": recorded_id, "run_root": self._state_relative(run_root)},
            )
            raise OperatorError(ErrorCode.EXPORT_FAILED, detail=str(error)) from error
        table = reconciliation_table(export_payload)
        for line in table:
            self.present(line)
        # The receipt's state, the exit status and the notice all follow the
        # aggregate's own status. A receipt hardcoded to `complete` over a
        # partial run, exiting 0, is the partial result presented as complete
        # that GOVERNANCE 2 forbids -- the screen was honest, the exit status
        # and the record were not.
        aggregate = export_payload.get("aggregate")
        recorded_status = aggregate.get("status") if isinstance(aggregate, dict) else None
        state = recorded_status if isinstance(recorded_status, str) else "unknown"
        complete = state == "complete"
        reasons = aggregate.get("reasons") if isinstance(aggregate, dict) else None
        receipt = self._write_action(
            "export",
            {
                "summary": (
                    "Base Armarium evidence bundle copied locally."
                    if complete
                    else "Base Armarium evidence bundle copied locally for a run whose "
                    f"recorded state is {state}: a partial result, not a finished one."
                ),
                "state": state,
                "run_id": recorded_id,
                "run_root": self._state_relative(run_root),
                "bundle": self._state_relative(destination),
                "sha256": digest,
                "reconciliation": table,
                "reasons": (
                    [reason if isinstance(reason, str) else str(reason) for reason in reasons]
                    if isinstance(reasons, list)
                    else None
                ),
                "assumption": "Spec 11 is not in this tree; this is a copy of the base Armarium evidence, not a Spec 11 product bundle.",
            },
            descriptor_action="export",
        )
        self.present(f"Local Armarium evidence bundle: {destination}")
        self.present("This is base Armarium evidence, not a Spec 11 product bundle.")
        self.present(f"Saved export receipt: {receipt}")
        if complete:
            self._notify(
                "milestone", f"Verbatus export for run {recorded_id} landed: {destination}"
            )
            return destination
        self.present(
            f"This export is PARTIAL: the run's recorded state is {state}, and the bundle holds "
            "only what was delivered."
        )
        self._notify(
            "milestone",
            f"Verbatus export for run {recorded_id} landed as {state}, not complete: {destination}",
        )
        raise OperatorError(
            ErrorCode.EXPORT_PARTIAL,
            detail=(
                f"recorded run state {state}; the reconciliation above lists every recorded "
                f"reason. Saved export receipt: {receipt}"
            ),
        )

    # -- close ---------------------------------------------------------------

    def prepare_close(self, *, pod_id: str | None = None) -> PreparedClose:
        """Resolve the recorded pod and show the close notice — before any confirmation.

        Mirrors prepare_launch/launch: the notice a person reads has to be shown
        before they are asked to type anything back, never after.
        """

        launch_receipt = self._active_launch_receipt()
        if launch_receipt is None:
            raise OperatorError(ErrorCode.CLOSE_NOTHING)
        launch = self._read_receipt(launch_receipt)["payload"]
        pod_raw = launch.get("pod")
        if not isinstance(pod_raw, dict):
            raise OperatorError(ErrorCode.CLOSE_NOTHING)
        record = _pod_from_record(pod_raw)
        if pod_id is not None and pod_id != record.pod_id:
            raise OperatorError(
                ErrorCode.CLOSE_NOTHING,
                detail="the requested pod is not the pod recorded for this operator session",
            )
        close_receipt = self._descriptor_receipt("close")
        if close_receipt is not None:
            prior = self._read_receipt(close_receipt)["payload"]
            if prior.get("pod_id") == record.pod_id:
                prior_report = prior.get("close_report")
                if (
                    isinstance(prior_report, dict)
                    and prior_report.get("state") == "verified"
                    and prior.get("lease_reconciled") is True
                ):
                    raise OperatorError(
                        ErrorCode.CLOSE_NOTHING,
                        detail="a verified close result is already recorded for this pod",
                    )
                self.present(
                    "A prior close result was unverified. This new check needs its own confirmation."
                )
        try:
            lease_store, lease = self._lease_for_close(launch, record)
        except OperatorError:
            # The lease error carries the pod's billing note; the volume's
            # ongoing price is this surface's own to say, and volume_cost.py
            # requires it "on every close, verified or not".
            self._show_volume_cost(
                volume_id=record.volume_id, hourly_usd=str(record.estimate.volume_hourly_usd)
            )
            raise
        if lease.phase == "closed-verified":
            raise OperatorError(
                ErrorCode.CLOSE_NOTHING,
                detail="the recorded safety lease already has a verified close result",
            )
        phrase = f"{OPERATOR_CLOSE_PREFIX} {record.pod_id}"
        self.present(f"Close will remove fixture pod {record.pod_id}.")
        self.present("The attached volume is retained and keeps its own ongoing price.")
        self.present(f"Type exactly {phrase!r} to continue.")
        return PreparedClose(launch, record, lease_store, lease, phrase)

    def close(self, prepared: PreparedClose, confirmation: str | None) -> CloseReport:
        """Confirm a prepared close, then record it before the fake provider sees it."""

        launch, record, lease_store, lease = (
            prepared.launch,
            prepared.record,
            prepared.lease_store,
            prepared.lease,
        )
        if confirmation != prepared.phrase:
            raise OperatorError(ErrorCode.CLOSE_REFUSED)
        confirmation_receipt = self._write_action(
            "close-confirmation",
            {
                "summary": "Manual close confirmation recorded before the provider call.",
                "pod_id": record.pod_id,
                "confirmation": prepared.phrase,
            },
            descriptor_action="close-confirmation",
            failure_code=ErrorCode.CONFIRMATION_RECORD_FAILED,
        )
        provider = self._provider_for_record(launch, record)
        if self.faults.failed_close:
            self.faults.failed_close = False
            provider.inject_failure(
                "terminate", ProviderFailure("injected close failure"), times=100
            )
        else:
            provider.clear_failures("terminate")
            provider.bill(record.pod_id, Decimal("0.82"), description="fixture pod runtime")
        policy, policy_error = self._close_policy()
        if policy_error is not None:
            self.present(
                "Your reviewed spend policy could not be read; close is using its "
                "built-in operational deadline instead."
            )
        report = self._shutdown(policy).close(record, reason="manual operator close")
        try:
            lease_store.record_close(
                owner_token=lease.owner_token,
                close_record=report.to_record(),
                verified=report.verified,
                now=self.now(),
            )
        except Exception as error:
            receipt = self._write_action(
                "close",
                {
                    "summary": "Close is UNVERIFIED because the safety lease could not record the provider evidence.",
                    "pod_id": record.pod_id,
                    "confirmation_receipt": self._state_relative(confirmation_receipt),
                    "close_report": report.to_record(),
                    "lease_reconciled": False,
                    "lease": self._state_relative(lease_store.path),
                    "lease_record_error": str(error),
                    "spend_policy_error": policy_error,
                },
                descriptor_action="close",
            )
            self.present(
                "UNVERIFIED CLOSE: provider evidence could not be joined to the safety lease."
            )
            self.present(
                "Manual check: Review the saved close receipt and the safety lease before any retry."
            )
            self._present_captured_cost(report)
            self._show_volume_cost(
                volume_id=report.volume_id, hourly_usd=str(report.volume_ongoing_hourly_usd)
            )
            self.present("Saved close receipt: " + str(receipt))
            raise OperatorError(
                ErrorCode.CLOSE_LEASE_RECORD_FAILED,
                detail="Saved close receipt: " + str(receipt),
            ) from error
        receipt = self._write_action(
            "close",
            {
                "summary": (
                    "Close is verified by fixture absence and fixture billing evidence."
                    if report.verified
                    else "Close is UNVERIFIED; manual reconciliation is required."
                ),
                "pod_id": record.pod_id,
                "confirmation_receipt": self._state_relative(confirmation_receipt),
                "close_report": report.to_record(),
                "lease_reconciled": True,
                "lease": self._state_relative(lease_store.path),
                "spend_policy_error": policy_error,
            },
            descriptor_action="close",
        )
        self._show_close(report, receipt)
        if not report.verified:
            raise OperatorError(
                ErrorCode.CLOSE_UNVERIFIED, detail=f"Saved close receipt: {receipt}"
            )
        return report

    # -- status ---------------------------------------------------------------

    def status(self) -> list[str]:
        """Read descriptors, receipts, and leases without writes or provider calls."""

        try:
            descriptor = self.descriptor.load()
        except RecordError as error:
            raise OperatorError(ErrorCode.STATUS_UNREADABLE, detail=str(error)) from error
        # A lease is operator evidence even when no receipt was written; unreadable
        # lease evidence likewise prevents an honest claim that the state is empty.
        open_leases, lease_unreadable = self._open_leases()
        if (
            (descriptor is None or not descriptor["actions"])
            and not open_leases
            and not lease_unreadable
        ):
            raise OperatorError(ErrorCode.STATUS_EMPTY)
        lines = ["Saved operator records (read-only; no new provider check was made):"]
        unreadable: list[str] = list(lease_unreadable)
        for path, lease in open_leases:
            lines.append(
                f"- open safety lease {path.name}: {lease.phase}"
                + ("" if lease.pod_id is None else f" for pod {lease.pod_id}")
                + f"; hard deadline {utc_stamp(lease.hard_deadline)}."
            )
            lines.append(
                "  No verified close is recorded for it. A pod may still be billing; "
                "the lease-backed safety controllers own it until that deadline."
            )
            lines.extend(self._supervisor_status_lines(lease))
        for reason in lease_unreadable:
            lines.append(f"- safety lease: UNREADABLE; it was not treated as closed. {reason}")
        if descriptor is not None and descriptor["actions"]:
            for action, path_texts in sorted(descriptor["history"].items()):
                if action == "active-launch":
                    continue
                # Read every record of the action first: a run's `started`
                # receipt is rendered differently once a later receipt of the
                # same run recorded its end, and that later receipt is further
                # down the same list.
                loaded: list[tuple[int, Path | None, dict[str, Any] | None, str | None]] = []
                for number, path_text in enumerate(path_texts, start=1):
                    try:
                        receipt_path = self.descriptor.receipt_path(path_text)
                        loaded.append(
                            (
                                number,
                                receipt_path,
                                self.receipts.read(receipt_path)["payload"],
                                None,
                            )
                        )
                    except RecordError as error:
                        loaded.append((number, None, None, str(error)))
                for number, receipt_path, payload, failure in loaded:
                    if payload is None or receipt_path is None:
                        label = f"{action} record {number}"
                        unreadable.append(f"{label}: {failure}")
                        lines.append(f"- {label}: UNREADABLE; it was not treated as success.")
                        continue
                    summary = payload.get("summary")
                    lines.append(
                        f"- {action} record {number}: "
                        + (summary if isinstance(summary, str) else "saved record")
                    )
                    ended_later = action == "run" and any(
                        later_payload is not None
                        and later_payload.get("run_id") == payload.get("run_id")
                        and later_payload.get("state") != "started"
                        for later_number, _, later_payload, _ in loaded
                        if later_number > number
                    )
                    try:
                        lines.extend(
                            _status_projection(
                                action,
                                payload,
                                state_root=self.state_root,
                                ended_later=ended_later,
                            )
                        )
                    except RecordError as error:
                        label = f"{action} record {number}"
                        unreadable.append(f"{label}: {error}")
                        lines.append(f"- {label}: UNREADABLE; it was not treated as success.")
                        continue
                    # The file every failure message tells the operator to
                    # preserve, named where they are told to look for it.
                    lines.append(f"  Saved receipt: {receipt_path}")
        for line in lines:
            self.present(line)
        if unreadable:
            raise OperatorError(
                ErrorCode.STATUS_UNREADABLE,
                detail="; ".join(unreadable),
            )
        return lines

    # -- internal -------------------------------------------------------------

    def _runtime(self, policy: SpendPolicy) -> PodRuntime:
        return PodRuntime(
            self.provider,
            provider_name="offline-fixture",
            spend_policy=policy,
            lease_root=self.state_root / "leases",
            shutdown=self._shutdown(policy),
            now=self.now,
            controller_armer=FixtureControllerArmer(self.now),
            notifier=self._notify_spend,
        )

    def _close_policy(self) -> tuple[SpendPolicy | None, str | None]:
        """The reviewed policy for close timing, and why it was unavailable if so.

        Always the workspace's own `config/spend.toml`, never the `--spend` path
        `launch` may have been given: nothing records which policy path a launch
        used, so close has no path to read back even if it wanted one. `launch`
        must refuse without a reviewed policy, because it spends. `close` must
        not: an unreadable or unconfigured policy is no reason to leave a pod
        running, and the ceiling checks a policy carries have nothing to say
        about stopping one. The second element is the reason close is falling
        back to `VerifiedShutdown`'s own operational defaults, or `None` when
        the workspace's policy was read without incident — the caller says so
        rather than silently substituting a shorter deadline for the reviewed
        one.
        """

        try:
            return load_spend_policy(self.workspace / "config" / "spend.toml"), None
        except Exception as error:
            return None, str(error)

    def _shutdown(self, policy: SpendPolicy | None) -> VerifiedShutdown:
        """Close timing comes from the reviewed policy, never from a constant here.

        A reviewed `shutdown_deadline_seconds` and `shutdown_poll_interval_seconds`
        are what the operator's own policy says a close may take — read from
        `_close_policy`'s fixed workspace default, which may not be the policy a
        `launch --spend` used (see that method's docstring). Where no configured
        policy is in hand — `close` is deliberately runnable without one, because
        closing is always the safe direction — `VerifiedShutdown`'s own
        operational defaults apply. Speeding either up belongs in a test's
        injected clock, not in the shipped surface: a close that gives up in
        milliseconds reports UNVERIFIED every time, which is loud, wrong, and the
        fastest way to teach someone to ignore the one message that matters.
        """

        timings: dict[str, float | int] = {
            "billing_cutoff_margin_seconds": FIXTURE_BILLING_CUTOFF_MARGIN_SECONDS
        }
        if policy is not None and policy.configured:
            margin = policy.billing_cutoff_margin_seconds
            if isinstance(self.provider, OperatorFakeProvider):
                # **The fixture's stamp sets this floor, not the policy.**
                # `bill()` puts its cutoff an hour ahead of its own clock, so any
                # margin short of that makes a perfectly healthy close fail its
                # billing-evidence check. The no-policy branch above already
                # carried the floor; the configured branch took the reviewed
                # number raw, and the whole accepted range is 0-3600 -- so every
                # value but exactly 3600 turned a good close red. Measured at
                # 1800, 600 and 0, all three raised
                # "Close could not verify both pod absence and billing evidence."
                #
                # Dormant only because the shipped `config/spend.toml` is
                # `unconfigured`: the first time Tyrel fills it in with anything
                # but 3600, his first close rehearsal is red for no real reason.
                # `_shutdown`'s own docstring calls that "the fastest way to
                # teach someone to ignore the one message that matters".
                #
                # Gated on the fixture provider rather than applied flat, so the
                # floor disappears of its own accord when a real provider with a
                # real cutoff arrives and the reviewed policy becomes the honest
                # number. Today this surface is fixture-only by type, so the
                # branch is always taken.
                margin = max(margin, FIXTURE_BILLING_CUTOFF_MARGIN_SECONDS)
            timings = {
                "timeout_seconds": policy.shutdown_deadline_seconds,
                "poll_seconds": policy.shutdown_poll_interval_seconds,
                "billing_cutoff_margin_seconds": margin,
            }
        return VerifiedShutdown(
            self.provider,
            monotonic=self.monotonic,
            sleeper=self.sleeper,
            now=self.now,
            **timings,
        )

    def _show_paid_preview(self, prepared: PreparedLaunch) -> None:
        preview = prepared.result.preview
        if preview is None:
            self.present("Launch could not obtain a price preview.")
            return
        assessment = preview.assessment
        self.present("Paid-action preview (fixture prices only):")
        self.present(
            "- Reviewed request: "
            f"{prepared.request.name}; GPU {prepared.request.gpu_type}; "
            f"volume {prepared.request.volume_id}; hard deadline "
            f"{utc_stamp(prepared.request.hard_deadline)}"
        )
        self.present(f"- Pod hourly price: ${assessment.estimate.pod_hourly_usd}")
        self.present(f"- Attached-volume hourly price: ${assessment.estimate.volume_hourly_usd}")
        self.present(
            "- Combined estimated cost through the hard lifetime: "
            f"${_display_usd(assessment.estimated_total_cost_usd)}"
        )
        if assessment.policy.configured:
            self.present(f"- Hourly ceiling: ${assessment.policy.max_hourly_usd}")
            self.present(
                f"- Lifetime cost ceiling: ${assessment.policy.max_estimated_metered_cost_usd}"
            )
            self.present(
                "- Hard lifetime ceiling: "
                + _human_duration(assessment.policy.hard_lifetime_seconds)
            )
            self.present(
                f"- Account-balance hard floor: ${assessment.policy.account_balance_floor_usd}"
            )
            self.present(
                "- Account-balance warning threshold: "
                f"${assessment.policy.account_balance_alert_usd}"
            )
            observation = assessment.balance_observation
            if observation is None:
                self.present("- Observed account balance: unavailable")
            else:
                self.present(
                    "- Observed account balance: "
                    f"${observation.available_usd} at {observation.observed_at.isoformat()} "
                    f"from {observation.source}"
                )
            self.present(f"- Other reserved liability: ${assessment.reserved_liability_usd}")
        else:
            self.present("- Spending ceiling: not configured")
        for alert in assessment.alerts:
            self.present(f"- Warning: {alert}")
        for notification in assessment.alert_notifications:
            self.present(notification)
        if assessment.reasons:
            for reason in assessment.reasons:
                self.present(f"- Check: {reason}")
        if assessment.allowed:
            self.present(
                f"- Reviewed request, price, and ceilings digest: {prepared.review_digest}"
            )
            self.present(
                f"Type exactly {prepared.confirmation_phrase!r} to continue with this paid action."
            )

    def _launch_error(self, result: LaunchResult, *, receipt: Path | None = None) -> OperatorError:
        detail = result.detail if receipt is None else f"{result.detail} Saved receipt: {receipt}"
        if result.state is LaunchState.REFUSED_CONFIRMATION:
            # This shared state needs the gate-owned marker to distinguish a moved
            # price from a mistyped confirmation without duplicating refusal text.
            if PRICE_MOVE_MARKER in result.detail:
                return OperatorError(ErrorCode.PRICE_CHANGED, detail=detail)
            return OperatorError(ErrorCode.CONFIRMATION_REQUIRED, detail=detail)
        if result.state is LaunchState.REFUSED_CEILING:
            return OperatorError(ErrorCode.PAID_ACTION_REFUSED, detail=detail)
        if (
            result.state
            in {
                LaunchState.REFUSED_BALANCE_FLOOR,
                LaunchState.REFUSED_BALANCE_UNOBSERVABLE,
            }
            and result.record is not None
        ):
            return OperatorError(ErrorCode.LAUNCH_UNRESOLVED, detail=detail)
        if result.state is LaunchState.REFUSED_BALANCE_FLOOR:
            return OperatorError(ErrorCode.BALANCE_FLOOR_REACHED, detail=detail)
        if result.state is LaunchState.REFUSED_BALANCE_UNOBSERVABLE:
            return OperatorError(ErrorCode.BALANCE_UNOBSERVABLE, detail=detail)
        if result.state in {
            LaunchState.REFUSED_SHUTDOWN_NOT_READY,
            LaunchState.REFUSED_CONTROLLER_NOT_READY,
            LaunchState.LEASE_FAILURE,
        }:
            return OperatorError(ErrorCode.SAFETY_CHECK_FAILED, detail=detail)
        if result.state in {
            LaunchState.REFUSED_RUNTIME_CONTRACT,
            LaunchState.CREATE_UNLEASED,
            LaunchState.CONTROLLERS_UNARMED,
            # A gate-level lease refusal means the console's earlier lease read raced;
            # it must keep the same unresolved-close instruction.
            LaunchState.REFUSED_ACTIVE_LEASE,
        }:
            return OperatorError(ErrorCode.LAUNCH_UNRESOLVED, detail=detail)
        if result.state is LaunchState.PROVIDER_FAILURE:
            if result.lease_path is not None:
                return OperatorError(ErrorCode.LAUNCH_UNRESOLVED, detail=detail)
            # Only a provider failure with no lease can safely use the provider's
            # wording to choose between retryable codes. State always wins where
            # a pod may exist and may already be billing.
            if "timeout" in detail.lower():
                return OperatorError(ErrorCode.PROVIDER_TIMEOUT, detail=detail)
            return OperatorError(ErrorCode.PROVIDER_ERROR, detail=detail)
        return OperatorError(ErrorCode.SAFETY_CHECK_FAILED, detail=detail)

    def _inject_provider_preview_fault(self) -> None:
        if self.faults.provider_timeout:
            self.faults.provider_timeout = False
            self.provider.inject_failure("estimate", ProviderFailure("injected provider timeout"))
        elif self.faults.provider_error:
            self.faults.provider_error = False
            self.provider.inject_failure("estimate", ProviderFailure("injected provider failure"))

    def _fault_upload_key(self, manifest_path: Path, prefix: str) -> str | None:
        if not self.faults.partial_upload:
            return None
        self.faults.partial_upload = False
        try:
            manifest = submission_door.load_manifest(manifest_path)
            first = manifest["files"][0]["relative_path"]
        except Exception:
            return "__invalid__"
        return f"{prefix}/{first}"

    def _descriptor_receipt(self, *actions: str) -> Path | None:
        """The receipt for the first named action the descriptor actually carries."""

        descriptor = self._load_descriptor()
        if descriptor is None:
            return None
        for action in actions:
            value = descriptor["actions"].get(action)
            if isinstance(value, str):
                try:
                    return self.descriptor.receipt_path(value)
                except RecordError as error:
                    raise OperatorError(ErrorCode.STATUS_UNREADABLE, detail=str(error)) from error
        return None

    def _load_descriptor(self) -> dict[str, Any] | None:
        """The descriptor, or the same named refusal `status` gives for an unreadable one.

        Every verb that reads its own records used to let `RecordError` escape
        to the unclassified handler, whose copy told the operator to photograph
        the screen and ask for help over a record `status` would have named.
        """

        try:
            return self.descriptor.load()
        except RecordError as error:
            raise OperatorError(ErrorCode.STATUS_UNREADABLE, detail=str(error)) from error

    def _read_receipt(self, path: Path) -> dict[str, Any]:
        """One receipt, with an unreadable one named the way `status` names it."""

        try:
            return self.receipts.read(path)
        except RecordError as error:
            raise OperatorError(ErrorCode.STATUS_UNREADABLE, detail=str(error)) from error

    def _run_receipts(self) -> list[tuple[Path, dict[str, Any]]]:
        """Every run receipt this state root indexes, oldest first, each read whole."""

        descriptor = self._load_descriptor()
        if descriptor is None:
            return []
        loaded: list[tuple[Path, dict[str, Any]]] = []
        for entry in descriptor["history"].get("run", []):
            try:
                path = self.descriptor.receipt_path(entry)
            except RecordError as error:
                raise OperatorError(ErrorCode.STATUS_UNREADABLE, detail=str(error)) from error
            loaded.append((path, self._read_receipt(path)["payload"]))
        return loaded

    def _state_relative(self, path: Path) -> str:
        """Record a path under the state root relative to it.

        A receipt that names its run root, its lease or a sibling receipt by
        absolute path is bound to the directory it was written in; after a copy,
        a restore or a move every such reference is stale. Under the state root
        the reference is kept relative and rejoined on read (`_state_path`);
        anything outside it stays absolute, because there is nothing to rejoin
        it to.
        """

        try:
            return path.resolve().relative_to(self.state_root).as_posix()
        except (OSError, ValueError):
            return str(path)

    def _state_path(self, text: str) -> Path:
        """Rejoin a recorded path: state-relative against this root, absolute as it is."""

        candidate = Path(text)
        return candidate if candidate.is_absolute() else self.state_root / candidate

    def record_backup(
        self,
        *,
        state: str,
        facts: dict[str, Any],
        detail: str | None = None,
        report: dict[str, Any] | None = None,
    ) -> Path | None:
        """The backup verb's receipt, so `status` can say which run went where.

        A backup left only a snapshot on its destination and nothing in the
        operator's own records; the sequence "ran, backed up, exported" could
        not be read back without the terminal. A failed attempt is recorded the
        way every other verb's failure is (`_record_failure`: said aloud, never
        raised over the failure it records). A verified snapshot's receipt is
        the verb's own result, and failing to write it is the command's failure.
        """

        if state != "complete":
            self._record_failure("backup", state, detail or "", facts=facts)
            return None
        return self._write_action(
            "backup",
            {
                **facts,
                "summary": (
                    f"Backup of run {facts.get('run_id')} is complete: a verified snapshot is "
                    f"at {facts.get('mac_directory')}."
                ),
                "state": "complete",
                "report": report,
            },
            descriptor_action="backup",
        )

    def _active_launch_receipt(self) -> Path | None:
        return self._descriptor_receipt("active-launch", "launch")

    def _refuse_if_active_pod(self) -> None:
        """Keep every open cost path visible until its own verified close.

        Two readings, because the receipt and the lease become true at different
        moments and a paid action lives in the gap between them.
        """

        self._refuse_if_recorded_active_pod()
        self._refuse_if_open_lease()

    def _refuse_if_recorded_active_pod(self) -> None:
        """Keep a recorded open cost path visible until its own verified close."""

        try:
            launch_receipt = self._active_launch_receipt()
            if launch_receipt is None:
                return
            launch = self._read_receipt(launch_receipt)["payload"]
            pod_raw = launch.get("pod")
            if not isinstance(pod_raw, dict):
                # A plain launch receipt with no pod is a recorded refusal, not an
                # open cost path. An active-launch pointer without one is a broken
                # record, and must not read as "nothing is running".
                if self._descriptor_receipt("active-launch") is None:
                    return
                raise ValueError("active launch has no pod record")
            record = _pod_from_record(pod_raw)
            _, lease = self._lease_for_close(launch, record)
        except Exception as error:
            reason = getattr(error, "detail", None) or str(error)
            raise OperatorError(
                ErrorCode.SAFETY_CHECK_FAILED,
                detail=f"the recorded active fixture pod could not be checked safely: {reason}",
            ) from error
        if lease.phase != "closed-verified":
            raise OperatorError(
                ErrorCode.ACTIVE_POD_REQUIRES_CLOSE,
                detail="recorded fixture pod " + record.pod_id + " has lease state " + lease.phase,
            )

    def _open_leases(self) -> tuple[list[tuple[Path, PodLease]], list[str]]:
        """Every durable lease in this state that has not reached a verified close.

        Unreadable leases are returned as evidence rather than skipped because
        neither caller may infer a verified close from an unreadable record.
        A symlink is one of those: `operations/pod/launch.py` refuses to read a
        lease through one at the paid gate, and the console reader that decides
        whether `status` says "a pod may still be billing" cannot be the lenient
        one — a lease replaced by a link to some closed record would otherwise
        report an open pod as absent.
        """

        root = self.state_root / "leases"
        unreadable: list[str] = []
        try:
            paths = sorted(root.glob("*.json"))
        except OSError as error:
            return [], [f"the lease directory {root} could not be listed: {error}"]
        open_leases: list[tuple[Path, PodLease]] = []
        for path in paths:
            if path.is_symlink():
                unreadable.append(f"lease {path} is a symlink")
                continue
            try:
                lease = _read_published_lease(path)
            except Exception as error:
                unreadable.append(f"lease {path} could not be read: {error}")
                continue
            if lease is None or lease.phase == "closed-verified":
                continue
            open_leases.append((path, lease))
        return open_leases, unreadable

    def _supervisor_status_lines(self, lease: PodLease) -> list[str]:
        """Read-only supervisor telemetry for one open lease -- no new verb.

        Every fact here comes from the durable identity file `supervise.py`
        writes beside the lease and from the lease's own close record; this
        never asks the provider anything, which keeps `status` a pure read.
        """

        leases_root = self.state_root / "leases"
        path = _supervisor_identity_path(leases_root, lease.lease_id)
        try:
            identity = _read_supervisor_identity(path)
        except Exception as error:
            return [f"  supervisor: identity file UNREADABLE ({error})."]
        if identity is None:
            return ["  supervisor: absent -- no identity file has been written for this lease yet."]
        # Built from the token-free projection, never from `identity` itself,
        # so the capability that closes this lease structurally cannot reach
        # a terminal (`ps` is public) through this read path.
        telemetry = identity.telemetry()
        try:
            running = _supervisor_peek_running(leases_root, lease.lease_id)
        except Exception as error:
            # Never let a failure in the lock check swallow the lines below --
            # "a pod may still be billing" must still be printed.
            running = f"UNREADABLE ({error})"
        age = max((self.now() - telemetry["started_at"]).total_seconds(), 0.0)
        if running is True:
            word = "running"
        elif running is False:
            word = "absent (pid " + str(telemetry["pid"]) + " not found)"
        elif running is None:
            word = (
                "UNKNOWN -- the ownership lock could not be checked; "
                "treat this pod as unsupervised and go look"
            )
        else:
            word = str(running)
        lines = [f"  supervisor: {word}, identity file age {age:.0f}s (pid {telemetry['pid']})."]
        if telemetry["last_tick_at"] is not None:
            lines.append(
                f"  last tick: {telemetry['last_tick_state']} at "
                f"{utc_stamp(telemetry['last_tick_at'])} -- {telemetry['last_tick_detail']}"
            )
        else:
            lines.append("  last tick: none recorded yet.")
        if lease.close_record is not None:
            lines.append(
                "  last close record: "
                + json.dumps(lease.close_record, sort_keys=True, separators=(",", ":"))
            )
        else:
            lines.append("  last close record: none; this lease has not closed.")
        lines.append(f"  volume's ongoing hourly price: ${lease.volume_hourly_usd}.")
        return lines

    def _refuse_if_open_lease(self) -> None:
        """Refuse durable paid intent whose provider outcome may be unknown.

        The receipt follows the provider response, but the lease precedes the
        request. An unclosed lease therefore proves only that a paid action was
        armed and lacks verified-close evidence, never whether the pod exists.
        """

        open_leases, unreadable = self._open_leases()
        if unreadable:
            raise OperatorError(
                ErrorCode.SAFETY_CHECK_FAILED,
                detail="; ".join(unreadable),
            )
        if not open_leases:
            return
        described = "; ".join(
            f"{path} is {lease.phase}"
            + ("" if lease.pod_id is None else f" for pod {lease.pod_id}")
            for path, lease in open_leases
        )
        raise OperatorError(
            ErrorCode.LAUNCH_UNRESOLVED,
            detail=(
                "a paid action was armed here and no verified close is recorded for it: "
                f"{described}. The lease-backed safety controllers own that pod until its "
                "hard deadline; do not start another one on top of it."
            ),
        )

    @contextmanager
    def _exclusive_paid_launch(self) -> Iterator[None]:
        """Hold the paid-launch claim across the active check and result record.

        The claim is non-blocking so another window receives a refusal without
        spending its challenge. Process death releases this claim; durable lease
        evidence, not the lock file, carries any unresolved provider action.
        """

        self.state_root.mkdir(parents=True, exist_ok=True)
        path = self.state_root / ".paid-launch.lock"
        try:
            # O_NOFOLLOW like every other evidence reader here: the claim that
            # decides whether two windows may both send a paid create must not
            # be redirectable through a planted link.
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except OSError as error:
            raise OperatorError(
                ErrorCode.SAFETY_CHECK_FAILED,
                detail=f"the paid-launch claim {path} could not be opened: {error}",
            ) from error
        handle = os.fdopen(descriptor, "r+b")
        with handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise OperatorError(
                    ErrorCode.LAUNCH_ALREADY_IN_FLIGHT,
                    detail=(
                        f"another process holds the paid-launch claim {path} ({error}); "
                        "no paid action was sent from this window"
                    ),
                ) from error
            except OSError as error:
                # Only BlockingIOError proves another holder; every other errno means
                # this process failed to establish mutual exclusion at all.
                raise OperatorError(
                    ErrorCode.SAFETY_CHECK_FAILED,
                    detail=(
                        f"the paid-launch claim {path} could not be taken, so this window "
                        f"cannot prove it is the only one launching: {error}; no paid "
                        "action was sent from this window"
                    ),
                ) from error
            try:
                yield
            finally:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    # Closing the handle releases the POSIX lock. An unlock
                    # failure must not replace the paid-launch result this
                    # block was protecting.
                    pass

    def _prior_run_state(self, run_id: str) -> str | None:
        """The state this run's own latest receipt records, whatever ran since.

        The receipts are read by the run id they carry, never chosen by
        timestamp or filename; only the last run's receipt used to be
        consulted, so a run interrupted before another one started was
        greeted as new.
        """

        for _path, payload in reversed(self._run_receipts()):
            if payload.get("run_id") == run_id:
                state = payload.get("state")
                return state if isinstance(state, str) else None
        return None

    def _write_action(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        descriptor_action: str,
        additional_descriptor_actions: tuple[str, ...] = (),
        failure_code: ErrorCode = ErrorCode.RECORD_WRITE_FAILED,
    ) -> Path:
        receipt: Path | None = None
        try:
            receipt = self.receipts.write(kind, payload)
            for action in (descriptor_action, *additional_descriptor_actions):
                self.descriptor.record(action, receipt)
            return receipt
        except RecordError as error:
            detail = str(error)
            if receipt is not None:
                detail = (
                    f"Receipt saved at {receipt}, but its operator index was not updated: {detail}"
                )
            raise OperatorError(failure_code, detail=detail) from error

    def _record_failure(
        self, action: str, state: str, detail: str, *, facts: dict[str, Any] | None = None
    ) -> None:
        """Record a verb's failure beside its cause, and any identity the verb had.

        `facts` is what the failed step already knew -- the run id and run root
        an export was reading, the volume a fetch named -- so the receipt says
        *which* export or backup failed and not only that one did.
        """

        try:
            self._write_action(
                action,
                {
                    **(facts or {}),
                    "summary": f"{action.capitalize()} did not complete: {state}.",
                    "state": state,
                    "detail": detail,
                },
                descriptor_action=action,
            )
        except OperatorError as record_error:
            # Keep the original operational failure as the command's result,
            # but never hide that its supporting receipt/index also failed.
            for line in record_error.render().splitlines():
                self.present(line)

    def _record_spend_alert(self, prepared: PreparedLaunch) -> None:
        """Persist warning/delivery evidence without retaining its live challenge."""

        preview = prepared.result.preview
        if preview is None or not preview.assessment.alerts:
            return
        try:
            self._write_action(
                "spend-alert",
                {
                    "summary": "A low account-balance warning was assessed.",
                    "action": preview.action,
                    "subject": preview.subject,
                    "spend": preview.assessment.to_record(),
                },
                descriptor_action="spend-alert",
            )
        except OperatorError as record_error:
            # A warning and its launch decision already exist. Losing the
            # receipt is said aloud, but notification bookkeeping cannot become
            # a paid-action gate.
            for line in record_error.render().splitlines():
                self.present(line)

    def _run_door_stage(
        self,
        run_root: Path,
        run_id: str,
        scenario: str,
        *,
        submission_folder: str | Path | None = None,
        submission_manifest: str | Path | None = None,
        data_gate_policy: str | Path | None = None,
        roster_argv: Sequence[str] = (),
    ) -> None:
        command = [
            sys.executable,
            str(self.workspace / DOOR_PROGRAM),
            "--run-root",
            str(run_root),
            "--run-id",
            run_id,
            "--scenario",
            scenario,
        ]
        command.extend(
            _real_ingress_argv(
                submission_folder=submission_folder,
                submission_manifest=submission_manifest,
                data_gate_policy=data_gate_policy,
            )
        )
        command.extend(roster_argv)
        completed = self.runner(
            command,
            cwd=self.workspace,
            capture_output=True,
            text=True,
            check=False,
            env=_stage_environment(),
        )
        if completed.returncode not in {0, 3}:
            raise OperatorError(ErrorCode.RUN_FAILED, detail=completed.stderr or completed.stdout)
        # Match the orchestrator's normal Door path: an admitted-but-partial
        # Door reports its private refusal record on stderr even when its exit
        # is accepted. The crash drill must not replace that security result
        # with only its generic interruption message.
        for line in completed.stderr.rstrip().splitlines():
            self.present(line)

    def _armarium_export(self, run_root: Path, run_id: str) -> dict[str, Any]:
        tree = RunTree(run_root, run_id)
        record = tree.read_artifact(
            ARMARIUM, "export", artifact_id(ARMARIUM, "export", "export", None)
        )
        payload = record.get("payload")
        if not isinstance(payload, dict) or not isinstance(payload.get("aggregate"), dict):
            raise ValueError("Armarium export record has no usable aggregate")
        # The list-valued members every consumer counts or walks: a string here
        # would render a confident wrong page count into a receipt, and a number
        # would kill export with a bare TypeError after the bundle exists.
        for member in ("pages", "delivered", "non_delivered"):
            if member in payload and not isinstance(payload[member], list):
                raise ValueError(f"Armarium export record's {member} is not a list")
        return payload

    def _write_base_armarium_bundle(self, run_root: Path, run_id: str, destination: Path) -> None:
        tree = RunTree(run_root, run_id)
        source = tree.root
        # **A member of the wrong kind was written as complete, same as a missing one.**
        # The loop below is `if is_file() ... elif is_dir()`, so an absent `run.json`
        # matched neither arm, contributed nothing, and the receipt still recorded
        # `"state": "complete"` -- a bundle short of the record saying which run
        # produced it, describing itself as whole.
        #
        # Checking `exists()` alone closed only half of that, and CodeRabbit caught
        # the other half on this very repair: a `7_armarium` that is a **regular
        # file** exists, takes the `is_file()` arm, and is written as a single
        # member -- so the bundle ships without any of the Armarium output and
        # still says complete. That is the same defect through a different door,
        # which is the shape this whole review keeps finding. So each member is
        # required to be the *kind* it is expected to be, not merely present.
        #
        # GOVERNANCE 2: "a partial result is visibly partial; 'complete' is refused
        # unless everything reconciles."
        temporary = destination.with_name(f".{destination.name}.tmp-{secrets.token_hex(16)}")
        root_descriptor: int | None = None
        run_descriptor: int | None = None
        armarium_descriptor: int | None = None
        try:
            root_descriptor = _open_bundle_root(source)
            run_descriptor = _open_expected_member(
                root_descriptor, "run.json", directory=False, label="run.json"
            )
            armarium_descriptor = _open_expected_member(
                root_descriptor, "7_armarium", directory=True, label="7_armarium"
            )
            descriptor = os.open(
                temporary,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
            archive_names: dict[str, str] = {}
            with (
                os.fdopen(descriptor, "w+b") as temporary_handle,
                zipfile.ZipFile(temporary_handle, "w", compression=zipfile.ZIP_DEFLATED) as bundle,
            ):
                _write_bundle_descriptor(
                    bundle, run_descriptor, f"{run_id}/run.json", archive_names
                )
                members = _write_bundle_directory(
                    bundle,
                    armarium_descriptor,
                    f"{run_id}/7_armarium",
                    "7_armarium",
                    archive_names,
                )
            if not members:
                # The same defect through one more door. `7_armarium` was proved
                # to be a directory, never to hold anything: an empty one wrote
                # zero members, this function returned normally, and `export`
                # recorded `"state": "complete"` for a bundle carrying `run.json`
                # and not one established reading. For a parish run that is every
                # act in it missing, with a receipt vouching for the absence.
                raise OperatorError(
                    ErrorCode.EXPORT_FAILED,
                    detail=(
                        "the Armarium evidence bundle cannot be written as complete: "
                        "7_armarium holds no evidence files"
                    ),
                )
            try:
                os.link(temporary, destination, follow_symlinks=False)
            except FileExistsError as error:
                raise OperatorError(
                    ErrorCode.EXPORT_FAILED,
                    detail=f"the evidence bundle destination already exists: {destination}",
                ) from error
        finally:
            for open_descriptor in (armarium_descriptor, run_descriptor, root_descriptor):
                if open_descriptor is not None:
                    os.close(open_descriptor)
            temporary.unlink(missing_ok=True)

    def _provider_for_record(
        self, launch: dict[str, Any], record: PodRecord
    ) -> OperatorFakeProvider:
        """Recreate the one fake pod when a later CLI process performs close."""

        if record.pod_id in self.provider.pods:
            return self.provider
        request_raw = launch.get("request")
        if not isinstance(request_raw, dict):
            raise OperatorError(
                ErrorCode.CLOSE_NOTHING, detail="the launch receipt has no exact request"
            )
        request = _request_from_record(request_raw)
        # This surface's own `now`, not a clock frozen at `record.created_at`:
        # `bill()` below stamps its billing-capture cutoff from whichever clock
        # the provider carries, and `VerifiedShutdown` requests its cutoff from
        # this surface's wall clock. A frozen provider clock and a live request
        # clock drift apart with every minute between launch and close, and once
        # that drift exceeds `bill()`'s one-hour buffer, a real, healthy close
        # reports UNVERIFIED — the one failure this surface exists to avoid
        # reporting spuriously.
        recreated = OperatorFakeProvider(now=self.now)
        try:
            recreated.seed_existing(record, request)
        except ValueError as error:
            raise OperatorError(
                ErrorCode.CLOSE_NOTHING, detail="the fixture pod cannot be safely reconstructed"
            ) from error
        self.provider = recreated
        return recreated

    def _lease_for_close(
        self, launch: dict[str, Any], record: PodRecord
    ) -> tuple[LeaseStore, PodLease]:
        """Read the exact launch lease before a close can alter a provider state."""

        lease_text = launch.get("lease")
        if not isinstance(lease_text, str):
            raise OperatorError(
                ErrorCode.CLOSE_LEASE_UNREADABLE,
                detail="the launch receipt does not name its safety lease",
            )
        try:
            # Recorded relative to the state root (`launch`), so a moved or
            # restored state directory still finds the lease it wrote; an
            # older absolute reference is taken as it is.
            lease_path = self._state_path(lease_text).resolve()
            lease_root = (self.state_root / "leases").resolve()
            if not lease_path.is_relative_to(lease_root):
                raise ValueError("the launch lease is outside this operator state")
            store = LeaseStore(lease_path)
            lease = store.load()
            if lease is None or lease.pod_id != record.pod_id:
                raise ValueError("the launch lease does not bind this exact recorded pod")
        except (OSError, RuntimeError, ValueError) as error:
            raise OperatorError(ErrorCode.CLOSE_LEASE_UNREADABLE, detail=str(error)) from error
        return store, lease

    def _show_volume_cost(self, *, volume_id: str | None, hourly_usd: str) -> None:
        """Printed on every close, verified or not: the pod stopped, the volume did not."""

        for line in volume_cost_lines(volume_id=volume_id, hourly_usd=hourly_usd):
            self.present(line)

    def _present_captured_cost(self, report: CloseReport) -> None:
        if report.captured_cost_usd is not None:
            self.present(
                f"Charges captured through {utc_stamp(report.cutoff_at)}: "
                f"${report.captured_cost_usd} (fixture billing, not a measurement)."
            )
        else:
            self.present("No captured-cost line was available; this close remains unverified.")

    def _notify(self, event: str, message: str) -> None:
        """One standing moment, reported honestly and never able to fail a verb."""

        if self.notifier is notify_bridge.silent:
            # Every *attempted* send is reported, delivered or not — and this
            # is not an attempt, so there is nothing to report.
            return
        # One line, always. A hold reason arrives from an artifact and may
        # carry a newline; shell_notifier refuses a multi-line message, so the
        # decision moment would be dropped exactly when a person is needed.
        one_line = " ".join(message.split()) or "no detail recorded"
        try:
            outcome = self.notifier(event, one_line)
        except Exception as error:  # a broken notifier is not a broken run
            outcome = notify_bridge.NotifyOutcome(
                True, False, f"the notifier raised: {type(error).__name__}"
            )
        self.present(outcome.line())

    def _notify_spend(self, message: str) -> PodNotifyOutcome:
        """Adapt the event-aware notifier without letting failure gate spend."""

        one_line = " ".join(message.split()) or "no spend-warning detail recorded"
        try:
            outcome = self.notifier("milestone", one_line)
        except Exception as error:  # a broken notifier is not a spend gate
            return PodNotifyOutcome(True, False, f"the notifier raised: {type(error).__name__}")
        return PodNotifyOutcome(outcome.attempted, outcome.delivered, outcome.detail)

    def _show_close(self, report: CloseReport, receipt: Path) -> None:
        self._present_captured_cost(report)
        if report.verified:
            self.present(
                "Close verified by the fixture provider's exact-pod and list observations."
            )
        else:
            self.present(
                "UNVERIFIED CLOSE: fixture absence and billing evidence did not both prove the result."
            )
            self.present(
                "Manual check: Open the saved close receipt. Confirm it names this fixture pod, "
                "shows it absent in both saved checks, and gives the billed-through time. "
                "If any part is missing, leave this close unverified and ask for help."
            )
        self._show_volume_cost(
            volume_id=report.volume_id, hourly_usd=str(report.volume_ongoing_hourly_usd)
        )
        self.present(f"Saved close receipt: {receipt}")


def reconciliation_table(export_payload: dict[str, Any]) -> list[str]:
    """Display the Armarium's already-recorded reconciliation in a compact table."""

    aggregate = export_payload.get("aggregate", {})
    expected = export_payload.get("expected_acts", "unknown")

    def counted(member: str) -> str:
        value = export_payload.get(member)
        return str(len(value)) if isinstance(value, list) else "not recorded"

    def counted_category(category: str) -> str:
        value = export_payload.get("non_delivered")
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            return "not recorded"
        return str(sum(item.get("category") == category for item in value))

    rows = [
        "Reconciliation from the recorded Armarium export:",
        "| Recorded item | Count or state |",
        "| --- | --- |",
        f"| Submitted pages accounted for | {counted('pages')} |",
        f"| Expected acts | {expected} |",
        f"| Delivered acts | {counted('delivered')} |",
        f"| Acts held for review | {counted_category('held-for-review')} |",
        f"| Acts refused with reason | {counted_category('refused-with-reason')} |",
        f"| Confirmed blank acts | {counted_category('confirmed-blank')} |",
        f"| Acts excluded with approval | {counted_category('excluded-with-approval')} |",
        f"| Recorded run state | {aggregate.get('status', 'unknown')} |",
    ]
    reasons = aggregate.get("reasons", [])
    if isinstance(reasons, list):
        rows.extend(f"Recorded reason: {reason}" for reason in reasons)
    return rows


def _status_projection(
    action: str,
    payload: dict[str, Any],
    *,
    state_root: Path | None = None,
    ended_later: bool = False,
) -> list[str]:
    """Present selected already-recorded ledger facts without deriving a new truth.

    ``state_root`` is where a state-relative path in a receipt (a run root, a
    bundle) is rejoined for display; the receipt itself keeps the relative
    spelling so a moved state directory still describes its own files.
    ``ended_later`` says a later receipt of the same run recorded its end, so a
    `started` receipt is not read as a run that never came back.
    """

    lines: list[str] = []
    if action == "boot":
        report = payload.get("report")
        if isinstance(report, dict) and isinstance(report.get("color"), str):
            lines.append(f"  Saved boot report: {report['color'].upper()}.")
            remediation = report.get("remediation")
            if report["color"] != "green" and isinstance(remediation, str) and remediation:
                lines.append(f"  Saved boot next step: {remediation}")
    elif action == "upload":
        if payload.get("zero_gpu_hours") is True:
            lines.append("  Saved upload statement: zero GPU-hours were used.")
        # Local paths are machine details; the digest is the durable manifest identity.
        recorded_sha256 = payload.get("submission_manifest_sha256")
        # Only a receipt that claims bytes moved must bind a digest. An upload
        # refused before any transfer began -- `submission-refused`,
        # `volume-unavailable` -- never had a sealed record to bind, and demanding
        # one turned that honest receipt into "UNREADABLE; it was not treated as
        # success", with `status` then exiting 2. `status` is read-only, is
        # documented as always safe, and is the verb every failure message sends
        # the operator to, so it is the one that must keep working after a
        # failure; breaking it there also teaches them to ignore
        # STATUS_UNREADABLE. `zero_gpu_hours` above is guarded with `is True` for
        # exactly the same reason.
        if payload.get("state") in {"complete", "partial-transfer"}:
            if not (
                isinstance(recorded_sha256, str)
                and len(recorded_sha256) == 64
                and all(character in "0123456789abcdef" for character in recorded_sha256)
            ):
                raise RecordError("saved upload record does not bind its submission record digest")
            lines.append(f"  Sealed submission record digest: {recorded_sha256}.")
        lines.extend(_volume_status_lines(payload.get("volume")))
        detail = payload.get("detail")
        if isinstance(detail, str) and detail.strip():
            lines.append(f"  Reason: {detail}")
    elif action == "run":
        run_id = payload.get("run_id")
        run_root = _display_path(payload.get("run_root"), state_root)
        state = payload.get("state")
        if isinstance(run_id, str):
            # Every run record used to render identically; a state directory
            # that saw three runs, or one run that failed two ways, showed
            # three indistinguishable lines.
            lines.append(f"  Run: {run_id}" + (f"; run root: {run_root}" if run_root else "") + ".")
        if isinstance(state, str):
            lines.append(f"  Saved run state: {state}.")
        if state == "started" and isinstance(run_id, str) and not ended_later:
            lines.append(
                "  No later record of this run is saved here, so it never reported an end "
                "state: it was interrupted or killed, or is still running. Once no writer is "
                f"active, `verbatus run --run-id {run_id}` resumes it."
            )
        reason = payload.get("reason")
        reasons = payload.get("reasons")
        listed = reasons if isinstance(reasons, list) else []
        if isinstance(reason, str) and reason and reason not in listed:
            lines.append(f"  Reason: {reason}")
        unreadable = payload.get("reasons_unreadable")
        if isinstance(unreadable, str) and unreadable:
            lines.append(f"  Hold reason: UNREADABLE. {unreadable}")
        for item in listed:
            lines.append(f"  Hold reason: {item}")
        observed = payload.get("last_observed_work")
        if isinstance(observed, str):
            lines.append(f"  Saved last recorded work: {observed}")
        lines.extend(_recorded_output_lines(payload.get("detail"), reason))
        if isinstance(run_id, str) and run_root:
            lines.append(f"  Review it read-only with: {review_command(Path(run_root), run_id)}")
    elif action == "export":
        run_id = payload.get("run_id")
        bundle = _display_path(payload.get("bundle"), state_root)
        state = payload.get("state")
        if isinstance(run_id, str):
            lines.append(f"  Run: {run_id}" + (f"; bundle: {bundle}" if bundle else "") + ".")
        if isinstance(state, str):
            lines.append(f"  Saved export state: {state}.")
        detail = payload.get("detail")
        if isinstance(detail, str) and detail.strip():
            lines.append(f"  Reason: {detail}")
        table = payload.get("reconciliation")
        if isinstance(table, list):
            lines.extend(f"  {line}" for line in table if isinstance(line, str))
    elif action == "fetch-run":
        run_id = payload.get("run_id")
        into = payload.get("into")
        state = payload.get("state")
        if isinstance(run_id, str):
            lines.append(
                f"  Run: {run_id}"
                + (f"; fetched into: {into}" if isinstance(into, str) else "")
                + "."
            )
        lines.extend(_volume_status_lines(payload.get("volume")))
        if isinstance(state, str):
            lines.append(f"  Saved fetch state: {state}.")
        detail = payload.get("detail")
        if isinstance(detail, str) and detail.strip():
            lines.append(f"  Reason: {detail}")
        if isinstance(run_id, str) and isinstance(into, str):
            lines.append(f"  Review it read-only with: {review_command(Path(into), run_id)}")
    elif action == "backup":
        run_id = payload.get("run_id")
        run_root = payload.get("run_root")
        destination = payload.get("mac_directory")
        state = payload.get("state")
        if isinstance(run_id, str):
            lines.append(
                f"  Run: {run_id}"
                + (f"; run root: {run_root}" if isinstance(run_root, str) else "")
                + (f"; destination: {destination}" if isinstance(destination, str) else "")
                + "."
            )
        if isinstance(state, str):
            lines.append(f"  Saved backup state: {state}.")
        report = payload.get("report")
        if isinstance(report, dict):
            lines.append(
                f"  Snapshot {report.get('snapshot_sha256')}: {report.get('copied')} copied, "
                f"{report.get('reused')} reused."
            )
        detail = payload.get("detail")
        if isinstance(detail, str) and detail.strip():
            lines.append(f"  Reason: {detail}")
    elif action == "unexpected":
        exception_type = payload.get("exception_type")
        message = payload.get("message")
        if isinstance(exception_type, str):
            # One line on the screen; the receipt keeps the whole message.
            first = message.strip().splitlines() if isinstance(message, str) else []
            lines.append(f"  {exception_type}: {first[0] if first else ''}")
        argv = payload.get("argv")
        if isinstance(argv, list) and all(isinstance(word, str) for word in argv):
            lines.append(f"  Command: verbatus {' '.join(argv)}")
        cwd = payload.get("cwd")
        if isinstance(cwd, str):
            lines.append(f"  Working directory: {cwd}")
    elif action == "close":
        report = payload.get("close_report")
        if not isinstance(report, dict):
            return lines
        state = report.get("state")
        cost = report.get("cost_capture")
        volume = report.get("volume")
        if isinstance(cost, dict):
            cutoff = cost.get("cutoff_at")
            total = cost.get("total_usd")
            if isinstance(cutoff, str) and isinstance(total, str):
                lines.append(
                    "  Saved charges captured through {}: ${} (fixture billing, not a "
                    "measurement).".format(cutoff, total)
                )
        if state != "verified" and isinstance(state, str):
            lines.append(f"  Saved close state: {state.upper()}.")
        if isinstance(volume, dict) and isinstance(volume.get("ongoing_hourly_usd"), str):
            lines.append(
                "  Saved retained-volume price: $" + volume["ongoing_hourly_usd"] + " per hour."
            )
    return lines


STATUS_OUTPUT_LINES: Final = 12
"""How many lines of a run's recorded output `status` shows before pointing at
the receipt for the rest. The receipt keeps the bounded tail whole; the screen
shows enough to name the cause without burying the rows around it."""


def _recorded_output_lines(detail: object, reason: object) -> list[str]:
    """The last lines of a failed run's recorded output, for the `status` screen."""

    if not isinstance(detail, str) or not detail.strip() or detail.strip() == reason:
        return []
    every_line = detail.rstrip().splitlines()
    shown = every_line[-STATUS_OUTPUT_LINES:]
    heading = "  Recorded output"
    if len(shown) < len(every_line):
        heading += f" (last {len(shown)} of {len(every_line)} lines; the receipt holds all of them)"
    return [f"{heading}:", *(f"    {line}" for line in shown)]


def _volume_status_lines(volume: object) -> list[str]:
    """Which volume a fetch or upload receipt names, or nothing when it names none."""

    if not isinstance(volume, dict):
        return []
    datacenter = volume.get("datacenter_id")
    volume_id = volume.get("volume_id")
    endpoint = volume.get("endpoint_url")
    if not isinstance(datacenter, str) or not isinstance(volume_id, str):
        return []
    line = f"  Volume: {datacenter}:{volume_id}"
    if isinstance(endpoint, str):
        line += f" at {endpoint}"
    return [line + "."]


def _display_path(value: object, state_root: Path | None) -> str | None:
    """A recorded path as the operator can open it: state-relative ones rejoined."""

    if not isinstance(value, str) or not value:
        return None
    candidate = Path(value)
    if candidate.is_absolute() or state_root is None:
        return value
    return str(state_root / candidate)


def review_command(run_root: Path, run_id: str) -> str:
    """The ready-to-paste `review` invocation for one run, quoted for a shell."""

    return f"verbatus review --run-root {shlex.quote(str(run_root))} --run-id {shlex.quote(run_id)}"


def _read_sealed_manifest(path: Path) -> bytes:
    """Read one bounded regular-file snapshot without following its final name."""

    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OSError("the sealed submission record is not a regular file")
        data = handle.read(MAX_SEALED_MANIFEST_BYTES + 1)
    if len(data) > MAX_SEALED_MANIFEST_BYTES:
        raise OSError(f"the sealed submission record exceeds {MAX_SEALED_MANIFEST_BYTES} bytes")
    return data


def _load_policy(path: str | Path) -> SpendPolicy:
    try:
        return load_spend_policy(path)
    except Exception as error:
        raise OperatorError(ErrorCode.SPEND_POLICY_REQUIRED, detail=str(error)) from error


def _read_published_lease(path: Path) -> PodLease | None:
    """Read one published lease without writing anything beside it.

    `LeaseStore.load()` takes the writer's advisory lock, and taking it *creates*
    `.<name>.lock` in the lease directory. `status` is documented — here, in
    `records.py`, and in this package's README — as reading only, and it is the
    one verb an operator runs to find a pod that may still be billing: a state
    directory that cannot be written must not turn every lease into UNREADABLE
    and hide exactly that (GOVERNANCE 2). A lease is only ever published whole
    (`os.link` of an fsynced temporary, or `os.replace`), so a lock-free read
    sees one complete version or another, never a torn one, and
    `PodLease.from_record` still refuses any record whose seal does not
    recompute. The authority over a second paid action remains the gate's own
    locked read in `operations/pod/launch.py`.

    Opened the way `records.sha256_file` opens evidence, and for its reasons:
    no-follow, so the caller's symlink check cannot be raced between the look
    and the open; non-blocking and refused unless the open descriptor says
    regular, so a FIFO planted at a lease name cannot hang `status` forever
    having printed nothing; and bounded, because a file this tool did not write
    is the only way one of these reaches a mebibyte.
    """

    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OSError(errno.EINVAL, "a lease needs a regular file", str(path))
        data = handle.read(MAX_RECORD_BYTES + 1)
    if len(data) > MAX_RECORD_BYTES:
        raise RecordError(f"lease {path} is larger than {MAX_RECORD_BYTES} bytes and was not read")
    return PodLease.from_record(json.loads(data.decode("utf-8")))


def _request_record(request: PodCreateRequest) -> dict[str, Any]:
    return {
        "name": request.name,
        "gpu_type": request.gpu_type,
        "image": request.image,
        "volume_id": request.volume_id,
        "volume_mount_path": request.volume_mount_path,
        "docker_start_cmd": list(request.docker_start_cmd),
        "hard_deadline": utc_stamp(request.hard_deadline),
        "repository_commit": request.repository_commit,
        "container_disk_gb": request.container_disk_gb,
        "template": request.template,
        "metadata": dict(request.metadata),
        "interruptible": request.interruptible,
        "recovery_only": request.recovery_only,
    }


def _review_record(
    request: PodCreateRequest,
    action: str,
    adopted_pod_id: str | None,
    preview: PaidActionPreview,
) -> dict[str, object]:
    """Return the recomputable, phraseless preimage of the UI review digest.

    A durable record must not retain a spendable challenge, and its digest must
    cover only bytes a reader can recover from that record.
    """

    return {
        "action": action,
        "adopted_pod_id": adopted_pod_id,
        "request": _request_record(request),
        "preview": phraseless(preview).to_record(),
    }


def _request_from_record(value: dict[str, Any]) -> PodCreateRequest:
    try:
        required = {
            "name",
            "gpu_type",
            "image",
            "volume_id",
            "volume_mount_path",
            "docker_start_cmd",
            "hard_deadline",
            "repository_commit",
            "template",
            "metadata",
            "interruptible",
            "recovery_only",
        }
        # Written by every record this surface writes, and read back as the
        # default when it is absent: a record saved before the field existed
        # must still load, because the verb that reads it is `close`, and a
        # close that cannot read its own saved request cannot stop a pod that
        # is billing.
        optional = {"container_disk_gb"}
        if set(value) - optional != required:
            raise ValueError("request has missing or unknown fields")
        deadline_text = value["hard_deadline"]
        if not isinstance(deadline_text, str):
            raise ValueError("deadline is invalid")
        deadline = datetime.fromisoformat(deadline_text.replace("Z", "+00:00"))
        command = value["docker_start_cmd"]
        metadata = value["metadata"]
        interruptible = value["interruptible"]
        recovery_only = value["recovery_only"]
        if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
            raise ValueError("command is invalid")
        if not isinstance(metadata, dict) or not all(
            isinstance(key, str) and isinstance(item, str) for key, item in metadata.items()
        ):
            raise ValueError("metadata is invalid")
        if not isinstance(interruptible, bool) or not isinstance(recovery_only, bool):
            raise ValueError("request booleans are invalid")
        return PodCreateRequest(
            name=value["name"],
            gpu_type=value["gpu_type"],
            image=value["image"],
            volume_id=value["volume_id"],
            volume_mount_path=value["volume_mount_path"],
            docker_start_cmd=tuple(command),
            hard_deadline=require_utc(deadline, "recorded hard deadline"),
            repository_commit=value["repository_commit"],
            container_disk_gb=value.get("container_disk_gb", DEFAULT_CONTAINER_DISK_GB),
            template=value["template"],
            metadata=metadata,
            interruptible=interruptible,
            recovery_only=recovery_only,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise OperatorError(
            ErrorCode.CLOSE_NOTHING, detail="the saved launch request is invalid"
        ) from error


def _pod_record(record: PodRecord) -> dict[str, Any]:
    contract = record.runtime_contract
    return {
        "pod_id": record.pod_id,
        "name": record.name,
        "volume_id": record.volume_id,
        "created_at": utc_stamp(record.created_at),
        "state": record.state,
        "estimate": {
            "pod_hourly_usd": str(record.estimate.pod_hourly_usd),
            "volume_hourly_usd": str(record.estimate.volume_hourly_usd),
            "source": record.estimate.source,
            "observed_at": utc_stamp(record.estimate.observed_at),
        },
        "runtime_contract": None
        if contract is None
        else {
            "interruptible": contract.interruptible,
            "gpu_type": contract.gpu_type,
            "image": contract.image,
            "volume_id": contract.volume_id,
            "volume_mount_path": contract.volume_mount_path,
            "docker_start_cmd": list(contract.docker_start_cmd),
            "billing_cutoff_margin_seconds": contract.billing_cutoff_margin_seconds,
            "template": contract.template,
        },
    }


def _pod_from_record(value: dict[str, Any]) -> PodRecord:
    try:
        required = {
            "pod_id",
            "name",
            "volume_id",
            "created_at",
            "state",
            "estimate",
            "runtime_contract",
        }
        if set(value) != required:
            raise ValueError("pod record has missing or unknown fields")
        estimate_raw = value["estimate"]
        contract_raw = value["runtime_contract"]
        if not isinstance(estimate_raw, dict) or not isinstance(contract_raw, dict):
            raise ValueError("pod record is missing immutable observations")
        if set(estimate_raw) != {
            "pod_hourly_usd",
            "volume_hourly_usd",
            "source",
            "observed_at",
        }:
            raise ValueError("pod estimate has missing or unknown fields")
        if set(contract_raw) != {
            "interruptible",
            "gpu_type",
            "image",
            "volume_id",
            "volume_mount_path",
            "docker_start_cmd",
            "billing_cutoff_margin_seconds",
            "template",
        }:
            raise ValueError("pod runtime contract has missing or unknown fields")
        created_text = value["created_at"]
        observed_text = estimate_raw["observed_at"]
        if not isinstance(created_text, str) or not isinstance(observed_text, str):
            raise ValueError("pod observation times are invalid")
        if not all(
            isinstance(value[field], str) for field in ("pod_id", "name", "volume_id", "state")
        ):
            raise ValueError("pod identity fields are invalid")
        if not all(
            isinstance(estimate_raw[field], str)
            for field in ("pod_hourly_usd", "volume_hourly_usd", "source")
        ):
            raise ValueError("pod estimate fields are invalid")
        interruptible = contract_raw["interruptible"]
        if not isinstance(interruptible, bool):
            raise ValueError("pod interruptible observation is invalid")
        if not all(
            isinstance(contract_raw[field], str)
            for field in ("gpu_type", "image", "volume_id", "volume_mount_path")
        ):
            raise ValueError("pod runtime identity fields are invalid")
        template = contract_raw["template"]
        if template is not None and not isinstance(template, str):
            raise ValueError("pod template is invalid")
        created = datetime.fromisoformat(created_text.replace("Z", "+00:00"))
        observed = datetime.fromisoformat(observed_text.replace("Z", "+00:00"))
        command = contract_raw["docker_start_cmd"]
        if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
            raise ValueError("pod command is invalid")
        return PodRecord(
            pod_id=value["pod_id"],
            name=value["name"],
            estimate=PodEstimate(
                pod_hourly_usd=Decimal(estimate_raw["pod_hourly_usd"]),
                volume_hourly_usd=Decimal(estimate_raw["volume_hourly_usd"]),
                source=estimate_raw["source"],
                observed_at=require_utc(observed, "recorded estimate time"),
            ),
            volume_id=value["volume_id"],
            created_at=require_utc(created, "recorded pod creation time"),
            state=value["state"],
            runtime_contract=PodRuntimeContract(
                interruptible=interruptible,
                gpu_type=contract_raw["gpu_type"],
                image=contract_raw["image"],
                volume_id=contract_raw["volume_id"],
                volume_mount_path=contract_raw["volume_mount_path"],
                docker_start_cmd=tuple(command),
                billing_cutoff_margin_seconds=require_billing_cutoff_margin_seconds(
                    contract_raw["billing_cutoff_margin_seconds"], "recorded billing cutoff margin"
                ),
                template=template,
            ),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise OperatorError(
            ErrorCode.CLOSE_NOTHING, detail="the saved pod record is invalid"
        ) from error


def _stage_environment() -> dict[str, str]:
    """Pass the ordinary runtime environment, but never a provider credential.

    Used to strip only the two upload-only S3 keys, reasoning that "pipeline
    stages neither upload nor inspect a network volume" -- true, but no reason
    to keep RUNPOD_API_KEY (pod creation = money), HF_TOKEN, AWS_*, or any other
    provider credential either (F016): these processes decode attacker-supplied
    PDFs, TIFFs, HEICs and PNGs, and talk to the serving endpoint, which is
    already *more* hostile material than the console/backup/advance/ScanTailor
    children `credential_free_environment` was built to protect. Built on the
    same predicate rather than its own list, so a credential shape added there
    protects a stage subprocess too.
    """

    return credential_free_environment()


def _sha256_regular_file_nofollow(path: Path) -> str:
    """Hash one anchored regular file, refusing a link or a concurrent rewrite."""

    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError as error:
        raise OSError(
            f"the existing content-addressed export is not a readable file: {path}"
        ) from error
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise OSError(f"the existing content-addressed export is not a regular file: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            while chunk := handle.read(_COPY_CHUNK_BYTES):
                digest.update(chunk)
        after = os.fstat(descriptor)
        observed_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        observed_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if observed_after != observed_before:
            raise OSError(f"the existing content-addressed export changed while read: {path}")
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _open_bundle_root(source: Path) -> int:
    """Anchor the run root itself before opening either required member."""

    try:
        named = os.stat(source, follow_symlinks=False)
        if not stat.S_ISDIR(named.st_mode):
            raise OSError("not a directory")
        descriptor = os.open(
            source,
            os.O_RDONLY | os.O_NONBLOCK | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
    except OSError as error:
        raise OperatorError(
            ErrorCode.EXPORT_FAILED,
            detail=f"the Armarium evidence run root cannot be opened safely: {source}: {error}",
        ) from error
    opened = os.fstat(descriptor)
    if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
        os.close(descriptor)
        raise OperatorError(
            ErrorCode.EXPORT_FAILED,
            detail="the Armarium evidence run root changed between check and open",
        )
    return descriptor


def _open_expected_member(
    parent_descriptor: int,
    name: str,
    *,
    directory: bool,
    label: str,
) -> int:
    """Open one member relative to an anchored directory, without following links."""

    expected_kind = "directory" if directory else "regular file"
    try:
        named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError as error:
        raise OperatorError(
            ErrorCode.EXPORT_FAILED,
            detail=(
                f"the Armarium evidence bundle cannot be written as complete: {label} is missing"
            ),
        ) from error
    if stat.S_ISLNK(named.st_mode):
        raise OperatorError(
            ErrorCode.EXPORT_FAILED,
            detail=(
                "the Armarium evidence bundle cannot be written as complete: "
                f"{label} is a symbolic link, not a {expected_kind}"
            ),
        )
    expected = stat.S_ISDIR(named.st_mode) if directory else stat.S_ISREG(named.st_mode)
    if not expected:
        raise OperatorError(
            ErrorCode.EXPORT_FAILED,
            detail=(
                "the Armarium evidence bundle cannot be written as complete: "
                f"{label} is not a {expected_kind}"
            ),
        )
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
    if directory:
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except OSError as error:
        raise OperatorError(
            ErrorCode.EXPORT_FAILED,
            detail=(
                "the Armarium evidence bundle cannot be written as complete: "
                f"{label} changed before it could be opened safely"
            ),
        ) from error
    opened = os.fstat(descriptor)
    if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
        os.close(descriptor)
        raise OperatorError(
            ErrorCode.EXPORT_FAILED,
            detail=(
                "the Armarium evidence bundle cannot be written as complete: "
                f"{label} changed between check and open"
            ),
        )
    return descriptor


def _register_archive_name(archive_name: str, archive_names: dict[str, str]) -> None:
    """Refuse byte-distinct members that extract to one default-APFS name."""

    collision_key = unicodedata.normalize("NFD", archive_name).casefold()
    prior = archive_names.get(collision_key)
    if prior is not None and prior != archive_name:
        raise OperatorError(
            ErrorCode.EXPORT_FAILED,
            detail=(
                "the Armarium evidence bundle has names that collide on the default "
                f"macOS filesystem: {prior!r} and {archive_name!r}"
            ),
        )
    archive_names[collision_key] = archive_name


def _write_bundle_descriptor(
    bundle: zipfile.ZipFile,
    descriptor: int,
    archive_name: str,
    archive_names: dict[str, str],
) -> None:
    """Copy one already-anchored regular file and reject an in-place rewrite."""

    _register_archive_name(archive_name, archive_names)
    opened = os.fstat(descriptor)
    with (
        os.fdopen(os.dup(descriptor), "rb") as input_handle,
        bundle.open(archive_name, "w", force_zip64=True) as output_handle,
    ):
        while chunk := input_handle.read(_COPY_CHUNK_BYTES):
            output_handle.write(chunk)
    after = os.fstat(descriptor)
    observed_opened = (
        opened.st_dev,
        opened.st_ino,
        opened.st_size,
        opened.st_mtime_ns,
        opened.st_ctime_ns,
    )
    observed_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if observed_after != observed_opened:
        raise OperatorError(
            ErrorCode.EXPORT_FAILED,
            detail=f"the Armarium evidence member changed while copied: {archive_name}",
        )


def _write_bundle_directory(
    bundle: zipfile.ZipFile,
    directory_descriptor: int,
    archive_prefix: str,
    source_prefix: str,
    archive_names: dict[str, str],
) -> int:
    """Walk one anchored directory using only descriptor-relative opens.

    Returns how many regular-file members this subtree contributed, so a caller
    can refuse a required member that turned out to hold nothing.
    """

    written = 0
    before = os.fstat(directory_descriptor)
    try:
        names = sorted(os.listdir(directory_descriptor))
    except OSError as error:
        raise OperatorError(
            ErrorCode.EXPORT_FAILED,
            detail=f"the Armarium evidence bundle cannot read {source_prefix}: {error}",
        ) from error
    for name in names:
        label = f"{source_prefix}/{name}"
        try:
            named = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        except OSError as error:
            raise OperatorError(
                ErrorCode.EXPORT_FAILED,
                detail=f"the Armarium evidence bundle cannot read {label}: {error}",
            ) from error
        if stat.S_ISDIR(named.st_mode):
            child = _open_expected_member(directory_descriptor, name, directory=True, label=label)
            try:
                written += _write_bundle_directory(
                    bundle,
                    child,
                    f"{archive_prefix}/{name}",
                    label,
                    archive_names,
                )
            finally:
                os.close(child)
        elif stat.S_ISREG(named.st_mode):
            child = _open_expected_member(directory_descriptor, name, directory=False, label=label)
            try:
                _write_bundle_descriptor(bundle, child, f"{archive_prefix}/{name}", archive_names)
                written += 1
            finally:
                os.close(child)
        else:
            raise OperatorError(
                ErrorCode.EXPORT_FAILED,
                detail=(
                    "the Armarium evidence bundle cannot be written as complete: "
                    f"{label} is not a regular file"
                ),
            )
    after = os.fstat(directory_descriptor)
    observed_before = (
        before.st_dev,
        before.st_ino,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    observed_after = (
        after.st_dev,
        after.st_ino,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if observed_after != observed_before:
        raise OperatorError(
            ErrorCode.EXPORT_FAILED,
            detail=f"the Armarium evidence directory changed while copied: {source_prefix}",
        )
    return written


def _repository_commit(workspace: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=workspace,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise OperatorError(
            ErrorCode.BOOT_RED,
            detail=f"the current repository commit could not be read: {error}",
        ) from error
    value = result.stdout.strip()
    if (
        result.returncode != 0
        or len(value) != 40
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise OperatorError(
            ErrorCode.BOOT_RED, detail="the current repository commit could not be read"
        )
    return value


def _repository_commit_or_reason(workspace: Path) -> tuple[str | None, str | None]:
    """The commit a run is about to use, or the reason it could not be read.

    `boot` refuses without one; a run does not, because a workspace that is
    not a git checkout (a copied tree on a pod) can still run. The receipt then
    says the commit was unreadable and why, rather than omitting the field.
    """

    try:
        return _repository_commit(workspace), None
    except OperatorError as error:
        return None, error.detail or str(error)


def _config_binding(path: str | Path | None) -> dict[str, Any] | None:
    """One configuration input as the receipt records it: its path and digest.

    Absent inputs stay `None` (the orchestrator's default applies and is
    sealed by the stage itself). A named file that cannot be digested is
    recorded with the reason, never silently without a digest.
    """

    if path is None:
        return None
    absolute = Path(path).absolute()
    try:
        return {"path": str(absolute), "sha256": sha256_file(absolute)}
    except OSError as error:
        return {"path": str(absolute), "sha256": None, "unreadable": str(error)}


MAX_RECEIPT_OUTPUT_CHARACTERS: Final = 64_000
"""How much of a child's stderr or stdout one receipt keeps.

A receipt above `MAX_RECORD_BYTES` cannot be read back at all, so an unbounded
stderr would make the one record of a failure unreadable exactly when it is
long. The tail is kept because a stage's last words name the failure; the cut
is named inside the record, so a shortened tail never reads as the whole."""


def bounded_tail(text: str) -> str:
    """The last `MAX_RECEIPT_OUTPUT_CHARACTERS` of a child's output, cut named."""

    if len(text) <= MAX_RECEIPT_OUTPUT_CHARACTERS:
        return text
    omitted = len(text) - MAX_RECEIPT_OUTPUT_CHARACTERS
    return (
        f"[{omitted} earlier characters omitted from this receipt]\n"
        + text[-MAX_RECEIPT_OUTPUT_CHARACTERS:]
    )


def _last_line(text: str) -> str:
    """The last non-blank line of a child's output: where a stage names its failure."""

    for line in reversed(text.splitlines()):
        if line.strip():
            return line.strip()
    return ""


def _volume_record(volume: VolumeSpec | None) -> dict[str, str] | None:
    """Which volume a transfer receipt names -- the identity, never the keys."""

    if volume is None:
        return None
    return {
        "datacenter_id": volume.datacenter_id,
        "volume_id": volume.volume_id,
        "endpoint_url": volume.endpoint_url,
    }


def _armarium_reference(run_root: Path, run_id: str) -> str:
    tree = RunTree(run_root, run_id)
    relative = tree.artifact_path(
        ARMARIUM, "export", artifact_id(ARMARIUM, "export", "export", None)
    )
    return str(tree.resolve(relative))


def _run_program(*args, **kwargs) -> subprocess.CompletedProcess[str]:  # type: ignore[no-untyped-def]
    return subprocess.run(*args, **kwargs)


def _real_ingress_argv(
    *,
    submission_folder: str | Path | None,
    submission_manifest: str | Path | None,
    data_gate_policy: str | Path | None,
) -> list[str]:
    """Bind paths to the operator's cwd without hiding symlinks from the Door."""
    argv: list[str] = []
    for flag, value in (
        ("--submission-folder", submission_folder),
        ("--submission-manifest", submission_manifest),
        ("--data-gate-policy", data_gate_policy),
    ):
        if value is not None:
            argv.extend((flag, str(Path(value).absolute())))
    return argv


def _roster_argv(
    *,
    models_config: str | Path | None,
    serving_recipes_config: str | Path | None,
    witness_context_config: str | Path | None,
) -> list[str]:
    """The real-roster trio, forwarded together; a partial selection is refused.

    The witness-context declaration joined the pair for the same reason the
    catalogue was in it: the shipped declaration
    (`config/witness_context.toml`) says of every chair that it is a synthetic
    fixture with no real training domain, and under the `named` regime that
    sentence is handed to the Perlector as fact about the witness whose
    testimony it is reading. Forwarding a real roster without it seals a run
    whose own record calls its real witnesses fixtures --
    `common/stage.py::validate_witness_context_bindings` refuses that at the
    Door, and refusing it here keeps the console's message about the console's
    own flags.
    """

    selected = (models_config, serving_recipes_config, witness_context_config)
    if any(value is None for value in selected) and any(value is not None for value in selected):
        raise OperatorError(
            ErrorCode.INVALID_COMMAND,
            detail=(
                "--models-config, --serving-recipes-config and --witness-context-config "
                "select one roster together (the chairs, the catalogue they are served "
                "under, and the factual witness context the Perlector is told about them); "
                "supply all three or none"
            ),
        )
    if models_config is None:
        return []
    return [
        "--models-config",
        str(Path(models_config).absolute()),
        "--serving-recipes-config",
        str(Path(serving_recipes_config).absolute()),  # type: ignore[arg-type]
        "--witness-context-config",
        str(Path(witness_context_config).absolute()),  # type: ignore[arg-type]
    ]


_MANIFEST_NAMES = frozenset({MANIFEST_FILE, DOOR_MANIFEST_FILE})


class FetchRunRefusal(RuntimeError):
    """The fetched tree could not be verified as one whole; it is never called fetched."""


class RunObjectReader(Protocol):
    """What `fetch_run` needs from the volume: a listing and a streamed read."""

    def list_keys(self, prefix: str) -> tuple[str, ...]:
        """Every key under `prefix`, or a refusal; never a shorter listing."""

    def fetch_to(self, key: str, destination: Path, *, max_bytes: int) -> int:
        """Stream one object into `destination`; the byte count, or a refusal."""


@dataclass(frozen=True, slots=True)
class FetchRunOutcome:
    fetched: int
    reused: int
    bytes: int
    stages: tuple[str, ...]
    excluded: tuple[str, ...]
    # A stage whose last write reached no `manifest.json` -- a crash, an
    # EXIT_FATAL, a SIGKILL, or the pod timer destroying the pod at the hard
    # deadline can all leave artifacts with no manifest recording them. Such a
    # stage's artifacts are still verified, individually, through the run
    # tree's own envelope reader (`RunTree.build_manifest(..., verify_inputs
    # =False)`, the same checks a stored manifest would have applied) rather
    # than refused as a whole. Non-empty here means the outcome is
    # "verified-partial", never "verified".
    unmanifested_stages: tuple[str, ...] = ()
    envelope_only_artifacts: tuple[str, ...] = ()
    # `<stage>/serving-logs/`: the vLLM launch log a served chair leaves in the
    # run tree. No manifest records an engine log and nothing ever digested one,
    # so it cannot be verified the way the rest of the tree is -- but refusing
    # the whole tree for it, which is what an unnamed path did, threw away the
    # evidence the run existed to produce. It comes home as side evidence, named
    # here and digested in the receipt so the local copy can be told apart from
    # a later one, and never counted among what was verified (GOVERNANCE 10).
    unverified_serving_logs: tuple[tuple[str, str], ...] = ()
    # A serving log that did not come home, and why. Named per object rather
    # than raised: the log is the one append-only file in the tree -- a live
    # engine is still writing it -- so local bytes that differ from the
    # volume's, or a debug-level log past `MAX_FETCH_OBJECT_BYTES`, are ordinary
    # states of a file nobody digested, and taking the whole verified run tree
    # down for one of them is the failure this verb was fixed to stop. Nothing
    # is lost silently (GOVERNANCE 2): each is in the receipt and on the screen.
    refused_serving_logs: tuple[str, ...] = ()


def _fetch_run_tree(
    reader: RunObjectReader, prefix: str, destination_root: Path, run_id: str
) -> FetchRunOutcome:
    """List, fetch, and verify one run tree; refuse the first thing that does not reconcile."""

    tree = RunTree(destination_root, run_id)
    scope = tree.inventory_scope()
    keys = reader.list_keys(prefix)
    if not keys:
        raise FetchRunRefusal(
            f"nothing is stored under {prefix!r} on the volume; either the run was never "
            "written there or the prefix is not where pod_run wrote it."
        )
    relative_paths: list[str] = []
    excluded: list[str] = []
    for key in keys:
        relative = key[len(prefix) :]
        if is_publication_temporary(relative, scope):
            excluded.append(relative)
            continue
        if (
            not relative
            or relative.startswith("/")
            or ".." in relative.split("/")
            or not any(
                relative.startswith(item) if item.endswith("/") else relative == item
                for item in scope
            )
        ):
            raise FetchRunRefusal(
                f"the volume holds {key!r} under the run prefix, and no stage of a run tree "
                "accounts for an object at that path; nothing was fetched past it."
            )
        relative_paths.append(relative)
    if RUN_FILE not in relative_paths:
        raise FetchRunRefusal(
            f"no {RUN_FILE} under {prefix!r}: there is no run authority to check the rest "
            "against, so nothing was fetched."
        )
    # Authority first, then the inventories, then everything the inventories
    # account for -- each verified as it lands, so a mismatch stops the fetch
    # at the object that failed rather than after a directory full of them.
    ordered = sorted(
        relative_paths,
        key=lambda item: (
            item != RUN_FILE,
            PurePosixPath(item).name not in _MANIFEST_NAMES,
            item,
        ),
    )
    fetched = reused = total = 0
    expected: dict[str, str] = {}
    manifests: dict[str, dict[str, Any]] = {}
    serving_logs: list[tuple[str, str]] = []
    refused_logs: list[str] = []
    unresolved: dict[str, str] = {}  # every fetched artifact -> its digest, resolved below
    # Every target this call itself wrote fresh (never one already on disk that
    # was only compared). A refusal anywhere below -- including one raised well
    # after the object that turned out bad was fetched, such as the stage
    # reconciliation at the end -- unwinds every one of them, so a forged
    # artifact, blob, or receipt this call fetched never survives under its
    # real name once the fetch as a whole is refused.
    staged: list[Path] = []
    root = tree.root
    try:
        for relative in ordered:
            target = root / relative
            if _is_serving_log(relative):
                # Side evidence, and refused per object rather than fatally.
                # An engine log is in no manifest, is not content-addressed and
                # is not JSON, so there is nothing to check it against; it is
                # also the one file in the tree that is still being appended to
                # while the fetch runs. A second fetch of a held run therefore
                # meets different bytes than the first brought home, and
                # `_fetch_or_compare` refuses that -- correctly, for immutable
                # evidence, and fatally for the whole tree if it were raised
                # here. The same goes for a log that has grown past
                # `MAX_FETCH_OBJECT_BYTES`. Both are named and the verified run
                # tree still comes home.
                #
                # Classified before anything is fetched: the arms below verify,
                # and a log is the one object none of them can speak for.
                # `_fetch_or_compare`'s own guards still run and still refuse
                # -- a symlink standing where a log belongs is refused before a
                # byte is written, exactly as anywhere else in the tree. What
                # narrows is the blast radius, not the check.
                # `is_symlink` as well as `exists`: a dangling symlink standing
                # where a log belongs is refused by `_fetch_or_compare` and is
                # still something this call did not create, so the cleanup below
                # must not delete it. Nothing local is removed that this call
                # did not write.
                existed = target.exists() or target.is_symlink()
                parent_existed = target.parent.exists() or target.parent.is_symlink()
                try:
                    log_size, log_reused = _fetch_or_compare(reader, prefix + relative, target)
                except Exception as error:  # noqa: BLE001 -- recorded per log, never fatal
                    if not existed:
                        target.unlink(missing_ok=True)
                    if not parent_existed:
                        try:
                            target.parent.rmdir()
                        except OSError:
                            pass
                    refused_logs.append(f"{relative}: {error}")
                    continue
                if not log_reused:
                    staged.append(target)
                total += log_size
                fetched += not log_reused
                reused += log_reused
                # Digest what arrived so the receipt can name it, and check it
                # against nothing, because there is nothing to check it against.
                serving_logs.append((relative, _sha256_of(target)))
                continue
            data_size, was_reused = _fetch_or_compare(reader, prefix + relative, target)
            if not was_reused:
                staged.append(target)
            total += data_size
            fetched += not was_reused
            reused += was_reused
            name = PurePosixPath(relative).name
            if relative == RUN_FILE:
                tree.read_run()  # self-hash, schema, and run id, or a ContractError
            elif name in _MANIFEST_NAMES:
                manifest = _fetched_manifest(tree, relative)
                manifests[relative] = manifest
                for entry in manifest["artifacts"]:
                    expected[entry["relative_path"]] = entry["sha256"]
            elif "/artifacts/" in relative:
                # Manifests sort before artifacts (see `ordered` above), so every
                # manifest that exists on the volume is already in `expected`.
                # An artifact this loop cannot yet place is either the last write
                # of a stage that never reached `finish()` -- resolved below,
                # through the tree's own envelope reader, never refused outright
                # -- or genuinely orphaned, which the resolution pass still refuses.
                unresolved[relative] = _sha256_of(target)
            elif "/blobs/sha256/" in relative or relative.startswith(f"{RECEIPTS_DIR}/"):
                digest = _sha256_of(target)
                if PurePosixPath(relative).stem != digest:
                    raise FetchRunRefusal(
                        f"{relative} is content-addressed but its bytes digest to {digest}; "
                        "the object on the volume is not the one its name claims."
                    )
            else:
                # A rebuildable index or the derived Recensor receipt: readable
                # JSON, digested into the receipt, verified by the tree's own
                # readers when a verb next opens it.
                try:
                    size = target.stat().st_size
                    if size > MAX_RECORD_READ_BYTES:
                        raise FetchRunRefusal(
                            f"{relative} is {size} bytes, above the "
                            f"{MAX_RECORD_READ_BYTES}-byte limit for a JSON record; it is "
                            "not readable here as one."
                        )
                    json.loads(target.read_bytes().decode("utf-8"))
                except (UnicodeDecodeError, ValueError, RecursionError) as error:
                    # `RecursionError` because nesting, not length, is what breaks
                    # the JSON parser -- a hostile volume can otherwise defeat this
                    # named refusal with a few thousand brackets that fit easily
                    # inside any size bound (G13).
                    raise FetchRunRefusal(f"{relative} is not readable JSON: {error}") from error
        # An artifact with no manifest entry is not necessarily orphaned: the
        # stage that wrote it last may never have reached `finish()` (a crash,
        # an EXIT_FATAL, a SIGKILL, or the pod timer destroying the pod at the
        # hard deadline all skip the manifest write). Resolve each such
        # artifact against the manifest its own writing directory's stage
        # would derive -- the same envelope, run, and path checks
        # `build_manifest` always applies -- rather than refusing the whole
        # tree for a manifest a genuine crash never got to write.
        # Every artifact a *stored* manifest recorded, taken before the derived
        # pass below adds any. `unresolved` holds every artifact that arrived,
        # because the digest comparison at the end checks them all; only the
        # ones missing from this set were verified by envelope alone, and only
        # those belong in the receipt's `envelope_only_artifacts`. Reporting
        # the whole set named manifest-covered artifacts as unmanifested --
        # a false statement about what was measured (GOVERNANCE 10) and
        # useless for telling a crashed stage's artifacts from the rest.
        manifest_recorded = set(expected)
        manifested_stage_names = {manifest["stage"] for manifest in manifests.values()}
        unmanifested_stages: set[str] = set()
        if unresolved:
            directories_needed = {relative.partition("/artifacts/")[0] for relative in unresolved}
            for directory in sorted(directories_needed):
                candidates = sorted(
                    stage
                    for stage, stage_directory in WRITING_DIRECTORIES.items()
                    if stage_directory == directory and stage not in manifested_stage_names
                )
                for stage in candidates:
                    derived = tree.build_manifest(stage, verify_inputs=False)
                    if not derived["artifacts"]:
                        continue
                    unmanifested_stages.add(stage)
                    for entry in derived["artifacts"]:
                        expected.setdefault(entry["relative_path"], entry["sha256"])
            for relative, digest in unresolved.items():
                recorded = expected.get(relative)
                if recorded is None:
                    raise FetchRunRefusal(
                        f"{relative} arrived from the volume but no stage manifest -- stored "
                        "or derived from its own envelope -- records it; an artifact nobody "
                        "inventoried is not evidence."
                    )
                if recorded != digest:
                    raise FetchRunRefusal(
                        f"{relative} digests to {digest}, not the {recorded} its stage "
                        "manifest records; the fetched tree does not reconcile with itself."
                    )
        stages: list[str] = []
        for relative, manifest in manifests.items():
            stage = manifest["stage"]
            rebuilt = tree.build_manifest(stage, verify_inputs=False)
            if (
                rebuilt["artifacts"] != manifest["artifacts"]
                or rebuilt["blobs"] != manifest["blobs"]
            ):
                raise FetchRunRefusal(
                    f"{relative} does not match the manifest the fetched artifacts rebuild "
                    f"for stage {stage!r}; the tree on the volume and the tree here disagree."
                )
            stages.append(stage)
    except BaseException:
        for path in staged:
            path.unlink(missing_ok=True)
        # And the directories those files were created in, bottom-up. A refused
        # fetch that left an empty run-tree skeleton behind put a directory
        # structure on disk that no completed fetch ever wrote and the partial
        # receipt does not mention; a later `fetch_run` into the same root then
        # walks it. `rmdir` removes only what is already empty, so a directory
        # holding a file an earlier, verified fetch left is untouched.
        for directory in sorted(
            {parent for path in staged for parent in path.parents if root in parent.parents},
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            try:
                directory.rmdir()
            except OSError:
                pass
        try:
            root.rmdir()
        except OSError:
            pass
        raise
    return FetchRunOutcome(
        fetched,
        reused,
        total,
        tuple(sorted(stages)),
        tuple(excluded),
        tuple(sorted(unmanifested_stages)),
        tuple(sorted(name for name in unresolved if name not in manifest_recorded)),
        tuple(sorted(serving_logs)),
        tuple(sorted(refused_logs)),
    )


def _is_serving_log(relative: str) -> bool:
    """True for `<writing directory>/serving-logs/<name>`, the served engine's own log.

    A classification, not a boundary: `inventory_scope()` has already refused
    anything that is not under a writing directory's serving-log prefix by the
    time this is asked.

    The last component must be a name. An S3 listing can carry a zero-byte
    directory marker whose key ends in `/`, and calling that a log would fetch
    it to the directory's own path; left unclassified it falls through to the
    JSON arm and refuses by name, which is the right answer for a key that is
    not an object anyone wrote.
    """

    parts = relative.split("/")
    return len(parts) > 2 and parts[1] == SERVING_LOGS_DIR and bool(parts[-1])


@dataclass(frozen=True, slots=True)
class FetchEvidenceOutcome:
    """What the evidence pass brought home, and what it did not.

    The run tree is not the whole record of a run that billed a card: the
    launch's PREFLIGHT evidence -- the golden page, the serving logs, the
    published serving receipts and launch audits -- lives beside it on the
    volume and says which chairs were preflighted, against which catalogue
    digests, at what measured tier. The volume is destroyed under the retention
    policy, so that provenance has to travel with the record (GOVERNANCE 6),
    and what could *not* be brought home has to be named rather than left
    silent (GOVERNANCE 2).
    """

    fetched: int
    reused: int
    bytes: int
    objects: tuple[tuple[str, str], ...] = ()
    empty_prefixes: tuple[str, ...] = ()
    refusals: tuple[str, ...] = ()


_CONTENT_ADDRESSED_PARENT = "sha256"


def _fetch_evidence(
    reader: RunObjectReader,
    prefixes: tuple[str, ...],
    keys: tuple[str, ...],
    destination: Path,
) -> FetchEvidenceOutcome:
    """Bring the launch's evidence home beside the run tree, each object digested.

    Unlike the run tree, this material carries no manifest to reconcile against:
    the receipts and audits under it are content-addressed and are checked
    against their own names, and everything else is recorded in the fetch-run
    receipt with the digest of the bytes that arrived, which is what lets a
    later reader say the local copy is the one that was fetched.

    Nothing here refuses the fetch as a whole. The run tree has already been
    verified when this runs, and losing that verified result because a log file
    could not be read would be the wrong trade; every object that did not come
    home is named in the returned refusals and lands in the receipt instead.
    """

    fetched = reused = total = 0
    objects: list[tuple[str, str]] = []
    empty: list[str] = []
    refusals: list[str] = []
    wanted: list[str] = []
    for prefix in prefixes:
        listing_prefix = prefix if prefix.endswith("/") else f"{prefix}/"
        try:
            listed = reader.list_keys(listing_prefix)
        except Exception as error:  # noqa: BLE001 -- a failed listing is recorded, never raised here
            refusals.append(f"{listing_prefix}: the volume listing failed: {error}")
            continue
        if not listed:
            empty.append(listing_prefix)
        wanted.extend(listed)
    wanted.extend(keys)
    ordered = sorted(dict.fromkeys(wanted))
    if len(ordered) > MAX_FETCH_EVIDENCE_OBJECTS:
        refusals.append(
            f"{len(ordered)} objects are stored under the evidence prefixes, past the "
            f"{MAX_FETCH_EVIDENCE_OBJECTS}-object bound; none were fetched"
        )
        return FetchEvidenceOutcome(0, 0, 0, (), tuple(empty), tuple(refusals))
    for key in ordered:
        if not key or key.startswith("/") or ".." in key.split("/"):
            refusals.append(f"{key!r} is not a path this verb will write under {destination}")
            continue
        target = destination / key
        existed = target.exists()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            size, was_reused = _fetch_or_compare(reader, key, target)
            digest = _sha256_of(target)
            name = PurePosixPath(key)
            if name.parent.name == _CONTENT_ADDRESSED_PARENT and name.stem != digest:
                raise FetchRunRefusal(
                    f"{key} is content-addressed but its bytes digest to {digest}; the object "
                    "on the volume is not the one its name claims."
                )
        except Exception as error:  # noqa: BLE001 -- recorded per object, never fatal to the run tree
            if not existed:
                target.unlink(missing_ok=True)
            refusals.append(f"{key}: {error}")
            continue
        objects.append((key, digest))
        total += size
        fetched += not was_reused
        reused += was_reused
    return FetchEvidenceOutcome(
        fetched, reused, total, tuple(objects), tuple(empty), tuple(refusals)
    )


def _fetch_or_compare(reader: RunObjectReader, key: str, target: Path) -> tuple[int, bool]:
    """Fetch into `target`, or -- if it exists -- fetch beside it and compare, never replace."""

    if target.is_symlink():
        raise FetchRunRefusal(f"{target} is a symbolic link; a run tree holds no aliases.")
    if not target.exists():
        return reader.fetch_to(key, target, max_bytes=MAX_FETCH_OBJECT_BYTES), False
    if not target.is_file():
        raise FetchRunRefusal(f"{target} exists and is not a regular file.")
    staging = target.with_name(f".{target.name}.fetch-{secrets.token_hex(4)}")
    try:
        size = reader.fetch_to(key, staging, max_bytes=MAX_FETCH_OBJECT_BYTES)
        if _sha256_of(staging) != _sha256_of(target):
            raise FetchRunRefusal(
                f"{target} already exists with different bytes than the volume holds for "
                f"{key!r}; the local run was not overwritten."
            )
    finally:
        staging.unlink(missing_ok=True)
    return size, True


def _fetched_manifest(tree: RunTree, relative: str) -> dict[str, Any]:
    try:
        # The record ceiling by name, not the tree's blob-sized default: this
        # file came off a volume and is about to be parsed, and `json.loads`
        # costs several times the bytes again in parsed objects, so a manifest
        # the size an *image* may legitimately be is already too large to read
        # here (G13). Without the explicit bound, the ceiling that does apply
        # sits above `MAX_FETCH_OBJECT_BYTES` and can never fire on this path.
        manifest = json.loads(
            tree.read_bytes(relative, max_bytes=MAX_RECORD_READ_BYTES).decode("utf-8")
        )
    except (UnicodeDecodeError, ValueError, RecursionError, OSError, SchemaRefusal) as error:
        # `RecursionError` because nesting, not length, is what breaks the JSON
        # parser -- a hostile `manifest.json` (~10k nesting suffices) is otherwise
        # a crash on the fetch-run path rather than this named refusal; and
        # `SchemaRefusal` because that is what the ceiling above raises (G13).
        raise FetchRunRefusal(f"{relative} is not a readable manifest: {error}") from error
    if (
        not isinstance(manifest, dict)
        or manifest.get("run_id") != tree.run_id
        or not isinstance(manifest.get("stage"), str)
        or not isinstance(manifest.get("artifacts"), list)
        or not isinstance(manifest.get("blobs"), list)
    ):
        raise FetchRunRefusal(f"{relative} is not a manifest of run {tree.run_id!r}.")
    if relative != tree.manifest_path(manifest["stage"]):
        raise FetchRunRefusal(
            f"{relative} names stage {manifest['stage']!r}, whose manifest lives at "
            f"{tree.manifest_path(manifest['stage'])!r}."
        )
    for entry in manifest["artifacts"]:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("relative_path"), str)
            or not isinstance(entry.get("sha256"), str)
        ):
            raise FetchRunRefusal(f"{relative} lists an artifact with no path and digest.")
    return manifest


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_COPY_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _display_usd(amount: Decimal) -> str:
    """Round a dollar amount to two places for the screen a person reads.

    Display only — the stored record keeps the exact `Decimal`, and the paid
    confirmation phrase is derived from the two hourly rates shown just above
    this line, never from this total, so rounding it here cannot weaken what
    the typed confirmation actually authorizes.
    """

    return str(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _human_duration(seconds: int) -> str:
    """Pair an exact limit with the duration a person can recognize quickly."""

    if seconds % 3600 == 0:
        count = seconds // 3600
        unit = "hour" if count == 1 else "hours"
        return f"{count} {unit} ({seconds} seconds)"
    if seconds % 60 == 0:
        count = seconds // 60
        unit = "minute" if count == 1 else "minutes"
        return f"{count} {unit} ({seconds} seconds)"
    return f"{seconds} seconds"


def _declared_work(workspace: Path) -> tuple[list[str], list[str], bool]:
    """Name the fixture's actual declared work rather than invent a progress number.

    The third element is `False` exactly when the declared fixture could not
    be read at all, so the caller can say so — a generic placeholder shown
    without comment reads as the real page/act list, which it is not.
    """

    try:
        fixture = load_fixture(str(workspace / "proof"))
        pages = [f"page {page['ordinal']}" for page in fixture["page"]]
        acts = [f"act {act['key']}" for act in fixture["act"]]
        if pages and acts and all(isinstance(value, str) for value in pages + acts):
            return pages, acts, True
    except Exception:
        pass
    return ["the declared pages"], ["the declared acts"], False
