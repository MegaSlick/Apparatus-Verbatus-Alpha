"""`bundle.py` run as a real subprocess: the product actually leaving the pipeline.

Shelling out rather than importing, for the reason the orchestrator's own acceptance
suite does it: `bundle.py` is the program an operator runs, and calling its functions
would prove the functions work rather than that the program does.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest
from armarium_export import EXPORT_MANIFEST_NAME

from common.contracts.canonical import digest_bytes
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
    second = _publish(happy_run, "r", out)
    assert second.returncode != 0
    assert "already exists" in second.stderr


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
    assert not out.exists()


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
