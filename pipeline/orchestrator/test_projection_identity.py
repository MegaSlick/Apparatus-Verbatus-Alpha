"""GOVERNANCE 5's executable half: every export format's clean text hashes to the
established record's own text hash.

Spec 10, test 4: "every export format carries a canonical clean-text field, and
that field's hash equals the record's text hash per act, mechanically." The old
pipeline could never have passed this — the audit found the established text was
decided *twice*, so there was no single record for an export format to agree
with in the first place. This drives the real orchestrator end to end and checks
the one export format the (unedited, off-limits this round) Armarium actually
produces today: its `delivered[i]["text"]` field must be byte-identical to, and
therefore hash-identical to, the Archetypus record's own `text` and `text_hash`.

Rendered displays (brackets, sigla) are not built by any stage yet — spec 10
names that as the Armarium's future business at export time — so the
render -> strip -> hash round-trip half of test 4 is proven separately, as a
schema-sufficiency demonstration, in `pipeline/6_archetypus/test_annotations.py`.

What this therefore does **not** prove: that *every* export format agrees, because
one is all the pipeline builds today. When the Armarium lane adds a second, this
test is where it must be added, and its failure mode should be a missing format
rather than a silent pass over the one that exists.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from common.contracts.canonical import digest_of
from common.contracts.identities import artifact_id
from common.contracts.stages import ARCHETYPUS, ARMARIUM, RECENSOR
from common.runtree.store import RunTree

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"
FIXTURE = "synthetic-two-page-v0"


def orchestrate(root: Path, run_id: str, scenario: str) -> subprocess.CompletedProcess:
    """Run the pipeline the way a person would, and return the whole result.

    Deliberately not imported from `test_orchestrator_acceptance.py`:
    `pipeline/test_stage_import_boundaries.py` refuses any stage file that
    imports `pipeline` by its dotted path, so this small helper is duplicated
    rather than shared across that boundary.
    """
    return subprocess.run(
        [
            sys.executable,
            str(ORCHESTRATOR),
            "--fixture",
            FIXTURE,
            "--scenario",
            scenario,
            "--run-id",
            run_id,
            "--run-root",
            str(root),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        # Generous for the two-page fixture; a hung recovery loop should fail
        # this test with captured output, not block CI until an outer watchdog.
        timeout=600,
    )


def export_of(tree: RunTree) -> dict:
    return tree.read_artifact(ARMARIUM, "export", artifact_id(ARMARIUM, "export", "export", None))[
        "payload"
    ]


@pytest.mark.parametrize(("scenario", "exit_code"), [("happy", 0), ("review", 3)])
def test_every_delivered_export_text_hashes_to_its_archetypus_record(tmp_path, scenario, exit_code):
    root = tmp_path / "runs"
    result = orchestrate(root, "r", scenario)
    # The scenario's own exit, not "either": a held happy run or a completed
    # review run is a wrong result this test must not read past.
    assert result.returncode == exit_code, result.stderr
    tree = RunTree(root, "r")
    export = export_of(tree)
    assert export["delivered"], f"the {scenario!r} scenario must deliver at least one act"

    for delivered in export["delivered"]:
        established = tree.read_artifact(
            ARCHETYPUS, "archetypus", artifact_id(ARCHETYPUS, "archetypus", delivered["act_id"])
        )
        payload = established["payload"]
        # The one canonical clean-text field this export format carries.
        assert delivered["text"] == payload["text"]
        # And its hash mechanically equals the record's own text hash — the
        # projection-identity claim, proven rather than asserted by construction.
        assert digest_of(delivered["text"]) == payload["text_hash"]
    # The whole delivered set, not a sample: a projection check that skipped an
    # act would pass while that act's export carried a different reading. These
    # comparisons — not the loop above, which visits whatever "delivered"
    # happens to hold — are the only lines that prove no act was passed over,
    # and the length checks are what a bare set comparison would forgive: an
    # act delivered twice collapses to one set member and a consumer counts it
    # twice while the suite reports success.
    delivered_ids = [item["act_id"] for item in export["delivered"]]
    archetypus_ids = [
        entry["subject_id"]
        for entry in tree.build_manifest(ARCHETYPUS)["artifacts"]
        if entry["kind"] == "archetypus"
    ]
    # A census from a different stage's records: the acts the Recensor
    # accepted. An omission shared by the Archetypus and the export cannot
    # also delete the Recensor's sealed review, so agreeing with this set is
    # not the run agreeing with itself. (Completeness against the *fixture* is
    # the acceptance suite's job, via its pinned file counts and digests.)
    recensor_accepted_ids = [
        entry["subject_id"]
        for entry in tree.build_manifest(RECENSOR)["artifacts"]
        if entry["kind"] == "review" and entry["outcome"] == "accepted"
    ]
    assert len(delivered_ids) == len(set(delivered_ids))
    assert len(recensor_accepted_ids) == len(set(recensor_accepted_ids))
    assert len(delivered_ids) == len(archetypus_ids) == len(recensor_accepted_ids)
    assert set(delivered_ids) == set(archetypus_ids) == set(recensor_accepted_ids)
