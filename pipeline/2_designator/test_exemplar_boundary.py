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

import pytest
from _test_support import load_designator
from PIL import Image

from common.contracts.canonical import canonical_bytes, self_hash
from common.contracts.errors import ContractError
from common.contracts.identities import artifact_id
from common.contracts.stages import EXEMPLAR
from common.runtree.store import RunTree
from common.stage import EXIT_FATAL, EXIT_HELD, open_context, stage_parser

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


def test_missing_exemplar_page_stops_at_the_first_downstream_boundary(tmp_path):
    tree = populated_run(tmp_path)
    entry = next(
        entry
        for entry in tree.build_manifest(EXEMPLAR)["artifacts"]
        if entry["kind"] == "page"
        and tree.read_artifact(EXEMPLAR, "page", entry["artifact_id"])["payload"]["ordinal"] == 2
    )
    tree.resolve(entry["relative_path"]).unlink()
    before = snapshot(tree.root)

    result = invoke_designator(tmp_path)
    assert result.returncode == EXIT_FATAL
    assert "artifact input" in result.stderr
    assert entry["relative_path"] in result.stderr
    assert snapshot(tree.root) == before


def test_a_tampered_corpus_seal_stops_before_the_designator_reads_any_page(tmp_path):
    tree = populated_run(tmp_path)
    identity = artifact_id(EXEMPLAR, "seal", "corpus-seal")
    path = tree.resolve(tree.artifact_path(EXEMPLAR, "seal", identity))
    record = json.loads(path.read_text(encoding="utf-8"))
    record["payload"]["pages"][0]["declared_path"] = "wrong-name.png"
    record["self_hash"] = self_hash(record)
    path.write_bytes(canonical_bytes(record))
    tree.write_manifest(EXEMPLAR)
    # Captured after `write_manifest`, which mutates the tree itself. This is the
    # assertion that actually proves the module docstring's claim — that the
    # Designator stops *before publishing any new proposal* — and without it a
    # regression that refused only after writing a region stayed green here while
    # the four neighbouring tests caught it.
    before = snapshot(tree.root)

    result = invoke_designator(tmp_path)
    assert result.returncode == EXIT_FATAL
    assert "valid self-hashed census" in result.stderr
    assert snapshot(tree.root) == before


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
        changed = image.copy()
        original = changed.getpixel((0, 0))
        changed.putpixel((0, 0), 0 if original else 255)
        output = BytesIO()
        changed.save(output, format="PNG")
    blob_path.write_bytes(output.getvalue())
    before = snapshot(tree.root)

    result = invoke_designator(tmp_path)

    assert result.returncode == EXIT_FATAL
    assert "changed under a sealed reference" in result.stderr
    assert page["payload"]["image_path"] in result.stderr
    assert snapshot(tree.root) == before


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


def test_a_page_outcome_missing_from_the_exemplar_stops_before_any_act_is_cut(tmp_path):
    """The reconciliation branch at `common/exemplar_boundary.py`, which had no test
    at all: replacing its condition with `if False` left the whole suite green.

    Deleting the page artifact alone does not reach it — the corpus seal still
    inputs that artifact, so the run tree refuses at the input-reference check one
    layer earlier, which is what the neighbouring test exercises. Reaching this
    branch means removing the page *and* the seal's reference to it, which is
    exactly the shape a producer bug would leave behind: an Exemplar that no longer
    accounts for a page `run.json` says arrived, with nothing dangling to notice.

    The refusal names ordinals rather than submitted filenames on purpose. Every
    ContractError reaches stderr through `run_stage`, and the data-handling
    policy's logging rule keeps a declared path out of that channel — the same
    reason `operations/submit/inventory.py` names its refusals by entry. The
    message used to interpolate `relative_path`, and nothing tested it either way.
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
    before = snapshot(tree.root)

    result = invoke_designator(tmp_path)
    assert result.returncode == EXIT_FATAL
    assert "lost submitted page ordinal(s) [2]" in result.stderr
    assert "page-2.png" not in result.stderr
    assert snapshot(tree.root) == before


def _load_designator():
    return load_designator("designator_toctou_under_test")


def test_a_sealed_pixel_blob_tampered_after_the_upfront_check_is_still_caught(tmp_path):
    """The upfront boundary check (`page_records`, proven above) runs once, before
    the first region is cut. This proves the narrower claim that check alone
    cannot: a page's bytes changing on disk *after* that one-time check has
    already passed, but before this specific page's own later read (the
    structure scan, a crop), is still caught -- rather than being silently
    baked into sealed Designator evidence on the strength of a check that
    already happened.
    """
    root = tmp_path / "runs"
    for program in ("pipeline/1_exemplar/door.py", "pipeline/1_exemplar/run.py"):
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

    designator = _load_designator()
    args = stage_parser("toctou acceptance").parse_args(
        ["--run-root", str(root), "--run-id", "r", "--scenario", "happy"]
    )
    context = open_context(args, designator.DESIGNATOR)

    # The upfront check: passes, exactly as it does at the top of a real run.
    records = designator.page_records(context)
    pages = designator.sealed_pages(records)
    page_record = pages[1]

    # Tamper the same page's blob *after* that check already passed.
    blob_path = context.tree.resolve(page_record["payload"]["image_path"])
    with Image.open(BytesIO(blob_path.read_bytes())) as image:
        changed = image.copy()
        original = changed.getpixel((0, 0))
        changed.putpixel((0, 0), 0 if original else 255)
        output = BytesIO()
        changed.save(output, format="PNG")
    blob_path.write_bytes(output.getvalue())

    with pytest.raises(ContractError, match="no longer matches its recorded digest"):
        designator.page_pixels(context, page_record)
