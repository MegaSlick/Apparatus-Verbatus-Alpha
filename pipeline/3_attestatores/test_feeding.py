"""R3 fixture contracts: no model calls, downloads, or model roster edits."""

from __future__ import annotations

import re
import time
from types import SimpleNamespace

import churro
import feeding
import pytest
from feeding import (
    CHURRO_OUTPUT_TOKENS,
    DAI_FORMAT_CAPABILITIES,
    DAI_MAX_WIDTH_PX,
    churro_generation,
    dai_generation,
    dai_generation_accounting,
    dai_model_view,
    dai_prompt,
    detect_repetition,
    retain_model_view,
    validate_dai_generation_accounting,
    validate_dai_model_view,
    validate_dai_text,
)

from common.chairs.models import AbsentChair, ChairIdentity
from common.contracts.canonical import digest_bytes, digest_of
from common.contracts.errors import SchemaRefusal
from common.contracts.stages import ATTESTATORES, writing_directory
from common.native_witness import CHURRO_MAX_RESPONSE_BYTES, validate_vendor_identity
from common.runtree.store import BLOBS_DIR
from common.stage import StageContext
from conftest import load_stage


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


class _Context(SimpleNamespace):
    stage = ATTESTATORES
    sealed = False
    retain = StageContext.retain


class _Tree:
    """Mimics the run tree's real numbered stage directories (`writing_directory`),
    not the bare stage name, so a retained reference's path is the one the
    shared blob-reference validators close on."""

    def __init__(self):
        self.blobs = {}
        self.put_calls = 0

    def put_blob(self, stage, data):
        self.put_calls += 1
        digest = digest_bytes(data)
        path = f"{writing_directory(stage)}/{BLOBS_DIR}/{digest}"
        self.blobs[path] = data
        return digest, type("Published", (), {"relative_path": path})()

    def read_bytes(self, path):
        # `RunTree.read_bytes` is a filesystem read, so an absent blob raises
        # FileNotFoundError (an OSError). A KeyError here would let a
        # missing-blob refusal pass in this suite and fail against the real
        # tree, where callers catch OSError only.
        try:
            return self.blobs[path]
        except KeyError:
            raise FileNotFoundError(2, "No such file or directory", path) from None


# --- Churro on its vendor's own grammar --------------------------------------
#
# The chair carries no prompt bytes of this repository's any more: what it is
# asked is one of two vendor-attested system strings in
# `common/churro_document.py`, resolved by name through `churro.FRAMINGS`. What
# this module still owns for the chair is the retention seam -- one parser name,
# the grammar's findings beside the post-hoc scan's, and the vendor pin on the
# record.

_DOCUMENT = b"<HistoricalDocument><Page><Body><Line>read</Line></Body></Page></HistoricalDocument>"


def _churro_view() -> dict:
    """The prompt and generation view a served Churro chair retains."""
    return {"prompt": churro.prompt(), "generation": churro_generation()}


def test_churro_records_its_declared_bound_and_detects_repetition_after_complete_capture():
    tree = _Tree()
    raw = b"a" * 72
    record = retain_model_view(
        _Context(tree=tree),
        adapter="churro.v1",
        view=_churro_view(),
        raw_response=raw,
        transport_stop_reason="eos",
        parser="xml",
    )
    # The vendor's own `DEFAULT_OCR_MAX_TOKENS` (Churro v0.3.0), not a number of ours.
    assert CHURRO_OUTPUT_TOKENS == 25_000
    # The retained view actually carries that bound, not only the module
    # constant this test could otherwise check in isolation from it.
    assert record["view"]["generation"]["max_new_tokens"] == CHURRO_OUTPUT_TOKENS
    assert record["raw_response_ref"]["sha256"] == digest_bytes(raw)
    assert record["findings"][0]["kind"] == "post-hoc-repetition"
    # A body that offers no grammar at all is the plain reading-order text the
    # paper-era harness itself expected, so it reads rather than being thrown
    # away; the repeated tail is still a finding beside it.
    assert record["parse"]["state"] == "parsed"
    assert record["stop_reason"] == "partial-post-hoc-repetition-detected"
    assert tree.blobs[record["raw_response_ref"]["relative_path"]] == raw


@pytest.mark.parametrize(
    ("raw", "parser", "state", "text_view"),
    [
        (_DOCUMENT, "xml", "parsed", "churro-historical-document-text.v2"),
        (
            b"a" * (CHURRO_MAX_RESPONSE_BYTES + 1),
            "xml",
            "failed",
            "churro-historical-document-text.v2",
        ),
        (_DOCUMENT, None, "not-requested", None),
    ],
)
def test_a_churro_capture_records_the_text_view_its_grammar_reads_under(
    raw, parser, state, text_view
):
    """Named whatever the parse state, so a later build can refuse it by that name."""
    record = retain_model_view(
        _Context(tree=_Tree()),
        adapter="churro.v1",
        view=_churro_view(),
        raw_response=raw,
        transport_stop_reason="eos",
        parser=parser,
    )
    assert record["parse"]["state"] == state
    assert record.get("text_view") == text_view


def test_an_undecodable_churro_capture_records_uninspected_without_claiming_repetition():
    """An undecodable response was never inspected for repetition; the record
    says so as its own finding kind, and the transport's stop reason survives —
    an uninspected capture is a recorded fact, not a detected failure."""
    tree = _Tree()
    record = retain_model_view(
        _Context(tree=tree),
        adapter="churro.v1",
        view=_churro_view(),
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


def test_the_chair_is_asked_in_the_vendors_own_system_only_framing():
    prompt = churro.prompt()
    assert set(prompt) == {"system"}
    assert prompt["system"] == "Transcribe the entirety of this historical document to XML format."
    assert digest_bytes(prompt["system"].encode("utf-8")) == (
        "13592f5580805cf12d2aa14c963b872e3a4e6a5834e5d2afd7b86effd42a8b4d"
    )


def test_the_runnable_parser_names_are_exactly_the_ones_the_capture_contract_admits():
    """Two modules name this chair's parsers; a drift would strand one posture.

    `feeding` decides which `(adapter, parser)` pairs may be asked for, and
    `common/native_witness.py::CHURRO_PARSERS` decides which a retained capture
    may carry. A name in one and not the other is either a parse this boundary
    can ask for and no record may hold, or a record shape nothing can produce.
    """
    from common.native_witness import CHURRO_PARSERS

    names = {parser for adapter, parser in feeding._RUNNABLE_PARSERS if adapter == "churro.v1"}
    assert names == set(CHURRO_PARSERS) == {"xml"}


def test_the_one_churro_parser_name_runs_and_no_other_does():
    tree = _Tree()
    record = retain_model_view(
        _Context(tree=tree),
        adapter="churro.v1",
        view=_churro_view(),
        raw_response=_DOCUMENT,
        transport_stop_reason="eos",
        parser="xml",
    )
    assert record["parse"] == {"state": "parsed", "parser": "xml", "text": "read"}
    for retired in ("churro", "json", "html"):
        with pytest.raises(SchemaRefusal, match="does not run for adapter"):
            retain_model_view(
                _Context(tree=tree),
                adapter="churro.v1",
                view=_churro_view(),
                raw_response=_DOCUMENT,
                transport_stop_reason="eos",
                parser=retired,
            )


def test_the_vendor_pin_travels_on_every_churro_capture_beside_the_model_identity():
    """Provenance's other half once the chair runs the vendor's own system.

    Keyed on the bytes the view retained rather than on a framing name, so a
    record cannot name a pin for a prompt it did not send.
    """
    tree = _Tree()
    record = retain_model_view(
        _Context(tree=tree),
        adapter="churro.v1",
        view=_churro_view(),
        raw_response=_DOCUMENT,
        transport_stop_reason="eos",
        parser="xml",
    )
    assert record["vendor_identity"] == {
        "repository": "github.com/stanford-oval/Churro",
        "sha": "4abb17386d9656199c2776195926545fc527a691",
        "carried_strings": {
            "CHURRO_3B_XML_TEMPLATE.system_message": (
                "13592f5580805cf12d2aa14c963b872e3a4e6a5834e5d2afd7b86effd42a8b4d"
            )
        },
    }
    validate_vendor_identity(record["vendor_identity"])

    # A prompt no vendor artifact supplied leaves the field honestly absent
    # rather than naming a provenance for bytes nobody carried.
    unpinned = retain_model_view(
        _Context(tree=tree),
        adapter="churro.v1",
        view={"prompt": {"system": "a framing of our own"}, "generation": churro_generation()},
        raw_response=_DOCUMENT,
        transport_stop_reason="eos",
        parser="xml",
    )
    assert "vendor_identity" not in unpinned


def test_the_paper_harness_framing_pins_its_own_file_and_commit():
    identity = churro.vendor_identity(churro.prompt("paper-harness-ed09bc7")["system"])
    assert identity["sha"] == "ed09bc7fd6475c333a25427f3d0b9227af46ce27"
    assert set(identity["carried_strings"]) == {"SYSTEM_MESSAGE"}


def test_churro_reads_the_vendor_grammar_without_discarding_the_raw_response():
    tree = _Tree()
    raw = (
        b"<HistoricalDocument><Page><Header><Line>rubric</Line></Header>"
        b"<Body><Line>verbatim</Line></Body></Page></HistoricalDocument>"
    )
    record = retain_model_view(
        _Context(tree=tree),
        adapter="churro.v1",
        view=_churro_view(),
        raw_response=raw,
        transport_stop_reason="length",
        parser="xml",
    )
    assert record["parse"] == {"state": "parsed", "parser": "xml", "text": "rubric\nverbatim"}
    assert record["stop_reason"] == "length"
    assert tree.blobs[record["raw_response_ref"]["relative_path"]] == raw


def test_the_grammars_own_findings_reach_the_capture_beside_the_repetition_scan():
    """Ink the grammar left outside every section is visible on the record, not silent."""
    tree = _Tree()
    record = retain_model_view(
        _Context(tree=tree),
        adapter="churro.v1",
        view=_churro_view(),
        raw_response=(
            b"<HistoricalDocument><Page>stray<Body><Line>retained</Line></Body></Page>"
            b"</HistoricalDocument>"
        ),
        transport_stop_reason="eos",
        parser="xml",
    )
    assert record["parse"]["text"] == "retained"
    assert record["findings"] == [{"kind": "page-text-outside-sections", "page_ordinal": 1}]


def test_a_body_that_offers_the_grammar_and_will_not_parse_is_a_retained_failure():
    tree = _Tree()
    raw = b"<HistoricalDocument><Page><Body><Line>cut off mid-element"
    record = retain_model_view(
        _Context(tree=tree),
        adapter="churro.v1",
        view=_churro_view(),
        raw_response=raw,
        transport_stop_reason="length",
        parser="xml",
    )
    assert record["parse"]["state"] == "failed"
    assert "not parseable XML" in record["parse"]["reason"]
    assert record["stop_reason"] == "partial-parse-failed"
    assert tree.blobs[record["raw_response_ref"]["relative_path"]] == raw


def test_churro_parse_normalization_is_harmless_because_raw_bytes_are_retained():
    tree = _Tree()
    raw = (
        b"<HistoricalDocument><Page><Body><Line>line one\r\n</Line><Line>Ren&#233;</Line>"
        b"</Body></Page></HistoricalDocument>"
    )
    record = retain_model_view(
        _Context(tree=tree),
        adapter="churro.v1",
        view=_churro_view(),
        raw_response=raw,
        transport_stop_reason="eos",
        parser="xml",
    )
    assert record["parse"]["text"] == "line one\nRené"
    assert tree.blobs[record["raw_response_ref"]["relative_path"]] == raw


def test_an_oversized_churro_response_is_retained_but_never_parsed_or_scanned():
    tree = _Tree()
    raw = b"<HistoricalDocument>" + b"x" * CHURRO_MAX_RESPONSE_BYTES + b"</HistoricalDocument>"

    record = retain_model_view(
        _Context(tree=tree),
        adapter="churro.v1",
        view=_churro_view(),
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
        assert raw in tree.blobs.values(), "capture must precede every detector call"
        return {"kind": "post-hoc-repetition", "unit_characters": 24, "repeats": 3}

    monkeypatch.setattr(feeding, "detect_repetition", detector)
    record = retain_model_view(
        _Context(tree=tree),
        adapter="churro.v1",
        view=_churro_view(),
        raw_response=raw,
        transport_stop_reason="eos",
    )
    assert observations == [raw]
    assert tree.blobs[record["raw_response_ref"]["relative_path"]] == raw


def test_repetition_is_detected_in_a_COMPLETE_churro_response_and_reads_the_transcription():
    tree = _Tree()
    clause = "the same clause repeated over and over. "
    raw = (
        b"<HistoricalDocument><Page><Body><Line>"
        + (clause * 6).encode("utf-8")
        + b"</Line></Body></Page></HistoricalDocument>"
    )

    record = retain_model_view(
        _Context(tree=tree),
        adapter="churro.v1",
        view=_churro_view(),
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
    # The closing tags make raw tail windows unequal.
    assert detect_repetition(raw) is None


def test_an_unparseable_capture_is_still_inspected_for_repetition_on_its_raw_bytes():
    tree = _Tree()
    clause = "the same clause repeated over and over. "
    raw = b"<HistoricalDocument><Page><Body><Line>" + (clause * 6).encode("utf-8")

    record = retain_model_view(
        _Context(tree=tree),
        adapter="churro.v1",
        view=_churro_view(),
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
    attestatores = load_stage("3_attestatores")
    tree = _Tree()
    context = _Context(
        tree=tree,
        scenario="churro-native",
        fixture={
            "churro_page_response": [
                {
                    "scenario": "churro-native",
                    "page_ordinal": 1,
                    "chair": "attestator_1",
                    "raw_xml": "<HistoricalDocument><Page><Body><Line>first act</Line><Line>second act</Line></Body></Page></HistoricalDocument>",
                    "transport_stop_reason": "length",
                }
            ]
        },
    )

    result = attestatores.captured_churro_page_attempt(
        context, context.fixture["churro_page_response"][0], "attestator_1", "churro.v1"
    )

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
        b"<HistoricalDocument><Page><Body><Line>first act</Line><Line>second act</Line></Body></Page></HistoricalDocument>"
    )
    assert tree.put_calls == 1, "one declared response must cross capture exactly once"


def test_churro_page_capture_keeps_repetition_finding_after_raw_capture(monkeypatch):
    attestatores = load_stage("3_attestatores")
    tree = _Tree()
    raw = (
        "<HistoricalDocument><Page><Body><Line>complete captured text</Line>"
        "</Body></Page></HistoricalDocument>"
    )
    context = _Context(
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
        # The transcription, not the markup: the grammar's closing tags inside
        # every tail window make the real detector blind to a wholly repeated
        # response.
        assert observed == b"complete captured text"
        return {"kind": "post-hoc-repetition", "unit_characters": 24, "repeats": 3}

    monkeypatch.setattr(feeding, "detect_repetition", detector)
    _, capture = attestatores.captured_churro_page_attempt(
        context, context.fixture["churro_page_response"][0], "attestator_1", "churro.v1"
    )

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
    """A received parse failure is unrecordable, not a no-response channel.

    "Malformed" is now measured against the vendor grammar: a body that offers
    `HistoricalDocument` and will not parse is `failed` with its bytes retained.
    A body that offers no grammar at all is not malformed -- it is the plain
    reading-order text the paper-era harness itself expected, and throwing a
    page of ink away over an unclosed tag is exactly the loss this pipeline
    refuses.
    """
    attestatores = load_stage("3_attestatores")
    tree = _Tree()
    raw = "<HistoricalDocument><Page><Body><Line>unterminated"
    context = _Context(
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

    result = attestatores.captured_churro_page_attempt(
        context, context.fixture["churro_page_response"][0], "attestator_1", "churro.v1"
    )

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
    attestatores = load_stage("3_attestatores")

    def _capture(raw: str, stop: str):
        context = _Context(
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
        return attestatores.captured_churro_page_attempt(
            context, context.fixture["churro_page_response"][0], "attestator_1", "churro.v1"
        )

    cut, _ = _capture(
        "<HistoricalDocument><Page><Body></Body></Page></HistoricalDocument>", "length"
    )
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
    finished, _ = _capture(
        "<HistoricalDocument><Page><Body></Body></Page></HistoricalDocument>", "eos"
    )
    assert finished.outcome == "genuinely-empty"
    assert finished.native_payload == ""
    assert finished.health["recordable"] is True
    assert finished.health["truncated"] is False

    partial, _ = _capture(
        "<HistoricalDocument><Page><Body><Line>half an act and then</Line></Body></Page></HistoricalDocument>",
        "length",
    )
    assert partial.outcome == "read"
    assert partial.native_payload == "half an act and then"
    assert partial.health["truncated"] is True


def test_a_declared_response_no_page_chair_could_be_asked_for_is_refused():
    attestatores = load_stage("3_attestatores")

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
                        "raw_xml": "<HistoricalDocument><Page><Body><Line>x</Line></Body></Page></HistoricalDocument>",
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
                    "attestator_2": _chair("attestator_2"),
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
    attestatores = load_stage("3_attestatores")
    chair = _chair("attestator_1")
    rows = [
        {
            "page_ordinal": 1,
            "chair": "attestator_1",
            "raw_xml": "<HistoricalDocument><Page><Body><Line>default</Line></Body></Page></HistoricalDocument>",
            "transport_stop_reason": "eos",
        },
        {
            "scenario": "churro-native",
            "page_ordinal": 1,
            "chair": "attestator_1",
            "raw_xml": "<HistoricalDocument><Page><Body><Line>scoped</Line></Body></Page></HistoricalDocument>",
            "transport_stop_reason": "eos",
        },
    ]
    context = _Context(
        scenario="churro-native",
        witness_chairs=["attestator_1"],
        registry=SimpleNamespace(config=SimpleNamespace(chairs={"attestator_1": chair})),
        fixture={"page": [{"ordinal": 1}], "churro_page_response": rows},
    )

    attestatores.validate_declared_churro_page_responses(context, {"attestator_1"})
    assert attestatores.declared_page_response(context, 1, "attestator_1", 1) == (
        "churro_page_response",
        rows[1],
    )


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
    attestatores = load_stage("3_attestatores")
    row = {
        "page_ordinal": 1,
        "chair": "attestator_3",
        "raw_xml": "<HistoricalDocument><Page><Body><Line>x</Line></Body></Page></HistoricalDocument>",
        "transport_stop_reason": "eos",
    }
    mutate(row)
    absent = AbsentChair(role="attestator_3", reason="fixture absence")
    context = _Context(
        scenario="churro-native",
        witness_chairs=["attestator_3"],
        registry=SimpleNamespace(config=SimpleNamespace(chairs={"attestator_3": absent})),
        fixture={"page": [{"ordinal": 1}], "churro_page_response": [row]},
    )
    with pytest.raises(attestatores.SchemaRefusal, match=message):
        attestatores.validate_declared_churro_page_responses(context, set())


def test_churro_declarations_are_checked_in_the_no_write_attempt_preflight():
    attestatores = load_stage("3_attestatores")
    chair = _chair("attestator_1", adapter="not-churro.v1")
    context = _Context(
        scenario="churro-native",
        witness_chairs=["attestator_1"],
        registry=SimpleNamespace(config=SimpleNamespace(chairs={"attestator_1": chair})),
        fixture={
            "page": [{"ordinal": 1}],
            "churro_page_response": [
                {
                    "page_ordinal": 1,
                    "chair": "attestator_1",
                    "raw_xml": "<HistoricalDocument><Page><Body><Line>x</Line></Body></Page></HistoricalDocument>",
                    "transport_stop_reason": "eos",
                }
            ],
        },
    )
    with pytest.raises(attestatores.SchemaRefusal, match="different model boundary"):
        attestatores.preflight(context, [], 1, {}, fixture=True)


def test_one_scenarios_declared_response_is_not_another_scenarios_default():
    attestatores = load_stage("3_attestatores")
    rows = [
        {
            "scenario": "churro-native",
            "page_ordinal": 1,
            "chair": "attestator_1",
            "raw_xml": "<HistoricalDocument><Page><Body><Line>other scenario</Line></Body></Page></HistoricalDocument>",
            "transport_stop_reason": "eos",
        },
        {
            "scenario": "churro-native-two",
            "page_ordinal": 1,
            "chair": "attestator_1",
            "raw_xml": "<HistoricalDocument><Page><Body><Line>a third scenario</Line></Body></Page></HistoricalDocument>",
            "transport_stop_reason": "eos",
        },
    ]
    context = _Context(scenario="happy", fixture={"churro_page_response": rows})

    def declared():
        found = attestatores.declared_page_response(context, 1, "attestator_1", 1)
        return None if found is None else found[1]

    assert declared() is None

    context.scenario = "churro-native"
    assert declared() is rows[0]

    rows.append(
        {
            "page_ordinal": 1,
            "chair": "attestator_1",
            "raw_xml": "<HistoricalDocument><Page><Body><Line>the default</Line></Body></Page></HistoricalDocument>",
            "transport_stop_reason": "eos",
        }
    )
    context.scenario = "happy"
    assert declared() is rows[2]
    context.scenario = "churro-native"
    assert declared() is rows[0]


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
        generation_accounting=dai_generation_accounting(),
    )
    assert DAI_MAX_WIDTH_PX == 1_500
    assert view["transform"]["target_width_px"] == 1_500
    assert view["transform"]["target_height_px"] == 500
    assert view["model_image_ref"] == _ref("attestatores/model-views/a.jpg", "b" * 64)
    assert view["transform"]["resampler"] == "pillow-lanczos"
    assert view["image_limits_sha256"] == digest_of(view["image_limits"])
    assert view["uncertainty_tokens_preserved"] == ["[UNCERTAIN]", "[CROSSED_OUT]"]
    assert view["prompts"]["system"] == _ref("models/dai/system.txt")
    assert set(view["prompts"]["system"]) == {"relative_path", "sha256"}


def test_dai_model_view_retains_the_auto_generation_ledger():
    kwargs = {
        "source_image_ref": _ref("designator/crops/a.png"),
        "model_image_ref": _ref("attestatores/model-views/a.jpg", "b" * 64),
        "width_px": 3_000,
        "height_px": 1_001,
        "system_prompt_ref": _ref("models/dai/system.txt"),
        "query_prompt_ref": _ref("models/dai/query.txt"),
        "generation_config_ref": _ref("models/dai/generation_config.json"),
    }
    ledger = dai_generation_accounting()
    current = dai_model_view(**kwargs, generation_accounting=ledger)
    assert current["adapter"] == "dai-atr.v2"
    assert current["generation_accounting"] == ledger
    assert ledger["schema"] == "dai-generation-accounting.v3"
    assert ledger["engine_generation_config"] == "vllm"
    assert ledger["vendor_keys_sent_by_sealed_decoding"] == [
        "repetition_penalty",
        "temperature",
        "top_k",
        "top_p",
    ]
    assert ledger["vendor_keys_read_by_engine"] == ["eos_token_id"]
    assert ledger["vendor_keys_without_request_field"] == [
        "bos_token_id",
        "do_sample",
        "pad_token_id",
    ]
    carried_generation = dai_generation()
    assert ledger["vendor_do_sample"] is carried_generation["do_sample"]
    assert validate_dai_generation_accounting(ledger) is ledger
    assert validate_dai_model_view(current) is current
    without_ledger = {k: v for k, v in current.items() if k != "generation_accounting"}
    for view in (without_ledger, {**without_ledger, "adapter": "dai-atr.v1"}):
        with pytest.raises(SchemaRefusal, match="closed adapter schema"):
            validate_dai_model_view(view)

    forged = {**ledger, "vendor_keys_without_request_field": []}
    with pytest.raises(SchemaRefusal, match="generation accounting differs"):
        validate_dai_generation_accounting(forged)
    retired = {**ledger, "schema": "dai-generation-accounting.v2"}
    with pytest.raises(SchemaRefusal, match="generation accounting differs"):
        validate_dai_generation_accounting(retired)


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


def test_dai_declares_its_own_format_capabilities():
    """DAI's grammar carries a doubt and no geometry, and says exactly that.

    Dissent strips the doubt markers of a chair that declares uncertainty
    (`common/page_path.py::_comparison_text`).
    """
    assert dict(DAI_FORMAT_CAPABILITIES) == {
        "can_express_uncertainty": True,
        "can_express_layout": False,
    }
    with pytest.raises(TypeError):
        DAI_FORMAT_CAPABILITIES["can_express_uncertainty"] = False


def test_every_dai_ceiling_seals_where_it_came_from():
    view = dai_model_view(
        source_image_ref=_ref("designator/crops/a.png"),
        model_image_ref=_ref("attestatores/model-views/a.jpg", "b" * 64),
        width_px=3_000,
        height_px=1_001,
        system_prompt_ref=_ref("models/dai/system.txt"),
        query_prompt_ref=_ref("models/dai/query.txt"),
        generation_config_ref=_ref("models/dai/generation_config.json"),
        generation_accounting=dai_generation_accounting(),
    )
    limits = view["image_limits"]
    # Width only: the model card states no height or area ceiling.
    assert limits["schema"] == "dai-image-limits.v5"
    ceilings = set(limits) - {"schema", "sources"}
    assert ceilings == {"max_width_px"}
    assert ceilings == set(limits["sources"]), "every ceiling names a source, and only ceilings do"
    assert "Qwen2.5-VL-7B-DAI-CReTDHI-RecordGold-ATR" in limits["sources"]["max_width_px"]
    assert "1500 pixels (max)" in limits["sources"]["max_width_px"]
    assert view["image_limits_sha256"] == digest_of(limits)


@pytest.mark.parametrize(
    ("width_px", "height_px", "expected", "resized"),
    [
        # Under the width ceiling: identity however tall. The serving row's
        # `max_pixels` and `request_capacity` bound the rest; there is no
        # second, area-based shrink.
        (500, 10_000, (500, 10_000), False),
        (1_500, 3_000, (1_500, 3_000), False),
        # Over the width ceiling: one floor-rounded aspect-preserving resize --
        # 1,000 x 1,500 // 4,501 truncates to 333, not 334.
        (4_501, 1_000, (1_500, 333), True),
    ],
)
def test_dai_resize_applies_only_the_model_cards_width_ceiling(
    width_px, height_px, expected, resized
):
    source = _ref("designator/crops/tall.png")
    model = _ref("attestatores/model-views/tall.jpg", "b" * 64) if resized else source
    view = dai_model_view(
        source_image_ref=source,
        model_image_ref=model,
        width_px=width_px,
        height_px=height_px,
        system_prompt_ref=_ref("models/dai/system.txt"),
        query_prompt_ref=_ref("models/dai/query.txt"),
        generation_config_ref=_ref("models/dai/generation_config.json"),
        generation_accounting=dai_generation_accounting(),
    )
    target = (view["transform"]["target_width_px"], view["transform"]["target_height_px"])
    assert target == expected
    assert target[0] <= DAI_MAX_WIDTH_PX
    assert (view["transform"]["kind"] == "identity") != resized


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
        generation_accounting=dai_generation_accounting(),
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
            generation_accounting=dai_generation_accounting(),
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
        generation_accounting=dai_generation_accounting(),
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
            generation_accounting=dai_generation_accounting(),
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
        generation_accounting=dai_generation_accounting(),
    )
    valid = retain_model_view(
        _Context(tree=_Tree()),
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
            _Context(tree=tree),
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
        generation_accounting=dai_generation_accounting(),
    )
    view["image_limits"]["max_width_px"] += 1
    view["image_limits_sha256"] = digest_of(view["image_limits"])

    with pytest.raises(SchemaRefusal, match="differ from the sealed executable limits"):
        validate_dai_model_view(view)


def test_model_view_refuses_a_parser_it_cannot_run_instead_of_recording_pending():
    tree = _Tree()
    with pytest.raises(SchemaRefusal, match="does not run for adapter"):
        retain_model_view(
            _Context(tree=tree),
            adapter="dai-atr.v2",
            view={},
            raw_response=b"native DAI text",
            transport_stop_reason="eos",
            parser="xml",
        )
    assert tree.blobs == {}
    unparsed = retain_model_view(
        _Context(tree=tree),
        adapter="dai-atr.v2",
        view={},
        raw_response=b"native DAI text",
        transport_stop_reason="eos",
    )
    assert unparsed["parse"] == {"state": "not-requested", "parser": None}
    assert unparsed["raw_response_ref"]["relative_path"].startswith(
        f"{writing_directory(ATTESTATORES)}/{BLOBS_DIR}/"
    )


def test_a_raw_chair_response_is_not_stored_after_the_seal():
    tree = _Tree()
    with pytest.raises(SchemaRefusal, match="storing a raw chair response afterwards"):
        retain_model_view(
            _Context(tree=tree, sealed=True),
            adapter="churro.v1",
            view=_churro_view(),
            raw_response=_DOCUMENT,
            transport_stop_reason="eos",
            parser="xml",
        )
    assert tree.blobs == {}
