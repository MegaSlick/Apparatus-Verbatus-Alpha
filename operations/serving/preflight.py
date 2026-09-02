"""Connect the serving lifecycle to the pod runtime's golden-page smoke seam.

``operations.pod.preflight`` owns measurement, placement, and the red/green
report.  This adapter supplies its ``SmokeReader`` protocol without creating a
second preflight loop: it starts the already named chair for the measured tier,
hands the owned endpoint to one smoke callable, records the published service
receipt beside that smoke evidence, and stops the exact child in ``finally``.

The callable is intentionally supplied by the stage/pod assembler.  It owns
the page-specific prompt and output-format rules, but must send its image through
``ServiceHandle.request_fixture_image`` so this module can bind the exact local
golden-page bytes to a real request. This module owns lifecycle and makes it
impossible for that callable to receive an unstarted service handle. There is no
fallback chair or nearest-tier behaviour here.
"""

from __future__ import annotations

import hashlib
import stat
from dataclasses import replace
from pathlib import Path
from typing import Callable, Mapping

from common.chairs.models import ChairIdentity, is_sha256
from operations.pod.preflight import (
    GpuProfile,
    PlacementRefusal,
    PlacementTable,
    PlacementTier,
    SmokeResult,
)

from .config import ServingProfile
from .errors import AdapterActivityError, ServiceStopError, ServingConfigurationError
from .manager import AdapterCalibration, ServiceHandle, ServingManager

SmokeCall = Callable[[ServiceHandle, ChairIdentity, Path, PlacementTier], SmokeResult]
CalibrationFor = Callable[[ChairIdentity, Path], AdapterCalibration | None]


def prepare_log_root(log_root: str | Path) -> Path:
    """Create and verify the exact log filesystem a launch will write into.

    Lives here so both production seams give the same guarantee:
    ``assemble_serving_preflight_callback`` calls it when its callback runs,
    and :meth:`ServingSmokeReader.read` calls it before each start — so the
    plain smoke-reader seam cannot write a run's logs through a symlinked or
    group-readable root that only the callback seam used to refuse.
    Construction stays effect-free on both seams either way.
    """

    prepared = Path(log_root)
    # **A symlink to a directory is an existing directory as far as `mkdir` is
    # concerned.** The comment below used to end "nothing further is needed to
    # establish that this path is one", and that was the gap: `exist_ok=True`
    # refuses a file, a symlink to a file and a broken symlink, but forgives a
    # symlink pointing at a real directory somewhere else — and then `chmod`
    # follows it and re-modes the target. The run's logs would be written
    # wherever the link pointed, under a mode this function set on a directory it
    # never named. `lstat` does not follow, so asking here is what makes "this is
    # the directory we will write into" true rather than merely likely.
    #
    # The residual race is named rather than closed: between this check and the
    # `mkdir` below, anything that can write the parent directory could swap the
    # path. Closing that needs `O_NOFOLLOW` directory descriptors and `openat`
    # throughout, which is disproportionate for a log directory inside the run
    # tree on a single-user machine. Found by CodeRabbit on this branch.
    try:
        existing = prepared.lstat()
    except FileNotFoundError:
        existing = None
    except OSError as error:
        raise ServingConfigurationError(
            f"cannot prepare serving log root {prepared}: {error}"
        ) from error
    if existing is not None and stat.S_ISLNK(existing.st_mode):
        raise ServingConfigurationError(
            f"serving log root {prepared} is a symbolic link, so the logs and the "
            "owner-only mode this sets would land on a directory the run never named; "
            "it must be a real directory"
        )
    try:
        # exist_ok=True still raises FileExistsError -- an OSError caught below
        # -- when the path exists as a file, a symlink to one, or a broken
        # symlink. The symlink-to-a-directory case is refused above.
        prepared.mkdir(parents=True, exist_ok=True)
        # mkdir's mode argument is subject to the ambient umask and, with
        # parents=True, is never applied to intermediate directories at all --
        # chmod afterward is the only way to make this owner-only regardless of
        # umask or a pre-existing looser mode, matching the per-launch log
        # files, which are already forced 0600 at creation.
        prepared.chmod(0o700)
    except OSError as error:
        raise ServingConfigurationError(
            f"cannot prepare serving log root {prepared}: {error}"
        ) from error
    return prepared


class ServingSmokeReader:
    """One lifecycle-backed implementation of the pod ``SmokeReader`` protocol.

    An adapted chair receives its calibration only from the explicit
    ``calibration_for`` seam.  Returning ``None`` is safe for an unadapted
    chair; for an adapter it makes :class:`ServingManager` refuse the start
    before a smoke result can be made.
    """

    def __init__(
        self,
        manager: ServingManager,
        smoke_call: SmokeCall,
        *,
        calibration_for: CalibrationFor | None = None,
        placement_table: PlacementTable | None = None,
        gpu_profile: GpuProfile | None = None,
    ) -> None:
        self.manager = manager
        self.smoke_call = smoke_call
        self.calibration_for = calibration_for
        self.placement_table = placement_table
        # `operations.pod.preflight.SmokeReader.read` does not carry the measured
        # profile as of spec 04's landed shape, so it travels bound to the reader
        # instead of per call. `assemble_serving_preflight_callback` sets this the
        # moment its own probe measures one, right before `PreflightRunner.run`.
        self.gpu_profile = gpu_profile

    def read(
        self,
        identity: ChairIdentity,
        fixture: Path,
        placement: PlacementTier,
    ) -> SmokeResult:
        """Start → prove → smoke → stop one named chair for this measured tier."""

        gpu_profile = self.gpu_profile
        if self.placement_table is not None:
            if gpu_profile is None:
                raise ServingConfigurationError(
                    "serving smoke reader has a run-sealed placement table but no bound "
                    "measured GPU profile to check the smoke placement against"
                )
            try:
                sealed_placement = self.placement_table.choose(gpu_profile.vram_gib)
            except PlacementRefusal as error:
                raise ServingConfigurationError(
                    f"sealed placement table cannot place the measured GPU: {error}"
                ) from error
            if placement != sealed_placement:
                raise ServingConfigurationError(
                    "smoke placement differs from the run-sealed placement table for the "
                    f"measured {gpu_profile.vram_gib} GiB GPU"
                )
        serving_profile = self.manager.recipes.for_identity(identity, placement.identifier)
        if isinstance(serving_profile, ServingProfile):
            # These two coherence checks only mean something for a profile that
            # will actually launch. A fixture row, and a captured row (its
            # reading is a retained response filed by the Attestatores, never a
            # process this preflight starts), carry no flags to check and are
            # refused by name inside `manager.start` below, through the
            # registry's own no-substitution door.
            if gpu_profile is not None and serving_profile.dtype != gpu_profile.dtype:
                raise ServingConfigurationError(
                    "serving profile dtype "
                    f"{serving_profile.dtype!r} differs from preflight's measured dtype "
                    f"{gpu_profile.dtype!r}"
                )
            self._assert_profile_within_placement(serving_profile, placement)
        fixture_sha256 = _fixture_digest(fixture)
        calibration = self.calibration_for(identity, fixture) if self.calibration_for else None
        self._verify_local_calibration_fixture(calibration, fixture)
        # The same log-root guarantee the callback seam gives: refuse a
        # symlinked root and force it owner-only before anything can write a
        # launch log through it. Idempotent, so once per read is cheap.
        prepare_log_root(self.manager.log_root)
        handle = self.manager.start(
            identity,
            placement.identifier,
            adapter_calibration=calibration,
        )
        primary_error: BaseException | None = None
        try:
            fixture_requests_before_smoke = handle.fixture_requests_completed
            result = self.smoke_call(handle, identity, fixture, placement)
            if not isinstance(result, SmokeResult):
                raise TypeError(
                    "serving smoke callable must return operations.pod.preflight.SmokeResult"
                )
            fixture_response_sha256 = handle.last_fixture_response_sha256
            if (
                handle.fixture_requests_completed <= fixture_requests_before_smoke
                or handle.last_fixture_request_sha256 != fixture_sha256
                or not handle.last_request_was_fixture
            ):
                raise ServingConfigurationError(
                    "golden-page smoke returned without a final completed fixture-bound request "
                    "to the owned service"
                )
            if (
                not is_sha256(fixture_response_sha256)
                or result.receipt.get("fixture_response_sha256") != fixture_response_sha256
            ):
                raise ServingConfigurationError(
                    "golden-page smoke result does not name the exact fixture response from "
                    "the owned service"
                )
            return _with_service_evidence(result, handle, fixture_sha256)
        except BaseException as error:
            primary_error = error
            raise
        finally:
            # A failed page read must not leave an owned vLLM process resident.
            try:
                handle.stop()
            except BaseException as stop_error:
                if primary_error is None:
                    raise
                raise ServiceStopError(
                    "golden-page smoke failed and owned serving shutdown was not verified: "
                    f"smoke={primary_error}; stop={stop_error}"
                ) from primary_error

    @staticmethod
    def _verify_local_calibration_fixture(
        calibration: AdapterCalibration | None, fixture: Path
    ) -> None:
        """Bind a vision adapter probe to the local bytes preflight actually names."""

        if calibration is None or not calibration.requires_image:
            return
        try:
            observed = hashlib.sha256(fixture.read_bytes()).hexdigest()
        except OSError as error:
            raise AdapterActivityError(
                f"cannot read local adapter calibration fixture {fixture}: {error}"
            ) from error
        if observed != calibration.fixture_sha256:
            raise AdapterActivityError(
                "adapter calibration data URI does not match the local golden-page fixture bytes"
            )

    @staticmethod
    def _assert_profile_within_placement(profile: ServingProfile, placement: PlacementTier) -> None:
        """Refuse a vLLM profile that would exceed the measured tier's plan.

        **`pixel_cap` and `max_pixels` are not in the same unit, and comparing
        them directly is wrong.** `config/pod_placement.toml`'s `pixel_cap` is a
        longest-edge cap in pixels — its committed values are 1344, 1792 and
        2304, which are nonsense as pixel counts (roughly 37x37, roughly 42x42,
        and exactly 48x48 pixels) and are the ordinary vision-model side caps.
        A serving profile's `min_pixels`/`max_pixels` go straight into vLLM's
        `--mm-processor-kwargs`, where they are *total pixel counts*: the old
        pipeline's own proven values are 3136 (= 56x56, the patch minimum) and
        2359296 (= 1536x1536), read at the window in `serve_dai.sh` and
        `serve_chandra.sh`.

        Compared directly, every realistic profile fails: 2359296 > 1792. The
        sound relation is the square — an image whose longest edge is at most L
        has at most L*L pixels — so that is what is checked, and it is
        deliberately conservative rather than exact.

        The underlying problem is that one word carries two meanings, which
        GLOSSARY does not allow. Renaming the placement field to something that
        states its unit is the real repair; it belongs to whoever owns
        `config/pod_placement.toml`, not to this check.
        """

        recipe = placement.recipe
        overages: list[str] = []
        if profile.gpu_memory_utilization > recipe.engine_memory_fraction:
            overages.append("gpu_memory_utilization")
        if profile.max_model_len > recipe.context_cap:
            overages.append("max_model_len")
        if profile.max_pixels > recipe.pixel_cap**2:
            overages.append("max_pixels")
        if profile.max_num_seqs > recipe.batch_size:
            overages.append("max_num_seqs")
        if overages:
            raise ServingConfigurationError(
                "serving profile exceeds measured placement limits for "
                f"tier {placement.identifier!r}: {', '.join(overages)}"
            )


def _with_service_evidence(
    result: SmokeResult, handle: ServiceHandle, fixture_sha256: str
) -> SmokeResult:
    """Keep published service provenance alongside, never in place of, smoke facts."""

    receipt = dict(result.receipt)
    reserved = {
        "service_receipt",
        "receipt_reference",
        "serving_launch_audit",
        "serving_launch_audit_reference",
        "serving_evidence_reference",
        "supplied_fixture_sha256",
        "smoke_fixture_response_sha256",
        "smoke_fixture_output_sha256",
        "smoke_service_request_count",
        "smoke_fixture_request_count",
    }
    collision = sorted(reserved & set(receipt))
    if collision:
        raise ValueError(f"smoke receipt cannot pre-populate service evidence fields {collision}")
    receipt.update(
        {
            "service_receipt": handle.receipt.to_record(),
            "receipt_reference": dict(handle.receipt_reference),
            "serving_launch_audit": _plain_mapping(handle.launch_audit),
            "serving_launch_audit_reference": dict(handle.audit_reference),
            "serving_evidence_reference": dict(handle.evidence_reference),
            "supplied_fixture_sha256": fixture_sha256,
            "smoke_fixture_response_sha256": handle.last_fixture_response_sha256,
            "smoke_fixture_output_sha256": handle.last_fixture_output_sha256,
            "smoke_service_request_count": handle.requests_completed,
            "smoke_fixture_request_count": handle.fixture_requests_completed,
        }
    )
    return replace(result, receipt=receipt)


def _plain_mapping(value: Mapping[str, object]) -> dict[str, object]:
    """Copy nested mappings/lists out of immutable operational audit values."""

    result: dict[str, object] = {}
    for key, item in value.items():
        if isinstance(item, Mapping):
            result[key] = _plain_mapping(item)
        elif isinstance(item, tuple):
            result[key] = [
                _plain_mapping(entry) if isinstance(entry, Mapping) else entry for entry in item
            ]
        else:
            result[key] = item
    return result


def _fixture_digest(fixture: Path) -> str:
    """Record the exact local fixture supplied to the smoke callable."""

    try:
        data = fixture.read_bytes()
    except OSError as error:
        raise ServingConfigurationError(
            f"cannot read golden-page fixture supplied to serving smoke {fixture}: {error}"
        ) from error
    if not data:
        raise ServingConfigurationError("golden-page fixture supplied to serving smoke is empty")
    return hashlib.sha256(data).hexdigest()
