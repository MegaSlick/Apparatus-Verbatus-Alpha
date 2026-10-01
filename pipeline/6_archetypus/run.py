"""Archetypus: exactly one established reading per act, written once.

The authoritative pipeline output — a machine reading, not truth. Every closed
field set in this file exists so a producer cannot rebuild a second text field
one name at a time (one established text, projected identically);
`_REGION_FIELDS` closes the same way because a region is embedded whole and
travels into the export whole.

**Three silences, never collapsed into each other: `no_readable_text`** — a
positive finding carrying its own evidence; **ink present and unread by a
human**; and **ink the machine could not see**. The last two are
indistinguishable from inside the pipeline and both are gaps inside `partial`
-- fine on their own, but never reported as the first. A blank page is
ordinary material either way, so the refusals here are about the confusion,
never about blankness.

**A witness variant is evidence beside a gap, never a substitute inside `text`.**

**Write-once is enforced a layer down**, by the run tree refusing different
bytes under one identity; this stage adds only that it never tries -- a
revised reading is a new run over the same Exemplar (4b), and human
correction lives *above* this record (4a) as a different kind of thing.

**A held act reaches no Archetypus record at all**, and that absence is the
evidence the Armarium reconciles against: an export showing a held act as
delivered would have to invent a record that does not exist.

    python pipeline/6_archetypus/run.py --run-root <dir> --run-id <id>
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common import page_path  # noqa: E402
from common.chairs.registry import ChairRegistry  # noqa: E402
from common.contracts.annotations import (  # noqa: E402, F401  (re-export)
    ANNOTATION_KINDS,
    CERTAINTIES,
    validate_annotations,
)
from common.contracts.canonical import (  # noqa: E402
    SCHEMA_LABEL,
    digest_of,
    self_hash,
    verify_self_hash,
)
from common.contracts.envelope import validate_input_refs  # noqa: E402
from common.contracts.errors import ContractError, FatalAccounting, SchemaRefusal  # noqa: E402
from common.contracts.outcomes import (  # noqa: E402
    TEXT_STATUSES,
    derive_record_text_status,
    terminal_category,
)
from common.contracts.outcomes import derive_text_status as derive_text_status  # noqa: E402
from common.contracts.stages import (  # noqa: E402
    ARCHETYPUS,
    PERLECTOR,
    RECENSOR,
)
from common.contracts.uncertainty import from_page_perlectio  # noqa: E402
from common.contracts.uncertainty import validate as validate_uncertainty
from common.exemplar_boundary import verify_reading_region_lineage  # noqa: E402
from common.page_review import (  # noqa: E402
    current_page_reviews,
    require_establishable,
    reviewed_rows,
)
from common.page_testimonia import (  # noqa: E402
    current_page_testimonia,
    shown_page_witnesses,
)
from common.stage import (  # noqa: E402
    EXIT_COMPLETE,
    EXIT_HELD,
    open_stage_context,
    reading_acts,
    run_stage,
    stage_parser,
    validate_serving_provenance,
)

DESCRIPTION = "Archetypus: exactly one established reading per act, written once."

# The three silences and their derivation live in `common/contracts/outcomes.py`,
# not here, because the Armarium recomputes the same status from the layers
# beside the text at export and stages talk only through `common/`
# (`pipeline/test_stage_import_boundaries.py`); a private copy here would be a
# second spelling that drifts. `derive_text_status` is re-exported because this
# stage's tests exercise it directly.

# Spec 10 maps onto the mature TEI P5/EpiDoc convention rather than inventing
# markup: `<unclear cert="">` for characters that ARE in `text`, `<gap>` for a
# zero-width anchor where none were read. Rendering either is the Armarium's
# business at export time, deliberately not stored. The layer's closed
# vocabularies and validator live in `common/contracts/annotations.py` and are
# re-exported here for the same reason: one spelling, so producer and consumer
# cannot drift about what the layer may hold.

# The record's whole field set, closed, so "is there a second text-bearing field?"
# is answered mechanically rather than by reading the constructor. Every field is
# required; `evidence_ref` is present and null except under `no_readable_text`,
# so the set never varies by act.
_RECORD_FIELDS = frozenset(
    {
        "act_id",
        "act_key",
        "page_id",
        "kind",
        "text",
        "text_hash",
        "status",
        "text_status",
        "regions",
        "provenance",
        "annotations",
        "uncertainty",
        "evidence_ref",
        "dissent_ref",
        "perlectio_ref",
        "recensor_ref",
        "self_hash",
    }
)

# `kind` is `act` or `other`: the Perlector names both on a page, and each is
# established so every export shows the one established reading.
READING_KINDS = frozenset({"act", "other"})
# Exactly what `verify_reading_region_lineage` proves of the Perlector's
# `act-region`. Spelled out, so a producer that starts writing another field is
# refused here rather than travelling sealed into the record and the export.
_REGION_FIELDS = frozenset(
    {
        "region_id",
        "image_path",
        "image_sha256",
        "verified_dimensions",
        "source_page_ordinal",
        "source_page_id",
        "transform",
    }
)

# An index row names the reading's `kind`, so a reader of the index alone can
# tell an act from an other reading.
_INDEX_ROW_FIELDS = frozenset(
    {
        "act_id",
        "act_key",
        "kind",
        "artifact_id",
        "text_status",
        "text_hash",
        "relative_path",
        "sha256",
    }
)
_INDEX_FIELDS = frozenset({"schema", "run_id", "stage", "record_count", "rows", "self_hash"})


def _is_ref_shaped(value) -> bool:
    if not isinstance(value, dict) or set(value) != {"relative_path", "sha256"}:
        return False
    try:
        validate_input_refs([value])
    except SchemaRefusal:
        return False
    return True


def validate_text_status(text: str, text_status: str, evidence_ref) -> None:
    """Refuse a status the text does not support.

    Spec 10 test 3: an empty `text` with `established` status is refused at the
    schema. `no_readable_text` is a positive finding and
    requires its own evidence reference — an unlabeled empty string is never
    proof that a page was blank (4c: exactly the silent loss this pipeline refuses).
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


def validate_record_fields(record: dict) -> None:
    """The closed record schema, checked mechanically rather than by reading.

    Refuses any field the record is not defined to carry, and any absence of
    one it is, so a second text-bearing field cannot be reintroduced one name
    at a time (CONTRACT.md, `kind="archetypus"`).
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
    if record["kind"] not in READING_KINDS:
        raise SchemaRefusal(
            f"the Archetypus record kind {record['kind']!r} is neither act nor other"
        )
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
    validate_uncertainty(record["uncertainty"], text)
    derived_status = derive_record_text_status(text, annotations, record["uncertainty"])
    if record["text_status"] != derived_status:
        raise SchemaRefusal(
            f"the Archetypus text_status {record['text_status']!r} disagrees with its text "
            f"and gaps (expected {derived_status!r})"
        )
    validate_text_status(text, record["text_status"], record["evidence_ref"])
    regions = record["regions"]
    if not isinstance(regions, list) or not regions:
        raise SchemaRefusal("the Archetypus record retains no source region")
    for index, region in enumerate(regions):
        _validate_region_fields(region, f"Archetypus record region {index}")
    if not isinstance(record["provenance"], dict):
        raise SchemaRefusal("the Archetypus provenance is not an object")
    for field in ("dissent_ref", "perlectio_ref", "recensor_ref"):
        if not _is_ref_shaped(record[field]):
            raise SchemaRefusal(f"the Archetypus {field} is not a digest-checked reference")
    if record["dissent_ref"] != record["perlectio_ref"]:
        raise SchemaRefusal("dissent must travel by reference to this record's one Perlectio")
    return record


def _no_readable_text_evidence(
    review: dict, reading_ref: dict[str, str], reading_inputs: list
) -> dict[str, str] | None:
    """Return the Recensor's retained blank proof; never manufacture one here.

    `CONTRACT.md`'s whole argument for this field is that an accepted review is
    evidence the Recensor accepted a reading, not evidence the page was blank.
    No `blank-proof` artifact kind exists yet to check this reference's kind
    against, so the one class checkable today without inventing that contract is
    refused here: nothing from the reading's own evidentiary chain — the reading
    itself, or any crop it read — is allowed to stand as proof of its silence.
    An accepted review's inputs are the reading plus that reading's crops, so
    without the second refusal the very image the reading failed to read would
    pass the direct-input check and seal as proof the page was blank.
    """
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
    if reference == reading_ref:
        raise SchemaRefusal(
            "no_readable_text evidence names the accepted Perlectio itself; a reading is "
            "never evidence of its own silence"
        )
    if reference in reading_inputs:
        raise SchemaRefusal(
            "no_readable_text evidence names an input of the accepted Perlectio itself; "
            "the ink a reading failed to read is never evidence of its own silence"
        )
    return reference


def _validate_region_fields(region, label: str) -> None:
    """The region's closed field set, checked identically at write and read-back.

    A region is embedded from the reading whole and copied field-for-field into
    the export, so the record's own closed top-level schema
    (`validate_record_fields`) says nothing about what rides inside one — this
    is the field-set closure for that sub-object, the shape that stopped
    `consolidated_literal` at construction (`_crop_references`) and now also
    stops it surviving a reseal past `validate_record`, the function every later
    stage-local read and CONTRACT.md's `kind="archetypus"` section both rely on.
    """
    if not isinstance(region, dict):
        raise SchemaRefusal(f"{label} is not an object")
    unexpected = sorted(set(region) - _REGION_FIELDS)
    missing = sorted(_REGION_FIELDS - set(region))
    if unexpected or missing:
        raise SchemaRefusal(
            f"{label} is outside the closed region schema (missing {missing}, unexpected "
            f"{unexpected}); a region travels into this record and out through the export "
            "whole, so a field beside the crop facts is a second unvalidated payload"
        )


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

    `input_ref` hashes the bytes on disk, so an unreadable crop would otherwise
    arrive as `OSError`, outside the `ContractError` family `run_stage`
    classifies — a bare traceback and exit 1, taking every other act's record
    with it, for what is as often a pruned blob as a forged reading. The `except`
    below converts it, so what actually happens is the `FatalAccounting` message
    the acceptance test asserts on, not a traceback.
    """
    references = []
    seen_paths: dict[str, int] = {}
    for index, region in enumerate(regions):
        label = f"accepted reading of {act_id} region {index}"
        _validate_region_fields(region, label)
        image_path = region["image_path"]
        # The Armarium's `verify_established_page_record` builds its expected
        # input set as one reference per region, undeduplicated, and requires
        # exact equality with what this stage names. Two regions naming one path
        # would satisfy this stage's own envelope (a content-addressed crop
        # cannot disagree with itself) but seal a record the Armarium then
        # refuses at export — after the write-once seal, where it can only be
        # abandoned, never repaired. Refusing it here matches what the Perlector
        # already refuses at publish (`validate_input_refs`, one path listed
        # twice), so the same shape is refused at the same layer end to end.
        if image_path in seen_paths:
            raise FatalAccounting(
                f"{label} names crop {image_path!r}, already named by region "
                f"{seen_paths[image_path]} of this same reading; one path standing for "
                "two regions would seal a record its own consumer's accounting cannot reconcile"
            )
        seen_paths[image_path] = index
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
    """Combine the record's required evidence into one list.

    `_crop_references` already refuses two regions naming one crop path, so the
    only remaining way two groups could name one path is the review or the
    Perlectio itself coinciding with a crop path — which the run tree's own
    layout (`5_recensor/artifacts/...`, `4_perlector/artifacts/...` and
    `4_perlector/blobs/...` never overlap) makes structurally impossible.
    The dedup-by-path stays as the cheap defensive form of that same guarantee:
    every digest here was read off the same disk moments earlier, so two
    entries naming one path cannot disagree.
    """
    by_path: dict[str, dict[str, str]] = {}
    for group in groups:
        for reference in group:
            path = reference["relative_path"]
            existing = by_path.get(path)
            if existing is not None and existing["sha256"] != reference["sha256"]:
                raise FatalAccounting(
                    f"two direct inputs name {path!r} with different digests; one path "
                    "cannot hold two sets of bytes, and collapsing them would seal a "
                    "record whose inputs the Armarium cannot reconcile"
                )
            by_path[path] = reference
    return list(by_path.values())


def establish_from_accepted_page_reading(
    context, *, row: dict, review_ref: dict[str, str], page_testimonia: list[dict]
) -> tuple[dict, list[dict[str, str]]]:
    """The page path's one constructor: a `reading_acts` row and its accepted review.

    `page_testimonia` is the row's page's entry in `current_page_testimonia`.

    The reading is the `perlectio.v3` the row and the review both name; its one
    region is the `act-region` that reading names, proven from the Exemplar by
    `verify_reading_region_lineage`. `other` readings are established exactly
    like acts, so every export shows the same established text for them.
    """
    act_id = row["act_id"]
    if not _is_ref_shaped(review_ref):
        raise SchemaRefusal("accepted Recensor review reference is malformed")
    review = context.tree.read_artifact_reference(
        review_ref, stage=RECENSOR, kind="review", subject_id=act_id
    )
    reading_ref = row["perlectio_ref"]
    review_payload = review.get("payload")
    if (
        review.get("outcome") != "accepted"
        or not isinstance(review_payload, dict)
        or not _is_ref_shaped(reading_ref)
        or review_payload.get("perlectio_ref") != reading_ref
        or reading_ref not in review.get("inputs", [])
    ):
        raise SchemaRefusal(
            f"the Archetypus constructor for {row['act_key']} accepts only the exact page "
            "reading a Recensor accepted"
        )
    require_establishable(row, review)
    reading = context.tree.read_artifact_reference(
        reading_ref, stage=PERLECTOR, kind="perlectio", subject_id=act_id
    )
    payload = reading.get("payload")
    if (
        reading.get("outcome") != "read"
        or not isinstance(payload, dict)
        or payload.get("schema") != page_path.PERLECTIO_SCHEMA
        or payload.get("kind") != row["kind"]
        or payload.get("holds") != []
        or payload.get("page_holds") != []
    ):
        raise FatalAccounting(
            f"{row['act_key']} would be established from a page reading that is held or is not "
            "the row's own reading; a held reading is never written"
        )
    for field in ("tier", "source_tier", "reading_tier"):
        if payload.get(field) == "salvage":
            raise SchemaRefusal(
                f"{row['act_key']} carries salvage-tier material, which can never become an "
                "Archetypus"
            )
    region_ref = payload.get("act_region_ref")
    if region_ref != row["region_ref"] or region_ref not in reading.get("inputs", []):
        raise FatalAccounting(
            f"the accepted reading of {row['act_key']} does not input the act-region the "
            "denominator counted"
        )
    region_record = context.tree.read_artifact_reference(
        region_ref, stage=PERLECTOR, kind="act-region", subject_id=act_id
    )
    try:
        region = verify_reading_region_lineage(context.tree, context.run, region_record)
    except ContractError as error:
        raise FatalAccounting(
            f"the act-region of {row['act_key']} does not trace to the Exemplar: {error}"
        ) from error
    regions = [region]
    crop_references = _crop_references(context, regions, act_id)
    # Custody: every witness the feed showed is its chair's current page
    # Testimonium, and a feed that showed none made the reading a Lectio nuda.
    shown_page_witnesses(context, reading, page_testimonia, f"the page reading of {row['act_key']}")
    text = payload.get("text")
    if not isinstance(text, str):
        raise SchemaRefusal("the accepted page reading has no string text")
    # A page reading records its doubt as spans and gaps; it has no annotation layer.
    if "annotations" in payload:
        raise SchemaRefusal(
            f"the page reading of {row['act_key']} carries an annotation layer, which a page "
            "reading does not record"
        )
    annotations: list[dict] = []
    uncertainty = from_page_perlectio(payload)
    text_status = derive_record_text_status(text, annotations, uncertainty)
    evidence_ref = _no_readable_text_evidence(review, reading_ref, reading.get("inputs", []))
    if evidence_ref is not None and text_status != "no_readable_text":
        raise FatalAccounting(
            f"the accepted review of {row['act_key']} retains a proof that it held no readable "
            f"ink, but its reading establishes {text_status!r} text"
        )
    validate_text_status(text, text_status, evidence_ref)
    validate_serving_provenance(
        context,
        payload.get("provenance"),
        producer_stage=PERLECTOR,
        require_receipt=True,
    )
    record = {
        "act_id": act_id,
        "act_key": row["act_key"],
        "page_id": row["page_id"],
        "kind": row["kind"],
        "text": text,
        "text_hash": digest_of(text),
        "status": "established",
        "text_status": text_status,
        "regions": regions,
        "provenance": payload.get("provenance"),
        "annotations": annotations,
        "uncertainty": uncertainty,
        "evidence_ref": evidence_ref,
        "dissent_ref": reading_ref,
        "perlectio_ref": reading_ref,
        "recensor_ref": review_ref,
    }
    record["self_hash"] = self_hash(record)
    validate_record(record)
    return record, _direct_inputs([review_ref, reading_ref, region_ref], crop_references)


def accepted_act_ids(context, rows: list[dict] | None = None) -> set[str]:
    """The counted readings whose current Recensor review is exactly `accepted`.

    Recomputed from the immutable review records rather than remembered from
    this invocation's own loop: the index reconciles with what the *Recensor*
    accepted, and an index checked only against the list the writer just built
    would agree with itself about a reading it had skipped.
    """
    rows = reviewed_rows(reading_acts(context)) if rows is None else rows
    reviews = current_page_reviews(context, rows)
    return {act_id for act_id, review in reviews.items() if review["outcome"] == "accepted"}


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
                "kind": payload["kind"],
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
        # The number of immutable records this index summarizes. `validate_index`
        # is what proves that set equals the acts the Recensor accepted; this
        # field never claims it on its own.
        "record_count": len(rows),
        "rows": rows,
    }
    index["self_hash"] = self_hash(index)
    return index


def validate_index(context, index, *, on_disk=None, accepted=None) -> dict:
    """Spec 10 test 6, as a consumer check: 1:1 with the acts the Recensor accepted.

    The index is derived and rewritable. That does not make a missing or
    duplicate row harmless where someone relies on it for accounting: it is
    FATAL until it is regenerated from the immutable records, never a warning
    and never quietly repaired underneath a reader.

    `on_disk` and `accepted` are derived from the immutable records when
    omitted — the one-argument consumer form CONTRACT.md documents. The stage's
    own finishing step passes both, because it reconciles twice back to back
    and re-reading a parish of records for the same answer buys nothing.
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
        not isinstance(index["record_count"], int)
        or isinstance(index["record_count"], bool)
        or index["record_count"] < 0
    ):
        raise FatalAccounting("the Archetypus index record_count is not a non-negative integer")

    if on_disk is None:
        on_disk = {row["act_id"]: row for row in _archetypus_rows(context)}
    seen: set[str] = set()
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row) != _INDEX_ROW_FIELDS
            or row["kind"] not in READING_KINDS
        ):
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
    if index["record_count"] != len(rows):
        raise FatalAccounting("the Archetypus index count disagrees with the rows it carries")

    if accepted is None:
        accepted = accepted_act_ids(context)
    if seen != accepted or set(on_disk) != accepted:
        raise FatalAccounting(
            f"the Archetypus records and index ({sorted(seen)}) do not reconcile 1:1 with the "
            f"acts the Recensor accepted ({sorted(accepted)}); a missing or duplicate row is a "
            "fatal accounting imbalance, never a warning"
        )
    return index


def main(registry_factory=ChairRegistry.from_toml) -> int:
    """Establish every accepted reading, and reconcile the index."""
    args = stage_parser(DESCRIPTION).parse_args()
    # Either ingress route, decided from one read of the run authority. This
    # stage reads no fixture declaration: its denominator is `reading_acts`.
    context = open_stage_context(args, ARCHETYPUS, registry_factory=registry_factory)
    rows = reviewed_rows(reading_acts(context))
    reviews = current_page_reviews(context, rows)
    testimonia = current_page_testimonia(context)
    unresolved: list[str] = []
    # Every record is built and checked before any is published, so a refused
    # one leaves no partial set of established readings behind it.
    established: list[tuple[dict, dict, list[dict[str, str]]]] = []
    for row in rows:
        review = reviews[row["act_id"]]
        if review["outcome"] != "accepted":
            # Held and page rows end at the Recensor with no record; only an
            # outcome the algebra leaves open is unfinished work.
            if terminal_category(RECENSOR, review["outcome"]) is None:
                unresolved.append(row["act_key"])
            continue
        require_establishable(row, review)
        review_ref = context.artifact_ref(RECENSOR, "review", review["artifact_id"])
        record, inputs = establish_from_accepted_page_reading(
            context,
            row=row,
            review_ref=review_ref,
            page_testimonia=testimonia.get(row["page_id"], []),
        )
        established.append((row, record, inputs))
    for row, record, inputs in established:
        context.publish(
            kind="archetypus",
            subject_id=row["act_id"],
            outcome="established",
            inputs=inputs,
            payload=record,
        )
    # Reconciled against the Recensor's accepted set before it is written, then
    # read back and checked again against the same cached rows: nothing can
    # publish a record between the two calls in one process.
    on_disk = {row["act_id"]: row for row in _archetypus_rows(context)}
    accepted = accepted_act_ids(context, rows)
    index = validate_index(context, build_index(context), on_disk=on_disk, accepted=accepted)
    context.tree.write_index(ARCHETYPUS, index)
    validate_index(context, context.tree.read_index(ARCHETYPUS), on_disk=on_disk, accepted=accepted)
    context.seal_boundary()
    context.finish()
    if unresolved:
        print(
            f"held: {len(unresolved)} reading(s) have no terminal review yet: {sorted(unresolved)}",
            file=sys.stderr,
        )
        return EXIT_HELD
    return EXIT_COMPLETE


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
