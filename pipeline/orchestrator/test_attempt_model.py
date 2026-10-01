"""Witness attempts, driven through the real stage programs.

A whole second Attestatores pass appends every configured chair's next attempt,
and a page witness appended after the Perlector read its page makes every later
stage refuse the tree rather than keep exporting what that witness no longer
says. Every test drives the real programs as subprocesses over the synthetic
fixture.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from common.contracts.canonical import canonical_bytes, self_hash
from common.contracts.identities import artifact_id, attempt_id
from common.contracts.stages import ARMARIUM, ATTESTATORES
from common.runtree.store import RunTree
from conftest import file_bytes_snapshot as snapshot
from conftest import programs_through

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"
FIXTURE_ROOT = ROOT / "proof"
FIXTURE = "synthetic-two-page-v0"

ATTESTATORES_PROGRAM = "pipeline/3_attestatores/run.py"
RECENSOR_PROGRAM = "pipeline/5_recensor/run.py"
ARCHETYPUS_PROGRAM = "pipeline/6_archetypus/run.py"
CONIECTOR_PROGRAM = "pipeline/4b_coniector/run.py"
ARMARIUM_PROGRAM = "pipeline/7_armarium/run.py"


def invoke(run_root: Path, run_id: str, scenario: str, program: str, *extra: str):
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
            *extra,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def orchestrate(run_root: Path, run_id: str, scenario: str):
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
            str(run_root),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def through_attestatores(run_root: Path, run_id: str, scenario: str) -> RunTree:
    for program in programs_through("attestatores"):
        result = invoke(run_root, run_id, scenario, program)
        assert result.returncode == 0, f"{program}: {result.stderr}"
    return RunTree(run_root, run_id)


def artifacts(tree: RunTree, stage: str, kind: str, subject: str | None = None) -> list[dict]:
    return [
        tree.read_artifact(stage, kind, entry["artifact_id"])
        for entry in tree.build_manifest(stage)["artifacts"]
        if entry["kind"] == kind and subject in (None, entry["subject_id"])
    ]


def reseal(path: Path, record: dict) -> None:
    record["self_hash"] = self_hash(
        {key: value for key, value in record.items() if key != "self_hash"}
    )
    path.write_bytes(canonical_bytes(record))


def test_a_whole_second_pass_appends_every_configured_chairs_next_attempt(tmp_path):
    """A second attempt is an expensive instrument, and it stays available."""
    root = tmp_path / "runs"
    tree = through_attestatores(root, "r", "happy")

    result = invoke(root, "r", "happy", ATTESTATORES_PROGRAM, "--attempt-ordinal", "2")

    assert result.returncode == 0, result.stderr
    by_pair: dict[tuple[int, str], set[int]] = {}
    for record in artifacts(tree, ATTESTATORES, "page-testimonium"):
        payload = record["payload"]
        by_pair.setdefault((payload["page_ordinal"], payload["chair"]), set()).add(
            payload["attempt_ordinal"]
        )
    assert len(by_pair) == 6
    assert all(ordinals == {1, 2} for ordinals in by_pair.values()), by_pair


def _supersede_a_page_witness(tree: RunTree, page_ordinal: int, chair: str) -> None:
    """Append a page Testimonium attempt the page feed the reading was shown does not cite.

    Written directly: the point is that a tree assembled, resumed or resealed
    some other way is refused on its own structure.
    """
    current = max(
        (
            record
            for record in artifacts(tree, ATTESTATORES, "page-testimonium")
            if record["payload"]["chair"] == chair
            and record["payload"]["page_ordinal"] == page_ordinal
        ),
        key=lambda record: record["payload"]["attempt_ordinal"],
    )
    page_id = current["subject_id"]
    ordinal = current["payload"]["attempt_ordinal"] + 1
    appended = json.loads(json.dumps(current))
    appended["payload"]["attempt_ordinal"] = ordinal
    appended["attempt_id"] = attempt_id(page_id, f"read:{chair}", ordinal)
    appended["artifact_id"] = artifact_id(
        ATTESTATORES, "page-testimonium", page_id, appended["attempt_id"]
    )
    reseal(
        tree.resolve(tree.artifact_path(ATTESTATORES, "page-testimonium", appended["artifact_id"])),
        appended,
    )
    tree.write_manifest(ATTESTATORES)


def test_no_later_stage_completes_over_a_page_witness_superseded_after_the_reading(tmp_path):
    """A green run, a page witness appended after it, then stages 5 to 7 by hand.

    Each stage rebuilds the page feed from the current page Testimonia and finds
    it is not the feed the reading was shown, so none re-derives `complete` from
    a reading whose witnesses have since changed; each refuses without writing.
    """
    root = tmp_path / "runs"
    assert orchestrate(root, "r", "page-unbroken").returncode == 0
    tree = RunTree(root, "r")
    [export] = artifacts(tree, ARMARIUM, "export")
    assert export["outcome"] == "delivered", "the run must be green before it is contradicted"

    _supersede_a_page_witness(tree, 2, "attestator_2")
    before = snapshot(root)

    # The Coniector reads no witness basis; the run's sealed one stands for the Armarium.
    for program in (RECENSOR_PROGRAM, ARCHETYPUS_PROGRAM, ARMARIUM_PROGRAM):
        result = invoke(root, "r", "page-unbroken", program)
        assert result.returncode != 0, f"{program} accepted a superseded page witness"
        assert "page feed is not the feed its sealed inputs build" in result.stderr, (
            f"{program}: {result.stderr}"
        )
    assert snapshot(root) == before
