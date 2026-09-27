from __future__ import annotations

from pathlib import Path

import pytest

from common.chandra_native_retry import recipe_record
from common.contracts.errors import ContractError
from common.decoding import (
    load_decoding_policy,
    structure_recovery_policy,
)


def test_shipped_decoding_policy_declares_a_zero_temperature_record_and_variance_shape():
    policy, digest = load_decoding_policy()
    assert policy["reading_of_record"] == {"temperature": 0}
    assert policy["variance_experiment"] == {
        "label": "variance.v1",
        "seed": 20260820,
        "passes": 2,
    }
    assert policy["schema"] == "decoding.v3"
    assert policy["chandra_native_inference"] == recipe_record()
    assert policy["structure"] == {
        "temperature": 1,
        "recovery_seed_schedule": "base-plus-attempt-ordinal-minus-one",
        "recovery_max_attempts": 3,
    }
    assert structure_recovery_policy(policy) == {
        "max_attempts": 3,
        "seed_schedule": "base-plus-attempt-ordinal-minus-one",
    }
    assert len(digest) == 64


@pytest.mark.parametrize("schema", ["decoding.v1", "decoding.v2"])
def test_legacy_decoding_schema_is_refused_by_name(tmp_path: Path, schema: str):
    path = tmp_path / "decoding.toml"
    path.write_text(f'schema = "{schema}"\n', encoding="utf-8")

    with pytest.raises(
        ContractError,
        match=f"sealed under {schema}, which this build no longer reads; re-run",
    ):
        load_decoding_policy(path)


def test_a_nonzero_reading_temperature_is_refused():
    policy, _digest = load_decoding_policy()
    policy["reading_of_record"]["temperature"] = 1

    with pytest.raises(ContractError, match="reading_of_record must declare temperature 0"):
        structure_recovery_policy(policy)


@pytest.mark.parametrize(
    "change",
    [
        {"label": "", "seed": 20260820, "passes": 2},
        {"label": "variance.v1", "seed": True, "passes": 2},
        {"label": "variance.v1", "seed": -1, "passes": 2},
        {"label": "variance.v1", "seed": 20260820, "passes": True},
    ],
)
def test_a_malformed_variance_experiment_is_refused(change):
    policy, _digest = load_decoding_policy()
    policy["variance_experiment"] = change

    with pytest.raises(ContractError, match="decoding variance_experiment"):
        structure_recovery_policy(policy)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"\xff", "not valid UTF-8"),
        (b'schema = "decoding.v3"\n[', "not valid TOML"),
    ],
)
def test_decoding_policy_parse_refusals_name_the_actual_cause(tmp_path, body, message):
    path = tmp_path / "decoding.toml"
    path.write_bytes(body)

    with pytest.raises(ContractError, match=message) as refusal:
        load_decoding_policy(path)
    assert "No run or stage artifact was written" in str(refusal.value)
    assert "Restore or correct the decoding file and retry" in str(refusal.value)
