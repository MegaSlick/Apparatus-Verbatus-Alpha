"""A page the structure pass could not mark out is held, never skipped (spec 06 test 4).

Spec 06's shape section states the requirement and the reason together: "A page
the structure seat fails on is **held visibly** and recoverable ... never
silently skipped — the old design made a missing witness fatal to the corpus;
this one makes it a named, recoverable hold."

Two levels. The pure failure-reading rule needs no run tree at all. Everything
after it is one real end-to-end orchestrator run of the `structure-failure`
scenario, because the property that matters is not that this stage writes a
hold — it is that a hold here survives every stage after it and arrives in the
Armarium's review list as a named loss rather than as an absence.

What this file does NOT prove, deliberately: spec 06 test 4 also asks that "the
recovery operation proposes a replacement region on request" for such a page.
It does not, and no change here makes it. `recovery_pass` refuses any act the
seal holds ("a held act is terminal and may not be recropped back to life"),
which is the landed cross-stage recovery contract shared with the Recensor
(spec 09), not a Designator decision. Making a structural hold recoverable is a
change to that contract and is named in this build's report rather than made
quietly here.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from common.contracts.errors import ContractError
from common.contracts.stages import ARMARIUM, DESIGNATOR
from common.runtree.store import RunTree
from common.stage import EXIT_HELD

ROOT = Path(__file__).resolve().parents[2]


def _load_designator():
    path = ROOT / "pipeline" / "2_designator" / "run.py"
    spec = importlib.util.spec_from_file_location("designator_structure_failure_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Context:
    """The two attributes `structure_failures` reads, and nothing else."""

    def __init__(self, fixture, scenario):
        self.fixture = fixture
        self.scenario = scenario


# --- level 1: reading the declared failures ------------------------------------


def test_a_failure_for_another_scenario_is_not_this_runs_failure():
    designator = _load_designator()
    fixture = {"structure_failure": [{"scenario": "other", "page_ordinal": 1, "reason_code": "x"}]}
    assert designator.structure_failures(_Context(fixture, "happy"), {1: {}}) == {}


def test_a_failure_for_this_scenario_is_read_by_page_ordinal():
    designator = _load_designator()
    fixture = {"structure_failure": [{"scenario": "s", "page_ordinal": 2, "reason_code": "why"}]}
    assert designator.structure_failures(_Context(fixture, "s"), {1: {}, 2: {}}) == {2: "why"}


def test_a_failure_naming_a_page_this_run_never_sealed_is_not_counted_twice():
    """The Exemplar's own refusal already accounts for an unsealed page.

    Holding the act a second time for a structural reason would put two holds
    on one loss and make the review list overstate what happened.
    """
    designator = _load_designator()
    fixture = {"structure_failure": [{"scenario": "s", "page_ordinal": 9, "reason_code": "why"}]}
    assert designator.structure_failures(_Context(fixture, "s"), {1: {}}) == {}


def test_two_declared_failures_for_one_page_refuse_rather_than_pick_one():
    designator = _load_designator()
    fixture = {
        "structure_failure": [
            {"scenario": "s", "page_ordinal": 1, "reason_code": "first"},
            {"scenario": "s", "page_ordinal": 1, "reason_code": "second"},
        ]
    }
    with pytest.raises(ContractError, match="may not choose one of them by order"):
        designator.structure_failures(_Context(fixture, "s"), {1: {}})


@pytest.mark.parametrize(
    ("row", "refusal"),
    [
        # Four malformed rows, three different refusals. Unpinned, this test
        # passed if *any* of them fired for *any* row -- so the closed-contract
        # check and the page-ordinal check could have swapped places, or one
        # could have stopped firing entirely, without the test noticing.
        (
            # A *missing* `reason_code` trips the closed-contract check, not the
            # reason-code check: the contract is a key set, and an absent key
            # makes the set differ just as a surplus one does. Pinning these
            # caught me assuming the opposite.
            {"scenario": "s", "page_ordinal": 1},
            r"a declared structure failure has fields outside its closed contract",
        ),
        (
            {"scenario": "s", "page_ordinal": 1, "reason_code": "why", "extra": 1},
            r"a declared structure failure has fields outside its closed contract",
        ),
        (
            {"scenario": "s", "page_ordinal": "1", "reason_code": "why"},
            r"a declared structure failure names no integer page ordinal",
        ),
        (
            {"scenario": "s", "page_ordinal": 1, "reason_code": ""},
            r"a declared structure failure names no reason code",
        ),
    ],
)
def test_a_malformed_declared_failure_is_refused(row, refusal):
    designator = _load_designator()
    with pytest.raises(ContractError, match=refusal):
        designator.structure_failures(_Context({"structure_failure": [row]}, "s"), {1: {}})


# --- level 2: the whole pipeline over a real structure failure -------------------


@pytest.fixture(scope="module")
def structure_failure_run(tmp_path_factory):
    root = tmp_path_factory.mktemp("runs")
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline" / "orchestrator" / "run.py"),
            "--fixture",
            "synthetic-two-page-v0",
            "--scenario",
            "structure-failure",
            "--run-root",
            str(root),
            "--run-id",
            "r",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    # Held, not complete and not fatal: a page nothing could mark out is an
    # honest partial result, which is the whole distinction spec 06 draws
    # against the old design's "missing witness is fatal to the corpus".
    assert result.returncode == EXIT_HELD, result.stderr
    return RunTree(root, "r")


def _artifacts(tree: RunTree, stage: str, kind: str) -> list[dict]:
    return [
        tree.read_artifact(stage, kind, entry["artifact_id"])
        for entry in tree.build_manifest(stage)["artifacts"]
        if entry["kind"] == kind
    ]


def test_the_failing_page_carries_a_held_status_naming_the_reason(structure_failure_run):
    statuses = {
        record["payload"]["page_ordinal"]: record
        for record in _artifacts(structure_failure_run, DESIGNATOR, "structure-status")
    }
    # Both sealed pages have a status. The one that succeeded says so; a page
    # whose structural outcome could only be inferred from the absence of crops
    # would be exactly the silent gap this record exists to close.
    assert set(statuses) == {1, 2}
    assert statuses[1]["outcome"] == "held"
    assert statuses[1]["payload"]["reason_code"] == "recorded-fixture-structure-failure"
    assert statuses[2]["outcome"] == "proposed"
    assert statuses[2]["payload"]["reason_code"] is None


def test_every_act_on_the_failing_page_is_held_with_the_structural_reason(structure_failure_run):
    seal = _artifacts(structure_failure_run, DESIGNATOR, "proposal-seal")[0]["payload"]
    rows = {row["act_key"]: row for row in seal["expected_acts"]}
    # The fixture's own two acts both sit on page 1 and both survive as held
    # rows. An act that vanished from the denominator is the exact failure the
    # seal exists to make impossible.
    assert rows["a1"]["outcome"] == "held"
    assert rows["a2"]["outcome"] == "held"
    assert rows["a2"]["has_continuation"] is False

    holds = {
        record["payload"]["act_key"]: record["payload"]
        for record in _artifacts(structure_failure_run, DESIGNATOR, "hold")
    }
    for key in ("a1", "a2"):
        assert holds[key]["reason_code"] == "structure-pass-held"
        assert holds[key]["blocking_page_ordinal"] == 1
        assert "recorded-fixture-structure-failure" in holds[key]["reason"]


def test_no_crop_is_cut_on_the_page_the_structure_pass_could_not_mark_out(structure_failure_run):
    regions = _artifacts(structure_failure_run, DESIGNATOR, "region")
    assert [
        record for record in regions if record["payload"]["transform"]["source_page_ordinal"] == 1
    ] == []


def test_the_unmarked_pages_ink_is_accounted_as_residual_not_as_absence(structure_failure_run):
    """The page sealed, so its ink exists. Nothing claimed it, so all of it is residual.

    This is the difference Tyrel drew on 2026-08-05 between "there was nothing
    to read" and "we could not read it": a page the structure pass failed on is
    not a blank page, and its ink has to appear somewhere. It appears here, as
    conservation residual, and each residual becomes its own held act.
    """
    conservation = {
        record["payload"]["page_ordinal"]: record["payload"]
        for record in _artifacts(structure_failure_run, DESIGNATOR, "conservation")
    }
    page_one = conservation[1]
    assert page_one["total_ink_pixel_count"] > 0
    assert page_one["claimed_pixel_count"] == 0
    assert page_one["residual_pixel_count"] == page_one["total_ink_pixel_count"]
    assert page_one["residual_components"]

    seal = _artifacts(structure_failure_run, DESIGNATOR, "proposal-seal")[0]["payload"]
    residual_keys = [
        row["act_key"] for row in seal["expected_acts"] if row["act_key"].startswith("residual:1:")
    ]
    assert len(residual_keys) == len(page_one["residual_components"])


def test_nothing_downstream_reports_the_lost_page_as_a_success(structure_failure_run):
    """Every held unit reaches the Armarium as a review item, and the run is partial."""
    export = _artifacts(structure_failure_run, ARMARIUM, "export")[0]["payload"]
    assert export["aggregate"]["status"] == "partial"
    assert export["review"], "a page nobody could mark out must leave a review item"
    assert {item["category"] for item in export["review"]} == {"held-for-review"}

    seal = _artifacts(structure_failure_run, DESIGNATOR, "proposal-seal")[0]["payload"]
    # Conservation, act for act: every expected act ends in exactly one Armarium
    # category, and here every one of them is held.
    entries = [
        entry
        for entry in structure_failure_run.build_manifest(ARMARIUM)["artifacts"]
        if entry["kind"] == "manifest-entry"
    ]
    assert len(entries) == seal["count"] == len(export["review"])


# --- a page whose background cannot be inferred is held, never dropped ----------


def _run_program(program: str, root):
    import subprocess
    import sys

    return subprocess.run(
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


def test_a_page_whose_background_cannot_be_inferred_is_tiled_and_read_not_held(
    tmp_path, monkeypatch
):
    """Tyrel, 2026-08-11, twice, and the second ruling overrides the first.

    "I'd rather err on the side of sending a blank page downstream than pull a
    page assuming it's blank and have it end up with text. Missing text is the
    worst failure." Then, settling it: **"Everything gets read every time nothing
    gets pulled out or held."**

    So a page whose modal pixel is not its paper is neither dropped, nor called
    blank, nor held for a human. It is cut into predetermined overlapping crops
    and sent downstream, and the witnesses and the Perlector -- the strong
    instruments -- decide whether there is text on it. This stage's one
    threshold is the weakest instrument in the pipeline and it does not get to
    end a page's life.
    """

    root = tmp_path / "runs"
    for program in ("pipeline/1_exemplar/door.py", "pipeline/1_exemplar/run.py"):
        result = _run_program(program, root)
        assert result.returncode == 0, f"{program}: {result.stderr}"

    designator = _load_designator()
    from common.stage import open_context, stage_parser

    args = stage_parser("background refusal test").parse_args(
        ["--run-root", str(root), "--run-id", "r", "--scenario", "happy"]
    )
    context = open_context(args, designator.DESIGNATOR)

    real_infer = designator.structure.infer_background
    refused_pages = []

    def refuse_the_first_page(width, height, rows):
        if not refused_pages:
            refused_pages.append(True)
            raise designator.structure.BackgroundInferenceRefusal(
                "the page is majority ink, so its background cannot be inferred"
            )
        return real_infer(width, height, rows)

    monkeypatch.setattr(designator.structure, "infer_background", refuse_the_first_page)

    held = designator.initial_pass(context)

    assert refused_pages, "the test never drove the refusal it is named for"
    assert held is False, (
        "a page that could not be thresholded must not put the run into a hold: "
        "everything gets read, nothing gets pulled out or held"
    )

    def _artifact_payloads(kind):
        return [
            context.tree.read_artifact(DESIGNATOR, kind, entry["artifact_id"])["payload"]
            for entry in context.tree.build_manifest(DESIGNATOR)["artifacts"]
            if entry["kind"] == kind
        ]

    holds = _artifact_payloads("hold")
    assert holds == [], f"nothing may be held for this page, got {holds}"

    seal = _artifact_payloads("proposal-seal")[0]
    outcomes = {row["act_key"]: row["outcome"] for row in seal["expected_acts"]}
    assert "held" not in outcomes.values(), f"no act may be held, got {outcomes}"

    regions = _artifact_payloads("region")
    assert regions, "the page must still have been cut: crops are what reach the readers"


def test_fallback_tiles_cover_every_row_of_the_page_and_overlap_their_neighbours():
    """The grid's two obligations, and a strip in no crop would break the first.

    Coverage, because a band of the page inside no crop is text nothing will ever
    be shown. And overlap, so a line sitting exactly on a boundary is whole
    inside one of the two neighbours rather than halved by both.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "grouping_fallback_under_test", ROOT / "pipeline" / "2_designator" / "grouping.py"
    )
    grouping = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(ROOT / "pipeline" / "2_designator"))
    spec.loader.exec_module(grouping)

    page_w, page_h = 100, 200
    tiles = grouping.fallback_tiles(page_w, page_h)

    covered = set()
    for tile in tiles:
        bounds = tile["bounds"]
        assert bounds["x"] == 0 and bounds["w"] == page_w, "a band spans the full page width"
        assert "fallback tile" in tile["rationale"], (
            "a grid crop must say it is one, so nothing downstream reads it as a detection"
        )
        covered |= set(range(bounds["y"], bounds["y"] + bounds["h"]))
    assert covered == set(range(page_h)), "every row of the page must fall inside some crop"

    for earlier, later in zip(tiles, tiles[1:], strict=False):
        earlier_end = earlier["bounds"]["y"] + earlier["bounds"]["h"]
        assert later["bounds"]["y"] < earlier_end, "adjacent bands must overlap, not merely touch"
