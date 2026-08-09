"""Spec 10, test 2: a second write for the same act_id fails loudly, and the
first record is byte-identical afterwards.

Tyrel's 4b ruling: a revised reading is a whole new pipeline run over the same
Exemplar, never a rewrite of an existing record. This is enforced a layer down,
in `common.runtree.store.RunTree._publish_bytes` (immutable, atomic publish; a
second publish of *different* bytes under the same identity is refused before
anything is written) — `pipeline/6_archetypus/run.py` adds nothing here except
that it never tries to work around it. Both directions are asserted, per
harvest invariant #14 ("a seal that stops refusing bad things in order to stop
refusing good things is not a fix"): identical bytes reuse silently, and
different bytes under the same act_id are refused loudly with the original left
untouched.
"""

import json
import subprocess
import sys
from pathlib import Path

from common.contracts.canonical import canonical_bytes, self_hash
from common.contracts.identities import artifact_id, attempt_id
from common.contracts.stages import ARCHETYPUS
from common.runtree.store import RunTree

ROOT = Path(__file__).resolve().parents[2]


def orchestrate(root: Path, run_id: str, scenario: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline/orchestrator/run.py"),
            "--fixture",
            "synthetic-two-page-v0",
            "--scenario",
            scenario,
            "--run-id",
            run_id,
            "--run-root",
            str(root),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def invoke_archetypus(root: Path, run_id: str, scenario: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline/6_archetypus/run.py"),
            "--run-root",
            str(root),
            "--run-id",
            run_id,
            "--scenario",
            scenario,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def test_a_second_differing_write_for_the_same_act_is_refused_and_the_original_survives(tmp_path):
    root = tmp_path / "runs"
    assert orchestrate(root, "r", "happy").returncode == 0
    tree = RunTree(root, "r")

    entry = next(
        entry
        for entry in tree.build_manifest(ARCHETYPUS)["artifacts"]
        if entry["kind"] == "archetypus"
    )
    act_id = entry["subject_id"]
    path = tree.resolve(entry["relative_path"])
    original_bytes = path.read_bytes()

    # A "revised reading" written straight over the existing record — exactly
    # the write 4b forbids: not a new run, just different bytes under the same
    # once-only identity.
    tampered = json.loads(original_bytes.decode("utf-8"))
    tampered["payload"] = dict(tampered["payload"])
    tampered["payload"]["text"] = "A REVISION THAT IS NOT A NEW RUN"
    tampered["payload"]["self_hash"] = self_hash(tampered["payload"])
    tampered["self_hash"] = self_hash(tampered)
    path.write_bytes(canonical_bytes(tampered))
    tampered_bytes = path.read_bytes()
    assert tampered_bytes != original_bytes

    # Re-running the real stage recomputes the *original* correct bytes from
    # unchanged upstream evidence and tries to publish them at the same path,
    # which now holds the tampered bytes: a genuine same-identity conflict,
    # not a rewrite this stage requests.
    result = invoke_archetypus(root, "r", "happy")
    assert result.returncode == 2, result.stderr
    assert "Traceback" not in result.stderr
    assert "already holds different bytes" in result.stderr
    assert "Artifacts are immutable" in result.stderr

    # The record on disk is exactly what was there before the refused attempt —
    # neither silently overwritten back to the "correct" bytes, nor half-written.
    assert path.read_bytes() == tampered_bytes
    assert path.read_bytes() != original_bytes

    reread = json.loads(path.read_bytes().decode("utf-8"))
    assert reread["subject_id"] == act_id
    assert reread["payload"]["text"] == "A REVISION THAT IS NOT A NEW RUN"


def test_rerunning_with_unchanged_upstream_evidence_reuses_the_original_bytes(tmp_path):
    """The acceptance half beside the refusal: nothing about resume rewrites."""
    root = tmp_path / "runs"
    assert orchestrate(root, "r", "happy").returncode == 0
    tree = RunTree(root, "r")

    before = {
        entry["relative_path"]: tree.resolve(entry["relative_path"]).read_bytes()
        for entry in tree.build_manifest(ARCHETYPUS)["artifacts"]
        if entry["kind"] == "archetypus"
    }
    assert before

    result = invoke_archetypus(root, "r", "happy")
    assert result.returncode == 0, result.stderr

    after = {
        entry["relative_path"]: tree.resolve(entry["relative_path"]).read_bytes()
        for entry in tree.build_manifest(ARCHETYPUS)["artifacts"]
        if entry["kind"] == "archetypus"
    }
    assert after == before


def test_index_json_is_rewritable_and_reflects_the_same_reconciled_rows(tmp_path):
    """index.json is a derived summary, not a sealed artifact -- it may be
    rewritten each run, unlike the per-act records it summarizes, and doing so
    must not change what it reconciles to."""
    root = tmp_path / "runs"
    assert orchestrate(root, "r", "happy").returncode == 0
    tree = RunTree(root, "r")
    index_path = tree.resolve(tree.index_path(ARCHETYPUS))
    first = json.loads(index_path.read_bytes().decode("utf-8"))

    result = invoke_archetypus(root, "r", "happy")
    assert result.returncode == 0, result.stderr
    second = json.loads(index_path.read_bytes().decode("utf-8"))
    assert second == first


def test_a_forged_second_archetypus_artifact_for_one_act_is_a_selection_nothing_makes(tmp_path):
    """Even in the (structurally unreachable) case of two records under two
    different attempt identities for the same act, nothing in this codebase
    picks between them -- `latest_attempt` refuses a duplicate ordinal, and
    the index's own reconciliation refuses more than one record per act_id."""
    root = tmp_path / "runs"
    assert orchestrate(root, "r", "happy").returncode == 0
    tree = RunTree(root, "r")
    original_entry = next(
        entry
        for entry in tree.build_manifest(ARCHETYPUS)["artifacts"]
        if entry["kind"] == "archetypus"
    )
    original = tree.read_artifact(ARCHETYPUS, "archetypus", original_entry["artifact_id"])
    act_id = original["subject_id"]

    forged = json.loads(json.dumps(original))
    forged_attempt = attempt_id(act_id, "establish", 2)
    forged["attempt_id"] = forged_attempt
    forged["artifact_id"] = artifact_id(ARCHETYPUS, "archetypus", act_id, forged_attempt)
    forged["self_hash"] = self_hash(forged)
    forged_path = tree.resolve(tree.artifact_path(ARCHETYPUS, "archetypus", forged["artifact_id"]))
    forged_path.parent.mkdir(parents=True, exist_ok=True)
    forged_path.write_bytes(canonical_bytes(forged))
    tree.write_manifest(ARCHETYPUS)

    result = invoke_archetypus(root, "r", "happy")
    assert result.returncode == 2, result.stderr
    assert "more than one Archetypus record" in result.stderr
