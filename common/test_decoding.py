from __future__ import annotations

import re
from pathlib import Path

import pytest

from common.chandra_native_retry import recipe_record
from common.contracts.errors import ContractError
from common.decoding import (
    DEFAULT_DECODING_CONFIG_PATH,
    READING_CHAIRS,
    chair_decoding,
    load_decoding_policy,
    perlector_max_tokens,
    structure_recovery_policy,
)

# The makers' recommendations, typed here from their sources rather than read
# back from the file under test, so a drifted row fails by value.
MAKERS_SAMPLING = {
    # datalab-to/chandra@d4f7467, chandra/model/vllm.py::generate_vllm defaults.
    "designator_structure": {"temperature": 0.0, "top_p": 0.1},
    "attestator_1": {"temperature": 0.0, "top_p": 0.1},
    # Teklia DAI generation_config.json @ e371095.
    "attestator_2": {"temperature": 0.1, "top_k": 1, "top_p": 0.001, "repetition_penalty": 1.05},
    # stanford-oval/churro-3B generation_config.json @ ca2150e.
    "attestator_3": {"temperature": 1e-06, "repetition_penalty": 1.05},
    # Qwen/Qwen3.8-27B model card @ 1d4bf0f, non-thinking mode.
    "perlector": {
        "temperature": 0.7,
        "top_p": 0.8,
        "top_k": 20,
        "min_p": 0.0,
        "presence_penalty": 1.5,
        "repetition_penalty": 1.0,
    },
}


def test_each_reading_chair_carries_its_makers_sampling_and_its_source():
    policy, _digest = load_decoding_policy()
    assert set(policy["chair_decoding"]) == READING_CHAIRS == set(MAKERS_SAMPLING)
    for chair, expected in MAKERS_SAMPLING.items():
        assert chair_decoding(policy, chair) == expected, chair
        row = policy["chair_decoding"][chair]
        assert row["revision"] in row["source"], chair
        assert row["verification"].strip(), chair


def test_a_chair_without_a_row_is_refused_by_name():
    policy, _digest = load_decoding_policy()
    with pytest.raises(ContractError, match="no row for chair 'attestator_9'"):
        chair_decoding(policy, "attestator_9")


def test_the_chandra_chairs_must_keep_the_pinned_recipes_first_request(tmp_path: Path):
    source = DEFAULT_DECODING_CONFIG_PATH.read_text(encoding="utf-8")
    moved = source.replace(
        "[chair_decoding.designator_structure]\n# datalab-to/chandra-ocr-2, read the way Chandra's own page pipeline reads a\n# page: `generate_vllm`'s defaults for the first request.\ntemperature = 0.0",
        "[chair_decoding.designator_structure]\ntemperature = 1",
        1,
    )
    assert moved != source
    path = tmp_path / "decoding.toml"
    path.write_text(moved, encoding="utf-8")
    with pytest.raises(ContractError, match="first request of the pinned Chandra recipe"):
        load_decoding_policy(path)


def test_a_sampling_value_change_moves_the_decoding_digest(tmp_path: Path):
    source = DEFAULT_DECODING_CONFIG_PATH.read_text(encoding="utf-8")
    path = tmp_path / "decoding.toml"
    path.write_text(source.replace("presence_penalty = 1.5", "presence_penalty = 1.0", 1))
    moved_policy, moved_digest = load_decoding_policy(path)
    policy, digest = load_decoding_policy()
    assert moved_digest != digest
    assert chair_decoding(moved_policy, "perlector")["presence_penalty"] == 1.0
    assert chair_decoding(policy, "perlector")["presence_penalty"] == 1.5


def test_shipped_decoding_policy_declares_its_sections_and_variance_shape():
    policy, digest = load_decoding_policy()
    assert policy["variance_experiment"] == {
        "label": "variance.v1",
        "seed": 20260820,
        "passes": 2,
    }
    assert policy["schema"] == "decoding.v5"
    assert policy["perlector_generation"] == {
        "reading_max_tokens": 4096,
        "reproof_max_tokens": 8192,
    }
    assert perlector_max_tokens(policy) == (4096, 8192)
    assert policy["chandra_native_inference"] == recipe_record()
    assert policy["structure"] == {
        "recovery_seed_schedule": "base-plus-attempt-ordinal-minus-one",
        "recovery_max_attempts": 3,
    }
    assert structure_recovery_policy(policy) == {
        "max_attempts": 3,
        "seed_schedule": "base-plus-attempt-ordinal-minus-one",
    }
    assert len(digest) == 64


@pytest.mark.parametrize("schema", ["decoding.v1", "decoding.v2", "decoding.v3", "decoding.v4"])
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


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("passes = 2", "passes = 1", "at least 2"),
        ("[structure]", "[missing_structure]", "wrong closed schema"),
        ("[structure]\n", "[structure]\ntemperature = 1\n", "only its coverage recovery"),
        ("temperature = 0.7", "temperature = -0.7", "finite number, non-negative"),
        ("temperature = 0.7", "temperature = true", "finite number, non-negative"),
        ("temperature = 0.7", 'temperature = "0.7"', "finite number, non-negative"),
        ("top_k = 20", "top_k = 20.0", "an integer for top_k"),
        ("temperature = 0.7", "temperature = nan", "NaN or infinity"),
        ("temperature = 0.7", "temperature = inf", "NaN or infinity"),
        ("[chair_decoding.perlector]\n", "[chair_decoding.perlector]\nseed = 1\n", "unknown field"),
        ("[chair_decoding.perlector]\n", "[chair_decoding.reader]\n", "one row per reading chair"),
        (
            'verification = "read by the project lead"\n',
            'verification = " "\n',
            "nonblank text",
        ),
    ],
)
def test_shipped_policy_refuses_invalid_postures(tmp_path, old, new, message):
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
    ["0", "-1", "4096.0", '"4096"', "true"],
    ids=["zero", "negative", "float", "str", "bool"],
)
@pytest.mark.parametrize("field", ["reading_max_tokens", "reproof_max_tokens"])
def test_a_perlector_output_bound_that_is_not_a_positive_integer_is_refused(
    tmp_path: Path, field: str, bound: str
):
    source = DEFAULT_DECODING_CONFIG_PATH.read_text(encoding="utf-8")
    path = tmp_path / "decoding.toml"
    path.write_text(
        re.sub(rf"^{field} = \d+$", f"{field} = {bound}", source, count=1, flags=re.M),
        encoding="utf-8",
    )

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
