"""Residual presentation preserves components without inventing acts.

Significant components become individual held acts. Components below both
sealed presentation floors retain exact geometry and pixels on a page-level
aggregate hold. The legacy component ceiling is varied here only to prove it no
longer changes current producer behavior.

The A4 case itself is the last test in the file, marked `full`: an 8.7
megapixel pure-Python structure scan does not belong in the everyday leg.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from common.contracts.stages import DESIGNATOR
from common.imaging import encode_grayscale_png, grayscale_rows
from common.stage import (
    RESIDUAL_ENUMERATION_AGGREGATED,
    RESIDUAL_ENUMERATION_COMPLETE,
    page_residual_act_key,
)
from conftest import load_stage, programs_through

ROOT = Path(__file__).resolve().parents[2]
SHIPPED_GROUPING_CONFIG = ROOT / "config" / "designator_grouping.toml"

# A band starting at y=240 is ink no declared crop's padded rectangle claims
# (they reach y=238 at furthest), and the 10px pitch is wide enough that each
# speck labels as its own component rather than chaining into a neighbour.
# `measured_scatter` counts the actual reconciliation, not this arithmetic.
_SCATTER_X = range(2, 198, 10)
_SCATTER_Y = range(240, 259, 10)
_SCATTER_INK = 40


def _grouping_config_with_bound(
    directory: Path, bound: int, *, promote_all_components: bool = True
) -> Path:
    """The shipped grouping policy with an isolated cardinality bound.

    The six page-fraction thresholds are copied byte for byte, so these runs
    resolve to the same geometry the shipped policy resolves to. Cardinality tests
    promote every component to isolate their legacy ceiling; aggregate
    presentation cases keep the sealed floors.
    """
    source = SHIPPED_GROUPING_CONFIG.read_text(encoding="utf-8")
    # Asserted against the literal so a shipped-value change fails loudly here
    # rather than silently becoming a no-op edit.
    assert "max_residual_components = 2000" in source, (
        "the shipped grouping config no longer declares a bound of 2000"
    )
    edited = source.replace("max_residual_components = 2000", f"max_residual_components = {bound}")
    if promote_all_components:
        for declaration in (
            "residual_aggregate_max_pixel_count = 500",
            "residual_aggregate_max_area_px = 2000",
        ):
            assert declaration in edited, (
                f"the shipped grouping config no longer declares {declaration!r}"
            )
        edited = edited.replace(
            "residual_aggregate_max_pixel_count = 500", "residual_aggregate_max_pixel_count = 0"
        )
        edited = edited.replace(
            "residual_aggregate_max_area_px = 2000", "residual_aggregate_max_area_px = 0"
        )
    path = directory / "designator_grouping.toml"
    path.write_text(edited, encoding="utf-8")
    return path


def _scattered_page_png() -> bytes:
    """The fixture's own page 1, plus a deterministic grid of unclaimed specks."""
    from proof.synthetic_pages import page_bytes

    width, height, rows = grayscale_rows(page_bytes(1))
    for y in _SCATTER_Y:
        for x in _SCATTER_X:
            rows[y][x] = _SCATTER_INK
    return encode_grayscale_png(width, height, rows)


def _base_run(root: Path, grouping_config: Path) -> None:
    """Door, Exemplar and Ink Map, so a real sealed page is on disk to read.

    Every stage is told the same grouping policy: each re-derives the run's
    config digest from its own loaded inputs and refuses to reuse a run whose
    sealed inputs moved, so giving them different policies would trip
    IncompatibleReuse rather than test anything.
    """
    for program in programs_through("ink-map"):
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


def _pass_over_scattered_page(
    root: Path,
    monkeypatch,
    bound: int,
    page_png: bytes,
    ordinal: int,
    *,
    promote_all_components: bool = True,
):
    """One whole Designator initial pass over a page carrying unclaimed scatter."""
    grouping_config = _grouping_config_with_bound(
        root.parent, bound, promote_all_components=promote_all_components
    )
    _base_run(root, grouping_config)
    designator = load_stage("2_designator")
    context = _designator_context(root, designator, grouping_config)
    _substitute_page_pixels(designator, monkeypatch, ordinal, page_png)
    held = designator.initial_pass(context)
    return designator, context, held


@pytest.fixture(scope="module")
def measured_scatter(tmp_path_factory):
    """How many residual components the scatter band actually reconciles to.

    Measured through a real pass with a bound nothing can reach, rather than
    predicted from the pitch arithmetic above, so the boundary tests below are
    stated against what the instrument actually produced.
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

    The bound is `>`, not `>=`; pinned from the side that costs a reviewer nothing.
    """
    _designator, context, held = _pass_over_scattered_page(
        tmp_path / "runs", monkeypatch, measured_scatter, _scattered_page_png(), 1
    )
    payload = _conservation_for(context, 1)["payload"]

    assert payload["residual_enumeration"] == RESIDUAL_ENUMERATION_COMPLETE
    assert payload["residual_component_count"] == measured_scatter
    assert len(payload["residual_components"]) == measured_scatter
    assert "max_residual_components" not in payload
    assert _conservation_for(context, 1)["outcome"] == "proposed"
    assert _page_residual_holds(context) == []

    rows = _seal_rows(context)
    residual_rows = [row for row in rows if row["act_key"].startswith("residual:1:")]
    assert len(residual_rows) == measured_scatter
    assert all(row["outcome"] == "held" for row in residual_rows)
    assert [row for row in rows if row["act_key"].startswith("page-residual:")] == []
    assert held is True


def test_small_residuals_are_retained_as_accounting_not_fictitious_acts(tmp_path, monkeypatch):
    """Every speck remains inspectable without claiming the specks are one act."""
    source = SHIPPED_GROUPING_CONFIG.read_text(encoding="utf-8")
    grouping_config = tmp_path / "designator_grouping.toml"
    grouping_config.write_text(
        source.replace("max_residual_components = 2000", "max_residual_components = 1000000"),
        encoding="utf-8",
    )
    _base_run(tmp_path / "runs", grouping_config)
    designator = load_stage("2_designator")
    context = _designator_context(tmp_path / "runs", designator, grouping_config)
    _substitute_page_pixels(designator, monkeypatch, 1, _scattered_page_png())
    assert designator.initial_pass(context) is True

    payload = _conservation_for(context, 1)["payload"]
    aggregate = payload["aggregated_residual_components"]
    assert payload["residual_enumeration"] == RESIDUAL_ENUMERATION_AGGREGATED
    assert payload["residual_component_count"] == len(aggregate)
    assert payload["residual_promoted_component_count"] == 0
    assert payload["residual_aggregated_component_count"] == len(aggregate)
    assert all({"bounds", "pixel_count"} <= set(component) for component in aggregate)
    assert payload["residual_components"] == []

    rows = _seal_rows(context)
    assert [row for row in rows if row["act_key"].startswith("residual:1:")] == []
    page_rows = [row for row in rows if row["act_key"] == page_residual_act_key(1)]
    assert len(page_rows) == 1 and page_rows[0]["outcome"] == "held"

    finding = load_stage("5_recensor").geometry_coverage_inputs(context)[1]
    assert finding["residual_enumeration"] == RESIDUAL_ENUMERATION_AGGREGATED
    assert finding["residual_component_count"] == len(aggregate)
    assert finding["page_residual_act_count"] == 1


def test_residual_at_either_presentation_threshold_is_promoted():
    designator = load_stage("2_designator")
    policy = designator.grouping_config.load_grouping_config(SHIPPED_GROUPING_CONFIG)
    thresholds = designator.grouping_config.resolve_thresholds(policy, 200, 260)
    pixel_equal = {"bounds": {"x": 0, "y": 0, "w": 1, "h": 1}, "pixel_count": 500}
    area_equal = {
        "bounds": {"x": 0, "y": 0, "w": 40, "h": 50},
        "pixel_count": 1,
    }

    promoted, aggregated = designator._partition_residual_components(
        [pixel_equal, area_equal], thresholds
    )

    assert promoted == [pixel_equal, area_equal]
    assert aggregated == []


def test_component_count_never_suppresses_individual_significant_residuals(
    tmp_path, monkeypatch, measured_scatter
):
    """A dust-count cap cannot turn significant components into one fake act."""
    bound = measured_scatter - 1
    designator, context, held = _pass_over_scattered_page(
        tmp_path / "runs", monkeypatch, bound, _scattered_page_png(), 1
    )
    payload = _conservation_for(context, 1)["payload"]
    assert payload["residual_enumeration"] == RESIDUAL_ENUMERATION_COMPLETE
    assert payload["residual_component_count"] == measured_scatter
    rows = _seal_rows(context)
    assert (
        len([row for row in rows if row["act_key"].startswith("residual:1:")]) == measured_scatter
    )
    assert _page_residual_holds(context) == []
    assert held is True


def test_the_shipped_bound_holds_no_fixture_page(tmp_path, monkeypatch):
    """A green fixture run stays green, and every page stays enumerated.

    The shipped bound of 2000 is orders of magnitude above anything these
    pages reconcile to; if it ever began holding a fixture page, that would
    change what a green run means, not just what it contains.
    """
    root = tmp_path / "runs"
    _base_run(root, SHIPPED_GROUPING_CONFIG)
    designator = load_stage("2_designator")
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
        assert "max_residual_components" not in payload
    assert [row for row in _seal_rows(context) if row["act_key"].startswith("page-residual:")] == []


@pytest.mark.full
def test_an_a4_page_at_three_percent_scatter_is_held_as_one_item(tmp_path, monkeypatch):
    """The measured regression, at the size it was measured at: a synthetic A4
    page at 300 dpi with 3% scattered ink, which reconciles to roughly sixty
    thousand residual components.

    Budget: ~104 seconds on this build's development machine, ~89 of which is
    `grouping.group_page` over a quarter of a million components; `full` keeps
    that cost out of the everyday leg. `live_initial_pass` calls the same
    `_analyze_page` as this test exercises, so the live route is bounded too.
    `structure.py`'s own docstring names what's still outstanding in
    `ink_pixels`'s memory use.

    The scatter goes on page 2, which carries one declared act: with two, the
    chain gap at this scale (81px, wider than the speck spacing) would chain
    the scatter into large groups that both declared acts match, which
    `_claim_structural_group` refuses by design -- a different property this
    test does not mean to exercise.
    """
    from proof.synthetic_pages import PAGES, render_page

    width, height = 2480, 3508
    descriptor = dict(PAGES[1], width=width, height=height)
    _width, _height, rows = grayscale_rows(render_page(descriptor))
    # Deterministic, not random: a fixture that varies between machines
    # cannot pin a count. The row offset walks so specks don't line up in columns.
    marked = 0
    for y in range(120, height):
        row = rows[y]
        for x in range((y * 7) % 33, width, 33):
            row[x] = _SCATTER_INK
            marked += 1
    assert marked > 100_000, "the A4 scatter must be dense enough to be the measured case"
    page_png = encode_grayscale_png(width, height, rows)

    _designator, context, held = _pass_over_scattered_page(
        tmp_path / "runs", monkeypatch, 2000, page_png, 2, promote_all_components=False
    )
    payload = _conservation_for(context, 2)["payload"]

    assert payload["residual_enumeration"] == RESIDUAL_ENUMERATION_AGGREGATED
    assert payload["residual_component_count"] > 2000
    assert payload["residual_component_count"] == (
        len(payload["residual_components"]) + len(payload["aggregated_residual_components"])
    )
    holds = [hold for hold in _page_residual_holds(context) if hold["payload"]["page_ordinal"] == 2]
    assert len(holds) == 1
    # The whole point: one seal row, not tens of thousands.
    rows_sealed = _seal_rows(context)
    assert [row for row in rows_sealed if row["act_key"].startswith("residual:2:")] == []
    assert len([row for row in rows_sealed if row["act_key"] == page_residual_act_key(2)]) == 1
    assert held is True
