from __future__ import annotations

from copy import deepcopy

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

_V1_DEFINITION_DIGESTS = {
    "B0": "3901353b314037d586dccb879cf8fb91c252b6a4fa187d52dc1a1b26dcbcc0b9",
    "B0.5": "ee528ec4953e868de636fb4ee8aa71b3133182e9a902f027c92e9ce640eea640",
    "B2": "0fe63faad754b66236697ff3c59da4c948fb67879e8369639249ff9e77f628f8",
    "B3": "7372cc62e8d80f6b117c55baf7c66f0cf4fc499fcfc93d7fabaa726b1fb8daf5",
    "B4": "1eea90fbefabfd12d35313775a3c954433ae08570c73c62c5e145df2bae18968",
    "B5": "b6c02773684e724a8e1081fd3ddd4e862a7a55342f787bf0ef30be0475d012f4",
    "B5a": "492b611bc8d02ea9a0e7da99f69a2f7c3117f133fe7f30464d58ac5c88011855",
    "B6": "02f886ef4b6d9c0b1e05b9aeec62a7887e49ad9225abdf3b1989227a7b26850a",
}


def test_each_r7b_cell_has_one_sealed_predeclared_definition():
    cells = [record["cell"] for record in all_definitions()]
    assert cells == ["B0", "B0.5", "B2", "B3", "B4", "B5", "B5a", "B6"]
    assert all(validate_definition(record) == record for record in all_definitions())


def test_v1_definition_digests_are_immutable_until_the_version_moves():
    actual = {record["cell"]: record["definition_digest"] for record in all_definitions()}

    assert actual == _V1_DEFINITION_DIGESTS, (
        "an R7b v1 definition changed without moving records.VERSION; change the version "
        "before re-pinning the deliberately new definitions"
    )


def test_mutating_returned_measures_cannot_change_the_sealed_definition():
    pristine = deepcopy(definition("B0.5"))
    returned = definition("B0.5")

    returned["measures"][0]["name"] = "caller-mutated-measure"

    assert definition("B0.5") == pristine


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


def test_a_non_string_cell_refuses_by_name_instead_of_crashing_the_validator():
    """Both validators take `cell` from a record they have not vouched for yet.

    An unhashable one made `cell not in _MEASURES` raise `TypeError`, so the one
    boundary whose job is to produce a named refusal produced a traceback
    instead.
    """
    record = definition("B2")
    record["cell"] = ["B2"]
    with pytest.raises(SchemaRefusal, match="unknown R7b bench cell"):
        validate_definition(record)

    result = not_run("B2", fixture_verified=True)
    result["cell"] = {"B2": True}
    with pytest.raises(SchemaRefusal, match="unknown R7b bench cell"):
        validate_result(result)


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


def test_rehashed_not_run_result_cannot_carry_observations():
    forged = not_run("B2", fixture_verified=True)
    forged["observations"] = [{"measure": "forged execution"}]
    forged["self_hash"] = self_hash(forged)

    with pytest.raises(SchemaRefusal, match="not-run bench result has no observations"):
        validate_result(forged)


def test_rehashed_not_run_result_requires_a_boolean_fixture_marker():
    forged = not_run("B2", fixture_verified=True)
    forged["fixture_verified"] = "verified"
    forged["self_hash"] = self_hash(forged)

    with pytest.raises(SchemaRefusal, match="boolean fixture marker"):
        validate_result(forged)


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
