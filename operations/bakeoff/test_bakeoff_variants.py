"""The bake-off launches a variant serving row by name, renders its engine options the
serving layer's way, pins the FP8 checkpoint, and may restrict output tokens (off by
default). Offline; nothing is downloaded."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from common.chairs.manifests import manifest_digest
from common.chairs.models import DigestManifest, ManifestRow
from operations.bakeoff import allowed_tokens as T
from operations.bakeoff import arms as A
from operations.bakeoff import weights as W

ROOT = Path(__file__).resolve().parents[2]
FP8 = "qwen3.8-27b-fp8"
FP8_MANIFEST_DIGEST = "9825ce119c9693172e04dd2a1f2437884503ceab9bf55606141e6662c9fe301e"
NVFP4 = "qwen3.8-27b-nvfp4"
NVFP4_MANIFEST_DIGEST = "7e6b9db8a572b49f32864b250ae90b1690d7559fa3492ce4484f2b31146ebe3f"
# What the bf16, FP8 and NVFP4 builds share byte for byte: what turns a page into a prompt.
PROMPT_FILES = (
    "tokenizer.json",
    "vocab.json",
    "merges.txt",
    "chat_template.jinja",
    "preprocessor_config.json",
    "video_preprocessor_config.json",
)
BF16_MANIFEST = ROOT / "config" / "manifests" / "qwen3.8-27B.json"


def test_the_run_row_is_still_what_an_arm_gets_by_default():
    row = A.serving_row("perlector", "generic-80gb-plus")
    assert row["recipe"] == "unproven-real-perlector"
    argv = A.server_argv(
        row, Path("/w"), port=8190, served_name="x", gpu_memory_utilization=0.92, max_num_seqs=32
    )
    assert argv[-1] == "--no-trust-remote-code"
    assert not {"--quantization", "--speculative-config", "--kv-cache-dtype"} & set(argv)


def test_a_variant_row_is_found_by_name_and_launched_with_its_options():
    row = A.serving_row("perlector", "generic-80gb-plus", "unproven-real-perlector-fp8-mtp3")
    argv = A.server_argv(
        row, Path("/w"), port=8190, served_name="x", gpu_memory_utilization=0.92, max_num_seqs=32
    )
    assert argv[-4:] == [
        "--quantization",
        "fp8",
        "--speculative-config",
        '{"method":"mtp","num_speculative_tokens":3}',
    ]
    assert A.serving_row("perlector", "generic-80gb-plus", "unproven-real-perlector")["dtype"]
    with pytest.raises(SystemExit, match="0 vLLM rows"):
        A.serving_row("perlector", "generic-48gb", "unproven-real-perlector-fp8")
    with pytest.raises(SystemExit, match="0 vLLM rows"):
        A.serving_row("perlector", "generic-80gb-plus", "no-such-recipe")


def test_the_vendor_preset_covers_the_fp8_and_nvfp4_repositories():
    assert A.vendor_preset("Qwen/Qwen3.8-27B-FP8")["family"] == "qwen3.8"
    nvidia = A.vendor_preset("nvidia/Qwen3.8-27B-NVFP4")
    assert nvidia is A.vendor_preset("Qwen/Qwen3.8-27B")
    with pytest.raises(SystemExit, match="no preset"):
        A.vendor_preset("nvidia/Qwen3.5-27B-NVFP4")


def test_an_nvfp4_variant_is_launched_as_vllms_modelopt_mixed():
    row = A.serving_row("perlector", "generic-80gb-plus", "unproven-real-perlector-nvfp4-mtp3")
    argv = A.server_argv(
        row, Path("/w"), port=8190, served_name="x", gpu_memory_utilization=0.92, max_num_seqs=32
    )
    assert argv[-4:] == [
        "--quantization",
        "modelopt_mixed",
        "--speculative-config",
        '{"method":"mtp","num_speculative_tokens":3}',
    ]


def test_the_launcher_refuses_a_snapshot_that_does_not_declare_the_rows_quantization(tmp_path):
    row = A.serving_row("perlector", "generic-80gb-plus", "unproven-real-perlector-nvfp4")
    (tmp_path / "config.json").write_text(
        json.dumps(
            {"quantization_config": {"quant_method": "modelopt", "quant_algo": "MIXED_PRECISION"}}
        )
    )
    A.assert_row_quantization(row, tmp_path)
    (tmp_path / "config.json").write_text(
        json.dumps({"architectures": ["Qwen3_5ForConditionalGeneration"]})
    )
    with pytest.raises(SystemExit, match="quant_method=None"):
        A.assert_row_quantization(row, tmp_path)
    # The bf16 run row names no quantization and is not checked.
    A.assert_row_quantization(A.serving_row("perlector", "generic-80gb-plus"), tmp_path / "absent")


# --- the FP8 checkpoint's pins ----------------------------------------------------------


def test_the_fp8_pin_names_every_file_by_sha256_and_gives_the_manifest_a_chair_would_seal():
    pin = W.pins()[FP8]
    assert pin["repo"] == "Qwen/Qwen3.8-27B-FP8"
    assert pin["revision"] == "017b9c7af6b5689d5dd426a76e0bc077eb5ca20a"
    assert len(pin["files"]) == 81 and 30.8e9 < W.size_of(FP8) < 31.0e9
    assert all(len(entry["sha256"]) == 64 for entry in pin["files"].values())
    # The canonical manifest the model store would measure on first download: its
    # digest is what a future `[chairs.perlector]` FP8 pin would carry.
    manifest = DigestManifest(
        rows=tuple(
            ManifestRow(path=path, sha256=entry["sha256"], size=entry["size"])
            for path, entry in sorted(pin["files"].items())
        )
    )
    assert manifest_digest(manifest) == FP8_MANIFEST_DIGEST


def test_the_fp8_build_reads_prompts_and_images_exactly_as_the_bf16_one():
    # Same tokenizer, chat template, image preprocessor and generation config bytes, so
    # the prompt, the image's token cost and the sampling rows carry over unchanged.
    fp8 = {path: entry["sha256"] for path, entry in W.pins()[FP8]["files"].items()}
    bf16 = {row["path"]: row["sha256"] for row in json.loads(BF16_MANIFEST.read_text())}
    for name in (*PROMPT_FILES, "tokenizer_config.json", "generation_config.json"):
        assert fp8[name] == bf16[name], name


# --- the NVFP4 checkpoint's pins --------------------------------------------------------


def test_the_nvfp4_pin_names_every_file_by_sha256_and_gives_the_manifest_a_chair_would_seal():
    pin = W.pins()[NVFP4]
    assert pin["repo"] == "nvidia/Qwen3.8-27B-NVFP4"
    assert pin["revision"] == "482ca0f3832238542f8f5295dde86b5f22711d80"
    assert len(pin["files"]) == 19 and W.size_of(NVFP4) == 21_945_291_730
    assert all(len(entry["sha256"]) == 64 for entry in pin["files"].values())
    assert NVFP4 in W.known()
    manifest = DigestManifest(
        rows=tuple(
            ManifestRow(path=path, sha256=entry["sha256"], size=entry["size"])
            for path, entry in sorted(pin["files"].items())
        )
    )
    assert manifest_digest(manifest) == NVFP4_MANIFEST_DIGEST


def test_the_nvfp4_build_differs_from_bf16_only_where_the_design_note_says():
    # The prompt files are the bf16 ones byte for byte. Besides the weights and their
    # index and config, it differs in tokenizer_config.json (no embedded chat template, pad
    # token <|im_end|>), generation_config.json (same values, newer writer) and the README,
    # and adds processor_config.json, hf_quant_config.json and .quant_summary.txt.
    nvfp4 = {path: entry["sha256"] for path, entry in W.pins()[NVFP4]["files"].items()}
    bf16 = {row["path"]: row["sha256"] for row in json.loads(BF16_MANIFEST.read_text())}
    for name in PROMPT_FILES:
        assert nvfp4[name] == bf16[name], name
    small = {p for p in nvfp4 if not p.endswith(".safetensors")}
    assert {p for p in small if p in bf16 and nvfp4[p] != bf16[p]} == {
        "README.md",
        "config.json",
        "model.safetensors.index.json",
        "tokenizer_config.json",
        "generation_config.json",
    }
    assert small - set(bf16) == {
        "processor_config.json",
        "hf_quant_config.json",
        ".quant_summary.txt",
    }


# --- the output-token restriction -------------------------------------------------------


def _tokenizer() -> dict:
    to_char = {byte: char for char, byte in T._byte_decoder().items()}

    def token(text: str) -> str:
        return "".join(to_char[b] for b in text.encode("utf-8"))

    vocab = {to_char[b]: b for b in range(256)}
    for text in (" Baptême", "Ste", "中文", "Ж", ' {"n":', "é́"):
        vocab[token(text)] = len(vocab)
    vocab[token("é")[:1] + token("中")[:1]] = len(vocab)  # an undecodable two-byte piece
    added = [
        {"id": 900, "content": "<|endoftext|>", "special": True},
        {"id": 901, "content": "<|im_end|>", "special": True},
        {"id": 902, "content": "<think>", "special": True},
    ]
    return {
        "model": {"type": "BPE", "vocab": vocab},
        "decoder": {"type": "ByteLevel"},
        "added_tokens": added,
    }


def test_latin_json_keeps_latin_words_every_byte_and_the_end_tokens():
    tokenizer = _tokenizer()
    ids = set(T.latin_json_v1(tokenizer))
    vocab = tokenizer["model"]["vocab"]
    by_id = {v: k for k, v in vocab.items()}
    assert set(range(256)) <= ids  # anything can still be spelled byte by byte
    kept = {by_id[i] for i in ids if 256 <= i < 900}
    assert len(kept) == 4  # " Baptême", "Ste", ' {"n":', "é" + combining acute
    assert {900, 901} <= ids and 902 not in ids
    assert len(ids) == 256 + 4 + 2


def test_the_restriction_is_off_unless_asked_and_named_not_listed_in_the_record():
    arm = A.ARMS["qwen-vendor"]
    row = A.arm_row(arm, A.serving_row("perlector", "generic-80gb-plus"), "Qwen/Qwen3.8-27B-FP8")
    png = _tiny_png()
    unit = {"unit": "page", "bounds": {"x": 0, "y": 0, "w": 32, "h": 32}, "png": png}
    body, record = A.build_request(
        arm, unit, row=row, served_name="s", max_model_len=65536, repo="Qwen/Qwen3.8-27B-FP8"
    )
    assert "allowed_token_ids" not in body and "allowed_tokens" not in record
    ids = (5, 7, 11)
    body, record = A.build_request(
        arm,
        unit,
        row=row,
        served_name="s",
        max_model_len=65536,
        repo="Qwen/Qwen3.8-27B-FP8",
        allowed_tokens=("latin-json-v1", ids),
    )
    assert body["allowed_token_ids"] == [5, 7, 11]
    assert "allowed_token_ids" not in record["sampling"]
    assert record["allowed_tokens"] == {
        "set": "latin-json-v1",
        "count": 3,
        "sha256": hashlib.sha256(b"[5,7,11]").hexdigest(),
    }


def _tiny_png() -> bytes:
    import io

    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (32, 32), "white").save(out, format="PNG")
    return out.getvalue()
