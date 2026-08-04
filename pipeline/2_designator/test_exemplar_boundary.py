"""The first downstream boundary reconciles the Exemplar before cutting ink.

Both cases begin with the real synthetic orchestrator run, then damage only its
already-written Exemplar evidence.  The Designator must stop before publishing any
new proposal rather than allowing the final Armarium census to discover a missing
source after later stages have worked around it.
"""

import json
import subprocess
import sys
from io import BytesIO
from pathlib import Path

from PIL import Image

from common.contracts.canonical import canonical_bytes
from common.contracts.identities import artifact_id
from common.contracts.stages import DESIGNATOR, EXEMPLAR
from common.runtree.store import RunTree
from common.stage import EXIT_FATAL, EXIT_HELD

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"
DESIGNATOR_CLI = ROOT / "pipeline" / "2_designator" / "run.py"


def populated_run(tmp_path, scenario: str = "happy") -> RunTree:
    result = subprocess.run(
        [
            sys.executable,
            str(ORCHESTRATOR),
            "--fixture",
            "synthetic-two-page-v0",
            "--scenario",
            scenario,
            "--run-root",
            str(tmp_path / "runs"),
            "--run-id",
            "boundary",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    expected = EXIT_HELD if scenario.startswith("refused-") else 0
    assert result.returncode == expected, result.stderr
    return RunTree(tmp_path / "runs", "boundary")


def invoke_designator(tmp_path, scenario: str = "happy") -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(DESIGNATOR_CLI),
            "--run-root",
            str(tmp_path / "runs"),
            "--run-id",
            "boundary",
            "--fixture-root",
            str(ROOT / "proof"),
            "--scenario",
            scenario,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def test_missing_exemplar_page_stops_at_the_first_downstream_boundary_with_its_filename(tmp_path):
    tree = populated_run(tmp_path)
    entry = next(
        entry
        for entry in tree.build_manifest(EXEMPLAR)["artifacts"]
        if entry["kind"] == "page"
        and tree.read_artifact(EXEMPLAR, "page", entry["artifact_id"])["payload"]["ordinal"] == 2
    )
    before = tree.build_manifest(DESIGNATOR)
    tree.resolve(entry["relative_path"]).unlink()
    tree.write_manifest(EXEMPLAR)

    result = invoke_designator(tmp_path)
    assert result.returncode == EXIT_FATAL
    assert "page-2.png" in result.stderr
    assert "lost submitted page" in result.stderr
    assert tree.build_manifest(DESIGNATOR) == before


def test_a_tampered_corpus_seal_stops_before_the_designator_reads_any_page(tmp_path):
    tree = populated_run(tmp_path)
    identity = artifact_id(EXEMPLAR, "seal", "corpus-seal")
    path = tree.resolve(tree.artifact_path(EXEMPLAR, "seal", identity))
    record = json.loads(path.read_text(encoding="utf-8"))
    record["payload"]["pages"][0]["declared_path"] = "wrong-name.png"
    path.write_bytes(canonical_bytes(record))
    tree.write_manifest(EXEMPLAR)

    result = invoke_designator(tmp_path)
    assert result.returncode == EXIT_FATAL
    assert "valid self-hashed census" in result.stderr


def test_a_changed_sealed_pixel_blob_stops_before_designator_crops_or_rehashes_it(tmp_path):
    """A later stage must not turn altered pixels into a fresh valid crop digest."""
    tree = populated_run(tmp_path)
    page_entry = next(
        entry
        for entry in tree.build_manifest(EXEMPLAR)["artifacts"]
        if entry["kind"] == "page"
        and tree.read_artifact(EXEMPLAR, "page", entry["artifact_id"])["outcome"] == "sealed"
    )
    page = tree.read_artifact(EXEMPLAR, "page", page_entry["artifact_id"])
    blob_path = tree.resolve(page["payload"]["image_path"])
    with Image.open(BytesIO(blob_path.read_bytes())) as image:
        changed = image.convert("RGB")
        changed.putpixel((0, 0), (255, 0, 0))
        output = BytesIO()
        changed.save(output, format="PNG")
    blob_path.write_bytes(output.getvalue())
    before = tree.build_manifest(DESIGNATOR)

    result = invoke_designator(tmp_path)

    assert result.returncode == EXIT_FATAL
    assert "sealed Exemplar pixel blob" in result.stderr
    assert tree.build_manifest(DESIGNATOR) == before


def test_a_missing_sealed_pixel_blob_is_a_named_boundary_failure_not_a_traceback(tmp_path):
    tree = populated_run(tmp_path)
    page_entry = next(
        entry
        for entry in tree.build_manifest(EXEMPLAR)["artifacts"]
        if entry["kind"] == "page"
        and tree.read_artifact(EXEMPLAR, "page", entry["artifact_id"])["outcome"] == "sealed"
    )
    page = tree.read_artifact(EXEMPLAR, "page", page_entry["artifact_id"])
    tree.resolve(page["payload"]["image_path"]).unlink()
    before = tree.build_manifest(DESIGNATOR)

    result = invoke_designator(tmp_path)

    assert result.returncode == EXIT_FATAL
    assert "sealed Exemplar pixel blob could not be read" in result.stderr
    assert "Traceback" not in result.stderr
    assert tree.build_manifest(DESIGNATOR) == before


def test_a_refused_page_keeps_its_door_alarm_evidence_at_the_downstream_boundary(tmp_path):
    tree = populated_run(tmp_path, "refused-page")
    refused = next(
        tree.read_artifact(EXEMPLAR, "page", entry["artifact_id"])
        for entry in tree.build_manifest(EXEMPLAR)["artifacts"]
        if entry["kind"] == "page"
        and tree.read_artifact(EXEMPLAR, "page", entry["artifact_id"])["outcome"] == "refused"
    )
    assert len(refused["inputs"]) == 1
    admission_path = refused["inputs"][0]["relative_path"]
    tree.resolve(admission_path).unlink()

    result = invoke_designator(tmp_path, "refused-page")

    assert result.returncode == EXIT_FATAL
    assert "refused Door admission could not be read" in result.stderr
