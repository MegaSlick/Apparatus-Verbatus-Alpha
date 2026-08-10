"""Dissent on derived comparison views: equality only, never a distance
metric; a raw-string cross-check beside the normalized one; an honest
`"unknown"` for a witness format the comparator cannot yet reduce.
"""

import time
import unicodedata

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


def test_comparison_view_treats_precomposed_and_decomposed_accents_as_equal():
    """A precomposed 'e with acute' and a bare 'e' plus a combining acute
    render identically and are the same ink -- an OCR engine and a witness
    model are not guaranteed to agree on which Unicode form they emit for the
    same character, and parish-register French is full of exactly this."""
    precomposed = unicodedata.normalize("NFC", "baptisé")
    decomposed = unicodedata.normalize("NFD", precomposed)
    assert precomposed != decomposed, "the fixture must actually differ at the codepoint level"
    assert (
        dissent.comparison_view(precomposed)["normalized"]
        == dissent.comparison_view(decomposed)["normalized"]
    )


def test_composing_an_accent_is_not_counted_as_a_dropped_character():
    """`dropped_characters` is the whitespace collapse's account and nothing
    else. NFC re-encodes a character, it does not remove one, so a decomposed
    witness report must show zero loss -- otherwise every diacritic-heavy act
    in a French parish register carries a loss figure that is simply wrong, and
    a reader weighing `departed` against it is misled on exactly the text this
    normalization was added for."""
    decomposed = unicodedata.normalize("NFD", "baptisé le premier février")
    composed = unicodedata.normalize("NFC", decomposed)
    assert len(decomposed) > len(composed), "the fixture must actually decompose"
    assert dissent.comparison_view(decomposed)["dropped_characters"] == 0

    # And a real collapse is still counted, over the composed string.
    assert dissent.comparison_view(f"{decomposed}  x")["dropped_characters"] == 1


def test_a_normalization_form_difference_alone_produces_no_dissent():
    reading = unicodedata.normalize("NFC", "baptisé le premier février")
    reported = unicodedata.normalize("NFD", reading)
    rows = dissent.dissent_against(
        reading, [{"outcome": "read", "payload": {"chair": "attestator_1", "reported": reported}}]
    )
    assert rows[0]["departed"] is False
    assert rows[0]["departed_raw"] is True, (
        "the raw strings really do differ codepoint-for-codepoint; only the "
        "normalized view is expected to treat them as the same ink"
    )


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
            # The one whitespace character, located rather than merely counted.
            "departures": [
                {
                    "reading_span": {"start": 5, "end": 5},
                    "testimonium_span": {"start": 5, "end": 6},
                }
            ],
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


def test_departures_locate_the_one_character_the_witness_read_differently():
    """The instrument's whole point: one wrong letter in an otherwise identical
    reading must look different from wholesale disagreement, which one boolean
    per chair cannot express."""
    reading = "alpha beta gamma"
    testimonia = [
        {"outcome": "read", "payload": {"chair": "attestator_2", "reported": "alpha beta gamna"}}
    ]
    spans = dissent.dissent_against(reading, testimonia)[0]["departures"]
    assert spans == [
        {"reading_span": {"start": 14, "end": 15}, "testimonium_span": {"start": 14, "end": 15}}
    ]
    assert reading[14:15] == "m"


def test_an_agreeing_witness_produces_no_departures_at_all():
    """Zero dissent on an easy line is the correct output, not a missing
    measurement -- ARCHITECTURE, verbatim: "a metric that rewards disagreement
    rewards hallucination"."""
    testimonia = [
        {"outcome": "read", "payload": {"chair": "attestator_2", "reported": "alpha beta gamma"}}
    ]
    assert dissent.dissent_against("alpha beta gamma", testimonia)[0]["departures"] == []


def test_departures_are_an_alignment_and_expose_no_similarity_number():
    """The line this module must not cross. `get_opcodes` describes *where* two
    strings differ; `ratio()` is the metric a fuzzy-match picker would be built
    from, and no row may carry one."""
    testimonia = [
        {"outcome": "read", "payload": {"chair": "attestator_2", "reported": "entirely other"}}
    ]
    row = dissent.dissent_against("alpha beta gamma", testimonia)[0]
    for span in row["departures"]:
        assert set(span) == {"reading_span", "testimonium_span"}
        for bounds in span.values():
            assert set(bounds) == {"start", "end"}
            assert all(isinstance(value, int) for value in bounds.values())
    assert not any(
        isinstance(value, float) for value in row.values() if not isinstance(value, (dict, list))
    )


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


def test_a_runaway_witness_report_is_unknown_rather_than_aligned_for_twenty_minutes():
    """A witness's `reported` is a model's own output and nothing upstream bounds
    it. `SequenceMatcher` costs the product of the two lengths, so a model stuck
    in a repetition loop until its token cap would hold this stage for tens of
    minutes on every act it touched. The bound is on the comparison, not the
    text: neither string is clipped, and the chair keeps a visible row."""
    reading = "alpha beta gamma" * 700  # a ~11k-character act, already unrealistic
    runaway = "ab" * 60_000  # ~120k characters, a plausible 32k-token repetition loop
    assert len(reading) * len(runaway) > dissent.MAX_COMPARISON_CHARACTER_PAIRS

    started = time.monotonic()
    rows = dissent.dissent_against(
        reading, [{"outcome": "read", "payload": {"chair": "attestator_1", "reported": runaway}}]
    )
    assert time.monotonic() - started < 5, "the bound must stop the alignment, not merely note it"
    assert rows[0]["chair"] == "attestator_1", "the witness must not vanish from the record"
    assert rows[0]["compared"] == "unknown"
    assert "did not run" in rows[0]["reason"]


def test_a_long_but_affordable_comparison_is_still_genuinely_aligned():
    """Prove the bound is not swallowing ordinary work: an act far longer than a
    real register entry still gets its real spans."""
    reading = "alpha beta gamma " * 200
    reported = reading[:-6] + "gamna "
    rows = dissent.dissent_against(
        reading, [{"outcome": "read", "payload": {"chair": "attestator_1", "reported": reported}}]
    )
    assert rows[0]["compared"] is True
    assert rows[0]["departures"], "an affordable comparison must still locate its departures"


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
