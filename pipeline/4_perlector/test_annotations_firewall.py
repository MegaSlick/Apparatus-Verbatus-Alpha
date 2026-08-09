"""The declared-gap firewall: a gap cannot claim testimony characters inside
`text`, and the schema refuses any record shaped that way.

Spec_08's sharpest requirement: "the established text never contains
testimony-supplied characters. No count of agreeing witnesses changes this."
"""

import annotations
import pytest

from common.contracts.errors import SchemaRefusal

# --- Uncertain spans: read text, bounds-checked -------------------------------


def test_a_well_formed_uncertain_span_validates():
    spans = [{"start": 2, "end": 5, "alternatives": ["abc", "abd"], "confidence": "low"}]
    assert annotations.validate_uncertain_spans(spans, "xxabcxx") == spans


def test_an_uncertain_span_outside_text_bounds_refuses():
    with pytest.raises(SchemaRefusal, match="outside text bounds"):
        annotations.validate_uncertain_spans(
            [{"start": 0, "end": 100, "alternatives": [], "confidence": "low"}], "short"
        )


def test_an_uncertain_span_with_start_after_end_refuses():
    with pytest.raises(SchemaRefusal, match="outside text bounds"):
        annotations.validate_uncertain_spans(
            [{"start": 5, "end": 2, "alternatives": [], "confidence": "low"}], "reading text"
        )


@pytest.mark.parametrize("confidence", ["certain", "maybe", "", None, 1])
def test_an_uncertain_span_with_an_undeclared_confidence_refuses(confidence):
    with pytest.raises(SchemaRefusal, match="confidence"):
        annotations.validate_uncertain_spans(
            [{"start": 0, "end": 1, "alternatives": [], "confidence": confidence}], "reading"
        )


def test_an_uncertain_span_with_a_non_string_alternative_refuses():
    with pytest.raises(SchemaRefusal, match="alternatives"):
        annotations.validate_uncertain_spans(
            [{"start": 0, "end": 1, "alternatives": [123], "confidence": "low"}], "reading"
        )


def test_an_uncertain_span_with_an_unexpected_field_refuses():
    with pytest.raises(SchemaRefusal, match="closed span schema"):
        annotations.validate_uncertain_spans(
            [
                {
                    "start": 0,
                    "end": 1,
                    "alternatives": [],
                    "confidence": "low",
                    "trust": "high",
                }
            ],
            "reading",
        )


# --- Gaps: the establishment firewall itself ----------------------------------

# One well-formed evidence row, spelled once. A gap's evidence names the chair,
# the Testimonium artifact that reported the variant, and the digest-checked
# reference to it -- the variant alone would be a claim nobody can trace back.
EVIDENCE = {
    "chair": "attestator_1",
    "testimonium_id": "testimonium-0001",
    "reference": {"relative_path": "3_attestatores/artifacts/t.json", "sha256": "a" * 64},
    "variant": "Tyrel",
}


def test_a_well_formed_zero_width_gap_validates():
    gaps = [
        {
            "position": "internal",
            "start": 3,
            "end": 3,
            "witness_evidence": [EVIDENCE],
        }
    ]
    assert annotations.validate_gaps(gaps, "abcxyz") == gaps


def test_the_firewall_refuses_a_fake_seat_that_fills_a_gap_from_testimony():
    """The attack this schema exists to catch: a "helpful" reader copies a
    witness's exact reported string into the gap's own span inside `text`,
    rather than leaving the gap zero-width and attaching the witness's words
    only as linked evidence. This must be refused regardless of what the
    smuggled characters equal -- the check does not even need to inspect
    `witness_evidence` to catch it."""
    witness_variant = "Tyrel"
    text = f"the child of {witness_variant}, baptised"
    gaps = [
        {
            "position": "internal",
            # The gap claims the exact span the witness's variant occupies in
            # `text` -- exactly the shape a fake chair would produce if it
            # "helpfully" filled an illegible name from testimony.
            "start": text.index(witness_variant),
            "end": text.index(witness_variant) + len(witness_variant),
            "witness_evidence": [EVIDENCE],
        }
    ]
    with pytest.raises(SchemaRefusal, match="establishment firewall"):
        annotations.validate_gaps(gaps, text)


def test_the_firewall_check_is_not_vacuous():
    """Prove the guard can go red: remove the `start != end` check and confirm
    the attack above would otherwise pass silently. This reimplements
    `validate_gaps` with that one check deleted, rather than monkeypatching
    the module, so the rest of the schema's behaviour is still exercised."""
    witness_variant = "Tyrel"
    text = f"the child of {witness_variant}, baptised"
    gap = {
        "position": "internal",
        "start": text.index(witness_variant),
        "end": text.index(witness_variant) + len(witness_variant),
        "witness_evidence": [EVIDENCE],
    }

    def validate_gaps_without_the_firewall_check(gaps, text):
        for candidate in gaps:
            # Every check except `start != end` -- proving the omission, not
            # merely asserting it.
            assert candidate["position"] in annotations.GAP_POSITIONS
            assert 0 <= candidate["start"] <= len(text)
            assert 0 <= candidate["end"] <= len(text)
        return gaps

    # With the firewall check removed, the attack is accepted.
    assert validate_gaps_without_the_firewall_check([gap], text) == [gap]
    # With it present (the real function), the identical input is refused.
    with pytest.raises(SchemaRefusal, match="establishment firewall"):
        annotations.validate_gaps([gap], text)


def test_a_leading_gap_must_start_at_zero():
    with pytest.raises(SchemaRefusal, match="leading"):
        annotations.validate_gaps(
            [{"position": "leading", "start": 2, "end": 2, "witness_evidence": []}], "abcdef"
        )


def test_a_trailing_gap_must_end_at_text_length():
    with pytest.raises(SchemaRefusal, match="trailing"):
        annotations.validate_gaps(
            [{"position": "trailing", "start": 2, "end": 2, "witness_evidence": []}], "abcdef"
        )


@pytest.mark.parametrize("position", [0, 6])
def test_an_internal_gap_must_be_strictly_inside_the_text(position):
    with pytest.raises(SchemaRefusal, match="strictly inside"):
        annotations.validate_gaps(
            [
                {
                    "position": "internal",
                    "start": position,
                    "end": position,
                    "witness_evidence": [],
                }
            ],
            "abcdef",
        )


def test_a_whole_act_gap_requires_empty_text():
    with pytest.raises(SchemaRefusal, match="whole-act"):
        annotations.validate_gaps(
            [{"position": "whole-act", "start": 0, "end": 0, "witness_evidence": []}], "not empty"
        )


def test_a_whole_act_gap_must_be_the_only_gap():
    gaps = [
        {"position": "whole-act", "start": 0, "end": 0, "witness_evidence": []},
        {"position": "whole-act", "start": 0, "end": 0, "witness_evidence": []},
    ]
    with pytest.raises(SchemaRefusal, match="only gap"):
        annotations.validate_gaps(gaps, "")


def test_witness_evidence_entries_are_a_closed_record():
    with pytest.raises(SchemaRefusal, match="witness_evidence"):
        annotations.validate_gaps(
            [
                {
                    "position": "internal",
                    "start": 1,
                    "end": 1,
                    "witness_evidence": [EVIDENCE | {"trust": "high"}],
                }
            ],
            "abc",
        )


def test_witness_evidence_must_name_the_testimonium_it_came_from():
    """A variant with no artifact behind it is a claim about a witness rather
    than the witness's own sealed record (GOALS 5)."""
    without_reference = {key: value for key, value in EVIDENCE.items() if key != "reference"}
    with pytest.raises(SchemaRefusal, match="witness_evidence"):
        annotations.validate_gaps(
            [
                {
                    "position": "internal",
                    "start": 1,
                    "end": 1,
                    "witness_evidence": [without_reference],
                }
            ],
            "abc",
        )


def test_a_gap_with_an_undeclared_position_refuses():
    with pytest.raises(SchemaRefusal, match="not one of"):
        annotations.validate_gaps(
            [{"position": "sideways", "start": 0, "end": 0, "witness_evidence": []}], "abc"
        )


# --- Bidirectional whole-act / no-readable-text consistency -------------------


def test_no_readable_text_outcome_requires_empty_text_and_a_whole_act_gap():
    with pytest.raises(SchemaRefusal, match="no-readable-text"):
        annotations.validate_whole_act_consistency(outcome="no-readable-text", text="", gaps=[])


def test_a_whole_act_gap_forces_the_no_readable_text_outcome():
    """The direction the design note originally missed: an outcome of `read`
    may not carry a whole-act gap and flow onward as though something had
    been established over an empty text."""
    gaps = [{"position": "whole-act", "start": 0, "end": 0, "witness_evidence": []}]
    with pytest.raises(SchemaRefusal, match="wholly illegible"):
        annotations.validate_whole_act_consistency(outcome="read", text="", gaps=gaps)


def test_the_consistent_no_readable_text_combination_validates():
    gaps = [{"position": "whole-act", "start": 0, "end": 0, "witness_evidence": []}]
    annotations.validate_whole_act_consistency(outcome="no-readable-text", text="", gaps=gaps)


def test_validate_annotations_enforces_bidirectional_consistency_when_outcome_supplied():
    payload = {
        "text": "",
        "gaps": [{"position": "whole-act", "start": 0, "end": 0, "witness_evidence": []}],
    }
    with pytest.raises(SchemaRefusal, match="wholly illegible"):
        annotations.validate_annotations(payload, outcome="read")
    # The same payload validates under the outcome it is actually consistent with.
    annotations.validate_annotations(payload, outcome="no-readable-text")


def test_validate_annotations_requires_a_text_field():
    with pytest.raises(SchemaRefusal, match="no text field"):
        annotations.validate_annotations({})
