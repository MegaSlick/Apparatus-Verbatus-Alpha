"""Assemble serving from run-sealed configuration without starting anything: the pod
preflight's smoke reader, and the chair client each serving stage reads through.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from common.chairs.models import ChairIdentity
from common.contracts.errors import ContractError
from common.contracts.stages import stage_directory
from common.sealed_config import parse_sealed_toml
from common.stage import DEFAULT_POD_PLACEMENT_CONFIG_PATH
from operations.pod.preflight import (
    ChairCacheVerifier,
    GpuProfile,
    PlacementRefusal,
    PlacementTable,
    PreflightRunner,
    SystemGpuProbe,
    load_placement_table,
)

from .client import ChairClient
from .config import ServingConfigInputs, ServingRecipes, load_serving_recipes
from .errors import ServingConfigurationError, ServingError
from .http import HttpTransport, UrllibHttpTransport
from .manager import (
    _PREFLIGHT_QUALIFICATION_PURPOSE,
    MECHANICS_QUALIFICATION_PURPOSE,
    PackageInspector,
    ReceiptPublisher,
    ServingManager,
    StageContextReceiptPublisher,
)
from .preflight import CalibrationFor, ServingSmokeReader, SmokeCall, prepare_log_root
from .process import ProcessLauncher, SubprocessLauncher
from .residency import POD_RESIDENCY_LOCK_PATH, FileResidencyLease, ResidencyLease

DEFAULT_SERVING_RECIPES_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "serving_recipes.toml"
)


class ProfileProbe(Protocol):
    """The measured-GPU seam used only when pod preflight actually runs."""

    def profile(self, dtype: str) -> GpuProfile:
        """Measure or return the named dtype profile."""
        ...


def assemble_serving_smoke_reader(
    *,
    registry: Any,
    stage_context: Any,
    receipt_publisher: ReceiptPublisher,
    smoke_call: SmokeCall,
    gpu_profile: GpuProfile,
    log_root: str | Path,
    calibration_for: CalibrationFor | None = None,
    recipes_path: str | Path = DEFAULT_SERVING_RECIPES_PATH,
    placement_path: str | Path = DEFAULT_POD_PLACEMENT_CONFIG_PATH,
    launcher: ProcessLauncher | None = None,
    http: HttpTransport | None = None,
    package_inspector: PackageInspector | None = None,
    command_prefix: tuple[str, ...] | None = None,
    residency_lease: ResidencyLease,
    producer: str = "operations.serving.assembly",
) -> ServingSmokeReader:
    """Return the one lifecycle-backed ``SmokeReader`` used by pod preflight.

    It deliberately accepts one existing receipt publisher and one page-specific
    smoke callable.  ``stage_context`` must be the same context owned by its
    publisher, so the exact input digests came from ``open_context`` after it
    rechecked the run digest; callers cannot inject a free-standing hash map.
    Both supplied paths are parsed from the exact bytes those digests name, so
    an arbitrary TOML override refuses before a subprocess could launch.  It
    does not know a provider, a credential, a pod request, or a replacement
    chair. The required ``gpu_profile`` is the measurement whose placement the
    returned reader will verify on its first read; the factory therefore cannot
    return a run-sealed reader that lacks the state needed to read. Callers still
    wire this returned reader to the existing
    ``PreflightRunner``/bootstrap injection point.
    """

    recipes, placement, config_inputs = _load_bound_configuration(
        sealed_config_inputs=_sealed_config_inputs(stage_context, receipt_publisher, registry),
        recipes_path=recipes_path,
        placement_path=placement_path,
    )
    return _make_reader(
        registry=registry,
        receipt_publisher=receipt_publisher,
        smoke_call=smoke_call,
        gpu_profile=gpu_profile,
        log_root=log_root,
        recipes=recipes,
        placement=placement,
        config_inputs=config_inputs,
        calibration_for=calibration_for,
        launcher=launcher,
        http=http,
        package_inspector=package_inspector,
        command_prefix=command_prefix,
        residency_lease=residency_lease,
        producer=producer,
    )


def assemble_serving_preflight_callback(
    *,
    registry: Any,
    stage_context: Any,
    cache_verifier: ChairCacheVerifier,
    receipt_publisher: ReceiptPublisher,
    smoke_call: SmokeCall,
    fixture: str | Path,
    dtype: str,
    log_root: str | Path,
    residency_lease: ResidencyLease,
    calibration_for: CalibrationFor | None = None,
    recipes_path: str | Path = DEFAULT_SERVING_RECIPES_PATH,
    placement_path: str | Path = DEFAULT_POD_PLACEMENT_CONFIG_PATH,
    launcher: ProcessLauncher | None = None,
    http: HttpTransport | None = None,
    package_inspector: PackageInspector | None = None,
    command_prefix: tuple[str, ...] | None = None,
    gpu_probe: ProfileProbe | None = None,
    producer: str = "operations.serving.assembly",
) -> Callable[[], dict[str, object]]:
    """Build the callback that ``SubprocessBootstrapActions`` already accepts.

    Construction performs no provider, GPU, socket, or model effect.  When
    bootstrap calls the returned function, it obtains a measured profile and
    feeds the existing ``PreflightRunner`` with this lifecycle-backed smoke
    reader.  Thus bootstrap has one real seam rather than a second parallel
    preflight loop.
    """

    recipes, placement, config_inputs = _load_bound_configuration(
        sealed_config_inputs=_sealed_config_inputs(stage_context, receipt_publisher, registry),
        recipes_path=recipes_path,
        placement_path=placement_path,
    )
    reader = _make_reader(
        registry=registry,
        receipt_publisher=receipt_publisher,
        smoke_call=smoke_call,
        gpu_profile=None,
        log_root=log_root,
        residency_lease=residency_lease,
        calibration_for=calibration_for,
        recipes=recipes,
        placement=placement,
        config_inputs=config_inputs,
        launcher=launcher,
        http=http,
        package_inspector=package_inspector,
        command_prefix=command_prefix,
        producer=producer,
    )
    runner = PreflightRunner(
        registry.config,
        placement,
        cache_verifier,
        reader,
        fixture,
    )

    def run_preflight() -> dict[str, object]:
        prepared_log_root = prepare_log_root(log_root)
        probe = gpu_probe or SystemGpuProbe(disk_path=prepared_log_root)
        profile = probe.profile(dtype)
        # `operations.pod.preflight.SmokeReader.read` carries no profile
        # parameter, so the reader holds the measured profile itself; bind it
        # the moment it exists, immediately before the one run that will read it.
        reader.gpu_profile = profile
        return runner.run(profile).to_record()

    return run_preflight


def bound_serving_recipes(context: Any, recipes_path: str | Path) -> ServingRecipes:
    """The serving catalogue this run sealed, re-read with its placement table and
    proven by digest at the moment of use, so the rows deciding live or fixture are
    the sealed ones."""

    return _bound_serving(context, recipes_path)[0]


def _bound_serving(
    context: Any, recipes_path: str | Path
) -> tuple[ServingRecipes, ServingConfigInputs]:
    if context.serving_config_inputs is None:
        raise ContractError(
            "this run authority seals no serving configuration inputs, so the serving "
            "posture of its chairs cannot be proven; open the run with `open_stage_context`"
        )
    try:
        recipes, _, inputs = _load_bound_configuration(
            sealed_config_inputs=dict(context.serving_config_inputs),
            recipes_path=recipes_path,
            placement_path=DEFAULT_POD_PLACEMENT_CONFIG_PATH,
        )
    except ServingError as error:
        raise ContractError(
            f"the sealed serving configuration was refused for {recipes_path} and "
            f"{DEFAULT_POD_PLACEMENT_CONFIG_PATH}: {error}; rerun with the files this run sealed"
        ) from error
    return recipes, inputs


def retain_chair_bytes(context: Any, data: bytes) -> dict[str, str]:
    """Store one chair response or call record in the stage's own blob store."""

    return context.retain(data, label="a chair response")


def stage_chair_client(
    context: Any,
    identity: ChairIdentity,
    tier: str,
    *,
    decoding_config_sha256: str,
    record_temperature: int | float,
    chandra_native_policy: Mapping[str, object] | None = None,
) -> ChairClient:
    """The client a stage reads one configured chair through; nothing starts until
    it is entered. Logs travel with the run tree; the residency lease belongs to the
    pod's one card, so every stage and run id contends for it on container-local disk."""

    recipes, config_inputs = _bound_serving(context, context.args.serving_recipes_config)
    manager = ServingManager(
        registry=context.registry,
        recipes=recipes,
        config_inputs=config_inputs,
        launcher=SubprocessLauncher(),
        http=UrllibHttpTransport(),
        receipt_publisher=StageContextReceiptPublisher(context),
        log_root=context.tree.resolve(context.tree.serving_log_path(context.stage)),
        residency_lease=FileResidencyLease(POD_RESIDENCY_LOCK_PATH),
        producer=f"pipeline/{stage_directory(context.stage)}/run.py",
        _launch_purpose=(
            MECHANICS_QUALIFICATION_PURPOSE
            if getattr(context.args, "mechanics_qualification", False)
            else None
        ),
    )
    return ChairClient(
        manager=manager,
        identity=identity,
        tier=tier,
        retain=lambda data: retain_chair_bytes(context, data),
        decoding_config_sha256=decoding_config_sha256,
        record_temperature=record_temperature,
        read_receipt=context.tree.read_run_receipt,
        chandra_native_policy=chandra_native_policy,
    )


def _load_bound_configuration(
    *,
    sealed_config_inputs: Mapping[str, object],
    recipes_path: str | Path,
    placement_path: str | Path,
) -> tuple[ServingRecipes, PlacementTable, ServingConfigInputs]:
    """Read one immutable configuration snapshot and compare it to run evidence."""

    expected = ServingConfigInputs.from_record(sealed_config_inputs)
    recipes = load_serving_recipes(recipes_path)
    placement, placement_sha256 = _read_and_parse_placement(placement_path)
    expected.require_loaded(
        recipes_sha256=recipes.source_sha256,
        placement_sha256=placement_sha256,
    )
    return recipes, placement, expected


def _read_and_parse_placement(placement_path: str | Path) -> tuple[PlacementTable, str]:
    """Read the placement table once and parse those same bytes.

    `PlacementTable` carries no seal of its own, so the seal and the parsed
    table must come from one read; a second read could see a replaced file and
    seal a run to a placement it never parsed. Both the read and the parse are
    translated to `ServingConfigurationError`, the boundary's own vocabulary,
    since a missing file raises `OSError` while malformed or non-UTF-8 TOML
    raises `PlacementRefusal` (a `ValueError`, not a `ServingError`).
    """

    try:
        placement_bytes = Path(placement_path).read_bytes()
    except OSError as error:
        raise ServingConfigurationError(
            f"cannot read placement table {placement_path}: {error}"
        ) from error
    try:
        placement = load_placement_table(placement_path, source_bytes=placement_bytes)
        _, placement_sha256 = parse_sealed_toml(placement_bytes, "placement table")
    except (PlacementRefusal, ContractError) as error:
        raise ServingConfigurationError(
            f"cannot parse placement table {placement_path}: {error}"
        ) from error
    return placement, placement_sha256


def _sealed_config_inputs(
    stage_context: Any, receipt_publisher: ReceiptPublisher, registry: Any
) -> Mapping[str, object]:
    """Take launch-input authority only from the context that publishes evidence."""

    inputs = getattr(stage_context, "serving_config_inputs", None)
    if inputs is None:
        raise ServingConfigurationError(
            "serving assembly requires a StageContext opened with run-sealed serving inputs"
        )
    if getattr(receipt_publisher, "context", None) is not stage_context:
        raise ServingConfigurationError(
            "serving assembly receipt publisher must belong to the supplied StageContext"
        )
    if getattr(stage_context, "registry", None) is not registry:
        raise ServingConfigurationError(
            "serving assembly registry must be the registry owned by the supplied StageContext"
        )
    if not isinstance(inputs, Mapping):
        raise ServingConfigurationError("StageContext serving configuration inputs are malformed")
    return inputs


def _make_reader(
    *,
    registry: Any,
    receipt_publisher: ReceiptPublisher,
    smoke_call: SmokeCall,
    gpu_profile: GpuProfile | None,
    log_root: str | Path,
    recipes: ServingRecipes,
    placement: PlacementTable,
    config_inputs: ServingConfigInputs,
    residency_lease: ResidencyLease,
    calibration_for: CalibrationFor | None,
    launcher: ProcessLauncher | None,
    http: HttpTransport | None,
    package_inspector: PackageInspector | None,
    command_prefix: tuple[str, ...] | None,
    producer: str,
) -> ServingSmokeReader:
    """Construct a dormant manager only after configuration substitution is refused."""

    manager = ServingManager(
        registry=registry,
        recipes=recipes,
        config_inputs=config_inputs,
        launcher=launcher or SubprocessLauncher(),
        http=http or UrllibHttpTransport(),
        receipt_publisher=receipt_publisher,
        log_root=log_root,
        package_inspector=package_inspector,
        command_prefix=command_prefix,
        residency_lease=residency_lease,
        producer=producer,
        _launch_purpose=_PREFLIGHT_QUALIFICATION_PURPOSE,
    )
    return ServingSmokeReader(
        manager,
        smoke_call,
        calibration_for=calibration_for,
        placement_table=placement,
        gpu_profile=gpu_profile,
    )
