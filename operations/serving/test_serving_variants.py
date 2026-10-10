"""Optional engine options (FP8 or mixed NVFP4/FP8 weights, FP8 KV cache, MTP speculation)
and the variants catalogue that uses them. Offline: no vLLM, no GPU, no network."""

from __future__ import annotations

import copy
import json
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from operations.serving.config import (
    ServingProfile,
    engine_option_argv,
    launch_differences,
    parse_serving_recipes,
    profile_preflight_digest,
)
from operations.serving.errors import ServingConfigurationError
from operations.serving.manager import (
    _HYBRID_ATTENTION_REPOSITORIES,
    assert_quantization,
    render_vllm_argv,
)

ROOT = Path(__file__).resolve().parents[2]
RUN_CATALOGUE = ROOT / "config" / "serving_recipes_real.toml"
VARIANTS = ROOT / "config" / "serving_recipes_real_variants.toml"
BASE_KEY = ("unproven-real-perlector", "perlector", "generic-80gb-plus")
# What a variant may change against the bf16 Perlector row; anything else would
# make the comparison measure two things at once.
VARIANT_MAY_CHANGE = {
    "recipe",
    "served_model_id",
    "weights_gib",
    "kv_gib_per_seq",
    "startup_timeout_seconds",
    "quantization",
    "kv_cache_dtype",
    "speculative_config",
}
ENGINE_OPTIONS = ("quantization", "kv_cache_dtype", "speculative_config")


def _raw(path: Path) -> dict:
    return tomllib.loads(path.read_text("utf-8"))


def _row(path: Path, key: tuple[str, str, str]) -> dict:
    return next(
        row
        for row in _raw(path)["profiles"]
        if (row.get("recipe"), row.get("chair"), row.get("tier")) == key
    )


def _catalogue(*rows: dict):
    return parse_serving_recipes({"schema": "serving-recipes.v1", "profiles": list(rows)})


def _snapshot(root: Path = Path("/snapshot")):
    # A local-repository identity renders no revision flags; the argv is otherwise whole.
    return SimpleNamespace(root=root, identity=SimpleNamespace(source="local-repository"))


# --- the run catalogue is untouched ---------------------------------------------------


def test_no_run_catalogue_row_carries_an_engine_option_or_renders_one():
    raw = _raw(RUN_CATALOGUE)
    assert not any(option in row for row in raw["profiles"] for option in ENGINE_OPTIONS)
    for profile in parse_serving_recipes(raw).profiles:
        if isinstance(profile, ServingProfile):
            assert (profile.quantization, profile.kv_cache_dtype) == (None, None)
            assert profile.speculative_config is None
            assert profile.engine_option_argv() == ()


def test_the_bf16_perlector_argv_is_the_one_it_always_was():
    profile = next(
        p for p in parse_serving_recipes(_raw(RUN_CATALOGUE)).profiles if p.key == BASE_KEY
    )
    argv = render_vllm_argv(command_prefix=("vllm",), profile=profile, snapshot=_snapshot())
    assert list(argv) == [
        "vllm", "serve", "/snapshot", "--tokenizer", "/snapshot",
        "--host", "127.0.0.1", "--port", "8106",
        "--served-model-name", "perlector-qwen3.8-27b", "--dtype", "bfloat16", "--seed", "0",
        "--max-model-len", "65536", "--max-num-seqs", "4", "--max-num-batched-tokens", "8192",
        "--gpu-memory-utilization", "0.88",
        "--mm-processor-kwargs", '{"max_pixels":5299200,"min_pixels":65536}',
        "--generation-config", "vllm", "--no-enable-log-requests",
        "--enable-prompt-tokens-details", "--chat-template-content-format", "openai",
        "--no-enable-prefix-caching", "--no-enforce-eager", "--no-trust-remote-code",
    ]  # fmt: skip


def test_the_bf16_perlector_row_digest_is_unchanged():
    # The canonical digest of the row's fields, as on origin/main at f0b28186 before
    # the optional options existed: adding a schema field must not move it.
    assert profile_preflight_digest(_row(RUN_CATALOGUE, BASE_KEY)) == (
        "c0a5001394086aa34bb2e1da4f83150708c892d77f7c0ec75b092906032245e8"
    )


# --- the variants catalogue -----------------------------------------------------------


def test_the_variants_catalogue_is_a_valid_catalogue_of_unproven_perlector_rows():
    recipes = parse_serving_recipes(_raw(VARIANTS))
    names = {profile.recipe for profile in recipes.profiles}
    assert names == {
        "unproven-real-perlector-fp8",
        "unproven-real-perlector-fp8-mtp3",
        "unproven-real-perlector-fp8-mtp1",
        "unproven-real-perlector-fp8-kvfp8",
        "unproven-real-perlector-nvfp4",
        "unproven-real-perlector-nvfp4-mtp3",
    }
    for profile in recipes.profiles:
        assert isinstance(profile, ServingProfile)
        assert (profile.chair, profile.tier) == ("perlector", "generic-80gb-plus")
        assert profile.preflight_state == "unproven"
        # Each row's weights by its name: `-fp8` the official FP8 build, `-nvfp4`
        # NVIDIA's ModelOpt mixed NVFP4/FP8 build.
        expected = "modelopt_mixed" if "-nvfp4" in profile.recipe else "fp8"
        assert profile.quantization == expected, profile.recipe
        assert profile.enable_prefix_caching is False  # hybrid attention
    run = {profile.recipe for profile in parse_serving_recipes(_raw(RUN_CATALOGUE)).profiles}
    assert not names & run


def test_each_variant_differs_from_the_bf16_row_only_in_what_it_tests():
    base = _row(RUN_CATALOGUE, BASE_KEY)
    for row in _raw(VARIANTS)["profiles"]:
        changed = {key for key in set(base) | set(row) if base.get(key) != row.get(key)}
        assert changed <= VARIANT_MAY_CHANGE, (row["recipe"], changed - VARIANT_MAY_CHANGE)


def test_the_speculating_rows_render_their_flags_last():
    recipes = parse_serving_recipes(_raw(VARIANTS))
    by_name = {profile.recipe: profile for profile in recipes.profiles}
    argv = render_vllm_argv(
        command_prefix=("vllm",),
        profile=by_name["unproven-real-perlector-fp8-mtp3"],
        snapshot=_snapshot(),
    )
    assert list(argv[-5:]) == [
        "--no-trust-remote-code",
        "--quantization",
        "fp8",
        "--speculative-config",
        '{"method":"mtp","num_speculative_tokens":3}',
    ]
    kv = by_name["unproven-real-perlector-fp8-kvfp8"].engine_option_argv()
    assert kv == ("--quantization", "fp8", "--kv-cache-dtype", "fp8")
    assert launch_differences(
        by_name["unproven-real-perlector-fp8-mtp1"], by_name["unproven-real-perlector-fp8-mtp3"]
    ) == ["served_model_id", "speculative_config"]


def test_the_nvfp4_rows_render_vllms_modelopt_mixed_name_last():
    recipes = parse_serving_recipes(_raw(VARIANTS))
    by_name = {profile.recipe: profile for profile in recipes.profiles}
    argv = render_vllm_argv(
        command_prefix=("vllm",),
        profile=by_name["unproven-real-perlector-nvfp4-mtp3"],
        snapshot=_snapshot(),
    )
    assert list(argv[-5:]) == [
        "--no-trust-remote-code",
        "--quantization",
        "modelopt_mixed",
        "--speculative-config",
        '{"method":"mtp","num_speculative_tokens":3}',
    ]
    assert by_name["unproven-real-perlector-nvfp4"].engine_option_argv() == (
        "--quantization",
        "modelopt_mixed",
    )
    # The NVFP4 pair differs from the FP8 pair only in the weights it names.
    assert launch_differences(
        by_name["unproven-real-perlector-nvfp4"], by_name["unproven-real-perlector-fp8"]
    ) == ["served_model_id", "quantization"]


def test_the_quantized_repositories_are_guarded_as_hybrid_attention():
    assert "Qwen/Qwen3.8-27B-FP8" in _HYBRID_ATTENTION_REPOSITORIES
    assert "nvidia/Qwen3.8-27B-NVFP4" in _HYBRID_ATTENTION_REPOSITORIES


# --- the closed schema ----------------------------------------------------------------


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("quantization", "awq", "quantization must be one of"),
        ("quantization", "modelopt", "quantization must be one of"),
        ("quantization", "modelopt_fp4", "quantization must be one of"),
        ("quantization", "", "non-blank"),
        ("kv_cache_dtype", "fp8_e5m2", "kv_cache_dtype must be one of"),
        ("speculative_config", {"method": "mtp"}, "exactly"),
        ("speculative_config", {"method": "mtp", "num_speculative_tokens": 3, "model": "x"}, "exactly"),
        ("speculative_config", {"method": "ngram", "num_speculative_tokens": 3}, "method must be"),
        ("speculative_config", {"method": "mtp", "num_speculative_tokens": 0}, "positive"),
        ("speculative_config", {"method": "mtp", "num_speculative_tokens": 9}, "at most 8"),
        ("speculative_config", {"method": "mtp", "num_speculative_tokens": True}, "positive"),
        ("speculative_config", '{"method":"mtp"}', "exactly"),
    ],
)  # fmt: skip
def test_engine_options_take_only_the_reviewed_values(field, value, message):
    row = copy.deepcopy(_row(VARIANTS, ("unproven-real-perlector-fp8", *BASE_KEY[1:])))
    row[field] = value
    with pytest.raises(ServingConfigurationError, match=message):
        _catalogue(row)


def test_a_row_without_options_renders_none_and_options_render_in_one_order():
    assert engine_option_argv(quantization=None, kv_cache_dtype=None, speculative_config=None) == ()
    assert engine_option_argv(
        quantization="fp8",
        kv_cache_dtype="fp8",
        speculative_config={"num_speculative_tokens": 1, "method": "mtp"},
    ) == (
        "--quantization",
        "fp8",
        "--kv-cache-dtype",
        "fp8",
        "--speculative-config",
        '{"method":"mtp","num_speculative_tokens":1}',
    )


def test_a_proven_mark_covers_the_engine_options():
    row = copy.deepcopy(_row(VARIANTS, ("unproven-real-perlector-fp8-mtp3", *BASE_KEY[1:])))
    row["preflight_state"] = "proven"
    row["preflight_identity_digest"] = "0" * 64
    row["preflight_digest"] = profile_preflight_digest(row)
    _catalogue(row)
    row["speculative_config"] = {"method": "mtp", "num_speculative_tokens": 5}
    with pytest.raises(ServingConfigurationError, match="stale preflight_digest"):
        _catalogue(row)


# --- the snapshot must already be what the row says -----------------------------------


def _config_snapshot(tmp_path: Path, document: object) -> SimpleNamespace:
    (tmp_path / "config.json").write_text(json.dumps(document), "utf-8")
    return SimpleNamespace(root=tmp_path)


def _quant_row(quantization: str | None):
    return SimpleNamespace(
        quantization=quantization, chair="perlector", recipe="r", tier="generic-80gb-plus"
    )


def test_an_fp8_row_needs_a_checkpoint_that_declares_fp8(tmp_path):
    fp8 = {"quantization_config": {"quant_method": "fp8", "weight_block_size": [128, 128]}}
    assert_quantization(_config_snapshot(tmp_path, fp8), _quant_row("fp8"))
    with pytest.raises(ServingConfigurationError, match="quant_method=None"):
        assert_quantization(_config_snapshot(tmp_path, {"architectures": ["x"]}), _quant_row("fp8"))


def test_a_row_without_quantization_is_not_checked(tmp_path):
    assert_quantization(SimpleNamespace(root=tmp_path / "absent"), _quant_row(None))


def test_a_modelopt_mixed_row_needs_a_modelopt_mixed_precision_checkpoint(tmp_path):
    # As NVIDIA's Qwen3.8-27B-NVFP4 config.json says it (quant_algo case is vLLM's to
    # normalise); per-layer FP8 / NVFP4 is vLLM's `modelopt_mixed` method.
    mixed = {
        "quantization_config": {
            "quant_method": "modelopt",
            "quant_algo": "MIXED_PRECISION",
            "quantized_layers": {"lm_head": {"quant_algo": "NVFP4", "group_size": 16}},
        }
    }
    assert_quantization(_config_snapshot(tmp_path, mixed), _quant_row("modelopt_mixed"))
    mixed["quantization_config"]["quant_algo"] = "mixed_precision"
    assert_quantization(_config_snapshot(tmp_path, mixed), _quant_row("modelopt_mixed"))
    refused = [
        ({"architectures": ["x"]}, "quant_method=None"),
        ({"quantization_config": {"quant_method": "modelopt", "quant_algo": "NVFP4"}}, "'NVFP4'"),
        ({"quantization_config": {"quant_method": "modelopt"}}, "quant_algo=None"),
        ({"quantization_config": {"quant_method": "fp8"}}, "quant_method='fp8'"),
    ]
    for document, message in refused:
        with pytest.raises(ServingConfigurationError, match=message):
            assert_quantization(_config_snapshot(tmp_path, document), _quant_row("modelopt_mixed"))
    # And an fp8 row is not satisfied by the ModelOpt checkpoint.
    with pytest.raises(ServingConfigurationError, match="quant_method='modelopt'"):
        assert_quantization(_config_snapshot(tmp_path, mixed), _quant_row("fp8"))
