"""Archetypus: exactly one established reading per act, written once.

The authoritative pipeline output — a machine reading, not truth. Four properties
make that claim honest rather than decorative.

**One text.** A single `text` field per act, written once. There is no second
field holding an alternative, no per-format variant, and no place for a witness's
words to sit beside the reading as an option. The record's field set is closed
and checked (`_RECORD_FIELDS`), so the old pipeline's dead shape — a fallback
chain reaching through `consolidated_literal`, `reader_text`, `literal`, `text`,
`markdown` for whichever was non-empty — cannot be reintroduced field by field.
GOVERNANCE 5: one established text, projected identically into every format.

**Status honesty.** `text_status` is a closed enum — `established`, `partial`,
`no_readable_text` — and never an empty string standing in for one (Tyrel's
2026-08-05 ruling, 4c). A blank page is ordinary material, not a fatal error: his
own words, "It is not a fatal error there might be blank pages." What must never
happen is the opposite collapse — ink that is merely unread quietly reported as
ink that was never there. The three silences he named stay apart: nothing there
(`no_readable_text`, a positive finding with its own evidence), ink present but
unread by a human (a gap, inside `partial`), and ink the machine could not see
properly (also a gap — indistinguishable from the last from inside the pipeline,
and that is fine, so long as neither is ever reported as the first).

**Uncertainty carried whole, never as characters.** The Perlectio's `uncertain`
spans (real characters in `text`, flagged with an explicit certainty, carrying the
reader's own alternative readings) and `illegible` gaps (zero-width anchors that
cannot, structurally, carry a character) are validated and carried into this
record exactly. A witness variant is evidence beside a gap, never a substitute
inside `text` — Tyrel, 2026-07-30 and 2026-08-05: "we don't want it making shit
up."

**Write-once.** An Archetypus already on disk is never rewritten. The run tree
refuses different bytes under the same identity, so this is enforced a layer down
rather than promised here; what this stage adds is that it never tries. A revised
reading is a new pipeline run over the same Exemplar (Tyrel, 4b) — runs are cheap,
silent mutation is not.

**Only for acts the Recensor accepted.** A held act reaches no Archetypus at all.
That is the load-bearing half of "partial cannot look complete" — the absence is
the evidence, and an export that showed a held act as delivered would have to
invent a record that does not exist.

Human correction lives *above* this record, never inside it (4a): the output is a
machine reading, and a corrected text is a different kind of thing.

    python pipeline/6_archetypus/run.py --run-root <dir> --run-id <id>
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.chairs.registry import ChairRegistry  # noqa: E402
from common.contracts.canonical import (  # noqa: E402
    SCHEMA_LABEL,
    digest_of,
    self_hash,
    verify_self_hash,
)
from common.contracts.envelope import validate_input_refs  # noqa: E402
from common.contracts.errors import FatalAccounting, SchemaRefusal  # noqa: E402
from common.contracts.outcomes import OutcomeClass, classify, terminal_category  # noqa: E402
from common.contracts.stages import (  # noqa: E402
    ARCHETYPUS,
    ATTESTATORES,
    DESIGNATOR,
    PERLECTOR,
    RECENSOR,
)
from common.stage import (  # noqa: E402
    EXIT_COMPLETE,
    WITNESS_READING_OUTCOMES,
    expected_acts,
    latest_attempt,
    open_context,
    reading_basis_regions,
    recovery_region_count,
    run_stage,
    stage_parser,
    validate_serving_provenance,
)

# text_status: the closed enum, and the rule that keeps it honest ---------------
#
# Tyrel's ruling, 2026-08-05: "no_readable_text" is a positive finding that a
# page held no ink; a gap (inside "partial") is the opposite claim — ink was
# there and is unread, whether from damage or from a reading limitation. The two
# must never collapse into each other, and this enum is what keeps them apart.
TEXT_STATUSES = frozenset({"established", "partial", "no_readable_text"})

# The two annotation kinds this stage carries whole from the Perlectio. Closed,
# per spec 10 test 5. "uncertain" covers characters that ARE in `text`, flagged
# with a certainty — the mature convention's `<unclear cert="">` (TEI P5 ch. 11,
# "Representation of Primary Sources"; EpiDoc Guidelines, "Unclear characters").
# "illegible" is a zero-width gap anchor, structurally unable to carry characters
# into `text` — `<gap>`, whose own content model never admits character data
# either. Spec 10 asks the shapes to map onto that convention rather than invent
# markup; the *rendering* of either (brackets, underdots, sigla) is the
# Armarium's business at export time and is deliberately not stored here.
ANNOTATION_KINDS = frozenset({"uncertain", "illegible"})

# `<unclear cert="">` in TEI takes a certainty value. Closed here rather than
# free-text, because an open field is a place for a score nobody defined.
CERTAINTIES = frozenset({"high", "medium", "low", "unknown"})

_WITNESS_EVIDENCE_FIELDS = frozenset({"witness_ref", "variant"})
_UNCERTAIN_FIELDS = frozenset({"kind", "start", "end", "certainty", "alternatives"})
_ILLEGIBLE_FIELDS = frozenset({"kind", "start", "end", "witness_evidence"})

# No real text is within light-years of this many characters, so any start/end
# past it is already refused by the bounds check below. The cap exists to keep
# a forged, absurdly large offset from ever reaching Python's own int-to-str
# conversion limit (~4300 digits, CPython's "Integer string conversion length
# limitation", default since 3.11): formatting a start/end that large into
# this refusal's own message would raise ValueError and crash the whole run
# instead of refusing one annotation.
_MAX_PLAUSIBLE_OFFSET = 10**15

# The record's whole field set, closed. A reviewer's question — "is there a
# second text-bearing field?" — is answered mechanically by this rather than by
# reading the constructor. Every field is required; `evidence_ref` is present
# and null except under `no_readable_text`, so the set never varies by act.
_RECORD_FIELDS = frozenset(
    {
        "act_id",
        "act_key",
        "page_id",
        "text",
        "text_hash",
        "status",
        "text_status",
        "regions",
        "provenance",
        "annotations",
        "evidence_ref",
        "dissent_ref",
        "perlectio_ref",
        "recensor_ref",
        "self_hash",
    }
)

_INDEX_ROW_FIELDS = frozenset(
    {"act_id", "act_key", "artifact_id", "text_status", "text_hash", "relative_path", "sha256"}
)
_INDEX_FIELDS = frozenset({"schema", "run_id", "stage", "accepted_count", "rows", "self_hash"})


def _is_ref_shaped(value) -> bool:
    if not isinstance(value, dict) or set(value) != {"relative_path", "sha256"}:
        return False
    try:
        validate_input_refs([value])
    except SchemaRefusal:
        return False
    return True


def _reference_key(reference: dict) -> tuple[str, str]:
    return (reference["relative_path"], reference["sha256"])


def validate_annotations(annotations, text: str, witnesses: dict | None, what: str) -> list[dict]:
    """The Perlectio's uncertainty layer, carried whole and refused if malformed.

    Every span is checked against the text it annotates, every gap is refused
    unless it is a genuine zero-width anchor, and every piece of witness evidence
    must cite a witness this act was actually read against — one of the exact
    references already in this reading's own basis, never an arbitrary one. None
    of this may ever place a character into `text`: there is no field here a
    character could travel through, and the zero-width requirement on a gap makes
    that a property of the schema rather than a rule someone has to remember.

    `witnesses` maps `(relative_path, sha256)` to the text that witness actually
    reported, or `None` where it reported none. A quoted `variant` must be a
    substring of what its cited witness reported: a variant that no witness said
    is neither the ink nor testimony, and there is nothing else it could honestly
    be. Comparison is exact — normalizing here would be a place for the record to
    differ from the testimony it claims to quote.

    **`witnesses=None` is the read-back caller**, `validate_record`, which has a
    sealed record and no reading beside it: the roster lives in the Perlectio's
    basis, so attribution and quotation are the two things it cannot re-check.
    Everything else is checked identically, from this one spelling, because the
    alternative — a second copy of these rules for the record's own read path —
    is a pair that drifts. It already had: the two copies disagreed about
    whether an uncertain span had to cover a readable character.
    """
    if not isinstance(annotations, list):
        raise SchemaRefusal(f"{what} is not a list")
    length = len(text)
    validated: list[dict] = []
    for index, note in enumerate(annotations):
        label = f"{what}[{index}]"
        if not isinstance(note, dict):
            raise SchemaRefusal(f"{label} is not an object")
        kind = note.get("kind")
        if kind not in ANNOTATION_KINDS:
            raise SchemaRefusal(
                f"{label} has kind {kind!r}, which is not one of {sorted(ANNOTATION_KINDS)}"
            )
        start, end = note.get("start"), note.get("end")
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
        ):
            raise SchemaRefusal(f"{label} has a non-integer start or end")
        if abs(start) > _MAX_PLAUSIBLE_OFFSET or abs(end) > _MAX_PLAUSIBLE_OFFSET:
            raise SchemaRefusal(f"{label} has a start or end far outside any plausible text length")
        if start < 0 or end > length or start > end:
            raise SchemaRefusal(
                f"{label} spans [{start}, {end}), which is outside this reading's own text "
                f"bounds [0, {length}]"
            )
        if kind == "illegible":
            allowed_fields = _ILLEGIBLE_FIELDS
            validated_note = {
                "kind": kind,
                "start": start,
                "end": end,
                "witness_evidence": _validate_witness_evidence(note, start, end, witnesses, label),
            }
        else:  # "uncertain"
            allowed_fields = _UNCERTAIN_FIELDS
            validated_note = {
                "kind": kind,
                "start": start,
                "end": end,
                "certainty": _validate_certainty(note, label),
                "alternatives": _validate_alternatives(note, text, start, end, label),
            }
        unknown = set(note) - allowed_fields
        if unknown:
            raise SchemaRefusal(
                f"{label} carries field(s) {sorted(unknown)} outside its closed schema"
            )
        validated.append(validated_note)
    return validated


def _validate_certainty(note: dict, label: str) -> str:
    certainty = note.get("certainty")
    if certainty not in CERTAINTIES:
        raise SchemaRefusal(
            f"{label} has certainty {certainty!r}, which is not one of {sorted(CERTAINTIES)}"
        )
    return certainty


def _validate_alternatives(note: dict, text: str, start: int, end: int, label: str) -> list[str]:
    """The reader's own candidate readings for characters that ARE in `text`.

    Deliberately the Perlector's own alternatives and not a witness's: the
    Perlector reads the ink (ARCHITECTURE), so its uncertainty about a span it
    did read is its own. Witness material attaches to a *gap*, where the reader
    read nothing — which is the only place spec 10 asks for it.

    The span must cover a readable character, not merely a width. An uncertain
    span over blank text is what lets the two silences collapse: on all-blank
    text `derive_text_status` sees no gap and returns `no_readable_text`, so the
    record would positively claim the page held no ink while carrying an
    annotation asserting the reader read characters there and offering
    alternatives for them. Where the reader saw nothing to be uncertain about,
    the honest shape is an illegible gap.
    """
    if not text[start:end].strip():
        raise SchemaRefusal(
            f"{label} is an uncertain span covering no readable character; uncertainty "
            "flags characters that ARE present in `text`, so it must cover at least one, "
            "and where nothing was read the honest shape is an illegible gap"
        )
    alternatives = note.get("alternatives")
    if not isinstance(alternatives, list) or not alternatives:
        raise SchemaRefusal(f"{label} is uncertain but names no alternatives")
    for alternative in alternatives:
        if not isinstance(alternative, str) or not alternative:
            raise SchemaRefusal(f"{label} names an empty or non-string alternative reading")
    if len(set(alternatives)) != len(alternatives):
        raise SchemaRefusal(f"{label} repeats an alternative reading")
    return list(alternatives)


def _validate_witness_evidence(
    note: dict, start: int, end: int, witnesses: dict | None, label: str
) -> list[dict]:
    """What a witness claimed at a gap: retained as evidence, never as characters.

    An absent or empty list is ordinary — every witness may have found the same
    damage the reader did. What is refused is a claim attributed to a witness that
    this act was not read against, or words that witness never reported. Both of
    those need the roster; `witnesses=None` is the read-back caller that has no
    reading to get one from, and checks the shape alone.
    """
    if start != end:
        raise SchemaRefusal(
            f"{label} is an illegible gap with start {start} != end {end}; a gap is a "
            "zero-width anchor and structurally cannot carry characters into `text`, "
            "whatever evidence hangs off it"
        )
    evidence = note.get("witness_evidence", [])
    if not isinstance(evidence, list):
        raise SchemaRefusal(f"{label}.witness_evidence is not a list")
    checked: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for index, item in enumerate(evidence):
        item_label = f"{label}.witness_evidence[{index}]"
        if not isinstance(item, dict):
            raise SchemaRefusal(f"{item_label} is not an object")
        if set(item) != _WITNESS_EVIDENCE_FIELDS:
            raise SchemaRefusal(
                f"{item_label} is not exactly {sorted(_WITNESS_EVIDENCE_FIELDS)}; a candidate-text "
                "field beside a gap would be a second source of established characters"
            )
        witness_ref = item.get("witness_ref")
        if not _is_ref_shaped(witness_ref) or (
            witnesses is not None and _reference_key(witness_ref) not in witnesses
        ):
            raise SchemaRefusal(
                f"{item_label} names a witness reference that is not one of this act's own "
                "witnesses; annotation evidence may only cite a testimonium already in "
                "this reading's basis"
            )
        variant = item.get("variant")
        if not isinstance(variant, str) or not variant:
            raise SchemaRefusal(f"{item_label} names no variant reading")
        if witnesses is not None:
            reported = witnesses[_reference_key(witness_ref)]
            if not isinstance(reported, str) or variant not in reported:
                raise SchemaRefusal(
                    f"{item_label} quotes a variant its cited witness never reported; a variant "
                    "that is neither the ink nor something a witness actually said is a "
                    "reconstruction, and the record carries none"
                )
        identity = (witness_ref["relative_path"], witness_ref["sha256"], variant)
        if identity in seen:
            raise SchemaRefusal(f"{item_label} repeats the same witness claim")
        seen.add(identity)
        checked.append({"witness_ref": witness_ref, "variant": variant})
    return checked


def derive_text_status(text: str, annotations: list[dict]) -> str:
    """established | partial | no_readable_text, from the text and its gaps alone.

    A gap anywhere — regardless of whether `text` is otherwise empty or full —
    means some ink is known and unread: `partial`. Tyrel: "many of our records
    are damaged," so this is expected to be the common case, not an edge one, and
    an act may carry many gaps at once. No gap and no text is the one remaining
    case, and it is the only one this stage may call `no_readable_text`; see
    `validate_text_status` for the evidence that status requires.
    """
    if any(note["kind"] == "illegible" for note in annotations):
        return "partial"
    if text.strip() == "":
        return "no_readable_text"
    return "established"


def validate_text_status(text: str, text_status: str, evidence_ref) -> None:
    """Refuse a status the text does not support.

    Spec 10 test 3: an empty `text` with `established` status is refused at the
    schema. `no_readable_text` is a positive finding (Tyrel, 2026-08-05) and
    requires its own evidence reference — an unlabeled empty string is never
    proof that a page was blank (4c: GOVERNANCE 2's exact enemy).
    """
    if text_status not in TEXT_STATUSES:
        raise SchemaRefusal(f"text_status {text_status!r} is not one of {sorted(TEXT_STATUSES)}")
    if text_status == "established" and text.strip() == "":
        raise SchemaRefusal(
            "an established reading may not carry empty (or all-whitespace) text; an "
            "established reading has text, or it is not established"
        )
    if text_status == "no_readable_text":
        if text.strip() != "":
            raise SchemaRefusal(
                "no_readable_text must carry empty (or all-whitespace) text; text with "
                "actual content is not a positive finding of no ink"
            )
        if not _is_ref_shaped(evidence_ref):
            raise SchemaRefusal(
                "no_readable_text is a positive finding about the page and requires its "
                "evidence reference; an unlabeled empty string is never proof that a page "
                "was blank"
            )
    elif evidence_ref is not None:
        raise SchemaRefusal(
            f"text_status {text_status!r} carries a no_readable_text evidence reference, "
            "which only that status may carry"
        )


# --- The single constructor path: an accepted, primed Perlectio, and nothing else


def artifacts_for(context, stage: str, kind: str, subject: str) -> list[dict]:
    records = []
    for entry in context.tree.build_manifest(stage)["artifacts"]:
        if entry["kind"] == kind and entry["subject_id"] == subject:
            records.append(context.tree.read_artifact(stage, kind, entry["artifact_id"]))
    return records


def final_review(context, act_id: str) -> dict:
    """The Recensor's last word on this act."""
    return latest_attempt(
        artifacts_for(context, RECENSOR, "review", act_id),
        f"review of {act_id}",
        operation="recense",
    )


def reviewed_reading(context, review: dict, act_id: str) -> tuple[dict, dict[str, str]]:
    """Resolve the exact Perlectio the Recensor reviewed, never a newer one.

    `latest_attempt` establishes which review is current.  That review then
    carries the evidence of the reading it assessed.  Looking up the current
    Perlectio independently would silently establish a recovery attempt nobody
    reviewed, which is a reconciliation failure rather than a useful fallback.

    The lookup is also the structural half of "single path": `read_artifact_reference`
    below refuses anything that is not exactly a `(perlector, perlectio)` artifact,
    so a Testimonium, a hypothetical salvage-tier record, or any other kind cannot
    reach this stage by being named in `perlectio_ref` — the reference's declared
    stage and kind are checked against the actual bytes, not merely trusted.
    """
    payload = review.get("payload")
    if not isinstance(payload, dict):
        raise FatalAccounting(f"review of {act_id} has no payload")
    reference = payload.get("perlectio_ref")
    if not isinstance(reference, dict) or reference not in review.get("inputs", []):
        raise FatalAccounting(
            f"accepted review of {act_id} does not retain its digest-checked Perlectio reference"
        )
    reading = context.tree.read_artifact_reference(
        reference,
        stage=PERLECTOR,
        kind="perlectio",
        subject_id=act_id,
    )
    readings = artifacts_for(context, PERLECTOR, "perlectio", act_id)
    current = latest_attempt(readings, f"reading of {act_id}", operation="perlegere")
    if current["artifact_id"] != reading["artifact_id"]:
        raise FatalAccounting(
            f"act {act_id} has a newer Perlectio that the accepted Recensor review did not "
            "assess; no unreconciled reading may become established"
        )
    recovery_regions = recovery_region_count(
        act_id, artifacts_for(context, DESIGNATOR, "region", act_id)
    )
    if len(readings) != recovery_regions + 1:
        raise FatalAccounting(
            f"act {act_id} has {recovery_regions} recovery crop(s) but {len(readings)} "
            "Perlectio attempt(s); a recovery crop must be reread before any text is established"
        )
    return reading, reference


def accepted_primed_perlectio(
    context, review, reading, reading_ref, act_id
) -> tuple[dict, dict, list]:
    """The only material permitted to reach the establishing constructor.

    Spec 10 test 1 names four things that must not reach it: a Testimonium, a
    salvage-tier piece, a Lectio nuda, and a raw Perlectio the Recensor never
    accepted. The stage/kind check inside `read_artifact_reference` already
    closes the first, structurally, before `review`/`reading` ever reach this
    function; the rest are closed here, by name, so a producer that later
    starts labelling its readings cannot slip an unprimed or salvage one
    through on a field this stage does not look at.

    The `stage`/`kind` checks below repeat what `read_artifact_reference`
    already guaranteed for both arguments at their one call site
    (`establish_from_accepted_primed_perlectio`, via `reviewed_reading`) —
    deliberately: this is the single function spec 10 names as the whole of
    the boundary, and a second, cheap check here means a future caller that
    resolves `review`/`reading` some other way still cannot slip past it
    silently.

    This is a boundary, not a ranking mechanism. It compares, counts and scores
    nothing, and the only witness text it reads is checked against a quotation
    later claimed of that same witness. It returns the reading's own payload, the
    witnesses it was read against keyed by digest-checked reference — the roster
    an annotation may cite, and nothing more — and the regions it read.
    """
    review_payload = review.get("payload")
    if (
        review.get("stage") != RECENSOR
        or review.get("kind") != "review"
        or review.get("outcome") != "accepted"
        or not isinstance(review_payload, dict)
        or review_payload.get("perlectio_ref") != reading_ref
        or reading_ref not in review.get("inputs", [])
    ):
        raise SchemaRefusal(
            f"the Archetypus constructor for {act_id} accepts only the exact Perlectio a "
            "Recensor accepted"
        )
    if reading.get("stage") != PERLECTOR or reading.get("kind") != "perlectio":
        raise SchemaRefusal("only a Perlectio may enter the Archetypus constructor")
    reading_class = classify(PERLECTOR, reading.get("outcome"))
    if reading_class is not OutcomeClass.COMPLETED:
        raise FatalAccounting(
            f"act {act_id} would be established from a {reading['outcome']!r} "
            f"reading ({reading_class.value}); the established text may only come "
            "from a reading that succeeded, and a failed one is held, never written"
        )
    payload = reading.get("payload")
    if not isinstance(payload, dict):
        raise SchemaRefusal(f"the accepted Perlectio for {act_id} has no object payload")

    # No Perlectio in this build records primed/unprimed — that field is the
    # Perlector lane's to add. Until it exists, an unlabeled reading is accepted
    # and an explicitly unprimed one is refused, so the check is already in place
    # when the producer starts writing the field. The retained Testimonium basis
    # below is the transitional indication that a reading was primed at all; it is
    # a compatibility assumption, not proof, and it is named as one here.
    lectio_kind = payload.get("lectio_kind")
    if lectio_kind is not None and lectio_kind != "primed":
        raise SchemaRefusal(
            f"act {act_id} names lectio_kind {lectio_kind!r}; only an explicitly primed "
            "Lectio may establish, and a Lectio nuda is an instrument record, never an "
            "establishing read"
        )
    if payload.get("primed") not in (None, True):
        raise SchemaRefusal(
            f"act {act_id} carries an explicitly non-primed Lectio, which is an instrument "
            "record, never an establishing read"
        )
    for field in ("tier", "source_tier", "reading_tier"):
        if payload.get(field) == "salvage":
            raise SchemaRefusal(
                f"act {act_id} carries salvage-tier material, which can never become an "
                "Archetypus (invariant #31's boundary)"
            )

    # Regions first, so a resealed reading with no object basis is refused by the
    # shared helper that every consumer of a completed Perlectio uses, in the same
    # words, rather than by this stage's narrower witness check.
    regions = reading_basis_regions(reading, f"accepted reading of {act_id}")
    basis = payload.get("basis")
    testimonia = basis.get("testimonia") if isinstance(basis, dict) else None
    if not isinstance(testimonia, list) or not testimonia:
        raise SchemaRefusal(
            f"act {act_id} reached the Archetypus constructor with no retained Testimonium "
            "basis; a reading shown no witness at all is a Lectio nuda by any other name"
        )
    witnesses: dict[tuple[str, str], str | None] = {}
    for index, item in enumerate(testimonia):
        if not isinstance(item, dict):
            raise SchemaRefusal(f"act {act_id} Testimonium basis {index} is not an object")
        reference = item.get("reference")
        if not _is_ref_shaped(reference) or reference not in reading.get("inputs", []):
            raise SchemaRefusal(
                f"act {act_id} Testimonium basis {index} is not a digest-checked direct input "
                "of the reading that claims to have seen it"
            )
        testimonium = context.tree.read_artifact_reference(
            reference,
            stage=ATTESTATORES,
            kind="testimonium",
            subject_id=act_id,
        )
        reference_key = _reference_key(reference)
        if reference_key in witnesses:
            raise SchemaRefusal(
                f"act {act_id} repeats Testimonium basis {index}; one retained witness "
                "reference may not count twice as evidence that this reading was primed"
            )
        testimonium_payload = testimonium.get("payload")
        if not isinstance(testimonium_payload, dict):
            raise SchemaRefusal(f"act {act_id} Testimonium basis {index} has no object payload")
        reported = testimonium_payload.get("reported")
        witnesses[reference_key] = (
            reported if testimonium["outcome"] in WITNESS_READING_OUTCOMES else None
        )
    return payload, witnesses, regions


def validate_record_fields(record: dict) -> None:
    """The closed record schema, checked mechanically rather than by reading.

    The old pipeline's export reached through five spellings of "the text" for
    whichever was non-empty. That shape cannot be reintroduced one field at a
    time while this refuses any field the record is not defined to carry, and
    any absence of one it is.
    """
    unexpected = sorted(set(record) - _RECORD_FIELDS)
    missing = sorted(_RECORD_FIELDS - set(record))
    if unexpected or missing:
        raise SchemaRefusal(
            f"the Archetypus record schema is closed and this one does not match it "
            f"(missing {missing}, unexpected {unexpected}); a second text-bearing field is "
            "the named dead shape this refuses"
        )


def validate_record(record: dict) -> dict:
    """Refuse a malformed record before an index can repeat its claims.

    The constructor checks the upstream evidence.  This validator checks the
    sealed record itself on every later stage-local read: its one-text schema,
    nested self-hash, text hash, status derivation, references, and the scalar
    identities needed to return to the act.  A derived index must not turn a
    resealed but internally contradictory payload into a trusted summary.

    The annotation layer goes back through `validate_annotations`, without a
    witness roster, and the result must equal what is stored: anything the
    constructor would not have written is refused, without a second copy of the
    annotation rules living here to drift from the first.
    """
    if not isinstance(record, dict):
        raise SchemaRefusal("the Archetypus record is not an object")
    validate_record_fields(record)
    if not verify_self_hash(record):
        raise SchemaRefusal("the Archetypus record fails its nested self-hash")
    for field in ("act_id", "act_key", "page_id"):
        if not isinstance(record[field], str) or not record[field]:
            raise SchemaRefusal(f"the Archetypus record has no {field}")
    text = record["text"]
    if not isinstance(text, str):
        raise SchemaRefusal("the Archetypus text is not a string")
    if record["text_hash"] != digest_of(text):
        raise SchemaRefusal("the Archetypus text_hash disagrees with its one text")
    if record["status"] != "established":
        raise SchemaRefusal("the Archetypus record status is not the fixed 'established' literal")
    annotations = record["annotations"]
    if validate_annotations(annotations, text, None, "Archetypus annotation") != annotations:
        raise SchemaRefusal(
            "the Archetypus annotations are not in the exact form validation produces; a "
            "resealed record may not carry a shape the constructor would never have written"
        )
    derived_status = derive_text_status(text, annotations)
    if record["text_status"] != derived_status:
        raise SchemaRefusal(
            f"the Archetypus text_status {record['text_status']!r} disagrees with its text "
            f"and gaps (expected {derived_status!r})"
        )
    validate_text_status(text, record["text_status"], record["evidence_ref"])
    if not isinstance(record["regions"], list) or not record["regions"]:
        raise SchemaRefusal("the Archetypus record retains no source region")
    if not isinstance(record["provenance"], dict):
        raise SchemaRefusal("the Archetypus provenance is not an object")
    for field in ("dissent_ref", "perlectio_ref", "recensor_ref"):
        if not _is_ref_shaped(record[field]):
            raise SchemaRefusal(f"the Archetypus {field} is not a digest-checked reference")
    if record["dissent_ref"] != record["perlectio_ref"]:
        raise SchemaRefusal("dissent must travel by reference to this record's one Perlectio")
    return record


def _no_readable_text_evidence(review: dict) -> dict[str, str] | None:
    """Return the Recensor's retained blank proof; never manufacture one here."""
    payload = review.get("payload")
    if not isinstance(payload, dict):
        raise SchemaRefusal("accepted Recensor review has no object payload")
    reference = payload.get("no_readable_text_evidence_ref")
    if reference is None:
        return None
    if not _is_ref_shaped(reference) or reference not in review.get("inputs", []):
        raise SchemaRefusal(
            "no_readable_text evidence is not a digest-checked direct input of the Recensor review"
        )
    return reference


def _crop_input(context, image_path: str, act_id: str) -> dict[str, str]:
    """A digest-checked reference to one crop the accepted reading names.

    `input_ref` hashes the bytes on disk, so a crop the reading names but the
    tree does not hold raises `OSError` from inside a list comprehension —
    outside the `ContractError` family `run_stage` classifies, so the whole run
    ends in a traceback and exit 1 rather than one refused act and exit 2. A
    missing crop is an ordinary accounting failure (an interrupted copy, a
    pruned blob) as much as a forged one, and it is named as such here.
    """
    try:
        return context.input_ref(image_path)
    except OSError as error:
        raise FatalAccounting(
            f"accepted reading of {act_id} names crop {image_path!r}, which this run tree "
            f"cannot read: {error}"
        ) from error


def _direct_inputs(*groups: list[dict[str, str]]) -> list[dict[str, str]]:
    """Combine the record's required evidence, naming one path only once.

    `validate_input_refs` refuses an envelope that lists a path twice, and a
    reading may legitimately name the same crop from two regions — or name the
    review or the Perlectio itself as its crop. Every digest here was read off
    the same disk moments earlier, so two entries for one path cannot disagree;
    the only work is the deduplication.
    """
    by_path: dict[str, dict[str, str]] = {}
    for group in groups:
        for reference in group:
            by_path[reference["relative_path"]] = reference
    return list(by_path.values())


def establish_from_accepted_primed_perlectio(
    context, *, act: dict, review_ref: dict[str, str]
) -> tuple[dict, list[dict[str, str]]]:
    """The one public constructor, resolving all canonical characters from evidence.

    A caller supplies only an act and a sealed Recensor-review reference.  It
    cannot pass an in-memory reading or text around the acceptance boundary.
    """
    if not _is_ref_shaped(review_ref):
        raise SchemaRefusal("accepted Recensor review reference is malformed")
    review = context.tree.read_artifact_reference(
        review_ref, stage=RECENSOR, kind="review", subject_id=act["act_id"]
    )
    reading, reading_ref = reviewed_reading(context, review, act["act_id"])
    payload, witnesses, regions = accepted_primed_perlectio(
        context, review, reading, reading_ref, act["act_id"]
    )
    if "text" not in payload:
        raise SchemaRefusal(
            "the accepted Perlectio has no text field; missing evidence cannot mean blank"
        )
    text = payload["text"]
    if not isinstance(text, str):
        raise SchemaRefusal("the accepted Perlectio text is not a string")
    annotations = validate_annotations(
        payload.get("annotations", []),
        text,
        witnesses,
        f"accepted reading of {act['act_id']} annotations",
    )
    text_status = derive_text_status(text, annotations)
    evidence_ref = _no_readable_text_evidence(review)
    if evidence_ref is not None and text_status != "no_readable_text":
        raise FatalAccounting(
            f"the accepted review of {act['act_id']} retains a proof that this act held no "
            f"readable ink, but the reading it accepted establishes {text_status!r} text; two "
            "upstream claims that contradict each other are a reconciliation failure, and "
            "dropping the unread one is how the contradiction disappears"
        )
    validate_text_status(text, text_status, evidence_ref)
    validate_serving_provenance(
        context,
        payload.get("provenance"),
        producer_stage=PERLECTOR,
        require_receipt=True,
    )
    # The one construction site.  There is no helper accepting free-standing
    # canonical text: these characters are in scope only because this function
    # resolved them from the exact accepted Perlectio above.
    record = {
        "act_id": act["act_id"],
        "act_key": act["act_key"],
        "page_id": act["page_id"],
        "text": text,
        "text_hash": digest_of(text),
        # The Armarium's frozen record-level literal; text_status separately
        # describes whether the one reading is full, partial, or blank-proved.
        "status": "established",
        "text_status": text_status,
        "regions": regions,
        "provenance": payload.get("provenance"),
        "annotations": annotations,
        "evidence_ref": evidence_ref,
        # Dissent lives inside the one Perlectio and therefore travels by the
        # same reference, never as a copied value beside the established text.
        "dissent_ref": reading_ref,
        "perlectio_ref": reading_ref,
        "recensor_ref": review_ref,
    }
    record["self_hash"] = self_hash(record)
    validate_record(record)
    # Not `evidence_ref`: the Armarium's frozen `verify_established_record`
    # reconciles an Archetypus's own `inputs` against exactly
    # `[review_ref, reading_ref, *region refs]` and was not written to expect a
    # fourth kind (off-limits this round). `evidence_ref` is already proven to
    # be a digest-checked direct input of the *review* by
    # `_no_readable_text_evidence` above, and the record's own self-hash makes
    # the reference itself tamper-evident, so nothing is unchecked by leaving
    # it out here -- and leaving it in would make every future
    # `no_readable_text` record unexportable the day a real blank-proof
    # reference stops coinciding with `reading_ref`.
    inputs = _direct_inputs(
        [review_ref, reading_ref],
        [_crop_input(context, region["image_path"], act["act_id"]) for region in regions],
    )
    return record, inputs


# --- index.json: a rebuildable manifest, reconciled against the accepted acts ---


def accepted_act_ids(context) -> set[str]:
    """The acts whose current Recensor review is exactly `accepted`.

    Recomputed from the immutable review records rather than remembered from
    this invocation's own loop: spec 10 test 6 asks the index to reconcile with
    what the *Recensor* accepted, and an index checked only against the list the
    writer just built would agree with itself about an act it had skipped.
    """
    accepted: set[str] = set()
    for act in expected_acts(context):
        if final_review(context, act["act_id"])["outcome"] == "accepted":
            accepted.add(act["act_id"])
    return accepted


def _archetypus_rows(context) -> list[dict]:
    """One row per immutable Archetypus record on disk, refusing a duplicate act."""
    rows: list[dict] = []
    seen: set[str] = set()
    for entry in context.tree.build_manifest(ARCHETYPUS)["artifacts"]:
        if entry["kind"] != "archetypus":
            continue
        subject = entry["subject_id"]
        if subject in seen:
            raise FatalAccounting(
                f"act {subject} carries more than one Archetypus record on disk; a duplicate "
                "row is not an additional established reading, and there is no rule for "
                "choosing one established text"
            )
        seen.add(subject)
        record = context.tree.read_artifact(ARCHETYPUS, "archetypus", entry["artifact_id"])
        payload = validate_record(record["payload"])
        if payload["act_id"] != subject:
            raise FatalAccounting(
                f"Archetypus artifact for {subject} carries payload identity {payload['act_id']!r}"
            )
        rows.append(
            {
                "act_id": subject,
                "act_key": payload["act_key"],
                "artifact_id": entry["artifact_id"],
                "text_status": payload["text_status"],
                "text_hash": payload["text_hash"],
                "relative_path": entry["relative_path"],
                "sha256": entry["sha256"],
            }
        )
    return sorted(rows, key=lambda row: row["act_id"])


def build_index(context) -> dict:
    """The rebuildable per-run summary of every Archetypus record this run holds.

    Derived from the immutable per-act records on disk — spec 01's
    artifact/manifest split, "never the only evidence" — exactly as
    `manifest.json` is, and safe to delete and rebuild identically.
    """
    rows = _archetypus_rows(context)
    index = {
        "schema": SCHEMA_LABEL,
        "run_id": context.tree.run_id,
        "stage": ARCHETYPUS,
        "accepted_count": len(rows),
        "rows": rows,
    }
    index["self_hash"] = self_hash(index)
    return index


def validate_index(context, index) -> dict:
    """Spec 10 test 6, as a consumer check: 1:1 with the acts the Recensor accepted.

    The index is derived and rewritable. That does not make a missing or
    duplicate row harmless where someone relies on it for accounting: it is
    FATAL until it is regenerated from the immutable records, never a warning
    and never quietly repaired underneath a reader.
    """
    if not isinstance(index, dict) or set(index) != _INDEX_FIELDS:
        raise FatalAccounting("the Archetypus index is not the closed derived-index shape")
    if index["schema"] != SCHEMA_LABEL or index["run_id"] != context.tree.run_id:
        raise FatalAccounting("the Archetypus index belongs to a different schema or run")
    if index["stage"] != ARCHETYPUS or not verify_self_hash(index):
        raise FatalAccounting("the Archetypus index fails its own stage label or self-hash")
    rows = index["rows"]
    if not isinstance(rows, list):
        raise FatalAccounting("the Archetypus index rows are not a list")
    if (
        not isinstance(index["accepted_count"], int)
        or isinstance(index["accepted_count"], bool)
        or index["accepted_count"] < 0
    ):
        raise FatalAccounting("the Archetypus index accepted_count is not a non-negative integer")

    on_disk = {row["act_id"]: row for row in _archetypus_rows(context)}
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != _INDEX_ROW_FIELDS:
            raise FatalAccounting("the Archetypus index carries a malformed row")
        if any(
            not isinstance(row[field], str) or not row[field]
            for field in ("act_id", "act_key", "artifact_id", "text_status", "text_hash")
        ) or not _is_ref_shaped(
            {"relative_path": row.get("relative_path"), "sha256": row.get("sha256")}
        ):
            raise FatalAccounting("the Archetypus index carries a row with malformed values")
        act_id = row["act_id"]
        if act_id in seen:
            raise FatalAccounting(f"the Archetypus index carries a duplicate row for act {act_id}")
        seen.add(act_id)
        if on_disk.get(act_id) != row:
            raise FatalAccounting(
                f"the Archetypus index row for act {act_id} does not match its immutable record"
            )
    if index["accepted_count"] != len(rows):
        raise FatalAccounting("the Archetypus index count disagrees with the rows it carries")

    accepted = accepted_act_ids(context)
    if seen != accepted or set(on_disk) != accepted:
        raise FatalAccounting(
            f"the Archetypus records and index ({sorted(seen)}) do not reconcile 1:1 with the "
            f"acts the Recensor accepted ({sorted(accepted)}); a missing or duplicate row is a "
            "fatal accounting imbalance, never a warning"
        )
    return index


def main(registry_factory=ChairRegistry.from_toml) -> int:
    """Run under the explicitly supplied chair/config implementation."""
    args = stage_parser(__doc__.splitlines()[0]).parse_args()
    context = open_context(args, ARCHETYPUS, registry_factory=registry_factory)

    for act in expected_acts(context):
        act_id = act["act_id"]
        review = final_review(context, act_id)

        # An act the seal already holds is terminal at the Designator. If the
        # Recensor nonetheless accepted it, establishing a text here would
        # resurrect a held act into a delivered one — refused before a single
        # character is written, because the Archetypus is the last stage before
        # the text exists.
        if terminal_category(DESIGNATOR, act["outcome"]) is not None and (
            review["outcome"] == "accepted"
        ):
            raise FatalAccounting(
                f"act {act_id} is {act['outcome']!r} at the proposal seal, but the "
                "Recensor accepted it; a stage may not resurrect a held act into "
                "an established reading"
            )

        if review["outcome"] != "accepted":
            # Deliberately nothing. A held, recovery-requested, held-for-review,
            # or confirmed-blank act is already terminal at (or before) the
            # Recensor — the outcome algebra resolves its category without an
            # Archetypus record — and that absence is what the Armarium
            # reconciles against.
            continue

        review_ref = context.artifact_ref(RECENSOR, "review", review["artifact_id"])
        record, inputs = establish_from_accepted_primed_perlectio(
            context, act=act, review_ref=review_ref
        )

        context.publish(
            kind="archetypus",
            subject_id=act_id,
            outcome="established",
            inputs=inputs,
            payload=record,
        )

    # Rebuilt from the immutable records every run, then read back and reconciled
    # against the Recensor's accepted set before anything may rely on it.
    context.tree.write_index(ARCHETYPUS, build_index(context))
    validate_index(context, context.tree.read_index(ARCHETYPUS))
    context.finish()
    return EXIT_COMPLETE


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
