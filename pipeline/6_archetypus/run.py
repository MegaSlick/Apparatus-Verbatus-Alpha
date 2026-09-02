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
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.alignment import markup_text_view  # noqa: E402
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
from common.contracts.errors import FatalAccounting, SchemaRefusal  # noqa: E402
from common.contracts.identities import is_well_formed  # noqa: E402
from common.contracts.outcomes import (  # noqa: E402
    TEXT_STATUSES,
    classify,
    derive_record_text_status,
    terminal_category,
)
from common.contracts.outcomes import derive_text_status as derive_text_status  # noqa: E402
from common.contracts.stages import (  # noqa: E402
    ARCHETYPUS,
    ATTESTATORES,
    DESIGNATOR,
    PERLECTOR,
    RECENSOR,
)
from common.contracts.uncertainty import from_perlectio  # noqa: E402
from common.contracts.uncertainty import validate as validate_uncertainty
from common.cross_capture_autopsia import validate_autopsia  # noqa: E402
from common.cross_capture_coverage import validate_cross_capture_coverage  # noqa: E402
from common.cross_capture_dissent import validate_cross_capture_dissent  # noqa: E402
from common.physical_act_partition import validate_physical_act_partition  # noqa: E402
from common.stage import (  # noqa: E402
    EXIT_COMPLETE,
    EXIT_HELD,
    WITNESS_CONTEXT_REGIMES,
    WITNESS_READING_OUTCOMES,
    expected_acts,
    latest_attempt,
    open_context,
    reading_basis_regions,
    recovery_region_count,
    require_current_witness_basis,
    run_stage,
    stage_parser,
    validate_serving_provenance,
)
from common.witness_regime import witness_label  # noqa: E402

# The three silences, kept apart, and the derivation over them, both imported
# from `common/contracts/outcomes.py` rather than spelled here. The Armarium
# recomputes the same status from the layers travelling beside the text at export
# (`verify_established_record`), and stages talk only through `common/`
# (`pipeline/test_stage_import_boundaries.py`), so a private copy here would be
# the second spelling of one rule — the pair that drifts. `derive_text_status` is
# re-exported deliberately: it is this stage's own derivation over the older
# annotation layer, and this stage's tests are what exercise it directly.

# Spec 10 asks these shapes to map onto the mature convention rather than invent
# markup: `<unclear cert="">` for characters that ARE in `text`, and `<gap>` —
# whose content model never admits character data — for a zero-width anchor where
# none were read (TEI P5 ch. 11, "Representation of Primary Sources"; EpiDoc
# Guidelines, "Unclear characters"). Rendering either one is the Armarium's
# business at export time and is deliberately not stored.
# The layer's closed vocabularies and its validator live in
# `common/contracts/annotations.py` and are re-exported below: the Armarium
# reconciles a record's layer against its accepted reading's and re-validates
# the carried copy in every packaged product, and stages talk only through
# `common/` (`pipeline/test_stage_import_boundaries.py`). One spelling; the
# producer and its consumers cannot drift about what this layer may hold.

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
        "uncertainty",
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
_INDEX_FIELDS = frozenset({"schema", "run_id", "stage", "record_count", "rows", "self_hash"})

# Unit 19D is additive while image-local runs remain valid.  A physical-act
# record never carries a representative local key or page: its subject and
# index key are the one logical act, with every capture component retained.
_LOGICAL_RECORD_FIELDS = frozenset(
    {
        "logical_act_id",
        "physical_page_components",
        "member_local_acts",
        "text",
        "text_hash",
        "status",
        "text_status",
        "regions",
        "provenance",
        "annotations",
        "uncertainty",
        "evidence_ref",
        "cross_capture_dissent_ref",
        "perlectio_ref",
        "recensor_ref",
        "self_hash",
    }
)
_LOGICAL_COMPONENT_FIELDS = frozenset({"physical_page_id", "required_capture_sha256s"})
_LOGICAL_MEMBER_FIELDS = frozenset(
    {"act_id", "act_key", "page_id", "page_ordinal", "source_sha256", "proposal_refs"}
)


def _is_ref_shaped(value) -> bool:
    if not isinstance(value, dict) or set(value) != {"relative_path", "sha256"}:
        return False
    try:
        validate_input_refs([value])
    except SchemaRefusal:
        return False
    return True


def _logical_sha(value, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise SchemaRefusal(
            f"the logical Archetypus {label} is not a lowercase SHA-256; the record is "
            "refused because every capture and crop it cites must retain a digest identity"
        )
    return value


def _logical_component(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != _LOGICAL_COMPONENT_FIELDS:
        raise SchemaRefusal(
            "the logical Archetypus physical-page component is outside its closed schema; "
            "the record is refused because its capture denominator cannot be reconstructed"
        )
    page = value["physical_page_id"]
    captures = value["required_capture_sha256s"]
    if (
        not is_well_formed(page)
        or not page.startswith("ppg_")
        or not isinstance(captures, list)
        or not captures
        # Element types before `set(...)`: an unhashable member would raise
        # TypeError out of the dedupe itself, and run_stage catches only the
        # named contract errors -- the malformed record must refuse, not crash.
        or not all(isinstance(source, str) for source in captures)
        or captures != sorted(set(captures))
    ):
        raise SchemaRefusal(
            "the logical Archetypus physical-page component has a malformed page identity "
            "or capture set; the record is refused because its required evidence is not a "
            "non-empty canonical set"
        )
    return {
        "physical_page_id": page,
        "required_capture_sha256s": [
            _logical_sha(source, "required capture") for source in captures
        ],
    }


def _logical_member(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != _LOGICAL_MEMBER_FIELDS:
        raise SchemaRefusal(
            "the logical Archetypus member act is outside its closed lineage schema; the "
            "record is refused because every local proposal must remain identifiable"
        )
    act = value["act_id"]
    page = value["page_id"]
    key = value["act_key"]
    ordinal = value["page_ordinal"]
    refs = value["proposal_refs"]
    if (
        not is_well_formed(act)
        or not act.startswith("act_")
        or not is_well_formed(page)
        or not page.startswith("pg_")
        or not isinstance(key, str)
        or not key
        or not key.isprintable()
        or unicodedata.normalize("NFC", key) != key
        or not isinstance(ordinal, int)
        or isinstance(ordinal, bool)
        or ordinal < 0
        or not isinstance(refs, list)
        or not refs
        # Element types before `set(...)`, for the same reason as the component
        # captures: an unhashable reference must be a refusal, not a TypeError.
        or not all(isinstance(reference, str) and reference for reference in refs)
        or refs != sorted(set(refs))
    ):
        raise SchemaRefusal(
            "the logical Archetypus member act has malformed identity, key, ordinal, or "
            "proposal references; the record is refused because local-act provenance is "
            "not its canonical lineage"
        )
    return {
        "act_id": act,
        "act_key": key,
        "page_id": page,
        "page_ordinal": ordinal,
        "source_sha256": _logical_sha(value["source_sha256"], "member source_sha256"),
        "proposal_refs": list(refs),
    }


def _reference_key(reference: dict) -> tuple[str, str]:
    return (reference["relative_path"], reference["sha256"])


def _verify_comparison_views(
    context, act_id: str, derived: dict[str, str], embedded: dict, dossier: dict
) -> None:
    """Bind every displayed witness label to its retained Testimonium slice.

    The dossier keys views by chair under ``named`` and by a run-scoped pseudonym
    under ``blinded``. Comparing only the text multiset would let two authentic
    slices exchange pseudonyms and silently corrupt their attribution.
    """
    if not isinstance(embedded, dict):
        raise SchemaRefusal(f"act {act_id} embedded comparison views are not a mapping")
    regime = dossier.get("witness_regime")
    if regime not in WITNESS_CONTEXT_REGIMES:
        raise SchemaRefusal(
            f"act {act_id} embeds a dossier under witness regime {regime!r}, which is not one "
            f"of {sorted(WITNESS_CONTEXT_REGIMES)}; an unrecognized regime is refused, never "
            "checked as though it were the default"
        )
    rows = dossier.get("testimonia")
    if not isinstance(rows, list):
        raise SchemaRefusal(f"act {act_id} embeds a dossier with no testimonium rows")
    labels = [row.get("witness_label") for row in rows if isinstance(row, dict)]
    if (
        len(labels) != len(rows)
        or any(not isinstance(label, str) or not label for label in labels)
        or len(set(labels)) != len(labels)
    ):
        raise SchemaRefusal(f"act {act_id} embeds malformed or repeated witness labels")
    try:
        expected = {
            witness_label(
                chair,
                regime=regime,
                run_id=context.tree.run_id,
                config_digest=context.config_digest,
            ): text
            for chair, text in derived.items()
        }
    except ValueError as error:
        raise SchemaRefusal(
            f"act {act_id} attachment names a witness that cannot be labelled"
        ) from error
    if len(expected) != len(derived):
        raise SchemaRefusal(
            f"act {act_id} attachment witness labels collide; one comparison view would "
            "silently replace another"
        )
    if not set(expected).issubset(labels):
        raise SchemaRefusal(
            f"act {act_id} embeds comparison view(s) that name no witness this dossier carries"
        )
    if embedded != expected:
        raise SchemaRefusal(f"act {act_id} embedded comparison views disagree with its attachment")


def _verify_act_attachment_view(
    context, act_id: str, page_id: str, regions: list[dict], dossier: dict
) -> None:
    """Re-derive a Perlectio's page-witness facts from retained evidence.

    ``page_id`` is the act roster's attachment subject. Basis regions may span
    pages, so their first entry cannot supply that identity, but at least one basis
    region must cite it.
    """
    attachment_view = dossier.get("act_attachment")
    if (
        not isinstance(attachment_view, dict)
        or set(attachment_view)
        != {"reference", "page_witness_count", "comparison_views", "edge_deltas"}
        or not isinstance(attachment_view["page_witness_count"], int)
        or isinstance(attachment_view["page_witness_count"], bool)
        or attachment_view["page_witness_count"] < 0
        or not isinstance(attachment_view["comparison_views"], dict)
        or not isinstance(attachment_view["edge_deltas"], dict)
    ):
        raise SchemaRefusal(f"act {act_id} has malformed embedded act-attachment facts")
    reference = attachment_view["reference"]
    if not _is_ref_shaped(reference):
        raise SchemaRefusal(f"act {act_id} has no direct act-attachment reference")
    attachment_record = context.tree.read_artifact_reference(
        reference, stage=ATTESTATORES, kind="act-attachment", subject_id=act_id
    )
    payload = attachment_record.get("payload")
    attachments = payload.get("attachments") if isinstance(payload, dict) else None
    if not isinstance(attachments, list):
        raise SchemaRefusal(f"act {act_id} referenced act-attachment has no attachment list")
    if not isinstance(page_id, str) or not page_id:
        raise SchemaRefusal(f"act {act_id} has no source page for attachment verification")
    if page_id not in {region.get("source_page_id") for region in regions}:
        raise SchemaRefusal(
            f"act {act_id} is accounted to page {page_id!r}, which none of its basis regions "
            "cites; a record's page identity and the ink it was read from are one fact"
        )
    views: dict[str, str] = {}
    page_witness_chairs: set[str] = set()
    seen_rows: set[tuple[str, int | None]] = set()
    for item in attachments:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("page_witness"), bool)
            or not isinstance(item.get("attached"), bool)
            or not isinstance(item.get("chair"), str)
            or not item["chair"]
        ):
            raise SchemaRefusal(
                f"act {act_id} referenced act-attachment has malformed witness scope"
            )
        # A row is identified by (chair, page), not by chair alone. A continuation
        # act's crop spans more than one source page, so one chair legitimately
        # contributes one row per contributing page -- exactly one of which is the
        # act's own page and can be `attached`. The duplicate-row refusal below and
        # the per-chair comparison-view refusal further down are both kept: between
        # them nothing a repeated chair could silently replace survives.
        # Scope decides which rule applies, in the same words the Recensor uses on
        # the same artifact. Accepting a page-witness row with no ordinal — which
        # this check did while `page_ordinal is not None` gated it — left two
        # consumers of one act-attachment disagreeing about whether that row is
        # valid, and keyed it as `(chair, None)`, so a chair with one row on page 2
        # and one row missing the field made two distinct keys and passed the
        # duplicate guard below as two contributing pages when nobody could say
        # what the second page was. A stock run is protected only because the
        # Recensor refuses first, and this function documents itself as the whole
        # of the boundary for a caller that resolved its arguments another way.
        page_ordinal = item.get("page_ordinal")
        if item["page_witness"]:
            if not isinstance(page_ordinal, int) or isinstance(page_ordinal, bool):
                raise SchemaRefusal(
                    f"act {act_id} referenced act-attachment page witness {item['chair']!r} "
                    "has no integer page ordinal; its attachment cannot be placed"
                )
        elif page_ordinal is not None:
            raise SchemaRefusal(
                f"act {act_id} referenced act-attachment act-scoped witness "
                f"{item['chair']!r} carries page ordinal {page_ordinal!r}; its scope "
                "is contradictory"
            )
        row = (item["chair"], page_ordinal)
        if row in seen_rows:
            raise SchemaRefusal(
                f"act {act_id} referenced act-attachment repeats witness {item['chair']!r} "
                f"on page {page_ordinal!r}; a repeated row would silently replace a "
                "comparison view"
            )
        seen_rows.add(row)
        if not item["page_witness"]:
            continue
        # What the dossier discloses is how many distinct chairs witnessed a page,
        # never how many attachment rows there are: a two-page continuation act must
        # not report four witnesses where the run configured two.
        page_witness_chairs.add(item["chair"])
        if not item.get("attached"):
            continue
        chair, alignment, testimony_ref = (
            item.get("chair"),
            item.get("alignment"),
            item.get("testimonium_ref"),
        )
        span = alignment.get("witness_span") if isinstance(alignment, dict) else None
        if not _is_ref_shaped(testimony_ref):
            raise SchemaRefusal(f"act {act_id} referenced act-attachment has malformed witness")
        testimony = context.tree.read_artifact_reference(
            testimony_ref, stage=ATTESTATORES, kind="page-testimonium", subject_id=page_id
        )
        # Guarded like `alignment` above: a payload that is present but not a
        # mapping would otherwise reach `.get` and raise AttributeError, and a
        # traceback is not a refusal -- this stage's contract is a named one.
        testimony_payload = testimony.get("payload")
        reported = testimony_payload.get("payload") if isinstance(testimony_payload, dict) else None
        if (
            not isinstance(reported, str)
            or not isinstance(span, dict)
            or set(span) != {"start", "end"}
            or any(not isinstance(value, int) or isinstance(value, bool) for value in span.values())
            or span["start"] < 0
            or span["end"] < span["start"]
            or span["end"] > len(reported)
        ):
            raise SchemaRefusal(
                f"act {act_id} referenced act-attachment has no valid comparison view"
            )
        if chair in views:
            raise SchemaRefusal(
                f"act {act_id} referenced act-attachment gives witness {chair!r} a second "
                "comparison view; a repeated chair would silently replace a comparison view"
            )
        views[chair] = markup_text_view(reported[span["start"] : span["end"]])["text"]
    if len(page_witness_chairs) != attachment_view["page_witness_count"]:
        raise SchemaRefusal(
            f"act {act_id} embedded page-witness count disagrees with its attachment"
        )
    _verify_comparison_views(context, act_id, views, attachment_view["comparison_views"], dossier)
    if dossier.get("dossier_digest") != digest_of(
        {key: value for key, value in dossier.items() if key != "dossier_digest"}
    ):
        raise SchemaRefusal(f"act {act_id} embedded dossier digest disagrees with its dossier")


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
    # The same question on the other side of the reading. Above: is this the
    # newest Perlectio, and does the crop history account for every attempt.
    # Here: is the testimony it was established from still the current testimony.
    # Nothing established a text over evidence a later attempt has superseded.
    require_current_witness_basis(
        act_id,
        reading,
        artifacts_for(context, ATTESTATORES, "testimonium", act_id),
        f"the accepted Perlectio of {act_id}",
    )
    return reading, reference


def accepted_primed_perlectio(
    context, review, reading, reading_ref, act_id, *, page_id
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
    # Boundary records use the ordinary envelope vocabulary too.  Their
    # ``recorded``/``sealed`` outcomes are completed *boundary evidence*, never
    # a successful Perlectio: the constructor accepts the one reading outcome
    # that can establish text, rather than treating every completed-class value
    # for this producer as interchangeable.
    if reading.get("outcome") != "read":
        raise FatalAccounting(
            f"act {act_id} would be established from a {reading['outcome']!r} "
            f"reading ({reading_class.value}); the established text may only come "
            "from a reading that succeeded, and a failed one is held, never written"
        )
    payload = reading.get("payload")
    if not isinstance(payload, dict):
        raise SchemaRefusal(f"the accepted Perlectio for {act_id} has no object payload")

    lectio_kind = payload.get("lectio_kind")
    claimed_dossier = payload.get("dossier")
    claimed_prior_draft = (
        claimed_dossier.get("prior_draft") if isinstance(claimed_dossier, dict) else None
    )
    if lectio_kind == "primed-without-prior" and claimed_prior_draft is not None:
        raise SchemaRefusal(
            f"act {act_id} claims primed-without-prior but carries a prior-draft reference"
        )
    if lectio_kind != "primed-with-prior":
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
    attachment = (
        claimed_dossier.get("act_attachment") if isinstance(claimed_dossier, dict) else None
    )
    # Required, exactly as the Testimonium basis above is required, and for the
    # same reason. Every primed reading this pipeline seals carries the
    # act-attachment dossier view, so a reading that reaches here without one is
    # a reading whose page-witness custody nobody checked -- and checking it only
    # `if attachment is not None` made the whole chain opt-out at the one stage
    # that reads a Perlectio back off disk. Found in audit; F-O2.
    if not isinstance(attachment, dict):
        raise SchemaRefusal(
            f"act {act_id} reached the Archetypus constructor with no act-attachment view; "
            "the page-witness evidence a reading was built on is consumed here or the "
            "reading is refused"
        )
    reference = attachment.get("reference")
    if not _is_ref_shaped(reference) or reference not in reading.get("inputs", []):
        raise SchemaRefusal(
            f"act {act_id} carries an act-attachment dossier view without a direct input reference"
        )
    # Unconditional: `lectio_kind` is already proved to be `primed-with-prior`
    # above, and nothing below it reassigns the name. Guarding these checks on
    # the value again read as though some other kind reached them, which would
    # have made the whole prior-draft chain look optional at the one stage that
    # reads a Perlectio back off disk -- the shape F-O2 had already had to
    # repair once for the act-attachment view.
    prior_draft = claimed_prior_draft
    prior_reference = prior_draft.get("reference") if isinstance(prior_draft, dict) else None
    if not _is_ref_shaped(prior_reference):
        raise SchemaRefusal(
            f"act {act_id} claims primed-with-prior but carries no prior-draft reference"
        )
    if prior_reference not in reading.get("inputs", []):
        raise SchemaRefusal(
            f"act {act_id} carries a prior-draft reference that is not a digest-checked "
            "direct input of the reading"
        )
    prior_record = context.tree.read_artifact_reference(
        prior_reference,
        stage=PERLECTOR,
        kind="lectio-prior",
        subject_id=act_id,
    )
    prior_payload = prior_record.get("payload")
    if (
        not isinstance(prior_payload, dict)
        or not isinstance(prior_draft.get("text"), str)
        or prior_draft["text"] != prior_payload.get("text")
    ):
        raise SchemaRefusal(
            f"act {act_id} embeds prior-draft text that disagrees with its referenced lectio-prior"
        )
    _verify_act_attachment_view(context, act_id, page_id, regions, claimed_dossier)
    # The reference is bound to stage, kind, subject and digest above -- and
    # not, until here, to the attempt. A recovered act carries one Pass-A
    # draft per attempt, and a Perlectio citing a superseded one would
    # publish `self_revision` measured against a draft its reader never saw.
    # Where the two drafts happen to read alike (the ordinary case: recovery
    # recovers coverage, not text) every other check above passes, so this
    # is the binding that makes the citation the reading's own.
    #
    # Both ordinals are held to be integers before the comparison. The reading's
    # own is already proven by `latest_attempt`, but this function documents
    # itself as the whole of the boundary for a caller that resolved its
    # arguments some other way — and two absent ordinals comparing None == None
    # would pass the one binding this block exists to make.
    for owner, candidate in (("lectio-prior", prior_payload), ("Perlectio", payload)):
        ordinal = candidate.get("attempt_ordinal")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool):
            raise SchemaRefusal(
                f"act {act_id} carries a {owner} payload with no integer attempt ordinal; "
                "an attempt binding cannot be made over a missing ordinal"
            )
    if prior_payload.get("attempt_ordinal") != payload.get("attempt_ordinal"):
        raise SchemaRefusal(
            f"act {act_id} cites a prior draft from reading attempt "
            f"{prior_payload.get('attempt_ordinal')!r}, not its own "
            f"{payload.get('attempt_ordinal')!r}"
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
        reference_key = _reference_key(reference)
        if reference_key in witnesses:
            raise SchemaRefusal(
                f"act {act_id} repeats Testimonium basis {index}; one retained witness "
                "reference may not count twice as evidence that this reading was primed"
            )
        testimonium = context.tree.read_artifact_reference(
            reference,
            stage=ATTESTATORES,
            kind="testimonium",
            subject_id=act_id,
        )
        testimonium_payload = testimonium.get("payload")
        if not isinstance(testimonium_payload, dict):
            raise SchemaRefusal(f"act {act_id} Testimonium basis {index} has no object payload")
        reported = testimonium_payload.get("payload")
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


def validate_logical_record(record: dict) -> dict:
    """Validate the clustered Archetypus shape without a representative member.

    The legacy record remains the image-local contract.  This separate closed
    shape makes the migration explicit: accepting an extra ``logical_act_id``
    beside an old ``page_id``/``act_key`` would only conceal the picker in a
    compatibility field.
    """
    if not isinstance(record, dict) or set(record) != _LOGICAL_RECORD_FIELDS:
        raise SchemaRefusal(
            "the logical Archetypus record is outside its closed schema; the record is "
            "refused because added or missing fields can bypass one-text conservation"
        )
    if not verify_self_hash(record):
        raise SchemaRefusal(
            "the logical Archetypus record fails its nested self_hash; the record is refused "
            "because member, text, and evidence bytes must remain bound after establishment"
        )
    if not is_well_formed(record["logical_act_id"]) or not record["logical_act_id"].startswith(
        "pac_"
    ):
        raise SchemaRefusal(
            "the logical Archetypus logical_act_id is not a physical-act identity; the "
            "record is refused because a free-form or image-local id cannot key clustered text"
        )
    if (
        not isinstance(record["physical_page_components"], list)
        or not record["physical_page_components"]
    ):
        raise SchemaRefusal("the logical Archetypus record has no physical page components")
    if not isinstance(record["member_local_acts"], list) or not record["member_local_acts"]:
        raise SchemaRefusal("the logical Archetypus record has no retained local members")
    components = [_logical_component(component) for component in record["physical_page_components"]]
    if components != record["physical_page_components"] or [
        component["physical_page_id"] for component in components
    ] != sorted({component["physical_page_id"] for component in components}):
        raise SchemaRefusal(
            "the logical Archetypus physical-page components are not sorted unique canonical "
            "rows; the record is refused because one component may not count twice"
        )
    members = [_logical_member(member) for member in record["member_local_acts"]]
    member_ids = [member["act_id"] for member in members]
    member_keys = [member["act_key"] for member in members]
    if (
        members != record["member_local_acts"]
        or member_ids != sorted(set(member_ids))
        or len(member_keys) != len(set(member_keys))
    ):
        raise SchemaRefusal(
            "the logical Archetypus local members repeat an act id/key or are not in "
            "canonical act-id order; the record is refused because every proposal row must "
            "be conserved exactly once"
        )
    component_sources = {
        source for component in components for source in component["required_capture_sha256s"]
    }
    member_sources = {member["source_sha256"] for member in members}
    if not member_sources <= component_sources:
        outside_components = sorted(member_sources - component_sources)
        raise SchemaRefusal(
            f"the logical Archetypus member capture(s) {outside_components} occur in no "
            "physical-page component; the record is refused because member ink cannot fall "
            "outside its capture denominator"
        )
    text = record["text"]
    if not isinstance(text, str) or record["text_hash"] != digest_of(text):
        raise SchemaRefusal("the logical Archetypus record text is not its one hashed string")
    if record["status"] != "established":
        raise SchemaRefusal("the logical Archetypus record status is not established")
    validate_uncertainty(record["uncertainty"], text)
    if (
        validate_annotations(record["annotations"], text, None, "logical Archetypus annotation")
        != record["annotations"]
    ):
        raise SchemaRefusal("the logical Archetypus annotation layer is malformed")
    if record["text_status"] != derive_record_text_status(
        text, record["annotations"], record["uncertainty"]
    ):
        raise SchemaRefusal("the logical Archetypus text status disagrees with its one text")
    validate_text_status(text, record["text_status"], record["evidence_ref"])
    if not isinstance(record["regions"], list) or not record["regions"]:
        raise SchemaRefusal(
            "the logical Archetypus has no source regions; the record is refused because "
            "established text must remain anchored to ink"
        )
    for region in record["regions"]:
        _validate_region_fields(region, "logical Archetypus source region")
    if not isinstance(record["provenance"], dict) or not record["provenance"]:
        raise SchemaRefusal(
            "the logical Archetypus has no model provenance; the record is refused because "
            "its established text must retain the reader identity and revision"
        )
    for field in ("cross_capture_dissent_ref", "perlectio_ref", "recensor_ref"):
        if not _is_ref_shaped(record[field]):
            raise SchemaRefusal(f"the logical Archetypus {field} is not digest-bound")
    paths = {
        record[field]["relative_path"]
        for field in ("cross_capture_dissent_ref", "perlectio_ref", "recensor_ref")
    }
    if len(paths) != 3:
        raise SchemaRefusal(
            "the logical Archetypus parent references reuse one artifact path; the record is "
            "refused because a dissent, Perlectio, and Recensor review are three different "
            "pieces of evidence"
        )
    return record


def _require_the_partition_this_reading_was_made_over(
    *,
    partition: dict,
    logical_act: dict,
    logical_id: str,
    dossier: dict,
    dissent: dict,
) -> dict:
    """Bind the row, the reading, and the dissent to one sealed partition.

    ``logical_act`` decides what this record says about the ink behind its one
    text: which captures were required, which local acts are members, which
    physical pages the act sits on. Matching it to the reading by
    ``logical_act_id`` alone would make that provenance the caller's assertion
    rather than the reading's -- a row naming five captures stapled to a joint
    autopsia that only ever presented two, and an established record claiming
    evidence its own reading never demonstrated (consult §5.1, hard rule 6).

    The dossier's ``cross_capture_autopsia`` closes it. It is a full
    ``cross-capture-autopsia.v1`` (``assemble_reader_input`` puts the validated
    record into the delivered dossier, and the Perlector's own reading schema
    admits the pair or neither), and it names both the ``partition_ref`` the
    read was made over and the exact ``required_capture_sha256s`` that reached
    the reader in one call. So:

    1. the partition object must be the bytes that reference names;
    2. ``logical_act`` must be *the* row that partition publishes for this
       logical act, field for field -- not a row that merely agrees about its
       id;
    3. the captures the row declares required must be the captures the
       autopsia actually delivered; and
    4. the sibling dissent must cite the same partition.

    Nothing here reads a member, a view, or an observation as text. The
    partition is re-validated rather than trusted, on the same principle the
    Armarium's ``verify_established_record`` re-derives Archetypus's own
    checks: a denominator a consumer never re-computes is one refactor away
    from being wrong where nobody looks.
    """
    autopsia = dossier.get("cross_capture_autopsia")
    if not isinstance(autopsia, dict):
        raise SchemaRefusal(
            "logical establishment requires the joint reading's own cross-capture autopsia; "
            "a dossier that names a logical act with no presentation behind it proves nothing "
            "about which captures were read"
        )
    checked_autopsia = validate_autopsia(autopsia)
    if checked_autopsia["logical_act_id"] != logical_id:
        raise SchemaRefusal("logical establishment's autopsia presents another logical act")
    if not isinstance(partition, dict):
        raise SchemaRefusal("logical establishment has no physical-act partition")
    checked_partition = validate_physical_act_partition(partition)
    if digest_of(checked_partition) != checked_autopsia["partition_ref"]["sha256"]:
        raise SchemaRefusal(
            "logical establishment's partition is not the bytes the joint reading's own "
            "autopsia names; the row that supplies this record's member and capture "
            "provenance must come from the partition the read was actually made over"
        )
    if dissent["partition_ref"] != checked_autopsia["partition_ref"]:
        raise SchemaRefusal(
            "logical establishment's dissent cites a different partition than its reading"
        )
    published = [
        row for row in checked_partition["logical_acts"] if row["logical_act_id"] == logical_id
    ]
    if len(published) != 1:
        raise SchemaRefusal(
            "logical establishment's partition publishes no single row for that logical act"
        )
    (published_row,) = published
    if published_row != logical_act:
        raise SchemaRefusal(
            "logical establishment's partition row is not the row this partition publishes "
            "for that logical act"
        )
    required = {
        source
        for component in logical_act["physical_page_components"]
        for source in component["required_capture_sha256s"]
    }
    if required != set(checked_autopsia["required_capture_sha256s"]):
        raise SchemaRefusal(
            "logical establishment's partition row requires captures the joint reading did "
            "not present; the established record may not claim evidence its own reading "
            "never received"
        )
    return checked_autopsia


def _require_joint_evidence_binding(
    *,
    logical_id: str,
    accepted_perlectio: dict,
    accepted_review: dict,
    cross_capture_dissent: dict,
    cross_capture_dissent_ref: dict[str, str],
    autopsia: dict,
    logical_act: dict,
) -> None:
    """Prove the sibling dissent and review describe this one joint read."""
    payload = accepted_perlectio["payload"]
    review_payload = accepted_review["payload"]
    if digest_of(cross_capture_dissent) != cross_capture_dissent_ref["sha256"]:
        raise SchemaRefusal(
            "logical establishment's cross-capture dissent bytes do not match their "
            "digest-bound reference; the Archetypus is refused because it may not cite one "
            "dissent artifact while carrying another"
        )
    if cross_capture_dissent["config_digest"] != accepted_perlectio.get(
        "config_digest"
    ) or cross_capture_dissent["model_provenance"] != payload.get("provenance"):
        raise SchemaRefusal(
            "logical establishment's dissent configuration or model provenance differs from "
            "its Perlectio; the Archetypus is refused because observations from another "
            "reader invocation cannot accompany this text"
        )
    if cross_capture_dissent["reader_invocation_ref"] != payload.get(
        "reader_invocation_ref"
    ) or cross_capture_dissent["response_observation_digest"] != payload.get(
        "response_observation_digest"
    ):
        raise SchemaRefusal(
            "logical establishment's dissent does not bind the Perlectio's reader invocation "
            "and observation digest; the Archetypus is refused because post-reading evidence "
            "must come from the same single call"
        )
    presented = {
        view["view_id"]: {
            "source_sha256": view["source_sha256"],
            "region_refs": view["region_refs"],
        }
        for view in autopsia["views"]
    }
    observed = {
        view["view_id"]: {
            "source_sha256": view["source_sha256"],
            "region_refs": view["region_refs"],
        }
        for view in cross_capture_dissent["views"]
    }
    if observed != presented:
        raise SchemaRefusal(
            "logical establishment's dissent views do not equal the autopsia views its "
            "Perlectio received; the Archetypus is refused because observations about other "
            "captures cannot travel beside this text"
        )
    basis = payload.get("basis")
    reading_regions = basis.get("regions") if isinstance(basis, dict) else None
    if not isinstance(reading_regions, list):
        raise SchemaRefusal(
            "logical establishment's Perlectio has no region basis; the Archetypus is refused "
            "because the established text must retain every crop used by the joint read"
        )
    retained_region_refs = []
    for region in reading_regions:
        if not isinstance(region, dict):
            raise SchemaRefusal(
                "logical establishment's Perlectio has a non-object region basis; the "
                "Archetypus is refused because its crop provenance cannot be reconstructed"
            )
        image_path = region.get("image_path")
        image_sha256 = region.get("image_sha256")
        if not isinstance(image_path, str) or not image_path:
            raise SchemaRefusal(
                "logical establishment's Perlectio region has no image path; the Archetypus "
                "is refused because a crop used by the joint read cannot be cited"
            )
        retained_region_refs.append(
            {"relative_path": image_path, "sha256": _logical_sha(image_sha256, "region image")}
        )
    presented_region_refs = [
        reference for view in autopsia["views"] for reference in view["region_refs"]
    ]
    if sorted(retained_region_refs, key=_reference_key) != sorted(
        presented_region_refs, key=_reference_key
    ):
        raise SchemaRefusal(
            "logical establishment's Perlectio region basis does not equal every crop in its "
            "joint autopsia; the Archetypus is refused because a capture used to establish "
            "the text would be lost from its source-region provenance"
        )
    if review_payload.get("cross_capture_dissent_ref") != cross_capture_dissent_ref:
        raise SchemaRefusal(
            "logical establishment's accepted review does not cite this cross-capture "
            "dissent; the Archetypus is refused because the Recensor did not review the "
            "sibling evidence it would export"
        )
    coverage = review_payload.get("cross_capture_coverage")
    if (
        not isinstance(coverage, dict)
        or coverage.get("logical_act_id") != logical_id
        or not isinstance(coverage.get("findings"), list)
    ):
        raise SchemaRefusal(
            "logical establishment's accepted review has no cross-capture coverage record "
            "for this logical act; the Archetypus is refused because acceptance cannot erase "
            "the Recensor's visibility denominator"
        )
    try:
        checked_coverage = validate_cross_capture_coverage(coverage)
    except (SchemaRefusal, TypeError) as error:
        raise SchemaRefusal(
            "logical establishment's accepted review has malformed cross-capture coverage; "
            "the Archetypus is refused because an unvalidated visibility record cannot "
            "account for the act's capture denominator"
        ) from error
    expected_components = {
        component["physical_page_id"]: component["required_capture_sha256s"]
        for component in logical_act["physical_page_components"]
    }
    measured_components = {
        component["physical_page_id"]: component["required_capture_sha256s"]
        for component in checked_coverage["components"]
    }
    if measured_components != expected_components:
        raise SchemaRefusal(
            "logical establishment's accepted review coverage does not equal the partition's "
            "physical-page and capture denominator; the Archetypus is refused because a "
            "visibility finding about other evidence cannot accept this act"
        )
    if checked_coverage["act_state"] != "full" or checked_coverage["findings"]:
        finding_codes = sorted(
            f"{finding['code']}:{finding['physical_page_id']}"
            for finding in checked_coverage["findings"]
        )
        raise SchemaRefusal(
            "logical establishment's accepted review carries unresolved cross-capture "
            f"coverage ({checked_coverage['act_state']!r}; findings {finding_codes}); the "
            "Archetypus is refused because every measured visibility finding routes the act "
            "to a review item, never to established text"
        )


def establish_logical_record(
    *,
    partition: dict,
    logical_act: dict,
    accepted_perlectio: dict,
    accepted_review: dict,
    perlectio_ref: dict[str, str],
    recensor_ref: dict[str, str],
    cross_capture_dissent: dict,
    cross_capture_dissent_ref: dict[str, str],
) -> dict:
    """Copy the one accepted joint Perlectio text into a logical Archetypus.

    This deliberately takes no capture observation or member text argument.
    Those forms are structurally confined to ``cross_capture_dissent``; only
    the Perlector's single joint response is permitted to supply ``text``.

    ``accepted_perlectio`` and ``accepted_review`` are checked against
    ``perlectio_ref``/``recensor_ref`` by digest before either is read, so a
    caller cannot name one sealed reading in the reference while establishing
    the text of an object that was never sealed under it.

    ``partition`` is the ``physical-act-partition.v1`` the joint reading was
    actually made over, and ``logical_act`` must be a row published in it.
    Both are proved against the reading's own sealed
    ``cross_capture_autopsia.partition_ref`` rather than taken on the caller's
    word: the member and capture provenance this record carries is a claim
    about *which* ink was read, so an unproved row could staple five required
    captures onto a reading that only ever demonstrated two.
    """
    if not isinstance(logical_act, dict):
        raise SchemaRefusal("logical establishment has no partition row")
    logical_id = logical_act.get("logical_act_id")
    if not isinstance(logical_id, str) or not logical_id:
        raise SchemaRefusal("logical establishment has no logical act identity")
    if (
        not _is_ref_shaped(perlectio_ref)
        or not _is_ref_shaped(recensor_ref)
        or not _is_ref_shaped(cross_capture_dissent_ref)
    ):
        raise SchemaRefusal("logical establishment has malformed parent references")
    # The image-local constructor resolves its reading and review itself through
    # `context.tree.read_artifact_reference` (`reviewed_reading`,
    # `accepted_primed_perlectio`), so its evidence is provably the sealed bytes
    # a digest-checked reference names. This constructor takes them as plain
    # dicts, so it must make that proof itself or nothing ties
    # `accepted_perlectio`/`accepted_review` to their references at all. Every
    # artifact in this tree is written as exactly `canonical_bytes(envelope)`
    # (`RunTree.publish_artifact`), so a genuine `read_artifact_reference`
    # result reproduces its own reference's digest here for free -- the same
    # proof `verify_input_bytes` makes, made again because this function does
    # not call it.
    if (
        not isinstance(accepted_perlectio, dict)
        or digest_of(accepted_perlectio) != perlectio_ref["sha256"]
    ):
        raise SchemaRefusal(
            "logical establishment's Perlectio is not the exact bytes its own "
            "digest-bound reference names; an established text may only be copied "
            "from the reading it claims to cite, never from an unverified object "
            "beside a plausible-looking reference"
        )
    if (
        not isinstance(accepted_review, dict)
        or digest_of(accepted_review) != recensor_ref["sha256"]
    ):
        raise SchemaRefusal(
            "logical establishment's Recensor review is not the exact bytes its own "
            "digest-bound reference names"
        )
    if (
        not isinstance(cross_capture_dissent, dict)
        or digest_of(cross_capture_dissent) != cross_capture_dissent_ref["sha256"]
    ):
        raise SchemaRefusal(
            "logical establishment's cross-capture dissent is not the exact bytes its own "
            "digest-bound reference names; the Archetypus is refused because sibling "
            "evidence cannot be substituted beside a valid reference"
        )
    payload = accepted_perlectio.get("payload")
    review_payload = accepted_review.get("payload")
    if accepted_perlectio.get("outcome") != "read":
        reason = payload.get("reason") if isinstance(payload, dict) else None
        raise SchemaRefusal(
            f"logical establishment's Perlectio outcome is "
            f"{accepted_perlectio.get('outcome')!r} ({reason!r}); the Archetypus is refused "
            "because a capacity hold, failed call, or not-run reading establishes no text"
        )
    if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
        raise SchemaRefusal(
            "logical establishment's read Perlectio has no string text payload; the "
            "Archetypus is refused because absence or a malformed result is not a reading"
        )
    # The same discriminator `accepted_primed_perlectio` applies on the
    # image-local path: the establishing joint pass seals
    # `lectio_kind = "primed-with-prior"` (the Perlector's combined protocol),
    # and a Lectio nuda, lectio-prior, or primed-without-prior arm is an
    # instrument record whose draft text may never become established text.
    if payload.get("lectio_kind") != "primed-with-prior":
        raise SchemaRefusal(
            f"logical establishment's Perlectio names lectio_kind "
            f"{payload.get('lectio_kind')!r}; only the explicitly primed establishing "
            "pass may establish, and an instrument arm is evidence, never text"
        )
    if payload.get("primed") not in (None, True):
        raise SchemaRefusal(
            "logical establishment's Perlectio carries an explicitly non-primed flag, "
            "which is an instrument record, never an establishing read"
        )
    if not isinstance(payload.get("dossier"), dict):
        raise SchemaRefusal(
            "logical establishment's Perlectio has no object dossier; the Archetypus is "
            "refused because its text has no joint presentation provenance"
        )
    if payload["dossier"].get("logical_act_id") != logical_id:
        raise SchemaRefusal(
            "logical establishment's Perlectio dossier names another logical act; the "
            "Archetypus is refused because one act's reading cannot establish another"
        )
    if accepted_review.get("outcome") != "accepted":
        reason = review_payload.get("reason") if isinstance(review_payload, dict) else None
        raise SchemaRefusal(
            f"logical establishment's Recensor outcome is "
            f"{accepted_review.get('outcome')!r} ({reason!r}); the Archetypus is refused "
            "because a held review must leave a review item, never established text"
        )
    if not isinstance(review_payload, dict):
        raise SchemaRefusal(
            "logical establishment's accepted Recensor review has no object payload; the "
            "Archetypus is refused because acceptance has no checkable evidence"
        )
    if review_payload.get("perlectio_ref") != perlectio_ref:
        raise SchemaRefusal(
            "logical establishment's accepted Recensor review cites another Perlectio; the "
            "Archetypus is refused because only the exact reviewed reading may supply text"
        )
    dissent = validate_cross_capture_dissent(cross_capture_dissent)
    if dissent["logical_act_id"] != logical_id or dissent["perlectio_ref"] != perlectio_ref:
        raise SchemaRefusal(
            "logical establishment's dissent names another logical act or Perlectio; the "
            "Archetypus is refused because its sibling evidence does not bind this reading"
        )
    autopsia = _require_the_partition_this_reading_was_made_over(
        partition=partition,
        logical_act=logical_act,
        logical_id=logical_id,
        dossier=payload["dossier"],
        dissent=dissent,
    )
    _require_joint_evidence_binding(
        logical_id=logical_id,
        accepted_perlectio=accepted_perlectio,
        accepted_review=accepted_review,
        cross_capture_dissent=dissent,
        cross_capture_dissent_ref=cross_capture_dissent_ref,
        autopsia=autopsia,
        logical_act=logical_act,
    )
    text = payload["text"]
    # Normalized before it is sealed, as the image-local constructor does: an
    # `illegible` note may legally arrive without `witness_evidence` (the
    # Perlector's HANDOFF says so), and `validate_logical_record` compares the
    # stored layer against the validated form of itself. Storing the raw layer
    # would refuse the first joint reading that annotates unread ink, and the
    # act would establish nothing. `witnesses=None` because this constructor is
    # handed plain dicts and resolves no witness roster, so quotation and
    # attribution are the two rules it cannot re-check -- the same argument
    # `common/contracts/annotations.py` makes for every read-back caller.
    annotations = validate_annotations(
        payload.get("annotations", []),
        text,
        None,
        f"accepted joint reading of {logical_id} annotations",
    )
    uncertainty = from_perlectio(payload)
    record = {
        "logical_act_id": logical_id,
        "physical_page_components": logical_act["physical_page_components"],
        "member_local_acts": logical_act["member_local_acts"],
        "text": text,
        "text_hash": digest_of(text),
        "status": "established",
        "text_status": derive_record_text_status(text, annotations, uncertainty),
        "regions": payload["basis"]["regions"],
        "provenance": payload.get("provenance"),
        "annotations": annotations,
        "uncertainty": uncertainty,
        "evidence_ref": None,
        "cross_capture_dissent_ref": cross_capture_dissent_ref,
        "perlectio_ref": perlectio_ref,
        "recensor_ref": recensor_ref,
    }
    record["self_hash"] = self_hash(record)
    return validate_logical_record(record)


def build_logical_index(records: list[dict], *, run_id: str) -> dict:
    """Build the one-row-per-logical-act index used by clustered consumers."""
    rows = []
    seen: set[str] = set()
    for record in records:
        checked = validate_logical_record(record)
        logical_id = checked["logical_act_id"]
        if logical_id in seen:
            raise FatalAccounting("the logical Archetypus index has duplicate logical subjects")
        seen.add(logical_id)
        rows.append({"logical_act_id": logical_id, "text_hash": checked["text_hash"]})
    index = {
        "schema": "archetypus-logical-index.v1",
        "run_id": run_id,
        "record_count": len(rows),
        "rows": sorted(rows, key=lambda row: row["logical_act_id"]),
    }
    index["self_hash"] = self_hash(index)
    return index


def _no_readable_text_evidence(
    review: dict, reading_ref: dict[str, str], reading_inputs: list
) -> dict[str, str] | None:
    """Return the Recensor's retained blank proof; never manufacture one here.

    `HANDOFF.md`'s whole argument for this field is that an accepted review is
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
    stage-local read and `HANDOFF.md` both rely on.
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
    if not isinstance(region["witness_covered"], bool):
        raise SchemaRefusal(
            f"{label} has non-boolean witness_covered; geometric witness coverage is a fact, "
            "not an omitted or truthy presentation hint"
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
        # The Armarium's frozen `verify_established_record` builds its expected
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
    `2_designator/blobs/...` never overlap) makes structurally impossible today.
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
        context, review, reading, reading_ref, act["act_id"], page_id=act.get("page_id")
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
    uncertainty = from_perlectio(payload)
    text_status = derive_record_text_status(text, annotations, uncertainty)
    evidence_ref = _no_readable_text_evidence(review, reading_ref, reading.get("inputs", []))
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
        "uncertainty": uncertainty,
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
    # One walk of the Recensor manifest, not one per act: `final_review` per
    # act re-walks and re-verifies the whole stage, an O(acts^2) finishing
    # step a whole parish would pay for. `latest_attempt` per act keeps every
    # refusal — a missing, duplicate or non-contiguous attempt still fails.
    by_subject: dict[str, list[dict]] = {}
    for entry in context.tree.build_manifest(RECENSOR)["artifacts"]:
        if entry["kind"] != "review":
            continue
        record = context.tree.read_artifact(RECENSOR, "review", entry["artifact_id"])
        by_subject.setdefault(entry["subject_id"], []).append(record)
    accepted: set[str] = set()
    for act in expected_acts(context):
        act_id = act["act_id"]
        review = latest_attempt(
            by_subject.get(act_id, []), f"review of {act_id}", operation="recense"
        )
        if review["outcome"] == "accepted":
            accepted.add(act_id)
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
    omitted — the one-argument consumer form HANDOFF.md documents. The stage's
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
    """Run under the explicitly supplied chair/config implementation."""
    args = stage_parser(__doc__.splitlines()[0]).parse_args()
    context = open_context(args, ARCHETYPUS, registry_factory=registry_factory)

    unresolved_acts: list[str] = []
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
            # A held-for-review, confirmed-blank, or failed act is already
            # terminal at the Recensor — the outcome algebra resolves its
            # category without an Archetypus record — and that absence is what
            # the Armarium reconciles against. An outcome the algebra leaves
            # unresolved (recovery-requested: "flows onward, nobody has decided
            # yet") is different in kind: skipping it silently and exiting 0
            # would report success over an act whose reread never happened.
            if terminal_category(RECENSOR, review["outcome"]) is None:
                unresolved_acts.append(act_id)
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

    # Reconciled against the Recensor's accepted set *before* it is published, so
    # a run that fails its accounting never leaves an internally consistent index
    # on disk that summarizes fewer acts than the Recensor accepted. Then read
    # back and checked again against the same cached rows, proving the bytes on
    # disk parse to the index just checked. The read-back deliberately does NOT
    # re-derive the rows — nothing can publish a record between these two calls
    # in one process, and re-reading a parish of records per pass buys nothing.
    # A change that moves record publication after this point must drop the
    # cached arguments, or the read-back would pass against stale rows.
    on_disk = {row["act_id"]: row for row in _archetypus_rows(context)}
    accepted = accepted_act_ids(context)
    index = validate_index(context, build_index(context), on_disk=on_disk, accepted=accepted)
    context.tree.write_index(ARCHETYPUS, index)
    validate_index(context, context.tree.read_index(ARCHETYPUS), on_disk=on_disk, accepted=accepted)
    context.seal_boundary()
    context.finish()
    # Establishment for the accepted acts is real either way; the exit code
    # answers a different question — "is this stage's work finished?" — and an
    # outstanding recovery request means it is not (the Armarium refuses the
    # same state as fatal). EXIT_HELD, exactly as the Recensor reports its own
    # unfinished acts.
    if unresolved_acts:
        print(
            f"held: {len(unresolved_acts)} act(s) with an outstanding recovery request "
            f"await a reread before an Archetypus can exist: {sorted(unresolved_acts)}",
            file=sys.stderr,
        )
        return EXIT_HELD
    return EXIT_COMPLETE


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
