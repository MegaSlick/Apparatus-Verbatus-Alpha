"""R4 alignment: markup loss is visible and bounded failures are records."""

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
