"""Every page's `structure-status` says what geometry that page actually ran at.

The sealed grouping policy is expressed in basis points of a page dimension, so
the pixel thresholds a page runs under are a function of the policy *and* of
that page's own size. A calibration session reading a finished run back could
recover the policy from the seal and the dimensions from the sealed pixels and
re-derive them -- and a re-derivation is exactly what stops matching the run the
day the resolution rule changes. The record publishes what executed instead
(SPEC_C 4.2): every resolved integer of `GroupingThresholds` and the page's own
width and height, on a per-page record that already exists. The field set is
spelled out below rather than taken from `dataclasses.asdict` a second time, so
a field added to the dataclass and dropped from the record fails here.

Nothing reads these back to decide anything. They are a recording, and the two
tests below are about the two ways a recording goes wrong: publishing numbers
that are not what the page ran under, and publishing numbers for a page that ran
nothing at all.
"""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from _test_support import load_designator

from common.contracts.stages import DESIGNATOR
from common.imaging import encode_grayscale_png, grayscale_rows

ROOT = Path(__file__).resolve().parents[2]
SHIPPED_GROUPING_CONFIG = ROOT / "config" / "designator_grouping.toml"


def _load_designator():
    return load_designator("designator_structure_status_thresholds_under_test")


def _base_run(root: Path) -> None:
    """Door, Exemplar and Ink Map on the shipped policy, so real pages exist."""
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
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{program}: {result.stderr}"


def _designator_context(root: Path, designator):
    from common.stage import open_context, stage_parser

    args = stage_parser("structure status thresholds test").parse_args(
        ["--run-root", str(root), "--run-id", "r", "--scenario", "happy"]
    )
    return open_context(args, designator.DESIGNATOR)


def test_each_status_publishes_the_thresholds_and_dimensions_its_page_ran_at(tmp_path):
    """Resolved from the shipped policy against each page's own decoded size.

    The expectation is re-resolved here from the sealed policy bytes and from
    the page's *own* pixels, never copied from the record under test, so a stage
    that published one page's numbers on another page's record -- the failure a
    per-page resolution invites -- fails this rather than agreeing with itself.
    """
    import grouping_config
    import structure

    root = tmp_path / "runs"
    _base_run(root)
    designator = _load_designator()
    context = _designator_context(root, designator)
    designator.initial_pass(context)

    policy = grouping_config.load_grouping_config(str(SHIPPED_GROUPING_CONFIG))
    statuses = {
        record["payload"]["page_ordinal"]: record["payload"]
        for record in (
            context.tree.read_artifact(DESIGNATOR, "structure-status", entry["artifact_id"])
            for entry in context.tree.build_manifest(DESIGNATOR)["artifacts"]
            if entry["kind"] == "structure-status"
        )
    }
    assert statuses, "the happy scenario seals pages, so it publishes statuses"

    for ordinal, status in sorted(statuses.items()):
        assert status["state"] == "scanned", (
            "no happy page is held, so every one of them ran a real structure pass"
        )
        stored = context.tree.read_bytes(_page_image_path(context, ordinal))
        width, height, _rows = grayscale_rows(stored)
        assert (status["page_width"], status["page_height"]) == (width, height)
        expected = grouping_config.resolve_thresholds(policy, width, height)
        assert status["resolved_thresholds"] == {
            "margin_px": expected.margin_px,
            "chain_gap_px": expected.chain_gap_px,
            "anchor_reach_px": expected.anchor_reach_px,
            "brace_min_height_px": expected.brace_min_height_px,
            "page_edge_reach_px": expected.page_edge_reach_px,
            "review_priority_min_dimension_px": expected.review_priority_min_dimension_px,
            "fallback_overlap_px": expected.fallback_overlap_px,
            "gap_tolerance_px": expected.gap_tolerance_px,
            "max_residual_components": expected.max_residual_components,
            "max_secondary_proposals": expected.max_secondary_proposals,
            "fallback_bands": expected.fallback_bands,
            "page_spanning_area_bp": expected.page_spanning_area_bp,
        }
        # Integers only. A float in a canonical payload is a determinism defect,
        # and basis points exist so that this resolution never produces one.
        for value in status["resolved_thresholds"].values():
            assert isinstance(value, int) and not isinstance(value, bool)

        # The ink margin is not a resolved *threshold* -- it is not a function
        # of the page's size at all -- so it sits beside them rather than inside
        # `resolved_thresholds`, and it is re-derived here from this page's own
        # pixels rather than copied off the record under test.
        background_policy = grouping_config.resolve_background_policy(policy, width, height)
        evidence = structure.infer_background_evidence(
            width, height, _rows, background_policy=background_policy
        )
        assert status["ink_margin"] == evidence["ink_margin"]
        assert status["ink_threshold"] == evidence["background"] - evidence["ink_margin"]
        assert isinstance(status["ink_margin"], int) and not isinstance(status["ink_margin"], bool)

        # `dark_mode` is the other end of the distance the margin is a fraction
        # of, and it is on the record for that reason: with it and the sealed
        # `ink_margin_bp` the margin is recomputable, and without it the margin
        # is a number whose derivation was dropped. Every fixture page takes the
        # plain modal branch and publishes no `surround` block, so this record is
        # the only place it appears at all.
        assert status["dark_mode"] == evidence["dark_mode"]
        assert status["ink_margin"] == max(
            structure.PRIMARY_MARGIN,
            (evidence["background"] - status["dark_mode"])
            * background_policy["ink_margin_bp"]
            // 10000,
        ), "the published pair recomputes the published margin"


def _page_image_path(context, ordinal: int) -> str:
    """Where the Exemplar sealed the pixels of one page of this run."""
    from common.contracts.stages import EXEMPLAR

    for entry in context.tree.build_manifest(EXEMPLAR)["artifacts"]:
        if entry["kind"] != "page":
            continue
        record = context.tree.read_artifact(EXEMPLAR, "page", entry["artifact_id"])
        if record["payload"].get("ordinal") == ordinal:
            return record["payload"]["image_path"]
    raise AssertionError(f"no sealed Exemplar page for ordinal {ordinal}")


class _Recorder:
    """The two `StageContext` methods `publish_structure_status` uses."""

    def __init__(self):
        self.payloads: dict[int, dict] = {}

    def publish(self, *, kind, subject_id, outcome, inputs, payload):
        self.payloads[payload["page_ordinal"]] = payload
        return SimpleNamespace(relative_path=f"{kind}/{payload['page_ordinal']}.json")

    def input_ref(self, relative_path: str) -> dict[str, str]:
        return {"relative_path": relative_path, "sha256": "0" * 64}


def test_a_page_held_before_analysis_publishes_no_thresholds_and_no_dimensions():
    """Null, not the numbers the structure pass would have run under.

    This record answers for the structure pass, and on a held page that pass
    never ran: naming the thresholds it *would* have resolved to would be a
    resolution reported as an execution -- the same defect `background_source`
    and `structure_evidence` are null for on exactly this page. The fields
    either say what happened or say nothing.

    The null is about this pass, not about the page. Conservation scans a
    structure-held page for real, and `test_structural_reconciliation.py` pins
    the thresholds it executed under on that page's own conservation record --
    which is where a null beside a real computation would be the lie this null
    is not.
    """
    import grouping_config

    designator = _load_designator()
    context = _Recorder()
    thresholds = grouping_config.resolve_thresholds(
        grouping_config.load_grouping_config(str(SHIPPED_GROUPING_CONFIG)), 200, 260
    )
    designator.publish_structure_status(
        context,
        {
            1: {"relative_path": "exemplar/page-1.json"},
            2: {"relative_path": "exemplar/page-2.json"},
        },
        {1: {"subject_id": "page-one"}, 2: {"subject_id": "page-two"}},
        {"chair": "test"},
        {1: "recorded-fixture-structure-failure"},
        {2: {"width": 200, "height": 260, "thresholds": thresholds, **_ANALYSIS_FIELDS}},
    )

    held = context.payloads[1]
    assert held["state"] == "held"
    assert held["page_width"] is None
    assert held["page_height"] is None
    assert held["resolved_thresholds"] is None
    assert held["background_source"] is None and held["structure_evidence"] is None
    # Same reason, same page: no pass ran here, so there is no margin it ran at
    # and no mode it was derived from.
    assert held["ink_margin"] is None and held["ink_threshold"] is None
    assert held["dark_mode"] is None

    scanned = context.payloads[2]
    assert scanned["state"] == "scanned"
    assert (scanned["page_width"], scanned["page_height"]) == (200, 260)
    assert scanned["resolved_thresholds"]["gap_tolerance_px"] == thresholds.gap_tolerance_px
    assert scanned["resolved_thresholds"]["margin_px"] == thresholds.margin_px
    assert scanned["ink_margin"] == 46
    assert scanned["ink_threshold"] == 230 - 46
    assert scanned["dark_mode"] == 90


def test_two_pages_of_different_size_each_publish_their_own_numbers():
    """A per-page record, proven by two pages that cannot agree by accident.

    Every fixture page in the happy scenario is 200x260, and `resolve_thresholds`
    is a pure function of (policy, width, height): a stage that published one
    page's resolved numbers on every page's record would still pass a test built
    entirely from same-sized pages. This test resolves against two distinct
    sizes -- a fixture-sized page and a full scan-sized page -- so a mix-up
    between the two records fails on both the dimensions and the resolved
    integers, not just one or the other.
    """
    import grouping_config

    designator = _load_designator()
    context = _Recorder()
    policy = grouping_config.load_grouping_config(str(SHIPPED_GROUPING_CONFIG))
    small = grouping_config.resolve_thresholds(policy, 200, 260)
    large = grouping_config.resolve_thresholds(policy, 2480, 3508)
    assert small != large, "the two sizes must resolve to different thresholds"

    designator.publish_structure_status(
        context,
        {
            2: {"relative_path": "exemplar/page-2.json"},
            3: {"relative_path": "exemplar/page-3.json"},
        },
        {2: {"subject_id": "page-two"}, 3: {"subject_id": "page-three"}},
        {"chair": "test"},
        {},
        {
            2: {"width": 200, "height": 260, "thresholds": small, **_ANALYSIS_FIELDS},
            3: {"width": 2480, "height": 3508, "thresholds": large, **_ANALYSIS_FIELDS},
        },
    )

    small_page = context.payloads[2]
    large_page = context.payloads[3]
    assert (small_page["page_width"], small_page["page_height"]) == (200, 260)
    assert (large_page["page_width"], large_page["page_height"]) == (2480, 3508)
    assert small_page["resolved_thresholds"] == {
        "margin_px": small.margin_px,
        "chain_gap_px": small.chain_gap_px,
        "anchor_reach_px": small.anchor_reach_px,
        "brace_min_height_px": small.brace_min_height_px,
        "page_edge_reach_px": small.page_edge_reach_px,
        "review_priority_min_dimension_px": small.review_priority_min_dimension_px,
        "fallback_overlap_px": small.fallback_overlap_px,
        "gap_tolerance_px": small.gap_tolerance_px,
        "max_residual_components": small.max_residual_components,
        "max_secondary_proposals": small.max_secondary_proposals,
        "fallback_bands": small.fallback_bands,
        "page_spanning_area_bp": small.page_spanning_area_bp,
    }
    assert large_page["resolved_thresholds"] == {
        "margin_px": large.margin_px,
        "chain_gap_px": large.chain_gap_px,
        "anchor_reach_px": large.anchor_reach_px,
        "brace_min_height_px": large.brace_min_height_px,
        "page_edge_reach_px": large.page_edge_reach_px,
        "review_priority_min_dimension_px": large.review_priority_min_dimension_px,
        "fallback_overlap_px": large.fallback_overlap_px,
        "gap_tolerance_px": large.gap_tolerance_px,
        "max_residual_components": large.max_residual_components,
        "max_secondary_proposals": large.max_secondary_proposals,
        "fallback_bands": large.fallback_bands,
        "page_spanning_area_bp": large.page_spanning_area_bp,
    }
    assert small_page["resolved_thresholds"] != large_page["resolved_thresholds"]


# The three facts `publish_structure_status` takes off an analysed page, beside
# its geometry. `ink_margin` is a per-page derivation rather than a constant, so
# a record that dropped it would leave the page's ink counts with no divider
# anyone could recover from the sealed policy alone.
_ANALYSIS_FIELDS = {
    "background_source": "inferred-modal",
    "structure_evidence": "detected",
    "background": 230,
    "ink_margin": 46,
    # The walking-skeleton page's own two modes: paper 230, ink 90. 46 is
    # `(230 - 90) * 3333 // 10000`, which is what makes the pair on the record
    # checkable rather than two numbers that happen to sit beside each other.
    "dark_mode": 90,
}


def test_analyze_page_uses_its_derived_margin_for_real_decoded_pixels(monkeypatch):
    """A shade between the derived and fixed margins is excluded only live.

    The only stub is the already-checked page-byte boundary. `_analyze_page`
    still decodes an actual PNG, infers the background, derives the margin, and
    calls its production `primary_scan`; changing that call back to
    `PRIMARY_MARGIN` makes the isolated shade become a second component.
    """
    import grouping_config

    designator = _load_designator()
    width = height = 100
    rows = [bytearray([230]) * width for _ in range(height)]
    # The 5x5 dark mark establishes dark_mode=90. The separate 3x3 shade at
    # 200 is above the derived threshold 184 but at or below fixed-20's 210.
    for y in range(10, 15):
        for x in range(10, 15):
            rows[y][x] = 90
    for y in range(70, 73):
        for x in range(70, 73):
            rows[y][x] = 200
    png = encode_grayscale_png(width, height, rows)
    page_record = {
        "payload": {"ordinal": 1, "image_path": "sealed/page.png", "source_sha256": "0" * 64}
    }
    monkeypatch.setattr(designator, "_read_checked_page_bytes", lambda _context, _page: png)
    policy = grouping_config.load_grouping_config(str(SHIPPED_GROUPING_CONFIG))

    analysis = designator._analyze_page({}, SimpleNamespace(), 1, page_record, policy)

    assert (analysis["width"], analysis["height"]) == (100, 100)
    assert analysis["background"] == 230
    assert analysis["dark_mode"] == 90
    assert analysis["ink_margin"] == 46
    assert analysis["ink_margin"] > designator.structure.PRIMARY_MARGIN
    assert [component["bounds"] for component in analysis["components"]] == [
        {"x": 10, "y": 10, "w": 5, "h": 5}
    ]

    fixed_components = designator.structure.primary_scan(
        width,
        height,
        rows,
        background=230,
        margin=designator.structure.PRIMARY_MARGIN,
        gap_tolerance_px=analysis["thresholds"].gap_tolerance_px,
    )
    assert [component["bounds"] for component in fixed_components] == [
        {"x": 10, "y": 10, "w": 5, "h": 5},
        {"x": 70, "y": 70, "w": 3, "h": 3},
    ]
