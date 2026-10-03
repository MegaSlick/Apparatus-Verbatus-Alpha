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
from typing import Callable, Iterable, Mapping

from common.chairs.models import ChairIdentity, is_sha256

# `_mint_runtime_provenance` is deliberately a private name from another
# module. It mints the opaque token `operations.pod.preflight` checks before a
# receipt may claim a real assembly was measured, and `_with_service_evidence`
# below is one of its two minting sites -- the other is that module's own GPU
# probe. Keeping the mint private is the point: a public helper would be an
# invitation for any caller to name its own serving engine and publish the
# claim on the strength of it.
from operations.pod.preflight import (
    GpuProfile,
    PlacementRefusal,
    PlacementTable,
    PlacementTier,
    SmokeResult,
    _mint_runtime_provenance,
)

from .config import ServingProfile, thawed_json
from .errors import ServiceStopError, ServingConfigurationError
from .manager import ServiceHandle, ServingManager

SmokeCall = Callable[[ServiceHandle, ChairIdentity, Path, PlacementTier], SmokeResult]


def prepare_log_root(log_root: str | Path) -> Path:
    """Create and verify the exact log filesystem a launch will write into.

    :meth:`ServingSmokeReader.read` calls it before each start, so a smoke can
    never write a run's logs through a symlinked or group-readable root.
    """

    prepared = Path(log_root)
    # `mkdir(exist_ok=True)` forgives a symlink pointing at a real directory
    # elsewhere, and a later `chmod` would follow it and re-mode that target
    # instead -- so `lstat` (which does not follow) checks first. The residual
    # race between this check and `mkdir` below is left open: closing it needs
    # `O_NOFOLLOW` descriptors throughout, disproportionate for a log directory
    # on a single-user machine.
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


def assert_generation_config_key_coverage(
    *,
    chair: str,
    vendor_generation_config: Mapping[str, object],
    sent_keys: Iterable[str],
    deliberately_not_sent: Mapping[str, str],
) -> None:
    """Refuse a vendor ``generation_config.json`` key accounted for nowhere.

    Every key the vendor's own shipped file carries must be either sent
    verbatim on the wire (``sent_keys``) or named in ``deliberately_not_sent``
    with the recorded reason it is withheld (DAI's ``bos_token_id`` and
    ``pad_token_id``, which an OpenAI request has no field for, are named
    there with their reasons).  A key in neither set is not a decision anyone
    made -- it is a vendor value quietly falling on the floor.  A key claimed
    both sent and deliberately withheld is a contradiction, refused the same
    way.
    """

    vendor_keys = set(vendor_generation_config)
    sent = set(sent_keys)
    excluded = set(deliberately_not_sent)
    both = sorted(sent & excluded)
    if both:
        raise ServingConfigurationError(
            f"chair {chair!r} generation_config key(s) {both} are claimed both sent and "
            "deliberately not sent; that is a contradiction, not a decision"
        )
    unaccounted = sorted(vendor_keys - sent - excluded)
    if unaccounted:
        raise ServingConfigurationError(
            f"chair {chair!r} vendor generation_config.json ships key(s) {unaccounted} that are "
            "neither sent on the wire nor named as deliberately not sent; a vendor value may "
            "not silently fall on the floor"
        )


class ServingSmokeReader:
    """One lifecycle-backed implementation of the pod ``SmokeReader`` protocol."""

    def __init__(
        self,
        manager: ServingManager,
        smoke_call: SmokeCall,
        *,
        placement_table: PlacementTable | None = None,
        gpu_profile: GpuProfile | None = None,
    ) -> None:
        self.manager = manager
        self.smoke_call = smoke_call
        self.placement_table = placement_table
        # `operations.pod.preflight.SmokeReader.read` does not carry the measured
        # profile, so it travels bound to the reader instead of per call.
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
            # will actually launch. A fixture row carries no flags to check and
            # is refused by name inside `manager.start` below, through the
            # registry's own no-substitution door.
            if gpu_profile is not None and serving_profile.dtype != gpu_profile.dtype:
                raise ServingConfigurationError(
                    "serving profile dtype "
                    f"{serving_profile.dtype!r} differs from preflight's measured dtype "
                    f"{gpu_profile.dtype!r}"
                )
            self._assert_profile_within_placement(serving_profile, placement)
        fixture_sha256 = _fixture_digest(fixture)
        # Refuse a symlinked log root and force it owner-only before anything
        # can write a launch log through it. Idempotent, so once per read is cheap.
        prepare_log_root(self.manager.log_root)
        handle = self.manager.start(identity, placement.identifier)
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
    def _assert_profile_within_placement(profile: ServingProfile, placement: PlacementTier) -> None:
        """Refuse a vLLM profile that would exceed the measured tier's plan.

        `pixel_cap` (`config/pod_placement.toml`) is a longest-edge cap in
        pixels, while a profile's `max_pixels` is a total pixel count sent to
        vLLM's `--mm-processor-kwargs` -- comparing them directly always fails,
        so this checks the sound relation instead: an image whose longest edge
        is at most L has at most L*L pixels, which is conservative rather than
        exact.
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
        "smoke_fixture_request_count",
    }
    collision = sorted(reserved & set(receipt))
    if collision:
        raise ValueError(f"smoke receipt cannot pre-populate service evidence fields {collision}")
    # The engine that answered, named from the receipt of the handle this module
    # started, proved fixture-bound and stopped -- never from anything the smoke
    # callable said about itself. `operations.pod.preflight` derives
    # `PreflightReport.assembly_proven` from this, so it must be a fact about the
    # lifecycle rather than a label a caller can supply.
    details = handle.receipt.details
    served_by = " ".join(
        part for part in (details.engine, details.engine_version) if isinstance(part, str) and part
    )
    receipt.update(
        {
            "service_receipt": handle.receipt.to_record(),
            "receipt_reference": dict(handle.receipt_reference),
            "serving_launch_audit": thawed_json(handle.launch_audit),
            "serving_launch_audit_reference": dict(handle.audit_reference),
            "serving_evidence_reference": dict(handle.evidence_reference),
            "supplied_fixture_sha256": fixture_sha256,
            "smoke_fixture_response_sha256": handle.last_fixture_response_sha256,
            "smoke_fixture_output_sha256": handle.last_fixture_output_sha256,
            "smoke_fixture_request_count": handle.fixture_requests_completed,
        }
    )
    if not served_by:
        # Nothing to claim: the handle published no engine name, so the read
        # proves a page came back and nothing about what served it.
        return replace(result, receipt=receipt, served_by=None, provenance=None)
    return replace(
        result,
        receipt=receipt,
        served_by=served_by,
        # Minted here and nowhere else in this package: this line is reachable
        # only from `ServingSmokeReader.read`, holding a `ServiceHandle` this
        # module started, proved fixture-bound and will stop in its `finally`.
        provenance=_mint_runtime_provenance(f"service handle receipt: {served_by}"),
    )


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
