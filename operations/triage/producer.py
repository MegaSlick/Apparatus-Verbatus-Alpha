"""The deterministic pre-door triage producer and confirmation intake.

Candidate evidence is deliberately not a link.  This module creates one row for every
submitted master and applies a separately validated confirmation before a row can name
a re-shoot cluster.  It never calls a model and never reads a verdict as a preference.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import tomllib
import unicodedata
import warnings
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any, Final, Mapping, Sequence

from PIL import Image

from common.contracts.canonical import canonical_bytes, digest_bytes, digest_of, is_sha256
from common.contracts.errors import IncompatibleReuse, SchemaRefusal
from common.contracts.identities import physical_page_id
from common.corpus_register import (
    EMPTY_REGISTER_DIGEST,
    append_records,
    empty_register,
    membership_heads,
    read_register_path,
    refuse_capture_preference,
    register_digest,
    validate_register_bytes,
)
from common.imaging import ENCODER_LOSSLESS_MODES
from operations.triage.instrument import (
    EVIDENCE_MANIFEST_SCHEMA,
    EVIDENCE_SCHEMA,
    InstrumentConfig,
    InstrumentRefusal,
    validate_candidate_evidence,
    validate_producer_recipe,
)

# `pipeline/0_triage` is intentionally the single top-level `manifest` module.
# The Door uses this same import seam; a second manifest implementation would let
# pre-door validation drift from its consumer.
_TRIAGE_ROOT = Path(__file__).resolve().parents[2] / "pipeline" / "0_triage"
if str(_TRIAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(_TRIAGE_ROOT))
import manifest as triage_manifest  # noqa: E402

CONFIRMATION_SCHEMA: Final = "triage-re-shoot-confirmation.v1"
PRODUCER_IDENTITY: Final = "operations.triage.producer"
PRODUCER_REVISION: Final = "triage-producer-v1"
_EVIDENCE_MANIFEST_FIELDS: Final = {
    "schema",
    "instrument_config_sha256",
    "frame_count",
    "frame_digests",
    "candidate_cost",
    "emitted_evidence_records",
    "emitted_pairs_sha256",
    "evidence_records_sha256",
    "dimension_refused_pairs",
}
_CANDIDATE_COST_FIELDS: Final = {
    "submission_window_pairs",
    "coarse_pairs_examined",
    "global_prefilter_passes",
    "unique_candidate_pairs",
    "dimension_refused_pairs",
}
_MAX_CONFIRMATION_BYTES: Final = 16 * 1024 * 1024


class ProducerRefusal(SchemaRefusal):
    """A named refusal that rejects the complete producer manifest."""


@dataclass(frozen=True)
class SubmittedFrame:
    """One master supplied to the producer; its bytes are read, never rewritten."""

    path: str
    data: bytes


@dataclass(frozen=True)
class ProducedTriage:
    manifest: dict[str, Any]
    clusters: dict[str, dict[str, Any]]
    rows_by_digest: dict[str, dict[str, Any]]


def routes_to_review(row: Mapping[str, Any], path: str | Path) -> bool:
    """Apply the one triage-mode configuration without changing a decision row."""
    if not isinstance(row, Mapping):
        raise ProducerRefusal("producer review routing requires a decision-row mapping")
    try:
        mode = row["mode"]
        confidence = row["confidence"]
    except KeyError as error:
        raise ProducerRefusal("producer review routing row has no mode or confidence") from error
    if mode not in {"manual", "semi", "auto"}:
        raise ProducerRefusal("producer review routing row has an undeclared triage mode")
    if (
        not isinstance(confidence, int)
        or isinstance(confidence, bool)
        or confidence not in range(5)
    ):
        raise ProducerRefusal("producer review routing row has confidence outside [0, 4]")
    try:
        policy = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ProducerRefusal("producer triage modes configuration could not be read") from error
    if not isinstance(policy, dict) or set(policy) != {"manual", "semi", "auto"}:
        raise ProducerRefusal("producer triage modes configuration has the wrong closed schema")
    for declared_mode in ("manual", "semi", "auto"):
        declared = policy[declared_mode]
        if not isinstance(declared, dict) or set(declared) != {"review_at_or_below_confidence"}:
            raise ProducerRefusal("producer triage modes configuration has the wrong closed schema")
        threshold = declared["review_at_or_below_confidence"]
        if (
            not isinstance(threshold, int)
            or isinstance(threshold, bool)
            or threshold not in range(5)
        ):
            raise ProducerRefusal("producer triage modes configuration has the wrong closed schema")
    return confidence <= policy[mode]["review_at_or_below_confidence"]


def _plain_string(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProducerRefusal(f"{what} must be a non-blank string")
    return value


def _nonnegative_int(value: Any, what: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ProducerRefusal(f"{what} must be a non-negative integer")
    return value


def _manifest_pair(value: Any, what: str, frame_digests: set[str]) -> tuple[str, str]:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or value[0] == value[1]
        or not all(is_sha256(item) for item in value)
        or value != sorted(value)
        or not set(value) <= frame_digests
    ):
        raise ProducerRefusal(
            f"manual refusal evidence-not-instrumented: {what} is not one canonical pair "
            "from the instrument pass"
        )
    return value[0], value[1]


def _config_from_recipe(recipe: Mapping[str, Any]) -> InstrumentConfig:
    """Rebuild the recorded comparison declaration without rereading live config."""
    proxy = recipe["proxy_recipe"]
    signature = recipe["signature_recipe"]
    comparison = recipe["comparison_recipe"]
    selection = recipe["candidate_selection_recipe"]
    return InstrumentConfig(
        source_sha256=recipe["instrument_config_sha256"],
        signature_max_edge=proxy["signature_max_edge"],
        review_max_edge=proxy["review_max_edge"],
        grid_columns=signature["grid_columns"],
        grid_rows=signature["grid_rows"],
        global_prefilter_columns=selection["global_prefilter_grid"][0],
        global_prefilter_rows=selection["global_prefilter_grid"][1],
        offset_cells=comparison["offset_cells"],
        mean_delta=signature["mean_delta"],
        mean_tolerance=comparison["mean_tolerance"],
        ink_tolerance=comparison["ink_tolerance"],
        link_agreement_per_mille=comparison["link_agreement_per_mille"],
        blob_share_per_mille=comparison["blob_share_per_mille"],
        span_share_per_mille=comparison["span_share_per_mille"],
        submission_window=selection["submission_window"],
        global_prefilter_agreeing_cells=selection["global_prefilter_agreeing_cells"],
        global_prefilter_mean_tolerance=selection["global_prefilter_mean_tolerance"],
        global_prefilter_ink_tolerance=selection["global_prefilter_ink_tolerance"],
    )


def _decode_dimensions_and_mode(data: bytes, path: str) -> tuple[int, int, str]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                image.load()
                return image.width, image.height, image.mode
    except (
        OSError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as error:
        raise ProducerRefusal(f"producer frame {path!r} could not be decoded") from error


def _whole_frame_row(
    *, corpus_id: str, digest: str, width: int, height: int, mode: str, triage_mode: str
) -> dict[str, Any]:
    # `keep` is only honest where the deterministic encoder can preserve the source
    # samples.  A producer-created fallback does not guess whether a high precision
    # page should become bitonal: RGB is the explicit, loss-aware display conversion.
    colour_mode = "keep" if mode in ENCODER_LOSSLESS_MODES else "rgb"
    part = triage_manifest.make_part(
        {"x": 0, "y": 0, "w": width, "h": height},
        {"x": 0, "y": 0, "w": width, "h": height},
        0,
        colour_mode=colour_mode,
    )
    return triage_manifest.make_row(
        corpus_id=corpus_id,
        source_frame_sha256=digest,
        frame={"width": width, "height": height},
        split=triage_manifest.make_split([part]),
        re_shoot_cluster_id=None,
        confidence=0,
        mode=triage_mode,
        actor={"kind": "producer", "identity": PRODUCER_IDENTITY, "revision": PRODUCER_REVISION},
        human_override=False,
    )


def _verify_transcribed_row(
    row: Mapping[str, Any], frame: SubmittedFrame, digest: str, triage_mode: str
) -> dict[str, Any]:
    """Bind a caller-provided geometry transcription to its submitted master.

    Fixture transcriptions and human proposals enter through the same boundary. A
    structurally valid row is insufficient by itself: its digest, decoded dimensions,
    triage declaration, and colour handling must also agree with the submitted bytes.
    """
    if not isinstance(row, Mapping):
        raise ProducerRefusal(
            "manual refusal invalid-transcription: submitted geometry is not a decision-row mapping"
        )
    try:
        checked = triage_manifest.validate_row(dict(row))
    except (SchemaRefusal, TypeError, ValueError) as error:
        raise ProducerRefusal(
            "manual refusal invalid-transcription: submitted geometry is not a valid closed "
            "decision row"
        ) from error
    if checked["source_frame_sha256"] != digest:
        raise ProducerRefusal(
            "manual refusal digest-bytes-mismatch: transcription names other bytes"
        )
    width, height, source_mode = _decode_dimensions_and_mode(frame.data, frame.path)
    if checked["frame"] != {"width": width, "height": height}:
        raise ProducerRefusal(
            "manual refusal frame-dimensions-mismatch: transcription disagrees with decoded master"
        )
    if checked["mode"] != triage_mode or checked["confidence"] != 0:
        raise ProducerRefusal("transcribed rows must keep producer mode and confidence 0")
    for part in checked["split"]["parts"]:
        if source_mode not in ENCODER_LOSSLESS_MODES and part["colour_mode"] == "keep":
            raise ProducerRefusal(
                "manual refusal keep-over-lossy-mode: master requires explicit colour conversion"
            )
    return checked


def _evidenced_pairs(
    confirmation: Mapping[str, Any],
    instrument_recipe: Mapping[str, Any],
    evidence_manifest: Mapping[str, Any],
    evidence_records: Sequence[Mapping[str, Any]],
    submitted: set[str],
) -> tuple[set[tuple[str, str]], set[str]]:
    """The pairs and frames the instrument actually evidenced for this confirmation.

    A confirmation's `instrument_config_sha256`/`evidence_manifest_sha256` are, on their
    own, well-formed strings a caller could simply invent; nothing upstream of this call
    reads the instrument's real output. This binds both digests to the supplied evidence
    manifest and per-pair candidate records, so a confirmation naming a pair the
    instrument never compared is refused rather than silently trusted.

    Matching each record's declared instrument configuration is necessary and *not*
    sufficient: the configuration digest is public in the manifest, so a caller who
    supplies the genuine manifest can pad the record list with an invented pair that
    quotes it.  The manifest already closes its own books — `emitted_evidence_records`
    and `emitted_pairs_sha256` over the exact pair multiset the pass emitted — so the
    supplied records are reconciled against that accounting rather than merely counted.
    Without it a pair the instrument explicitly *refused* to compare (mismatched
    dimensions, never selected) could still mint a permanent register membership.
    """
    try:
        validated_recipe = validate_producer_recipe(instrument_recipe)
    except InstrumentRefusal as error:
        raise ProducerRefusal(
            "manual refusal evidence-not-instrumented: the supplied producer recipe is invalid"
        ) from error
    if validated_recipe["instrument_config_sha256"] != confirmation["instrument_config_sha256"]:
        raise ProducerRefusal(
            "manual refusal evidence-not-instrumented: confirmation names a different "
            "instrument configuration than its supplied producer recipe"
        )
    if (
        not isinstance(evidence_manifest, Mapping)
        or set(evidence_manifest) != _EVIDENCE_MANIFEST_FIELDS
        or evidence_manifest.get("schema") != EVIDENCE_MANIFEST_SCHEMA
    ):
        raise ProducerRefusal(
            "manual refusal evidence-not-instrumented: the supplied evidence manifest is "
            f"not a closed {EVIDENCE_MANIFEST_SCHEMA} record"
        )
    emitted_record_count = _nonnegative_int(
        evidence_manifest.get("emitted_evidence_records"),
        "evidence manifest emitted_evidence_records",
    )
    if not is_sha256(evidence_manifest.get("emitted_pairs_sha256")):
        raise ProducerRefusal(
            "manual refusal evidence-not-instrumented: the evidence manifest has no valid "
            "emitted-pair accounting digest"
        )
    if not is_sha256(evidence_manifest.get("evidence_records_sha256")):
        raise ProducerRefusal(
            "manual refusal evidence-not-instrumented: the evidence manifest has no valid "
            "evidence-record accounting digest"
        )
    if (
        not isinstance(evidence_records, Sequence)
        or isinstance(evidence_records, (str, bytes))
        or len(evidence_records) != emitted_record_count
    ):
        raise ProducerRefusal(
            "manual refusal evidence-not-instrumented: the supplied candidate evidence "
            "record count does not match the evidence manifest"
        )
    if (
        evidence_manifest.get("instrument_config_sha256")
        != confirmation["instrument_config_sha256"]
    ):
        raise ProducerRefusal(
            "manual refusal evidence-not-instrumented: confirmation names a different "
            "instrument configuration than its supplied evidence manifest"
        )
    if digest_of(evidence_manifest) != confirmation["evidence_manifest_sha256"]:
        raise ProducerRefusal(
            "manual refusal evidence-not-instrumented: confirmation's evidence manifest "
            "digest does not match the supplied evidence manifest"
        )
    # Retained in the pass's own frame order, not sorted: the order is the instrument's
    # submission order and carries meaning its selector window depends on.
    frame_digests = evidence_manifest.get("frame_digests")
    if (
        not isinstance(frame_digests, list)
        or not isinstance(evidence_manifest.get("frame_count"), int)
        or isinstance(evidence_manifest.get("frame_count"), bool)
        or evidence_manifest.get("frame_count") != len(frame_digests)
        or len(set(frame_digests)) != len(frame_digests)
        or not all(is_sha256(item) for item in frame_digests)
    ):
        raise ProducerRefusal(
            "manual refusal evidence-not-instrumented: the supplied evidence manifest "
            "names no distinct set of instrumented frame digests"
        )
    frame_set = set(frame_digests)
    if not frame_set <= submitted:
        raise ProducerRefusal(
            "manual refusal evidence-not-instrumented: the evidence manifest frame set reaches "
            "outside this producer submission"
        )
    candidate_cost = evidence_manifest.get("candidate_cost")
    if not isinstance(candidate_cost, Mapping) or set(candidate_cost) != _CANDIDATE_COST_FIELDS:
        raise ProducerRefusal(
            "manual refusal evidence-not-instrumented: the evidence manifest has no closed "
            "candidate-selection conservation record"
        )
    costs = {
        field: _nonnegative_int(candidate_cost[field], f"evidence manifest candidate cost {field}")
        for field in _CANDIDATE_COST_FIELDS
    }
    frame_count = len(frame_digests)
    all_pair_count = frame_count * (frame_count - 1) // 2
    if any(
        costs[field] > all_pair_count
        for field in (
            "submission_window_pairs",
            "global_prefilter_passes",
            "unique_candidate_pairs",
            "dimension_refused_pairs",
        )
    ):
        raise ProducerRefusal(
            "manual refusal evidence-not-instrumented: candidate accounting exceeds the "
            "submitted frame set's possible pair count"
        )
    selection_recipe = validated_recipe["candidate_selection_recipe"]
    submission_window = selection_recipe["submission_window"]
    expected_window_pairs = {
        tuple(sorted((frame_digests[left], frame_digests[right])))
        for right in range(frame_count)
        for left in range(max(0, right - submission_window), right)
    }
    if costs["submission_window_pairs"] != len(expected_window_pairs):
        raise ProducerRefusal(
            "manual refusal evidence-not-instrumented: candidate accounting disagrees with "
            "the producer recipe's exact submission-window denominator"
        )
    if costs["coarse_pairs_examined"] != all_pair_count:
        raise ProducerRefusal(
            "manual refusal evidence-not-instrumented: candidate accounting silently omits "
            "a pair from the global-prefilter denominator"
        )
    if costs["global_prefilter_passes"] > all_pair_count:
        raise ProducerRefusal(
            "manual refusal evidence-not-instrumented: candidate accounting reports more "
            "global-prefilter passes than examined pairs"
        )
    refused_values = evidence_manifest.get("dimension_refused_pairs")
    if not isinstance(refused_values, list):
        raise ProducerRefusal(
            "manual refusal evidence-not-instrumented: unequal-dimension refusals are not a list"
        )
    if len(refused_values) != costs["dimension_refused_pairs"]:
        raise ProducerRefusal(
            "manual refusal evidence-not-instrumented: unequal-dimension refusal count does "
            "not match the candidate accounting"
        )
    refused_pairs = {
        _manifest_pair(value, "an unequal-dimension refusal", frame_set) for value in refused_values
    }
    if (
        len(refused_pairs) != len(refused_values)
        or len(refused_pairs) != costs["dimension_refused_pairs"]
    ):
        raise ProducerRefusal(
            "manual refusal evidence-not-instrumented: unequal-dimension refusal accounting "
            "does not name each refused pair exactly once"
        )
    pairs: list[list[str]] = []
    validated_records: list[dict[str, Any]] = []
    recorded_config = _config_from_recipe(validated_recipe)
    for record in evidence_records:
        try:
            checked = validate_candidate_evidence(dict(record), recorded_config)
        except (InstrumentRefusal, TypeError, ValueError) as error:
            raise ProducerRefusal(
                "manual refusal evidence-not-instrumented: a supplied candidate evidence "
                f"record is not a valid closed {EVIDENCE_SCHEMA} record under the supplied recipe"
            ) from error
        digests = checked["both_digests"]
        _manifest_pair(digests, "candidate evidence pair", frame_set)
        pairs.append(list(digests))
        validated_records.append(checked)
    # `emitted_pairs_sha256` is a digest over the *sorted list* of emitted pairs, so it
    # closes multiplicity as well as membership: a duplicated record cannot stand in for
    # a dropped one, and a padded record cannot hide behind a matching count.
    if len(pairs) != emitted_record_count or digest_of(sorted(pairs)) != evidence_manifest.get(
        "emitted_pairs_sha256"
    ):
        raise ProducerRefusal(
            "manual refusal evidence-not-instrumented: the supplied candidate evidence "
            "records do not reconcile with the evidence manifest's own accounting of "
            "what the pass emitted"
        )
    # The manifest also binds the records' full content, not only their pair
    # membership: a record edited after the pass (a verdict flipped, a metric
    # altered) still names the same pair and would pass the pair digest alone.
    if digest_of(
        sorted(validated_records, key=lambda item: tuple(item["both_digests"]))
    ) != evidence_manifest.get("evidence_records_sha256"):
        raise ProducerRefusal(
            "manual refusal evidence-not-instrumented: the supplied candidate evidence "
            "record bytes do not match the evidence manifest's sealed record digest"
        )
    emitted_pairs = {(pair[0], pair[1]) for pair in pairs}
    if len(emitted_pairs) != len(pairs) or len(emitted_pairs) != costs["unique_candidate_pairs"]:
        raise ProducerRefusal(
            "manual refusal evidence-not-instrumented: emitted candidate accounting does not "
            "name each selected pair exactly once"
        )
    if emitted_pairs & refused_pairs:
        raise ProducerRefusal(
            "manual refusal evidence-not-instrumented: one candidate pair is both evidenced "
            "and refused"
        )
    reached_pairs = emitted_pairs | refused_pairs
    if not expected_window_pairs <= reached_pairs:
        raise ProducerRefusal(
            "manual refusal evidence-not-instrumented: the selector silently dropped a pair "
            "required by its declared submission window"
        )
    if len(reached_pairs) != costs["unique_candidate_pairs"] + costs[
        "dimension_refused_pairs"
    ] or not (
        max(costs["submission_window_pairs"], costs["global_prefilter_passes"])
        <= len(reached_pairs)
        <= costs["submission_window_pairs"] + costs["global_prefilter_passes"]
    ):
        raise ProducerRefusal(
            "manual refusal evidence-not-instrumented: selected and refused pairs do not "
            "conserve the candidate selector's recorded reach"
        )
    return emitted_pairs, frame_set


def _confirmation_cluster_ids(
    confirmation: Mapping[str, Any],
    submitted: set[str],
    *,
    instrument_recipe: Mapping[str, Any],
    evidence_manifest: Mapping[str, Any],
    evidence_records: Sequence[Mapping[str, Any]],
) -> dict[str, tuple[list[str], list[dict[str, Any]]]]:
    """Validate the closed confirmation and return stable cluster memberships.

    Each page declaration lists every submitted frame that shows it.  The union is the
    cluster's frame membership, so a 63/65-style frame can join *both* pages rather
    than being forced to pick one.  The cluster identity derives only from physical
    page identities and consequently does not change when another capture is appended.
    """
    fields = {
        "schema",
        "corpus_id",
        "appending_run",
        "authority",
        "instrument_config_sha256",
        "evidence_manifest_sha256",
        "clusters",
    }
    if not isinstance(confirmation, dict) or set(confirmation) != fields:
        raise ProducerRefusal("confirmation file has the wrong closed schema")
    if confirmation["schema"] != CONFIRMATION_SCHEMA:
        raise ProducerRefusal("confirmation file has an unknown schema")
    _plain_string(confirmation["corpus_id"], "confirmation corpus_id")
    _plain_string(confirmation["appending_run"], "confirmation appending_run")
    if not is_sha256(confirmation["instrument_config_sha256"]):
        raise ProducerRefusal("confirmation has no valid instrument configuration digest")
    if not is_sha256(confirmation["evidence_manifest_sha256"]):
        raise ProducerRefusal("confirmation has no valid evidence manifest digest")
    evidenced, instrumented_frames = _evidenced_pairs(
        confirmation, instrument_recipe, evidence_manifest, evidence_records, submitted
    )
    authority = confirmation["authority"]
    if not isinstance(authority, dict) or set(authority) != {"kind", "identity", "revision"}:
        raise ProducerRefusal(
            "confirmation authority must be a closed kind/identity/revision record"
        )
    if authority["kind"] not in {"human", "fixture", "measured"}:
        raise ProducerRefusal("confirmation authority kind is not declared")
    _plain_string(authority["identity"], "confirmation authority identity")
    if authority["kind"] == "human":
        if authority["revision"] is not None:
            raise ProducerRefusal("human confirmation authority carries no revision")
    else:
        _plain_string(authority["revision"], "confirmation authority revision")
    clusters = confirmation["clusters"]
    if not isinstance(clusters, list):
        raise ProducerRefusal("confirmation clusters must be a list")
    result: dict[str, tuple[list[str], list[dict[str, Any]]]] = {}
    used_members: set[str] = set()
    used_pages: set[str] = set()
    if not clusters:
        raise ProducerRefusal("confirmation names no cluster; nothing can be confirmed")
    for cluster in clusters:
        if not isinstance(cluster, dict) or set(cluster) != {"pages", "evidence_pairs"}:
            raise ProducerRefusal(
                "confirmation cluster must be a closed pages/evidence_pairs record"
            )
        pages = cluster["pages"]
        if not isinstance(pages, list) or not pages:
            raise ProducerRefusal(
                "confirmation has no physical-page designation; nothing was written"
            )
        members: set[str] = set()
        page_records: list[dict[str, Any]] = []
        identities: list[str] = []
        for page in pages:
            if not isinstance(page, dict) or set(page) != {
                "volume_id",
                "designation",
                "member_frame_sha256",
            }:
                raise ProducerRefusal("confirmation physical page must be a closed declaration")
            volume = _plain_string(page["volume_id"], "confirmation volume_id")
            designation = _plain_string(page["designation"], "confirmation designation")
            page_members = page["member_frame_sha256"]
            if (
                not isinstance(page_members, list)
                or not page_members
                or not all(is_sha256(item) for item in page_members)
                or page_members != sorted(set(page_members))
            ):
                raise ProducerRefusal(
                    "confirmation page members must be sorted unique source-frame SHA-256 digests"
                )
            members.update(page_members)
            identity = physical_page_id(confirmation["corpus_id"], volume, designation)
            if identity in identities:
                raise ProducerRefusal(
                    "confirmation cluster repeats a physical page designation; each page must "
                    "be declared exactly once"
                )
            identities.append(identity)
            page_records.append({"physical_page_id": identity, "members": page_members, **page})
        if not members <= submitted:
            raise ProducerRefusal(
                "manual refusal cluster-member-not-submitted: confirmation names a missing frame"
            )
        if not members <= instrumented_frames:
            # A member the instrument never even proxied cannot have been compared,
            # so no pair naming it can be evidence; refusing here names the frame
            # rather than leaving the pair check to report a confusing absence.
            raise ProducerRefusal(
                "manual refusal evidence-not-instrumented: confirmation names a cluster "
                "member the supplied instrument pass never saw"
            )
        if len(members) < 2:
            raise ProducerRefusal("confirmation cluster must join two or more submitted frames")
        pairs = cluster["evidence_pairs"]
        if not isinstance(pairs, list) or not pairs:
            raise ProducerRefusal("confirmation cluster must retain candidate evidence pairs")
        seen_pairs: set[tuple[str, str]] = set()
        for pair in pairs:
            if (
                not isinstance(pair, list)
                or len(pair) != 2
                or not all(is_sha256(v) for v in pair)
                or pair != sorted(pair)
            ):
                raise ProducerRefusal("confirmation evidence pair must be a sorted digest pair")
            canonical = (pair[0], pair[1])
            if (
                canonical[0] == canonical[1]
                or canonical in seen_pairs
                or not set(canonical) <= members
            ):
                raise ProducerRefusal(
                    "confirmation evidence pairs must be distinct cluster-member pairs"
                )
            if canonical not in evidenced:
                raise ProducerRefusal(
                    "manual refusal evidence-not-instrumented: confirmation names a pair "
                    "the instrument never evidenced"
                )
            seen_pairs.add(canonical)
        cluster_id = "rsc_" + digest_of(sorted(identities))
        if cluster_id in result or used_members & members:
            raise ProducerRefusal("confirmation clusters overlap; a frame cannot be assigned twice")
        if used_pages & set(identities):
            raise ProducerRefusal(
                "confirmation clusters overlap; a physical page cannot be assigned twice"
            )
        used_members.update(members)
        used_pages.update(identities)
        result[cluster_id] = (sorted(members), page_records)
    refuse_capture_preference(confirmation)
    return result


def produce(
    frames: Sequence[SubmittedFrame],
    *,
    corpus_id: str,
    mode: str,
    confirmation: Mapping[str, Any] | None = None,
    instrument_recipe: Mapping[str, Any] | None = None,
    evidence_manifest: Mapping[str, Any] | None = None,
    evidence_records: Sequence[Mapping[str, Any]] | None = None,
    transcribed_rows_by_path: Mapping[str, Mapping[str, Any]] | None = None,
    max_pages_per_shard: int = 1000,
) -> ProducedTriage:
    """Produce exact-coverage rows and apply only an explicit confirmation."""
    _plain_string(corpus_id, "producer corpus_id")
    if mode not in {"manual", "semi", "auto"}:
        raise ProducerRefusal("producer triage mode is not declared")
    if (
        not isinstance(max_pages_per_shard, int)
        or isinstance(max_pages_per_shard, bool)
        or max_pages_per_shard < 1
    ):
        raise ProducerRefusal("producer max_pages_per_shard must be a positive integer")
    if (
        not isinstance(frames, Sequence)
        or isinstance(frames, (str, bytes))
        or not all(
            isinstance(frame, SubmittedFrame)
            and isinstance(frame.path, str)
            and bool(frame.path.strip())
            and isinstance(frame.data, bytes)
            for frame in frames
        )
    ):
        raise ProducerRefusal(
            "manual refusal coverage: every submission must be a named frame with immutable bytes"
        )
    path_keys: set[str] = set()
    for frame in frames:
        path = PurePosixPath(frame.path)
        if (
            path.is_absolute()
            or frame.path == "."
            or path.as_posix() != frame.path
            or ".." in path.parts
            or "\x00" in frame.path
        ):
            raise ProducerRefusal(
                "manual refusal coverage: submitted source paths must be canonical relative paths"
            )
        key = unicodedata.normalize("NFC", frame.path).casefold()
        if key in path_keys:
            raise ProducerRefusal(
                "manual refusal coverage: submitted source paths collide on a "
                "case-insensitive filesystem"
            )
        path_keys.add(key)
    digests = [digest_bytes(frame.data) for frame in frames]
    if len(set(digests)) != len(digests):
        raise ProducerRefusal(
            "manual refusal coverage: submitted source digest appears more than once"
        )
    if len({frame.path for frame in frames}) != len(frames):
        # The Door keys and orders submitted sources by relative path, so two frames
        # at one path have no distinct place in its fan-out; the ambiguity would land
        # as a cluster span this producer computed against an order the Door does not
        # share.
        raise ProducerRefusal(
            "manual refusal coverage: submitted source path appears more than once"
        )
    rows: list[dict[str, Any]] = []
    by_digest: dict[str, dict[str, Any]] = {}
    supplied_paths = {frame.path for frame in frames}
    if transcribed_rows_by_path is None:
        transcribed_rows_by_path = {}
    elif not isinstance(transcribed_rows_by_path, Mapping):
        raise ProducerRefusal(
            "manual refusal coverage: transcribed rows must be a path-to-row mapping"
        )
    if set(transcribed_rows_by_path) - supplied_paths:
        raise ProducerRefusal(
            "manual refusal coverage: transcription has a row without a submitted frame"
        )
    for frame, digest in zip(frames, digests, strict=True):
        width, height, source_mode = _decode_dimensions_and_mode(frame.data, frame.path)
        supplied = transcribed_rows_by_path.get(frame.path)
        row = (
            _verify_transcribed_row(supplied, frame, digest, mode)
            if supplied is not None
            else _whole_frame_row(
                corpus_id=corpus_id,
                digest=digest,
                width=width,
                height=height,
                mode=source_mode,
                triage_mode=mode,
            )
        )
        if row["corpus_id"] != corpus_id:
            raise ProducerRefusal(
                "manual refusal wrong-corpus: row corpus does not match this producer pass"
            )
        # This is intentionally a second byte check even for native fallbacks: it
        # is the producer-side counterpart to the Door's later verification.
        triage_manifest.verify_submitted_frame(row, frame.data)
        rows.append(row)
        by_digest[digest] = row
    if set(by_digest) != set(digests) or len(rows) != len(frames):
        raise ProducerRefusal(
            "manual refusal coverage: every submitted frame requires exactly one row"
        )
    clusters: dict[str, dict[str, Any]] = {}
    if confirmation is not None:
        if not isinstance(confirmation, Mapping):
            raise ProducerRefusal("confirmation file has the wrong closed schema")
        if confirmation.get("corpus_id") != corpus_id:
            raise ProducerRefusal(
                "manual refusal wrong-corpus: confirmation corpus does not match producer pass"
            )
        if instrument_recipe is None or evidence_manifest is None or evidence_records is None:
            raise ProducerRefusal(
                "manual refusal evidence-not-instrumented: a confirmation requires the "
                "producer recipe, candidate evidence manifest, and records it traces to"
            )
        confirmed = _confirmation_cluster_ids(
            confirmation,
            set(digests),
            instrument_recipe=instrument_recipe,
            evidence_manifest=evidence_manifest,
            evidence_records=evidence_records,
        )
        # The span a shard has to hold is measured in Door ordinals, not in frames.
        # `expand_sources` orders submitted sources by relative path and emits one
        # ordinal per split part, and `content_aware_shards` blocks every seam between
        # a cluster's first and last ordinal. Counting members in submission order
        # therefore understates the real span twice over — three frames of five parts
        # each occupy fifteen ordinals — and a cluster this producer waved through can
        # leave the Door with no legal seam at all, which refuses the whole submission.
        ordinal = 0
        first_ordinal: dict[str, int] = {}
        last_ordinal: dict[str, int] = {}
        for position in sorted(range(len(frames)), key=lambda index: frames[index].path):
            digest = digests[position]
            first_ordinal[digest] = ordinal + 1
            ordinal += len(by_digest[digest]["split"]["parts"])
            last_ordinal[digest] = ordinal
        for cluster_id, (members, _pages) in confirmed.items():
            span = (
                max(last_ordinal[member] for member in members)
                - min(first_ordinal[member] for member in members)
                + 1
            )
            if span > max_pages_per_shard:
                raise ProducerRefusal(
                    "manual refusal cluster-span-over-cap: confirmed cluster exceeds shard limit"
                )
            split_counts = {len(by_digest[member]["split"]["parts"]) for member in members}
            if len(split_counts) != 1:
                raise ProducerRefusal("confirmation cluster members have incompatible split counts")
            clusters[cluster_id] = {
                "schema": triage_manifest.CLUSTER_SCHEMA,
                "corpus_id": corpus_id,
                "cluster_id": cluster_id,
                "member_frame_sha256": members,
                "split_count": split_counts.pop(),
            }
            for member in members:
                old = by_digest[member]
                by_digest[member] = triage_manifest.make_row(
                    **{
                        key: value
                        for key, value in old.items()
                        if key not in {"manifest_row_sha256", "re_shoot_cluster_id"}
                    },
                    re_shoot_cluster_id=cluster_id,
                )
        rows = [by_digest[digest] for digest in digests]
    manifest = {"schema": triage_manifest.MANIFEST_SCHEMA, "corpus_id": corpus_id, "records": rows}
    try:
        triage_manifest.validate_manifest(manifest, clusters or None)
    except SchemaRefusal as error:
        raise ProducerRefusal(
            f"manual refusal coverage: produced manifest cannot close: {error}"
        ) from error
    refuse_capture_preference(manifest)
    refuse_capture_preference(clusters)
    return ProducedTriage(manifest=manifest, clusters=clusters, rows_by_digest=by_digest)


def load_confirmation(path: str | Path) -> dict[str, Any]:
    """Read the pre-console confirmation file as canonical closed JSON."""
    try:
        raw = _read_direct_regular_bytes(Path(path), _MAX_CONFIRMATION_BYTES)
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise ProducerRefusal("confirmation file could not be read") from error
    try:
        canonical = canonical_bytes(value)
    except TypeError as error:
        raise ProducerRefusal(
            "confirmation file cannot be represented as canonical JSON"
        ) from error
    if canonical != raw:
        raise ProducerRefusal("confirmation file must be canonical JSON")
    if not isinstance(value, dict) or value.get("schema") != CONFIRMATION_SCHEMA:
        raise ProducerRefusal("confirmation file has an unknown schema")
    refuse_capture_preference(value)
    return value


def append_confirmation_to_register(
    confirmation: Mapping[str, Any],
    produced: ProducedTriage,
    *,
    instrument_recipe: Mapping[str, Any],
    evidence_manifest: Mapping[str, Any],
    evidence_records: Sequence[Mapping[str, Any]],
    register_path: str | Path,
    expected_register_digest: str = EMPTY_REGISTER_DIGEST,
) -> str:
    """Make the confirmation's single ordered register append.

    Physical-page declarations precede membership chain links in the one append.  The
    already validated manifest/cluster records remain in memory for the caller to write
    only after this succeeds; without a designation, `produce` has refused before this
    function can touch either destination.

    A retry that observes the *current* register digest and finds each of this
    confirmation's memberships recorded exactly (the register write of a prior,
    interrupted commit succeeded before its manifest/cluster documents were durably
    published) is not a fresh append: it returns that current digest unchanged so the
    caller can go on to (re)publish the Door documents, rather than being refused
    forever by a commit that can never again find "new" membership to add. A caller
    whose expected digest disagrees with the register's actual current bytes still
    gets the ordinary concurrent-write refusal. A confirmation that omits a member
    from the current head is stale, not idempotent, and is refused before it can
    regress the Door documents.
    """
    confirmed = _confirmation_cluster_ids(
        confirmation,
        set(produced.rows_by_digest),
        instrument_recipe=instrument_recipe,
        evidence_manifest=evidence_manifest,
        evidence_records=evidence_records,
    )
    if not confirmed:
        raise ProducerRefusal("confirmation has no designation; nothing was written")
    try:
        current_bytes = read_register_path(register_path)
    except FileNotFoundError:
        current_bytes = empty_register()
    except OSError as error:
        raise ProducerRefusal(
            "corpus register could not be read; no confirmation append was written. "
            "Restore readable register bytes and retry against their digest."
        ) from error
    prior = validate_register_bytes(current_bytes)
    existing_pages = {
        record["physical_page_id"]
        for record in prior["records"]
        if record["kind"] == "physical-page"
    }
    # Replay, do not scan history. A retracted membership stays in ``records`` as
    # evidence, so the last historical membership record may no longer be the head.
    # Treating it as current lets a repeated confirmation skip the register append
    # and then republish a manifest cluster whose page has no current captures.
    heads = {
        page_id: (head_digest, set(members))
        for page_id, (head_digest, members) in membership_heads(current_bytes).items()
    }
    records: list[dict[str, Any]] = []
    for _cluster_id, (_members, pages) in confirmed.items():
        for page in pages:
            page_id = page["physical_page_id"]
            if page_id not in existing_pages:
                records.append(
                    {
                        "kind": "physical-page",
                        "corpus_id": confirmation["corpus_id"],
                        "volume_id": page["volume_id"],
                        "designation": page["designation"],
                        "physical_page_id": page_id,
                    }
                )
                existing_pages.add(page_id)
            predecessor, prior_members = heads.get(page_id, (None, set()))
            requested_members = set(page["members"])
            if not prior_members <= requested_members:
                raise ProducerRefusal(
                    "manual refusal stale-confirmation-membership: confirmation omits a capture "
                    f"already retained for physical page {page_id}; no register append was "
                    "written. Rebuild the confirmation from the current membership head."
                )
            members = sorted(requested_members)
            if requested_members == prior_members:
                continue
            membership = {
                "kind": "membership",
                "physical_page_id": page_id,
                "members": members,
                "predecessor": predecessor,
                "appending_run": confirmation["appending_run"],
            }
            records.append(membership)
            heads[page_id] = (digest_of(membership), set(members))
    refuse_capture_preference(records)
    if not records:
        # Every membership this confirmation names is already in the register: either
        # a genuine no-op resubmission, or the register half of an earlier crash-split
        # commit. Either way there is nothing new to append. A caller whose expected
        # digest is stale still gets the ordinary concurrent-write refusal below.
        current_digest = register_digest(current_bytes)
        if current_digest != expected_register_digest:
            raise IncompatibleReuse(
                "the corpus register changed after this writer read it; the append was "
                "not written and must be rebuilt against the current register digest"
            )
        return current_digest
    return append_records(register_path, records, expected_digest=expected_register_digest)


def commit_confirmed_production(
    frames: Sequence[SubmittedFrame],
    *,
    corpus_id: str,
    mode: str,
    confirmation: Mapping[str, Any],
    instrument_recipe: Mapping[str, Any],
    evidence_manifest: Mapping[str, Any],
    evidence_records: Sequence[Mapping[str, Any]],
    register_path: str | Path,
    expected_register_digest: str = EMPTY_REGISTER_DIGEST,
    manifest_path: str | Path,
    clusters_path: str | Path,
    authority_path: str | Path,
    transcribed_rows_by_path: Mapping[str, Mapping[str, Any]] | None = None,
    max_pages_per_shard: int = 1000,
) -> tuple[ProducedTriage, str]:
    """Retain authority, append the register, then publish Door documents.

    All manifest and cluster validation happens before any write. The immutable authority
    record is published before the corpus-lifetime register change, so a confirmation can
    never be committed and then lose the evidence that authorized it. The register itself
    has optimistic concurrency plus an atomic replacement; only a successful append permits
    the two prevalidated Door documents to become visible. If the append is refused, the
    retained authority is an honest record of the attempted confirmation, not a claim that
    the register changed.

    A cluster is a corpus-lifetime assertion about which frames show one physical page.
    Its confirmation therefore remains verbatim at ``authority_path``: ``appending_run``
    alone cannot preserve the authority, instrument configuration, and evidence manifest
    behind the assertion. Publishing authority first ensures no register membership or
    Door cluster can appear without it. A byte-identical retry may reuse the path;
    different bytes are refused without overwrite.

    A retry after a crash between the register append and the document writes converges:
    `append_confirmation_to_register` recognizes its memberships are already recorded and
    returns the current register digest instead of refusing, so this function proceeds
    straight to republishing all three documents byte-identically.
    """
    produced = produce(
        frames,
        corpus_id=corpus_id,
        mode=mode,
        confirmation=confirmation,
        instrument_recipe=instrument_recipe,
        evidence_manifest=evidence_manifest,
        evidence_records=evidence_records,
        transcribed_rows_by_path=transcribed_rows_by_path,
        max_pages_per_shard=max_pages_per_shard,
    )
    if not produced.clusters:
        raise ProducerRefusal("confirmation names no cluster; no manifest documents were written")
    try:
        destinations = {
            "register": Path(register_path),
            "authority": Path(authority_path),
            "manifest": Path(manifest_path),
            "clusters": Path(clusters_path),
        }
    except TypeError as error:
        raise ProducerRefusal(
            "confirmed triage destination is not a filesystem path; nothing was written. "
            "Supply one distinct path for each record role."
        ) from error
    destinations = _canonical_distinct_destinations(**destinations)
    _publish_immutable_canonical(destinations["authority"], confirmation)
    successor_digest = append_confirmation_to_register(
        confirmation,
        produced,
        instrument_recipe=instrument_recipe,
        evidence_manifest=evidence_manifest,
        evidence_records=evidence_records,
        register_path=destinations["register"],
        expected_register_digest=expected_register_digest,
    )
    _atomic_write_canonical(destinations["manifest"], produced.manifest)
    _atomic_write_canonical(destinations["clusters"], produced.clusters)
    return produced, successor_digest


def _atomic_write_canonical(path: Path, value: Mapping[str, Any]) -> None:
    """Publish one complete Door document, never a partially written JSON file."""
    data = canonical_bytes(value)
    temporary: Path | None = None
    published = False
    failure: ProducerRefusal | None = None
    cause: OSError | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=path.parent)
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        published = True
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as error:
        state = "was published but is not proven durable" if published else "was not published"
        failure = ProducerRefusal(f"confirmed triage document {path} {state}")
        cause = error
    cleanup_error = _temporary_cleanup_error(temporary)
    if failure is not None:
        if cleanup_error is not None:
            failure.add_note(
                f"confirmed triage document temporary {temporary} also could not be removed: "
                f"{cleanup_error}"
            )
        raise failure from cause
    if cleanup_error is not None:
        raise ProducerRefusal(
            f"confirmed triage document temporary {temporary} could not be removed"
        ) from cleanup_error


def _case_insensitive_path_key(path: Path) -> str:
    return unicodedata.normalize("NFC", os.fspath(path)).casefold()


def _canonical_distinct_destinations(**paths: Path) -> dict[str, Path]:
    """Resolve parents once and refuse spelling or inode aliases before any write."""
    canonical: dict[str, Path] = {}
    spellings: dict[str, str] = {}
    identities: dict[tuple[int, int], str] = {}
    for role, path in paths.items():
        try:
            target = path.parent.resolve(strict=False) / path.name
        except (OSError, RuntimeError) as error:
            raise ProducerRefusal(
                "confirmed triage destination could not be resolved; nothing was written. "
                "Repair the named path and retry."
            ) from error
        key = _case_insensitive_path_key(target)
        prior = spellings.get(key)
        if prior is not None:
            raise ProducerRefusal(
                f"confirmed triage destinations for {prior} and {role} collide on a "
                "case-insensitive filesystem; "
                "nothing was written. Give every record role its own path."
            )
        spellings[key] = role
        try:
            status = os.lstat(target)
        except FileNotFoundError:
            pass
        except OSError as error:
            raise ProducerRefusal(
                "confirmed triage destination identity could not be verified; nothing was "
                "written. Restore readable destination paths and retry."
            ) from error
        else:
            if stat.S_ISLNK(status.st_mode):
                raise ProducerRefusal(
                    f"confirmed triage destination for {role} is a symbolic link; nothing was "
                    "written. Supply a direct path."
                )
            if not stat.S_ISREG(status.st_mode):
                raise ProducerRefusal(
                    f"confirmed triage destination for {role} is not a regular file; nothing "
                    "was written. Supply a direct file path."
                )
            identity = (status.st_dev, status.st_ino)
            prior = identities.get(identity)
            if prior is not None:
                raise ProducerRefusal(
                    f"confirmed triage destinations for {prior} and {role} name one file; "
                    "nothing was written. Give every record role its own path."
                )
            identities[identity] = role
        canonical[role] = target
    return canonical


def _publish_immutable_canonical(path: Path, value: Mapping[str, Any]) -> None:
    """Create one immutable evidence name, accepting only a byte-identical retry."""
    data = canonical_bytes(value)
    temporary: Path | None = None
    published = False
    failure: ProducerRefusal | None = None
    cause: OSError | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=path.parent)
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            try:
                existing = _read_existing_immutable(path, len(data))
            except OSError as error:
                # The reason is carried, and the one recoverable cause is named. An
                # earlier attempt that died between its `os.link` and its cleanup
                # leaves this record with a second name — its own `.tmp-` sibling —
                # and the unaliased check then refuses the byte-identical retry this
                # function documents. "Choose a new path" is the wrong instruction
                # for that case: the bytes on disk are already correct, and removing
                # the leftover sibling restores the retry. Nothing is removed here,
                # because a second link this code did not make is exactly the live
                # mutation channel into immutable evidence the check exists to catch.
                raise ProducerRefusal(
                    f"confirmation authority path {path} already exists but cannot be verified "
                    f"({error}); nothing was overwritten. If an earlier publish was interrupted, "
                    f"a .{path.name}.tmp-* sibling in {path.parent} is a second name for this "
                    "record: remove it and retry. Otherwise choose a new readable authority path."
                ) from error
            if existing != data:
                raise ProducerRefusal(
                    f"confirmation authority path {path} already contains different immutable "
                    "evidence; nothing was overwritten. Choose a new authority path."
                ) from None
        published = True
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except ProducerRefusal as error:
        failure = error
    except OSError as error:
        state = "was published but is not proven durable" if published else "was not published"
        failure = ProducerRefusal(f"confirmation authority record {path} {state}")
        cause = error
    cleanup_error = _temporary_cleanup_error(temporary)
    if failure is not None:
        if cleanup_error is not None:
            failure.add_note(
                f"confirmation authority record temporary {temporary} also could not be "
                f"removed: {cleanup_error}"
            )
        if cause is not None:
            raise failure from cause
        raise failure
    if cleanup_error is not None:
        raise ProducerRefusal(
            f"confirmation authority record temporary {temporary} could not be removed"
        ) from cleanup_error


def _read_existing_immutable(path: Path, expected_size: int) -> bytes:
    """Read a retry target by descriptor without following or accepting aliases."""
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as handle:
        status = os.fstat(handle.fileno())
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
            raise OSError("immutable authority target is not one unaliased regular file")
        existing = handle.read(expected_size + 1)
        current = os.lstat(path)
        if (current.st_dev, current.st_ino) != (status.st_dev, status.st_ino):
            raise OSError("immutable authority target changed while it was verified")
    return existing


def _read_direct_regular_bytes(path: Path, maximum: int) -> bytes:
    """Bound one untrusted file read and never follow or block on its final name."""
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as handle:
        status = os.fstat(handle.fileno())
        if not stat.S_ISREG(status.st_mode):
            raise OSError("input path is not a regular file")
        data = handle.read(maximum + 1)
    if len(data) > maximum:
        raise OSError(f"input exceeds the {maximum}-byte limit")
    return data


def _temporary_cleanup_error(path: Path | None) -> OSError | None:
    """Return cleanup failure so the named operation refusal remains primary."""
    if path is None:
        return None
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        return error
    return None
