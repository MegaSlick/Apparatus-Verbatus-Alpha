"""The capacity plan: how wide each served chair may run on the card actually measured.

A serving row's ``max_num_seqs`` is written for the smallest card of its tier. On a
bigger card the engine has room for more sequences at once, and the stages size
their windows from the launched width. PREFLIGHT derives this plan from the
measured card and the sealed rows, smoke-reads each chair at the planned width, and
publishes the plan in its receipt; ``pod_run`` forwards it to every stage as
``--capacity-plan``. It is a runtime fact of the card, like the placement tier, and is
never sealed into reading evidence: only ``max_num_seqs`` changes, nothing that
shapes a reading.

The row is always the floor: a planned width is never below the row's own
``max_num_seqs``. A row without ``weights_gib`` and ``kv_gib_per_seq``, or a run
without a plan, launches exactly as written.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Final, Mapping

from common.chairs.models import ChairIdentity
from common.contracts.canonical import canonical_bytes, digest_bytes, is_sha256

from .config import ServingConfigInputs, ServingProfile, ServingRecipes
from .errors import ServingConfigurationError, ServingError

CAPACITY_PLAN_SCHEMA: Final = "capacity-plan.v1"

# What the engine holds beside its weights and KV: activations at a full prefill
# chunk, the vision encoder's peak, CUDA graphs and the sampler. Not measured on
# any card; set wide so a planned width leaves room for it. A pod's launch log
# reports the KV pool vLLM actually allocated, which is what to size this from.
ENGINE_OVERHEAD_GIB: Final = Decimal("4")

# The cap when a placement tier sets no `planned_batch_ceiling`
# (config/pod_placement.toml): past it a stage's window threads and prepared
# requests cost more than the batch gains.
MAX_NUM_SEQS_CAP: Final = 64


def planned_ceiling(tier: Any) -> int:
    """The most sequences a plan may launch a row with at this placement tier."""

    ceiling = tier.recipe.planned_batch_ceiling
    return MAX_NUM_SEQS_CAP if ceiling is None else ceiling


_PLAN_FIELDS: Final = frozenset(
    {
        "schema",
        "card",
        "tier",
        "engine_overhead_gib",
        "max_num_seqs_cap",
        "serving_config_inputs",
        "chairs",
        "plan_sha256",
    }
)
_CARD_FIELDS: Final = frozenset({"vram_gib", "gpu_count", "compute_capability"})
_CHAIR_FIELDS: Final = frozenset(
    {
        "recipe",
        "row_max_num_seqs",
        "max_num_seqs",
        "weights_gib",
        "kv_gib_per_seq",
        "memory_fraction",
    }
)


def derive_max_num_seqs(
    *,
    vram_gib: Decimal,
    memory_fraction: Decimal,
    weights_gib: Decimal,
    kv_gib_per_seq: Decimal,
    row_max_num_seqs: int,
    overhead_gib: Decimal = ENGINE_OVERHEAD_GIB,
    cap: int = MAX_NUM_SEQS_CAP,
) -> int:
    """How many sequences one chair may run at once on this card.

    ``floor((fraction x VRAM - weights - overhead) / KV per sequence)``, at most
    ``cap`` (the tier's planned ceiling) and never below the row's own ``max_num_seqs``.
    """

    room = memory_fraction * vram_gib - weights_gib - overhead_gib
    fits = int((room / kv_gib_per_seq).to_integral_value(ROUND_FLOOR)) if room > 0 else 0
    return max(row_max_num_seqs, min(fits, cap))


@dataclass(frozen=True, slots=True)
class ChairCapacity:
    """One chair's planned width and the figures it was derived from."""

    recipe: str
    row_max_num_seqs: int
    max_num_seqs: int
    weights_gib: Decimal
    kv_gib_per_seq: Decimal
    memory_fraction: Decimal

    def to_record(self) -> dict[str, object]:
        return {
            "recipe": self.recipe,
            "row_max_num_seqs": self.row_max_num_seqs,
            "max_num_seqs": self.max_num_seqs,
            "weights_gib": str(self.weights_gib),
            "kv_gib_per_seq": str(self.kv_gib_per_seq),
            "memory_fraction": str(self.memory_fraction),
        }


@dataclass(frozen=True, slots=True)
class CapacityPlan:
    """The measured card, and the width each widenable chair is launched at on it."""

    vram_gib: Decimal
    gpu_count: int
    compute_capability: str | None
    tier: str
    serving_config_inputs: ServingConfigInputs
    chairs: Mapping[str, ChairCapacity]
    engine_overhead_gib: Decimal = ENGINE_OVERHEAD_GIB
    max_num_seqs_cap: int = MAX_NUM_SEQS_CAP

    def __post_init__(self) -> None:
        object.__setattr__(self, "chairs", MappingProxyType(dict(self.chairs)))
        for role, chair in self.chairs.items():
            if chair.max_num_seqs < chair.row_max_num_seqs:
                raise ServingConfigurationError(
                    f"capacity plan gives chair {role!r} {chair.max_num_seqs} sequences, below "
                    f"its row's {chair.row_max_num_seqs}; the row is the floor"
                )
            if chair.max_num_seqs > max(self.max_num_seqs_cap, chair.row_max_num_seqs):
                raise ServingConfigurationError(
                    f"capacity plan gives chair {role!r} {chair.max_num_seqs} sequences, above "
                    f"the plan's cap of {self.max_num_seqs_cap}"
                )

    def card_record(self) -> dict[str, object]:
        return {
            "vram_gib": str(self.vram_gib),
            "gpu_count": self.gpu_count,
            "compute_capability": self.compute_capability,
        }

    def _body(self) -> dict[str, object]:
        return {
            "schema": CAPACITY_PLAN_SCHEMA,
            "card": self.card_record(),
            "tier": self.tier,
            "engine_overhead_gib": str(self.engine_overhead_gib),
            "max_num_seqs_cap": self.max_num_seqs_cap,
            "serving_config_inputs": self.serving_config_inputs.to_record(),
            "chairs": {role: chair.to_record() for role, chair in sorted(self.chairs.items())},
        }

    @property
    def digest(self) -> str:
        return digest_bytes(canonical_bytes(self._body()))

    def to_record(self) -> dict[str, object]:
        return {**self._body(), "plan_sha256": self.digest}

    def to_argument(self) -> str:
        """The plan as one canonical JSON argument, as ``pod_run`` forwards it."""

        return canonical_bytes(self.to_record()).decode("utf-8")

    def launch_profile(self, profile: ServingProfile, role: str) -> ServingProfile:
        """The row this chair is launched with: widened when the plan names it.

        Refused when the plan was derived from another row or tier, so a plan can
        never widen a row it did not see.
        """

        chair = self.chairs.get(role)
        if chair is None:
            return profile
        if profile.tier != self.tier or profile.recipe != chair.recipe:
            raise ServingConfigurationError(
                f"capacity plan for chair {role!r} was derived at recipe {chair.recipe!r}, tier "
                f"{self.tier!r}, not the row being launched (recipe {profile.recipe!r}, tier "
                f"{profile.tier!r})"
            )
        if chair.row_max_num_seqs != profile.max_num_seqs:
            raise ServingConfigurationError(
                f"capacity plan for chair {role!r} was derived from a row with max_num_seqs "
                f"{chair.row_max_num_seqs}, but the row being launched has "
                f"{profile.max_num_seqs}"
            )
        if chair.max_num_seqs < profile.max_num_seqs:
            raise ServingConfigurationError(
                f"capacity plan would launch chair {role!r} below its row's max_num_seqs"
            )
        return replace(profile, max_num_seqs=chair.max_num_seqs)

    def require_inputs(self, inputs: ServingConfigInputs) -> None:
        """Refuse a plan derived from other serving configuration than this run sealed."""

        if self.serving_config_inputs != inputs:
            raise ServingConfigurationError(
                "capacity plan was derived from other serving recipes or placement bytes than "
                "the ones this run sealed"
            )

    @classmethod
    def from_record(cls, value: object) -> "CapacityPlan":
        record = _mapping(value, "capacity plan", _PLAN_FIELDS)
        if record["schema"] != CAPACITY_PLAN_SCHEMA:
            raise ServingConfigurationError(
                f"capacity plan schema must be {CAPACITY_PLAN_SCHEMA!r}, not {record['schema']!r}"
            )
        card = _mapping(record["card"], "capacity plan card", _CARD_FIELDS)
        capability = card["compute_capability"]
        if capability is not None and not isinstance(capability, str):
            raise ServingConfigurationError("capacity plan compute_capability must be text or null")
        chairs_raw = record["chairs"]
        if not isinstance(chairs_raw, Mapping):
            raise ServingConfigurationError("capacity plan chairs must be an object")
        chairs = {}
        for role, raw in chairs_raw.items():
            entry = _mapping(raw, f"capacity plan chair {role!r}", _CHAIR_FIELDS)
            chairs[_text(role, "chair role")] = ChairCapacity(
                recipe=_text(entry["recipe"], "recipe"),
                row_max_num_seqs=_count(entry["row_max_num_seqs"], "row_max_num_seqs"),
                max_num_seqs=_count(entry["max_num_seqs"], "max_num_seqs"),
                weights_gib=_decimal(entry["weights_gib"], "weights_gib"),
                kv_gib_per_seq=_decimal(entry["kv_gib_per_seq"], "kv_gib_per_seq"),
                memory_fraction=_decimal(entry["memory_fraction"], "memory_fraction"),
            )
        inputs_raw = record["serving_config_inputs"]
        if not isinstance(inputs_raw, Mapping):
            raise ServingConfigurationError("capacity plan serving_config_inputs must be an object")
        plan = cls(
            vram_gib=_decimal(card["vram_gib"], "vram_gib"),
            gpu_count=_count(card["gpu_count"], "gpu_count"),
            compute_capability=capability,
            tier=_text(record["tier"], "tier"),
            serving_config_inputs=ServingConfigInputs.from_record(inputs_raw),
            chairs=chairs,
            engine_overhead_gib=_decimal(record["engine_overhead_gib"], "engine_overhead_gib"),
            max_num_seqs_cap=_count(record["max_num_seqs_cap"], "max_num_seqs_cap"),
        )
        if not is_sha256(record["plan_sha256"]) or record["plan_sha256"] != plan.digest:
            raise ServingConfigurationError(
                "capacity plan digest does not match its contents; the plan was changed after "
                "PREFLIGHT derived it"
            )
        return plan

    @classmethod
    def from_argument(cls, text: str) -> "CapacityPlan":
        try:
            value = json.loads(text)
        except (TypeError, ValueError) as error:
            raise ServingConfigurationError(f"--capacity-plan is not JSON: {error}") from error
        return cls.from_record(value)


def derive_capacity_plan(
    *,
    vram_gib: Decimal,
    gpu_count: int,
    compute_capability: str | None,
    tier: str,
    engine_memory_fraction: Decimal,
    recipes: ServingRecipes,
    chairs: Mapping[str, object],
    serving_config_inputs: ServingConfigInputs,
    max_num_seqs_cap: int = MAX_NUM_SEQS_CAP,
) -> CapacityPlan:
    """The plan for every configured chair whose row at ``tier`` states its capacity.

    A row with ``shares_service_with`` takes the width planned for the chair whose
    service it shares, so both render the same launch.

    The budget is the fraction the engine is launched with: the row's
    ``gpu_memory_utilization``, which PREFLIGHT holds at or under the tier's
    ``engine_memory_fraction``; the smaller of the two is used.
    """

    rows: dict[str, ServingProfile] = {}
    for role, identity in sorted(chairs.items()):
        if not isinstance(identity, ChairIdentity):
            continue
        try:
            row = recipes.for_identity(identity, tier)
        except ServingError:
            # PREFLIGHT reports a missing row itself; the chair is simply not planned.
            continue
        if isinstance(row, ServingProfile):
            rows[role] = row
    planned: dict[str, ChairCapacity] = {}
    for role, row in rows.items():
        # A row sharing another chair's service is launched as that chair's service
        # was, so the pair is planned once, from the shared chair's row, and both get
        # the same width; otherwise taking the service over would be refused.
        source = rows.get(row.shares_service_with or "", row)
        if source.weights_gib is None or source.kv_gib_per_seq is None:
            continue
        fraction = min(source.gpu_memory_utilization, engine_memory_fraction)
        planned[role] = ChairCapacity(
            recipe=row.recipe,
            row_max_num_seqs=row.max_num_seqs,
            max_num_seqs=derive_max_num_seqs(
                vram_gib=vram_gib,
                memory_fraction=fraction,
                weights_gib=source.weights_gib,
                kv_gib_per_seq=source.kv_gib_per_seq,
                row_max_num_seqs=source.max_num_seqs,
                cap=max_num_seqs_cap,
            ),
            weights_gib=source.weights_gib,
            kv_gib_per_seq=source.kv_gib_per_seq,
            memory_fraction=fraction,
        )
    return CapacityPlan(
        vram_gib=vram_gib,
        gpu_count=gpu_count,
        compute_capability=compute_capability,
        tier=tier,
        serving_config_inputs=serving_config_inputs,
        chairs=planned,
        max_num_seqs_cap=max_num_seqs_cap,
    )


def plan_from_argument(text: str | None) -> CapacityPlan | None:
    """The stage's ``--capacity-plan``, parsed and checked; ``None`` when not given."""

    return None if text is None else CapacityPlan.from_argument(text)


def _mapping(value: object, label: str, fields: frozenset[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ServingConfigurationError(f"{label} must be an object with exactly {sorted(fields)}")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ServingConfigurationError(f"capacity plan {label} must be non-blank text")
    return value


def _count(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ServingConfigurationError(f"capacity plan {label} must be a positive integer")
    return value


def _decimal(value: object, label: str) -> Decimal:
    try:
        parsed = Decimal(value) if isinstance(value, str) else None
    except InvalidOperation:
        parsed = None
    if parsed is None or not parsed.is_finite() or parsed <= 0:
        raise ServingConfigurationError(f"capacity plan {label} must be a positive decimal string")
    return parsed


def plan_for_measured_card(
    *,
    profile: Any,
    placement: Any,
    recipes: ServingRecipes,
    chairs: Mapping[str, object],
    serving_config_inputs: ServingConfigInputs,
) -> CapacityPlan | None:
    """PREFLIGHT's plan for the card ``SystemGpuProbe`` measured, or ``None``.

    ``None`` when the card was not measured by the probe or no single placement
    tier covers it: the stages then launch every row exactly as written.
    """

    if not getattr(profile, "measured", False):
        return None
    try:
        tier = placement.choose(profile.vram_gib)
    except ValueError:
        return None
    capability = profile.compute_capability
    return derive_capacity_plan(
        vram_gib=profile.vram_gib,
        gpu_count=profile.gpu_count,
        compute_capability=f"{capability[0]}.{capability[1]}" if capability else None,
        tier=tier.identifier,
        engine_memory_fraction=tier.recipe.engine_memory_fraction,
        recipes=recipes,
        chairs=chairs,
        serving_config_inputs=serving_config_inputs,
        max_num_seqs_cap=planned_ceiling(tier),
    )
