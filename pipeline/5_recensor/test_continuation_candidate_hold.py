"""Both acts a Designator continuation candidate names are read, then held for review.

Delivered alone, the head of an act crossing a page break is a truncation and
its tail an act with no heading. The Recensor does not make the link, which is
a decision for review; it holds both acts so neither is delivered as a whole
act, and the export says partial.
"""

import sys
from pathlib import Path

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
    for key in ("proposal:1:0", "proposal:2:1"):
        assert reviews[act_ids[key]]["outcome"] == "accepted"

    export = verify_final_seal(tree)
    assert export["payload"]["aggregate"]["status"] == "partial"
