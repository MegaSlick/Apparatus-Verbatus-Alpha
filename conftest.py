"""Fixtures shared by tests that cross pipeline stage directories."""

import hashlib
import json
import os
import shutil
import stat
import tomllib
from pathlib import Path

import pytest

from common.contracts.canonical import canonical_bytes, digest_bytes, self_hash
from common.stage import _stage_seal_payload, latest_attempt

ROOT = Path(__file__).resolve().parent


def tree_snapshot(root: Path) -> dict[str, str]:
    """Every entry under `root`, described -- not only its regular files.

    A refusal that tells the operator "Nothing was written" is a claim about a
    directory, and the suites that check that claim compare a snapshot taken
    before against one taken after. Filtering that snapshot by `is_file()` made
    it unable to see most of what a half-finished refusal leaves behind: an
    empty directory, a dangling symlink, a symlink to a directory, a fifo. Two
    trees that differ by any of those compared equal, so the check passed by not
    looking -- the shape GOVERNANCE 10 refuses, in the very test written to stop
    a claim being taken on trust.

    So every entry is described rather than skipped:

    * a regular file, by the digest of its bytes -- names alone would miss an
      overwrite of a file that already existed
    * a directory, as a directory: a run root created and then disowned is a run
      id claimed under inputs that were never accepted
    * a symlink, by its target, **recorded without following it**. The link is
      the thing that was left behind; where it points is somebody else's tree,
      and a symlink to a directory read as a directory would report files this
      run never wrote while hiding the link that reported them.
    * anything else -- fifo, socket, device -- by a marker. What it is matters
      less than that it appeared.

    `os.walk` rather than `rglob`, and with `followlinks` left at its default:
    the walk must not descend through a symlinked directory it has just recorded
    as a symlink.

    **`root` itself is described, under `"."`.** Describing only what is *under*
    it left one blindness of exactly the shape above: `os.walk` of a path that
    does not exist yields nothing, so a refusal that created the root and wrote
    nothing into it produced the same empty snapshot as a refusal that created
    nothing at all. That is not a detail -- the root is usually the run root,
    and a run root that exists is a run id claimed under inputs that were
    refused. A missing root is `{}` and an empty one is `{".": "directory"}`, so
    the two can no longer compare equal, and every before/after probe that uses
    this helper now also sees a root deleted, replaced, or swapped for a link
    between its two snapshots.

    A symlinked root is described and not walked, for the reason a symlinked
    entry is: the link is what was left behind, and following it would report a
    tree that lives somewhere else.
    """

    root = Path(root)
    if not root.is_symlink() and not root.exists():
        return {}
    snapshot: dict[str, str] = {".": _describe(root)}
    if root.is_symlink():
        return snapshot
    for parent, directory_names, file_names in os.walk(root):
        for name in (*directory_names, *file_names):
            path = Path(parent) / name
            snapshot[str(path.relative_to(root))] = _describe(path)
    return dict(sorted(snapshot.items()))


def _describe(path: Path) -> str:
    """One entry, as the snapshot records it -- the root included."""
    if path.is_symlink():
        return f"symlink -> {os.readlink(path)}"
    mode = path.lstat().st_mode
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return f"file {hashlib.sha256(path.read_bytes()).hexdigest()}"
    return "irregular entry"


def rebind_stage_seal_artifact(tree, stage: str, *, rewrite_manifest: bool = True) -> None:
    """Carry a deliberate semantic forgery through its producer's completion seal.

    A stage seal witnesses the producer's whole boundary, so any test that
    rewrites an upstream record now trips the seal first and proves the boundary
    refusal instead of the check it was written for. Rebinding models the other
    hypothesis — a producer that wrote the bad record *and* honestly witnessed
    it — which is the state the downstream check exists to catch and the only
    state in which it is reachable at all.

    Boundary-corruption tests deliberately do NOT rebind: leaving the seal alone
    is what proves the earlier refusal.

    `rewrite_manifest=False` is for the forgeries that leave a dangling input
    reference behind on purpose. `write_manifest` revalidates every input, so it
    would refuse here instead of at the consumer the test is aiming at — and the
    stored inventory is not what the seal is read out of anyway.
    """
    seals = [
        tree.read_artifact(stage, "stage-seal", entry["artifact_id"])
        for entry in tree.build_manifest(stage, verify_inputs=False)["artifacts"]
        if entry["kind"] == "stage-seal"
    ]
    seal = latest_attempt(seals, f"{stage} stage seal", operation="seal")
    payload = seal["payload"]
    seal["payload"] = _stage_seal_payload(
        tree,
        stage,
        payload["attempt_ordinal"],
        seal["attempt_id"],
        verify_inputs=rewrite_manifest,
        verify_blob_addresses=rewrite_manifest,
    )
    seal["self_hash"] = self_hash(seal)
    tree.resolve(tree.artifact_path(stage, "stage-seal", seal["artifact_id"])).write_bytes(
        canonical_bytes(seal)
    )
    if rewrite_manifest:
        tree.write_manifest(stage)


def _repoint_retained_references(tree, node) -> bool:
    """Repoint every `{relative_path, sha256}` pair in a record to the bytes on disk.

    A retained reference is not only the envelope's `inputs` list: a producer that
    reconciles evidence carries the same pair inside its payload (the Designator's
    proposal seal names each act's region and hold evidence that way). Repointing
    one and not the other leaves the record disagreeing with itself, so the walk
    is over the whole record. A reference whose file is absent is left alone --
    that is a dangling reference some tests plant on purpose.
    """
    changed = False
    if isinstance(node, dict):
        path, sha = node.get("relative_path"), node.get("sha256")
        if isinstance(path, str) and isinstance(sha, str):
            try:
                actual = digest_bytes(tree.read_bytes(path))
            except OSError:
                actual = sha
            if actual != sha:
                node["sha256"] = actual
                changed = True
        for value in node.values():
            changed |= _repoint_retained_references(tree, value)
    elif isinstance(node, list):
        for value in node:
            changed |= _repoint_retained_references(tree, value)
    return changed


def rewitness_stage_boundary(tree, stage: str) -> None:
    """Rebind a stage's whole boundary, retained input references included.

    `rebind_stage_seal_artifact` rebinds the seal alone, which is enough while the
    forgery is the last thing in the tree that names the changed bytes. It is not
    enough once some artifact of this stage retained an input reference to them:
    the reference still records the pre-forgery digest, so the next reader stops
    at `the bytes changed under a sealed reference` — this stage's own boundary —
    instead of at the semantic check the calling test is named for.

    Repointing each stale reference to the bytes now on disk models the same
    hypothesis the seal rebind does, one link further out: a producer that read
    the forged record and honestly witnessed what it read. No refusal is
    softened; the boundary checks are satisfied rather than stepped around, so
    the consumer must catch the forgery on its own re-derivation or catch it
    nowhere. Tests aimed *at* a boundary refusal must not call this.

    Artifacts of one stage also reference each other, so rewriting one moves the
    bytes a sibling recorded; the sweep runs to a fixed point.
    """
    for _ in range(len(tree.build_manifest(stage, verify_inputs=False)["artifacts"]) + 1):
        settled = True
        for entry in tree.build_manifest(stage, verify_inputs=False)["artifacts"]:
            path = tree.resolve(entry["relative_path"])
            record = json.loads(path.read_text(encoding="utf-8"))
            if _repoint_retained_references(tree, record):
                payload = record.get("payload")
                # A producer that seals its payload separately -- the Designator's
                # proposal-seal denominator does -- must have that inner hash
                # recomputed too, or the reader stops on the denominator's own
                # self-hash instead of on the check the calling test names.
                if isinstance(payload, dict) and "self_hash" in payload:
                    payload["self_hash"] = self_hash(payload)
                record["self_hash"] = self_hash(record)
                path.write_bytes(canonical_bytes(record))
                settled = False
        if settled:
            break
    else:  # pragma: no cover - a reference cycle would be a contract failure, not a test bug
        raise AssertionError(f"{stage} input references never settled; the sweep found a cycle")
    rebind_stage_seal_artifact(tree, stage)


@pytest.fixture
def rebind_stage_seal():
    """Expose coherent test forgeries without putting test support in stage code."""
    return rebind_stage_seal_artifact


@pytest.fixture
def rewitness_boundary():
    """The seal rebind above, extended to the stage's retained input references."""
    return rewitness_stage_boundary


@pytest.fixture
def absent_third_chair_config(tmp_path: Path) -> Path:
    """Copy the live model config and mark its third witness explicitly absent."""
    config_root = tmp_path / "chair-config"
    shutil.copytree(ROOT / "config" / "model-fixtures", config_root / "model-fixtures")
    shutil.copytree(ROOT / "config" / "manifests", config_root / "manifests")
    live = (ROOT / "config" / "models.toml").read_text(encoding="utf-8")
    assert tomllib.loads(live)["chairs"]["attestator_3"]["state"] == "configured"
    section_start = live.index("[chairs.attestator_3]\n")
    next_table = live.find("\n[", section_start + 1)
    section_end = len(live) - 1 if next_table == -1 else next_table
    absent = """[chairs.attestator_3]
state = "absent"
reason = "fixture test removes this witness without replacing it"
"""
    path = config_root / "models.toml"
    path.write_text(live[:section_start] + absent + live[section_end + 1 :], encoding="utf-8")
    rewritten = tomllib.loads(path.read_text(encoding="utf-8"))
    assert rewritten["chairs"]["attestator_3"]["state"] == "absent"
    assert set(rewritten["chairs"]) == set(tomllib.loads(live)["chairs"]), (
        "the splice changed which chairs the roster declares"
    )
    return path
