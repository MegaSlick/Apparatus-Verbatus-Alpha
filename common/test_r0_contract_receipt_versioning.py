"""R0 falsification tests: receipt versioning (D9, brief priority 2/3).

Written blind, from /out/R0_CONTRACT_NOTE.md (v2) before the R0 build chamber runs.
Must fail RED on the chamber's base commit (main 176b09e) because the versioning
behaviour it checks is not yet built.

D9: the receipt schema constant versions (v1 -> v2) when coverage grows granularity
fields; the old constant is refused with a named error, not silently accepted.
"""

from __future__ import annotations

import pytest

import common.recensor_receipt as receipt_module
from common.contracts.errors import SchemaRefusal


def test_a_v2_receipt_schema_constant_exists_once_coverage_grows_granularity_fields():
    """D9: the schema constant must version v1 -> v2 alongside the D2/D3 coverage
    growth this contract note requires. On the base commit there is exactly one
    schema constant, `recensor-partition-receipt.v1`, and coverage carries none of
    the page-granularity fields R0 needs (see test_r0_contract_floor_honesty.py).
    """
    v2_name_candidates = (
        "RECENSOR_PARTITION_RECEIPT_SCHEMA_V2",
        "RECENSOR_PARTITION_RECEIPT_SCHEMA_V2_NAME",
    )
    v2_schema = next(
        (
            getattr(receipt_module, name)
            for name in v2_name_candidates
            if hasattr(receipt_module, name)
        ),
        None,
    )
    if v2_schema is None:
        pytest.fail(
            "common/recensor_receipt.py carries no v2 receipt schema constant "
            f"(checked for {v2_name_candidates!r}); D9 requires the receipt schema to "
            "version v1 -> v2 once coverage grows page-granularity fields"
        )
    assert v2_schema != receipt_module.RECENSOR_PARTITION_RECEIPT_SCHEMA, (
        "the v2 receipt schema constant is byte-identical to the v1 constant; D9 "
        "requires the two to be distinguishable schema labels"
    )


def test_a_receipt_still_declaring_the_v1_schema_is_refused_once_coverage_carries_granularity_fields():
    """D9: the old v1 constant is refused with a named error, not silently accepted.

    Builds a receipt that is otherwise well-formed under today's `v1` schema and
    validation rules, but whose coverage additionally carries the page-granularity
    fact D2 requires (`page_granularity_only`). Under D9 this combination -- v1
    schema label plus granularity-shaped coverage -- must be a NAMED refusal: a v1
    receipt was never supposed to describe page-granularity accounting at all.

    On the base commit `validate_recensor_partition_receipt` refuses this record
    anyway, but for the wrong reason (its coverage validator's field set is closed
    and rejects the extra key outright, exactly as
    test_r0_contract_floor_honesty.py's `test_coverage_schema_has_no_room_for_a_
    page_granularity_only_contribution` already proves) -- not because a version
    mismatch was detected and named as one. This test fails red for the same
    underlying reason, recorded here under D9 specifically because a build that
    fixes it by only widening the v1 coverage schema, without introducing a v2
    schema label and a version-mismatch refusal, satisfies floor honesty but not
    D9's versioning half.
    """
    proposal_seal_ref = {
        "relative_path": "designator/proposal-seal/art_seal.json",
        "sha256": "0" * 64,
    }
    review_ref = {"relative_path": "recensor/review/art_review.json", "sha256": "1" * 64}
    item = {
        "act_id": "act_0000000000000000",
        "act_key": "a1",
        "designator_outcome": "proposed",
        "review_ref": review_ref,
        "review_outcome": "accepted",
        "partition_class": "completed",
        "coverage": {
            "configured": 3,
            "floor": 3,
            "by_outcome": {"read": 3},
            "by_class": {"completed": 3, "unresolved": 0, "failed": 0},
            "under_witnessed": False,
            "unresolved_chairs": 0,
            "page_granularity_only": 1,
        },
    }
    record = {
        "schema": receipt_module.RECENSOR_PARTITION_RECEIPT_SCHEMA,
        "run_id": "r0-receipt-versioning-probe",
        "config_digest": "2" * 64,
        "scope": receipt_module.RECENSOR_PARTITION_RECEIPT_SCOPE,
        "proposal_seal_ref": proposal_seal_ref,
        "expected_act_count": 1,
        "items": [item],
        "by_partition_class": {"completed": 1, "unresolved": 0, "failed": 0},
        "recensor_status": "complete",
        "reasons": [],
    }
    from common.contracts.canonical import self_hash

    record["self_hash"] = self_hash(record)

    try:
        receipt_module.validate_recensor_partition_receipt(record)
    except SchemaRefusal as error:
        assert "v1" in str(error).lower() or "version" in str(error).lower(), (
            "the record was refused, but not by a named version-mismatch check: "
            f"{error!r}. D9 asks for the OLD v1 constant to be refused as a version "
            "mismatch once v2 exists, not merely rejected by an unrelated closed-"
            "schema check on the coverage record it happens to also carry"
        )
        return
    pytest.fail(
        "a receipt that declares the v1 schema label while carrying page-"
        "granularity coverage facts was accepted; D9 requires this to be refused "
        "with a named version-mismatch error"
    )
