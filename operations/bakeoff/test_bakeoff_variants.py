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


def test_the_vendor_preset_covers_the_fp8_repository():
    assert A.vendor_preset("Qwen/Qwen3.8-27B-FP8")["family"] == "qwen3.8"


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
    for name in (
        "tokenizer.json",
        "tokenizer_config.json",
        "vocab.json",
        "merges.txt",
        "chat_template.jinja",
        "preprocessor_config.json",
        "video_preprocessor_config.json",
        "generation_config.json",
    ):
        assert fp8[name] == bf16[name], name


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
