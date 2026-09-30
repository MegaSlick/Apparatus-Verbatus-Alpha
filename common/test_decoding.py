from __future__ import annotations

import re
from pathlib import Path

import pytest

from common.chandra_native_retry import recipe_record
from common.contracts.errors import ContractError, SchemaRefusal
from common.contracts.serving import RETIRED_CALL_RECORD_SCHEMAS
from common.decoding import (
    DEFAULT_DECODING_CONFIG_PATH,
    ENGINE_FILLED_SAMPLING_FIELDS,
    READING_CHAIRS,
    VARIANCE_ARMS,
    chair_attempt_decoding,
    chair_decoding,
    engine_effective_sampling,
    load_decoding_policy,
    perlector_max_tokens,
    recorded_sampling,
    refuse_retired_call_record,
    structure_recovery_policy,
    variance_arm_seed,
    verify_call_sampling,
)

# The makers' recommendations, typed here from their sources rather than read
# back from the file under test, so a drifted row fails by value. A field the
# maker leaves unset is at the value the maker's own pipeline runs under.
_VLLM_UNSENT = {"top_k": 0, "min_p": 0.0, "repetition_penalty": 1.0}
MAKERS_SAMPLING = {
    # datalab-to/chandra@d4f7467, chandra/model/vllm.py::generate_vllm defaults,
    # sent to a vLLM server; the rest are vllm==0.27.1's request defaults.
    "designator_structure": {"temperature": 0.0, "top_p": 0.1, **_VLLM_UNSENT},
    "attestator_1": {"temperature": 0.0, "top_p": 0.1, **_VLLM_UNSENT},
    # Teklia DAI generation_config.json @ e371095; min_p unset in transformers.
    "attestator_2": {
        "temperature": 0.1,
        "top_k": 1,
        "top_p": 0.001,
        "repetition_penalty": 1.05,
        "min_p": 0.0,
    },
    # stanford-oval/churro-3B generation_config.json @ ca2150e; the rest are
    # transformers v5.2.0 GenerationConfig defaults.
    "attestator_3": {
        "temperature": 1e-06,
        "repetition_penalty": 1.05,
        "top_k": 50,
        "top_p": 1.0,
        "min_p": 0.0,
    },
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


def test_every_row_names_every_field_the_engine_would_otherwise_fill():
    """vllm==0.27.1's `get_diff_sampling_param` fills these from a generation config."""
    assert ENGINE_FILLED_SAMPLING_FIELDS == {
        "temperature",
        "top_p",
        "top_k",
        "min_p",
        "repetition_penalty",
    }
    policy, _digest = load_decoding_policy()
    for chair in READING_CHAIRS:
        assert ENGINE_FILLED_SAMPLING_FIELDS <= set(chair_decoding(policy, chair)), chair


@pytest.mark.parametrize("field", sorted(ENGINE_FILLED_SAMPLING_FIELDS))
def test_a_row_that_leaves_a_fillable_field_to_the_engine_is_refused(field):
    policy, _digest = load_decoding_policy()
    del policy["chair_decoding"]["attestator_3"][field]
    with pytest.raises(ContractError, match=rf"attestator_3 must state \['{field}'\]"):
        chair_decoding(policy, "attestator_3")


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
        "[chair_decoding.designator_structure]\n# datalab-to/chandra-ocr-2, read the way Chandra's own page pipeline reads a\n# page: `generate_vllm` sends a vLLM server only temperature and top_p.\ntemperature = 0.0",
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
    assert policy["variance_experiment"] == {"seed": 20260820}
    assert policy["schema"] == "decoding.v5"
    assert policy["perlector_generation"] == {
        "reading_max_tokens": 4096,
        "reproof_max_tokens": 8192,
    }
    assert perlector_max_tokens(policy) == (4096, 8192)
    assert policy["chandra_native_inference"] == recipe_record()
    assert policy["structure"] == {
        "recovery_schedule": "chandra-native-retry",
        "recovery_max_attempts": 3,
    }
    assert structure_recovery_policy(policy) == {
        "max_attempts": 3,
        "sampling_schedule": "chandra-native-retry",
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
        ("seed = 20260820", "seed = 20260820\npasses = 2", "wrong closed schema"),
        ("[structure]", "[missing_structure]", "wrong closed schema"),
        ("[structure]\n", "[structure]\ntemperature = 1\n", "only its coverage recovery"),
        (
            'recovery_schedule = "chandra-native-retry"',
            'recovery_schedule = "base-plus-attempt-ordinal-minus-one"',
            "'chandra-native-retry' schedule",
        ),
        (
            "temperature = 0.7",
            "temperature = -0.7",
            r"temperature must be a finite number in \[0.0, 2.0\]",
        ),
        ("temperature = 0.7", "temperature = 2.5", r"in \[0.0, 2.0\]"),
        ("temperature = 0.7", "temperature = true", "must be a finite number"),
        ("temperature = 0.7", 'temperature = "0.7"', "must be a finite number"),
        ("temperature = 0.7\n", "", r"must state \['temperature'\]"),
        ("top_p = 0.8", "top_p = 0.0", r"top_p must be a finite number in \(0.0, 1.0\]"),
        ("top_p = 0.8", "top_p = 1.5", r"top_p must be a finite number in \(0.0, 1.0\]"),
        ("min_p = 0.0", "min_p = 1.5", r"min_p must be a finite number in \[0.0, 1.0\]"),
        ("min_p = 0.0", "min_p = -0.1", r"min_p must be a finite number in \[0.0, 1.0\]"),
        (
            "presence_penalty = 1.5",
            "presence_penalty = 2.5",
            r"presence_penalty must be a finite number in \[-2.0, 2.0\]",
        ),
        (
            "repetition_penalty = 1.0\n",
            "repetition_penalty = 0.0\n",
            r"repetition_penalty must be a finite number in \(0.0, inf\]",
        ),
        ("top_k = 20", "top_k = 20.0", "and an integer"),
        ("top_k = 20", "top_k = -1", "top_k must be a finite number"),
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
        {"seed": True},
        {"seed": -1},
        {"seed": 2**63 - 1},
        {"seed": 20260820, "label": "variance.v1"},
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


# --- per-attempt, per-arm and engine-effective sampling -----------------------


def test_the_structure_chair_recovers_along_chandras_own_retry_schedule():
    policy, _digest = load_decoding_policy()
    assert [chair_attempt_decoding(policy, "designator_structure", n) for n in (1, 2, 3)] == [
        {"temperature": 0.0, "top_p": 0.1, **_VLLM_UNSENT},
        {"temperature": 0.2, "top_p": 0.95, **_VLLM_UNSENT},
        {"temperature": 0.4, "top_p": 0.95, **_VLLM_UNSENT},
    ]
    with pytest.raises(ContractError, match="no attempt 4"):
        chair_attempt_decoding(policy, "designator_structure", 4)
    assert chair_attempt_decoding(policy, "attestator_1", 7) == {
        "temperature": 0.8,
        "top_p": 0.95,
        **_VLLM_UNSENT,
    }
    with pytest.raises(ContractError, match="only the Chandra chairs retry"):
        chair_attempt_decoding(policy, "perlector", 2)
    with pytest.raises(ContractError, match="positive integer"):
        chair_attempt_decoding(policy, "designator_structure", 0)


def test_each_variance_arm_draws_under_its_own_seed():
    policy, _digest = load_decoding_policy()
    seeds = [variance_arm_seed(policy, arm) for arm in VARIANCE_ARMS]
    assert seeds == [20260820, 20260821]
    with pytest.raises(ContractError, match="not an arm"):
        variance_arm_seed(policy, "perlectio")


@pytest.mark.parametrize(
    ("sent", "effective"),
    [
        # Churro: raised to vLLM's _MAX_TEMP, not greedy.
        (
            {"temperature": 1e-06, "repetition_penalty": 1.05},
            {"temperature": 0.01, "repetition_penalty": 1.05},
        ),
        # Chandra: greedy, so the engine sets top_p to 1.
        ({"temperature": 0.0, "top_p": 0.1}, {"temperature": 0.0, "top_p": 1.0}),
        (
            {"temperature": 0, "top_p": 0.5, "top_k": 5, "min_p": 0.2},
            {"temperature": 0, "top_p": 1.0, "top_k": 0, "min_p": 0.0},
        ),
        # At or above 0.01 nothing moves.
        ({"temperature": 0.01, "top_p": 0.1}, {"temperature": 0.01, "top_p": 0.1}),
        (
            {"temperature": 0.1, "top_k": 1, "top_p": 0.001},
            {"temperature": 0.1, "top_k": 1, "top_p": 0.001},
        ),
    ],
)
def test_the_engine_effective_mapping_is_vllm_0_27_1s(sent, effective):
    assert engine_effective_sampling(sent) == effective


def test_the_engine_effective_mapping_agrees_with_the_pinned_engine_when_installed():
    """Run where the pod's `vllm==0.27.1` is installed; the unit cases above hold offline."""
    sampling_params = pytest.importorskip("vllm.sampling_params")
    import vllm

    assert vllm.__version__ == "0.27.1"
    policy, _digest = load_decoding_policy()
    for chair in sorted(READING_CHAIRS):
        sent = chair_decoding(policy, chair)
        params = sampling_params.SamplingParams(**sent)
        assert {field: getattr(params, field) for field in sent} == engine_effective_sampling(sent)


def _call(chair: str, attempt: int = 1, seed: int | None = 7) -> dict:
    policy, _digest = load_decoding_policy()
    sampling = chair_attempt_decoding(policy, chair, attempt)
    sent = {**recorded_sampling(sampling), "max_tokens": 10}
    if seed is not None:
        sent["seed"] = seed
    return {
        "schema": "chair-call-record.v3",
        "generation_sent": sent,
        "sampling_effective": recorded_sampling(engine_effective_sampling(sampling)),
    }


@pytest.mark.parametrize("chair", sorted(READING_CHAIRS))
def test_a_call_record_at_its_sealed_row_verifies(chair):
    policy, _digest = load_decoding_policy()
    verify_call_sampling(_call(chair), policy, chair, expected_seed=7)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda call: call["generation_sent"].pop("temperature"), "not the sealed"),
        (lambda call: call["generation_sent"].update(top_k=5), "not the sealed"),
        (
            lambda call: call["generation_sent"].update(
                temperature=recorded_sampling({"t": 0.2})["t"]
            ),
            "not the sealed",
        ),
        (lambda call: call.pop("sampling_effective"), "sampling_effective"),
        (lambda call: call.update(generation_sent=None), "no generation_sent"),
        (lambda call: call["generation_sent"].update(seed=8), "sent seed 8, not 7"),
        (lambda call: call["generation_sent"].pop("seed"), "sent seed None, not 7"),
        (lambda call: call["generation_sent"].update(seed=True), "sent seed True"),
        (lambda call: call["generation_sent"].update(n=2), r"generation field\(s\) \['n'\]"),
        (
            lambda call: call["generation_sent"].update(model="x", stream=False),
            r"\['model', 'stream'\]",
        ),
    ],
)
def test_a_call_record_off_its_sealed_row_is_refused(mutate, message):
    policy, _digest = load_decoding_policy()
    call = _call("attestator_3")
    mutate(call)
    with pytest.raises(ContractError, match=message):
        verify_call_sampling(call, policy, "attestator_3", expected_seed=7)


def test_a_caller_generation_field_is_admitted_on_the_call_record():
    policy, _digest = load_decoding_policy()
    call = _call("attestator_2")
    call["generation_sent"].update(
        stop_token_ids=[151643], chat_template_kwargs={"enable_thinking": False}
    )
    verify_call_sampling(call, policy, "attestator_2", expected_seed=7)


def test_a_structure_attempt_call_is_verified_against_its_own_attempt():
    policy, _digest = load_decoding_policy()
    call = _call("designator_structure", 2)
    verify_call_sampling(call, policy, "designator_structure", attempt_ordinal=2, expected_seed=7)
    with pytest.raises(ContractError, match="for attempt 1"):
        verify_call_sampling(call, policy, "designator_structure", expected_seed=7)


def test_a_request_that_sends_no_seed_is_stated_and_held_to_it():
    """A Chandra native request sends no seed; its reader says so with `None`."""
    policy, _digest = load_decoding_policy()
    unseeded = _call("attestator_1", 4, seed=None)
    verify_call_sampling(unseeded, policy, "attestator_1", attempt_ordinal=4, expected_seed=None)
    with pytest.raises(ContractError, match="sent a seed its request sends none of"):
        verify_call_sampling(
            _call("attestator_1", 4), policy, "attestator_1", attempt_ordinal=4, expected_seed=None
        )
    with pytest.raises(ContractError, match="sent seed None"):
        verify_call_sampling(unseeded, policy, "attestator_1", attempt_ordinal=4, expected_seed=7)


def test_each_variance_arm_call_is_held_to_its_own_seed():
    policy, _digest = load_decoding_policy()
    prior, nuda = (variance_arm_seed(policy, arm) for arm in VARIANCE_ARMS)
    verify_call_sampling(_call("perlector", seed=nuda), policy, "perlector", expected_seed=nuda)
    with pytest.raises(ContractError, match=f"not {prior}"):
        verify_call_sampling(
            _call("perlector", seed=nuda), policy, "perlector", expected_seed=prior
        )


@pytest.mark.parametrize("schema", sorted(RETIRED_CALL_RECORD_SCHEMAS))
def test_a_retired_call_record_is_refused_by_its_schema_name(schema):
    policy, _digest = load_decoding_policy()
    call = {**_call("attestator_3"), "schema": schema}
    with pytest.raises(ContractError, match=f"written as {schema}, which this build no longer"):
        verify_call_sampling(call, policy, "attestator_3", expected_seed=7)
    with pytest.raises(SchemaRefusal, match=schema):
        refuse_retired_call_record(schema, subject="a record", error_type=SchemaRefusal)
    refuse_retired_call_record("chair-call-record.v3", subject="a record")
