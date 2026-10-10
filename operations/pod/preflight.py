"""GPU, cache, placement, and smoke-read preflight with honest red reports."""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time
import tomllib
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Protocol

from common.chairs.errors import CacheRevisionRefusal, DigestMismatchRefusal, DiskSpaceRefusal
from common.chairs.models import AbsentChair, ChairIdentity, DigestManifest, ModelsConfig

if TYPE_CHECKING:
    from operations.serving.config import ServingRecipes

from .chair_order import in_stage_need_order
from .models import as_decimal

PLACEMENT_SCHEMA = "pod-placement.v1"

FIXTURE_ONLY_ASSEMBLY_NOTE = "fixture-only result; no real chair or GPU assembly is proven"
"""What a preflight that measured no real card and served no real chair says of itself.

Kept as one constant because it is the sentence a receipt publishes about its
own worth, and `assembly_proven` is derived rather than declared: the note and
the flag must never be able to drift apart.
"""


class _RuntimeProvenance:
    """Opaque proof that a runtime in this package produced the value it sits on.

    `assembly_proven` is the one claim a preflight receipt makes about a paid
    measurement. It rests on two facts, `GpuProfile.measured` and
    `SmokeResult.served_by`, and each can be set only together with an instance
    of this class: both are constructor arguments that a caller of
    `PreflightRunner.run` supplies, so without the token a caller-built profile
    and smoke result could publish "real assembly measured on <card>" with no
    `nvidia-smi` read and no served engine. (`_bound_receipt` guards the page;
    this guards the claim about the hardware.) The token is:

    * module-private, and never exported, so no public name reaches it;
    * minted in exactly two places -- `SystemGpuProbe.profile`'s successful
      `nvidia-smi` path, and `operations.serving.preflight`'s service-evidence
      path, which names the engine off a `ServiceHandle` it started, proved
      fixture-bound and stopped;
    * checked by `isinstance`, never by a string, a flag or a truthy value;
    * never serialised. It appears in no receipt, no record and no JSON, so it
      cannot be captured from an artifact and replayed into a constructor.

    This is a guard against a caller asserting its own proof, not a security
    boundary: Python has no private, and a determined caller can always reach a
    module-private name. That is the same posture `served_engine` takes on
    its receipt -- what it buys is that a
    fixture, an operator rehearsal, a test double, or a future adapter cannot
    claim a measured card *by accident* or by filling in an inviting field.
    """

    __slots__ = ("origin",)

    def __init__(self, origin: str) -> None:
        self.origin = origin

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"<runtime provenance: {self.origin}>"


def _mint_runtime_provenance(origin: str) -> _RuntimeProvenance:
    """The only constructor either minting site calls; `origin` names which.

    `operations.serving.preflight` imports this deliberately private name. The
    two runtimes that may mint provenance live in two packages, and the token
    they mint has to be the same type for `_assembly_claim` to check it by
    identity -- so the import is the seam, and its privacy is the statement
    that nothing else may call it.
    """

    return _RuntimeProvenance(origin)


def _is_runtime_provenance(value: object) -> bool:
    """One `isinstance` check, in one place, for both halves of the claim."""

    return isinstance(value, _RuntimeProvenance)


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
    """Why measurement failed, when it did: the probe is the only place that
    sees the driver's own error text, and a red report must say what happened
    as well as what to do next."""
    measured: bool = False
    """True only when a real driver read produced these numbers.

    `SystemGpuProbe.profile` sets it on its success path and nowhere else, so a
    synthetic profile -- an operator fixture, a test, a hand-built planning
    profile -- carries `False` by construction rather than by remembering to say
    so.  `PreflightReport.assembly_proven` reads it: a receipt may not claim a
    real GPU was measured on the strength of a number somebody typed.

    It cannot be set without `provenance`, and `provenance` cannot be minted
    outside this module, so this flag states where the profile came from, not
    what its constructor was told.
    """
    provenance: object | None = field(default=None, repr=False, compare=False)
    """The probe's own opaque token, or `None`.  Never serialised.

    Typed `object` on purpose: `_RuntimeProvenance` is module-private, and a
    public annotation naming it would put it in this module's exported surface
    and in every reader's autocomplete.  `__post_init__` refuses anything else.
    """
    gpu_count: int = 1
    """How many cards `nvidia-smi` reported. A single query prints one line
    per visible GPU; reading only the first would silently measure one card's
    VRAM as the machine's whole placement-deciding number. Default 1 for a
    synthetic/unmeasured profile, matching this build's one-GPU assumption
    everywhere else."""

    def __post_init__(self) -> None:
        # The two halves of one fact, so neither can be set alone: a caller that
        # passes `measured=True` has no token to pass with it and is refused
        # here, and a token can only come from the one probe path that mints it.
        if self.provenance is not None and not _is_runtime_provenance(self.provenance):
            raise ValueError("GPU profile provenance is minted by the probe, not supplied")
        if self.measured != (self.provenance is not None):
            raise ValueError(
                "a GPU profile is measured exactly when it carries the probe's own "
                "provenance; a profile cannot declare itself measured"
            )
        object.__setattr__(self, "vram_gib", as_decimal(self.vram_gib, "VRAM GiB"))
        object.__setattr__(self, "disk_gib", as_decimal(self.disk_gib, "disk GiB"))
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("GPU profile name must be non-blank")
        if not isinstance(self.dtype, str) or not self.dtype.strip():
            raise ValueError("dtype must be non-blank")
        if (
            not isinstance(self.gpu_count, int)
            or isinstance(self.gpu_count, bool)
            or self.gpu_count < 1
        ):
            raise ValueError("GPU profile gpu_count must be a positive integer")
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

    def profile(self, dtype: str, *, expected_gpu_count: int | None = None) -> GpuProfile:
        """Measure the visible card(s).

        `expected_gpu_count`, when the caller knows it (the create request's
        own `gpuCount`), is checked against what `nvidia-smi` actually
        measured rather than left as two independent numbers.
        """
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
            lines = [line for line in query.stdout.splitlines() if line.strip()]
            if not lines:
                raise RuntimeError("nvidia-smi returned no GPU lines")
            rows = []
            for line in lines:
                fields = [field.strip() for field in line.split(",")]
                if len(fields) != 4:
                    raise RuntimeError(
                        "nvidia-smi did not return name, driver, VRAM, compute capability "
                        f"for every visible card (line {line!r})"
                    )
                rows.append(tuple(fields))
            gpu_count = len(rows)
            baseline = rows[0]
            if any(row != baseline for row in rows[1:]):
                raise RuntimeError(
                    f"nvidia-smi reported {gpu_count} non-identical GPUs "
                    f"({sorted(set(rows))}); this build measures and places a "
                    "single, uniform card class"
                )
            if expected_gpu_count is not None and gpu_count != expected_gpu_count:
                raise RuntimeError(
                    f"nvidia-smi measured {gpu_count} GPU(s); the pod was requested "
                    f"with gpuCount={expected_gpu_count}"
                )
            name, driver, vram, capability = baseline
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
                gpu_count=gpu_count,
                # The one place `measured` is ever set: `nvidia-smi` answered
                # with four parseable fields for a card this process can see.
                # The token beside it is what makes that unforgeable from
                # outside this module; the flag without it is refused.
                measured=True,
                provenance=_mint_runtime_provenance("nvidia-smi read by SystemGpuProbe"),
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
    # `GpuProfile` path in `profile` would never be reached.  `TimeoutExpired` is an
    # `Exception`, so the handler in `profile` records it in `discovery_detail`
    # like any other discovery failure.
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
    # The most sequences a capacity plan may launch a row with at this tier
    # (operations/serving/capacity.py); None leaves the plan's own hard cap.
    planned_batch_ceiling: int | None = None

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
        ceiling = self.planned_batch_ceiling
        if ceiling is not None and (
            not isinstance(ceiling, int) or isinstance(ceiling, bool) or ceiling < self.batch_size
        ):
            raise PlacementRefusal(
                "planned batch ceiling must be an integer no smaller than the batch size"
            )


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

    Shipped for the cards this project rents; anything else falls back to
    computed placement. The profile names a tier; it never names a *model*,
    so nothing here selects among chairs or witnesses.
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

        `None` for every unknown card: it falls back to computed placement
        rather than to a guess.
        """

        if not card_name:
            return None
        for profile in self.card_profiles:
            if card_name in {profile.name, profile.gpu_type_id}:
                return profile
        return None

    def profile_for_gpu_type_id(self, gpu_type_id: str | None) -> CardProfile | None:
        """The reviewed row a request's `gpu_type` names, matched on `gpu_type_id` alone.

        Distinct from `profile_for`, and deliberately stricter. `profile_for`
        resolves a card a *probe* reported and accepts either spelling, because
        a probe reports whatever the driver calls the card. This one resolves
        the string a `PodCreateRequest` will send to the provider's API, and
        only `gpu_type_id` is ever sent: `boot_a_request.py` renders
        `"gpu_type": card.gpu_type_id`, and the `name` column exists so an
        operator can read about the card in prose. Accepting the human name
        here would make an allowlist entry out of a string that has never
        reached the API and that no provider is known to accept -- a create
        that passed the gate and then failed, or worse, rented something else.
        """

        if not gpu_type_id:
            return None
        for profile in self.card_profiles:
            if profile.gpu_type_id == gpu_type_id:
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


def load_placement_table(path: str | Path, *, source_bytes: bytes | None = None) -> PlacementTable:
    """Load a strict data-only placement table; no GPU name or chair is selected here.

    `source_bytes` lets a caller that has already read and digested the file parse
    **those exact bytes** rather than the path's current contents. A caller sealing
    a configuration must digest and parse one snapshot: reading twice means the
    digest can describe the sealed bytes while the parsed table describes something
    else entirely, so a run works under a placement it never sealed while every
    check still passes. `path` is still required and is still what a refusal names,
    because a message pointing at no file helps nobody.
    """

    source = Path(path)
    if source_bytes is None:
        try:
            payload = source.read_bytes()
        except OSError as error:
            raise PlacementRefusal(f"cannot read placement table {source}: {error}") from error
        parse_label = "placement table"
    else:
        payload = source_bytes
        parse_label = "supplied placement table bytes for"
    try:
        raw = tomllib.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise PlacementRefusal(f"cannot parse {parse_label} {source}: {error}") from error
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
        if not isinstance(recipe_raw, dict) or set(recipe_raw) - {"planned_batch_ceiling"} != {
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
                        planned_batch_ceiling=recipe_raw.get("planned_batch_ceiling"),
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
    served_by: str | None = None
    """The engine that actually answered, or `None` when nothing served this read.

    Only `operations.serving.preflight.ServingSmokeReader` sets it, from the
    receipt of the service handle it started, proved and stopped -- so a fixture
    reader that fabricates a green `SmokeResult` cannot also fabricate the claim
    that an engine produced it.  `PreflightReport.assembly_proven` reads this.

    It cannot be set without the `provenance` token below, which is minted in
    that one lifecycle path.
    """
    provenance: object | None = field(default=None, repr=False, compare=False)
    """The serving runtime's own opaque token, or `None`.  Never serialised.

    Typed `object` for the same reason `GpuProfile.provenance` is: the class is
    module-private to `operations.pod.preflight` and stays out of this one's
    public annotations.  `__post_init__` refuses anything else.
    """

    def __post_init__(self) -> None:
        for label, value in (
            ("shape_valid", self.shape_valid),
            ("nonempty", self.nonempty),
            ("format_valid", self.format_valid),
        ):
            if not isinstance(value, bool):
                raise ValueError(f"smoke result {label} must be boolean")
        if self.served_by is not None and (
            not isinstance(self.served_by, str) or not self.served_by.strip()
        ):
            raise ValueError("smoke result served_by must be a non-blank string or None")
        # Named engine and token together, or neither -- checked after the shape
        # above, so a blank name is still refused as a blank name. A smoke
        # reader that could name an engine without the lifecycle that started
        # it could assert the serving half of `assembly_proven` on its own
        # say-so.
        if self.provenance is not None and not _is_runtime_provenance(self.provenance):
            raise ValueError("smoke result provenance is minted by the serving runtime")
        if (self.served_by is not None) != (self.provenance is not None):
            raise ValueError(
                "a smoke result names a serving engine exactly when it carries the serving "
                "runtime's own provenance; a reader cannot name its own engine"
            )
        if not isinstance(self.receipt, dict):
            raise ValueError("smoke result receipt must be an object")
        if not isinstance(self.utilization, tuple) or not all(
            isinstance(sample, UtilizationSample) for sample in self.utilization
        ):
            raise ValueError("smoke result utilization must contain typed samples")


class ChairCacheVerifier(Protocol):
    """Verify one exact chair cache at a time."""

    def verify(self, identity: ChairIdentity) -> dict[str, object]:
        """Return an identity-bound verification receipt or raise a named refusal."""

    def manifest(self, identity: ChairIdentity) -> DigestManifest:
        """The chair's pinned digest manifest, the one ``verify`` checks the cache against."""


CachePrefetcher = Callable[[ChairIdentity], dict[str, object]]
"""Verify (filling if needed) the next chair's cache while the current one smokes.

It must never evict another cache, so it cannot take the one being served; one
that finds no room raises `DiskSpaceRefusal` and the chair is verified again,
with eviction, once the smoke has finished.
"""


# How long the next chair's fill may run before preflight calls it stuck: ten
# times what its snapshot takes at the 13.6 GB/min one copy pass was measured at
# (review 01), never less than a quarter of an hour. A fill called stuck is a red
# preflight, so a slow network volume gets the same allowance as the prefill.
PREFETCH_BYTES_PER_SECOND = 13.6e9 / 60 / 10
PREFETCH_MINIMUM_SECONDS = 15 * 60


class PrefetchTimeout(RuntimeError):
    """The background fill of a chair's cache did not finish by its deadline."""


class _CacheLookahead:
    """Verify the next chair's cache on one background thread while the card smokes.

    The card still serves one chair at a time; only the copy and hash of the next
    chair overlap. A prefetch's result, or its refusal, is the verification of
    that chair, exactly as the foreground call would have returned or raised it.
    """

    def __init__(
        self,
        verify: Callable[[ChairIdentity], dict[str, object]],
        prefetch: CachePrefetcher | None,
        order: list[ChairIdentity],
        *,
        snapshot_bytes: Callable[[ChairIdentity], int] = lambda _identity: 0,
        bytes_per_second: float = PREFETCH_BYTES_PER_SECOND,
        minimum_seconds: float = PREFETCH_MINIMUM_SECONDS,
    ) -> None:
        self._verify = verify
        self._prefetch = prefetch
        self._order = order
        self._snapshot_bytes = snapshot_bytes
        self._bytes_per_second = bytes_per_second
        self._minimum_seconds = minimum_seconds
        # Each pending prefetch, and the monotonic time it must be done by.
        self._pending: dict[str, tuple[Future[dict[str, object]], float]] = {}
        # Set once a prefetch overran: its thread cannot be stopped, so nothing
        # more is prefetched and nothing waits for it again.
        self._expired = False

    def verify(self, identity: ChairIdentity) -> dict[str, object]:
        pending = self._pending.pop(identity.role, None)
        if pending is not None:
            future, deadline = pending
            try:
                return future.result(timeout=max(0.0, deadline - time.monotonic()))
            except FutureTimeout as expired:
                # A running prefetch cannot be cancelled: its thread is left to
                # finish or hang on its own, and this chair counts as not filled.
                self._expired = True
                raise PrefetchTimeout(
                    f"chair {identity.role}'s cache fill beside the previous smoke did not "
                    "finish by its deadline; a copy or a store read has stalled"
                ) from expired
            except DiskSpaceRefusal:
                pass  # no room without evicting; verified below, where evicting is safe
        return self._verify(identity)

    def start_next(self, identity: ChairIdentity) -> None:
        """Begin verifying the chair after `identity`, if there is one."""

        if self._prefetch is None or self._expired:
            return
        roles = [chair.role for chair in self._order]
        if identity.role not in roles:
            return
        position = roles.index(identity.role) + 1
        if position >= len(self._order):
            return
        following = self._order[position]
        if following.role not in self._pending:
            try:
                size = self._snapshot_bytes(following)
            except Exception:
                size = 0
            deadline = time.monotonic() + max(self._minimum_seconds, size / self._bytes_per_second)
            self._pending[following.role] = (self._run(self._prefetch, following), deadline)

    @staticmethod
    def _run(prefetch: CachePrefetcher, identity: ChairIdentity) -> Future[dict[str, object]]:
        """Start one prefetch on a daemon thread, so a hung one cannot hold up exit."""

        future: Future[dict[str, object]] = Future()
        future.set_running_or_notify_cancel()

        def work() -> None:
            try:
                future.set_result(prefetch(identity))
            except BaseException as error:  # handed to `verify`, as the call would raise it
                future.set_exception(error)

        threading.Thread(target=work, name="cache-lookahead", daemon=True).start()
        return future

    def close(self) -> None:
        """Let a prefetch nobody collected finish, unless one has already overrun.

        After an overrun nothing waits again: the stuck thread is left behind, so
        preflight still returns its report.
        """

        for future, deadline in self._pending.values():
            if self._expired:
                break
            try:
                future.result(timeout=max(0.0, deadline - time.monotonic()))
            except Exception:
                continue
        self._pending.clear()


SubprocessChecker = Callable[
    [ChairIdentity, Any, Path, Path, list[dict[str, object]]], dict[str, object]
]
"""Run a subprocess chair once on the golden page; return what the run measured.

Called as ``(identity, profile, verified_weights_root, golden_page, manifest_rows)``,
where the rows are the chair's pinned digest manifest.
"""

_MEASURED_RUN_FACTS = ("surya_ocr", "torch", "python", "cpu_capability", "machine")


def check_subprocess_environment(
    identity: ChairIdentity,
    profile: Any,
    weights_root: Path,
    golden_page: Path,
    manifest_rows: list[dict[str, object]],
) -> dict[str, object]:
    """Run the chair's own runner once, on the CPU, over the golden page.

    The run starts with the environment's version check against the row's
    pins, then loads the verified weights and reads one small page, so a broken
    environment or bundle fails here rather than in the paid run after it. The
    weights the run names are checked against the pinned manifest rows, as the
    stage checks them.
    """
    from common.imaging import dimensions
    from operations.serving.surya_detector import run_surya_subprocess

    data = golden_page.read_bytes()
    run = run_surya_subprocess(
        profile,
        weights_root,
        {1: data},
        {1: dimensions(data)},
        identity,
        manifest_rows=manifest_rows,
    )
    page = run.pages[1].document
    return {
        "versions": {key: run.run_facts[key] for key in _MEASURED_RUN_FACTS},
        "engine_version": run.serving_details.engine_version,
        "golden_page": {
            "lines": len(page["text_detection"]["bboxes"]),
            "blocks": len(page["layout"]["bboxes"]),
            "reading_order": page["reading_order"],
        },
    }


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
    assembly_note: str = FIXTURE_ONLY_ASSEMBLY_NOTE
    """What was proven, in the report's own words -- names the chairs and the card.

    Derived beside `assembly_proven` in `PreflightRunner.run`, never re-derived
    from the flag: "proven" and "proven *of what*" are one statement.
    """
    card_profile: str | None = None
    card_profile_note: str | None = None
    plan_source: str = "computed from measured VRAM"
    subprocess_receipts: tuple[dict[str, object], ...] = ()
    """What each subprocess chair's golden-page run measured: its versions and CPU."""

    def to_record(self) -> dict[str, object]:
        return {
            "color": self.color,
            "environment": {
                "gpu": self.profile.name,
                "cuda_version": self.profile.cuda_version,
                "driver_version": self.profile.driver_version,
                "compute_capability": self.profile.compute_capability,
                "vram_gib": str(self.profile.vram_gib),
                "gpu_count": self.profile.gpu_count,
                "disk_gib": str(self.profile.disk_gib),
                "dtype": self.profile.dtype,
                "discovery_detail": self.profile.discovery_detail or None,
            },
            "placement_tier": self.tier,
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
            "subprocess_receipts": list(self.subprocess_receipts),
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
            "assembly_note": self.assembly_note,
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
        *,
        serving_recipes: ServingRecipes | None = None,
        selected_roles: frozenset[str] | None = None,
        subprocess_checker: SubprocessChecker = check_subprocess_environment,
        chair_fixtures: dict[str, str | Path] | None = None,
        cache_prefetcher: CachePrefetcher | None = None,
        prefetch_bytes_per_second: float = PREFETCH_BYTES_PER_SECOND,
        prefetch_minimum_seconds: float = PREFETCH_MINIMUM_SECONDS,
    ) -> None:
        self.models = models
        self.prefetch_bytes_per_second = prefetch_bytes_per_second
        self.prefetch_minimum_seconds = prefetch_minimum_seconds
        # Fills the next chair's cache while the current one smokes; None to
        # verify each chair only when its turn comes.
        self.cache_prefetcher = cache_prefetcher
        self._lookahead: _CacheLookahead | None = None
        self.subprocess_checker = subprocess_checker
        self.placement = placement
        self.cache_verifier = cache_verifier
        self.smoke_reader = smoke_reader
        self.fixture = Path(fixture)
        # A chair named here smoke-reads its own page instead of `fixture` (the
        # DAI chair's RecordGold record); every other chair reads `fixture`.
        self.chair_fixtures = {role: Path(page) for role, page in (chair_fixtures or {}).items()}
        self.serving_recipes = serving_recipes
        self.selected_roles = selected_roles

    def fixture_for(self, role: str) -> Path:
        """The page this chair's smoke reads."""

        return self.chair_fixtures.get(role, self.fixture)

    def run(self, profile: GpuProfile) -> PreflightReport:
        from operations.serving.config import UnsupportedProfile

        issues: list[PreflightIssue] = []
        placements: list[ChairPlacement] = []
        cache_receipts: list[dict[str, object]] = []
        smoke_receipts: list[dict[str, object]] = []
        subprocess_receipts: list[dict[str, object]] = []
        utilization: list[UtilizationSample] = []
        # (chair, engine) for every chair that read the golden page back through
        # an engine that actually served it.  Half of the assembly claim below.
        served_reads: list[tuple[str, str]] = []
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
        # Chairs in the order the stages first need them, so the copy of each
        # can overlap the smoke of the one before it.
        roles = [
            role
            for role in in_stage_need_order(self.models.chairs)
            if self.selected_roles is None or role in self.selected_roles
        ]
        serving_profiles: dict[str, Any] = {}
        for role in roles:
            configured = self.models.chairs[role]
            if isinstance(configured, ChairIdentity) and tier is not None:
                serving_profiles[role] = (
                    self.serving_recipes.for_identity(configured, tier.identifier)
                    if self.serving_recipes is not None
                    else None
                )
        self._lookahead = _CacheLookahead(
            self.cache_verifier.verify,
            self.cache_prefetcher,
            [
                self.models.chairs[role]  # type: ignore[misc]
                for role in roles
                if role in serving_profiles
                and not isinstance(serving_profiles[role], UnsupportedProfile)
            ],
            snapshot_bytes=lambda identity: sum(
                row.size for row in self.cache_verifier.manifest(identity).rows
            ),
            bytes_per_second=self.prefetch_bytes_per_second,
            minimum_seconds=self.prefetch_minimum_seconds,
        )
        try:
            self._run_chairs(
                roles,
                serving_profiles,
                tier,
                fixture_present,
                issues,
                placements,
                cache_receipts,
                smoke_receipts,
                subprocess_receipts,
                utilization,
                served_reads,
            )
        finally:
            self._lookahead.close()
            self._lookahead = None
        if not smoke_receipts and self.selected_roles != frozenset():
            # An all-absent or fully-failed roster produced placements and no
            # measurements; green here would claim a serving assembly nobody
            # smoke-read.
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
            routed = (
                f"; routed chair(s) {list(floor.routed_roles)} read only the pages their rule "
                "sends them and do not count"
                if floor.routed_roles
                else ""
            )
            issues.append(
                PreflightIssue(
                    "witness-floor-unmet",
                    f"configured Attestator chairs reading every page ({floor.configured_count}) "
                    f"fall short of the witness floor ({floor.floor}){routed}",
                    "Configure the missing Attestator chairs or lower the floor deliberately "
                    "before a paid run.",
                )
            )
        assembly_proven, assembly_note = self._assembly_claim(profile, served_reads)
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
            assembly_note=assembly_note,
            card_profile=matched.name if matched is not None else None,
            card_profile_note=matched.note if matched is not None and matched.note else None,
            plan_source=plan_source,
            subprocess_receipts=tuple(subprocess_receipts),
        )

    def _run_chairs(
        self,
        roles: list[str],
        serving_profiles: dict[str, Any],
        tier: PlacementTier | None,
        fixture_present: bool,
        issues: list[PreflightIssue],
        placements: list[ChairPlacement],
        cache_receipts: list[dict[str, object]],
        smoke_receipts: list[dict[str, object]],
        subprocess_receipts: list[dict[str, object]],
        utilization: list[UtilizationSample],
        served_reads: list[tuple[str, str]],
    ) -> None:
        """Place, verify and smoke each selected chair in turn, recording every outcome."""
        from operations.serving.config import (
            InProcessProfile,
            SubprocessProfile,
            UnsupportedProfile,
        )

        for role in roles:
            configured = self.models.chairs[role]
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
            serving_profile = serving_profiles[role]
            if isinstance(serving_profile, UnsupportedProfile):
                placements.append(
                    ChairPlacement(
                        role,
                        configured.serving_recipe,
                        tier.identifier,
                        None,
                        None,
                        None,
                        None,
                        None,
                        "unservable-at-tier",
                    )
                )
                issues.append(
                    PreflightIssue(
                        "chair-unservable-at-tier",
                        f"chair {role} cannot be served at measured tier {tier.identifier}: "
                        f"{serving_profile.reason}",
                        "Select a tier with a supported serving profile for this chair.",
                        role,
                    )
                )
                continue
            if isinstance(serving_profile, InProcessProfile):
                # Loaded by its own stage on the CPU: no card residency to plan and
                # no engine to smoke-read, but its weights are verified like any chair's.
                placements.append(
                    ChairPlacement(
                        role,
                        configured.serving_recipe,
                        tier.identifier,
                        None,
                        None,
                        None,
                        None,
                        None,
                        "in-process",
                    )
                )
                self._verify_cache(configured, issues, cache_receipts)
                self._lookahead.start_next(configured)
                continue
            if isinstance(serving_profile, SubprocessProfile):
                # Never served and never on the card: its weights are verified,
                # then its own runner reads the golden page once on the CPU, so a
                # broken environment or bundle is red before any paid work.
                placements.append(
                    ChairPlacement(
                        role,
                        configured.serving_recipe,
                        tier.identifier,
                        None,
                        None,
                        None,
                        None,
                        None,
                        "subprocess",
                    )
                )
                cache = self._verify_cache(configured, issues, cache_receipts)
                self._lookahead.start_next(configured)
                if cache is not None and fixture_present:
                    self._check_subprocess(
                        configured, serving_profile, cache, issues, subprocess_receipts
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
            self._lookahead.start_next(configured)
            if not verified or not fixture_present:
                continue
            served_by = self._smoke(configured, tier, issues, smoke_receipts, utilization)
            if served_by is not None:
                served_reads.append((role, served_by))

    @staticmethod
    def _assembly_claim(
        profile: GpuProfile, served_reads: list[tuple[str, str]]
    ) -> tuple[bool, str]:
        """Derive the receipt's assembly claim from what this run actually did.

        A constant `False` here, with every result called "fixture-only",
        would be a false record of a paid measurement on a rented card: the
        receipt would disown the one measurement it was bought to make
        (claims are made only about what was actually
        measured, and an understatement is as untrue as an overstatement).

        Both halves must hold, and each is a fact the layer that produced it
        recorded rather than a label this method infers:

        * the card was read by a real driver -- `GpuProfile.measured`, set only
          by `SystemGpuProbe`'s successful `nvidia-smi` path;
        * at least one chair read the golden page back through an engine that
          served it -- `SmokeResult.served_by`, set only by
          `ServingSmokeReader` from the service handle it started and stopped.

        "Set only by" is enforced by `_RuntimeProvenance`, not by convention.
        `PreflightRunner` takes both values from callers -- a caller supplies
        the profile to `run` and the reader to the constructor -- so each
        travels with an opaque token that only those two runtime paths can
        mint, and this method checks the token rather than the flag: a caller-built
        `GpuProfile(measured=True)` and a caller-built
        `SmokeResult(served_by=...)` are refused at construction, and no
        combination of ordinary values reaches `True` here.

        A smoke read that came back invalid does not count: the assembly claim
        says a chair *read its witness*, not that a process was started.  The
        colour of the report is deliberately not consulted -- a red preflight
        that nonetheless served one chair on a real card proved that much, and
        hiding it would lose a measured fact behind a status.
        """

        measured = profile.measured and _is_runtime_provenance(profile.provenance)
        if measured and served_reads:
            chairs = ", ".join(f"{chair} via {engine}" for chair, engine in sorted(served_reads))
            return True, (
                f"real assembly measured on {profile.name}: {chairs} smoke-read the "
                "golden page through a served engine"
            )
        if measured:
            # An honest third case: the card is real, the assembly is not proven.
            # Calling this "fixture-only" would misdescribe a paid measurement.
            return False, (
                f"no assembly proven: {profile.name} was measured by a real driver read, "
                "but no chair smoke-read the golden page through a served engine"
            )
        return False, FIXTURE_ONLY_ASSEMBLY_NOTE

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

    def _check_subprocess(
        self,
        identity: ChairIdentity,
        profile: Any,
        cache_receipt: dict[str, object],
        issues: list[PreflightIssue],
        receipts: list[dict[str, object]],
    ) -> None:
        root = cache_receipt.get("root")
        if not isinstance(root, str) or not root:
            issues.append(
                PreflightIssue(
                    "cache-receipt-invalid",
                    f"chair {identity.role}'s cache receipt names no verified weights root.",
                    "Repair the cache adapter so it returns the verified snapshot's root.",
                    identity.role,
                )
            )
            return
        try:
            manifest_rows = self.cache_verifier.manifest(identity).to_record()
        except Exception as error:
            issues.append(
                PreflightIssue(
                    "cache-mismatch" if is_cache_mismatch(error) else "cache-verification-failed",
                    f"chair {identity.role}'s pinned manifest could not be read: {error}",
                    "Inspect the named cache and pinned manifest; repair the cause before retrying.",
                    identity.role,
                )
            )
            return
        try:
            measured = self.subprocess_checker(
                identity, profile, Path(root), self.fixture, manifest_rows
            )
        except Exception as error:
            issues.append(
                PreflightIssue(
                    "subprocess-environment-unready",
                    f"chair {identity.role} could not run its own environment in "
                    f"{profile.environment} on the golden page: {error}",
                    f"Run `uv sync --locked --project {profile.environment}` on the pod, check "
                    "the chair's weights, then run preflight again.",
                    identity.role,
                )
            )
            return
        receipts.append({"chair": identity.role, "environment": profile.environment, **measured})

    def _verify_cache(
        self,
        identity: ChairIdentity,
        issues: list[PreflightIssue],
        receipts: list[dict[str, object]],
    ) -> dict[str, object] | None:
        """The chair's bound cache receipt, recorded, or None with the issue recorded."""
        try:
            receipt = (
                self._lookahead.verify(identity)
                if self._lookahead is not None
                else self.cache_verifier.verify(identity)
            )
        except PrefetchTimeout as expired:
            issues.append(
                PreflightIssue(
                    "cache-prefetch-timeout",
                    f"{expired}.",
                    "Check the model volume and the container disk (a hung network mount, "
                    "a full disk), then run preflight again; the chair was not filled.",
                    identity.role,
                )
            )
            return None
        except Exception as initial_error:
            issues.append(
                PreflightIssue(
                    "cache-mismatch"
                    if is_cache_mismatch(initial_error)
                    else "cache-verification-failed",
                    f"chair {identity.role} cache verification failed: {initial_error}",
                    "Inspect the named cache and pinned manifest; repair the cause before retrying.",
                    identity.role,
                )
            )
            return None
        normalized = self._bound_receipt(identity, receipt, issues, "cache")
        if normalized is None:
            return None
        recorded = {"chair": identity.role, **normalized}
        receipts.append(recorded)
        return recorded

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
        if kind == "smoke" and "served_engine" in receipt:
            # The field the assembly claim is published under. A reader that
            # could write it could assert its own proof.
            issues.append(
                PreflightIssue(
                    "smoke-receipt-invalid",
                    f"chair {identity.role} returned the runtime-owned served_engine field.",
                    "Repair the smoke adapter; naming the serving engine belongs to the "
                    "preflight runtime.",
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
    ) -> str | None:
        """Return the engine that served a valid read, else `None`.

        `None` covers every path that proves no assembly: a failed read, an
        unstructured result, a misbound receipt, an invalid page, and the
        ordinary fixture reader, which names no engine at all.
        """

        try:
            result = self.smoke_reader.read(identity, self.fixture_for(identity.role), tier)
        except Exception as error:
            issues.append(
                PreflightIssue(
                    "smoke-read-failed",
                    f"chair {identity.role} could not read the golden proof page: {error}",
                    "Repair the named chair service and rerun the golden-page smoke read.",
                    identity.role,
                )
            )
            return None
        if not isinstance(result, SmokeResult):
            issues.append(
                PreflightIssue(
                    "smoke-receipt-invalid",
                    f"chair {identity.role} returned no structured smoke result.",
                    "Repair the named chair service so shape, format, and utilization are all measurable.",
                    identity.role,
                )
            )
            return None
        receipt = self._bound_receipt(identity, result.receipt, issues, "smoke")
        if receipt is None:
            return None
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
        valid = result.shape_valid and result.nonempty and result.format_valid
        receipts.append(
            {
                "chair": identity.role,
                **receipt,
                # Runtime-owned: the reader reports what it read, the runtime reports what served
                # it.  `_bound_receipt` refuses an adapter that pre-populates it.
                "served_engine": result.served_by,
                "utilization": [
                    {
                        "gpu_percent": str(sample.gpu_percent),
                        "cpu_percent": str(sample.cpu_percent),
                    }
                    for sample in result.utilization
                ],
            }
        )
        # The token, not the string: `served_by` is the engine's name, and the
        # provenance beside it is what says a lifecycle in this repository
        # started that engine.  A result that carries one without the other
        # cannot exist (`SmokeResult.__post_init__`), and this is the check that
        # keeps that true if it ever could.
        proven = _is_runtime_provenance(result.provenance)
        return result.served_by if valid and proven else None


def is_cache_mismatch(error: BaseException) -> bool:
    return isinstance(error, (CacheMismatch, DigestMismatchRefusal, CacheRevisionRefusal))
