"""Dissent on derived comparison views: equality only, never a distance
metric; a raw-string cross-check beside the normalized one; an honest
`"unknown"` for a witness format the comparator cannot yet reduce.
"""

import dissent
import pytest


def test_comparison_view_collapses_whitespace_and_reports_what_it_dropped():
    view = dissent.comparison_view("alpha   beta\tgamma")
    assert view["normalized"] == "alpha beta gamma"
    assert view["dropped_characters"] == len("alpha   beta\tgamma") - len("alpha beta gamma")


def test_comparison_view_never_folds_case():
    """A case difference is a real disagreement about the ink, not a
    formatting artifact this normalization may erase."""
    view = dissent.comparison_view("Alpha")
    assert view["normalized"] == "Alpha"


def test_a_witness_that_agrees_after_whitespace_normalization_departs_only_raw():
    testimonia = [
        {
            "outcome": "read",
            "payload": {"chair": "attestator_1", "reported": "alpha  beta gamma"},
        }
    ]
    rows = dissent.dissent_against("alpha beta gamma", testimonia)
    assert rows == [
        {
            "chair": "attestator_1",
            "compared": True,
            "departed": False,
            "departed_raw": True,
            "comparison_loss": {"reading_dropped_characters": 0, "witness_dropped_characters": 1},
        }
    ]


def test_a_witness_that_actually_disagrees_departs_on_both_views():
    testimonia = [
        {"outcome": "read", "payload": {"chair": "attestator_2", "reported": "alpha beta gamna"}}
    ]
    rows = dissent.dissent_against("alpha beta gamma", testimonia)
    assert rows[0]["departed"] is True
    assert rows[0]["departed_raw"] is True


def test_a_non_reading_outcome_is_recorded_as_no_opinion_not_agreement():
    """Silence is not assent: a chair that failed or never ran gets
    `compared: False` with its outcome as the reason, never folded into the
    agreeing set."""
    for outcome in ("failed", "dead", "not-run", "excluded"):
        rows = dissent.dissent_against(
            "reading", [{"outcome": outcome, "payload": {"chair": "attestator_3"}}]
        )
        assert rows == [{"chair": "attestator_3", "compared": False, "reason": outcome}]


def test_a_witness_whose_format_can_express_uncertainty_is_unknown_not_guessed():
    """No live producer declares this today (`is_comparable`'s own docstring):
    forged directly onto a bare record, the same technique
    `test_testimonia_latest_attempt.py` already uses to exercise a boundary no
    live producer reaches yet either."""
    testimonia = [
        {
            "outcome": "read",
            "payload": {
                "chair": "attestator_2",
                "reported": "alpha [beta|beeta] gamma",
                "format_capabilities": {"can_express_uncertainty": True},
            },
        }
    ]
    rows = dissent.dissent_against("alpha beta gamma", testimonia)
    assert rows == [
        {
            "chair": "attestator_2",
            "compared": "unknown",
            "reason": (
                "this witness's declared format cannot be reduced to a plain comparison view"
            ),
        }
    ]


def test_is_comparable_defaults_true_when_a_testimonium_declares_no_capabilities():
    assert dissent.is_comparable({"payload": {"format_capabilities": {}}}) is True
    assert dissent.is_comparable({"payload": {}}) is True


def test_dissent_never_drops_an_unknown_chair_from_the_record():
    """An incomparable witness must stay visible in the list -- never silently
    absent, which would look identical to a chair the run never configured."""
    testimonia = [
        {
            "outcome": "read",
            "payload": {
                "chair": "attestator_1",
                "reported": "alpha beta gamma",
                "format_capabilities": {"can_express_uncertainty": True},
            },
        },
        {"outcome": "read", "payload": {"chair": "attestator_2", "reported": "alpha beta gamma"}},
    ]
    rows = dissent.dissent_against("alpha beta gamma", testimonia)
    chairs = {row["chair"] for row in rows}
    assert chairs == {"attestator_1", "attestator_2"}
    unknown_row = next(row for row in rows if row["chair"] == "attestator_1")
    assert unknown_row["compared"] == "unknown"


def test_a_completed_reading_outcome_with_no_reported_text_refuses():
    with pytest.raises(Exception, match="no text to compare"):
        dissent.dissent_against(
            "reading", [{"outcome": "read", "payload": {"chair": "attestator_1"}}]
        )


def test_this_module_pins_equality_only_and_takes_no_similarity_parameter():
    """Structural pin, not merely a docstring promise: `comparison_view` must
    never grow a threshold, weight, or distance-metric parameter. A reviewer
    changing this file should find this test and refuse the change rather
    than update it to match."""
    import inspect

    parameters = inspect.signature(dissent.comparison_view).parameters
    assert list(parameters) == ["text"], (
        "comparison_view must take no per-chair parameter and no similarity "
        "threshold -- 'closest match' needs a metric, and refusing metrics is "
        "what keeps a normalization from becoming a fuzzy-match picker"
    )
