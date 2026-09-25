"""Both acts a Designator continuation candidate names are read, then held for review.

Delivered alone, the head of an act crossing a page break is a truncation and
its tail an act with no heading. The Recensor does not make the link, which is
a decision for review; it holds both acts so neither is delivered as a whole
act, and the export says partial.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

PIPELINE = Path(__file__).resolve().parents[1]
for directory in (PIPELINE, PIPELINE / "2_designator"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from test_structure_chair_e2e import (  # noqa: E402
    PAGE_BREAK_HEAD,
    PAGE_BREAK_TAIL,
    RUN_ID,
    artifacts,
    page_break_run,
    seal_rows,
)

from common.contracts.errors import FatalAccounting  # noqa: E402
from common.contracts.stages import DESIGNATOR, PERLECTOR, RECENSOR  # noqa: E402
from common.runtree.store import RunTree  # noqa: E402
from common.stage import verify_final_seal  # noqa: E402


def test_both_acts_are_read_then_held_and_the_export_is_partial(tmp_path):
    run = page_break_run(tmp_path)
    rows = seal_rows(run.run_root)
    act_ids = {key: row["act_id"] for key, row in rows.items()}
    (candidate,) = artifacts(run.run_root, DESIGNATOR, "continuation-candidate")
    tree = RunTree(run.run_root, RUN_ID)
    candidate_path = tree.artifact_path(
        DESIGNATOR, "continuation-candidate", candidate["artifact_id"]
    )

    readings = {record["subject_id"] for record in artifacts(run.run_root, PERLECTOR, "perlectio")}
    assert readings == set(act_ids.values())

    reviews = {
        record["subject_id"]: record for record in artifacts(run.run_root, RECENSOR, "review")
    }
    for key in (PAGE_BREAK_HEAD, PAGE_BREAK_TAIL):
        review = reviews[act_ids[key]]
        assert review["outcome"] == "held-for-review"
        assert "continuation candidate" in review["payload"]["reason"]
        assert candidate_path in {reference["relative_path"] for reference in review["inputs"]}
        assert [
            reference["relative_path"]
            for reference in review["payload"]["continuation_candidate_refs"]
        ] == [candidate_path]
    for key in ("proposal:1:0", "proposal:2:1"):
        assert reviews[act_ids[key]]["outcome"] == "accepted"
        assert "continuation_candidate_refs" not in reviews[act_ids[key]]["payload"]

    export = verify_final_seal(tree)
    assert export["payload"]["aggregate"]["status"] == "partial"


def _load_recensor():
    path = PIPELINE / "5_recensor" / "run.py"
    spec = importlib.util.spec_from_file_location("recensor_candidate_refs_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


recensor = _load_recensor()


class _Context:
    def artifact_ref(self, stage, kind, artifact_id):
        return {"relative_path": f"{stage}/{kind}/{artifact_id}", "sha256": "0" * 64}


def _candidate(artifact_id: str, acts_a: list[str], acts_b: list[str], **payload) -> dict:
    return {
        "artifact_id": artifact_id,
        "payload": {
            "authoritative": False,
            "acts_a": [{"act_id": act, "act_key": act} for act in acts_a],
            "acts_b": [{"act_id": act, "act_key": act} for act in acts_b],
            **payload,
        },
    }


def _refs(monkeypatch, records, sealed=("a", "b", "c")):
    monkeypatch.setattr(recensor, "_records_of_kind", lambda *_args: iter(records))
    return recensor.continuation_candidate_refs(_Context(), set(sealed))


def test_an_act_that_ends_one_break_and_opens_the_next_cites_both(monkeypatch):
    refs = _refs(
        monkeypatch, [_candidate("first", ["a"], ["b"]), _candidate("second", ["b"], ["c"])]
    )
    assert [ref["relative_path"] for ref in refs["b"]] == [
        "designator/continuation-candidate/first",
        "designator/continuation-candidate/second",
    ]
    assert len(refs["a"]) == len(refs["c"]) == 1


def test_a_candidate_claiming_authority_is_refused(monkeypatch):
    with pytest.raises(FatalAccounting, match="authoritative"):
        _refs(monkeypatch, [_candidate("x", ["a"], ["b"], authoritative=True)])


def test_a_candidate_naming_an_act_outside_the_seal_is_refused(monkeypatch):
    with pytest.raises(FatalAccounting, match="proposal seal"):
        _refs(monkeypatch, [_candidate("x", ["a"], ["unsealed"])])


@pytest.mark.parametrize(
    "payload",
    [
        {"authoritative": False, "acts_b": []},
        {"authoritative": False, "acts_a": "a", "acts_b": []},
        {"authoritative": False, "acts_a": [{"act_key": "a"}], "acts_b": []},
    ],
)
def test_a_malformed_candidate_is_refused_by_name(monkeypatch, payload):
    with pytest.raises(FatalAccounting, match="malformed"):
        _refs(monkeypatch, [{"artifact_id": "x", "payload": payload}])


def test_another_hold_cause_still_names_the_candidate():
    reason = recensor.with_candidate_reason("page(s) [1] carry ink outside every region", True)
    assert reason.startswith("page(s) [1]")
    assert "continuation candidate" in reason
    assert recensor.with_candidate_reason(reason, True) == reason
    assert recensor.with_candidate_reason("coverage reconciles", False) == "coverage reconciles"


def test_a_hold_review_of_a_named_act_cites_its_candidates(monkeypatch):
    """A re-shoot hold once reached review without the page-break candidate naming the act."""
    published = {}
    monkeypatch.setattr(recensor, "current_review", lambda *_args: None)
    monkeypatch.setattr(recensor, "publish_review", lambda _context, **kw: published.update(kw))
    context = _Context()
    reading = context.artifact_ref(PERLECTOR, "perlectio", "reading")
    refs = [context.artifact_ref(DESIGNATOR, "continuation-candidate", "first")]
    recensor._publish_hold_review(
        context,
        {"act_id": "a", "act_key": "a", "page_ordinal": 1},
        reason="cross-capture-read-not-built: held",
        candidate_refs=refs,
        regions=[],
        perlectio_ref=reading,
        budget={"allowed": 0, "absolute_cap": 0},
        coverage={},
        geometry_coverage={},
        content_coverage={},
        content_findings={},
        page_findings={},
    )
    assert published["inputs"] == [reading, *refs]
    assert published["payload"]["continuation_candidate_refs"] == refs
    assert published["payload"]["reason"].startswith("cross-capture-read-not-built")
    assert "continuation candidate" in published["payload"]["reason"]
