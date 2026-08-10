"""Residual-ink page coverage: the wiring, proven against a real run tree.

`test_residual_ink.py` proves the pixel-level arithmetic against hand-built
canvases. This proves the extraction functions in `run.py`
(`regions_by_source_page`, `sealed_page_images`, `page_coverage_findings`,
`page_coverage_for`) read the *real* Designator/Exemplar artifact shapes
correctly, and that `page_residual_ink` fires on genuine pipeline pixel bytes
when handed an incomplete covered set -- not a synthetic canvas standing in
for one.

What this file cannot yet prove end-to-end through `main()`: the walking
skeleton's synthetic Designator derives every act from the declared fixture,
so it never proposes a *short* denominator the way a real structural detector
eventually could -- there is no scenario in which the real pipeline, run
start to finish, actually misses an act on `proof/skeleton_fixture.toml`'s
pages (their ink is painted to exactly match the declared act bounds by
construction; see `proof/synthetic_pages.py`). Forging a missing region
directly is not a shortcut either: `common/stage.py`'s proposal-seal
reconciliation refuses a region set that disagrees with the sealed seal's own
evidence list before Recensor ever runs, which is a working guard, not a gap
to route around. So this exercises every function `main()` actually calls,
against a real tree, with the one input the walking skeleton cannot yet
supply (a genuinely short covered set) provided directly -- exactly the
boundary named in HANDOFF.md and `/out/report.md`.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from common.contracts.errors import FatalAccounting
from common.contracts.stages import DESIGNATOR
from common.runtree.store import RunTree

ROOT = Path(__file__).resolve().parents[2]


def _load_module(relative_path: str, name: str):
    path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RUN = _load_module("pipeline/5_recensor/run.py", "recensor_run_residual_ink_wiring")
sys.path.insert(0, str((ROOT / "pipeline" / "5_recensor")))
from residual_ink import page_residual_ink  # noqa: E402


def _invoke(root: Path, run_id: str, scenario: str, program: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / program),
            "--run-root",
            str(root),
            "--run-id",
            run_id,
            "--scenario",
            scenario,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"{program}: {result.stderr}"


def _built_through_designator(tmp_path, scenario="happy"):
    root = tmp_path / "runs"
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/2_designator/run.py",
    ):
        _invoke(root, "r", scenario, program)
    return RunTree(root, "r")


class _FakeContext:
    """Just enough of `StageContext` for these free functions: a `.tree` and
    the real sealed `.run`, which `sealed_page_images` now verifies every
    page's pixels against before trusting them."""

    def __init__(self, tree):
        self.tree = tree
        self.run = tree.read_run()


def test_regions_by_source_page_reads_every_real_designator_region(tmp_path):
    tree = _built_through_designator(tmp_path)
    by_page = RUN.regions_by_source_page(_FakeContext(tree))

    # The happy-scenario fixture: a1 on page 1, a2 on page 1 with a
    # continuation region on page 2 (proof/skeleton_fixture.toml).
    assert set(by_page) == {1, 2}
    assert len(by_page[1]) == 2  # a1's region and a2's page-1 region
    assert len(by_page[2]) == 1  # a2's continuation region
    for bounds in by_page[1] + by_page[2]:
        assert {"x", "y", "w", "h"} == set(bounds)


def test_sealed_page_images_reads_every_real_sealed_page(tmp_path):
    tree = _built_through_designator(tmp_path)
    pages = RUN.sealed_page_images(_FakeContext(tree))
    assert set(pages) == {1, 2}
    for record in pages.values():
        assert record["outcome"] == "sealed"
        assert isinstance(record["payload"]["image_path"], str)


def test_sealed_page_images_refuses_a_page_whose_pixels_no_longer_verify(tmp_path):
    """`sealed_page_images` reads raw bytes off `payload["image_path"]`, a
    self-declared field `validate_envelope` never relates to a page's own
    digest-checked `inputs`. Every other stage that reads sealed page pixels
    (`pipeline/2_designator/run.py`, `pipeline/7_armarium/run.py`) calls
    `verify_sealed_page_pixels` first; this proves the Recensor's own wiring
    does too, rather than trusting the pixels this stage was told to check.

    `verify_sealed_page_pixels` itself is already proven directly against
    every kind of mismatch in `common/test_exemplar_boundary.py`; this only
    proves this stage's own call site actually reaches it -- corrupting the
    run's submitted filename ledger for page 1 is the simplest mismatch that
    function is already known to refuse.
    """
    tree = _built_through_designator(tmp_path)
    context = _FakeContext(tree)
    context.run = dict(context.run)
    context.run["source_manifest"] = [
        dict(row, relative_path="a-different-file.png") if row["ordinal"] == 1 else row
        for row in context.run["source_manifest"]
    ]
    with pytest.raises(FatalAccounting, match="failed pixel verification"):
        RUN.sealed_page_images(context)


def test_sealed_page_images_refuses_duplicate_ordinals_instead_of_selecting_one():
    class DuplicatePageTree:
        def build_manifest(self, _stage):
            return {
                "artifacts": [
                    {"kind": "page", "artifact_id": "page-a"},
                    {"kind": "page", "artifact_id": "page-b"},
                ]
            }

        def read_artifact(self, _stage, _kind, artifact_id):
            return {
                "artifact_id": artifact_id,
                "outcome": "sealed",
                "payload": {"ordinal": 1, "image_path": f"{artifact_id}.png"},
            }

        def read_run(self):
            # Never reached: the duplicate-ordinal refusal fires in the
            # purely structural first pass, before pixel verification (which
            # would need this to be a real source manifest) ever runs.
            return {"source_manifest": [{"ordinal": 1}]}

    with pytest.raises(FatalAccounting, match="more than one sealed page for ordinal 1"):
        RUN.sealed_page_images(_FakeContext(DuplicatePageTree()))


def test_page_coverage_findings_does_not_flag_the_real_fully_covered_fixture(tmp_path):
    """The fixture's own ink is painted to exactly match its declared act
    bounds (`proof/synthetic_pages.py`) -- proof the check does not misfire
    on ordinary, fully-accounted-for pages."""
    tree = _built_through_designator(tmp_path)
    findings = RUN.page_coverage_findings(_FakeContext(tree))
    assert set(findings) == {1, 2}
    for ordinal, finding in findings.items():
        assert finding["flagged"] is False, f"page {ordinal}: {finding}"
        assert finding["outside_ink_pixels"] == 0


def test_a_genuinely_incomplete_covered_set_flags_real_pipeline_pixels(tmp_path):
    """Real sealed page-1 bytes (Designator/Exemplar output, not a hand-built
    canvas), with a1's region deliberately left out of `covered` -- exactly
    the shape of evidence a Designator that missed an act would leave behind.
    Proves detection against genuine pipeline pixels, not synthetic ones."""
    tree = _built_through_designator(tmp_path)
    context = _FakeContext(tree)
    by_page = RUN.regions_by_source_page(context)
    pages = RUN.sealed_page_images(context)
    image_bytes = tree.read_bytes(pages[1]["payload"]["image_path"])

    # a1 is the smaller-x0/y0 region (x=20,y=20); a2 is x=20,y=120. Keep only
    # the region with the larger y as "covered", omitting a1's.
    incomplete_coverage = [max(by_page[1], key=lambda bounds: bounds["y"])]
    assert len(incomplete_coverage) < len(by_page[1])

    finding = page_residual_ink(image_bytes, incomplete_coverage)
    assert finding["flagged"] is True
    assert finding["outside_ink_pixels"] > 0

    # And the full, real coverage set clears it, on the identical bytes.
    full_finding = page_residual_ink(image_bytes, by_page[1])
    assert full_finding["flagged"] is False


def test_page_coverage_for_reads_every_page_an_acts_own_regions_touch(tmp_path):
    """Exercises the exact call `main()` makes -- real `state["regions"]`-shaped
    region records against a synthetic findings dict, so the ACT-level
    extraction (which page ordinals an act's regions touch, and whether any of
    them is flagged) is proven independent of the pixel arithmetic."""
    tree = _built_through_designator(tmp_path)
    a2_regions = [
        record
        for record in (
            tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
            for entry in tree.build_manifest(DESIGNATOR)["artifacts"]
            if entry["kind"] == "region"
        )
        if record["payload"]["act_key"] == "a2"
    ]
    assert {region["payload"]["transform"]["source_page_ordinal"] for region in a2_regions} == {
        1,
        2,
    }

    def flagged(findings):
        return RUN.page_coverage_for(a2_regions, findings)["flagged_pages"]

    # `checked_pages` is every page the act's regions touch, whatever the
    # findings say about them; only `flagged_pages` narrows.
    assert RUN.page_coverage_for(a2_regions, {})["checked_pages"] == [1, 2]
    assert flagged({1: {"flagged": False}, 2: {"flagged": False}}) == []
    assert flagged({1: {"flagged": False}, 2: {"flagged": True}}) == [2]
    assert flagged({1: {"flagged": True}, 2: {"flagged": True}}) == [1, 2]
    # A page with no finding at all (never checked) is never treated as flagged.
    assert flagged({}) == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
