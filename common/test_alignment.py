"""Alignment: markup loss is visible and bounded failures are records."""

from difflib import SequenceMatcher

import pytest

import common.alignment as alignment_module
from common.alignment import (
    DEFAULT_ALIGNMENT_CONFIG_PATH,
    STEP_LIMIT_REASON,
    UNMEASURED_REASONS,
    AlignmentLimits,
    StepCountedMatcher,
    align_to_anchor,
    bracket_marker_view,
    load_alignment_limits,
    load_dissent_limits,
    markup_text_view,
    refuse_retired_alignment_record,
)
from common.contracts.errors import ContractError, SchemaRefusal
from common.contracts.uncertainty import UNCERTAINTY_TOKENS
from common.sealed_config import read_sealed_toml
from conftest import load_stage


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
        AlignmentLimits(max_characters=100, max_character_pairs=4, max_alignment_steps=10**9),
    )

    assert result["status"] == "unaligned"
    assert result["reason"] == "character-pair-limit"
    assert result["witness"]["text"] == "alpha beta gamma"


def test_alignment_carries_matching_spans_through_markup_normalization():
    result = align_to_anchor(
        "<output>alpha beta</output>",
        "<p>alpha beta gamma</p>",
        AlignmentLimits(max_characters=100, max_character_pairs=10_000, max_alignment_steps=10**9),
    )

    assert result["status"] == "aligned"
    assert result["spans"] == [
        {"witness": {"start": 0, "end": 10}, "anchor": {"start": 0, "end": 10}}
    ]


# --- The ampersand that ate the markup ---------------------------------


def test_a_literal_ampersand_does_not_swallow_the_markup_after_it():
    """`&` is ordinary ink ("Jean & Marie", "&c.") and a later `;` is ordinary
    punctuation; reading the pair as one entity would hand every tag between
    them back as stripped text.
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


# --- Adversarial offset-map battery ---------------------------------------


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
        AlignmentLimits(
            max_characters=1000, max_character_pairs=100_000, max_alignment_steps=10**9
        ),
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
        AlignmentLimits(max_characters=50, max_character_pairs=10_000, max_alignment_steps=10**9),
    )
    assert result["status"] == "aligned"


def test_witness_text_one_character_past_the_limit_is_explicitly_unaligned():
    text = "a" * 51
    result = align_to_anchor(
        text,
        text,
        AlignmentLimits(max_characters=50, max_character_pairs=10_000, max_alignment_steps=10**9),
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


def _steps_to_align(witness: str, anchor: str) -> int:
    """The steps one alignment of the two normalized texts takes to finish."""
    matcher = StepCountedMatcher(witness, anchor, 10**12)
    matcher.get_matching_blocks()
    return 10**12 - matcher.steps_left


def test_an_alignment_finishes_on_its_exact_step_count_and_not_one_step_fewer():
    """The budget is a count, so its edge is exact: the same pair aligns with
    exactly the steps it needs and is unaligned with one fewer, on any machine
    and under any load. Running out says `unaligned` with its own reason --
    never a spans list that stopped partway and pretends to be complete."""
    witness, anchor = "alpha beta gamma", "alpha beta gamna"
    needed = _steps_to_align(witness, anchor)
    assert needed > 1

    def limits(steps: int) -> AlignmentLimits:
        return AlignmentLimits(
            max_characters=100, max_character_pairs=10_000, max_alignment_steps=steps
        )

    assert align_to_anchor(witness, anchor, limits(needed))["status"] == "aligned"
    result = align_to_anchor(witness, anchor, limits(needed - 1))

    assert result["status"] == "unaligned"
    # Named for what happened -- this module stopped -- so a receipt cannot be
    # read as saying the witness failed or that coverage was measured and found
    # absent.
    assert result["reason"] == STEP_LIMIT_REASON == "alignment-step-limit"
    assert "spans" not in result, "a stopped alignment must never carry a partial spans list"
    assert result["witness"]["text"] == witness, "refused, never clipped"


def test_a_retired_deadline_record_of_either_status_is_refused_by_name():
    """A wall-clock stop measured nothing. Read today it would land in
    `unaligned`, the bucket of comparisons made, so its reason is refused by
    name, as the aligned record's retired field is."""
    with pytest.raises(
        SchemaRefusal, match="retired unaligned reason 'alignment-deadline-exceeded'"
    ):
        refuse_retired_alignment_record(
            {"status": "unaligned", "reason": "alignment-deadline-exceeded"}, "a record"
        )
    with pytest.raises(
        SchemaRefusal, match=r"retired alignment field\(s\) \['deadline_in_force'\]"
    ):
        refuse_retired_alignment_record(
            {"status": "aligned", "deadline_in_force": True}, "a record"
        )
    for current in (
        {"status": "unaligned", "reason": STEP_LIMIT_REASON},
        {"status": "unaligned", "reason": "no-common-anchor-text"},
        None,
    ):
        refuse_retired_alignment_record(current, "a record")


class _VisitCounter(dict):
    """`b2j` with every position list counting the times `difflib` iterates it.

    `find_longest_match`'s inner loop is `for j in b2j.get(a[i], nothing)`, so
    each yielded position is one visit of the loop the step budget charges for,
    counted by the loop itself rather than by the charge's own formula.
    """

    visits = 0

    def __init__(self, b2j: dict) -> None:
        super().__init__(
            {char: _CountedPositions(self, positions) for char, positions in b2j.items()}
        )


class _CountedPositions(list):
    def __init__(self, counter: _VisitCounter, positions: list[int]) -> None:
        super().__init__(positions)
        self.counter = counter

    def __iter__(self):
        for position in super().__iter__():
            self.counter.visits += 1
            yield position


def _charge_and_visits(witness: str, anchor: str) -> tuple[int, int]:
    matcher = StepCountedMatcher(witness, anchor, 10**12)
    counter = matcher.b2j = _VisitCounter(matcher.b2j)
    matcher.get_matching_blocks()
    return 10**12 - matcher.steps_left, counter.visits


def test_the_charge_is_a_hand_counted_number_and_covers_every_inner_loop_visit():
    """ "ab" against "abab" is one search: two witness characters scanned, each
    with two anchor positions below the range's end, so 2 + 2 + 2 = 6 steps
    for 4 visits. The longest match is the whole witness, so neither side
    recurses. Over other pairs the charge is never below the visits `difflib`
    actually makes: the budget counts at least the work."""
    assert _charge_and_visits("ab", "abab") == (6, 4)
    pairs = [
        ("alpha beta gamma", "alpha beta gamna"),
        ("abcabcabcabc", "cbacbacba"),
        ("a" * 300, "a" * 200 + "b" + "a" * 50),
        ("et de Marie Bernard, laboureur", "et de Marie Bernart, laboreur de ceste paroisse"),
    ]
    for witness, anchor in pairs:
        charged, visits = _charge_and_visits(witness, anchor)
        assert 0 < visits <= charged, (witness, anchor)


# One short act, 150 characters, repeated verbatim to fill a page at the pair
# ceiling, read by a witness that misreads the same character in every act.
# Every repeat is an equally long candidate match, so the search revisits them
# all: the costliest page of register acts the budget is sized to cover.
_ACT = (
    "L'an mil sept cent quarante-trois, le douziesme jour du mois de may, a este "
    "baptise par nous soubsigne Jean, fils legitime de Pierre Moreau, laboureur"
)


def _repeated_page(unit: str, side: int) -> tuple[str, str]:
    """`unit` repeated to `side` characters, and a witness misreading one character per unit."""
    middle = len(unit) // 2
    misread = unit[:middle] + "X" + unit[middle + 1 :]
    return (misread * (side // len(unit) + 1))[:side], (unit * (side // len(unit) + 1))[:side]


@pytest.mark.full
def test_a_page_of_150_character_acts_at_the_pair_ceiling_aligns_with_twice_the_steps_to_spare():
    limits = _matcher_limits()
    side = int(limits.max_character_pairs**0.5)
    assert len(_ACT) == 150
    witness, anchor = _repeated_page(_ACT, side)
    assert len(witness) * len(anchor) <= limits.max_character_pairs

    steps = _steps_to_align(witness, anchor)

    # An unaligned page witness leaves the act's witness floor, so running out
    # here would record a page read perfectly well as uncorroborated.
    assert 2 * steps <= limits.max_alignment_steps, steps


@pytest.mark.full
def test_short_repeated_units_at_the_pair_ceiling_are_not_covered_and_stop_as_unmeasured():
    """60-character units repeated across the ceiling, such as index rows, need
    more than the budget. They are not covered: they come out on the step-limit
    reason the Recensor counts as unmeasured, a named hold, never a silent loss."""
    limits = load_alignment_limits()[0]
    side = int(limits.max_character_pairs**0.5)
    witness, anchor = _repeated_page(_ACT[:60], side)
    result = align_to_anchor(witness, anchor, limits)
    assert (result["status"], result["reason"]) == ("unaligned", STEP_LIMIT_REASON)
    assert STEP_LIMIT_REASON in UNMEASURED_REASONS


def test_the_step_counted_matcher_returns_exactly_the_standard_library_blocks():
    """The budget counts the work and changes none of it: every block and
    opcode is `difflib`'s own, so no aligned record moves because it is
    counted."""
    pairs = [
        ("alpha beta gamma", "alpha beta gamna"),
        ("abcabcabcabc", "cbacbacba"),
        (
            "et de Marie Bernard, laboureur de cette paroisse, en presence de Jean Moreau",
            "et de Marie Bernart, laboureur de ceste parroisse, en presence de Jan Moreau",
        ),
        ("", "alpha"),
        ("a" * 300, "a" * 200 + "b" + "a" * 50),
    ]
    for witness, anchor in pairs:
        reference = SequenceMatcher(None, witness, anchor, autojunk=False)
        counted = StepCountedMatcher(witness, anchor, 10**9)
        assert counted.get_matching_blocks() == reference.get_matching_blocks()
        assert counted.get_opcodes() == reference.get_opcodes()


@pytest.mark.full
def test_a_degenerate_pair_the_pair_bound_admits_stops_on_the_sealed_budget():
    """Two different low-entropy responses at the pair ceiling -- a chair stuck
    repeating one phrase against a page that repeats another -- would take many
    times the budget to finish. They stop on it, unaligned with the reason
    that says the aligner stopped, not that the witness was measured."""
    limits = load_alignment_limits()[0]
    side = int(limits.max_character_pairs**0.5)
    witness, anchor = ("et le " * side)[:side], ("de la " * side)[:side]
    result = align_to_anchor(witness, anchor, limits)
    assert (result["status"], result["reason"]) == ("unaligned", STEP_LIMIT_REASON)


# --- The matcher's contract --------------------------------------------------
#
# What `align_to_anchor`'s callers actually depend on, pinned so a faster
# matcher fails loudly instead of quietly redefining what "aligned" means:
# fidelity to the codepoints handed in, monotonicity, and -- the one a
# coverage-maximizing matcher breaks -- which of two equally large attachments
# wins.


def _matcher_limits() -> AlignmentLimits:
    """The shipped limits, loaded, never a copy of them.

    Restating them here would have made the tests below assert against
    numbers that agree with `config/alignment.toml` only until someone edits
    it, and the step budget is the number these tests exist to watch.
    """
    return load_alignment_limits()[0]


def _blocks(witness: str, anchor: str) -> list[tuple[int, int, int]]:
    return alignment_module._matching_blocks(witness, anchor, _matcher_limits().max_alignment_steps)


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
    the silent-empty regression costs coverage exactly where this
    file is meant to be watching. Only the two empty-input rows may return
    nothing.
    """
    blocks = _blocks(witness, anchor)
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
    an act's anchor range pull in witness text from the far end of the page."""
    witness = "et de Marie Bernard, laboureur de cette paroisse, en presence de Jean Moreau"
    anchor = "et de Marie Bernart, laboureur de ceste parroisse, en presence de Jan Moreau"
    blocks = _blocks(witness, anchor)
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
    assert _blocks("ABC", "abc") == []
    # Composed vs decomposed: the same grapheme, different codepoints. Only
    # `markup_text_view` may reconcile those, and it records the count when it
    # does; a matcher that did it silently would hide the difference.
    assert _blocks("\u00e9", "e\u0301") == []
    assert _blocks("\u00e9", "\u00e9") == [(0, 0, 1)]


def test_offsets_are_codepoint_indices_even_past_the_basic_multilingual_plane():
    """The offsets index the same normalized string `markup_text_view` built
    its offset map against, so they must be Python `str` indices -- codepoints,
    not UTF-8 bytes and not UTF-16 units. An astral character counting as two
    would shift every later offset and mis-place the raw span."""
    witness = "\U0001f600\U0001f600abc"
    assert _blocks(witness, "abc") == [(2, 0, 3)]


def test_a_shared_act_opening_attaches_to_the_act_the_witness_actually_read():
    """The property a coverage-maximizing matcher lacks, pinned so it is not
    lost to a faster one.

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
    it is a wrong one, and the pipeline's `confirmed-blank` scenario depends on
    the difference: under the second, twelve characters of page text fall
    outside every act attachment, the Recensor reads that as incomplete
    testimony coverage, and both acts are held instead of the blank being
    sealed. Longest verbatim agreement wins; that is the disambiguation this
    module is for.
    """
    anchor = "SYNTHETIC ACT ONE alpha beta gamma SYNTHETIC ACT TWO delta epsilon zeta eta"
    witness = "SYNTHETIC ACT TWO delta epsilon zeta eta"
    second_act_start = anchor.index("SYNTHETIC ACT TWO")

    blocks = _blocks(witness, anchor)

    assert sum(size for _, _, size in blocks) == len(witness), (
        "the witness read one act verbatim, so all of it has a counterpart"
    )
    assert all(anchor_start >= second_act_start for _, anchor_start, _ in blocks), (
        "no part of a reading of the second act may be attributed to the first, "
        "however many characters the two acts' openings share"
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

_VALID_LIMITS = "[limits]\nmax_characters = 1\nmax_character_pairs = 1\nmax_alignment_steps = 1\n"
_VALID_DISSENT = "[dissent]\nmax_comparison_steps = 1\n"


def test_the_loader_returns_the_sealed_limits_and_the_file_seal():
    limits, digest = load_alignment_limits()
    assert limits.max_characters > 0
    assert limits.max_character_pairs > 0
    assert limits.max_alignment_steps > 0
    assert digest == read_sealed_toml(DEFAULT_ALIGNMENT_CONFIG_PATH, "alignment")[1]


def test_the_dissent_budget_is_its_own_sealed_key_in_the_same_file():
    """The Perlector's comparison budget is read from `[dissent]`, under the
    same seal as the page limits, so a run cannot compare under one file and
    align under another."""
    record, digest = read_sealed_toml(DEFAULT_ALIGNMENT_CONFIG_PATH, "alignment")
    dissent, dissent_digest = load_dissent_limits()
    assert dissent_digest == digest == load_alignment_limits()[1]
    assert dissent.max_comparison_steps == record["dissent"]["max_comparison_steps"] > 0


def _config(limits: str = _VALID_LIMITS, dissent: str = _VALID_DISSENT) -> str:
    return f"{limits}{dissent}"


def test_either_loader_reads_its_own_table_of_one_valid_file(tmp_path):
    path = tmp_path / "limits.toml"
    path.write_text(_config(dissent="[dissent]\nmax_comparison_steps = 7\n"))
    assert load_alignment_limits(path)[0] == AlignmentLimits(1, 1, 1)
    assert load_dissent_limits(path)[0].max_comparison_steps == 7


def test_the_loader_refuses_an_unreadable_file(tmp_path):
    with pytest.raises(ContractError, match="could not be read"):
        load_alignment_limits(tmp_path / "absent.toml")


@pytest.mark.parametrize("loader", [load_alignment_limits, load_dissent_limits])
@pytest.mark.parametrize(
    "text",
    [
        _config(limits=_VALID_LIMITS.replace("max_alignment_steps", "max_alignment_step")),
        _config(limits="[limits]\nmax_characters = 1\n"),
        _config(dissent=""),
        _config(dissent="[dissent]\nmax_comparison_step = 1\n"),
        _config(dissent="[dissent]\nmax_comparison_steps = 1\nextra = 1\n"),
    ],
)
def test_the_loader_refuses_an_unknown_or_missing_key(tmp_path, loader, text):
    """Either loader refuses the whole file alike: the page limits are not
    loaded from a file whose dissent table is wrong, nor the reverse."""
    path = tmp_path / "limits.toml"
    path.write_text(text)
    with pytest.raises(ContractError, match="closed schema"):
        loader(path)


@pytest.mark.parametrize("loader", [load_alignment_limits, load_dissent_limits])
@pytest.mark.parametrize("bad", ['"3"', "true", "0", "-1", "1.5"])
@pytest.mark.parametrize("table", ["limits", "dissent"])
def test_the_loader_refuses_a_value_that_is_not_a_positive_integer(tmp_path, loader, bad, table):
    """`true` would parse as 1 and quietly cut every alignment or comparison to
    one step; a float or string would reach the matcher's budget at run time.
    The loader is where those stop."""
    path = tmp_path / "limits.toml"
    if table == "limits":
        text = _config(
            limits=_VALID_LIMITS.replace("max_alignment_steps = 1", f"max_alignment_steps = {bad}")
        )
    else:
        text = _config(dissent=f"[dissent]\nmax_comparison_steps = {bad}\n")
    path.write_text(text)
    with pytest.raises(ContractError, match="positive integers"):
        loader(path)


# --- NFC composition and the offset map


def test_nfc_composition_keeps_the_offset_map_pointing_at_the_raw_cluster():
    """Composition changes codepoint count, so indexing pre-composition offsets
    with a post-composition index would mis-point every entry after the first
    merge. Each composed character maps to the raw offset of the cluster that
    produced it -- for NFD French, the base letter the accent composed into."""
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


# --- bracket_marker_view: removes exactly the RecordGold uncertainty markers,
# offset-mapped so a span found in the stripped text still resolves back to
# the raw ink it came from.


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
    # suffix is `UNCERTAINTY_TOKENS` verbatim, so nothing is removed: only an
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


def test_feedings_uncertainty_tokens_are_the_contracts():
    feeding_module = load_stage("3_attestatores", "feeding")

    assert UNCERTAINTY_TOKENS == feeding_module._UNCERTAINTY_TOKENS
