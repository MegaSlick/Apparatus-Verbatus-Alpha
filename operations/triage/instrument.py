"""Deterministic, offline co-visibility candidate evidence (Unit 6A).

The instrument reads masters but never writes them, uses no model, and never emits a
cluster link.  Its only conclusion is a recorded candidate verdict for a pair the
two-tier selector considered.  Confirmation and manifest writing belong to Unit 6B.
"""

from __future__ import annotations

import tomllib
from collections import Counter
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Final, Iterable, Sequence

from PIL import Image

from common.contracts.canonical import canonical_bytes, digest_bytes, digest_of
from common.contracts.errors import ContractError
from common.corpus_register import refuse_capture_preference
from common.imaging import MAX_PIXELS, encode_image_deterministic, imaging_library_versions

RECIPE_SCHEMA: Final = "triage-producer-recipe.v1"
EVIDENCE_SCHEMA: Final = "cluster-candidate-evidence.v1"
EVIDENCE_MANIFEST_SCHEMA: Final = "cluster-candidate-evidence-manifest.v1"
DEFAULT_CONFIG_PATH: Final = Path(__file__).with_name("instrument.toml")
_STATUS: Final = "UNMEASURED"
_VERDICTS: Final = ("near-duplicate", "complementary-candidate", "unrelated")
_INSTRUMENT_FIELDS: Final = {
    "signature_max_edge",
    "review_max_edge",
    "grid_columns",
    "grid_rows",
    "global_prefilter_columns",
    "global_prefilter_rows",
    "offset_cells",
}
_THRESHOLD_FIELDS: Final = {
    "mean_delta",
    "mean_tolerance",
    "ink_tolerance",
    "link_agreement_per_mille",
    "blob_share_per_mille",
    "span_share_per_mille",
    "submission_window",
    "global_prefilter_agreeing_cells",
    "global_prefilter_mean_tolerance",
    "global_prefilter_ink_tolerance",
}
_RECIPE_FIELDS: Final = {
    "schema",
    "instrument_config_sha256",
    "proxy_recipe",
    "signature_recipe",
    "comparison_recipe",
    "candidate_selection_recipe",
    "imaging_library_versions",
    "determinism",
    "measurement_status",
}
JPEG_REMEASURE_FAILURE: Final = (
    "JPEG proxy digest is recorded with imaging library versions; if those versions change, "
    "re-measure the JPEG case before making any determinism claim."
)
# Every sentence the sealed recipe declares and the validator then checks is written
# once here. Both halves used to hold their own copy, wrapped differently, so a typo
# corrected in one would have made the validator refuse every recipe the producer
# builds — the producer unable to commit a confirmation at all, over a sentence.
PROXY_REDUCTION: Final = "Pillow.Image.reduce(integer BOX average, nearest integer sample)"
PROXY_INTEGER_FACTOR_RULE: Final = "smallest k with max(width,height)/k <= declared_max_edge"
OFFSET_SELECTION: Final = (
    "maximize agreeing-cell count, then overlap, then minimize Manhattan offset, "
    "then prefer lower signed y and x"
)
VERDICT_RULE: Final = (
    "near-duplicate iff agreement reaches link threshold and either no component "
    "exists, or the whole disagreement is below blob share, or at most two "
    "components include a largest blob reaching blob share; otherwise "
    "complementary-candidate iff agreement is below link threshold and at most "
    "three components include a largest blob reaching span share; otherwise "
    "unrelated"
)
DETERMINISM_CLAIM: Final = (
    "identical input bytes, recorded recipe, and recorded library versions produce "
    "identical proxy bytes"
)
COMPLEMENTARY_CANDIDATE_REASON: Final = "insufficient-co-visible-evidence"
# A near-duplicate verdict is agreement between two signatures, never an assertion
# that two frames show one physical page.  On this corpus that gap is not academic:
# a parish register is a printed ruled form, so two frames of *different* openings
# of one form produce the same 64x48 mean+ink signature and are recorded
# near-duplicate at every plausible threshold.  The named reason travels in every
# emitted record beside the complementary one, and `SIGNATURE_BLINDNESS` states in
# the sealed recipe what the signature provably cannot separate, so nothing
# downstream may read this verdict as identity (GOVERNANCE 10).
NEAR_DUPLICATE_REASON: Final = "signature-agreement-only-not-page-identity"
# Parenthesised element by element on purpose. These are adjacent string literals, so a
# dropped comma merges two statements into one silently, and the validator compares the
# recipe against this same shortened tuple — the sealed recipe would stop telling a
# reader what the signature cannot separate, and nothing would refuse it.
SIGNATURE_BLINDNESS: Final = (
    (
        "two different frames of one printed ruled form, both blank or near-blank, agree "
        "in every cell and are recorded near-duplicate"
    ),
    (
        "two different openings whose ink is co-located to within the cell tolerances "
        "agree in every cell; ink magnitude, not cell agreement, is what separates them"
    ),
    (
        "a written page and its own blank verso form disagree in one connected component "
        "and reach near-duplicate whenever the written share stays inside the link threshold"
    ),
    (
        "cell agreement is a thresholded boolean and discards magnitude; the recorded ink "
        "count totals and their distance are the fields a human reads to catch the three above"
    ),
    (
        "an ink count is within-cell contrast, not an amount of ink: a cell of uniform tone "
        "counts zero whether it is blank paper, a solid dark insert, or a blown highlight"
    ),
)
EVIDENCE_FIELDS: Final = (
    "schema",
    "instrument_config_sha256",
    "both_digests",
    "offset",
    "agreeing_cells",
    "overlapping_cells",
    "disagreeing_component_count",
    "largest_component_share",
    "ink_count_total_left",
    "ink_count_total_right",
    "ink_count_distance_per_mille",
    "verdict",
    "thresholds",
)
EVIDENCE_MANIFEST_FIELDS: Final = (
    "schema",
    "instrument_config_sha256",
    "frame_count",
    "frame_digests",
    "candidate_cost",
    "emitted_evidence_records",
    "emitted_pairs_sha256",
    "evidence_records_sha256",
    "dimension_refused_pairs",
)


class InstrumentRefusal(ContractError):
    """A named configuration, recipe, source, or comparison contract refusal."""


@dataclass(frozen=True)
class InstrumentConfig:
    """The producer's unmeasured tuning declaration, parsed from one TOML file."""

    source_sha256: str
    signature_max_edge: int
    review_max_edge: int
    grid_columns: int
    grid_rows: int
    global_prefilter_columns: int
    global_prefilter_rows: int
    offset_cells: int
    mean_delta: int
    mean_tolerance: int
    ink_tolerance: int
    link_agreement_per_mille: int
    blob_share_per_mille: int
    span_share_per_mille: int
    submission_window: int
    global_prefilter_agreeing_cells: int
    global_prefilter_mean_tolerance: int
    global_prefilter_ink_tolerance: int

    def thresholds_record(self) -> dict[str, int | str]:
        return {
            "mean_delta": self.mean_delta,
            "mean_tolerance": self.mean_tolerance,
            "ink_tolerance": self.ink_tolerance,
            "link_agreement_per_mille": self.link_agreement_per_mille,
            "blob_share_per_mille": self.blob_share_per_mille,
            "span_share_per_mille": self.span_share_per_mille,
            # The threshold snapshot carries both named non-verdict reasons, so
            # neither flag can be mistaken for an assertion by a reader holding
            # the record alone: a complementary candidate is not a link, and a
            # near-duplicate is agreement between signatures, not page identity.
            "complementary_candidate_reason": COMPLEMENTARY_CANDIDATE_REASON,
            "near_duplicate_reason": NEAR_DUPLICATE_REASON,
            # A reader holding evidence without the recipe must not infer that
            # these shipped numbers were measured or tuned. The recipe carries
            # the per-knob declaration; this is the record-local named status.
            "measurement_status": _STATUS,
        }


@dataclass(frozen=True)
class Cell:
    mean_intensity: int
    ink_count: int


@dataclass(frozen=True)
class SignatureGrid:
    source_frame_sha256: str
    frame_size: tuple[int, int]
    proxy_size: tuple[int, int]
    cells: tuple[Cell, ...]
    coarse_cells: tuple[Cell, ...]


@dataclass(frozen=True)
class ProxySet:
    """Both encoded proxy derivatives and the signature data derived from one master."""

    source_frame_sha256: str
    frame_size: tuple[int, int]
    signature_png: bytes
    signature_png_sha256: str
    review_png: bytes
    review_png_sha256: str
    signature: SignatureGrid


@dataclass(frozen=True)
class CandidateCost:
    """Every pair the candidate rule reached, split by what became of it.

    ``submission_window_pairs`` and ``global_prefilter_passes`` count pairs the two
    tiers *reached*, not pairs they admitted: a pair the declared equal-dimension
    precondition then refuses is counted in ``dimension_refused_pairs`` rather than
    dropped, so the arithmetic over one selection closes
    (``unique_candidate_pairs + dimension_refused_pairs`` is the deduplicated reach)
    and no candidate leaves the selector unaccounted for (GOVERNANCE 2).
    """

    submission_window_pairs: int
    coarse_pairs_examined: int
    global_prefilter_passes: int
    unique_candidate_pairs: int
    dimension_refused_pairs: int

    def to_record(self) -> dict[str, int]:
        return {
            "submission_window_pairs": self.submission_window_pairs,
            "coarse_pairs_examined": self.coarse_pairs_examined,
            "global_prefilter_passes": self.global_prefilter_passes,
            "unique_candidate_pairs": self.unique_candidate_pairs,
            "dimension_refused_pairs": self.dimension_refused_pairs,
        }


@dataclass(frozen=True)
class CandidateSelection:
    pairs: tuple[tuple[int, int], ...]
    dimension_refused: tuple[tuple[int, int], ...]
    cost: CandidateCost


def _plain_positive_int(value: Any, what: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise InstrumentRefusal(f"triage instrument {what} must be a positive integer")
    return value


def _lower_sha256(value: Any, what: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise InstrumentRefusal(f"{what} must be a lowercase sha256")
    return value


def _canonical_pair(left_digest: Any, right_digest: Any) -> tuple[str, str]:
    left = _lower_sha256(left_digest, "candidate left frame digest")
    right = _lower_sha256(right_digest, "candidate right frame digest")
    if left == right:
        raise InstrumentRefusal(
            "candidate evidence requires two distinct source frame digests; "
            "a repeated frame identity cannot be counted as a pair"
        )
    return tuple(sorted((left, right)))


def _require_unique_frame_digests(signatures: Sequence[SignatureGrid]) -> None:
    digests = [
        _lower_sha256(signature.source_frame_sha256, "candidate source frame digest")
        for signature in signatures
    ]
    duplicates = sorted(digest for digest, count in Counter(digests).items() if count > 1)
    if duplicates:
        raise InstrumentRefusal(
            "candidate pass repeats source frame digest(s), so index pairs would not be "
            f"unique evidence identities: {duplicates}"
        )


def _require_proxy_integrity(proxies: Sequence[ProxySet]) -> None:
    """Refuse proxy bytes whose carried digests or signature identity drifted."""
    for proxy in proxies:
        if not isinstance(proxy, ProxySet) or not isinstance(proxy.signature, SignatureGrid):
            raise InstrumentRefusal("candidate pass contains no declared proxy set")
        if not isinstance(proxy.signature_png, bytes) or not isinstance(proxy.review_png, bytes):
            raise InstrumentRefusal("candidate proxy payloads must be bytes")
        _lower_sha256(proxy.signature_png_sha256, "candidate signature proxy digest")
        _lower_sha256(proxy.review_png_sha256, "candidate review proxy digest")
        if proxy.source_frame_sha256 != proxy.signature.source_frame_sha256:
            raise InstrumentRefusal(
                "candidate proxy and signature disagree about their source frame digest"
            )
        if proxy.frame_size != proxy.signature.frame_size:
            raise InstrumentRefusal(
                "candidate proxy and signature disagree about their source frame dimensions"
            )
        if digest_bytes(proxy.signature_png) != proxy.signature_png_sha256:
            raise InstrumentRefusal(
                "candidate signature proxy bytes do not match their recorded digest"
            )
        if digest_bytes(proxy.review_png) != proxy.review_png_sha256:
            raise InstrumentRefusal(
                "candidate review proxy bytes do not match their recorded digest"
            )


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> InstrumentConfig:
    """Read the producer-only declaration once and refuse undeclared tuning."""
    try:
        raw = Path(path).read_bytes()
    except OSError as error:
        raise InstrumentRefusal(
            f"triage instrument configuration at {path} could not be read"
        ) from error
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise InstrumentRefusal(
            f"triage instrument configuration at {path} is not valid UTF-8"
        ) from error
    try:
        record = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise InstrumentRefusal(
            f"triage instrument configuration at {path} is not valid TOML"
        ) from error
    if set(record) != {"instrument", "thresholds", "measurement_status"}:
        raise InstrumentRefusal("triage instrument configuration has the wrong closed schema")
    instrument = record["instrument"]
    thresholds = record["thresholds"]
    statuses = record["measurement_status"]
    if not isinstance(instrument, dict) or set(instrument) != _INSTRUMENT_FIELDS:
        raise InstrumentRefusal("triage instrument dimensions have the wrong closed schema")
    if not isinstance(thresholds, dict) or set(thresholds) != _THRESHOLD_FIELDS:
        raise InstrumentRefusal("triage instrument thresholds have the wrong closed schema")
    expected_statuses = _INSTRUMENT_FIELDS | _THRESHOLD_FIELDS
    if not isinstance(statuses, dict) or set(statuses) != expected_statuses:
        raise InstrumentRefusal(
            "triage instrument measurement statuses have the wrong closed schema"
        )
    if any(statuses[name] != _STATUS for name in expected_statuses):
        raise InstrumentRefusal("every triage instrument tuning value must be declared UNMEASURED")
    values = {
        name: _plain_positive_int(value, name)
        for source in (instrument, thresholds)
        for name, value in source.items()
    }
    if values["signature_max_edge"] > values["review_max_edge"]:
        raise InstrumentRefusal("signature proxy edge cannot exceed review proxy edge")
    if values["global_prefilter_columns"] * values["global_prefilter_rows"] != 16:
        raise InstrumentRefusal("global prefilter must use exactly 16 cells")
    if values["grid_columns"] != 64 or values["grid_rows"] != 48:
        raise InstrumentRefusal("signature grid must be exactly 64 by 48 cells")
    if values["offset_cells"] != 3:
        raise InstrumentRefusal(
            "signature comparison offset range must be exactly plus/minus 3 cells"
        )
    if any(
        values[name] > 1000
        for name in ("link_agreement_per_mille", "blob_share_per_mille", "span_share_per_mille")
    ):
        raise InstrumentRefusal("triage per-mille thresholds cannot exceed 1000")
    if values["global_prefilter_agreeing_cells"] > 16:
        raise InstrumentRefusal("global prefilter cannot require more than its 16 cells")
    return InstrumentConfig(source_sha256=digest_bytes(raw), **values)


def _reduce_to_edge(image: Image.Image, max_edge: int) -> Image.Image:
    """Use Pillow's integer ``Image.reduce`` BOX average, never a resampler.

    Pillow 12.3.0 implements ``reduce(k)`` as an integer BOX reduction, rounding
    the average to the nearest representable sample.  The factor is the smallest
    integer that makes the longest edge at most the declared maximum.
    """
    longest = max(image.size)
    factor = max(1, (longest + max_edge - 1) // max_edge)
    return image.reduce(factor)


def _axis_bounds(length: int, cells: int) -> tuple[tuple[int, int], ...]:
    if length < cells:
        raise InstrumentRefusal(
            f"proxy dimension {length} is too small for its required {cells}-cell grid axis"
        )
    base, remainder = divmod(length, cells)
    bounds = []
    start = 0
    for index in range(cells):
        width = base + (1 if index < remainder else 0)
        bounds.append((start, start + width))
        start += width
    return tuple(bounds)


def _require_image_bounds(image: Image.Image) -> None:
    if image.width <= 0 or image.height <= 0:
        raise InstrumentRefusal("cannot build a proxy from an empty image")
    if image.width * image.height > MAX_PIXELS:
        raise InstrumentRefusal(
            f"triage proxy source is past the declared {MAX_PIXELS}-pixel bound"
        )


def _grid(image: Image.Image, columns: int, rows: int, mean_delta: int) -> tuple[Cell, ...]:
    if image.mode != "L":
        raise InstrumentRefusal("triage grids require grayscale L pixels")
    horizontal = _axis_bounds(image.width, columns)
    vertical = _axis_bounds(image.height, rows)
    pixels = image.load()
    result = []
    for top, bottom in vertical:
        for left, right in horizontal:
            count = (right - left) * (bottom - top)
            total = sum(pixels[x, y] for y in range(top, bottom) for x in range(left, right))
            mean = total // count
            ink = sum(
                pixels[x, y] < mean - mean_delta
                for y in range(top, bottom)
                for x in range(left, right)
            )
            result.append(Cell(mean_intensity=mean, ink_count=ink))
    return tuple(result)


def build_proxies(
    image: Image.Image, *, source_frame_sha256: str, config: InstrumentConfig
) -> ProxySet:
    """Make deterministic proxy PNGs and the 64×48 signature from decoded pixels."""
    _lower_sha256(source_frame_sha256, "proxy source frame digest")
    _require_image_bounds(image)
    grayscale = image.convert("L")
    signature_image = _reduce_to_edge(grayscale, config.signature_max_edge)
    review_image = _reduce_to_edge(grayscale, config.review_max_edge)
    cells = _grid(signature_image, config.grid_columns, config.grid_rows, config.mean_delta)
    coarse = _grid(
        signature_image,
        config.global_prefilter_columns,
        config.global_prefilter_rows,
        config.mean_delta,
    )
    signature_png = encode_image_deterministic(signature_image)
    review_png = encode_image_deterministic(review_image)
    return ProxySet(
        source_frame_sha256=source_frame_sha256,
        frame_size=image.size,
        signature_png=signature_png,
        signature_png_sha256=digest_bytes(signature_png),
        review_png=review_png,
        review_png_sha256=digest_bytes(review_png),
        signature=SignatureGrid(
            source_frame_sha256=source_frame_sha256,
            frame_size=image.size,
            proxy_size=signature_image.size,
            cells=cells,
            coarse_cells=coarse,
        ),
    )


def build_proxies_from_bytes(data: bytes, config: InstrumentConfig) -> ProxySet:
    """Decode source bytes once for an offline producer; masters remain untouched."""
    try:
        with Image.open(BytesIO(data)) as image:
            _require_image_bounds(image)
            image.load()
            return build_proxies(image, source_frame_sha256=digest_bytes(data), config=config)
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise InstrumentRefusal(
            "triage proxy source exceeds the decoder pixel safety bound"
        ) from error
    except MemoryError as error:
        raise InstrumentRefusal(
            "triage proxy decoding exhausted memory before a proxy could be built"
        ) from error
    except (OSError, ValueError) as error:
        raise InstrumentRefusal("triage proxy source bytes could not be decoded") from error


def producer_recipe(config: InstrumentConfig) -> dict[str, Any]:
    """Return the sealed recipe written beside the downstream manifest output."""
    record = {
        "schema": RECIPE_SCHEMA,
        "instrument_config_sha256": config.source_sha256,
        "proxy_recipe": {
            "colour_mode": "L",
            "reduction": PROXY_REDUCTION,
            "integer_factor_rule": PROXY_INTEGER_FACTOR_RULE,
            "signature_max_edge": config.signature_max_edge,
            "review_max_edge": config.review_max_edge,
            "encoder": "common.imaging.encode_image_deterministic-v1",
        },
        "signature_recipe": {
            "grid_columns": config.grid_columns,
            "grid_rows": config.grid_rows,
            "remainder_policy": "leading-cells-receive-one-pixel",
            "cell_mean_rounding": "floor",
            "cell_values": ["mean_intensity", "ink_count_below_cell_mean_minus_delta"],
            "mean_delta": config.mean_delta,
        },
        "comparison_recipe": {
            "equal_dimension_precondition": "refuse",
            "offset_cells": config.offset_cells,
            "offset_selection": OFFSET_SELECTION,
            "disagreement_components": "four-connected cells in the left-grid overlap",
            "per_mille_rounding": "floor",
            "verdict_rule": VERDICT_RULE,
            "mean_tolerance": config.mean_tolerance,
            "ink_tolerance": config.ink_tolerance,
            "link_agreement_per_mille": config.link_agreement_per_mille,
            "blob_share_per_mille": config.blob_share_per_mille,
            "span_share_per_mille": config.span_share_per_mille,
            "verdicts": list(_VERDICTS),
            "evidence_fields": list(EVIDENCE_FIELDS),
            "known_blindness": list(SIGNATURE_BLINDNESS),
        },
        "candidate_selection_recipe": {
            "submission_window": config.submission_window,
            "submission_window_rule": "each frame with its preceding N submission indices",
            "global_prefilter_grid": [
                config.global_prefilter_columns,
                config.global_prefilter_rows,
            ],
            "global_prefilter_agreeing_cells": config.global_prefilter_agreeing_cells,
            "global_prefilter_mean_tolerance": config.global_prefilter_mean_tolerance,
            "global_prefilter_ink_tolerance": config.global_prefilter_ink_tolerance,
            "global_prefilter_scope": "every unordered source-frame pair",
            "deduplication": "union by submission-index pair before full comparison",
        },
        "imaging_library_versions": imaging_library_versions(),
        "determinism": {
            "claim": DETERMINISM_CLAIM,
            "cross_version_claim": "NOT_CLAIMED",
            "jpeg_remeasure_failure": JPEG_REMEASURE_FAILURE,
        },
        "measurement_status": {
            name: _STATUS for name in sorted(_INSTRUMENT_FIELDS | _THRESHOLD_FIELDS)
        },
    }
    refuse_capture_preference(record)
    return record


def validate_producer_recipe(record: Any) -> dict[str, Any]:
    """Refuse a malformed recipe before the Door binds its document digest."""
    if not isinstance(record, dict) or set(record) != _RECIPE_FIELDS:
        raise InstrumentRefusal("triage producer recipe has the wrong closed schema")
    if record.get("schema") != RECIPE_SCHEMA:
        raise InstrumentRefusal("triage producer recipe has an unknown schema")
    digest = record.get("instrument_config_sha256")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise InstrumentRefusal("triage producer recipe has no source configuration digest")
    proxy = record["proxy_recipe"]
    if not isinstance(proxy, dict) or set(proxy) != {
        "colour_mode",
        "reduction",
        "integer_factor_rule",
        "signature_max_edge",
        "review_max_edge",
        "encoder",
    }:
        raise InstrumentRefusal("triage producer recipe proxy recipe has the wrong closed schema")
    if (
        proxy["colour_mode"] != "L"
        or proxy["encoder"] != "common.imaging.encode_image_deterministic-v1"
    ):
        raise InstrumentRefusal("triage producer recipe proxy recipe changes the declared encoder")
    if proxy["reduction"] != PROXY_REDUCTION:
        raise InstrumentRefusal(
            "triage producer recipe proxy reduction is not the declared BOX average"
        )
    if proxy["integer_factor_rule"] != PROXY_INTEGER_FACTOR_RULE:
        raise InstrumentRefusal("triage producer recipe proxy scale rule is not declared")
    signature_max_edge = _plain_positive_int(
        proxy["signature_max_edge"], "recipe signature_max_edge"
    )
    review_max_edge = _plain_positive_int(proxy["review_max_edge"], "recipe review_max_edge")
    if signature_max_edge > review_max_edge:
        raise InstrumentRefusal(
            "triage producer recipe signature proxy edge exceeds its review proxy edge"
        )
    signature = record["signature_recipe"]
    if not isinstance(signature, dict) or set(signature) != {
        "grid_columns",
        "grid_rows",
        "remainder_policy",
        "cell_mean_rounding",
        "cell_values",
        "mean_delta",
    }:
        raise InstrumentRefusal(
            "triage producer recipe signature recipe has the wrong closed schema"
        )
    if signature["grid_columns"] != 64 or signature["grid_rows"] != 48:
        raise InstrumentRefusal("triage producer recipe signature grid is not 64 by 48")
    if (
        signature["remainder_policy"] != "leading-cells-receive-one-pixel"
        or signature["cell_mean_rounding"] != "floor"
        or signature["cell_values"] != ["mean_intensity", "ink_count_below_cell_mean_minus_delta"]
    ):
        raise InstrumentRefusal(
            "triage producer recipe signature semantics are not the declared integer grid"
        )
    _plain_positive_int(signature["mean_delta"], "recipe mean_delta")
    comparison = record["comparison_recipe"]
    if not isinstance(comparison, dict) or set(comparison) != {
        "equal_dimension_precondition",
        "offset_cells",
        "offset_selection",
        "disagreement_components",
        "per_mille_rounding",
        "verdict_rule",
        "mean_tolerance",
        "ink_tolerance",
        "link_agreement_per_mille",
        "blob_share_per_mille",
        "span_share_per_mille",
        "verdicts",
        "evidence_fields",
        "known_blindness",
    }:
        raise InstrumentRefusal(
            "triage producer recipe comparison recipe has the wrong closed schema"
        )
    if comparison["equal_dimension_precondition"] != "refuse" or comparison["offset_cells"] != 3:
        raise InstrumentRefusal("triage producer recipe comparison preconditions are not declared")
    if (
        comparison["offset_selection"] != OFFSET_SELECTION
        or comparison["disagreement_components"] != "four-connected cells in the left-grid overlap"
        or comparison["per_mille_rounding"] != "floor"
        or comparison["verdict_rule"] != VERDICT_RULE
    ):
        raise InstrumentRefusal(
            "triage producer recipe comparison semantics are not the declared integer rules"
        )
    if comparison["verdicts"] != list(_VERDICTS):
        raise InstrumentRefusal("triage producer recipe verdict vocabulary is not closed")
    if comparison["evidence_fields"] != list(EVIDENCE_FIELDS):
        raise InstrumentRefusal(
            "triage producer recipe does not describe the evidence record this instrument emits"
        )
    if comparison["known_blindness"] != list(SIGNATURE_BLINDNESS):
        # A recipe that drops the blindness statement would let a reader take a
        # near-duplicate verdict for page identity with nothing in the sealed
        # record contradicting it.  It is the honesty half of GOVERNANCE 10 and
        # is refused exactly as a changed encoder is.
        raise InstrumentRefusal(
            "triage producer recipe does not carry the declared signature blindness statement"
        )
    comparison_values = {
        field: _plain_positive_int(comparison[field], f"recipe {field}")
        for field in (
            "mean_tolerance",
            "ink_tolerance",
            "link_agreement_per_mille",
            "blob_share_per_mille",
            "span_share_per_mille",
        )
    }
    if any(
        comparison_values[field] > 1000
        for field in (
            "link_agreement_per_mille",
            "blob_share_per_mille",
            "span_share_per_mille",
        )
    ):
        raise InstrumentRefusal("triage producer recipe per-mille thresholds exceed 1000")
    selection = record["candidate_selection_recipe"]
    if not isinstance(selection, dict) or set(selection) != {
        "submission_window",
        "submission_window_rule",
        "global_prefilter_grid",
        "global_prefilter_agreeing_cells",
        "global_prefilter_mean_tolerance",
        "global_prefilter_ink_tolerance",
        "global_prefilter_scope",
        "deduplication",
    }:
        raise InstrumentRefusal(
            "triage producer recipe candidate selection has the wrong closed schema"
        )
    # The cell *count* is the invariant, exactly as `load_config` reads it: 16 is what
    # bounds `global_prefilter_agreeing_cells`, and the columns and rows are declared
    # tunable and UNMEASURED. Demanding the literal [4, 4] shipped today would refuse a
    # recipe this module's own configuration loader had just accepted, and refuse it
    # with a message naming 16 cells for a grid that has 16 — sending an operator to
    # count cells in a file that is already correct.
    grid = selection["global_prefilter_grid"]
    if not isinstance(grid, list) or len(grid) != 2:
        raise InstrumentRefusal(
            "triage producer recipe global prefilter grid is not columns by rows"
        )
    columns = _plain_positive_int(grid[0], "recipe global prefilter columns")
    rows = _plain_positive_int(grid[1], "recipe global prefilter rows")
    if columns * rows != 16:
        raise InstrumentRefusal("triage producer recipe global prefilter is not 16 cells")
    if (
        selection["submission_window_rule"] != "each frame with its preceding N submission indices"
        or selection["global_prefilter_scope"] != "every unordered source-frame pair"
        or selection["deduplication"] != "union by submission-index pair before full comparison"
    ):
        raise InstrumentRefusal(
            "triage producer recipe candidate selection semantics are not declared"
        )
    selection_values = {
        field: _plain_positive_int(selection[field], f"recipe {field}")
        for field in (
            "submission_window",
            "global_prefilter_agreeing_cells",
            "global_prefilter_mean_tolerance",
            "global_prefilter_ink_tolerance",
        )
    }
    if selection_values["global_prefilter_agreeing_cells"] > 16:
        raise InstrumentRefusal(
            "triage producer recipe global prefilter requires more than its 16 cells"
        )
    versions = record["imaging_library_versions"]
    if (
        not isinstance(versions, dict)
        or set(versions)
        != {
            "renderer",
            "renderer_version",
            "pillow_heif_version",
            "libheif_version",
        }
        or not all(isinstance(value, str) and value for value in versions.values())
    ):
        raise InstrumentRefusal(
            "triage producer recipe imaging library versions have the wrong closed schema"
        )
    determinism = record["determinism"]
    if (
        not isinstance(determinism, dict)
        or set(determinism)
        != {
            "claim",
            "cross_version_claim",
            "jpeg_remeasure_failure",
        }
        or determinism["claim"] != DETERMINISM_CLAIM
        or determinism["cross_version_claim"] != "NOT_CLAIMED"
        or determinism["jpeg_remeasure_failure"] != JPEG_REMEASURE_FAILURE
    ):
        raise InstrumentRefusal(
            "triage producer recipe determinism claim has the wrong closed schema"
        )
    statuses = record["measurement_status"]
    expected_statuses = _INSTRUMENT_FIELDS | _THRESHOLD_FIELDS
    if (
        not isinstance(statuses, dict)
        or set(statuses) != expected_statuses
        or any(value != _STATUS for value in statuses.values())
    ):
        raise InstrumentRefusal("triage producer recipe must declare every tuning value UNMEASURED")
    refuse_capture_preference(record)
    return record


def _cell_agrees(left: Cell, right: Cell, config: InstrumentConfig) -> bool:
    return (
        abs(left.mean_intensity - right.mean_intensity) <= config.mean_tolerance
        and abs(left.ink_count - right.ink_count) <= config.ink_tolerance
    )


def _require_cells(cells: Sequence[Cell], expected: int, what: str) -> None:
    if len(cells) != expected or any(not isinstance(cell, Cell) for cell in cells):
        raise InstrumentRefusal(f"{what} must carry exactly {expected} declared mean-and-ink cells")


def _overlap_pairs(
    left: SignatureGrid,
    right: SignatureGrid,
    offset_x: int,
    offset_y: int,
    config: InstrumentConfig,
) -> tuple[tuple[bool, int, int], ...]:
    """Return agreement and left-grid coordinates for one signed integer offset."""
    pairs = []
    for y in range(config.grid_rows):
        right_y = y + offset_y
        if not 0 <= right_y < config.grid_rows:
            continue
        for x in range(config.grid_columns):
            right_x = x + offset_x
            if not 0 <= right_x < config.grid_columns:
                continue
            left_cell = left.cells[y * config.grid_columns + x]
            right_cell = right.cells[right_y * config.grid_columns + right_x]
            pairs.append((_cell_agrees(left_cell, right_cell, config), x, y))
    return tuple(pairs)


def _components(disagreements: Iterable[tuple[int, int]]) -> tuple[int, int]:
    remaining = set(disagreements)
    component_count = 0
    largest = 0
    while remaining:
        component_count += 1
        pending = [remaining.pop()]
        size = 0
        while pending:
            x, y = pending.pop()
            size += 1
            for neighbor in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    pending.append(neighbor)
        largest = max(largest, size)
    return component_count, largest


def _ink_overlap(
    left: SignatureGrid,
    right: SignatureGrid,
    offset_x: int,
    offset_y: int,
    config: InstrumentConfig,
) -> tuple[int, int, int]:
    """Ink-count magnitude over the chosen overlap: both totals and their distance.

    Cell agreement is a thresholded boolean, so it throws magnitude away.  Two frames
    whose cells all agree can still carry very different ink counts, and on a ruled
    register that difference is the whole question: a blank form and a written page of
    the same form agree wherever the print dominates.  The distance is the sum of
    absolute per-cell differences over the summed totals, in per mille, all integer.
    Both totals are recorded beside it so the denominator, and an empty one, are visible.

    An ink count is *within-cell contrast* — pixels below that cell's own mean less
    delta — and not an amount of ink.  A cell of uniform tone counts zero whether it
    holds blank paper or a solid dark insert, so these totals are read as what the
    signature saw, not as how much was written.  `SIGNATURE_BLINDNESS` says so in the
    sealed recipe, since a reader who takes them for ink volume would draw the wrong
    conclusion in exactly the case they exist to expose.
    """
    left_total = right_total = absolute = 0
    for y in range(config.grid_rows):
        right_y = y + offset_y
        if not 0 <= right_y < config.grid_rows:
            continue
        for x in range(config.grid_columns):
            right_x = x + offset_x
            if not 0 <= right_x < config.grid_columns:
                continue
            one = left.cells[y * config.grid_columns + x].ink_count
            two = right.cells[right_y * config.grid_columns + right_x].ink_count
            left_total += one
            right_total += two
            absolute += abs(one - two)
    total = left_total + right_total
    return left_total, right_total, (absolute * 1000 // total if total else 0)


def validate_candidate_evidence(record: Any, config: InstrumentConfig) -> dict[str, Any]:
    """Validate the complete record the confirmation interface may consume."""
    if not isinstance(record, dict) or set(record) != set(EVIDENCE_FIELDS):
        raise InstrumentRefusal("candidate evidence has the wrong closed field set")
    if record.get("schema") != EVIDENCE_SCHEMA:
        raise InstrumentRefusal("candidate evidence has an unknown schema")
    if record.get("instrument_config_sha256") != config.source_sha256:
        raise InstrumentRefusal(
            "candidate evidence was not produced by the supplied instrument configuration"
        )
    digests = record.get("both_digests")
    if (
        not isinstance(digests, list)
        or len(digests) != 2
        or list(_canonical_pair(*digests)) != digests
    ):
        raise InstrumentRefusal(
            "candidate evidence must name one canonical pair of distinct source frame digests"
        )
    offset = record.get("offset")
    if not isinstance(offset, dict) or set(offset) != {"x", "y"}:
        raise InstrumentRefusal("candidate evidence offset has the wrong closed schema")
    if any(
        not isinstance(offset[axis], int)
        or isinstance(offset[axis], bool)
        or abs(offset[axis]) > config.offset_cells
        for axis in ("x", "y")
    ):
        raise InstrumentRefusal("candidate evidence offset is outside the declared integer grid")
    integer_fields = (
        "agreeing_cells",
        "overlapping_cells",
        "disagreeing_component_count",
        "largest_component_share",
        "ink_count_total_left",
        "ink_count_total_right",
        "ink_count_distance_per_mille",
    )
    if any(
        not isinstance(record.get(field), int)
        or isinstance(record[field], bool)
        or record[field] < 0
        for field in integer_fields
    ):
        raise InstrumentRefusal("candidate evidence measures must be non-negative integers")
    expected_overlap = (config.grid_columns - abs(offset["x"])) * (
        config.grid_rows - abs(offset["y"])
    )
    overlapping = record["overlapping_cells"]
    agreeing = record["agreeing_cells"]
    components = record["disagreeing_component_count"]
    largest_share = record["largest_component_share"]
    if (
        overlapping != expected_overlap
        or agreeing > overlapping
        or largest_share > 1000
        or record["ink_count_distance_per_mille"] > 1000
        or (
            record["ink_count_total_left"] + record["ink_count_total_right"] == 0
            and record["ink_count_distance_per_mille"] != 0
        )
    ):
        raise InstrumentRefusal("candidate evidence integer measures are internally inconsistent")
    disagreements = overlapping - agreeing
    if disagreements == 0:
        inconsistent_components = components != 0 or largest_share != 0
    else:
        minimum_largest = (disagreements + components - 1) // components if components else 0
        inconsistent_components = (
            not 1 <= components <= disagreements
            or largest_share > disagreements * 1000 // overlapping
            or largest_share < minimum_largest * 1000 // overlapping
        )
    if inconsistent_components:
        raise InstrumentRefusal("candidate evidence component measures are internally inconsistent")
    if record.get("verdict") not in _VERDICTS:
        raise InstrumentRefusal("candidate evidence verdict is outside the named enum")
    expected_verdict = _verdict_for_metrics(
        agreeing,
        overlapping,
        components,
        largest_share,
        config,
    )
    if record["verdict"] != expected_verdict:
        raise InstrumentRefusal(
            "candidate evidence verdict contradicts its recorded integer measures"
        )
    if record.get("thresholds") != config.thresholds_record():
        raise InstrumentRefusal(
            "candidate evidence thresholds, reasons, or measurement status do not match its configuration"
        )
    refuse_capture_preference(record)
    return record


def _verdict_for_metrics(
    agreeing: int,
    overlapping: int,
    components: int,
    largest_share: int,
    config: InstrumentConfig,
) -> str:
    agreement_reaches_link = agreeing * 1000 >= config.link_agreement_per_mille * overlapping
    # A disagreement smaller than the blob share is negligible, not disqualifying.
    # Requiring the largest blob to *reach* that share made the verdict non-monotone in
    # its own evidence: at the shipped values a pair disagreeing in one to thirty of
    # 3072 cells fell past both clauses to "unrelated" — the verdict that tells a human
    # to stop looking — while a pair disagreeing in a hundred was recorded
    # near-duplicate. The tightest agreement is the likeliest re-shoot, and a re-shoot
    # read as unrelated is how two frames of one physical page both enter the corpus as
    # separate pages. The share keeps one meaning, as the floor below which a
    # difference is not worth arguing about; a large *diffuse* disagreement is still
    # not near-duplicate, which is what the component bound was for.
    negligible = (overlapping - agreeing) * 1000 < config.blob_share_per_mille * overlapping
    near_duplicate = agreement_reaches_link and (
        components == 0
        or negligible
        or (components <= 2 and largest_share >= config.blob_share_per_mille)
    )
    complementary = (
        not agreement_reaches_link
        and components <= 3
        and largest_share >= config.span_share_per_mille
    )
    return (
        "near-duplicate"
        if near_duplicate
        else "complementary-candidate"
        if complementary
        else "unrelated"
    )


def compare_signatures(
    left: SignatureGrid, right: SignatureGrid, config: InstrumentConfig
) -> dict[str, Any]:
    """Emit one closed candidate-evidence record, never a link assertion."""
    if left.frame_size != right.frame_size or left.proxy_size != right.proxy_size:
        raise InstrumentRefusal(
            "candidate frames have unequal dimensions; deterministic co-visibility comparison refuses"
        )
    expected_cells = config.grid_columns * config.grid_rows
    _require_cells(left.cells, expected_cells, "candidate left signature")
    _require_cells(right.cells, expected_cells, "candidate right signature")
    pair = _canonical_pair(left.source_frame_sha256, right.source_frame_sha256)
    # Pair identity is unordered. Canonicalizing the operands makes the signed
    # offset and left/right ink totals stable too, rather than letting submission
    # order produce two records for one pair.
    if left.source_frame_sha256 != pair[0]:
        left, right = right, left
    choices = []
    for offset_y in range(-config.offset_cells, config.offset_cells + 1):
        for offset_x in range(-config.offset_cells, config.offset_cells + 1):
            pairs = _overlap_pairs(left, right, offset_x, offset_y, config)
            agreeing = sum(agrees for agrees, _x, _y in pairs)
            # Maximize agreement count, then overlap.  Remaining tie-breakers are
            # explicit so two equivalent offsets cannot depend on dict iteration.
            choices.append(
                (agreeing, len(pairs), -abs(offset_x) - abs(offset_y), -offset_y, -offset_x, pairs)
            )
    agreeing, overlapping, _distance, neg_y, neg_x, pairs = max(choices)
    offset_x, offset_y = -neg_x, -neg_y
    components, largest = _components((x, y) for agrees, x, y in pairs if not agrees)
    largest_share = largest * 1000 // overlapping
    verdict = _verdict_for_metrics(
        agreeing,
        overlapping,
        components,
        largest_share,
        config,
    )
    ink_left, ink_right, ink_disparity = _ink_overlap(left, right, offset_x, offset_y, config)
    record = {
        "schema": EVIDENCE_SCHEMA,
        # The join to the sealed recipe. Without it a confirmation could
        # not say which instrument produced the evidence it acted on: the threshold
        # snapshot below is a copy, not an identity, and two configurations can share
        # thresholds while differing in grid, proxy edge, or candidate rule.
        "instrument_config_sha256": config.source_sha256,
        "both_digests": list(pair),
        "offset": {"x": offset_x, "y": offset_y},
        "agreeing_cells": agreeing,
        "overlapping_cells": overlapping,
        "disagreeing_component_count": components,
        "largest_component_share": largest_share,
        "ink_count_total_left": ink_left,
        "ink_count_total_right": ink_right,
        "ink_count_distance_per_mille": ink_disparity,
        "verdict": verdict,
        "thresholds": config.thresholds_record(),
    }
    if tuple(record) != EVIDENCE_FIELDS:
        raise InstrumentRefusal("candidate evidence does not carry its declared closed field set")
    return validate_candidate_evidence(record, config)


def _comparable(left: SignatureGrid, right: SignatureGrid) -> bool:
    return left.frame_size == right.frame_size and left.proxy_size == right.proxy_size


def _coarse_passes(left: SignatureGrid, right: SignatureGrid, config: InstrumentConfig) -> bool:
    """The 16-cell agreement test alone.

    Dimensions are deliberately not consulted here.  The coarse grid is 4x4 for any
    frame size, so a mismatched pair can still be scored, and scoring it is what lets
    the caller tell "the prefilter did not like this pair" apart from "the prefilter
    wanted this pair and the equal-dimension precondition refused it". Folding the
    two together would make the second case leave no trace.
    """
    agreeing = sum(
        abs(one.mean_intensity - two.mean_intensity) <= config.global_prefilter_mean_tolerance
        and abs(one.ink_count - two.ink_count) <= config.global_prefilter_ink_tolerance
        for one, two in zip(left.coarse_cells, right.coarse_cells, strict=True)
    )
    return agreeing >= config.global_prefilter_agreeing_cells


def select_candidate_pairs(
    signatures: Sequence[SignatureGrid], config: InstrumentConfig
) -> CandidateSelection:
    """Combine bounded submission adjacency with a 16-cell global prefilter.

    Coarse comparisons are deliberately counted separately: the prefilter is a
    recall-visible, cheap first tier rather than a claim that candidate discovery is
    subquadratic.  Full 64×48 comparisons run only for the deduplicated pair set.
    """
    _require_unique_frame_digests(signatures)
    expected_coarse_cells = config.global_prefilter_columns * config.global_prefilter_rows
    for signature in signatures:
        _require_cells(signature.coarse_cells, expected_coarse_cells, "candidate coarse signature")
    pairs: set[tuple[int, int]] = set()
    refused: set[tuple[int, int]] = set()
    window_pairs = 0
    for right_index in range(len(signatures)):
        for left_index in range(max(0, right_index - config.submission_window), right_index):
            left, right = signatures[left_index], signatures[right_index]
            window_pairs += 1
            target = pairs if _comparable(left, right) else refused
            target.add((left_index, right_index))
    coarse_pairs = 0
    coarse_passes = 0
    for left_index, left in enumerate(signatures):
        for right_index in range(left_index + 1, len(signatures)):
            coarse_pairs += 1
            if not _coarse_passes(left, signatures[right_index], config):
                continue
            coarse_passes += 1
            target = pairs if _comparable(left, signatures[right_index]) else refused
            target.add((left_index, right_index))
    ordered = tuple(sorted(pairs))
    dropped = tuple(sorted(refused))
    return CandidateSelection(
        pairs=ordered,
        dimension_refused=dropped,
        cost=CandidateCost(
            submission_window_pairs=window_pairs,
            coarse_pairs_examined=coarse_pairs,
            global_prefilter_passes=coarse_passes,
            unique_candidate_pairs=len(ordered),
            dimension_refused_pairs=len(dropped),
        ),
    )


def evidence_manifest(
    proxies: Sequence[ProxySet],
    selection: CandidateSelection,
    evidence: Sequence[dict[str, Any]],
    config: InstrumentConfig,
) -> dict[str, Any]:
    """Close the books over one instrument pass: what was reached, kept, and refused.

    A verdict is only as honest as the set it belongs to.  Counting the emitted
    records against the candidate rule is what makes a dropped pair *detectable* — a
    reader with the frame digests, the recipe, and this record can recompute the
    selection and find any pair the run failed to emit, instead of taking a shorter
    evidence list at face value (GOVERNANCE 2, and 7's "measure honestly": the pass
    reports its own denominator).  Refused pairs are named, not merely tallied,
    because "which frames could not be compared" is the question a mixed-dimension
    reel actually raises.
    """
    _require_proxy_integrity(proxies)
    _require_unique_frame_digests([proxy.signature for proxy in proxies])
    frame_digests = [
        _lower_sha256(proxy.source_frame_sha256, "candidate proxy source frame digest")
        for proxy in proxies
    ]
    if len(set(frame_digests)) != len(frame_digests):
        raise InstrumentRefusal(
            "candidate pass repeats a proxy source frame digest, so pair identity is ambiguous"
        )
    expected_selection = select_candidate_pairs([proxy.signature for proxy in proxies], config)
    if selection != expected_selection:
        raise InstrumentRefusal(
            "candidate selection does not match the complete selection recomputed from this pass"
        )
    if selection.cost.unique_candidate_pairs != len(
        selection.pairs
    ) or selection.cost.dimension_refused_pairs != len(selection.dimension_refused):
        raise InstrumentRefusal(
            "candidate selection cost does not account for its exact emitted and refused pair sets"
        )
    if len(evidence) != selection.cost.unique_candidate_pairs:
        # Cardinality is checked before a caller-controlled sequence is traversed.
        # Otherwise an arbitrarily long evidence list is fully validated and only
        # then refused for not matching the bounded selected set.
        raise InstrumentRefusal(
            "candidate evidence does not account for every selected pair: "
            f"{len(evidence)} records for {selection.cost.unique_candidate_pairs} selected pairs"
        )
    validated_evidence = [validate_candidate_evidence(record, config) for record in evidence]
    emitted = tuple(_canonical_pair(*record["both_digests"]) for record in validated_evidence)
    selected = tuple(
        _canonical_pair(
            proxies[left].source_frame_sha256,
            proxies[right].source_frame_sha256,
        )
        for left, right in selection.pairs
    )
    if len(set(selected)) != len(selected):
        raise InstrumentRefusal(
            "candidate selection repeats a digest pair; every evidence identity must be unique"
        )
    # Counter equality closes both the set and its multiplicity. A duplicate
    # emission cannot stand in for a missing pair, and cannot be reported twice
    # even if a caller supplies a malformed selection.
    if Counter(emitted) != Counter(selected):
        raise InstrumentRefusal(
            "candidate evidence does not account for every selected pair: "
            f"{len(emitted)} records for {selection.cost.unique_candidate_pairs} selected pairs"
        )
    expected_evidence = [
        compare_signatures(proxies[left].signature, proxies[right].signature, config)
        for left, right in selection.pairs
    ]
    actual_by_pair = {tuple(record["both_digests"]): record for record in validated_evidence}
    expected_by_pair = {tuple(record["both_digests"]): record for record in expected_evidence}
    if actual_by_pair != expected_by_pair:
        raise InstrumentRefusal(
            "candidate evidence measures do not match the records recomputed from this pass"
        )
    record = {
        "schema": EVIDENCE_MANIFEST_SCHEMA,
        "instrument_config_sha256": config.source_sha256,
        "frame_count": len(proxies),
        "frame_digests": frame_digests,
        "candidate_cost": selection.cost.to_record(),
        "emitted_evidence_records": len(emitted),
        "emitted_pairs_sha256": digest_of(sorted(list(pair) for pair in emitted)),
        "evidence_records_sha256": digest_of(
            sorted(validated_evidence, key=lambda item: tuple(item["both_digests"]))
        ),
        "dimension_refused_pairs": [
            list(
                _canonical_pair(
                    proxies[left].source_frame_sha256,
                    proxies[right].source_frame_sha256,
                )
            )
            for left, right in selection.dimension_refused
        ],
    }
    refuse_capture_preference(record)
    return record


def validate_evidence_manifest(
    record: Any,
    proxies: Sequence[ProxySet],
    evidence: Sequence[dict[str, Any]],
    config: InstrumentConfig,
) -> dict[str, Any]:
    """Verify a persisted pass manifest against the evidence and proxy pass it seals."""
    if not isinstance(record, dict) or set(record) != set(EVIDENCE_MANIFEST_FIELDS):
        raise InstrumentRefusal("candidate evidence manifest has the wrong closed field set")
    if record.get("schema") != EVIDENCE_MANIFEST_SCHEMA:
        raise InstrumentRefusal("candidate evidence manifest has an unknown schema")
    expected = evidence_manifest(
        proxies,
        select_candidate_pairs([proxy.signature for proxy in proxies], config),
        evidence,
        config,
    )
    try:
        matches = canonical_bytes(record) == canonical_bytes(expected)
    except (TypeError, UnicodeError) as error:
        raise InstrumentRefusal(
            "candidate evidence manifest cannot be canonically serialized"
        ) from error
    if not matches:
        raise InstrumentRefusal(
            "candidate evidence manifest does not match the proxy pass and evidence it seals"
        )
    refuse_capture_preference(record)
    return record


def candidate_evidence(
    proxies: Sequence[ProxySet], config: InstrumentConfig
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Compare every selected, equal-dimension pair and retain every verdict."""
    _require_proxy_integrity(proxies)
    selection = select_candidate_pairs([proxy.signature for proxy in proxies], config)
    evidence = [
        compare_signatures(proxies[left].signature, proxies[right].signature, config)
        for left, right in selection.pairs
    ]
    return evidence, evidence_manifest(proxies, selection, evidence, config)
