"""The comparator: a post-hoc, read-only IoU join between a completed run and truth.

Not a picker (hard rule 8 / GOVERNANCE 3), and this is enforced, not merely
asserted: it runs after pipeline output is immutable (this module never imports
`pipeline/`, pinned by `test_no_pipeline_imports_corpus.py`'s AST scan across the
whole tree), it never returns to the pipeline (`RunTree` is read here only through
`build_manifest`/`read_artifact`, and this module never calls
`publish_artifact`/`put_blob`/`write_manifest`/`write_index` — `test_compare.py`
asserts this with a `RunTree` subclass that raises if any of those are invoked),
it selects nothing about the *reading* (the pipeline already decided what it
proposed; this only pairs a proposal with a reference box after the fact), and it
drops nothing on either side: every unmatched reference act is reported as a MISS
and every unmatched pipeline act is reported, not scored. `SPEC.md` Section
5.3(d) names this boundary exactly this way.

**IoU assignment.** For one page, every sealed proposal region's final bounds
(`origin: "proposal"`, `2_designator`'s own crop rectangle, never a padded or
recovery bound) is matched against `reference.py`'s reference acts by IoU,
maximising the assignment's total IoU under one predeclared threshold
(`PREDECLARED_IOU_THRESHOLD`) — a pair below threshold is not an eligible edge at
all, so the optimum can never be dragged down by a near-miss it should have
refused. IoU stays exact throughout: `x,y,w,h` are always integers, so
intersection and union areas are integers and every comparison is an exact
`Fraction`, never a float — nothing here is a canonical artifact until the final
record is built, and that record stores areas, not the ratio, because
`common.contracts.canonical` refuses floats outright. The assignment itself is a
small bitmask DP (`_best_assignment`), exact for the page sizes this corpus
actually has (`SPEC.md` Section 5.6: 2-4 records/page measured; `MAX_ACTS_PER_PAGE`
bounds the DP's `2**R` state space and refuses rather than silently degrading to
an approximation on an unexpectedly crowded page).

**Scoring.** A matched pair's CER/WER comes from the sealed instruments this
package does not reimplement: `operations.spike_perlector.normalization`'s
`graphemic-v1` profile and `operations.spike_perlector.scoring.score_text`. This
module supplies the reference text (carried on the reference act, `SPEC.md`
Section 5.3(b)/(d)) and each matched pipeline act's hypothesis text, obtained from
a caller-supplied mapping rather than an assumed Perlector artifact shape:
`compare.py` owns the join and the scoring call, not the Perlector's internal
kinds, and inventing a read of an unverified internal shape here would be
entering evidence this unit cannot justify (hard rule 6). The caller — the
operator surface that actually knows where its run keeps final per-act text —
supplies `hypotheses: {act_id: (OutputStatus, text_or_None)}`.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any, Mapping

from common.contracts.canonical import self_hash, verify_self_hash
from common.contracts.identities import is_well_formed
from common.contracts.stages import DESIGNATOR, EXEMPLAR
from common.runtree.store import RunTree
from operations.spike_perlector.models import OutputStatus
from operations.spike_perlector.normalization import GRAPHEMIC_V1, NormalizationProfile
from operations.spike_perlector.scoring import score_response

from . import CorpusRefusal
from .reference import validate_reference_page

SCHEMA = "reference-comparison.v1"

# One decision, in one place: a proposal/reference pair whose IoU falls below
# this is not an eligible match at all, never merely a low-scoring one.
# Standard object-detection convention (0.5); nothing in this corpus's measured
# geometry argues for a different predeclared value, and a threshold that moves
# per run would make "how many misses" a knob rather than a measurement.
PREDECLARED_IOU_THRESHOLD = Fraction(1, 2)

# Bounds the assignment DP's `2**R` state space. `SPEC.md` Section 5.6 measures
# 2-4 records/page; this is a wide, deliberately round margin above that, not a
# tuned figure — a page that actually exceeds it is refused by name rather than
# silently handed to an assignment that would stop being exact.
MAX_ACTS_PER_PAGE = 20

COMPARE_REFUSAL_REASONS = frozenset(
    {
        "malformed-record",
        "wrong-schema",
        "too-many-acts-for-page",
        "wrong-identity-family",
        "unresolvable-page-ordinal",
        "missing-hypothesis",
        "self-hash-mismatch",
        "run-tree-write-refused",
    }
)

_BOUNDS_FIELDS = frozenset({"x", "y", "w", "h"})
_PIPELINE_ACT_FIELDS = frozenset({"act_id", "bounds"})


def _closed(value: Any, fields: frozenset[str], what: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise CorpusRefusal(f"malformed-record: {what} must be the closed record {sorted(fields)}")
    return value


def _bounds(value: Any, what: str) -> dict[str, int]:
    bounds = _closed(value, _BOUNDS_FIELDS, what)
    if any(not isinstance(bounds[key], int) or isinstance(bounds[key], bool) for key in bounds):
        raise CorpusRefusal(f"malformed-record: {what} must be plain integers")
    if bounds["x"] < 0 or bounds["y"] < 0 or bounds["w"] <= 0 or bounds["h"] <= 0:
        raise CorpusRefusal(f"malformed-record: {what} must have non-negative x/y, positive w/h")
    return bounds


def _validate_pipeline_act(act: Any) -> dict[str, Any]:
    act = _closed(act, _PIPELINE_ACT_FIELDS, "pipeline act")
    if not is_well_formed(act["act_id"]) or not act["act_id"].startswith("act_"):
        raise CorpusRefusal(
            f"wrong-identity-family: pipeline act carries {act['act_id']!r}, which is "
            "not a well-formed act_ identity -- a pac_ reference identity must never "
            "be accepted here"
        )
    _bounds(act["bounds"], f"pipeline act {act['act_id']!r} bounds")
    return act


# --- Geometry: exact, integer-only ------------------------------------------


def _intersection_area(a: dict[str, int], b: dict[str, int]) -> int:
    x1, y1 = max(a["x"], b["x"]), max(a["y"], b["y"])
    x2 = min(a["x"] + a["w"], b["x"] + b["w"])
    y2 = min(a["y"] + a["h"], b["y"] + b["h"])
    if x2 <= x1 or y2 <= y1:
        return 0
    return (x2 - x1) * (y2 - y1)


def _area(box: dict[str, int]) -> int:
    return box["w"] * box["h"]


def _iou(a: dict[str, int], b: dict[str, int]) -> Fraction:
    intersection = _intersection_area(a, b)
    if intersection == 0:
        return Fraction(0)
    union = _area(a) + _area(b) - intersection
    return Fraction(intersection, union)


# --- Assignment: exact bitmask DP over a small state space -----------------


def _best_assignment(
    pipeline_acts: list[dict[str, Any]],
    reference_acts: list[dict[str, Any]],
    threshold: Fraction,
) -> dict[int, int]:
    """The pipeline-index -> reference-index pairing maximising total IoU.

    Every pair below `threshold` is simply not an edge, so the optimum can never
    include one. Deterministic given a fixed input order: ties are broken toward
    the lexicographically-earliest reference mask, which is why every caller sorts
    both lists by id before calling this.
    """
    pipeline_count = len(pipeline_acts)
    reference_count = len(reference_acts)
    if pipeline_count > MAX_ACTS_PER_PAGE or reference_count > MAX_ACTS_PER_PAGE:
        raise CorpusRefusal(
            "too-many-acts-for-page: "
            f"{pipeline_count} pipeline acts / {reference_count} reference acts exceeds "
            f"the predeclared cap of {MAX_ACTS_PER_PAGE} the assignment DP is exact for"
        )
    weight: dict[tuple[int, int], Fraction] = {}
    for p, pact in enumerate(pipeline_acts):
        for r, ract in enumerate(reference_acts):
            iou = _iou(pact["bounds"], ract["region"])
            if iou >= threshold:
                weight[(p, r)] = iou

    size = 1 << reference_count
    dp: list[list[Fraction | None]] = [[None] * size for _ in range(pipeline_count + 1)]
    choice: list[list[tuple[str, int | None] | None]] = [
        [None] * size for _ in range(pipeline_count + 1)
    ]
    dp[0][0] = Fraction(0)
    for p in range(pipeline_count):
        for mask in range(size):
            current = dp[p][mask]
            if current is None:
                continue
            if dp[p + 1][mask] is None or current > dp[p + 1][mask]:
                dp[p + 1][mask] = current
                choice[p + 1][mask] = ("skip", None)
            for r in range(reference_count):
                if mask & (1 << r):
                    continue
                edge = weight.get((p, r))
                if edge is None:
                    continue
                new_mask = mask | (1 << r)
                candidate = current + edge
                best_so_far = dp[p + 1][new_mask]
                if best_so_far is None or candidate > best_so_far:
                    dp[p + 1][new_mask] = candidate
                    choice[p + 1][new_mask] = ("match", r)

    best_mask = 0
    best_value = dp[pipeline_count][0]
    for mask in range(size):
        value = dp[pipeline_count][mask]
        if value is not None and (best_value is None or value > best_value):
            best_value = value
            best_mask = mask

    matches: dict[int, int] = {}
    mask = best_mask
    for p in range(pipeline_count, 0, -1):
        decision = choice[p][mask]
        if decision is None:
            continue
        kind, r = decision
        if kind == "match":
            matches[p - 1] = r
            mask ^= 1 << r
    return matches


# --- Run-tree reading: read-only, bounded to what this join needs ----------


def load_exemplar_page_shas(tree: RunTree) -> dict[int, str]:
    """Ordinal -> sealed page sha256, read from the Exemplar's own manifest.

    Read-only: `build_manifest`/`read_artifact` only. Nothing here writes.
    """
    shas: dict[int, str] = {}
    manifest = tree.build_manifest(EXEMPLAR)
    for entry in manifest["artifacts"]:
        if entry["kind"] != "page":
            continue
        record = tree.read_artifact(EXEMPLAR, "page", entry["artifact_id"])
        payload = record["payload"]
        shas[payload["ordinal"]] = payload["source_sha256"]
    return shas


def load_pipeline_proposal_acts(tree: RunTree) -> list[dict[str, Any]]:
    """Every sealed Designator proposal region, read-only, grouped with its page sha256.

    Only `origin: "proposal"` regions -- a recovery crop's bounds are a Recensor
    request, not a detected act, and `SPEC.md` Section 5.3(d) is explicit that the
    matrix runs over "sealed proposal regions." Returns
    `[{"act_id", "bounds", "page_sha256"}, ...]`.
    """
    page_shas = load_exemplar_page_shas(tree)
    acts: list[dict[str, Any]] = []
    manifest = tree.build_manifest(DESIGNATOR)
    for entry in manifest["artifacts"]:
        if entry["kind"] != "region":
            continue
        record = tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
        payload = record["payload"]
        if payload.get("origin") != "proposal":
            continue
        ordinal = payload["transform"]["source_page_ordinal"]
        page_sha256 = page_shas.get(ordinal)
        if page_sha256 is None:
            raise CorpusRefusal(
                f"unresolvable-page-ordinal: region {record['subject_id']!r} names "
                f"source page ordinal {ordinal}, which no sealed Exemplar page carries"
            )
        acts.append(
            {
                "act_id": record["subject_id"],
                "bounds": dict(payload["transform"]["bounds"]),
                "page_sha256": page_sha256,
            }
        )
    return acts


def _refused_write(*_args: Any, **_kwargs: Any) -> None:
    raise CorpusRefusal(
        "run-tree-write-refused: the comparator is read-only over a completed run "
        "tree by contract -- it never writes into one"
    )


class ReadOnlyRunTree:
    """A `RunTree` wrapper this module uses so its own read-only contract is provable.

    Delegates every read to the wrapped tree and refuses every write outright,
    rather than merely promising not to call one. `test_compare.py` passes this
    wrapper wherever `compare.py` reads a run tree, so a future edit that adds a
    write call fails the test immediately instead of only failing review.
    """

    def __init__(self, tree: RunTree) -> None:
        self._tree = tree

    def build_manifest(self, stage: str, *, verify_inputs: bool = True) -> dict[str, Any]:
        return self._tree.build_manifest(stage, verify_inputs=verify_inputs)

    def read_artifact(self, stage: str, kind: str, artifact_id: str) -> dict[str, Any]:
        return self._tree.read_artifact(stage, kind, artifact_id)

    publish_artifact = _refused_write
    put_blob = _refused_write
    write_manifest = _refused_write
    write_index = _refused_write
    write_run_receipt = _refused_write
    write_approval_record = _refused_write


# --- The comparison record ---------------------------------------------------

_MATRIX_ENTRY_FIELDS = frozenset(
    {"pipeline_act_id", "reference_physical_act_id", "intersection_area", "union_area", "eligible"}
)


def compare_page(
    reference_page: dict[str, Any],
    pipeline_acts: list[dict[str, Any]],
    hypotheses: Mapping[str, tuple[OutputStatus, str | None]],
    *,
    threshold: Fraction = PREDECLARED_IOU_THRESHOLD,
    profile: NormalizationProfile = GRAPHEMIC_V1,
) -> dict[str, Any]:
    """Build one `reference-comparison.v1` for a single page.

    `pipeline_acts` must already be restricted to this page (callers filter
    `load_pipeline_proposal_acts`'s output by `page_sha256 ==
    reference_page["page"]["sha256"]`) -- this function does not filter by page
    itself, so it can be exercised directly against synthetic acts in tests
    without a run tree at all.

    Every reference act not selected by the assignment is a MISS. Every pipeline
    act not selected is reported, not scored -- `reference_page`'s
    `completeness: "records-only"` means an unmatched pipeline act is not
    evidence of a false positive (`SPEC.md` Section 5.3(b)). A matched pair
    without a supplied hypothesis is refused by name rather than silently scored
    as empty: the caller promised a mapping covering every act it expects
    compare_page to score.
    """
    reference_page = validate_reference_page(reference_page)
    pipeline_acts = [_validate_pipeline_act(act) for act in pipeline_acts]

    reference_acts = list(reference_page["acts"])  # already sorted by record_id
    ordered_pipeline = sorted(pipeline_acts, key=lambda act: act["act_id"])

    matches = _best_assignment(ordered_pipeline, reference_acts, threshold)

    matrix: list[dict[str, Any]] = []
    for pact in ordered_pipeline:
        for ract in reference_acts:
            intersection = _intersection_area(pact["bounds"], ract["region"])
            union = _area(pact["bounds"]) + _area(ract["region"]) - intersection
            eligible = intersection * threshold.denominator >= threshold.numerator * union
            matrix.append(
                {
                    "pipeline_act_id": pact["act_id"],
                    "reference_physical_act_id": ract["physical_act_id"],
                    "intersection_area": intersection,
                    "union_area": union,
                    "eligible": eligible,
                }
            )

    matched_pairs: list[dict[str, Any]] = []
    for p, r in sorted(matches.items()):
        pact = ordered_pipeline[p]
        ract = reference_acts[r]
        hypothesis = hypotheses.get(pact["act_id"])
        if hypothesis is None:
            raise CorpusRefusal(
                f"missing-hypothesis: matched pipeline act {pact['act_id']!r} has no "
                "entry in the supplied hypotheses mapping"
            )
        status, text = hypothesis
        score = score_response(ract["text"], status=status, text=text, profile=profile)
        intersection = _intersection_area(pact["bounds"], ract["region"])
        union = _area(pact["bounds"]) + _area(ract["region"]) - intersection
        matched_pairs.append(
            {
                "pipeline_act_id": pact["act_id"],
                "reference_physical_act_id": ract["physical_act_id"],
                "record_id": ract["record_id"],
                "intersection_area": intersection,
                "union_area": union,
                "cer": {
                    "reference_units": score.cer.reference_units,
                    "hypothesis_units": score.cer.hypothesis_units,
                    "matches": score.cer.edits.matches,
                    "substitutions": score.cer.edits.substitutions,
                    "insertions": score.cer.edits.insertions,
                    "deletions": score.cer.edits.deletions,
                },
                "wer": {
                    "reference_units": score.wer.reference_units,
                    "hypothesis_units": score.wer.hypothesis_units,
                    "matches": score.wer.edits.matches,
                    "substitutions": score.wer.edits.substitutions,
                    "insertions": score.wer.edits.insertions,
                    "deletions": score.wer.edits.deletions,
                },
                "status": status.value,
            }
        )

    matched_reference_indices = set(matches.values())
    misses = [
        {"physical_act_id": ract["physical_act_id"], "record_id": ract["record_id"]}
        for r, ract in enumerate(reference_acts)
        if r not in matched_reference_indices
    ]

    matched_pipeline_indices = set(matches.keys())
    unmatched_pipeline = [
        {"act_id": pact["act_id"]}
        for p, pact in enumerate(ordered_pipeline)
        if p not in matched_pipeline_indices
    ]

    body = {
        "schema": SCHEMA,
        "page": {"sha256": reference_page["page"]["sha256"]},
        "threshold": {"numerator": threshold.numerator, "denominator": threshold.denominator},
        "normalization_profile_id": profile.profile_id,
        "matrix": matrix,
        "matched_pairs": matched_pairs,
        "misses": misses,
        "unmatched_pipeline_acts": unmatched_pipeline,
    }
    body["self_hash"] = self_hash(body)
    return validate_comparison(body)


_TOP_FIELDS = frozenset(
    {
        "schema",
        "page",
        "threshold",
        "normalization_profile_id",
        "matrix",
        "matched_pairs",
        "misses",
        "unmatched_pipeline_acts",
        "self_hash",
    }
)


def validate_comparison(comparison: Any) -> dict[str, Any]:
    """Refuse a comparison record that is not exactly `reference-comparison.v1`."""
    comparison = _closed(comparison, _TOP_FIELDS, "reference comparison")
    if comparison["schema"] != SCHEMA:
        raise CorpusRefusal(f"wrong-schema: expected {SCHEMA!r}, got {comparison['schema']!r}")
    if not verify_self_hash(comparison):
        raise CorpusRefusal(
            "self-hash-mismatch: reference comparison self_hash does not verify "
            "against its own content"
        )
    return comparison


__all__ = [
    "SCHEMA",
    "PREDECLARED_IOU_THRESHOLD",
    "MAX_ACTS_PER_PAGE",
    "COMPARE_REFUSAL_REASONS",
    "ReadOnlyRunTree",
    "load_exemplar_page_shas",
    "load_pipeline_proposal_acts",
    "compare_page",
    "validate_comparison",
]
