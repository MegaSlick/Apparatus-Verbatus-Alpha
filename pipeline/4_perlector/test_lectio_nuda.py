"""Lectio nuda: sampled, carries no testimonia, and structurally invisible to
every consumer that establishes text.

Spec_08: nuda "never establishes text: it is an instrument record with no path
to the Archetypus constructor" -- enforced here at the module boundary, not by
convention.
"""

import subprocess
import sys
from pathlib import Path

import pytest
from nuda import LECTIO_NUDA_KIND, SELECTION_RULE, is_nuda_sampled

from common.contracts.errors import SchemaRefusal
from common.contracts.identities import attempt_id
from common.contracts.stages import ARCHETYPUS, PERLECTOR
from common.runtree.store import RunTree

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"


# A stand-in for the reference Tyrel's own approval would carry. It is required
# whenever anything is sampled, so these tests must supply one exactly as a real
# run would; a test that could sample without it would be testing a pipeline
# nobody is allowed to run.
APPROVAL = "fixture-nuda-design/test-only"


def orchestrate(
    run_root: Path,
    run_id: str,
    scenario: str,
    *,
    nuda_per_mille: int = 0,
    approval_ref: str = APPROVAL,
):
    command = [
        sys.executable,
        str(ORCHESTRATOR),
        "--fixture",
        "synthetic-two-page-v0",
        "--scenario",
        scenario,
        "--run-id",
        run_id,
        "--run-root",
        str(run_root),
        "--nuda-per-mille",
        str(nuda_per_mille),
        "--nuda-approval-ref",
        approval_ref,
    ]
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True)


def perlectio_kind_artifacts(tree, act_id):
    """The exact query every real consumer runs: filtered to kind == 'perlectio'."""
    return [
        tree.read_artifact(PERLECTOR, "perlectio", entry["artifact_id"])
        for entry in tree.build_manifest(PERLECTOR)["artifacts"]
        if entry["kind"] == "perlectio" and entry["subject_id"] == act_id
    ]


@pytest.fixture(scope="module")
def nuda_run(tmp_path_factory):
    root = tmp_path_factory.mktemp("nuda") / "runs"
    result = orchestrate(root, "r", "happy", nuda_per_mille=1000)
    assert result.returncode == 0, result.stderr
    return RunTree(root, "r")


def test_nuda_per_mille_1000_samples_every_act(nuda_run):
    """1000 per mille means every act is sampled -- proof the sampling design
    itself is real, not merely that a switch exists."""
    entries = [
        entry
        for entry in nuda_run.build_manifest(PERLECTOR)["artifacts"]
        if entry["kind"] == LECTIO_NUDA_KIND
    ]
    subjects = {entry["subject_id"] for entry in entries}
    assert len(subjects) == 2, "both acts in the happy fixture should be nuda-sampled at 1000/1000"


def test_a_nuda_reading_carries_no_testimonia_at_all(nuda_run):
    entries = [
        entry
        for entry in nuda_run.build_manifest(PERLECTOR)["artifacts"]
        if entry["kind"] == LECTIO_NUDA_KIND
    ]
    assert entries, "a sampled run must publish nuda records before their contents can be tested"
    for entry in entries:
        record = nuda_run.read_artifact(PERLECTOR, LECTIO_NUDA_KIND, entry["artifact_id"])
        assert record["payload"]["dossier"]["testimonia"] == []
        assert record["payload"]["dissent"] == []


def test_a_nuda_attempt_uses_its_own_operation_never_perlegere(nuda_run):
    entries = [
        entry
        for entry in nuda_run.build_manifest(PERLECTOR)["artifacts"]
        if entry["kind"] == LECTIO_NUDA_KIND
    ]
    assert entries
    for entry in entries:
        record = nuda_run.read_artifact(PERLECTOR, LECTIO_NUDA_KIND, entry["artifact_id"])
        ordinal = record["payload"]["attempt_ordinal"]
        subject = record["subject_id"]
        assert record["attempt_id"] == attempt_id(subject, "lectio-nuda", ordinal)
        assert record["attempt_id"] != attempt_id(subject, "perlegere", ordinal)


def test_nuda_records_are_structurally_invisible_to_the_perlectio_kind_query(nuda_run):
    """The module boundary itself: every real consumer (Recensor, Archetypus,
    Armarium, the orchestrator's own recovery dispatch) filters on
    kind == "perlectio" before reading anything. Run the identical query and
    show the nuda record never appears in the result set."""
    subjects = {
        entry["subject_id"]
        for entry in nuda_run.build_manifest(PERLECTOR)["artifacts"]
        if entry["kind"] == LECTIO_NUDA_KIND
    }
    assert subjects
    for act_id in subjects:
        readings = perlectio_kind_artifacts(nuda_run, act_id)
        # Exactly the one real establishing Perlectio -- the nuda record for
        # the same act is not merely absent from a filtered subset, it never
        # entered the population the filter ran over.
        assert len(readings) == 1
        assert readings[0]["kind"] == "perlectio"


def test_nuda_never_disturbs_normal_establishment(nuda_run):
    """Both acts still reach the Archetypus exactly as the happy path always
    does; the instrument reading runs alongside establishment, never inside it."""
    established = [
        entry
        for entry in nuda_run.build_manifest(ARCHETYPUS)["artifacts"]
        if entry["kind"] == "archetypus"
    ]
    assert len(established) == 2


def test_a_forged_review_naming_a_nuda_artifact_as_its_perlectio_is_refused(nuda_run):
    """The negative path: even if some future code forged a Recensor-style
    reference pointing at a `lectio-nuda` artifact and called it a Perlectio,
    the digest-checked reference read is refused by kind, because
    `read_artifact_reference` requires an exact `kind="perlectio"` match."""
    entry = next(
        entry
        for entry in nuda_run.build_manifest(PERLECTOR)["artifacts"]
        if entry["kind"] == LECTIO_NUDA_KIND
    )
    reference = {
        "relative_path": entry["relative_path"],
        "sha256": entry["sha256"],
    }
    with pytest.raises(SchemaRefusal, match="not required"):
        nuda_run.read_artifact_reference(
            reference, stage=PERLECTOR, kind="perlectio", subject_id=entry["subject_id"]
        )


def test_a_sampled_nuda_records_the_design_it_was_drawn_under(nuda_run):
    """GOVERNANCE 10: a sample of unknown design measures nothing. Each record
    names the rate, the selection rule, and the approval it was drawn under."""
    entry = next(
        entry
        for entry in nuda_run.build_manifest(PERLECTOR)["artifacts"]
        if entry["kind"] == LECTIO_NUDA_KIND
    )
    record = nuda_run.read_artifact(PERLECTOR, LECTIO_NUDA_KIND, entry["artifact_id"])
    assert record["payload"]["sampling"] == {
        "nuda_per_mille": 1000,
        "selection_rule": SELECTION_RULE,
        "approval_ref": APPROVAL,
    }


def test_a_run_may_not_sample_nuda_without_tyrels_predeclared_design(tmp_path):
    """Hard rule 1: the sampling design is his to approve. A rate with no
    approval reference refuses before anything is written, rather than drawing
    an instrument sample nobody asked for."""
    root = tmp_path / "runs"
    result = orchestrate(root, "r", "happy", nuda_per_mille=1000, approval_ref="")
    assert result.returncode != 0
    assert "predeclared sampling design reference" in result.stderr
    assert not (root / "r").exists()


def test_nuda_per_mille_zero_produces_no_nuda_records_at_all(tmp_path):
    """The unchanged default: every existing scenario runs exactly as it did
    before this instrument existed."""
    root = tmp_path / "runs"
    result = orchestrate(root, "r", "happy", nuda_per_mille=0)
    assert result.returncode == 0, result.stderr
    tree = RunTree(root, "r")
    entries = [
        entry
        for entry in tree.build_manifest(PERLECTOR)["artifacts"]
        if entry["kind"] == LECTIO_NUDA_KIND
    ]
    assert entries == []


def test_is_nuda_sampled_rejects_an_out_of_range_per_mille():
    with pytest.raises(ValueError, match=r"\[0, 1000\]"):
        is_nuda_sampled("act_1", run_id="r", nuda_per_mille=1001)
    with pytest.raises(ValueError, match=r"\[0, 1000\]"):
        is_nuda_sampled("act_1", run_id="r", nuda_per_mille=-1)


def test_is_nuda_sampled_is_deterministic_for_the_same_act_and_run():
    first = is_nuda_sampled("act_1", run_id="r", nuda_per_mille=500)
    second = is_nuda_sampled("act_1", run_id="r", nuda_per_mille=500)
    assert first == second
