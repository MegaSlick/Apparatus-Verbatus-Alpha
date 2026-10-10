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
``in-process`` is a model the calling stage loads and runs itself, on the CPU,
with no server (the record detector); ``subprocess`` is a detector its stage
runs as a child process in its own pinned environment, on the CPU (Surya);
``fixture`` is the offline walking
skeleton's stand-in; and ``unsupported`` keeps a configured real chair covered
without inventing launch flags for an engine this package does not implement.
Every kind but ``vllm`` carries no vLLM flags and must refuse by its actual cause
before runtime checks.

A ``vllm`` row may name ``shares_service_with``: another chair whose service,
already running at the same tier, this chair may take over instead of starting
its own (the reconstructor and the Perlector serve one checkpoint). The pair
must have identical launch fields (``LAUNCH_FIELDS``), and only such a pair may
share an endpoint and a served model id.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from common.chairs.models import (
    AbsentChair,
    ChairIdentity,
    ModelsConfig,
    is_hf_revision,
    is_sha256,
)
from common.contracts.canonical import canonical_bytes, digest_bytes
from common.contracts.errors import ContractError
from common.contracts.serving import SERVING_CONFIG_INPUTS_FIELDS
from common.contracts.serving import SERVING_CONFIG_INPUTS_SCHEMA as CONFIG_INPUTS_SCHEMA
from common.sealed_config import read_sealed_toml

from .errors import ServingConfigurationError

SCHEMA = "serving-recipes.v1"
_TOP_LEVEL = {"schema", "profiles"}
_KINDS = {"vllm", "in-process", "subprocess", "fixture", "unsupported"}
# 'vllm' only: vLLM's 'auto' would fill a sampling field a request leaves out
# from the model's generation_config.json, unseen on the wire. Every chair's
# request carries its full sealed sampling row (`common.decoding`), so nothing
# a reading samples under is left to a file.
_GENERATION_CONFIG_VALUES = {"vllm"}
_PROFILE_COMMON = {"kind", "recipe", "chair", "tier"}
_FIXTURE_FIELDS = _PROFILE_COMMON | {"description"}
_UNSUPPORTED_FIELDS = _PROFILE_COMMON | {"reason"}
_IN_PROCESS_FIELDS = _PROFILE_COMMON | {
    "engine",
    "task",
    "device",
    "imgsz",
    "conf_bp",
    "iou_bp",
    "max_det",
    "required_packages",
}
# The one engine and device an in-process row may name today: the Ultralytics
# runtime its record detector was trained with, run on the CPU so it never
# shares a card with a served chair and its output does not vary with a GPU kernel.
_IN_PROCESS_ENGINES = {"ultralytics": frozenset({"ultralytics", "torch"})}
_IN_PROCESS_DEVICES = {"cpu"}
_SUBPROCESS_FIELDS = _PROFILE_COMMON | {
    "engine",
    "environment",
    "device",
    "threads",
    "workers",
    "startup_timeout_seconds",
    "seconds_per_page",
    "required_packages",
}
# `workers` is the one optional subprocess field: a ceiling on runner processes.
# Left out, the CPUs the stage can use decide, `threads` each.
_SUBPROCESS_OPTIONAL = {"workers"}
# The one engine a subprocess row may name, the packages its row pins, and the
# pinned environment it runs in. CPU only: no card is shared with a served chair,
# and the output does not vary with a GPU kernel.
_SUBPROCESS_ENGINES = {"surya": frozenset({"surya-ocr", "torch"})}
_SUBPROCESS_ENVIRONMENTS = {"surya": "operations/serving/surya"}
_SUBPROCESS_DEVICES = {"cpu"}
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
    "generation_config",
    "startup_timeout_seconds",
    "poll_interval_seconds",
    "request_timeout_seconds",
    "readiness_probe",
    "preflight_state",
}
# Optional deliberately: `patch_size`/`merge_size` are the chair's
# vision-encoder geometry (checked against the pinned revision's processor
# config by `manager.assert_processor_geometry`) that decide one image's
# prompt-token cost. vLLM itself reads them from the model repository, so a
# row omitting them still launches -- `request_capacity.row_image_geometry`
# refuses by name instead of counting against a wrong default -- so a
# catalogue not yet measured is incomplete rather than unloadable.
#
# `weights_gib` and `kv_gib_per_seq` are optional too: a row's weights on the
# card and the KV one full-length sequence holds, both GiB as decimal strings,
# from the row's own notes. A row carrying both can be widened past its
# `max_num_seqs` on a card with room (`operations/serving/capacity.py`); a row
# without them is launched exactly as written.
#
# `quantization`, `kv_cache_dtype` and `speculative_config` are optional engine
# options, absent on every row that predates them, so those rows, their digests
# and their argv are unchanged. Each takes only the values listed below, all
# read from vLLM 0.30.0's own argument parser and configuration
# (`vllm/engine/arg_utils.py`, `vllm/config/{model,cache,speculative}.py` at
# tag v0.30.0); a new value is a reviewed edit here, never a free string.
_OPTIONAL_PROFILE_FIELDS = {
    "patch_size",
    "merge_size",
    "shares_service_with",
    "weights_gib",
    "kv_gib_per_seq",
    "quantization",
    "kv_cache_dtype",
    "speculative_config",
}
# `--quantization`: vLLM compares it with the checkpoint's own
# `quantization_config.quant_method` and refuses a mismatch; on a checkpoint
# with no quantization config it would quantize the bf16 weights itself at load,
# a different model under the same repository pin. `manager.assert_quantization`
# therefore requires the verified snapshot to declare the same method.
#
# Each value maps to what the checkpoint's own `config.json`
# `quantization_config` must say: its `quant_method` and, where vLLM picks the
# method from it, its `quant_algo`. vLLM's name is not always the checkpoint's:
# a ModelOpt checkpoint says `quant_method: "modelopt"` and vLLM 0.30.0 turns
# `quant_algo: "MIXED_PRECISION"` into its `modelopt_mixed` method (per-layer
# FP8 / NVFP4, `ModelOptMixedPrecisionConfig.override_quantization_method` in
# `vllm/model_executor/layers/quantization/modelopt.py`); `--quantization
# modelopt_mixed` on such a checkpoint is accepted, on any other refused
# (`vllm/config/model.py::_verify_quantization`).
QUANTIZATION_CHECKPOINT_DECLARATIONS: Mapping[str, tuple[str, str | None]] = MappingProxyType(
    {
        "fp8": ("fp8", None),
        "modelopt_mixed": ("modelopt", "MIXED_PRECISION"),
    }
)
QUANTIZATION_VALUES = frozenset(QUANTIZATION_CHECKPOINT_DECLARATIONS)
# `--kv-cache-dtype`: `fp8` is vLLM's e4m3 KV cache on CUDA.
KV_CACHE_DTYPE_VALUES = frozenset({"fp8"})
# `--speculative-config`: only the checkpoint's own multi-token-prediction head.
# vLLM 0.30.0 aliases the per-family names (`qwen3_5_mtp`, ...) to `mtp`, and
# for a Qwen3.5/3.8 checkpoint it reads the head's depth from
# `text_config.mtp_num_hidden_layers`; `num_speculative_tokens` is stated
# because the vLLM recipe for Qwen3.8-27B says startup fails without it.
# The full model verifies each drafted token, so speculation aims at the same
# distribution, but the engine's numerics and batching can still change which
# tokens come out: a speculative recipe is compared, not assumed equal. vLLM
# refuses `min_p` > 0 and `logit_bias` under it, which no chair sends.
SPECULATIVE_METHODS = frozenset({"mtp"})
_SPECULATIVE_FIELDS = frozenset({"method", "num_speculative_tokens"})
MAX_SPECULATIVE_TOKENS = 8
# What shapes the running service, as distinct from how a caller waits for it or
# times its requests. Two rows sharing one service must agree on every one: the
# argv, the readiness probe the service was proven ready with, and the package
# pins it runs under.
LAUNCH_FIELDS = (
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
    "patch_size",
    "merge_size",
    "enable_prefix_caching",
    "enforce_eager",
    "trust_remote_code",
    "generation_config",
    "readiness_probe",
    "quantization",
    "kv_cache_dtype",
    "speculative_config",
)
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
class InProcessProfile:
    """A model the calling stage loads and runs itself, with no serving process.

    The row states every inference setting the stage passes, so a run's sealed
    catalogue says what the detector was asked with. Scores and thresholds are
    integer basis points, because the canonical writer refuses floats.
    """

    recipe: str
    chair: str
    tier: str
    engine: str
    task: str
    device: str
    imgsz: int
    conf_bp: int
    iou_bp: int
    max_det: int
    required_packages: Mapping[str, str]
    kind: str = "in-process"

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "required_packages", MappingProxyType(dict(self.required_packages))
        )

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.recipe, self.chair, self.tier)


@dataclass(frozen=True, slots=True)
class SubprocessProfile:
    """A detector its own stage runs as a child process, in a pinned environment.

    Never launched by the serving manager and never on the card: the stage
    starts it, on the CPU, with the stated thread count, and reads the JSON it
    writes. ``required_packages`` are the versions that environment's lock
    installs, checked against the environment before a run.

    The pages are split in page order into as many contiguous slices as the
    usable CPUs give ``threads`` each (never more than ``workers``, where a row
    sets that ceiling), each read by its own runner process with ``threads``
    torch threads, so the
    slices never share a thread budget and each page is read exactly as one
    process over every page would read it. The version check and each process's
    model load get ``startup_timeout_seconds``, and a process over ``n`` pages
    gets that plus ``n * seconds_per_page``.
    """

    recipe: str
    chair: str
    tier: str
    engine: str
    environment: str
    device: str
    threads: int
    startup_timeout_seconds: int
    seconds_per_page: int
    required_packages: Mapping[str, str]
    workers: int | None = None
    kind: str = "subprocess"

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "required_packages", MappingProxyType(dict(self.required_packages))
        )

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.recipe, self.chair, self.tier)

    def run_timeout_seconds(self, pages: int) -> int:
        return self.startup_timeout_seconds + pages * self.seconds_per_page


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
    generation_config: str
    startup_timeout_seconds: int
    poll_interval_seconds: int
    request_timeout_seconds: int
    readiness_probe: ProbeSpec
    preflight_state: str
    preflight_digest: str | None
    preflight_identity_digest: str | None
    # The chair's vision-encoder geometry, optional on the row and therefore
    # optional here.  ``None`` means the catalogue has not stated it, which
    # ``common/request_capacity.py`` refuses by name rather than defaulting.
    patch_size: int | None = None
    merge_size: int | None = None
    # The row's capacity estimates in GiB, optional on the row; ``None`` means the
    # row is never widened past its own ``max_num_seqs``.
    weights_gib: Decimal | None = None
    kv_gib_per_seq: Decimal | None = None
    kind: str = "vllm"
    # The chair whose running service this row may take over (see the module
    # docstring); ``None`` for a row that always starts its own.
    shares_service_with: str | None = None
    # Optional engine options (see `_OPTIONAL_PROFILE_FIELDS`); ``None`` means
    # the row does not pass the flag, and vLLM's default applies as before.
    quantization: str | None = None
    kv_cache_dtype: str | None = None
    speculative_config: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "required_packages", MappingProxyType(dict(self.required_packages))
        )
        if self.speculative_config is not None:
            object.__setattr__(
                self, "speculative_config", MappingProxyType(dict(self.speculative_config))
            )

    def engine_option_argv(self) -> tuple[str, ...]:
        """The optional engine flags this row passes; empty for a row without them."""

        return engine_option_argv(
            quantization=self.quantization,
            kv_cache_dtype=self.kv_cache_dtype,
            speculative_config=self.speculative_config,
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

    profiles: tuple[
        "ServingProfile | InProcessProfile | SubprocessProfile | FixtureProfile | UnsupportedProfile",
        ...,
    ]
    source_path: Path | None = None
    source_sha256: str | None = None

    def for_identity(
        self, identity: ChairIdentity, tier: str
    ) -> "ServingProfile | InProcessProfile | SubprocessProfile | FixtureProfile | UnsupportedProfile":
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
                "serving recipes differ from the run-sealed serving configuration"
            )
        if placement_sha256 != self.pod_placement_sha256:
            raise ServingConfigurationError(
                "pod placement differs from the run-sealed serving configuration"
            )


def load_serving_recipes(path: str | Path) -> ServingRecipes:
    """Read a strict TOML catalogue without importing vLLM or opening a socket."""

    source = Path(path)
    try:
        raw, digest = read_sealed_toml(source, "serving recipes")
    except ContractError as error:
        raise ServingConfigurationError(f"cannot read serving recipes {source}: {error}") from error
    return parse_serving_recipes(raw, source_path=source, source_sha256=digest)


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

    Refusing every non-Hub identity here would make a locally trained chair
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


def _parse_profile(
    raw: Any,
) -> "ServingProfile | InProcessProfile | SubprocessProfile | FixtureProfile | UnsupportedProfile":
    if not isinstance(raw, dict):
        raise ServingConfigurationError("each serving profile must be a table")
    kind = raw.get("kind")
    if kind not in _KINDS:
        raise ServingConfigurationError(
            f"serving profile kind must be one of {sorted(_KINDS)}, not {kind!r}"
        )
    if kind == "fixture":
        return _parse_fixture_profile(raw)
    if kind == "in-process":
        return _parse_in_process_profile(raw)
    if kind == "subprocess":
        return _parse_subprocess_profile(raw)
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
        set(raw)
        - (
            _PROFILE_FIELDS
            | _OPTIONAL_PROFILE_FIELDS
            | {_PREFLIGHT_DIGEST_FIELD, _PREFLIGHT_IDENTITY_FIELD}
        )
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
    patch_size = _optional_positive_int(raw, "patch_size")
    merge_size = _optional_positive_int(raw, "merge_size")
    weights_gib = _optional_gib(raw, "weights_gib")
    kv_gib_per_seq = _optional_gib(raw, "kv_gib_per_seq")
    enable_prefix_caching = _bool(raw["enable_prefix_caching"], "enable_prefix_caching")
    enforce_eager = _bool(raw["enforce_eager"], "enforce_eager")
    trust_remote_code = _bool(raw["trust_remote_code"], "trust_remote_code")
    generation_config = _text(raw["generation_config"], "generation_config")
    if generation_config not in _GENERATION_CONFIG_VALUES:
        raise ServingConfigurationError(
            f"generation_config must be one of {sorted(_GENERATION_CONFIG_VALUES)}, not "
            f"{generation_config!r}"
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
    shares_service_with = (
        _text(raw["shares_service_with"], "shares_service_with")
        if "shares_service_with" in raw
        else None
    )
    if shares_service_with == chair:
        raise ServingConfigurationError(
            f"serving profile {profile_name} names its own chair in shares_service_with"
        )
    quantization = _optional_choice(raw, "quantization", QUANTIZATION_VALUES)
    kv_cache_dtype = _optional_choice(raw, "kv_cache_dtype", KV_CACHE_DTYPE_VALUES)
    speculative_config = _optional_speculative_config(raw)
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
        patch_size=patch_size,
        merge_size=merge_size,
        weights_gib=weights_gib,
        kv_gib_per_seq=kv_gib_per_seq,
        enable_prefix_caching=enable_prefix_caching,
        enforce_eager=enforce_eager,
        trust_remote_code=trust_remote_code,
        generation_config=generation_config,
        startup_timeout_seconds=timeout,
        poll_interval_seconds=poll,
        request_timeout_seconds=request_timeout,
        readiness_probe=_probe(raw["readiness_probe"]),
        preflight_state=preflight_state,
        preflight_digest=preflight_digest,
        preflight_identity_digest=preflight_identity_digest,
        kind="vllm",
        shares_service_with=shares_service_with,
        quantization=quantization,
        kv_cache_dtype=kv_cache_dtype,
        speculative_config=speculative_config,
    )


def engine_option_argv(
    *,
    quantization: str | None,
    kv_cache_dtype: str | None,
    speculative_config: Mapping[str, object] | None,
) -> tuple[str, ...]:
    """Render the optional engine flags, in one fixed order, for any launcher.

    The serving manager and the bake-off's own launcher both call this, so a row
    is launched with the same flags whichever of them starts it. Nothing is
    rendered for an absent field.
    """

    argv: list[str] = []
    if quantization is not None:
        argv += ["--quantization", quantization]
    if kv_cache_dtype is not None:
        argv += ["--kv-cache-dtype", kv_cache_dtype]
    if speculative_config is not None:
        argv += [
            "--speculative-config",
            json.dumps(dict(speculative_config), sort_keys=True, separators=(",", ":")),
        ]
    return tuple(argv)


def _optional_choice(raw: Mapping[str, Any], field: str, allowed: frozenset[str]) -> str | None:
    if field not in raw:
        return None
    value = _text(raw[field], field)
    if value not in allowed:
        raise ServingConfigurationError(
            f"{field} must be one of {sorted(allowed)}, not {value!r}; a new engine option "
            "is a reviewed edit to operations/serving/config.py"
        )
    return value


def _optional_speculative_config(raw: Mapping[str, Any]) -> dict[str, object] | None:
    if "speculative_config" not in raw:
        return None
    value = raw["speculative_config"]
    if not isinstance(value, dict) or set(value) != _SPECULATIVE_FIELDS:
        raise ServingConfigurationError(
            f"speculative_config must be a table of exactly {sorted(_SPECULATIVE_FIELDS)}"
        )
    method = _text(value["method"], "speculative_config.method")
    if method not in SPECULATIVE_METHODS:
        raise ServingConfigurationError(
            f"speculative_config.method must be one of {sorted(SPECULATIVE_METHODS)}, "
            f"not {method!r}"
        )
    tokens = _positive_int(
        value["num_speculative_tokens"], "speculative_config.num_speculative_tokens"
    )
    if tokens > MAX_SPECULATIVE_TOKENS:
        raise ServingConfigurationError(
            f"speculative_config.num_speculative_tokens must be at most {MAX_SPECULATIVE_TOKENS}"
        )
    return {"method": method, "num_speculative_tokens": tokens}


def _parse_fixture_profile(raw: Mapping[str, Any]) -> FixtureProfile:
    """A fixture row carries its kind, recipe, chair, tier and a description, nothing else.

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


def _parse_in_process_profile(raw: Mapping[str, Any]) -> InProcessProfile:
    """An in-process row names its engine, device and every inference setting."""

    unknown = sorted(set(raw) - _IN_PROCESS_FIELDS)
    missing = sorted(_IN_PROCESS_FIELDS - set(raw))
    if unknown or missing:
        raise ServingConfigurationError(
            f"in-process serving profile has unknown field(s) {unknown} or missing field(s) "
            f"{missing}"
        )
    engine = _text(raw["engine"], "engine")
    if engine not in _IN_PROCESS_ENGINES:
        raise ServingConfigurationError(
            f"in-process engine must be one of {sorted(_IN_PROCESS_ENGINES)}, not {engine!r}"
        )
    device = _text(raw["device"], "device")
    if device not in _IN_PROCESS_DEVICES:
        raise ServingConfigurationError(
            f"in-process device must be one of {sorted(_IN_PROCESS_DEVICES)}, not {device!r}"
        )
    task = _text(raw["task"], "task")
    if task != "obb":
        raise ServingConfigurationError(f"in-process task must be 'obb', not {task!r}")
    raw_packages = raw["required_packages"]
    if not isinstance(raw_packages, dict) or set(raw_packages) != _IN_PROCESS_ENGINES[engine]:
        raise ServingConfigurationError(
            f"an in-process {engine} row pins exactly {sorted(_IN_PROCESS_ENGINES[engine])}"
        )
    packages = {
        package: _text(version, f"required_packages.{package}")
        for package, version in raw_packages.items()
    }
    basis_points = {}
    for name in ("conf_bp", "iou_bp"):
        value = _nonnegative_int(raw[name], name)
        if value > 10_000:
            raise ServingConfigurationError(f"{name} must be at most 10000 basis points")
        basis_points[name] = value
    return InProcessProfile(
        recipe=_text(raw["recipe"], "recipe"),
        chair=_text(raw["chair"], "chair"),
        tier=_text(raw["tier"], "tier"),
        engine=engine,
        task=task,
        device=device,
        imgsz=_positive_int(raw["imgsz"], "imgsz"),
        conf_bp=basis_points["conf_bp"],
        iou_bp=basis_points["iou_bp"],
        max_det=_positive_int(raw["max_det"], "max_det"),
        required_packages=packages,
    )


def _parse_subprocess_profile(raw: Mapping[str, Any]) -> SubprocessProfile:
    """A subprocess row names its engine, environment, device, threads and pins."""

    unknown = sorted(set(raw) - _SUBPROCESS_FIELDS)
    missing = sorted(_SUBPROCESS_FIELDS - _SUBPROCESS_OPTIONAL - set(raw))
    if unknown or missing:
        raise ServingConfigurationError(
            f"subprocess serving profile has unknown field(s) {unknown} or missing field(s) "
            f"{missing}"
        )
    engine = _text(raw["engine"], "engine")
    if engine not in _SUBPROCESS_ENGINES:
        raise ServingConfigurationError(
            f"subprocess engine must be one of {sorted(_SUBPROCESS_ENGINES)}, not {engine!r}"
        )
    environment = _text(raw["environment"], "environment")
    if environment != _SUBPROCESS_ENVIRONMENTS[engine]:
        raise ServingConfigurationError(
            f"the {engine} engine runs in {_SUBPROCESS_ENVIRONMENTS[engine]!r}, not {environment!r}"
        )
    device = _text(raw["device"], "device")
    if device not in _SUBPROCESS_DEVICES:
        raise ServingConfigurationError(
            f"subprocess device must be one of {sorted(_SUBPROCESS_DEVICES)}, not {device!r}"
        )
    raw_packages = raw["required_packages"]
    if not isinstance(raw_packages, dict) or set(raw_packages) != _SUBPROCESS_ENGINES[engine]:
        raise ServingConfigurationError(
            f"a subprocess {engine} row pins exactly {sorted(_SUBPROCESS_ENGINES[engine])}"
        )
    return SubprocessProfile(
        recipe=_text(raw["recipe"], "recipe"),
        chair=_text(raw["chair"], "chair"),
        tier=_text(raw["tier"], "tier"),
        engine=engine,
        environment=environment,
        device=device,
        threads=_positive_int(raw["threads"], "threads"),
        workers=_optional_positive_int(raw, "workers"),
        startup_timeout_seconds=_positive_int(
            raw["startup_timeout_seconds"], "startup_timeout_seconds"
        ),
        seconds_per_page=_positive_int(raw["seconds_per_page"], "seconds_per_page"),
        required_packages={
            package: _text(version, f"required_packages.{package}")
            for package, version in raw_packages.items()
        },
    )


def launch_differences(first: ServingProfile, second: ServingProfile) -> list[str]:
    """The launch fields on which two vLLM rows differ; empty when one service fits both."""

    return [name for name in LAUNCH_FIELDS if getattr(first, name) != getattr(second, name)]


def _validate_catalogue(
    profiles: tuple[
        "ServingProfile | InProcessProfile | SubprocessProfile | FixtureProfile | UnsupportedProfile",
        ...,
    ],
) -> None:
    keys = [profile.key for profile in profiles]
    if len(keys) != len(set(keys)):
        raise ServingConfigurationError("serving profiles duplicate a recipe/chair/tier key")
    served = [profile for profile in profiles if isinstance(profile, ServingProfile)]
    for profile in served:
        if profile.shares_service_with is not None:
            _validate_shared_service(profile, served)
    endpoint_chairs: dict[tuple[str, int], set[str]] = {}
    served_chairs: dict[str, set[str]] = {}
    for profile in served:
        # Only a vLLM row owns an endpoint and an API alias; applying these
        # launch-only collision rules to any other kind would invent serving claims.
        # A row sharing another chair's service counts as that chair, so only the
        # checked pair may share an endpoint and an alias.
        owner = profile.shares_service_with or profile.chair
        endpoint_chairs.setdefault((profile.host, profile.port), set()).add(owner)
        served_chairs.setdefault(profile.served_model_id, set()).add(owner)
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


def _validate_shared_service(profile: ServingProfile, served: list[ServingProfile]) -> None:
    """A sharing row needs its partner's vLLM row at its tier, with identical launch fields."""

    name = f"recipe={profile.recipe!r}, chair={profile.chair!r}, tier={profile.tier!r}"
    partners = [
        other
        for other in served
        if other.chair == profile.shares_service_with and other.tier == profile.tier
    ]
    if not partners:
        raise ServingConfigurationError(
            f"serving profile {name} shares the service of chair "
            f"{profile.shares_service_with!r}, which has no vllm row at that tier"
        )
    for partner in partners:
        if partner.shares_service_with is not None:
            raise ServingConfigurationError(
                f"serving profile {name} shares the service of chair {partner.chair!r}, "
                "which itself shares another chair's service; only one hop is allowed"
            )
        differing = launch_differences(profile, partner)
        if differing:
            raise ServingConfigurationError(
                f"serving profile {name} shares the service of chair {partner.chair!r} "
                f"(recipe={partner.recipe!r}), but their launch fields differ: {differing}; "
                "one running service cannot be both"
            )


def verify_recipes_cover_chairs(
    models: ModelsConfig, recipes: ServingRecipes, tiers: Iterable[str]
) -> None:
    """Every configured chair resolves to exactly one profile at every tier.

    ``for_identity`` already refuses zero or several matches, but only at
    launch, on a rented GPU with the meter running. This is the same check
    made offline: a misspelt ``serving_recipe``, a chair or tier added without
    a catalogue row, is a configured thing nothing could ever ask for. Absent
    chairs carry no recipe and are skipped. A proven profile's
    ``preflight_identity_digest`` is reconciled here too, for the same
    reason ``manager._launchable`` refuses a stale one only at launch.
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
        catalogue = recipes.source_path or "the serving catalogue"
        raise ServingConfigurationError(
            f"{catalogue} must match exactly every configured chair at every "
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


def _optional_positive_int(raw: Mapping[str, Any], field: str) -> int | None:
    """A row's optional positive integer: absent is ``None``, present is checked.

    Absent is a fact a consumer can refuse on by name; present-but-nonsense is
    a catalogue defect and is refused here, at load, where every other malformed
    field is.
    """

    if field not in raw:
        return None
    return _positive_int(raw[field], field)


def _optional_gib(raw: Mapping[str, Any], field: str) -> Decimal | None:
    """A row's optional size in GiB, written as a decimal string so its digest is exact."""

    if field not in raw:
        return None
    value = raw[field]
    try:
        parsed = Decimal(value) if isinstance(value, str) else None
    except InvalidOperation:
        parsed = None
    if parsed is None or not parsed.is_finite() or parsed <= 0:
        raise ServingConfigurationError(f"{field} must be a positive decimal string of GiB")
    return parsed


def _nonnegative_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ServingConfigurationError(f"{field} must be a non-negative integer")
    return value


def seal_json_object(value: object, *, label: str) -> tuple[dict[str, object], str]:
    """Materialize one caller-supplied JSON object, returning both of its forms.

    Every request payload this package accepts — a readiness probe from the
    catalogue or a golden-page request — is frozen here
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


def frozen_json(value: object) -> object:
    """Deep-freeze one already-validated JSON value: mappings to read-only proxies, lists to tuples."""

    if isinstance(value, Mapping):
        return MappingProxyType({key: frozen_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(frozen_json(item) for item in value)
    return value


# How deep a frozen JSON value may nest before `thawed_json` refuses it. What it
# thaws is assembled or parsed by this package, a handful of levels deep; the
# bound names a pathological value instead of exhausting the stack.
MAX_JSON_DEPTH = 64


def thawed_json(value: object, _depth: int = 0) -> object:
    """The inverse of :func:`frozen_json`: the plain dicts and lists a JSON writer holds.

    ``json.dumps`` refuses a ``mappingproxy``, so a frozen record is thawed
    before it is serialized. Every level is copied, so the result shares no
    container with its source. Each mapping and sequence level counts toward
    :data:`MAX_JSON_DEPTH`; past it the value is refused by name.
    """

    if isinstance(value, (Mapping, list, tuple)) and _depth > MAX_JSON_DEPTH:
        raise ServingConfigurationError(
            f"a JSON value nests deeper than {MAX_JSON_DEPTH} levels; a value that deep is "
            "a defect in whatever assembled it, not evidence a record can carry"
        )
    if isinstance(value, Mapping):
        return {key: thawed_json(item, _depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [thawed_json(item, _depth + 1) for item in value]
    return value


def package_release(version: str) -> str:
    """The release a package version names: a local build tag such as `+cu130` is dropped."""

    return version.split("+", 1)[0]
