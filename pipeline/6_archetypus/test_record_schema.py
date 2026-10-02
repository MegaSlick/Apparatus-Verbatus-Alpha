"""The record's field set is closed, and the closure is what refuses the dead shape.

A record has exactly one `text` field: no fallback chain, no alternate-text
fields, no display variant stored beside it, so a second text-bearing field is a
defect. A closed field set is what stops a chain of text fields being rebuilt one
field at a time, and it is checked mechanically rather than by reading the
constructor.
"""

import inspect

import pytest

from common.contracts.canonical import digest_of, self_hash, verify_self_hash
from common.contracts.errors import FatalAccounting, SchemaRefusal
from conftest import load_stage

archetypus = load_stage("6_archetypus")

ACT = {
    "act_id": "act_0000000000000001",
    "act_key": "a1",
    "page_id": "pg_0000000000000001",
    "kind": "act",
}
READING_REF = {"relative_path": "4_perlector/artifacts/perlectio/art_b.json", "sha256": "b" * 64}
REVIEW_REF = {"relative_path": "5_recensor/artifacts/review/art_c.json", "sha256": "c" * 64}
REGION = {
    "region_id": "reg_0000000000000001",
    "image_path": "2_designator/blobs/sha256/deadbeef",
    "image_sha256": "d" * 64,
    "verified_dimensions": {"w": 100, "h": 50},
    "source_page_ordinal": 1,
    "source_page_id": "pg_0000000000000001",
    "transform": {"x": 0, "y": 0, "w": 100, "h": 50},
}


# The reader's own doubt assessment, closed into the canonical uncertainty
# layer because the span layers alone cannot say whether an empty list is "no
# doubt" or "no doubt was ever asked for". `assessed` is the state every
# record below wants, because it is the only one under which the spans and
# gaps may be non-empty.
_ASSESSED = {"state": "assessed", "problem": None}


def _uncertainty(**overrides) -> dict:
    """A canonical uncertainty layer, empty but for what a case overrides."""
    return {
        "uncertain_spans": [],
        "gaps": [],
        "self_revisions": None,
        "assessment": _ASSESSED,
        "lectio_kind": "page-read",
        **overrides,
    }


def seal_record(**overrides) -> dict:
    """A record with a correct self-hash, whether or not it is otherwise valid.

    Every refusal below is about a record that was *resealed* after editing —
    a self-hash mismatch would refuse it a step earlier and prove nothing about
    the check under test.
    """
    record = {
        **ACT,
        "text": "Maria",
        "text_hash": digest_of("Maria"),
        "status": "established",
        "text_status": "established",
        "regions": [dict(REGION)],
        "provenance": {"chair": "perlector"},
        "uncertainty": _uncertainty(),
        "dissent_ref": READING_REF,
        "perlectio_ref": READING_REF,
        "recensor_ref": REVIEW_REF,
    }
    record.update(overrides)
    record["self_hash"] = self_hash(record)
    return record


def make_record(**overrides) -> dict:
    record = seal_record(**overrides)
    archetypus.validate_record(record)
    return record


def test_the_only_public_constructor_resolves_the_accepted_evidence_itself():
    assert not hasattr(archetypus, "build_record")
    assert not hasattr(archetypus, "_build_record")
    assert tuple(inspect.signature(archetypus.establish_from_accepted_page_reading).parameters) == (
        "context",
        "row",
        "review_ref",
        "page_testimonia",
        "applied",
        "approvals",
    )


def test_the_record_carries_exactly_the_closed_field_set():
    record = make_record()
    assert set(record) == set(archetypus._RECORD_FIELDS)
    assert verify_self_hash(record)


def test_exactly_one_field_holds_the_established_characters():
    """Every other string-valued field is a hash, a status, or an identifier.

    Pinned by field name against the closed schema, not by comparing values: a
    revived fallback field holding *different* characters (the old pipeline's
    exact shape) would never equal `text`, so a value filter cannot fail. The
    test below proves the closed set refuses such a field outright.
    """
    # The closed set, spelled out: any revived fallback field — reader_text,
    # alternate_text, literal, markdown, consolidated_literal — fails here by
    # name rather than by a suffix scan that catches only two of the five.
    assert archetypus._RECORD_FIELDS == frozenset(
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
            "uncertainty",
            "dissent_ref",
            "perlectio_ref",
            "recensor_ref",
            "self_hash",
        }
    )


def test_a_second_text_bearing_field_is_outside_the_closed_schema():
    record = make_record()
    forged = dict(record, alternate_text="Marta")
    forged["self_hash"] = self_hash(forged)
    with pytest.raises(SchemaRefusal, match="unexpected"):
        archetypus.validate_record_fields(forged)


def test_a_missing_field_is_refused_as_loudly_as_an_extra_one():
    record = make_record()
    forged = {field: value for field, value in record.items() if field != "text_status"}
    with pytest.raises(SchemaRefusal, match="missing"):
        archetypus.validate_record_fields(forged)


def test_status_is_the_record_level_literal_and_never_mirrors_text_status():
    """`status` answers "does this act have exactly one Archetypus record", which
    the Armarium checks literally. `text_status` answers what the text contains.
    Mirroring them would make every damaged act fail that consumer's check, and
    would put a second status decision where there is meant to be one."""
    gap = {"position": "leading", "start": 0, "end": 0, "witness_evidence": []}
    for text_status, gaps in (("established", []), ("partial", [gap])):
        record = make_record(text_status=text_status, uncertainty=_uncertainty(gaps=gaps))
        assert record["status"] == "established"
        assert record["text_status"] == text_status


def test_dissent_travels_by_reference_and_never_by_value():
    """The pointer is the Perlectio; no dissent rows are copied in."""
    record = make_record()
    assert record["dissent_ref"] == READING_REF
    assert record["dissent_ref"] == record["perlectio_ref"]
    assert "dissent" not in record


def test_the_text_hash_is_the_digest_of_the_text_alone():
    """Scope stated plainly: `make_record` goes through `seal_record`, not the
    full constructor, so this and the dissent test above pin the sealed shape
    against values their own fixture wrote. That the *constructor* assigns
    these fields correctly from real accepted evidence is proven end to end by
    the acceptance suite and `test_projection_identity.py`, which hash-check
    text/text_hash agreement on records the real CLI established."""
    record = make_record()
    assert record["text_hash"] == digest_of("Maria")


def test_record_validation_accepts_a_partial_text_with_an_internal_gap():
    gap = {
        "position": "internal",
        "start": 2,
        "end": 2,
        "witness_evidence": [],
    }

    record = make_record(
        text_status="partial",
        uncertainty=_uncertainty(gaps=[gap]),
    )

    assert record["text_status"] == "partial"
    assert record["uncertainty"]["gaps"] == [gap]


def test_record_validation_refuses_a_bad_nested_self_hash():
    record = make_record()
    record["act_key"] = "edited-after-construction"
    with pytest.raises(SchemaRefusal, match="nested self-hash"):
        archetypus.validate_record(record)


# --- The rest of the resealed-record refusals, each exercised ------------------
#
# `validate_record` runs on every later stage-local read, and CONTRACT.md offers
# it to any consumer wanting to prove a record before relying on it. So each of
# its refusals gets a case that fails without it: a refusal no test can kill is
# a claim nobody has measured.


def test_record_validation_refuses_a_dissent_pointer_that_left_its_perlectio():
    """Dissent travels *to this record's own Perlectio*.

    A `dissent_ref` naming some other artifact would send a reader looking for
    this act's dissent at a reading this record did not establish from.
    """
    other = {"relative_path": "4_perlector/artifacts/perlectio/art_d.json", "sha256": "d" * 64}
    with pytest.raises(SchemaRefusal, match="dissent must travel by reference"):
        archetypus.validate_record(seal_record(dissent_ref=other))


@pytest.mark.parametrize(
    ("gap", "expected"),
    [
        (
            {"position": "not-a-real-position", "start": 2, "end": 2, "witness_evidence": []},
            "is not one of",
        ),
        (
            {"position": "leading", "start": 3, "end": 3, "witness_evidence": []},
            "declared leading but does not start at 0",
        ),
        (
            {"position": "trailing", "start": 2, "end": 2, "witness_evidence": []},
            "declared trailing but text follows it",
        ),
        (
            {"position": "internal", "start": 0, "end": 0, "witness_evidence": []},
            "declared internal but is not strictly inside",
        ),
    ],
)
def test_record_validation_refuses_a_gap_whose_position_label_lies_about_its_own_bounds(
    gap, expected
):
    """The canonical uncertainty layer's gap position is a claim, not free text.

    A resealed record must not claim `leading` three characters in, or
    `internal` at the very edge of the text: a labelled gap's bounds are
    checked against what that label means, the same way the producer-side
    `common/reading_annotations.py::validate_gaps` checks them, so the
    canonical projection layer does not trust a restatement its own
    producer would have refused to write.
    """
    with pytest.raises(SchemaRefusal, match=expected):
        archetypus.validate_record(seal_record(uncertainty=_uncertainty(gaps=[gap])))


def test_record_validation_refuses_a_page_reading_that_claims_self_revisions():
    """A page reading had no prior draft, so it has no self-revisions to record."""
    revision = {"reading_span": {"start": 0, "end": 0}, "prior_span": {"start": 0, "end": 0}}
    with pytest.raises(SchemaRefusal, match="self-revisions are not measured"):
        archetypus.validate_record(seal_record(uncertainty=_uncertainty(self_revisions=[revision])))


def test_record_validation_refuses_an_uncertain_span_outside_the_canonical_text():
    """Canonical offsets are measured only against this record's one text."""
    span = {"start": 0, "end": 6, "alternatives": ["Mariam"], "confidence": "low"}
    with pytest.raises(SchemaRefusal, match="outside the canonical text's Unicode offsets"):
        archetypus.validate_record(seal_record(uncertainty=_uncertainty(uncertain_spans=[span])))


def test_record_validation_refuses_a_whole_act_gap_beside_any_other_gap():
    """Wholly unread and partly read are mutually exclusive canonical claims."""
    gaps = [
        {"position": "whole-act", "start": 0, "end": 0, "witness_evidence": []},
        {"position": "leading", "start": 0, "end": 0, "witness_evidence": []},
    ]
    with pytest.raises(SchemaRefusal, match="whole-act gap must be the only gap"):
        archetypus.validate_record(
            seal_record(
                text="",
                text_hash=digest_of(""),
                text_status="partial",
                uncertainty=_uncertainty(gaps=gaps),
            )
        )


def test_record_validation_refuses_a_partly_read_gap_over_an_empty_text():
    """The same exclusivity, reached from the side the position rules leave open.

    Every bounds rule is satisfied vacuously over an empty text -- `leading`
    starts at 0 and `trailing` ends at `len("")` -- and the whole-act rule only
    runs when the label already says `whole-act`. Without its own check an empty
    text could carry a gap declaring a partly-read position: read characters
    around a gap, where no character was read at all.
    """
    gap = {"position": "leading", "start": 0, "end": 0, "witness_evidence": []}
    with pytest.raises(SchemaRefusal, match="over an empty text"):
        archetypus.validate_record(
            seal_record(
                text="",
                text_hash=digest_of(""),
                text_status="partial",
                uncertainty=_uncertainty(gaps=[gap]),
            )
        )


def test_record_validation_refuses_unvalidated_gap_witness_evidence():
    """Every nested field retained by the canonical layer is validated on reseal."""
    gap = {
        "position": "internal",
        "start": 2,
        "end": 2,
        "witness_evidence": [{"chair": "attestator_1"}],
    }
    with pytest.raises(SchemaRefusal, match=r"witness_evidence\[0\].*record"):
        archetypus.validate_record(seal_record(uncertainty=_uncertainty(gaps=[gap])))


def test_record_validation_refuses_gap_evidence_with_a_non_digest_reference():
    """Canonical provenance cannot retain an uncheckable digest-shaped claim."""
    evidence = {
        "chair": "attestator_1",
        "testimonium_id": "testimonium-1",
        "reference": {"relative_path": "3_attestatores/testimonium.json", "sha256": "nope"},
        "variant": "Maria",
    }
    gap = {"position": "internal", "start": 2, "end": 2, "witness_evidence": [evidence]}
    with pytest.raises(SchemaRefusal, match="sha256 is not a lowercase sha256"):
        archetypus.validate_record(seal_record(uncertainty=_uncertainty(gaps=[gap])))


def test_two_groups_naming_one_crop_path_collapse_to_a_single_input():
    """`_direct_inputs`'s dedup-by-path guards the cross-group case -- a review
    or Perlectio reference coinciding with a crop path -- which the run tree's
    layout makes structurally impossible today; `_crop_references` already
    refuses two *regions* naming one crop path before this function runs. The
    dedup is the cheap defensive form of that layout guarantee, and this test
    pins the collapse plus the no-distinct-input-dropped half so the defence
    cannot rot unnoticed.
    """
    shared = {"relative_path": "2_designator/blobs/ab/cdef", "sha256": "a" * 64}
    other = {"relative_path": "4_perlector/artifacts/reading.json", "sha256": "b" * 64}

    combined = archetypus._direct_inputs([shared, other], [shared])

    paths = [reference["relative_path"] for reference in combined]
    assert len(paths) == len(set(paths)), (
        f"one crop path reached the envelope twice: {paths}; build_envelope refuses "
        "a path listed twice, so the defensive collapse must hold"
    )
    assert set(paths) == {shared["relative_path"], other["relative_path"]}, (
        "collapsing duplicates must not drop a distinct input"
    )

    # The other half of the same function: one path cannot hold two sets of
    # bytes, so collapsing them silently would seal a record whose inputs the
    # Armarium cannot reconcile at export.
    conflicting = {"relative_path": shared["relative_path"], "sha256": "c" * 64}
    with pytest.raises(FatalAccounting, match="different digests"):
        archetypus._direct_inputs([shared], [conflicting])
