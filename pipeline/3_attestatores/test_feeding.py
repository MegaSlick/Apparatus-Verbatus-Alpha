"""R3 fixture contracts: no model calls, downloads, or model roster edits."""

from __future__ import annotations

import json
import re
import time

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
    dai_generation,
    dai_model_view,
    dai_prompt,
    detect_repetition,
    execute_stage_major_schedule,
    retain_model_view,
    stage_major_schedule,
    validate_churro_xml,
    validate_dai_text,
)

from common.chandra_custody import retain_chandra_response
from common.contracts.canonical import digest_bytes, digest_of
from common.contracts.errors import SchemaRefusal
from common.contracts.stages import ATTESTATORES, DESIGNATOR, writing_directory
from common.runtree.store import BLOBS_DIR

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
        path = f"{writing_directory(stage)}/{BLOBS_DIR}/{digest}"
        self.blobs[path] = data
        return digest, type("Published", (), {"relative_path": path})()

    def read_run_receipt(self, reference):
        self.receipts.append(reference)
        return {"chair": self.receipt_chair}

    def read_bytes(self, path):
        # `RunTree.read_bytes` is a filesystem read, so an absent blob raises
        # FileNotFoundError (an OSError). A KeyError here would let a
        # missing-blob refusal pass in this suite and fail against the real
        # tree, because `_read_custody_bytes` catches OSError only.
        try:
            return self.blobs[path]
        except KeyError:
            raise FileNotFoundError(2, "No such file or directory", path) from None


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


@pytest.mark.parametrize(
    "raw",
    [
        b'<!DOCTYPE output [<!ENTITY x "y">]><output>&x;</output>',
        b'<?xml version="1.0"?><!doctype output><output>text</output>',
    ],
)
def test_churro_xml_refuses_a_doctype_before_the_parser_sees_it(raw):
    """A legitimate Churro response is one plain <output> element; a DTD is the
    door to entity tricks the validator has no reason to keep open. Escaped text
    (&lt;!DOCTYPE) never carries these bytes, so honest transcriptions pass."""
    with pytest.raises(SchemaRefusal, match="DOCTYPE"):
        validate_churro_xml(raw)


def test_churro_xml_refuses_a_response_that_is_not_raw_bytes():
    """A str here would slip past the DOCTYPE byte scan and still parse, making
    that refusal skippable by the caller's choice of type."""
    with pytest.raises(SchemaRefusal, match="not raw bytes"):
        validate_churro_xml("<output>text</output>")


def test_churro_xml_refuses_an_output_element_carrying_attributes_or_children():
    with pytest.raises(SchemaRefusal, match="plain <output> XML element"):
        validate_churro_xml(b'<output kind="decorated">text</output>')
    with pytest.raises(SchemaRefusal, match="plain <output> XML element"):
        validate_churro_xml(b"<output><child>text</child></output>")


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


def test_an_undecodable_churro_capture_records_uninspected_without_claiming_repetition():
    """An undecodable response was never inspected for repetition; the record
    says so as its own finding kind, and the transport's stop reason survives —
    an uninspected capture is a recorded fact, not a detected failure."""
    tree = _Tree()
    record = retain_model_view(
        tree,
        adapter="churro.v1",
        view={"prompt": churro_prompt(), "generation": churro_generation()},
        raw_response=b"\xff\xfe not utf-8 at all",
        transport_stop_reason="eos",
    )
    assert {"kind": "post-hoc-repetition-uninspected", "reason": "response is not UTF-8 text"} in (
        record["findings"]
    )
    assert record["stop_reason"] == "eos"


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


def _repetition_by_construction(raw: bytes) -> dict | None:
    """The straightforward reading of the same rule, built-string form and all.

    Kept here as the thing `detect_repetition`'s windowed counter must agree
    with: the shipped form exists only because building `unit * (repeats + 1)`
    on every step is quadratic in the response length, and an optimisation that
    is not pinned against the obvious version is a claim rather than a fact.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # Mirrors the shipped detector: an undecodable capture is reported as
        # uninspected, never silently passed.
        return {"kind": "post-hoc-repetition-uninspected", "reason": "response is not UTF-8 text"}
    normalized = re.sub(r"\s+", " ", text).strip()
    if len(normalized) < 24 * 3:
        return None
    for width in range(24, min(256, len(normalized) // 3) + 1):
        unit = normalized[-width:]
        repeats = 1
        while normalized.endswith(unit * (repeats + 1)):
            repeats += 1
        if repeats >= 3:
            return {"kind": "post-hoc-repetition", "unit_characters": width, "repeats": repeats}
    return None


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"a" * 71,
        b"a" * 72,
        b"a" * 1_000,
        b"ab" * 400,
        b"\xff\xfe not utf-8 at all",
        "un acte ordinaire qui ne se repète pas du tout, ligne par ligne".encode(),
        b"preamble that does not repeat, then " + b"the same tail again and again. " * 40,
        b"   spaced   out   " + b"repeated window of twenty-four+ chars " * 12,
        # Exactly at the boundary of the window sweep, and one repetition short.
        b"z" * 24 * 3,
        b"z" * (24 * 3 - 1),
    ],
)
def test_repetition_counting_matches_the_built_string_reading(raw):
    assert detect_repetition(raw) == _repetition_by_construction(raw)


def test_repetition_detection_stays_linear_on_a_long_degenerate_tail():
    """The case the windowed counter exists for: a large, wholly repeated tail.

    Nothing bounds a captured response's size, and the built-string form spent
    time quadratic in it precisely when the detector fires.
    """
    unit = b"the same twelve word tail over and over again "
    raw = unit * 100_000
    finding = detect_repetition(raw)
    # 99_999, not 100_000: whitespace normalization strips the final trailing
    # space, so the last window is one character short of the repeated unit.
    assert finding == {"kind": "post-hoc-repetition", "unit_characters": 46, "repeats": 99_999}
    # Same machine, same load, same input: the windowed counter must beat the
    # quadratic built-string reading it replaced by a wide margin. A ratio
    # between two measurements taken back to back is immune to a slow or busy
    # runner in a way no absolute wall-clock bound is (measured at this probe
    # size: ~0.04 s windowed vs ~0.58 s built-string, a 14x gap asserted at 3x).
    probe = unit * 20_000
    started = time.perf_counter()
    assert detect_repetition(probe) is not None
    windowed = time.perf_counter() - started
    started = time.perf_counter()
    assert _repetition_by_construction(probe) is not None
    built_string = time.perf_counter() - started
    assert windowed * 3 < built_string, (
        f"windowed {windowed:.3f}s vs built-string {built_string:.3f}s: the windowed "
        "counter no longer clearly beats the quadratic form it exists to replace"
    )


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


def test_dai_carried_request_bytes_and_uncertainty_tokens_are_not_normalized():
    prompt = dai_prompt()
    assert prompt == {
        "system": (
            "Tu es un assistant archiviste. Tu dois lire des actes issus de registres "
            "paroissiaux français, du 16è au 18è siècle. Extrais le texte de la marge, du "
            "corps de l'acte, et éventuellement les signatures.\n"
        ),
        "user": "Extrais le texte de ce document.\n",
    }
    # Byte-exact against the cited vendor files, trailing newline included: the
    # docstring's "byte-for-byte" claim is a checkable fact, not a description.
    assert len(prompt["system"].encode("utf-8")) == 206
    assert len(prompt["user"].encode("utf-8")) == 33
    assert digest_bytes(prompt["system"].encode("utf-8")) == (
        "b4e7d61d4f27f0aa46ba597ebfac3925b3ed87e72583def4bce2bd4f0393c333"
    )
    assert digest_bytes(prompt["user"].encode("utf-8")) == (
        "3a5cd8eb3263f2511d207f49f9933b1cf184e95fd7a9534871207d8d8b6a3489"
    )
    # Every carried value, not only the one that would be tempting to "fix":
    # `dai_generation`'s docstring says the shipped configuration crosses
    # unchanged, and a single-field check left the other eight free to drift
    # away from the cited file without any test noticing.
    assert dai_generation() == {
        "bos_token_id": 151_643,
        "do_sample": True,
        "eos_token_id": [151_645, 151_643],
        "pad_token_id": 151_643,
        "repetition_penalty": 1.05,
        "temperature": 0.1,
        "top_k": 1,
        "top_p": 0.001,
        "transformers_version": "5.2.0",
    }
    assert dai_generation()["do_sample"] is True
    response = "[UNCERTAIN]  ſ [CROSSED_OUT]"
    assert validate_dai_text(response.encode("utf-8")) == response


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
    [
        (500, 10_000, (204, 4_080)),
        (1_500, 3_000, (1_086, 2_172)),
        # Just past the 576:4096 crossover (design v2.1 s2 x the chosen height
        # ceiling): a pre-folded `width_px * DAI_MAX_HEIGHT_PX // height_px`
        # bound is a floor of a floor and previously undercut the true largest
        # feasible width by one pixel here (564 x 4088, area 2,305,632) against
        # the actual largest fit (565 x 4096, area 2,314,240 <= 2,359,296).
        (581, 4_212, (565, 4_096)),
    ],
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
    assert intake["schema"] == "attestatores-chandra-capture.v1"
    assert intake["raw_response_sha256"] == digest_bytes(raw)
    # The exact receipt reference was handed to the tree's verifier at both
    # ends of custody -- once at retain, once at intake. Verification itself is
    # the tree's own (read_run_receipt -> validate_receipt), proven in the
    # store suite; what custody must prove is that it asks, both times, with
    # the reference it was given.
    assert tree.receipts == [receipt, receipt]


@pytest.mark.parametrize("vanished", ["response_ref", "custody_ref"])
def test_chandra_intake_names_a_vanished_blob_instead_of_leaking_the_oserror(vanished):
    """P2-1's named refusal, proven against a double that fails like the real
    tree: `RunTree.read_bytes` raises FileNotFoundError for an absent blob, and
    only an OSError-shaped double can show the refusal is actually wired."""
    tree = _Tree()
    receipt = _ref("receipts/sha256/" + "b" * 64 + ".json")
    stored = _retain(tree, b"a response that will vanish", receipt)
    del tree.blobs[stored[vanished]["relative_path"]]
    with pytest.raises(SchemaRefusal, match="could not be read"):
        _intake(tree, stored, receipt)


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
    with pytest.raises(SchemaRefusal, match="response blob differs from its sealed reference"):
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


def test_schedule_refuses_a_repeated_act_for_the_same_reason_it_refuses_a_chair():
    """One duplicate act row in becomes one duplicate serving per chair out."""
    with pytest.raises(SchemaRefusal, match="repeats an act"):
        stage_major_schedule(
            "parish-7",
            [{"act_id": "a1", "page_ordinal": 0}, {"act_id": "a1", "page_ordinal": 1}],
            ["attestator_1", "attestator_2"],
        )


@pytest.mark.parametrize(
    ("acts", "chairs", "message"),
    [
        ([{"act_id": "a1"}], ["attestator_1", ""], "chair identity is blank"),
        ([{"act_id": "a1"}], ["attestator_1", 7], "chair identity is blank"),
        (["a1"], ["attestator_1"], "act has no identity"),
        ([{"act_id": "a1", "page_ordinal": "0"}], ["attestator_1"], "ordinal is not an integer"),
        ([{"act_id": "a1", "page_ordinal": True}], ["attestator_1"], "ordinal is not an integer"),
    ],
)
def test_schedule_refuses_malformed_rows_instead_of_failing_inside_a_sort(acts, chairs, message):
    """Each of these previously escaped as a bare TypeError or AttributeError."""
    with pytest.raises(SchemaRefusal, match=message):
        stage_major_schedule("parish-7", acts, chairs)


def test_stage_major_execution_refuses_a_schedule_that_serves_one_act_twice():
    schedule = stage_major_schedule("parish-7", [{"act_id": "a1"}], ["attestator_1"])
    residency = SingleChairResidency(lambda chair: chair, lambda *_: None)
    with pytest.raises(SchemaRefusal, match="serves one act twice"):
        execute_stage_major_schedule(
            [schedule[0], dict(schedule[0])], residency=residency, serve=lambda *_: None
        )
    assert residency.resident is None


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


def test_stage_major_execution_fails_closed_when_the_load_itself_fails():
    """A chair whose load raised is resident, not vacant: nothing may follow it.

    Distinct from the failed-unload case above. There the resource was known to
    have been loaded; here it may have been half loaded, and the reservation is
    deliberately taken before `load` so that the difference cannot be guessed at.
    """
    unloads = []

    def load(chair):
        raise RuntimeError("chair weights did not map")

    def unload(chair, resource):
        unloads.append((chair, resource))

    residency = SingleChairResidency(load, unload)
    with pytest.raises(RuntimeError, match="did not map"):
        execute_stage_major_schedule(
            stage_major_schedule("parish-7", [{"act_id": "a1"}], ["attestator_1"]),
            residency=residency,
            serve=lambda *_: None,
        )
    assert residency.resident == "attestator_1"
    assert unloads == [], "no unload may be claimed for a resource that never loaded"
    with pytest.raises(SchemaRefusal, match="while chair 'attestator_1' is resident"):
        execute_stage_major_schedule(
            stage_major_schedule("parish-7", [{"act_id": "a1"}], ["attestator_2"]),
            residency=residency,
            serve=lambda *_: None,
        )


def test_model_view_refuses_a_parser_it_cannot_run_instead_of_recording_pending():
    tree = _Tree()
    with pytest.raises(SchemaRefusal, match="does not run for adapter"):
        retain_model_view(
            tree,
            adapter="dai-atr.v1",
            view={},
            raw_response=b"native DAI text",
            transport_stop_reason="eos",
            parser="xml",
        )
    assert tree.blobs == {}
    unparsed = retain_model_view(
        tree,
        adapter="dai-atr.v1",
        view={},
        raw_response=b"native DAI text",
        transport_stop_reason="eos",
    )
    assert unparsed["parse"] == {"state": "not-requested", "parser": None}
    assert unparsed["raw_response_ref"]["relative_path"].startswith(
        f"{writing_directory(ATTESTATORES)}/{BLOBS_DIR}/"
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
