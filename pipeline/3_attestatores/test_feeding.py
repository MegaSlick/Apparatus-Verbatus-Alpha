"""R3 fixture contracts: no model calls, downloads, or model roster edits."""

from __future__ import annotations

import json

import pytest
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

from common.chandra_custody import retain_chandra_response
from common.contracts.canonical import digest_bytes
from common.contracts.errors import SchemaRefusal
from common.contracts.stages import DESIGNATOR, writing_directory

PAGE_ID = "pg_fixture"
PAGE_ORDINAL = 0


def _ref(path: str) -> dict[str, str]:
    return {"relative_path": path, "sha256": "a" * 64}


class _Tree:
    """Mimics the run tree's real numbered stage directories (`writing_directory`),
    not the bare stage name — see common/chandra_custody.py's module docstring for
    why that distinction is load-bearing here."""

    def __init__(self, *, receipt_chair="designator_structure"):
        self.blobs = {}
        self.receipts = []
        self.receipt_chair = receipt_chair

    def put_blob(self, stage, data):
        digest = digest_bytes(data)
        path = f"{writing_directory(stage)}/blobs/sha256/{digest}"
        self.blobs[path] = data
        return digest, type("Published", (), {"relative_path": path})()

    def read_run_receipt(self, reference):
        self.receipts.append(reference)
        return {"chair": self.receipt_chair}

    def read_bytes(self, path):
        return self.blobs[path]


def _retain(tree, raw, receipt, *, page_id=PAGE_ID, page_ordinal=PAGE_ORDINAL):
    return retain_chandra_response(tree, raw, receipt, page_id=page_id, page_ordinal=page_ordinal)


def _intake(tree, stored, receipt, *, page_id=PAGE_ID, page_ordinal=PAGE_ORDINAL):
    return chandra_capture_intake(
        tree,
        page_id=page_id,
        page_ordinal=page_ordinal,
        response_ref=stored["response_ref"],
        receipt_ref=receipt,
        custody_ref=stored["custody_ref"],
    )


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
    stored = _retain(tree, raw, receipt)
    intake = _intake(tree, stored, receipt)
    assert intake["page_id"] == PAGE_ID
    assert intake["page_ordinal"] == PAGE_ORDINAL
    assert intake["response_ref"] == stored["response_ref"]
    assert intake["receipt_ref"] == receipt
    assert intake["custody_ref"] == stored["custody_ref"]
    assert intake["raw_response_sha256"] == digest_bytes(raw)
    assert tree.receipts == [receipt]


def test_chandra_intake_refuses_a_response_retained_under_a_different_receipt():
    """H3 forgery: two individually-valid references, mismatched pairing."""
    tree = _Tree()
    receipt_a = {"relative_path": "receipts/sha256/" + "b" * 64 + ".json", "sha256": "b" * 64}
    receipt_b = {"relative_path": "receipts/sha256/" + "c" * 64 + ".json", "sha256": "c" * 64}
    _retain(tree, b"call A's response", receipt_a)
    stored_b = _retain(tree, b"call B's unrelated response", receipt_b)
    with pytest.raises(SchemaRefusal, match="different receipt"):
        _intake(tree, stored_b, receipt_a)


def test_chandra_intake_refuses_a_receipt_reference_substituted_for_a_response_reference():
    """H3 forgery: swap the two reference roles."""
    tree = _Tree()
    receipt = _ref("receipts/sha256/" + "b" * 64 + ".json")
    stored = _retain(tree, b"a response", receipt)
    with pytest.raises(SchemaRefusal, match="does not name"):
        chandra_capture_intake(
            tree,
            page_id=PAGE_ID,
            page_ordinal=PAGE_ORDINAL,
            response_ref=receipt,
            receipt_ref=receipt,
            custody_ref=stored["custody_ref"],
        )
    with pytest.raises(SchemaRefusal, match="does not name"):
        chandra_capture_intake(
            tree,
            page_id=PAGE_ID,
            page_ordinal=PAGE_ORDINAL,
            response_ref=stored["response_ref"],
            receipt_ref=stored["response_ref"],
            custody_ref=stored["custody_ref"],
        )


def test_chandra_intake_refuses_a_tampered_response_blob():
    """H3 forgery: the retained bytes no longer match their sealed digest."""
    tree = _Tree()
    receipt = _ref("receipts/sha256/" + "b" * 64 + ".json")
    stored = _retain(tree, b"original response bytes", receipt)
    tree.blobs[stored["response_ref"]["relative_path"]] = b"tampered response bytes"
    with pytest.raises(SchemaRefusal, match="differs"):
        _intake(tree, stored, receipt)


@pytest.mark.parametrize("variant", ["duplicate-key", "whitespace", "unicode-escape"])
def test_chandra_intake_refuses_noncanonical_custody_json_bytes(variant):
    tree = _Tree()
    receipt = _ref("receipts/sha256/" + "b" * 64 + ".json")
    stored = _retain(tree, b"raw Chandra response", receipt)
    original = tree.blobs[stored["custody_ref"]["relative_path"]]
    parsed = json.loads(original)
    if variant == "duplicate-key":
        pair = f'"receipt_sha256":"{parsed["receipt_sha256"]}"'.encode()
        malformed = original.replace(pair, pair + b"," + pair, 1)
    elif variant == "whitespace":
        malformed = json.dumps(parsed, sort_keys=True).encode()
    else:
        malformed = original.replace(b'"schema"', b'"sch\\u0065ma"', 1)
    assert json.loads(malformed) == parsed, "the ordinary JSON reader sees the same object"
    digest, published = tree.put_blob(DESIGNATOR, malformed)
    forged = {
        **stored,
        "custody_ref": {"relative_path": published.relative_path, "sha256": digest},
    }
    with pytest.raises(SchemaRefusal, match="exact canonical JSON bytes"):
        _intake(tree, forged, receipt)


@pytest.mark.parametrize(
    ("page_id", "page_ordinal"),
    [("pg_other", PAGE_ORDINAL), (PAGE_ID, PAGE_ORDINAL + 1)],
)
def test_chandra_intake_refuses_custody_bound_to_a_different_page(page_id, page_ordinal):
    tree = _Tree()
    receipt = _ref("receipts/sha256/" + "b" * 64 + ".json")
    stored = _retain(tree, b"page-specific response", receipt)
    with pytest.raises(SchemaRefusal, match="different page"):
        _intake(tree, stored, receipt, page_id=page_id, page_ordinal=page_ordinal)


def test_chandra_intake_refuses_a_non_designator_receipt_chair():
    tree = _Tree(receipt_chair="attestator_1")
    receipt = _ref("receipts/sha256/" + "b" * 64 + ".json")
    stored = _retain(tree, b"response under the wrong serving role", receipt)
    with pytest.raises(SchemaRefusal, match="designator_structure"):
        _intake(tree, stored, receipt)


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
