from __future__ import annotations

import pytest

from common.contracts.canonical import self_hash
from common.contracts.errors import SchemaRefusal
from operations.bench.records import (
    all_definitions,
    definition,
    not_run,
    validate_definition,
    validate_result,
)
from operations.bench.runner import exercise_synthetic_definitions


def test_each_r7b_cell_has_one_sealed_predeclared_definition():
    cells = [record["cell"] for record in all_definitions()]
    assert cells == ["B0", "B0.5", "B2", "B3", "B4", "B5", "B5a", "B6"]
    assert all(validate_definition(record) == record for record in all_definitions())


def test_fixture_exercise_leaves_real_cells_visibly_not_run_not_green():
    results = exercise_synthetic_definitions()
    assert [result["cell"] for result in results] == [
        "B0",
        "B0.5",
        "B2",
        "B3",
        "B4",
        "B5",
        "B5a",
        "B6",
    ]
    assert all(result["state"] == "not-run" and result["fixture_verified"] for result in results)
    assert all(validate_result(result) == result for result in results)


def test_result_cannot_move_its_own_goalposts_or_claim_observations():
    # Re-keyed *and* resealed, which is the binding's real threat model: a
    # goalpost swap that leaves a stale self-hash behind is refused by the
    # self-hash whether the binding is checked or not, so it cannot show this
    # guard is load-bearing. B2 and B3 share an execution requirement, so their
    # sealed reasons match too, and this record is well-formed in every respect
    # except the definition it names.
    resealed = not_run("B2", fixture_verified=True)
    resealed["definition_digest"] = definition("B3")["definition_digest"]
    resealed["self_hash"] = self_hash(resealed)
    with pytest.raises(SchemaRefusal, match="frozen definition"):
        validate_result(resealed)

    stale = not_run("B0", fixture_verified=True)
    stale["definition_digest"] = definition("B0.5")["definition_digest"]
    with pytest.raises(SchemaRefusal, match="frozen definition"):
        validate_result(stale)


def test_not_run_reason_names_the_actual_blocker_per_cell():
    pod_cells = {"B0", "B0.5"}
    for cell in (record["cell"] for record in all_definitions()):
        reason = not_run(cell, fixture_verified=True)["reason"]
        if cell in pod_cells:
            assert "authorized pod session" in reason
        else:
            assert "authorized pod session" not in reason
            assert "runner execution against gold" in reason


def test_forged_measured_result_and_tampered_row_are_both_refused():
    forged = not_run("B0", fixture_verified=True)
    forged["state"] = "measured"
    forged["self_hash"] = self_hash(forged)
    with pytest.raises(SchemaRefusal, match="not-run state"):
        validate_result(forged)

    tampered = not_run("B2", fixture_verified=True)
    tampered["fixture_verified"] = False
    with pytest.raises(SchemaRefusal, match="self-hash"):
        validate_result(tampered)


def test_rehashed_reason_and_rehashed_extra_fields_do_not_forge_sealed_records():
    result = not_run("B2", fixture_verified=True)
    result["reason"] = "operator chose not to run this cell"
    result["self_hash"] = self_hash(result)
    with pytest.raises(SchemaRefusal, match="sealed execution blocker"):
        validate_result(result)

    result = not_run("B2", fixture_verified=True)
    result["claimed_pass"] = True
    result["self_hash"] = self_hash(result)
    with pytest.raises(SchemaRefusal, match="wrong closed schema"):
        validate_result(result)

    frozen = definition("B2")
    frozen["threshold"] = "forged after output"
    frozen["self_hash"] = self_hash(frozen)
    with pytest.raises(SchemaRefusal, match="wrong closed schema"):
        validate_definition(frozen)
