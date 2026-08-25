"""`bundle.py` run as a real subprocess: the product actually leaving the pipeline.

Shelling out rather than importing, for the reason the orchestrator's own acceptance
suite does it: `bundle.py` is the program an operator runs, and calling its functions
would prove the functions work rather than that the program does.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZIP_STORED, ZipFile, ZipInfo

import pytest
from armarium_export import EXPORT_MANIFEST_NAME

from common.contracts.canonical import canonical_bytes, digest_bytes, self_hash
from common.contracts.errors import ContractError, SchemaRefusal
from common.contracts.identities import artifact_id
from common.contracts.stages import ARMARIUM
from common.runtree.store import RunTree

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"
BUNDLE = ROOT / "pipeline" / "7_armarium" / "bundle.py"


def _orchestrate(
    run_root: Path, run_id: str, scenario: str, **extra
) -> subprocess.CompletedProcess:
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
    ]
    for key, value in extra.items():
        command += [f"--{key.replace('_', '-')}", str(value)]
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True)


def _publish(run_root: Path, run_id: str, out: Path, **extra) -> subprocess.CompletedProcess:
    command = [
        sys.executable,
        str(BUNDLE),
        "--run-root",
        str(run_root),
        "--run-id",
        run_id,
        "--out",
        str(out),
    ]
    for key, value in extra.items():
        command += [f"--{key.replace('_', '-')}", str(value)]
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True)


@pytest.fixture(scope="module")
def happy_run(tmp_path_factory):
    root = tmp_path_factory.mktemp("bundle-publish-happy")
    result = _orchestrate(root, "r", "happy")
    assert result.returncode == 0, result.stderr
    return root


@pytest.fixture(scope="module")
def review_run(tmp_path_factory):
    root = tmp_path_factory.mktemp("bundle-publish-review")
    result = _orchestrate(root, "r", "review")
    assert result.returncode == 3, result.stderr
    return root


def _reseal_export_bundle(tree: RunTree, mutate) -> None:
    """Keep all seals coherent so tests reach the publisher's semantic checks."""
    export_path = tree.resolve(
        tree.artifact_path(
            ARMARIUM,
            "export",
            artifact_id(ARMARIUM, "export", "export", None),
        )
    )
    export = json.loads(export_path.read_text(encoding="utf-8"))
    old_reference = export["payload"]["bundle"]["reference"]
    with ZipFile(BytesIO(tree.read_bytes(old_reference["relative_path"]))) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    package_manifest = json.loads(members[EXPORT_MANIFEST_NAME])
    mutate(members, package_manifest)
    for row in package_manifest["members"]:
        row["sha256"] = digest_bytes(members[row["path"]])
        row["bytes"] = len(members[row["path"]])
    package_manifest["self_hash"] = self_hash(package_manifest)
    members[EXPORT_MANIFEST_NAME] = canonical_bytes(package_manifest)

    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_STORED) as archive:
        for name in [EXPORT_MANIFEST_NAME] + sorted(
            name for name in members if name != EXPORT_MANIFEST_NAME
        ):
            info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_STORED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, members[name])
    resealed = buffer.getvalue()
    resealed_digest = digest_bytes(resealed)
    resealed_relative = tree.blob_path(ARMARIUM, resealed_digest)
    tree.resolve(resealed_relative).write_bytes(resealed)
    new_reference = {"relative_path": resealed_relative, "sha256": resealed_digest}
    export["inputs"] = [
        new_reference if item == old_reference else item for item in export["inputs"]
    ]
    export["payload"]["bundle"].update(
        {
            "manifest_self_hash": package_manifest["self_hash"],
            "reference": new_reference,
            "sha256": resealed_digest,
        }
    )
    export["self_hash"] = self_hash(export)
    export_path.write_bytes(canonical_bytes(export))


def test_the_sealed_bundle_is_published_and_verifies_outside_the_run_tree(tmp_path, happy_run):
    out = tmp_path / "delivery"
    result = _publish(happy_run, "r", out)
    assert result.returncode == 0, result.stderr

    archive = out / "armarium-export.zip"
    assert archive.is_file()
    with ZipFile(archive) as opened:
        assert opened.namelist()[0] == EXPORT_MANIFEST_NAME
    # The extraction verification produced, readable without a zip tool, and holding
    # exactly the archive's members.
    extracted = out / "bundle"
    with ZipFile(archive) as opened:
        names = set(opened.namelist())
    assert {
        path.relative_to(extracted).as_posix() for path in extracted.rglob("*") if path.is_file()
    } == names
    assert digest_bytes(archive.read_bytes()) in result.stdout


def test_publication_reports_which_checks_the_clean_pass_actually_made(tmp_path, happy_run):
    """A verification that ran and one that declined to run must not read alike.

    `verify_delivered_bundle` returns both answers and `publish` used to drop them:
    the search-fold recomputation honestly declines under a different Unicode
    database, and an operator told only "published: complete" could not tell that
    from a fold that was recomputed and matched.
    """
    out = tmp_path / "delivery"
    result = _publish(happy_run, "r", out)

    assert result.returncode == 0, result.stderr
    assert "projection_identity: verified" in result.stdout
    assert "search_fold: verified" in result.stdout


def test_a_resealed_bundle_that_declines_search_fold_recomputation_is_not_published(
    tmp_path, happy_run
):
    """The real publisher must treat an unmade measurement as a refusal.

    This is a whole-file reseal, not a mocked verifier result: the package editor
    changes the Unicode-version row in the real SQLite member, refreshes the package
    inventory and self-hash, stores the resulting ZIP under its new content address,
    and updates the export artifact's reference and self-hash.  The subprocess then
    drives the same verifier and filesystem publication path an operator invokes.
    """
    root = tmp_path / "runs"
    shutil.copytree(happy_run / "r", root / "r")
    tree = RunTree(root, "r")
    tree.read_run()

    def decline_recomputation(members, _manifest):
        database = tmp_path / "acts.sqlite"
        database.write_bytes(members["acts.sqlite"])
        connection = sqlite3.connect(database)
        try:
            connection.execute(
                "UPDATE export_metadata SET value = 'resealed-different-version' "
                "WHERE key = 'unidata_version'"
            )
            connection.commit()
        finally:
            connection.close()
        members["acts.sqlite"] = database.read_bytes()

    _reseal_export_bundle(tree, decline_recomputation)

    out = tmp_path / "delivery"
    result = _publish(root, "r", out)

    assert result.returncode != 0
    assert "search-fold recomputation was not run" in result.stderr
    assert "use a verifier whose Unicode database matches" in result.stderr
    assert not out.exists()
    assert not list(tmp_path.glob(".delivery.publishing-*"))


def test_a_resealed_package_cannot_disagree_with_its_export_artifacts_run_binding(
    tmp_path, happy_run
):
    """The publisher checks package labels against the immutable run-tree records.

    This proves internal consistency, not cryptographic authenticity: a party able
    to rewrite both the export artifact and the package is outside the run tree's
    immutability contract and would require an external trust root to detect.
    """
    root = tmp_path / "runs"
    shutil.copytree(happy_run / "r", root / "r")
    tree = RunTree(root, "r")
    tree.read_run()
    _reseal_export_bundle(
        tree,
        lambda _members, manifest: manifest["run"].update(scenario="a run that never happened"),
    )

    out = tmp_path / "delivery"
    result = _publish(root, "r", out)

    assert result.returncode != 0
    assert "run binding" in result.stderr
    assert "restore the immutable run tree from an intact copy" in result.stderr
    assert not out.exists()


def test_a_resealed_export_cannot_misreport_the_verified_package_aggregate(tmp_path, happy_run):
    """The publisher must not print an envelope-only account of verified package bytes."""
    root = tmp_path / "runs"
    shutil.copytree(happy_run / "r", root / "r")
    tree = RunTree(root, "r")
    tree.read_run()
    export_path = tree.resolve(
        tree.artifact_path(
            ARMARIUM,
            "export",
            artifact_id(ARMARIUM, "export", "export", None),
        )
    )
    export = json.loads(export_path.read_text(encoding="utf-8"))
    export["payload"]["aggregate"]["status"] = "fabricated-terminal-status"
    export["self_hash"] = self_hash(export)
    export_path.write_bytes(canonical_bytes(export))

    out = tmp_path / "delivery"
    result = _publish(root, "r", out)

    assert result.returncode != 0
    assert "aggregate disagrees" in result.stderr
    assert "restore the immutable run tree from an intact copy" in result.stderr
    assert not out.exists()


def test_a_bundle_whose_formats_disagree_about_one_reading_is_never_published(
    tmp_path, happy_run, monkeypatch
):
    """GOVERNANCE 5 at the gate the product leaves by, not only at the one it was built by.

    The tampered package is internally whole -- every member digest, byte count and
    self-hash agrees -- and its manifest claims `identity_verified_across` all three
    literal formats. Only the cross-format comparison catches it, and until this the
    publish path did not make that comparison at all. Driven in-process because the
    substitution has to happen between the sealed read and the clean verification.
    """
    import bundle as bundle_module
    from armarium_export import EXPORT_MANIFEST_NAME as MANIFEST

    from common.contracts.canonical import canonical_bytes, self_hash

    tree = RunTree(happy_run, "r")
    tree.read_run()
    real_sealed_bundle = bundle_module.sealed_bundle

    def drifted(run_tree):
        data, payload = real_sealed_bundle(run_tree)
        with ZipFile(BytesIO(data)) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
        records = [json.loads(line) for line in members["acts.jsonl"].decode("utf-8").splitlines()]
        for record in records:
            if record.get("canonical_clean_text") is not None:
                record["canonical_clean_text"] = "a second, different purported reading"
                record["canonical_text_sha256"] = digest_bytes(
                    record["canonical_clean_text"].encode("utf-8")
                )
        members["acts.jsonl"] = b"".join(canonical_bytes(record) + b"\n" for record in records)
        manifest = json.loads(members[MANIFEST])
        for row in manifest["members"]:
            if row["path"] == "acts.jsonl":
                row["sha256"] = digest_bytes(members["acts.jsonl"])
                row["bytes"] = len(members["acts.jsonl"])
        manifest["self_hash"] = self_hash(manifest)
        members[MANIFEST] = canonical_bytes(manifest)
        buffer = BytesIO()
        with ZipFile(buffer, "w", compression=ZIP_STORED) as archive:
            for name in [MANIFEST] + sorted(name for name in members if name != MANIFEST):
                info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = ZIP_STORED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, members[name])
        return buffer.getvalue(), payload

    monkeypatch.setattr(bundle_module, "sealed_bundle", drifted)

    out = tmp_path / "delivery"
    with pytest.raises(SchemaRefusal, match="projection differs"):
        bundle_module.publish(tree, out)

    assert not out.exists()
    assert not list(tmp_path.glob(".delivery.publishing-*"))


def test_a_partial_run_publishes_and_says_it_is_partial(tmp_path, review_run):
    """A held act must not stop the product leaving, and must not be hidden in it."""
    out = tmp_path / "delivery"
    result = _publish(review_run, "r", out, scenario="review")
    assert result.returncode == 0, result.stderr
    assert "partial" in result.stdout
    assert (out / "armarium-export.zip").is_file()


def test_an_existing_destination_is_refused_rather_than_merged_into(tmp_path, happy_run):
    out = tmp_path / "delivery"
    assert _publish(happy_run, "r", out).returncode == 0
    before = {
        path.relative_to(out): path.read_bytes() if path.is_file() else None
        for path in out.rglob("*")
    }
    second = _publish(happy_run, "r", out)
    assert second.returncode != 0
    assert "File exists" in second.stderr
    assert "never reused or merged" in second.stderr
    after = {
        path.relative_to(out): path.read_bytes() if path.is_file() else None
        for path in out.rglob("*")
    }
    assert after == before


def test_a_nonexistence_mkdir_error_reports_the_os_reason(tmp_path, happy_run, monkeypatch):
    import bundle as bundle_module

    tree = RunTree(happy_run, "r")
    tree.read_run()
    out = tmp_path / "delivery"
    real_mkdir = Path.mkdir

    def refuse_destination(path, *args, **kwargs):
        if path == out:
            raise OSError("simulated permission denied")
        return real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", refuse_destination)

    with pytest.raises(ContractError, match="simulated permission denied") as caught:
        bundle_module.publish(tree, out)

    assert "never reused or merged" in str(caught.value)
    assert isinstance(caught.value.__cause__, OSError)
    assert not out.exists()


def test_publication_leaves_nothing_behind_when_the_run_has_no_export(tmp_path):
    """Half a delivery is worse than none: the destination must simply not appear."""
    root = tmp_path / "runs"
    assert _orchestrate(root, "r", "happy").returncode == 0
    export = next((root / "r" / "7_armarium" / "artifacts" / "export").glob("*.json"))
    export.unlink()

    out = tmp_path / "delivery"
    result = _publish(root, "r", out)
    assert result.returncode != 0
    assert "no sealed armarium/export artifact" in result.stderr
    assert not out.exists()
    assert not list(out.parent.glob(".delivery.publishing-*"))


def test_a_rename_failure_at_publish_does_not_orphan_the_staging_directory(
    tmp_path, happy_run, monkeypatch
):
    """The reservation-and-rename dance must clean up even when the rename itself fails.

    Driven in-process rather than as a subprocess (the file's usual style) because the
    failure has to be injected inside `os.replace`, which a subprocess boundary cannot
    reach from the test.
    """
    import bundle as bundle_module

    tree = RunTree(happy_run, "r")
    tree.read_run()

    def _boom(_src, _dst):
        raise OSError("simulated ENOSPC at rename")

    monkeypatch.setattr(bundle_module.os, "replace", _boom)

    out = tmp_path / "delivery"
    with pytest.raises(OSError, match="simulated ENOSPC"):
        bundle_module.publish(tree, out)

    assert not out.exists()
    assert list(tmp_path.glob(".delivery.publishing-*")) == []


def test_a_cleanup_failure_names_the_leftover_staging_directory(
    tmp_path, happy_run, monkeypatch, capsys
):
    import bundle as bundle_module

    tree = RunTree(happy_run, "r")
    tree.read_run()
    monkeypatch.setattr(
        bundle_module.os,
        "replace",
        lambda _src, _dst: (_ for _ in ()).throw(OSError("simulated rename failure")),
    )
    # tempfile shares this module's rmtree, so an unconditional patch would fail
    # verifier scratch cleanup before the publication cleanup under test.
    real_rmtree = bundle_module.shutil.rmtree

    def failing_staging_rmtree(path, *args, **kwargs):
        if ".publishing-" in str(path):
            raise OSError("simulated cleanup failure")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(bundle_module.shutil, "rmtree", failing_staging_rmtree)

    with pytest.raises(OSError, match="simulated rename failure"):
        bundle_module.publish(tree, tmp_path / "delivery")

    captured = capsys.readouterr()
    assert "warning: could not remove staging directory" in captured.err
    assert "simulated cleanup failure" in captured.err
    assert list(tmp_path.glob(".delivery.publishing-*"))


def test_a_tampered_sealed_blob_is_refused_before_anything_is_published(tmp_path, happy_run):
    """The digest the export artifact recorded is the authority, not the blob's name."""
    root = tmp_path / "runs"
    shutil.copytree(happy_run / "r", root / "r")
    blob = next(path for path in (root / "r" / "7_armarium" / "blobs").rglob("*") if path.is_file())
    blob.write_bytes(blob.read_bytes() + b"tampered")

    out = tmp_path / "delivery"
    result = _publish(root, "r", out)
    assert result.returncode != 0
    # The run tree's own damage report, not a reassuring "there is no export here".
    assert "no sealed armarium/export artifact" not in result.stderr
    assert "bytes changed under a sealed reference" in result.stderr
    assert not out.exists()


def test_a_run_authority_edited_after_sealing_publishes_nothing(tmp_path, happy_run):
    """A publisher whose run authority was edited after sealing produces no product.

    `main`'s explicit `tree.read_run()` is not the only guard that reaches this --
    `RunTree._verify_artifact_run` binds every artifact read to the same authority,
    so deleting the explicit call leaves this refusal in place. The property is
    pinned at the program's own boundary anyway: it is the sentence a recipient
    depends on, and the audit that removed the explicit call had nothing to fail.
    """
    root = tmp_path / "runs"
    shutil.copytree(happy_run / "r", root / "r")
    authority = root / "r" / "run.json"
    record = json.loads(authority.read_text(encoding="utf-8"))
    record["fixture_id"] = "a-fixture-this-run-never-used"
    authority.write_text(json.dumps(record), encoding="utf-8")

    out = tmp_path / "delivery"
    result = _publish(root, "r", out)

    assert result.returncode != 0
    assert "fails its own self-hash" in result.stderr
    assert not out.exists()


def test_a_payload_without_an_aggregate_is_refused_before_publication(
    tmp_path, happy_run, monkeypatch
):
    import bundle as bundle_module

    tree = RunTree(happy_run, "r")
    tree.read_run()
    real_sealed_bundle = bundle_module.sealed_bundle

    def payload_without_aggregate(run_tree):
        data, payload = real_sealed_bundle(run_tree)
        return data, {key: value for key, value in payload.items() if key != "aggregate"}

    monkeypatch.setattr(bundle_module, "sealed_bundle", payload_without_aggregate)

    out = tmp_path / "delivery"
    with pytest.raises(ContractError, match="aggregate status"):
        bundle_module.publish(tree, out)

    assert not out.exists()
    assert not list(tmp_path.glob(".delivery.publishing-*"))


def test_sealed_bundle_directly_refuses_bytes_changed_after_the_export_was_read():
    """Isolate the publisher's guard from RunTree's earlier reference checks."""
    import bundle as bundle_module

    declared = digest_bytes(b"sealed bundle")
    relative_path = "7_armarium/blobs/sha256/sealed"
    tree = SimpleNamespace(
        artifact_path=lambda *_args: "7_armarium/artifacts/export/export.json",
        resolve=lambda _path: SimpleNamespace(is_file=lambda: True),
        read_artifact=lambda *_args: {
            "inputs": [{"relative_path": relative_path, "sha256": declared}],
            "payload": {
                "bundle": {
                    "reference": {"relative_path": relative_path, "sha256": declared},
                    "sha256": declared,
                }
            },
        },
        blob_path=lambda _stage, _digest: relative_path,
        read_bytes=lambda _path: b"changed bundle",
    )

    with pytest.raises(ContractError, match="no longer matches the digest"):
        bundle_module.sealed_bundle(tree)


def test_the_sealed_bundle_must_occupy_its_content_addressed_armarium_path():
    """A sealed input path cannot be relabelled as the Armarium's product blob."""
    import bundle as bundle_module

    data = b"sealed bundle"
    declared = digest_bytes(data)
    substituted_path = "receipts/sha256/a-different-record.json"
    reference = {"relative_path": substituted_path, "sha256": declared}
    tree = SimpleNamespace(
        artifact_path=lambda *_args: "7_armarium/artifacts/export/export.json",
        resolve=lambda _path: SimpleNamespace(is_file=lambda: True),
        read_artifact=lambda *_args: {
            "inputs": [reference],
            "payload": {"bundle": {"reference": reference, "sha256": declared}},
        },
        blob_path=lambda _stage, digest: f"7_armarium/blobs/sha256/{digest}",
        read_bytes=lambda _path: data,
    )

    with pytest.raises(ContractError, match="does not occupy the Armarium"):
        bundle_module.sealed_bundle(tree)


def test_the_published_bundle_reference_must_be_the_export_artifacts_sealed_input(
    tmp_path, happy_run
):
    """A digest-checked blob is not this export's evidence until its envelope binds it."""
    import bundle as bundle_module

    root = tmp_path / "runs"
    shutil.copytree(happy_run / "r", root / "r")
    tree = RunTree(root, "r")
    export_path = tree.resolve(
        tree.artifact_path(
            ARMARIUM,
            "export",
            artifact_id(ARMARIUM, "export", "export", None),
        )
    )
    export = json.loads(export_path.read_text(encoding="utf-8"))
    old_reference = export["payload"]["bundle"]["reference"]
    with ZipFile(BytesIO(tree.read_bytes(old_reference["relative_path"]))) as archive:
        names = archive.namelist()
        members = {name: archive.read(name) for name in names}

    # Change only container metadata. The package's sealed members and manifest
    # remain valid, but the resulting blob is a different object that the export
    # artifact never named among its inputs.
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_STORED) as archive:
        for name in names:
            info = ZipInfo(name, date_time=(1981, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_STORED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, members[name])
    substituted = buffer.getvalue()
    substituted_digest = digest_bytes(substituted)
    assert substituted_digest != old_reference["sha256"]
    substituted_path = tree.blob_path(ARMARIUM, substituted_digest)
    tree.resolve(substituted_path).write_bytes(substituted)
    substituted_reference = {
        "relative_path": substituted_path,
        "sha256": substituted_digest,
    }
    export["payload"]["bundle"].update(
        reference=substituted_reference,
        sha256=substituted_digest,
    )
    export["self_hash"] = self_hash(export)
    export_path.write_bytes(canonical_bytes(export))

    with pytest.raises(ContractError, match="not the export artifact's sole digest-checked input"):
        bundle_module.sealed_bundle(tree)


def test_a_sealed_blob_read_error_is_reported_as_a_product_contract_failure(happy_run, monkeypatch):
    import bundle as bundle_module

    tree = RunTree(happy_run, "r")
    tree.read_run()
    real_read = tree.read_bytes
    reads = 0

    def fail_read(relative_path):
        nonlocal reads
        reads += 1
        if reads == 1:
            return real_read(relative_path)
        raise OSError("simulated read failure")

    monkeypatch.setattr(tree, "read_bytes", fail_read)

    with pytest.raises(ContractError, match="sealed product bundle .* could not be read"):
        bundle_module.sealed_bundle(tree)


def test_a_sealed_export_remains_publishable_after_its_config_file_changes(tmp_path):
    """The publisher verifies sealed evidence; it does not resume the writer."""
    formats = tmp_path / "formats.toml"
    shutil.copyfile(ROOT / "config" / "formats.toml", formats)
    root = tmp_path / "runs"
    result = _orchestrate(root, "sealed-config", "happy", formats_config=formats)
    assert result.returncode == 0, result.stderr

    formats.write_text(formats.read_text(encoding="utf-8") + "\n# edited after sealing\n")
    out = tmp_path / "delivery"
    result = _publish(root, "sealed-config", out, formats_config=formats)

    assert result.returncode == 0, result.stderr
    assert (out / "armarium-export.zip").is_file()


def test_a_published_bundle_directory_carries_the_operators_umask_not_mkdtemps(happy_run, tmp_path):
    """`mkdtemp` creates at 0o700 and `os.replace` moves that directory intact.

    So the published product inherited a *temporary* directory's permissions
    rather than the operator's own, and a bundle a recipient cannot enter is not
    a bundle that was published. The mode is set from the umask before the
    rename, the way `mkdir` would have done it. Found by CodeRabbit.
    """
    # **The umask is set here rather than read.** Computing the expectation the
    # same way the code does made this test conditional on the machine running
    # it: `mkdtemp` always creates at 0o700, so an operator whose umask is 0o077
    # expects exactly 0o700 and the test passes against the *unfixed* code. It
    # only ever failed correctly at a permissive umask. Measured on the branch:
    # umask 022 and 002 catch the defect, umask 077 does not. Pinning 0o022 makes
    # the expected 0o755 a fact about the fix rather than about the machine.
    # Found by the Opus read of this branch, which is the second test of mine
    # tonight that passed for a reason other than its own title.
    previous = os.umask(0o022)
    try:
        out = tmp_path / "bundle-out"
        result = _publish(happy_run, "r", out)
        assert result.returncode == 0, result.stderr
        mode = stat.S_IMODE(out.stat().st_mode)
        assert mode == 0o755, f"published at {mode:o}, not 0o755 under a 0o022 umask"
    finally:
        os.umask(previous)
