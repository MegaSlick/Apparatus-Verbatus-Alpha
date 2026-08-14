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

import subprocess
import sys
from pathlib import Path

import pytest
from _test_support import load_designator

from common.contracts.errors import ContractError
from common.contracts.stages import ARMARIUM, DESIGNATOR
from common.runtree.store import RunTree
from common.stage import EXIT_HELD

ROOT = Path(__file__).resolve().parents[2]


def _load_designator():
    return load_designator("designator_structure_failure_under_test")


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
    assert page_one["background_source"] == "inferred-modal"
    assert isinstance(page_one["background_value"], int)
    assert page_one["total_ink_pixel_count"] > 0
    assert page_one["claimed_pixel_count"] == 0
    assert page_one["residual_pixel_count"] == page_one["total_ink_pixel_count"]
    assert page_one["residual_components"]

    status = {
        record["payload"]["page_ordinal"]: record["payload"]
        for record in _artifacts(structure_failure_run, DESIGNATOR, "structure-status")
    }[1]
    assert status["background_source"] is None, (
        "the held structure pass did not infer a background; the independent "
        "conservation record above attributes the measurement that actually ran"
    )

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


# --- a page whose background cannot be inferred: read, but never claimed ---------


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


def test_a_page_whose_background_cannot_be_inferred_is_still_cut_and_still_read(
    tmp_path, monkeypatch
):
    """Tyrel, 2026-08-11, twice, and the second ruling overrides the first.

    "I'd rather err on the side of sending a blank page downstream than pull a
    page assuming it's blank and have it end up with text. Missing text is the
    worst failure." Then, settling it: **"Everything gets read every time nothing
    gets pulled out or held."**

    So a page whose modal pixel is not its paper is neither dropped, nor called
    blank, nor held for a human. Its crops are cut and sent downstream, and the
    witnesses and the Perlector -- the strong instruments -- decide whether
    there is text on it. This stage's one threshold is the weakest instrument in
    the pipeline and it does not get to end a page's life.

    **Two facts, and this test keeps them apart.** Nothing is pulled out and
    nothing is held: no act is held, every declared act is still cut, and the
    page's own predetermined crops are cut too. And the *run* does not claim to
    have completed, because conservation could not run on this page and
    GOVERNANCE 2 refuses "complete" "unless everything reconciles". Holding a
    page out of reading is what the ruling forbids; withholding the run's
    completeness claim over a measurement that did not happen is what
    GOVERNANCE 2 and 10 require, and the two are different acts.

    Previously the page's own mean was substituted as a divider so the
    accounting "had something defensible". On the inverted scan
    `test_structure.py` uses -- 80% at 30, 20% at 220 -- that divider is 68, and
    every pixel of the dark paper counts as ink: four fifths of the page
    reconciles as unclaimed ink and mints a held act over the background.
    """

    root = tmp_path / "runs"
    for program in ("pipeline/1_exemplar/door.py", "pipeline/1_exemplar/run.py"):
        result = _run_program(program, root)
        assert result.returncode == 0, f"{program}: {result.stderr}"

    designator = _load_designator()
    context = _designator_context(root, designator)

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
    assert held is True, (
        "a page whose ink could not be measured has not reconciled, so the run may not "
        "report itself complete over it"
    )

    holds = _payloads(context, "hold")
    assert holds == [], f"nothing may be held for this page, got {holds}"

    seal = _payloads(context, "proposal-seal")[0]
    outcomes = {row["act_key"]: row["outcome"] for row in seal["expected_acts"]}
    assert "held" not in outcomes.values(), f"no act may be held, got {outcomes}"
    assert "page-fallback:1" in outcomes, (
        "the page could not be scanned, so it is cut into predetermined crops and those "
        "crops must reach the denominator that decides what is read"
    )

    regions = _payloads(context, "region")
    assert regions, "the page must still have been cut: crops are what reach the readers"

    status = next(row for row in _payloads(context, "structure-status") if row["page_ordinal"] == 1)
    assert status["background_source"] == "not-inferable"
    assert status["structure_evidence"] == "fallback-tiles"

    reconciliation = next(
        row for row in _payloads(context, "conservation") if row["page_ordinal"] == 1
    )
    assert reconciliation["ink_measurable"] is False
    assert reconciliation["total_ink_pixel_count"] is None
    assert reconciliation["residual_components"] == [], (
        "no residual may be minted over a page whose ink was never measured"
    )
    assert reconciliation["reason"]


def test_a_uniformly_dark_page_is_refused_rather_than_counted_as_zero_ink():
    """The hole the majority-ink guard could not see: mode == mean, both wrong.

    A page of solid black has `mode == mean == 0`, so `mode * count >= total`
    holds exactly and the old check passed it. `_ink_threshold(0, 20)` is then
    -20, no 8-bit sample is at or below it, and the page counts zero ink pixels
    -- so with no declared act on it the run exits `complete` over a visibly
    black page. That is the same silent loss the majority-ink guard exists to
    stop, reached by the one route it did not cover.
    """
    from structure import PRIMARY_MARGIN, infer_background, primary_scan

    width, height = 12, 12
    rows = [bytearray([0] * width) for _ in range(height)]

    # The arithmetic the old guard passed, shown rather than described.
    assert max(range(256), key=lambda v: sum(row.count(v) for row in rows)) == 0
    with pytest.raises(ContractError, match=r"darker than the 20-point ink margin"):
        infer_background(width, height, rows)
    # Defence in depth: even a caller bypassing inference cannot turn the
    # impossible threshold into an all-zero measurement.
    with pytest.raises(ContractError, match=r"below every 8-bit sample"):
        primary_scan(width, height, rows, background=0)
    assert PRIMARY_MARGIN == 20


@pytest.mark.parametrize("paper", [0, 19])
def test_a_background_too_dark_to_express_an_ink_threshold_is_refused(paper):
    """Not only pure black: any mode below the margin separates nothing."""
    from structure import infer_background

    rows = [bytearray([paper] * 8) for _ in range(8)]
    with pytest.raises(ContractError, match=r"darker than the 20-point ink margin"):
        infer_background(8, 8, rows)


def test_a_background_exactly_at_the_margin_still_infers():
    """The bound is where it is claimed to be: at 20, pure black is still ink."""
    from structure import infer_background, primary_scan

    rows = [bytearray([20] * 8) for _ in range(8)]
    rows[3][3] = 0
    assert infer_background(8, 8, rows) == 20
    assert primary_scan(8, 8, rows, background=20) == [
        {"bounds": {"x": 3, "y": 3, "w": 1, "h": 1}, "pixel_count": 1}
    ]


# --- a page the structure pass finds no ink on is cut into crops that go on ------
#
# Tyrel, 2026-08-11: "If the designator sees no text it should default to
# predetermined crops with a small margin of overlap and send the crops down
# stream to be read by everything. If all the witnesses and the perlector see no
# text on any of the crops then it's likely a true blank."
#
# `grouping.fallback_tiles` computed that grid and `run.py` handed it to
# `_match_structural_group` as match candidates. No tile was ever *cut*, so the
# second half of the ruling -- send the crops downstream -- was not built, and a
# sealed page with no ink and no declared act sent nothing at all. These tests
# drive the mechanism over a page whose pixels really are blank, so
# `structure.primary_scan` genuinely returns no component and the grid genuinely
# fires; nothing here monkeypatches the scan, the grouping, or the grid.
#
# The one substitution is the page's *pixels*: the door refuses any fixture root
# but `proof/` (`door.declared_synthetic_fixture_root`, ruling 2026-08-04 item
# 1), and every shipped fixture page carries ink, so an ink-free page cannot be
# sealed here without adding one to the shipped fixture and moving every digest
# in the tree. Substituting the sealed page's own byte reader gives this stage a
# genuinely blank page to scan, crop and reconcile end to end. What that cannot
# reach is the *downstream* lineage check, which recomputes a crop from the
# Exemplar's stored pixels; proving the fallback crops through a real
# orchestrator run needs an ink-free fixture page, and that is named in this
# build's report rather than faked here.


def _flat_page_png(width: int, height: int, value: int) -> bytes:
    from common.imaging import encode_grayscale_png

    return encode_grayscale_png(width, height, [bytearray([value]) * width for _ in range(height)])


def _page_with_one_mark_png(width: int, height: int, value: int, mark: dict, ink: int) -> bytes:
    from common.imaging import encode_grayscale_png

    rows = [bytearray([value]) * width for _ in range(height)]
    for y in range(mark["y"], mark["y"] + mark["h"]):
        for x in range(mark["x"], mark["x"] + mark["w"]):
            rows[y][x] = ink
    return encode_grayscale_png(width, height, rows)


def _designator_context(root, designator):
    from common.stage import open_context, stage_parser

    args = stage_parser("fallback crop test").parse_args(
        ["--run-root", str(root), "--run-id", "r", "--scenario", "happy"]
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


def _payloads(context, kind):
    return [
        context.tree.read_artifact(DESIGNATOR, kind, entry["artifact_id"])["payload"]
        for entry in context.tree.build_manifest(DESIGNATOR)["artifacts"]
        if entry["kind"] == kind
    ]


@pytest.fixture
def blank_first_page_run(tmp_path, monkeypatch):
    """One Designator pass whose page 1 has no ink on it at all."""
    root = tmp_path / "runs"
    for program in ("pipeline/1_exemplar/door.py", "pipeline/1_exemplar/run.py"):
        result = _run_program(program, root)
        assert result.returncode == 0, f"{program}: {result.stderr}"

    designator = _load_designator()
    context = _designator_context(root, designator)
    _substitute_page_pixels(designator, monkeypatch, 1, _flat_page_png(200, 260, 230))

    # The premise, asserted rather than assumed: the real structure pass over
    # these real pixels finds nothing. If a later threshold change made this page
    # scan as inked, every assertion below would still pass for the wrong reason.
    width, height, rows = designator.decode_grayscale_png(_flat_page_png(200, 260, 230))
    background = designator.structure.infer_background(width, height, rows)
    assert designator.structure.primary_scan(width, height, rows, background=background) == []
    assert designator.grouping.group_page([], width, height) == []

    held = designator.initial_pass(context)
    return designator, context, held


def test_a_page_with_no_found_ink_is_cut_into_fallback_crops(blank_first_page_run):
    """The tiles become real proposal regions of one minted act, not inert geometry."""
    designator, context, _held = blank_first_page_run

    statuses = {row["page_ordinal"]: row for row in _payloads(context, "structure-status")}
    assert statuses[1]["structure_evidence"] == "fallback-tiles"
    assert statuses[2]["structure_evidence"] == "detected"

    fallbacks = _payloads(context, "page-fallback")
    assert len(fallbacks) == 1, "one minted act per fallback-tiled page, never one per tile"
    fallback = fallbacks[0]
    assert fallback["page_ordinal"] == 1
    assert fallback["tile_count"] == len(designator.grouping.fallback_tiles(200, 260))

    tiles = [tile["bounds"] for tile in fallback["tiles"]]
    covered = set()
    for bounds in tiles:
        covered |= set(range(bounds["y"], bounds["y"] + bounds["h"]))
    assert covered == set(range(260)), "every row of the page must be inside some cut crop"

    regions = [
        row
        for row in _payloads(context, "region")
        if row["act_key"] == fallback["act_key"] and row["origin"] == "proposal"
    ]
    assert len(regions) == fallback["tile_count"]
    assert [
        row["transform"]["bounds"] for row in sorted(regions, key=lambda r: r["attempt_ordinal"])
    ] == tiles
    for row in regions:
        assert row["transform"]["source_page_ordinal"] == 1
        # The tile is already the final rectangle, overlap built in: expanding it
        # again by the capture padding would conflate a structural pad with a
        # capture pad (`geometry.py`).
        assert row["padding"] is None
        assert row["image_sha256"], "a fallback crop is real cut pixels, not a rectangle on paper"


def test_the_fallback_crops_reach_the_downstream_denominator(blank_first_page_run):
    """The seal is what every later stage reads. A crop outside it is not sent anywhere."""
    from common.stage import expected_acts

    _designator, context, _held = blank_first_page_run

    fallback = _payloads(context, "page-fallback")[0]
    seal = _payloads(context, "proposal-seal")[0]
    rows = {row["act_key"]: row for row in seal["expected_acts"]}
    assert fallback["act_key"] in rows, (
        "a fallback crop nothing accounts for is a crop nobody reads"
    )
    row = rows[fallback["act_key"]]
    assert row["outcome"] == "proposed", (
        "a held act is terminal and is never read; crops cut so that they can be read "
        "must reach the readers"
    )
    assert len(row["evidence"]) == fallback["tile_count"]

    # And the seal that carries it validates as the downstream expected-act
    # denominator -- the actual cross-stage contract, not this stage's own idea
    # of one. `_verify_page_fallback_act_row` recomputes the minted identity and
    # reads page 1's structure-status through a digest-checked hop to confirm the
    # premise that the structure pass really did fall back to tiles there.
    validated = {act["act_key"]: act for act in expected_acts(context)}
    assert validated[fallback["act_key"]]["act_id"] == row["act_id"]


def test_a_fallback_act_minted_over_a_detected_page_is_refused(blank_first_page_run):
    """The premise is checked against the page's own record, not the minter's word."""
    from common.contracts.errors import FatalAccounting
    from common.stage import expected_acts

    _designator, context, _held = blank_first_page_run

    seal = _payloads(context, "proposal-seal")[0]
    fallback_row = next(
        row for row in seal["expected_acts"] if row["act_key"].startswith("page-fallback:")
    )
    real_read = context.tree.read_artifact_reference

    def status_says_detected(reference, **kwargs):
        record = real_read(reference, **kwargs)
        if record["kind"] == "structure-status":
            record["payload"] = {**record["payload"], "structure_evidence": "detected"}
        return record

    context.tree.read_artifact_reference = status_says_detected
    with pytest.raises(FatalAccounting, match="may not be minted over a page"):
        expected_acts(context)
    context.tree.read_artifact_reference = real_read
    assert fallback_row["outcome"] == "proposed"


def test_a_fallback_act_bound_to_only_part_of_its_sealed_page_is_refused(blank_first_page_run):
    """A self-consistent minted identity must still deliver the complete page downstream."""
    from common.contracts.errors import FatalAccounting
    from common.stage import FALLBACK_PAGE_ACT_ORDINAL, _verify_page_fallback_act_row

    designator, context, _held = blank_first_page_run
    fallback_record = next(
        context.tree.read_artifact(DESIGNATOR, "page-fallback", entry["artifact_id"])
        for entry in context.tree.build_manifest(DESIGNATOR)["artifacts"]
        if entry["kind"] == "page-fallback"
    )
    payload = fallback_record["payload"]
    partial_bounds = {**payload["page_bounds"], "h": payload["page_bounds"]["h"] - 1}
    corrupt_act_id = designator.derive_minted_act_id(
        payload["page_id"], FALLBACK_PAGE_ACT_ORDINAL, partial_bounds
    )
    corrupt_record = {
        **fallback_record,
        "subject_id": corrupt_act_id,
        "payload": {**payload, "page_bounds": partial_bounds},
    }
    original_row = next(
        row
        for row in _payloads(context, "proposal-seal")[0]["expected_acts"]
        if row["act_key"] == payload["act_key"]
    )
    corrupt_row = {**original_row, "act_id": corrupt_act_id}

    with pytest.raises(FatalAccounting, match="not the complete sealed page rectangle"):
        _verify_page_fallback_act_row(
            context, corrupt_act_id, corrupt_row, {corrupt_act_id: corrupt_record}
        )


def test_an_act_on_a_fallback_tiled_page_records_no_detected_bounds(blank_first_page_run):
    """A computed band is not a detection, and a consumer must not need prose to tell.

    The bands span the whole page by construction, so before this distinction
    existed every declared act on such a page "matched" one: `detected_bounds`
    recorded a rectangle nothing detected, `body_member_count` was 0 beside it,
    and `_match_structural_group`'s missed-act refusal could not fire on any
    fallback-tiled page at all.
    """
    _designator, context, _held = blank_first_page_run

    groups = {row["act_key"]: row for row in _payloads(context, "act-group")}
    for key in ("a1", "a2"):
        assert groups[key]["structure_evidence"] == "fallback-tiles"
        assert groups[key]["detected_bounds"] is None
        assert groups[key]["body_member_count"] == 0
        assert groups[key]["anchor_count"] == 0

    # Act a2 continues onto page 2, which has its real ink and was really
    # detected: one payload carrying both kinds of evidence, each labelled.
    continuation = groups["a2"]["continuation"]
    assert continuation["structure_evidence"] == "detected"
    assert continuation["detected_bounds"] is not None
    assert continuation["geometric_corroboration"] is False


def test_the_missed_act_refusal_still_fires_where_detection_actually_ran(tmp_path, monkeypatch):
    """The other half of the same finding: a real detector that missed an act refuses.

    A page with one small mark, nowhere near either declared act, is a page the
    structure pass *did* find regions on. Nothing covers half of act a1's
    declared bounds, so the structure pass missed it -- and a missed act is the
    finding GOALS 1 cares about most. It must still be a refusal, not a fallback
    band quietly standing in for the detection that did not happen.
    """
    root = tmp_path / "runs"
    for program in ("pipeline/1_exemplar/door.py", "pipeline/1_exemplar/run.py"):
        result = _run_program(program, root)
        assert result.returncode == 0, f"{program}: {result.stderr}"

    designator = _load_designator()
    context = _designator_context(root, designator)
    marked = _page_with_one_mark_png(200, 260, 230, {"x": 4, "y": 240, "w": 8, "h": 8}, 30)
    _substitute_page_pixels(designator, monkeypatch, 1, marked)

    with pytest.raises(ContractError, match="may have missed this act entirely"):
        designator.initial_pass(context)


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
