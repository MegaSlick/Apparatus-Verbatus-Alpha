"""The record's field set is closed, and the closure is what refuses the dead shape.

Spec 10: "Exactly one `text` field. No fallback chain, no alternate-text fields,
no display variant stored beside it... a reviewer finding a second text-bearing
field finds a defect." The old pipeline's export reached through
`consolidated_literal`, `reader_text`, `literal`, `text`, `markdown` for whichever
was non-empty (read in the window at `remote/export_views.py::_unit_text`; nothing
from it is carried here). A closed field set is what stops that being rebuilt one
field at a time, and it is checked mechanically rather than by reading the
constructor.
"""

import importlib.util
import inspect
from pathlib import Path

import pytest

from common.contracts.canonical import digest_of, self_hash, verify_self_hash
from common.contracts.errors import FatalAccounting, SchemaRefusal
from common.contracts.outcomes import VOCABULARIES
from common.contracts.stages import PERLECTOR


def _load_archetypus():
    path = Path(__file__).resolve().parent / "run.py"
    spec = importlib.util.spec_from_file_location("archetypus_run_under_test_schema", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


archetypus = _load_archetypus()

ACT = {"act_id": "act_0000000000000001", "act_key": "a1", "page_id": "pg_0000000000000001"}
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
    "structure_provenance": {"chair": "designator"},
    "witness_covered": True,
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
        "annotations": [],
        "uncertainty": {"uncertain_spans": [], "gaps": [], "self_revisions": []},
        "evidence_ref": None,
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
    assert "establish_from_accepted_primed_perlectio" in {
        name
        for name, function in inspect.getmembers(archetypus, inspect.isfunction)
        if not name.startswith("_")
    }
    assert tuple(
        inspect.signature(archetypus.establish_from_accepted_primed_perlectio).parameters
    ) == (
        "context",
        "act",
        "review_ref",
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
    for text, text_status, evidence in (
        ("Maria", "established", None),
        ("Maria", "partial", None),
        ("", "no_readable_text", REVIEW_REF),
    ):
        record = make_record(
            text=text,
            text_hash=digest_of(text),
            text_status=text_status,
            evidence_ref=evidence,
            annotations=(
                [{"kind": "illegible", "start": 0, "end": 0, "witness_evidence": []}]
                if text_status == "partial"
                else []
            ),
        )
        assert record["status"] == "established"
        assert record["text_status"] == text_status


def test_dissent_travels_by_reference_and_never_by_value():
    """Tyrel's 4d. The pointer is the Perlectio; no dissent rows are copied in."""
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


def test_record_validation_refuses_a_resealed_wrong_text_hash():
    record = make_record()
    record["text_hash"] = digest_of("Marta")
    record["self_hash"] = self_hash(record)
    with pytest.raises(SchemaRefusal, match="text_hash disagrees"):
        archetypus.validate_record(record)


def test_record_validation_refuses_a_resealed_dishonest_text_status():
    record = make_record()
    record["text_status"] = "partial"
    record["self_hash"] = self_hash(record)
    with pytest.raises(SchemaRefusal, match="disagrees with its text"):
        archetypus.validate_record(record)


def test_record_validation_refuses_a_bad_nested_self_hash():
    record = make_record()
    record["act_key"] = "edited-after-construction"
    with pytest.raises(SchemaRefusal, match="nested self-hash"):
        archetypus.validate_record(record)


# --- The rest of the resealed-record refusals, each exercised ------------------
#
# `validate_record` runs on every later stage-local read, and HANDOFF.md offers
# it to any consumer wanting to prove a record before relying on it. So each of
# its refusals gets a case that fails without it: a refusal no test can kill is
# a claim nobody has measured.


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"status": "partial"}, "fixed 'established' literal"),
        ({"act_id": ""}, "has no act_id"),
        ({"act_key": 7}, "has no act_key"),
        ({"page_id": None}, "has no page_id"),
        ({"text": 7}, "text is not a string"),
        ({"regions": []}, "retains no source region"),
        ({"regions": "2_designator/blobs/sha256/deadbeef"}, "retains no source region"),
        ({"provenance": "perlector"}, "provenance is not an object"),
        ({"perlectio_ref": {"relative_path": "x"}}, "perlectio_ref is not a digest-checked"),
        ({"recensor_ref": None}, "recensor_ref is not a digest-checked"),
        ({"annotations": {}}, "annotation is not a list"),
        ({"annotations": ["not an object"]}, r"annotation\[0\] is not an object"),
    ],
)
def test_record_validation_refuses_each_resealed_defect(overrides, expected):
    with pytest.raises(SchemaRefusal, match=expected):
        archetypus.validate_record(seal_record(**overrides))


def test_record_validation_refuses_a_resealed_region_outside_the_closed_schema():
    """The read-back proof used to stop at the record's top level.

    `_crop_references` closes `_REGION_FIELDS` at construction — the fix that
    stopped `consolidated_literal`, the first name in the old pipeline's dead
    fallback chain, from travelling sealed into the record and out through the
    export. But `validate_record`, the function every later stage-local read and
    `HANDOFF.md` both rely on, checked only that `regions` was a non-empty list:
    a record resealed on disk with the same dead field smuggled inside a region
    passed it. `_validate_region_fields` now runs on both paths.
    """
    smuggled = dict(REGION, consolidated_literal="A SECOND READING NOBODY ESTABLISHED")
    with pytest.raises(SchemaRefusal, match="outside the closed region schema"):
        archetypus.validate_record(seal_record(regions=[smuggled]))


def test_record_validation_refuses_a_resealed_region_missing_a_crop_fact():
    stripped = {key: value for key, value in REGION.items() if key != "verified_dimensions"}
    with pytest.raises(SchemaRefusal, match="outside the closed region schema"):
        archetypus.validate_record(seal_record(regions=[stripped]))


def test_record_validation_refuses_a_dissent_pointer_that_left_its_perlectio():
    """Tyrel's 4d is that dissent travels *to this record's own Perlectio*.

    A `dissent_ref` naming some other artifact would send a reader looking for
    this act's dissent at a reading this record did not establish from.
    """
    other = {"relative_path": "4_perlector/artifacts/perlectio/art_d.json", "sha256": "d" * 64}
    with pytest.raises(SchemaRefusal, match="dissent must travel by reference"):
        archetypus.validate_record(seal_record(dissent_ref=other))


@pytest.mark.parametrize(
    ("note", "expected"),
    [
        ({"kind": "illegible", "start": 0, "end": 1, "witness_evidence": []}, "zero-width anchor"),
        ({"kind": "speculative", "start": 0, "end": 0}, "not one of"),
        (
            {"kind": "illegible", "start": 0, "end": 0, "witness_evidence": [{"variant": "x"}]},
            "is not exactly",
        ),
        (
            {"kind": "uncertain", "start": 0, "end": 5, "certainty": "0.9", "alternatives": ["M"]},
            "not one of",
        ),
        (
            {"kind": "uncertain", "start": 0, "end": 5, "certainty": "low", "alternatives": []},
            "names no alternatives",
        ),
        (
            {"kind": "uncertain", "start": 0, "end": 9, "certainty": "low", "alternatives": ["M"]},
            "outside this reading's own text bounds",
        ),
    ],
)
def test_record_validation_refuses_a_resealed_malformed_annotation(note, expected):
    """The read-back annotation check, which no test reached before.

    A record is validated again on every later stage-local read precisely
    because a sealed payload can be edited and resealed on disk. The annotation
    layer is the part of it a reader most needs to trust — it is where witness
    material sits beside the established text — so its refusals are exercised
    here as well as at the constructor, over the one validator both now use.
    """
    with pytest.raises(SchemaRefusal, match=expected):
        archetypus.validate_record(seal_record(annotations=[note], text_status="partial"))


def test_record_validation_refuses_an_annotation_short_of_its_validated_form():
    """A gap with no `witness_evidence` key validates, but not as what is stored.

    `validate_annotations` fills the absent field in, so the record on disk is
    not what validation produces from it — and a record carrying a shape the
    constructor would never have written is refused rather than normalized
    underneath the reader.
    """
    with pytest.raises(SchemaRefusal, match="not in the exact form validation produces"):
        archetypus.validate_record(
            seal_record(annotations=[{"kind": "illegible", "start": 0, "end": 0}])
        )


def test_record_validation_refuses_a_no_readable_text_record_carrying_an_annotation():
    """The two silences, kept apart at read-back as well as at construction."""
    note = {"kind": "uncertain", "start": 0, "end": 3, "certainty": "low", "alternatives": ["Ave"]}
    with pytest.raises(SchemaRefusal, match="covering no readable character"):
        archetypus.validate_record(
            seal_record(
                text="   ",
                text_hash=digest_of("   "),
                text_status="no_readable_text",
                evidence_ref=REVIEW_REF,
                annotations=[note],
            )
        )


# --- The guard that decides whether an unresolved reading can establish text ----


_READING_REF = {"relative_path": "4_perlector/artifacts/x.json", "sha256": "0" * 64}


def _perlectio(outcome: str) -> dict:
    # `lectio_kind` carries R5a's production marker for the tests that pass a
    # completed outcome: the outcome-classification guard runs first, so a
    # failing outcome refuses regardless of kind, but a completed reading
    # without the marker would stop at the later only-primed-with-prior-
    # establishes refusal instead of the guard those tests aim at.
    return {
        "stage": archetypus.PERLECTOR,
        "kind": "perlectio",
        "outcome": outcome,
        "payload": {"text": "some established characters", "lectio_kind": "primed-with-prior"},
    }


def _accepted_review() -> dict:
    """A review that passes every guard *before* the completed-class check.

    Built deliberately so the refusal under test is the one being reached: with a
    weaker review the earlier "accepts only the exact Perlectio a Recensor
    accepted" refusal fires first and the test passes without ever touching the
    guard it names. That happened on the first draft of this test.
    """
    return {
        "stage": archetypus.RECENSOR,
        "kind": "review",
        "outcome": "accepted",
        "inputs": [_READING_REF],
        "payload": {"perlectio_ref": _READING_REF, "decision": "accepted"},
    }


# Every non-`read` member of the Perlector's own closed vocabulary. `held-for-review`
# is deliberately absent: it is not one of this stage's outcomes at all, so it is
# refused a step earlier by the invariant-#10 check and would have made this test
# pass without reaching the guard it names.
@pytest.mark.parametrize(
    "outcome",
    sorted(outcome for outcome in VOCABULARIES[PERLECTOR] if outcome != "read"),
)
def test_only_a_completed_reading_may_establish_text(outcome):
    """The one guard standing between an unresolved reading and the established text.

    Measured untested by mutation during the final read of this branch: removing
    it broke nothing in a 1,761-test suite. It is also the guard that makes the
    stage-08/stage-10 field-name seam harmless — stage 08 writes gaps only under
    `no-readable-text`, which classes as unresolved, and *this* is what refuses
    it. A protection nothing exercises is a protection nobody would notice
    losing, and this one is load-bearing for a claim made to Tyrel about two
    other branches.
    """
    with pytest.raises(FatalAccounting, match="may only come"):
        archetypus.accepted_primed_perlectio(
            None,
            _accepted_review(),
            _perlectio(outcome),
            _READING_REF,
            "act_0000000000000001",
        )


def test_a_completed_reading_is_not_refused_by_that_guard():
    """Invariant #14: the refusal must not have been bought by refusing good input.

    A `read` outcome passes the completed-class check — it fails later, on the
    parts of the boundary this test does not supply, which is what proves the
    guard above is the thing being exercised rather than some earlier refusal.
    """
    with pytest.raises(FatalAccounting, match="no object basis") as caught:
        archetypus.accepted_primed_perlectio(
            None,
            _accepted_review(),
            _perlectio("read"),
            _READING_REF,
            "act_0000000000000001",
        )
    assert "may only come" not in str(caught.value)


# --- The act-attachment view is required, not merely checked when present -------
#
# Opus audit-and-repair seat 3, R0. F-O2: `accepted_primed_perlectio` checked the
# R0 act-attachment dossier view only `if attachment is not None`, so a resealed
# reading that had simply dropped the field walked past the whole page-witness
# custody chain -- reference shape, direct-input binding, and the digest-checked
# dereference of the attachment artifact itself. The retained Testimonium basis
# beside it was already required for the same reason.


def _primed_perlectio_without_an_attachment_view() -> dict:
    """A `read` Perlectio that passes every guard before the attachment check.

    Regions and a retained Testimonium basis are supplied precisely so the
    refusal under test is the one reached, not an earlier and unrelated one --
    the idiom `test_only_a_completed_reading_may_establish_text` above already
    applies on this same boundary.
    """
    return {
        "stage": archetypus.PERLECTOR,
        "kind": "perlectio",
        "outcome": "read",
        "inputs": [_READING_REF],
        "payload": {
            "text": "some established characters",
            # R5a's production marker, so the attachment-view refusal under test
            # is reached instead of the earlier lectio_kind gate (host fix).
            "lectio_kind": "primed-with-prior",
            "basis": {
                "regions": [{"image_path": "2_designator/blobs/sha256/deadbeef"}],
                "testimonia": [{"chair": "attestator_1", "testimonium_ref": _READING_REF}],
            },
            "dossier": {"act_key": "a1", "dossier_digest": "d" * 64},
        },
    }


def test_a_primed_reading_without_its_act_attachment_view_may_not_establish_text():
    """R0's exit criterion says the attachment is consumed, not consumed-if-present."""
    with pytest.raises(SchemaRefusal, match="no act-attachment view"):
        archetypus.accepted_primed_perlectio(
            None,
            _accepted_review(),
            _primed_perlectio_without_an_attachment_view(),
            _READING_REF,
            "act_0000000000000001",
        )
