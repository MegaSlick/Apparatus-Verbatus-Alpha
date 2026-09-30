"""The canonical uncertainty layer's own contract, tested where it is defined.

`pipeline/6_archetypus/test_record_schema.py` exercises this module through a
sealed record, which is the right place for the record's rules. What it cannot
reach is the projection step itself: `from_perlectio` renames the producer's
`testimonium_span` to `prior_span`, and a rename is exactly the kind of thing
that is only visible when the value is non-empty. Every record the pipeline
builds today carries an empty layer, so the rename travels untested through
every other suite in this repository.
"""

from __future__ import annotations

import pytest

from common.alignment import load_dissent_limits
from common.contracts import uncertainty as canonical_uncertainty
from common.contracts.errors import SchemaRefusal
from common.contracts.prior_draft import budget_stopped_comparisons, unmeasured_comparison
from common.contracts.uncertainty import from_perlectio, utf8_round_trip, validate
from conftest import load_stage

# Every layer below carries the reader's own assessment, which the canonical
# schema closes over: the two span layers alone cannot say whether an empty list
# is "no doubt" or "no doubt was ever asked for". `assessed` is the state these shape tests want,
# because it is the only one under which spans and gaps may be non-empty.
_ASSESSED = {"state": "assessed", "problem": None}
_EMPTY = {
    "uncertain_spans": [],
    "gaps": [],
    "self_revisions": [],
    "assessment": _ASSESSED,
    "lectio_kind": "primed-with-prior",
}


def test_source_revision_vocabulary_matches_the_perlector_producer() -> None:
    dissent = load_stage("4_perlector", "dissent")

    produced = dissent.departures("a", "b", load_dissent_limits()[0].max_comparison_steps)

    assert len(produced) == 1
    assert canonical_uncertainty._SOURCE_REVISION_FIELDS == frozenset(produced[0])


def test_withheld_draft_has_no_self_revision_measurement() -> None:
    payload = {
        "text": "Maria",
        "lectio_kind": "primed-draft-withheld",
        "self_revision": [],
        "uncertain_spans": [],
        "gaps": [],
        "uncertainty_assessment": _ASSESSED,
    }
    layer = from_perlectio(payload)
    assert layer["self_revisions"] is None
    assert layer["lectio_kind"] == "primed-draft-withheld"
    assert validate(layer, "Maria") == layer
    with pytest.raises(SchemaRefusal, match="not measured"):
        validate({**layer, "self_revisions": []}, "Maria")


def test_a_fed_draft_whose_comparison_ran_out_is_not_measured_rather_than_unrevised() -> None:
    """The Perlectio carries the explicit non-verdict; the canonical layer says
    not measured (null), never `[]`, which would claim the reading and the
    draft were compared and agreed."""
    payload = {
        "text": "Maria",
        "lectio_kind": "primed-with-prior",
        "self_revision": unmeasured_comparison(10),
        "uncertain_spans": [],
        "gaps": [],
        "uncertainty_assessment": _ASSESSED,
    }
    layer = from_perlectio(payload)
    assert layer["self_revisions"] is None
    assert validate(layer, "Maria") == layer
    # Only the exact closed record, and only for a fed draft.
    for forged in (
        {**unmeasured_comparison(10), "measured": True},
        {**unmeasured_comparison(10), "reason": "other"},
        {**unmeasured_comparison(10), "max_comparison_steps": 0},
        None,
    ):
        with pytest.raises(SchemaRefusal, match="not a list"):
            from_perlectio({**payload, "self_revision": forged})
    with pytest.raises(SchemaRefusal):
        from_perlectio({**payload, "lectio_kind": "primed-draft-withheld"})
    # An audit projection has no draft to have measured against.
    without_kind = {key: value for key, value in _EMPTY.items() if key != "lectio_kind"}
    with pytest.raises(SchemaRefusal, match="must be a list when measured"):
        canonical_uncertainty.validate_audit_projection(
            {**without_kind, "self_revisions": None}, "Maria"
        )


def test_a_canonical_layer_requires_its_lectio_kind() -> None:
    without_kind = {key: value for key, value in _EMPTY.items() if key != "lectio_kind"}
    with pytest.raises(SchemaRefusal, match="closed canonical schema"):
        validate(without_kind, "Maria")
    assert canonical_uncertainty.validate_audit_projection(without_kind, "Maria") == without_kind
    with pytest.raises(SchemaRefusal, match="closed canonical schema"):
        canonical_uncertainty.validate_audit_projection(_EMPTY, "Maria")
    with pytest.raises(SchemaRefusal, match="unknown lectio kind"):
        from_perlectio(
            {
                "text": "Maria",
                "self_revision": [],
                "uncertain_spans": [],
                "gaps": [],
                "uncertainty_assessment": _ASSESSED,
            }
        )


def test_whitespace_only_text_accepts_a_whole_act_gap() -> None:
    layer = {
        "uncertain_spans": [],
        "gaps": [{"position": "whole-act", "start": 0, "end": 0, "witness_evidence": []}],
        "self_revisions": [],
        "assessment": _ASSESSED,
        "lectio_kind": "primed-with-prior",
    }

    assert validate(layer, " \t\n") == layer


def test_whitespace_only_text_refuses_a_partly_read_gap_position() -> None:
    layer = {
        "uncertain_spans": [],
        "gaps": [{"position": "trailing", "start": 3, "end": 3, "witness_evidence": []}],
        "self_revisions": [],
        "assessment": _ASSESSED,
        "lectio_kind": "primed-with-prior",
    }

    with pytest.raises(SchemaRefusal, match="over an empty text"):
        validate(layer, " \t\n")


def test_an_internal_gap_before_only_closing_punctuation_is_refused() -> None:
    layer = {
        "uncertain_spans": [],
        "gaps": [{"position": "internal", "start": 3, "end": 3, "witness_evidence": []}],
        "self_revisions": [],
        "assessment": _ASSESSED,
        "lectio_kind": "primed-with-prior",
    }

    with pytest.raises(SchemaRefusal, match="declared internal"):
        validate(layer, "abc)")


def test_projection_renames_the_prior_draft_span_and_keeps_its_offsets() -> None:
    """`testimonium_span` indexes the prior draft, not a witness's report.

    `self_revision` reuses `departures()`, the same function that measures
    witness dissent, so its second span is named for the witness case it was
    written for. Carrying that name into an export would tell a recipient the
    offsets index a Testimonium. They index the Perlector's own earlier draft,
    and this is where the record starts saying so.
    """
    layer = from_perlectio(
        {
            "text": "Maria",
            "lectio_kind": "primed-with-prior",
            "uncertain_spans": [],
            "gaps": [],
            "uncertainty_assessment": {"state": "assessed", "problem": None},
            "self_revision": [
                {
                    "reading_span": {"start": 0, "end": 5},
                    "testimonium_span": {"start": 0, "end": 4},
                }
            ],
        }
    )

    assert layer["self_revisions"] == [
        {"reading_span": {"start": 0, "end": 5}, "prior_span": {"start": 0, "end": 4}}
    ]
    assert validate(layer, "Maria") == layer


def test_a_prior_span_is_not_bounded_by_the_established_text() -> None:
    """The prior draft is a string this layer never sees, and may be longer.

    A revision that cut characters leaves `prior_span` indexing past the end of
    what survived. Bounding it against the established text would refuse the
    ordinary case; only the non-negative and non-reversed rules apply.
    """
    layer = from_perlectio(
        {
            "text": "Mari",
            "lectio_kind": "primed-with-prior",
            "uncertain_spans": [],
            "gaps": [],
            "uncertainty_assessment": {"state": "assessed", "problem": None},
            "self_revision": [
                {
                    "reading_span": {"start": 4, "end": 4},
                    "testimonium_span": {"start": 4, "end": 40},
                }
            ],
        }
    )

    assert layer["self_revisions"][0]["prior_span"] == {"start": 4, "end": 40}


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ("not an object", "object Perlectio payload"),
        ({"text": "Maria", "self_revision": {}}, "self_revision is not a list"),
        (
            {
                "text": "Maria",
                "lectio_kind": "primed-with-prior",
                "uncertain_spans": [],
                "gaps": [],
                "uncertainty_assessment": {"state": "assessed", "problem": None},
                "self_revision": [
                    {
                        "reading_span": {"start": 0, "end": 5},
                        "testimonium_span": {"start": 0, "end": 5},
                        "unsupported": True,
                    }
                ],
            },
            r"self_revision\[0\].*closed source schema",
        ),
    ],
)
def test_projection_refuses_a_payload_it_cannot_canonicalize(payload, expected) -> None:
    with pytest.raises(SchemaRefusal, match=expected):
        from_perlectio(payload)


@pytest.mark.parametrize(
    ("layer", "text", "expected"),
    [
        (_EMPTY, None, "require exactly one string text field"),
        ({"uncertain_spans": [], "gaps": []}, "Maria", "closed canonical schema"),
        (
            {
                "uncertain_spans": {},
                "gaps": [],
                "self_revisions": [],
                "assessment": _ASSESSED,
                "lectio_kind": "primed-with-prior",
            },
            "Maria",
            "members must all be lists",
        ),
        (
            {
                "uncertain_spans": [],
                "gaps": [{"position": "internal", "start": 1, "end": 2, "witness_evidence": []}],
                "self_revisions": [],
                "assessment": _ASSESSED,
                "lectio_kind": "primed-with-prior",
            },
            "Maria",
            "not a zero-width canonical gap",
        ),
        (
            {
                "uncertain_spans": [],
                "gaps": [],
                "self_revisions": [
                    {"reading_span": {"start": 0, "end": 0}, "prior_span": {"start": 4, "end": 1}}
                ],
                "assessment": _ASSESSED,
                "lectio_kind": "primed-with-prior",
            },
            "Maria",
            "prior_span is reversed",
        ),
        # Both are in bounds over an empty text and both are refused: `leading`
        # starts at 0 and `trailing` ends at len("") whatever the text is, so the
        # bounds rules say nothing here and the position label alone would decide
        # whether a record holding no characters looked partly read.
        (
            {
                "uncertain_spans": [],
                "gaps": [{"position": "leading", "start": 0, "end": 0, "witness_evidence": []}],
                "self_revisions": [],
                "assessment": _ASSESSED,
                "lectio_kind": "primed-with-prior",
            },
            "",
            "over an empty text",
        ),
        (
            {
                "uncertain_spans": [],
                "gaps": [{"position": "trailing", "start": 0, "end": 0, "witness_evidence": []}],
                "self_revisions": [],
                "assessment": _ASSESSED,
                "lectio_kind": "primed-with-prior",
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
                "self_revisions": [],
                "assessment": _ASSESSED,
                "lectio_kind": "primed-with-prior",
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
                "self_revisions": [],
                "assessment": _ASSESSED,
                "lectio_kind": "primed-with-prior",
            },
            "Maria",
            r"uncertain_spans\[0\] is malformed",
        ),
    ],
)
def test_validation_refuses_a_layer_that_cannot_anchor(layer, text, expected) -> None:
    with pytest.raises(SchemaRefusal, match=expected):
        validate(layer, text)


def test_the_round_trip_asks_the_shape_question_before_its_own() -> None:
    """Callers rely on this to avoid validating the same arguments twice."""
    with pytest.raises(SchemaRefusal, match="closed canonical schema"):
        utf8_round_trip({"uncertain_spans": []}, "Maria")
    assert utf8_round_trip(_EMPTY, "Cǣsar d’Amours") is None


def test_a_reading_sealed_without_a_doubt_report_cannot_be_projected() -> None:
    """The absent field is refused, not defaulted.

    Defaulting it to `not-assessed` here would let this layer invent a fact
    about a call it never saw, and defaulting it to `assessed` would publish an
    empty layer as confidence. A record from before the assessment existed is
    re-read under the current contract instead.
    """
    with pytest.raises(SchemaRefusal, match="carries no uncertainty_assessment"):
        from_perlectio(
            {
                "text": "Maria",
                "lectio_kind": "primed-with-prior",
                "uncertain_spans": [],
                "gaps": [],
                "self_revision": [],
            }
        )


def test_an_unassessed_layer_is_an_absence_and_never_an_empty_confidence() -> None:
    """The whole point of F2: `not-assessed` with empty layers is valid and says so."""
    layer = from_perlectio(
        {
            "text": "Maria",
            "lectio_kind": "primed-with-prior",
            "uncertain_spans": [],
            "gaps": [],
            "uncertainty_assessment": {
                "state": "not-assessed",
                "problem": "this chair has no doubt channel",
            },
            "self_revision": [],
        }
    )

    assert layer["assessment"] == {
        "state": "not-assessed",
        "problem": "this chair has no doubt channel",
    }
    assert validate(layer, "Maria") == layer


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


def test_budget_stopped_comparisons_names_the_stops_and_refuses_an_unsealed_budget():
    payload = {
        "self_revision": unmeasured_comparison(7),
        "dissent": [
            {"chair": "b", "compared": "unknown", "reason": "stopped", "max_comparison_steps": 7},
            {"chair": "a", "compared": True},
            {"letter": "C", "compared": "unknown", "reason": "stopped", "max_comparison_steps": 7},
        ],
    }
    assert budget_stopped_comparisons(payload, 7, "a reading") == (True, ["C", "b"])
    assert budget_stopped_comparisons({**payload, "self_revision": []}, 7, "a reading")[0] is False
    with pytest.raises(SchemaRefusal, match="self_revision stopped on a 7-step .* sealed 8"):
        budget_stopped_comparisons(payload, 8, "a reading")
    unnamed = {"self_revision": [], "dissent": [{"max_comparison_steps": 7}]}
    with pytest.raises(SchemaRefusal, match="naming no witness"):
        budget_stopped_comparisons(unnamed, 7, "a reading")
