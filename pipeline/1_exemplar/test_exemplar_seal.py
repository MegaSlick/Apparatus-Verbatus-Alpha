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

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
from door import SourceEntry, process_sources
from synthetic_sources import png

from common.contracts.approval import real_ingress_record, synthetic_fixture_ingress_record
from common.contracts.canonical import canonical_bytes, digest_bytes, self_hash, verify_self_hash
from common.contracts.errors import ContractError
from common.contracts.identities import artifact_id, page_id, physical_page_id
from common.contracts.stages import DOOR, EXEMPLAR
from common.exemplar_boundary import verify_sealed_page_pixels
from common.runtree.store import RunTree
from common.stage import EXIT_FATAL, StageContext
from operations.submit import submit

ROOT = Path(__file__).resolve().parents[2]
EXEMPLAR_CLI = ROOT / "pipeline" / "1_exemplar" / "run.py"


def _load_exemplar_run():
    """Load this directory's ``run.py`` under an unambiguous module name.

    Every pipeline stage has a top-level module called ``run``.  A bare import
    therefore returns whichever stage pytest happened to collect first, making
    this module pass or fail with test ordering instead of the code under test.
    """
    spec = importlib.util.spec_from_file_location("exemplar_run_under_test", EXEMPLAR_CLI)
    module = importlib.util.module_from_spec(spec)
    original_path = list(sys.path)
    try:
        sys.path.insert(0, str(EXEMPLAR_CLI.parent))
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = original_path
    return module


_EXEMPLAR_RUN = _load_exemplar_run()
SEAL_SUBJECT = _EXEMPLAR_RUN.SEAL_SUBJECT
_page_payload = _EXEMPLAR_RUN._page_payload

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


def build_door_run(
    run_root: Path,
    run_id: str = "r1",
    *,
    files: dict[str, bytes] | None = None,
    register_bytes: bytes | None = None,
):
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
        register_bytes=register_bytes,
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
    )
    context.seal_boundary()
    context.finish(DOOR)
    return tree, files


def run_exemplar(
    run_root: Path, run_id: str = "r1", *, corpus_register: str | None = None
) -> subprocess.CompletedProcess:
    """Drive the real CLI the way the orchestrator does."""
    args = [
        sys.executable,
        str(EXEMPLAR_CLI),
        "--run-root",
        str(run_root),
        "--run-id",
        run_id,
        "--fixture-root",
        str(ROOT / "proof"),
    ]
    if corpus_register is not None:
        args += ["--corpus-register", corpus_register]
    return subprocess.run(args, cwd=ROOT, capture_output=True, text=True)


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
    assert [page["declared_path"] for page in payload["pages"]] == [
        "page-1.png",
        "page-2.png",
        "page-3.png",
    ]
    assert verify_self_hash(payload)


def test_a_sealed_page_is_named_by_the_digest_that_was_actually_admitted(tmp_path):
    """Audit Q12's defect was a truncated hash of the *path*. Identity binds the
    immutable source digest and whole-image transform — and the digest of the bytes
    the door admitted, not the submission ordinal or what anybody declared."""
    tree, files = build_door_run(tmp_path / "runs")
    assert run_exemplar(tmp_path / "runs").returncode == 0

    expected = page_id(
        {"kind": "source", "sha256": digest_bytes(files["page-1.png"])},
        {"operation": "whole"},
    )
    record = tree.read_artifact(EXEMPLAR, "page", artifact_id(EXEMPLAR, "page", expected))
    assert record["outcome"] == "sealed"
    assert record["payload"]["ordinal"] == 1


def test_loading_the_exemplar_run_module_does_not_change_import_search_order():
    before = list(sys.path)
    _load_exemplar_run()
    assert sys.path == before


def test_a_pdf_page_rendered_below_its_run_target_says_so_in_the_sealed_page_record():
    """The provisional per-page cap is visible until Tyrel settles ruling 14.

    This is deliberately a page payload rather than a corpus-seal field: the
    existing cross-stage corpus-seal contract closes its census-row schema in
    ``common/exemplar_boundary.py``. The immutable page record is the existing
    place downstream consumers receive the full render contract, and the explicit
    projection makes an under-target render visible without silently changing the
    unresolved DPI behaviour.
    """
    source = {
        "relative_path": "large-plan.pdf",
        "sha256": "a" * 64,
        "container_page_index": 0,
    }
    payload = {
        "sha256": "b" * 64,
        "stored_at": "blobs/sha256/bb/bb" + "b" * 60,
        "rendered_from": {
            "container_format": "pdf",
            "container_sha256": "a" * 64,
            "container_page_index": 0,
            "render_contract": {
                "configured_target_dpi": 400,
                "dpi": 400,
                "effective_dpi": 119,
            },
        },
    }

    sealed = _page_payload(payload, 1, source)

    assert sealed["render_resolution"] == {
        "configured_target_dpi": 400,
        "resolved_target_dpi": 400,
        "effective_dpi": 119,
        "below_resolved_target": True,
        "shortfall_dpi": 281,
    }


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
    assert "fails its self-hash" in result.stderr


# --- The reconciliation the seal rests on ----------------------------------------


def test_a_source_that_lost_its_door_outcome_refuses_before_anything_is_sealed(
    tmp_path, rebind_stage_seal
):
    tree, _ = build_door_run(tmp_path / "runs")
    identity = artifact_id(DOOR, "admission", "source-2")
    tree.resolve(tree.artifact_path(DOOR, "admission", identity)).unlink()
    tree.write_manifest(DOOR)
    rebind_stage_seal(tree, DOOR)

    result = run_exemplar(tmp_path / "runs")
    assert result.returncode != 0
    assert "may not disappear between submission and sealing" in result.stderr
    assert "page-2.png" not in result.stderr
    assert not (tree.root / "1_exemplar" / "artifacts" / "page").exists()
    # The ordering claim, on a run that actually reached the Exemplar and refused:
    # the seal names the pages, so a seal written before the census closed would be
    # a summary of a partial corpus. Asserted here rather than in a door-only test,
    # where "no seal exists" is true whatever the Exemplar does.
    assert not (tree.root / "1_exemplar" / "artifacts" / "seal").exists()


def test_an_admitted_blob_whose_bytes_changed_refuses(tmp_path, rebind_stage_seal):
    tree, _ = build_door_run(tmp_path / "runs")
    admission = tree.read_artifact(DOOR, "admission", artifact_id(DOOR, "admission", "source-1"))
    tree.resolve(admission["payload"]["stored_at"]).write_bytes(b"different bytes entirely")
    rebind_stage_seal(tree, DOOR, rewrite_manifest=False)

    result = run_exemplar(tmp_path / "runs")
    assert result.returncode != 0
    assert "changed under a sealed reference" in result.stderr


def test_an_admitted_blob_that_is_gone_refuses_by_name_rather_than_crashing(
    tmp_path, rebind_stage_seal
):
    """The *deleted* blob, beside the *changed* one above. It escaped as a
    FileNotFoundError traceback and CPython's exit 1, where `common/stage.py` says
    an exit code carries cause and reserves 2 for a named contract failure. A
    stage that dies by traceback has still failed loudly — but it has told the
    orchestrator "something went wrong" instead of "this run does not reconcile"."""
    tree, _ = build_door_run(tmp_path / "runs")
    admission = tree.read_artifact(DOOR, "admission", artifact_id(DOOR, "admission", "source-1"))
    tree.resolve(admission["payload"]["stored_at"]).unlink()
    rebind_stage_seal(tree, DOOR, rewrite_manifest=False)

    result = run_exemplar(tmp_path / "runs")
    assert result.returncode == EXIT_FATAL
    assert "Traceback" not in result.stderr
    assert "artifact input" in result.stderr
    assert admission["payload"]["stored_at"] in result.stderr
    assert not (tree.root / "1_exemplar" / "artifacts" / "seal").exists()


def test_a_door_refusal_carrying_a_free_text_reason_is_refused(tmp_path, rebind_stage_seal):
    """The closed reason set is the actual work of this spec. A consumer that took a
    free-text reason because it happened to be a string would have replaced nothing."""
    tree, _ = build_door_run(tmp_path / "runs")
    identity = artifact_id(DOOR, "admission", "source-3")
    path = tree.resolve(tree.artifact_path(DOOR, "admission", identity))
    record = json.loads(path.read_text(encoding="utf-8"))
    record["payload"]["reason"] = "page-3.png does not carry a PNG signature"
    record["self_hash"] = self_hash(record)
    path.write_bytes(canonical_bytes(record))
    tree.write_manifest(DOOR)
    rebind_stage_seal(tree, DOOR)

    result = run_exemplar(tmp_path / "runs")
    assert result.returncode != 0
    assert "closed-set code" in result.stderr


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
    # A missing Door boundary must refuse before Exemplar examines admissions;
    # the next test uses a coherent empty boundary to reach the admission check.
    assert "predecessor door has no stage-seal" in result.stderr


def test_the_exemplar_refuses_a_sealed_door_boundary_that_admitted_nothing(
    tmp_path, rebind_stage_seal
):
    """A coherent empty Door boundary must reach the admission refusal.

    A missing boundary stops at the earlier completion-seal refusal, so it cannot
    exercise this distinct check.
    """
    tree, _ = build_door_run(tmp_path / "runs")
    admissions = [
        entry for entry in tree.build_manifest(DOOR)["artifacts"] if entry["kind"] == "admission"
    ]
    assert admissions, "the door fixture published no admission to remove"
    for entry in admissions:
        tree.resolve(entry["relative_path"]).unlink()
    tree.write_manifest(DOOR)
    rebind_stage_seal(tree, DOOR)

    result = run_exemplar(tmp_path / "runs")
    assert result.returncode != 0
    assert "no admissions to seal" in result.stderr
    assert not (tree.root / "1_exemplar" / "artifacts" / "seal").exists()


def test_a_run_with_no_submitted_source_manifest_cannot_be_reconciled_at_all():
    """This used to close with "no seal artifact exists" over a tree only the *door*
    had written, and call that an ordering assertion. It was true before the test
    body ran and stayed true against an Exemplar mutated to publish the seal first —
    a guard nobody has seen fail. The ordering claim now lives in
    `test_a_source_that_lost_its_door_outcome_refuses_before_anything_is_sealed`,
    which actually runs the Exemplar; what is left here is the one thing this test
    really exercised, said plainly."""
    with pytest.raises(ContractError, match="no submitted source manifest"):
        _EXEMPLAR_RUN._submitted_sources({"source_manifest": []})


def test_a_real_run_source_manifest_reconstructs_its_self_hashed_filename_ledger():
    """The run authority retains the original-name/digest ledger, not a second list."""
    files = [
        {"relative_path": "FS-101.png", "sha256": "1" * 64, "bytes": 123},
        {"relative_path": "volume/FS-102.pdf", "sha256": "2" * 64, "bytes": 456},
    ]
    ledger = submit.build_manifest(files)
    run = {
        "ingress": real_ingress_record(),
        "source_manifest": [
            {
                **files[0],
                "ordinal": 1,
                "ledger_sha256": ledger["self_hash"],
                "container_page_index": None,
            },
            {
                **files[1],
                "ordinal": 2,
                "ledger_sha256": ledger["self_hash"],
                "container_page_index": 0,
            },
            {
                **files[1],
                "ordinal": 3,
                "ledger_sha256": ledger["self_hash"],
                "container_page_index": 1,
            },
        ],
    }
    assert _EXEMPLAR_RUN._submitted_sources(run)[3]["relative_path"] == "volume/FS-102.pdf"
    run["source_manifest"][2]["bytes"] += 1
    with pytest.raises(ContractError, match="incompatible digest or byte-count"):
        _EXEMPLAR_RUN._submitted_sources(run)


def test_a_fabricated_render_transform_is_refused_rather_than_sealed_or_crashed(
    tmp_path, rebind_stage_seal
):
    """A standalone raster cannot claim a partial container render explanation."""
    tree, _ = build_door_run(tmp_path / "runs")
    identity = artifact_id(DOOR, "admission", "source-1")
    path = tree.resolve(tree.artifact_path(DOOR, "admission", identity))
    record = json.loads(path.read_text(encoding="utf-8"))
    record["payload"]["rendered_from"] = {"container_page_index": 0}
    record["self_hash"] = self_hash(record)
    path.write_bytes(canonical_bytes(record))
    tree.write_manifest(DOOR)
    rebind_stage_seal(tree, DOOR)

    result = run_exemplar(tmp_path / "runs")
    assert result.returncode == EXIT_FATAL
    assert "Traceback" not in result.stderr
    assert "does not carry exactly" in result.stderr
    assert not (tree.root / "1_exemplar" / "artifacts" / "seal").exists()


def test_a_malformed_render_origin_in_a_sealed_page_is_a_named_refusal(tmp_path):
    tree, _ = build_door_run(tmp_path / "runs")
    result = run_exemplar(tmp_path / "runs")
    assert result.returncode == 0, result.stderr
    run = tree.read_run()
    entry = next(
        row
        for row in tree.build_manifest(EXEMPLAR)["artifacts"]
        if row["kind"] == "page" and row["outcome"] == "sealed"
    )
    page = tree.read_artifact(EXEMPLAR, "page", entry["artifact_id"])
    source = next(
        row for row in run["source_manifest"] if row["ordinal"] == page["payload"]["ordinal"]
    )
    page["payload"]["rendered_from"] = {"container_page_index": 0}

    with pytest.raises(ContractError, match="complete closed container render record"):
        verify_sealed_page_pixels(tree, run, source, page)


def test_a_fanned_source_cannot_seal_raw_container_bytes_without_a_render_transform(tmp_path):
    """A page ordinal from a container is never permission to pass its raw bytes on."""
    tree, _ = build_door_run(tmp_path / "runs")
    admission = tree.read_artifact(DOOR, "admission", artifact_id(DOOR, "admission", "source-1"))
    source = {
        "ordinal": 1,
        "relative_path": admission["payload"]["declared_path"],
        "sha256": admission["payload"]["declared_sha256"],
        "container_page_index": 0,
    }
    with pytest.raises(ContractError, match="fanned source page must carry the render transform"):
        _EXEMPLAR_RUN._verify_admitted_blob(tree, tree.read_run(), admission, source)


# --- The corpus register's sealed snapshot cannot drift under a run ---------------


def test_a_register_that_drifted_since_run_creation_refuses_before_anything_seals(tmp_path):
    """A register append between two stage invocations must refuse by name --
    never be read live and silently change what a run resolves against."""
    from common.corpus_register import empty_register

    register_path = tmp_path / "register.json"
    register_path.write_bytes(empty_register())
    tree, _ = build_door_run(tmp_path / "runs", register_bytes=register_path.read_bytes())

    first = run_exemplar(tmp_path / "runs", corpus_register=str(register_path))
    assert first.returncode == 0, first.stderr
    before = snapshot(tmp_path / "runs")

    drifted = canonical_bytes(
        {
            "schema": "corpus-register-v1",
            "records": [
                {
                    "kind": "physical-page",
                    "corpus_id": "corpus",
                    "volume_id": "volume-1",
                    "designation": "12r",
                    "physical_page_id": physical_page_id("corpus", "volume-1", "12r"),
                    "appending_run": "triage-1",
                }
            ],
        }
    )
    register_path.write_bytes(drifted)

    second = run_exemplar(tmp_path / "runs", corpus_register=str(register_path))
    assert second.returncode == EXIT_FATAL
    assert "the corpus register changed" in second.stderr
    assert snapshot(tmp_path / "runs") == before, "a refused stage may not write anything"


def test_a_register_naming_the_run_creation_snapshot_is_accepted_unchanged(tmp_path):
    """The sealed snapshot itself, re-read from disk, is never mistaken for drift."""
    from common.corpus_register import empty_register

    register_path = tmp_path / "register.json"
    register_path.write_bytes(empty_register())
    build_door_run(tmp_path / "runs", register_bytes=register_path.read_bytes())

    result = run_exemplar(tmp_path / "runs", corpus_register=str(register_path))
    assert result.returncode == 0, result.stderr


# --- Byte-identical duplicate sources are one page, not a collision ---------------


def test_two_byte_identical_submitted_sources_seal_as_one_page_naming_both_rows(tmp_path):
    """Digest-primary identity makes identical submissions one cited page.

    Two submission rows over identical bytes derive one page_id, so the
    Exemplar must merge their citations before publishing the immutable page
    artifact."""
    data = png(4, 3)
    tree, _ = build_door_run(tmp_path / "runs", files={"dup-a.png": data, "dup-b.png": data})

    result = run_exemplar(tmp_path / "runs")
    assert result.returncode == 0, result.stderr

    pages = [
        entry for entry in tree.build_manifest(EXEMPLAR)["artifacts"] if entry["kind"] == "page"
    ]
    assert len(pages) == 1, "one page identity must seal as one page artifact, not one per row"
    record = tree.read_artifact(EXEMPLAR, "page", pages[0]["artifact_id"])
    submission_rows = record["payload"]["submission_rows"]
    assert sorted(row["ordinal"] for row in submission_rows) == [1, 2]
    assert sorted(row["relative_path"] for row in submission_rows) == ["dup-a.png", "dup-b.png"]
    assert {row["sha256"] for row in submission_rows} == {digest_bytes(data)}

    payload = seal_of(tree)["payload"]
    assert payload["page_count"] == 2, "the census still names one row per submitted ordinal"
    assert [page["ordinal"] for page in payload["pages"]] == [1, 2]
    assert [page["outcome"] for page in payload["pages"]] == ["sealed", "sealed"]
    assert [page["page_id"] for page in payload["pages"]] == [record["subject_id"]] * 2


def test_the_merged_page_verifies_at_the_pixel_boundary_for_each_row_it_cites(tmp_path):
    """A merged page verifies for every cited source row and no invented row.

    Every later pixel consumer relies on this boundary, so its admission check
    must cover the page's complete submitted-row set rather than assume exactly
    one Door admission.
    """
    from common.exemplar_boundary import verify_sealed_page_pixels

    data = png(4, 3)
    tree, _ = build_door_run(tmp_path / "runs", files={"dup-a.png": data, "dup-b.png": data})
    assert run_exemplar(tmp_path / "runs").returncode == 0

    run = tree.read_run()
    page = next(
        tree.read_artifact(EXEMPLAR, "page", entry["artifact_id"])
        for entry in tree.build_manifest(EXEMPLAR)["artifacts"]
        if entry["kind"] == "page"
    )
    for source in run["source_manifest"]:
        verify_sealed_page_pixels(tree, run, source, page)

    invented = dict(run["source_manifest"][0], ordinal=99)
    with pytest.raises(ContractError, match="not the page this source was sealed into"):
        verify_sealed_page_pixels(tree, run, invented, page)


def test_a_merged_page_is_refused_by_name_at_the_first_stage_that_would_read_it_twice(tmp_path):
    """Every stage behind the Exemplar keys its work by submitted ordinal and
    would mint each act on this page twice. The seal boundary refuses until
    consumers process merged pages by identity, rather than reporting the
    second row as a lost page it plainly is not."""
    from common.exemplar_boundary import verify_exemplar_corpus_seal

    data = png(4, 3)
    tree, _ = build_door_run(tmp_path / "runs", files={"dup-a.png": data, "dup-b.png": data})
    assert run_exemplar(tmp_path / "runs").returncode == 0

    run = tree.read_run()
    manifest = tree.build_manifest(EXEMPLAR)
    entry = next(item for item in manifest["artifacts"] if item["kind"] == "page")
    page = tree.read_artifact(EXEMPLAR, "page", entry["artifact_id"])
    sources = {row["ordinal"]: row for row in run["source_manifest"]}
    with pytest.raises(ContractError, match="would mint each act on it twice"):
        verify_exemplar_corpus_seal(tree, run, manifest, sources, {1: page}, {1: entry})
