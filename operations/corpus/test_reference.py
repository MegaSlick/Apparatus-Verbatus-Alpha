"""`reference-page-truth.v1`: minted, closed, and refused by name."""

from __future__ import annotations

import pytest

from common.contracts.canonical import digest_bytes
from common.contracts.identities import physical_act_id, physical_page_id

from . import CorpusRefusal
from .reference import CORPUS_ID, SCHEMA, build_reference_page, validate_reference_page

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
    tampered["expected_act_count"] = reference["expected_act_count"]
    tampered["designation"] = "00099"
    with pytest.raises(CorpusRefusal, match="self-hash-mismatch"):
        validate_reference_page(tampered)


def test_validate_refuses_extra_field():
    reference = _build()
    tampered = dict(reference)
    tampered["extra"] = True
    with pytest.raises(CorpusRefusal, match="malformed-record"):
        validate_reference_page(tampered)
