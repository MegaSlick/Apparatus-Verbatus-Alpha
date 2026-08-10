"""Archetypus: exactly one established reading per act, written once.

The authoritative pipeline output — a machine reading, not truth. Five things a
reader needs that the code below does not say for itself.

**The dead shape.** The audit found the old pipeline decided its established text
twice, and its export then reached through `consolidated_literal`, `reader_text`,
`literal`, `text`, `markdown` for whichever was non-empty. Every closed field set
in this file exists to stop that being rebuilt one field at a time — including
`_REGION_FIELDS`, because a region is embedded whole and travels into the export
whole (GOVERNANCE 5: one established text, projected identically).

**The three silences, which must never collapse** (Tyrel, 2026-08-05). Nothing
there — `no_readable_text`, a positive finding carrying its own evidence. Ink
present and unread by a human. Ink the machine could not see. The last two are
indistinguishable from inside the pipeline and both are gaps, inside `partial`;
that is fine. Reporting either of them as the first is not. A blank page is
ordinary material either way — "It is not a fatal error there might be blank
pages" — so the refusals here are about the confusion, never about blankness.

**A witness variant is evidence beside a gap, never a substitute inside `text`**
(Tyrel, 2026-07-30: "we don't want it making shit up").

**Write-once is enforced a layer down**, by the run tree refusing different bytes
under one identity. What this stage adds is that it never tries: a revised
reading is a new run over the same Exemplar (4b). Human correction lives *above*
this record (4a) — a corrected text is a different kind of thing.

**A held act reaches no Archetypus at all**, and that absence is the evidence the
Armarium reconciles against. It is the load-bearing half of "partial cannot look
complete": an export showing a held act as delivered would have to invent a
record that does not exist.

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

# The three silences, kept apart. See the module docstring for the ruling.
TEXT_STATUSES = frozenset({"established", "partial", "no_readable_text"})

# Spec 10 asks these shapes to map onto the mature convention rather than invent
# markup: `<unclear cert="">` for characters that ARE in `text`, and `<gap>` —
# whose content model never admits character data — for a zero-width anchor where
# none were read (TEI P5 ch. 11, "Representation of Primary Sources"; EpiDoc
# Guidelines, "Unclear characters"). Rendering either one is the Armarium's
# business at export time and is deliberately not stored.
ANNOTATION_KINDS = frozenset({"uncertain", "illegible"})

# Closed rather than free-text, because an open certainty field is a place for a
# score nobody defined.
CERTAINTIES = frozenset({"high", "medium", "low", "unknown"})

_WITNESS_EVIDENCE_FIELDS = frozenset({"witness_ref", "variant"})
_UNCERTAIN_FIELDS = frozenset({"kind", "start", "end", "certainty", "alternatives"})
_ILLEGIBLE_FIELDS = frozenset({"kind", "start", "end", "witness_evidence"})

# The bounds check below already refuses any offset this large. The cap is about
# the refusal *message*: CPython raises ValueError rather than format an int of
# more than ~4300 digits ("Integer string conversion length limitation", default
# since 3.11), so a forged offset would crash the run inside the code written to
# refuse it.
_MAX_PLAUSIBLE_OFFSET = 10**15

# The record's whole field set, closed, so "is there a second text-bearing field?"
# is answered mechanically rather than by reading the constructor. Every field is
# required; `evidence_ref` is present and null except under `no_readable_text`,
# so the set never varies by act.
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

# Exactly what the shared crop verification returns to the Perlector
# (`common/exemplar_boundary.py`), plus the `witness_covered` flag the Perlector
# adds before sealing its basis. Spelled out rather than derived, so a producer
# that starts writing a tenth field is refused here and read by a person instead
# of travelling sealed into the record and out through the export.
_REGION_FIELDS = frozenset(
    {
        "region_id",
        "image_path",
        "image_sha256",
        "verified_dimensions",
        "source_page_ordinal",
        "source_page_id",
        "transform",
        "structure_provenance",
        "witness_covered",
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

    Nothing here can place a character into `text`, and that is a property of the
    schema rather than a rule someone has to remember: no field on either kind
    could carry one, and a gap is required to be zero-width.

    `witnesses` maps `(relative_path, sha256)` to the text that witness actually
    reported, or `None` where it reported none. A quoted `variant` must be a
    substring of what its cited witness reported: a variant no witness said is
    neither the ink nor testimony, and there is nothing else it could honestly be.
    The comparison is exact, because normalizing is where a record starts to
    differ from the testimony it claims to quote.

    `witnesses=None` is the read-back caller, `validate_record`, which holds a
    sealed record and no reading: the roster lives in the Perlectio's basis, so
    attribution and quotation are the two things it cannot re-check. Everything
    else it checks from this one spelling, because a second copy of these rules
    for the record's own read path is a pair that drifts.
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

    The Perlector's alternatives and not a witness's: it reads the ink
    (ARCHITECTURE), so its uncertainty about a span it did read is its own.
    Witness material attaches to a *gap*, which is the only place spec 10 asks
    for it.

    The span must cover a readable character rather than merely a width, because
    a span over blank text is where the silences collapse: `derive_text_status`
    finds no gap there and returns `no_readable_text`, so one record would claim
    the act held no ink while carrying an annotation asserting characters were
    read at that position and offering alternatives for them.
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

    A gap anywhere means some ink is known and unread, whether `text` is
    otherwise empty or full: `partial`, which Tyrel expects to be the common case
    rather than the edge one — "many of our records are damaged". No gap and no
    text is the only remaining case, and the only one this stage may call
    `no_readable_text`; `validate_text_status` holds the evidence it then owes.
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

    The current review carries the evidence of the reading it assessed. Looking
    the current Perlectio up independently would silently establish a recovery
    attempt nobody reviewed — a reconciliation failure, not a useful fallback.

    This lookup is also the structural half of "single path": the reference's
    declared stage and kind are checked against the actual bytes, so nothing but
    a `(perlector, perlectio)` artifact can reach this stage by being named in
    `perlectio_ref`.
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
    accepted. `read_artifact_reference` closes the first structurally, before
    either argument arrives; the rest are closed here by name, so a producer that
    later starts labelling its readings cannot slip an unprimed or salvage one
    through on a field this stage does not look at.

    The `stage`/`kind` checks below are redundant at the one current call site,
    and kept anyway: this is the single function spec 10 names as the whole of
    the boundary, and a caller that resolves `review`/`reading` some other way
    must not be able to walk past it in silence.

    **A boundary, not a ranking mechanism.** It compares, counts and scores
    nothing. The only witness text it reads is the roster an annotation may
    cite, and every entry of that roster is a digest-checked direct input of the
    reading that claims to have seen it.
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

    # No Perlectio in this build records primed/unprimed; that field is the
    # Perlector lane's to add. So an unlabeled reading is accepted and an
    # explicitly unprimed one refused, and the check is already in place when the
    # producer starts writing it. Until then the retained Testimonium basis below
    # stands in — a compatibility assumption, not proof, named as one here
    # because everything else in this function is proof.
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

    # Regions first, so a resealed reading with no object basis is refused in the
    # words every consumer of a completed Perlectio uses, rather than in this
    # stage's narrower ones.
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

    The constructor checks the upstream evidence; this checks the sealed record
    itself, on every later stage-local read, so that a derived index cannot turn
    a resealed but internally contradictory payload into a trusted summary.

    The annotation layer goes back through `validate_annotations` — without a
    witness roster, which a record read off disk cannot have — and the result
    must equal what is stored, so no second copy of those rules lives here to
    drift from the first.
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


def _crop_references(context, regions: list[dict], act_id: str) -> list[dict[str, str]]:
    """Close the regions this record will carry, and prove each crop by its bytes.

    A region is embedded from the reading verbatim, self-hashed into the record,
    and copied field-for-field into the export, so the record's own closed field
    set says nothing about what rides inside one. Hence an allowlist, for the
    reason `validate_serving_provenance` gives about provenance: a denylist
    passes whatever a later producer invents. **Extras are refused rather than
    dropped** — the Armarium compares this list to the reading's own for exact
    equality, so filtering here would refuse a legitimate act at export instead.

    The declared `image_sha256` is checked against the bytes because the stage
    before and the stage after both check it and this one is the stage that makes
    the record immutable: a record sealed naming a digest its crop does not have
    can only be abandoned with the whole run, never repaired.

    `input_ref` hashes the bytes on disk, so an unreadable crop arrives as
    `OSError`, outside the `ContractError` family `run_stage` classifies — a bare
    traceback and exit 1, taking every other act's record with it, for what is as
    often a pruned blob as a forged reading.
    """
    references = []
    for index, region in enumerate(regions):
        label = f"accepted reading of {act_id} region {index}"
        unexpected = sorted(set(region) - _REGION_FIELDS)
        missing = sorted(_REGION_FIELDS - set(region))
        if unexpected or missing:
            raise SchemaRefusal(
                f"{label} is outside the closed region schema (missing {missing}, unexpected "
                f"{unexpected}); a region travels into this record and out through the export "
                "whole, so a field beside the crop facts is a second unvalidated payload"
            )
        image_path = region["image_path"]
        try:
            reference = context.input_ref(image_path)
        except OSError as error:
            raise FatalAccounting(
                f"{label} names crop {image_path!r}, which this run tree cannot read: {error}"
            ) from error
        if region["image_sha256"] != reference["sha256"]:
            raise FatalAccounting(
                f"{label} declares crop digest {region['image_sha256']!r} but the bytes at "
                f"{image_path!r} hash to {reference['sha256']!r}; the record would be sealed, "
                "immutably, naming ink it does not point at"
            )
        references.append(reference)
    return references


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
    # Before the record exists, not after: these regions are about to be
    # self-hashed into something write-once.
    crop_references = _crop_references(context, regions, act["act_id"])
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
    # The one construction site. No helper anywhere accepts free-standing
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
    # Not `evidence_ref`, however natural it looks beside the other two. The
    # Armarium's frozen `verify_established_record` reconciles these `inputs`
    # against exactly `[review_ref, reading_ref, *crop refs]`, so a fourth kind
    # makes every `no_readable_text` record unexportable. Nothing goes unchecked
    # for its absence: `_no_readable_text_evidence` has already proven it a
    # digest-checked direct input of the *review*.
    return record, _direct_inputs([review_ref, reading_ref], crop_references)


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
