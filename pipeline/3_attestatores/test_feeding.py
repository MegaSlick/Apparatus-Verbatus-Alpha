"""R3 fixture contracts: no model calls, downloads, or model roster edits."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "2_designator"))

from feeding import (
    CHURRO_OUTPUT_TOKENS,
    DAI_MAX_WIDTH_PX,
    SCHEDULING_POLICY,
    chandra_capture_intake,
    churro_generation,
    churro_prompt,
    dai_model_view,
    retain_model_view,
    stage_major_schedule,
)
from geometry_layer import retain_chandra_response

from common.contracts.canonical import digest_bytes
from common.contracts.errors import SchemaRefusal


def _ref(path: str) -> dict[str, str]:
    return {"relative_path": path, "sha256": "a" * 64}


class _Tree:
    def __init__(self):
        self.blobs = {}
        self.receipts = []

    def put_blob(self, stage, data):
        digest = digest_bytes(data)
        path = f"{stage}/blobs/sha256/{digest}"
        self.blobs[path] = data
        return digest, type("Published", (), {"relative_path": path})()

    def read_run_receipt(self, reference):
        self.receipts.append(reference)
        return {"fixture": True}

    def read_bytes(self, path):
        return self.blobs[path]


def test_churro_records_a_24k_bound_and_detects_repetition_after_complete_capture():
    tree = _Tree()
    raw = b"a" * 72
    record = retain_model_view(
        tree,
        adapter="churro.v1",
        view={"prompt": churro_prompt(), "generation": churro_generation()},
        raw_response=raw,
        transport_stop_reason="eos",
        parser="xml",
    )
    assert CHURRO_OUTPUT_TOKENS == 24_000
    assert record["raw_response_ref"]["sha256"] == digest_bytes(raw)
    assert record["findings"][0]["kind"] == "post-hoc-repetition"
    assert record["stop_reason"] == "partial-parse-failed"
    assert record["parse"]["state"] == "failed"
    assert tree.blobs[record["raw_response_ref"]["relative_path"]] == raw


def test_churro_validates_xml_without_discarding_the_raw_response():
    tree = _Tree()
    record = retain_model_view(
        tree,
        adapter="churro.v1",
        view={"prompt": churro_prompt(), "generation": churro_generation()},
        raw_response=b"<output>verbatim</output>",
        transport_stop_reason="length",
        parser="xml",
    )
    assert record["parse"] == {"state": "parsed", "parser": "xml", "text": "verbatim"}
    assert record["stop_reason"] == "length"


def test_dai_retains_resize_and_manifest_references_not_carried_prompt_bytes():
    view = dai_model_view(
        image_ref=_ref("designator/crops/a.png"),
        width_px=3_000,
        height_px=1_001,
        system_prompt_ref=_ref("models/dai/system.txt"),
        query_prompt_ref=_ref("models/dai/query.txt"),
        generation_config_ref=_ref("models/dai/generation_config.json"),
    )
    assert DAI_MAX_WIDTH_PX == 1_500
    assert view["transform"]["target_width_px"] == 1_500
    assert view["transform"]["target_height_px"] == 501
    assert view["uncertainty_tokens_preserved"] == ["[UNCERTAIN]", "[CROSSED_OUT]"]
    assert view["prompts"]["system"] == _ref("models/dai/system.txt")
    assert set(view["prompts"]["system"]) == {"relative_path", "sha256"}


def test_chandra_intake_consumes_the_r2_blob_under_its_original_receipt():
    tree = _Tree()
    receipt = _ref("receipts/sha256/" + "b" * 64 + ".json")
    raw = b'{"html":"the retained Chandra response"}'
    response = retain_chandra_response(tree, raw, receipt)
    intake = chandra_capture_intake(tree, response_ref=response, receipt_ref=receipt)
    assert intake["response_ref"] == response
    assert intake["receipt_ref"] == receipt
    assert intake["raw_response_sha256"] == digest_bytes(raw)
    assert tree.receipts == [receipt]


def test_schedule_is_stage_major_chair_outer_act_inner_and_single_resident():
    schedule = stage_major_schedule(
        "parish-7",
        [{"act_id": "a2", "page_ordinal": 1}, {"act_id": "a1", "page_ordinal": 0}],
        ["attestator_3", "attestator_1"],
    )
    assert schedule == [
        {
            "policy": SCHEDULING_POLICY,
            "parish_id": "parish-7",
            "chair": "attestator_1",
            "act_id": "a1",
        },
        {
            "policy": SCHEDULING_POLICY,
            "parish_id": "parish-7",
            "chair": "attestator_1",
            "act_id": "a2",
        },
        {
            "policy": SCHEDULING_POLICY,
            "parish_id": "parish-7",
            "chair": "attestator_3",
            "act_id": "a1",
        },
        {
            "policy": SCHEDULING_POLICY,
            "parish_id": "parish-7",
            "chair": "attestator_3",
            "act_id": "a2",
        },
    ]
    with pytest.raises(SchemaRefusal, match="repeats a chair"):
        stage_major_schedule("parish-7", [{"act_id": "a1"}], ["attestator_1", "attestator_1"])
