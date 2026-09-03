"""R3 fixture contracts: no model calls, downloads, or model roster edits."""

from __future__ import annotations

import importlib.util
import json
import re
import time
from pathlib import Path
from types import SimpleNamespace

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
    validate_dai_model_view,
    validate_dai_text,
)

from common.chairs.models import AbsentChair, ChairIdentity
from common.chandra_custody import retain_chandra_response
from common.contracts.canonical import digest_bytes, digest_of
from common.contracts.errors import SchemaRefusal
from common.contracts.stages import ATTESTATORES, DESIGNATOR, writing_directory
from common.native_witness import CHURRO_MAX_RESPONSE_BYTES
from common.runtree.store import BLOBS_DIR

PAGE_ID = "pg_fixture"
PAGE_ORDINAL = 0


def _load_attestatores():
    path = Path(__file__).resolve().parent / "run.py"
    spec = importlib.util.spec_from_file_location("attestatores_churro_capture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ref(path: str, digest: str = "a" * 64) -> dict[str, str]:
    return {"relative_path": path, "sha256": digest}


def _chair(role: str, *, adapter: str = "churro.v1", scope: str = "page") -> ChairIdentity:
    return ChairIdentity(
        role=role,
        source="local-repository",
        repo=None,
        path=role,
        revision=None,
        digest_manifest="a" * 64,
        manifest=f"{role}.json",
        adapter_of=None,
        serving_recipe="fixture",
        license_note="fixture",
        witness_adapter=adapter,
        witness_scope=scope,
    )


class _Tree:
    """Mimics the run tree's real numbered stage directories (`writing_directory`),
    not the bare stage name — see common/chandra_custody.py's module docstring for
    why that distinction is load-bearing here."""

    def __init__(self, *, receipt_chair="designator_structure"):
        self.blobs = {}
        self.put_calls = 0
        self.receipts = []
        self.receipt_chair = receipt_chair

    def put_blob(self, stage, data):
        self.put_calls += 1
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


@pytest.mark.parametrize(
    ("raw", "expected"),
    (
        (
            b"<output>Marie Anne, fille de<!-- scribe note --> Pierre</output>",
            "Marie Anne, fille de Pierre",
        ),
        (b"<output>AAA<!--one-->BBB<!--two-->CCC</output>", "AAABBBCCC"),
        (b"<output>AAA<?render x?>BBB</output>", "AAABBB"),
        (b"<output>AAA<![CDATA[BBB]]>CCC</output>", "AAABBBCCC"),
    ),
)
def test_a_comment_inside_the_output_element_does_not_shorten_the_transcription(raw, expected):
    """The whole reading must survive a node the parser does not keep.

    ElementTree drops comments and processing instructions, and the worry is
    that `root.text` would then stop at the first one: half an act's text gone
    while the capture still sealed as parsed, complete and untruncated, with
    nothing downstream able to see the loss (GOALS 2). It does not happen --
    the builder accumulates character data across a dropped node, so the text
    either side is joined -- and this pins that rather than assuming it, on
    every interpreter the matrix runs.

    It also pins why the obvious hardening is wrong. Retaining comments with
    `TreeBuilder(insert_comments=True)` would put them in `list(root)`, and the
    closed-shape check above refuses any `<output>` with children -- turning a
    correct reading that merely contains a comment into a refusal.
    """
    assert validate_churro_xml(raw) == expected


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
    assert record["findings"] == [
        {
            "kind": "post-hoc-repetition-uninspected",
            "reason": "response is not UTF-8 text",
            # No parse was asked for, so the raw bytes are all there was to look
            # at, and the finding says which view it failed to read.
            "inspected": "raw-response",
        }
    ]
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


def test_an_oversized_churro_response_is_retained_but_never_parsed_or_scanned():
    tree = _Tree()
    raw = b"<output>" + b"x" * CHURRO_MAX_RESPONSE_BYTES + b"</output>"

    record = retain_model_view(
        tree,
        adapter="churro.v1",
        view={"prompt": churro_prompt(), "generation": churro_generation()},
        raw_response=raw,
        transport_stop_reason="eos",
        parser="xml",
    )

    assert tree.blobs[record["raw_response_ref"]["relative_path"]] is raw
    assert record["parse"]["state"] == "failed"
    assert "exceeds the retained parsing limit" in record["parse"]["reason"]
    assert record["findings"] == [
        {
            "kind": "post-hoc-repetition-uninspected",
            "reason": record["parse"]["reason"],
            "inspected": "raw-response",
        }
    ]
    assert record["stop_reason"] == "partial-parse-failed"


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


def test_repetition_is_detected_in_a_COMPLETE_churro_response_envelope_and_all():
    """The XML envelope must not hide repetition in an otherwise complete response."""
    tree = _Tree()
    clause = "the same clause repeated over and over. "
    raw = f"<output>{clause * 6}</output>".encode()

    record = retain_model_view(
        tree,
        adapter="churro.v1",
        view={"prompt": churro_prompt(), "generation": churro_generation()},
        raw_response=raw,
        transport_stop_reason="eos",
        parser="xml",
    )

    assert record["parse"]["state"] == "parsed"
    assert record["findings"] == [
        {
            "kind": "post-hoc-repetition",
            "unit_characters": len(clause),
            # Normalization strips the final space, leaving five complete windows.
            "repeats": 5,
            "inspected": "parsed-text",
        }
    ]
    assert record["stop_reason"] == "partial-post-hoc-repetition-detected"
    assert tree.blobs[record["raw_response_ref"]["relative_path"]] == raw
    # The closing tag makes raw tail windows unequal.
    assert detect_repetition(raw) is None


def test_an_unparseable_capture_is_still_inspected_for_repetition_on_its_raw_bytes():
    """Without parsed text, repetition remains an independent raw-byte finding."""
    tree = _Tree()
    clause = "the same clause repeated over and over. "
    raw = f"<output>{clause * 6}".encode()

    record = retain_model_view(
        tree,
        adapter="churro.v1",
        view={"prompt": churro_prompt(), "generation": churro_generation()},
        raw_response=raw,
        transport_stop_reason="length",
        parser="xml",
    )

    assert record["parse"]["state"] == "failed"
    assert record["stop_reason"] == "partial-parse-failed"
    assert [finding["kind"] for finding in record["findings"]] == ["post-hoc-repetition"]
    assert record["findings"][0]["inspected"] == "raw-response"
    assert tree.blobs[record["raw_response_ref"]["relative_path"]] == raw


def test_churro_page_capture_is_full_page_xml_and_surfaces_transport_truncation():
    """The stage consumes one page response, never an act join or a retry."""
    attestatores = _load_attestatores()
    tree = _Tree()
    context = SimpleNamespace(
        tree=tree,
        scenario="churro-native",
        fixture={
            "churro_page_response": [
                {
                    "scenario": "churro-native",
                    "page_ordinal": 1,
                    "chair": "attestator_1",
                    "raw_xml": "<output>first act\nsecond act</output>",
                    "transport_stop_reason": "length",
                }
            ]
        },
    )

    result = attestatores.captured_churro_page_attempt(context, 1, "attestator_1", "churro.v1")

    assert result is not None
    attempt, capture = result
    assert attempt.native_payload == "first act\nsecond act"
    assert attempt.health["truncated"] is True
    assert attempt.health["truncation_basis"] == "trusted-response-boundary"
    assert capture["parse"] == {
        "state": "parsed",
        "parser": "xml",
        "text": "first act\nsecond act",
    }
    assert tree.blobs[capture["raw_response_ref"]["relative_path"]] == (
        b"<output>first act\nsecond act</output>"
    )
    assert tree.put_calls == 1, "one declared response must cross capture exactly once"


def test_churro_page_capture_keeps_repetition_finding_after_raw_capture(monkeypatch):
    attestatores = _load_attestatores()
    tree = _Tree()
    raw = "<output>complete captured text</output>"
    context = SimpleNamespace(
        tree=tree,
        scenario="churro-native",
        fixture={
            "churro_page_response": [
                {
                    "page_ordinal": 1,
                    "chair": "attestator_1",
                    "raw_xml": raw,
                    "transport_stop_reason": "eos",
                }
            ]
        },
    )

    def detector(observed):
        assert tree.blobs, "the response must be retained before detection"
        # The transcription, not the envelope: `</output>` inside every tail
        # window makes the real detector blind to a wholly repeated response.
        assert observed == b"complete captured text"
        return {"kind": "post-hoc-repetition", "unit_characters": 24, "repeats": 3}

    monkeypatch.setattr(feeding, "detect_repetition", detector)
    _, capture = attestatores.captured_churro_page_attempt(context, 1, "attestator_1", "churro.v1")

    assert capture["findings"] == [
        {
            "kind": "post-hoc-repetition",
            "unit_characters": 24,
            "repeats": 3,
            "inspected": "parsed-text",
        }
    ]
    assert tree.blobs[capture["raw_response_ref"]["relative_path"]] == raw.encode()


def test_churro_page_capture_of_malformed_xml_keeps_raw_bytes_and_is_unrecordable():
    """A received parse failure is unrecordable, not a no-response channel."""
    attestatores = _load_attestatores()
    tree = _Tree()
    raw = "<output>unterminated"
    context = SimpleNamespace(
        tree=tree,
        scenario="churro-native",
        fixture={
            "churro_page_response": [
                {
                    "page_ordinal": 1,
                    "chair": "attestator_1",
                    "raw_xml": raw,
                    "transport_stop_reason": "length",
                }
            ]
        },
    )

    result = attestatores.captured_churro_page_attempt(context, 1, "attestator_1", "churro.v1")

    assert result is not None
    attempt, capture = result
    assert attempt.outcome == "failed"
    assert attempt.native_payload is None
    assert attempt.health["recordable"] is False
    assert attempt.health["encoding"] == "invalid-or-unrecordable"
    for field in ("empty", "blank", "truncated", "characters"):
        assert attempt.health[field] is None
    assert attempt.health["truncation_basis"]
    assert tree.blobs[capture["raw_response_ref"]["relative_path"]] == raw.encode("utf-8")


def test_a_cut_off_empty_response_is_not_a_confirmed_blank_page():
    """Interruption cannot establish absence; partial characters remain evidence."""
    attestatores = _load_attestatores()

    def _capture(raw: str, stop: str):
        context = SimpleNamespace(
            tree=_Tree(),
            scenario="churro-native",
            fixture={
                "churro_page_response": [
                    {
                        "page_ordinal": 1,
                        "chair": "attestator_1",
                        "raw_xml": raw,
                        "transport_stop_reason": stop,
                    }
                ]
            },
        )
        return attestatores.captured_churro_page_attempt(context, 1, "attestator_1", "churro.v1")

    cut, _ = _capture("<output></output>", "length")
    assert cut.outcome == "failed"
    assert cut.native_payload == ""
    assert cut.health == {
        "native_type": "string",
        "encoding": "utf-8-json-native",
        "recordable": True,
        "empty": True,
        "blank": True,
        "truncated": True,
        "characters": 0,
        "truncation_basis": "trusted-response-boundary",
    }
    assert "not a confirmed blank page" in cut.reason

    # A normally completed empty response is evidence of reported absence.
    finished, _ = _capture("<output></output>", "eos")
    assert finished.outcome == "genuinely-empty"
    assert finished.native_payload == ""
    assert finished.health["recordable"] is True
    assert finished.health["truncated"] is False

    partial, _ = _capture("<output>half an act and then</output>", "length")
    assert partial.outcome == "read"
    assert partial.native_payload == "half an act and then"
    assert partial.health["truncated"] is True


def test_a_declared_response_no_page_chair_could_be_asked_for_is_refused():
    """Unreachable declarations refuse; absent occupants remain roster facts."""
    attestatores = _load_attestatores()

    def _context(chair: str, page_ordinal: int, chairs: dict):
        return SimpleNamespace(
            scenario="churro-native",
            witness_chairs=sorted(chairs),
            registry=SimpleNamespace(config=SimpleNamespace(chairs=chairs)),
            fixture={
                "page": [{"ordinal": 1}, {"ordinal": 2}],
                "churro_page_response": [
                    {
                        "scenario": "churro-native",
                        "page_ordinal": page_ordinal,
                        "chair": chair,
                        "raw_xml": "<output>x</output>",
                        "transport_stop_reason": "eos",
                    }
                ],
            },
        )

    page_chairs = {"attestator_1"}
    absent = AbsentChair(role="attestator_3", reason="declared absent for this run")

    with pytest.raises(attestatores.SchemaRefusal, match="does not seal"):
        attestatores.validate_declared_churro_page_responses(
            _context(
                "attestator_2",
                1,
                {
                    "attestator_1": _chair("attestator_1"),
                    "attestator_2": _chair("attestator_2", scope="act"),
                },
            ),
            page_chairs,
        )
    with pytest.raises(attestatores.SchemaRefusal, match="does not declare"):
        attestatores.validate_declared_churro_page_responses(
            _context("attestator_1", 9, {"attestator_1": _chair("attestator_1")}),
            page_chairs,
        )
    attestatores.validate_declared_churro_page_responses(
        _context(
            "attestator_3",
            1,
            {"attestator_1": _chair("attestator_1"), "attestator_3": absent},
        ),
        page_chairs,
    )


def test_churro_declaration_preflight_allows_one_default_overridden_by_one_scenario_row():
    """Validation must preserve the lookup precedence it documents."""
    attestatores = _load_attestatores()
    chair = _chair("attestator_1")
    rows = [
        {
            "page_ordinal": 1,
            "chair": "attestator_1",
            "raw_xml": "<output>default</output>",
            "transport_stop_reason": "eos",
        },
        {
            "scenario": "churro-native",
            "page_ordinal": 1,
            "chair": "attestator_1",
            "raw_xml": "<output>scoped</output>",
            "transport_stop_reason": "eos",
        },
    ]
    context = SimpleNamespace(
        scenario="churro-native",
        witness_chairs=["attestator_1"],
        registry=SimpleNamespace(config=SimpleNamespace(chairs={"attestator_1": chair})),
        fixture={"page": [{"ordinal": 1}], "churro_page_response": rows},
    )

    attestatores.validate_declared_churro_page_responses(context, {"attestator_1"})
    assert attestatores.churro_page_capture(context, 1, "attestator_1") is rows[1]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda row: row.pop("raw_xml"), "lacks required field"),
        (lambda row: row.update(transport_stop_reason="network-error"), "unknown transport"),
        (lambda row: row.update(raw_xml="\ud800"), "not valid UTF-8"),
    ],
)
def test_churro_declaration_preflight_names_malformed_transport_facts_even_for_an_absent_chair(
    mutate, message
):
    """An absent occupant does not make its fixture declaration schema-free."""
    attestatores = _load_attestatores()
    row = {
        "page_ordinal": 1,
        "chair": "attestator_3",
        "raw_xml": "<output>x</output>",
        "transport_stop_reason": "eos",
    }
    mutate(row)
    absent = AbsentChair(role="attestator_3", reason="fixture absence")
    context = SimpleNamespace(
        scenario="churro-native",
        witness_chairs=["attestator_3"],
        registry=SimpleNamespace(config=SimpleNamespace(chairs={"attestator_3": absent})),
        fixture={"page": [{"ordinal": 1}], "churro_page_response": [row]},
    )
    with pytest.raises(attestatores.SchemaRefusal, match=message):
        attestatores.validate_declared_churro_page_responses(context, set())


def test_churro_declarations_are_checked_in_the_no_write_attempt_preflight():
    """A bad page row refuses before the caller can enter `attempt_pass`."""
    attestatores = _load_attestatores()
    chair = _chair("attestator_1", adapter="not-churro.v1")
    context = SimpleNamespace(
        scenario="churro-native",
        witness_chairs=["attestator_1"],
        registry=SimpleNamespace(config=SimpleNamespace(chairs={"attestator_1": chair})),
        fixture={
            "page": [{"ordinal": 1}],
            "churro_page_response": [
                {
                    "page_ordinal": 1,
                    "chair": "attestator_1",
                    "raw_xml": "<output>x</output>",
                    "transport_stop_reason": "eos",
                }
            ],
        },
    )
    index = attestatores.AttemptIndex(False, {}, {})

    with pytest.raises(attestatores.SchemaRefusal, match="different model boundary"):
        attestatores.preflight_appendable_ordinals(
            context, [], 1, {}, index, resume_incomplete_pass=False
        )


def test_one_scenarios_declared_response_is_not_another_scenarios_default():
    """Only an unscoped row is a default; other scenario rows are inaccessible."""
    attestatores = _load_attestatores()
    rows = [
        {
            "scenario": "churro-native",
            "page_ordinal": 1,
            "chair": "attestator_1",
            "raw_xml": "<output>other scenario</output>",
            "transport_stop_reason": "eos",
        },
        {
            "scenario": "churro-native-two",
            "page_ordinal": 1,
            "chair": "attestator_1",
            "raw_xml": "<output>a third scenario</output>",
            "transport_stop_reason": "eos",
        },
    ]
    context = SimpleNamespace(scenario="happy", fixture={"churro_page_response": rows})
    assert attestatores.churro_page_capture(context, 1, "attestator_1") is None

    context.scenario = "churro-native"
    assert attestatores.churro_page_capture(context, 1, "attestator_1") is rows[0]

    rows.append(
        {
            "page_ordinal": 1,
            "chair": "attestator_1",
            "raw_xml": "<output>the default</output>",
            "transport_stop_reason": "eos",
        }
    )
    context.scenario = "happy"
    assert attestatores.churro_page_capture(context, 1, "attestator_1") is rows[2]
    context.scenario = "churro-native"
    assert attestatores.churro_page_capture(context, 1, "attestator_1") is rows[0]


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
    # Vendor prompt bytes are pinned including trailing newlines because any
    # character change alters the trained request framing.
    assert len(prompt["system"].encode("utf-8")) == 206
    assert len(prompt["user"].encode("utf-8")) == 33
    assert digest_bytes(prompt["system"].encode("utf-8")) == (
        "b4e7d61d4f27f0aa46ba597ebfac3925b3ed87e72583def4bce2bd4f0393c333"
    )
    assert digest_bytes(prompt["user"].encode("utf-8")) == (
        "3a5cd8eb3263f2511d207f49f9933b1cf184e95fd7a9534871207d8d8b6a3489"
    )
    # Every vendor value is pinned; changing any one creates a local decoding
    # policy instead of reproducing the shipped configuration.
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
        # This crossover distinguishes predicate search from a pre-floored
        # height bound: 565x4096 fits, while nested flooring chooses only 564.
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


def test_dai_identity_view_accepts_one_image_under_two_stage_owned_paths():
    """The identity rule is about bytes; the two paths are about who owns them.

    On the no-resize path -- every act crop in the reference fixture -- DAI's
    `adapter-crop` is `crop_png` of the same sealed page at the same bounds as
    the Designator's proposal crop, so the two references carry one digest.
    They do not carry one path: the proposal crop lives under `2_designator/`,
    and every image a witness is actually shown is inventoried in this stage's
    own content-addressed store. Held to the whole reference dict, this rule
    refused a genuine DAI act after its response had already come back.
    """

    digest = "a" * 64
    view = dai_model_view(
        source_image_ref=_ref("2_designator/crops/small.png", digest),
        model_image_ref=_ref(f"3_attestatores/blobs/sha256/{digest}", digest),
        width_px=1_000,
        height_px=1_000,
        system_prompt_ref=_ref("models/dai/system.txt"),
        query_prompt_ref=_ref("models/dai/query.txt"),
        generation_config_ref=_ref("models/dai/generation_config.json"),
    )
    assert view["transform"]["kind"] == "identity"
    # Both references survive verbatim: the record shows the one set of bytes
    # under each store that holds it, rather than collapsing them to one name.
    assert view["source_image_ref"]["relative_path"] == "2_designator/crops/small.png"
    assert view["model_image_ref"]["relative_path"] == f"3_attestatores/blobs/sha256/{digest}"
    assert validate_dai_model_view(view) is view


@pytest.mark.parametrize(
    "unsafe_path",
    ["/etc/passwd", "../outside", "prompts/../../outside"],
)
def test_dai_model_view_refuses_reference_paths_that_escape_the_run_tree(unsafe_path):
    with pytest.raises(SchemaRefusal, match="reference path escapes the run tree"):
        dai_model_view(
            source_image_ref=_ref(unsafe_path),
            model_image_ref=_ref(unsafe_path),
            width_px=1_000,
            height_px=1_000,
            system_prompt_ref=_ref("models/dai/system.txt"),
            query_prompt_ref=_ref("models/dai/query.txt"),
            generation_config_ref=_ref("models/dai/generation_config.json"),
        )


def test_dai_retention_refuses_an_image_limits_digest_that_was_not_compared():
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
    valid = retain_model_view(
        _Tree(),
        adapter="dai.v1",
        view=view,
        raw_response=b"native DAI text",
        transport_stop_reason="eos",
        parser="text",
    )
    assert valid["parse"] == {"state": "parsed", "parser": "text", "text": "native DAI text"}

    view["image_limits_sha256"] = "b" * 64
    tree = _Tree()

    with pytest.raises(SchemaRefusal, match="image-limits digest does not match"):
        retain_model_view(
            tree,
            adapter="dai.v1",
            view=view,
            raw_response=b"native DAI text",
            transport_stop_reason="eos",
            parser="text",
        )
    assert tree.blobs == {}


def test_dai_model_view_refuses_rehashed_limits_that_change_the_sealed_ceiling():
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
    view["image_limits"]["max_width_px"] += 1
    view["image_limits_sha256"] = digest_of(view["image_limits"])

    with pytest.raises(SchemaRefusal, match="differ from the sealed executable limits"):
        validate_dai_model_view(view)


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


def test_unload_failure_cannot_mask_a_security_refusal_from_the_resident_body():
    def unload_fails(*_args):
        raise RuntimeError("unload not verified")

    residency = SingleChairResidency(lambda chair: chair, unload_fails)

    with pytest.raises(SchemaRefusal, match="security refusal survives cleanup") as caught:
        with residency.occupy("attestator_1"):
            raise SchemaRefusal("security refusal survives cleanup")

    assert isinstance(caught.value.__cause__, RuntimeError)
    assert str(caught.value.__cause__) == "unload not verified"
    assert residency.resident == "attestator_1"


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
