"""The Designator reconciles the Exemplar before cutting ink: most tests run
the real orchestrator, damage only the Exemplar's written evidence, and check
the Designator stops before publishing any new proposal. The missing-outcome
test also damages the Ink Map's own accounting so the run reaches census
reconciliation; the TOCTOU test below calls the Designator's internals
directly, on pixels tampered after the upfront check.
"""

import json
import subprocess
import sys
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from common.contracts.canonical import canonical_bytes, self_hash
from common.contracts.errors import ContractError
from common.contracts.identities import artifact_id
from common.contracts.stages import EXEMPLAR, INK_MAP
from common.runtree.store import RunTree
from common.stage import EXIT_FATAL, EXIT_HELD, open_context, stage_parser
from conftest import load_stage, programs_through

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"
DESIGNATOR_CLI = ROOT / "pipeline" / "2_designator" / "run.py"


def snapshot(root: Path) -> dict[str, bytes]:
    """Raw tree bytes for assertions after deliberately breaking validation."""
    return {
        str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }


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


def test_missing_exemplar_page_stops_at_the_first_downstream_boundary(tmp_path, rebind_stage_seal):
    tree = populated_run(tmp_path)
    entry = next(
        entry
        for entry in tree.build_manifest(EXEMPLAR)["artifacts"]
        if entry["kind"] == "page"
        and tree.read_artifact(EXEMPLAR, "page", entry["artifact_id"])["payload"]["ordinal"] == 2
    )
    tree.resolve(entry["relative_path"]).unlink()
    rebind_stage_seal(tree, EXEMPLAR, rewrite_manifest=False)
    before = snapshot(tree.root)

    result = invoke_designator(tmp_path)
    assert result.returncode == EXIT_FATAL
    assert "artifact input" in result.stderr
    assert entry["relative_path"] in result.stderr
    assert snapshot(tree.root) == before


def test_a_tampered_corpus_seal_stops_before_the_designator_reads_any_page(
    tmp_path, rebind_stage_seal
):
    tree = populated_run(tmp_path)
    identity = artifact_id(EXEMPLAR, "seal", "corpus-seal")
    path = tree.resolve(tree.artifact_path(EXEMPLAR, "seal", identity))
    record = json.loads(path.read_text(encoding="utf-8"))
    record["payload"]["pages"][0]["declared_path"] = "wrong-name.png"
    record["self_hash"] = self_hash(record)
    path.write_bytes(canonical_bytes(record))
    tree.write_manifest(EXEMPLAR)
    rebind_stage_seal(tree, EXEMPLAR)
    # Captured after write_manifest (which mutates the tree), proving the
    # Designator stops before publishing any new proposal.
    before = snapshot(tree.root)

    result = invoke_designator(tmp_path)
    assert result.returncode == EXIT_FATAL
    assert "valid self-hashed census" in result.stderr
    assert snapshot(tree.root) == before


def test_a_changed_sealed_pixel_blob_stops_before_designator_crops_or_rehashes_it(
    tmp_path, rebind_stage_seal
):
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
        changed = image.copy()
        original = changed.getpixel((0, 0))
        changed.putpixel((0, 0), 0 if original else 255)
        output = BytesIO()
        changed.save(output, format="PNG")
    blob_path.write_bytes(output.getvalue())
    rebind_stage_seal(tree, EXEMPLAR, rewrite_manifest=False)
    before = snapshot(tree.root)

    result = invoke_designator(tmp_path)

    assert result.returncode == EXIT_FATAL
    assert "changed under a sealed reference" in result.stderr
    assert page["payload"]["image_path"] in result.stderr
    assert snapshot(tree.root) == before


def test_a_missing_sealed_pixel_blob_is_a_named_boundary_failure_not_a_traceback(
    tmp_path, rebind_stage_seal
):
    tree = populated_run(tmp_path)
    page_entry = next(
        entry
        for entry in tree.build_manifest(EXEMPLAR)["artifacts"]
        if entry["kind"] == "page"
        and tree.read_artifact(EXEMPLAR, "page", entry["artifact_id"])["outcome"] == "sealed"
    )
    page = tree.read_artifact(EXEMPLAR, "page", page_entry["artifact_id"])
    tree.resolve(page["payload"]["image_path"]).unlink()
    rebind_stage_seal(tree, EXEMPLAR, rewrite_manifest=False)
    before = snapshot(tree.root)

    result = invoke_designator(tmp_path)

    assert result.returncode == EXIT_FATAL
    assert "artifact input" in result.stderr
    assert page["payload"]["image_path"] in result.stderr
    assert "Traceback" not in result.stderr
    assert snapshot(tree.root) == before


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
    before = snapshot(tree.root)

    result = invoke_designator(tmp_path, "refused-page")

    assert result.returncode == EXIT_FATAL
    assert "artifact input" in result.stderr
    assert admission_path in result.stderr
    assert snapshot(tree.root) == before


def test_a_page_outcome_missing_from_the_exemplar_stops_before_any_act_is_cut(
    tmp_path, rebind_stage_seal, rewitness_boundary
):
    """The reconciliation branch at `common/exemplar_boundary.py`, previously
    untested: reaching it means removing both the page artifact and the
    seal's reference to it (a producer bug with nothing dangling to notice),
    not just the artifact alone. The refusal names ordinals, never a submitted
    filename, per the data-handling policy's logging rule.
    """
    tree = populated_run(tmp_path)
    page = next(
        entry
        for entry in tree.build_manifest(EXEMPLAR)["artifacts"]
        if entry["kind"] == "page"
        and tree.read_artifact(EXEMPLAR, "page", entry["artifact_id"])["payload"]["ordinal"] == 2
    )
    seal_id = artifact_id(EXEMPLAR, "seal", "corpus-seal")
    seal_path = tree.resolve(tree.artifact_path(EXEMPLAR, "seal", seal_id))
    seal = json.loads(seal_path.read_text(encoding="utf-8"))
    seal["inputs"] = [
        reference
        for reference in seal["inputs"]
        if reference["relative_path"] != page["relative_path"]
    ]
    seal["self_hash"] = self_hash(seal)
    seal_path.write_bytes(canonical_bytes(seal))
    tree.resolve(page["relative_path"]).unlink()
    rebind_stage_seal(tree, EXEMPLAR)
    # Also remove the Ink Map's own accounting of the lost page, so no stage
    # accounts for it and the Designator reaches census reconciliation rather
    # than an upstream dangling-input refusal.
    removed = 0
    for entry in list(tree.build_manifest(INK_MAP, verify_inputs=False)["artifacts"]):
        artifact_file = tree.resolve(entry["relative_path"])
        record = json.loads(artifact_file.read_bytes())
        if any(
            reference["relative_path"] == page["relative_path"]
            for reference in record.get("inputs", [])
        ):
            artifact_file.unlink()
            removed += 1
    assert removed == 1, "no ink-map record bound the lost page by its artifact path"
    tree.write_manifest(INK_MAP)
    rewitness_boundary(tree, INK_MAP)
    before = snapshot(tree.root)

    result = invoke_designator(tmp_path)
    assert result.returncode == EXIT_FATAL
    assert "lost submitted page ordinal(s) [2]" in result.stderr
    assert "page-2.png" not in result.stderr
    assert snapshot(tree.root) == before


def test_a_sealed_pixel_blob_tampered_after_the_upfront_check_is_still_caught(tmp_path):
    """The upfront boundary check runs once; this proves a page's bytes
    changing on disk after that check but before this page's own later read
    is still caught.
    """
    root = tmp_path / "runs"
    for program in programs_through("ink-map"):
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / program),
                "--run-root",
                str(root),
                "--run-id",
                "r",
                "--scenario",
                "happy",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{program}: {result.stderr}"

    designator = load_stage("2_designator")
    args = stage_parser("toctou acceptance").parse_args(
        ["--run-root", str(root), "--run-id", "r", "--scenario", "happy"]
    )
    context = open_context(args, designator.DESIGNATOR)

    records = designator.page_records(context)
    pages = designator.sealed_pages(records)
    page_record = pages[1]

    blob_path = context.tree.resolve(page_record["payload"]["image_path"])
    with Image.open(BytesIO(blob_path.read_bytes())) as image:
        changed = image.copy()
        original = changed.getpixel((0, 0))
        changed.putpixel((0, 0), 0 if original else 255)
        output = BytesIO()
        changed.save(output, format="PNG")
    blob_path.write_bytes(output.getvalue())

    with pytest.raises(ContractError, match="no longer matches its recorded digest"):
        designator.page_pixels(
            context,
            page_record,
            grouping_policy=designator.grouping_config.load_grouping_config(),
        )
