"""The two recovery operations are a real, distinguished fact, not one bucket.

ARCHITECTURE and spec 09 both name two distinct recovery operations -- a
Designator recrop and a Perlector page-level/continuation-aware reread -- and
`config/recovery.toml` already budgets them separately (`fallback_recrop`,
`page_level_reread`). Before this build nothing downstream distinguished them:
every recovery request became a recrop regardless of which kind it was
supposed to mean. `recovery_kind` on the request/review payload, and the
per-kind budget in `recovery_state`, are that fix.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from common.contracts.envelope import build_envelope
from common.contracts.errors import ContractError
from common.contracts.identities import artifact_id, attempt_id
from common.contracts.stages import DESIGNATOR, RECENSOR
from common.recovery import FALLBACK_RECROP, PAGE_LEVEL_REREAD
from common.runtree.store import RunTree

ROOT = Path(__file__).resolve().parents[2]

CONFIG_DIGEST = "f" * 64
RECIPES = {"recensor": "fake-recensor-v0"}

# A resolved recovery policy of the shape `common/recovery.load_recovery_policy`
# returns, so the counters these fixtures record reconcile against it exactly as a
# real run's would.
BUDGET = {
    "config_sha256": "0" * 64,
    "absolute_cap": 3,
    "fallback_recrop": 1,
    "page_level_reread": 1,
    "allowed": 2,
}


def _load_module(relative_path: str, name: str):
    path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _invoke(root: Path, run_id: str, scenario: str, program: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / program),
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
    assert result.returncode in (0, 3), f"{program}: {result.stderr}"


def _run_through_recensor(root: Path, run_id: str, scenario: str) -> None:
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/2_designator/run.py",
        "pipeline/3_attestatores/run.py",
        "pipeline/4_perlector/run.py",
        "pipeline/5_recensor/run.py",
    ):
        _invoke(root, run_id, scenario, program)


def test_the_real_recovery_request_names_fallback_recrop(tmp_path):
    """The only recovery kind this build can actually dispatch is the only kind
    the Recensor requests today -- never silently something else."""
    root = tmp_path / "runs"
    _run_through_recensor(root, "r", "review")
    tree = RunTree(root, "r")
    requests = [
        tree.read_artifact(RECENSOR, "recovery-request", entry["artifact_id"])
        for entry in tree.build_manifest(RECENSOR)["artifacts"]
        if entry["kind"] == "recovery-request"
    ]
    assert len(requests) == 1
    assert requests[0]["payload"]["recovery_kind"] == FALLBACK_RECROP

    reviews = [
        tree.read_artifact(RECENSOR, "review", entry["artifact_id"])
        for entry in tree.build_manifest(RECENSOR)["artifacts"]
        if entry["kind"] == "review" and entry["outcome"] == "recovery-requested"
    ]
    assert len(reviews) == 1
    assert reviews[0]["payload"]["continuation"]["is_continuation"] is False


# --- recovery_state: an isolated tree, so a malformed kind is the only thing
# under test rather than something a cross-referenced digest catches first ----


def _publish_recovery_request(
    tree: RunTree, act_id: str, payload_overrides: dict, ordinal: int = 1
) -> None:
    payload = {
        "act_key": "a1",
        "attempt_ordinal": ordinal,
        "recovery_kind": FALLBACK_RECROP,
        "reason": "test fixture",
        "budget_allowed": BUDGET["allowed"],
        "budget_used": ordinal - 1,
        "kind_budget_allowed": 1,
        "kind_budget_used": 0,
        "coverage": {},
        "perlectio_ref": {
            "relative_path": "4_perlector/artifacts/perlectio/none.json",
            "sha256": "0" * 64,
        },
        "recovery_policy": BUDGET,
    }
    payload.update(payload_overrides)
    envelope = build_envelope(
        run_id=tree.run_id,
        artifact_id=artifact_id(
            RECENSOR, "recovery-request", act_id, attempt_id(act_id, "recover", ordinal)
        ),
        subject_id=act_id,
        stage=RECENSOR,
        kind="recovery-request",
        outcome="recovery-requested",
        config_digest=CONFIG_DIGEST,
        adapter_revision="fake-recensor-v0",
        inputs=[],
        payload=payload,
        attempt=attempt_id(act_id, "recover", ordinal),
    )
    tree.publish_artifact(envelope)
    tree.write_manifest(RECENSOR)


class _MiniContext:
    """Just enough of `StageContext` for `recovery_state` to run against."""

    def __init__(self, tree: RunTree):
        self.tree = tree

    def input_ref(self, relative_path: str) -> dict:
        from common.contracts.canonical import digest_bytes

        return {
            "relative_path": relative_path,
            "sha256": digest_bytes(self.tree.read_bytes(relative_path)),
        }

    def artifact_ref(self, stage: str, kind: str, artifact_id_: str) -> dict:
        return self.input_ref(self.tree.artifact_path(stage, kind, artifact_id_))


def _minimal_tree(tmp_path) -> RunTree:
    return RunTree.create(
        tmp_path,
        "r1",
        source_manifest=[{"relative_path": "proof/page-1.png", "sha256": "e" * 64, "ordinal": 1}],
        config_digest=CONFIG_DIGEST,
        adapter_recipes=RECIPES,
        witness_chairs=["attestator_1"],
    )


def test_recovery_state_refuses_a_request_with_no_recognized_kind(tmp_path):
    """A malformed or missing `recovery_kind` is fatal accounting, not a silent
    default to whichever operation happens to run first."""
    from common.contracts.errors import FatalAccounting

    recensor = _load_module("pipeline/5_recensor/run.py", "recensor_missing_kind_under_test")
    tree = _minimal_tree(tmp_path)
    _publish_recovery_request(tree, "act_1", {"recovery_kind": "not-a-real-kind"})

    with pytest.raises(FatalAccounting, match="recognized recovery_kind"):
        recensor.recovery_state(_MiniContext(tree), "act_1", BUDGET)


def test_recovery_state_accepts_both_real_kinds(tmp_path):
    recensor = _load_module("pipeline/5_recensor/run.py", "recensor_both_kinds_under_test")
    tree = _minimal_tree(tmp_path)
    act_id = "act_1"
    perlectio_ref = {
        "relative_path": "4_perlector/artifacts/perlectio/none.json",
        "sha256": "0" * 64,
    }
    _publish_recovery_request(
        tree, act_id, {"recovery_kind": PAGE_LEVEL_REREAD, "perlectio_ref": perlectio_ref}
    )
    request_ref = _MiniContext(tree).artifact_ref(
        RECENSOR,
        "recovery-request",
        artifact_id(RECENSOR, "recovery-request", act_id, attempt_id(act_id, "recover", 1)),
    )
    review_envelope = build_envelope(
        run_id=tree.run_id,
        artifact_id=artifact_id(RECENSOR, "review", act_id, attempt_id(act_id, "recense", 1)),
        subject_id=act_id,
        stage=RECENSOR,
        kind="review",
        outcome="recovery-requested",
        config_digest=CONFIG_DIGEST,
        adapter_revision="fake-recensor-v0",
        inputs=[request_ref],
        payload={
            "act_key": "a1",
            "attempt_ordinal": 1,
            "recovery_kind": PAGE_LEVEL_REREAD,
            "coverage": {},
            "perlectio_ref": perlectio_ref,
            "recovery_request_ref": request_ref,
            "recovery_policy": BUDGET,
        },
        attempt=attempt_id(act_id, "recense", 1),
    )
    tree.publish_artifact(review_envelope)
    tree.write_manifest(RECENSOR)

    # A second, distinct kind at the next ordinal -- "both real kinds" means
    # both, not one accepted and the other's bucket merely asserted empty.
    _publish_recovery_request(
        tree,
        act_id,
        {"recovery_kind": FALLBACK_RECROP, "perlectio_ref": perlectio_ref},
        ordinal=2,
    )
    second_request_ref = _MiniContext(tree).artifact_ref(
        RECENSOR,
        "recovery-request",
        artifact_id(RECENSOR, "recovery-request", act_id, attempt_id(act_id, "recover", 2)),
    )
    second_review_envelope = build_envelope(
        run_id=tree.run_id,
        artifact_id=artifact_id(RECENSOR, "review", act_id, attempt_id(act_id, "recense", 2)),
        subject_id=act_id,
        stage=RECENSOR,
        kind="review",
        outcome="recovery-requested",
        config_digest=CONFIG_DIGEST,
        adapter_revision="fake-recensor-v0",
        inputs=[second_request_ref],
        payload={
            "act_key": "a1",
            "attempt_ordinal": 2,
            "recovery_kind": FALLBACK_RECROP,
            "coverage": {},
            "perlectio_ref": perlectio_ref,
            "recovery_request_ref": second_request_ref,
            "recovery_policy": BUDGET,
        },
        attempt=attempt_id(act_id, "recense", 2),
    )
    tree.publish_artifact(second_review_envelope)
    tree.write_manifest(RECENSOR)

    state = recensor.recovery_state(_MiniContext(tree), act_id, BUDGET)
    assert len(state["requests_by_kind"][PAGE_LEVEL_REREAD]) == 1
    assert len(state["requests_by_kind"][FALLBACK_RECROP]) == 1


# --- The Designator: refuses to answer a request meant for another stage ------


def test_the_designator_refuses_to_answer_a_non_recrop_recovery_kind(tmp_path, monkeypatch):
    """The Designator only ever cuts crops. Asked to answer a request whose
    `recovery_kind` names the OTHER operation, it must refuse rather than
    substitute a recrop -- exactly the silent conflation finding #4 named.

    `current_recovery_request` is stubbed rather than forged end-to-end: it
    already has its own dedicated coverage (`test_recovery_idempotency.py`,
    `test_orchestrator_acceptance.py`) proving it validates the request/review/
    policy chain. What is new and under test here is `recovery_pass`'s own
    reaction to the `recovery_kind` it returns, so that one dependency is
    faked and everything upstream of it (a real seal, a real act) stays real.
    """
    root = tmp_path / "runs"
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/2_designator/run.py",
    ):
        _invoke(root, "r", "review", program)

    designator = _load_module("pipeline/2_designator/run.py", "designator_kind_refusal_under_test")

    from common.stage import open_context, stage_parser

    args = stage_parser("recovery kind refusal").parse_args(
        ["--run-root", str(root), "--run-id", "r", "--scenario", "review"]
    )
    context = open_context(args, DESIGNATOR)
    seal = context.tree.read_artifact(
        DESIGNATOR, "proposal-seal", artifact_id(DESIGNATOR, "proposal-seal", "proposal-seal", None)
    )
    act_id = seal["payload"]["expected_acts"][0]["act_id"]
    act_key = seal["payload"]["expected_acts"][0]["act_key"]

    monkeypatch.setattr(
        designator,
        "current_recovery_request",
        lambda *args, **kwargs: {
            "artifact_id": "fake_request",
            "payload": {
                "attempt_ordinal": 1,
                "act_key": act_key,
                "recovery_kind": PAGE_LEVEL_REREAD,
                "budget_used": 0,
            },
        },
    )

    with pytest.raises(ContractError, match="only answers") as caught:
        designator.recovery_pass(context, act_id, "fake_request")
    assert FALLBACK_RECROP in str(caught.value)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
