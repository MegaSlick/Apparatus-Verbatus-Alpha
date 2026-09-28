"""Exact audit edit and prompt records, including legacy schema refusals."""

import importlib.util
import json
from pathlib import Path

import pytest

from common import perlector_audit
from common.contracts.canonical import digest_bytes
from common.contracts.errors import SchemaRefusal
from common.perlector_audit import (
    LEGACY_REQUEST_SCHEMA,
    LEGACY_SCHEMA,
    ReproofResponseRefusal,
    assemble_reproof_response,
    audit_prompt_evidence,
    audit_request,
    change_records_from_edits,
    render_reproof_instruction,
    text_change_span,
    validate_audit_prompt_evidence,
)


def _request(text: str = "alpha βeta gamma") -> dict:
    return audit_request(
        act_key="a1",
        attempt_ordinal=1,
        draft_ref={"relative_path": "4_perlector/audit-draft/a1.json", "sha256": "a" * 64},
        semi_final_text=text,
        flags=[{"class": "testimony-diff", "location": {"start": 6, "end": 10}}],
    )


@pytest.mark.parametrize("wrap", ["{}", "```json\n{}\n```", "```\n{}\n```\n"])
def test_reproof_edits_assemble_only_from_exact_unicode_anchored_originals(wrap):
    request = _request()
    raw = wrap.format(
        json.dumps(
            {
                "schema": "perlector-audit-response.v1",
                "edits": [
                    {
                        "class": "testimony-diff",
                        "location": {"start": 6, "end": 10},
                        "original": "βeta",
                        "replacement": "beta",
                    }
                ],
            }
        )
    )
    assembled, response = assemble_reproof_response(raw, request)
    assert assembled == "alpha beta gamma"
    assert response["edits"][0]["original"] == "βeta"


@pytest.mark.parametrize(
    "edit, match",
    [
        (
            {
                "class": "testimony-diff",
                "location": {"start": 0, "end": 5},
                "original": "alpha",
                "replacement": "omega",
            },
            "exact requested location",
        ),
        (
            {
                "class": "testimony-diff",
                "location": {"start": 6, "end": 10},
                "original": "beta",
                "replacement": "beta",
            },
            "exact frozen text",
        ),
    ],
)
def test_reproof_rejects_wrong_scope_or_original_without_fuzzy_assembly(edit, match):
    raw = json.dumps({"schema": "perlector-audit-response.v1", "edits": [edit]})
    with pytest.raises(ReproofResponseRefusal, match=match):
        assemble_reproof_response(raw, _request())


def test_two_disjoint_exact_edits_assemble_and_keep_one_change_record_each():
    request = audit_request(
        act_key="a1",
        attempt_ordinal=1,
        draft_ref={"relative_path": "4_perlector/audit-draft/a1.json", "sha256": "a" * 64},
        semi_final_text="alpha beta gamma",
        flags=[
            {"class": "testimony-diff", "location": {"start": 0, "end": 5}},
            {"class": "repetition", "location": {"start": 11, "end": 16}},
        ],
    )
    edits = [
        {
            "class": "testimony-diff",
            "location": {"start": 0, "end": 5},
            "original": "alpha",
            "replacement": "ALPHA",
        },
        {
            "class": "repetition",
            "location": {"start": 11, "end": 16},
            "original": "gamma",
            "replacement": "GAMMA",
        },
    ]
    assembled, response = assemble_reproof_response(
        json.dumps({"schema": "perlector-audit-response.v1", "edits": edits}), request
    )
    assert assembled == "ALPHA beta GAMMA"
    assert change_records_from_edits(response["edits"]) == [
        {"start": 0, "end": 5, "triggering_flag_class": "testimony-diff"},
        {"start": 11, "end": 16, "triggering_flag_class": "repetition"},
    ]


def test_colocated_cross_class_insertions_are_applied_once_and_keep_both_findings():
    request = audit_request(
        act_key="a1",
        attempt_ordinal=1,
        draft_ref={"relative_path": "4_perlector/audit-draft/a1.json", "sha256": "a" * 64},
        semi_final_text="ab",
        flags=[
            {"class": "testimony-diff", "location": {"start": 1, "end": 1}},
            {"class": "repetition", "location": {"start": 1, "end": 1}},
        ],
    )
    edits = [
        {
            "class": row["class"],
            "location": row["location"],
            "original": "",
            "replacement": "X",
        }
        for row in request["reproofs"]
    ]
    assembled, response = assemble_reproof_response(
        json.dumps({"schema": "perlector-audit-response.v1", "edits": edits}), request
    )
    assert assembled == "aXb"
    assert change_records_from_edits(response["edits"]) == [
        {"start": 1, "end": 1, "triggering_flag_class": "testimony-diff"},
        {"start": 1, "end": 1, "triggering_flag_class": "repetition"},
    ]


@pytest.mark.parametrize("second_replacement", ["", "Y"])
def test_colocated_cross_class_insertions_refuse_disagreeing_answers(second_replacement):
    request = audit_request(
        act_key="a1",
        attempt_ordinal=1,
        draft_ref={"relative_path": "4_perlector/audit-draft/a1.json", "sha256": "a" * 64},
        semi_final_text="ab",
        flags=[
            {"class": "testimony-diff", "location": {"start": 1, "end": 1}},
            {"class": "repetition", "location": {"start": 1, "end": 1}},
        ],
    )
    edits = [
        {
            "class": "testimony-diff",
            "location": {"start": 1, "end": 1},
            "original": "",
            "replacement": "X",
        },
        {
            "class": "repetition",
            "location": {"start": 1, "end": 1},
            "original": "",
            "replacement": second_replacement,
        },
    ]
    with pytest.raises(ReproofResponseRefusal, match="contradictory answers"):
        assemble_reproof_response(
            json.dumps({"schema": "perlector-audit-response.v1", "edits": edits}), request
        )


def test_colocated_cross_class_nonzero_corrections_are_applied_once():
    request = audit_request(
        act_key="a1",
        attempt_ordinal=1,
        draft_ref={"relative_path": "4_perlector/audit-draft/a1.json", "sha256": "a" * 64},
        semi_final_text="alpha beta",
        flags=[
            {"class": "testimony-diff", "location": {"start": 6, "end": 10}},
            {"class": "repetition", "location": {"start": 6, "end": 10}},
        ],
    )
    edits = [
        {
            "class": row["class"],
            "location": row["location"],
            "original": "beta",
            "replacement": "bêta",
        }
        for row in request["reproofs"]
    ]
    assembled, _response = assemble_reproof_response(
        json.dumps({"schema": "perlector-audit-response.v1", "edits": edits}), request
    )
    assert assembled == "alpha bêta"

    edits[1]["replacement"] = "beta"
    with pytest.raises(ReproofResponseRefusal, match="contradictory answers"):
        assemble_reproof_response(
            json.dumps({"schema": "perlector-audit-response.v1", "edits": edits}), request
        )


def test_live_instruction_contains_frozen_text_closed_shape_and_unicode_offset_rule():
    request = _request("alpha βeta")
    instruction = render_reproof_instruction(request)
    assert json.dumps(request["semi_final_text"], ensure_ascii=False) in instruction
    assert "zero-based Python Unicode code-point offsets" in instruction
    assert "exactly schema and edits" in instruction
    assert all(field in instruction for field in ("class", "location", "original", "replacement"))
    assert "replacement must equal original" in instruction


def test_audit_prompt_evidence_binds_the_full_rendered_instruction_and_request():
    request = _request("alpha βeta")
    base_text = "base prompt"
    base_prompt = {"rendered_sha256": digest_bytes(base_text.encode("utf-8"))}
    rendered = "\n".join((base_text, render_reproof_instruction(request)))
    evidence = audit_prompt_evidence(
        base_prompt=base_prompt,
        base_text=base_text,
        request=request,
        request_sha256="c" * 64,
        rendered_text=rendered,
    )
    assert validate_audit_prompt_evidence(evidence, request=request) == evidence
    with pytest.raises(SchemaRefusal, match="omits its exact frozen"):
        validate_audit_prompt_evidence(
            {**evidence, "rendered_text": "x", "rendered_sha256": digest_bytes(b"x")},
            request=request,
        )


def test_reproof_rejects_a_partial_reply_before_any_text_is_assembled():
    request = audit_request(
        act_key="a1",
        attempt_ordinal=1,
        draft_ref={"relative_path": "4_perlector/audit-draft/a1.json", "sha256": "a" * 64},
        semi_final_text="alpha beta gamma",
        flags=[
            {"class": "testimony-diff", "location": {"start": 0, "end": 5}},
            {"class": "repetition", "location": {"start": 6, "end": 10}},
        ],
    )
    raw = json.dumps(
        {
            "schema": "perlector-audit-response.v1",
            "edits": [
                {
                    "class": "testimony-diff",
                    "location": {"start": 0, "end": 5},
                    "original": "alpha",
                    "replacement": "alpha",
                }
            ],
        }
    )
    with pytest.raises(ReproofResponseRefusal, match="does not answer every flagged span"):
        assemble_reproof_response(raw, request)


@pytest.mark.parametrize(
    "raw",
    [
        '{"schema":"perlector-audit-response.v1","schema":"other","edits":[]}',
        (
            '{"schema":"perlector-audit-response.v1","edits":['
            '{"class":"testimony-diff","location":{"start":6,"end":10},'
            '"original":"βeta","original":"beta","replacement":"beta"}]}'
        ),
    ],
)
def test_reproof_rejects_duplicate_json_members(raw):
    with pytest.raises(ReproofResponseRefusal, match="repeats JSON member"):
        assemble_reproof_response(raw, _request())


def test_legacy_request_is_refused_by_name_before_it_can_reach_the_reader():
    request = _request()
    request["schema"] = LEGACY_REQUEST_SCHEMA
    with pytest.raises(SchemaRefusal, match="sealed under perlector-audit-request.v1"):
        assemble_reproof_response(
            json.dumps({"schema": "perlector-audit-response.v1", "edits": []}), request
        )
    with pytest.raises(SchemaRefusal, match="sealed under perlector-audit.v2"):
        audit_request(
            act_key="a1",
            attempt_ordinal=1,
            draft_ref={"relative_path": "4_perlector/audit-draft/a1.json", "sha256": "a" * 64},
            semi_final_text="alpha beta",
            flags=[{"class": "testimony-diff", "location": {"start": 6, "end": 10}}],
            policy_schema=LEGACY_SCHEMA,
        )


def test_text_change_span_trims_shared_prefix_and_suffix():
    assert text_change_span("abcXdef", "abcYdef") == (3, 4)
    assert text_change_span("same text", "same text") == (9, 9)
    assert text_change_span("abc", "abcXYZ") == (3, 3)


@pytest.mark.parametrize("change_renderer", ["unrelated", "prose", "text"])
def test_renderer_identity_moves_only_with_renderer_code(tmp_path, change_renderer):
    request = _request("alpha βeta")
    base_text = "base prompt"
    evidence = audit_prompt_evidence(
        base_prompt={"rendered_sha256": digest_bytes(base_text.encode("utf-8"))},
        base_text=base_text,
        request=request,
        request_sha256="c" * 64,
        rendered_text="\n".join((base_text, render_reproof_instruction(request))),
    )
    assert validate_audit_prompt_evidence(evidence, request=request) == evidence
    source = Path(perlector_audit.__file__).read_text()
    if change_renderer == "text":
        original = "Re-examine only the requested character locations against the ink."
        assert source.count(original) == 1
        source = source.replace(original, "Re-examine the requested locations against the ink.")
    elif change_renderer == "prose":
        docstring = '"""Render the complete live instruction for one frozen exact-edit request."""'
        assert source.count(docstring) == 1
        source = source.replace(docstring, '"""Reworded."""\n    # a comment inside the renderer')
    else:
        source += "\n# An unrelated maintenance edit outside the renderer.\n"
    changed_path = tmp_path / "changed_perlector_audit.py"
    changed_path.write_text(source)
    spec = importlib.util.spec_from_file_location("changed_perlector_audit", changed_path)
    assert spec is not None and spec.loader is not None
    changed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(changed)
    if change_renderer == "text":
        assert changed.AUDIT_PROMPT_RENDERER_SHA256 != evidence["renderer_sha256"]
        with pytest.raises(SchemaRefusal, match="another renderer revision"):
            changed.validate_audit_prompt_evidence(evidence, request=request)
    else:
        assert changed.AUDIT_PROMPT_RENDERER_SHA256 == evidence["renderer_sha256"]
        assert changed.validate_audit_prompt_evidence(evidence, request=request) == evidence


_EMPTY_EDITS = '{"schema":"perlector-audit-response.v1","edits":[]}'


@pytest.mark.parametrize(
    "raw",
    [
        f"Here are the edits:\n```json\n{_EMPTY_EDITS}\n```",
        f"```json\n{_EMPTY_EDITS}\n```\nDone.",
        f"```json\n{_EMPTY_EDITS}\n```\n```json\n{_EMPTY_EDITS}\n```",
    ],
)
def test_reproof_refuses_anything_but_one_whole_reply_fence(raw):
    with pytest.raises(ReproofResponseRefusal, match="not valid JSON"):
        assemble_reproof_response(raw, _request())
