"""The Perlector reads each chair's current testimonium, never a superseded one.

`pipeline/3_attestatores/run.py` cannot itself produce a second attempt for one
(act, chair) today -- every attempt is hardcoded to ordinal 1 -- so this forges
the second attempt directly onto the tree, the same technique
`pipeline/orchestrator/test_orchestrator_acceptance.py` already uses to exercise a
chair-identity boundary no live producer reaches yet either.
"""

import copy
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from common.chairs.registry import ChairRegistry
from common.contracts.canonical import canonical_bytes, digest_bytes, self_hash
from common.contracts.identities import artifact_id, attempt_id
from common.contracts.stages import ATTESTATORES, DESIGNATOR
from common.runtree.store import RunTree

ROOT = Path(__file__).resolve().parents[2]
MODELS_CONFIG = ROOT / "config" / "models.toml"


def _load_perlector():
    path = Path(__file__).resolve().parent / "run.py"
    spec = importlib.util.spec_from_file_location("perlector_testimonia_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


perlector = _load_perlector()


class _Context:
    def __init__(self, tree):
        self.tree = tree
        self.run = tree.read_run()
        self.registry = ChairRegistry.from_toml(MODELS_CONFIG)

    def input_ref(self, relative_path):
        return {
            "relative_path": relative_path,
            "sha256": digest_bytes(self.tree.read_bytes(relative_path)),
        }


@pytest.fixture
def run_with_a_superseded_attempt(tmp_path):
    """A real happy-path run, with one chair's testimonium given a second, later
    attempt that disagrees with its first — forged directly, since no CLI path
    produces a second attempt today."""
    root = tmp_path / "runs"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline/orchestrator/run.py"),
            "--fixture",
            "synthetic-two-page-v0",
            "--scenario",
            "happy",
            "--run-root",
            str(root),
            "--run-id",
            "superseded-attempt",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    tree = RunTree(root, "superseded-attempt")
    entries = tree.build_manifest(ATTESTATORES)["artifacts"]
    first = next(
        tree.read_artifact(ATTESTATORES, "testimonium", entry["artifact_id"])
        for entry in entries
        if entry["kind"] == "testimonium"
        and tree.read_artifact(ATTESTATORES, "testimonium", entry["artifact_id"])["outcome"]
        == "read"
    )
    act_id = first["subject_id"]
    chair = first["payload"]["chair"]

    second = copy.deepcopy(first)
    second["payload"]["attempt_ordinal"] = 2
    second["outcome"] = "failed"
    second["payload"]["reported"] = "a later, failed re-read"
    second["payload"]["reason"] = "the chair returned no usable report on the second attempt"
    second["attempt_id"] = attempt_id(act_id, f"read:{chair}", 2)
    second["artifact_id"] = artifact_id(ATTESTATORES, "testimonium", act_id, second["attempt_id"])
    second["self_hash"] = self_hash(second)
    path = tree.resolve(f"3_attestatores/artifacts/testimonium/{second['artifact_id']}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(second))
    tree.write_manifest(ATTESTATORES)

    return _Context(tree), act_id, chair


def _proposal_regions(context, act_id):
    return sorted(
        [
            context.tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
            for entry in context.tree.build_manifest(DESIGNATOR)["artifacts"]
            if entry["kind"] == "region"
            and entry["subject_id"] == act_id
            and context.tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])["payload"][
                "origin"
            ]
            == "proposal"
        ],
        key=lambda record: record["payload"]["attempt_ordinal"],
    )


def test_a_superseded_attempt_does_not_still_count_as_current_evidence(
    run_with_a_superseded_attempt,
):
    context, act_id, chair = run_with_a_superseded_attempt
    testimonia = perlector.testimonia_of(context, act_id, _proposal_regions(context, act_id))

    by_chair = {record["payload"]["chair"]: record for record in testimonia}
    assert len(testimonia) == len(by_chair), "more than one record survived for the same chair"
    assert by_chair[chair]["payload"]["attempt_ordinal"] == 2
    assert by_chair[chair]["outcome"] == "failed"


def test_the_witness_read_filter_excludes_a_chair_whose_current_attempt_failed(
    run_with_a_superseded_attempt,
):
    """The failure mode named in the finding: `pipeline/4_perlector/run.py::main`
    builds its witness-covered region set by filtering `testimonia_of(...)` to
    `outcome == "read"`. Before this fix, a since-superseded `read` attempt for
    this chair would still pass that filter even though the chair's current
    attempt is `failed` — this is the exact filter, exercised directly."""
    context, act_id, chair = run_with_a_superseded_attempt
    testimonia = perlector.testimonia_of(context, act_id, _proposal_regions(context, act_id))
    read_chairs = {
        record["payload"]["chair"] for record in testimonia if record["outcome"] == "read"
    }
    assert chair not in read_chairs
