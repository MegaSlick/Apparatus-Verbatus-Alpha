"""Two acts cannot both own one stretch of a page witness's reading.

Alignment is computed page-wide once per (page, chair), and each act clips its
own hull from that result. If two hulls overlap, both must become unaligned;
choosing by size would turn correspondence into witness selection.

The rule is exercised directly rather than through a run because the
combination needs one chair's page text to match a single act's anchor range in
two separate places -- no scenario in `proof/skeleton_fixture.toml` does, and
adding one to reach a rule that is already deterministic would move the pinned
run digests for nothing.

What must not appear here, ever: a size comparison, an overlap fraction, an act
ordering, or any other way of preferring one of the two claims.  That would be a
picker over one witness's text (GOVERNANCE 3, hard rule 8) wearing an alignment's
clothes.
"""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load_stage():
    spec = importlib.util.spec_from_file_location(
        "attestatores_ambiguity_under_test", ROOT / "pipeline/3_attestatores/run.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


stage = _load_stage()


def _entry(chair="attestator_1", page_ordinal=1, span=None, page_witness=True, attached=True):
    if span is None:
        alignment = {"status": "unaligned", "reason": "no-overlap-with-act-anchor"}
        act_span = None
    else:
        alignment = {
            "status": "aligned",
            "anchor_basis": "act-anchor",
            "anchor_chair": chair,
            "anchor_span": {"start": 0, "end": 0},
            "witness_span": dict(span),
            "line_geometry": [],
            "loss": {},
            "offset_maps": {},
        }
        act_span = dict(span)
    return {
        "chair": chair,
        "page_witness": page_witness,
        "page_ordinal": page_ordinal,
        "attached": attached,
        "comparable": attached and span is not None,
        "attachment_basis": "geometric-overlap" if attached else "unattached",
        "alignment": alignment,
        "span": act_span,
    }


def test_two_overlapping_act_spans_are_both_refused_and_neither_is_preferred():
    """Overlap makes both claims unusable; nothing chooses between them."""
    first = _entry(span={"start": 0, "end": 40})
    second = _entry(span={"start": 30, "end": 200})

    stage.refuse_ambiguous_act_alignments([[first], [second]])

    for entry in (first, second):
        assert entry["alignment"] == {
            "status": "unaligned",
            "reason": "ambiguous-overlapping-act-alignment",
        }
        assert entry["span"] is None
        assert entry["comparable"] is False
        # The chair really did report ink over this act's geometry, and that
        # fact is not what became ambiguous.
        assert entry["attached"] is True


def test_a_third_act_that_overlaps_neither_keeps_its_alignment():
    """Ambiguity is a property of the pair, not of the page."""
    first = _entry(span={"start": 0, "end": 40})
    second = _entry(span={"start": 30, "end": 200})
    untouched = _entry(span={"start": 400, "end": 450})

    stage.refuse_ambiguous_act_alignments([[first], [second], [untouched]])

    assert untouched["alignment"]["status"] == "aligned"
    assert untouched["span"] == {"start": 400, "end": 450}
    assert untouched["comparable"] is True


@pytest.mark.parametrize(
    ("left", "right"),
    (
        # Abutting, not overlapping: one ends exactly where the other begins.
        ({"start": 0, "end": 40}, {"start": 40, "end": 90}),
        # The trivial zero-width attach a genuinely-empty page reading gets for
        # every act on its page. Reading these as mutually ambiguous would turn
        # an honest blank corroboration into a page-wide alignment failure.
        ({"start": 0, "end": 0}, {"start": 0, "end": 0}),
    ),
)
def test_spans_that_share_no_character_are_not_ambiguous(left, right):
    first = _entry(span=left)
    second = _entry(span=right)

    stage.refuse_ambiguous_act_alignments([[first], [second]])

    assert first["alignment"]["status"] == "aligned"
    assert second["alignment"]["status"] == "aligned"


def test_overlap_is_scoped_to_one_chair_on_one_page():
    """Two chairs' spans index two different readings and never collide.

    Comparing them would be the one thing the dossier forbids outright: a
    cross-chair comparison standing in for evidence about the ink.
    """
    same_span = {"start": 10, "end": 60}
    other_chair = _entry(chair="attestator_3", span=same_span)
    other_page = _entry(page_ordinal=2, span=same_span)
    first = _entry(span=same_span)

    stage.refuse_ambiguous_act_alignments([[first], [other_chair], [other_page]])

    for entry in (first, other_chair, other_page):
        assert entry["alignment"]["status"] == "aligned", entry["chair"]


def test_an_unattached_or_act_scoped_row_is_never_swept_into_the_pair():
    """Only a geometrically attached page row carries a span to be ambiguous.

    Both other shapes are written as the pass really writes them: an unattached
    page witness keeps its own text-span derivation but carries no act span, and
    an act-scoped chair read its own crop and has no page alignment at all.
    """
    attached = _entry(span={"start": 0, "end": 40})
    unattached = _entry(span={"start": 10, "end": 20}, attached=False)
    unattached["span"] = None
    act_scoped = _entry(span={"start": 10, "end": 20}, page_witness=False, page_ordinal=None)
    act_scoped["alignment"] = None

    stage.refuse_ambiguous_act_alignments([[attached], [unattached], [act_scoped]])

    assert attached["alignment"]["status"] == "aligned"
    assert unattached["alignment"]["status"] == "aligned"
    assert act_scoped["alignment"] is None
