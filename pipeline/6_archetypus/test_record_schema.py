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
from common.contracts.errors import SchemaRefusal


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


def make_record(**overrides) -> dict:
    record = {
        **ACT,
        "text": "Maria",
        "text_hash": digest_of("Maria"),
        "status": "established",
        "text_status": "established",
        "regions": [{"image_path": "2_designator/blobs/sha256/deadbeef"}],
        "provenance": {"chair": "perlector"},
        "annotations": [],
        "evidence_ref": None,
        "dissent_ref": READING_REF,
        "perlectio_ref": READING_REF,
        "recensor_ref": REVIEW_REF,
    }
    record.update(overrides)
    record["self_hash"] = self_hash(record)
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
    assert tuple(inspect.signature(archetypus.establish_from_accepted_primed_perlectio).parameters) == (
        "context",
        "act",
        "review_ref",
    )


def test_the_record_carries_exactly_the_closed_field_set():
    record = make_record()
    assert set(record) == set(archetypus._RECORD_FIELDS)
    assert verify_self_hash(record)


def test_exactly_one_field_holds_the_established_characters():
    """Every other string-valued field is a hash, a status, or an identifier."""
    record = make_record()
    text_bearing = [
        field
        for field, value in record.items()
        if isinstance(value, str) and value == record["text"]
    ]
    assert text_bearing == ["text"]


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
