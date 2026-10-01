"""A person's page re-ask, read again on the fixture path when the run resumes from the Perlector.

The fixture's `page-review` scenario holds page 2 after the Recensor. A person
records a re-ask of page 2 and resumes the run from the Perlector: the
Perlector reads page 2 again as attempt 3, bound to the decision and
superseding the page's first reading and its re-ask, and every stage after it
works from that reading. A fixture reader asked the same request gives the same
answer, so page 2 is held again, now on its re-read; a person's advance then
exports it, labelled as read on an operator re-read.
"""

from __future__ import annotations

import json
import subprocess
import sys
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from common.contracts.stages import ARMARIUM, PERLECTOR
from common.page_path import OPERATOR_REREAD_FIELD
from common.page_review import published_units, superseded_readings
from common.runtree.store import RunTree
from common.stage import EXIT_HELD
from conftest import HELD_RECENSOR_STOP, advance_held_recensor
from operations.operator import decide

ROOT = Path(__file__).resolve().parents[2]


def _orchestrate(root: Path, *extra: str) -> subprocess.CompletedProcess:
    command = [
        sys.executable,
        str(ROOT / "pipeline" / "orchestrator" / "run.py"),
        "--fixture",
        "synthetic-two-page-v0",
        "--scenario",
        "page-review",
        "--run-id",
        "r",
        "--run-root",
        str(root),
        *extra,
    ]
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True)


def _readings(tree: RunTree, ordinal: int) -> dict[int, dict]:
    found = {}
    for entry in tree.build_manifest(PERLECTOR)["artifacts"]:
        if entry["kind"] == "page-reading":
            record = tree.read_artifact(PERLECTOR, "page-reading", entry["artifact_id"])
            if record["payload"]["page_ordinal"] == ordinal:
                found[record["payload"]["attempt_ordinal"]] = {
                    **record,
                    "relative_path": entry["relative_path"],
                }
    return found


def _act_readings(tree: RunTree) -> dict[str, str | None]:
    [export] = [
        tree.read_artifact(ARMARIUM, "export", entry["artifact_id"])
        for entry in tree.build_manifest(ARMARIUM)["artifacts"]
        if entry["kind"] == "export"
    ]
    data = tree.read_bytes(export["payload"]["bundle"]["reference"]["relative_path"])
    with ZipFile(BytesIO(data)) as archive:
        sources = json.loads(archive.read("sources.json"))
    return {row["act_key"]: row["reading"] for row in sources["act_readings"]}


def test_a_page_re_ask_is_read_again_when_the_run_resumes_from_the_perlector(tmp_path):
    root = tmp_path / "runs"
    first = _orchestrate(root)
    assert first.returncode == EXIT_HELD, first.stdout + first.stderr
    tree = RunTree(root, "r")
    prepared = decide.prepare_decision(
        tree,
        decision="re-ask",
        page=2,
        reason="read page 2 again",
        timestamp="2026-10-01T12:00:00Z",
    )
    reference = decide.record_decision(tree, prepared)
    assert "--from perlector --to armarium" in " ".join(decide.report(prepared, reference))

    again = _orchestrate(root, "--from", "perlector", "--to", "armarium")
    assert again.returncode == EXIT_HELD, again.stdout + again.stderr
    assert "reading 1 pages again as a person asked" in again.stderr
    assert HELD_RECENSOR_STOP in again.stdout

    readings = _readings(tree, 2)
    assert sorted(readings) == [1, 2, 3]
    block = readings[3]["payload"][OPERATOR_REREAD_FIELD]
    assert [item["approval_ref"]["relative_path"] for item in block["decisions"]] == [
        reference.relative_path
    ]
    assert [item["relative_path"] for item in block["supersedes"]] == [
        readings[1]["relative_path"],
        readings[2]["relative_path"],
    ]
    assert superseded_readings(tree) == {readings[1]["relative_path"], readings[2]["relative_path"]}
    # Every current review of page 2 is of its re-read.
    page_two = [unit for unit in published_units(tree) if unit["payload"]["page_ordinal"] == 2]
    assert page_two
    assert {unit["payload"]["page_reading_ref"]["relative_path"] for unit in page_two} == {
        readings[3]["relative_path"]
    }

    advance_held_recensor(root, "r")
    exported = _orchestrate(root, "--from", "recensor", "--to", "armarium")
    assert exported.returncode == EXIT_HELD, exported.stdout + exported.stderr
    labels = _act_readings(tree)
    assert labels["p2:1"] == "read on operator re-read"
    assert labels["p1:1"] == "first reading"
