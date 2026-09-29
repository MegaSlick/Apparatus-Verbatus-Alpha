from __future__ import annotations

from pathlib import Path

import pytest

from common.chandra_native_retry import recipe_record
from common.contracts.errors import ContractError
from common.decoding import (
    DEFAULT_DECODING_CONFIG_PATH,
    load_decoding_policy,
    perlector_max_tokens,
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
    assert policy["schema"] == "decoding.v4"
    assert policy["perlector_generation"] == {
        "reading_max_tokens": 2048,
        "reproof_max_tokens": 2048,
    }
    assert perlector_max_tokens(policy) == (2048, 2048)
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


@pytest.mark.parametrize("schema", ["decoding.v1", "decoding.v2", "decoding.v3"])
def test_legacy_decoding_schema_is_refused_by_name(tmp_path: Path, schema: str):
    path = tmp_path / "decoding.toml"
    path.write_text(f'schema = "{schema}"\n', encoding="utf-8")

    with pytest.raises(
        ContractError,
        match=f"sealed under {schema}, which this build no longer reads; re-run",
    ):
        load_decoding_policy(path)


def test_non_string_decoding_schema_gets_a_named_refusal(tmp_path: Path):
    path = tmp_path / "decoding.toml"
    path.write_text("schema = []\n", encoding="utf-8")

    with pytest.raises(
        ContractError, match="decoding configuration has an unsupported schema"
    ) as refusal:
        load_decoding_policy(path)
    assert "Restore or correct the decoding file and retry" in str(refusal.value)


def test_a_nonzero_reading_temperature_is_refused():
    policy, _digest = load_decoding_policy()
    policy["reading_of_record"]["temperature"] = 1

    with pytest.raises(ContractError, match="reading_of_record must declare temperature 0"):
        structure_recovery_policy(policy)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (
            "[reading_of_record]\ntemperature = 0",
            "[reading_of_record]\ntemperature = false",
            "temperature 0",
        ),
        ("passes = 2", "passes = 1", "at least 2"),
        ("[structure]", "[missing_structure]", "wrong closed schema"),
        ("temperature = 1", "temperature = -1", "structure must declare"),
        ("temperature = 1", "temperature = true", "structure must declare"),
        ("[structure]\n", "[structure]\nextra = 1\n", "structure must declare"),
        ("temperature = 1", "temperature = nan", "NaN or infinity"),
        ("temperature = 1", "temperature = inf", "NaN or infinity"),
    ],
)
def test_shipped_v3_policy_refuses_invalid_postures(tmp_path, old, new, message):
    source = DEFAULT_DECODING_CONFIG_PATH.read_text(encoding="utf-8")
    assert old in source
    source = source.replace(old, new, 1)
    path = tmp_path / "decoding.toml"
    path.write_text(source, encoding="utf-8")
    with pytest.raises(ContractError, match=message):
        load_decoding_policy(path)


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


@pytest.mark.parametrize(
    "bound",
    ["0", "-1", "2048.0", '"2048"', "true"],
    ids=["zero", "negative", "float", "str", "bool"],
)
@pytest.mark.parametrize("field", ["reading_max_tokens", "reproof_max_tokens"])
def test_a_perlector_output_bound_that_is_not_a_positive_integer_is_refused(
    tmp_path: Path, field: str, bound: str
):
    source = DEFAULT_DECODING_CONFIG_PATH.read_text(encoding="utf-8")
    path = tmp_path / "decoding.toml"
    path.write_text(source.replace(f"{field} = 2048", f"{field} = {bound}", 1), encoding="utf-8")

    with pytest.raises(ContractError, match="perlector_generation must declare positive integer"):
        load_decoding_policy(path)


def test_a_missing_perlector_generation_section_is_refused(tmp_path: Path):
    source = DEFAULT_DECODING_CONFIG_PATH.read_text(encoding="utf-8")
    before, _section, after = source.partition("[perlector_generation]")
    path = tmp_path / "decoding.toml"
    path.write_text(
        before + "[variance_experiment]" + after.partition("[variance_experiment]")[2],
        encoding="utf-8",
    )

    with pytest.raises(ContractError, match="wrong closed schema"):
        load_decoding_policy(path)
