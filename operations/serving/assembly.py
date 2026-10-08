"""Assemble serving from run-sealed configuration without starting anything: the pod
preflight's smoke reader, and the chair client each serving stage reads through.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final, Mapping, Protocol

from common.chairs.models import ChairIdentity
from common.contracts.errors import ContractError
from common.contracts.stages import stage_directory
from common.sealed_config import parse_sealed_toml
from common.stage import DEFAULT_POD_PLACEMENT_CONFIG_PATH, DEFAULT_SERVING_RECIPES_CONFIG_PATH
from operations.pod.preflight import (
    GpuProfile,
    PlacementRefusal,
    PlacementTable,
    load_placement_table,
)

from .capacity import CapacityPlan, plan_from_argument
from .client import ChairClient
from .config import ServingConfigInputs, ServingProfile, ServingRecipes, load_serving_recipes
from .errors import (
    ChairRequestRefusal,
    ChairResponseRefusal,
    ServingConfigurationError,
    ServingError,
)
from .http import (
    HttpResponse,
    HttpTransport,
    UrllibHttpTransport,
    parse_openai_reading,
    parse_openai_stream_reading,
    request_body,
)
from .manager import (
    _PREFLIGHT_QUALIFICATION_PURPOSE,
    MECHANICS_QUALIFICATION_PURPOSE,
    PackageInspector,
    ReceiptPublisher,
    ServingManager,
    StageContextReceiptPublisher,
)
from .preflight import ServingSmokeReader, SmokeCall
from .process import ProcessLauncher, SubprocessLauncher
from .residency import (
    POD_HAND_OFF_PATH,
    POD_RESIDENCY_LOCK_PATH,
    FileResidencyLease,
    ResidencyLease,
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
    recipes_path: str | Path = DEFAULT_SERVING_RECIPES_CONFIG_PATH,
    placement_path: str | Path = DEFAULT_POD_PLACEMENT_CONFIG_PATH,
    launcher: ProcessLauncher | None = None,
    http: HttpTransport | None = None,
    package_inspector: PackageInspector | None = None,
    command_prefix: tuple[str, ...] | None = None,
    residency_lease: ResidencyLease,
    producer: str = "operations.serving.assembly",
    capacity_plan: CapacityPlan | None = None,
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
        launcher=launcher,
        http=http,
        package_inspector=package_inspector,
        command_prefix=command_prefix,
        residency_lease=residency_lease,
        producer=producer,
        capacity_plan=capacity_plan,
    )


def bound_serving_recipes(context: Any, recipes_path: str | Path) -> ServingRecipes:
    """The serving catalogue this run sealed, re-read with its placement table and
    proven by digest at the moment of use, so the rows deciding live or fixture are
    the sealed ones."""

    return _bound_serving(context, recipes_path)[0]


def _bound_serving(
    context: Any, recipes_path: str | Path
) -> tuple[ServingRecipes, ServingConfigInputs, PlacementTable]:
    if context.serving_config_inputs is None:
        raise ContractError(
            "this run authority seals no serving configuration inputs, so the serving "
            "posture of its chairs cannot be proven; open the run with `open_stage_context`"
        )
    try:
        recipes, placement, inputs = _load_bound_configuration(
            sealed_config_inputs=dict(context.serving_config_inputs),
            recipes_path=recipes_path,
            placement_path=DEFAULT_POD_PLACEMENT_CONFIG_PATH,
        )
    except ServingError as error:
        raise ContractError(
            f"the sealed serving configuration was refused for {recipes_path} and "
            f"{DEFAULT_POD_PLACEMENT_CONFIG_PATH}: {error}; rerun with the files this run sealed"
        ) from error
    return recipes, inputs, placement


class _BoundServingReader:
    """`common.stage.ServingReader` over this package: the sealed catalogue, the client's
    reply parser and its request renderer, each refusal named as a `ContractError`."""

    def serving_row(self, context: Any, chair: ChairIdentity, tier: Any) -> Any:
        try:
            return bound_serving_recipes(context, context.args.serving_recipes_config).for_identity(
                chair, tier
            )
        except ServingError as error:
            raise ContractError(str(error)) from error

    def reading_reply(
        self, *, status: Any, body: bytes, kind: Any, model_id: str, stopped: bool | None = None
    ) -> tuple[str, str | None]:
        response = HttpResponse(status=status, body=body)
        try:
            result = (
                parse_openai_reading(response, kind=kind, expected_model_id=model_id)
                if stopped is None
                else parse_openai_stream_reading(
                    response, kind=kind, expected_model_id=model_id, stopped=stopped
                )
            )
        except (ChairRequestRefusal, ChairResponseRefusal) as error:
            raise ContractError(str(error)) from error
        return result.outputs[0], result.finish_reasons[0]

    def request_bytes(
        self,
        payload: Mapping[str, Any],
        *,
        model_id: str,
        seed: Any,
        sampling: Mapping[str, Any] | None = None,
        stream: bool = False,
    ) -> bytes:
        try:
            return request_body(
                payload,
                model_id=model_id,
                seed=seed,
                deterministic=False,
                sampling=sampling,
                stream=stream,
            )
        except ServingError as error:
            raise ContractError(str(error)) from error


SERVING_READER: Final = _BoundServingReader()


def retain_chair_bytes(context: Any, data: bytes) -> dict[str, str]:
    """Store one chair response or call record in the stage's own blob store."""

    return context.retain(data, label="a chair response")


def stage_chair_client(
    context: Any,
    identity: ChairIdentity,
    tier: str,
    *,
    decoding_policy: Mapping[str, Any],
    decoding_config_sha256: str,
) -> ChairClient:
    """The client a stage reads one configured chair through; nothing starts until
    it is entered. Logs travel with the run tree; the residency lease belongs to the
    pod's one card, so every stage and run id contends for it on container-local disk,
    and so does the record of a service handed from one stage's process to the next.
    ``decoding_policy`` is the policy the stage already loaded and sealed; the client
    sends its chair's row of it."""

    recipes, config_inputs, placement = _bound_serving(context, context.args.serving_recipes_config)
    manager = ServingManager(
        registry=context.registry,
        recipes=recipes,
        config_inputs=config_inputs,
        launcher=SubprocessLauncher(),
        http=UrllibHttpTransport(),
        receipt_publisher=StageContextReceiptPublisher(context),
        log_root=context.tree.resolve(context.tree.serving_log_path(context.stage)),
        residency_lease=FileResidencyLease(POD_RESIDENCY_LOCK_PATH),
        # A service handed off on this pod is taken over only within its own run.
        hand_off_path=POD_HAND_OFF_PATH,
        service_scope=str(context.tree.root),
        producer=f"pipeline/{stage_directory(context.stage)}/run.py",
        capacity_plan=stage_capacity_plan(context),
        placement_table=placement,
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
        decoding_policy=decoding_policy,
        read_receipt=context.tree.read_run_receipt,
    )


def stage_capacity_plan(context: Any) -> CapacityPlan | None:
    """The run's ``--capacity-plan``, checked against the serving digests it sealed.

    ``None`` when the stage was given none: every row then launches as written.
    """

    plan = plan_from_argument(getattr(context.args, "capacity_plan", None))
    if plan is not None:
        plan.require_inputs(_bound_serving(context, context.args.serving_recipes_config)[1])
    return plan


def launch_row(context: Any, chair: ChairIdentity, tier: str) -> Any:
    """The row a stage's chair is launched with: the sealed row, widened by the
    run's capacity plan when it names the chair. A stage sizes its window from this
    before the chair starts; once started, ``handle.profile`` is the same row."""

    row = bound_serving_recipes(context, context.args.serving_recipes_config).for_identity(
        chair, tier
    )
    try:
        plan = stage_capacity_plan(context)
        if plan is None or not isinstance(row, ServingProfile):
            return row
        return plan.launch_profile(row, chair.role)
    except ServingError as error:
        raise ContractError(f"the run's capacity plan was refused: {error}") from error


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
    launcher: ProcessLauncher | None,
    http: HttpTransport | None,
    package_inspector: PackageInspector | None,
    command_prefix: tuple[str, ...] | None,
    producer: str,
    capacity_plan: CapacityPlan | None = None,
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
        # The smoke reads at the planned width, so the card is proven at the shape
        # the stages will launch.
        capacity_plan=capacity_plan,
        placement_table=placement,
        _launch_purpose=_PREFLIGHT_QUALIFICATION_PURPOSE,
    )
    return ServingSmokeReader(
        manager,
        smoke_call,
        placement_table=placement,
        gpu_profile=gpu_profile,
    )
