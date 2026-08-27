"""Keep act, ink, and witness denominators independent on a real tree.

Three denominators are deliberately separate and never mixed: **acts** is the
proposal seal's expected set, **ink** is Unit 9's per-page map plus the
residual-ink measure against every region currently cut, and **witnesses** is
the configured chairs and their floor. Witness geometry appears only as
per-chair facts; it is never subtracted from ink and never unioned into it.

A separation like that is only worth the words if moving one side can be
observed not to move the other. This file drives the stage's own denominator
functions over a real `coverage-recovery` run tree and asserts the property in
both directions, each with the control that stops it passing vacuously:

* perturb a chair's own boxes -- the witness facts move, and every ink number,
  every cut rectangle and the whole act set are unchanged;
* perturb a sealed cut rectangle -- the ink numbers move, while Unit 9's
  pre-proposal map (measured before any proposal existed) and the act set are
  unchanged.

Perturbations are applied at the artifact read boundary so the sealed run tree
stays unchanged.
"""

import copy
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from common.contracts.stages import ATTESTATORES, DESIGNATOR, RECENSOR
from common.native_witness import partition_disagreement

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "pipeline/orchestrator/run.py"
FIXTURE = "synthetic-two-page-v0"
# The one shipped scenario carrying a real unclaimed witness observation, which
# is what makes the witness side of the perturbation observable at all.
SCENARIO = "coverage-recovery"


def _load_recensor():
    spec = importlib.util.spec_from_file_location(
        "recensor_u14b_denominators", ROOT / "pipeline/5_recensor/run.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _context(recensor, root: Path, run_id: str):
    args = recensor.stage_parser("denominator falsification").parse_args(
        [
            "--run-root",
            str(root),
            "--run-id",
            run_id,
            "--scenario",
            SCENARIO,
            "--fixture-root",
            str(ROOT / "proof"),
        ]
    )
    return recensor.open_context(args, RECENSOR)


def _denominators(recensor, context) -> dict[str, str]:
    """Every number each denominator states, frozen as text.

    `repr` rather than a canonical digest: the residual-ink finding carries a
    float fraction, which `canonical_bytes` refuses by design, and `repr` pins
    both the value and the key order the producer emitted it in.
    """
    return {
        "acts": repr(recensor.expected_acts(context)),
        "ink_map": repr(recensor.ink_map_by_page(context)),
        "cut_mask": repr(recensor.regions_by_source_page(context)),
        "residual_ink": repr(recensor.page_coverage_findings(context)),
        "witness_observations": repr(
            {
                ordinal: finding.get("unclaimed_observations", [])
                for ordinal, finding in recensor.testimony_content_findings(context).items()
            }
        ),
    }


@pytest.fixture(scope="module")
def run_root(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("denominators") / "runs"
    result = subprocess.run(
        [
            sys.executable,
            str(ORCHESTRATOR),
            "--fixture",
            FIXTURE,
            "--scenario",
            SCENARIO,
            "--run-id",
            "r",
            "--run-root",
            str(root),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return root


def test_moving_a_chairs_boxes_moves_no_ink_number_and_no_act(run_root, monkeypatch):
    """Witness geometry is a per-chair fact, never an input to ink or acts.

    Measured over a deliberately shortened cut, because on the shipped fixture
    every page's ink lies exactly inside its crops and `outside_ink_pixels` is
    0 on both pages. An invariance asserted over a measure that reads zero
    whatever happens is not an invariance: a build that fed witness boxes
    straight into the coverage mask would satisfy it. Shrinking one page-1 cut
    by `SHRINK_ROWS` first gives the ink measure something to say, and the
    witness box is moved onto exactly the rows that shrink uncovered, so a leak
    between the two denominators would change the number this asserts is equal.
    """
    recensor = _load_recensor()
    context = _context(recensor, run_root, "r")
    target, page = _region_to_shrink(recensor, context)

    shrinking = _shrinking_reader(context, target)
    monkeypatch.setattr(context.tree, "read_artifact", shrinking)
    before = _denominators(recensor, context)

    # The control that makes the equalities below load-bearing: with that cut
    # shortened, the page really does carry ink outside every region, and the
    # witness box really does sit over some of it.
    uncovered = recensor.page_coverage_findings(context)[page]["outside_ink_pixels"]
    assert uncovered > 0, "the shortened cut uncovered no ink, so the ink measure says nothing"

    moving, moved = _witness_moving_reader(shrinking)
    monkeypatch.setattr(context.tree, "read_artifact", moving)
    after = _denominators(recensor, context)

    assert moved, "the perturbation reached no retained unclaimed observation"
    assert after["witness_observations"] != before["witness_observations"]

    assert after["acts"] == before["acts"]
    assert after["ink_map"] == before["ink_map"]
    assert after["cut_mask"] == before["cut_mask"]
    assert after["residual_ink"] == before["residual_ink"]


SHRINK_ROWS = 40
WITNESS_SHIFT_X = 37


def _region_to_shrink(recensor, context) -> tuple[str, int]:
    """The page-1 region whose bottom rows carry the ink this file uncovers.

    Chosen from the manifest, not from read order: targeting "the first region
    this walk happens to read" would make the perturbation depend on which
    denominator is computed first, which is the kind of hidden ordering this
    file exists to rule out. The tallest region on page 1 is the one whose
    bottom `SHRINK_ROWS` rows lie under the witness box this file also moves,
    which is what makes a leak between the two denominators observable rather
    than merely absent.
    """
    candidates = []
    for entry in sorted(
        context.tree.build_manifest(DESIGNATOR)["artifacts"],
        key=lambda entry: entry["artifact_id"],
    ):
        if entry["kind"] != "region":
            continue
        record = context.tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
        transform = record["payload"]["transform"]
        if transform["source_page_ordinal"] != 1:
            continue
        candidates.append((transform["bounds"]["h"], entry["artifact_id"]))
    assert candidates, "the run tree has no page-1 Designator region to perturb"
    height, artifact = max(candidates)
    assert height > SHRINK_ROWS, "the fixture region is too small to shrink meaningfully"
    return artifact, 1


def _shrinking_reader(context, target: str):
    original = context.tree.read_artifact
    # Each page Testimonium retains the sealed proposal boxes it partitioned
    # against, and the Recensor refuses a partition that contradicts the
    # current proposals. The perturbation therefore models a producer chain
    # that honestly witnessed the shrunk cut: the same rectangle shrinks in
    # the region record and in every retained partition fact that names it.
    sealed = original(DESIGNATOR, "region", target)["payload"]["transform"]["bounds"]

    def read(stage, kind, artifact_id):
        record = original(stage, kind, artifact_id)
        if stage == DESIGNATOR and kind == "region" and artifact_id == target:
            record = copy.deepcopy(record)
            record["payload"]["transform"]["bounds"]["h"] -= SHRINK_ROWS
            return record
        if stage == ATTESTATORES and kind == "page-testimonium":
            record = copy.deepcopy(record)
            payload = record["payload"]
            disagreement = payload.get("partition_disagreement")
            if not disagreement or sealed not in disagreement["proposal_boxes"]:
                return record
            shrunk_boxes = copy.deepcopy(disagreement["proposal_boxes"])
            shrunk_boxes[shrunk_boxes.index(sealed)]["h"] -= SHRINK_ROWS
            # Rebuild through the real derivation rather than hand-editing the
            # derived fields: boundary deltas and pairings are cross-checked
            # against the boxes, so only the producer's own arithmetic yields
            # a coherent witnessed record of the shrunk cut.
            payload["partition_disagreement"] = partition_disagreement(
                record,
                [
                    {
                        "payload": {
                            "origin": "proposal",
                            "transform": {
                                "source_page_id": payload["presented"]["source_page_id"],
                                "bounds": box,
                            },
                        }
                    }
                    for box in shrunk_boxes
                ],
                page_edge_overshoots=disagreement.get("page_edge_overshoots"),
            )
            return record
        return record

    return read


def _witness_moving_reader(inner):
    moved: list[str] = []

    def read(stage, kind, artifact_id):
        record = inner(stage, kind, artifact_id)
        if stage != ATTESTATORES or kind != "page-testimonium":
            return record
        record = copy.deepcopy(record)
        payload = record["payload"]
        disagreement = payload.get("partition_disagreement")
        for observation in payload.get("observed") or []:
            observation["bounds"]["x"] += WITNESS_SHIFT_X
        if not disagreement:
            return record
        # Rebuilt through the real derivation for the same reason as the
        # shrinking reader above: every derived partition fact is cross-checked
        # against the observed geometry it claims to partition.
        rebuilt = partition_disagreement(
            record,
            [
                {
                    "payload": {
                        "origin": "proposal",
                        "transform": {
                            "source_page_id": payload["presented"]["source_page_id"],
                            "bounds": box,
                        },
                    }
                }
                for box in disagreement["proposal_boxes"]
            ],
            page_edge_overshoots=disagreement.get("page_edge_overshoots"),
        )
        if rebuilt.get("unclaimed_observations") != disagreement.get("unclaimed_observations"):
            moved.append(artifact_id)
        payload["partition_disagreement"] = rebuilt
        return record

    return read, moved


def test_moving_a_sealed_cut_moves_the_ink_numbers_and_nothing_upstream(run_root, monkeypatch):
    """The control in the other direction: the ink measure is not inert.

    A test that only ever asserts "the ink did not move" passes just as well
    against an ink measure wired to nothing. Shrinking one real cut rectangle
    has to move the residual-ink finding for its page -- and must not move Unit
    9's map, which was measured before any proposal existed and cannot depend on
    what was later cut.
    """
    recensor = _load_recensor()
    baseline_context = _context(recensor, run_root, "r")
    before = _denominators(recensor, baseline_context)

    context = _context(recensor, run_root, "r")
    target, page = _region_to_shrink(recensor, context)
    monkeypatch.setattr(context.tree, "read_artifact", _shrinking_reader(context, target))
    after = _denominators(recensor, context)

    assert after["cut_mask"] != before["cut_mask"]

    # The direction is named, not merely "different": uncut ink is ink outside
    # coverage, so the page carrying the shrunken crop must now measure more of
    # it, over the same total the page always had.
    baseline_ink = recensor.page_coverage_findings(baseline_context)
    shrunken_ink = recensor.page_coverage_findings(context)
    assert shrunken_ink[page]["outside_ink_pixels"] > baseline_ink[page]["outside_ink_pixels"]
    assert shrunken_ink[page]["total_ink_pixels"] == baseline_ink[page]["total_ink_pixels"]

    assert after["ink_map"] == before["ink_map"]
    assert after["acts"] == before["acts"]
