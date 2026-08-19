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

import importlib.util
from pathlib import Path

import pytest

from common.contracts import uncertainty as canonical_uncertainty
from common.contracts.errors import SchemaRefusal
from common.contracts.uncertainty import from_perlectio, utf8_round_trip, validate

ROOT = Path(__file__).resolve().parents[2]
_EMPTY = {"uncertain_spans": [], "gaps": [], "self_revisions": []}


def test_canonical_vocabulary_matches_the_perlector_producer() -> None:
    path = ROOT / "pipeline/4_perlector/annotations.py"
    spec = importlib.util.spec_from_file_location("perlector_annotations_contract_drift", path)
    annotations = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(annotations)

    assert canonical_uncertainty._CONFIDENCE == annotations.CONFIDENCE_LEVELS
    assert canonical_uncertainty._GAP_POSITIONS == annotations.GAP_POSITIONS


def test_source_revision_vocabulary_matches_the_perlector_producer() -> None:
    path = ROOT / "pipeline/4_perlector/dissent.py"
    spec = importlib.util.spec_from_file_location("perlector_dissent_contract_drift", path)
    dissent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dissent)

    produced = dissent.departures("a", "b")

    assert len(produced) == 1
    assert canonical_uncertainty._SOURCE_REVISION_FIELDS == frozenset(produced[0])


def test_whitespace_only_text_accepts_a_whole_act_gap() -> None:
    layer = {
        "uncertain_spans": [],
        "gaps": [{"position": "whole-act", "start": 0, "end": 0, "witness_evidence": []}],
        "self_revisions": [],
    }

    assert validate(layer, " \t\n") == layer


def test_whitespace_only_text_refuses_a_partly_read_gap_position() -> None:
    layer = {
        "uncertain_spans": [],
        "gaps": [{"position": "trailing", "start": 3, "end": 3, "witness_evidence": []}],
        "self_revisions": [],
    }

    with pytest.raises(SchemaRefusal, match="over an empty text"):
        validate(layer, " \t\n")


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
            "uncertain_spans": [],
            "gaps": [],
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
            "uncertain_spans": [],
            "gaps": [],
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
                "uncertain_spans": [],
                "gaps": [],
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
            {"uncertain_spans": {}, "gaps": [], "self_revisions": []},
            "Maria",
            "members must all be lists",
        ),
        (
            {
                "uncertain_spans": [],
                "gaps": [{"position": "internal", "start": 1, "end": 2, "witness_evidence": []}],
                "self_revisions": [],
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
            },
            "",
            "over an empty text",
        ),
        (
            {
                "uncertain_spans": [],
                "gaps": [{"position": "trailing", "start": 0, "end": 0, "witness_evidence": []}],
                "self_revisions": [],
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
