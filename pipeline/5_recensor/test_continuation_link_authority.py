"""The Recensor's own continuation fact, derived from evidence, not the seal.

ARCHITECTURE and spec 09 agree: "the Designator proposes continuations; the
Recensor's link is the authoritative relation." Before this build, the only
continuation fact anywhere in the pipeline was the Designator's own
`has_continuation` seal flag, and the Recensor merely checked a shortfall
against it -- the Designator's proposal was being treated as the settled
answer rather than as a claim the Recensor reconciles against its own
evidence. `recensor_continuation_link` and `reconcile_continuation` are that
fix, and this file drives both directly (module-loaded, like `test_
designator_terminal_outcomes.py` drives `_refuse_an_unhandled_designator_
terminal`) plus once end to end over the real two-act fixture.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from common.contracts.errors import FatalAccounting
from common.contracts.stages import DESIGNATOR, RECENSOR
from common.runtree.store import RunTree

ROOT = Path(__file__).resolve().parents[2]


def _load_recensor():
    path = ROOT / "pipeline/5_recensor/run.py"
    spec = importlib.util.spec_from_file_location("recensor_continuation_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    spec.loader.exec_module(module)
    return module


recensor = _load_recensor()


def _region(*, act_id: str, origin: str, ordinal: int, page_ordinal: int) -> dict:
    """A minimal Designator-region-shaped record: enough for `recensor_continuation_link`."""
    return {
        "payload": {
            "origin": origin,
            "region_id": f"rgn_{act_id}_{ordinal}",
            "image_path": f"designator/blobs/{act_id}_{ordinal}.png",
            "image_sha256": "a" * 64,
            "transform": {
                "operation": "crop",
                "source_page_ordinal": page_ordinal,
                "source_page_id": f"pg_{page_ordinal}",
                "bounds": {"x": 0, "y": 0, "w": 1, "h": 1},
            },
        }
    }


# --- recensor_continuation_link: pure evidence, never the seal's word ----------


def test_two_proposal_regions_on_two_pages_is_a_continuation():
    regions = [
        _region(act_id="a", origin="proposal", ordinal=1, page_ordinal=1),
        _region(act_id="a", origin="proposal", ordinal=2, page_ordinal=2),
    ]
    link = recensor.recensor_continuation_link(regions, "a")
    assert link == {
        "is_continuation": True,
        "page_ordinals": [1, 2],
        "region_ids": ["rgn_a_1", "rgn_a_2"],
    }


def test_two_proposal_regions_on_the_same_page_is_not_a_continuation():
    """A bare region COUNT is the wrong test: two crops on one page are not a
    continuation, whatever the Designator's seal happens to claim."""
    regions = [
        _region(act_id="a", origin="proposal", ordinal=1, page_ordinal=1),
        _region(act_id="a", origin="proposal", ordinal=2, page_ordinal=1),
    ]
    link = recensor.recensor_continuation_link(regions, "a")
    assert link["is_continuation"] is False
    assert link["page_ordinals"] == [1]


def test_a_single_proposal_region_is_not_a_continuation():
    regions = [_region(act_id="a", origin="proposal", ordinal=1, page_ordinal=1)]
    link = recensor.recensor_continuation_link(regions, "a")
    assert link["is_continuation"] is False
    assert link["page_ordinals"] == [1]


def test_no_regions_at_all_is_not_a_continuation():
    assert recensor.recensor_continuation_link([], "a") == {
        "is_continuation": False,
        "page_ordinals": [],
        "region_ids": [],
    }


def test_a_recovery_region_on_a_second_page_does_not_count_as_a_continuation():
    """Only ORIGINAL proposal regions establish the continuation fact. A later
    recovery recrop on a different page is coverage recovery, not evidence that
    the act was always meant to span two pages."""
    regions = [
        _region(act_id="a", origin="proposal", ordinal=1, page_ordinal=1),
        _region(act_id="a", origin="recovery", ordinal=2, page_ordinal=2),
    ]
    link = recensor.recensor_continuation_link(regions, "a")
    assert link["is_continuation"] is False
    assert link["page_ordinals"] == [1]


# --- reconcile_continuation: the seal's claim checked against the link ---------


def test_a_confirmed_continuation_reconciles_without_a_shortfall():
    link = {"is_continuation": True, "page_ordinals": [1, 2], "region_ids": ["r1", "r2"]}
    assert recensor.reconcile_continuation({"has_continuation": True}, link, "a") is False


def test_no_claimed_continuation_and_none_found_reconciles():
    link = {"is_continuation": False, "page_ordinals": [1], "region_ids": ["r1"]}
    assert recensor.reconcile_continuation({"has_continuation": False}, link, "a") is False


def test_a_claimed_continuation_the_evidence_does_not_confirm_is_a_shortfall():
    link = {"is_continuation": False, "page_ordinals": [1], "region_ids": ["r1"]}
    assert recensor.reconcile_continuation({"has_continuation": True}, link, "a") is True


def test_evidence_of_a_continuation_the_seal_denies_is_fatal():
    """The direction that matters: the seal may not silently under-claim against
    hard evidence the Recensor's own reconciliation already confirms. This is
    the Recensor's link asserting authority OVER the seal, not merely checking
    the seal's arithmetic."""
    link = {"is_continuation": True, "page_ordinals": [3, 4], "region_ids": ["r1", "r2"]}
    with pytest.raises(FatalAccounting, match="authoritative continuation fact"):
        recensor.reconcile_continuation({"has_continuation": False}, link, "act_1")


# --- End to end: the real fixture's continuation fact travels in the review ----


def _invoke(root, run_id, scenario, program):
    import subprocess
    import sys as _sys

    result = subprocess.run(
        [
            _sys.executable,
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
    assert result.returncode in (0, 3), f"{program}: {result.stderr}"


def _run_through_recensor(root: Path, run_id: str, scenario: str) -> None:
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/2_designator/run.py",
        "pipeline/3_attestatores/run.py",
        "pipeline/4_perlector/run.py",
        "pipeline/5_recensor/run.py",
    ):
        _invoke(root, run_id, scenario, program)


def _reviews_by_key(tree: RunTree) -> dict[str, dict]:
    reviews = [
        tree.read_artifact(RECENSOR, "review", entry["artifact_id"])
        for entry in tree.build_manifest(RECENSOR)["artifacts"]
        if entry["kind"] == "review"
    ]
    latest: dict[str, dict] = {}
    for review in reviews:
        key = review["payload"]["act_key"]
        if (
            key not in latest
            or review["payload"]["attempt_ordinal"] > latest[key]["payload"]["attempt_ordinal"]
        ):
            latest[key] = review
    return latest


def test_the_non_continuation_act_carries_a_settled_false_continuation_fact(tmp_path):
    root = tmp_path / "runs"
    _run_through_recensor(root, "r", "happy")
    tree = RunTree(root, "r")
    reviews = _reviews_by_key(tree)
    assert reviews["a1"]["payload"]["continuation"]["is_continuation"] is False
    assert reviews["a1"]["payload"]["continuation"]["page_ordinals"] == [1]


def test_the_continuation_act_carries_the_recensors_own_confirmed_link(tmp_path):
    root = tmp_path / "runs"
    _run_through_recensor(root, "r", "happy")
    tree = RunTree(root, "r")
    reviews = _reviews_by_key(tree)
    link = reviews["a2"]["payload"]["continuation"]
    assert link["is_continuation"] is True
    assert link["page_ordinals"] == [1, 2]
    assert len(link["region_ids"]) == 2

    # The fact is derived from the Designator's real region evidence, not
    # copied from the seal: prove the two identities in the link are the
    # act's own real proposal regions.
    regions = [
        tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
        for entry in tree.build_manifest(DESIGNATOR)["artifacts"]
        if entry["kind"] == "region" and entry["subject_id"] == reviews["a2"]["subject_id"]
    ]
    assert sorted(region["payload"]["region_id"] for region in regions) == link["region_ids"]


def test_a_designator_held_act_with_a_real_region_carries_that_regions_own_facts(tmp_path):
    """A hold has two distinct shapes (`pipeline/2_designator/run.py::
    initial_pass`): the act's own page never sealed, and no region of it is cut
    at all; or the act's own page sealed and its near-side region really was
    cut, and only a declared continuation's page never sealed. The "refused-
    page" scenario is the second shape for act a2 -- its near-side region on
    page 1 is real, on-disk, sealed evidence. The Recensor's continuation and
    page_coverage facts must reflect that real region rather than assume every
    hold cut nothing, or a flagged page whose only touching act is a hold of
    this shape would never be reported anywhere."""
    root = tmp_path / "runs"
    _run_through_recensor(root, "r", "refused-page")
    tree = RunTree(root, "r")
    reviews = _reviews_by_key(tree)
    held = reviews["a2"]
    assert held["outcome"] == "held-for-review"

    regions = [
        tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
        for entry in tree.build_manifest(DESIGNATOR)["artifacts"]
        if entry["kind"] == "region" and entry["subject_id"] == held["subject_id"]
    ]
    assert len(regions) == 1
    region_id = regions[0]["payload"]["region_id"]

    assert held["payload"]["continuation"] == {
        "is_continuation": False,
        "page_ordinals": [1],
        "region_ids": [region_id],
    }
    assert held["payload"]["page_coverage"] == {"checked_pages": [1], "flagged_pages": []}


def test_a_designator_held_act_with_no_region_at_all_carries_empty_facts(tmp_path):
    """The other hold shape: the act's own page never sealed, so no region of
    it was ever cut, and the empty continuation/page_coverage the previous
    test used to assert for BOTH shapes is only actually correct for this
    one. The "refused-first-page" scenario loses page 1, the page both a1 and
    a2 live on, so neither act has any region at all."""
    root = tmp_path / "runs"
    _run_through_recensor(root, "r", "refused-first-page")
    tree = RunTree(root, "r")
    reviews = _reviews_by_key(tree)
    for act_key in ("a1", "a2"):
        held = reviews[act_key]
        assert held["outcome"] == "held-for-review"
        assert not [
            entry
            for entry in tree.build_manifest(DESIGNATOR)["artifacts"]
            if entry["kind"] == "region" and entry["subject_id"] == held["subject_id"]
        ]
        assert held["payload"]["continuation"] == {
            "is_continuation": False,
            "page_ordinals": [],
            "region_ids": [],
        }
        assert held["payload"]["page_coverage"] == {"checked_pages": [], "flagged_pages": []}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
