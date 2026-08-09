"""The proposed display convention, and the round trip spec 11 test 2 asks for.

Every span in this file is built by hand. The Archetypus record carries no
uncertainty or gap layer yet, so there is no real one to render, and that is stated
in `display.py` rather than left for a reader to discover. What these tests prove is
that the convention *cannot* leak into the canonical field once such a layer lands:
strip a rendering and you are back at the established text, byte for byte.
"""

import pytest
from display import (
    DISPLAY_CONVENTION,
    GapAnchor,
    UncertainSpan,
    render_display,
    strip_display,
)

from common.contracts.canonical import digest_bytes

TEXT = "L'an mil sept cent trente, Marie Anne fille de Pierre"


def _hash(value: str) -> str:
    return digest_bytes(value.encode("utf-8"))


def test_a_plain_reading_renders_and_strips_to_itself():
    assert render_display(TEXT) == TEXT
    assert strip_display(render_display(TEXT)) == TEXT


@pytest.mark.parametrize(
    "spans",
    [
        {"uncertain": [UncertainSpan(28, 38)]},
        {"uncertain": [UncertainSpan(28, 38, ("Marie Aune",))]},
        {"gaps": [GapAnchor("internal", 27, "about six words")]},
        {"gaps": [GapAnchor("leading", 0)]},
        {"gaps": [GapAnchor("trailing", len(TEXT), "", ("de Boucherville",))]},
        {"gaps": [GapAnchor("whole-act", 0, "the leaf is torn away")]},
        {
            "uncertain": [UncertainSpan(28, 38, ("Marie Aune",))],
            "gaps": [GapAnchor("internal", 27), GapAnchor("trailing", len(TEXT))],
        },
    ],
)
def test_render_then_strip_returns_the_established_text_and_its_hash(spans):
    rendered = render_display(TEXT, **spans)
    assert strip_display(rendered) == TEXT
    assert _hash(strip_display(rendered)) == _hash(TEXT)


def test_a_rendering_carries_witness_variants_beside_the_text_never_inside_it():
    """Tyrel, 2026-08-05: a witness variant attaches to a gap as evidence beside the
    text, never as characters inside it. Stripping the rendering must not leave the
    variant behind, which is the mechanical form of "we don't want it making shit up".
    """
    rendered = render_display(
        TEXT, gaps=[GapAnchor("internal", 27, "one word", ("Marguerite", "Margueritte"))]
    )
    assert "Marguerite" in rendered
    assert "Marguerite" not in strip_display(rendered)
    assert strip_display(rendered) == TEXT


def test_a_gap_inside_an_uncertain_span_is_refused():
    """Ink cannot be both present-and-doubted and gone at one position."""
    with pytest.raises(ValueError, match="cannot be both"):
        render_display(TEXT, uncertain=[UncertainSpan(28, 38)], gaps=[GapAnchor("internal", 32)])


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"uncertain": [UncertainSpan(0, 5), UncertainSpan(3, 9)]}, "overlap"),
        ({"uncertain": [UncertainSpan(0, len(TEXT) + 1)]}, "past the end"),
        ({"gaps": [GapAnchor("internal", len(TEXT) + 1)]}, "past the end"),
    ],
)
def test_a_span_that_does_not_describe_the_text_is_refused(kwargs, match):
    with pytest.raises(ValueError, match=match):
        render_display(TEXT, **kwargs)


def test_an_unknown_gap_kind_is_refused():
    with pytest.raises(ValueError, match="gap kind"):
        GapAnchor("smudge", 0)


def test_the_convention_name_says_it_is_a_proposal():
    """The name travels into EXPORT_MANIFEST.json, so a reader of the product can
    tell that nobody has ruled on it yet without reading this repository."""
    assert DISPLAY_CONVENTION.endswith(".proposed.v1")
