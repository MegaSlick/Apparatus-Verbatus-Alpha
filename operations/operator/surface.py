"""The safe, plain-language façade for the operator's seven words.

This module deliberately has no live provider or S3 adapter.  It joins the
existing fake-first seams into an offline rehearsal, persists what the operator
confirmed before an action, and leaves the provider-facing machinery behind the
surface.
"""

from __future__ import annotations

import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Protocol

from common.chairs.config import load_models_toml
from common.contracts.identities import artifact_id
from common.contracts.stages import ARMARIUM
from common.runtree.store import RunTree
from common.stage import load_fixture
from operations.pod.arming import ControllerArming, ControllerReadiness
from operations.pod.bootstrap import (
    BootstrapJournal,
    Bootstrapper,
    BootstrapPlan,
    BootstrapStep,
    BootstrapStepFailure,
)
from operations.pod.fake_provider import FakeProvider
from operations.pod.launch import LaunchResult, LaunchState, PodRuntime
from operations.pod.lease import LeaseStore, PodLease
from operations.pod.models import (
    PodCreateRequest,
    PodEstimate,
    PodRecord,
    PodRuntimeContract,
    ProviderFailure,
    ProviderTimeout,
    require_utc,
)
from operations.pod.preflight import (
    CacheMismatch,
    GpuProfile,
    PreflightRunner,
    SmokeResult,
    UtilizationSample,
    load_placement_table,
)
from operations.pod.shutdown import CloseReport, VerifiedShutdown
from operations.pod.spend import SpendPolicy, load_spend_policy
from operations.pod.transfer import ChecksummedTransfer, TransferFailure, TransferTarget
from operations.submit import submit as submission_door

from . import notify_bridge
from .errors import ErrorCode, OperatorError
from .errors import sanitize_detail as _detail
from .fakes import LocalFixtureObjectStore
from .notify_bridge import Notifier
from .records import DescriptorStore, ReceiptStore, RecordError, sha256_file, utc_stamp
from .volume_cost import volume_cost_lines
from .volume_s3 import S3VolumeTarget, VolumeSpec

UTC = timezone.utc
OPERATOR_CLOSE_PREFIX = "CLOSE"
DEFAULT_FIXTURE = "synthetic-two-page-v0"


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
        del action, request, store, owner_token, policy
        observed = self.now()
        stamp = utc_stamp(observed)
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
                    "report_path": "/workspace/private/fixture-pod-timer-report.json",
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
        provider: FakeProvider | None = None,
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
        self.present = present or print
        candidate = provider or FakeProvider(now=self.now)
        if not isinstance(candidate, FakeProvider):
            raise OperatorError(ErrorCode.LIVE_PROVIDER_BLOCKED)
        self.provider = candidate
        self.faults = faults or Faults()
        self.runner = runner or _run_program
        # Default silent: nothing this surface does reaches a phone unless the
        # caller supplied a notifier, so no test and no first rehearsal can send
        # a ping nobody asked for.
        self.notifier: Notifier = notifier or notify_bridge.silent
        # Real time by default. A drill that needs a close to give up quickly
        # injects a fast clock here rather than shortening the shipped deadline.
        self.monotonic = monotonic or time.monotonic
        self.sleeper = sleeper or time.sleep
        self.receipts = ReceiptStore(self.state_root, now=self.now)
        self.descriptor = DescriptorStore(self.state_root)

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
        if result.state is not LaunchState.PREVIEW or result.preview is None:
            self._record_failure("launch", result.state.value, result.detail)
            if adopt_pod_id is not None:
                raise OperatorError(ErrorCode.ADOPTION_REFUSED, detail=result.detail)
            raise self._launch_error(result)
        if not result.preview.assessment.allowed:
            self._record_failure("launch", "refused-ceiling", result.detail)
            if adopt_pod_id is not None:
                code = ErrorCode.ADOPTION_REFUSED
            else:
                code = (
                    ErrorCode.SPEND_POLICY_REQUIRED
                    if not policy.configured
                    else ErrorCode.PAID_ACTION_REFUSED
                )
            raise OperatorError(code, detail=result.detail)
        return prepared

    def launch(self, prepared: PreparedLaunch, confirmation: str | None) -> LaunchResult:
        """Record a confirmation first, then cross the fake provider's paid seam."""

        # Re-checked here, not only in prepare_launch(): two overlapping prepare
        # calls (two double-clicks, two terminal windows) can each pass the earlier
        # check before either confirms. Without this, the second confirmed launch
        # would create a second real pod that status/close could never reach again.
        self._refuse_if_active_pod()
        expected = prepared.confirmation_phrase
        if confirmation != expected:
            raise OperatorError(ErrorCode.CONFIRMATION_REQUIRED)
        confirmation_receipt = self._write_action(
            "launch-confirmation",
            {
                "summary": f"Paid {prepared.action} confirmation recorded before the provider call.",
                "action": prepared.action,
                "adopted_pod_id": prepared.adopted_pod_id,
                "request": _request_record(prepared.request),
                "preview": prepared.result.preview.to_record(),
                "confirmation": expected,
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
                    "confirmation_receipt": str(confirmation_receipt),
                },
                descriptor_action="launch",
            )
            del receipt
            if prepared.action == "adopt":
                raise OperatorError(ErrorCode.ADOPTION_REFUSED, detail=result.detail)
            raise self._launch_error(result)
        receipt = self._write_action(
            "launch",
            {
                "summary": (
                    "Fixture pod is created with both fixture safety timers recorded."
                    if prepared.action == "create"
                    else "Existing fixture pod is adopted with both fixture safety timers recorded."
                ),
                "state": result.state.value,
                "action": prepared.action,
                "pod": _pod_record(result.record),
                "request": _request_record(prepared.request),
                "confirmation_receipt": str(confirmation_receipt),
                "lease": str(result.lease_path) if result.lease_path is not None else None,
                "controller_arming": (
                    result.controller_arming.to_record()
                    if result.controller_arming is not None
                    else None
                ),
            },
            descriptor_action="active-launch",
            additional_descriptor_actions=("launch",),
        )
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
        approval_record: str | Path,
        policy_path: str | Path | None = None,
        volume: VolumeSpec | None = None,
    ) -> Path:
        """Run Spec 03's local door before transferring only a sealed manifest."""

        try:
            submission_door.submit(
                Path(source),
                Path(manifest_out),
                approval_record=Path(approval_record),
                policy_path=(
                    Path(policy_path)
                    if policy_path is not None
                    else submission_door.gate.DEFAULT_POLICY_PATH
                ),
            )
        except Exception as error:
            self._record_failure("upload", "submission-refused", str(error))
            raise OperatorError(ErrorCode.UPLOAD_REFUSED, detail=str(error)) from error
        return self.upload(source, sealed_manifest=manifest_out, volume=volume)

    def upload(
        self,
        source: str | Path,
        *,
        sealed_manifest: str | Path,
        prefix: str = "volume",
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
            manifest_sha256 = sha256_file(manifest_path)
        except OSError as error:
            raise OperatorError(
                ErrorCode.UPLOAD_MANIFEST_MISSING,
                detail="the sealed submission record could not be read",
            ) from error
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
            report = ChecksummedTransfer(
                source_root=source_path,
                submission_manifest=manifest_path,
                target=store,
                prefix=prefix,
                journal_path=self.state_root / "transfer" / f"{manifest_sha256}.json",
            ).resume()
            if sha256_file(manifest_path) != manifest_sha256:
                raise TransferFailure("sealed submission record changed during transfer")
        except (TransferFailure, OSError, ValueError) as error:
            receipt = self._write_action(
                "upload",
                {
                    "summary": "Upload is partial and can be resumed from its verified files.",
                    "state": "partial-transfer",
                    "source": str(source_path.resolve()),
                    "submission_manifest": str(manifest_path.resolve()),
                    "submission_manifest_sha256": manifest_sha256,
                    "detail": str(error),
                    "zero_gpu_hours": True,
                },
                descriptor_action="upload",
            )
            raise OperatorError(
                ErrorCode.UPLOAD_PARTIAL, detail=f"Saved receipt: {receipt}"
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
                "source": str(source_path.resolve()),
                "submission_manifest": str(manifest_path.resolve()),
                "submission_manifest_sha256": manifest_sha256,
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
            "bootstrap_journal": str(journal.path),
            "launch_receipt": str(launch_receipt) if launch_receipt is not None else None,
            "upload_receipt": str(upload_receipt) if upload_receipt is not None else None,
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

    # -- run ------------------------------------------------------------------

    def run(
        self, *, run_id: str, scenario: str = "happy", fixture: str = DEFAULT_FIXTURE
    ) -> RunOutcome:
        """Drive the current tree's actual fixture orchestrator, with resumable evidence."""

        run_root = self.state_root / "runs"
        pages, acts = _declared_work(self.workspace)
        prior_state = self._prior_run_state(run_id)
        if prior_state == "interrupted-recoverable":
            self.present(f"Resuming run {run_id}. Checking {', '.join(pages)}.")
        elif prior_state is not None:
            self.present(
                f"Run {run_id} already has saved state {prior_state}; checking its recorded pages again."
            )
        else:
            self.present(f"Run started. Checking {', '.join(pages)}.")
        self.present(f"Working next: {', '.join(acts)}.")
        self.present(
            "This rehearsal uses declared synthetic pages, not an uploaded real submission."
        )
        if self.faults.laptop_crash:
            self.faults.laptop_crash = False
            self._run_one_stage(run_root, run_id, scenario, "pipeline/1_exemplar/door.py")
            receipt = self._write_action(
                "run",
                {
                    "summary": "Run interrupted after the door recorded its page evidence; it can resume.",
                    "state": "interrupted-recoverable",
                    "run_root": str(run_root),
                    "run_id": run_id,
                    "scenario": scenario,
                    "fixture": fixture,
                    "last_observed_work": "The fixture pages reached the door.",
                },
                descriptor_action="run",
            )
            self.present(
                "The laptop-crash drill interrupted after the fixture pages reached the door."
            )
            raise OperatorError(ErrorCode.RUN_INTERRUPTED, detail=f"Saved run receipt: {receipt}")
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
        completed = self.runner(
            command, cwd=self.workspace, capture_output=True, text=True, check=False
        )
        if completed.returncode not in {0, 3}:
            receipt = self._write_action(
                "run",
                {
                    "summary": "Run ended before its Armarium record was available.",
                    "state": "failed",
                    "run_root": str(run_root),
                    "run_id": run_id,
                    "scenario": scenario,
                    "fixture": fixture,
                    "detail": _detail(completed.stderr or completed.stdout),
                },
                descriptor_action="run",
            )
            raise OperatorError(ErrorCode.RUN_FAILED, detail=f"Saved run receipt: {receipt}")
        try:
            export_payload = self._armarium_export(run_root, run_id)
        except Exception as error:
            self._record_failure("run", "armarium-record-unreadable", str(error))
            raise OperatorError(ErrorCode.RUN_FAILED, detail=str(error)) from error
        aggregate = export_payload["aggregate"]
        state = str(aggregate["status"])
        receipt = self._write_action(
            "run",
            {
                "summary": f"Run finished with recorded state: {state}.",
                "state": state,
                "run_root": str(run_root),
                "run_id": run_id,
                "scenario": scenario,
                "fixture": fixture,
                "armarium_export": _armarium_reference(run_root, run_id),
            },
            descriptor_action="run",
        )
        page_records = export_payload.get("pages", [])
        expected = export_payload.get("expected_acts")
        self.present(
            f"Pages accounted for: {', '.join(pages)} ({len(page_records)} total). "
            f"Acts accounted for: {', '.join(acts)} ({expected} total)."
        )
        if state == "complete":
            self.present("Run complete. The named pages and acts reached the Armarium record.")
            self.present(f"Saved run receipt: {receipt}")
            self._notify(
                "milestone",
                f"Verbatus run {run_id} finished: {len(page_records)} page(s), "
                f"{expected} act(s) accounted for.",
            )
            return RunOutcome(state, run_root, run_id, aggregate, export_payload)
        reasons = aggregate.get("reasons", [])
        self.present("Run is held. It was not called complete.")
        for reason in reasons:
            self.present(f"Hold reason: {reason}")
        # A hold is the pipeline asking a person to decide, which is exactly the
        # `decision` moment — sent when the hold happens, not when someone
        # eventually looks.
        self._notify(
            "decision",
            f"Verbatus run {run_id} is held and needs a decision: "
            f"{'; '.join(str(reason) for reason in reasons) or 'no reason recorded'}",
        )
        raise OperatorError(ErrorCode.RUN_FAILED, detail=f"Saved run receipt: {receipt}")

    # -- export ---------------------------------------------------------------

    def export(self, *, run_id: str | None = None) -> Path:
        """Make a local evidence bundle from the base-tree Armarium artifact."""

        run_receipt = self._descriptor_receipt("run")
        if run_receipt is None:
            raise OperatorError(ErrorCode.EXPORT_MISSING)
        run_record = self.receipts.read(run_receipt)["payload"]
        recorded_id = run_record.get("run_id")
        if not isinstance(recorded_id, str):
            raise OperatorError(ErrorCode.EXPORT_MISSING)
        if run_id is not None and run_id != recorded_id:
            raise OperatorError(
                ErrorCode.EXPORT_MISSING,
                detail="that run is not the run recorded for this operator session",
            )
        run_root = Path(str(run_record["run_root"]))
        try:
            export_payload = self._armarium_export(run_root, recorded_id)
        except Exception as error:
            raise OperatorError(ErrorCode.EXPORT_MISSING, detail=str(error)) from error
        destination = self.state_root / "exports" / f"{recorded_id}-armarium-base.zip"
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            self._write_base_armarium_bundle(run_root, recorded_id, destination)
        except (OSError, zipfile.BadZipFile) as error:
            self._record_failure("export", "local-copy-failed", str(error))
            raise OperatorError(ErrorCode.EXPORT_FAILED, detail=str(error)) from error
        table = reconciliation_table(export_payload)
        for line in table:
            self.present(line)
        receipt = self._write_action(
            "export",
            {
                "summary": "Base Armarium evidence bundle copied locally.",
                "state": "complete",
                "run_id": recorded_id,
                "bundle": str(destination),
                "sha256": sha256_file(destination),
                "reconciliation": table,
                "assumption": "Spec 11 is not in this tree; this is a copy of the base Armarium evidence, not a Spec 11 product bundle.",
            },
            descriptor_action="export",
        )
        self.present(f"Local Armarium evidence bundle: {destination}")
        self.present("This is base Armarium evidence, not a Spec 11 product bundle.")
        self.present(f"Saved export receipt: {receipt}")
        self._notify("milestone", f"Verbatus export for run {recorded_id} landed: {destination}")
        return destination

    # -- close ---------------------------------------------------------------

    def prepare_close(self, *, pod_id: str | None = None) -> PreparedClose:
        """Resolve the recorded pod and show the close notice — before any confirmation.

        Mirrors prepare_launch/launch: the notice a person reads has to be shown
        before they are asked to type anything back, never after.
        """

        launch_receipt = self._active_launch_receipt()
        if launch_receipt is None:
            raise OperatorError(ErrorCode.CLOSE_NOTHING)
        launch = self.receipts.read(launch_receipt)["payload"]
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
            prior = self.receipts.read(close_receipt)["payload"]
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
            # The pod's own billing note is what _lease_for_close's error carries;
            # the volume's ongoing price is a fact this surface already has in
            # hand (record.estimate) and must still say, per volume_cost.py's own
            # "printed on every close, verified or not" contract.
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
        report = self._shutdown(self._close_policy()).close(record, reason="manual operator close")
        try:
            lease_store.record_close(
                owner_token=lease.owner_token,
                report=report,
                now=self.now(),
            )
        except Exception as error:
            receipt = self._write_action(
                "close",
                {
                    "summary": "Close is UNVERIFIED because the safety lease could not record the provider evidence.",
                    "pod_id": record.pod_id,
                    "confirmation_receipt": str(confirmation_receipt),
                    "close_report": report.to_record(),
                    "lease_reconciled": False,
                    "lease": str(lease_store.path),
                    "lease_record_error": _detail(str(error)),
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
                "confirmation_receipt": str(confirmation_receipt),
                "close_report": report.to_record(),
                "lease_reconciled": True,
                "lease": str(lease_store.path),
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
        """Read the explicit descriptor and immutable receipts only: no writes or provider calls."""

        try:
            descriptor = self.descriptor.load()
        except RecordError as error:
            raise OperatorError(ErrorCode.STATUS_UNREADABLE, detail=str(error)) from error
        if descriptor is None or not descriptor["actions"]:
            raise OperatorError(ErrorCode.STATUS_EMPTY)
        lines = ["Saved operator records (read-only; no new provider check was made):"]
        unreadable: list[str] = []
        for action, path_texts in sorted(descriptor["history"].items()):
            if action == "active-launch":
                continue
            for number, path_text in enumerate(path_texts, start=1):
                try:
                    record = self.receipts.read(Path(path_text))
                    payload = record["payload"]
                    summary = payload.get("summary")
                    lines.append(
                        f"- {action} record {number}: "
                        + (summary if isinstance(summary, str) else "saved record")
                    )
                    lines.extend(_status_projection(action, payload))
                    if action == "upload":
                        lines.extend(_status_manifest_projection(payload))
                except RecordError as error:
                    label = f"{action} record {number}"
                    unreadable.append(f"{label}: {error}")
                    lines.append(f"- {label}: UNREADABLE; it was not treated as success.")
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
        )

    def _close_policy(self) -> SpendPolicy | None:
        """The reviewed policy for close timing, or `None` — never a refusal.

        `launch` must refuse without a reviewed policy, because it spends.
        `close` must not: an unreadable or unconfigured policy is no reason to
        leave a pod running, and the ceiling checks a policy carries have nothing
        to say about stopping one.
        """

        try:
            return load_spend_policy(self.workspace / "config" / "spend.toml")
        except Exception:
            return None

    def _shutdown(self, policy: SpendPolicy | None) -> VerifiedShutdown:
        """Close timing comes from the reviewed policy, never from a constant here.

        A reviewed `shutdown_deadline_seconds` and `shutdown_poll_interval_seconds`
        are what the operator's own policy says a close may take. Where no
        configured policy is in hand — `close` is deliberately runnable without
        one, because closing is always the safe direction — `VerifiedShutdown`'s
        own operational defaults apply. Speeding either up belongs in a test's
        injected clock, not in the shipped surface: a close that gives up in
        milliseconds reports UNVERIFIED every time, which is loud, wrong, and the
        fastest way to teach someone to ignore the one message that matters.
        """

        timings: dict[str, float] = {}
        if policy is not None and policy.configured:
            timings = {
                "timeout_seconds": policy.shutdown_deadline_seconds,
                "poll_seconds": policy.shutdown_poll_interval_seconds,
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
        self.present(f"- Pod hourly price: ${assessment.estimate.pod_hourly_usd}")
        self.present(f"- Attached-volume hourly price: ${assessment.estimate.volume_hourly_usd}")
        self.present(
            f"- Combined estimated cost through the hard lifetime: ${assessment.estimated_total_cost_usd}"
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
        else:
            self.present("- Spending ceiling: not configured")
        if assessment.reasons:
            for reason in assessment.reasons:
                self.present(f"- Check: {reason}")
        if assessment.allowed:
            self.present(
                f"Type exactly {prepared.confirmation_phrase!r} to continue with this paid action."
            )

    def _launch_error(self, result: LaunchResult) -> OperatorError:
        detail = result.detail
        if "timeout" in detail.lower():
            return OperatorError(ErrorCode.PROVIDER_TIMEOUT, detail=detail)
        if result.state is LaunchState.REFUSED_CONFIRMATION:
            return OperatorError(ErrorCode.CONFIRMATION_REQUIRED, detail=detail)
        if result.state is LaunchState.REFUSED_REPREVIEW:
            return OperatorError(ErrorCode.PRICE_CHANGED, detail=detail)
        if result.state is LaunchState.REFUSED_CEILING:
            return OperatorError(ErrorCode.PAID_ACTION_REFUSED, detail=detail)
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
        }:
            return OperatorError(ErrorCode.LAUNCH_UNRESOLVED, detail=detail)
        if result.state is LaunchState.PROVIDER_FAILURE:
            if result.lease_path is not None:
                return OperatorError(ErrorCode.LAUNCH_UNRESOLVED, detail=detail)
            return OperatorError(ErrorCode.PROVIDER_ERROR, detail=detail)
        return OperatorError(ErrorCode.SAFETY_CHECK_FAILED, detail=detail)

    def _inject_provider_preview_fault(self) -> None:
        if self.faults.provider_timeout:
            self.faults.provider_timeout = False
            self.provider.inject_failure("estimate", ProviderTimeout("injected provider timeout"))
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

        descriptor = self.descriptor.load()
        if descriptor is None:
            return None
        for action in actions:
            value = descriptor["actions"].get(action)
            if isinstance(value, str):
                return Path(value)
        return None

    def _active_launch_receipt(self) -> Path | None:
        """The active-launch receipt if one is recorded, else the plain launch receipt."""

        return self._descriptor_receipt("active-launch", "launch")

    def _refuse_if_active_pod(self) -> None:
        """Keep a recorded open cost path visible until its own verified close."""

        try:
            launch_receipt = self._active_launch_receipt()
            if launch_receipt is None:
                return
            launch = self.receipts.read(launch_receipt)["payload"]
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
            raise OperatorError(
                ErrorCode.SAFETY_CHECK_FAILED,
                detail="the recorded active fixture pod could not be checked safely",
            ) from error
        if lease.phase != "closed-verified":
            raise OperatorError(
                ErrorCode.ACTIVE_POD_REQUIRES_CLOSE,
                detail="recorded fixture pod " + record.pod_id + " has lease state " + lease.phase,
            )

    def _prior_run_state(self, run_id: str) -> str | None:
        """Use the explicitly named prior run receipt, never a search for a convenient one."""

        receipt = self._descriptor_receipt("run")
        if receipt is None:
            return None
        payload = self.receipts.read(receipt)["payload"]
        if payload.get("run_id") != run_id:
            return None
        state = payload.get("state")
        return state if isinstance(state, str) else None

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

    def _record_failure(self, action: str, state: str, detail: str) -> None:
        try:
            self._write_action(
                action,
                {
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

    def _run_one_stage(self, run_root: Path, run_id: str, scenario: str, program: str) -> None:
        command = [
            sys.executable,
            str(self.workspace / program),
            "--run-root",
            str(run_root),
            "--run-id",
            run_id,
            "--scenario",
            scenario,
        ]
        completed = self.runner(
            command, cwd=self.workspace, capture_output=True, text=True, check=False
        )
        if completed.returncode not in {0, 3}:
            raise OperatorError(ErrorCode.RUN_FAILED, detail=completed.stderr or completed.stdout)

    def _armarium_export(self, run_root: Path, run_id: str) -> dict[str, Any]:
        tree = RunTree(run_root, run_id)
        record = tree.read_artifact(
            ARMARIUM, "export", artifact_id(ARMARIUM, "export", "export", None)
        )
        payload = record.get("payload")
        if not isinstance(payload, dict) or not isinstance(payload.get("aggregate"), dict):
            raise ValueError("Armarium export record has no usable aggregate")
        return payload

    def _write_base_armarium_bundle(self, run_root: Path, run_id: str, destination: Path) -> None:
        tree = RunTree(run_root, run_id)
        source = tree.root
        selected = [source / "run.json", source / "7_armarium"]
        temporary = destination.with_name(f".{destination.name}.tmp")
        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                for entry in selected:
                    if entry.is_file():
                        bundle.write(entry, arcname=f"{run_id}/{entry.relative_to(source)}")
                    elif entry.is_dir():
                        for member in sorted(entry.rglob("*")):
                            if member.is_file() and not member.is_symlink():
                                bundle.write(
                                    member, arcname=f"{run_id}/{member.relative_to(source)}"
                                )
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)

    def _provider_for_record(self, launch: dict[str, Any], record: PodRecord) -> FakeProvider:
        """Recreate the one fake pod when a later CLI process performs close."""

        if record.pod_id in self.provider.pods:
            return self.provider
        request_raw = launch.get("request")
        if not isinstance(request_raw, dict):
            raise OperatorError(
                ErrorCode.CLOSE_NOTHING, detail="the launch receipt has no exact request"
            )
        request = _request_from_record(request_raw)
        recreated = FakeProvider(now=lambda: record.created_at)
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
            lease_path = Path(lease_text).resolve()
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
                f"${report.captured_cost_usd}."
            )
        else:
            self.present("No captured-cost line was available; this close remains unverified.")

    def _notify(self, event: str, message: str) -> None:
        """One standing moment, reported honestly and never able to fail a verb."""

        if self.notifier is notify_bridge.silent:
            # Nobody asked for a phone message, so there is nothing to report
            # about one. Every attempted send is reported, delivered or not.
            return
        try:
            outcome = self.notifier(event, message)
        except Exception as error:  # a broken notifier is not a broken run
            outcome = notify_bridge.NotifyOutcome(
                True, False, f"the notifier raised: {type(error).__name__}"
            )
        self.present(outcome.line())

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
    delivered = export_payload.get("delivered", [])
    review = export_payload.get("review", [])
    pages = export_payload.get("pages", [])
    expected = export_payload.get("expected_acts", "unknown")
    rows = [
        "Reconciliation from the recorded Armarium export:",
        "| Recorded item | Count or state |",
        "| --- | --- |",
        f"| Submitted pages accounted for | {len(pages)} |",
        f"| Expected acts | {expected} |",
        f"| Delivered acts | {len(delivered)} |",
        f"| Acts held for review | {len(review)} |",
        f"| Recorded run state | {aggregate.get('status', 'unknown')} |",
    ]
    reasons = aggregate.get("reasons", [])
    if isinstance(reasons, list):
        rows.extend(f"Recorded reason: {reason}" for reason in reasons)
    return rows


def _status_projection(action: str, payload: dict[str, Any]) -> list[str]:
    """Present selected already-recorded ledger facts without deriving a new truth."""

    lines: list[str] = []
    if action == "boot":
        report = payload.get("report")
        if isinstance(report, dict) and isinstance(report.get("color"), str):
            lines.append(f"  Saved boot report: {report['color'].upper()}.")
            remediation = report.get("remediation")
            if report["color"] != "green" and isinstance(remediation, str) and remediation:
                lines.append(f"  Saved boot next step: {remediation}")
    elif action == "upload" and payload.get("zero_gpu_hours") is True:
        lines.append("  Saved upload statement: zero GPU-hours were used.")
    elif action == "run":
        state = payload.get("state")
        if isinstance(state, str):
            lines.append(f"  Saved run state: {state}.")
        observed = payload.get("last_observed_work")
        if isinstance(observed, str):
            lines.append(f"  Saved last recorded work: {observed}")
    elif action == "export":
        table = payload.get("reconciliation")
        if isinstance(table, list):
            lines.extend(f"  {line}" for line in table if isinstance(line, str))
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
                lines.append("  Saved charges captured through {}: ${}.".format(cutoff, total))
        if state != "verified" and isinstance(state, str):
            lines.append(f"  Saved close state: {state.upper()}.")
        if isinstance(volume, dict) and isinstance(volume.get("ongoing_hourly_usd"), str):
            lines.append(
                "  Saved retained-volume price: $" + volume["ongoing_hourly_usd"] + " per hour."
            )
    return lines


def _status_manifest_projection(payload: dict[str, Any]) -> list[str]:
    """Read only the exact sealed manifest bound into the upload receipt."""

    path_text = payload.get("submission_manifest")
    recorded_sha256 = payload.get("submission_manifest_sha256")
    if path_text is None and recorded_sha256 is None:
        return []
    if not isinstance(path_text, str) or not isinstance(recorded_sha256, str):
        raise RecordError("saved upload record does not bind its submission record digest")
    try:
        path = Path(path_text)
        if sha256_file(path) != recorded_sha256:
            raise ValueError("saved submission record no longer matches the upload receipt")
        manifest = submission_door.load_manifest(path)
        files = manifest.get("files")
        if not isinstance(files, list):
            raise ValueError("saved submission record has no file list")
    except Exception as error:
        raise RecordError("saved submission record cannot be read safely") from error
    return [f"  Saved sealed submission record names {len(files)} file(s)."]


def _load_policy(path: str | Path) -> SpendPolicy:
    try:
        return load_spend_policy(path)
    except Exception as error:
        raise OperatorError(ErrorCode.SPEND_POLICY_REQUIRED, detail=str(error)) from error


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
        "template": request.template,
        "metadata": dict(request.metadata),
        "interruptible": request.interruptible,
        "recovery_only": request.recovery_only,
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
        if set(value) != required:
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
                template=template,
            ),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise OperatorError(
            ErrorCode.CLOSE_NOTHING, detail="the saved pod record is invalid"
        ) from error


def _repository_commit(workspace: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=workspace, text=True, capture_output=True, check=False
    )
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


def _armarium_reference(run_root: Path, run_id: str) -> str:
    tree = RunTree(run_root, run_id)
    relative = tree.artifact_path(
        ARMARIUM, "export", artifact_id(ARMARIUM, "export", "export", None)
    )
    return str(tree.resolve(relative))


def _run_program(*args, **kwargs) -> subprocess.CompletedProcess[str]:  # type: ignore[no-untyped-def]
    return subprocess.run(*args, **kwargs)


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


def _declared_work(workspace: Path) -> tuple[list[str], list[str]]:
    """Name the fixture's actual declared work rather than invent a progress number."""

    try:
        fixture = load_fixture(str(workspace / "proof"))
        pages = [f"page {page['ordinal']}" for page in fixture["page"]]
        acts = [f"act {act['key']}" for act in fixture["act"]]
        if pages and acts and all(isinstance(value, str) for value in pages + acts):
            return pages, acts
    except Exception:
        pass
    return ["the declared pages"], ["the declared acts"]
