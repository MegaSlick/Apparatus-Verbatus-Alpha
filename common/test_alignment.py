"""Comparison views: markup loss is visible, offsets resolve to raw ink."""

import pytest

from common.alignment import (
    bracket_marker_view,
    markup_text_view,
)
from common.contracts.errors import SchemaRefusal
from common.contracts.uncertainty import UNCERTAINTY_TOKENS
from conftest import load_stage


def test_markup_view_strips_tags_with_offsets_and_explicit_loss():
    raw = "<p>alpha <b>beta</b></p>"
    view = markup_text_view(raw)

    assert view["text"] == "alpha beta"
    assert view["offset_map"][0] == raw.index("a")
    assert view["loss"]["markup_characters"] > 0


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


def test_an_all_markup_input_normalizes_to_a_genuinely_zero_width_offset_map():
    """Text that is entirely tags and whitespace collapses to nothing -- the
    offset map must be an honest empty list, not a crash or a fabricated
    entry standing in for characters that were never there."""
    view = markup_text_view("<p>   </p><br/>")
    assert view["text"] == ""
    assert view["offset_map"] == []


# --- Unicode normalisation keeps offsets on the raw text


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
