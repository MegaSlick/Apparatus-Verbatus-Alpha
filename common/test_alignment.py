"""R4 alignment: markup loss is visible and bounded failures are records."""

import signal
import time

import pytest

import common.alignment as alignment_module
from common.alignment import AlignmentLimits, align_to_anchor, markup_text_view


def test_markup_view_strips_tags_with_offsets_and_explicit_loss():
    raw = "<p>alpha <b>beta</b></p>"
    view = markup_text_view(raw)

    assert view["text"] == "alpha beta"
    assert view["offset_map"][0] == raw.index("a")
    assert view["loss"]["markup_characters"] > 0


def test_alignment_returns_an_explicit_unaligned_record_at_the_sealed_pair_limit():
    result = align_to_anchor(
        "alpha beta gamma",
        "alpha beta gamma",
        AlignmentLimits(max_characters=100, max_character_pairs=4, timeout_seconds=1),
    )

    assert result["status"] == "unaligned"
    assert result["reason"] == "character-pair-limit"
    assert result["witness"]["text"] == "alpha beta gamma"


def test_alignment_carries_matching_spans_through_markup_normalization():
    result = align_to_anchor(
        "<output>alpha beta</output>",
        "<p>alpha beta gamma</p>",
        AlignmentLimits(max_characters=100, max_character_pairs=10_000, timeout_seconds=1),
    )

    assert result["status"] == "aligned"
    assert result["spans"] == [
        {"witness": {"start": 0, "end": 10}, "anchor": {"start": 0, "end": 10}}
    ]


# --- BREAKER battery (R4 audit, Sonnet seat 1) ------------------------------


def test_markup_that_decodes_to_the_same_text_keeps_independent_raw_offsets():
    """Two differently-marked-up strings that normalize to identical text must
    not share an offset map: each `offset_map` traces back to its OWN raw
    bytes, never the other's, even though `text` is byte-for-byte equal."""
    tag_before = markup_text_view("<b>alpha</b> beta")
    tag_after = markup_text_view("alpha <i>beta</i>")

    assert tag_before["text"] == tag_after["text"] == "alpha beta"
    assert tag_before["offset_map"][0] == "<b>alpha</b> beta".index("a")
    assert tag_after["offset_map"][0] == "alpha <i>beta</i>".index("a")
    # The "beta" half sits at a different raw offset in each source string;
    # an aliased or cached map would fail exactly here.
    beta_index_before = tag_before["text"].index("beta")
    beta_index_after = tag_after["text"].index("beta")
    assert tag_before["offset_map"][beta_index_before] != tag_after["offset_map"][beta_index_after]


def test_an_anchor_that_repeats_the_witness_text_still_aligns_without_crashing():
    """A phrase appearing twice in the anchor (a repeated formulaic opening,
    most plainly) must not raise or silently drop the witness: `SequenceMatcher`
    resolves it to a real, well-formed span selection, never a partial map."""
    result = align_to_anchor(
        "alpha beta",
        "alpha beta gamma alpha beta",
        AlignmentLimits(max_characters=1000, max_character_pairs=100_000, timeout_seconds=1),
    )

    assert result["status"] == "aligned"
    for span in result["spans"]:
        assert span["witness"]["start"] >= 0
        assert span["witness"]["end"] <= len("alpha beta")
        assert span["anchor"]["end"] <= len("alpha beta gamma alpha beta")
    matched = sum(span["witness"]["end"] - span["witness"]["start"] for span in result["spans"])
    assert matched == len("alpha beta"), "a real repeated phrase must fully match somewhere"


def test_witness_text_exactly_at_the_character_limit_still_aligns():
    """The bound is `>`, not `>=`: text sized exactly to the sealed limit is
    still real work, not a refusal in disguise."""
    text = "a" * 50
    result = align_to_anchor(
        text,
        text,
        AlignmentLimits(max_characters=50, max_character_pairs=10_000, timeout_seconds=1),
    )
    assert result["status"] == "aligned"


def test_witness_text_one_character_past_the_limit_is_explicitly_unaligned():
    text = "a" * 51
    result = align_to_anchor(
        text,
        text,
        AlignmentLimits(max_characters=50, max_character_pairs=10_000, timeout_seconds=1),
    )
    assert result["status"] == "unaligned"
    assert result["reason"] == "character-limit"
    # Refused, never clipped: the full retained text is still there to read.
    assert result["witness"]["text"] == text


def test_an_all_markup_input_normalizes_to_a_genuinely_zero_width_offset_map():
    """Text that is entirely tags and whitespace collapses to nothing -- the
    offset map must be an honest empty list, not a crash or a fabricated
    entry standing in for characters that were never there."""
    view = markup_text_view("<p>   </p><br/>")
    assert view["text"] == ""
    assert view["offset_map"] == []


@pytest.mark.skipif(
    not hasattr(signal, "SIGALRM"),
    reason="the wall-clock backstop is a SIGALRM mechanism; where it cannot exist the "
    "comparison runs unbounded and this test would hang for minutes to say nothing",
)
def test_alignment_deadline_reports_unaligned_honestly_never_a_partial_map():
    """The timeout path must say `unaligned` -- never return a spans list that
    stopped partway through and pretend it was complete (GOVERNANCE 2/10)."""
    witness = "alpha beta gamma " * 400
    anchor = "alpha beta gamna " * 400
    limits = AlignmentLimits(max_characters=100_000, max_character_pairs=10**9, timeout_seconds=1)

    started = time.monotonic()
    result = align_to_anchor(witness, anchor, limits)
    elapsed = time.monotonic() - started

    assert elapsed < 10, "the wall-clock bound must stop the alignment"
    assert result["status"] == "unaligned"
    assert result["reason"] == "timeout"
    assert "spans" not in result, "a timed-out alignment must never carry a partial spans list"


@pytest.mark.skipif(
    not all(hasattr(signal, name) for name in ("SIGALRM", "ITIMER_REAL")),
    reason="requires the POSIX real-time alarm inspected by the alignment backstop",
)
def test_alignment_does_not_cancel_an_unrelated_existing_alarm():
    """A caller's timer remains its timer; alignment must not borrow or clear it."""
    previous_handler = signal.getsignal(signal.SIGALRM)

    def unrelated_handler(signum, frame):
        pass

    signal.signal(signal.SIGALRM, unrelated_handler)
    signal.alarm(30)
    try:
        result = align_to_anchor(
            "alpha beta",
            "alpha beta",
            AlignmentLimits(max_characters=100, max_character_pairs=10_000, timeout_seconds=1),
        )
        remaining = signal.alarm(0)

        assert result["status"] == "aligned"
        assert remaining > 0
        assert signal.getsignal(signal.SIGALRM) is unrelated_handler
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_handler)


@pytest.mark.skipif(
    not all(hasattr(signal, name) for name in ("SIGALRM", "ITIMER_REAL")),
    reason="requires the POSIX real-time alarm inspected by the alignment backstop",
)
def test_alignment_clears_its_alarm_and_restores_the_handler_on_an_exception(monkeypatch):
    """No alignment-owned alarm may escape into unrelated work after a failure."""
    previous_handler = signal.getsignal(signal.SIGALRM)

    def caller_handler(signum, frame):
        pass

    def broken_matcher(**kwargs):
        raise RuntimeError("matcher failed")

    signal.alarm(0)
    signal.signal(signal.SIGALRM, caller_handler)
    monkeypatch.setattr(alignment_module, "SequenceMatcher", broken_matcher)
    try:
        with pytest.raises(RuntimeError, match="matcher failed"):
            align_to_anchor(
                "alpha beta",
                "alpha beta",
                AlignmentLimits(
                    max_characters=100,
                    max_character_pairs=10_000,
                    timeout_seconds=10,
                ),
            )

        assert signal.alarm(0) == 0
        assert signal.getsignal(signal.SIGALRM) is caller_handler
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_handler)
