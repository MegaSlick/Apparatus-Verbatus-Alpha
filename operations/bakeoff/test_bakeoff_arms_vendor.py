"""The qwen-vendor arm: each family's request, and one synthetic page end to end."""

import io
import json
import sys

import pytest
from PIL import Image

from operations.bakeoff import arms as A
from operations.bakeoff import witness_run as W
from operations.bakeoff.test_bakeoff_runner import FAKE, _free_port, _pages

FAMILIES = {
    "Qwen/Qwen3.8-27B": "qwen3.8",
    "Qwen/Qwen3.5-27B": "qwen3.5",
    "Qwen/Qwen3.5-9B": "qwen3.5",
}
NON_THINKING = {
    "temperature": 0.7,
    "top_p": 0.8,
    "top_k": 20,
    "min_p": 0.0,
    "presence_penalty": 1.5,
    "repetition_penalty": 1.0,
}


def _png(width: int, height: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("L", (width, height), 255).save(buffer, format="PNG")
    return buffer.getvalue()


def _request(repo: str, width: int = 400, height: int = 300):
    arm = A.ARMS["qwen-vendor"]
    row = A.arm_row(arm, A.serving_row(arm.chair, arm.default_tier), repo)
    unit = A.page_units(arm, _png(width, height))[0]
    return row, *A.build_request(
        arm, unit, row=row, served_name="m", max_model_len=65_536, repo=repo
    )


@pytest.mark.parametrize("repo", sorted(FAMILIES))
def test_each_family_sends_its_vendor_preset(repo):
    row, body, record = _request(repo)
    assert [m["role"] for m in body["messages"]] == ["user"]  # no system prompt
    image, text = body["messages"][0]["content"]
    assert image["type"] == "image_url" and text["text"] == A.QWEN_VENDOR_PROMPT
    assert text["text"].startswith("Please output only the text content from the image")
    assert "[[?]]" in text["text"] and "abbreviations" in text["text"]
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert {k: body[k] for k in NON_THINKING} == NON_THINKING and body["seed"] == 0
    assert "max_tokens" not in body and record["max_tokens"] is None
    assert record["max_tokens_basis"].startswith("omitted: no reply cap")
    assert (row["min_pixels"], row["max_pixels"]) == (65_536, 16_777_216)
    preset = record["vendor_preset"]
    assert preset["repo"] == repo and preset["family"] == FAMILIES[repo]
    assert preset["prompt"] == A.QWEN_VENDOR_PROMPT and preset["max_pixels"] == 16_777_216
    assert record["sampling"]["presence_penalty"] == 1.5


def test_vendor_pixels_reach_the_server_and_the_estimate():
    arm = A.ARMS["qwen-vendor"]
    plain = A.serving_row(arm.chair, arm.default_tier)
    row = A.arm_row(arm, plain, "Qwen/Qwen3.5-9B")
    argv = A.server_argv(
        row, A.ROOT, port=1, served_name="m", gpu_memory_utilization=0.9, max_num_seqs=2
    )
    pixels = json.loads(argv[argv.index("--mm-processor-kwargs") + 1])
    assert pixels == {"min_pixels": 65_536, "max_pixels": 16_777_216}
    assert A.arm_row(A.ARMS["qwen-blind"], plain, "Qwen/Qwen3.5-9B") is plain
    _, _, record = _request("Qwen/Qwen3.8-27B", 4000, 6000)
    assert record["image_tokens_estimate"] > A.image_tokens(plain, 4000, 6000)


def test_the_plain_arm_is_unchanged():
    arm = A.ARMS["qwen-blind"]
    row = A.serving_row(arm.chair, arm.default_tier)
    unit = A.page_units(arm, _png(400, 300))[0]
    body, record = A.build_request(arm, unit, row=row, served_name="m", max_model_len=65_536)
    assert body["temperature"] == 0.0 and body["max_tokens"] == A.QWEN_BLIND_MAX_TOKENS
    assert "vendor_preset" not in record


def test_an_unknown_repo_is_refused():
    with pytest.raises(SystemExit, match="no preset"):
        _request("someone/Other-VL")


def test_one_page_end_to_end(tmp_path):
    pages, out, weights = tmp_path / "pages", tmp_path / "cache", tmp_path / "w"
    _pages(pages, 1)
    weights.mkdir()
    (weights / "config.json").write_text("{}")
    argv = [
        "run", "--model", "qwen-vendor", "--label", "qwen35-9b-vendor",
        "--repo", "Qwen/Qwen3.5-9B", "--revision", "c202236235762e1c871ad0ccb60c8ee5ba337b9a",
        "--weights", str(weights), "--pages", str(pages), "--out", str(out),
        "--port", str(_free_port()), "--max-num-seqs", "2", "--startup-timeout", "60",
        "--vllm-cmd", sys.executable, str(FAKE),
    ]  # fmt: skip
    assert W.main(argv) == 0
    record = json.loads((out / "qwen35-9b-vendor" / "p000.json").read_text())
    assert record["arm"] == "qwen-vendor" and record["repo"] == "Qwen/Qwen3.5-9B"
    assert record["error"] is None and record["text"] == "Le dix mai"
    request = record["units"][0]["request"]
    assert request["vendor_preset"]["family"] == "qwen3.5"
    assert request["sampling"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert request["sampling"]["top_k"] == 20 and request["max_tokens"] is None
    argv_sent = record["server"]["argv"]
    pixels = json.loads(argv_sent[argv_sent.index("--mm-processor-kwargs") + 1])
    assert pixels["max_pixels"] == 16_777_216
