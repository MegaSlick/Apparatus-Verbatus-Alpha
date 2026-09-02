"""Strict, data-only vLLM serving-profile configuration.

``config/models.toml`` remains the only place that names model artifacts. Its
``serving_recipe`` string is a family key. This catalogue resolves that key
only together with the already-selected chair and measured placement tier, to
one complete vLLM flag profile.  That three-part lookup is deliberate: a
24-GiB profile is not a substitute for the same chair's 48-GiB profile.

``required_packages`` is an exact runtime/model-stack package map. It is
deliberately per profile because an engine version alone cannot establish
whether a model-support package changed under the same launch command.

A ``proven`` preflight mark carries ``preflight_digest``, the canonical digest
of every other field in that profile row. Editing a runtime field while leaving
the mark in place therefore refuses at catalogue load, before launch.

A profile row is never launched by itself: it is launched *against* a chair
identity resolved from ``config/models.toml``, and a real-silicon preflight
proves the pair.  So a proven row also carries ``preflight_identity_digest``,
the digest of that chair's cache descriptor.  Repointing the chair at other
weights, or bumping its revision, leaves this row byte-identical — so the row
digest alone cannot notice it, and ``manager._launchable`` refuses the
mismatch at launch instead.

A profile declares its ``kind``. ``vllm`` is a complete launch shape;
``fixture`` is the offline walking skeleton's stand-in; and ``unsupported``
keeps a configured real chair covered without inventing launch flags for an
engine this package does not implement. The latter two carry no vLLM flags and
must refuse by their actual cause before runtime checks.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from common.chairs.models import AbsentChair, ChairIdentity, ModelsConfig, is_hf_revision, is_sha256
from common.contracts.canonical import canonical_bytes, digest_bytes
from common.contracts.serving import SERVING_CONFIG_INPUTS_FIELDS
from common.contracts.serving import SERVING_CONFIG_INPUTS_SCHEMA as CONFIG_INPUTS_SCHEMA

from .errors import ServingConfigurationError

SCHEMA = "serving-recipes.v1"
_TOP_LEVEL = {"schema", "profiles"}
_KINDS = {"vllm", "fixture", "unsupported"}
_PROFILE_COMMON = {"kind", "recipe", "chair", "tier"}
_FIXTURE_FIELDS = _PROFILE_COMMON | {"description"}
_UNSUPPORTED_FIELDS = _PROFILE_COMMON | {"reason"}
_PROFILE_FIELDS = {
    "kind",
    "recipe",
    "chair",
    "tier",
    "host",
    "port",
    "served_model_id",
    "dtype",
    "seed",
    "required_packages",
    "max_model_len",
    "max_num_seqs",
    "max_num_batched_tokens",
    "gpu_memory_utilization",
    "min_pixels",
    "max_pixels",
    "enable_prefix_caching",
    "enforce_eager",
    "trust_remote_code",
    "enable_tower_connector_lora",
    "max_lora_rank",
    "generation_config",
    "startup_timeout_seconds",
    "poll_interval_seconds",
    "request_timeout_seconds",
    "readiness_probe",
    "preflight_state",
}
_PREFLIGHT_DIGEST_FIELD = "preflight_digest"
_PREFLIGHT_IDENTITY_FIELD = "preflight_identity_digest"
_PREFLIGHT_MARK_FIELDS = frozenset({"preflight_state", _PREFLIGHT_DIGEST_FIELD})
_PROBE_FIELDS = {"kind", "request_json"}
_PACKAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@dataclass(frozen=True, slots=True)
class ProbeSpec:
    """One deterministic-shape OpenAI probe used to prove an engine answers."""

    kind: str
    payload: Mapping[str, object]
    _canonical_payload: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        normalized_payload, canonical_payload = seal_json_object(
            self.payload, label="readiness probe payload"
        )
        object.__setattr__(self, "payload", MappingProxyType(normalized_payload))
        object.__setattr__(self, "_canonical_payload", canonical_payload)

    def request_payload(self) -> Mapping[str, object]:
        """Return the sealed request, unaffected by mutation of its public projection."""

        return json.loads(self._canonical_payload)


@dataclass(frozen=True, slots=True)
class FixtureProfile:
    """A named place in the catalogue for a chair that is never served.

    It exists so coverage stays complete: a chair the walking skeleton answers
    from declared details is still a row at every tier rather than a hole
    ``verify_recipes_cover_chairs`` would have to be taught to forgive.
    """

    recipe: str
    chair: str
    tier: str
    description: str
    kind: str = "fixture"

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.recipe, self.chair, self.tier)


@dataclass(frozen=True, slots=True)
class UnsupportedProfile:
    """A real configured chair with no honest serving implementation yet.

    It remains in catalogue coverage so the gap cannot disappear, but carries
    no launch flags: inventing a vLLM shape for a model served by another engine
    would turn a named missing implementation into a misleading preflight.
    """

    recipe: str
    chair: str
    tier: str
    reason: str
    kind: str = "unsupported"

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.recipe, self.chair, self.tier)


@dataclass(frozen=True, slots=True)
class ServingProfile:
    """One complete vLLM flag profile for one chair at one GPU tier.

    Capacity-related fields are configuration/planning values, never a claim
    that an unmeasured card can sustain them.
    """

    recipe: str
    chair: str
    tier: str
    host: str
    port: int
    served_model_id: str
    dtype: str
    seed: int
    required_packages: Mapping[str, str]
    max_model_len: int
    max_num_seqs: int
    max_num_batched_tokens: int
    gpu_memory_utilization: Decimal
    min_pixels: int
    max_pixels: int
    enable_prefix_caching: bool
    enforce_eager: bool
    trust_remote_code: bool
    enable_tower_connector_lora: bool
    max_lora_rank: int
    generation_config: str
    startup_timeout_seconds: int
    poll_interval_seconds: int
    request_timeout_seconds: int
    readiness_probe: ProbeSpec
    preflight_state: str
    preflight_digest: str | None
    preflight_identity_digest: str | None
    kind: str = "vllm"

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "required_packages", MappingProxyType(dict(self.required_packages))
        )

    @property
    def endpoint(self) -> str:
        return f"http://{self.host}:{self.port}/v1"

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.recipe, self.chair, self.tier)


@dataclass(frozen=True, slots=True)
class ServingRecipes:
    """The complete closed serving-profile catalogue."""

    profiles: tuple["ServingProfile | FixtureProfile | UnsupportedProfile", ...]
    source_path: Path | None = None
    source_sha256: str | None = None

    def for_identity(
        self, identity: ChairIdentity, tier: str
    ) -> "ServingProfile | FixtureProfile | UnsupportedProfile":
        """Return the only profile configured for this identity and tier.

        This is lookup, not a ranking or fallback: zero or multiple matches are
        both a refusal before any process can start.
        """

        matches = [
            profile
            for profile in self.profiles
            if profile.recipe == identity.serving_recipe
            and profile.chair == identity.role
            and profile.tier == tier
        ]
        if len(matches) != 1:
            raise ServingConfigurationError(
                "serving profile lookup for "
                f"chair={identity.role!r}, recipe={identity.serving_recipe!r}, tier={tier!r} "
                f"returned {len(matches)} profiles; exactly one is required"
            )
        return matches[0]


@dataclass(frozen=True, slots=True)
class ServingConfigInputs:
    """Exact recipe and placement bytes that a serving launch is allowed to use.

    The run's broader configuration digest seals these two values.  This small
    projection travels to pod assembly and the launch audit so the manager can
    reject a caller that would otherwise substitute a different TOML path after
    the run was sealed.
    """

    serving_recipes_sha256: str
    pod_placement_sha256: str

    def __post_init__(self) -> None:
        if not is_sha256(self.serving_recipes_sha256):
            raise ServingConfigurationError(
                "serving configuration recipes digest must be a lowercase SHA-256"
            )
        if not is_sha256(self.pod_placement_sha256):
            raise ServingConfigurationError(
                "serving configuration placement digest must be a lowercase SHA-256"
            )

    def to_record(self) -> dict[str, str]:
        """Return the closed data shape shared with ``common.stage``."""

        return {
            "schema": CONFIG_INPUTS_SCHEMA,
            "serving_recipes_sha256": self.serving_recipes_sha256,
            "pod_placement_sha256": self.pod_placement_sha256,
        }

    @classmethod
    def from_record(cls, value: Mapping[str, object]) -> "ServingConfigInputs":
        """Validate a run-sealed configuration projection before assembly."""

        if not isinstance(value, Mapping) or set(value) != SERVING_CONFIG_INPUTS_FIELDS:
            raise ServingConfigurationError(
                "sealed serving configuration must contain exactly schema, recipes, and placement digests"
            )
        if value["schema"] != CONFIG_INPUTS_SCHEMA:
            raise ServingConfigurationError(
                f"sealed serving configuration schema must be {CONFIG_INPUTS_SCHEMA!r}"
            )
        return cls(
            serving_recipes_sha256=value["serving_recipes_sha256"],
            pod_placement_sha256=value["pod_placement_sha256"],
        )

    def require_loaded(
        self,
        *,
        recipes_sha256: str | None,
        placement_sha256: str | None,
    ) -> None:
        """Refuse configuration substitution before any process can launch."""

        if recipes_sha256 != self.serving_recipes_sha256:
            raise ServingConfigurationError(
                "serving recipes bytes differ from the run-sealed serving configuration"
            )
        if placement_sha256 != self.pod_placement_sha256:
            raise ServingConfigurationError(
                "pod placement bytes differ from the run-sealed serving configuration"
            )


def load_serving_recipes(path: str | Path) -> ServingRecipes:
    """Read a strict TOML catalogue without importing vLLM or opening a socket."""

    source = Path(path)
    try:
        source_bytes = source.read_bytes()
        raw = tomllib.loads(source_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ServingConfigurationError(f"cannot read serving recipes {source}: {error}") from error
    return parse_serving_recipes(
        raw,
        source_path=source,
        source_sha256=digest_bytes(source_bytes),
    )


def parse_serving_recipes(
    raw: Any,
    *,
    source_path: str | Path | None = None,
    source_sha256: str | None = None,
) -> ServingRecipes:
    """Validate parsed TOML for offline tests and callers."""

    if not isinstance(raw, dict) or set(raw) != _TOP_LEVEL:
        raise ServingConfigurationError("serving recipes must contain only schema and profiles")
    if raw.get("schema") != SCHEMA:
        raise ServingConfigurationError(f"serving recipe schema must be {SCHEMA!r}")
    rows = raw.get("profiles")
    if not isinstance(rows, list) or not rows:
        raise ServingConfigurationError("serving recipes must contain a non-empty profiles array")
    profiles = tuple(_parse_profile(item) for item in rows)
    _validate_catalogue(profiles)
    if source_sha256 is not None and not is_sha256(source_sha256):
        raise ServingConfigurationError("serving recipes source_sha256 must be a lowercase SHA-256")
    return ServingRecipes(
        profiles,
        Path(source_path) if source_path is not None else None,
        source_sha256,
    )


def model_and_tokenizer_pins(identity: ChairIdentity) -> tuple[str, str] | None:
    """The duplicated commit pins a Hugging Face launch needs, or ``None``.

    ``None`` is not a failure and is not a missing pin.  A local-repository
    identity has no Git revision *by contract*
    (``ChairIdentity.receipt_revision_kind`` says so), and its pin is the
    verified digest manifest the snapshot was checked against byte for byte —
    which is the stronger of the two, since it names the bytes rather than a
    ref that resolves to them.  ``--revision``/``--tokenizer-revision`` exist to
    stop vLLM resolving a *mutable* Hub ref, so they mean something for a Hub
    identity and nothing for a directory that has already been verified.

    Refusing every non-Hub identity here would make the Perlector chair
    unservable: ARCHITECTURE requires a locally trained checkpoint to be
    "*called* like any other model, from its own model repository".
    """

    if identity.source != "huggingface":
        return None
    if not is_hf_revision(identity.revision):
        raise ServingConfigurationError(
            f"chair {identity.role!r} is a Hugging Face identity without a commit pin; "
            "a vLLM Hub launch requires one model and tokenizer commit pin"
        )
    # It is intentionally one identity value twice.  A profile may shape flags,
    # but may not quietly send a tokenizer from another revision.
    return identity.revision, identity.revision


def chair_preflight_identity_digest(identity: ChairIdentity) -> str:
    """Digest the chair identity a preflight proved a profile against.

    ``cache_descriptor()`` is already "the immutable facts a cache must match"
    — repo/path, revision, and the verified digest manifest — which is exactly
    the set whose change invalidates a real-silicon proof of a flag profile: a
    profile proven with one checkpoint says nothing about the memory fraction
    or context cap another one needs.  ``serving_recipe`` is deliberately not
    in it, because that field is the catalogue *key*: changing it moves the
    lookup to a different row rather than changing this one's meaning.

    This is a binding, not a second place that names a model artifact.
    ``config/models.toml`` still owns the artifact; the digest only lets a
    proof say which one it was taken against.
    """

    return digest_bytes(canonical_bytes(identity.cache_descriptor()))


def profile_preflight_digest(raw: Mapping[str, Any]) -> str:
    """Digest a vLLM profile's canonical fields other than its proof mark.

    ``preflight_state`` and ``preflight_digest`` are the mark, so neither may
    attest to itself.  ``preflight_identity_digest`` is deliberately *not*
    excluded: it is an ordinary sealed field, so retargeting a proof at another
    chair identity invalidates the row digest exactly as editing ``dtype``
    would.  TOML permits a decimal to arrive as a float; normalize that one
    accepted decimal field to the same text the typed profile uses before
    passing the row through the repository's canonical serializer.
    """

    profile = {key: value for key, value in raw.items() if key not in _PREFLIGHT_MARK_FIELDS}
    fraction = profile.get("gpu_memory_utilization")
    if isinstance(fraction, float):
        profile["gpu_memory_utilization"] = str(Decimal(str(fraction)))
    try:
        return digest_bytes(canonical_bytes(profile))
    except (RecursionError, TypeError, ValueError) as error:
        raise ServingConfigurationError(
            "serving profile cannot be canonically digested for preflight"
        ) from error


def _parse_profile(raw: Any) -> "ServingProfile | FixtureProfile | UnsupportedProfile":
    if not isinstance(raw, dict):
        raise ServingConfigurationError("each serving profile must be a table")
    kind = raw.get("kind")
    if kind not in _KINDS:
        raise ServingConfigurationError(
            f"serving profile kind must be one of {sorted(_KINDS)}, not {kind!r}"
        )
    if kind == "fixture":
        return _parse_fixture_profile(raw)
    if kind == "unsupported":
        unknown = sorted(set(raw) - _UNSUPPORTED_FIELDS)
        missing = sorted(_UNSUPPORTED_FIELDS - set(raw))
        if unknown or missing:
            raise ServingConfigurationError(
                f"unsupported serving profile has unknown field(s) {unknown} or missing field(s) "
                f"{missing}; an unsupported row carries identity and its refusal reason only"
            )
        return UnsupportedProfile(
            recipe=_text(raw["recipe"], "recipe"),
            chair=_text(raw["chair"], "chair"),
            tier=_text(raw["tier"], "tier"),
            reason=_text(raw["reason"], "reason"),
        )
    unknown = sorted(
        set(raw) - (_PROFILE_FIELDS | {_PREFLIGHT_DIGEST_FIELD, _PREFLIGHT_IDENTITY_FIELD})
    )
    missing = sorted(_PROFILE_FIELDS - set(raw))
    if unknown or missing:
        raise ServingConfigurationError(
            f"serving profile has unknown field(s) {unknown} or missing field(s) {missing}"
        )
    recipe = _text(raw["recipe"], "recipe")
    chair = _text(raw["chair"], "chair")
    tier = _text(raw["tier"], "tier")
    host = _text(raw["host"], "host")
    if host != "127.0.0.1":
        raise ServingConfigurationError("serving profiles must bind vLLM to loopback 127.0.0.1")
    port = _positive_int(raw["port"], "port")
    if port > 65535:
        raise ServingConfigurationError("port must be at most 65535")
    served_model_id = _text(raw["served_model_id"], "served_model_id")
    dtype = _text(raw["dtype"], "dtype")
    seed = _nonnegative_int(raw["seed"], "seed")
    packages = _packages(raw["required_packages"])
    max_model_len = _positive_int(raw["max_model_len"], "max_model_len")
    max_num_seqs = _positive_int(raw["max_num_seqs"], "max_num_seqs")
    max_num_batched_tokens = _positive_int(raw["max_num_batched_tokens"], "max_num_batched_tokens")
    fraction = _fraction(raw["gpu_memory_utilization"])
    min_pixels = _positive_int(raw["min_pixels"], "min_pixels")
    max_pixels = _positive_int(raw["max_pixels"], "max_pixels")
    if min_pixels > max_pixels:
        raise ServingConfigurationError("min_pixels cannot exceed max_pixels")
    enable_prefix_caching = _bool(raw["enable_prefix_caching"], "enable_prefix_caching")
    enforce_eager = _bool(raw["enforce_eager"], "enforce_eager")
    trust_remote_code = _bool(raw["trust_remote_code"], "trust_remote_code")
    enable_tower_connector_lora = _bool(
        raw["enable_tower_connector_lora"], "enable_tower_connector_lora"
    )
    max_lora_rank = _positive_int(raw["max_lora_rank"], "max_lora_rank")
    if max_lora_rank not in {1, 8, 16, 32, 64, 128, 256, 320, 512}:
        raise ServingConfigurationError(
            "max_lora_rank must be one of vLLM's supported static LoRA ranks"
        )
    generation_config = _text(raw["generation_config"], "generation_config")
    if generation_config != "vllm":
        raise ServingConfigurationError(
            "generation_config must be exactly 'vllm'; model-supplied generation defaults are not a pinned profile"
        )
    preflight_state = _text(raw["preflight_state"], "preflight_state")
    if preflight_state not in {"unproven", "proven"}:
        raise ServingConfigurationError("preflight_state must be 'unproven' or 'proven'")
    profile_name = f"recipe={recipe!r}, chair={chair!r}, tier={tier!r}"
    preflight_digest: str | None = None
    preflight_identity_digest: str | None = None
    if preflight_state == "proven":
        preflight_digest = raw.get(_PREFLIGHT_DIGEST_FIELD)
        if not is_sha256(preflight_digest):
            raise ServingConfigurationError(
                f"serving profile {profile_name} is marked proven without a lowercase "
                "SHA-256 preflight_digest"
            )
        preflight_identity_digest = raw.get(_PREFLIGHT_IDENTITY_FIELD)
        if not is_sha256(preflight_identity_digest):
            raise ServingConfigurationError(
                f"serving profile {profile_name} is marked proven without a lowercase SHA-256 "
                "preflight_identity_digest naming the chair identity it was proven against; a "
                "flag profile is never preflighted apart from a checkpoint"
            )
        expected_preflight_digest = profile_preflight_digest(raw)
        if preflight_digest != expected_preflight_digest:
            raise ServingConfigurationError(
                f"serving profile {profile_name} has a stale preflight_digest; its canonical "
                "fields changed after this profile was proven"
            )
    else:
        stale_mark = sorted({_PREFLIGHT_DIGEST_FIELD, _PREFLIGHT_IDENTITY_FIELD} & set(raw))
        if stale_mark:
            raise ServingConfigurationError(
                f"serving profile {profile_name} is unproven and must not carry {stale_mark}"
            )
    timeout = _positive_int(raw["startup_timeout_seconds"], "startup_timeout_seconds")
    poll = _positive_int(raw["poll_interval_seconds"], "poll_interval_seconds")
    if poll > timeout:
        raise ServingConfigurationError(
            "poll_interval_seconds cannot exceed startup_timeout_seconds"
        )
    request_timeout = _positive_int(raw["request_timeout_seconds"], "request_timeout_seconds")
    return ServingProfile(
        recipe=recipe,
        chair=chair,
        tier=tier,
        host=host,
        port=port,
        served_model_id=served_model_id,
        dtype=dtype,
        seed=seed,
        required_packages=packages,
        max_model_len=max_model_len,
        max_num_seqs=max_num_seqs,
        max_num_batched_tokens=max_num_batched_tokens,
        gpu_memory_utilization=fraction,
        min_pixels=min_pixels,
        max_pixels=max_pixels,
        enable_prefix_caching=enable_prefix_caching,
        enforce_eager=enforce_eager,
        trust_remote_code=trust_remote_code,
        enable_tower_connector_lora=enable_tower_connector_lora,
        max_lora_rank=max_lora_rank,
        generation_config=generation_config,
        startup_timeout_seconds=timeout,
        poll_interval_seconds=poll,
        request_timeout_seconds=request_timeout,
        readiness_probe=_probe(raw["readiness_probe"]),
        preflight_state=preflight_state,
        preflight_digest=preflight_digest,
        preflight_identity_digest=preflight_identity_digest,
        kind="vllm",
    )


def _parse_fixture_profile(raw: Mapping[str, Any]) -> FixtureProfile:
    """A fixture row carries its four identifying fields and a reason, nothing else.

    Refusing every flag field here is the point: a row that is never launched
    may not carry a memory fraction, a context cap, or a batch size, because
    those would read in review as measured planning values for a real chair.
    """

    unknown = sorted(set(raw) - _FIXTURE_FIELDS)
    missing = sorted(_FIXTURE_FIELDS - set(raw))
    if unknown or missing:
        raise ServingConfigurationError(
            f"fixture serving profile has unknown field(s) {unknown} or missing field(s) {missing}; "
            "a fixture row is never launched and carries no vLLM flags"
        )
    return FixtureProfile(
        recipe=_text(raw["recipe"], "recipe"),
        chair=_text(raw["chair"], "chair"),
        tier=_text(raw["tier"], "tier"),
        description=_text(raw["description"], "description"),
    )


def _validate_catalogue(
    profiles: tuple["ServingProfile | FixtureProfile | UnsupportedProfile", ...],
) -> None:
    keys = [profile.key for profile in profiles]
    if len(keys) != len(set(keys)):
        raise ServingConfigurationError("serving profiles duplicate a recipe/chair/tier key")
    endpoint_chairs: dict[tuple[str, int], set[str]] = {}
    served_chairs: dict[str, set[str]] = {}
    for profile in profiles:
        # Fixture and unsupported rows own neither endpoint nor API alias;
        # applying launch-only collision rules would invent serving claims.
        if not isinstance(profile, ServingProfile):
            continue
        endpoint_chairs.setdefault((profile.host, profile.port), set()).add(profile.chair)
        served_chairs.setdefault(profile.served_model_id, set()).add(profile.chair)
    conflicts = sorted(endpoint for endpoint, chairs in endpoint_chairs.items() if len(chairs) > 1)
    if conflicts:
        raise ServingConfigurationError(
            f"loopback endpoint(s) are assigned to more than one chair: {conflicts}"
        )
    aliases = sorted(alias for alias, chairs in served_chairs.items() if len(chairs) > 1)
    if aliases:
        raise ServingConfigurationError(
            f"served model id(s) are assigned to more than one chair: {aliases}"
        )


def verify_recipes_cover_chairs(
    models: ModelsConfig, recipes: ServingRecipes, tiers: Iterable[str]
) -> None:
    """Every configured chair resolves to exactly one profile at every tier.

    ``for_identity`` already refuses zero or several matches — but only at the
    moment a start is attempted, which on the real path is on a rented GPU with
    the meter running.  A misspelt ``serving_recipe``, a chair added to
    ``models.toml`` without a catalogue row, or a placement tier added to
    ``pod_placement.toml`` without one, are all the same shape of gap
    ``common/stage.py::unaddressed_chairs`` closes a layer up: a configured
    thing nothing will ever be able to ask for.  This is the offline check, so
    the three files are reconciled by the test suite rather than by the pod.

    Absent chairs carry no recipe and are skipped: an absence is a decision
    already recorded.

    A proven profile's ``preflight_identity_digest`` is reconciled here for the
    same reason.  ``manager._launchable`` refuses a proof that has stopped
    describing its chair, but it refuses at the moment of launch — which on the
    real path is again the rented GPU.  A chair repointed in ``models.toml``
    without its profile being preflighted again is a configuration gap of
    exactly the shape above, so it fails in a test run too.
    """

    tier_values = tuple(tiers)
    if not tier_values:
        raise ServingConfigurationError("serving recipe coverage requires at least one tier")
    if len(tier_values) != len(set(tier_values)):
        raise ServingConfigurationError("serving recipe coverage tiers must be unique")

    expected: set[tuple[str, str, str]] = set()
    for role, value in sorted(models.chairs.items()):
        if isinstance(value, AbsentChair):
            continue
        expected.update((value.serving_recipe, role, tier) for tier in tier_values)
    actual = {profile.key for profile in recipes.profiles}
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    if missing or unexpected:
        raise ServingConfigurationError(
            "config/serving_recipes.toml must match exactly every configured chair at every "
            f"configured placement tier; missing={missing}, unexpected={unexpected}"
        )

    repointed = sorted(
        f"recipe={profile.recipe!r}, chair={profile.chair!r}, tier={profile.tier!r}"
        for profile in recipes.profiles
        if isinstance(profile, ServingProfile)
        and profile.preflight_state == "proven"
        and isinstance(models.chairs.get(profile.chair), ChairIdentity)
        and profile.preflight_identity_digest
        != chair_preflight_identity_digest(models.chairs[profile.chair])
    )
    if repointed:
        raise ServingConfigurationError(
            "serving profile(s) are marked proven against a chair identity config/models.toml "
            f"no longer configures; preflight them again before launch: {repointed}"
        )


def _probe(raw: Any) -> ProbeSpec:
    if not isinstance(raw, dict) or set(raw) != _PROBE_FIELDS:
        raise ServingConfigurationError("readiness_probe must contain only kind and request_json")
    kind = _text(raw["kind"], "readiness_probe.kind")
    if kind not in {"chat-completions", "completions"}:
        raise ServingConfigurationError(
            "readiness_probe.kind must be chat-completions or completions"
        )
    encoded = _text(raw["request_json"], "readiness_probe.request_json")
    try:
        payload = json.loads(encoded)
    except (json.JSONDecodeError, RecursionError) as error:
        raise ServingConfigurationError("readiness_probe.request_json is not JSON") from error
    if not isinstance(payload, dict):
        raise ServingConfigurationError("readiness_probe.request_json must decode to an object")
    forbidden = {"model", "seed", "temperature", "stream"} & set(payload)
    if forbidden:
        raise ServingConfigurationError(
            f"readiness probe controls {sorted(forbidden)} are manager-owned"
        )
    if kind == "chat-completions":
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ServingConfigurationError("chat readiness probe needs a non-empty messages list")
    elif not isinstance(payload.get("prompt"), str) or not payload["prompt"].strip():
        raise ServingConfigurationError("completion readiness probe needs a non-blank prompt")
    return ProbeSpec(kind, payload)


def _packages(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict) or not raw:
        raise ServingConfigurationError("required_packages must be a non-empty table")
    result: dict[str, str] = {}
    for package, version in raw.items():
        if not isinstance(package, str) or not _PACKAGE.fullmatch(package):
            raise ServingConfigurationError(f"invalid required package name {package!r}")
        result[package] = _text(version, f"required_packages.{package}")
    if "vllm" not in result:
        raise ServingConfigurationError("required_packages must pin vllm exactly")
    return result


def _fraction(value: Any) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ServingConfigurationError("gpu_memory_utilization must be decimal") from error
    if not parsed.is_finite() or not Decimal("0") < parsed <= Decimal("1"):
        raise ServingConfigurationError("gpu_memory_utilization must be in (0, 1]")
    return parsed


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value.strip() != value:
        raise ServingConfigurationError(
            f"{field} must be a non-blank string without surrounding whitespace"
        )
    return value


def _bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ServingConfigurationError(f"{field} must be boolean")
    return value


def _positive_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ServingConfigurationError(f"{field} must be a positive integer")
    return value


def _nonnegative_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ServingConfigurationError(f"{field} must be a non-negative integer")
    return value


def seal_json_object(value: object, *, label: str) -> tuple[dict[str, object], str]:
    """Materialize one caller-supplied JSON object, returning both of its forms.

    Every request payload this package accepts — a readiness probe from the
    catalogue, an adapter calibration, a golden-page request — is frozen here
    exactly once, and both the validators and the eventual POST read that one
    snapshot.  A stateful ``Mapping`` can otherwise show an image to a validator
    and serialize text-only content afterwards.  The canonical string is
    returned alongside so a sealed payload can be rebuilt later without keeping
    a mutable object alive.
    """

    try:
        canonical = json.dumps(
            _json_copy(value, label),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        snapshot = json.loads(canonical)
    except (TypeError, ValueError, RecursionError) as error:
        # RecursionError because nesting, not length, is what breaks the JSON
        # parser and _json_copy's own recursion -- http.py's _json_object
        # catches it for the inbound side for the same reason; this closes
        # the outbound/config side.
        raise ServingConfigurationError(f"{label} must be JSON-compatible: {error}") from error
    if not isinstance(snapshot, dict):
        raise ServingConfigurationError(f"{label} must encode to a JSON object")
    return snapshot, canonical


def _json_copy(value: object, label: str) -> object:
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ServingConfigurationError(f"{label} object keys must be strings")
            result[key] = _json_copy(item, label)
        return result
    if isinstance(value, (list, tuple)):
        return [_json_copy(item, label) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ServingConfigurationError(f"{label} has non-JSON value {type(value).__name__}")
