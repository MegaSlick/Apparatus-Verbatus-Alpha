"""R4 alignment: markup loss is visible and bounded failures are records."""

import signal
import time
from pathlib import Path

import pytest

import common.alignment as alignment_module
from common.alignment import (
    DEFAULT_ALIGNMENT_CONFIG_PATH,
    AlignmentLimits,
    align_to_anchor,
    load_alignment_limits,
    markup_text_view,
)
from common.contracts.canonical import digest_bytes
from common.contracts.errors import ContractError


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


# --- F-X1 (R4 audit, Opus seat 3): the ampersand that ate the markup ---------


def test_a_literal_ampersand_does_not_swallow_the_markup_after_it():
    """`&` is ordinary ink ("Jean & Marie", "&c.") and a later `;` is ordinary
    punctuation. Reading the pair as one entity handed every tag between them
    back as stripped text, so dissent counted `</p><p>` as witness
    disagreement and `loss.markup_characters` under-reported what was removed.
    """
    raw = "<p>Jean & Marie</p><p>born 1688</p><i>note; here</i>"
    view = markup_text_view(raw)

    # Tags are removed without substituting a separator -- a separate, recorded
    # normalization property, symmetric across witness and anchor, which is why
    # `Marie` and `born` meet here. What matters is that no markup survives.
    assert view["text"] == "Jean & Marieborn 1688note; here"
    assert "<" not in view["text"] and ">" not in view["text"]
    # Every tag character, counted rather than quietly carried through.
    assert view["loss"]["markup_characters"] == sum(
        len(tag) for tag in ("<p>", "</p>", "<p>", "</p>", "<i>", "</i>")
    )


def test_a_genuine_entity_still_decodes_to_one_character_at_its_ampersand():
    """The narrowing must not cost the case the branch exists for."""
    raw = "<p>A &amp; B &#233; C</p>"
    view = markup_text_view(raw)

    assert view["text"] == "A & B é C"
    assert view["offset_map"][view["text"].index("&")] == raw.index("&amp;")
    assert view["offset_map"][view["text"].index("é")] == raw.index("&#233;")


def test_an_ampersand_terminated_far_past_any_entity_stays_a_literal_ampersand():
    """A semicolon 200 characters later never began an entity, however valid
    the intervening bytes look."""
    raw = "&" + "x" * 200 + ";"
    view = markup_text_view(raw)

    assert view["text"] == raw
    assert view["loss"]["markup_characters"] == 0


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
def test_alignment_deadline_reports_unaligned_honestly_never_a_partial_map(monkeypatch):
    """The timeout path must say `unaligned` -- never return a spans list that
    stopped partway through and pretend it was complete (GOVERNANCE 2/10).

    The deadline is forced deterministically: a matcher that sleeps past the
    timeout stands in for `SequenceMatcher`, so the alarm always fires. Racing
    real inputs against the wall clock made the test's verdict a machine claim
    -- a fast runner finishes the comparison and goes red for no code reason,
    and a loaded runner is what makes it pass, so a genuine loss of the
    deadline would not reliably show up either.
    """

    class _StuckMatcher:
        def __init__(self, a="", b="", autojunk=False):
            pass

        def get_matching_blocks(self):
            time.sleep(30)
            raise AssertionError("the deadline never fired")

    monkeypatch.setattr(alignment_module, "SequenceMatcher", _StuckMatcher)
    limits = AlignmentLimits(max_characters=100_000, max_character_pairs=10**9, timeout_seconds=1)

    result = align_to_anchor("alpha beta gamma", "alpha beta gamna", limits)

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


@pytest.mark.skipif(
    not all(hasattr(signal, name) for name in ("SIGALRM", "ITIMER_REAL")),
    reason="requires the POSIX real-time alarm inspected by the alignment backstop",
)
def test_an_alarm_firing_at_the_cancellation_point_is_a_record_not_an_exception(monkeypatch):
    """P2 review. Cancelling only in the `finally` left a real window: an alarm
    firing after `get_matching_blocks` returned raised `_TimedOut` from inside
    the `finally` itself, past the `except` above it, so a SUCCESSFUL alignment
    propagated an internal exception out of a function whose whole contract is
    to return an `unaligned` record instead. The sibling deadline in
    `pipeline/4_perlector/dissent.py::_aligned_within_deadline` already closes
    exactly this window.

    The fire is simulated at the first cancellation, which is where the real
    signal would land. Recording `timeout` there understates a finished
    alignment; escaping as an exception crashes the Attestatores stage.
    """
    real_alarm = signal.alarm
    fired: list[bool] = []

    def firing_alarm(seconds):
        # The one cancellation the alignment itself owns, whichever it is.
        if seconds == 0 and not fired:
            fired.append(True)
            real_alarm(0)
            raise alignment_module._TimedOut()
        return real_alarm(seconds)

    monkeypatch.setattr(signal, "alarm", firing_alarm)

    result = align_to_anchor(
        "alpha beta",
        "alpha beta",
        AlignmentLimits(max_characters=100, max_character_pairs=10_000, timeout_seconds=5),
    )

    assert fired, "the alignment armed no alarm, so this window was never exercised"
    assert result["status"] == "unaligned"
    assert result["reason"] == "timeout"
    assert "spans" not in result


# --- The limits loader: the only gate between config/alignment.toml and every run


def test_the_loader_returns_the_sealed_limits_and_the_exact_file_digest():
    limits, digest = load_alignment_limits()
    assert limits.max_characters > 0
    assert limits.max_character_pairs > 0
    assert limits.timeout_seconds > 0
    assert digest == digest_bytes(Path(DEFAULT_ALIGNMENT_CONFIG_PATH).read_bytes())


def test_the_loader_refuses_an_unreadable_file(tmp_path):
    with pytest.raises(ContractError, match="could not be read"):
        load_alignment_limits(tmp_path / "absent.toml")


def test_the_loader_refuses_an_unknown_or_missing_key(tmp_path):
    misspelt = tmp_path / "misspelt.toml"
    misspelt.write_text(
        "[limits]\nmax_characters = 1\nmax_character_pairs = 1\ntimeout_second = 1\n"
    )
    with pytest.raises(ContractError, match="closed schema"):
        load_alignment_limits(misspelt)
    partial = tmp_path / "partial.toml"
    partial.write_text("[limits]\nmax_characters = 1\n")
    with pytest.raises(ContractError, match="closed schema"):
        load_alignment_limits(partial)


@pytest.mark.parametrize("bad", ['"3"', "true", "0", "-1", "1.5"])
def test_the_loader_refuses_a_value_that_is_not_a_positive_integer(tmp_path, bad):
    """`true` would parse as 1 and quietly cut every page alignment to one
    second; a float or string would land in signal.alarm at run time. The
    loader is where those stop."""
    path = tmp_path / "limits.toml"
    path.write_text(
        f"[limits]\nmax_characters = 1\nmax_character_pairs = 1\ntimeout_seconds = {bad}\n"
    )
    with pytest.raises(ContractError, match="positive integers"):
        load_alignment_limits(path)


# --- NFC composition and the offset map


def test_nfc_composition_keeps_the_offset_map_pointing_at_the_raw_cluster():
    """Composition changes codepoint count, so indexing pre-composition offsets
    with a post-composition index mis-pointed every entry after the first
    merge. Each composed character now maps to the raw offset of the cluster
    that produced it -- for NFD French, the base letter the accent composed
    into."""
    raw = "Genevie\u0300ve ne\u0301e"  # NFD: base letters with combining accents
    view = markup_text_view(raw)

    assert view["text"] == "Genevi\u00e8ve n\u00e9e"  # NFC: composed \u00e8 and \u00e9
    offsets = view["offset_map"]
    # \u00e8 composed from raw[6] ("e") + raw[7] (combining grave) -> maps to 6.
    assert offsets[6] == 6
    # Every later offset names its own raw character, not one shifted by the
    # merge: v is raw[8], e raw[9], the collapsed space None, n raw[11], and
    # \u00e9 maps to its base letter raw[12].
    assert offsets[7] == 8
    assert offsets[8] == 9
    assert offsets[9] is None
    assert offsets[10] == 11
    assert offsets[11] == 12
    assert offsets[12] == 14
    assert view["loss"]["unicode_reencoded_characters"] == 2


def test_starter_starter_composition_yields_honest_none_offsets_never_shifted_ones():
    """The documented fallback: where per-cluster composition cannot reproduce
    NFC of the whole (starter-starter composition -- Hangul jamo compose
    across combining-class-0 boundaries), every offset entry is None. An
    absent measurement, never a fabricated one: publishing shifted offsets
    there would reach the act attachment as measured geometry."""
    raw = "가"  # Hangul jamo G + A, NFC-composed to one syllable
    view = markup_text_view(raw)

    assert view["text"] == "가"
    assert view["offset_map"] == [None]
