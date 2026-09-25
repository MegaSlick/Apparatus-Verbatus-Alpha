"""Designator: marks out the acts and cuts the crops. It establishes no text.

Only this stage cuts crops; the Recensor may request a recrop, so every crop has
one author. It also emits the **proposal seal**, the immutable list of every act
this run expects, so an act lost between later stages leaves a visible hole.

Each seal entry is `proposed` or `held`; a held act gets a `hold` artifact naming
why (page unsealed, continuation unsealed, or structure pass failed), so it is
still accounted for. A run that held anything exits `EXIT_HELD`.

Regions are append-only per act. Each carries an `origin`: **proposal** regions
(the first crop and any continuation) are what witnesses read; **recovery**
regions are later recrops, so ink only a recovery uncovered was never shown to a
witness. Act identity binds the original proposal and survives recrops; region
identity binds the transform and changes with it.

Sibling modules do the marking-out: `structure.py` finds ink regions,
`grouping.py` assembles them into acts by geometry alone (principle 1),
`geometry.py` pads a rectangle into the capture rectangle, and `conservation.py`
reconciles each page's ink against what was claimed. Their thresholds come from
`grouping_config.py`, resolved against each page's own size in `_analyze_page`
only, because a page-fraction threshold cannot be fixed pixels across a fixture
and a full scan.

    python pipeline/2_designator/run.py --run-root <dir> --run-id <id>
    python pipeline/2_designator/run.py ... --operation recover --act <act_id>
"""

import dataclasses
import sys
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
# `2_designator` is not an importable package name, so sibling modules import
# by plain name; tests load this file via `importlib`, which does not add it.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import conservation  # noqa: E402
import geometry  # noqa: E402
import geometry_layer  # noqa: E402
import grouping  # noqa: E402
import grouping_config  # noqa: E402
import structure  # noqa: E402
import structure_pass  # noqa: E402

from common.chairs.models import AbsentChair, ChairIdentity  # noqa: E402
from common.chairs.registry import ChairRegistry  # noqa: E402
from common.contracts.approval import REAL_INGRESS, parse_ingress_record  # noqa: E402
from common.contracts.canonical import digest_bytes, digest_of, self_hash  # noqa: E402
from common.contracts.errors import ContractError  # noqa: E402
from common.contracts.identities import act_id as derive_minted_act_id  # noqa: E402
from common.contracts.identities import artifact_id, attempt_id, region_id  # noqa: E402
from common.contracts.stages import (  # noqa: E402
    ATTESTATORES,
    DESIGNATOR,
    EXEMPLAR,
    INK_MAP,
    RECENSOR,
)
from common.decoding import load_decoding_policy, structure_recovery_policy  # noqa: E402
from common.exemplar_boundary import (  # noqa: E402
    verify_exemplar_corpus_seal,
    verify_sealed_page_pixels,
)
from common.fixture_identity import act_bounds, act_identity, page_identity  # noqa: E402
from common.imaging import crop_png, dimensions, grayscale_rows  # noqa: E402
from common.recovery import FALLBACK_RECROP  # noqa: E402
from common.stage import (  # noqa: E402
    DESIGNATOR_CHAIR,
    EXIT_COMPLETE,
    EXIT_HELD,
    PAGE_RESIDUAL_AGGREGATE_REASON_CODE,
    RESIDUAL_ENUMERATION_AGGREGATED,
    RESIDUAL_ENUMERATION_COMPLETE,
    SECONDARY_PROPOSER_CHAIR,
    STRUCTURE_ANSWER_KIND,
    STRUCTURE_ANSWER_RECORD_SCHEMA,
    STRUCTURE_ANSWER_RECORD_SCHEMA_V2,
    STRUCTURE_ANSWER_RECORD_SCHEMA_V3,
    StageContext,
    _stage_records,
    continuation_for,
    current_recovery_request,
    expected_acts,
    fallback_page_act_key,
    fixture_serving_details,
    open_stage_context,
    page_residual_act_key,
    run_stage,
    stage_parser,
    validate_serving_provenance,
    verify_structure_attempt_call,
)

# Total attempts, first included, for a whole-page structure call; retries only
# recover a structural loop or invalid layout, never sample for quality. Kept
# equal to common/recovery.py's RULED_ABSOLUTE_CAP by hand: recovery restores
# coverage, never quality, so the attempt ceiling must equal the absolute cap.
ABSOLUTE_STRUCTURE_ATTEMPT_CEILING = 3
STRUCTURE_ATTEMPT_KIND = "structure-attempt"

# Fields a Designator artifact may never carry, at any depth: this stage
# establishes no text, and a transcription would otherwise pass as geometry-shaped
# JSON. "reason" and "rationale" describe which rule fired, not ink, so they stay.
_FORBIDDEN_TEXT_KEYS = frozenset(
    {
        "text",
        "reported",
        "transcription",
        "transcript",
        "content",
        "reading",
        "literal",
        "token",
        "tokens",
        # Not text: the retired picker's words for an elected witness
        # (GLOSSARY, "Retired terms"). No stage elects a witness (principle 1).
        "chosen",
        "pivot",
    }
)

# How much of a page's secondary rescue pass its records enumerate. Kept here,
# not in `common/`, because nothing outside this stage reads it.
SECONDARY_ENUMERATION_COMPLETE = "complete"
SECONDARY_ENUMERATION_WITHHELD = "withheld-page-held"

# Why an act could not be marked out: a closed vocabulary, so consumers branch
# on a code and a new cause must be declared here.
HOLD_REASON_CODES = frozenset(
    {
        "exemplar-page-not-sealed",
        "exemplar-continuation-not-sealed",
        "structure-pass-held",
        "structure-pass-held-on-continuation",
        # Small residuals stay listed one by one on the conservation record but
        # appear as one page hold.
        PAGE_RESIDUAL_AGGREGATE_REASON_CODE,
    }
)


def _is_int(value: object) -> bool:
    """An int that is not a bool, since bool subclasses int."""
    return isinstance(value, int) and not isinstance(value, bool)


def _refuse_text_fields(value, path: str = "$", *, kind: str = "act-group") -> None:
    """Walk a payload and refuse any forbidden content-bearing key, at any depth."""
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in _FORBIDDEN_TEXT_KEYS:
                raise ContractError(
                    f"payload at {path}.{key} carries a forbidden content field; a "
                    f"Designator {kind} artifact carries no text at the schema boundary"
                )
            _refuse_text_fields(item, f"{path}.{key}", kind=kind)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _refuse_text_fields(item, f"{path}[{index}]", kind=kind)


# What an act-group's `detected_bounds` rests on, as a field so consumers can
# tell a measurement from a fallback without reading prose. `detected`: a scanned
# region covers the act. `fallback-tiles`: no eligible group, so the page is cut
# into a fixed grid. Live path only, where the chair proposes and the scan
# corroborates: `shared-detection` (a real region that also covers another act),
# `split-detection` (several regions each cover half) and `model-only` (none
# does). Every value without measured bounds carries null bounds and zero counts,
# since nothing was measured (principle 8).
ACT_GROUP_EVIDENCE = frozenset(
    {
        "detected",
        "fallback-tiles",
        structure_pass.EVIDENCE_SHARED_DETECTION,
        structure_pass.EVIDENCE_SPLIT_DETECTION,
        structure_pass.EVIDENCE_MODEL_ONLY,
    }
)
# Which of the five carry a measured rectangle, and which say nothing measured.
_EVIDENCE_WITH_DETECTED_BOUNDS = frozenset({"detected", structure_pass.EVIDENCE_SHARED_DETECTION})


def _fixture_fallback_explanation(analysis: dict) -> tuple[str, str]:
    """The recorded fallback reason and act rationale for the measured cause."""
    if analysis["background"] is None:
        return (
            "the page's background could not be inferred, so no ink threshold or structural "
            "groups were measured; the page is cut into predetermined overlapping crops and "
            "sent downstream to be read rather than being called blank here",
            "the page's background could not be inferred, so no ink threshold or detected "
            "region corroborates this act; the page's predetermined fallback crops are "
            "separate evidence and are not a detection",
        )
    components = analysis["components"]
    if not components:
        return (
            "the structure pass found no ink to group on this page, so the page is cut into "
            "predetermined overlapping crops and sent downstream to be read rather than being "
            "called blank here; blankness is proved by the witnesses and the Perlector, which "
            "only get a say if the crops reach them",
            "the structure pass found no ink to group on this page, so no detected region "
            "corroborates this act; the page's predetermined fallback crops are separate "
            "evidence and are not a detection",
        )
    if len(analysis["page_spanning"]) == len(components):
        return (
            "the structure pass found ink, but every connected component met the sealed "
            "page-spanning bound and was withheld from grouping; the page is cut into "
            "predetermined overlapping crops and sent downstream to be read",
            "the structure pass found ink, but every connected component met the sealed "
            "page-spanning bound, so no eligible detected region corroborates this act; the "
            "page's predetermined fallback crops are separate evidence and are not a detection",
        )
    return (
        "the structure pass found ink components but assembled no eligible detected group, "
        "so the page is cut into predetermined overlapping crops and sent downstream to be read",
        "the structure pass found ink components but assembled no eligible detected group "
        "that corroborates this act; the page's predetermined fallback crops are separate "
        "evidence and are not a detection",
    )


def _require_evidence_block(block: dict, what: str) -> None:
    """A declared rectangle always; a detected one exactly when detection ran."""
    declared = block["declared_bounds"]
    if not isinstance(declared, dict) or set(declared) != {"x", "y", "w", "h"}:
        raise ContractError(f"a Designator act-group {what} has invalid declared_bounds")
    evidence = block["structure_evidence"]
    if evidence not in ACT_GROUP_EVIDENCE:
        raise ContractError(
            f"a Designator act-group {what} claims structural evidence {evidence!r}, which is "
            f"not one of {sorted(ACT_GROUP_EVIDENCE)}"
        )
    detected = block["detected_bounds"]
    if evidence in _EVIDENCE_WITH_DETECTED_BOUNDS:
        if not isinstance(detected, dict) or set(detected) != {"x", "y", "w", "h"}:
            raise ContractError(f"a Designator act-group {what} has invalid detected_bounds")
        return
    if detected is not None or block["body_member_count"] or block["anchor_count"]:
        raise ContractError(
            f"a Designator act-group {what} claims {evidence} evidence but carries detected "
            "bounds or members; a predetermined grid or an uncorroborated rectangle detected "
            "nothing and may not report a region or a member count as if it had"
        )


def _validate_act_group_payload(payload: object) -> None:
    """Validate the closed, geometry-only act-group contract before publication."""
    required = {
        "act_key",
        "declared_bounds",
        "structure_evidence",
        "detected_bounds",
        "body_member_count",
        "anchor_count",
        "rationale",
        "continuation",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise ContractError("a Designator act-group payload has fields outside its closed contract")
    _require_evidence_block(payload, "payload")
    continuation = payload["continuation"]
    if continuation is not None:
        continuation_fields = {
            "declared_bounds",
            "structure_evidence",
            "detected_bounds",
            "body_member_count",
            "anchor_count",
            "rationale",
            "geometric_corroboration",
        }
        if not isinstance(continuation, dict) or set(continuation) != continuation_fields:
            raise ContractError(
                "a Designator act-group continuation has fields outside its closed contract"
            )
        _require_evidence_block(continuation, "continuation")
    _refuse_text_fields(payload)


# The `structure-answer` record's closed field sets. `_refuse_text_fields` only
# catches known names; a closed set also refuses a new field nobody declared,
# which is how a chair's reading could leak. Nested shapes are closed too.
_STRUCTURE_ANSWER_V1_FIELDS = frozenset(
    {
        "schema",
        "page_id",
        "page_ordinal",
        "page_w",
        "page_h",
        "prompt_version",
        "prompt_sha256",
        "answer_schema",
        # The rule the text digests were taken under, and the vendor code whose
        # prompt and grammar were used; neither carries text.
        "text_view",
        "vendor",
        "call_record_ref",
        "raw_response_ref",
        "custody_ref",
        "custody_problem",
        "receipt_ref",
        "request_sha256",
        "finish_reason",
        "served_model_id",
        "call_problem",
        "parse_state",
        "parse_outcome",
        "disposition",
        "reason_code",
        # Blocks that proposed no rectangle are recorded, never minted or dropped.
        "block_count",
        "blocks_without_proposal",
        "act_count",
        "acts",
        "findings",
        "quantization",
        "page_text_rule",
        "decoding",
        "provenance",
        # The request-capacity record this page was admitted or held on; counts
        # and dimensions only.
        "capacity",
    }
)
_STRUCTURE_ANSWER_V2_FIELDS = _STRUCTURE_ANSWER_V1_FIELDS | frozenset(
    {"attempt_ordinal", "attempts", "attempt_seed", "attempt_policy"}
)
_STRUCTURE_ANSWER_V3_FIELDS = _STRUCTURE_ANSWER_V2_FIELDS | frozenset({"presentation_ref"})
# Geometry, with the chair's free strings only as digest and length; `label` and
# `text` are absent so they refuse. `label_vocabulary` is one of the vendor
# grammar's fixed words or null, which is not a reading.
_STRUCTURE_ANSWER_ACT_FIELDS = frozenset(
    {
        "ordinal",
        "box_1000",
        "raw_bounds",
        "text_digest",
        "text_length",
        "label_vocabulary",
        "label_declared",
        "label_digest",
        "label_length",
        "nested_bbox_count",
    }
)
# A block that proposed no rectangle: an act row without geometry, plus a closed
# reason. Closed separately so a field cannot wander between the two shapes.
_STRUCTURE_ANSWER_UNPROPOSED_FIELDS = frozenset(
    {
        "ordinal",
        "reason",
        "blank_page",
        "label_vocabulary",
        "label_declared",
        "label_digest",
        "label_length",
        "text_digest",
        "text_length",
        "nested_bbox_count",
    }
)
_STRUCTURE_ANSWER_VENDOR_FIELDS = frozenset(
    {"repository", "commit", "licence", "prompt_source", "parser_source", "prompt_sha256"}
)
_STRUCTURE_ANSWER_DECODING_FIELDS = frozenset({"policy", "temperature", "decoding_config_sha256"})
_STRUCTURE_ATTEMPT_REFERENCE_FIELDS = frozenset({"relative_path", "sha256"})
_STRUCTURE_ATTEMPT_POLICY_FIELDS = frozenset({"max_attempts", "seed_schedule"})
# Finding kinds from this pass and `common/chandra_layout.py`, declared
# independently of the producer so the validator cannot agree by construction.
# `data_bbox` appears only as a digest: this stage publishes no string the chair wrote.
_STRUCTURE_ANSWER_FINDING_FIELDS = {
    "duplicate-rectangle": frozenset({"kind", "ordinals"}),
    "malformed-bbox": frozenset(
        {"kind", "ordinal", "reason", "data_bbox_digest", "data_bbox_truncated"}
    ),
    "blank-page-retained": frozenset({"kind", "ordinal"}),
    "nested-bbox-retained": frozenset({"kind", "blocks", "attributes"}),
    "unclosed-block": frozenset({"kind", "ordinal", "detail"}),
    "block-count-mismatch": frozenset({"kind", "parsed_blocks", "top_level_divs"}),
    "content-outside-blocks": frozenset({"kind", "characters", "detail"}),
}


def _closed_object(value: object, fields: frozenset, what: str) -> dict:
    """Exactly these field names, no more and no fewer -- and the extras named."""
    if not isinstance(value, dict):
        raise ContractError(f"a Designator {what} is not an object")
    unexpected = sorted(set(value) - fields)
    missing = sorted(fields - set(value))
    if unexpected or missing:
        raise ContractError(
            f"a Designator {what} is outside its closed contract: unexpected "
            f"{unexpected}, missing {missing}"
        )
    return value


def _validate_structure_answer_payload(payload: object, *, terminal: bool = True) -> None:
    """Validate one legacy terminal answer or one versioned attempt/terminal record."""
    if not isinstance(payload, dict):
        raise ContractError("a Designator structure-answer payload is not an object")
    schema = payload.get("schema")
    if schema == STRUCTURE_ANSWER_RECORD_SCHEMA:
        if not terminal:
            raise ContractError("a legacy structure-answer cannot be used as an attempt record")
        record = _closed_object(
            payload, _STRUCTURE_ANSWER_V1_FIELDS, "legacy structure-answer payload"
        )
    elif schema == STRUCTURE_ANSWER_RECORD_SCHEMA_V2:
        record = _closed_object(payload, _STRUCTURE_ANSWER_V2_FIELDS, "v2 structure-answer payload")
    elif schema == STRUCTURE_ANSWER_RECORD_SCHEMA_V3:
        record = _closed_object(payload, _STRUCTURE_ANSWER_V3_FIELDS, "v3 structure-answer payload")
        _closed_object(
            record["presentation_ref"],
            _STRUCTURE_ATTEMPT_REFERENCE_FIELDS,
            "structure presentation reference",
        )
    else:
        raise ContractError(f"a Designator structure answer has unsupported schema {schema!r}")
    _closed_object(
        record["decoding"], _STRUCTURE_ANSWER_DECODING_FIELDS, "structure-answer decoding block"
    )
    _closed_object(record["vendor"], _STRUCTURE_ANSWER_VENDOR_FIELDS, "structure-answer vendor")
    if schema in {STRUCTURE_ANSWER_RECORD_SCHEMA_V2, STRUCTURE_ANSWER_RECORD_SCHEMA_V3}:
        policy = _closed_object(
            record["attempt_policy"],
            _STRUCTURE_ATTEMPT_POLICY_FIELDS,
            "structure attempt policy",
        )
        maximum = policy["max_attempts"]
        if (
            not _is_int(maximum)
            or not 1 <= maximum <= ABSOLUTE_STRUCTURE_ATTEMPT_CEILING
            or policy["seed_schedule"] not in {"fixed-base", "base-plus-attempt-ordinal-minus-one"}
        ):
            raise ContractError("a Designator structure answer has an invalid attempt policy")
        ordinal = record["attempt_ordinal"]
        if not _is_int(ordinal) or not 1 <= ordinal <= maximum:
            raise ContractError(
                "a Designator structure attempt ordinal is outside the sealed range"
            )
        if not _is_int(record["attempt_seed"]) or record["attempt_seed"] < 0:
            raise ContractError("a Designator structure attempt has no non-negative derived seed")
        attempts = record["attempts"]
        expected_count = ordinal if terminal else ordinal - 1
        if not isinstance(attempts, list) or len(attempts) != expected_count:
            raise ContractError(
                "a Designator structure answer does not name its exact contiguous attempt history"
            )
        for reference in attempts:
            _closed_object(
                reference, _STRUCTURE_ATTEMPT_REFERENCE_FIELDS, "structure attempt reference"
            )
    acts = record["acts"]
    if not isinstance(acts, list):
        raise ContractError("a Designator structure-answer payload carries no act list")
    for act in acts:
        _closed_object(act, _STRUCTURE_ANSWER_ACT_FIELDS, "structure-answer act")
    unproposed = record["blocks_without_proposal"]
    if not isinstance(unproposed, list):
        raise ContractError(
            "a Designator structure-answer payload carries no list of the blocks that "
            "proposed nothing; a block dropped from the mint is recorded or it is lost"
        )
    for block in unproposed:
        _closed_object(
            block, _STRUCTURE_ANSWER_UNPROPOSED_FIELDS, "structure-answer unproposed block"
        )
        reason = block["reason"]
        if reason not in structure_pass.NO_PROPOSAL_REASONS:
            raise ContractError(
                f"a Designator structure-answer block proposed nothing for reason {reason!r}, "
                f"which is not one of the declared reasons "
                f"{sorted(structure_pass.NO_PROPOSAL_REASONS)}"
            )
    findings = record["findings"]
    if not isinstance(findings, list):
        raise ContractError("a Designator structure-answer payload carries no finding list")
    for finding in findings:
        kind = finding.get("kind") if isinstance(finding, dict) else None
        fields = _STRUCTURE_ANSWER_FINDING_FIELDS.get(kind)
        if fields is None:
            raise ContractError(
                f"a Designator structure-answer finding of kind {kind!r} is not a declared "
                f"finding kind; declared kinds are "
                f"{sorted(_STRUCTURE_ANSWER_FINDING_FIELDS)}"
            )
        _closed_object(finding, fields, f"structure-answer {kind} finding")
    _refuse_text_fields(record)


def _configured_chair_record(context, resolved: ChairIdentity) -> dict:
    """The provenance block a resolved chair contributes to every artifact."""
    return {
        "chair": resolved.role,
        "chair_state": "configured",
        "resolved_identity": resolved.to_record(),
        "resolved_revision": {
            "kind": resolved.receipt_revision_kind,
            "value": resolved.receipt_revision,
        },
        "receipt_ref": context.write_serving_receipt(resolved, fixture_serving_details(resolved)),
        "adapter_revision": context.adapter_revision,
    }


def _absent_chair_record(context, resolved: AbsentChair) -> dict:
    return {
        "chair": resolved.role,
        "chair_state": "absent",
        "absence": resolved.to_record(),
        "resolved_identity": None,
        "resolved_revision": None,
        "receipt_ref": None,
        "adapter_revision": context.adapter_revision,
    }


def structure_provenance(context) -> dict:
    """Verify and record the exact chair that produced structural proposals.

    An absent or unverifiable Designator is a refusal, never a cue to mark out
    structure through a different role.
    """
    resolved = context.registry.resolve(DESIGNATOR_CHAIR)
    if isinstance(resolved, AbsentChair):
        raise ContractError(
            f"the Designator chair is explicitly absent: {resolved.reason}; "
            "no other chair may mark out structure"
        )
    if not isinstance(resolved, ChairIdentity):
        raise ContractError("Designator resolution returned neither an identity nor an absence")
    return _configured_chair_record(context, resolved)


def _publish_secondary_provenance(context, secondary: dict) -> dict:
    context.publish(
        kind="secondary-provenance",
        subject_id="secondary-provenance",
        outcome="proposed",
        inputs=[],
        payload=secondary,
    )
    return secondary


def secondary_provenance(context) -> dict:
    """Resolve and record the secondary proposer chair, absent or configured.

    Absence is not a refusal, since the secondary proposer has no crop
    authority. The role is still resolved every run so
    `common/stage.py::unaddressed_chairs` stays accurate about it.
    """
    resolved = context.registry.resolve(SECONDARY_PROPOSER_CHAIR)
    if isinstance(resolved, AbsentChair):
        return _absent_chair_record(context, resolved)
    if not isinstance(resolved, ChairIdentity):
        raise ContractError(
            "secondary proposer resolution returned neither an identity nor an absence"
        )
    return _configured_chair_record(context, resolved)


def _read_checked_page_bytes(context, page_record: dict) -> bytes:
    """Re-read a sealed page's pixels and re-verify their digest before use.

    The upfront boundary check runs once; re-checking at each use catches pixels
    changed on disk mid-run before they enter sealed Designator evidence.
    """
    image_path = page_record["payload"]["image_path"]
    expected = page_record["payload"]["source_sha256"]
    data = context.tree.read_bytes(image_path)
    if digest_bytes(data) != expected:
        raise ContractError(
            f"the sealed page pixel blob at {image_path} no longer matches its recorded "
            "digest; a sealed page's pixels may not change after they are sealed"
        )
    return data


def page_pixels(
    context, page_record: dict, *, grouping_policy: dict
) -> tuple[int, int, list, structure.BackgroundEvidence]:
    """Decode one sealed page and infer its own background, with the evidence.

    Returns the full background evidence, not an integer, so an interior-mode
    page's dark distribution is published rather than dropped (principle 2).

    Decodes with `grayscale_rows`, not `decode_grayscale_png`, because the latter
    accepts only this project's own encoder output and a sealed photograph from
    real ingress must decode too.
    """
    page_bytes = _read_checked_page_bytes(context, page_record)
    width, height, rows = grayscale_rows(page_bytes)
    evidence = structure.infer_background_evidence(
        width,
        height,
        rows,
        background_policy=grouping_config.resolve_background_policy(grouping_policy, width, height),
    )
    return width, height, rows, evidence


def _bounds_of(row: dict) -> dict:
    """The one reader of a fixture row's `x, y, w, h` fields as a `Bounds` dict.

    One reader, so every call site cuts and compares the same projection. Used
    for continuation and recovery rows; declared acts use `act_bounds`.
    """
    return {key: row[key] for key in ("x", "y", "w", "h")}


def _overlap_area(a: dict, b: dict) -> int:
    x0, y0 = max(a["x"], b["x"]), max(a["y"], b["y"])
    x1 = min(a["x"] + a["w"], b["x"] + b["w"])
    y1 = min(a["y"] + a["h"], b["y"] + b["h"])
    return max(0, x1 - x0) * max(0, y1 - y0)


def _uncovered_area(target: dict, covers: list[dict]) -> int:
    """How many pixels of `target` no rectangle in `covers` already contains.

    Shares `_subtract_all` with the fallback tiling, so the two agree on what
    "covered" means; overlapping covers are not double counted, and a rectangle
    two covers contain only jointly still counts as covered.

    `target` must be a validated rectangle of positive area; a degenerate one
    yields a meaningless area rather than zero.
    """
    return sum(piece["w"] * piece["h"] for piece in _subtract_all(target, covers))


def _coverage_on_page(records: list[dict], page_ordinal: int, page_id: str) -> list[dict]:
    """The capture rectangles those region records already cut from one page.

    The padded capture bounds, not `raw_bounds`, so a recrop inside the padding
    does not count as recovery. Scoped by ordinal and page identity, since a
    continuation region lies on another page and an ordinal alone can collide.
    """
    return [
        record["payload"]["transform"]["bounds"]
        for record in records
        if record["payload"]["transform"]["source_page_ordinal"] == page_ordinal
        and record["payload"]["transform"]["source_page_id"] == page_id
    ]


def _body_overlap_area(group: dict, declared_bounds: dict) -> int:
    """Sum of a group's own body members' overlap with `declared_bounds`.

    Breaks a tie between brace-linked groups whose shared anchor makes their
    full bounds identical; only each group's own body can tell them apart.
    """
    body_members = group.get("body_members")
    if not isinstance(body_members, list):
        raise ContractError("a structural group carries no body_members evidence")
    return sum(_overlap_area(member["bounds"], declared_bounds) for member in body_members)


def _match_structural_group(groups: list[dict], declared_bounds: dict, what: str) -> dict:
    """The detected act-group that best overlaps a declared act's bounds.

    Majority overlap, not equality: the synthetic pages' striped ink means a
    detected box never matches the declared rectangle exactly. Less than half
    covered means the detector missed the act, and it is refused.

    A tie is broken by body overlap, never input order, which would silently
    give one act's evidence to its sibling.
    """
    declared_area = declared_bounds["w"] * declared_bounds["h"]
    best_score = (0, 0)
    best_groups = []
    for group in groups:
        overlap = _overlap_area(group["bounds"], declared_bounds)
        body_overlap = _body_overlap_area(group, declared_bounds)
        score = (overlap, body_overlap)
        if score > best_score:
            best_score = score
            best_groups = [group]
        elif score == best_score:
            best_groups.append(group)
    best_overlap, _best_body_overlap = best_score
    if not best_groups or best_overlap * 2 < declared_area:
        raise ContractError(
            f"{what}: structural grouping found no detected region covering at least "
            f"half of the declared bounds {declared_bounds}; the structure pass may "
            "have missed this act entirely"
        )
    if len(best_groups) != 1:
        raise ContractError(
            f"{what}: unresolved structural tie between {len(best_groups)} detected regions "
            f"at overlap score {best_score}; input order is not measured evidence"
        )
    return best_groups[0]


def _claim_structural_group(analysis: dict, group: dict, act_key: str, what: str) -> None:
    """Bind one detected group to one act, and refuse a second claimant.

    Two acts inside one detected group means the pass missed the boundary
    between them; letting both claim it would record detection corroborating
    each act when it found neither (principle 8). Brace-linked pairs are two
    groups sharing an anchor, so each still claims its own.
    """
    claims = analysis.setdefault("group_claims", {})
    # A digest survives copying and still tells brace-linked siblings apart;
    # object identity does neither.
    key = digest_of(group)
    holder = claims.get(key)
    if holder is not None and holder != act_key:
        raise ContractError(
            f"{what}: the detected region {group['bounds']} already corresponds to act "
            f"{holder!r}; the structure pass found one region where two acts are declared, "
            "so it corroborates neither and the boundary between them was not detected"
        )
    claims[key] = act_key


def page_records(context) -> dict[int, dict]:
    """Every page outcome the Exemplar recorded — sealed and refused — by ordinal.

    Read from the Exemplar, not the fixture, so a refused page is never seen as
    ink; refused records are the evidence a hold rests on.
    """
    manifest = context.tree.build_manifest(EXEMPLAR)
    source_rows = _source_rows(context.run)
    records = {}
    entries_by_ordinal = {}
    for entry in manifest["artifacts"]:
        if entry["kind"] != "page":
            continue
        record = context.tree.read_artifact(EXEMPLAR, "page", entry["artifact_id"])
        ordinal = record["payload"].get("ordinal")
        if not _is_int(ordinal):
            raise ContractError("an Exemplar page carries no integer ordinal")
        if ordinal in records:
            raise ContractError(f"the Exemplar carries more than one outcome for ordinal {ordinal}")
        records[ordinal] = {
            "record": record,
            "relative_path": entry["relative_path"],
        }
        entries_by_ordinal[ordinal] = entry
    _verify_exemplar_boundary(context, manifest, source_rows, records, entries_by_ordinal)
    # Manifests are ordered by identity path; process in submission order.
    return {ordinal: records[ordinal] for ordinal in sorted(records)}


def _source_rows(run: dict) -> dict[int, dict]:
    """The submitted denominator, retaining each filename for a useful failure."""
    rows = run.get("source_manifest")
    if not isinstance(rows, list) or not rows:
        raise ContractError("run.json carries no source manifest for the Exemplar boundary")
    sources: dict[int, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ContractError("run.json carries a source-manifest row that is not an object")
        ordinal = row.get("ordinal")
        path = row.get("relative_path")
        if not _is_int(ordinal):
            raise ContractError("run.json carries a source-manifest row without an integer ordinal")
        if ordinal in sources:
            raise ContractError(f"run.json repeats source ordinal {ordinal}")
        if not isinstance(path, str) or not path:
            raise ContractError(f"run.json source ordinal {ordinal} carries no filename")
        sources[ordinal] = row
    return sources


def _verify_exemplar_boundary(context, manifest, sources, records, entries_by_ordinal) -> None:
    """Reconcile the immutable Exemplar census before the Designator reads pixels."""
    verify_exemplar_corpus_seal(
        context.tree,
        context.run,
        manifest,
        sources,
        {ordinal: item["record"] for ordinal, item in records.items()},
        entries_by_ordinal,
    )
    for ordinal, source in sources.items():
        record = records[ordinal]["record"]
        if record["outcome"] == "sealed":
            verify_sealed_page_pixels(context.tree, context.run, source, record)


def sealed_pages(records: dict[int, dict]) -> dict[int, dict]:
    """The sealed subset, by ordinal, each value the page artifact itself."""
    return {
        ordinal: entry["record"]
        for ordinal, entry in records.items()
        if entry["record"]["outcome"] == "sealed"
    }


def _crop_transform(page_ordinal: int, page_id: str, bounds: dict) -> dict:
    """The one construction of a crop transform, as `verify_exemplar_crop_lineage` reads it.

    Region identity derives from this shape and `recovery_pass` predicts identities
    with it, so a second copy would silently disable the duplicate-recrop check.
    """
    return {
        "operation": "crop",
        "source_page_ordinal": page_ordinal,
        "source_page_id": page_id,
        "bounds": bounds,
    }


def _stored_crop(
    context, page_bytes: bytes, page_ordinal: int, page_record: dict, final_bounds: dict
) -> dict:
    """Cut and store one crop, returned as the payload fields that describe it."""
    transform = _crop_transform(page_ordinal, page_record["subject_id"], final_bounds)
    digest, stored = context.tree.put_blob(DESIGNATOR, crop_png(page_bytes, final_bounds))
    return {
        "transform": transform,
        "transform_digest": geometry.transform_digest(transform),
        "image_path": stored.relative_path,
        "image_sha256": digest,
    }


def cut_region(
    context,
    act,
    page_record,
    bounds,
    ordinal,
    page_ordinal,
    origin,
    recovery_request: dict[str, str] | None = None,
    *,
    padding: dict | None = None,
    provenance: dict | None = None,
):
    """Cut one region of one *fixture-declared* act, by that act's own identity."""
    return cut_minted_region(
        context,
        act_identity(context.fixture, act),
        act["key"],
        page_record,
        bounds,
        ordinal,
        page_ordinal,
        origin,
        recovery_request,
        padding=padding,
        provenance=provenance,
    )


def cut_minted_region(
    context,
    act_id,
    act_key,
    page_record,
    bounds,
    ordinal,
    page_ordinal,
    origin,
    recovery_request: dict[str, str] | None = None,
    *,
    padding: dict | None = None,
    provenance: dict | None = None,
):
    """Cut one region of one act and publish it.

    Declared and minted acts are cut by this one function, so every crop,
    fallback included, has one author.

    `origin` is `proposal` (the original marking-out, continuations included) or
    `recovery` (a later recrop); witnesses read only proposal regions.

    `bounds` is the structural rectangle act identity binds, never the padded one.
    `padding`, given only for a proposal cut, expands it into the capture
    rectangle; a recovery request already names its exact rectangle.

    `transform` keeps only the four fields `verify_exemplar_crop_lineage` reads
    as a closed schema; `raw_bounds` and `padding` sit beside it as provenance.
    """
    if provenance is None:
        provenance = structure_provenance(context)
    image_path = page_record["payload"]["image_path"]
    page_bytes = _read_checked_page_bytes(context, page_record)

    page_w, page_h = dimensions(page_bytes)
    if padding is not None:
        padded = geometry.apply_padding(bounds, page_w, page_h, padding)
        final_bounds = padded["bounds"]
        padding_record = {
            "applied_px": padded["applied_px"],
            "configured_bp": padded["configured_bp"],
            "config_sha256": padding["config_sha256"],
            # Whether this padding was calibrated for this corpus, carried on
            # the evidence itself.
            "provenance": padding["provenance"],
        }
    else:
        # A recovery rectangle skips `apply_padding`, which is what validates
        # bounds; validate here so a bad one is a refusal, not a bare ValueError.
        geometry.validate_bounds(bounds, page_w, page_h, "recovery bounds")
        final_bounds = bounds
        padding_record = None

    crop = _stored_crop(context, page_bytes, page_ordinal, page_record, final_bounds)
    return context.publish(
        kind="region",
        subject_id=act_id,
        outcome="proposed",
        attempt=attempt_id(act_id, "crop", ordinal),
        inputs=[context.input_ref(image_path)] + ([recovery_request] if recovery_request else []),
        payload={
            "region_id": region_id(act_id, crop["transform"]),
            "act_key": act_key,
            "attempt_ordinal": ordinal,
            "origin": origin,
            **crop,
            "raw_bounds": bounds,
            "padding": padding_record,
            "provenance": provenance,
        },
    )


def hold_act(
    context, act, act_id: str, blocking_ordinal: int, records, reason: str, reason_code: str
):
    """Publish the artifact that says why this act could not be marked out.

    A skipped act would leave the proposal seal short and conservation would
    reconcile against its absence. The hold cites the Exemplar's page outcome as
    evidence. `reason_code` is separate from `blocking_ordinal` because the
    blocking page is not always unsealed: a structure-held page is sealed ink.
    """
    entry = records.get(blocking_ordinal)
    if entry is None:
        raise ContractError(
            f"act {act['key']} needs page {blocking_ordinal}, and the Exemplar "
            "recorded no outcome for it at all — a page in neither the sealed nor "
            "the refused set is invariant #10's imbalance, not a page to skip"
        )
    if reason_code not in HOLD_REASON_CODES:
        raise ContractError(
            f"act {act['key']} is held for {reason_code!r}, which is not one of the "
            f"declared hold reasons {sorted(HOLD_REASON_CODES)}"
        )
    return context.publish(
        kind="hold",
        subject_id=act_id,
        outcome="held",
        inputs=[context.input_ref(entry["relative_path"])],
        payload={
            "act_key": act["key"],
            "blocking_page_ordinal": blocking_ordinal,
            "reason_code": reason_code,
            "reason": reason,
        },
    )


def structure_failures(context, pages: dict[int, dict]) -> dict[int, str]:
    """The sealed pages this run's structure pass could not mark out, by ordinal.

    A failed page is held visibly and recoverably, never skipped. On the fixture
    path the failure is declared; everything downstream of it is real. A failure
    on an unsealed page is ignored, since its Exemplar refusal already counts it.
    """
    failures: dict[int, str] = {}
    for row in context.fixture.get("structure_failure", []):
        if not isinstance(row, dict) or set(row) != {"scenario", "page_ordinal", "reason_code"}:
            raise ContractError(
                "a declared structure failure has fields outside its closed contract"
            )
        if row["scenario"] != context.scenario:
            continue
        ordinal, reason_code = row["page_ordinal"], row["reason_code"]
        if not _is_int(ordinal):
            raise ContractError("a declared structure failure names no integer page ordinal")
        if not isinstance(reason_code, str) or not reason_code:
            raise ContractError("a declared structure failure names no reason code")
        if ordinal in failures:
            raise ContractError(
                f"the fixture declares more than one structure failure for page {ordinal}; "
                "this stage may not choose one of them by order"
            )
        if ordinal in pages:
            failures[ordinal] = reason_code
    return failures


def publish_structure_status(
    context,
    records,
    pages,
    provenance,
    failures,
    analyses,
    *,
    answers: dict[int, tuple[str | None, dict[str, str]]] | None = None,
    provenance_by_page: dict[int, dict] | None = None,
) -> dict:
    """One visible per-page outcome for the structure pass: scanned or held.

    Published for every sealed page, so a successful scan is a record, not an
    absence a reader must infer from crops (principle 2). `state` says "scanned",
    not "marked out": a scanned page may carry no act.

    It records how the page was read (`background_source`, `structure_evidence`)
    and the geometry actually executed (`page_width`, `page_height`,
    `resolved_thresholds`), since pixel thresholds depend on each page's size and
    re-deriving them later can silently drift (principle 6). All are null on a
    page held before analysis. `max_residual_components` is omitted because this
    producer does not use it.

    Live path only: `answers` makes `structure_evidence` the chair's answer and
    adds `structure_answer_ref`; `provenance_by_page` names the serving session
    that answered each page of a resumed pass.

    Returns each page's status reference, which a page-fallback act must cite.
    """
    published: dict[int, dict[str, str]] = {}
    for ordinal in sorted(pages):
        reason_code = failures.get(ordinal)
        analysis = analyses.get(ordinal)
        answer = answers.get(ordinal) if answers is not None else None
        live_fields: dict[str, object] = {}
        if answer is not None:
            evidence, answer_ref = answer
            live_fields = {"structure_answer_ref": answer_ref}
        else:
            evidence = analysis["structure_evidence"] if analysis else None
        result = context.publish(
            kind="structure-status",
            # The sealed page's own subject; a real page has no fixture identity.
            subject_id=pages[ordinal]["subject_id"],
            outcome="held" if reason_code else "proposed",
            inputs=[context.input_ref(records[ordinal]["relative_path"])],
            payload={
                "page_id": pages[ordinal]["subject_id"],
                "page_ordinal": ordinal,
                "state": "held" if reason_code else "scanned",
                "reason_code": reason_code,
                "background_source": analysis["background_source"] if analysis else None,
                # How this page's ink was thresholded. `ink_margin` is derived
                # per page, so it cannot be recovered from the policy alone, and
                # `dark_mode` is published so the margin can be rechecked. Null
                # where no scan ran.
                "ink_margin": analysis["ink_margin"] if analysis else None,
                "dark_mode": analysis["dark_mode"] if analysis else None,
                "ink_threshold": (
                    None
                    if analysis is None or analysis["ink_margin"] is None
                    else analysis["background"] - analysis["ink_margin"]
                ),
                "structure_evidence": evidence,
                # Null on a page held before analysis: numbers the pass would
                # have used are not numbers it ran under.
                "page_width": analysis["width"] if analysis else None,
                "page_height": analysis["height"] if analysis else None,
                "resolved_thresholds": (
                    {
                        name: value
                        for name, value in dataclasses.asdict(analysis["thresholds"]).items()
                        if name != "max_residual_components"
                    }
                    if analysis
                    else None
                ),
                "provenance": (
                    provenance
                    if provenance_by_page is None
                    else provenance_by_page.get(ordinal, provenance)
                ),
                **live_fields,
            },
        )
        published[ordinal] = context.input_ref(result.relative_path)
    return published


_BACKGROUND_NOT_INFERABLE = {
    "background": None,
    "source": "not-inferable",
    "dark_distribution": None,
    "ink_margin": None,
    "dark_mode": None,
}


def _page_bounds(analysis: dict) -> dict:
    return {"x": 0, "y": 0, "w": analysis["width"], "h": analysis["height"]}


def _fallback_grid(width: int, height: int, thresholds) -> list[dict]:
    return grouping.fallback_tiles(
        width,
        height,
        bands=thresholds.fallback_bands,
        overlap_px=thresholds.fallback_overlap_px,
    )


def _analyze_page(
    cache: dict, context, ordinal: int, page_record: dict, grouping_policy: dict
) -> dict:
    """Structure-pass and grouping results for one sealed page, computed once.

    A page whose background cannot be inferred is still cut into the fallback
    grid, since every page must be read, but its ink is not measured:
    `background` stays None rather than a guessed stand-in.

    Every geometric threshold is resolved here from the sealed policy and this
    page's own size, and cached with the pixels so every later consumer runs
    under the same numbers.
    """
    if ordinal not in cache:
        try:
            width, height, rows, evidence = page_pixels(
                context, page_record, grouping_policy=grouping_policy
            )
        except structure.BackgroundInferenceRefusal:
            width, height, rows = grayscale_rows(_read_checked_page_bytes(context, page_record))
            evidence = _BACKGROUND_NOT_INFERABLE
        background = evidence["background"]
        # Carried, not recomputed, so the scan and the record use one value.
        ink_margin = evidence["ink_margin"]
        thresholds = grouping_config.resolve_thresholds(grouping_policy, width, height)
        components = (
            []
            if background is None
            else structure.primary_scan(
                width,
                height,
                rows,
                background=background,
                margin=ink_margin,
                gap_tolerance_px=thresholds.gap_tolerance_px,
            )
        )
        # Repeats `group_page`'s pure partition so the withheld components can
        # be published; `group_page` returns only the groups.
        _grouped, page_spanning = grouping.partition_page_spanning(
            components,
            width,
            height,
            page_spanning_area_bp=thresholds.page_spanning_area_bp,
        )
        groups = grouping.group_page(
            components,
            width,
            height,
            margin_px=thresholds.margin_px,
            chain_gap_px=thresholds.chain_gap_px,
            anchor_reach_px=thresholds.anchor_reach_px,
            brace_min_height_px=thresholds.brace_min_height_px,
            page_spanning_area_bp=thresholds.page_spanning_area_bp,
        )
        # A page with no eligible group is cut into overlapping fallback tiles
        # anyway: one threshold here is too weak to call a page blank, so the
        # witnesses and the Perlector decide. `structure_evidence` marks the
        # tiles as a grid, not a detection.
        structure_evidence = "detected"
        if not groups:
            structure_evidence = "fallback-tiles"
            groups = _fallback_grid(width, height, thresholds)
        cache[ordinal] = {
            "width": width,
            "height": height,
            "rows": rows,
            "background": background,
            "background_source": evidence["source"],
            # None where the interior-mode branch did not run.
            "dark_distribution": evidence["dark_distribution"],
            # None when the background could not be inferred.
            "ink_margin": ink_margin,
            "dark_mode": evidence["dark_mode"],
            "groups": groups,
            "page_spanning": page_spanning,
            "structure_evidence": structure_evidence,
            "thresholds": thresholds,
            # Raw components for the live ink tripwire, which tests a chair's
            # rectangle against counted ink rather than grouped bands.
            "components": components,
        }
    return cache[ordinal]


def _structural_evidence_block(
    analysis: dict, declared_bounds: dict, act_key: str, what: str
) -> dict:
    """The four fields that say what structural evidence stands behind one rectangle.

    One builder for the primary and continuation blocks so both read alike. A
    fallback-tiled page has nothing to match against and says so.
    """
    if analysis["structure_evidence"] == "fallback-tiles":
        return {
            "structure_evidence": "fallback-tiles",
            "detected_bounds": None,
            "body_member_count": 0,
            "anchor_count": 0,
            "rationale": _fixture_fallback_explanation(analysis)[1],
        }
    group = _match_structural_group(analysis["groups"], declared_bounds, what)
    _claim_structural_group(analysis, group, act_key, what)
    return {
        "structure_evidence": "detected",
        "detected_bounds": group["bounds"],
        "body_member_count": len(group["body_members"]),
        "anchor_count": len(group["anchors"]),
        "rationale": group["rationale"],
    }


def _publish_act_group(
    context,
    act: dict,
    act_id: str,
    page_record: dict,
    analysis: dict,
    continuation: dict | None,
    continuation_page_record: dict | None,
    continuation_analysis: dict | None,
):
    """Record how geometry and structural cues grouped this act — no text.

    This is evidence for the act's declared identity, never its source, so a
    grouping disagreement is a refusal, not a substitution. A fallback-tiled page
    is never matched against its grid, which covers everything and would
    corroborate any act.
    """
    inputs = [context.input_ref(page_record["payload"]["image_path"])]
    payload: dict = {
        "act_key": act["key"],
        "declared_bounds": act_bounds(act),
        "continuation": None,
        **_structural_evidence_block(analysis, act_bounds(act), act["key"], f"act {act['key']}"),
    }
    if continuation is not None:
        continuation_bounds = _bounds_of(continuation)
        # Recorded, not gated: the synthetic continuations do not touch page
        # edges, so this is usually False even for a genuine continuation.
        # Never run over fallback tiles, which touch both edges by construction.
        corroborated = (
            analysis["structure_evidence"] == "detected"
            and continuation_analysis["structure_evidence"] == "detected"
            and grouping.find_continuation_candidate(
                analysis["groups"],
                analysis["height"],
                continuation_analysis["groups"],
                # Each page's own edge reach; two pages need not share a height.
                edge_reach_a_px=analysis["thresholds"].page_edge_reach_px,
                edge_reach_b_px=continuation_analysis["thresholds"].page_edge_reach_px,
            )
            != []
        )
        payload["continuation"] = {
            "declared_bounds": continuation_bounds,
            "geometric_corroboration": corroborated,
            **_structural_evidence_block(
                continuation_analysis,
                continuation_bounds,
                act["key"],
                f"act {act['key']} continuation",
            ),
        }
        inputs.append(context.input_ref(continuation_page_record["payload"]["image_path"]))
    _validate_act_group_payload(payload)
    return context.publish(
        kind="act-group", subject_id=act_id, outcome="proposed", inputs=inputs, payload=payload
    )


def _acts_at_edge(
    acts: list[dict], group_bounds: dict, *, reach: int, bottom_of: int | None
) -> list[dict]:
    """The acts over a page-edge group whose own edge lies within the page's edge
    reach, or the nearest to the edge when none does; over-holding is safe."""
    over = [act for act in acts if _overlap_area(act["bounds"], group_bounds)]
    if bottom_of is None:
        gaps = [act["bounds"]["y"] for act in over]
    else:
        gaps = [bottom_of - act["bounds"]["y"] - act["bounds"]["h"] for act in over]
    limit = max(reach, min(gaps, default=0))
    return sorted(
        (act for act, gap in zip(over, gaps, strict=True) if gap <= limit),
        key=lambda act: act["act_key"],
    )


def _publish_continuation_candidates(
    context,
    pages: dict[int, dict],
    page_cache: dict[int, dict],
    status_refs: dict[int, dict[str, str]],
    acts_by_page: dict[int, list[dict]],
    grouping_policy: dict,
) -> None:
    """Name every crossing of an adjacent page break that the geometry shows.

    `acts_by_page` holds every page marked out by detection, with its proposed
    acts (possibly none); `linked` marks an act whose continuation the fixture
    declares, which is dropped from the head side as already linked. The record is not authoritative: it enters no act and no seal, and
    the Recensor holds every act it names, since a head alone is truncated and
    a tail alone has no heading. Whether they are one act stays unmade here. A
    side with no proposed act over its group is published empty, never
    dropped: that ink is also unclaimed, and conservation holds it.
    """
    for ordinal_a in sorted(acts_by_page):
        ordinal_b = ordinal_a + 1
        if ordinal_b not in acts_by_page:
            continue
        analysis_a, analysis_b = page_cache[ordinal_a], page_cache[ordinal_b]
        # Fallback tiles touch both edges by construction and would pair any two pages.
        if "fallback-tiles" in (analysis_a["structure_evidence"], analysis_b["structure_evidence"]):
            continue
        edge_reach_a = analysis_a["thresholds"].page_edge_reach_px
        edge_reach_b = analysis_b["thresholds"].page_edge_reach_px
        pairs = grouping.find_continuation_candidate(
            analysis_a["groups"],
            analysis_a["height"],
            analysis_b["groups"],
            edge_reach_a_px=edge_reach_a,
            edge_reach_b_px=edge_reach_b,
        )
        for index, pair in enumerate(pairs):
            group_a, group_b = pair["page_a_group"]["bounds"], pair["page_b_group"]["bounds"]
            at_edge = _acts_at_edge(
                acts_by_page[ordinal_a],
                group_a,
                reach=edge_reach_a,
                bottom_of=analysis_a["height"],
            )
            # A declared continuation runs forward, so it links only the head side.
            acts_a = [act for act in at_edge if not act["linked"]]
            if at_edge and not acts_a:
                continue
            acts_b = _acts_at_edge(
                acts_by_page[ordinal_b], group_b, reach=edge_reach_b, bottom_of=None
            )
            payload = {
                "authoritative": False,
                "page_a": {"page_id": pages[ordinal_a]["subject_id"], "page_ordinal": ordinal_a},
                "page_b": {"page_id": pages[ordinal_b]["subject_id"], "page_ordinal": ordinal_b},
                "acts_a": [{"act_id": a["act_id"], "act_key": a["act_key"]} for a in acts_a],
                "acts_b": [{"act_id": a["act_id"], "act_key": a["act_key"]} for a in acts_b],
                "group_a_bounds": group_a,
                "group_b_bounds": group_b,
                "edge_reach_a_px": edge_reach_a,
                "edge_reach_b_px": edge_reach_b,
                "grouping_config_sha256": grouping_policy["config_sha256"],
            }
            _refuse_text_fields(payload, kind="continuation-candidate")
            context.publish(
                kind="continuation-candidate",
                subject_id=f"{pages[ordinal_a]['subject_id']}:page-break:{index}",
                outcome="proposed",
                inputs=[
                    status_refs[ordinal_a],
                    status_refs[ordinal_b],
                    *(
                        context.artifact_ref(
                            DESIGNATOR,
                            "act-group",
                            artifact_id(DESIGNATOR, "act-group", act["act_id"]),
                        )
                        for act in acts_a + acts_b
                    ),
                ],
                payload=payload,
            )


def _claimed_regions_by_page(context) -> dict[int, list[dict]]:
    """Every proposal region's final (capture) bounds cut so far, by page ordinal.

    One pass over the artifacts; a pass per page would be quadratic.
    """
    claimed: dict[int, list[dict]] = {}
    for record in _regions_of(context):
        payload = record["payload"]
        if payload.get("origin") != "proposal":
            continue
        claimed.setdefault(payload["transform"]["source_page_ordinal"], []).append(
            {"act_id": record["subject_id"], "bounds": payload["transform"]["bounds"]}
        )
    return claimed


def _contains(outer: dict, inner: dict) -> bool:
    return (
        outer["x"] <= inner["x"]
        and outer["y"] <= inner["y"]
        and outer["x"] + outer["w"] >= inner["x"] + inner["w"]
        and outer["y"] + outer["h"] >= inner["y"] + inner["h"]
    )


def _secondary_rescue_candidates(claimed: list[dict], candidates: list[dict]) -> list[dict]:
    """Every secondary-scan candidate that genuinely adds coverage, none that refine one.

    A candidate inside one claim adds nothing; one merely touching a claim does.
    Each is returned with the number of claimed acts it touches, since one
    spanning two acts is the shape a reviewer must see (a merged boundary).

    That count is recorded, never acted on: padded claims can abut, so a single
    pen mark can touch two acts, and the secondary proposer adds recall, never
    verdicts.
    """
    rescues = []
    for candidate in candidates:
        if any(_contains(entry["bounds"], candidate["bounds"]) for entry in claimed):
            continue
        rescues.append(
            {
                "candidate": candidate,
                "overlapping_claimed_act_count": len(
                    {
                        entry["act_id"]
                        for entry in claimed
                        if _overlap_area(entry["bounds"], candidate["bounds"]) > 0
                    }
                ),
            }
        )
    return rescues


def _publish_secondary_proposals(
    context,
    ordinal: int,
    page_record: dict,
    analysis: dict,
    claimed: list[dict],
    secondary: dict,
    grouping_policy: dict,
) -> bool:
    """Cut and hold every non-authoritative rescue candidate for review.

    Separate from the conservation publisher so it can be tested on a hand-fed
    analysis.

    Bounded per page: a speckled page would otherwise mint thousands of crops
    and make the run unopenable. Past `max_secondary_proposals` the pass becomes
    one held record with the count and bound, and nothing is cut. Candidates are
    counted, never filtered (principle 8). `secondary_enumeration` tells "no
    candidate" apart from "counted, not cut".
    """
    if secondary["chair_state"] != "configured":
        return False
    validate_serving_provenance(
        context,
        secondary,
        producer_stage=DESIGNATOR,
        require_receipt=True,
    )
    if analysis["background"] is None:
        # The secondary scan needs a background; a substituted one would crop
        # paper.
        return False
    candidates = structure.secondary_scan(
        analysis["width"],
        analysis["height"],
        analysis["rows"],
        background=analysis["background"],
        gap_tolerance_px=analysis["thresholds"].gap_tolerance_px,
    )
    rescues = _secondary_rescue_candidates(claimed, candidates)
    image_path = page_record["payload"]["image_path"]
    max_secondary_proposals = analysis["thresholds"].max_secondary_proposals
    if len(rescues) > max_secondary_proposals:
        return _publish_withheld_secondary_pass(
            context,
            ordinal,
            page_record,
            analysis,
            secondary,
            candidate_count=len(rescues),
            max_secondary_proposals=max_secondary_proposals,
            grouping_config_sha256=grouping_policy["config_sha256"],
        )
    page_bytes = _read_checked_page_bytes(context, page_record)
    for index, rescue_row in enumerate(rescues):
        candidate = rescue_row["candidate"]
        # In-page by construction today; a detector chair would not be, and a
        # bad box must refuse rather than raise a bare ValueError in `crop_png`.
        geometry.validate_bounds(
            candidate["bounds"], analysis["width"], analysis["height"], "secondary candidate bounds"
        )
        overlap_count = rescue_row["overlapping_claimed_act_count"]
        subject = f"{page_record['subject_id']}-secondary-{index}"
        crop = _stored_crop(context, page_bytes, ordinal, page_record, candidate["bounds"])
        rescue_payload = {
            "page_ordinal": ordinal,
            "pixel_count": candidate["pixel_count"],
            "origin": "secondary-proposer",
            "padding": None,
            "authoritative": False,
            "authority_effect": "review-only",
            "overlapping_claimed_act_count": overlap_count,
            **crop,
            "provenance": secondary,
        }
        _refuse_text_fields(rescue_payload)
        rescue = context.publish(
            kind="rescue-crop",
            subject_id=subject,
            outcome="held",
            inputs=[context.input_ref(image_path)],
            payload=rescue_payload,
        )
        proposal_payload = {
            "page_ordinal": ordinal,
            "bounds": candidate["bounds"],
            "pixel_count": candidate["pixel_count"],
            "authoritative": False,
            "terminal_disposition": "held-for-review",
            "secondary_enumeration": SECONDARY_ENUMERATION_COMPLETE,
            "overlapping_claimed_act_count": overlap_count,
            "rescue_ref": context.input_ref(rescue.relative_path),
            "provenance": secondary,
        }
        _refuse_text_fields(proposal_payload)
        context.publish(
            kind="secondary-proposal",
            subject_id=subject,
            outcome="held",
            inputs=[context.input_ref(image_path), context.input_ref(rescue.relative_path)],
            payload=proposal_payload,
        )
    return bool(rescues)


def _publish_withheld_secondary_pass(
    context,
    ordinal: int,
    page_record: dict,
    analysis: dict,
    secondary: dict,
    *,
    candidate_count: int,
    max_secondary_proposals: int,
    grouping_config_sha256: str,
) -> bool:
    """One held record for a page whose secondary pass found more than the bound.

    No crop is cut, but the count stays on the record. Like the rescues it
    replaces, it mints no act and enters no seal; it does hold the run.
    """
    payload = {
        "page_ordinal": ordinal,
        "page_bounds": _page_bounds(analysis),
        "authoritative": False,
        "terminal_disposition": "held-for-review",
        "secondary_enumeration": SECONDARY_ENUMERATION_WITHHELD,
        "secondary_candidate_count": candidate_count,
        "max_secondary_proposals": max_secondary_proposals,
        "grouping_config_sha256": grouping_config_sha256,
        "reason": (
            f"this page's secondary pass found {candidate_count} rescue candidates no crop "
            f"claims, more than the sealed bound of {max_secondary_proposals} this run may cut "
            "and hold separately on one page, so the pass is held as a single review item and "
            "no rescue crop was cut; nothing was filtered out of the scan, and the candidates "
            "remain recomputable from the sealed page bytes under the sealed policy"
        ),
        "provenance": secondary,
    }
    _refuse_text_fields(payload)
    context.publish(
        kind="secondary-proposal",
        subject_id=f"{page_record['subject_id']}-secondary-withheld",
        outcome="held",
        inputs=[context.input_ref(page_record["payload"]["image_path"])],
        payload=payload,
    )
    return True


def _seal_row(
    act_id: str,
    act_key: str,
    page_id: str,
    page_ordinal: int,
    outcome: str,
    evidence: list[dict],
    *,
    has_continuation: bool = False,
) -> dict:
    """One `expected_acts` entry; `has_continuation` comes from regions actually cut."""
    return {
        "act_id": act_id,
        "act_key": act_key,
        "page_id": page_id,
        "page_ordinal": page_ordinal,
        "has_continuation": has_continuation,
        "outcome": outcome,
        "evidence": evidence,
    }


def _by_path(references: list[dict]) -> list[dict]:
    return sorted(references, key=lambda reference: reference["relative_path"])


def residual_act_key(page_ordinal: int, index: int) -> str:
    """The human-readable label for a conservation-residual act.

    A label only; the ``residual`` act class, not this string, keeps residual
    identities from colliding with proposals.
    """
    return f"residual:{page_ordinal}:{index}"


def hold_residual_act(
    context,
    page_id: str,
    page_ordinal: int,
    index: int,
    bounds: dict,
    pixel_count: int,
    conservation_ref: dict[str, str],
):
    """Mint and hold the one act a conservation residual becomes.

    No region claimed this ink, so it was never witnessed; it is held from the
    start. The ``residual`` class is part of the identity binding, so it cannot
    collide with a proposal over the same rectangle. The hold carries the bounds
    so `_verify_minted_act_rows` can recompute that identity.
    """
    minted_act_id = derive_minted_act_id(page_id, "residual", bounds)
    hold = context.publish(
        kind="hold",
        subject_id=minted_act_id,
        outcome="held",
        inputs=[conservation_ref],
        payload={
            "act_key": residual_act_key(page_ordinal, index),
            "page_ordinal": page_ordinal,
            "residual_bounds": bounds,
            "residual_pixel_count": pixel_count,
            "reason": (
                "structural grouping claimed no region covering this ink; the residual "
                "is held for review, never witnessed and never read, because no "
                "structural proposal exists for it"
            ),
        },
    )
    return minted_act_id, hold


def _publish_residual_holds(
    context,
    page_id: str,
    page_ordinal: int,
    residual_components: list[dict],
    conservation_ref: dict[str, str],
) -> list[dict]:
    """Mint one held act per conservation residual.

    Every residual is minted; `review_priority` only orders review. `index` is a
    list position and stays out of identity, so two residuals on one page differ
    only by rectangle. Two components sharing a bounding box would mint one act
    over two pieces of ink, losing an act, so that is refused.
    """
    seen: dict[tuple[int, int, int, int], int] = {}
    for index, component in enumerate(residual_components):
        bounds = component["bounds"]
        key = tuple(bounds[name] for name in ("x", "y", "w", "h"))
        prior = seen.get(key)
        if prior is not None:
            raise ContractError(
                f"conservation residuals {prior} and {index} on page {page_ordinal} share the "
                f"bounding box {bounds}; the residual act class has no ordinal namespace, so "
                "minting both would account for two pieces of unclaimed ink as one act"
            )
        seen[key] = index
    rows = []
    for index, component in enumerate(residual_components):
        minted_act_id, hold = hold_residual_act(
            context,
            page_id,
            page_ordinal,
            index,
            component["bounds"],
            component["pixel_count"],
            conservation_ref,
        )
        rows.append(
            _seal_row(
                minted_act_id,
                residual_act_key(page_ordinal, index),
                page_id,
                page_ordinal,
                "held",
                [context.input_ref(hold.relative_path)],
            )
        )
    return rows


def _partition_residual_components(
    components: list[dict], thresholds: grouping_config.GroupingThresholds
) -> tuple[list[dict], list[dict]]:
    """Separate individually held ink from explicitly aggregated dust.

    The aggregate is accounting, not a claim that its components form one act;
    the sealed floors decide only whether a component gets its own act.
    """
    promoted, aggregated = [], []
    for component in components:
        bounds = component["bounds"]
        area = bounds["w"] * bounds["h"]
        if (
            component["pixel_count"] >= thresholds.residual_aggregate_max_pixel_count
            or area >= thresholds.residual_aggregate_max_area_px
        ):
            promoted.append(component)
        else:
            aggregated.append(component)
    return promoted, aggregated


def _publish_page_residual_hold(
    context,
    page_id: str,
    page_ordinal: int,
    page_bounds: dict,
    *,
    residual_component_count: int,
    aggregated_component_count: int,
    grouping_config_sha256: str,
    conservation_ref: dict[str, str],
) -> dict:
    """Hold the below-threshold residual partition as one page review item.

    Every component stays on the conservation record; the hold changes only how
    many review items there are. Its one input is that conservation record, the
    premise `_verify_page_residual_act_row` recomputes every field against.
    """
    minted_act_id = derive_minted_act_id(page_id, "page-residual", page_bounds)
    act_key = page_residual_act_key(page_ordinal)
    payload = {
        "act_key": act_key,
        "page_id": page_id,
        "page_ordinal": page_ordinal,
        "page_bounds": page_bounds,
        "residual_component_count": residual_component_count,
        "aggregated_component_count": aggregated_component_count,
        "grouping_config_sha256": grouping_config_sha256,
        "blocking_page_ordinal": page_ordinal,
        "reason_code": PAGE_RESIDUAL_AGGREGATE_REASON_CODE,
        "reason": (
            f"this page's conservation reconciled {residual_component_count} residual "
            f"components against the sealed policy, including {aggregated_component_count} "
            "components retained as page-level aggregate accounting rather than fictitious "
            "acts; every aggregate component's bounds and pixels are retained on the linked "
            "conservation record"
        ),
    }
    _refuse_text_fields(payload)
    hold = context.publish(
        kind="hold",
        subject_id=minted_act_id,
        outcome="held",
        inputs=[conservation_ref],
        payload=payload,
    )
    return _seal_row(
        minted_act_id,
        act_key,
        page_id,
        page_ordinal,
        "held",
        [context.input_ref(hold.relative_path)],
    )


def _residual_ink_fraction_bp(residual_pixel_count: int, total_ink_pixel_count: int) -> int:
    """How much of this page's ink no crop claimed, in integer basis points.

    Recorded for calibration, gating nothing: the bound is on cardinality,
    because one large smudge is fine and thousands of specks are not, which a
    fraction gate gets backwards. Measured against the page's ink so it is
    recomputable from the record, in integer round-half-up arithmetic because a
    float in a canonical payload breaks determinism.
    """
    if total_ink_pixel_count <= 0:
        return 0
    return (2 * residual_pixel_count * 10_000 + total_ink_pixel_count) // (
        2 * total_ink_pixel_count
    )


def _subtract_rectangle(bounds: dict, claimed: dict) -> list[dict]:
    """Non-overlapping rectangles covering ``bounds`` minus one claimed box."""
    x0, y0 = bounds["x"], bounds["y"]
    x1, y1 = x0 + bounds["w"], y0 + bounds["h"]
    cx0, cy0 = max(x0, claimed["x"]), max(y0, claimed["y"])
    cx1 = min(x1, claimed["x"] + claimed["w"])
    cy1 = min(y1, claimed["y"] + claimed["h"])
    if cx0 >= cx1 or cy0 >= cy1:
        return [dict(bounds)]
    pieces = []
    if y0 < cy0:
        pieces.append({"x": x0, "y": y0, "w": x1 - x0, "h": cy0 - y0})
    if cy1 < y1:
        pieces.append({"x": x0, "y": cy1, "w": x1 - x0, "h": y1 - cy1})
    if x0 < cx0:
        pieces.append({"x": x0, "y": cy0, "w": cx0 - x0, "h": cy1 - cy0})
    if cx1 < x1:
        pieces.append({"x": cx1, "y": cy0, "w": x1 - cx1, "h": cy1 - cy0})
    return pieces


def _subtract_all(bounds: dict, covers: list[dict]) -> list[dict]:
    """Disjoint rectangles covering ``bounds`` minus every cover."""
    pieces = [dict(bounds)]
    for cover in covers:
        pieces = [remainder for piece in pieces for remainder in _subtract_rectangle(piece, cover)]
    return pieces


def _unclaimed_fallback_tiles(tiles: list[dict], claimed: list[dict]) -> list[dict]:
    """Clip fallback bands to pixels no declared proposal region already owns."""
    unclaimed = []
    for tile in tiles:
        pieces = _subtract_all(tile["bounds"], [claim["bounds"] for claim in claimed])
        unclaimed.extend(
            {
                "bounds": piece,
                "rationale": tile["rationale"]
                + "; excludes any pixels already assigned to a declared act",
            }
            for piece in pieces
        )
    return sorted(
        unclaimed,
        key=lambda tile: (
            tile["bounds"]["y"],
            tile["bounds"]["x"],
            tile["bounds"]["h"],
            tile["bounds"]["w"],
        ),
    )


_FALLBACK_REASON_LIVE = (
    "the structure chair returned no act for this page, so the page is cut into "
    "predetermined overlapping crops and sent downstream to be read rather than being "
    "called blank here; this page's own ink scan is recorded separately on its "
    "conservation record"
)


def _publish_page_fallback(
    context,
    ordinal: int,
    page_record: dict,
    analysis: dict,
    status_ref: dict[str, str],
    claimed: list[dict],
    provenance: dict,
    *,
    reason: str | None = None,
) -> dict | None:
    """Cut predetermined crops over a page with no eligible structural group.

    A page with no found ink is still sent downstream whole, so the witnesses
    and the Perlector decide whether it is blank.

    One minted act per page with one region per tile: nothing established how
    many acts the page holds, so counting tiles would invent an act count.
    Declared crops are subtracted first, so no pixel is read under two acts;
    if they already cover the page, nothing is minted.

    The act is `proposed`, not `held`, because held acts are never read. Its
    identity binds the ``page-fallback`` class and the page rectangle, which
    `_verify_page_fallback_act_row` recomputes with the page's `structure-status`.
    Tiles carry no padding: each already is the final rectangle, overlap included.
    """
    page_id = page_record["subject_id"]
    page_bounds = _page_bounds(analysis)
    act_id = derive_minted_act_id(page_id, "page-fallback", page_bounds)
    act_key = fallback_page_act_key(ordinal)
    # Exclude this act's own tiles, or a resumed pass would subtract them from
    # themselves, mint nothing and seal one act short (principle 2).
    claimed = [claim for claim in claimed if claim["act_id"] != act_id]
    tiles = _unclaimed_fallback_tiles(analysis["groups"], claimed)
    if not tiles:
        return None
    if reason is None:
        reason = _fixture_fallback_explanation(analysis)[0]
    fallback_payload = {
        "act_key": act_key,
        "page_id": page_id,
        "page_ordinal": ordinal,
        "page_bounds": page_bounds,
        "tile_count": len(tiles),
        "tiles": [
            {"bounds": dict(tile["bounds"]), "rationale": tile["rationale"]} for tile in tiles
        ],
        "reason": reason,
        "provenance": provenance,
    }
    _refuse_text_fields(fallback_payload)
    context.publish(
        kind="page-fallback",
        subject_id=act_id,
        outcome="proposed",
        inputs=[status_ref],
        payload=fallback_payload,
    )

    evidence = []
    for index, tile in enumerate(tiles):
        region = cut_minted_region(
            context,
            act_id,
            act_key,
            page_record,
            dict(tile["bounds"]),
            index + 1,
            ordinal,
            "proposal",
            provenance=provenance,
        )
        evidence.append(context.input_ref(region.relative_path))
    return _seal_row(act_id, act_key, page_id, ordinal, "proposed", _by_path(evidence))


def _publish_conservation_and_secondary(
    context,
    ordinal: int,
    page_record: dict,
    analysis: dict,
    claimed: list[dict],
    secondary: dict,
    grouping_policy: dict,
) -> tuple[list[dict], bool]:
    """Independent ink-vs-crop reconciliation, plus non-authoritative rescue crops.

    Conservation rescans the page's own pixels rather than trusting grouping, so
    it can prove no ink was missed entirely. Residuals are returned as seal rows,
    so the proposal seal accounts for them.

    A page with no inferable background reconciles nothing and says so
    (`ink_measurable`); a substituted threshold would count dark paper as ink.
    The record is published either way.

    Every component is measured before presentation: those at either sealed floor
    become held acts, the rest share one page review item; none is dropped.

    The geometry this reconciliation actually ran under is published here too,
    because conservation also runs on structure-held pages (principle 8).
    """
    thresholds = analysis["thresholds"]
    measurable = analysis["background"] is not None
    result = (
        conservation.reconcile(
            analysis["width"],
            analysis["height"],
            analysis["rows"],
            background=analysis["background"],
            claimed_bounds=[entry["bounds"] for entry in claimed],
            gap_tolerance_px=thresholds.gap_tolerance_px,
            review_priority_min_dimension_px=thresholds.review_priority_min_dimension_px,
        )
        if measurable
        else {
            "total_ink_pixel_count": None,
            "claimed_pixel_count": None,
            "residual_pixel_count": None,
            "residual_components": [],
        }
    )
    page_id = page_record["subject_id"]
    components = result["residual_components"]
    component_count = len(components)
    promoted, aggregated = _partition_residual_components(components, thresholds)
    # `complete` on an unmeasured page too: nothing was withheld, and
    # `ink_measurable` already says nothing was measured.
    enumeration = RESIDUAL_ENUMERATION_AGGREGATED if aggregated else RESIDUAL_ENUMERATION_COMPLETE
    conservation_payload = {
        "page_ordinal": ordinal,
        # Recorded even where structure-status says null: this scan is independent.
        "background_source": analysis["background_source"],
        "background_value": analysis["background"],
        "ink_measurable": measurable,
        # Exactly the geometry `conservation.reconcile` was given.
        "page_width": analysis["width"],
        "page_height": analysis["height"],
        "reconciliation_thresholds": {
            "gap_tolerance_px": thresholds.gap_tolerance_px,
            "review_priority_min_dimension_px": thresholds.review_priority_min_dimension_px,
        }
        if measurable
        else None,
        "reason": _conservation_reason(measurable, bool(aggregated), component_count),
        "total_ink_pixel_count": result["total_ink_pixel_count"],
        "claimed_pixel_count": result["claimed_pixel_count"],
        "residual_pixel_count": result["residual_pixel_count"],
        # Zero, not null, on an unmeasurable page: nothing was enumerated, and
        # `common/stage.py` checks this equals the listed components.
        "residual_component_count": component_count,
        "residual_ink_fraction_bp": None
        if not measurable
        else _residual_ink_fraction_bp(
            result["residual_pixel_count"], result["total_ink_pixel_count"]
        ),
        "residual_enumeration": enumeration,
        "residual_promoted_component_count": len(promoted),
        "residual_aggregated_component_count": len(aggregated),
        "residual_aggregate_max_pixel_count": thresholds.residual_aggregate_max_pixel_count,
        "residual_aggregate_max_area_px": thresholds.residual_aggregate_max_area_px,
    }
    # Present only when measured; a sampled population, not a page-boundary mask.
    if analysis["dark_distribution"] is not None:
        conservation_payload["dark_distribution"] = analysis["dark_distribution"]
    # Present only when non-empty. Records the page-spanning decision directly;
    # those pixels stay in every ink count. Indexed, not `.get`: a missing key
    # is a bug.
    if analysis["page_spanning"]:
        conservation_payload["page_spanning_components"] = [
            {"bounds": dict(component["bounds"]), "pixel_count": component["pixel_count"]}
            for component in analysis["page_spanning"]
        ]
    conservation_payload["residual_components"] = promoted
    if aggregated:
        conservation_payload["aggregated_residual_components"] = aggregated
    _refuse_text_fields(conservation_payload)
    published = context.publish(
        kind="conservation",
        subject_id=page_id,
        outcome="held" if (aggregated or not measurable) else "proposed",
        inputs=[context.input_ref(page_record["payload"]["image_path"])],
        payload=conservation_payload,
    )
    secondary_held = _publish_secondary_proposals(
        context, ordinal, page_record, analysis, claimed, secondary, grouping_policy
    )
    conservation_ref = context.input_ref(published.relative_path)
    rows = _publish_residual_holds(context, page_id, ordinal, promoted, conservation_ref)
    if aggregated:
        rows.append(
            _publish_page_residual_hold(
                context,
                page_id,
                ordinal,
                _page_bounds(analysis),
                residual_component_count=component_count,
                aggregated_component_count=len(aggregated),
                grouping_config_sha256=grouping_policy["config_sha256"],
                conservation_ref=conservation_ref,
            )
        )
    return rows, secondary_held


def _conservation_reason(measurable: bool, aggregated: bool, component_count: int) -> str | None:
    """The one sentence a reviewer reads about why this record is not ordinary.

    Unmeasurable, aggregated, or None when the record is ordinary.
    """
    if not measurable:
        return (
            "this page's background could not be inferred, so it has no threshold to "
            "separate ink from paper and its ink was not measured; a count taken at a "
            "substituted divider would be a guess reported as a measurement"
        )
    if aggregated:
        return (
            f"this page's ink was measured in full and reconciled to {component_count} "
            "residual components; components below both sealed presentation thresholds are "
            "retained with exact geometry and pixels on this record and represented by one "
            "page review item, while significant components remain individual held acts"
        )
    return None


def _publish_page_fallbacks(
    context,
    pages: dict[int, dict],
    failures: dict[int, str],
    page_cache: dict[int, dict],
    status_refs: dict[int, dict[str, str]],
    provenance: dict,
    grouping_policy: dict,
) -> list[dict]:
    """Publish each page's unclaimed fallback coverage and return its seal rows."""
    rows = []
    claimed_by_page = _claimed_regions_by_page(context)
    for ordinal, page_record in pages.items():
        if ordinal in failures:
            continue
        analysis = _analyze_page(page_cache, context, ordinal, page_record, grouping_policy)
        if analysis["structure_evidence"] != "fallback-tiles":
            continue
        row = _publish_page_fallback(
            context,
            ordinal,
            page_record,
            analysis,
            status_refs[ordinal],
            claimed_by_page.get(ordinal, []),
            provenance,
        )
        if row is not None:
            rows.append(row)
    return rows


def _publish_page_conservation(
    context,
    pages: dict[int, dict],
    failures: dict[int, str],
    page_cache: dict[int, dict],
    secondary: dict,
    grouping_policy: dict,
) -> tuple[list[dict], bool, bool]:
    """Reconcile every sealed page and return rows plus named hold facts."""
    residual_rows = []
    secondary_held = False
    claimed_by_page = _claimed_regions_by_page(context)
    for ordinal, page_record in pages.items():
        analysis = _analyze_page(page_cache, context, ordinal, page_record, grouping_policy)
        page_rows, page_secondary_held = _publish_conservation_and_secondary(
            context,
            ordinal,
            page_record,
            analysis,
            claimed_by_page.get(ordinal, []),
            secondary,
            grouping_policy,
        )
        secondary_held = secondary_held or page_secondary_held
        residual_rows.extend(page_rows)
    unmeasured = any(
        analysis["background"] is None
        for ordinal, analysis in page_cache.items()
        if ordinal not in failures
    )
    return residual_rows, secondary_held, unmeasured


def _evidence_of(rows: list[dict]) -> list[dict]:
    return [reference for row in rows for reference in row["evidence"]]


def _initial_pass_has_holds(
    expected: list[dict],
    failures: dict[int, str],
    *,
    secondary_held: bool,
    unmeasured: bool,
) -> bool:
    """One explicit list of the facts that withhold a complete exit."""
    hold_facts = (
        any(row["outcome"] == "held" for row in expected),
        bool(failures),
        secondary_held,
        unmeasured,
    )
    return any(hold_facts)


def _account_for_declared_act(
    context,
    act: dict,
    pages: dict[int, dict],
    records: dict[int, dict],
    failures: dict[int, str],
    page_cache: dict[int, dict],
    padding: dict,
    provenance: dict,
    grouping_policy: dict,
) -> tuple[dict, list[dict]]:
    """Account for one fixture act and return its seal row and evidence."""
    page_ordinal = act["page_ordinal"]
    act_id = act_identity(context.fixture, act)
    continuation = continuation_for(context.fixture, act["key"])
    continuation_cut = False
    evidence = []
    # (blocking page ordinal, reason, reason code) when the act is held.
    hold_cause = None

    if page_ordinal not in pages:
        # Unsealed page: held, and no region is cut, not even a sealed
        # continuation, which would be an orphan crop.
        hold_cause = (
            page_ordinal,
            f"page {page_ordinal} was not sealed, so the act could not be marked out",
            "exemplar-page-not-sealed",
        )
    elif page_ordinal in failures:
        # Sealed but structure-held: the act is held, and its ink still reaches
        # conservation as residual.
        hold_cause = (
            page_ordinal,
            f"the structure pass could not mark out page {page_ordinal} "
            f"({failures[page_ordinal]}), so the act could not be bounded",
            "structure-pass-held",
        )
    else:
        analysis = _analyze_page(
            page_cache, context, page_ordinal, pages[page_ordinal], grouping_policy
        )
        primary = cut_region(
            context,
            act,
            pages[page_ordinal],
            act_bounds(act),
            1,
            page_ordinal,
            "proposal",
            padding=padding,
            provenance=provenance,
        )
        evidence.append(context.input_ref(primary.relative_path))

        # A continuation is a second region of the same act, never a new act.
        far_ordinal = continuation["page_ordinal"] if continuation else None
        continuation_analysis = None
        if continuation and far_ordinal in pages and far_ordinal not in failures:
            continuation_analysis = _analyze_page(
                page_cache, context, far_ordinal, pages[far_ordinal], grouping_policy
            )
            continuation_region = cut_region(
                context,
                act,
                pages[far_ordinal],
                _bounds_of(continuation),
                2,
                far_ordinal,
                "proposal",
                padding=padding,
                provenance=provenance,
            )
            evidence.append(context.input_ref(continuation_region.relative_path))
            continuation_cut = True

        if continuation and not continuation_cut:
            # The near side stays cut as evidence, but the act is held: reading
            # it alone would pass a truncation as complete.
            if far_ordinal in failures:
                hold_cause = (
                    far_ordinal,
                    f"the act continues onto page {far_ordinal}, which the structure "
                    f"pass could not mark out ({failures[far_ordinal]}), so its "
                    "continuation could not be cut",
                    "structure-pass-held-on-continuation",
                )
            else:
                hold_cause = (
                    far_ordinal,
                    f"the act continues onto page {far_ordinal}, "
                    "which was not sealed, so its continuation could not be cut",
                    "exemplar-continuation-not-sealed",
                )
        else:
            _publish_act_group(
                context,
                act,
                act_id,
                pages[page_ordinal],
                analysis,
                continuation if continuation_cut else None,
                pages[far_ordinal] if continuation_cut else None,
                continuation_analysis,
            )

    if hold_cause is not None:
        blocking_ordinal, reason, reason_code = hold_cause
        hold = hold_act(context, act, act_id, blocking_ordinal, records, reason, reason_code)
        evidence.append(context.input_ref(hold.relative_path))
    outcome = "proposed" if hold_cause is None else "held"

    # The sealed page's subject where one exists; the fixture derivation only
    # for an unsealed page.
    page_id = (
        pages[page_ordinal]["subject_id"]
        if page_ordinal in pages
        else page_identity(context.fixture, page_ordinal)
    )
    row = _seal_row(
        act_id,
        act["key"],
        page_id,
        page_ordinal,
        outcome,
        _by_path(evidence),
        has_continuation=continuation_cut,
    )
    return row, evidence


def _sealed_designator_policies(context) -> tuple[dict, dict]:
    """Padding and grouping policies, each checked against the run's seal on load.

    Geometry is loaded only to check it: a rewrite after `open_context` would
    otherwise go unnoticed.
    """
    padding = geometry.load_padding_config(context.args.designator_padding_config)
    context.require_sealed_config("designator-padding", padding["config_sha256"])
    geometry_policy = geometry_layer.load_geometry_policy(context.args.designator_geometry_config)
    context.require_sealed_config("designator-geometry", geometry_policy["config_sha256"])
    grouping_policy = grouping_config.load_grouping_config(context.args.designator_grouping_config)
    context.require_sealed_config("designator-grouping", grouping_policy["config_sha256"])
    return padding, grouping_policy


def _publish_proposal_seal(context, expected: list[dict], inputs: list, provenance: dict) -> None:
    """Emitted once, never rewritten: downstream stages reconcile against it."""
    payload = {"expected_acts": expected, "count": len(expected), "provenance": provenance}
    payload["self_hash"] = self_hash(payload)
    context.publish(
        kind="proposal-seal",
        subject_id="proposal-seal",
        outcome="proposed",
        inputs=inputs,
        payload=payload,
    )


def initial_pass(context) -> bool:
    """Mark out every act on every sealed page. True when anything was held."""
    records = page_records(context)
    pages = sealed_pages(records)
    if not pages:
        raise ContractError("the Designator found no sealed page to mark out")

    padding, grouping_policy = _sealed_designator_policies(context)
    provenance = structure_provenance(context)
    secondary = _publish_secondary_provenance(context, secondary_provenance(context))
    # Decided once, before any crop is cut.
    failures = structure_failures(context, pages)
    page_cache: dict[int, dict] = {}
    # Analyse before publishing status, so a fatal decode leaves no success claim.
    for ordinal, page_record in pages.items():
        if ordinal not in failures:
            # No hold here: an unthresholdable page is cut into fallback tiles,
            # since every page is read. A corrupt decode is still fatal.
            _analyze_page(page_cache, context, ordinal, page_record, grouping_policy)
    status_refs = publish_structure_status(
        context, records, pages, provenance, failures, page_cache
    )

    _refuse_duplicate_proposal_bounds(context)
    expected = []
    seal_inputs = []
    proposed_acts: dict[int, list[dict]] = {
        ordinal: [] for ordinal in pages if ordinal not in failures
    }
    for act in context.fixture["act"]:
        row, evidence = _account_for_declared_act(
            context,
            act,
            pages,
            records,
            failures,
            page_cache,
            padding,
            provenance,
            grouping_policy,
        )
        expected.append(row)
        seal_inputs.extend(evidence)
        if row["outcome"] == "proposed":
            proposed_acts[act["page_ordinal"]].append(
                {
                    "act_id": row["act_id"],
                    "act_key": act["key"],
                    "bounds": act_bounds(act),
                    "linked": continuation_for(context.fixture, act["key"]) is not None,
                }
            )
    _publish_continuation_candidates(
        context, pages, page_cache, status_refs, proposed_acts, grouping_policy
    )

    # Fallback tiles are cut before conservation, which must see them as claims
    # or would report their ink as residual. Structure-held pages are not tiled.
    fallback_rows = _publish_page_fallbacks(
        context, pages, failures, page_cache, status_refs, provenance, grouping_policy
    )
    expected.extend(fallback_rows)
    seal_inputs.extend(_evidence_of(fallback_rows))
    if not expected:
        raise ContractError("no declared act or page fallback was marked out on any sealed page")

    # Every sealed page, including pages no act touched; residuals join the seal.
    residual_rows, secondary_held, unmeasured = _publish_page_conservation(
        context, pages, failures, page_cache, secondary, grouping_policy
    )
    expected.extend(residual_rows)
    seal_inputs.extend(_evidence_of(residual_rows))
    _publish_proposal_seal(context, expected, seal_inputs, provenance)
    # Any hold, secondary hold or unmeasured page withholds "complete"
    # (principle 2). An unmeasured page has not reconciled, but its crops still
    # go downstream; only the run's completion claim is withheld.
    return _initial_pass_has_holds(
        expected, failures, secondary_held=secondary_held, unmeasured=unmeasured
    )


def _live_secondary_provenance(context) -> dict:
    """The secondary proposer on the live path: recorded absent, or refused.

    Resolved every run, as in `secondary_provenance`. A configured secondary
    chair is refused: the live path serves none and may not write a receipt for
    a call it did not make (principle 6).
    """
    resolved = context.registry.resolve(SECONDARY_PROPOSER_CHAIR)
    if isinstance(resolved, AbsentChair):
        return _absent_chair_record(context, resolved)
    raise ContractError(
        f"the secondary proposer chair {SECONDARY_PROPOSER_CHAIR!r} is configured, but the "
        "live structure pass serves no secondary chair and writes no fixture receipt for one, "
        "so a live run must configure it absent"
    )


def _publish_live_act_groups(
    context, page_record: dict, analysis: dict, minted: list[tuple[str, str, dict]]
) -> None:
    """One text-free `act-group` per chair rectangle on one page, evidence from the scan.

    `declared_bounds` are the chair's. Evidence is computed for the whole page
    at once so a merged ink group is recorded on both acts it covers. No
    continuation: a per-page call cannot see across pages, and that link is the
    Recensor's.
    """
    blocks = structure_pass.model_evidence_blocks(
        analysis, [(act_key, bounds) for _act_id, act_key, bounds in minted]
    )
    image_ref = context.input_ref(page_record["payload"]["image_path"])
    for (act_id, act_key, bounds), block in zip(minted, blocks, strict=True):
        payload = {
            "act_key": act_key,
            "declared_bounds": dict(bounds),
            "continuation": None,
            **block,
        }
        _validate_act_group_payload(payload)
        context.publish(
            kind="act-group",
            subject_id=act_id,
            outcome="proposed",
            inputs=[image_ref],
            payload=payload,
        )


def _sealed_structure_answer(
    context,
    page_record: dict,
    attempt_policy: Mapping[str, Any],
) -> tuple[dict, dict[str, str]] | None:
    """A page's already-published structure answer and its reference, or None.

    A page already answered is never asked again. The payload is revalidated so
    an unknown schema refuses rather than mints, and its provenance too, because
    it is about to be copied onto new artifacts and its receipt must still exist.
    """
    page_id = page_record["subject_id"]
    # Must match the identity `context.publish` writes the answer under.
    identifier = artifact_id(DESIGNATOR, STRUCTURE_ANSWER_KIND, page_id, None)
    if not context.tree.has_artifact(DESIGNATOR, STRUCTURE_ANSWER_KIND, identifier):
        return None
    relative = context.tree.artifact_path(DESIGNATOR, STRUCTURE_ANSWER_KIND, identifier)
    record = context.tree.read_artifact(DESIGNATOR, STRUCTURE_ANSWER_KIND, identifier)
    payload = record["payload"]
    _validate_structure_answer_payload(payload, terminal=True)
    validate_serving_provenance(
        context,
        payload["provenance"],
        producer_stage=DESIGNATOR,
        require_receipt=True,
    )
    if payload["schema"] == STRUCTURE_ANSWER_RECORD_SCHEMA:
        if dict(attempt_policy) != {"max_attempts": 1, "seed_schedule": "fixed-base"}:
            raise ContractError(
                f"the legacy structure answer for page {page_id} is incompatible with "
                "this run's sealed recovery policy"
            )
    else:
        history = _published_structure_attempts(context, page_record, attempt_policy)
        references = [reference for _answer, reference in history]
        if payload["attempt_policy"] != dict(attempt_policy) or payload["attempts"] != references:
            raise ContractError(
                f"the terminal structure answer for page {page_id} does not name the exact "
                "sealed attempt policy and contiguous attempt history"
            )
        if not history:
            raise ContractError(f"the terminal structure answer for page {page_id} has no attempt")
        expected = dict(history[-1][0].record)
        expected["attempts"] = references
        if payload != expected:
            raise ContractError(
                f"the terminal structure answer for page {page_id} disagrees with its last "
                "immutable attempt"
            )
    return payload, context.input_ref(relative)


def _publish_structure_record(
    context, page_record: dict, answer: structure_pass.PageAnswer, kind: str, **attempt
) -> dict[str, str]:
    inputs = [context.input_ref(page_record["payload"]["image_path"])]
    if answer.record["schema"] == STRUCTURE_ANSWER_RECORD_SCHEMA_V3:
        inputs.append(dict(answer.record["presentation_ref"]))
    published = context.publish(
        kind=kind,
        subject_id=answer.page_id,
        outcome="held" if answer.disposition == structure_pass.DISPOSITION_HELD else "proposed",
        inputs=inputs,
        payload=answer.record,
        **attempt,
    )
    return context.input_ref(published.relative_path)


def _publish_structure_answer(
    context, page_record: dict, answer: structure_pass.PageAnswer
) -> dict[str, str]:
    """Publish one page's answer the moment it arrives, and return its reference."""
    _validate_structure_answer_payload(answer.record, terminal=True)
    return _publish_structure_record(context, page_record, answer, STRUCTURE_ANSWER_KIND)


def _recoverable_structure_outcome(answer: structure_pass.PageAnswer) -> bool:
    """Whether an answer permits another coverage attempt, never a quality reroll."""
    return answer.reason_code == structure_pass.HELD_DEGENERATE or answer.reason_code in {
        f"structure-answer-{outcome}" for outcome in structure_pass.chandra_layout.PARSE_OUTCOMES
    }


def _publish_structure_attempt(
    context,
    page_record: dict,
    answer: structure_pass.PageAnswer,
    ordinal: int,
    prior_references: list[dict[str, str]],
) -> dict[str, str]:
    """Persist one received answer before recovery can decide what comes next."""
    answer.record["attempt_ordinal"] = ordinal
    answer.record["attempts"] = list(prior_references)
    _validate_structure_answer_payload(answer.record, terminal=False)
    return _publish_structure_record(
        context,
        page_record,
        answer,
        STRUCTURE_ATTEMPT_KIND,
        attempt=attempt_id(answer.page_id, "structure", ordinal),
    )


def _published_structure_attempts(
    context,
    page_record: dict,
    attempt_policy: Mapping[str, Any],
) -> list[tuple[structure_pass.PageAnswer, dict[str, str]]]:
    """Read a contiguous immutable attempt history for an interrupted page."""
    page_id = page_record["subject_id"]
    page_ordinal = page_record["payload"]["ordinal"]
    rows = [
        row
        for row in _stage_records(context.tree, DESIGNATOR, STRUCTURE_ATTEMPT_KIND)
        if row["subject_id"] == page_id
    ]
    rows.sort(key=lambda row: row["payload"].get("attempt_ordinal", 0))
    result: list[tuple[structure_pass.PageAnswer, dict[str, str]]] = []
    for ordinal, row in enumerate(rows, start=1):
        payload = row["payload"]
        _validate_structure_answer_payload(payload, terminal=False)
        expected_reference = context.input_ref(
            context.tree.artifact_path(DESIGNATOR, STRUCTURE_ATTEMPT_KIND, row["artifact_id"])
        )
        prior_references = [reference for _answer, reference in result]
        if (
            payload["attempt_ordinal"] != ordinal
            or row["attempt_id"] != attempt_id(page_id, "structure", ordinal)
            or payload["page_id"] != page_id
            or payload["page_ordinal"] != page_ordinal
            or payload["attempt_policy"] != dict(attempt_policy)
            or payload["attempts"] != prior_references
        ):
            raise ContractError(
                f"structure attempts for page {page_id} are not a contiguous sealed history"
            )
        if result:
            if (
                result[-1][0].record["schema"] == STRUCTURE_ANSWER_RECORD_SCHEMA_V3
                and payload["schema"] == STRUCTURE_ANSWER_RECORD_SCHEMA_V2
            ):
                raise ContractError(
                    f"structure attempts for page {page_id} downgrade their native "
                    "presentation schema"
                )
            prior_seed = result[-1][0].record["attempt_seed"]
            expected_seed = (
                prior_seed if attempt_policy["seed_schedule"] == "fixed-base" else prior_seed + 1
            )
            if payload["attempt_seed"] != expected_seed:
                raise ContractError(
                    f"structure attempts for page {page_id} do not follow the sealed seed schedule"
                )
        validate_serving_provenance(
            context, payload["provenance"], producer_stage=DESIGNATOR, require_receipt=True
        )
        verify_structure_attempt_call(
            context,
            payload,
            page_id,
            attempt_inputs=row.get("inputs"),
        )
        result.append(
            (
                structure_pass.sealed_page_answer(payload),
                expected_reference,
            )
        )
    return result


def _terminalize_structure_history(
    context,
    page_record: dict,
    history: list[tuple[structure_pass.PageAnswer, dict[str, str]]],
) -> tuple[structure_pass.PageAnswer, dict[str, str]]:
    """Publish the once-only terminal answer from an already retained history."""
    answer = structure_pass.sealed_page_answer(history[-1][0].record)
    answer.record["attempts"] = [reference for _attempt, reference in history]
    return answer, _publish_structure_answer(context, page_record, answer)


def _needs_another_attempt(
    history: list[tuple[structure_pass.PageAnswer, dict[str, str]]],
    attempt_policy: Mapping[str, Any],
) -> bool:
    return not history or (
        _recoverable_structure_outcome(history[-1][0])
        and len(history) < attempt_policy["max_attempts"]
    )


def _resumed_structure_answers(
    context, pages: dict[int, dict], attempt_policy: Mapping[str, Any]
) -> tuple[dict[int, structure_pass.PageAnswer], dict[int, dict[str, str]]]:
    """Every page's already-sealed terminal answer and its reference, by ordinal."""
    answers: dict[int, structure_pass.PageAnswer] = {}
    answer_refs: dict[int, dict[str, str]] = {}
    for ordinal, page_record in sorted(pages.items()):
        sealed = _sealed_structure_answer(context, page_record, attempt_policy)
        if sealed is None:
            continue
        record, reference = sealed
        if record["page_ordinal"] != ordinal:
            raise ContractError(
                f"the sealed structure answer for page {page_record['subject_id']} says it is "
                f"page {record['page_ordinal']}, and the Exemplar sealed that page as "
                f"{ordinal}; a page answered under one ordinal and resumed under another would "
                "mint act keys for a page it is not"
            )
        answers[ordinal] = structure_pass.sealed_page_answer(record)
        answer_refs[ordinal] = reference
    return answers, answer_refs


def _publish_live_proposals(
    context,
    pages: dict[int, dict],
    page_cache: dict[int, dict],
    answers: dict[int, structure_pass.PageAnswer],
    padding: dict,
    provenance_by_page: dict[int, dict],
) -> tuple[list[dict], dict[int, list[dict]]]:
    """Cut and group every rectangle the chair proposed; return the seal rows and each
    page's acts."""
    rows = []
    acts_by_page: dict[int, list[dict]] = {}
    for ordinal, answer in answers.items():
        if answer.disposition != structure_pass.DISPOSITION_DETECTED:
            continue
        page_record = pages[ordinal]
        analysis = page_cache[ordinal]
        minted: list[tuple[str, str, dict]] = []
        acts_by_page[ordinal] = []
        for act in answer.mint:
            bounds = structure_pass.validated_rectangle(act, analysis["width"], analysis["height"])
            act_id = derive_minted_act_id(page_record["subject_id"], "proposal", bounds)
            act_key = structure_pass.proposal_act_key(ordinal, act["ordinal"])
            region = cut_minted_region(
                context,
                act_id,
                act_key,
                page_record,
                bounds,
                1,
                ordinal,
                "proposal",
                padding=padding,
                provenance=provenance_by_page[ordinal],
            )
            evidence = [context.input_ref(region.relative_path)]
            rows.append(
                _seal_row(act_id, act_key, page_record["subject_id"], ordinal, "proposed", evidence)
            )
            minted.append((act_id, act_key, bounds))
            acts_by_page[ordinal].append(
                {"act_id": act_id, "act_key": act_key, "bounds": bounds, "linked": False}
            )
        _publish_live_act_groups(context, page_record, analysis, minted)
    return rows, acts_by_page


def _publish_live_fallbacks(
    context,
    pages: dict[int, dict],
    page_cache: dict[int, dict],
    answers: dict[int, structure_pass.PageAnswer],
    status_refs: dict[int, dict[str, str]],
    provenance_by_page: dict[int, dict],
) -> list[dict]:
    """Tile each page the chair answered with no act over its own grid, never the
    scan's groups, which here only corroborate; return the seal rows."""
    rows = []
    claimed_by_page = _claimed_regions_by_page(context)
    for ordinal, answer in answers.items():
        if answer.disposition != structure_pass.DISPOSITION_FALLBACK_TILES:
            continue
        analysis = page_cache[ordinal]
        tiled = {
            **analysis,
            "structure_evidence": "fallback-tiles",
            # The chair decides when to tile; the sealed policy decides how.
            "groups": _fallback_grid(analysis["width"], analysis["height"], analysis["thresholds"]),
        }
        row = _publish_page_fallback(
            context,
            ordinal,
            pages[ordinal],
            tiled,
            status_refs[ordinal],
            claimed_by_page.get(ordinal, []),
            provenance_by_page[ordinal],
            reason=_FALLBACK_REASON_LIVE,
        )
        if row is not None:
            rows.append(row)
    return rows


def live_initial_pass(context, serving_factory, tier: str) -> bool:
    """Mark out every sealed page through the served structure chair. True when held.

    Shares everything with `initial_pass` except the proposer: one retained
    terminal answer per sealed page, from up to the sealed attempt limit of
    served calls, replaces the fixture's declared acts, and the chair's real
    receipt replaces the fixture provenance. `context.fixture` is never read.

    Per page, the answer publishes before the status that cites it, then the
    crops. A page the chair could not mark out goes into `failures`, so its hold
    follows the fixture path's proven route.

    The pass resumes: an answered page is read back, never asked again (a second
    answer would conflict with the fixed artifact identity), and each answer is
    published as it arrives so an interruption keeps what was paid for
    (principle 2). Each page's artifacts name the session that answered it.
    """
    records = page_records(context)
    pages = sealed_pages(records)
    if not pages:
        raise ContractError("the Designator found no sealed page to mark out")

    padding, grouping_policy = _sealed_designator_policies(context)
    # The sealed `[structure]` decoding posture, refused before any chair starts
    # if the live seam cannot execute it.
    decoding_policy, decoding_sha256 = load_decoding_policy(context.args.decoding_config)
    context.require_sealed_config("decoding", decoding_sha256)
    temperature = structure_pass.executable_temperature(decoding_policy)
    attempt_policy = structure_recovery_policy(decoding_policy)
    identity = structure_pass.resolved_structure_chair(context)
    secondary = _publish_secondary_provenance(context, _live_secondary_provenance(context))

    page_cache: dict[int, dict] = {}
    for ordinal, page_record in pages.items():
        _analyze_page(page_cache, context, ordinal, page_record, grouping_policy)

    answers, answer_refs = _resumed_structure_answers(context, pages, attempt_policy)
    unanswered = [ordinal for ordinal in sorted(pages) if ordinal not in answers]
    histories = {
        ordinal: _published_structure_attempts(context, pages[ordinal], attempt_policy)
        for ordinal in unanswered
    }
    needs_request = []
    for ordinal in unanswered:
        if _needs_another_attempt(histories[ordinal], attempt_policy):
            needs_request.append(ordinal)
        else:
            answers[ordinal], answer_refs[ordinal] = _terminalize_structure_history(
                context, pages[ordinal], histories[ordinal]
            )

    # Nothing left to ask means no chair, and no paid pod, is started.
    if needs_request:
        client = serving_factory(context, identity, tier)
        with client:
            engine_call = structure_pass.structure_engine_call(decoding_sha256)
            provenance = structure_pass.live_chair_record(
                context, identity, client.handle.receipt_reference, engine_call
            )
            for ordinal in needs_request:
                page_record = pages[ordinal]
                history = histories[ordinal]
                while _needs_another_attempt(history, attempt_policy):
                    answer = structure_pass.ask_page(
                        context,
                        client,
                        page_record,
                        ordinal,
                        _read_checked_page_bytes(context, page_record),
                        page_cache[ordinal],
                        temperature=temperature,
                        decoding_config_sha256=decoding_sha256,
                        provenance=provenance,
                        attempt_ordinal=len(history) + 1,
                        attempt_policy=attempt_policy,
                    )
                    # Publish before deciding on a retry, so a resume reuses
                    # this answer instead of paying for it again.
                    history.append(
                        (
                            answer,
                            _publish_structure_attempt(
                                context,
                                page_record,
                                answer,
                                len(history) + 1,
                                [reference for _attempt, reference in history],
                            ),
                        )
                    )
                answers[ordinal], answer_refs[ordinal] = _terminalize_structure_history(
                    context, page_record, history
                )

    # Page order, so a resume seals the same `expected_acts` list as a fresh run.
    answers = {ordinal: answers[ordinal] for ordinal in sorted(answers)}

    failures: dict[int, str] = {}
    status_answers: dict[int, tuple[str | None, dict[str, str]]] = {}
    # The session that answered each page, not the one running now.
    provenance_by_page = {
        ordinal: answer.record["provenance"] for ordinal, answer in answers.items()
    }
    for ordinal, answer in answers.items():
        if answer.disposition == structure_pass.DISPOSITION_HELD:
            # Null evidence, but the answer is still cited as the hold's evidence.
            failures[ordinal] = answer.reason_code
            status_answers[ordinal] = (None, answer_refs[ordinal])
        else:
            status_answers[ordinal] = (answer.disposition, answer_refs[ordinal])
    # The first page's session: the only choice stable across resumes, so the
    # seal republishes identically.
    seal_provenance = provenance_by_page[min(provenance_by_page)]
    status_refs = publish_structure_status(
        context,
        records,
        pages,
        seal_provenance,
        failures,
        page_cache,
        answers=status_answers,
        provenance_by_page=provenance_by_page,
    )

    expected, acts_by_page = _publish_live_proposals(
        context, pages, page_cache, answers, padding, provenance_by_page
    )
    _publish_continuation_candidates(
        context, pages, page_cache, status_refs, acts_by_page, grouping_policy
    )
    expected.extend(
        _publish_live_fallbacks(
            context, pages, page_cache, answers, status_refs, provenance_by_page
        )
    )
    if not expected and not failures:
        raise ContractError("no structural proposal or page fallback was marked out on any page")

    residual_rows, secondary_held, unmeasured = _publish_page_conservation(
        context, pages, failures, page_cache, secondary, grouping_policy
    )
    expected.extend(residual_rows)
    if not expected:
        raise ContractError(
            "every page was held and none carried ink to account for; the run has no act "
            "denominator to seal"
        )
    _publish_proposal_seal(context, expected, _evidence_of(expected), seal_provenance)
    return _initial_pass_has_holds(
        expected, failures, secondary_held=secondary_held, unmeasured=unmeasured
    )


def _refuse_duplicate_proposal_bounds(context) -> None:
    """A proposal class is one rectangle per page, never an ordinal namespace.

    Identity carries no ordinal, so two proposals with the same bounds would
    share an ``act_id``; refuse before anything is cut rather than merge them.
    """
    seen: dict[tuple[int, tuple[int, int, int, int]], str] = {}
    for act in context.fixture["act"]:
        bounds = act_bounds(act)
        key = (act["page_ordinal"], tuple(bounds[name] for name in ("x", "y", "w", "h")))
        prior = seen.get(key)
        if prior is not None:
            raise ContractError(
                f"Designator proposals {prior!r} and {act['key']!r} have identical bounds "
                f"on page {act['page_ordinal']}; proposal identity has no ordinal namespace"
            )
        seen[key] = act["key"]


def _ink_outside_cut_union(evidence: dict, bounds: dict, covered: list[dict]) -> int:
    """Recompute ink in a requested rectangle outside the prior crop union."""
    width, height, rows = evidence.get("width"), evidence.get("height"), evidence.get("rows")
    if (
        evidence.get("schema") != "ink-runs.v2"
        or set(evidence) != {"schema", "width", "height", "rows"}
        or not _is_int(width)
        or width <= 0
        or not _is_int(height)
        or height <= 0
        or not isinstance(rows, list)
        or len(rows) != height
    ):
        raise ContractError("the recovery request's Ink Map evidence is malformed")
    total = 0
    for y in range(bounds["y"], bounds["y"] + bounds["h"]):
        row = rows[y]
        if not isinstance(row, list):
            raise ContractError("the recovery request's Ink Map evidence has a malformed row")
        previous_end = 0
        ink_spans = []
        for run in row:
            if (
                not isinstance(run, list)
                or len(run) != 2
                or any(not _is_int(value) for value in run)
            ):
                raise ContractError("the recovery request's Ink Map evidence has a malformed run")
            start, length = run
            end = start + length
            if start < previous_end or length <= 0 or end > width:
                raise ContractError(
                    "the recovery request's Ink Map evidence has unordered or invalid runs"
                )
            previous_end = end
            start, end = max(start, bounds["x"]), min(end, bounds["x"] + bounds["w"])
            if start < end:
                ink_spans.append((start, end))
        cuts = sorted(
            (max(bounds["x"], cut["x"]), min(bounds["x"] + bounds["w"], cut["x"] + cut["w"]))
            for cut in covered
            if cut["y"] <= y < cut["y"] + cut["h"]
        )
        merged = []
        for start, end in cuts:
            if start >= end:
                continue
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        for start, end in ink_spans:
            cursor = start
            for cut_start, cut_end in merged:
                if cut_end <= cursor:
                    continue
                if cut_start >= end:
                    break
                total += max(0, min(cut_start, end) - cursor)
                cursor = max(cursor, cut_end)
            total += max(0, end - cursor)
    return total


def _verify_coverage_recovery_evidence(
    context,
    request: dict,
    request_payload: dict,
    expected_act: dict,
    page_id: str,
    page_ordinal: int,
    page_width: int,
    page_height: int,
) -> None:
    """Follow and remeasure the exact Testimonium and Ink Map authorization."""
    observation = request_payload.get("coverage_observation")
    bounds = request_payload.get("recovery_bounds")
    ink_map_ref = request_payload.get("ink_map_ref")
    inputs = request.get("inputs")
    if (
        request_payload.get("origin") != "coverage-observation"
        or not isinstance(observation, dict)
        or set(observation)
        != {"testimonium_ref", "testimonium_id", "observation_ordinal", "bounds"}
        or observation.get("bounds") != bounds
        or not isinstance(inputs, list)
        or observation.get("testimonium_ref") not in inputs
        or not isinstance(ink_map_ref, dict)
        or ink_map_ref not in inputs
    ):
        raise ContractError(
            "a real recovery request does not bind exact Testimonium and Ink Map evidence"
        )
    testimonium = context.tree.read_artifact_reference(
        observation["testimonium_ref"],
        stage=ATTESTATORES,
        kind="page-testimonium",
        subject_id=page_id,
    )
    ordinal = observation.get("observation_ordinal")
    rows = testimonium.get("payload", {}).get("observed")
    source_rows = (
        [row for row in rows if isinstance(row, dict) and row.get("ordinal") == ordinal]
        if isinstance(rows, list)
        else []
    )
    if (
        testimonium.get("artifact_id") != observation.get("testimonium_id")
        or testimonium.get("payload", {}).get("page_ordinal") != page_ordinal
        or not _is_int(ordinal)
        or len(source_rows) != 1
        or source_rows[0].get("bounds_source") not in {"native", "derived"}
    ):
        raise ContractError(
            "a real recovery request does not resolve to one reported coverage observation"
        )
    source = source_rows[0].get("bounds")
    if (
        not isinstance(source, dict)
        or set(source) != {"x", "y", "w", "h"}
        or any(not _is_int(source[name]) for name in ("x", "y", "w", "h"))
        or source["w"] <= 0
        or source["h"] <= 0
    ):
        raise ContractError("the bound coverage observation has malformed geometry")
    canonical = {
        "x": max(0, source["x"]),
        "y": max(0, source["y"]),
        "w": max(0, min(page_width, source["x"] + source["w"]) - max(0, source["x"])),
        "h": max(0, min(page_height, source["y"] + source["h"]) - max(0, source["y"])),
    }
    if canonical != bounds or canonical["w"] <= 0 or canonical["h"] <= 0:
        raise ContractError(
            "the requested recovery geometry is not the canonical on-page observation rectangle"
        )
    ink_map = context.tree.read_artifact_reference(
        ink_map_ref, stage=INK_MAP, kind="ink-map", subject_id=page_id
    )
    ink_payload = ink_map.get("payload")
    evidence = ink_payload.get("edge_findings") if isinstance(ink_payload, dict) else None
    if (
        not isinstance(evidence, dict)
        or ink_payload.get("page_ordinal") != page_ordinal
        or evidence.get("width") != page_width
        or evidence.get("height") != page_height
    ):
        raise ContractError("the recovery request's Ink Map does not bind this sealed page")
    grouping_policy = grouping_config.load_grouping_config(context.args.designator_grouping_config)
    context.require_sealed_config("designator-grouping", grouping_policy["config_sha256"])
    minimum = grouping_policy["coverage_audit"]["minimum_ink_pixels"]
    covered = _coverage_on_page(_regions_of(context), page_ordinal, page_id)
    measured = _ink_outside_cut_union(evidence, bounds, covered)
    if (
        request_payload.get("minimum_ink_pixels") != minimum
        or request_payload.get("outside_ink_pixels") != measured
        or measured < minimum
        or expected_act.get("page_ordinal") != page_ordinal
    ):
        raise ContractError(
            "the recovery request's claimed outside ink does not recompute from its sealed evidence"
        )


def _declared_recovery(context, act_key: str) -> tuple[dict, list[dict]]:
    """The fixture act for `act_key` and its one declared recovery row, as a list."""
    fixture_acts = [item for item in context.fixture["act"] if item["key"] == act_key]
    if not fixture_acts:
        raise ContractError(
            f"recovery fixture declares no act for key {act_key!r}; the fixture "
            "cannot supply recovery geometry for an act it never declared"
        )
    if len(fixture_acts) != 1:  # pragma: no cover - fixture loading already refuses duplicates
        raise ContractError(
            f"recovery fixture declares {len(fixture_acts)} acts for key "
            f"{act_key!r}; recovery geometry needs one unambiguous act"
        )
    act = fixture_acts[0]
    recovery = [row for row in context.fixture.get("recovery", []) if row["act_key"] == act["key"]]
    if len(recovery) != 1:
        raise ContractError(
            f"the fixture declares {len(recovery)} recovery regions for act {act['key']}; "
            "a recovery request must name exactly one coverage rectangle"
        )
    return act, recovery


def recovery_pass(context, act_id: str, request_id: str) -> None:
    """Cut one replacement region for one act, at the Recensor's request.

    The Recensor asks; only the Designator cuts, so crops keep one author.
    """
    # The shared consumer verifies the seal and every minted premise first.
    match = [item for item in expected_acts(context) if item["act_id"] == act_id]
    if not match:
        raise ContractError(f"recovery asked for {act_id}, which the proposal seal does not name")
    if match[0].get("outcome") != "proposed":
        raise ContractError(
            f"recovery asked for {act_id}, which the seal holds as "
            f"{match[0].get('outcome')!r}; a held act is terminal and may not be "
            "recropped back to life"
        )

    # The policy the run bound, never re-read, so a recrop's budget is the
    # sealed one.
    policy = context.recovery_policy
    context.require_sealed_config("recovery", policy["config_sha256"])
    request = current_recovery_request(
        context.tree,
        act_id,
        policy,
        request_id=request_id,
    )
    request_payload = request.get("payload")
    # Already verified by `current_recovery_request`; narrows the type only.
    if not isinstance(request_payload, dict):  # pragma: no cover - common guard above
        raise ContractError("the requested Recensor recovery record has no payload")
    ordinal = request_payload.get("attempt_ordinal")
    if not _is_int(ordinal):  # pragma: no cover
        raise ContractError("the requested Recensor recovery record has no attempt ordinal")
    if request_payload.get("act_key") != match[0]["act_key"]:
        raise ContractError(
            "the exact current Recensor recovery request does not bind this proposal-seal act"
        )
    # Only a recrop is this stage's to answer; a crop must never stand in for
    # the Perlector's reread.
    recovery_kind = request_payload.get("recovery_kind")
    if recovery_kind != FALLBACK_RECROP:
        raise ContractError(
            f"recovery for {act_id} names recovery_kind {recovery_kind!r}; the Designator "
            f"only answers {FALLBACK_RECROP!r} requests (a recrop). A different recovery "
            "kind names a different owning stage, not a substitute crop"
        )

    real_input = parse_ingress_record(context.run.get("ingress")) == REAL_INGRESS
    # Real ingress has no fixture: its recrop geometry comes from the request.
    act, recovery = (None, []) if real_input else _declared_recovery(context, match[0]["act_key"])

    pages = sealed_pages(page_records(context))
    if real_input:
        bounds = request_payload.get("recovery_bounds")
        if not isinstance(bounds, dict):
            raise ContractError(
                "a real-ingress recovery request has no ink-confirmed recovery_bounds; a "
                "Designator must never substitute fixture geometry"
            )
        page_ordinal = match[0]["page_ordinal"]
    else:
        bounds = _bounds_of(recovery[0])
        page_ordinal = act["page_ordinal"]
    page_record = pages[page_ordinal]
    # Validated before the coverage checks below, so a bad rectangle is refused
    # as one rather than as "recovers no coverage".
    page_w, page_h = dimensions(_read_checked_page_bytes(context, page_record))
    geometry.validate_bounds(bounds, page_w, page_h, "recovery bounds")
    if real_input:
        _verify_coverage_recovery_evidence(
            context,
            request,
            request_payload,
            match[0],
            page_record["subject_id"],
            page_ordinal,
            page_w,
            page_h,
        )
    # The same builder `cut_region` uses, so the predicted identity matches.
    transform = _crop_transform(page_ordinal, page_record["subject_id"], bounds)
    duplicate = region_id(act_id, transform)
    existing_regions = _regions_of(context, act_id)
    already_recovered = [
        record for record in existing_regions if record["payload"].get("origin") == "recovery"
    ]
    # Recutting an existing transform would be a re-roll, not recovered coverage.
    if any(record["payload"].get("region_id") == duplicate for record in existing_regions):
        raise ContractError(
            f"recovery asked for {act_id}, which already has a region cut for this exact "
            "transform; a recovery must add coverage rather than re-read identical pixels"
        )
    # The same rule over pixels: a recrop inside what the act already covers,
    # even jointly, adds nothing (principle 7). Refused rather than flagged,
    # because it would spend the act's bounded recovery budget.
    covered = _coverage_on_page(existing_regions, page_ordinal, page_record["subject_id"])
    if not _uncovered_area(bounds, covered):
        raise ContractError(
            f"recovery asked for {act_id} with bounds {bounds}, which recovers no page "
            f"pixel the act does not already have: every pixel of it already lies inside "
            f"the {len(covered)} region(s) cut for it on page {page_ordinal}. A "
            "recovery must add coverage, not recrop inside coverage it already has"
        )
    recovery_count = len(already_recovered)
    if request_payload.get("budget_used") != recovery_count or ordinal != recovery_count + 1:
        raise ContractError(
            "the supplied recovery request is stale or skips a recovery ordinal; a recrop "
            "may only answer the next recorded request"
        )
    region_ordinal = _next_region_ordinal(context, act_id)
    request_ref = context.artifact_ref(RECENSOR, "recovery-request", request["artifact_id"])
    if real_input:
        cut_minted_region(
            context,
            act_id,
            match[0]["act_key"],
            page_record,
            bounds,
            region_ordinal,
            page_ordinal,
            "recovery",
            request_ref,
        )
    else:
        cut_region(
            context, act, page_record, bounds, region_ordinal, page_ordinal, "recovery", request_ref
        )


def _seal_artifact_id() -> str:
    return artifact_id(DESIGNATOR, "proposal-seal", "proposal-seal", None)


def _next_region_ordinal(context, act_id: str) -> int:
    ordinals = [record["payload"]["attempt_ordinal"] for record in _regions_of(context, act_id)]
    return max(ordinals, default=0) + 1


def _regions_of(context, act_id: str | None = None) -> list[dict]:
    """Every region record cut so far, or only one act's."""
    return [
        context.tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
        for entry in context.tree.build_manifest(DESIGNATOR)["artifacts"]
        if entry["kind"] == "region" and (act_id is None or entry["subject_id"] == act_id)
    ]


def _open(args, registry_factory) -> tuple[StageContext, bool]:
    """Open the run on either ingress route, and say which route it was.

    The route comes from the same `context.run` the context was built on. It
    does not choose the pass; it only forbids the fixture chair on real input.
    """
    context = open_stage_context(args, DESIGNATOR, registry_factory=registry_factory)
    return context, parse_ingress_record(context.run.get("ingress")) == REAL_INGRESS


def main(registry_factory=ChairRegistry.from_toml, serving_factory=None) -> int:
    """Run through the explicitly supplied structure-chair implementation.

    `serving_factory(context, identity, tier) -> ChairClient` is the live seam;
    tests inject a fake, production gets `structure_pass.default_serving_factory`.
    The sealed catalogue, not this seam, decides which pass runs.
    """
    args = stage_parser(__doc__.splitlines()[0]).parse_args()
    context, real_input = _open(args, registry_factory)

    if args.operation == "recover":
        if not args.act:
            raise ContractError("a recovery operation must name the act it is recovering")
        if not args.recovery_request:
            raise ContractError(
                "a recovery operation must name the exact Recensor recovery request it answers"
            )
        recovery_pass(context, args.act, args.recovery_request)
        held = False
    elif args.operation == "initial":
        # The sealed serving catalogue picks the pass, never a flag or the route:
        # offline runs drive the live pass over fixture pages, and real input
        # under the fixture chair is refused.
        mode, _identity = structure_pass.structure_serving_mode(context, args)
        if mode == "fixture":
            if real_input:
                page_records(context)
                raise ContractError(
                    "a real submission may not be marked out by the fixture structure chair; "
                    "select a live catalogue. The Designator proved its Ink Map boundary and "
                    "reconciled the Exemplar filename ledger, and no proposals or holds were "
                    "fabricated"
                )
            held = initial_pass(context)
        elif mode == "live":
            held = live_initial_pass(
                context,
                structure_pass.default_serving_factory
                if serving_factory is None
                else serving_factory,
                args.placement_tier,
            )
        else:  # pragma: no cover - serving_mode_for closes the vocabulary
            raise ContractError(f"unknown serving mode {mode!r} for the structure chair")
    else:
        # The shared parser has no `choices=`, so refuse a typo here.
        raise ContractError(f"--operation {args.operation!r} is not one of 'initial' or 'recover'")

    context.seal_boundary()
    context.finish()
    return EXIT_HELD if held else EXIT_COMPLETE


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
