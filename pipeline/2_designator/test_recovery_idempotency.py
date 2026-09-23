"""Recovery cuts one crop per fulfilled request, never a second author for one.

The orchestrator's own recovery loop cannot double-invoke the Designator for one
act (`pending_recoveries` drops an act from the outstanding set the moment its
latest Recensor review stops being "recovery-requested"), so this drives the
stage directly the way its own module docstring documents as legitimate operator
usage -- the same "operator misuse, not orchestrator misuse" path this repair
closes.
"""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from _test_support import load_designator

from common.contracts.canonical import digest_bytes
from common.contracts.errors import ContractError
from common.contracts.stages import DESIGNATOR
from common.stage import EXIT_COMPLETE, EXIT_FATAL, EXIT_HELD

ROOT = Path(__file__).resolve().parents[2]


def _run(program: str, root: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / program),
            "--run-root",
            str(root),
            "--run-id",
            "r",
            "--scenario",
            "review",
            *extra,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def test_recovering_the_same_act_twice_refuses_rather_than_cutting_a_duplicate(tmp_path):
    root = tmp_path / "runs"
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/1_ink_map/run.py",
        "pipeline/2_designator/run.py",
        "pipeline/3_attestatores/run.py",
        "pipeline/4_perlector/run.py",
        "pipeline/5_recensor/run.py",
    ):
        result = _run(program, root)
        assert result.returncode in (EXIT_COMPLETE, EXIT_HELD), f"{program}: {result.stderr}"

    from common.contracts.stages import RECENSOR
    from common.runtree.store import RunTree

    tree = RunTree(root, "r")
    review = next(
        record
        for record in (
            tree.read_artifact(RECENSOR, "review", entry["artifact_id"])
            for entry in tree.build_manifest(RECENSOR)["artifacts"]
            if entry["kind"] == "review"
        )
        if record["payload"]["act_key"] == "a1"
    )
    act_id = review["subject_id"]
    request_id = review["payload"]["recovery_request_ref"]["relative_path"].rsplit("/", 1)[-1][:-5]
    assert review["outcome"] == "recovery-requested"

    recovery_args = (
        "--operation",
        "recover",
        "--act",
        act_id,
        "--recovery-request",
        request_id,
    )
    first = _run("pipeline/2_designator/run.py", root, *recovery_args)
    assert first.returncode == 0, first.stderr

    from common.contracts.stages import DESIGNATOR

    recovery_regions_before = [
        record
        for record in (
            tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
            for entry in tree.build_manifest(DESIGNATOR)["artifacts"]
            if entry["kind"] == "region"
        )
        if record["subject_id"] == act_id and record["payload"]["origin"] == "recovery"
    ]
    assert len(recovery_regions_before) == 1

    second = _run("pipeline/2_designator/run.py", root, *recovery_args)
    assert second.returncode == EXIT_FATAL
    assert "already has a region cut" in second.stderr

    recovery_regions_after = [
        record
        for record in (
            tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
            for entry in tree.build_manifest(DESIGNATOR)["artifacts"]
            if entry["kind"] == "region"
        )
        if record["subject_id"] == act_id and record["payload"]["origin"] == "recovery"
    ]
    assert recovery_regions_after == recovery_regions_before, (
        "a refused duplicate recovery call must not still cut a second region"
    )


def test_an_unrecognized_operation_refuses_rather_than_running_initial_pass(tmp_path):
    """A typo of "recover" must not silently fall through to a full initial
    pass -- it must be refused as the unrecognized operation it is."""
    root = tmp_path / "runs"
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/1_ink_map/run.py",
    ):
        result = _run(program, root)
        assert result.returncode == 0, f"{program}: {result.stderr}"

    result = _run("pipeline/2_designator/run.py", root, "--operation", "Recover")
    assert result.returncode == EXIT_FATAL, result.stdout
    assert "is not one of 'initial' or 'recover'" in result.stderr
    assert not (root / "r" / "2_designator" / "artifacts").exists(), (
        "an unrecognized operation must refuse before any region or seal is written"
    )


def _load_designator():
    return load_designator("designator_recovery_under_test")


class _EvidenceTree:
    def __init__(self, testimony, ink_map, regions=()):
        self.testimony = testimony
        self.ink_map = ink_map
        self.regions = list(regions)

    def read_artifact_reference(self, reference, *, stage, kind, subject_id):
        record = self.testimony if kind == "page-testimonium" else self.ink_map
        assert record["stage"] == stage and record["kind"] == kind
        assert record["subject_id"] == subject_id
        assert reference["relative_path"].endswith(record["artifact_id"] + ".json")
        return record

    def build_manifest(self, stage):
        return {
            "artifacts": [
                {"kind": "region", "artifact_id": row["artifact_id"]} for row in self.regions
            ]
        }

    def read_artifact(self, stage, kind, artifact_id):
        return next(row for row in self.regions if row["artifact_id"] == artifact_id)


def _coverage_evidence_case():
    testimony_ref = {
        "relative_path": "stages/3_attestatores/page-testimonium/testimony-1.json",
        "sha256": "1" * 64,
    }
    ink_map_ref = {
        "relative_path": "stages/1_ink_map/ink-map/ink-map-1.json",
        "sha256": "2" * 64,
    }
    bounds = {"x": 0, "y": 0, "w": 5, "h": 5}
    testimony = {
        "artifact_id": "testimony-1",
        "stage": "attestatores",
        "kind": "page-testimonium",
        "subject_id": "page-1",
        "payload": {
            "page_ordinal": 1,
            "observed": [{"ordinal": 0, "bounds": bounds, "bounds_source": "native", "span": None}],
        },
    }
    rows = [[[0, 5]] if y < 5 else [] for y in range(10)]
    ink_map = {
        "artifact_id": "ink-map-1",
        "stage": "ink-map",
        "kind": "ink-map",
        "subject_id": "page-1",
        "payload": {
            "page_ordinal": 1,
            "edge_findings": {
                "schema": "ink-runs.v2",
                "width": 10,
                "height": 10,
                "rows": rows,
            },
        },
    }
    config = ROOT / "config/designator_grouping.toml"
    context = SimpleNamespace(
        tree=_EvidenceTree(testimony, ink_map),
        args=SimpleNamespace(designator_grouping_config=str(config)),
        run={"sealed_config_digests": {"designator-grouping": digest_bytes(config.read_bytes())}},
    )
    context.require_sealed_config = lambda name, digest: (
        None
        if context.run["sealed_config_digests"].get(name) == digest
        else (_ for _ in ()).throw(ContractError("config drift"))
    )
    request_payload = {
        "origin": "coverage-observation",
        "recovery_bounds": bounds,
        "coverage_observation": {
            "testimonium_ref": testimony_ref,
            "testimonium_id": "testimony-1",
            "observation_ordinal": 0,
            "bounds": bounds,
        },
        "ink_map_ref": ink_map_ref,
        "outside_ink_pixels": 25,
        "minimum_ink_pixels": 24,
    }
    return context, {"inputs": [testimony_ref, ink_map_ref]}, request_payload


def test_real_recovery_recomputes_exact_digest_linked_ink_evidence():
    designator = _load_designator()
    context, request, payload = _coverage_evidence_case()

    designator._verify_coverage_recovery_evidence(
        context, request, payload, {"page_ordinal": 1}, "page-1", 1, 10, 10
    )


def test_real_recovery_reaches_the_crop_without_reading_fixture(monkeypatch):
    """The real recovery route must not touch the fixture-only accessor."""
    designator = _load_designator()
    act_id = "real-act"
    bounds = {"x": 0, "y": 0, "w": 5, "h": 5}

    class _RealContext:
        run = {"ingress": {"mode": "real"}}
        recovery_policy = {"config_sha256": "r" * 64}
        tree = object()

        @property
        def fixture(self):
            raise AssertionError("real recovery read fixture")

        def require_sealed_config(self, *_args):
            return None

        def artifact_ref(self, *_args):
            return {"relative_path": "request.json", "sha256": "a" * 64}

    context = _RealContext()
    request = {
        "artifact_id": "request",
        "payload": {
            "act_key": "real-key",
            "attempt_ordinal": 1,
            "recovery_kind": "fallback-recrop",
            "recovery_bounds": bounds,
            "budget_used": 0,
        },
    }
    page = {"subject_id": "page-1", "payload": {"image_path": "page.png"}}
    monkeypatch.setattr(
        designator,
        "expected_acts",
        lambda _context: [
            {
                "act_id": act_id,
                "act_key": "real-key",
                "outcome": "proposed",
                "page_ordinal": 1,
            }
        ],
    )
    monkeypatch.setattr(designator, "current_recovery_request", lambda *_args, **_kwargs: request)
    monkeypatch.setattr(designator, "sealed_pages", lambda _records: {1: page})
    monkeypatch.setattr(designator, "page_records", lambda _context: [])
    monkeypatch.setattr(designator, "_read_checked_page_bytes", lambda *_args: b"page")
    monkeypatch.setattr(designator, "dimensions", lambda _bytes: (10, 10))
    monkeypatch.setattr(designator.geometry, "validate_bounds", lambda *_args: None)
    monkeypatch.setattr(designator, "_verify_coverage_recovery_evidence", lambda *_args: None)
    monkeypatch.setattr(designator, "_regions_of", lambda *_args: [])
    monkeypatch.setattr(designator, "_coverage_on_page", lambda *_args: [])
    monkeypatch.setattr(designator, "_uncovered_area", lambda *_args: 1)
    monkeypatch.setattr(designator, "_next_region_ordinal", lambda *_args: 1)
    cut = []
    monkeypatch.setattr(designator, "cut_minted_region", lambda *args: cut.append(args))

    designator.recovery_pass(context, act_id, "request")
    assert cut and cut[0][1:3] == (act_id, "real-key")


@pytest.mark.parametrize(
    "tamper", ["ink-count", "observation-bounds", "ink-map-ref", "blank-map", "prior-cover"]
)
def test_real_recovery_refuses_tampered_coverage_evidence(tamper):
    designator = _load_designator()
    context, request, payload = _coverage_evidence_case()
    if tamper == "ink-count":
        payload["outside_ink_pixels"] = 24
    elif tamper == "observation-bounds":
        payload["coverage_observation"]["bounds"] = {"x": 0, "y": 0, "w": 4, "h": 5}
    else:
        if tamper == "ink-map-ref":
            payload["ink_map_ref"] = {"relative_path": "forged.json", "sha256": "3" * 64}
        elif tamper == "blank-map":
            context.tree.ink_map["payload"]["edge_findings"]["rows"] = [[] for _ in range(10)]
        else:
            context.tree.regions.append(
                {
                    "artifact_id": "prior-region",
                    "payload": {
                        "transform": {
                            "source_page_ordinal": 1,
                            "source_page_id": "page-1",
                            "bounds": payload["recovery_bounds"],
                        }
                    },
                }
            )

    with pytest.raises(ContractError):
        designator._verify_coverage_recovery_evidence(
            context, request, payload, {"page_ordinal": 1}, "page-1", 1, 10, 10
        )


def _designator_context(designator, root: Path):
    """A real Designator context over a real run, opened the way its CLI opens one."""
    from common.contracts.stages import DESIGNATOR
    from common.stage import open_context, stage_parser

    args = stage_parser("recovery bounds acceptance").parse_args(
        ["--run-root", str(root), "--run-id", "r", "--scenario", "review"]
    )
    return open_context(args, DESIGNATOR)


def test_a_recovery_at_existing_bounds_refuses_without_cutting_a_duplicate(tmp_path):
    """A recrop must add coverage rather than manufacture another reading pass.

    `region_id` binds the act and transform, so a recovery at an already-cut
    proposal rectangle would carry the same pixels and identity. It cannot recover
    coverage, and allowing it makes the Perlector receive duplicate evidence.

    Driven in process rather than by CLI: `--fixture-root` cannot carry a modified
    fixture through the door, because the door refuses any root but the declared
    synthetic one (a caller-owned folder is real input), and the run authority binds
    the fixture into its config digest. Both of those are correct and neither is
    worth weakening for a test, so the recovery bounds are moved on the loaded
    fixture object instead, one layer inside the CLI.
    """
    root = tmp_path / "runs"
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/1_ink_map/run.py",
        "pipeline/2_designator/run.py",
        "pipeline/3_attestatores/run.py",
        "pipeline/4_perlector/run.py",
        "pipeline/5_recensor/run.py",
    ):
        result = _run(program, root)
        assert result.returncode in (EXIT_COMPLETE, EXIT_HELD), f"{program}: {result.stderr}"

    from common.contracts.stages import DESIGNATOR, RECENSOR
    from common.runtree.store import RunTree

    designator = _load_designator()
    tree = RunTree(root, "r")
    review = next(
        record
        for record in (
            tree.read_artifact(RECENSOR, "review", entry["artifact_id"])
            for entry in tree.build_manifest(RECENSOR)["artifacts"]
            if entry["kind"] == "review"
        )
        if record["payload"]["act_key"] == "a1"
    )
    act_id = review["subject_id"]
    request_id = review["payload"]["recovery_request_ref"]["relative_path"].rsplit("/", 1)[-1][:-5]

    # The already-cut proposal region's *final* (padded) bounds, not the
    # fixture's raw declared act rectangle: `cut_region` expands a proposal
    # crop by the configured capture padding before cutting it, so the bounds
    # that would actually collide with an existing region are the padded
    # ones, not the pre-padding rectangle identity is bound to.
    existing_proposal = next(
        record
        for record in (
            tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
            for entry in tree.build_manifest(DESIGNATOR)["artifacts"]
            if entry["kind"] == "region"
        )
        if record["subject_id"] == act_id and record["payload"]["origin"] == "proposal"
    )
    existing_bounds = existing_proposal["payload"]["transform"]["bounds"]

    context = _designator_context(designator, root)
    for row in context.fixture["recovery"]:
        if row["act_key"] == "a1":
            row.update(existing_bounds)

    with pytest.raises(ContractError, match="already has a region cut"):
        designator.recovery_pass(context, act_id, request_id)
    context.finish()

    recovery_regions = [
        record
        for record in (
            tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
            for entry in tree.build_manifest(DESIGNATOR)["artifacts"]
            if entry["kind"] == "region"
        )
        if record["subject_id"] == act_id and record["payload"]["origin"] == "recovery"
    ]
    assert recovery_regions == []


def test_an_out_of_page_recovery_rectangle_refuses_with_a_contract_error(tmp_path):
    """A recovery crop skips `apply_padding` (it names its own exact final
    rectangle) and so has no other bounds check before `crop_png` -- which
    raises a bare `ValueError` `run_stage` does not turn into `EXIT_FATAL`.
    The recovery path needs its own explicit check to fail the same way every
    other refusal in this pipeline does."""
    root = tmp_path / "runs"
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/1_ink_map/run.py",
        "pipeline/2_designator/run.py",
        "pipeline/3_attestatores/run.py",
        "pipeline/4_perlector/run.py",
        "pipeline/5_recensor/run.py",
    ):
        result = _run(program, root)
        assert result.returncode in (EXIT_COMPLETE, EXIT_HELD), f"{program}: {result.stderr}"

    from common.contracts.stages import RECENSOR
    from common.runtree.store import RunTree

    designator = _load_designator()
    tree = RunTree(root, "r")
    review = next(
        record
        for record in (
            tree.read_artifact(RECENSOR, "review", entry["artifact_id"])
            for entry in tree.build_manifest(RECENSOR)["artifacts"]
            if entry["kind"] == "review"
        )
        if record["payload"]["act_key"] == "a1"
    )
    act_id = review["subject_id"]
    request_id = review["payload"]["recovery_request_ref"]["relative_path"].rsplit("/", 1)[-1][:-5]

    context = _designator_context(designator, root)
    for row in context.fixture["recovery"]:
        if row["act_key"] == "a1":
            row.update({"x": 0, "y": 0, "w": 10**6, "h": 10**6})

    with pytest.raises(ContractError, match="recovery bounds"):
        designator.recovery_pass(context, act_id, request_id)
    context.finish()

    recovery_regions = [
        record
        for record in (
            tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
            for entry in tree.build_manifest(DESIGNATOR)["artifacts"]
            if entry["kind"] == "region"
        )
        if record["subject_id"] == act_id and record["payload"]["origin"] == "recovery"
    ]
    assert recovery_regions == [], "a refused out-of-page recovery must cut no region"


def test_recovery_resolves_the_act_through_the_verified_denominator(tmp_path):
    """A mutated fixture is refused while re-verifying the seal, before any cut."""
    root = tmp_path / "runs"
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/1_ink_map/run.py",
        "pipeline/2_designator/run.py",
        "pipeline/3_attestatores/run.py",
        "pipeline/4_perlector/run.py",
        "pipeline/5_recensor/run.py",
    ):
        result = _run(program, root)
        assert result.returncode in (EXIT_COMPLETE, EXIT_HELD), f"{program}: {result.stderr}"

    from common.contracts.stages import RECENSOR
    from common.runtree.store import RunTree

    designator = _load_designator()
    tree = RunTree(root, "r")
    review = next(
        record
        for record in (
            tree.read_artifact(RECENSOR, "review", entry["artifact_id"])
            for entry in tree.build_manifest(RECENSOR)["artifacts"]
            if entry["kind"] == "review"
        )
        if record["payload"]["act_key"] == "a1"
    )
    request_id = review["payload"]["recovery_request_ref"]["relative_path"].rsplit("/", 1)[-1][:-5]
    context = _designator_context(designator, root)
    context.fixture["act"] = [row for row in context.fixture["act"] if row["key"] != "a1"]

    with pytest.raises(ContractError, match="extends the denominator beyond the fixture"):
        designator.recovery_pass(context, review["subject_id"], request_id)
    context.finish()


def test_multiple_declared_recovery_bounds_refuse_instead_of_selecting_the_first(tmp_path):
    """A recovery request may not pick one of several fixture rectangles by order."""
    root = tmp_path / "runs"
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/1_ink_map/run.py",
        "pipeline/2_designator/run.py",
        "pipeline/3_attestatores/run.py",
        "pipeline/4_perlector/run.py",
        "pipeline/5_recensor/run.py",
    ):
        result = _run(program, root)
        assert result.returncode in (EXIT_COMPLETE, EXIT_HELD), f"{program}: {result.stderr}"

    from common.contracts.stages import RECENSOR
    from common.runtree.store import RunTree

    designator = _load_designator()
    tree = RunTree(root, "r")
    review = next(
        record
        for record in (
            tree.read_artifact(RECENSOR, "review", entry["artifact_id"])
            for entry in tree.build_manifest(RECENSOR)["artifacts"]
            if entry["kind"] == "review"
        )
        if record["payload"]["act_key"] == "a1"
    )
    request_id = review["payload"]["recovery_request_ref"]["relative_path"].rsplit("/", 1)[-1][:-5]
    context = _designator_context(designator, root)
    original = next(row for row in context.fixture["recovery"] if row["act_key"] == "a1")
    context.fixture["recovery"].append(dict(original))

    with pytest.raises(ContractError, match="declares 2 recovery regions"):
        designator.recovery_pass(context, review["subject_id"], request_id)


def test_a_recrop_strictly_inside_the_existing_crop_refuses_by_name(tmp_path):
    """A recovery must recover *coverage*, which is a fact about pixels.

    The rectangle driven here is the one `proof/skeleton_fixture.toml` itself
    declared for act a1 until the audit finding this test carries: 16,16,168,88,
    against a padded proposal capture rect of 12,15,188,99. `[16,184) x [16,104)`
    is a strict subset of `[12,200) x [15,114)`, so the recrop uncovers not one
    pixel -- and yet it passed the transform-identity check above, spent the
    act's whole `fallback_recrop` budget, and left the export carrying a
    `witness_covered: false` caveat ("ink a recovery uncovers was never shown to
    them") over nothing at all. principle 7: "Recovery exists for completeness
    and coverage."

    The refusal has to be by name rather than by the duplicate-transform message,
    because the two are different defects: that one is a re-read of identical
    pixels, this one is a *smaller* crop wearing recovery's name.
    """
    root = tmp_path / "runs"
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/1_ink_map/run.py",
        "pipeline/2_designator/run.py",
        "pipeline/3_attestatores/run.py",
        "pipeline/4_perlector/run.py",
        "pipeline/5_recensor/run.py",
    ):
        result = _run(program, root)
        assert result.returncode in (EXIT_COMPLETE, EXIT_HELD), f"{program}: {result.stderr}"

    from common.contracts.stages import DESIGNATOR, RECENSOR
    from common.runtree.store import RunTree

    designator = _load_designator()
    tree = RunTree(root, "r")
    review = next(
        record
        for record in (
            tree.read_artifact(RECENSOR, "review", entry["artifact_id"])
            for entry in tree.build_manifest(RECENSOR)["artifacts"]
            if entry["kind"] == "review"
        )
        if record["payload"]["act_key"] == "a1"
    )
    act_id = review["subject_id"]
    request_id = review["payload"]["recovery_request_ref"]["relative_path"].rsplit("/", 1)[-1][:-5]

    existing_proposal = next(
        record
        for record in (
            tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
            for entry in tree.build_manifest(DESIGNATOR)["artifacts"]
            if entry["kind"] == "region"
        )
        if record["subject_id"] == act_id and record["payload"]["origin"] == "proposal"
    )
    # Asserted, not assumed: the whole point of the case is that this rectangle
    # sits strictly inside what was already cut, so the test would prove nothing
    # if padding ever moved the proposal crop off it.
    inside = {"x": 16, "y": 16, "w": 168, "h": 88}
    cut = existing_proposal["payload"]["transform"]["bounds"]
    assert cut["x"] < inside["x"] and cut["y"] < inside["y"]
    assert inside["x"] + inside["w"] < cut["x"] + cut["w"]
    assert inside["y"] + inside["h"] < cut["y"] + cut["h"]

    context = _designator_context(designator, root)
    for row in context.fixture["recovery"]:
        if row["act_key"] == "a1":
            row.update(inside)

    with pytest.raises(ContractError, match="recovers no page pixel"):
        designator.recovery_pass(context, act_id, request_id)
    context.finish()

    recovery_regions = [
        record
        for record in (
            tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
            for entry in tree.build_manifest(DESIGNATOR)["artifacts"]
            if entry["kind"] == "region"
        )
        if record["subject_id"] == act_id and record["payload"]["origin"] == "recovery"
    ]
    assert recovery_regions == [], "a refused recrop must not still cut a region"


# --- The pixel arithmetic the refusal rests on ---------------------------------


def test_an_empty_cover_set_leaves_the_whole_rectangle_uncovered():
    designator = _load_designator()
    assert designator._uncovered_area({"x": 3, "y": 4, "w": 10, "h": 20}, []) == 200


def test_uncovered_area_counts_exactly_the_pixels_no_cover_holds():
    """Against a brute-force pixel set, so the compression is checked rather
    than restated: an off-by-one in the grid would agree with itself."""
    designator = _load_designator()
    target = {"x": 0, "y": 0, "w": 9, "h": 7}
    covers = [
        {"x": -3, "y": 2, "w": 6, "h": 3},
        {"x": 4, "y": 0, "w": 3, "h": 5},
        {"x": 7, "y": 5, "w": 40, "h": 40},
    ]
    held = {
        (x, y)
        for cover in covers
        for x in range(cover["x"], cover["x"] + cover["w"])
        for y in range(cover["y"], cover["y"] + cover["h"])
    }
    expected = sum(
        1
        for x in range(target["x"], target["x"] + target["w"])
        for y in range(target["y"], target["y"] + target["h"])
        if (x, y) not in held
    )
    assert expected == 35
    assert designator._uncovered_area(target, covers) == expected


def test_a_single_pixel_outside_every_cover_is_enough_coverage_to_recover():
    """The guard's threshold is one pixel, not a fraction: goal 2 puts a missed
    act above a poorly read one, so a recrop that widens by a hair still widens.
    The covers here leave exactly ONE pixel, so the name is the measurement."""
    designator = _load_designator()
    target = {"x": 0, "y": 0, "w": 10, "h": 10}
    all_but_corner = [
        {"x": 0, "y": 0, "w": 10, "h": 9},
        {"x": 0, "y": 9, "w": 9, "h": 1},
    ]
    assert designator._uncovered_area(target, all_but_corner) == 1
    assert designator._uncovered_area(target, [{"x": 0, "y": 0, "w": 10, "h": 9}]) == 10
    assert designator._uncovered_area(target, [{"x": 0, "y": 0, "w": 10, "h": 10}]) == 0


def test_two_covers_that_only_jointly_contain_the_rectangle_still_leave_nothing():
    """The reason this is set arithmetic and not a pairwise containment test.

    Neither cover contains the target; their union does exactly. A "is it inside
    any single existing region" check would call this recrop new coverage and
    let it spend the budget on pixels the act already has.
    """
    designator = _load_designator()
    target = {"x": 0, "y": 0, "w": 10, "h": 10}
    left = {"x": 0, "y": 0, "w": 5, "h": 10}
    right = {"x": 5, "y": 0, "w": 5, "h": 10}
    assert designator._uncovered_area(target, [left]) == 50
    assert designator._uncovered_area(target, [right]) == 50
    assert designator._uncovered_area(target, [left, right]) == 0


def test_overlapping_covers_are_not_double_counted():
    """Two covers overlapping each other must not subtract the shared pixels
    twice and report a rectangle as more covered than it is."""
    designator = _load_designator()
    target = {"x": 0, "y": 0, "w": 10, "h": 10}
    covers = [{"x": 0, "y": 0, "w": 6, "h": 10}, {"x": 4, "y": 0, "w": 5, "h": 10}]
    assert designator._uncovered_area(target, covers) == 10


def _region_record(page_ordinal: int, page_id: str, bounds: dict) -> dict:
    return {
        "payload": {
            "transform": {
                "source_page_ordinal": page_ordinal,
                "source_page_id": page_id,
                "bounds": bounds,
            }
        }
    }


def test_coverage_is_scoped_to_the_page_being_recropped():
    """A continuation region shares the act's identity and none of its geometry.

    Counting one page's rectangle as coverage of another page's would refuse a
    legitimate recrop -- the dangerous direction, since a refused recovery is a
    recovery budget spent on nothing.
    """
    designator = _load_designator()
    near = _region_record(1, "page_one", {"x": 0, "y": 0, "w": 10, "h": 10})
    far = _region_record(2, "page_two", {"x": 0, "y": 0, "w": 10, "h": 10})
    assert designator._coverage_on_page([near, far], 1, "page_one") == [
        {"x": 0, "y": 0, "w": 10, "h": 10}
    ]
    assert designator._coverage_on_page([far], 1, "page_one") == []


def test_coverage_requires_the_page_identity_and_not_only_its_ordinal():
    """An ordinal is a position in one run's corpus; two different pages carry
    the same one. The sealed page identity is what says these are the same
    pixels."""
    designator = _load_designator()
    same_ordinal_other_page = _region_record(1, "page_elsewhere", {"x": 0, "y": 0, "w": 4, "h": 4})
    assert designator._coverage_on_page([same_ordinal_other_page], 1, "page_one") == []


def _review_run_to_recensor(root: Path) -> None:
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/1_ink_map/run.py",
        "pipeline/2_designator/run.py",
        "pipeline/3_attestatores/run.py",
        "pipeline/4_perlector/run.py",
        "pipeline/5_recensor/run.py",
    ):
        result = _run(program, root)
        assert result.returncode in (EXIT_COMPLETE, EXIT_HELD), f"{program}: {result.stderr}"


def _recovery_target(root: Path) -> tuple[str, str]:
    from common.contracts.stages import RECENSOR
    from common.runtree.store import RunTree

    tree = RunTree(root, "r")
    review = next(
        record
        for record in (
            tree.read_artifact(RECENSOR, "review", entry["artifact_id"])
            for entry in tree.build_manifest(RECENSOR)["artifacts"]
            if entry["kind"] == "review"
        )
        if record["payload"]["act_key"] == "a1"
    )
    request = review["payload"]["recovery_request_ref"]["relative_path"]
    return review["subject_id"], request.rsplit("/", 1)[-1][:-5]


def _recovery_regions_of(root, act_id):
    """Every recovery-origin region currently published for one act."""
    from common.contracts.stages import DESIGNATOR
    from common.runtree.store import RunTree

    tree = RunTree(root, "r")
    return [
        record
        for record in (
            tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
            for entry in tree.build_manifest(DESIGNATOR)["artifacts"]
            if entry["kind"] == "region"
        )
        if record["subject_id"] == act_id and record["payload"]["origin"] == "recovery"
    ]


@pytest.mark.parametrize(
    "bounds",
    (
        {"x": 20, "y": 20, "w": 0, "h": 80},
        {"x": 20, "y": 20, "w": -160, "h": 80},
        {"x": 20, "y": 20, "w": 160, "h": 0},
    ),
    ids=("zero-width", "negative-width", "zero-height"),
)
def test_a_degenerate_recovery_rectangle_is_refused_as_a_rectangle(tmp_path, bounds):
    """Ordering, not decoration: the coverage refusal must not answer this one.

    `_uncovered_area` measures a rectangle; handed a degenerate one it returns a
    meaningless number -- zero for an empty rectangle, and for a negative-width
    one an area that is not zero at all, which would sail past a guard that only
    tests for zero. Both are wrong answers to the wrong question. The bounds
    check runs first so the refusal names what is actually wrong, and this
    asserts that ordering rather than trusting it.
    """
    root = tmp_path / "runs"
    _review_run_to_recensor(root)
    designator = _load_designator()
    act_id, request_id = _recovery_target(root)

    context = _designator_context(designator, root)
    for row in context.fixture["recovery"]:
        if row["act_key"] == "a1":
            row.update(bounds)

    with pytest.raises(ContractError, match="recovery bounds"):
        designator.recovery_pass(context, act_id, request_id)
    context.finish()
    assert _recovery_regions_of(root, act_id) == [], (
        "a refused degenerate recrop must not still cut a region"
    )


def test_a_non_integer_recovery_coordinate_refuses_as_a_contract_error(tmp_path):
    """A float coordinate must not escape as a bare `TypeError`.

    `run_stage` turns a `ContractError` into `EXIT_FATAL`; anything else exits 1
    with a traceback, and the orchestrator then halts the corpus on a code that
    explains nothing -- the same defect class the recovery path already closed
    once for `crop_png`'s bare `ValueError`. A float reaches `region_id` ->
    `digest_of` before any bounds check unless the bounds check runs first,
    and canonicalization refuses floats.
    """
    root = tmp_path / "runs"
    _review_run_to_recensor(root)
    designator = _load_designator()
    act_id, request_id = _recovery_target(root)

    context = _designator_context(designator, root)
    for row in context.fixture["recovery"]:
        if row["act_key"] == "a1":
            row.update({"x": 20.5, "y": 20, "w": 160, "h": 80})

    with pytest.raises(ContractError, match="non-integer coordinate"):
        designator.recovery_pass(context, act_id, request_id)
    context.finish()
    assert _recovery_regions_of(root, act_id) == [], (
        "a refused non-integer recrop must not still cut a region"
    )
