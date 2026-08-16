"""R3 fixture contracts: no model calls, downloads, or model roster edits."""

from __future__ import annotations

import json

import feeding
import pytest
from feeding import (
    CHURRO_OUTPUT_TOKENS,
    DAI_MAX_HEIGHT_PX,
    DAI_MAX_TOTAL_PIXELS,
    DAI_MAX_WIDTH_PX,
    SCHEDULING_POLICY,
    SingleChairResidency,
    chandra_capture_intake,
    churro_generation,
    churro_prompt,
    dai_model_view,
    execute_stage_major_schedule,
    retain_model_view,
    stage_major_schedule,
)

from common.chandra_custody import retain_chandra_response
from common.contracts.canonical import digest_bytes, digest_of
from common.contracts.errors import SchemaRefusal
from common.contracts.stages import DESIGNATOR, writing_directory

PAGE_ID = "pg_fixture"
PAGE_ORDINAL = 0


def _ref(path: str, digest: str = "a" * 64) -> dict[str, str]:
    return {"relative_path": path, "sha256": digest}


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


def test_churro_prompt_retains_the_trained_two_message_xml_bytes():
    prompt = churro_prompt()
    assert set(prompt) == {"system", "user"}
    assert digest_bytes(prompt["system"].encode("utf-8")) == (
        "ee91b159b30493ae43ee035079114debdf20d651b40c4cd59d70c645d02ff704"
    )
    assert digest_bytes(prompt["user"].encode("utf-8")) == (
        "048a11aafd9fdac9e28a82d86b0554d43b22937f016246292bbc0c1250c318ea"
    )
    assert "<output>\nextracted text here\n</output>" in prompt["user"]
    assert "ſ" in prompt["user"] and "а" in prompt["user"]


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


def test_churro_parse_normalization_is_harmless_because_raw_bytes_are_retained():
    tree = _Tree()
    raw = b"<output>line one\r\nRen&#233;</output>"
    record = retain_model_view(
        tree,
        adapter="churro.v1",
        view={"prompt": churro_prompt(), "generation": churro_generation()},
        raw_response=raw,
        transport_stop_reason="eos",
        parser="xml",
    )
    assert record["parse"]["text"] == "line one\nRené"
    assert tree.blobs[record["raw_response_ref"]["relative_path"]] == raw


def test_repetition_detection_observes_only_bytes_already_captured(monkeypatch):
    tree = _Tree()
    raw = b"completed response bytes before any detector runs"
    observations = []

    def detector(observed):
        observations.append(observed)
        assert observed is raw
        assert raw in tree.blobs.values(), "capture must precede every detector call"
        return {"kind": "post-hoc-repetition", "unit_characters": 24, "repeats": 3}

    monkeypatch.setattr(feeding, "detect_repetition", detector)
    record = retain_model_view(
        tree,
        adapter="churro.v1",
        view={"prompt": churro_prompt(), "generation": churro_generation()},
        raw_response=raw,
        transport_stop_reason="eos",
    )
    assert observations == [raw]
    assert tree.blobs[record["raw_response_ref"]["relative_path"]] == raw


def test_dai_retains_resize_and_manifest_references_not_carried_prompt_bytes():
    view = dai_model_view(
        source_image_ref=_ref("designator/crops/a.png"),
        model_image_ref=_ref("attestatores/model-views/a.jpg", "b" * 64),
        width_px=3_000,
        height_px=1_001,
        system_prompt_ref=_ref("models/dai/system.txt"),
        query_prompt_ref=_ref("models/dai/query.txt"),
        generation_config_ref=_ref("models/dai/generation_config.json"),
    )
    assert DAI_MAX_WIDTH_PX == 1_500
    assert DAI_MAX_HEIGHT_PX == 4_096
    assert DAI_MAX_TOTAL_PIXELS == 2_359_296
    assert view["transform"]["target_width_px"] == 1_500
    assert view["transform"]["target_height_px"] == 500
    assert view["model_image_ref"] == _ref("attestatores/model-views/a.jpg", "b" * 64)
    assert view["transform"]["resampler"] == "pillow-lanczos"
    assert view["image_limits_sha256"] == digest_of(view["image_limits"])
    assert view["uncertainty_tokens_preserved"] == ["[UNCERTAIN]", "[CROSSED_OUT]"]
    assert view["prompts"]["system"] == _ref("models/dai/system.txt")
    assert set(view["prompts"]["system"]) == {"relative_path", "sha256"}


def test_every_dai_ceiling_seals_where_it_came_from():
    """A sealed ceiling states its source, and a chosen one says it was chosen."""
    view = dai_model_view(
        source_image_ref=_ref("designator/crops/a.png"),
        model_image_ref=_ref("attestatores/model-views/a.jpg", "b" * 64),
        width_px=3_000,
        height_px=1_001,
        system_prompt_ref=_ref("models/dai/system.txt"),
        query_prompt_ref=_ref("models/dai/query.txt"),
        generation_config_ref=_ref("models/dai/generation_config.json"),
    )
    limits = view["image_limits"]
    assert limits["schema"] == "dai-image-limits.v2"
    ceilings = set(limits) - {"schema", "sources"}
    assert ceilings == set(limits["sources"]), "every ceiling names a source, and only ceilings do"
    assert all(limits["sources"][name].strip() for name in ceilings)
    # The two sourced ceilings name where they were read; the chosen one names
    # itself as chosen and the arithmetic that fixes its value.
    assert "serve_dai.sh" in limits["sources"]["max_total_pixels"]
    assert "design v2.1" in limits["sources"]["max_width_px"]
    assert "no model source" in limits["sources"]["max_height_px"]
    assert DAI_MAX_HEIGHT_PX * 576 == DAI_MAX_TOTAL_PIXELS
    assert view["image_limits_sha256"] == digest_of(limits)


@pytest.mark.parametrize(
    ("width_px", "height_px", "expected"),
    [(500, 10_000, (204, 4_080)), (1_500, 3_000, (1_086, 2_172))],
)
def test_dai_resize_applies_height_and_total_pixel_ceilings(width_px, height_px, expected):
    view = dai_model_view(
        source_image_ref=_ref("designator/crops/tall.png"),
        model_image_ref=_ref("attestatores/model-views/tall.jpg", "b" * 64),
        width_px=width_px,
        height_px=height_px,
        system_prompt_ref=_ref("models/dai/system.txt"),
        query_prompt_ref=_ref("models/dai/query.txt"),
        generation_config_ref=_ref("models/dai/generation_config.json"),
    )
    target = (view["transform"]["target_width_px"], view["transform"]["target_height_px"])
    assert target == expected
    assert target[0] <= DAI_MAX_WIDTH_PX
    assert target[1] <= DAI_MAX_HEIGHT_PX
    assert target[0] * target[1] <= DAI_MAX_TOTAL_PIXELS


def test_dai_identity_view_requires_the_exact_source_image_reference():
    source = _ref("designator/crops/small.png")
    view = dai_model_view(
        source_image_ref=source,
        model_image_ref=source,
        width_px=1_000,
        height_px=1_000,
        system_prompt_ref=_ref("models/dai/system.txt"),
        query_prompt_ref=_ref("models/dai/query.txt"),
        generation_config_ref=_ref("models/dai/generation_config.json"),
    )
    assert view["transform"]["kind"] == "identity"
    assert view["model_image_ref"] == source
    with pytest.raises(SchemaRefusal, match="identity transform"):
        dai_model_view(
            source_image_ref=source,
            model_image_ref=_ref("attestatores/model-views/different.jpg", "b" * 64),
            width_px=1_000,
            height_px=1_000,
            system_prompt_ref=_ref("models/dai/system.txt"),
            query_prompt_ref=_ref("models/dai/query.txt"),
            generation_config_ref=_ref("models/dai/generation_config.json"),
        )


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
    # Verified at both ends of custody: once at retain, once at intake.
    assert tree.receipts == [receipt, receipt]


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
    """Intake refuses the wrong chair even for custody that was sealed honestly.

    The write half now refuses the same receipt (its own test lives in the R2
    suite), so this seals the response under the Designator's chair first and
    then changes what the receipt says -- otherwise the read-side check would be
    pinned only by a state the writer can no longer produce.
    """
    tree = _Tree()
    receipt = _ref("receipts/sha256/" + "b" * 64 + ".json")
    stored = _retain(tree, b"response under the wrong serving role", receipt)
    tree.receipt_chair = "attestator_1"
    with pytest.raises(SchemaRefusal, match="designator_structure"):
        _intake(tree, stored, receipt)


def test_schedule_is_stage_major_chair_outer_act_inner_and_refuses_duplicate_chairs():
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


def test_stage_major_execution_never_exposes_two_resident_chairs():
    schedule = stage_major_schedule(
        "parish-7",
        [{"act_id": "a2", "page_ordinal": 1}, {"act_id": "a1", "page_ordinal": 0}],
        ["attestator_2", "attestator_1"],
    )
    loaded = set()
    events = []

    def load(chair):
        assert not loaded
        loaded.add(chair)
        events.append(("load", chair))
        return {"chair": chair}

    def unload(chair, resource):
        assert resource["chair"] == chair
        assert loaded == {chair}
        loaded.remove(chair)
        events.append(("unload", chair))

    residency = SingleChairResidency(load, unload)

    def serve(resource, row):
        assert residency.resident == row["chair"] == resource["chair"]
        assert loaded == {row["chair"]}
        with pytest.raises(SchemaRefusal, match="while chair"):
            with residency.occupy("attestator_9"):
                raise AssertionError("a nested second chair must never load")
        events.append(("serve", row["chair"], row["act_id"]))
        return row["act_id"]

    assert execute_stage_major_schedule(schedule, residency=residency, serve=serve) == [
        "a1",
        "a2",
        "a1",
        "a2",
    ]
    assert loaded == set()
    assert residency.resident is None
    assert events == [
        ("load", "attestator_1"),
        ("serve", "attestator_1", "a1"),
        ("serve", "attestator_1", "a2"),
        ("unload", "attestator_1"),
        ("load", "attestator_2"),
        ("serve", "attestator_2", "a1"),
        ("serve", "attestator_2", "a2"),
        ("unload", "attestator_2"),
    ]


@pytest.mark.parametrize("failing_act", ["a1", "a2"])
def test_stage_major_execution_unloads_before_propagating_an_act_failure(failing_act):
    schedule = stage_major_schedule(
        "parish-7",
        [{"act_id": "a1", "page_ordinal": 0}, {"act_id": "a2", "page_ordinal": 1}],
        ["attestator_1"],
    )
    loaded = set()

    def load(chair):
        loaded.add(chair)
        return chair

    def unload(chair, resource):
        assert resource == chair
        loaded.remove(chair)

    residency = SingleChairResidency(load, unload)

    def serve(_resource, row):
        if row["act_id"] == failing_act:
            raise RuntimeError("fixture act failure")

    with pytest.raises(RuntimeError, match="fixture act failure"):
        execute_stage_major_schedule(schedule, residency=residency, serve=serve)
    assert loaded == set()
    assert residency.resident is None


def test_stage_major_execution_refuses_reentry_and_fails_closed_on_unload_failure():
    loaded = set()

    def load(chair):
        assert not loaded
        loaded.add(chair)
        return chair

    def unload(_chair, _resource):
        raise RuntimeError("unload not verified")

    residency = SingleChairResidency(load, unload)
    schedule = stage_major_schedule("parish-7", [{"act_id": "a1"}], ["attestator_1"])
    with pytest.raises(RuntimeError, match="unload not verified"):
        execute_stage_major_schedule(schedule, residency=residency, serve=lambda *_: None)
    assert residency.resident == "attestator_1"
    assert loaded == {"attestator_1"}
    with pytest.raises(SchemaRefusal, match="while chair 'attestator_1' is resident"):
        execute_stage_major_schedule(
            stage_major_schedule("parish-7", [{"act_id": "a1"}], ["attestator_2"]),
            residency=residency,
            serve=lambda *_: None,
        )


def test_stage_major_execution_refuses_a_schedule_that_returns_to_a_prior_chair():
    schedule = stage_major_schedule(
        "parish-7", [{"act_id": "a1"}, {"act_id": "a2"}], ["attestator_1", "attestator_2"]
    )
    tampered = [schedule[0], schedule[2], schedule[1], schedule[3]]
    residency = SingleChairResidency(lambda chair: chair, lambda *_: None)
    with pytest.raises(SchemaRefusal, match="returns to an unloaded chair"):
        execute_stage_major_schedule(tampered, residency=residency, serve=lambda *_: None)
    assert residency.resident is None
