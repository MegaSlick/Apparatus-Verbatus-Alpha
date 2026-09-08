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
    bracket_marker_view,
    load_alignment_limits,
    markup_text_view,
)
from common.contracts.canonical import digest_bytes
from common.contracts.errors import ContractError, SchemaRefusal


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
    most plainly) must not raise or silently drop the witness: the matcher
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
    timeout stands in for the real one, so the alarm always fires. Racing
    real inputs against the wall clock made the test's verdict a machine claim
    -- a fast runner finishes the comparison and goes red for no code reason,
    and a loaded runner is what makes it pass, so a genuine loss of the
    deadline would not reliably show up either. Since the sealed deadline was
    raised above the slowest input the pair bound admits (hostile review C),
    forcing it is not merely the robust way to test this path but the only
    way: no admissible input reaches 25 seconds.
    """

    def _stuck_matcher(witness_text, anchor_text):
        time.sleep(30)
        raise AssertionError("the deadline never fired")

    monkeypatch.setattr(alignment_module, "_matching_blocks", _stuck_matcher)
    limits = AlignmentLimits(max_characters=100_000, max_character_pairs=10**9, timeout_seconds=1)

    result = align_to_anchor("alpha beta gamma", "alpha beta gamna", limits)

    assert result["status"] == "unaligned"
    # Named for what happened -- this module's backstop fired -- so a receipt
    # cannot be read as saying the witness itself timed out or that coverage
    # was measured and found absent.
    assert result["reason"] == alignment_module.DEADLINE_REASON == "alignment-deadline-exceeded"
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

    def broken_matcher(witness_text, anchor_text):
        raise RuntimeError("matcher failed")

    signal.alarm(0)
    signal.signal(signal.SIGALRM, caller_handler)
    monkeypatch.setattr(alignment_module, "_matching_blocks", broken_matcher)
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
    assert result["reason"] == alignment_module.DEADLINE_REASON
    assert "spans" not in result


# --- The matcher's contract (hostile review C) -------------------------------
#
# Written while trying to replace `difflib` with RapidFuzz, and kept after that
# swap was refused on measurement. They pin what `align_to_anchor`'s callers
# actually depend on, so the next attempt fails loudly instead of quietly
# redefining what "aligned" means: fidelity to the codepoints handed in,
# monotonicity, and -- the one that killed the swap -- which of two equally
# large attachments wins.


def _matcher_limits() -> AlignmentLimits:
    """The shipped limits, loaded, never a copy of them.

    CodeRabbit pass 1. Restating 100,000 / 10^8 / 25 here would have made the
    two tests below assert against numbers that agree with
    `config/alignment.toml` only until someone edits it -- and the deadline is
    the number hostile review C is about, so a test that cannot notice it
    changing is the wrong test.
    """
    return load_alignment_limits()[0]


@pytest.mark.parametrize(
    "witness,anchor,expects_a_match",
    [
        ("alpha beta gamma", "alpha beta gamna", True),
        ("L'an mil sept cent quarante-trois", "L'an mil sepc cent quarante-troys", True),
        ("Geneviève née à Saint-Aubin", "Genevieve nee a Saint-Auban", True),
        ("abcabcabcabc", "cbacbacba", True),
        ("", "alpha", False),
        ("alpha", "", False),
    ],
)
def test_every_matched_span_names_text_that_is_actually_equal(witness, anchor, expects_a_match):
    """The one assertion that makes a span a measurement rather than a guess:
    the witness slice and the anchor slice it claims must be the same
    characters. A matcher that normalized, case-folded, or truncated either
    side would still return plausible-looking offsets, and only this check
    would notice.

    `expects_a_match` is what stops the check being vacuous, and it is not
    bookkeeping. A matcher that returned nothing at all would satisfy every
    assertion in the loop below while turning aligned records into `unaligned`
    ones -- and an unaligned page witness leaves the act's witness floor, so
    the silent-empty regression costs coverage (GOALS 1) exactly where this
    file is meant to be watching. Only the two empty-input rows may return
    nothing.
    """
    blocks = alignment_module._matching_blocks(witness, anchor)
    assert bool(blocks) is expects_a_match
    for witness_start, anchor_start, size in blocks:
        assert (
            witness[witness_start : witness_start + size]
            == anchor[anchor_start : anchor_start + size]
        )
        assert size > 0


def test_matched_blocks_are_strictly_ordered_and_non_overlapping_on_both_sides():
    """`pipeline/3_attestatores/run.py` clips a whole-page alignment to one
    act's anchor range and hulls what survives. That is only sound while the
    blocks advance monotonically on BOTH sides: out-of-order blocks would let
    an act's anchor range pull in witness text from the far end of the page,
    which is the F-X2 failure the clipping was written to end."""
    witness = "et de Marie Bernard, laboureur de cette paroisse, en presence de Jean Moreau"
    anchor = "et de Marie Bernart, laboureur de ceste parroisse, en presence de Jan Moreau"
    blocks = alignment_module._matching_blocks(witness, anchor)
    assert len(blocks) > 1, "this pair must exercise more than a single block"
    previous_witness_end = previous_anchor_end = 0
    for witness_start, anchor_start, size in blocks:
        assert witness_start >= previous_witness_end
        assert anchor_start >= previous_anchor_end
        previous_witness_end = witness_start + size
        previous_anchor_end = anchor_start + size
    assert previous_witness_end <= len(witness)
    assert previous_anchor_end <= len(anchor)


def test_the_matcher_folds_no_case_and_composes_no_accents():
    """`markup_text_view` decides normalization -- NFC and whitespace collapse,
    both recorded as loss. The matcher must add none of its own, or two
    readings that differ in the ink would be reported as agreeing."""
    assert alignment_module._matching_blocks("ABC", "abc") == []
    # Composed vs decomposed: the same grapheme, different codepoints. Only
    # `markup_text_view` may reconcile those, and it records the count when it
    # does; a matcher that did it silently would hide the difference.
    assert alignment_module._matching_blocks("\u00e9", "e\u0301") == []
    assert alignment_module._matching_blocks("\u00e9", "\u00e9") == [(0, 0, 1)]


def test_offsets_are_codepoint_indices_even_past_the_basic_multilingual_plane():
    """The offsets index the same normalized string `markup_text_view` built
    its offset map against, so they must be Python `str` indices -- codepoints,
    not UTF-8 bytes and not UTF-16 units. An astral character counting as two
    would shift every later offset and mis-place the raw span."""
    witness = "\U0001f600\U0001f600abc"
    assert alignment_module._matching_blocks(witness, "abc") == [(2, 0, 3)]


def test_a_shared_act_opening_attaches_to_the_act_the_witness_actually_read():
    """The property that refused the RapidFuzz swap (hostile review C), pinned
    so it is not lost the next time someone reaches for a faster matcher.

    Register acts open with the same formula, so a page of them contains the
    same opening several times. A witness that read only the second act
    presents a genuine ambiguity, and the two readings of it attach exactly the
    same number of characters:

      * the whole reading against the second act's range -- what this matcher
        returns, and what the witness actually did; or
      * the shared opening against the FIRST act, plus the remainder against
        the second -- what a coverage-maximizing matcher returns, because it
        ties on characters and breaks the tie towards the earliest match.

    RapidFuzz's Indel/LCS opcodes take the second. It is not a smaller answer,
    it is a wrong one, and the pipeline's `confirmed-blank` scenario failed on
    it: twelve characters of page text fell outside every act attachment, the
    Recensor read that as incomplete testimony coverage, and both acts were
    held instead of the blank being sealed. Longest verbatim agreement wins;
    that is the disambiguation this module is for.
    """
    anchor = "SYNTHETIC ACT ONE alpha beta gamma SYNTHETIC ACT TWO delta epsilon zeta eta"
    witness = "SYNTHETIC ACT TWO delta epsilon zeta eta"
    second_act_start = anchor.index("SYNTHETIC ACT TWO")

    blocks = alignment_module._matching_blocks(witness, anchor)

    assert sum(size for _, _, size in blocks) == len(witness), (
        "the witness read one act verbatim, so all of it has a counterpart"
    )
    assert all(anchor_start >= second_act_start for _, anchor_start, _ in blocks), (
        "no part of a reading of the second act may be attributed to the first, "
        "however many characters the two acts' openings share"
    )


@pytest.mark.full
def test_the_page_that_set_the_deadline_still_aligns_under_the_sealed_limits():
    """Hostile review C: the workload that decided `timeout_seconds`, run
    against the sealed value, so lowering that value goes red here.

    A fired deadline is `unaligned`, an unaligned page witness is not
    `comparable`, and an incomparable chair leaves the act's witness floor -- so
    a comparison that is merely slow is recorded as coverage that is missing
    (GOALS 1). The input below is what made five seconds too short: 7,500
    characters of register prose whose acts repeat one formula verbatim, which
    is what a scribe copying one form actually produces, and which is the shape
    Ratcliff-Obershelp works hardest on. It measures 10.1 s. Under the five
    seconds this config used to carry it came back `unaligned`, and a page that
    had been read perfectly well was recorded as an act nobody corroborated.

    The bar is the sealed deadline itself, not a fraction of it derived here
    (CodeRabbit pass 3): the claim is "this page aligns under the shipped
    limits", and a second invented threshold would be a different, weaker
    claim. Marked `full` so a ten-second alignment stays out of the fast loop
    (CodeRabbit pass 2, which also asked for the timing to go away entirely --
    declined: the deadline's adequacy is this change's whole subject, and a
    claim no test can notice going wrong is not a claim).

    What no deadline value can claim is that nothing reaches it: two different
    low-entropy chair responses at the pair ceiling measure 283.9 s, so the
    deadline still fires on degenerate output and is still an honest non-verdict
    when it does. `pipeline/3_attestatores/HANDOFF.md` carries that measurement
    and the design that would close it.
    """
    act = (
        "L'an mil sept cent quarante-trois, le douziesme jour du mois de may, "
        "a este baptise par nous soubsigne Jean, fils legitime de Pierre Moreau, "
        "laboureur, et de Marie Bernard sa femme, de cette paroisse de Saint-Pierre. "
    )
    anchor = (act * 40)[:7_500]
    witness = anchor.replace("este", "esté").replace("legitime", "legitirne")[:7_500]
    limits = _matcher_limits()
    start = time.perf_counter()
    result = align_to_anchor(witness, anchor, limits)
    elapsed = time.perf_counter() - start

    assert result["status"] == "aligned", (
        f"the page that set the deadline took {elapsed:.1f}s against a sealed "
        f"{limits.timeout_seconds}s and came back {result.get('reason')}; a page read "
        "perfectly well would be recorded as an act nobody corroborated"
    )


def test_no_input_the_sealed_bounds_admit_is_silently_truncated():
    """The largest input the sealed bounds admit at all, derived from the
    shipped limits rather than restated: a witness at `max_characters` against
    an anchor of `max_character_pairs // max_characters` sits exactly on both
    ceilings, and both bounds are `>`, so this runs rather than being refused.
    The retained view must carry every character, and the spans must still name
    equal text -- a matcher that clipped its inputs to some internal ceiling
    would pass every small-input test above and fail here.

    The anchor is planted verbatim at the end of the witness, so the correct
    answer is known independently of which library computes it: every anchor
    character has a counterpart.
    """
    # Trailing whitespace is trimmed off both, because `markup_text_view`
    # collapses a trailing separator away and this test's whole claim is that
    # the retained view is the same length as its input.
    limits = _matcher_limits()
    anchor_size = limits.max_character_pairs // limits.max_characters
    anchor = ("et de Marie Bernard, laboureur de cette paroisse. " * 21)[:anchor_size].rstrip()
    filler = ("Jean Moreau, tisserand, et de Perrine Girard. " * 2_300)[
        : limits.max_characters - len(anchor)
    ]
    witness = (filler + anchor).rstrip()
    assert len(witness) <= limits.max_characters and len(anchor) <= anchor_size
    assert len(witness) > limits.max_characters - 10, "the ceiling must actually be exercised"
    assert len(witness) * len(anchor) <= limits.max_character_pairs
    result = align_to_anchor(witness, anchor, limits)

    assert result["status"] == "aligned"
    witness_text, anchor_text = result["witness"]["text"], result["anchor"]["text"]
    # No whitespace runs and no markup in either input, so the normalized view
    # is the input itself; a shorter one would mean the matcher's inputs, not
    # the normalization, had been clipped.
    assert len(witness_text) == len(witness), "the retained witness view was clipped"
    assert len(anchor_text) == len(anchor)
    for span in result["spans"]:
        assert (
            witness_text[span["witness"]["start"] : span["witness"]["end"]]
            == anchor_text[span["anchor"]["start"] : span["anchor"]["end"]]
        )
    matched = sum(span["anchor"]["end"] - span["anchor"]["start"] for span in result["spans"])
    assert matched == len(anchor_text), (
        "the anchor appears verbatim inside the witness, so every anchor character "
        "has a counterpart; a short match means the comparison stopped early"
    )


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
    raw = "\u1100\u1161"  # Hangul jamo G + A, NFC-composed to one syllable
    view = markup_text_view(raw)

    assert view["text"] == "\uac00"
    assert view["offset_map"] == [None]


# --- bracket_marker_view (U6): removes exactly the RecordGold uncertainty
# markers, offset-mapped so a span found in the stripped text still resolves
# back to the raw ink it came from.


def test_bracket_marker_view_removes_both_markers_and_keeps_offsets_pointing_at_raw():
    raw = "Marie [UNCERTAIN] Dubois [CROSSED_OUT]"
    view = bracket_marker_view(raw)

    assert view["text"] == "Marie  Dubois "
    assert view["loss"]["marker_characters"] == len("[UNCERTAIN]") + len("[CROSSED_OUT]")
    # Every surviving character maps to its own index in raw, never a shifted
    # one: "M" is raw[0]; the space right after the first marker is raw[18].
    offsets = view["offset_map"]
    assert offsets[0] == raw.index("M")
    assert raw[offsets[6]] == " "
    assert offsets[6] == raw.index("Dubois") - 1


def test_bracket_marker_view_leaves_plain_text_with_no_marker_untouched():
    raw = "plain established text, no markers here"
    view = bracket_marker_view(raw)

    assert view["text"] == raw
    assert view["offset_map"] == list(range(len(raw)))
    assert view["loss"]["marker_characters"] == 0


def test_bracket_marker_view_does_not_normalize_or_collapse_neighbouring_whitespace():
    """Deliberately narrower than `markup_text_view`: only the exact marker
    bytes are removed, so a marker's neighbouring whitespace, and any
    unrelated Unicode composition, survive exactly as reported."""
    raw = "Genevie\u0300ve [UNCERTAIN]  ne\u0301e"  # NFD accents kept, double space kept
    view = bracket_marker_view(raw)

    assert view["text"] == "Genevie\u0300ve   ne\u0301e"
    assert view["loss"]["marker_characters"] == len("[UNCERTAIN]")
    assert "\u00e8" not in view["text"]  # no NFC composition performed here


def test_bracket_marker_view_removes_adjacent_markers_with_no_gap():
    raw = "[UNCERTAIN][CROSSED_OUT]tail"
    view = bracket_marker_view(raw)

    assert view["text"] == "tail"
    assert view["offset_map"] == [raw.index("tail") + i for i in range(len("tail"))]
    assert view["loss"]["marker_characters"] == len("[UNCERTAIN]") + len("[CROSSED_OUT]")


def test_bracket_marker_view_does_not_match_a_partial_or_unclosed_marker():
    raw = "[UNCERTAIN unterminated] and [CROSSED_OUT_EXTRA]"
    view = bracket_marker_view(raw)

    # Neither the unterminated left bracket text nor a token with a trailing
    # suffix is `_UNCERTAINTY_TOKENS` verbatim, so nothing is removed: only an
    # exact substring match counts, never a prefix or a loose bracket scan.
    assert view["text"] == raw
    assert view["loss"]["marker_characters"] == 0


def test_bracket_marker_view_all_markers_normalizes_to_a_genuinely_empty_text():
    raw = "[UNCERTAIN][CROSSED_OUT][UNCERTAIN]"
    view = bracket_marker_view(raw)

    assert view["text"] == ""
    assert view["offset_map"] == []
    assert view["loss"]["marker_characters"] == len(raw)


def test_bracket_marker_view_refuses_non_text_input():
    with pytest.raises(SchemaRefusal, match="not text"):
        bracket_marker_view(b"[UNCERTAIN] not a str")


def test_bracket_marker_view_agrees_with_feedings_own_uncertainty_tokens():
    """The RecordGold markers are carried in two places -- `feeding.py`'s
    retained-response contract and this alignment view -- because `common/`
    cannot import from `pipeline/3_attestatores/feeding.py` without a
    circular import (`feeding.py` already imports from `common`). `3_` makes
    the directory an invalid dotted package name, so this loads the module by
    file path, exactly as `pipeline/3_attestatores/test_feeding.py` already
    does for its own sibling files. This test is the seam that keeps the two
    tuples from drifting apart silently."""
    import importlib.util

    feeding_path = (
        Path(__file__).resolve().parents[1] / "pipeline" / "3_attestatores" / "feeding.py"
    )
    spec = importlib.util.spec_from_file_location("alignment_test_feeding", feeding_path)
    feeding_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(feeding_module)

    assert alignment_module._UNCERTAINTY_TOKENS == feeding_module._UNCERTAINTY_TOKENS
