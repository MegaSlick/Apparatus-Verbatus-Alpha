"""Unit 2: an unsealed Perlector pass resumes without minting a second reading.

`pipeline/4_perlector/run.py::_next_attempt` derives a reading's ordinal from
the act's own state (`recovery_region_count`), and that derivation is not local
to this stage: the Recensor, the Archetypus and the Armarium each re-derive it
and require an act's reading count to equal its recovery crop count plus one.
So a crashed pass resumes by recomputing the same ordinal and republishing --
byte-identical under every chair that exists, and refused by the run tree
(`IncompatibleReuse`) rather than overwritten if a future chair diverges.

Probing for the first free identity and appending there was tried, and this
test is what forbids it: the resumed stage sealed its boundary and reported
success while leaving the run dead at the Recensor on `act ... carries 2
Perlectio attempt(s) for 0 recovery crop(s); every reread must answer one
recorded recrop and no reading may appear unrequested`. The assertions
therefore run the consuming stages, not only this one.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from common.contracts.stages import PERLECTOR
from common.runtree.store import RunTree
from common.stage import StageContext

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = ROOT / "proof"


def _load_perlector():
    path = Path(__file__).resolve().parent / "run.py"
    spec = importlib.util.spec_from_file_location("perlector_resume_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


perlector = _load_perlector()


def invoke_stage(
    run_root: Path, run_id: str, scenario: str, program: str
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / program),
            "--run-root",
            str(run_root),
            "--run-id",
            run_id,
            "--scenario",
            scenario,
            "--fixture-root",
            str(FIXTURE_ROOT),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def _through_attestatores(root: Path, run_id: str, scenario: str = "happy") -> RunTree:
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/1_ink_map/run.py",
        "pipeline/2_designator/run.py",
        "pipeline/3_attestatores/run.py",
    ):
        result = invoke_stage(root, run_id, scenario, program)
        assert result.returncode == 0, f"{program}: {result.stderr}"
    return RunTree(root, run_id)


def _perlectiones(tree: RunTree) -> list[dict]:
    return [
        tree.read_artifact(PERLECTOR, "perlectio", entry["artifact_id"])
        for entry in tree.build_manifest(PERLECTOR, verify_inputs=False)["artifacts"]
        if entry["kind"] == "perlectio"
    ]


def _has_stage_seal(tree: RunTree) -> bool:
    return any(
        entry["kind"] == "stage-seal"
        for entry in tree.build_manifest(PERLECTOR, verify_inputs=False)["artifacts"]
    )


def test_an_unsealed_perlector_pass_resumes_without_minting_an_unrequested_reading(
    tmp_path, monkeypatch
):
    """A crash between acts must leave one reading per act, and a live run.

    The injected exception comes *after* the real writer has durably sealed
    the first act's establishing Perlectio, simulating a process crash before
    `context.seal_boundary()`. The next process is the ordinary stage entry
    point over the real run tree: the crashed act's Perlectio must survive
    byte-for-byte, no act may gain a second reading it has no recovery crop
    for, and the consuming stages must still complete.
    """
    root = tmp_path / "runs"
    tree = _through_attestatores(root, "resume")

    real_publish = StageContext.publish
    perlectio_writes = 0

    def crash_after_first_perlectio(self, *, kind, **kwargs):
        nonlocal perlectio_writes
        result = real_publish(self, kind=kind, **kwargs)
        if kind == "perlectio" and kwargs.get("outcome") != "not-run":
            perlectio_writes += 1
            if perlectio_writes == 1:
                raise RuntimeError("simulated process crash after the first real reading")
        return result

    monkeypatch.setattr(StageContext, "publish", crash_after_first_perlectio)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run.py",
            "--run-root",
            str(root),
            "--run-id",
            "resume",
            "--scenario",
            "happy",
            "--fixture-root",
            str(FIXTURE_ROOT),
        ],
    )
    with pytest.raises(RuntimeError, match="simulated process crash"):
        perlector.main()

    assert perlectio_writes == 1
    assert not _has_stage_seal(tree)
    before = _perlectiones(tree)
    assert len(before) == 1
    first_act_id = before[0]["subject_id"]
    assert before[0]["payload"]["attempt_ordinal"] == 1
    first_reading_before = before[0]
    monkeypatch.undo()

    resumed = invoke_stage(root, "resume", "happy", "pipeline/4_perlector/run.py")
    assert resumed.returncode == 0, resumed.stderr
    assert _has_stage_seal(tree)

    after = _perlectiones(tree)
    # The crashed act's reading was reused, never rewritten and never doubled.
    first_act_records = [record for record in after if record["subject_id"] == first_act_id]
    assert len(first_act_records) == 1
    assert first_act_records[0] == first_reading_before

    # No act carries a reading its crop history did not ask for. This run cuts
    # no recovery crop, so every act carries exactly one, at ordinal one.
    for act_id in {record["subject_id"] for record in after}:
        ordinals = sorted(
            record["payload"]["attempt_ordinal"]
            for record in after
            if record["subject_id"] == act_id
        )
        assert ordinals == [1], f"{act_id}: {ordinals}"

    # The stages that re-derive this ordinal are the ones that catch an
    # unrequested reading, and they only speak after this stage has sealed.
    for program in (
        "pipeline/5_recensor/run.py",
        "pipeline/6_archetypus/run.py",
        "pipeline/7_armarium/run.py",
    ):
        result = invoke_stage(root, "resume", "happy", program)
        assert result.returncode == 0, f"{program}: {result.stderr}"
