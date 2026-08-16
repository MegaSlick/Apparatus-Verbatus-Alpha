from __future__ import annotations

import pytest

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
    record = not_run("B0", fixture_verified=True)
    record["definition_digest"] = definition("B0.5")["definition_digest"]
    with pytest.raises(SchemaRefusal, match="frozen definition"):
        validate_result(record)


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
    forged["observations"] = [{"correct_feeding_rate_permille": 990}]
    # self_hash intentionally left stale: a forger who also recomputes it is
    # still caught by the explicit not-run state check.
    with pytest.raises(SchemaRefusal, match="not-run state"):
        validate_result(forged)

    tampered = not_run("B2", fixture_verified=True)
    tampered["fixture_verified"] = False
    with pytest.raises(SchemaRefusal, match="self-hash"):
        validate_result(tampered)
