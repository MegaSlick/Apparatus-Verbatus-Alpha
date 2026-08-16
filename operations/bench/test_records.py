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
