"""Churro's carried framing and its `HistoricalDocument` grammar.

The carried system strings are re-digested here, so an edit to either one
fails offline in the gate rather than at a pod. The grammar cases are written
by hand against the vendor's XSD and guide -- never pasted from the vendor's
own example output -- and each of the four departures the module declares from
`extract_actual_text_from_xml` is asserted as behaviour rather than left as a
docstring claim: marked-up text is kept and indexed, a `Line` is one line, a
broken response is `failed` rather than repaired or silently emptied, and no
character of the response is rewritten beyond the declared whitespace
collapsing.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

from common.churro_document import (
    CHURRO_PARSER,
    CHURRO_PROMPT_VARIANTS,
    CHURRO_TEXT_VIEW,
    DOCUMENT_FINDING_KINDS,
    DOCUMENT_SHAPES,
    MARKED_SPAN_KINDS,
    PAGE_SECTIONS,
    PARSE_STATES,
    churro_prompt_provenance,
    churro_prompt_view,
    churro_system_prompt,
    parse_churro_document,
    trim_leading_prompt,
    validate_churro_document_parse,
)
from common.contracts.errors import SchemaRefusal

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "common" / "churro_document.py"

REGISTRY = "registry-v0.3.0"
PAPER = "paper-harness-ed09bc7"


def _parse(document: str, **kwargs) -> dict:
    """Parse and close every record this suite produces, at one seam."""
    record = parse_churro_document(document.encode("utf-8"), **kwargs)
    return validate_churro_document_parse(record)


def _page(body: str, header: str = "", footer: str = "") -> str:
    return f"<Page>{header}<Body>{body}</Body>{footer}</Page>"


def _document(*pages: str, namespace: str = "") -> str:
    attribute = f' xmlns="{namespace}"' if namespace else ""
    return f"<HistoricalDocument{attribute}>{''.join(pages)}</HistoricalDocument>"


# --------------------------------------------------------------------------
# The carried strings
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("variant", "expected"),
    [
        (REGISTRY, "13592f5580805cf12d2aa14c963b872e3a4e6a5834e5d2afd7b86effd42a8b4d"),
        (PAPER, "dd7408ca72cf94f724b0522806427533a746f08cfa0cfd047e0272ebc4c4b489"),
    ],
)
def test_carried_system_string_digests_are_what_the_module_records(variant, expected):
    """A changed byte in either framing fails here, offline, before any pod."""
    rendered = churro_system_prompt(variant).encode("utf-8")
    assert hashlib.sha256(rendered).hexdigest() == expected
    assert CHURRO_PROMPT_VARIANTS[variant]["system_sha256"] == expected


def test_the_two_variants_are_the_two_vendor_framings_and_differ():
    registry = churro_system_prompt(REGISTRY)
    paper = churro_system_prompt(PAPER)
    assert registry == "Transcribe the entirety of this historical document to XML format."
    # The paper-era harness's two spelling errors are carried, not corrected:
    # they are the bytes that harness actually sent.
    assert paper == "Transcribe the entiretly of this historical documents to XML format."
    assert registry != paper
    assert set(CHURRO_PROMPT_VARIANTS) == {REGISTRY, PAPER}


def test_both_framings_are_system_only():
    """Neither vendor framing sends user text; the image is the whole user turn."""
    for variant in CHURRO_PROMPT_VARIANTS:
        assert CHURRO_PROMPT_VARIANTS[variant]["user"] is None
        assert churro_prompt_view(variant) == {"system": churro_system_prompt(variant)}


def test_provenance_carries_the_vendor_code_identity():
    provenance = churro_prompt_provenance(REGISTRY)
    assert provenance["prompt_variant"] == REGISTRY
    assert provenance["repository"] == "github.com/stanford-oval/Churro"
    assert provenance["commit"] == "4abb17386d9656199c2776195926545fc527a691"
    assert provenance["path"] == "src/churro_ocr/templates/presets.py"
    assert provenance["symbol"] == "CHURRO_3B_XML_TEMPLATE.system_message"
    assert provenance["licence"] == "Apache-2.0"
    assert provenance["system_sha256"] == CHURRO_PROMPT_VARIANTS[REGISTRY]["system_sha256"]
    assert churro_prompt_provenance(PAPER)["commit"] == "ed09bc7fd6"


def test_an_unknown_variant_is_refused_by_name():
    with pytest.raises(SchemaRefusal, match="unknown Churro prompt variant"):
        churro_system_prompt("churro_prompt")
    with pytest.raises(SchemaRefusal):
        churro_prompt_view(None)


def test_module_names_no_default_variant():
    """Which framing runs is configuration; a default here would be a second answer."""
    source = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    assigned = {
        target.id
        for node in source.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in ([node.target] if isinstance(node, ast.AnnAssign) else node.targets)
        if isinstance(target, ast.Name)
    }
    assert not [name for name in assigned if "DEFAULT" in name]


def test_module_imports_no_stage():
    """`common/` may not import a stage; this module is shared by two of them."""
    source = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    modules: list[str] = []
    for node in ast.walk(source):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    assert not [name for name in modules if name.split(".")[0] in {"pipeline", "operations"}]


# --------------------------------------------------------------------------
# `trim_leading_prompt`, reproduced from the vendor
# --------------------------------------------------------------------------


def test_trim_leading_prompt_strips_newlines_and_spaces_only():
    prompt = churro_system_prompt(REGISTRY)
    assert trim_leading_prompt(f"{prompt}\n \nAnno 1451", prompt) == "Anno 1451"
    # `lstrip("\n ")` is the vendor's exact character set: a tab and a carriage
    # return are not in it and survive.
    assert trim_leading_prompt(f"{prompt}\t x", prompt) == "\t x"
    assert trim_leading_prompt(f"{prompt}\r\nx", prompt) == "\r\nx"


def test_trim_leading_prompt_leaves_a_body_that_does_not_open_with_it():
    prompt = churro_system_prompt(REGISTRY)
    assert trim_leading_prompt("Anno 1451", prompt) == "Anno 1451"
    assert trim_leading_prompt("Anno 1451", None) == "Anno 1451"
    assert trim_leading_prompt("Anno 1451", "") == "Anno 1451"


def test_prompt_echo_is_a_finding_with_the_characters_it_removed():
    prompt = churro_system_prompt(REGISTRY)
    record = _parse(f"{prompt}\nAnno domini 1451", system_prompt=prompt)
    assert record["shape"] == "plain-text"
    assert record["text"] == "Anno domini 1451"
    assert record["findings"] == [{"kind": "prompt-echo-trimmed", "characters": len(prompt) + 1}]


def test_only_the_framing_that_was_sent_is_ever_trimmed():
    """Trimming against a prompt the request did not send could delete real ink."""
    echoed = f"{churro_system_prompt(PAPER)}\nAnno 1451"
    record = _parse(echoed, system_prompt=churro_system_prompt(REGISTRY))
    assert record["findings"] == []
    assert record["text"] == echoed
    # And with no prompt declared at all, nothing is guessed.
    assert _parse(echoed)["text"] == echoed


def test_a_trimmed_echo_in_front_of_the_grammar_still_parses_as_the_grammar():
    """The vendor trims first and parses second; so does this."""
    prompt = churro_system_prompt(REGISTRY)
    document = _document(_page("<Line>Anno 1451</Line>"))
    record = _parse(f"{prompt}\n{document}", system_prompt=prompt)
    assert record["shape"] == "historical-document"
    assert record["text"] == "Anno 1451"
    assert [finding["kind"] for finding in record["findings"]] == ["prompt-echo-trimmed"]


# --------------------------------------------------------------------------
# The grammar
# --------------------------------------------------------------------------


def test_sections_are_walked_header_body_footer_whatever_order_they_appear_in():
    page = (
        "<Page>"
        "<Footer><Line>foot</Line></Footer>"
        "<Body><Paragraph><Line>body</Line></Paragraph></Body>"
        "<Header><Line>head</Line></Header>"
        "</Page>"
    )
    record = _parse(_document(page))
    assert record["text"] == "head\nbody\nfoot"
    assert [section["section"] for section in record["sections"]] == list(PAGE_SECTIONS)
    assert [section["lines"] for section in record["sections"]] == [1, 1, 1]


def test_section_spans_address_their_own_text_and_exclude_the_separators():
    record = _parse(
        _document(_page("<Line>alpha</Line>", header="<Header><Line>head</Line></Header>"))
    )
    text = record["text"]
    assert text == "head\nalpha"
    spans = {
        section["section"]: text[section["span"]["start"] : section["span"]["end"]]
        for section in record["sections"]
    }
    assert spans == {"Header": "head", "Body": "alpha"}


def test_pages_are_joined_with_a_blank_line_and_numbered_from_one():
    record = _parse(_document(_page("<Line>one</Line>"), _page("<Line>two</Line>")))
    assert record["text"] == "one\n\ntwo"
    assert record["pages"] == 2
    assert [section["page_ordinal"] for section in record["sections"]] == [1, 2]


def test_a_default_namespace_is_matched_by_local_name():
    plain = _parse(_document(_page("<Line>alpha</Line>")))
    namespaced = _parse(
        _document(_page("<Line>alpha</Line>"), namespace="http://example.com/historicaldocument")
    )
    assert namespaced["text"] == plain["text"] == "alpha"
    assert namespaced["sections"] == plain["sections"]


def test_metadata_is_outside_the_transcription():
    """`Language` and `Script` are not ink; they stay in the retained bytes."""
    document = _document(
        "<Metadata><Language>lat</Language><Script>Latn</Script></Metadata>",
        _page("<Line>alpha</Line>"),
    )
    record = _parse(document)
    assert record["text"] == "alpha"
    assert "lat" not in record["text"]


def test_a_document_with_no_pages_reads_as_empty_and_says_so():
    record = _parse(_document("<Metadata><Language>lat</Language></Metadata>"))
    assert record["state"] == "parsed"
    assert record["text"] == ""
    assert record["pages"] == 0
    assert record["sections"] == []


def test_a_line_is_one_line_even_when_inline_markup_splits_its_text():
    """The vendor's flattener makes this three lines; reading order makes it one."""
    record = _parse(
        _document(
            _page(
                "<Paragraph><Line>\n  Nos <Addition>humiles</Addition> notarii\n"
                "  subscripsimus.\n</Line></Paragraph>"
            )
        )
    )
    assert record["text"] == "Nos humiles notarii subscripsimus."


def test_whitespace_inside_a_line_collapses_but_a_joined_boundary_stays_joined():
    record = _parse(_document(_page("<Line>Nos<Addition>que</Addition>   tarii</Line>")))
    assert record["text"] == "Nosque tarii"


def test_a_space_that_is_all_that_separates_two_inline_elements_survives():
    """A whitespace-only text node inside a line is a separator, not indentation."""
    record = _parse(
        _document(_page("<Line><Addition>alpha</Addition> <Deletion>beta</Deletion></Line>"))
    )
    assert record["text"] == "alpha beta"
    assert [span["kind"] for span in record["marked_spans"]] == ["Addition", "Deletion"]
    assert record["text"][6:10] == "beta"


def test_only_ascii_whitespace_is_collapsed():
    """Indentation is collapsed; other Unicode spaces are characters, not layout."""
    record = _parse(_document(_page("\n  <Line>Jean Baptiste  \n  Marie</Line>\n")))
    assert record["text"] == "Jean Baptiste Marie"
    # U+00A0 is not ASCII whitespace: it stays exactly where the model put it.
    nbsp = _parse(_document(_page("<Line>  Jean\u00a0Baptiste  </Line>")))
    assert nbsp["text"] == "Jean\u00a0Baptiste"


def test_nesting_deeper_than_the_declared_bound_is_failed_not_a_stack_overflow():
    """2,000 levels overflows this recursive walk; the bound turns that into a record.

    `xml.etree`'s parser is iterative and builds the tree happily, so without
    the declared bound a `RecursionError` would escape the parser where a named
    `failed` record belongs.
    """
    deep = "<Line>x</Line>"
    for _ in range(2000):
        deep = f"<Paragraph>{deep}</Paragraph>"
    record = _parse(_document(_page(deep)))
    assert record["state"] == "failed"
    assert "nests elements deeper than" in record["reason"]


def test_nesting_inside_the_declared_bound_is_read():
    deep = "<Line>x</Line>"
    for _ in range(200):
        deep = f"<Paragraph>{deep}</Paragraph>"
    record = _parse(_document(_page(deep)))
    assert record["state"] == "parsed"
    assert record["text"] == "x"


def test_nested_markup_spans_close_in_order_and_keep_their_own_ranges():
    record = _parse(_document(_page("<Line><Deletion><Addition>x</Addition>y</Deletion></Line>")))
    assert record["text"] == "xy"
    assert record["marked_spans"] == [
        {"kind": "Addition", "start": 0, "end": 1},
        {"kind": "Deletion", "start": 0, "end": 2},
    ]


def test_text_outside_a_line_becomes_its_own_line():
    """A `PageNumber` is not inside a `Line`; dropping it would lose ink."""
    page = (
        "<Page>"
        "<Header><PageNumber>12</PageNumber><Heading><Line>Registre</Line></Heading></Header>"
        "<Body><Paragraph><Line>alpha</Line></Paragraph></Body>"
        "</Page>"
    )
    record = _parse(_document(page))
    assert record["text"] == "12\nRegistre\nalpha"


def test_line_count_counts_line_elements_not_emitted_newlines():
    record = _parse(_document(_page("<Line>alpha</Line><Line>   </Line><Line>beta</Line>")))
    assert record["text"] == "alpha\nbeta"
    assert [section["lines"] for section in record["sections"]] == [3]


# --------------------------------------------------------------------------
# Marked spans: what the vendor deletes, kept and indexed
# --------------------------------------------------------------------------


def test_every_marked_kind_is_kept_as_text_and_indexed_by_span():
    page = _page(
        "<Line>a <Addition>add</Addition> b <Deletion>del</Deletion> c"
        '<Illegible reason="faded"/> d <InterlinearNote>note</InterlinearNote> e'
        '<Gap reason="illegible"/></Line>'
        "<Figure><Description>desc</Description></Figure>"
    )
    record = _parse(_document(page))
    text = record["text"]
    found = {span["kind"]: text[span["start"] : span["end"]] for span in record["marked_spans"]}
    assert found == {
        "Addition": "add",
        "Deletion": "del",
        "InterlinearNote": "note",
        "Description": "desc",
        # Both are empty elements in the XSD, so both mark a point, not a range.
        "Illegible": "",
        "Gap": "",
    }
    assert set(found) == set(MARKED_SPAN_KINDS)
    assert "del" in text and "add" in text


def test_marked_spans_are_ordered_by_offset():
    record = _parse(
        _document(_page("<Line><Addition>x</Addition> y <Deletion>z</Deletion></Line>"))
    )
    starts = [span["start"] for span in record["marked_spans"]]
    assert starts == sorted(starts)


def test_deleting_the_marked_spans_reproduces_the_vendor_drop():
    """The vendor's lossy flattening stays derivable; the ink does not stop being there."""
    record = _parse(_document(_page("<Line>a <Deletion>struck</Deletion> b</Line>")))
    text = record["text"]
    assert text == "a struck b"
    remaining = text
    for span in sorted(record["marked_spans"], key=lambda item: -item["start"]):
        remaining = remaining[: span["start"]] + remaining[span["end"] :]
    assert remaining == "a  b"


def test_an_empty_marker_at_the_end_of_a_line_marks_the_point_it_reached():
    record = _parse(_document(_page('<Line>alpha<Gap reason="missing"/></Line>')))
    assert record["text"] == "alpha"
    assert record["marked_spans"] == [{"kind": "Gap", "start": 5, "end": 5}]


# --------------------------------------------------------------------------
# The three shapes, and everything that is not one of them
# --------------------------------------------------------------------------


def test_plain_text_is_returned_exactly_as_it_decoded():
    body = "  Anno   domini 1451\n\n\n  Jean Baptiste  \n"
    record = _parse(body)
    assert record["shape"] == "plain-text"
    # No collapsing at all: there was no markup whose serialisation could have
    # introduced whitespace the model did not write.
    assert record["text"] == body
    assert record["pages"] == 0
    assert record["marked_spans"] == []
    assert record["view"] == CHURRO_TEXT_VIEW


def test_a_bare_output_envelope_is_accepted_as_retired_history_and_says_so():
    record = _parse("<output>Anno 1451</output>")
    assert record["shape"] == "output-element"
    assert record["text"] == "Anno 1451"
    assert record["findings"] == [{"kind": "retired-output-envelope"}]
    assert _parse("<output></output>")["text"] == ""


def test_an_output_element_that_is_not_bare_is_an_unrecognized_shape():
    for document in ("<output lang='fr'>x</output>", "<output><Line>x</Line></output>"):
        record = _parse(document)
        assert record["state"] == "unrecognized-shape"
        assert "output" in record["reason"]


def test_well_formed_xml_under_another_root_is_unrecognized_not_failed():
    """The parser ran and read the whole response; that is not a parse failure."""
    record = _parse("<Transcription><Line>alpha</Line></Transcription>")
    assert record["state"] == "unrecognized-shape"
    assert "Transcription" in record["reason"]
    assert "text" not in record


def test_broken_grammar_is_failed_never_repaired_and_never_silently_empty():
    """The vendor repairs with `recover=True` and returns `""` on error; this does not."""
    broken = "<HistoricalDocument><Page><Body><Line>alpha</Line></Body></Page>"
    record = _parse(broken)
    assert record["state"] == "failed"
    assert "not parseable XML" in record["reason"]
    assert "text" not in record
    assert record["response_bytes"] == len(broken.encode("utf-8"))


def test_trailing_content_after_the_root_is_failed_rather_than_partially_read():
    record = _parse(_document(_page("<Line>alpha</Line>")) + "\nand that is all")
    assert record["state"] == "failed"
    assert "not parseable XML" in record["reason"]


def test_text_that_merely_opens_with_an_angle_bracket_is_not_thrown_away():
    body = "<< Anno domini 1451 >>"
    record = _parse(body)
    assert record["shape"] == "plain-text"
    assert record["text"] == body


def test_a_leading_declaration_after_a_newline_still_parses():
    record = _parse("\n<?xml version='1.0'?>" + _document(_page("<Line>alpha</Line>")))
    assert record["shape"] == "historical-document"
    assert record["text"] == "alpha"


def test_a_doctype_is_refused_before_any_parser_expands_it():
    record = _parse(
        "<!DOCTYPE HistoricalDocument [<!ENTITY a 'x'>]>" + _document(_page("<Line>&a;</Line>"))
    )
    assert record["state"] == "failed"
    assert "DOCTYPE" in record["reason"]


def test_bytes_that_are_not_utf8_are_failed_with_their_length():
    raw = b"\xff\xfe not text"
    record = validate_churro_document_parse(parse_churro_document(raw))
    assert record["state"] == "failed"
    assert "not UTF-8" in record["reason"]
    assert record["response_bytes"] == len(raw)


def test_a_caller_supplied_ceiling_refuses_before_the_parser_runs():
    document = _document(_page("<Line>alpha</Line>")).encode("utf-8")
    record = validate_churro_document_parse(
        parse_churro_document(document, max_bytes=len(document) - 1)
    )
    assert record["state"] == "failed"
    assert "retained parsing limit" in record["reason"]
    # The same bytes under no ceiling are the reading they were.
    assert parse_churro_document(document)["state"] == "parsed"


def test_the_module_declares_no_byte_ceiling_of_its_own():
    """`native_witness.CHURRO_MAX_RESPONSE_BYTES` is the single owner of that number.

    A second copy here would be free to drift from the one the capture
    contract actually applies, and the drift would show up as a response
    refused at one seam and accepted at the other.
    """
    source = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    literals = [
        node.value.value
        for node in source.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, int)
        and not isinstance(node.value.value, bool)
    ]
    assert not [value for value in literals if value >= 1024]
    # And a large response is read, not refused, when no caller names a limit.
    large = "x" * 200_000
    assert parse_churro_document(large.encode("utf-8"))["text"] == large


def test_a_response_that_is_not_bytes_is_refused():
    with pytest.raises(SchemaRefusal, match="not raw bytes"):
        parse_churro_document("<output>x</output>")


# --------------------------------------------------------------------------
# The closed record
# --------------------------------------------------------------------------


def test_the_declared_vocabulary_is_what_the_records_use():
    assert CHURRO_TEXT_VIEW == "churro-historical-document-text.v1"
    assert CHURRO_PARSER == "xml"
    assert PARSE_STATES == {"parsed", "failed", "unrecognized-shape"}
    assert DOCUMENT_SHAPES == {"historical-document", "plain-text", "output-element"}
    assert DOCUMENT_FINDING_KINDS == {"prompt-echo-trimmed", "retired-output-envelope"}
    assert MARKED_SPAN_KINDS == {
        "Addition",
        "Deletion",
        "Illegible",
        "Gap",
        "InterlinearNote",
        "Description",
    }


@pytest.mark.parametrize(
    "body",
    [
        _document(_page("<Line>alpha</Line>")),
        "plain reading order text",
        "<output>x</output>",
        "<Transcription/>",
        "<HistoricalDocument><Page>",
    ],
)
def test_every_produced_record_closes_its_own_schema(body):
    record = parse_churro_document(body.encode("utf-8"))
    assert validate_churro_document_parse(record) is record
    assert record["state"] in PARSE_STATES


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda record: record.update(state="repaired"), "unknown state"),
        (lambda record: record.update(parser="html"), "does not name the Churro parser"),
        (lambda record: record.update(view="churro-text.v0"), "does not name the Churro text view"),
        (lambda record: record.update(shape="best-effort"), "unknown shape"),
        (lambda record: record.update(extra=1), "closed schema"),
        (lambda record: record.pop("marked_spans"), "closed schema"),
        (lambda record: record.update(response_bytes=-1), "response byte count"),
        (lambda record: record.update(pages=True), "page count"),
        (
            lambda record: record["marked_spans"].append(
                {"kind": "Addition", "start": 0, "end": 9_999}
            ),
            "runs past the end",
        ),
        (
            lambda record: record["marked_spans"].append({"kind": "Line", "start": 0, "end": 0}),
            "unknown kind",
        ),
        (
            lambda record: record["sections"].append(
                {"page_ordinal": 1, "section": "Margin", "span": {"start": 0, "end": 0}, "lines": 0}
            ),
            "section is named",
        ),
        (
            lambda record: record["sections"].append(
                {"page_ordinal": 0, "section": "Body", "span": {"start": 0, "end": 0}, "lines": 0}
            ),
            "1-based page ordinal",
        ),
        (
            lambda record: record["findings"].append({"kind": "guessed"}),
            "unknown finding kind",
        ),
        (
            lambda record: record["findings"].append({"kind": "prompt-echo-trimmed"}),
            "prompt-echo finding is malformed",
        ),
        (lambda record: record.update(sections=None), "are not lists"),
    ],
)
def test_a_mutated_record_is_refused_by_name(mutate, message):
    record = parse_churro_document(
        _document(_page("<Line>a <Addition>b</Addition></Line>")).encode("utf-8")
    )
    validate_churro_document_parse(record)
    mutate(record)
    with pytest.raises(SchemaRefusal, match=message):
        validate_churro_document_parse(record)


def test_a_shape_that_is_not_the_grammar_may_not_claim_page_structure():
    """Only the grammar was read for structure; the other two shapes have none."""
    record = parse_churro_document(b"plain reading order text")
    assert record["shape"] == "plain-text"
    validate_churro_document_parse(record)
    record["sections"] = [
        {"page_ordinal": 1, "section": "Body", "span": {"start": 0, "end": 0}, "lines": 0}
    ]
    with pytest.raises(SchemaRefusal, match="claims page structure it cannot have read"):
        validate_churro_document_parse(record)


def test_a_failed_record_may_not_claim_text_and_needs_a_reason():
    record = parse_churro_document(b"<HistoricalDocument>")
    assert validate_churro_document_parse(record)["state"] == "failed"
    record["reason"] = "  "
    with pytest.raises(SchemaRefusal, match="carries no reason"):
        validate_churro_document_parse(record)


def test_a_non_record_is_refused():
    with pytest.raises(SchemaRefusal, match="not an object"):
        validate_churro_document_parse(["parsed"])
