"""A page the structure pass could not mark out is held, never skipped.

Two levels: the pure failure-reading rule needs no run tree at all; everything
after it is one real end-to-end orchestrator run, because what matters is not
that this stage writes a hold but that it survives every later stage and
arrives in the Armarium's review list as a named loss, not an absence.

What this file does NOT prove: that recovery can propose a replacement region
for such a page. CONTRACT.md's "What this contract does not settle" names this
required and not yet met: `recovery_pass` refuses any act the seal holds -- a
held act is terminal -- which is a cross-stage recovery contract shared with
the Recensor, not a Designator decision.
"""

import subprocess
import sys
from pathlib import Path

import pytest
from _test_support import load_designator

from common.contracts.approval import (
    ApprovalRefusal,
    real_ingress_record,
    synthetic_fixture_ingress_record,
)
from common.contracts.errors import ContractError
from common.contracts.stages import ARMARIUM, DESIGNATOR
from common.runtree.store import RunTree
from common.stage import EXIT_HELD

ROOT = Path(__file__).resolve().parents[2]


def _shipped_background_policy(width: int, height: int):
    grouping_config = _load_designator().grouping_config
    return grouping_config.resolve_background_policy(
        grouping_config.load_grouping_config(ROOT / "config" / "designator_grouping.toml"),
        width,
        height,
    )


def _load_designator():
    return load_designator("designator_structure_failure_under_test")


class _Context:
    """The two attributes `structure_failures` reads, and nothing else."""

    def __init__(self, fixture, scenario):
        self.fixture = fixture
        self.scenario = scenario


def test_the_designator_reads_its_ingress_route_off_the_context_it_was_handed(monkeypatch):
    """`_open` keeps its `(context, real_input)` tuple; the flag comes from
    `context.run`, not argv, so each case here sets argv to name one route
    and the opened context's own `run` to name the other, and the assertion
    follows the context. A regression that read argv instead would pass a
    test that handed both the same record.
    """
    designator = _load_designator()
    handed = []

    class _Opened:
        def __init__(self, run):
            self.run = run

    opened = []

    def open_stage_context(args, stage, *, registry_factory):
        handed.append((args, stage, registry_factory))
        return _Opened(opened.pop(0))

    monkeypatch.setattr(designator, "open_stage_context", open_stage_context)
    factory = object()

    real_run = {"ingress": real_ingress_record()}
    fixture_run = {"ingress": synthetic_fixture_ingress_record()}

    real_args = {"run": dict(fixture_run)}
    opened.append(real_run)
    context, real_input = designator._open(real_args, factory)
    assert real_input is True
    assert context.run is real_run

    fixture_args = {"run": dict(real_run)}
    opened.append(fixture_run)
    context, real_input = designator._open(fixture_args, factory)
    assert real_input is False
    assert context.run is fixture_run

    unrecorded_args = {"run": dict(real_run)}
    opened.append({})
    with pytest.raises(ApprovalRefusal, match="not a closed fixture-or-real record"):
        designator._open(unrecorded_args, factory)

    assert handed == [
        (real_args, DESIGNATOR, factory),
        (fixture_args, DESIGNATOR, factory),
        (unrecorded_args, DESIGNATOR, factory),
    ]


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
    """The Exemplar's own refusal already accounts for an unsealed page;
    holding it again would put two holds on one loss.
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
        # Four malformed rows, three different refusals, each pinned by
        # message so the checks can't swap places or silently stop firing.
        (
            # A missing reason_code trips the closed-contract check (a key
            # set differs on an absent key just as on a surplus one), not
            # the reason-code check.
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
    # honest partial result.
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
    assert set(statuses) == {1, 2}
    assert statuses[1]["outcome"] == "held"
    assert statuses[1]["payload"]["reason_code"] == "recorded-fixture-structure-failure"
    assert statuses[2]["outcome"] == "proposed"
    assert statuses[2]["payload"]["reason_code"] is None


def test_every_act_on_the_failing_page_is_held_with_the_structural_reason(structure_failure_run):
    seal = _artifacts(structure_failure_run, DESIGNATOR, "proposal-seal")[0]["payload"]
    rows = {row["act_key"]: row for row in seal["expected_acts"]}
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
    """The page sealed, so its ink exists. Nothing claimed it, so all of it is
    residual: a page the structure pass failed on is not a blank page, and
    its ink has to appear somewhere.
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
    # Same for geometry: null here because the structure pass ran nothing on
    # this page; conservation's own record says what it ran under.
    assert status["resolved_thresholds"] is None
    assert (status["page_width"], status["page_height"]) == (None, None)

    import grouping_config

    resolved = grouping_config.resolve_thresholds(
        grouping_config.load_grouping_config(str(ROOT / "config" / "designator_grouping.toml")),
        page_one["page_width"],
        page_one["page_height"],
    )
    assert (page_one["page_width"], page_one["page_height"]) == (200, 260), (
        "the fixture pages measure 200x260; the record names the size it decoded"
    )
    assert page_one["reconciliation_thresholds"] == {
        "gap_tolerance_px": resolved.gap_tolerance_px,
        "review_priority_min_dimension_px": resolved.review_priority_min_dimension_px,
    }, "exactly the two thresholds conservation.reconcile was given, and no more"

    seal = _artifacts(structure_failure_run, DESIGNATOR, "proposal-seal")[0]["payload"]
    residual_keys = [
        row["act_key"] for row in seal["expected_acts"] if row["act_key"].startswith("residual:1:")
    ]
    assert len(residual_keys) == len(page_one["residual_components"])


def test_nothing_downstream_reports_the_lost_page_as_a_success(structure_failure_run):
    """Every held unit reaches the Armarium as a review item, and the run is partial."""
    export = _artifacts(structure_failure_run, ARMARIUM, "export")[0]["payload"]
    assert export["aggregate"]["status"] == "partial"
    assert export["non_delivered"], "a page nobody could mark out must leave a review item"
    assert {item["category"] for item in export["non_delivered"]} == {"held-for-review"}

    seal = _artifacts(structure_failure_run, DESIGNATOR, "proposal-seal")[0]["payload"]
    entries = [
        entry
        for entry in structure_failure_run.build_manifest(ARMARIUM)["artifacts"]
        if entry["kind"] == "manifest-entry"
    ]
    assert len(entries) == seal["count"] == len(export["non_delivered"])


# --- a page whose background cannot be inferred: read, but never claimed ---------


def _run_program(program: str, root):
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
    """A page whose modal pixel is not its paper is neither dropped, nor
    called blank, nor held for a human: its crops are cut and sent downstream
    for the witnesses and the Perlector to decide, since this stage's
    threshold is the weakest instrument in the pipeline.

    Two facts kept apart here: nothing is held (every act and the page's own
    predetermined crops are still cut), but the *run* does not claim to have
    completed, since conservation could not run on this page and completeness
    requires everything to reconcile.
    """

    root = tmp_path / "runs"
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/1_ink_map/run.py",
    ):
        result = _run_program(program, root)
        assert result.returncode == 0, f"{program}: {result.stderr}"

    designator = _load_designator()
    context = _designator_context(root, designator)

    # infer_background_evidence, not infer_background: that's what run.py
    # calls; patching the thin wrapper would leave the run untouched.
    # `refused_pages` below catches that regression.
    real_infer = designator.structure.infer_background_evidence
    refused_pages = []

    def refuse_the_first_page(width, height, rows, **keywords):
        if not refused_pages:
            refused_pages.append(True)
            raise designator.structure.BackgroundInferenceRefusal(
                "the page is majority ink, so its background cannot be inferred"
            )
        return real_infer(width, height, rows, **keywords)

    monkeypatch.setattr(designator.structure, "infer_background_evidence", refuse_the_first_page)

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
    fallback = next(row for row in _payloads(context, "page-fallback") if row["page_ordinal"] == 1)
    assert "background could not be inferred" in fallback["reason"]
    assert "found no ink" not in fallback["reason"]

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
    """The hole the majority-ink guard alone could not see: a page of solid
    black has mode == mean == 0, so `mode * count >= total` holds and a naive
    check would pass it. `_ink_threshold(0, 20)` is then -20, no 8-bit sample
    is at or below it, and the page would count zero ink pixels -- the same
    silent loss the majority-ink guard exists to stop, by a route it doesn't cover.
    """
    from _test_support import infer_background
    from structure import PRIMARY_MARGIN, primary_scan

    width, height = 12, 12
    rows = [bytearray([0] * width) for _ in range(height)]

    assert max(range(256), key=lambda v: sum(row.count(v) for row in rows)) == 0
    with pytest.raises(ContractError, match=r"darker than the 20-point ink margin"):
        infer_background(
            width, height, rows, background_policy=_shipped_background_policy(width, height)
        )
    with pytest.raises(ContractError, match=r"below every 8-bit sample"):
        primary_scan(width, height, rows, background=0, margin=PRIMARY_MARGIN, gap_tolerance_px=3)
    assert PRIMARY_MARGIN == 20


def test_faint_ink_outside_primary_proposals_withholds_complete_exit(tmp_path, monkeypatch):
    """The conservation denominator includes ink the primary proposer cannot see."""
    from structure import PRIMARY_MARGIN, SECONDARY_MARGIN

    from common.imaging import decode_grayscale_png, encode_grayscale_png
    from proof.synthetic_pages import page_bytes

    root = tmp_path / "runs"
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/1_ink_map/run.py",
    ):
        result = _run_program(program, root)
        assert result.returncode == 0, f"{program}: {result.stderr}"

    width, height, rows = decode_grayscale_png(page_bytes(2))
    background = 230
    faint = background - (PRIMARY_MARGIN - 1)
    assert faint <= background - SECONDARY_MARGIN
    assert faint > background - PRIMARY_MARGIN
    rows[200][5] = faint  # outside the continuation crop on page 2
    page_with_faint_ink = encode_grayscale_png(width, height, rows)

    designator = _load_designator()
    context = _designator_context(root, designator)
    _substitute_page_pixels(designator, monkeypatch, 2, page_with_faint_ink)

    held = designator.initial_pass(context)

    reconciliation = next(
        row for row in _payloads(context, "conservation") if row["page_ordinal"] == 2
    )
    assert reconciliation["residual_pixel_count"] == 1
    # A one-pixel residual is retained in the page-level aggregate, not
    # presented as a fictitious individual act.
    assert reconciliation["residual_components"] == []
    assert reconciliation["aggregated_residual_components"] == [
        {
            "bounds": {"x": 5, "y": 200, "w": 1, "h": 1},
            "pixel_count": 1,
            "review_priority": "low",
        }
    ]
    assert held is True
    seal = _payloads(context, "proposal-seal")[0]
    assert any(row["outcome"] == "held" for row in seal["expected_acts"])


@pytest.mark.parametrize("paper", [0, 19])
def test_a_background_too_dark_to_express_an_ink_threshold_is_refused(paper):
    """Not only pure black: any mode below the margin separates nothing."""
    from _test_support import infer_background

    rows = [bytearray([paper] * 8) for _ in range(8)]
    with pytest.raises(ContractError, match=r"darker than the 20-point ink margin"):
        infer_background(8, 8, rows, background_policy=_shipped_background_policy(8, 8))


def test_a_page_of_int_lists_is_refused_by_name_inside_the_dark_distribution_test():
    """The guard that names the scanline instead of dying inside `translate`.

    `_dark_distribution` reads scanlines with `bytes.translate`, but the histogram
    loop above it iterates any sequence of integers, so a caller handing this
    module a list-of-lists page gets all the way to the surround test and then
    fails with an `AttributeError` naming neither the scanline nor the reason.
    Without this test a named refusal can quietly become an unnamed one again.

    The page has to reach the surround test to reach the guard, so it is a
    framed one: a dark border around a lighter interior, in lists of ints.
    """
    from _test_support import infer_background

    width, height = 400, 300
    rows = [[0] * width for _ in range(height)]
    for y in range(23, height - 23):
        for x in range(30, width - 30):
            # The paper is many tones, as `test_structure.photographed_page`
            # builds it: a single flat paper tone would be the page's mode and
            # this page would never reach the surround test at all.
            slot = (x * 13 + y * 7) % 11
            rows[y][x] = 40 if slot == 0 else (205 if slot <= 3 else 195 + (slot - 4))
    # The premise: this page is majority ink by the mode/mean test, so the
    # surround branch is the one that runs.
    histogram = [0] * 256
    for row in rows:
        for value in row:
            histogram[value] += 1
    mode = max(range(256), key=lambda value: histogram[value])
    assert mode * (width * height) < sum(v * c for v, c in enumerate(histogram))

    with pytest.raises(ContractError, match=r"scanline \d+ is not grayscale bytes"):
        infer_background(
            width, height, rows, background_policy=_shipped_background_policy(width, height)
        )
    # A bytes page of the same shape gets through: the refusal above is about
    # the scanline type, not the page.
    as_bytes = [bytearray(row) for row in rows]
    assert (
        infer_background(
            width, height, as_bytes, background_policy=_shipped_background_policy(width, height)
        )
        == 205
    )


def test_a_background_exactly_at_the_margin_still_infers():
    """The bound is where it is claimed to be: at 20, pure black is still ink."""
    from structure import infer_background_evidence, primary_scan

    rows = [bytearray([20] * 8) for _ in range(8)]
    rows[3][3] = 0
    evidence = infer_background_evidence(
        8, 8, rows, background_policy=_shipped_background_policy(8, 8)
    )
    assert evidence["background"] == 20
    # At a margin of 20 under a paper value of 20, pure black is exactly at
    # the threshold and still counts.
    assert evidence["ink_margin"] == 20
    assert primary_scan(8, 8, rows, background=20, margin=20, gap_tolerance_px=3) == [
        {"bounds": {"x": 3, "y": 3, "w": 1, "h": 1}, "pixel_count": 1}
    ]


# --- a page the structure pass finds no ink on is cut into crops that go on ------
#
# A page with no ink and no declared act must still be cut into predetermined
# crops and sent downstream. These tests drive that over a page whose pixels
# really are blank, so the scan genuinely finds nothing and the grid genuinely
# fires; nothing here monkeypatches the scan, the grouping, or the grid.
#
# The one substitution is the page's own pixels: every shipped fixture page
# carries ink, so an ink-free page can't be sealed without moving every
# digest in the tree, and this substitutes the sealed page's own byte reader
# instead. This cannot reach the downstream lineage check, which recomputes a
# crop from the Exemplar's stored pixels; that needs a real ink-free fixture page.


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


def test_initial_pass_resolves_structure_provenance_once_for_all_crops(tmp_path, monkeypatch):
    root = tmp_path / "runs"
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/1_ink_map/run.py",
    ):
        result = _run_program(program, root)
        assert result.returncode == 0, f"{program}: {result.stderr}"

    designator = _load_designator()
    context = _designator_context(root, designator)
    context_type = type(context)
    real_write = context_type.write_serving_receipt
    written = []

    def count_write(self, resolved, details):
        written.append(resolved.role)
        return real_write(self, resolved, details)

    monkeypatch.setattr(context_type, "write_serving_receipt", count_write)
    designator.initial_pass(context)

    assert written.count("designator_structure") == 1


@pytest.fixture
def blank_first_page_run(tmp_path, monkeypatch):
    """One Designator pass whose page 1 has no ink on it at all."""
    root = tmp_path / "runs"
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/1_ink_map/run.py",
    ):
        result = _run_program(program, root)
        assert result.returncode == 0, f"{program}: {result.stderr}"

    designator = _load_designator()
    context = _designator_context(root, designator)
    _substitute_page_pixels(designator, monkeypatch, 1, _flat_page_png(200, 260, 230))

    # The premise, asserted rather than assumed: if a later threshold change
    # made this page scan as inked, everything below would pass for the wrong reason.
    width, height, rows = designator.grayscale_rows(_flat_page_png(200, 260, 230))
    evidence = designator.structure.infer_background_evidence(
        width, height, rows, background_policy=_shipped_background_policy(width, height)
    )
    background = evidence["background"]
    thresholds = designator.grouping_config.resolve_thresholds(
        designator.grouping_config.load_grouping_config(), width, height
    )
    assert (
        designator.structure.primary_scan(
            width,
            height,
            rows,
            background=background,
            margin=evidence["ink_margin"],
            gap_tolerance_px=thresholds.gap_tolerance_px,
        )
        == []
    )
    assert (
        designator.grouping.group_page(
            [],
            width,
            height,
            margin_px=thresholds.margin_px,
            chain_gap_px=thresholds.chain_gap_px,
            anchor_reach_px=thresholds.anchor_reach_px,
            brace_min_height_px=thresholds.brace_min_height_px,
            page_spanning_area_bp=thresholds.page_spanning_area_bp,
        )
        == []
    )

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
    assert "found no ink" in fallback["reason"]

    tiles = [tile["bounds"] for tile in fallback["tiles"]]
    assert fallback["tile_count"] == len(tiles) > 0

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
        # The tile is already the final rectangle, overlap built in; expanding
        # it by capture padding would conflate a structural pad with a capture one.
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

    # The seal validates as the downstream expected-act denominator, the
    # actual cross-stage contract, not this stage's own idea of one.
    validated = {act["act_key"]: act for act in expected_acts(context)}
    assert validated[fallback["act_key"]]["act_id"] == row["act_id"]


def test_a_fallback_act_minted_over_a_detected_page_is_refused(blank_first_page_run, monkeypatch):
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

    monkeypatch.setattr(context.tree, "read_artifact_reference", status_says_detected)
    with pytest.raises(FatalAccounting, match="may not be minted over a page"):
        expected_acts(context)
    assert fallback_row["outcome"] == "proposed"


def test_a_fallback_act_bound_to_only_part_of_its_sealed_page_is_refused(blank_first_page_run):
    """A self-consistent minted identity must still deliver the complete page downstream."""
    from common.contracts.errors import FatalAccounting
    from common.stage import _verify_page_fallback_act_row

    designator, context, _held = blank_first_page_run
    fallback_record = next(
        context.tree.read_artifact(DESIGNATOR, "page-fallback", entry["artifact_id"])
        for entry in context.tree.build_manifest(DESIGNATOR)["artifacts"]
        if entry["kind"] == "page-fallback"
    )
    payload = fallback_record["payload"]
    partial_bounds = {**payload["page_bounds"], "h": payload["page_bounds"]["h"] - 1}
    corrupt_act_id = designator.derive_minted_act_id(
        payload["page_id"], "page-fallback", partial_bounds
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


def test_fallback_tiles_cover_only_pixels_no_declared_act_crop_already_claims(
    blank_first_page_run,
):
    designator, context, _held = blank_first_page_run
    regions = [
        row
        for row in _payloads(context, "region")
        if row["origin"] == "proposal" and row["transform"]["source_page_ordinal"] == 1
    ]
    fallback = [row for row in regions if row["act_key"] == "page-fallback:1"]
    declared = [row for row in regions if row["act_key"] != "page-fallback:1"]
    assert fallback and declared, "the test must exercise both identities on one page"

    for fallback_region in fallback:
        for declared_region in declared:
            assert (
                designator._overlap_area(
                    fallback_region["transform"]["bounds"],
                    declared_region["transform"]["bounds"],
                )
                == 0
            ), "one pixel may not be delivered under both act identities"

    covered = set()
    for region in regions:
        bounds = region["transform"]["bounds"]
        covered.update(
            (x, y)
            for y in range(bounds["y"], bounds["y"] + bounds["h"])
            for x in range(bounds["x"], bounds["x"] + bounds["w"])
        )
    assert covered == {(x, y) for y in range(260) for x in range(200)}


def test_declared_acts_on_a_fallback_tiled_page_record_no_detected_bounds(
    blank_first_page_run,
):
    """A computed band is not a detection, even when declared crops coexist."""
    _designator, context, _held = blank_first_page_run

    groups = {row["act_key"]: row for row in _payloads(context, "act-group")}
    for key in ("a1", "a2"):
        assert groups[key]["structure_evidence"] == "fallback-tiles"
        assert groups[key]["detected_bounds"] is None
        assert groups[key]["body_member_count"] == 0
        assert groups[key]["anchor_count"] == 0
        assert "found no ink" in groups[key]["rationale"]

    continuation = groups["a2"]["continuation"]
    assert continuation["structure_evidence"] == "detected"
    assert continuation["detected_bounds"] is not None
    assert continuation["geometric_corroboration"] is False


def test_the_missed_act_refusal_still_fires_where_detection_actually_ran(tmp_path, monkeypatch):
    """A page with one small mark, nowhere near either declared act, is a page
    the structure pass *did* find regions on -- nothing covers half of act
    a1's declared bounds, so it missed the act, and that must still refuse,
    not have a fallback band quietly stand in for the detection that never
    happened.
    """
    root = tmp_path / "runs"
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/1_ink_map/run.py",
    ):
        result = _run_program(program, root)
        assert result.returncode == 0, f"{program}: {result.stderr}"

    designator = _load_designator()
    context = _designator_context(root, designator)
    marked = _page_with_one_mark_png(200, 260, 230, {"x": 4, "y": 240, "w": 8, "h": 8}, 30)
    _substitute_page_pixels(designator, monkeypatch, 1, marked)

    with pytest.raises(ContractError, match="may have missed this act entirely"):
        designator.initial_pass(context)


def test_fallback_tiles_cover_every_row_of_the_page_and_overlap_their_neighbours():
    """The grid's two obligations: coverage (a band of the page inside no crop
    is text nothing will ever be shown) and overlap (a line on a boundary is
    whole inside one neighbour rather than halved by both).
    """
    import grouping

    page_w, page_h = 100, 200
    tiles = grouping.fallback_tiles(page_w, page_h, bands=4, overlap_px=8)

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


def test_fallback_tiles_refuse_a_band_count_or_overlap_they_cannot_cut_under():
    """A caller that forgets a keyword fails on it; one that resolves a bad
    value fails here, rather than cutting a real page's only crops under it.
    """
    import grouping

    with pytest.raises(TypeError):
        grouping.fallback_tiles(100, 200, bands=4)
    with pytest.raises(ContractError, match="cuts nothing"):
        grouping.fallback_tiles(100, 200, bands=0, overlap_px=8)
    with pytest.raises(ContractError, match="cuts nothing"):
        grouping.fallback_tiles(100, 200, bands=4.0, overlap_px=8)
    with pytest.raises(ContractError, match="not a non-negative integer"):
        grouping.fallback_tiles(100, 200, bands=4, overlap_px=-1)


def test_fallback_tiles_refuse_more_bands_than_the_page_is_tall():
    """A sealed band count the page cannot honestly cut is refused, not
    clamped: a `min(bands, page_h)` clamp would silently cut fewer bands than
    the published record claims.
    """
    import grouping

    with pytest.raises(ContractError, match="4 bands cannot be cut on a 3px-tall page"):
        grouping.fallback_tiles(100, 3, bands=4, overlap_px=0)
    # A page exactly as tall as the band count is the boundary, not a refusal:
    # every band is exactly one pixel high, none is zero.
    tiles = grouping.fallback_tiles(100, 4, bands=4, overlap_px=0)
    assert len(tiles) == 4
    assert all(tile["bounds"]["h"] >= 1 for tile in tiles)


def test_designator_refuses_two_proposals_with_identical_bounds_on_one_page():
    """The new act identity has no ordinal fallback for coincident proposals."""
    from types import SimpleNamespace

    designator = _load_designator()
    context = SimpleNamespace(
        fixture={
            "act": [
                {"key": "first", "page_ordinal": 1, "x": 1, "y": 2, "w": 3, "h": 4},
                {"key": "second", "page_ordinal": 1, "x": 1, "y": 2, "w": 3, "h": 4},
            ]
        }
    )
    with pytest.raises(ContractError, match="identical bounds"):
        designator._refuse_duplicate_proposal_bounds(context)
