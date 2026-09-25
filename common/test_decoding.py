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


def test_legacy_decoding_v1_preserves_one_fixed_base_attempt(tmp_path: Path):
    path = tmp_path / "legacy.toml"
    path.write_text(
        'schema = "decoding.v1"\n[reading_of_record]\ntemperature = 0\n'
        '[variance_experiment]\nlabel = "v"\nseed = 1\npasses = 2\n'
        "[structure]\ntemperature = 0\n",
        encoding="utf-8",
    )
    policy, _digest = load_decoding_policy(path)
    assert structure_recovery_policy(policy) == {
        "max_attempts": 1,
        "seed_schedule": "fixed-base",
    }


def test_legacy_decoding_v2_keeps_structure_recovery_without_native_retry(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-v2.toml"
    path.write_text(
        'schema = "decoding.v2"\n[reading_of_record]\ntemperature = 0\n'
        '[variance_experiment]\nlabel = "v"\nseed = 1\npasses = 2\n'
        "[structure]\ntemperature = 1\n"
        'recovery_seed_schedule = "base-plus-attempt-ordinal-minus-one"\n'
        "recovery_max_attempts = 3\n",
        encoding="utf-8",
    )
    policy, _digest = load_decoding_policy(path)
    assert "chandra_native_inference" not in policy
    assert structure_recovery_policy(policy) == {
        "max_attempts": 3,
        "seed_schedule": "base-plus-attempt-ordinal-minus-one",
    }


_STRUCTURE = "[structure]\ntemperature = 0\n"


@pytest.mark.parametrize(
    "body, message",
    [
        (
            'schema = "decoding.v1"\n[reading_of_record]\ntemperature = 1\n'
            '[variance_experiment]\nlabel = "v"\nseed = 1\npasses = 2\n' + _STRUCTURE,
            "temperature 0",
        ),
        (
            'schema = "decoding.v1"\n[reading_of_record]\ntemperature = false\n'
            '[variance_experiment]\nlabel = "v"\nseed = 1\npasses = 2\n' + _STRUCTURE,
            "temperature 0",
        ),
        (
            'schema = "decoding.v1"\n[reading_of_record]\ntemperature = 0\n'
            '[variance_experiment]\nlabel = "v"\nseed = 1\npasses = 1\n' + _STRUCTURE,
            "at least 2",
        ),
        # A file with no structure section is not the closed schema: the
        # structural seal names the `structure` posture over these bytes, and
        # bytes with no such section cannot carry that name honestly.
        (
            'schema = "decoding.v1"\n[reading_of_record]\ntemperature = 0\n'
            '[variance_experiment]\nlabel = "v"\nseed = 1\npasses = 2\n',
            "wrong closed schema",
        ),
        (
            'schema = "decoding.v1"\n[reading_of_record]\ntemperature = 0\n'
            '[variance_experiment]\nlabel = "v"\nseed = 1\npasses = 2\n'
            "[structure]\ntemperature = -1\n",
            "structure must declare",
        ),
        (
            'schema = "decoding.v1"\n[reading_of_record]\ntemperature = 0\n'
            '[variance_experiment]\nlabel = "v"\nseed = 1\npasses = 2\n'
            "[structure]\ntemperature = true\n",
            "structure must declare",
        ),
        (
            'schema = "decoding.v1"\n[reading_of_record]\ntemperature = 0\n'
            '[variance_experiment]\nlabel = "v"\nseed = 1\npasses = 2\n'
            "[structure]\ntemperature = 0\nseed = 4\n",
            "structure must declare",
        ),
        # TOML spells both of these as ordinary floats, and neither is caught by
        # the non-negative test: `nan < 0` and `inf < 0` are both False. Only
        # the loader's `math.isfinite` clause refuses them, so these two rows
        # are what makes removing that clause fail a test rather than pass one.
        (
            'schema = "decoding.v1"\n[reading_of_record]\ntemperature = 0\n'
            '[variance_experiment]\nlabel = "v"\nseed = 1\npasses = 2\n'
            "[structure]\ntemperature = nan\n",
            "structure must declare",
        ),
        (
            'schema = "decoding.v1"\n[reading_of_record]\ntemperature = 0\n'
            '[variance_experiment]\nlabel = "v"\nseed = 1\npasses = 2\n'
            "[structure]\ntemperature = inf\n",
            "structure must declare",
        ),
    ],
)
def test_decoding_policy_refuses_a_non_record_posture_or_retry_shaped_experiment(
    tmp_path: Path, body: str, message: str
):
    path = tmp_path / "decoding.toml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ContractError, match=message) as refusal:
        load_decoding_policy(path)
    assert "No run or stage artifact was written" in str(refusal.value)
    assert "Restore or correct the decoding file and retry" in str(refusal.value)


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
        (b"\xff", "not UTF-8"),
        (b'schema = "decoding.v1"\n[', "not valid TOML"),
    ],
)
def test_decoding_policy_parse_refusals_name_the_actual_cause(tmp_path, body, message):
    path = tmp_path / "decoding.toml"
    path.write_bytes(body)

    with pytest.raises(ContractError, match=message) as refusal:
        load_decoding_policy(path)
    assert "No run or stage artifact was written" in str(refusal.value)
    assert "Restore or correct the decoding file and retry" in str(refusal.value)
