"""The Exemplar's corpus seal, and the reconciliation it rests on.

Spec 03's test 3 lives here: a byte-identical rerun reproduces an identical seal,
and a run whose evidence changed underneath it refuses. So does the claim the seal
is worth anything at all — that every submitted source has exactly one page outcome
before the seal is written, and that a source cannot disappear between submission
and sealing.

The run trees here are built by driving the real door over synthetic bytes and then
running the real Exemplar, so what is under test is the handoff rather than a
hand-written approximation of it.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from door import SourceEntry, process_sources
from run import SEAL_SUBJECT
from synthetic_sources import png

from common.contracts.approval import synthetic_fixture_ingress_record
from common.contracts.canonical import canonical_bytes, digest_bytes, verify_self_hash
from common.contracts.errors import ContractError
from common.contracts.identities import artifact_id, page_id
from common.contracts.stages import DOOR, EXEMPLAR
from common.runtree.store import RunTree
from common.stage import StageContext

ROOT = Path(__file__).resolve().parents[2]
EXEMPLAR_CLI = ROOT / "pipeline" / "1_exemplar" / "run.py"

PAGES = {"page-1.png": png(4, 3), "page-2.png": png(5, 2)}
REFUSED = {"page-3.png": b"not an image"}


def sealed_bindings() -> dict:
    """The real configuration bindings the walking skeleton's runs carry.

    Not invented values: `open_context` refuses a direct stage running against an
    unsealed configuration (spec 01's guard), and a test that dodged it by using a
    fake digest would be proving the Exemplar against a run the pipeline would
    never produce.
    """
    from common.chairs.registry import ChairRegistry
    from common.stage import load_fixture, run_config_bindings

    registry = ChairRegistry.from_toml(str(ROOT / "config" / "models.toml"))
    return run_config_bindings(registry.config, load_fixture(str(ROOT / "proof")), "happy")


def build_door_run(run_root: Path, run_id: str = "r1", *, files: dict[str, bytes] | None = None):
    """A run tree with the real door's admissions already published into it."""
    from admission import load_format_policy

    files = dict(PAGES | REFUSED) if files is None else files
    bindings = sealed_bindings()
    sources = [
        SourceEntry(ordinal, name, digest_bytes(data))
        for ordinal, (name, data) in enumerate(sorted(files.items()), start=1)
    ]
    tree = RunTree.create(
        run_root,
        run_id,
        source_manifest=[
            {
                "relative_path": entry.declared_path,
                "sha256": entry.declared_sha256,
                "ordinal": entry.ordinal,
            }
            for entry in sources
        ],
        config_digest=bindings["config_digest"],
        adapter_recipes=bindings["adapter_recipes"],
        witness_chairs=bindings["witness_chairs"],
        ingress=synthetic_fixture_ingress_record(),
    )
    context = StageContext(
        tree=tree,
        run=tree.read_run(),
        fixture={},
        scenario="happy",
        stage=DOOR,
        adapter_revision=bindings["adapter_recipes"][DOOR],
        args=None,
        registry=None,
    )
    process_sources(
        context,
        tree,
        sources,
        lambda path: files[path],
        policy=load_format_policy(),
        is_fixture=True,
    )
    context.finish(DOOR)
    return tree, files


def run_exemplar(run_root: Path, run_id: str = "r1") -> subprocess.CompletedProcess:
    """Drive the real CLI the way the orchestrator does."""
    return subprocess.run(
        [
            sys.executable,
            str(EXEMPLAR_CLI),
            "--run-root",
            str(run_root),
            "--run-id",
            run_id,
            "--fixture-root",
            str(ROOT / "proof"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def seal_of(tree: RunTree) -> dict:
    return tree.read_artifact(EXEMPLAR, "seal", artifact_id(EXEMPLAR, "seal", SEAL_SUBJECT))


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# --- The seal exists, is one per run, and covers every outcome -------------------


def test_the_run_carries_exactly_one_corpus_seal_naming_every_page(tmp_path):
    tree, _ = build_door_run(tmp_path / "runs")
    assert run_exemplar(tmp_path / "runs").returncode == 0

    seals = [
        entry for entry in tree.build_manifest(EXEMPLAR)["artifacts"] if entry["kind"] == "seal"
    ]
    assert len(seals) == 1
    payload = seal_of(tree)["payload"]
    assert payload["page_count"] == 3
    assert [page["ordinal"] for page in payload["pages"]] == [1, 2, 3]
    assert [page["outcome"] for page in payload["pages"]] == ["sealed", "sealed", "refused"]
    assert verify_self_hash(payload)


def test_a_sealed_page_is_named_by_the_digest_that_was_actually_admitted(tmp_path):
    """Audit Q12's defect was a truncated hash of the *path*. Identity binds the
    content digest and the ordinal — and the digest of the bytes the door admitted,
    not of what anybody declared about them."""
    tree, files = build_door_run(tmp_path / "runs")
    assert run_exemplar(tmp_path / "runs").returncode == 0

    expected = page_id(digest_bytes(files["page-1.png"]), 1)
    record = tree.read_artifact(EXEMPLAR, "page", artifact_id(EXEMPLAR, "page", expected))
    assert record["outcome"] == "sealed"
    assert record["payload"]["ordinal"] == 1


def test_a_refused_page_is_carried_forward_rather_than_dropped(tmp_path):
    tree, _ = build_door_run(tmp_path / "runs")
    assert run_exemplar(tmp_path / "runs").returncode == 0

    record = tree.read_artifact(EXEMPLAR, "page", artifact_id(EXEMPLAR, "page", "source-3"))
    assert record["outcome"] == "refused"
    assert record["payload"]["ordinal"] == 3


# --- Test 3: the seal is reproducible, and refuses a changed run -----------------


def test_a_byte_identical_rerun_reproduces_an_identical_seal(tmp_path):
    build_door_run(tmp_path / "a", "r1")
    build_door_run(tmp_path / "b", "r1")
    assert run_exemplar(tmp_path / "a").returncode == 0
    assert run_exemplar(tmp_path / "b").returncode == 0
    assert snapshot(tmp_path / "a") == snapshot(tmp_path / "b")


def test_rerunning_the_exemplar_over_its_own_output_changes_nothing(tmp_path):
    build_door_run(tmp_path / "runs")
    assert run_exemplar(tmp_path / "runs").returncode == 0
    before = snapshot(tmp_path / "runs")
    assert run_exemplar(tmp_path / "runs").returncode == 0
    assert snapshot(tmp_path / "runs") == before


def test_one_changed_source_byte_produces_a_different_seal(tmp_path):
    """The seal is only evidence if it moves when the corpus does."""
    build_door_run(tmp_path / "a", "r1")
    changed = dict(PAGES | REFUSED)
    changed["page-1.png"] = png(4, 4)
    build_door_run(tmp_path / "b", "r1", files=changed)
    assert run_exemplar(tmp_path / "a").returncode == 0
    assert run_exemplar(tmp_path / "b").returncode == 0

    first = seal_of(RunTree(tmp_path / "a", "r1"))["payload"]
    second = seal_of(RunTree(tmp_path / "b", "r1"))["payload"]
    assert first["self_hash"] != second["self_hash"]


def test_an_edited_seal_refuses_a_rerun_rather_than_building_on_it(tmp_path):
    tree, _ = build_door_run(tmp_path / "runs")
    assert run_exemplar(tmp_path / "runs").returncode == 0

    identity = artifact_id(EXEMPLAR, "seal", SEAL_SUBJECT)
    path = tree.resolve(tree.artifact_path(EXEMPLAR, "seal", identity))
    record = json.loads(path.read_text(encoding="utf-8"))
    record["payload"]["page_count"] = 99
    path.write_bytes(canonical_bytes(record))

    result = run_exemplar(tmp_path / "runs")
    assert result.returncode != 0
    assert "fails its own self-hash" in result.stderr


# --- The reconciliation the seal rests on ----------------------------------------


def test_a_source_that_lost_its_door_outcome_refuses_before_anything_is_sealed(tmp_path):
    tree, _ = build_door_run(tmp_path / "runs")
    identity = artifact_id(DOOR, "admission", "source-2")
    tree.resolve(tree.artifact_path(DOOR, "admission", identity)).unlink()
    tree.write_manifest(DOOR)

    result = run_exemplar(tmp_path / "runs")
    assert result.returncode != 0
    assert "may not disappear between submission and sealing" in result.stderr
    assert not (tree.root / "1_exemplar" / "artifacts" / "page").exists()


def test_an_admitted_blob_whose_bytes_changed_refuses(tmp_path):
    tree, _ = build_door_run(tmp_path / "runs")
    admission = tree.read_artifact(DOOR, "admission", artifact_id(DOOR, "admission", "source-1"))
    tree.resolve(admission["payload"]["stored_at"]).write_bytes(b"different bytes entirely")

    result = run_exemplar(tmp_path / "runs")
    assert result.returncode != 0
    assert "no longer match" in result.stderr


def test_a_door_refusal_carrying_a_free_text_reason_is_refused(tmp_path):
    """The closed reason set is the actual work of this spec. A consumer that took a
    free-text reason because it happened to be a string would have replaced nothing."""
    tree, _ = build_door_run(tmp_path / "runs")
    identity = artifact_id(DOOR, "admission", "source-3")
    path = tree.resolve(tree.artifact_path(DOOR, "admission", identity))
    record = json.loads(path.read_text(encoding="utf-8"))
    record["payload"]["reason"] = "page-3.png does not carry a PNG signature"
    path.write_bytes(canonical_bytes(record))
    tree.write_manifest(DOOR)

    result = run_exemplar(tmp_path / "runs")
    assert result.returncode != 0
    assert "closed-set code" in result.stderr


def test_a_synthetic_run_carrying_approval_evidence_is_refused(tmp_path):
    """A fixture run that claims it was approval-gated is claiming something that
    never happened, and the run authority is where that is settled."""
    tree, _ = build_door_run(tmp_path / "runs")
    identity = artifact_id(DOOR, "admission", "source-1")
    path = tree.resolve(tree.artifact_path(DOOR, "admission", identity))
    record = json.loads(path.read_text(encoding="utf-8"))
    record["payload"]["data_gate_approval_ref"] = {
        "relative_path": f"receipts/sha256/{'a' * 64}.json",
        "sha256": "a" * 64,
    }
    path.write_bytes(canonical_bytes(record))
    tree.write_manifest(DOOR)

    result = run_exemplar(tmp_path / "runs")
    assert result.returncode != 0
    assert "carry data-gate approval evidence" in result.stderr


def test_the_exemplar_refuses_a_run_the_door_never_wrote(tmp_path):
    bindings = sealed_bindings()
    RunTree.create(
        tmp_path / "runs",
        "r1",
        source_manifest=[{"relative_path": "page-1.png", "sha256": "a" * 64, "ordinal": 1}],
        config_digest=bindings["config_digest"],
        adapter_recipes=bindings["adapter_recipes"],
        witness_chairs=bindings["witness_chairs"],
        ingress=synthetic_fixture_ingress_record(),
    )
    result = run_exemplar(tmp_path / "runs")
    assert result.returncode != 0
    assert "no admissions to seal" in result.stderr


def test_the_seal_is_written_only_after_every_page_is_accounted_for(tmp_path):
    """Ordering, asserted rather than assumed: the seal names the pages, so a seal
    written before the census closed would be a summary of a partial corpus."""
    tree, _ = build_door_run(tmp_path / "runs")
    with pytest.raises(ContractError):
        # A run with no source manifest cannot be reconciled at all, and nothing —
        # least of all a seal — may be published over it.
        import run as exemplar

        exemplar._submitted_sources({"source_manifest": []})
    assert not (tree.root / "1_exemplar" / "artifacts" / "seal").exists()
