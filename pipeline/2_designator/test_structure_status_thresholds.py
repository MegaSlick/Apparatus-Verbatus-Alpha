"""Every page's `structure-status` says what geometry that page actually ran at.

The sealed grouping policy is expressed in basis points of a page dimension, so
the pixel thresholds a page runs under are a function of the policy *and* of
that page's own size. A calibration session reading a finished run back could
recover the policy from the seal and the dimensions from the sealed pixels and
re-derive them -- and a re-derivation is exactly what stops matching the run the
day the resolution rule changes. The record publishes what executed instead
(SPEC_C 4.2): eight resolved integers and the page's own width and height, on a
per-page record that already exists.

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
from common.imaging import grayscale_rows

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
            "gap_tolerance_px": expected.gap_tolerance_px,
            "max_residual_components": expected.max_residual_components,
        }
        # Integers only. A float in a canonical payload is a determinism defect,
        # and basis points exist so that this resolution never produces one.
        for value in status["resolved_thresholds"].values():
            assert isinstance(value, int) and not isinstance(value, bool)


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
    """Null, not the numbers the page would have run under.

    A page held before the structure pass ran executed no geometry, and naming
    the thresholds it *would* have resolved to would be a resolution reported as
    an execution -- the same defect `background_source` and `structure_evidence`
    are null for on exactly this page. The two fields either say what happened
    or say nothing.
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

    scanned = context.payloads[2]
    assert scanned["state"] == "scanned"
    assert (scanned["page_width"], scanned["page_height"]) == (200, 260)
    assert scanned["resolved_thresholds"]["gap_tolerance_px"] == thresholds.gap_tolerance_px
    assert scanned["resolved_thresholds"]["margin_px"] == thresholds.margin_px


def test_two_pages_of_different_size_each_publish_their_own_numbers():
    """A per-page record, proven by two pages that cannot agree by accident.

    Every fixture page in the happy scenario is 200x260, and `resolve_thresholds`
    is a pure function of (policy, width, height): a stage that published one
    page's resolved numbers on every page's record would still pass a test built
    entirely from same-sized pages. This test resolves against two distinct
    sizes -- a fixture-sized page and a full scan-sized page -- so a mix-up
    between the two records fails on both the dimensions and the eight integers,
    not just one or the other.
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
        "gap_tolerance_px": small.gap_tolerance_px,
        "max_residual_components": small.max_residual_components,
    }
    assert large_page["resolved_thresholds"] == {
        "margin_px": large.margin_px,
        "chain_gap_px": large.chain_gap_px,
        "anchor_reach_px": large.anchor_reach_px,
        "brace_min_height_px": large.brace_min_height_px,
        "page_edge_reach_px": large.page_edge_reach_px,
        "review_priority_min_dimension_px": large.review_priority_min_dimension_px,
        "gap_tolerance_px": large.gap_tolerance_px,
        "max_residual_components": large.max_residual_components,
    }
    assert small_page["resolved_thresholds"] != large_page["resolved_thresholds"]


_ANALYSIS_FIELDS = {"background_source": "inferred-modal", "structure_evidence": "detected"}
