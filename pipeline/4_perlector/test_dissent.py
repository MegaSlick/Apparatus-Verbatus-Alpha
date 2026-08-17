"""Dissent on derived comparison views: equality only, never a distance
metric; a raw-string cross-check beside the normalized one; an honest
`"unknown"` for a witness format the comparator cannot yet reduce.
"""

import signal
import threading
import time
import unicodedata

import dissent
import pytest

from common.contracts.errors import SchemaRefusal


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

    def scalars(value):
        if isinstance(value, dict):
            for item in value.values():
                yield from scalars(item)
        elif isinstance(value, list):
            for item in value:
                yield from scalars(item)
        else:
            yield value

    assert not any(isinstance(value, float) for value in scalars(row)), (
        "a float anywhere in a dissent row is a similarity score wearing a shape; "
        "refusing ratios is what keeps the comparison from becoming a fuzzy-match picker"
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
    """A capability-declared chair with no act-anchored comparison view (R4's
    alignment) stays honestly unmeasurable -- forged directly onto a bare
    record, the same technique `test_testimonia_latest_attempt.py` already
    uses to exercise a boundary no live act-scoped producer reaches."""
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


def test_a_page_witness_comparison_view_lifts_the_capability_exemption():
    """The same capability-declared format becomes measurable once R4's
    alignment hands it an act-anchored, markup-stripped `comparison_reported`
    view -- the exemption is about an unsafe raw report, not the chair."""
    testimonia = [
        {
            "outcome": "read",
            "payload": {
                "chair": "attestator_2",
                "reported": "alpha [beta|beeta] gamma",
                "comparison_reported": "alpha beta gamma",
                "format_capabilities": {"can_express_uncertainty": True},
            },
        }
    ]
    rows = dissent.dissent_against("alpha beta gamma", testimonia)
    assert rows[0]["compared"] is True


def test_a_runaway_witness_report_is_unknown_rather_than_aligned_for_twenty_minutes():
    """A witness's `reported` is a model's own output and nothing upstream bounds
    it. `SequenceMatcher` costs the product of the two lengths, so a model stuck
    in a repetition loop until its token cap would hold this stage for tens of
    minutes on every act it touched. The bound is on the comparison, not the
    text: neither string is clipped, and the chair keeps a visible row."""
    reading = "alpha beta gamma" * 700  # a ~11k-character act, already unrealistic
    runaway = "ab" * 60_000  # ~120k characters, a plausible 32k-token repetition loop
    assert len(reading) * len(runaway) > dissent.MAX_COMPARISON_CHARACTER_PAIRS

    # D-11: no wall-clock assert here. The prefilter this test exercises rejects
    # before `SequenceMatcher` ever runs, so timing it adds a failure mode on a
    # loaded box without adding a guarantee -- the functional asserts below
    # (the row survives, "unknown", "did not run") already prove the bound was
    # taken, and would themselves fail if the prefilter stopped firing.
    rows = dissent.dissent_against(
        reading, [{"outcome": "read", "payload": {"chair": "attestator_1", "reported": runaway}}]
    )
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


@pytest.mark.skipif(
    not hasattr(signal, "SIGALRM"),
    reason="the wall-clock backstop is a SIGALRM mechanism; where it cannot exist the "
    "comparison runs unbounded and this test would hang for minutes to say nothing",
)
def test_a_scattered_difference_comparison_well_under_the_pair_bound_is_still_stopped(
    monkeypatch,
):
    """`SequenceMatcher`'s cost is not the product of the two lengths -- text that
    differs in many scattered places (exactly what a systematically-mistaken
    witness produces) runs close to the *cube* of the length instead. Measured
    in this chamber: a 6,800-character scattered comparison took 127 seconds
    unbounded, while its pair count (~46M) is under
    `MAX_COMPARISON_CHARACTER_PAIRS` (100M) -- so the pair-count prefilter alone
    would let it run. The deadline is pinned to one second here so the test
    asserts the *mechanism* -- the alarm interrupting a comparison the prefilter
    admitted -- with a ~100x margin over the measured cost, rather than racing
    the production five-second bound on whatever hardware runs the suite."""
    reading = "alpha beta gamma " * 400
    reported = "alpha beta gamna " * 400
    pairs = len(reading) * len(reported)
    assert pairs < dissent.MAX_COMPARISON_CHARACTER_PAIRS, "the pair prefilter must not catch this"

    monkeypatch.setattr(dissent, "MAX_COMPARISON_SECONDS", 1)
    started = time.monotonic()
    rows = dissent.dissent_against(
        reading, [{"outcome": "read", "payload": {"chair": "attestator_1", "reported": reported}}]
    )
    elapsed = time.monotonic() - started
    # Generous headroom on purpose: the alarm fires at one second; the margin
    # covers a loaded CI machine, not the property under test.
    assert elapsed < 10, "the wall-clock bound must stop the alignment"
    assert rows[0]["chair"] == "attestator_1", "the witness must not vanish from the record"
    assert rows[0]["compared"] == "unknown"
    assert "did not align within" in rows[0]["reason"]


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
    with pytest.raises(SchemaRefusal, match="no text to compare"):
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


# --- F-X4 (R4 audit, Opus seat 3): the comparison deadline owns its own alarm ---


@pytest.mark.skipif(
    not all(hasattr(signal, name) for name in ("SIGALRM", "ITIMER_REAL")),
    reason="requires the POSIX real-time alarm this backstop is built on",
)
def test_the_comparison_deadline_leaves_a_callers_own_alarm_alone():
    """`SIGALRM` is process-global. Arming unconditionally replaced a caller's
    real-time timer and then cancelled it in `finally`, destroying a deadline
    this module never owned -- the same defect `common/alignment.py` closed as
    F-L3, still open in its sibling on the same call path."""
    previous_handler = signal.getsignal(signal.SIGALRM)

    def caller_handler(signum, frame):
        pass

    signal.signal(signal.SIGALRM, caller_handler)
    signal.setitimer(signal.ITIMER_REAL, 30.0)
    try:
        result = dissent._aligned_within_deadline("alpha beta", "alpha beta", seconds=1)

        assert result == []
        remaining, _ = signal.getitimer(signal.ITIMER_REAL)
        assert remaining > 0, "alignment cancelled a timer it did not own"
        assert signal.getsignal(signal.SIGALRM) is caller_handler
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, previous_handler)


@pytest.mark.skipif(
    not all(hasattr(signal, name) for name in ("SIGALRM", "ITIMER_REAL")),
    reason="requires the POSIX real-time alarm this backstop is built on",
)
def test_the_comparison_runs_off_the_main_thread_without_touching_signal_state():
    """`signal.signal` raises outright from a non-main thread, so the bounded
    comparison could not run there at all. It degrades to an unbounded run --
    the same honest degradation the missing-SIGALRM platform already takes --
    rather than crashing the caller."""
    captured = {}

    def work():
        try:
            captured["result"] = dissent._aligned_within_deadline(
                "alpha beta", "alpha gamma", seconds=1
            )
        except BaseException as error:  # noqa: BLE001 - the point of the test
            captured["error"] = error

    thread = threading.Thread(target=work)
    thread.start()
    thread.join()

    assert "error" not in captured, captured.get("error")
    assert captured["result"], "a real difference must still be reported"


# --- P2 review: the two halves of comparison_loss answer the same question ---


def test_a_decomposed_witness_report_is_not_charged_a_character_per_accent():
    """`comparison_view`'s docstring settles what `dropped_characters` counts:
    "NFC discards nothing -- it re-encodes a character, it does not remove
    one", and charging composition to the loss account "would put a wrong
    number on every diacritic-heavy act in the corpus this project exists to
    read". The witness half of `comparison_loss` must answer that same
    question: summing every `markup_text_view` loss field folded in its
    `unicode_reencoded_characters`, so a witness reporting decomposed French
    was recorded as losing one character per accent while the
    identically-composed reading was recorded as losing none.
    """
    precomposed = unicodedata.normalize("NFC", "baptisé et présenté")
    decomposed = unicodedata.normalize("NFD", precomposed)
    assert precomposed != decomposed, "the fixture must actually differ at the codepoint level"

    rows = dissent.dissent_against(
        precomposed,
        [{"outcome": "read", "payload": {"chair": "attestator_1", "reported": decomposed}}],
    )

    assert rows[0]["departed"] is False, "the same ink in another normal form is not dissent"
    assert rows[0]["comparison_loss"] == {
        "reading_dropped_characters": 0,
        "witness_dropped_characters": 0,
    }


def test_markup_and_collapsed_whitespace_stay_in_the_witness_loss_account():
    """The other direction: tags and a collapsed run are genuine removals, and
    dropping them from the account would hide what the comparison view discarded.
    """
    rows = dissent.dissent_against(
        "alpha beta",
        [
            {
                "outcome": "read",
                "payload": {"chair": "attestator_1", "reported": "<b>alpha   beta</b>"},
            }
        ],
    )

    assert rows[0]["departed"] is False
    assert rows[0]["comparison_loss"]["witness_dropped_characters"] == len("<b>") + len("</b>") + 2
