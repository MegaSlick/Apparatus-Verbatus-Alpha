"""`change_record`'s attribution, and the one-byte suffix-trim coincidence.

A witness-derived (`testimony-diff`) flag's location is itself computed by
`text_change_span`, trimming a common suffix against the one testimony that
located it. `change_record` trims a common suffix too, against the actual
re-proof result. Both trims are exact for the pair they compare, but the two
pairs are different, so a trailing character `before` shares with testimony
by coincidence — not because it is genuinely unchanged — can leave a flag's
recorded end one byte short of a re-proof envelope that reaches the true end
of the text. These tests pin the fix (a one-byte gap at `len(before)` for a
witness-derived flag is still contained) and its boundary (any wider gap, or
a gap against a non-witness-derived flag, still refuses).
"""

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
    change_record,
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


def test_reproof_edits_assemble_only_from_exact_unicode_anchored_originals():
    request = _request()
    raw = json.dumps(
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


def test_legacy_v2_request_stays_validatable_but_cannot_mix_with_a_v3_edit_reply():
    request = audit_request(
        act_key="a1",
        attempt_ordinal=1,
        draft_ref={"relative_path": "4_perlector/audit-draft/a1.json", "sha256": "a" * 64},
        semi_final_text="alpha beta",
        flags=[{"class": "testimony-diff", "location": {"start": 6, "end": 10}}],
        policy_schema=LEGACY_SCHEMA,
    )
    assert request["schema"] == LEGACY_REQUEST_SCHEMA
    with pytest.raises(ReproofResponseRefusal, match="legacy request"):
        assemble_reproof_response(
            json.dumps({"schema": "perlector-audit-response.v1", "edits": []}), request
        )


def _flag(flag_class: str, start: int, end: int) -> dict:
    return {"class": flag_class, "location": {"start": start, "end": end}}


def test_text_change_span_trims_shared_prefix_and_suffix():
    assert text_change_span("abcXdef", "abcYdef") == (3, 4)
    assert text_change_span("same text", "same text") == (9, 9)
    assert text_change_span("abc", "abcXYZ") == (3, 3)


def test_a_witness_derived_flag_shy_by_the_shared_final_character_still_contains_the_reproof():
    """The exact coincidence: a witness and the reading share a final character.

    `before` ends "...kappa"; the testimony that located the flag also ends
    in "a", so `text_change_span` trims that one shared byte off the flag's
    end (`len(before) - 1`, not `len(before)`). The re-proof rewrites the
    trailing word entirely (a real tail rewrite, the production shape named
    in the diagnosis) to something that does *not* end in "a", so the
    re-proof's own change span reaches the true end of the text untrimmed.
    Before the fix this refused; the fix credits the one-byte gap to the
    flag whose own trim produced it.
    """
    before = "reading alpha beta kappa"
    after = "reading alpha beta epsilon"
    flag_end = len(before) - 1  # what a witness ending in the same "a" trims to
    flags = [_flag("testimony-diff", 19, flag_end)]

    changes = change_record(before, after, flags)

    assert changes == [{"start": 19, "end": len(before), "triggering_flag_class": "testimony-diff"}]


def test_a_real_overrun_past_a_witness_derived_flag_still_refuses():
    """More than one byte past a witness-derived flag's end is real content.

    Here the re-proof changes two trailing words, not one, so its envelope
    reaches two characters past the flag's suffix-trimmed end rather than
    one. That is a genuine escape from the flagged location, and the one-byte
    slack the fix adds must not swallow it. The flag starts exactly where the
    re-proof's own change span starts (19), so it is the gap bound — not the
    `start <= location["start"]` overlap check — that must do the refusing.
    """
    before = "reading alpha beta gamma kappa"
    after = "reading alpha beta ZZZZZ YYYYY"
    flag_end = before.index("gamma") + len("gamma")  # only "gamma" was ever flagged
    flags = [_flag("testimony-diff", 19, flag_end)]

    with pytest.raises(SchemaRefusal, match="changed text outside every flagged location"):
        change_record(before, after, flags)


def test_the_one_byte_slack_is_refused_for_a_non_witness_derived_flag():
    """`date-sequence`, `numbering`, `order`, `repetition` and `within-crop`
    locations are not suffix-trimmed against a testimony this function never
    sees, so the coincidence the slack exists for cannot occur for them. A
    one-byte gap past one of these flags must still refuse.
    """
    before = "reading alpha beta kappa"
    after = "reading alpha beta epsilon"
    flag_end = len(before) - 1
    flags = [_flag("repetition", 19, flag_end)]

    with pytest.raises(SchemaRefusal, match="changed text outside every flagged location"):
        change_record(before, after, flags)


def test_a_gap_that_does_not_reach_the_true_end_of_the_text_still_refuses():
    """The slack only ever applies at `len(before)`: a suffix trim can only
    ever fall short at the true end of a string, never in the middle, so a
    one-byte-short flag whose gap sits short of `len(before)` is not this
    coincidence and must still refuse.
    """
    before = "reading alpha beta kappa trailing tail"
    after = "reading alpha beta epsilon trailing tail"
    flag_end = before.index("kappa") + len("kappa") - 1  # one byte short, mid-string
    flags = [_flag("testimony-diff", 19, flag_end)]

    with pytest.raises(SchemaRefusal, match="changed text outside every flagged location"):
        change_record(before, after, flags)


def test_a_change_disjoint_from_the_flag_still_refuses_despite_the_slack():
    """The slack credits a re-proof that reaches *into* the flag through the
    coincidental byte, not one that starts where the flag already ended.

    `before` and `after` differ only in their final byte, so the re-proof's
    own change span is a single character sitting immediately after the
    flag's end -- disjoint from the disagreement the flag located, not an
    extension through it. The one-byte gap-at-`len(before)` shape is
    identical to the credited coincidence; only the envelope's failure to
    overlap the flag distinguishes it, so this pins that overlap check.
    """
    before = "abXa"
    after = "abXb"
    flags = [_flag("testimony-diff", 2, 3)]

    with pytest.raises(SchemaRefusal, match="changed text outside every flagged location"):
        change_record(before, after, flags)


def test_change_record_attributes_to_the_narrowest_containing_flag():
    before = "one two three four"
    after = "one TWO three four"
    flags = [_flag("date-sequence", 0, len(before)), _flag("testimony-diff", 4, 7)]

    changes = change_record(before, after, flags)

    assert changes == [{"start": 4, "end": 7, "triggering_flag_class": "testimony-diff"}]


def test_change_record_returns_nothing_for_identical_text():
    assert change_record("same", "same", [_flag("testimony-diff", 0, 4)]) == []


@pytest.mark.parametrize("change_renderer", [False, True])
def test_renderer_identity_survives_unrelated_module_edits(tmp_path, change_renderer):
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
    if change_renderer:
        original = "Re-examine only the requested character locations against the ink."
        assert source.count(original) == 1
        source = source.replace(original, "Re-examine the requested locations against the ink.")
    else:
        source += "\n# An unrelated maintenance edit outside the renderer.\n"
    changed_path = tmp_path / "changed_perlector_audit.py"
    changed_path.write_text(source)
    spec = importlib.util.spec_from_file_location("changed_perlector_audit", changed_path)
    assert spec is not None and spec.loader is not None
    changed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(changed)
    if change_renderer:
        assert changed.AUDIT_PROMPT_RENDERER_SHA256 != evidence["renderer_sha256"]
        with pytest.raises(SchemaRefusal, match="another renderer revision"):
            changed.validate_audit_prompt_evidence(evidence, request=request)
    else:
        assert changed.AUDIT_PROMPT_RENDERER_SHA256 == evidence["renderer_sha256"]
        assert changed.validate_audit_prompt_evidence(evidence, request=request) == evidence
