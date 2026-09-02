"""`reference-page-truth.v1`: minted, closed, and refused by name."""

from __future__ import annotations

import pytest

from common.contracts.canonical import digest_bytes
from common.contracts.canonical import self_hash as _self_hash
from common.contracts.identities import physical_act_id, physical_page_id

from . import CorpusRefusal
from .reference import (
    CORPUS_ID,
    REFERENCE_REFUSAL_REASONS,
    SCHEMA,
    build_reference_page,
    validate_reference_page,
)

PAGE_SHA256 = "a" * 64


def _page(width: int = 2000, height: int = 3000) -> dict:
    return {"sha256": PAGE_SHA256, "width": width, "height": height}


def _record(
    record_id: str, region: dict, split: str = "val", text: str = "Baptisé le premier"
) -> dict:
    return {
        "record_id": record_id,
        "region": region,
        "split": split,
        "text": text,
        "text_sha256": digest_bytes(text.encode("utf-8")),
    }


def _build(**overrides) -> dict:
    kwargs = {
        "page": _page(),
        "source": "Ardennes",
        "volume": "geneanet/Ardennes_BMS/380403",
        "designation": "00026",
        "split": "val",
        "records": [
            _record("rec-1", {"x": 100, "y": 100, "w": 200, "h": 80}),
            _record("rec-2", {"x": 100, "y": 200, "w": 200, "h": 80}),
        ],
    }
    kwargs.update(overrides)
    return build_reference_page(**kwargs)


def test_builds_and_round_trips():
    reference = _build()
    assert reference["schema"] == SCHEMA
    assert reference["corpus_id"] == CORPUS_ID
    assert reference["completeness"] == "records-only"
    assert reference["provenance"] == "third-party-expert-annotation"
    assert reference["independent_readings"] == 1
    assert reference["adjudicated_by"] is None
    assert reference["provenance_class"] == "cleared_public"
    assert reference["expected_act_count"] == 2
    assert [act["record_id"] for act in reference["acts"]] == ["rec-1", "rec-2"]
    # Round-trips through validation unchanged.
    assert validate_reference_page(dict(reference)) == reference


def test_physical_act_id_matches_the_declared_ladder():
    reference = _build()
    physical_page = physical_page_id(CORPUS_ID, "Ardennes/geneanet/Ardennes_BMS/380403", "00026")
    expected = physical_act_id(physical_page, "rec-1")
    assert reference["acts"][0]["physical_act_id"] == expected
    assert expected.startswith("pac_")


def test_physical_act_id_never_derived_from_the_box():
    """Two reference pages with the same record_id but different boxes mint one id.

    `SPEC.md` Section 5.3(c): `physical_act_id` binds the declared page and
    `record_id`, never the box. A derivation from the box would produce two
    different ids for what is, by RecordGold's own record_id, one act.
    """
    first = _build(records=[_record("rec-1", {"x": 0, "y": 0, "w": 50, "h": 50})])
    second = _build(records=[_record("rec-1", {"x": 900, "y": 900, "w": 50, "h": 50})])
    assert first["acts"][0]["physical_act_id"] == second["acts"][0]["physical_act_id"]


def test_refuses_unknown_split():
    with pytest.raises(CorpusRefusal, match="unknown-split"):
        _build(split="holdout")


def test_refuses_split_not_present_among_records():
    with pytest.raises(CorpusRefusal, match="split-not-present"):
        _build(
            split="test",
            records=[_record("rec-1", {"x": 0, "y": 0, "w": 10, "h": 10}, split="val")],
        )


def test_refuses_region_outside_page():
    with pytest.raises(CorpusRefusal, match="region-outside-page"):
        _build(
            page=_page(width=100, height=100),
            records=[_record("rec-1", {"x": 50, "y": 50, "w": 100, "h": 100})],
        )


def test_refuses_duplicate_record_id():
    with pytest.raises(CorpusRefusal, match="duplicate-record-id"):
        _build(
            records=[
                _record("rec-1", {"x": 0, "y": 0, "w": 10, "h": 10}),
                _record("rec-1", {"x": 20, "y": 20, "w": 10, "h": 10}),
            ]
        )


def test_refuses_duplicate_region():
    with pytest.raises(CorpusRefusal, match="duplicate-region"):
        _build(
            records=[
                _record("rec-1", {"x": 0, "y": 0, "w": 10, "h": 10}),
                _record("rec-2", {"x": 0, "y": 0, "w": 10, "h": 10}),
            ]
        )


def test_refuses_text_sha256_mismatch():
    record = _record("rec-1", {"x": 0, "y": 0, "w": 10, "h": 10})
    record["text_sha256"] = "0" * 64
    with pytest.raises(CorpusRefusal, match="text-sha256-mismatch"):
        _build(records=[record])


def test_refuses_empty_records():
    with pytest.raises(CorpusRefusal, match="empty-acts"):
        _build(records=[])


def test_validate_refuses_act_id_where_pac_id_expected():
    reference = _build()
    tampered = dict(reference)
    tampered_acts = [dict(act) for act in reference["acts"]]
    tampered_acts[0]["physical_act_id"] = "act_0123456789abcdef"
    tampered["acts"] = tampered_acts
    with pytest.raises(CorpusRefusal, match="wrong-identity-family"):
        validate_reference_page(tampered)


def test_validate_refuses_non_null_adjudicated_by():
    reference = _build()
    tampered = dict(reference)
    tampered["adjudicated_by"] = "some-person"
    with pytest.raises(CorpusRefusal, match="malformed-record"):
        validate_reference_page(tampered)


def test_validate_refuses_tampered_self_hash():
    reference = _build()
    tampered = dict(reference)
    # A field no other check depends on, so this exercises self-hash verification
    # alone rather than tripping over an earlier, more specific refusal.
    tampered_page = dict(reference["page"])
    tampered_page["height"] = reference["page"]["height"] + 1
    tampered["page"] = tampered_page
    with pytest.raises(CorpusRefusal, match="self-hash-mismatch"):
        validate_reference_page(tampered)


def test_validate_refuses_extra_field():
    reference = _build()
    tampered = dict(reference)
    tampered["extra"] = True
    with pytest.raises(CorpusRefusal, match="malformed-record"):
        validate_reference_page(tampered)


# --- Every declared refusal reason actually fires -------------------------------


def test_validate_refuses_wrong_schema():
    reference = _build()
    tampered = dict(reference)
    tampered["schema"] = "some-other.v1"
    with pytest.raises(CorpusRefusal, match="^wrong-schema:"):
        validate_reference_page(tampered)


def test_validate_refuses_wrong_corpus():
    reference = _build()
    tampered = dict(reference)
    tampered["corpus_id"] = "some-other-corpus"
    with pytest.raises(CorpusRefusal, match="^wrong-corpus:"):
        validate_reference_page(tampered)


def test_validate_refuses_act_count_mismatch():
    reference = _build()
    tampered = dict(reference)
    tampered["expected_act_count"] = len(reference["acts"]) + 1
    with pytest.raises(CorpusRefusal, match="^act-count-mismatch:"):
        validate_reference_page(tampered)


def test_validate_refuses_empty_text_on_an_act():
    reference = _build()
    tampered = dict(reference)
    tampered_acts = [dict(act) for act in reference["acts"]]
    tampered_acts[0]["text"] = ""
    tampered["acts"] = tampered_acts
    with pytest.raises(CorpusRefusal, match="^empty-text:"):
        validate_reference_page(tampered)


def test_build_refuses_a_whitespace_only_designation_as_unmintable():
    with pytest.raises(CorpusRefusal, match="^unmintable-physical-act:"):
        _build(designation="   ")


def test_reference_refusal_reasons_covered_here_are_a_subset_of_the_declared_vocabulary():
    exercised = {
        "malformed-record",
        "wrong-schema",
        "wrong-corpus",
        "unknown-split",
        "split-not-present",
        "empty-acts",
        "duplicate-record-id",
        "duplicate-region",
        "act-count-mismatch",
        "region-outside-page",
        "empty-text",
        "text-sha256-mismatch",
        "wrong-identity-family",
        "unmintable-physical-act",
        "duplicate-physical-act-id",
        "self-hash-mismatch",
    }
    assert exercised <= REFERENCE_REFUSAL_REASONS


# --- The pac_ identity binds this page and this record_id, not the box ---------


def test_validate_refuses_a_well_formed_pac_id_minted_for_a_different_page():
    """A forged-but-well-formed `pac_` that verifies against its own bindings must
    still be refused: it was minted for a page, or a record, this reference page
    never declares.

    This is the exact forgery the module docstring names: well-formed, verifies
    against its own bindings, means nothing.
    """
    reference = _build()
    foreign_physical_page = physical_page_id("recordgold", "SOMEWHERE/ELSE", "99999")
    foreign_id = physical_act_id(foreign_physical_page, "not-this-record")

    tampered_acts = [dict(act) for act in reference["acts"]]
    tampered_acts[0]["physical_act_id"] = foreign_id
    tampered = dict(reference)
    tampered["acts"] = tampered_acts
    tampered["self_hash"] = _self_hash(tampered)

    with pytest.raises(CorpusRefusal, match="^wrong-identity-family:"):
        validate_reference_page(tampered)


def test_validate_refuses_a_non_string_element_in_splits_present_by_name():
    """A non-string in `splits_present` must refuse by name, not leak a bare `TypeError`.

    `splits_present != sorted(set(splits_present))` sorts before checking element
    types; mixing `str` and `int` raises an unguarded `TypeError` in CPython.
    """
    reference = _build()
    tampered = dict(reference)
    tampered["splits_present"] = [1, "val"]
    with pytest.raises(CorpusRefusal, match="^malformed-record:"):
        validate_reference_page(tampered)


def test_validate_refuses_duplicate_physical_act_ids_from_whitespace_variant_record_ids():
    """Two record_ids that fold to the same declared text mint one join key.

    `physical_act_bindings` NFC-normalises and collapses whitespace runs
    (`common/contracts/identities.py`), so `"rec 1"` and `"rec  1"` are one
    declaration by that fold even though they are two distinct raw record_ids
    here -- and `misses`/`matched_pairs` in `compare.py` key on `physical_act_id`,
    so a silent collision would make two acts indistinguishable rows.
    """
    with pytest.raises(CorpusRefusal, match="^duplicate-physical-act-id:"):
        _build(
            records=[
                _record("rec 1", {"x": 0, "y": 0, "w": 10, "h": 10}),
                _record("rec  1", {"x": 100, "y": 100, "w": 10, "h": 10}),
            ]
        )
