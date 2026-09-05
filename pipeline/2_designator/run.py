"""Designator: marks out the acts and cuts the crops. It establishes no text.

Two things it owns that nothing else may touch. **Crops** — the Recensor may
*request* a replacement region, but only this stage cuts one, so a crop always has
one author. And the **proposal seal**: an immutable record of every act this run
expects, emitted once, which becomes the downstream expected-act authority. Without
it, a later stage could only ask "did I account for the acts I happen to have seen"
rather than "did I account for the acts that were found", and an act lost between
stages would leave no hole to notice.

Every seal entry carries this stage's outcome for the act: `proposed` when it was
fully marked out, `held` when it could not be — its page unsealed, a declared
continuation whose page never sealed, or a sealed page the structure pass could
not mark out — with a `hold` artifact recording which of those it was. An act
this stage cannot mark out is a unit it still accounts for: skipped instead, it
is sealed nowhere and the run reports complete over its absence. A run that held
anything exits `EXIT_HELD`, so the same fact reaches an operator who never opens
the tree.

Regions are append-only per act, and each carries an `origin` saying what kind of
region it is: a **proposal** region is part of what was originally marked out — the
first crop, and a continuation on the next page, both — while a **recovery** region
is a recrop cut later at the Recensor's request. The distinction is load-bearing:
witnesses read the proposal regions, so ink that only a recovery uncovered was
never shown to a witness, and the Perlectio records that rather than papering over
it. A bare sequence number cannot express this, and reading one as an attempt count
made the witnesses skip the far side of a page break.

Act identity is bound to the *original proposal* and so is unchanged by any recrop;
the region identity is bound to the transform and so must change. ARCHITECTURE's
first invariant therefore falls out of the derivation rather than being maintained
by hand.

Four sibling modules in this directory do the actual marking-out: `structure.py`
finds every ink-bearing region on a decoded page, `grouping.py` assembles those
regions into acts by geometry and structural cues alone — no election among
candidates, GOVERNANCE 3's whole shape — `geometry.py` pads a structural
rectangle into the capture rectangle actually cut, and `conservation.py`
independently reconciles every page's own ink against what was actually
claimed. See their module docstrings and `HANDOFF.md` for what each publishes.

None of the four carries a geometric threshold of its own any more. A fifth,
`grouping_config.py`, reads the sealed `config/designator_grouping.toml` and
resolves its basis points against one page's own width and height; this file is
the only place that resolution happens (`_analyze_page`), and it hands the
resolved pixel integers to every call. A threshold that is a page fraction
cannot be one policy for a 200x260 fixture and a 2480x3508 scan, and a policy
sealed into a run but read by nobody is a closed window that nothing shuts.

    python pipeline/2_designator/run.py --run-root <dir> --run-id <id>
    python pipeline/2_designator/run.py ... --operation recover --act <act_id>
"""

import dataclasses
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
# This stage's own directory, so its sibling geometry/structure/grouping/
# conservation modules import as plain names. `2_designator` cannot be a
# dotted package path -- it starts with a digit -- so every module beside
# this file is loaded the way a script's own directory always is, made
# explicit here because this file is also loaded directly by tests via
# `importlib`, which does not set it automatically the way running it as
# `python run.py` would.
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
from common.contracts.stages import DESIGNATOR, EXEMPLAR, RECENSOR  # noqa: E402
from common.decoding import load_decoding_policy  # noqa: E402
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
    PAGE_RESIDUAL_REASON_CODE,
    RESIDUAL_ENUMERATION_COMPLETE,
    RESIDUAL_ENUMERATION_WITHHELD,
    SECONDARY_PROPOSER_CHAIR,
    STRUCTURE_ANSWER_KIND,
    StageContext,
    continuation_for,
    current_recovery_request,
    fallback_page_act_key,
    fixture_serving_details,
    open_stage_context,
    page_residual_act_key,
    run_stage,
    stage_parser,
    validate_serving_provenance,
)

# Fields a Designator artifact may never carry, at any depth of its payload.
# `acts/`-equivalent artifacts (`kind="act-group"`) "contain no text" per the
# spec's contracts section, and this is the schema-boundary enforcement of
# that sentence rather than a convention nobody checks: a payload carrying a
# transcription would still be geometry-shaped JSON and pass every other
# check silently. Named for *content* fields specifically -- "reason" and
# "rationale" describe a mechanism (which rule fired), never the ink itself,
# and stay allowed.
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
        # Not text, but the two words the retired picker used for the witness it
        # elected (GLOSSARY's "Retired terms"). A Designator payload that grew a
        # `chosen` or a `pivot` field would be a picker announcing itself, and
        # refusing the name at the same boundary as the text costs nothing.
        "chosen",
        "pivot",
    }
)

# How much of a page's secondary rescue pass its records actually enumerate.
# A closed pair, modelled on `common.stage`'s `RESIDUAL_ENUMERATION_*` and kept
# here rather than there because these two values are read by nothing outside
# this stage: the secondary proposer's output enters no act, no seal and no
# consumer, and a vocabulary published in `common/` that `common/` never reads
# would be an interface nobody holds.
SECONDARY_ENUMERATION_COMPLETE = "complete"
SECONDARY_ENUMERATION_WITHHELD = "withheld-page-held"

# Why an act could not be marked out. A closed vocabulary rather than free text,
# so a consumer can branch on the cause without parsing a sentence, and so a new
# cause has to be declared here rather than appearing as prose nothing expects.
HOLD_REASON_CODES = frozenset(
    {
        # The act's own page never sealed at the Exemplar door.
        "exemplar-page-not-sealed",
        # The act runs onto a page that never sealed, so it cannot be cut whole.
        "exemplar-continuation-not-sealed",
        # The page sealed, but the structure pass could not mark it out.
        "structure-pass-held",
        # The act's continuation page sealed, but its structure pass could not.
        "structure-pass-held-on-continuation",
        # The page sealed and its ink was measured, but its reconciliation found
        # more unclaimed components than the sealed grouping policy allows to be
        # minted separately, so the page itself is held as one review item. The
        # name is about the reconciliation, never about the paper: nothing here
        # says the page is speckled, foxed, or bad, only that this many
        # components were counted against this bound.
        PAGE_RESIDUAL_REASON_CODE,
    }
)


def _refuse_text_fields(value, path: str = "$") -> None:
    """Walk a payload and refuse any forbidden content-bearing key, at any depth."""
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in _FORBIDDEN_TEXT_KEYS:
                raise ContractError(
                    f"payload at {path}.{key} carries a forbidden content field; a "
                    "Designator act-group artifact carries no text at the schema boundary"
                )
            _refuse_text_fields(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _refuse_text_fields(item, f"{path}[{index}]")


# What kind of structural evidence an act-group's `detected_bounds` rests on. A
# **structural** field rather than a sentence, because a consumer must be able
# to tell a measurement from a fallback without reading a rationale string:
# `detected` means the structure pass genuinely found a region covering this act,
# and `fallback-tiles` means it found nothing on the page at all and the page was
# cut into a predetermined grid instead. In the second case `detected_bounds` is
# `null` and the two counts are zero -- recording a computed band there, with
# zero members, would be a claim about something nothing measured (GOVERNANCE 10).
#
# Three more values exist only on the live path, where the structure chair is
# the proposer and the ink scan is corroboration rather than ground truth
# (`structure_pass.model_evidence_blocks`). `shared-detection` carries a real
# detected region and its counts, like `detected`, but says the same region
# covers another proposed act too -- the merged-boundary case the fixture path
# refuses at `_claim_structural_group`, recorded here as *not* independent
# corroboration. `split-detection` is its mirror and carries null bounds and
# zero counts: two or more scanned regions each cover half of one rectangle, so
# the chair drew one act where the scan found several and no single region is
# the corroborating one. `model-only` is a rectangle no scanned region covers
# half of: null bounds and zero counts, like `fallback-tiles`, because nothing
# measured corroborates it. The last two are distinct facts -- too many regions
# and none -- and collapsing them would report a scan that found nothing where
# it found too much (GOVERNANCE 10). The fixture path never emits any of the
# three, so its records are unchanged.
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

# The rationale a fallback-tiled page's act-group carries. One string, defined
# once, because it is a statement about the mechanism and must read identically
# on a primary block and on a continuation block.
_FALLBACK_ACT_GROUP_RATIONALE = (
    "the structure pass found no ink to group on this page, so no detected region "
    "corroborates this act; the page's predetermined fallback crops are separate "
    "evidence and are not a detection"
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


# The `structure-answer` record's own closed field set, the live path's
# counterpart to `_validate_act_group_payload` above. `_refuse_text_fields`
# refuses a *known* content field by name and can do nothing about a field
# nobody has thought of yet -- which is exactly how `label` shipped a chair's
# reading in clear until this branch's reviewer found it. A closed set inverts
# that: a field added to the record without being declared here refuses at
# publication, naming itself, on the run that adds it rather than on the review
# that eventually notices. The three nested shapes are closed for the same
# reason, since a payload is only as closed as its deepest object.
_STRUCTURE_ANSWER_FIELDS = frozenset(
    {
        "schema",
        "page_id",
        "page_ordinal",
        "page_w",
        "page_h",
        "prompt_version",
        "prompt_sha256",
        "answer_schema",
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
        "act_count",
        "acts",
        "findings",
        "quantization",
        "page_text_rule",
        "decoding",
        "provenance",
    }
)
# Geometry, and both of the chair's free strings only as a digest and a length.
# `label` and `text` are absent from this set on purpose: the day either name
# reappears in the record, this refuses.
_STRUCTURE_ANSWER_ACT_FIELDS = frozenset(
    {
        "ordinal",
        "box_1000",
        "raw_bounds",
        "text_digest",
        "text_length",
        "label_digest",
        "label_length",
    }
)
_STRUCTURE_ANSWER_DECODING_FIELDS = frozenset({"policy", "temperature", "decoding_config_sha256"})
# One finding kind exists (`structure_pass.dedupe_rectangles`); a second one is
# declared here or it does not publish.
_STRUCTURE_ANSWER_FINDING_FIELDS = {"duplicate-rectangle": frozenset({"kind", "ordinals"})}


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


def _validate_structure_answer_payload(payload: object) -> None:
    """Validate the closed `structure-answer` contract before publication."""
    record = _closed_object(payload, _STRUCTURE_ANSWER_FIELDS, "structure-answer payload")
    _closed_object(
        record["decoding"], _STRUCTURE_ANSWER_DECODING_FIELDS, "structure-answer decoding block"
    )
    acts = record["acts"]
    if not isinstance(acts, list):
        raise ContractError("a Designator structure-answer payload carries no act list")
    for act in acts:
        _closed_object(act, _STRUCTURE_ANSWER_ACT_FIELDS, "structure-answer act")
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


def structure_provenance(context) -> dict:
    """Verify and record the exact chair that produced structural proposals.

    The walking skeleton derives deterministic crops, but it still exercises the
    structure-chair seam. An absent or unverifiable Designator is a refusal, never
    a cue to synthesize structure through a different role.
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


def secondary_provenance(context) -> dict:
    """Resolve and record the secondary proposer chair, absent or configured.

    Unlike `structure_provenance`, an absence here is not a refusal: spec 06's
    secondary proposer never carries crop authority, so its absence changes no
    authority decision (test 5's own words). But the role must still be
    *resolved*, every run, and the decision recorded — the shape Perlector's
    `provenance_for` already uses for its own optional chair — because
    `common/stage.py::unaddressed_chairs` only stays accurate about this role
    if something genuinely asks the registry for it. Recording nothing and
    relying on the config happening to say "absent" today is exactly the trap
    the day this roster is enabled would fall into.
    """
    resolved = context.registry.resolve(SECONDARY_PROPOSER_CHAIR)
    if isinstance(resolved, AbsentChair):
        return {
            "chair": resolved.role,
            "chair_state": "absent",
            "absence": resolved.to_record(),
            "resolved_identity": None,
            "resolved_revision": None,
            "receipt_ref": None,
            "adapter_revision": context.adapter_revision,
        }
    if not isinstance(resolved, ChairIdentity):
        raise ContractError(
            "secondary proposer resolution returned neither an identity nor an absence"
        )
    return _configured_chair_record(context, resolved)


def _read_checked_page_bytes(context, page_record: dict) -> bytes:
    """Re-read a sealed page's pixels and re-verify their digest before use.

    `_verify_exemplar_boundary` checks every sealed page's pixel digest once,
    up front, before the first region is cut. Every later read of the same
    bytes -- a structure scan, a proposal or recovery crop, a secondary
    rescue crop -- used to trust that one-time check for the rest of the run,
    with no re-verification of its own. Re-checking here closes the gap
    between that upfront check and each later use: a page's pixels changing
    on disk mid-run is caught before it is baked into sealed Designator
    evidence, rather than only the next time some downstream stage happens to
    call `verify_exemplar_crop_lineage`.
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


def page_pixels(context, page_record: dict) -> tuple[int, int, list, int]:
    """Decode one sealed page and infer its own background value.

    `common.imaging.grayscale_rows`, not `decode_grayscale_png`: the latter
    refuses by design anything this project's own encoder did not write, so
    this stage could decode a synthetic fixture page and nothing else. A sealed
    photograph would have reached here and raised a bare `ValueError` about a
    PNG colour type rather than any refusal this pipeline names.
    `grayscale_rows` is the reader `common/imaging.py` already built for
    exactly that: the same lossless fast path for our own pages, then one
    shared Pillow fallback under the same pixel bound `dimensions` already
    falls back through — a third hand-rolled decode-and-fallback pair is what
    that module exists to prevent.

    A real submission reaches this function on this branch. `main` refuses a
    real submission only under the *fixture* structure chair (SPEC_D §5); under
    a catalogue whose `designator_structure` row is served, the live pass runs
    over the sealed pages a real ingress produced, and their bytes are decoded
    here. That is the whole point of the change: a sealed photograph now
    decodes through the reader `common/imaging.py` built for it instead of
    raising a bare `ValueError` about a PNG colour type from inside a stage.
    """
    page_bytes = _read_checked_page_bytes(context, page_record)
    width, height, rows = grayscale_rows(page_bytes)
    background = structure.infer_background(width, height, rows)
    return width, height, rows, background


def _bounds_of(row: dict) -> dict:
    """The one reader of a fixture row's `x, y, w, h` fields as a `Bounds` dict.

    Same risk class as `_crop_transform`: a fifth field added to a fixture row
    and read by only some of the hand-written copies of this projection would
    change what one call site cuts or compares while the others carried on.
    `common.stage.act_bounds` is the sibling of this for a declared act row;
    this is what a continuation or recovery row uses, since neither is one.
    """
    return {key: row[key] for key in ("x", "y", "w", "h")}


def _overlap_area(a: dict, b: dict) -> int:
    x0, y0 = max(a["x"], b["x"]), max(a["y"], b["y"])
    x1 = min(a["x"] + a["w"], b["x"] + b["w"])
    y1 = min(a["y"] + a["h"], b["y"] + b["h"])
    return max(0, x1 - x0) * max(0, y1 - y0)


def _uncovered_area(target: dict, covers: list[dict]) -> int:
    """How many pixels of `target` no rectangle in `covers` already contains.

    The fold `_unclaimed_fallback_tiles` already runs, asked for an area rather
    than a tiling: `_subtract_rectangle` returns disjoint pieces, so subtracting
    each cover in turn leaves disjoint pieces covering exactly `target` minus
    the union of the covers, and their areas sum without double counting where
    two covers overlap each other. Reused rather than rewritten -- a second
    hand-written "this rectangle minus those rectangles" is one more thing to
    drift, and the recovery guard and the fallback tiling must not come to
    disagree about what "already covered" means.

    A cheaper "is `target` inside any *single* cover" test would answer a
    different question: two rectangles already cut for one act can jointly
    contain a rectangle neither contains alone, and that recrop recovers
    nothing while passing the pairwise check. `_overlap_area` above is that
    pairwise question, asked where it belongs.

    `target` must be a validated rectangle of positive area -- a degenerate one
    yields a meaningless area rather than zero, so it would pass a guard that
    only tests for zero. The one caller runs `geometry.validate_bounds` over it
    first, which is what makes the precondition true.
    """
    pieces = [dict(target)]
    for cover in covers:
        pieces = [remainder for piece in pieces for remainder in _subtract_rectangle(piece, cover)]
    return sum(piece["w"] * piece["h"] for piece in pieces)


def _coverage_on_page(records: list[dict], page_ordinal: int, page_id: str) -> list[dict]:
    """The capture rectangles those region records already cut from one page.

    The *final* `transform["bounds"]` rather than `raw_bounds`: coverage is
    about which pixels were actually cut and shown, and a proposal region's
    capture rectangle is the padded one. Measuring against `raw_bounds` would
    call the padding uncovered and let a recrop back inside it count as
    recovery.

    Scoped to one page because only pixels of *this* page can cover a rectangle
    on it. A continuation region shares the act's identity and none of its
    geometry (it is a second original region on the next page, not a later
    attempt), so counting it would measure one page's rectangle against
    another's. Both the ordinal and the page identity must match: an ordinal
    alone would agree across two runs' different pages.
    """
    return [
        record["payload"]["transform"]["bounds"]
        for record in records
        if record["payload"]["transform"]["source_page_ordinal"] == page_ordinal
        and record["payload"]["transform"]["source_page_id"] == page_id
    ]


def _body_overlap_area(group: dict, declared_bounds: dict) -> int:
    """Sum of a group's own body members' overlap with `declared_bounds`.

    Used only to break a tie in `_match_structural_group` between two groups
    whose full (body + anchor) bounds overlap `declared_bounds` identically —
    the brace-linked case, where one shared tall anchor dominates both
    groups' union bounds and makes them indistinguishable by that measure
    alone. The anchor is common evidence for both acts, so it cannot be what
    tells them apart; each group's own body text can.
    """
    body_members = group.get("body_members")
    if not isinstance(body_members, list):
        raise ContractError("a structural group carries no body_members evidence")
    return sum(_overlap_area(member["bounds"], declared_bounds) for member in body_members)


def _match_structural_group(groups: list[dict], declared_bounds: dict, what: str) -> dict:
    """The detected act-group that best overlaps a declared act's bounds.

    Grouping runs on real decoded pixels, and the synthetic pages' own
    deliberately-striped ink (`proof/synthetic_pages.py`: "distinguishable
    pixel-by-pixel from a crop of flat fill") means a detected component's
    exact bounding box is never pixel-identical to the declared rectangle —
    a striped fill's first and last rows/columns are frequently background by
    construction. Majority overlap is therefore the right correspondence
    test, not equality: if structural detection found nothing covering even
    half of where an act is declared to be, that is a real finding — the
    detector missed it — and is refused rather than silently accepted as a
    match of convenience.

    A tie in that overlap — two brace-linked groups sharing one anchor tall
    enough to dominate both groups' union bounds — is broken by
    `_body_overlap_area` rather than by input order: a strict `>` alone would
    silently attribute one act's evidence to its sibling whenever their full
    bounds happen to coincide, which is exactly the "silent substitution"
    this function's own callers document it as refusing rather than doing.
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

    `_match_structural_group` answers "which detected group best covers this
    declared act" for one act at a time, so two acts whose declared rectangles
    both fall inside a single detected group both match it — the case where the
    structure pass found one region across a boundary it did not detect (two
    entries with no margin anchor and fewer blank rows between them than the
    chain gap this page resolved from the sealed grouping policy, which used to
    be `grouping.DEFAULT_CHAIN_GAP_PX` and is now `chain_gap_bp` against this
    page's own height). Each act's `act-group` artifact would then
    record the merged rectangle as its own `detected_bounds` and the merged run
    as its own `body_member_count`, so the record claims detection corroborated
    each act separately when detection found neither. That is the "silent
    substitution" `_publish_act_group` documents itself as refusing, and it is a
    claim about what was measured that was not measured (GOVERNANCE 10).

    A brace-linked pair is not this case and stays legal: `grouping.group_page`
    returns two distinct groups sharing one anchor, so each act claims its own.
    """
    claims = analysis.setdefault("group_claims", {})
    # A digest of the detected evidence survives a copied/rebuilt group while
    # still distinguishing brace-linked siblings whose union bounds coincide.
    # Object identity does neither reliably: it changes on copy and can be
    # reused after collection.
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

    Read from the Exemplar's artifacts rather than from the fixture, so a page the
    door refused is a page this stage genuinely does not see as ink. The refused
    records still matter here: they are the evidence a hold rests on.
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
        if not isinstance(ordinal, int) or isinstance(ordinal, bool):
            raise ContractError("an Exemplar page carries no integer ordinal")
        if ordinal in records:
            raise ContractError(f"the Exemplar carries more than one outcome for ordinal {ordinal}")
        records[ordinal] = {
            "record": record,
            "relative_path": entry["relative_path"],
        }
        entries_by_ordinal[ordinal] = entry
    _verify_exemplar_boundary(context, manifest, source_rows, records, entries_by_ordinal)
    # Artifact inventories are identity-path ordered.  Identity may legitimately
    # change its lexical order when its derivation improves, but page processing
    # must retain the submission-row order used by the fixture and diagnostics.
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
        if not isinstance(ordinal, int) or isinstance(ordinal, bool):
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
    """The one construction of a crop transform: `verify_exemplar_crop_lineage`'s
    exact four fields.

    A region's identity derives from this shape, and `recovery_pass` builds one
    to predict what `region_id` a would-be duplicate recrop would carry before
    cutting it. A second hand-written copy of the shape therefore fails
    silently rather than loudly: the predicted identity would be computed for a
    transform `cut_region` could never produce, and the duplicate check would
    stop firing with neither function's code looking wrong.
    """
    return {
        "operation": "crop",
        "source_page_ordinal": page_ordinal,
        "source_page_id": page_id,
        "bounds": bounds,
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

    Split from `cut_region` so an act this stage *minted* -- one whose identity
    the fixture never declared, because the structure pass found nothing on its
    page -- is cut by exactly the same code that cuts a declared act's crop,
    rather than by a second copy of it. A crop has one author (this module's own
    docstring), and that has to stay true of a fallback crop too: the region
    record, its transform, its digest, its lineage back to the sealed Exemplar
    page and its Designator provenance are all produced here, once.

    `origin` separates two things that a bare sequence number runs together. A
    **proposal** region is part of what the Designator originally marked out —
    including a continuation on the next page, which is a second region of the
    same act rather than a later attempt at it. A **recovery** region is a recrop
    cut later at the Recensor's request. Witnesses read the proposal regions;
    ink a recovery uncovers was never shown to them. Numbering alone cannot say
    which is which, and reading it as an attempt count made this stage skip the
    far side of a page break.

    `bounds` is always the *structural* rectangle — the one act identity is
    bound to (`common/contracts/identities.py::act_bindings`) — never the
    padded one. In the synthetic walking skeleton it comes from the sealed
    fixture and is separately reconciled against the detected group; the
    unbuilt real-model path would receive the structure pass's own rectangle.
    `padding`, supplied only for a proposal cut, expands it into
    the *capture* rectangle actually cut; a recovery crop passes none, because
    a Recensor recovery request already names the exact rectangle it wants
    (structural pad and capture pad "must never be conflated" — see
    `geometry.py`'s module docstring).

    `transform` itself keeps exactly the four fields
    `common/exemplar_boundary.py::verify_exemplar_crop_lineage` has always
    required — `bounds` there is the *final* rectangle, so it alone is enough
    to reproduce this crop from the Exemplar. `raw_bounds` and `padding` are
    sibling provenance beside it, explaining how `bounds` was derived rather
    than changing what has to be reproduced; they were briefly nested inside
    `transform` and that broke the shared Exemplar-lineage boundary check,
    which reads `transform` as a closed four-field schema.
    """
    if provenance is None:
        provenance = structure_provenance(context)
    image_path = page_record["payload"]["image_path"]
    page_bytes = _read_checked_page_bytes(context, page_record)

    if padding is not None:
        page_w, page_h = dimensions(page_bytes)
        padded = geometry.apply_padding(bounds, page_w, page_h, padding)
        final_bounds = padded["bounds"]
        padding_record = {
            "applied_px": padded["applied_px"],
            "configured_bp": padded["configured_bp"],
            "config_sha256": padding["config_sha256"],
            # Travels with the evidence itself, not only with a repository file
            # a reviewer may never open: whether this padding was actually
            # calibrated for this corpus, and if not, what is known and unknown
            # about where it came from (`geometry.load_padding_config`).
            "provenance": padding["provenance"],
        }
    else:
        # A recovery crop names its own exact final rectangle (see the
        # docstring above) and so never goes through `apply_padding`, which is
        # what validates a proposal cut's bounds against the page before
        # `crop_png` ever sees them. Without an equivalent check here, an
        # out-of-page or degenerate recovery rectangle reaches `crop_png` and
        # raises a bare `ValueError` -- which `run_stage` does not catch as a
        # `ContractError` -- instead of this pipeline's own refusal shape.
        page_w, page_h = dimensions(page_bytes)
        geometry.validate_bounds(bounds, page_w, page_h, "recovery bounds")
        final_bounds = bounds
        padding_record = None

    transform = _crop_transform(page_ordinal, page_record["subject_id"], final_bounds)
    crop_bytes = crop_png(page_bytes, final_bounds)
    digest, stored = context.tree.put_blob(DESIGNATOR, crop_bytes)

    return context.publish(
        kind="region",
        subject_id=act_id,
        outcome="proposed",
        attempt=attempt_id(act_id, "crop", ordinal),
        inputs=[context.input_ref(image_path)] + ([recovery_request] if recovery_request else []),
        payload={
            "region_id": region_id(act_id, transform),
            "act_key": act_key,
            "attempt_ordinal": ordinal,
            "origin": origin,
            "transform": transform,
            "transform_digest": geometry.transform_digest(transform),
            "raw_bounds": bounds,
            "padding": padding_record,
            "image_path": stored.relative_path,
            "image_sha256": digest,
            "provenance": provenance,
        },
    )


def hold_act(
    context, act, act_id: str, blocking_ordinal: int, records, reason: str, reason_code: str
):
    """Publish the artifact that says why this act could not be marked out.

    The hold is a real record, never a skipped loop iteration. An act written
    nowhere leaves the proposal seal short, and the Armarium's conservation
    check then reconciles perfectly against a record of the loss's absence. The
    hold references the Exemplar's own page outcome as its evidence, so the
    refusal it rests on is one digest-checked hop away.

    `blocking_ordinal` is the page whose state stopped this act, and
    `reason_code` says which state that was. The two are separate fields because
    the page is not always *unsealed*: a page the structure pass could not mark
    out is sealed ink this stage still cannot bound. `reason` stays the sentence
    a reviewer reads; `reason_code` is the closed vocabulary a consumer may
    branch on without parsing prose.
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

    Spec 06 asks for this case by name: "A page the structure seat fails on is
    **held visibly** and recoverable ... never silently skipped — the old design
    made a missing witness fatal to the corpus; this one makes it a named,
    recoverable hold." The walking skeleton has no live structure model to fail,
    so the *failure* is declared by the fixture; everything downstream of it —
    the page's held status record, the hold on every act that needed that page,
    and the act's continued presence in the proposal seal — is real.

    A failure naming a page this run never sealed is ignored rather than
    invented: the page's own Exemplar refusal already accounts for it, and two
    holds for one loss would double-count it.
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
        if not isinstance(ordinal, int) or isinstance(ordinal, bool):
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
) -> dict:
    """One visible per-page outcome for the structure pass: scanned or held.

    `answers` is the live path's addition and is `None` on the fixture path,
    where this payload is byte-for-byte what it was: per page ordinal, the
    structural evidence the chair's answer established (`detected` or
    `fallback-tiles`, SPEC_D §1.4) and the digest-checked reference to the
    published `structure-answer` record, carried as the one live-only optional
    field `structure_answer_ref`. On that path `structure_evidence` is the
    answer's fact, not the ink scan's: the scan still ran and its background
    and thresholds are recorded beside it, but which crops this page got is
    decided by what the chair returned, and the field says so.

    Published for every sealed page, not only the failing ones, so "the
    structure pass ran on this page and succeeded" is a record rather than the
    absence of one. Without it a reader can only infer a page's structural
    outcome from whether crops happen to exist on it, which is exactly the
    inference GOVERNANCE 2 refuses: a page nothing marked out and a page nothing
    tried to mark out would look identical.

    `state` says "scanned", never "marked-out": GLOSSARY defines Designator as
    the stage that "marks out" acts, and the Recensor separately reports
    whether an act was actually marked out on a page. A page can be `scanned`
    by the structure pass and still have nothing marked out on it (no declared
    act touches it) -- the two are different facts, and reusing the Designator's
    own glossary verb for this field's success state would make them read as
    the same one.

    `background_source` and `structure_evidence` are this stage's own audit
    trail for *how* the page was read, published rather than computed and
    dropped. `_analyze_page` had both facts in an in-process dict that nothing
    ever wrote down, so a page whose ink threshold came from somewhere other
    than its own modal pixel, and a page cut into a predetermined grid because
    nothing was found on it, were indistinguishable on disk from an ordinary
    scan. Both are null on a page held before it was analysed at all: the
    structure pass produced no background and no evidence there, and saying
    "inferred" of a pass that never ran would be the same defect the fields
    exist to close.

    `page_width`/`page_height` and `resolved_thresholds` are the same kind of
    fact one step further out: not how the page was read, but *what geometry the
    run executed on it*. The sealed `designator-grouping` policy is expressed in
    basis points of a page dimension, so the pixel numbers this page actually
    ran under are a function of the policy and of this page's own size, and
    until now they existed only inside `_analyze_page`'s cache. A calibration
    session reading the tree back could recover the policy from the seal and the
    page from its bytes and re-derive them -- and re-derivation is the thing
    that goes quietly wrong when a resolution rule changes. Publishing what
    executed costs eight integers and two dimensions on a record already being
    written, which is GOVERNANCE 6 applied to these values (SPEC_C 4.2). It is a
    recording, never a decision: nothing reads these back to choose anything,
    and no threshold is computed here that `_analyze_page` did not already
    resolve for this page.

    The whole of `GroupingThresholds` is published rather than a chosen subset.
    `max_residual_components` is a count rather than a pixel threshold, but it
    is part of what this page ran under, and a subset boundary would be a second
    judgment about which of one dataclass's fields matter -- kept in step with
    the dataclass instead, so a field added there cannot silently stop being
    recorded.

    Returns each page's own published status reference, because the
    page-fallback act minted below has to name the record that independently
    says its premise is true (`common/stage.py::_verify_page_fallback_act_row`).
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
            # The sealed page record's own subject, not a second derivation of
            # it from the fixture. They are the same string on a fixture run --
            # both are `page_id` over the source digest -- but only one of them
            # exists on a real page, and this record already carried the sealed
            # id in its payload while its subject came from the fixture. Two
            # spellings of one identity in one artifact is how they drift.
            subject_id=pages[ordinal]["subject_id"],
            outcome="held" if reason_code else "proposed",
            inputs=[context.input_ref(records[ordinal]["relative_path"])],
            payload={
                "page_id": pages[ordinal]["subject_id"],
                "page_ordinal": ordinal,
                "state": "held" if reason_code else "scanned",
                "reason_code": reason_code,
                "background_source": analysis["background_source"] if analysis else None,
                "structure_evidence": evidence,
                # Null on a page held before the structure pass analysed it,
                # for the same reason the two fields above are: this record
                # answers for the structure pass, that pass resolved and ran
                # nothing here, and naming the numbers it *would* have run under
                # would be a resolution reported as an execution. It is not a
                # claim that nothing ran on the page at all -- conservation
                # scans a structure-held page and publishes the two thresholds
                # its own reconciliation executed under, on its own record.
                "page_width": analysis["width"] if analysis else None,
                "page_height": analysis["height"] if analysis else None,
                "resolved_thresholds": (
                    dataclasses.asdict(analysis["thresholds"]) if analysis else None
                ),
                "provenance": provenance,
                **live_fields,
            },
        )
        published[ordinal] = context.input_ref(result.relative_path)
    return published


def _analyze_page(
    cache: dict, context, ordinal: int, page_record: dict, grouping_policy: dict
) -> dict:
    """Structure-pass and grouping results for one sealed page, computed once.

    This is the genuinely visual half of "may use textual as well as visual
    cues" (ARCHITECTURE), run for real on the page's own decoded pixels —
    never assumed, never a fixture value standing in for it.

    A page whose background cannot be inferred is the one case where that pass
    cannot run at all, and the honest answer is two facts, not one. **The page
    is still cut**: the predetermined grid below covers it and its crops go
    downstream, because Tyrel ruled on 2026-08-11 that "everything gets read
    every time nothing gets pulled out or held" and this stage's single
    threshold is the weakest instrument in the pipeline. **And its ink is not
    measured**: `background` stays `None`, no scan runs, and
    `_publish_conservation_and_secondary` records a reconciliation that could
    not happen rather than one over a substituted divider. The two are separable
    and were previously conflated by taking the page's own mean as a stand-in,
    which is a guess wearing a measurement's name (see
    `structure.BackgroundInferenceRefusal` for what that guess actually did to
    an inverted scan).

    **Every geometric threshold this page runs under is resolved here**, from
    the run's own sealed `designator-grouping` policy and *this page's* own
    width and height (SPEC_C 2, "Where resolution happens"). One page's numbers
    are not another's: the six page-fraction fields are basis points, so a
    200x260 fixture and a 2480x3508 scan resolve the same sealed policy to
    different pixel counts, which is the whole reason the policy is expressed
    as fractions rather than as pixels. The resolved thresholds are cached
    beside the pixels because every later consumer of this analysis — the
    secondary scan, the continuation-candidate geometry, the conservation
    reconciliation and its residual bound — must run under the same page's
    numbers, and re-resolving them at each call site is how two of them would
    come to disagree.
    """
    if ordinal not in cache:
        try:
            width, height, rows, background = page_pixels(context, page_record)
            background_source = "inferred-modal"
        except structure.BackgroundInferenceRefusal:
            page_bytes = _read_checked_page_bytes(context, page_record)
            width, height, rows = grayscale_rows(page_bytes)
            background = None
            background_source = "not-inferable"
        thresholds = grouping_config.resolve_thresholds(grouping_policy, width, height)
        components = (
            []
            if background is None
            else structure.primary_scan(
                width,
                height,
                rows,
                background=background,
                gap_tolerance_px=thresholds.gap_tolerance_px,
            )
        )
        groups = grouping.group_page(
            components,
            width,
            height,
            margin_px=thresholds.margin_px,
            chain_gap_px=thresholds.chain_gap_px,
            anchor_reach_px=thresholds.anchor_reach_px,
            brace_min_height_px=thresholds.brace_min_height_px,
        )
        # **A page the structure pass found nothing on is cut anyway.** Tyrel
        # ruled 2026-08-11: "If the designator sees no text it should default to
        # predetermined crops with a small margin of overlap and send the crops
        # down stream to be read by everything. If all the witnesses and the
        # perlector see no text on any of the crops then it's likely a true
        # blank." Deciding blankness here, from one threshold on one page, is
        # deciding it with the weakest instrument in the pipeline; the witnesses
        # and the Perlector are the strong ones and they only get a say if the
        # crops reach them.
        #
        # `structure_evidence` is the whole difference, kept as a field rather
        # than as a sentence: these tiles are a grid computed from the page's
        # own dimensions, not a detection, and `_publish_page_fallback` is what
        # turns them into crops that actually go downstream. Grouping's output
        # being *used as match candidates* is not the same as its output being
        # cut, and this record is what keeps the two apart everywhere below.
        structure_evidence = "detected"
        if not groups:
            structure_evidence = "fallback-tiles"
            groups = grouping.fallback_tiles(
                width,
                height,
                bands=thresholds.fallback_bands,
                overlap_px=thresholds.fallback_overlap_px,
            )
        cache[ordinal] = {
            "width": width,
            "height": height,
            "rows": rows,
            "background": background,
            "background_source": background_source,
            "groups": groups,
            "structure_evidence": structure_evidence,
            "thresholds": thresholds,
            # The raw scanned components, kept beside the groups for the live
            # path's ink tripwire (`structure_pass.touches_ink`): a chair
            # rectangle is tested against the ink the scan actually counted,
            # not against the grouped bands. In-process only; nothing publishes
            # it, so no fixture record changes.
            "components": components,
        }
    return cache[ordinal]


def _structural_evidence_block(
    analysis: dict, declared_bounds: dict, act_key: str, what: str
) -> dict:
    """The four fields that say what structural evidence stands behind one rectangle.

    One builder for the primary block and the continuation block, because the
    distinction between a detected region and a predetermined grid has to read
    identically in both. On a detected page this matches the declared rectangle
    against the groups the structure pass actually found, claims the matched
    group for this act, and refuses when nothing covers half of it. On a
    fallback-tiled page there is nothing to match against and it says so.
    """
    if analysis["structure_evidence"] == "fallback-tiles":
        return {
            "structure_evidence": "fallback-tiles",
            "detected_bounds": None,
            "body_member_count": 0,
            "anchor_count": 0,
            "rationale": _FALLBACK_ACT_GROUP_RATIONALE,
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

    Every field here is geometry or a code-generated rationale describing
    which grouping rule fired; `_refuse_text_fields` is the schema-boundary
    proof that nothing else got in. This artifact is evidence *for* the act
    the fixture already bound identity to (`act_bounds`); it never becomes the
    source of that identity, so a grouping disagreement is a refusal
    (`_match_structural_group`), never a silent substitution.

    **A fallback-tiled page corroborates nothing, and says so structurally.**
    The predetermined bands cover the whole page by construction, so matching a
    declared act against one would always succeed -- which would silently
    disable `_match_structural_group`'s missed-act refusal on exactly the pages
    where the structure pass found nothing, and would publish a computed band as
    `detected_bounds` with zero members. Both are claims about something nothing
    measured. So the fallback branch below never consults the grid at all: it
    records `structure_evidence="fallback-tiles"` and null detected bounds, and
    the refusal stays live on every page where detection actually ran.
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
        # Recorded, not gated: the synthetic fixture's continuation rectangles
        # do not touch either page's edge (`proof/synthetic_pages.py` says
        # linking them is "a different unit's job"), so honest geometry here
        # is usually `False` for this fixture even though the continuation
        # itself is genuine. Forcing a match would be fabricating corroboration
        # a real page's geometry has not offered. A page cut into fallback tiles
        # offers none at all, so the check is not run over a grid: its bands
        # touch both page edges by construction and would corroborate every
        # continuation ever declared.
        corroborated = (
            analysis["structure_evidence"] == "detected"
            and continuation_analysis["structure_evidence"] == "detected"
            and grouping.find_continuation_candidate(
                analysis["groups"],
                analysis["height"],
                continuation_analysis["groups"],
                # Each page's own resolved edge reach. One value serving both
                # silently assumed the two pages shared a height, which a real
                # corpus does not guarantee and a mixed-format register breaks.
                edge_reach_a_px=analysis["thresholds"].page_edge_reach_px,
                edge_reach_b_px=continuation_analysis["thresholds"].page_edge_reach_px,
            )
            is not None
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


def _claimed_regions_by_page(context) -> dict[int, list[dict]]:
    """Every proposal region's final (capture) bounds cut so far, by page ordinal.

    Read in one pass over the stage's artifacts rather than once per page. Each
    pass walks the whole tree and re-reads every region record, so asking per
    page made conservation's own input cost pages x regions — quadratic in an
    ordinary book, on evidence that does not change between pages.
    """
    claimed: dict[int, list[dict]] = {}
    for entry in context.tree.build_manifest(DESIGNATOR)["artifacts"]:
        if entry["kind"] != "region":
            continue
        record = context.tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
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

    A candidate wholly contained by one claim is already inside ordinary
    coverage and is not a find; merely touching one does not erase the part
    outside it. Each surviving candidate is returned with the number of
    already-claimed acts it touches, because a candidate reaching two of them
    at once is the P0-incident shape and a reviewer has to be able to see that
    on the record rather than infer it from geometry.

    That count is recorded, never acted on — including by refusing. Two acts'
    *padded* claims can abut at a single row, so one ordinary pen mark in the
    blank band between two entries reaches both; raising here aborts
    `initial_pass` before the proposal seal is written, and every act on every
    page loses its denominator over a review-only box. Spec 06's test 5 wants
    the opposite — "removing the proposer changes no authority decision (it
    adds recall, never verdicts)" — and a held, flagged, page-subject rescue
    crop that enters no act and no seal decides nothing either way.
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

    Split from `_publish_conservation_and_secondary` so it can be exercised
    against a hand-fed page analysis without needing to also be the first
    (and, since a conservation record is a once-only artifact, therefore the
    only) publisher of that page's conservation record.

    **Bounded per page, the same way and for the same reason the residual
    enumeration is.** This is the stage's other per-page enumeration, and it is
    the more expensive one: each rescue cuts a PNG blob as well as two records.
    A page speckled enough to trip `max_residual_components` would trip this
    too, and without a bound it would reopen the unopenable run
    `_publish_page_residual_hold` exists to prevent, by the one route that bound
    does not cover. Past `max_secondary_proposals` the page's secondary pass
    becomes one held record naming the count, the bound and the sealed policy
    digest it was judged against, and no crop is cut. The candidates are
    counted, never filtered: `structure.secondary_scan` still returns
    everything it finds and `_secondary_rescue_candidates` still reduces it by
    geometry alone, so what the bound changes is how many separate review items
    one page mints -- never what was measured (GOVERNANCE 10).

    `secondary_enumeration` is on both shapes as a closed pair, exactly as
    `residual_enumeration` is on every conservation record: "this page had no
    unclaimed candidate" and "this page's candidates were counted and not cut"
    are two different facts, and a consumer that cannot tell them apart reads
    the second as the first.
    """
    if secondary["chair_state"] != "configured":
        # Nothing to add: the secondary proposer is explicitly absent, and its
        # absence changes no authority decision here either -- there is simply
        # no additive recall pass to run.
        return False
    validate_serving_provenance(
        context,
        secondary,
        producer_stage=DESIGNATOR,
        require_receipt=True,
    )
    if analysis["background"] is None:
        # The secondary scan is the same threshold at a more sensitive margin,
        # so a page with no inferable background gives it nothing to be
        # sensitive about. Running it at a substituted divider would publish
        # rescue crops over paper, which is the additive-recall pass producing
        # noise rather than recall.
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
        # Today's secondary scan derives every candidate from the page's own
        # pixel scan, so it is in-page by construction. A real detector chair
        # would not carry that guarantee, and without this check its box would
        # reach `crop_png`'s bare `ValueError` -- which `run_stage` does not
        # catch as a `ContractError` -- instead of this pipeline's own refusal
        # shape, exactly the defect class `bf6a716` closed for the recovery path.
        geometry.validate_bounds(
            candidate["bounds"], analysis["width"], analysis["height"], "secondary candidate bounds"
        )
        overlap_count = rescue_row["overlapping_claimed_act_count"]
        subject = f"{page_record['subject_id']}-secondary-{index}"
        transform = _crop_transform(ordinal, page_record["subject_id"], candidate["bounds"])
        crop_bytes = crop_png(page_bytes, candidate["bounds"])
        digest, stored = context.tree.put_blob(DESIGNATOR, crop_bytes)
        rescue_payload = {
            "page_ordinal": ordinal,
            "pixel_count": candidate["pixel_count"],
            "origin": "secondary-proposer",
            "padding": None,
            "authoritative": False,
            "authority_effect": "review-only",
            "overlapping_claimed_act_count": overlap_count,
            "transform": transform,
            "transform_digest": geometry.transform_digest(transform),
            "image_path": stored.relative_path,
            "image_sha256": digest,
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

    The page rectangle, the count, the bound and the sealed grouping digest, and
    no crop at all. Cutting them would be the unreadable run; dropping them
    silently would be worse, so the count stands on the record and the page is
    one review item instead of that many.

    This mints no act and enters no seal, exactly as the enumerated rescues it
    replaces do not: the secondary proposer adds recall for a reviewer's eye and
    decides no authority either way (spec 06 test 5), so a bound on how it is
    presented cannot change an authority outcome either. What it does do is hold
    the run, which is the same visible consequence the enumerated shape has.
    """
    payload = {
        "page_ordinal": ordinal,
        "page_bounds": {"x": 0, "y": 0, "w": analysis["width"], "h": analysis["height"]},
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


def residual_act_key(page_ordinal: int, index: int) -> str:
    """The human-readable label for a conservation-residual act.

    This string is for a reviewer's eye and this stage's own duplicate-act-key
    refusal in `common.stage.expected_acts`; it is not what keeps a residual's
    identity from colliding with a real proposal's. The closed ``residual`` act
    class does that by construction, whatever a fixture author happens to name
    their own acts, while ``index`` stays presentation order only.
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

    The residual was never a structural proposal: structural grouping claimed
    no region over this ink at all, so it could never have been witnessed or
    read. It is therefore `held` from the moment it exists — the same
    terminal shape an unsealed page already produces, extended to ink no
    structural pass claimed rather than to a page that never sealed.

    The closed ``residual`` act class gives it an identity that cannot collide
    with any real proposal's, present or future, by construction rather than by
    convention: a proposal and a residual over the identical rectangle derive
    different `act_id`s because the class is part of the binding. The hold
    record carries the exact bounds a reader needs to recompute that identity,
    because `common.stage._verify_minted_act_rows` does exactly that
    recomputation — every act beyond the fixture's own denominator must prove
    itself against evidence, never merely appear because this stage's own seal
    says so.
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

    Every residual is minted, never only the high-priority ones:
    `review_priority` orders which residual a reviewer looks at first and must
    never decide whether a region exists in the accounting at all. The
    `residual_components` list already arrives in the deterministic (top, then
    left) order `conservation.reconcile` produces, so
    `index` orders the evidence and names the residual for a reviewer; it is a
    position in a list, so it stays out of identity entirely.
    What separates two residuals on one page is therefore their rectangle
    alone, and two connected components can in principle share a bounding box —
    two strokes of one cross, laid down so that neither touches the other. That
    would mint one act over two pieces of ink, and GOAL 1 puts a lost act above
    every other cost, so it is refused by name here instead.
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
            {
                "act_id": minted_act_id,
                "act_key": residual_act_key(page_ordinal, index),
                "page_id": page_id,
                "page_ordinal": page_ordinal,
                "has_continuation": False,
                "outcome": "held",
                "evidence": [context.input_ref(hold.relative_path)],
            }
        )
    return rows


def _publish_page_residual_hold(
    context,
    page_id: str,
    page_ordinal: int,
    page_bounds: dict,
    *,
    # Keyword-only from here: the count and the bound are both plain integers on
    # the same call, and a hold that swapped them would name a policy the run
    # never applied while every type check still passed.
    residual_component_count: int,
    max_residual_components: int,
    grouping_config_sha256: str,
    conservation_ref: dict[str, str],
) -> dict:
    """Hold a whole page as one review item in place of its residual components.

    The alternative this replaces is not "enumerate them anyway"; it is a run
    the only surface a person reads refuses to open. `operations/operator/review.py`
    caps the console at `MAX_REVIEW_ITEMS` and refuses the run by name past it,
    so one page reconciling tens of thousands of unclaimed components makes the
    *whole* run unreadable — every other page's findings included. One held
    page is worse than N held acts for a page with three of them and better
    than N held acts for a page with sixty thousand, and
    `max_residual_components` is the sealed line between the two.

    **The bound is per page, and the console's ceiling is per run.** What this
    buys is that no single page can make a run unopenable on its own; it is not
    a guarantee that the run stays under `MAX_REVIEW_ITEMS`, and nothing here
    claims one. Thirty pages each just inside the bound still carry the run past
    the console's 50,000 items, and this stage cannot honestly refuse them for
    it: the queue an operator opens is assembled in the Armarium's export from
    every stage's review items, so a total counted here would be a fraction of
    the run's presented as the whole of it — a claim past what is measured
    (GOVERNANCE 10). The run-wide ceiling stays where it is enforced, at the
    console, refused by name against the queue it actually reads rather than by
    exhaustion. `HANDOFF.md` carries this as a named remainder rather than as a
    thing this bound does.

    **Nothing leaves the measurement.** `residual_pixel_count` on this page's
    conservation record is the same integer it would have been, the exact
    identity `claimed + residual == total` is still published, and the count of
    components is on both this hold and that record. What is not carried is the
    per-component rectangle list, which stays recomputable from the sealed page
    bytes and the sealed conservation policy — the same reasoning the pipeline
    already applies to the exact image a model was shown. That is a judgment
    with a cost, not a free one, and the cost is named here so nobody has to
    infer it: on a held page a reviewer cannot open this artifact and read off
    where the unclaimed ink was.

    **The reason code names the reconciliation, never the paper.** A page here
    is not "too speckled" and not "bad"; its conservation reconciled N
    components against a bound of M. If the structure pass is what is wrong —
    and on a real register today it very likely is — then a run where every page
    carries one of these is the legible first-run signal that says so, which is
    a finding delivered on run one rather than run ten. `HANDOFF.md` carries
    the retirement condition in full: if a real structural Designator lands and
    real pages still trip this bound, the bound is measuring the wrong thing and
    must be revisited rather than raised. A threshold with no stated falsifier
    is how an instrument becomes furniture.

    Exactly one input, and it is this page's own `conservation` record. That
    record is the independent premise — it is what says the count exceeded the
    bound — exactly as `structure-status` is the premise for a page-fallback
    act, so no second artifact is minted to say what one already says.
    `common/stage.py::_verify_page_residual_act_row` recomputes every field
    below rather than reading it: the page rectangle from the sealed page
    bytes, the identity from the reserved class and that rectangle, the bound
    against the run's own sealed `designator-grouping` digest, and the count
    against the conservation record reached through the digest-checked hop.
    """
    minted_act_id = derive_minted_act_id(page_id, "page-residual", page_bounds)
    act_key = page_residual_act_key(page_ordinal)
    payload = {
        "act_key": act_key,
        "page_id": page_id,
        "page_ordinal": page_ordinal,
        "page_bounds": page_bounds,
        "residual_component_count": residual_component_count,
        "max_residual_components": max_residual_components,
        "grouping_config_sha256": grouping_config_sha256,
        "blocking_page_ordinal": page_ordinal,
        "reason_code": PAGE_RESIDUAL_REASON_CODE,
        "reason": (
            f"this page's conservation reconciled {residual_component_count} residual "
            f"components against the sealed bound of {max_residual_components}, so the page "
            "is held as one review item instead of that many held acts; the components were "
            "counted, not listed, and remain recomputable from the sealed page bytes under "
            "the sealed conservation policy"
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
    return {
        "act_id": minted_act_id,
        "act_key": act_key,
        "page_id": page_id,
        "page_ordinal": page_ordinal,
        "has_continuation": False,
        "outcome": "held",
        "evidence": [context.input_ref(hold.relative_path)],
    }


def _residual_ink_fraction_bp(residual_pixel_count: int, total_ink_pixel_count: int) -> int:
    """How much of this page's ink no crop claimed, in integer basis points.

    Recorded, gating nothing. The bound above is on *cardinality*, deliberately:
    a page carrying one 30%-ink smudge is one held act and correct, while a page
    of sixty thousand specks totalling 3% is the failure, and a fraction gate
    holds the first and passes the second — exactly backwards. This number is
    still worth an integer, because it is the figure a calibration session will
    want beside the count when it comes to ask whether 2000 was the right line.

    The basis is this page's own measured ink (`residual / total`), not its
    area: every operand is already on the same record, so a reader recomputes
    it from the three integers printed beside it rather than having to fetch the
    page. Round-half-up, integer arithmetic throughout — a float reaching a
    canonical payload is a determinism defect. A page with no ink at all
    reconciles to zero rather than dividing by it.
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


def _unclaimed_fallback_tiles(tiles: list[dict], claimed: list[dict]) -> list[dict]:
    """Clip fallback bands to pixels no declared proposal region already owns."""
    unclaimed = []
    for tile in tiles:
        pieces = [dict(tile["bounds"])]
        for claim in claimed:
            pieces = [
                remainder
                for piece in pieces
                for remainder in _subtract_rectangle(piece, claim["bounds"])
            ]
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


_FALLBACK_REASON_FIXTURE = (
    "the structure pass found no ink to group on this page, so the page is cut into "
    "predetermined overlapping crops and sent downstream to be read rather than being "
    "called blank here; blankness is proved by the witnesses and the Perlector, which "
    "only get a say if the crops reach them"
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
    reason: str = _FALLBACK_REASON_FIXTURE,
) -> dict | None:
    """Cut the predetermined crops over a page the structure pass found nothing on.

    This is the half of Tyrel's 2026-08-11 ruling that `grouping.fallback_tiles`
    alone never delivered. The grid existed and was handed to
    `_match_structural_group` as match candidates; no tile ever became a crop, so
    a sealed page with no found ink and no declared act still sent *nothing*
    downstream — which is the outcome the ruling exists to forbid: "If the
    designator sees no text it should default to predetermined crops with a small
    margin of overlap and send the crops down stream to be read by everything. If
    all the witnesses and the perlector see no text on any of the crops then it's
    likely a true blank."

    **One minted act per page, one proposal region per tile**, rather than one
    act per tile. The structure pass found nothing, so it has no opinion at all
    about how many acts are on this page and must not manufacture one by
    counting bands: what it can honestly say is "here is a page, and here is
    every part of it, cut so a reader can be shown all of it". Every consumer
    already reads *all* of an act's proposal regions — the Attestatores witness
    each one and the Perlector reads through every region of the act — so one
    act with N regions is a page delivered whole, and N acts would be an act
    count invented from a grid.

    Declared proposal crops on the same page are subtracted from these tiles
    before publication. Together the declared regions and the remaining tile
    pieces still cover the whole page, while no pixel is read under two act
    identities. If declared crops already cover the whole page, there is no
    uncovered tile and therefore no second act to mint.

    The act is `proposed`, not `held`. A held act is terminal and is never read
    (`recovery_pass`, and `_publish_residual_holds`'s own "never witnessed and
    never read"), and crops nobody reads are exactly what the ruling says not to
    produce. Its identity binds the closed ``page-fallback`` class and the full
    page rectangle, and the record published here is what
    `common/stage.py::_verify_page_fallback_act_row` recomputes that identity
    from — together with the page's own `structure-status`, which independently
    states the premise that the structure pass fell back to tiles here.

    A fallback tile carries `padding: null`, like a recovery crop and for the
    same reason: the tile *is* the final rectangle. It was computed from the
    page's own dimensions with this page's own resolved overlap already built
    in (`fallback_overlap_px`), so expanding it again by the
    capture padding would conflate a structural pad with a capture pad, which
    `geometry.py`'s docstring says must never happen.
    """
    page_id = page_record["subject_id"]
    page_bounds = {"x": 0, "y": 0, "w": analysis["width"], "h": analysis["height"]}
    act_id = derive_minted_act_id(page_id, "page-fallback", page_bounds)
    act_key = fallback_page_act_key(ordinal)
    tiles = _unclaimed_fallback_tiles(analysis["groups"], claimed)
    if not tiles:
        return None
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
    return {
        "act_id": act_id,
        "act_key": act_key,
        "page_id": page_id,
        "page_ordinal": ordinal,
        "has_continuation": False,
        "outcome": "proposed",
        "evidence": sorted(evidence, key=lambda reference: reference["relative_path"]),
    }


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

    Conservation rescans this page's own pixels rather than trusting what
    grouping already claimed to have found — closing the gap an independent
    audit of the old pipeline named precisely: its conservation proved
    coverage of units a structural model had already emitted, and could not
    prove the model had not missed ink entirely. This can.

    Returns the expected-act row for every residual this page's reconciliation
    found, so `initial_pass` can extend the proposal seal's own denominator
    with them. A residual left inside the conservation artifact alone is an
    audit-trail entry nothing downstream reads; as a seal row it is a unit this
    run accounts for exactly as it accounts for a page that never sealed.

    **A page whose background could not be inferred reconciles nothing**, and
    says so instead of reporting counts taken at a substituted threshold. There
    is no honest divider to rescan at: the page's own mean classifies dark paper
    as ink on an inverted scan, so the "reconciliation" would report four fifths
    of the page as unclaimed ink and mint a held act over the background. The
    record is published either way, with `ink_measurable` saying which kind it
    is, because a page with no conservation record at all is the silent gap this
    artifact exists to close. Its crops were still cut and still go downstream
    (`_publish_page_fallback`); what is refused is the claim to have measured
    them.

    **The bound is applied here, at the publication boundary, and never inside
    `conservation.reconcile`.** That module's own docstring states the rule the
    separation exists for — the instrument may not constrain what it measures —
    so `reconcile` keeps returning the complete truth including all sixty
    thousand components, and the *policy* decision about how many of them become
    separate review items is taken after it has spoken. A pre-check would be
    worse still: it would have to estimate the count without labelling, and an
    estimate published as a bound is the defect this exists to close.

    **What ran is on the record that ran it.** `page_width`, `page_height` and
    `reconciliation_thresholds` say what geometry this reconciliation executed
    under. They are here rather than only on `structure-status` because this
    scan runs on every sealed page including one the structure pass was held on
    before it was ever analysed -- and that page's status record says null for
    the structure pass's own geometry, correctly, while this one had resolved
    integers in hand and used them. A null beside a computation is the shape
    GOVERNANCE 10 refuses. Only the two thresholds `conservation.reconcile` was
    actually given are published, and they are null on an unmeasurable page,
    where no reconciliation ran to have executed under anything.

    `residual_enumeration` says which of the two happened, on every record, as a
    closed value. It is the field that lets a consumer tell "this page had no
    unclaimed ink" from "this page's unclaimed ink was counted and not listed" —
    two states an absent or empty `residual_components` cannot distinguish, and
    reading one as the other is a page of lost ink reported as a clean one. On a
    withheld page the key is *omitted* rather than emptied, so every consumer
    that reads it as a list fails loudly instead of reading absence as none.
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
    max_residual_components = thresholds.max_residual_components
    # An unmeasured page never withholds. It enumerated nothing because there
    # was no threshold to enumerate against, not because a bound stopped it, and
    # holding it for over-bound scatter would name a reconciliation that never
    # ran. Its own `ink_measurable: false` is the fact that page carries.
    withheld = measurable and component_count > max_residual_components
    conservation_payload = {
        "page_ordinal": ordinal,
        # Conservation owns an independent page scan.  Its threshold basis
        # belongs on this record even when the structure pass was held before
        # analysis and its separate structure-status therefore says null.
        "background_source": analysis["background_source"],
        "background_value": analysis["background"],
        "ink_measurable": measurable,
        # The geometry this reconciliation actually executed on, on the record
        # of the computation that executed it. This page's `structure-status`
        # publishes the whole resolved set when the structure pass ran on it and
        # null when the page was held before that pass -- and conservation runs
        # on a held page all the same, so without these two lines a
        # structure-held page carried a null for "what geometry ran here" beside
        # a reconciliation that had just run under resolved integers. The pair
        # below is exactly what `conservation.reconcile` was given, never the
        # whole `GroupingThresholds`: this record answers for its own
        # measurement and not for a pass it did not make.
        "page_width": analysis["width"],
        "page_height": analysis["height"],
        "reconciliation_thresholds": {
            "gap_tolerance_px": thresholds.gap_tolerance_px,
            "review_priority_min_dimension_px": thresholds.review_priority_min_dimension_px,
        }
        if measurable
        else None,
        "reason": _conservation_reason(measurable, withheld, component_count),
        "total_ink_pixel_count": result["total_ink_pixel_count"],
        "claimed_pixel_count": result["claimed_pixel_count"],
        "residual_pixel_count": result["residual_pixel_count"],
        # The count is published whether or not the list is, and on an
        # enumerated page it is exactly `len(residual_components)` -- the number
        # a reviewer is told is the number the list beside it supports, which
        # `common/stage.py` recomputes rather than believes. It is an integer on
        # an unmeasurable page too, and zero there is literally true: nothing
        # was enumerated. `ink_measurable` is the field that says nothing was
        # measured, and saying it twice with a null would only cost the
        # equality that consumer checks.
        "residual_component_count": component_count,
        "residual_ink_fraction_bp": None
        if not measurable
        else _residual_ink_fraction_bp(
            result["residual_pixel_count"], result["total_ink_pixel_count"]
        ),
        # The sealed bound this page was judged against, published on every
        # record rather than only on the held ones: it is the policy that was in
        # force, not a measurement, so a page that stayed within it should say
        # what it stayed within.
        "max_residual_components": max_residual_components,
        "residual_enumeration": RESIDUAL_ENUMERATION_WITHHELD
        if withheld
        else RESIDUAL_ENUMERATION_COMPLETE,
    }
    if not withheld:
        conservation_payload["residual_components"] = components
    _refuse_text_fields(conservation_payload)
    published = context.publish(
        kind="conservation",
        subject_id=page_id,
        outcome="held" if (withheld or not measurable) else "proposed",
        inputs=[context.input_ref(page_record["payload"]["image_path"])],
        payload=conservation_payload,
    )
    secondary_held = _publish_secondary_proposals(
        context, ordinal, page_record, analysis, claimed, secondary, grouping_policy
    )
    conservation_ref = context.input_ref(published.relative_path)
    if withheld:
        # No per-component act is minted for a withheld page. Minting both the
        # page and its components would account for the same unlisted ink twice,
        # and minting the components alone is the unopenable run the bound
        # exists to prevent.
        rows = [
            _publish_page_residual_hold(
                context,
                page_id,
                ordinal,
                {"x": 0, "y": 0, "w": analysis["width"], "h": analysis["height"]},
                residual_component_count=component_count,
                max_residual_components=max_residual_components,
                grouping_config_sha256=grouping_policy["config_sha256"],
                conservation_ref=conservation_ref,
            )
        ]
    else:
        rows = _publish_residual_holds(context, page_id, ordinal, components, conservation_ref)
    return rows, secondary_held


def _conservation_reason(measurable: bool, withheld: bool, component_count: int) -> str | None:
    """The one sentence a reviewer reads about why this record is not ordinary.

    Three states, one field, because they are mutually exclusive and a reader
    asking "why is this page not simply reconciled" wants one answer: the ink
    could not be measured at all, the components were measured and not listed,
    or neither and there is nothing to say.
    """
    if not measurable:
        return (
            "this page's background could not be inferred, so it has no threshold to "
            "separate ink from paper and its ink was not measured; a count taken at a "
            "substituted divider would be a guess reported as a measurement"
        )
    if withheld:
        return (
            f"this page's ink was measured in full and reconciled to {component_count} "
            "residual components, more than the sealed grouping policy allows one page to "
            "enumerate, so the components were counted and not listed and the page is held "
            "as a single review item; no ink left the accounting and the per-component "
            "rectangles remain recomputable from the sealed page bytes"
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

    if page_ordinal not in pages:
        # The act's own page never sealed. It cannot be marked out, and it
        # may not disappear either: it is held, with the reason on record,
        # and no region of it — not even a sealed continuation — is cut. An
        # orphan far-side crop would be evidence of an act nothing accounts
        # for.
        outcome = "held"
        hold = hold_act(
            context,
            act,
            act_id,
            page_ordinal,
            records,
            f"page {page_ordinal} was not sealed, so the act could not be marked out",
            "exemplar-page-not-sealed",
        )
        evidence.append(context.input_ref(hold.relative_path))
    elif page_ordinal in failures:
        # The page sealed — its ink is real and reachable — but the structure
        # pass could not mark it out. That is not a blank page and not a page
        # to skip: the act stays in the denominator, held, with the structural
        # reason named. Its ink still reaches the accounting, as conservation
        # residual, because no crop claims any of it.
        outcome = "held"
        hold = hold_act(
            context,
            act,
            act_id,
            page_ordinal,
            records,
            f"the structure pass could not mark out page {page_ordinal} "
            f"({failures[page_ordinal]}), so the act could not be bounded",
            "structure-pass-held",
        )
        evidence.append(context.input_ref(hold.relative_path))
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

        # An act that runs over the page break gets a second region of the
        # SAME act. A continuation that became its own act would quietly turn
        # one entry into two and break identity where it is hardest to see.
        continuation_analysis = None
        if (
            continuation
            and continuation["page_ordinal"] in pages
            and continuation["page_ordinal"] not in failures
        ):
            continuation_analysis = _analyze_page(
                page_cache,
                context,
                continuation["page_ordinal"],
                pages[continuation["page_ordinal"]],
                grouping_policy,
            )
            continuation_region = cut_region(
                context,
                act,
                pages[continuation["page_ordinal"]],
                _bounds_of(continuation),
                2,
                continuation["page_ordinal"],
                "proposal",
                padding=padding,
                provenance=provenance,
            )
            evidence.append(context.input_ref(continuation_region.relative_path))
            continuation_cut = True

        if continuation and not continuation_cut:
            # The near side is sealed ink and stays cut as evidence for the
            # reviewer, but the act as marked out is incomplete: delivering
            # a reading of the near side alone would be a truncation wearing
            # a complete act's name.
            outcome = "held"
            far_ordinal = continuation["page_ordinal"]
            if far_ordinal in failures:
                reason = (
                    f"the act continues onto page {far_ordinal}, which the structure "
                    f"pass could not mark out ({failures[far_ordinal]}), so its "
                    "continuation could not be cut"
                )
                reason_code = "structure-pass-held-on-continuation"
            else:
                reason = (
                    f"the act continues onto page {far_ordinal}, "
                    "which was not sealed, so its continuation could not be cut"
                )
                reason_code = "exemplar-continuation-not-sealed"
            hold = hold_act(context, act, act_id, far_ordinal, records, reason, reason_code)
            evidence.append(context.input_ref(hold.relative_path))
        else:
            outcome = "proposed"
            _publish_act_group(
                context,
                act,
                act_id,
                pages[page_ordinal],
                analysis,
                continuation if continuation_cut else None,
                pages[continuation["page_ordinal"]] if continuation_cut else None,
                continuation_analysis if continuation_cut else None,
            )

    row = {
        "act_id": act_id,
        "act_key": act["key"],
        # The sealed page record's own subject wherever the page sealed, exactly
        # as this stage's other records now read it, and the fixture derivation
        # only on the one branch above where no sealed record exists to read.
        # The two are the same string on a fixture run and only one of them
        # exists on a real page; this row is the one every consumer joins the
        # others against, so it is the last place two spellings of one identity
        # should have been left standing.
        # `test_structure_pass.py` pins that equality for every sealed
        # fixture page.
        "page_id": (
            pages[page_ordinal]["subject_id"]
            if page_ordinal in pages
            else page_identity(context.fixture, page_ordinal)
        ),
        "page_ordinal": page_ordinal,
        # Derived from the regions actually cut, never from the fixture
        # declaration: a seal that claims a continuation nothing holds is
        # how an act gets read on one side of a page break and delivered as a
        # complete reading.
        "has_continuation": continuation_cut,
        "outcome": outcome,
        "evidence": sorted(evidence, key=lambda reference: reference["relative_path"]),
    }
    return row, evidence


def initial_pass(context) -> bool:
    """Mark out every act on every sealed page. True when anything was held."""
    records = page_records(context)
    pages = sealed_pages(records)
    if not pages:
        raise ContractError("the Designator found no sealed page to mark out")

    # The run's own argument, not the module default, so the file this pads
    # with is the file the run sealed. Same file is not yet same bytes:
    # `open_context` read it to check the binding and this reads it again for
    # the values, and a rewrite between the two reads pads every crop under a
    # policy the run never sealed while every other check still passes.
    padding = geometry.load_padding_config(context.args.designator_padding_config)
    context.require_sealed_config("designator-padding", padding["config_sha256"])
    geometry_policy = geometry_layer.load_geometry_policy(context.args.designator_geometry_config)
    context.require_sealed_config("designator-geometry", geometry_policy["config_sha256"])
    # The point of use the sealed `designator-grouping` name has had no reader
    # for. `run_config_bindings` sealed the digest and `stage_parser` carried
    # the flag, but nothing in this stage ever loaded the file, so the seal
    # named a window nothing actually shut: a rewritten grouping policy would
    # have changed no threshold, because the thresholds were still Python
    # constants. Read here, under the run's own argument and checked against the
    # run's own seal, for the same reason padding is: same path is not yet same
    # bytes.
    grouping_policy = grouping_config.load_grouping_config(context.args.designator_grouping_config)
    context.require_sealed_config("designator-grouping", grouping_policy["config_sha256"])
    provenance = structure_provenance(context)
    secondary = secondary_provenance(context)
    context.publish(
        kind="secondary-provenance",
        subject_id="secondary-provenance",
        outcome="proposed",
        inputs=[],
        payload=secondary,
    )
    # Which sealed pages the structure pass could not mark out, decided once and
    # before any crop is cut, so a page's structural outcome is a fact the act
    # loop reads rather than one it discovers halfway through.
    failures = structure_failures(context, pages)
    page_cache: dict[int, dict] = {}
    # Do not publish a successful status before the page analysis it describes
    # has actually succeeded. A fatal decode or grouping error must not leave a
    # durable `marked-out` claim behind it.
    for ordinal, page_record in pages.items():
        if ordinal not in failures:
            # No hold is added here and none should be. `_analyze_page` handles a
            # page it cannot threshold by cutting predetermined crops instead of
            # by removing it -- Tyrel, 2026-08-11, "everything gets read every
            # time nothing gets pulled out or held". A corrupt decode is still
            # fatal, and the comment above says why.
            _analyze_page(page_cache, context, ordinal, page_record, grouping_policy)
    status_refs = publish_structure_status(
        context, records, pages, provenance, failures, page_cache
    )

    _refuse_duplicate_proposal_bounds(context)
    expected = []
    seal_inputs = []
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

    # Every sealed page the structure pass found nothing on is cut into its
    # predetermined crops, which become real proposal regions of one minted act
    # per page. Before conservation, deliberately: these crops are claims on the
    # page's own pixels, so `_claimed_regions_by_page` below has to see them or
    # the reconciliation would report as residual exactly the ink these crops
    # already cover. A page the structure pass was *held* on is not tiled -- its
    # acts are held and no crop is cut on it at all, which is a different, named
    # outcome (`structure_failures`) rather than an absence of findings.
    fallback_rows = _publish_page_fallbacks(
        context, pages, failures, page_cache, status_refs, provenance, grouping_policy
    )
    expected.extend(fallback_rows)
    seal_inputs.extend(reference for row in fallback_rows for reference in row["evidence"])
    if not expected:
        raise ContractError("no declared act or page fallback was marked out on any sealed page")

    # Conservation runs over every sealed page this run reached, not only the
    # pages a declared act happened to touch — a page nothing was assigned to
    # is exactly the case a coverage proof must not skip by construction. Any
    # residual it finds extends the seal's own denominator, held from the
    # start, so it reaches expected_acts()/the seal exactly like every other
    # act rather than sitting inert inside the conservation artifact alone.
    residual_rows, secondary_held, unmeasured = _publish_page_conservation(
        context, pages, failures, page_cache, secondary, grouping_policy
    )
    expected.extend(residual_rows)
    seal_inputs.extend(reference for row in residual_rows for reference in row["evidence"])

    # The seal, emitted once and never rewritten: this is what downstream stages
    # reconcile against, so "every expected act has exactly one outcome" is a
    # question with an answer.
    payload = {
        "expected_acts": expected,
        "count": len(expected),
        "provenance": provenance,
    }
    payload["self_hash"] = self_hash(payload)
    context.publish(
        kind="proposal-seal",
        subject_id="proposal-seal",
        outcome="proposed",
        inputs=seal_inputs,
        payload=payload,
    )
    # A run that held an act, or held a page, or found ink no crop claimed has
    # not completed. The exit code is the one signal an operator reads without
    # opening the tree, and a 0 over a hold is a partial result wearing
    # "complete" (GOVERNANCE 2). Act holds come from the seal itself; secondary
    # holds deliberately sit outside that authority, so they arrive separately.
    #
    # So does a page whose ink could not be measured at all. GOVERNANCE 2 refuses
    # "complete" "unless everything reconciles", and conservation is the
    # reconciliation: a page it could not run on has not reconciled, whatever the
    # seal's own rows say. This is not the same as holding the page — nothing was
    # pulled out, every act on it was still cut, and its predetermined crops still
    # go downstream to be read, which is what Tyrel's 2026-08-11 ruling requires.
    # What is withheld is the run's claim to have completed, not the page.
    return _initial_pass_has_holds(
        expected,
        failures,
        secondary_held=secondary_held,
        unmeasured=unmeasured,
    )


def _live_secondary_provenance(context) -> dict:
    """The secondary proposer on the live path: recorded absent, or refused.

    Resolved every run for the reason `secondary_provenance` gives -- an
    absence is a decision only if something asks the registry and writes it
    down. What differs is the configured case. The fixture path writes a
    `fixture://` receipt for a configured secondary chair; the live path
    called a real chair and may not write a declared serving moment for one it
    did not (GOVERNANCE 6). Nothing here serves a secondary chair either: the
    role is absent by ruling (Tyrel, 2026-08-12, "keep the optional YOLO
    secondary proposer absent initially"), and a configured row on a live run
    is refused by name rather than run through a pass that does not exist.
    """
    resolved = context.registry.resolve(SECONDARY_PROPOSER_CHAIR)
    if isinstance(resolved, AbsentChair):
        return {
            "chair": resolved.role,
            "chair_state": "absent",
            "absence": resolved.to_record(),
            "resolved_identity": None,
            "resolved_revision": None,
            "receipt_ref": None,
            "adapter_revision": context.adapter_revision,
        }
    raise ContractError(
        f"the secondary proposer chair {SECONDARY_PROPOSER_CHAIR!r} is configured, but the "
        "live structure pass serves no secondary chair and writes no fixture receipt for one; "
        "the role is absent by ruling (2026-08-12), so a live run must configure it absent"
    )


def _publish_live_act_groups(
    context, page_record: dict, analysis: dict, minted: list[tuple[str, str, dict]]
) -> None:
    """One text-free `act-group` per chair rectangle on one page, evidence from the scan.

    `declared_bounds` here means declared by the structure chair: the same
    closed field set as a fixture act's record, with the chair as the declarer.
    The evidence block is `structure_pass.model_evidence_blocks`, computed for
    the whole page at once so a merged ink group is recorded on both acts it
    covers. No continuation: a per-page call has no cross-page knowledge, and
    the relation is the Recensor's (see "Continuation ownership" in HANDOFF.md).
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


def live_initial_pass(context, serving_factory, tier: str) -> bool:
    """Mark out every sealed page through the served structure chair. True when held.

    The live counterpart of `initial_pass`, sharing every piece that is not the
    proposer itself: the Exemplar boundary, the sealed padding, geometry and
    grouping policies, the per-page ink analysis, `cut_minted_region`,
    `publish_structure_status`, the page-fallback tiling, conservation, the
    residual holds, the once-only seal and the exit rule. What replaces the
    fixture's declared acts is one call per sealed page through the client
    (`structure_pass.ask_page`), and what replaces `structure_provenance` is
    the chair's real receipt with the sealed decoding posture beside it
    (`structure_pass.live_chair_record`). No `fixture://` receipt is written
    on this path, and `context.fixture` is never read.

    Order of publication per page: the answer record first, then the status
    that names it, then the crops -- so a status that says `scanned` always
    points at an answer that already exists, and `common/stage.py::
    _verify_proposal_act_row` can follow the reference on every row. A page
    the chair could not mark out is held with its code in `failures`, exactly
    where a fixture-declared failure would be, so everything downstream of the
    hold -- no crop cut, ink reconciled as residual, `EXIT_HELD` -- is the
    code path the fixture path already proves.
    """
    records = page_records(context)
    pages = sealed_pages(records)
    if not pages:
        raise ContractError("the Designator found no sealed page to mark out")

    padding = geometry.load_padding_config(context.args.designator_padding_config)
    context.require_sealed_config("designator-padding", padding["config_sha256"])
    geometry_policy = geometry_layer.load_geometry_policy(context.args.designator_geometry_config)
    context.require_sealed_config("designator-geometry", geometry_policy["config_sha256"])
    grouping_policy = grouping_config.load_grouping_config(context.args.designator_grouping_config)
    context.require_sealed_config("designator-grouping", grouping_policy["config_sha256"])
    # The structure pass's own posture, from the bytes this run sealed and
    # rechecked at this point of use (Tyrel, 2026-09-02: `[structure]`, never
    # `reading_of_record`). Refused by name before any chair starts when the
    # sealed value is one the live seam cannot execute.
    decoding_policy, decoding_sha256 = load_decoding_policy(context.args.decoding_config)
    context.require_sealed_config("decoding", decoding_sha256)
    temperature = structure_pass.executable_temperature(decoding_policy)
    identity = structure_pass.resolved_structure_chair(context)
    secondary = _live_secondary_provenance(context)
    context.publish(
        kind="secondary-provenance",
        subject_id="secondary-provenance",
        outcome="proposed",
        inputs=[],
        payload=secondary,
    )

    page_cache: dict[int, dict] = {}
    for ordinal, page_record in pages.items():
        _analyze_page(page_cache, context, ordinal, page_record, grouping_policy)

    answers: dict[int, structure_pass.PageAnswer] = {}
    client = serving_factory(context, identity, tier)
    with client:
        engine_call = structure_pass.structure_engine_call(decoding_sha256)
        provenance = structure_pass.live_chair_record(
            context, identity, client.handle.receipt_reference, engine_call
        )
        for ordinal, page_record in pages.items():
            answers[ordinal] = structure_pass.ask_page(
                context,
                client,
                page_record,
                ordinal,
                _read_checked_page_bytes(context, page_record),
                page_cache[ordinal],
                temperature=temperature,
                decoding_config_sha256=decoding_sha256,
                provenance=provenance,
            )

    failures: dict[int, str] = {}
    status_answers: dict[int, tuple[str | None, dict[str, str]]] = {}
    for ordinal, answer in answers.items():
        _validate_structure_answer_payload(answer.record)
        published = context.publish(
            kind=STRUCTURE_ANSWER_KIND,
            subject_id=answer.page_id,
            outcome="held" if answer.disposition == structure_pass.DISPOSITION_HELD else "proposed",
            inputs=[context.input_ref(pages[ordinal]["payload"]["image_path"])],
            payload=answer.record,
        )
        answer_ref = context.input_ref(published.relative_path)
        if answer.disposition == structure_pass.DISPOSITION_HELD:
            # A held page's status carries its reason code and null evidence
            # (`structure_evidence` names what corroborates a scanned page's
            # crops, and a held page has none), but still names the answer:
            # the retained bytes and the parse outcome are the hold's evidence.
            failures[ordinal] = answer.reason_code
            status_answers[ordinal] = (None, answer_ref)
        else:
            status_answers[ordinal] = (answer.disposition, answer_ref)
    status_refs = publish_structure_status(
        context, records, pages, provenance, failures, page_cache, answers=status_answers
    )

    expected = []
    seal_inputs = []
    for ordinal, answer in answers.items():
        if answer.disposition != structure_pass.DISPOSITION_DETECTED:
            continue
        page_record = pages[ordinal]
        analysis = page_cache[ordinal]
        minted: list[tuple[str, str, dict]] = []
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
                provenance=provenance,
            )
            evidence = [context.input_ref(region.relative_path)]
            expected.append(
                {
                    "act_id": act_id,
                    "act_key": act_key,
                    "page_id": page_record["subject_id"],
                    "page_ordinal": ordinal,
                    "has_continuation": False,
                    "outcome": "proposed",
                    "evidence": evidence,
                }
            )
            seal_inputs.extend(evidence)
            minted.append((act_id, act_key, bounds))
        _publish_live_act_groups(context, page_record, analysis, minted)

    # A page the chair answered with no act at all is cut into its predetermined
    # crops (Tyrel, 2026-08-11), over the grid computed from the page's own
    # dimensions -- never over the scan's groups, which on this path are
    # corroboration and not what decides which crops a page gets.
    claimed_by_page = _claimed_regions_by_page(context)
    for ordinal, answer in answers.items():
        if answer.disposition != structure_pass.DISPOSITION_FALLBACK_TILES:
            continue
        analysis = page_cache[ordinal]
        tiled = {
            **analysis,
            "structure_evidence": "fallback-tiles",
            # The page's own resolved sealed grouping policy, exactly as the
            # fixture path passes it (`_analyze_page`): the live path decides
            # *when* a page is tiled from the chair's answer, never under what
            # thresholds, and a grid cut under numbers nobody sealed for this
            # run would be a crop policy the run authority does not cover.
            "groups": grouping.fallback_tiles(
                analysis["width"],
                analysis["height"],
                bands=analysis["thresholds"].fallback_bands,
                overlap_px=analysis["thresholds"].fallback_overlap_px,
            ),
        }
        row = _publish_page_fallback(
            context,
            ordinal,
            pages[ordinal],
            tiled,
            status_refs[ordinal],
            claimed_by_page.get(ordinal, []),
            provenance,
            reason=_FALLBACK_REASON_LIVE,
        )
        if row is not None:
            expected.append(row)
            seal_inputs.extend(row["evidence"])
    if not expected and not failures:
        raise ContractError("no structural proposal or page fallback was marked out on any page")

    residual_rows, secondary_held, unmeasured = _publish_page_conservation(
        context, pages, failures, page_cache, secondary, grouping_policy
    )
    expected.extend(residual_rows)
    seal_inputs.extend(reference for row in residual_rows for reference in row["evidence"])
    if not expected:
        raise ContractError(
            "every page was held and none carried ink to account for; the run has no act "
            "denominator to seal"
        )

    payload = {
        "expected_acts": expected,
        "count": len(expected),
        "provenance": provenance,
    }
    payload["self_hash"] = self_hash(payload)
    context.publish(
        kind="proposal-seal",
        subject_id="proposal-seal",
        outcome="proposed",
        inputs=seal_inputs,
        payload=payload,
    )
    return _initial_pass_has_holds(
        expected,
        failures,
        secondary_held=secondary_held,
        unmeasured=unmeasured,
    )


def _refuse_duplicate_proposal_bounds(context) -> None:
    """A proposal class is one rectangle per page, never an ordinal namespace.

    The Designator's fixture path is the current proposal producer.  Once
    ordinal leaves identity, two fixture proposals with the same page-local
    bounds would claim the same ``act_id``; refuse before any artifact is cut so
    the ambiguity is visible instead of being silently merged by a dictionary.
    Raw-proposal coincidence is preserved by ``geometry_layer`` as an explicit
    ambiguity and cannot mint a second fixture act here.
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


def recovery_pass(context, act_id: str, request_id: str) -> None:
    """Cut one replacement region for one act, at the Recensor's request.

    The Recensor asked; the Designator cuts. Keeping the ownership straight is
    what stops the recovery loop from growing a second author for crops.
    """
    seal = context.tree.read_artifact(DESIGNATOR, "proposal-seal", _seal_artifact_id())
    match = [item for item in seal["payload"]["expected_acts"] if item["act_id"] == act_id]
    if not match:
        raise ContractError(f"recovery asked for {act_id}, which the proposal seal does not name")
    if match[0].get("outcome") != "proposed":
        raise ContractError(
            f"recovery asked for {act_id}, which the seal holds as "
            f"{match[0].get('outcome')!r}; a held act is terminal and may not be "
            "recropped back to life"
        )

    # The run's sealed policy, carried from the binding check rather than read
    # again here. This was the second unbound read of `config/recovery.toml` at
    # the recovery boundary (audit S3): the budget a recrop is authorized against
    # must be the budget the run bound, and the recheck below refuses rather than
    # cutting a crop under an allowance nothing sealed.
    policy = context.recovery_policy
    context.require_sealed_config("recovery", policy["config_sha256"])
    request = current_recovery_request(
        context.tree,
        act_id,
        policy,
        request_id=request_id,
    )
    request_payload = request.get("payload")
    # `current_recovery_request` has already verified the record's shape, its
    # exact current review reference, its Perlectio, and the run-bound policy.
    # Keep this local guard only to make the payload type explicit to the crop
    # accounting immediately below.
    if not isinstance(request_payload, dict):  # pragma: no cover - common guard above
        raise ContractError("the requested Recensor recovery record has no payload")
    ordinal = request_payload.get("attempt_ordinal")
    if not isinstance(ordinal, int) or isinstance(ordinal, bool):  # pragma: no cover
        raise ContractError("the requested Recensor recovery record has no attempt ordinal")
    if request_payload.get("act_key") != match[0]["act_key"]:
        raise ContractError(
            "the exact current Recensor recovery request does not bind this proposal-seal act"
        )
    # Two distinct recovery operations exist in the policy and the payload
    # schema (a Designator recrop, a Perlector page-level/continuation-aware
    # reread) and only one of them is this stage's to answer. Answering any
    # other kind here would silently substitute a crop for whatever the
    # Recensor actually asked for — exactly the conflation naming the kind
    # exists to stop.
    recovery_kind = request_payload.get("recovery_kind")
    if recovery_kind != FALLBACK_RECROP:
        raise ContractError(
            f"recovery for {act_id} names recovery_kind {recovery_kind!r}; the Designator "
            f"only answers {FALLBACK_RECROP!r} requests (a recrop). A different recovery "
            "kind names a different owning stage, not a substitute crop"
        )

    fixture_acts = [item for item in context.fixture["act"] if item["key"] == match[0]["act_key"]]
    if not fixture_acts:
        raise ContractError(
            f"recovery fixture declares no act for key {match[0]['act_key']!r}; the fixture "
            "cannot supply recovery geometry for an act it never declared"
        )
    if len(fixture_acts) != 1:  # pragma: no cover - fixture loading already refuses duplicates
        raise ContractError(
            f"recovery fixture declares {len(fixture_acts)} acts for key "
            f"{match[0]['act_key']!r}; recovery geometry needs one unambiguous act"
        )
    act = fixture_acts[0]
    recovery = [row for row in context.fixture.get("recovery", []) if row["act_key"] == act["key"]]
    if len(recovery) != 1:
        raise ContractError(
            f"the fixture declares {len(recovery)} recovery regions for act {act['key']}; "
            "a recovery request must name exactly one coverage rectangle"
        )

    pages = sealed_pages(page_records(context))
    bounds = _bounds_of(recovery[0])
    page_record = pages[act["page_ordinal"]]
    # Checked here, before anything is computed from the rectangle, even though
    # `cut_minted_region` checks it again as the crop author's own guard over
    # every caller. The coverage refusal below is a statement about pixels, and
    # a degenerate or off-page rectangle reaching it first would be refused for
    # recovering no coverage rather than for not being a rectangle on this page
    # -- a refusal that names the wrong defect sends its reader to the wrong
    # place. The page is read twice per recovery invocation as a result; a
    # recovery is bounded and rare, and the read re-verifies the sealed digest.
    page_w, page_h = dimensions(_read_checked_page_bytes(context, page_record))
    geometry.validate_bounds(bounds, page_w, page_h, "recovery bounds")
    # The same builder `cut_region` uses, so this duplicate check is computed
    # against the exact shape that would actually be published.
    transform = _crop_transform(act["page_ordinal"], page_record["subject_id"], bounds)
    duplicate = region_id(act_id, transform)
    existing_regions = _regions_of(context, act_id)
    already_recovered = [
        record for record in existing_regions if record["payload"].get("origin") == "recovery"
    ]
    # A recovery exists to recover coverage. A transform already cut for this act
    # produces the same crop bytes and region identity, whether its prior origin
    # was proposal or recovery. Publishing it would create a new reading attempt
    # without new evidence, which is a re-roll rather than coverage recovery.
    if any(record["payload"].get("region_id") == duplicate for record in existing_regions):
        raise ContractError(
            f"recovery asked for {act_id}, which already has a region cut for this exact "
            "transform; a recovery must add coverage rather than re-read identical pixels"
        )
    # The same rule, stated over pixels instead of over identity. The check
    # above only catches a recrop of the *exact* rectangle already cut, so a
    # rectangle strictly inside what this act already has -- or one covered
    # jointly by two of its regions -- passed it while recovering nothing.
    # GOVERNANCE 11 gives the operation its purpose ("Recovery exists for
    # completeness and coverage"), and ARCHITECTURE names it a "fallback or
    # **expanded** recrop": a recrop that adds no page pixel expands nothing.
    # It spends a bounded, recorded budget re-reading pixels the act already
    # carries, and the Perlector then marks the region witness-uncovered
    # (`cut_minted_region`: "ink a recovery uncovers was never shown to them"),
    # so the export ends up carrying a coverage caveat over no new coverage.
    #
    # Refused rather than accepted-and-flagged because a spent recovery budget
    # is not recoverable: the act's one recorded chance to widen its crop would
    # be gone, which is the direction GOALS 1 cares about.
    covered = _coverage_on_page(existing_regions, act["page_ordinal"], page_record["subject_id"])
    if not _uncovered_area(bounds, covered):
        raise ContractError(
            f"recovery asked for {act_id} with bounds {bounds}, which recovers no page "
            f"pixel the act does not already have: every pixel of it already lies inside "
            f"the {len(covered)} region(s) cut for it on page {act['page_ordinal']}. A "
            "recovery must add coverage, not recrop inside coverage it already has"
        )
    recovery_count = len(already_recovered)
    if request_payload.get("budget_used") != recovery_count or ordinal != recovery_count + 1:
        raise ContractError(
            "the supplied recovery request is stale or skips a recovery ordinal; a recrop "
            "may only answer the next recorded request"
        )
    region_ordinal = _next_region_ordinal(context, act_id)
    cut_region(
        context,
        act,
        pages[act["page_ordinal"]],
        bounds,
        region_ordinal,
        act["page_ordinal"],
        "recovery",
        context.artifact_ref(RECENSOR, "recovery-request", request["artifact_id"]),
    )


def _seal_artifact_id() -> str:
    return artifact_id(DESIGNATOR, "proposal-seal", "proposal-seal", None)


def _next_region_ordinal(context, act_id: str) -> int:
    ordinals = [record["payload"]["attempt_ordinal"] for record in _regions_of(context, act_id)]
    return max(ordinals, default=0) + 1


def _regions_of(context, act_id: str) -> list[dict]:
    records = []
    for entry in context.tree.build_manifest(DESIGNATOR)["artifacts"]:
        if entry["kind"] == "region" and entry["subject_id"] == act_id:
            records.append(context.tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"]))
    return records


def _open(args, registry_factory) -> tuple[StageContext, bool]:
    """Open the run on either ingress route, and say which route it was.

    The shared constructor decides the route from the run authority it read
    once; the flag is read back off that same `context.run`, never from a second
    read of `run.json`, so the route `main` acts on is the route the context was
    built for. Which *pass* then runs is not decided here and not by the route:
    `main` asks the sealed serving catalogue (`structure_pass.
    structure_serving_mode`), and the one thing the route decides is that a
    real submission may not be marked out by the fixture chair.
    """
    context = open_stage_context(args, DESIGNATOR, registry_factory=registry_factory)
    return context, parse_ingress_record(context.run.get("ingress")) == REAL_INGRESS


def main(registry_factory=ChairRegistry.from_toml, serving_factory=None) -> int:
    """Run through the explicitly supplied structure-chair implementation.

    `serving_factory(context, identity, tier) -> ChairClient` is the live
    reading seam, injected the way the Attestatores and the Perlector inject
    theirs: a stage test passes `operations.serving.fakes`' factory, production
    passes nothing and gets `structure_pass.default_serving_factory`. It is
    reached only once the sealed catalogue has said `live` for the structure
    chair; it never decides which pass runs.
    """
    args = stage_parser(__doc__.splitlines()[0]).parse_args()
    context, real_input = _open(args, registry_factory)

    if args.operation == "recover" and real_input:
        # `recovery_pass` reads a recrop's geometry from `context.fixture["act"]`
        # (the fixture's own declared rectangle) because that is the only
        # geometry the recovery contract carries today; a real submission has
        # no fixture and this stage has no other source for a recrop's bounds.
        # Refuse by name here rather than let the generic fixture accessor's
        # message stand in for it.
        raise ContractError(
            "bounded recovery from a real submission is not built; a recovery still reads "
            "the fixture's declared rectangle, which a real submission does not carry"
        )
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
        # The selector is the sealed serving-recipe row kind for the structure
        # chair, never a flag and never the ingress route (SPEC_D §5): the
        # offline end-to-end run drives the live pass over fixture pages, and
        # a real submission under the fixture catalogue is refused by name
        # rather than marked out by an ink scan standing in for a model.
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
        # A closed set, checked rather than assumed: `--operation` has no
        # `choices=` at the shared `stage_parser` level (other stages read it
        # differently), so a typo of "recover" -- or any other value -- would
        # otherwise fall through to a full `initial_pass` silently instead of
        # being refused, doing the wrong operation rather than none at all.
        raise ContractError(f"--operation {args.operation!r} is not one of 'initial' or 'recover'")

    context.seal_boundary()
    context.finish()
    return EXIT_HELD if held else EXIT_COMPLETE


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
