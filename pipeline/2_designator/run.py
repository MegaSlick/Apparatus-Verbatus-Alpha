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
this stage cannot mark out is a unit it still accounts for; before the hold
existed, such an act was skipped, sealed nowhere, and the run reported complete
over its absence. A run that held anything exits `EXIT_HELD`, so the same fact
reaches an operator who never opens the tree.

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

    python pipeline/2_designator/run.py --run-root <dir> --run-id <id>
    python pipeline/2_designator/run.py ... --operation recover --act <act_id>
"""

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
import grouping  # noqa: E402
import structure  # noqa: E402

from common.chairs.models import AbsentChair, ChairIdentity  # noqa: E402
from common.chairs.registry import ChairRegistry  # noqa: E402
from common.contracts.approval import REAL_INGRESS, parse_ingress_record  # noqa: E402
from common.contracts.canonical import digest_bytes, self_hash  # noqa: E402
from common.contracts.errors import ContractError  # noqa: E402
from common.contracts.identities import act_id as derive_residual_act_id  # noqa: E402
from common.contracts.identities import artifact_id, attempt_id, region_id  # noqa: E402
from common.contracts.stages import DESIGNATOR, EXEMPLAR, RECENSOR  # noqa: E402
from common.exemplar_boundary import (  # noqa: E402
    verify_exemplar_corpus_seal,
    verify_sealed_page_pixels,
)
from common.imaging import crop_png, decode_grayscale_png, dimensions  # noqa: E402
from common.recovery import FALLBACK_RECROP, load_recovery_policy  # noqa: E402
from common.runtree.store import RunTree  # noqa: E402
from common.stage import (  # noqa: E402
    DESIGNATOR_CHAIR,
    EXIT_COMPLETE,
    EXIT_HELD,
    SECONDARY_PROPOSER_CHAIR,
    StageContext,
    act_bounds,
    act_identity,
    adapter_recipe_for,
    continuation_for,
    current_recovery_request,
    fixture_serving_details,
    open_context,
    page_identity,
    residual_act_ordinal,
    run_stage,
    stage_parser,
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


def _require_rectangle_fields(block: dict, what: str) -> None:
    for field in ("declared_bounds", "detected_bounds"):
        bounds = block[field]
        if not isinstance(bounds, dict) or set(bounds) != {"x", "y", "w", "h"}:
            raise ContractError(f"a Designator act-group {what} has invalid {field}")


def _validate_act_group_payload(payload: object) -> None:
    """Validate the closed, geometry-only act-group contract before publication."""
    required = {
        "act_key",
        "declared_bounds",
        "detected_bounds",
        "body_member_count",
        "anchor_count",
        "rationale",
        "continuation",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise ContractError("a Designator act-group payload has fields outside its closed contract")
    _require_rectangle_fields(payload, "payload")
    continuation = payload["continuation"]
    if continuation is not None:
        continuation_fields = {
            "declared_bounds",
            "detected_bounds",
            "rationale",
            "geometric_corroboration",
        }
        if not isinstance(continuation, dict) or set(continuation) != continuation_fields:
            raise ContractError(
                "a Designator act-group continuation has fields outside its closed contract"
            )
        _require_rectangle_fields(continuation, "continuation")
    _refuse_text_fields(payload)


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

    Grayscale PNG only: the synthetic walking skeleton's pages are the only
    pixels this stage ever sees, and `common/imaging.py` states its own
    narrowness plainly — a codec that quietly half-handled a real photograph
    would be worse than one that says no. Real ingress never reaches here at
    all (`_open` stops before it).
    """
    page_bytes = _read_checked_page_bytes(context, page_record)
    width, height, rows = decode_grayscale_png(page_bytes)
    background = structure.infer_background(width, height, rows)
    return width, height, rows, background


def _bounds_of(row: dict) -> dict:
    """The one reader of a fixture row's `x, y, w, h` fields as a `Bounds` dict.

    A continuation row and a recovery row each carry their rectangle this way,
    and three call sites once each rebuilt the same four-key comprehension by
    hand: the act-group evidence, the continuation crop, and the recovery crop.
    Three independent copies of one projection is the same risk class as two
    independent copies of a transform (`_crop_transform`) -- a fourth field
    added to a fixture row and read by only some of the copies would silently
    change what one call site cuts or compares without the others noticing.
    `common.stage.act_bounds` is the sibling of this for a declared act row;
    this is what a continuation or recovery row uses, since neither is an act
    row itself.
    """
    return {key: row[key] for key in ("x", "y", "w", "h")}


def _overlap_area(a: dict, b: dict) -> int:
    x0, y0 = max(a["x"], b["x"]), max(a["y"], b["y"])
    x1 = min(a["x"] + a["w"], b["x"] + b["w"])
    y1 = min(a["y"] + a["h"], b["y"] + b["h"])
    return max(0, x1 - x0) * max(0, y1 - y0)


def _body_overlap_area(group: dict, declared_bounds: dict) -> int:
    """Sum of a group's own body members' overlap with `declared_bounds`.

    Used only to break a tie in `_match_structural_group` between two groups
    whose full (body + anchor) bounds overlap `declared_bounds` identically —
    the brace-linked case, where one shared tall anchor dominates both
    groups' union bounds and makes them indistinguishable by that measure
    alone. The anchor is common evidence for both acts, so it cannot be what
    tells them apart; each group's own body text can.
    """
    return sum(
        _overlap_area(member["bounds"], declared_bounds) for member in group.get("body_members", [])
    )


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
    best, best_overlap, best_body_overlap = None, 0, 0
    for group in groups:
        overlap = _overlap_area(group["bounds"], declared_bounds)
        body_overlap = _body_overlap_area(group, declared_bounds)
        if overlap > best_overlap or (overlap == best_overlap and body_overlap > best_body_overlap):
            best, best_overlap, best_body_overlap = group, overlap, body_overlap
    if best is None or best_overlap * 2 < declared_area:
        raise ContractError(
            f"{what}: structural grouping found no detected region covering at least "
            f"half of the declared bounds {declared_bounds}; the structure pass may "
            "have missed this act entirely"
        )
    return best


def _claim_structural_group(analysis: dict, group: dict, act_key: str, what: str) -> None:
    """Bind one detected group to one act, and refuse a second claimant.

    `_match_structural_group` answers "which detected group best covers this
    declared act" for one act at a time, so two acts whose declared rectangles
    both fall inside a single detected group both match it — the case where the
    structure pass found one region across a boundary it did not detect (two
    entries with no margin anchor and fewer blank rows between them than
    `grouping.DEFAULT_CHAIN_GAP_PX`). Each act's `act-group` artifact would then
    record the merged rectangle as its own `detected_bounds` and the merged run
    as its own `body_member_count`, so the record claims detection corroborated
    each act separately when detection found neither. That is the "silent
    substitution" `_publish_act_group` documents itself as refusing, and it is a
    claim about what was measured that was not measured (GOVERNANCE 10).

    A brace-linked pair is not this case and stays legal: `grouping.group_page`
    returns two distinct groups sharing one anchor, so each act claims its own.
    """
    claims = analysis.setdefault("group_claims", {})
    holder = claims.get(id(group))
    if holder is not None and holder != act_key:
        raise ContractError(
            f"{what}: the detected region {group['bounds']} already corresponds to act "
            f"{holder!r}; the structure pass found one region where two acts are declared, "
            "so it corroborates neither and the boundary between them was not detected"
        )
    claims[id(group)] = act_key


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
    return records


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
    exact four fields, and every caller that must know a region's identity uses
    this rather than hand-building the shape again.

    This used to have two independent authors: `cut_region` built it to
    actually publish a region, and `recovery_pass` built a second, separately
    typed copy of the identical shape purely to predict what `region_id` a
    would-be duplicate recrop would carry before ever cutting one. A field
    added to one and not the other would have let the two silently diverge —
    `recovery_pass`'s duplicate check would then compute a `region_id` for a
    transform `cut_region` could never actually produce, and the check would
    stop firing without either function's own code changing shape in a way a
    reader would notice. One builder removes the seam entirely rather than
    documenting a discipline for keeping two hand-written copies in lockstep.
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
):
    """Cut one region of one act and publish it.

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
    act_id = act_identity(context.fixture, act)
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
            "act_key": act["key"],
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

    The hold is a real record, never a skipped loop iteration: before it existed,
    an act on an unsealed page was written nowhere at all, the proposal seal came
    up short, and the Armarium's conservation check reconciled perfectly against
    a record of the loss's absence. The hold references the Exemplar's own page
    outcome as its evidence, so the refusal it rests on is one digest-checked
    hop away.

    `blocking_ordinal` is the page whose state stopped this act, and
    `reason_code` says which state that was. The two are separate fields because
    the page is not always *unsealed*: a page the structure pass could not mark
    out is sealed ink this stage still cannot bound, and a field named
    `unsealed_page_ordinal` — which is what this payload carried while an
    unsealed page was the only way to reach here — would have said something
    false about it. `reason` stays the sentence a reviewer reads; `reason_code`
    is the closed vocabulary a consumer may branch on without parsing prose.
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


def publish_structure_status(context, records, pages, provenance, failures) -> None:
    """One visible per-page outcome for the structure pass, held or marked out.

    Published for every sealed page, not only the failing ones, so "the
    structure pass ran on this page and succeeded" is a record rather than the
    absence of one. Without it a reader can only infer a page's structural
    outcome from whether crops happen to exist on it, which is exactly the
    inference GOVERNANCE 2 refuses: a page nothing marked out and a page nothing
    tried to mark out would look identical.
    """
    for ordinal in sorted(pages):
        reason_code = failures.get(ordinal)
        context.publish(
            kind="structure-status",
            subject_id=page_identity(context.fixture, ordinal),
            outcome="held" if reason_code else "proposed",
            inputs=[context.input_ref(records[ordinal]["relative_path"])],
            payload={
                "page_id": pages[ordinal]["subject_id"],
                "page_ordinal": ordinal,
                "state": "held" if reason_code else "marked-out",
                "reason_code": reason_code,
                "provenance": provenance,
            },
        )


def _analyze_page(cache: dict, context, ordinal: int, page_record: dict) -> dict:
    """Structure-pass and grouping results for one sealed page, computed once.

    This is the genuinely visual half of "may use textual as well as visual
    cues" (ARCHITECTURE), run for real on the page's own decoded pixels —
    never assumed, never a fixture value standing in for it.
    """
    if ordinal not in cache:
        width, height, rows, background = page_pixels(context, page_record)
        components = structure.primary_scan(width, height, rows, background=background)
        groups = grouping.group_page(components, width, height)
        cache[ordinal] = {
            "width": width,
            "height": height,
            "rows": rows,
            "background": background,
            "groups": groups,
        }
    return cache[ordinal]


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
    """
    primary_group = _match_structural_group(
        analysis["groups"], act_bounds(act), f"act {act['key']}"
    )
    _claim_structural_group(analysis, primary_group, act["key"], f"act {act['key']}")
    payload: dict = {
        "act_key": act["key"],
        "declared_bounds": act_bounds(act),
        "detected_bounds": primary_group["bounds"],
        "body_member_count": len(primary_group["body_members"]),
        "anchor_count": len(primary_group["anchors"]),
        "rationale": primary_group["rationale"],
        "continuation": None,
    }
    inputs = [context.input_ref(page_record["payload"]["image_path"])]
    if continuation is not None:
        continuation_bounds = _bounds_of(continuation)
        continuation_group = _match_structural_group(
            continuation_analysis["groups"], continuation_bounds, f"act {act['key']} continuation"
        )
        _claim_structural_group(
            continuation_analysis,
            continuation_group,
            act["key"],
            f"act {act['key']} continuation",
        )
        # Recorded, not gated: the synthetic fixture's continuation rectangles
        # do not touch either page's edge (`proof/synthetic_pages.py` says
        # linking them is "a different unit's job"), so honest geometry here
        # is usually `False` for this fixture even though the continuation
        # itself is genuine. Forcing a match would be fabricating corroboration
        # a real page's geometry has not offered.
        corroborated = (
            grouping.find_continuation_candidate(
                analysis["groups"], analysis["height"], continuation_analysis["groups"]
            )
            is not None
        )
        payload["continuation"] = {
            "declared_bounds": continuation_bounds,
            "detected_bounds": continuation_group["bounds"],
            "rationale": continuation_group["rationale"],
            "geometric_corroboration": corroborated,
        }
        inputs.append(context.input_ref(continuation_page_record["payload"]["image_path"]))
    _validate_act_group_payload(payload)
    return context.publish(
        kind="act-group", subject_id=act_id, outcome="proposed", inputs=inputs, payload=payload
    )


def _claimed_regions(context, page_ordinal: int) -> list[dict]:
    """Every proposal region's final (capture) bounds cut on this page so far."""
    claimed = []
    for entry in context.tree.build_manifest(DESIGNATOR)["artifacts"]:
        if entry["kind"] != "region":
            continue
        record = context.tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
        payload = record["payload"]
        if payload.get("origin") != "proposal":
            continue
        if payload["transform"]["source_page_ordinal"] != page_ordinal:
            continue
        claimed.append({"act_id": record["subject_id"], "bounds": payload["transform"]["bounds"]})
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

    That count is recorded, never acted on. This pass may not decide which of
    two acts such a box belongs to, and it may not merge or split either — but
    it also may not refuse the run over one, which is what it did until the
    second review pass of 2026-08-10 measured the consequence: a single
    review-only box straddling the row where two padded claims abut aborted
    `initial_pass` before the proposal seal was written, so configuring an
    optional, explicitly non-authoritative seat turned a complete run into a
    fatal one with no denominator at all. Spec 06's test 5 requires the
    opposite — "removing the proposer changes no authority decision (it adds
    recall, never verdicts)" — and a held, flagged, page-subject rescue crop
    that enters no act and no seal decides nothing either way.
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
    context, ordinal: int, page_record: dict, analysis: dict, claimed: list[dict], secondary: dict
) -> bool:
    """Cut and hold every non-authoritative rescue candidate for review.

    Split from `_publish_conservation_and_secondary` so it can be exercised
    against a hand-fed page analysis without needing to also be the first
    (and, since a conservation record is a once-only artifact, therefore the
    only) publisher of that page's conservation record.
    """
    if secondary["chair_state"] != "configured":
        # Nothing to add: the secondary proposer is explicitly absent, and its
        # absence changes no authority decision here either -- there is simply
        # no additive recall pass to run.
        return False
    candidates = structure.secondary_scan(
        analysis["width"], analysis["height"], analysis["rows"], background=analysis["background"]
    )
    rescues = _secondary_rescue_candidates(claimed, candidates)
    image_path = page_record["payload"]["image_path"]
    page_bytes = _read_checked_page_bytes(context, page_record)
    for index, rescue_row in enumerate(rescues):
        candidate = rescue_row["candidate"]
        overlap_count = rescue_row["overlapping_claimed_act_count"]
        subject = f"{page_identity(context.fixture, ordinal)}-secondary-{index}"
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


def residual_act_key(page_ordinal: int, index: int) -> str:
    """The human-readable label for a conservation-residual act.

    This string is for a reviewer's eye and this stage's own duplicate-act-key
    refusal in `common.stage.expected_acts`; it is not what keeps a residual's
    identity from colliding with a real proposal's. `residual_act_ordinal`'s
    disjoint ordinal space does that, by construction, whatever a fixture
    author happens to name their own acts.
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

    `common.stage.residual_act_ordinal` gives it an identity that cannot
    collide with any real proposal's, present or future, by construction of
    the ordinal space rather than by convention. The hold record carries the
    exact ordinal and bounds a reader needs to recompute that identity, because
    `common.stage._verify_residual_act_rows` does exactly that recomputation —
    every act beyond the fixture's own denominator must prove itself against
    evidence, never merely appear because this stage's own seal says so.
    """
    ordinal = residual_act_ordinal(index)
    minted_act_id = derive_residual_act_id(page_id, ordinal, bounds)
    hold = context.publish(
        kind="hold",
        subject_id=minted_act_id,
        outcome="held",
        inputs=[conservation_ref],
        payload={
            "act_key": residual_act_key(page_ordinal, index),
            "page_ordinal": page_ordinal,
            "residual_ordinal": ordinal,
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
    """Mint one held act per conservation residual, closing HANDOFF.md's own gap.

    Every residual is minted, never only the high-priority ones:
    `review_priority` orders which residual a reviewer looks at first and must
    never decide whether a region exists in the accounting at all — spec 06's
    own words, and `conservation.py`'s module docstring says the same of the
    artifact this extends. `residual_components` already arrives in the
    deterministic (top, then left) order `structure.label_components`
    produces, so `index` — and therefore `residual_act_ordinal(index)` — names
    the same residual on every run over an unchanged page.
    """
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


def _publish_conservation_and_secondary(
    context, ordinal: int, page_record: dict, analysis: dict, secondary: dict
) -> tuple[list[dict], bool]:
    """Independent ink-vs-crop reconciliation, plus non-authoritative rescue crops.

    Conservation rescans this page's own pixels rather than trusting what
    grouping already claimed to have found — closing the gap an independent
    audit of the old pipeline named precisely: its conservation proved
    coverage of units a structural model had already emitted, and could not
    prove the model had not missed ink entirely. This can.

    Returns the expected-act row for every residual this page's reconciliation
    found, so `initial_pass` can extend the proposal seal's own denominator
    with them. A residual is no longer only a passive audit-trail entry: it is
    a unit this run accounts for exactly as it accounts for a page that never
    sealed, closing the gap `HANDOFF.md` named as unclosed pending this exact
    change to `common.stage.expected_acts`.
    """
    claimed = _claimed_regions(context, ordinal)
    result = conservation.reconcile(
        analysis["width"],
        analysis["height"],
        analysis["rows"],
        background=analysis["background"],
        claimed_bounds=[entry["bounds"] for entry in claimed],
    )
    conservation_payload = {
        "page_ordinal": ordinal,
        "total_ink_pixel_count": result["total_ink_pixel_count"],
        "claimed_pixel_count": result["claimed_pixel_count"],
        "residual_pixel_count": result["residual_pixel_count"],
        "residual_components": result["residual_components"],
    }
    _refuse_text_fields(conservation_payload)
    published = context.publish(
        kind="conservation",
        subject_id=page_identity(context.fixture, ordinal),
        outcome="proposed",
        inputs=[context.input_ref(page_record["payload"]["image_path"])],
        payload=conservation_payload,
    )
    secondary_held = _publish_secondary_proposals(
        context, ordinal, page_record, analysis, claimed, secondary
    )
    return (
        _publish_residual_holds(
            context,
            page_identity(context.fixture, ordinal),
            ordinal,
            result["residual_components"],
            context.input_ref(published.relative_path),
        ),
        secondary_held,
    )


def initial_pass(context) -> bool:
    """Mark out every act on every sealed page. True when anything was held."""
    records = page_records(context)
    pages = sealed_pages(records)
    if not pages:
        raise ContractError("the Designator found no sealed page to mark out")

    # Read from the run's own argument rather than the module default, so the
    # bytes this stage pads with are the exact bytes `run_config_bindings`
    # sealed into `run.json` — one path, one digest, no way for the two to name
    # different files.
    padding = geometry.load_padding_config(context.args.designator_padding_config)
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
            _analyze_page(page_cache, context, ordinal, page_record)
    publish_structure_status(context, records, pages, structure_provenance(context), failures)

    expected = []
    seal_inputs = []
    for act in context.fixture["act"]:
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
            analysis = _analyze_page(page_cache, context, page_ordinal, pages[page_ordinal])
            primary = cut_region(
                context,
                act,
                pages[page_ordinal],
                act_bounds(act),
                1,
                page_ordinal,
                "proposal",
                padding=padding,
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

        expected.append(
            {
                "act_id": act_id,
                "act_key": act["key"],
                "page_id": page_identity(context.fixture, page_ordinal),
                "page_ordinal": page_ordinal,
                # Derived from the regions actually cut, never from the fixture
                # declaration: a seal that claims a continuation nothing holds is
                # how an act gets read on one side of a page break and delivered
                # as a complete reading.
                "has_continuation": continuation_cut,
                "outcome": outcome,
                "evidence": sorted(evidence, key=lambda reference: reference["relative_path"]),
            }
        )
        seal_inputs.extend(evidence)

    if not expected:
        raise ContractError("no act was marked out on any sealed page")

    # Conservation runs over every sealed page this run reached, not only the
    # pages a declared act happened to touch — a page nothing was assigned to
    # is exactly the case a coverage proof must not skip by construction. Any
    # residual it finds extends the seal's own denominator, held from the
    # start, so it reaches expected_acts()/the seal exactly like every other
    # act rather than sitting inert inside the conservation artifact alone.
    secondary_held = False
    for ordinal, page_record in pages.items():
        analysis = _analyze_page(page_cache, context, ordinal, page_record)
        residual_rows, page_secondary_held = _publish_conservation_and_secondary(
            context, ordinal, page_record, analysis, secondary
        )
        secondary_held = secondary_held or page_secondary_held
        expected.extend(residual_rows)
        for row in residual_rows:
            seal_inputs.extend(row["evidence"])

    # The seal, emitted once and never rewritten: this is what downstream stages
    # reconcile against, so "every expected act has exactly one outcome" is a
    # question with an answer.
    payload = {
        "expected_acts": expected,
        "count": len(expected),
        "provenance": structure_provenance(context),
    }
    payload["self_hash"] = self_hash(payload)
    context.publish(
        kind="proposal-seal",
        subject_id="proposal-seal",
        outcome="proposed",
        inputs=seal_inputs,
        payload=payload,
    )
    # A run that held an act, or held a page, or found ink no crop claimed, has
    # not completed — and said so only inside its artifacts until now. The exit
    # code is the one signal an operator reads without opening the tree, and a 0
    # over a hold is a partial result wearing "complete" (GOVERNANCE 2). The
    # Act holds are counted from the seal itself. Secondary holds deliberately
    # sit outside that authority and are carried by `secondary_held`, derived
    # from the same list of rescue records this pass just published.
    return any(row["outcome"] == "held" for row in expected) or bool(failures) or secondary_held


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

    policy = load_recovery_policy(context.args.recovery_config)
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

    act = next(item for item in context.fixture["act"] if item["key"] == match[0]["act_key"])
    recovery = [row for row in context.fixture.get("recovery", []) if row["act_key"] == act["key"]]
    if len(recovery) != 1:
        raise ContractError(
            f"the fixture declares {len(recovery)} recovery regions for act {act['key']}; "
            "a recovery request must name exactly one coverage rectangle"
        )

    pages = sealed_pages(page_records(context))
    bounds = _bounds_of(recovery[0])
    page_record = pages[act["page_ordinal"]]
    # The same builder `cut_region` uses, so this duplicate check is computed
    # against the exact shape that would actually be published -- see
    # `_crop_transform`'s docstring for why two independent copies of this
    # shape was a real defect class rather than a style preference.
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


def _open(args, registry_factory) -> tuple[object, bool]:
    """Open either a fixture stage context or the honest real-input boundary.

    System 03 owns the Exemplar-to-Designator reconciliation, but it does not own
    a real structural-proposal model.  A real run therefore reaches that check and
    then stops; it must not fabricate fixture acts, successful no-op work, or a
    synthetic hold that could make an unproposed corpus look exported.
    """
    tree = RunTree(Path(args.run_root), args.run_id)
    run = tree.read_run()
    mode = parse_ingress_record(run.get("ingress"))
    if mode != REAL_INGRESS:
        return open_context(args, DESIGNATOR, registry_factory=registry_factory), False
    return (
        StageContext(
            tree=tree,
            run=run,
            fixture={},
            scenario="real-submission",
            stage=DESIGNATOR,
            adapter_revision=adapter_recipe_for(run, DESIGNATOR),
            args=args,
            registry=None,
        ),
        True,
    )


def main(registry_factory=ChairRegistry.from_toml) -> int:
    """Run through the explicitly supplied structure-chair implementation."""
    args = stage_parser(__doc__.splitlines()[0]).parse_args()
    context, real_input = _open(args, registry_factory)

    if real_input:
        page_records(context)
        raise ContractError(
            "the Exemplar-to-Designator filename-ledger boundary reconciled, but real "
            "structural proposal/model work is outside System 03; no proposals or holds "
            "were fabricated"
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
        held = initial_pass(context)
    else:
        # A closed set, checked rather than assumed: `--operation` has no
        # `choices=` at the shared `stage_parser` level (other stages read it
        # differently), so a typo of "recover" -- or any other value -- would
        # otherwise fall through to a full `initial_pass` silently instead of
        # being refused, doing the wrong operation rather than none at all.
        raise ContractError(f"--operation {args.operation!r} is not one of 'initial' or 'recover'")

    context.finish()
    return EXIT_HELD if held else EXIT_COMPLETE


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
