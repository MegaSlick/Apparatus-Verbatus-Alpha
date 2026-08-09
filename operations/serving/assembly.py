"""Construct the serving-backed pod smoke reader without starting anything.

The pod bootstrap already accepts a preflight callable.  This factory is the
narrow assembly point an owner passes into that callable: construction reads only
the checked-in serving configuration catalogues, while all effects remain dormant until
``PreflightRunner`` asks the returned reader to read one configured chair.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from operations.pod.preflight import (
    ChairCacheVerifier,
    GpuProfile,
    PlacementTable,
    PreflightRunner,
    SystemGpuProbe,
    load_placement_table,
)

from .config import ServingConfigInputs, ServingRecipes, load_serving_recipes
from .errors import ServingConfigurationError
from .http import HttpTransport, UrllibHttpTransport
from .manager import PackageInspector, ReceiptPublisher, ServingManager
from .preflight import CalibrationFor, ServingSmokeReader, SmokeCall
from .process import ProcessLauncher, SubprocessLauncher
from .residency import ResidencyLease

DEFAULT_SERVING_RECIPES_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "serving_recipes.toml"
)
DEFAULT_POD_PLACEMENT_PATH = Path(__file__).resolve().parents[2] / "config" / "pod_placement.toml"


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
    log_root: str | Path,
    calibration_for: CalibrationFor | None = None,
    recipes_path: str | Path = DEFAULT_SERVING_RECIPES_PATH,
    placement_path: str | Path = DEFAULT_POD_PLACEMENT_PATH,
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
    chair; callers still wire this returned reader to the existing
    ``PreflightRunner``/bootstrap injection point.
    """

    recipes, _, config_inputs = _load_bound_configuration(
        sealed_config_inputs=_sealed_config_inputs(stage_context, receipt_publisher),
        recipes_path=recipes_path,
        placement_path=placement_path,
    )
    return _make_reader(
        registry=registry,
        receipt_publisher=receipt_publisher,
        smoke_call=smoke_call,
        log_root=log_root,
        recipes=recipes,
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
    placement_path: str | Path = DEFAULT_POD_PLACEMENT_PATH,
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
        sealed_config_inputs=_sealed_config_inputs(stage_context, receipt_publisher),
        recipes_path=recipes_path,
        placement_path=placement_path,
    )
    reader = _make_reader(
        registry=registry,
        receipt_publisher=receipt_publisher,
        smoke_call=smoke_call,
        log_root=log_root,
        residency_lease=residency_lease,
        calibration_for=calibration_for,
        recipes=recipes,
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
        prepared_log_root = _prepare_log_root(log_root)
        probe = gpu_probe or SystemGpuProbe(disk_path=prepared_log_root)
        return runner.run(probe.profile(dtype)).to_record()

    return run_preflight


def _load_bound_configuration(
    *,
    sealed_config_inputs: Mapping[str, object],
    recipes_path: str | Path,
    placement_path: str | Path,
) -> tuple[ServingRecipes, PlacementTable, ServingConfigInputs]:
    """Read one immutable configuration snapshot and compare it to run evidence."""

    expected = ServingConfigInputs.from_record(sealed_config_inputs)
    recipes = load_serving_recipes(recipes_path)
    placement = load_placement_table(placement_path)
    expected.require_loaded(
        recipes_sha256=recipes.source_sha256,
        placement_sha256=placement.source_sha256,
    )
    return recipes, placement, expected


def _sealed_config_inputs(
    stage_context: Any, receipt_publisher: ReceiptPublisher
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
    if not isinstance(inputs, Mapping):
        raise ServingConfigurationError("StageContext serving configuration inputs are malformed")
    return inputs


def _make_reader(
    *,
    registry: Any,
    receipt_publisher: ReceiptPublisher,
    smoke_call: SmokeCall,
    log_root: str | Path,
    recipes: ServingRecipes,
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
    )
    return ServingSmokeReader(manager, smoke_call, calibration_for=calibration_for)


def _prepare_log_root(log_root: str | Path) -> Path:
    """Create and verify the exact log filesystem before the default disk probe.

    Assembly construction remains effect-free.  Callback execution is the first
    pod lifecycle step, so it may prepare this ordinary local directory before
    preflight asks how much space the eventual vLLM logs can use.
    """

    prepared = Path(log_root)
    try:
        prepared.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ServingConfigurationError(
            f"cannot prepare serving log root {prepared}: {error}"
        ) from error
    if not prepared.is_dir():
        raise ServingConfigurationError(f"serving log root {prepared} is not a directory")
    return prepared
