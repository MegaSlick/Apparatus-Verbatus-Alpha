"""A page reconciling past the sealed residual bound becomes one review item.

The regression these tests stand for is measured, not hypothetical: this
build's own handoff records a synthetic A4 page at 300 dpi with 3% scattered
ink reconciling to roughly sixty thousand residual components, each of which
the Designator minted as its own held act, its own hold record and its own
proposal-seal row. `operations/operator/review.py` refuses to open a run past
`MAX_REVIEW_ITEMS`, by name, so one such page makes *every* page's findings
unreadable on the only surface a person uses. `max_residual_components` is the
sealed line between "one held act per residual" and "one held page".

What is under test here is the whole of that decision as the Designator takes
it, on a real Door-and-Exemplar run, over real pixels:

* the boundary itself, from both sides, against a count this file *measures*
  rather than predicts -- a page at exactly the bound enumerates every
  component and mints one held act each, and the same pixels one component
  over the bound mint one page-residual hold and no per-component act at all;
* the fixture pages, which no bound may hold: the shipped policy's 2000 is
  orders of magnitude above anything they reconcile to, and a change that
  started holding them would be a change to what a green fixture run means;
* the minted hold's every field, checked against the identity it must derive
  from and against the conservation record it must have been judged against --
  through `common/stage.py`'s own consumer, not through a second reading of it
  written here.

The A4 case itself is the last test in the file, marked `full`: an 8.7
megapixel pure-Python structure scan does not belong in the everyday leg.
"""

import subprocess
import sys
from pathlib import Path

import pytest
from _test_support import load_designator

from common.contracts.errors import FatalAccounting
from common.contracts.identities import act_bindings
from common.contracts.identities import verify as verify_identity
from common.contracts.stages import DESIGNATOR
from common.imaging import encode_grayscale_png, grayscale_rows
from common.stage import (
    RESIDUAL_ENUMERATION_COMPLETE,
    RESIDUAL_ENUMERATION_WITHHELD,
    page_residual_act_key,
    run_sealed_config_digests,
)

ROOT = Path(__file__).resolve().parents[2]
SHIPPED_GROUPING_CONFIG = ROOT / "config" / "designator_grouping.toml"

# Where the specks go on page 1, and how far apart. Page 1 is 200x260 with two
# declared acts at y 20-99 and y 120-219; their *padded* capture rectangles
# reach y 238 at the furthest, so a band starting at 240 is ink no declared
# crop can claim -- which is what makes each speck a conservation residual
# rather than part of an act. The 10px pitch is comfortably wider than the
# sealed 3px gap tolerance, so each speck labels as its own component instead
# of chaining into its neighbour; nothing here depends on that arithmetic being
# right, because `measured_scatter` counts what the reconciliation actually
# found rather than what this comment predicts.
_SCATTER_X = range(2, 198, 10)
_SCATTER_Y = range(240, 259, 10)
_SCATTER_INK = 40


def _load_designator():
    return load_designator("designator_page_residual_bound_under_test")


def _grouping_config_with_bound(directory: Path, bound: int) -> Path:
    """The shipped grouping policy with one field changed, and nothing else.

    The six page-fraction thresholds are copied byte for byte, so every page in
    these runs resolves to exactly the geometry the shipped policy resolves to
    and the only thing that varies between two runs of this file is where the
    residual bound sits. A test that also moved a threshold would be measuring
    two changes and attributing them to one.
    """
    source = SHIPPED_GROUPING_CONFIG.read_text(encoding="utf-8")
    # Asserted against the literal rather than against "the text changed", so
    # that asking for the shipped bound itself is a legitimate request rather
    # than a silent no-op that reads as a missing field.
    assert "max_residual_components = 2000" in source, (
        "the shipped grouping config no longer declares a bound of 2000"
    )
    edited = source.replace("max_residual_components = 2000", f"max_residual_components = {bound}")
    path = directory / "designator_grouping.toml"
    path.write_text(edited, encoding="utf-8")
    return path


def _scattered_page_png() -> bytes:
    """The fixture's own page 1, plus a deterministic grid of unclaimed specks.

    The page's real ink is left exactly as the fixture draws it, so both
    declared acts still match the structural groups detection finds for them and
    the run proceeds as an ordinary one. Only the band below every capture
    rectangle changes.
    """
    from proof.synthetic_pages import page_bytes

    width, height, rows = grayscale_rows(page_bytes(1))
    for y in _SCATTER_Y:
        for x in _SCATTER_X:
            rows[y][x] = _SCATTER_INK
    return encode_grayscale_png(width, height, rows)


def _base_run(root: Path, grouping_config: Path) -> None:
    """Door, Exemplar and Ink Map, so a real sealed page is on disk to read.

    Every one of the three is told which grouping policy this run uses, not only
    the door that seals its digest: each stage re-derives the run's config
    digest from its own loaded inputs and refuses to reuse a run whose sealed
    inputs have moved (`IncompatibleReuse`, naming `designator-grouping`). That
    refusal is the point-of-use recheck working, so the honest fix is to hand
    every stage the same policy rather than to route around it.
    """
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/1_ink_map/run.py",
    ):
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / program),
                "--run-root",
                str(root),
                "--run-id",
                "r",
                "--scenario",
                "happy",
                "--designator-grouping-config",
                str(grouping_config),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{program}: {result.stderr}"


def _designator_context(root: Path, designator, grouping_config: Path):
    from common.stage import open_context, stage_parser

    args = stage_parser("page residual bound test").parse_args(
        [
            "--run-root",
            str(root),
            "--run-id",
            "r",
            "--scenario",
            "happy",
            "--designator-grouping-config",
            str(grouping_config),
        ]
    )
    return open_context(args, designator.DESIGNATOR)


def _substitute_page_pixels(designator, monkeypatch, ordinal: int, data: bytes) -> None:
    """Give one sealed page different pixels, leaving every other page real."""
    real = designator._read_checked_page_bytes

    def substituted(context, page_record):
        if page_record["payload"]["ordinal"] == ordinal:
            return data
        return real(context, page_record)

    monkeypatch.setattr(designator, "_read_checked_page_bytes", substituted)


def _records(context, kind):
    return [
        context.tree.read_artifact(DESIGNATOR, kind, entry["artifact_id"])
        for entry in context.tree.build_manifest(DESIGNATOR)["artifacts"]
        if entry["kind"] == kind
    ]


def _conservation_for(context, ordinal: int) -> dict:
    records = [
        record
        for record in _records(context, "conservation")
        if record["payload"]["page_ordinal"] == ordinal
    ]
    assert len(records) == 1, f"page {ordinal} has {len(records)} conservation records"
    return records[0]


def _page_residual_holds(context) -> list[dict]:
    return [record for record in _records(context, "hold") if "page_bounds" in record["payload"]]


def _seal_rows(context) -> list[dict]:
    seals = _records(context, "proposal-seal")
    assert len(seals) == 1
    return seals[0]["payload"]["expected_acts"]


def _pass_over_scattered_page(root: Path, monkeypatch, bound: int, page_png: bytes, ordinal: int):
    """One whole Designator initial pass over a page carrying unclaimed scatter."""
    grouping_config = _grouping_config_with_bound(root.parent, bound)
    _base_run(root, grouping_config)
    designator = _load_designator()
    context = _designator_context(root, designator, grouping_config)
    _substitute_page_pixels(designator, monkeypatch, ordinal, page_png)
    held = designator.initial_pass(context)
    return designator, context, held


@pytest.fixture(scope="module")
def measured_scatter(tmp_path_factory):
    """How many residual components the scatter band actually reconciles to.

    Measured through a real pass with a bound nothing can reach, rather than
    predicted from the pitch arithmetic above. The two boundary tests are then
    stated against a number the instrument produced, which is the only way
    "exactly at the bound" and "one over it" can be the same pixels twice.
    """
    with pytest.MonkeyPatch.context() as monkeypatch:
        root = tmp_path_factory.mktemp("probe") / "runs"
        _designator, context, held = _pass_over_scattered_page(
            root, monkeypatch, 1_000_000, _scattered_page_png(), 1
        )
        payload = _conservation_for(context, 1)["payload"]
        assert payload["residual_enumeration"] == RESIDUAL_ENUMERATION_COMPLETE
        count = payload["residual_component_count"]
        assert count == len(payload["residual_components"])
        assert count > 1, "the scatter band must reconcile to more than one residual component"
        assert held is True, "unclaimed ink withholds a complete exit whether listed or not"
        return count


def test_a_page_exactly_at_the_bound_enumerates_every_component(
    tmp_path, monkeypatch, measured_scatter
):
    """At the bound, nothing changes: one held act per residual, no page hold.

    The bound is `>`, not `>=`, and which of the two it is decides the
    disposition of a page. Pinned from the side that costs a reviewer nothing.
    """
    _designator, context, held = _pass_over_scattered_page(
        tmp_path / "runs", monkeypatch, measured_scatter, _scattered_page_png(), 1
    )
    payload = _conservation_for(context, 1)["payload"]

    assert payload["residual_enumeration"] == RESIDUAL_ENUMERATION_COMPLETE
    assert payload["residual_component_count"] == measured_scatter
    assert len(payload["residual_components"]) == measured_scatter
    assert payload["max_residual_components"] == measured_scatter
    assert _conservation_for(context, 1)["outcome"] == "proposed"
    assert _page_residual_holds(context) == []

    rows = _seal_rows(context)
    residual_rows = [row for row in rows if row["act_key"].startswith("residual:1:")]
    assert len(residual_rows) == measured_scatter
    assert all(row["outcome"] == "held" for row in residual_rows)
    assert [row for row in rows if row["act_key"].startswith("page-residual:")] == []
    assert held is True


def test_one_component_over_the_bound_holds_the_page_as_a_single_item(
    tmp_path, monkeypatch, measured_scatter
):
    """The same pixels, one lower bound: one page-residual hold and nothing else.

    Every field of the record and of the hold is asserted here, because they
    are the two artifacts a reviewer reads about a page whose evidence they
    cannot open and count for themselves.
    """
    bound = measured_scatter - 1
    designator, context, held = _pass_over_scattered_page(
        tmp_path / "runs", monkeypatch, bound, _scattered_page_png(), 1
    )
    record = _conservation_for(context, 1)
    payload = record["payload"]

    assert payload["residual_enumeration"] == RESIDUAL_ENUMERATION_WITHHELD
    # Omitted, never emptied: an empty list is the claim "this page had no
    # unclaimed ink", which is the opposite of what happened, and every
    # existing consumer reads the key as a list and fails loudly on its absence.
    assert "residual_components" not in payload
    assert payload["residual_component_count"] == measured_scatter
    assert payload["max_residual_components"] == bound
    assert payload["ink_measurable"] is True
    assert record["outcome"] == "held"
    # Nothing left the measurement. The exact conservation identity is still
    # published and still exact on a page whose components are not listed.
    assert (
        payload["claimed_pixel_count"] + payload["residual_pixel_count"]
        == payload["total_ink_pixel_count"]
    )
    assert payload["residual_pixel_count"] > 0
    assert isinstance(payload["residual_ink_fraction_bp"], int)

    holds = _page_residual_holds(context)
    assert len(holds) == 1
    hold = holds[0]
    page_id = record["subject_id"]
    page_bounds = {"x": 0, "y": 0, "w": 200, "h": 260}
    assert hold["payload"] == {
        "act_key": page_residual_act_key(1),
        "page_id": page_id,
        "page_ordinal": 1,
        "page_bounds": page_bounds,
        "residual_component_count": measured_scatter,
        "max_residual_components": bound,
        "grouping_config_sha256": hold["payload"]["grouping_config_sha256"],
        "blocking_page_ordinal": 1,
        "reason_code": "residual-components-over-page-bound",
        "reason": hold["payload"]["reason"],
    }
    assert hold["payload"]["reason_code"] in designator.HOLD_REASON_CODES
    # The bound is bound to the run, not merely to itself.
    assert (
        hold["payload"]["grouping_config_sha256"]
        == run_sealed_config_digests(context.run)["designator-grouping"]
    )
    # The hold's own subject is the identity the page rectangle and the reserved
    # class derive -- recomputed here the way the consumer recomputes it.
    verify_identity(hold["subject_id"], "act", act_bindings(page_id, "page-residual", page_bounds))
    assert hold["outcome"] == "held"
    assert len(hold["inputs"]) == 1

    rows = _seal_rows(context)
    page_rows = [row for row in rows if row["act_key"] == page_residual_act_key(1)]
    assert len(page_rows) == 1
    assert page_rows[0]["act_id"] == hold["subject_id"]
    assert page_rows[0]["outcome"] == "held"
    assert page_rows[0]["page_ordinal"] == 1
    assert page_rows[0]["has_continuation"] is False
    # No per-component act is minted for a withheld page. Minting both would
    # account for the same unlisted ink twice.
    assert [row for row in rows if row["act_key"].startswith("residual:1:")] == []
    assert held is True

    # The seam this unit exists to close is producer-to-consumer, not merely
    # this file's own reading of the fields both sides call the same names.
    # `expected_acts` is the seal's own reader: it runs
    # `_verify_synthetic_act_denominator` -> `_verify_minted_act_rows` ->
    # `_verify_page_residual_act_row` (sealed-page rectangle, reserved class
    # identity, sealed grouping digest) and
    # `_verify_every_conservation_residual_is_accounted`, over this same run.
    # A real withheld run must satisfy all of it, not just the payload shape
    # asserted above.
    from common.stage import expected_acts

    acts = expected_acts(context)
    assert any(act["act_key"] == page_residual_act_key(1) for act in acts)


def test_the_withheld_pair_satisfies_the_consumers_own_premise_check(
    tmp_path, monkeypatch, measured_scatter
):
    """The two records D writes are handed to E's verifier, not to a copy of it.

    `common/stage.py::_verify_page_residual_premise` is the check that decides
    whether a withheld page is accounted for or silently lost, and asserting
    the fields separately above proves only that this file and that file agree
    about what the fields are called. This runs the consumer.
    """
    from common import stage

    bound = measured_scatter - 1
    _designator, context, _held = _pass_over_scattered_page(
        tmp_path / "runs", monkeypatch, bound, _scattered_page_png(), 1
    )
    record = _conservation_for(context, 1)
    hold = _page_residual_holds(context)[0]

    stage._verify_page_residual_premise(
        hold["subject_id"], record["subject_id"], hold["payload"], record
    )

    # And the same check refuses the same pair with one figure moved, so the
    # call above is not passing because nothing is examined.
    forged = dict(hold["payload"], residual_component_count=measured_scatter + 1)
    with pytest.raises(FatalAccounting, match="never a second figure beside it"):
        stage._verify_page_residual_premise(
            hold["subject_id"], record["subject_id"], forged, record
        )


def test_the_shipped_bound_holds_no_fixture_page(tmp_path, monkeypatch):
    """A green fixture run stays green, and every page stays enumerated.

    The shipped bound of 2000 is orders of magnitude above anything these pages
    reconcile to, and that is load-bearing rather than incidental: the
    acceptance pins are measured on this fixture, so a bound that began holding
    a fixture page would change what a green run means as well as what it
    contains.
    """
    root = tmp_path / "runs"
    _base_run(root, SHIPPED_GROUPING_CONFIG)
    designator = _load_designator()
    context = _designator_context(root, designator, SHIPPED_GROUPING_CONFIG)
    held = designator.initial_pass(context)

    assert held is False
    assert _page_residual_holds(context) == []
    conservations = _records(context, "conservation")
    assert len(conservations) == 2
    for record in conservations:
        payload = record["payload"]
        assert payload["residual_enumeration"] == RESIDUAL_ENUMERATION_COMPLETE
        assert payload["residual_component_count"] == len(payload["residual_components"])
        assert payload["residual_component_count"] <= payload["max_residual_components"]
        assert payload["max_residual_components"] == 2000
    assert [row for row in _seal_rows(context) if row["act_key"].startswith("page-residual:")] == []


@pytest.mark.full
def test_an_a4_page_at_three_percent_scatter_is_held_as_one_item(tmp_path, monkeypatch):
    """The measured regression, at the size it was measured at.

    `pipeline/2_designator/HANDOFF.md` records a synthetic A4 page at 300 dpi
    with 3% scattered ink reconciling to roughly sixty thousand residual
    components in about three seconds of labelling, and states plainly that the
    Designator would mint that many held acts, hold records and seal rows for
    it. This is that page, run through the pass that now bounds it.

    Budget: measured at ~104 seconds on this build's development machine, of
    which ~89 is `grouping.group_page` over a quarter of a million components
    and ~4 each is the structure scan and the reconciliation. `structure.py`'s
    own docstring says the pass proves mechanism rather than production scale,
    and roadmap item 4 replaces it rather than optimising it; `full` is the
    marker that keeps it out of the everyday leg until then.

    The scatter goes on page **2**, which carries one declared act rather than
    two. At this scale the chain gap resolves to 81px, which is wider than the
    gaps between scattered specks, so the structure pass chains them into a few
    thousand large groups and the declared act's own ink joins one of them. Two
    declared acts on one page would then both match a single group, which
    `_claim_structural_group` refuses by design -- a different and already
    tested property. Putting the scatter where that refusal cannot fire keeps
    this test measuring the bound.
    """
    from proof.synthetic_pages import PAGES, render_page

    width, height = 2480, 3508
    descriptor = dict(PAGES[1], width=width, height=height)
    _width, _height, rows = grayscale_rows(render_page(descriptor))
    # ~3% of the page as scattered ink: one pixel every 33 columns, on every row
    # below the declared act's padded rectangle, with the row's own start offset
    # walked so the specks do not line up into columns. No randomness, seeded or
    # otherwise -- a fixture that varies between machines cannot pin a count.
    marked = 0
    for y in range(120, height):
        row = rows[y]
        for x in range((y * 7) % 33, width, 33):
            row[x] = _SCATTER_INK
            marked += 1
    assert marked > 100_000, "the A4 scatter must be dense enough to be the measured case"
    page_png = encode_grayscale_png(width, height, rows)

    _designator, context, held = _pass_over_scattered_page(
        tmp_path / "runs", monkeypatch, 2000, page_png, 2
    )
    payload = _conservation_for(context, 2)["payload"]

    assert payload["residual_enumeration"] == RESIDUAL_ENUMERATION_WITHHELD
    assert "residual_components" not in payload
    assert payload["residual_component_count"] > 2000
    assert payload["max_residual_components"] == 2000
    holds = [hold for hold in _page_residual_holds(context) if hold["payload"]["page_ordinal"] == 2]
    assert len(holds) == 1
    # The whole point: one seal row, not tens of thousands.
    rows_sealed = _seal_rows(context)
    assert [row for row in rows_sealed if row["act_key"].startswith("residual:2:")] == []
    assert len([row for row in rows_sealed if row["act_key"] == page_residual_act_key(2)]) == 1
    assert held is True
