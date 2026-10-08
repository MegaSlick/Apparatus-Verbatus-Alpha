from __future__ import annotations

import re
from pathlib import Path

import pytest

from common.chandra_native_retry import recipe_record
from common.contracts.errors import ContractError, SchemaRefusal
from common.contracts.serving import CHAIR_CALL_RECORD_SCHEMA, RETIRED_CALL_RECORD_SCHEMAS
from common.decoding import (
    DEFAULT_DECODING_CONFIG_PATH,
    ENGINE_FILLED_SAMPLING_FIELDS,
    READING_CHAIRS,
    chair_attempt_decoding,
    chair_decoding,
    decoded_wire_decimals,
    engine_effective_sampling,
    load_decoding_policy,
    perlector_loop_guard,
    perlector_page_generation,
    perlector_page_max_tokens,
    reconstructor_max_tokens,
    recorded_wire_decimals,
    refuse_retired_call_record,
    verify_call_sampling,
)

# The makers' recommendations, typed here from their sources rather than read
# back from the file under test, so a drifted row fails by value. A field the
# maker leaves unset is at the value the maker's own pipeline runs under.
_VLLM_UNSENT = {"top_k": 0, "min_p": 0.0, "repetition_penalty": 1.0}
MAKERS_SAMPLING = {
    # datalab-to/chandra@d4f7467, chandra/model/vllm.py::generate_vllm defaults,
    # sent to a vLLM server; the rest are vllm==0.30.0's request defaults.
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
    # The same card and mode, asked text only.
    "reconstructor": {
        "temperature": 0.7,
        "top_p": 0.8,
        "top_k": 20,
        "min_p": 0.0,
        "presence_penalty": 1.5,
        "repetition_penalty": 1.0,
    },
}


def test_every_row_names_every_field_the_engine_would_otherwise_fill():
    """vllm==0.30.0's `get_diff_sampling_param` fills these from a generation config."""
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


def test_the_chandra_chair_must_keep_the_pinned_recipes_first_request(tmp_path: Path):
    source = DEFAULT_DECODING_CONFIG_PATH.read_text(encoding="utf-8")
    head, row, tail = source.partition("[chair_decoding.attestator_1]")
    moved = head + row + tail.replace("temperature = 0.0", "temperature = 1", 1)
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


def test_shipped_decoding_policy_declares_its_sections():
    policy, digest = load_decoding_policy()
    assert set(policy) == {
        "schema",
        "chair_decoding",
        "perlector_generation",
        "reconstructor_generation",
        "chandra_native_inference",
    }
    assert policy["schema"] == "decoding.v9"
    assert policy["perlector_generation"] == {
        "page_max_tokens": 12288,
        "loop_line_repeats": 30,
        "loop_block_repeats": 10,
        "loop_block_max_lines": 8,
    }
    assert perlector_page_max_tokens(policy) == 12288
    assert perlector_page_generation(policy) == {"page_max_tokens": 12288}
    assert perlector_loop_guard(policy) == {
        "loop_line_repeats": 30,
        "loop_block_repeats": 10,
        "loop_block_max_lines": 8,
    }
    assert policy["reconstructor_generation"] == {"answer_max_tokens": 8192}
    assert reconstructor_max_tokens(policy) == 8192
    assert policy["chandra_native_inference"] == recipe_record()
    assert len(digest) == 64


@pytest.mark.parametrize(
    "schema",
    [
        "decoding.v1",
        "decoding.v2",
        "decoding.v3",
        "decoding.v4",
        "decoding.v5",
        "decoding.v6",
        "decoding.v7",
        "decoding.v8",
    ],
)
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
        ("[perlector_generation]", "[missing_generation]", "wrong closed schema"),
        (
            "page_max_tokens = 12288",
            "page_max_tokens = 12288\nreading_max_tokens = 4096",
            "perlector_generation must declare a positive integer",
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
def test_a_page_output_bound_that_is_not_a_positive_integer_is_refused(tmp_path: Path, bound: str):
    source = DEFAULT_DECODING_CONFIG_PATH.read_text(encoding="utf-8")
    path = tmp_path / "decoding.toml"
    path.write_text(
        re.sub(
            r"^page_max_tokens = \d+$", f"page_max_tokens = {bound}", source, count=1, flags=re.M
        ),
        encoding="utf-8",
    )

    with pytest.raises(ContractError, match="perlector_generation must declare a positive integer"):
        load_decoding_policy(path)


def test_a_missing_perlector_generation_section_is_refused(tmp_path: Path):
    source = DEFAULT_DECODING_CONFIG_PATH.read_text(encoding="utf-8")
    before, _section, after = source.partition("[perlector_generation]")
    path = tmp_path / "decoding.toml"
    path.write_text(
        before + "[chandra_native_inference]" + after.partition("[chandra_native_inference]")[2],
        encoding="utf-8",
    )

    with pytest.raises(ContractError, match="wrong closed schema"):
        load_decoding_policy(path)


@pytest.mark.parametrize(
    ("field", "value"),
    [("loop_line_repeats", "1"), ("loop_block_repeats", "1"), ("loop_block_max_lines", "1")],
)
def test_a_repetition_loop_guard_that_would_stop_honest_replies_is_refused(
    tmp_path: Path, field: str, value: str
):
    source = DEFAULT_DECODING_CONFIG_PATH.read_text(encoding="utf-8")
    path = tmp_path / "decoding.toml"
    path.write_text(
        re.sub(rf"^{field} = \d+$", f"{field} = {value}", source, flags=re.M), encoding="utf-8"
    )

    with pytest.raises(ContractError, match="perlector_generation: a repetition loop|at least 2"):
        load_decoding_policy(path)


@pytest.mark.parametrize(
    "field", ["loop_line_repeats", "loop_block_repeats", "loop_block_max_lines"]
)
def test_a_perlector_generation_section_without_its_loop_guard_is_refused(
    tmp_path: Path, field: str
):
    source = DEFAULT_DECODING_CONFIG_PATH.read_text(encoding="utf-8")
    path = tmp_path / "decoding.toml"
    path.write_text(re.sub(rf"^{field} = \d+\n", "", source, flags=re.M), encoding="utf-8")

    with pytest.raises(ContractError, match="perlector_generation must declare a positive integer"):
        load_decoding_policy(path)


def test_a_perlector_generation_section_without_the_page_cap_is_refused(tmp_path: Path):
    source = DEFAULT_DECODING_CONFIG_PATH.read_text(encoding="utf-8")
    path = tmp_path / "decoding.toml"
    path.write_text(re.sub(r"^page_max_tokens = \d+\n", "", source, flags=re.M), encoding="utf-8")

    with pytest.raises(ContractError, match="perlector_generation must declare a positive integer"):
        load_decoding_policy(path)


# --- per-attempt, per-arm and engine-effective sampling -----------------------


def test_only_the_chandra_chair_retries_along_chandras_own_schedule():
    policy, _digest = load_decoding_policy()
    assert [chair_attempt_decoding(policy, "attestator_1", n) for n in (1, 2, 3)] == [
        {"temperature": 0.0, "top_p": 0.1, **_VLLM_UNSENT},
        {"temperature": 0.2, "top_p": 0.95, **_VLLM_UNSENT},
        {"temperature": 0.4, "top_p": 0.95, **_VLLM_UNSENT},
    ]
    assert chair_attempt_decoding(policy, "attestator_1", 7) == {
        "temperature": 0.8,
        "top_p": 0.95,
        **_VLLM_UNSENT,
    }
    with pytest.raises(ContractError, match="no attempt 8"):
        chair_attempt_decoding(policy, "attestator_1", 8)
    with pytest.raises(ContractError, match="only the Chandra chair retries"):
        chair_attempt_decoding(policy, "perlector", 2)
    with pytest.raises(ContractError, match="positive integer"):
        chair_attempt_decoding(policy, "attestator_1", 0)


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
def test_the_engine_effective_mapping_is_vllm_0_30_0s(sent, effective):
    assert engine_effective_sampling(sent) == effective


def test_the_engine_effective_mapping_agrees_with_the_pinned_engine_when_installed():
    """Run where the pod's `vllm==0.30.0` is installed; the unit cases above hold offline."""
    sampling_params = pytest.importorskip("vllm.sampling_params")
    import vllm

    assert vllm.__version__ == "0.30.0"
    policy, _digest = load_decoding_policy()
    for chair in sorted(READING_CHAIRS):
        sent = chair_decoding(policy, chair)
        params = sampling_params.SamplingParams(**sent)
        assert {field: getattr(params, field) for field in sent} == engine_effective_sampling(sent)


def _call(chair: str, attempt: int = 1, seed: int | None = 7) -> dict:
    policy, _digest = load_decoding_policy()
    sampling = chair_attempt_decoding(policy, chair, attempt)
    sent = {**recorded_wire_decimals(sampling), "max_tokens": 10}
    if seed is not None:
        sent["seed"] = seed
    return {
        "schema": CHAIR_CALL_RECORD_SCHEMA,
        "generation_sent": sent,
        "sampling_effective": recorded_wire_decimals(engine_effective_sampling(sampling)),
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
                temperature=recorded_wire_decimals({"t": 0.2})["t"]
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


def test_a_retry_call_is_verified_against_its_own_attempt():
    policy, _digest = load_decoding_policy()
    call = _call("attestator_1", 2)
    verify_call_sampling(call, policy, "attestator_1", attempt_ordinal=2, expected_seed=7)
    with pytest.raises(ContractError, match="for attempt 1"):
        verify_call_sampling(call, policy, "attestator_1", expected_seed=7)


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


@pytest.mark.parametrize("schema", sorted(RETIRED_CALL_RECORD_SCHEMAS))
def test_a_retired_call_record_is_refused_by_its_schema_name(schema):
    policy, _digest = load_decoding_policy()
    call = {**_call("attestator_3"), "schema": schema}
    with pytest.raises(ContractError, match=f"written as {schema}, which this build no longer"):
        verify_call_sampling(call, policy, "attestator_3", expected_seed=7)
    with pytest.raises(SchemaRefusal, match=schema):
        refuse_retired_call_record(schema, subject="a record", error_type=SchemaRefusal)
    refuse_retired_call_record(CHAIR_CALL_RECORD_SCHEMA, subject="a record")


def test_decoded_wire_decimals_restores_each_canonical_tagged_float() -> None:
    recorded = {
        "top_p": {"schema": "wire-decimal.v1", "decimal": "0.001"},
        "stop": [{"schema": "wire-decimal.v1", "decimal": "1.05"}, "x"],
        "seed": 7,
    }
    assert decoded_wire_decimals(recorded) == {"top_p": 0.001, "stop": [1.05, "x"], "seed": 7}


def test_recorded_wire_decimals_tags_every_float_at_any_depth_and_decodes_back() -> None:
    view = {"top_p": 0.001, "stop": [1.05, "x"], "nested": {"t": 0.2}, "seed": 7, "on": True}

    recorded = recorded_wire_decimals(view)

    assert recorded == {
        "top_p": {"schema": "wire-decimal.v1", "decimal": "0.001"},
        "stop": [{"schema": "wire-decimal.v1", "decimal": "1.05"}, "x"],
        "nested": {"t": {"schema": "wire-decimal.v1", "decimal": "0.2"}},
        "seed": 7,
        "on": True,
    }
    assert decoded_wire_decimals(recorded) == view


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_recorded_wire_decimals_refuses_a_value_its_inverse_would_refuse(value: float) -> None:
    with pytest.raises(ContractError, match="not finite"):
        recorded_wire_decimals({"nested": [{"t": value}]})


@pytest.mark.parametrize(
    ("decimal", "reason"),
    [
        (1.0, "not text"),
        ("abc", "not a number"),
        ("NaN", "not canonical"),
        ("0.10", "not canonical"),
    ],
)
def test_decoded_wire_decimals_refuses_every_form_but_the_one_canonical_text(
    decimal: object, reason: str
) -> None:
    with pytest.raises(ContractError, match=reason):
        decoded_wire_decimals({"t": {"schema": "wire-decimal.v1", "decimal": decimal}})
