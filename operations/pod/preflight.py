"""GPU, cache, placement, and smoke-read preflight with honest red reports."""

from __future__ import annotations

import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Protocol

from common.chairs.errors import CacheRevisionRefusal, DigestMismatchRefusal
from common.chairs.models import AbsentChair, ChairIdentity, ModelsConfig

from .models import as_decimal

PLACEMENT_SCHEMA = "pod-placement.v1"


class PlacementRefusal(ValueError):
    """The configured placement table cannot produce one honest single-resident plan."""


class CacheMismatch(RuntimeError):
    """A pod-owned verifier says a named chair cache differs from its pin."""


@dataclass(frozen=True, slots=True)
class GpuProfile:
    """Measured environment facts; tests supply synthetic profiles."""

    name: str
    cuda_version: str | None
    driver_version: str | None
    compute_capability: tuple[int, int] | None
    vram_gib: Decimal
    disk_gib: Decimal
    dtype: str
    discovery_detail: str = ""
    """Why measurement failed, when it did.  The probe is the only place that
    sees the driver's own error text, and spec 04 asks a red report for "what
    happened" as well as "what to do next"."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "vram_gib", as_decimal(self.vram_gib, "VRAM GiB"))
        object.__setattr__(self, "disk_gib", as_decimal(self.disk_gib, "disk GiB"))
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("GPU profile name must be non-blank")
        if not isinstance(self.dtype, str) or not self.dtype.strip():
            raise ValueError("dtype must be non-blank")
        if self.compute_capability is not None:
            major, minor = self.compute_capability
            if (
                not isinstance(major, int)
                or isinstance(major, bool)
                or not isinstance(minor, int)
                or isinstance(minor, bool)
                or major < 0
                or minor < 0
            ):
                raise ValueError("compute capability must be a non-negative major/minor pair")


class SystemGpuProbe:
    """Small production probe; its failed observation feeds a red preflight report.

    This class measures rather than assumes CUDA/driver/VRAM facts.  It has an
    injectable command runner so no GPU is needed to exercise its parsing path.
    """

    def __init__(
        self,
        *,
        disk_path: str | Path,
        runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
        disk_usage: Callable[[Path], Any] | None = None,
    ) -> None:
        self.disk_path = Path(disk_path)
        self.runner = runner or self._run
        self.disk_usage = disk_usage or shutil.disk_usage

    def profile(self, dtype: str) -> GpuProfile:
        disk_detail = ""
        try:
            disk_gib = Decimal(self.disk_usage(self.disk_path).free) / Decimal(1024**3)
        except Exception as error:
            disk_gib = Decimal("0")
            disk_detail = f"disk: {type(error).__name__}: {error}".strip()
        try:
            query = self.runner(
                [
                    "nvidia-smi",
                    "--query-gpu=name,driver_version,memory.total,compute_cap",
                    "--format=csv,noheader,nounits",
                ]
            )
            if query.returncode != 0:
                raise RuntimeError(query.stderr.strip() or "nvidia-smi query failed")
            fields = [field.strip() for field in query.stdout.splitlines()[0].split(",")]
            if len(fields) != 4:
                raise RuntimeError(
                    "nvidia-smi did not return name, driver, VRAM, compute capability"
                )
            name, driver, vram, capability = fields
            major_text, minor_text = capability.split(".", 1)
            basic = self.runner(["nvidia-smi"])
            cuda = _cuda_version(basic.stdout) if basic.returncode == 0 else None
            return GpuProfile(
                name=name,
                cuda_version=cuda,
                driver_version=driver or None,
                compute_capability=(int(major_text), int(minor_text)),
                # nvidia-smi's memory.total is MiB even with --format=...,nounits
                # (nounits only strips the text suffix, it does not rescale).
                vram_gib=Decimal(vram) / Decimal(1024),
                disk_gib=disk_gib,
                dtype=dtype,
                discovery_detail=disk_detail,
            )
        except Exception as error:
            gpu_detail = f"{type(error).__name__}: {error}".strip()
            return GpuProfile(
                name="GPU discovery unavailable",
                cuda_version=None,
                driver_version=None,
                compute_capability=None,
                vram_gib=Decimal("0"),
                disk_gib=disk_gib,
                dtype=dtype,
                discovery_detail=f"{gpu_detail}; {disk_detail}" if disk_detail else gpu_detail,
            )

    # A hung `nvidia-smi` -- a wedged driver, a card mid-reset -- would otherwise
    # block preflight forever on a pod that is already billing, and the red
    # `GpuProfile` path below would never be reached.  `TimeoutExpired` is an
    # `Exception`, so the handler in `profile` records it in `discovery_detail`
    # like any other discovery failure.  Found by CodeRabbit on this branch.
    _RUN_TIMEOUT_SECONDS = 30.0

    @classmethod
    def _run(cls, argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv,
            text=True,
            capture_output=True,
            check=False,
            timeout=cls._RUN_TIMEOUT_SECONDS,
        )


def _cuda_version(output: str) -> str | None:
    match = re.search(r"CUDA Version:\s*([0-9]+(?:\.[0-9]+)?)", output)
    return match.group(1) if match else None


@dataclass(frozen=True, slots=True)
class PlacementRecipe:
    """One model at a time, with resource limits supplied solely by config."""

    engine_memory_fraction: Decimal
    context_cap: int
    pixel_cap: int
    batch_size: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "engine_memory_fraction",
            as_decimal(self.engine_memory_fraction, "engine memory fraction"),
        )
        if not Decimal("0") < self.engine_memory_fraction <= Decimal("1"):
            raise PlacementRefusal("engine memory fraction must be in (0, 1]")
        for label, value in (
            ("context cap", self.context_cap),
            ("pixel cap", self.pixel_cap),
            ("batch size", self.batch_size),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise PlacementRefusal(f"{label} must be a positive integer")


@dataclass(frozen=True, slots=True)
class PlacementTier:
    """A nonoverlapping capability band, never a model-selection mechanism."""

    identifier: str
    min_vram_gib: Decimal
    max_vram_gib_exclusive: Decimal | None
    residency: str
    detector_device: str
    recipe: PlacementRecipe

    def __post_init__(self) -> None:
        object.__setattr__(self, "min_vram_gib", as_decimal(self.min_vram_gib, "tier minimum VRAM"))
        if self.max_vram_gib_exclusive is not None:
            object.__setattr__(
                self,
                "max_vram_gib_exclusive",
                as_decimal(self.max_vram_gib_exclusive, "tier maximum VRAM"),
            )
            if self.max_vram_gib_exclusive <= self.min_vram_gib:
                raise PlacementRefusal("tier maximum VRAM must exceed its minimum")
        if not isinstance(self.identifier, str) or not self.identifier.strip():
            raise PlacementRefusal("tier id must be non-blank")
        if self.residency != "single":
            raise PlacementRefusal("placement requires exactly one resident model")
        if self.detector_device != "cpu":
            raise PlacementRefusal("the detector must remain CPU-only in every tier")

    def covers(self, vram_gib: Decimal) -> bool:
        return vram_gib >= self.min_vram_gib and (
            self.max_vram_gib_exclusive is None or vram_gib < self.max_vram_gib_exclusive
        )


@dataclass(frozen=True, slots=True)
class CardProfile:
    """A prebuilt profile for a card actually rented, with its price-sheet entry.

    Spec 04 ships these for the cards this project rents and falls back to
    computed placement for anything else. The profile names a tier; it never
    names a *model*, so nothing here selects among chairs or witnesses.
    """

    name: str
    gpu_type_id: str
    vram_gib: Decimal
    hourly_usd: Decimal
    tier: str
    note: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "vram_gib", as_decimal(self.vram_gib, "card profile VRAM"))
        object.__setattr__(
            self, "hourly_usd", as_decimal(self.hourly_usd, "card profile hourly price")
        )
        for label, value in (
            ("name", self.name),
            ("gpu_type_id", self.gpu_type_id),
            ("tier", self.tier),
        ):
            if not isinstance(value, str) or not value.strip():
                raise PlacementRefusal(f"card profile {label} must be non-blank")
        if self.vram_gib <= 0 or self.hourly_usd <= 0:
            raise PlacementRefusal("card profile VRAM and hourly price must be positive")


@dataclass(frozen=True, slots=True)
class PlacementTable:
    """Closed configuration table used to compute resource limits from measured VRAM."""

    dtype_floors: dict[str, tuple[int, int]]
    tiers: tuple[PlacementTier, ...]
    card_profiles: tuple[CardProfile, ...] = ()

    def choose(self, vram_gib: Decimal) -> PlacementTier:
        matches = [tier for tier in self.tiers if tier.covers(vram_gib)]
        if len(matches) != 1:
            raise PlacementRefusal(
                f"measured {vram_gib} GiB VRAM maps to {len(matches)} placement tiers, not exactly one"
            )
        return matches[0]

    def dtype_floor(self, dtype: str) -> tuple[int, int]:
        try:
            return self.dtype_floors[dtype]
        except KeyError as error:
            raise PlacementRefusal(
                f"dtype {dtype!r} has no configured compute-capability floor"
            ) from error

    def profile_for(self, card_name: str | None) -> CardProfile | None:
        """The prebuilt profile whose `name` or `gpu_type_id` the card reported.

        `None` for every unknown card, which is spec 04's stated behaviour: an
        unknown card falls back to computed placement rather than to a guess.
        """

        if not card_name:
            return None
        for profile in self.card_profiles:
            if card_name in {profile.name, profile.gpu_type_id}:
                return profile
        return None

    def tier_named(self, identifier: str) -> PlacementTier:
        for tier in self.tiers:
            if tier.identifier == identifier:
                return tier
        raise PlacementRefusal(f"card profile names an undefined placement tier {identifier!r}")

    def price_for(self, gpu_type_id: str) -> Decimal:
        """The reviewed hourly price for one `gpuTypeId`, or a named refusal.

        There is no live "quote this GPU" endpoint, so this table *is* the price
        sheet. A card with no reviewed row cannot be priced, and an unpriced card
        cannot pass a spend ceiling — which is the intended posture.
        """

        for profile in self.card_profiles:
            if profile.gpu_type_id == gpu_type_id:
                return profile.hourly_usd
        raise PlacementRefusal(
            f"no reviewed card_profile in config/pod_placement.toml prices gpuTypeId {gpu_type_id!r}"
        )


def load_placement_table(path: str | Path) -> PlacementTable:
    """Load a strict data-only placement table; no GPU name or chair is selected here."""

    source = Path(path)
    try:
        with source.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise PlacementRefusal(f"cannot read placement table {source}: {error}") from error
    if not isinstance(raw, dict) or set(raw) - {"card_profile"} != {
        "schema",
        "dtype_floor",
        "tiers",
    }:
        raise PlacementRefusal(
            "placement table must contain only schema, dtype_floor, tiers and optional card_profile"
        )
    if raw.get("schema") != PLACEMENT_SCHEMA:
        raise PlacementRefusal(f"placement schema must be {PLACEMENT_SCHEMA!r}")
    floors_raw = raw["dtype_floor"]
    if not isinstance(floors_raw, dict) or not floors_raw:
        raise PlacementRefusal("dtype_floor must be a non-empty table")
    floors: dict[str, tuple[int, int]] = {}
    for dtype, text in floors_raw.items():
        if not isinstance(dtype, str) or not isinstance(text, str) or text.count(".") != 1:
            raise PlacementRefusal("each dtype floor must be a string such as '8.0'")
        major_text, minor_text = text.split(".")
        try:
            major, minor = int(major_text), int(minor_text)
        except ValueError as error:
            raise PlacementRefusal(f"invalid compute capability floor {text!r}") from error
        if major < 0 or minor < 0:
            raise PlacementRefusal("compute capability floor cannot be negative")
        floors[dtype] = (major, minor)
    tiers_raw = raw["tiers"]
    if not isinstance(tiers_raw, list) or not tiers_raw:
        raise PlacementRefusal("tiers must be a non-empty array")
    tiers: list[PlacementTier] = []
    for raw_tier in tiers_raw:
        if not isinstance(raw_tier, dict):
            raise PlacementRefusal("each tier must be a table")
        allowed = {
            "id",
            "min_vram_gib",
            "max_vram_gib_exclusive",
            "residency",
            "detector_device",
            "recipe",
        }
        unknown = sorted(set(raw_tier) - allowed)
        if unknown:
            raise PlacementRefusal(f"placement tier has unknown field(s) {unknown}")
        recipe_raw = raw_tier.get("recipe")
        if not isinstance(recipe_raw, dict) or set(recipe_raw) != {
            "engine_memory_fraction",
            "context_cap",
            "pixel_cap",
            "batch_size",
        }:
            raise PlacementRefusal("placement tier recipe has missing or unknown fields")
        try:
            tiers.append(
                PlacementTier(
                    identifier=raw_tier["id"],
                    min_vram_gib=raw_tier["min_vram_gib"],
                    max_vram_gib_exclusive=raw_tier.get("max_vram_gib_exclusive"),
                    residency=raw_tier["residency"],
                    detector_device=raw_tier["detector_device"],
                    recipe=PlacementRecipe(
                        engine_memory_fraction=recipe_raw["engine_memory_fraction"],
                        context_cap=recipe_raw["context_cap"],
                        pixel_cap=recipe_raw["pixel_cap"],
                        batch_size=recipe_raw["batch_size"],
                    ),
                )
            )
        except (KeyError, TypeError, ValueError, PlacementRefusal) as error:
            if isinstance(error, PlacementRefusal):
                raise
            raise PlacementRefusal(f"invalid placement tier: {error}") from error
    _validate_tiers(tiers)
    table = PlacementTable(floors, tuple(tiers), _load_card_profiles(raw.get("card_profile")))
    for profile in table.card_profiles:
        tier = table.tier_named(profile.tier)
        if not tier.covers(profile.vram_gib):
            raise PlacementRefusal(
                f"card profile {profile.name!r} has {profile.vram_gib} GiB, which its named tier "
                f"{tier.identifier!r} does not cover"
            )
    return table


def _load_card_profiles(raw: object) -> tuple[CardProfile, ...]:
    """Optional, but strictly shaped when present. Unknown fields refuse."""

    if raw is None:
        return ()
    if not isinstance(raw, list) or not raw:
        raise PlacementRefusal("card_profile must be a non-empty array of tables when present")
    allowed = {"name", "gpu_type_id", "vram_gib", "hourly_usd", "tier", "note"}
    profiles: list[CardProfile] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise PlacementRefusal("each card_profile must be a table")
        unknown = sorted(set(entry) - allowed)
        if unknown:
            raise PlacementRefusal(f"card_profile has unknown field(s) {unknown}")
        missing = sorted({"name", "gpu_type_id", "vram_gib", "hourly_usd", "tier"} - set(entry))
        if missing:
            raise PlacementRefusal(f"card_profile is missing field(s) {missing}")
        if not isinstance(entry["hourly_usd"], str):
            raise PlacementRefusal(
                "card_profile hourly_usd must be a decimal string, not a TOML number"
            )
        try:
            profiles.append(
                CardProfile(
                    name=entry["name"],
                    gpu_type_id=entry["gpu_type_id"],
                    vram_gib=entry["vram_gib"],
                    hourly_usd=entry["hourly_usd"],
                    tier=entry["tier"],
                    note=entry.get("note", ""),
                )
            )
        except (TypeError, ValueError) as error:
            raise PlacementRefusal(f"invalid card_profile: {error}") from error
    names = [profile.name for profile in profiles]
    gpu_ids = [profile.gpu_type_id for profile in profiles]
    if len(set(names)) != len(names) or len(set(gpu_ids)) != len(gpu_ids):
        raise PlacementRefusal("card_profile name and gpu_type_id values must each be unique")
    return tuple(profiles)


def _validate_tiers(tiers: list[PlacementTier]) -> None:
    identifiers = [tier.identifier for tier in tiers]
    if len(identifiers) != len(set(identifiers)):
        raise PlacementRefusal("placement tier ids must be unique")
    ordered = sorted(tiers, key=lambda tier: tier.min_vram_gib)
    for left, right in zip(ordered[:-1], ordered[1:], strict=True):
        if left.max_vram_gib_exclusive is None:
            raise PlacementRefusal("only the last placement tier may be unbounded")
        if left.max_vram_gib_exclusive != right.min_vram_gib:
            raise PlacementRefusal("placement tiers must meet exactly, without gaps or overlap")


@dataclass(frozen=True, slots=True)
class UtilizationSample:
    """An instrument reading; no threshold here declares a card 'saturated'."""

    gpu_percent: Decimal
    cpu_percent: Decimal

    def __post_init__(self) -> None:
        for field_name, label in (
            ("gpu_percent", "GPU utilization"),
            ("cpu_percent", "CPU utilization"),
        ):
            parsed = as_decimal(getattr(self, field_name), label)
            if parsed > 100:
                raise ValueError(f"{label} cannot exceed 100 percent")
            object.__setattr__(self, field_name, parsed)


@dataclass(frozen=True, slots=True)
class SmokeResult:
    """A stochastic golden-page read checked for shape, non-emptiness, and format only."""

    shape_valid: bool
    nonempty: bool
    format_valid: bool
    receipt: dict[str, object]
    utilization: tuple[UtilizationSample, ...]

    def __post_init__(self) -> None:
        for label, value in (
            ("shape_valid", self.shape_valid),
            ("nonempty", self.nonempty),
            ("format_valid", self.format_valid),
        ):
            if not isinstance(value, bool):
                raise ValueError(f"smoke result {label} must be boolean")
        if not isinstance(self.receipt, dict):
            raise ValueError("smoke result receipt must be an object")
        if not isinstance(self.utilization, tuple) or not all(
            isinstance(sample, UtilizationSample) for sample in self.utilization
        ):
            raise ValueError("smoke result utilization must contain typed samples")


class ChairCacheVerifier(Protocol):
    """One exact chair cache at a time; a mismatch receives one explicit repair."""

    def verify(self, identity: ChairIdentity) -> dict[str, object]:
        """Return an identity-bound verification receipt or raise a named refusal."""

    def refetch_once(self, identity: ChairIdentity) -> None:
        """Stage one fresh fetch of the exact same pin, never a replacement chair."""


class SmokeReader(Protocol):
    """Serving-manager seam; production must actually read the given proof page."""

    def read(self, identity: ChairIdentity, fixture: Path, placement: PlacementTier) -> SmokeResult:
        """Return one stochastic shape/format receipt and raw utilization samples."""


@dataclass(frozen=True, slots=True)
class PreflightIssue:
    """A red fact and plain-language next action, optionally naming its chair."""

    code: str
    message: str
    remediation: str
    chair: str | None = None


@dataclass(frozen=True, slots=True)
class ChairPlacement:
    """A resource plan for an already configured chair, never a roster choice."""

    chair: str
    configured_serving_recipe: str | None
    tier: str | None
    residency: str | None
    engine_memory_fraction: Decimal | None
    context_cap: int | None
    pixel_cap: int | None
    batch_size: int | None
    state: str


@dataclass(frozen=True, slots=True)
class PreflightReport:
    """One artifact, green only when every measured/preflight condition passed."""

    color: str
    profile: GpuProfile
    tier: str | None
    placements: tuple[ChairPlacement, ...]
    cache_receipts: tuple[dict[str, object], ...]
    smoke_receipts: tuple[dict[str, object], ...]
    utilization: tuple[UtilizationSample, ...]
    issues: tuple[PreflightIssue, ...]
    assembly_proven: bool
    card_profile: str | None = None
    card_profile_note: str | None = None
    plan_source: str = "computed from measured VRAM"

    def to_record(self) -> dict[str, object]:
        return {
            "color": self.color,
            "environment": {
                "gpu": self.profile.name,
                "cuda_version": self.profile.cuda_version,
                "driver_version": self.profile.driver_version,
                "compute_capability": self.profile.compute_capability,
                "vram_gib": str(self.profile.vram_gib),
                "disk_gib": str(self.profile.disk_gib),
                "dtype": self.profile.dtype,
                "discovery_detail": self.profile.discovery_detail or None,
            },
            "placement_tier": self.tier,
            # Spec 04: "the preflight report says which plan it chose and why."
            "placement_card_profile": self.card_profile,
            "placement_card_profile_note": self.card_profile_note,
            "placement_plan_source": self.plan_source,
            "placements": [
                {
                    "chair": item.chair,
                    "configured_serving_recipe": item.configured_serving_recipe,
                    "tier": item.tier,
                    "residency": item.residency,
                    "engine_memory_fraction": (
                        str(item.engine_memory_fraction)
                        if item.engine_memory_fraction is not None
                        else None
                    ),
                    "context_cap": item.context_cap,
                    "pixel_cap": item.pixel_cap,
                    "batch_size": item.batch_size,
                    "state": item.state,
                }
                for item in self.placements
            ],
            "cache_receipts": list(self.cache_receipts),
            "smoke_receipts": list(self.smoke_receipts),
            "utilization": [
                {"gpu_percent": str(sample.gpu_percent), "cpu_percent": str(sample.cpu_percent)}
                for sample in self.utilization
            ],
            "issues": [
                {
                    "code": issue.code,
                    "chair": issue.chair,
                    "message": issue.message,
                    "remediation": issue.remediation,
                }
                for issue in self.issues
            ],
            "assembly_proven": self.assembly_proven,
            "assembly_note": (
                "real chair/GPU assembly was measured"
                if self.assembly_proven
                else "fixture-only result; no real chair or GPU assembly is proven"
            ),
        }


class PreflightRunner:
    """Run all bounded checks and return one report rather than silently dropping a chair."""

    def __init__(
        self,
        models: ModelsConfig,
        placement: PlacementTable,
        cache_verifier: ChairCacheVerifier,
        smoke_reader: SmokeReader,
        fixture: str | Path,
    ) -> None:
        self.models = models
        self.placement = placement
        self.cache_verifier = cache_verifier
        self.smoke_reader = smoke_reader
        self.fixture = Path(fixture)

    def run(self, profile: GpuProfile) -> PreflightReport:
        issues: list[PreflightIssue] = []
        placements: list[ChairPlacement] = []
        cache_receipts: list[dict[str, object]] = []
        smoke_receipts: list[dict[str, object]] = []
        utilization: list[UtilizationSample] = []
        tier: PlacementTier | None = self._environment(profile, issues)
        matched = self.placement.profile_for(profile.name)
        plan_source = "computed from measured VRAM"
        if matched is not None and tier is not None:
            if matched.tier == tier.identifier:
                plan_source = f"prebuilt card profile {matched.name!r}"
            else:
                # The profile and the measurement disagree. Say so and keep the
                # measurement: a profile is a planning convenience, and the card
                # that actually arrived is the fact.
                plan_source = (
                    f"computed from measured VRAM; prebuilt profile {matched.name!r} expects tier "
                    f"{matched.tier!r} but {profile.vram_gib} GiB was measured"
                )
        fixture_present = self.fixture.is_file()
        if not fixture_present:
            issues.append(
                PreflightIssue(
                    "proof-fixture-missing",
                    f"golden-page fixture {self.fixture} is missing",
                    "Restore the named proof fixture before attempting a smoke read.",
                )
            )
        for role, configured in sorted(self.models.chairs.items()):
            if isinstance(configured, AbsentChair):
                placements.append(
                    ChairPlacement(role, None, None, None, None, None, None, None, "absent")
                )
                continue
            if tier is None:
                placements.append(
                    ChairPlacement(
                        role,
                        configured.serving_recipe,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        "unplanned",
                    )
                )
                continue
            placements.append(
                ChairPlacement(
                    role,
                    configured.serving_recipe,
                    tier.identifier,
                    tier.residency,
                    tier.recipe.engine_memory_fraction,
                    tier.recipe.context_cap,
                    tier.recipe.pixel_cap,
                    tier.recipe.batch_size,
                    "planned",
                )
            )
            verified = self._verify_cache(configured, issues, cache_receipts)
            if not verified or not fixture_present:
                continue
            self._smoke(configured, tier, issues, smoke_receipts, utilization)
        if not smoke_receipts:
            # An all-absent or fully-failed roster produced placements and no
            # measurements; green here would claim a serving assembly nobody
            # smoke-read (GOVERNANCE 10).
            issues.append(
                PreflightIssue(
                    "no-chair-verified",
                    "no configured chair completed cache verification and a smoke read; "
                    "this preflight measured no serving assembly at all",
                    "Configure at least one chair with a verified cache before a paid run.",
                )
            )
        floor = self.models.witness_floor_status()
        if not floor.meets_floor:
            issues.append(
                PreflightIssue(
                    "witness-floor-unmet",
                    f"configured Attestator chairs ({floor.configured_count}) fall short of "
                    f"the witness floor ({floor.floor})",
                    "Configure the missing Attestator chairs or lower the floor deliberately "
                    "before a paid run.",
                )
            )
        # This stage runs against injected/fake probes and the repository's
        # fixture roster.  It must never turn source labels into a claim that a
        # real GPU/chair assembly was measured; Spec 05 owns that demonstration.
        assembly_proven = False
        return PreflightReport(
            color="green" if not issues else "red",
            profile=profile,
            tier=tier.identifier if tier else None,
            placements=tuple(placements),
            cache_receipts=tuple(cache_receipts),
            smoke_receipts=tuple(smoke_receipts),
            utilization=tuple(utilization),
            issues=tuple(issues),
            assembly_proven=assembly_proven,
            card_profile=matched.name if matched is not None else None,
            card_profile_note=matched.note if matched is not None and matched.note else None,
            plan_source=plan_source,
        )

    def _environment(
        self, profile: GpuProfile, issues: list[PreflightIssue]
    ) -> PlacementTier | None:
        if not profile.cuda_version or not profile.driver_version:
            issues.append(
                PreflightIssue(
                    "cuda-driver-missing",
                    "CUDA or the GPU driver was not measured."
                    + (
                        f" Discovery reported: {profile.discovery_detail}"
                        if profile.discovery_detail
                        else ""
                    ),
                    "Install a compatible GPU driver/CUDA stack and run preflight again.",
                )
            )
        try:
            floor = self.placement.dtype_floor(profile.dtype)
        except PlacementRefusal as error:
            issues.append(
                PreflightIssue(
                    "dtype-unconfigured",
                    str(error),
                    "Add a reviewed dtype floor to pod_placement.toml.",
                )
            )
            floor = None
        if profile.compute_capability is None:
            issues.append(
                PreflightIssue(
                    "compute-capability-missing",
                    "GPU compute capability was not measured.",
                    "Expose the GPU compute capability and retry; dtype safety cannot be inferred.",
                )
            )
        elif floor is not None and profile.compute_capability < floor:
            issues.append(
                PreflightIssue(
                    "dtype-floor-failed",
                    f"dtype {profile.dtype} requires compute capability {floor[0]}.{floor[1]} or newer; measured {profile.compute_capability[0]}.{profile.compute_capability[1]}.",
                    "Use a GPU meeting the configured dtype floor or choose a separately reviewed dtype configuration.",
                )
            )
        if profile.vram_gib <= 0:
            issues.append(
                PreflightIssue(
                    "vram-missing",
                    "VRAM was not measured as positive.",
                    "Repair GPU discovery and retry.",
                )
            )
        if profile.disk_gib <= 0:
            issues.append(
                PreflightIssue(
                    "disk-missing",
                    "Disk capacity was not measured as positive."
                    + (
                        f" Discovery reported: {profile.discovery_detail}"
                        if profile.discovery_detail
                        else ""
                    ),
                    "Attach or provision usable disk and retry.",
                )
            )
        try:
            return self.placement.choose(profile.vram_gib)
        except PlacementRefusal as error:
            issues.append(
                PreflightIssue(
                    "placement-refused",
                    str(error),
                    "Use a covered GPU profile or extend the reviewed placement table.",
                )
            )
            return None

    def _verify_cache(
        self,
        identity: ChairIdentity,
        issues: list[PreflightIssue],
        receipts: list[dict[str, object]],
    ) -> bool:
        try:
            receipt = self.cache_verifier.verify(identity)
        except Exception as initial_error:
            if not is_cache_mismatch(initial_error):
                issues.append(
                    PreflightIssue(
                        "cache-verification-failed",
                        f"chair {identity.role} cache verification failed: {initial_error}",
                        "Repair the named chair cache and retry preflight.",
                        identity.role,
                    )
                )
                return False
            try:
                self.cache_verifier.refetch_once(identity)
                receipt = self.cache_verifier.verify(identity)
            except Exception as retry_error:
                issues.append(
                    PreflightIssue(
                        "cache-mismatch-after-refetch",
                        f"chair {identity.role} still differs from its pinned digest after one re-fetch: {retry_error}",
                        "Inspect the named cache and pinned manifest; do not substitute a chair or revision.",
                        identity.role,
                    )
                )
                return False
            normalized = self._bound_receipt(identity, receipt, issues, "cache")
            if normalized is None:
                return False
            receipts.append({"chair": identity.role, "repaired_once": True, **normalized})
            return True
        normalized = self._bound_receipt(identity, receipt, issues, "cache")
        if normalized is None:
            return False
        receipts.append({"chair": identity.role, "repaired_once": False, **normalized})
        return True

    @staticmethod
    def _bound_receipt(
        identity: ChairIdentity,
        receipt: object,
        issues: list[PreflightIssue],
        kind: str,
    ) -> dict[str, object] | None:
        """Keep a returned receipt from replacing the chair that produced it."""

        if not isinstance(receipt, dict):
            issues.append(
                PreflightIssue(
                    f"{kind}-receipt-invalid",
                    f"chair {identity.role} returned a non-object {kind} receipt.",
                    f"Repair the named chair's {kind} adapter so it returns identity-bound evidence.",
                    identity.role,
                )
            )
            return None
        reported_chair = receipt.get("chair", identity.role)
        if reported_chair != identity.role:
            issues.append(
                PreflightIssue(
                    f"{kind}-receipt-misbound",
                    f"chair {identity.role} returned a {kind} receipt naming {reported_chair!r}.",
                    "Repair the adapter; evidence from one chair cannot be recorded under another.",
                    identity.role,
                )
            )
            return None
        if kind == "cache" and "repaired_once" in receipt:
            issues.append(
                PreflightIssue(
                    "cache-receipt-invalid",
                    f"chair {identity.role} returned the runtime-owned repaired_once field.",
                    "Repair the cache adapter; retry accounting belongs to the preflight runtime.",
                    identity.role,
                )
            )
            return None
        return {key: value for key, value in receipt.items() if key != "chair"}

    def _smoke(
        self,
        identity: ChairIdentity,
        tier: PlacementTier,
        issues: list[PreflightIssue],
        receipts: list[dict[str, object]],
        utilization: list[UtilizationSample],
    ) -> None:
        try:
            result = self.smoke_reader.read(identity, self.fixture, tier)
        except Exception as error:
            issues.append(
                PreflightIssue(
                    "smoke-read-failed",
                    f"chair {identity.role} could not read the golden proof page: {error}",
                    "Repair the named chair service and rerun the golden-page smoke read.",
                    identity.role,
                )
            )
            return
        if not isinstance(result, SmokeResult):
            issues.append(
                PreflightIssue(
                    "smoke-receipt-invalid",
                    f"chair {identity.role} returned no structured smoke result.",
                    "Repair the named chair service so shape, format, and utilization are all measurable.",
                    identity.role,
                )
            )
            return
        receipt = self._bound_receipt(identity, result.receipt, issues, "smoke")
        if receipt is None:
            return
        utilization.extend(result.utilization)
        if not result.utilization:
            issues.append(
                PreflightIssue(
                    "utilization-missing",
                    f"chair {identity.role} smoke read returned no GPU/CPU utilization samples.",
                    "Repair utilization sampling and rerun; an unmeasured value is not a pass.",
                    identity.role,
                )
            )
        if not result.shape_valid or not result.nonempty or not result.format_valid:
            failed = []
            if not result.shape_valid:
                failed.append("shape")
            if not result.nonempty:
                failed.append("nonempty")
            if not result.format_valid:
                failed.append("format")
            issues.append(
                PreflightIssue(
                    "smoke-output-invalid",
                    f"chair {identity.role} golden-page smoke read failed {', '.join(failed)} validation.",
                    "Repair the named chair service; no quality downgrade or chair removal was applied.",
                    identity.role,
                )
            )
        receipts.append(
            {
                "chair": identity.role,
                **receipt,
                "utilization": [
                    {
                        "gpu_percent": str(sample.gpu_percent),
                        "cpu_percent": str(sample.cpu_percent),
                    }
                    for sample in result.utilization
                ],
            }
        )


def is_cache_mismatch(error: BaseException) -> bool:
    return isinstance(error, (CacheMismatch, DigestMismatchRefusal, CacheRevisionRefusal))
