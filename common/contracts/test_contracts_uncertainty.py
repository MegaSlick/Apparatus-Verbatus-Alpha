"""The canonical uncertainty layer's own contract, tested where it is defined."""

from __future__ import annotations

import pytest

from common.contracts import uncertainty as canonical_uncertainty
from common.contracts.errors import SchemaRefusal
from common.contracts.uncertainty import utf8_round_trip, validate

# Every layer below carries the reader's own assessment, which the canonical
# schema closes over: the two span layers alone cannot say whether an empty list
# is "no doubt" or "no doubt was ever asked for". `assessed` is the state these shape tests want,
# because it is the only one under which spans and gaps may be non-empty.
_ASSESSED = {"state": "assessed", "problem": None}
_EMPTY = {
    "uncertain_spans": [],
    "gaps": [],
    "self_revisions": None,
    "assessment": _ASSESSED,
    "lectio_kind": "page-read",
}


def test_a_gap_may_not_hold_the_characters_a_witness_reported() -> None:
    """A gap is where sight failed. Filling it with a witness's word, however well the
    witnesses agree, is refused for its width alone, with every other rule met."""
    text = "the child of Jean, baptised"
    evidence = {
        "chair": "attestator_1",
        "testimonium_id": "testimonium-0001",
        "reference": {"relative_path": "3_attestatores/artifacts/t.json", "sha256": "a" * 64},
        "variant": "Jean",
    }
    start = text.index("Jean")
    gap = {"position": "internal", "start": start, "end": start, "witness_evidence": [evidence]}
    validate({**_EMPTY, "gaps": [gap]}, text)
    with pytest.raises(SchemaRefusal, match="not a zero-width canonical gap"):
        validate({**_EMPTY, "gaps": [{**gap, "end": start + len("Jean")}]}, text)


def test_whitespace_only_text_accepts_a_whole_act_gap() -> None:
    layer = {
        "uncertain_spans": [],
        "gaps": [{"position": "whole-act", "start": 0, "end": 0, "witness_evidence": []}],
        "self_revisions": None,
        "assessment": _ASSESSED,
        "lectio_kind": "page-read",
    }

    assert validate(layer, " \t\n") == layer


def test_whitespace_only_text_refuses_a_partly_read_gap_position() -> None:
    layer = {
        "uncertain_spans": [],
        "gaps": [{"position": "trailing", "start": 3, "end": 3, "witness_evidence": []}],
        "self_revisions": None,
        "assessment": _ASSESSED,
        "lectio_kind": "page-read",
    }

    with pytest.raises(SchemaRefusal, match="over an empty text"):
        validate(layer, " \t\n")


def test_an_internal_gap_before_only_closing_punctuation_is_refused() -> None:
    layer = {
        "uncertain_spans": [],
        "gaps": [{"position": "internal", "start": 3, "end": 3, "witness_evidence": []}],
        "self_revisions": None,
        "assessment": _ASSESSED,
        "lectio_kind": "page-read",
    }

    with pytest.raises(SchemaRefusal, match="declared internal"):
        validate(layer, "abc)")


@pytest.mark.parametrize(
    ("layer", "text", "expected"),
    [
        (_EMPTY, None, "require exactly one string text field"),
        ({"uncertain_spans": [], "gaps": []}, "Maria", "closed canonical schema"),
        (
            {
                "uncertain_spans": {},
                "gaps": [],
                "self_revisions": None,
                "assessment": _ASSESSED,
                "lectio_kind": "page-read",
            },
            "Maria",
            "members must all be lists",
        ),
        (
            {
                "uncertain_spans": [],
                "gaps": [{"position": "internal", "start": 1, "end": 2, "witness_evidence": []}],
                "self_revisions": None,
                "assessment": _ASSESSED,
                "lectio_kind": "page-read",
            },
            "Maria",
            "not a zero-width canonical gap",
        ),
        # Both are in bounds over an empty text and both are refused: `leading`
        # starts at 0 and `trailing` ends at len("") whatever the text is, so the
        # bounds rules say nothing here and the position label alone would decide
        # whether a record holding no characters looked partly read.
        (
            {
                "uncertain_spans": [],
                "gaps": [{"position": "leading", "start": 0, "end": 0, "witness_evidence": []}],
                "self_revisions": None,
                "assessment": _ASSESSED,
                "lectio_kind": "page-read",
            },
            "",
            "over an empty text",
        ),
        (
            {
                "uncertain_spans": [],
                "gaps": [{"position": "trailing", "start": 0, "end": 0, "witness_evidence": []}],
                "self_revisions": None,
                "assessment": _ASSESSED,
                "lectio_kind": "page-read",
            },
            "",
            "over an empty text",
        ),
        (
            {
                "uncertain_spans": [
                    {"start": True, "end": 2, "alternatives": [], "confidence": "low"}
                ],
                "gaps": [],
                "self_revisions": None,
                "assessment": _ASSESSED,
                "lectio_kind": "page-read",
            },
            "Maria",
            "non-integer offsets",
        ),
        (
            {
                "uncertain_spans": [
                    {"start": 0, "end": 2, "alternatives": ["Ma"], "confidence": "certain"}
                ],
                "gaps": [],
                "self_revisions": None,
                "assessment": _ASSESSED,
                "lectio_kind": "page-read",
            },
            "Maria",
            r"uncertain_spans\[0\] is malformed",
        ),
        # Unhashable values from JSON: a named refusal, never a TypeError out of `in`.
        (
            {
                "uncertain_spans": [
                    {"start": 0, "end": 2, "alternatives": ["Ma"], "confidence": ["low"]}
                ],
                "gaps": [],
                "self_revisions": None,
                "assessment": _ASSESSED,
                "lectio_kind": "page-read",
            },
            "Maria",
            r"uncertain_spans\[0\] is malformed",
        ),
        (
            {
                "uncertain_spans": [],
                "gaps": [
                    {"position": {"internal": 1}, "start": 2, "end": 2, "witness_evidence": []}
                ],
                "self_revisions": None,
                "assessment": _ASSESSED,
                "lectio_kind": "page-read",
            },
            "Maria",
            r"gaps\[0\] position",
        ),
    ],
)
def test_validation_refuses_a_layer_that_cannot_anchor(layer, text, expected) -> None:
    with pytest.raises(SchemaRefusal, match=expected):
        validate(layer, text)


def test_a_layer_of_another_lectio_kind_is_refused() -> None:
    with pytest.raises(SchemaRefusal, match="unknown lectio kind"):
        validate(dict(_EMPTY, lectio_kind="primed-with-prior"), "Maria")


def test_a_persons_correction_carries_only_the_fixed_no_doubt_record() -> None:
    layer = canonical_uncertainty.corrected_layer()
    assert validate(layer, "Jean de la Roche") == layer
    assert layer["assessment"] == {
        "state": "not-assessed",
        "problem": canonical_uncertainty.CORRECTED_PROBLEM,
    }
    with pytest.raises(SchemaRefusal, match="carries no machine doubt layer"):
        validate(dict(layer, uncertain_spans=[{"start": 0, "end": 1}]), "Jean")
    with pytest.raises(SchemaRefusal, match="carries no machine doubt layer"):
        validate(dict(layer, assessment=_ASSESSED), "Jean")


def test_the_round_trip_asks_the_shape_question_before_its_own() -> None:
    """Callers rely on this to avoid validating the same arguments twice."""
    with pytest.raises(SchemaRefusal, match="closed canonical schema"):
        utf8_round_trip({"uncertain_spans": []}, "Maria")
    assert utf8_round_trip(_EMPTY, "Cǣsar d’Amours") is None


@pytest.mark.parametrize(
    ("assessment", "expected"),
    [
        ("not an object", "no closed assessment record"),
        ({"state": "assessed"}, "no closed assessment record"),
        ({"state": "assessed", "problem": None, "extra": 1}, "no closed assessment record"),
        ({"state": "confident", "problem": "why"}, "unknown assessment state"),
        # Unhashable, so `in` against the frozenset raises `TypeError` unless
        # the type guard leads: a resealed record would crash a consumer
        # instead of being refused by name.
        ({"state": ["assessed"], "problem": "why"}, "unknown assessment state"),
        ({"state": "malformed", "problem": 7}, "problem is not null or a string"),
        ({"state": "malformed", "problem": ""}, "problem is not null or a string"),
        # Both halves of the coupling: an unassessed report that says nothing
        # about why, and an assessed one carrying a problem it cannot have had.
        ({"state": "not-assessed", "problem": None}, "carries a problem exactly when"),
        ({"state": "assessed", "problem": "a doubt"}, "carries a problem exactly when"),
    ],
)
def test_validation_refuses_an_assessment_that_says_nothing_usable(assessment, expected) -> None:
    layer = dict(_EMPTY, assessment=assessment)

    with pytest.raises(SchemaRefusal, match=expected):
        validate(layer, "Maria")


def _page_perlectio(**changes):
    assessment = {"state": "assessed", "problem": None, "uncertain_spans": [], "gaps": []}
    payload = {
        "text": "abc",
        "uncertain_spans": [],
        "gaps": [],
        "uncertainty_assessment": assessment,
    }
    payload.update(changes)
    return payload


def test_a_page_reading_is_its_own_lectio_kind_with_no_measured_revisions() -> None:
    layer = canonical_uncertainty.from_page_perlectio(_page_perlectio())
    assert (layer["lectio_kind"], layer["self_revisions"]) == ("page-read", None)
    with pytest.raises(SchemaRefusal, match="self-revisions are not measured"):
        validate({**layer, "self_revisions": []}, "abc")


def test_a_page_reading_with_no_doubt_report_is_refused() -> None:
    with pytest.raises(SchemaRefusal, match="carries no uncertainty_assessment"):
        canonical_uncertainty.from_page_perlectio(_page_perlectio(uncertainty_assessment=None))


def test_a_page_reading_s_spans_must_be_the_copy_in_its_assessment() -> None:
    span = {"start": 0, "end": 1, "alternatives": [], "confidence": "low"}
    with pytest.raises(SchemaRefusal, match="uncertain_spans differ"):
        canonical_uncertainty.from_page_perlectio(_page_perlectio(uncertain_spans=[span]))
    with pytest.raises(SchemaRefusal, match="gaps differ"):
        canonical_uncertainty.from_page_perlectio(
            _page_perlectio(
                uncertainty_assessment={"state": "assessed", "problem": None, "uncertain_spans": []}
            )
        )
