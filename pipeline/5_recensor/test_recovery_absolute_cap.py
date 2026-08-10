"""The recovery request boundary genuinely refuses above the sealed cap.

Spec 09's third test: "loop 4 cannot be requested (cap enforced at the request
boundary, not by convention)". The walking skeleton's scenario driver cannot
exercise this directly -- `wants_recovery` in `run.py::main` only ever wants a
recovery on an act's very first pass (`used_total == 0`), by construction
(HANDOFF.md: "the walking skeleton's synthetic proposer always agrees with the
declared fixture"), so no scenario ever drives a real act to a second, third,
or fourth recovery request through the ordinary pipeline. `recovery_state`'s
own accounting (`pipeline/5_recensor/run.py`, the `len(ordered_requests) >
budget["allowed"] or ... > budget["absolute_cap"]` check) is the boundary that
actually has to hold regardless of how a caller got there -- seeded here
directly, bypassing the scenario driver's own limit, exactly the way a bug
upstream of that check would.
"""

import importlib.util
from pathlib import Path

import pytest

from common.contracts.envelope import build_envelope
from common.contracts.errors import FatalAccounting
from common.contracts.identities import artifact_id, attempt_id
from common.contracts.stages import RECENSOR
from common.recovery import FALLBACK_RECROP
from common.runtree.store import RunTree

ROOT = Path(__file__).resolve().parents[2]

CONFIG_DIGEST = "f" * 64
RECIPES = {"recensor": "fake-recensor-v0"}

# absolute_cap == allowed == fallback_recrop's own budget: the tightest legal
# policy `load_recovery_policy` permits (it always enforces allowed <=
# absolute_cap), so a fourth request trips every one of the three checks in
# the same request at once -- there is currently no policy shape that lets a
# test isolate the absolute_cap disjunct from the allowed disjunct, since
# load_recovery_policy's own invariant makes allowed the tighter-or-equal
# bound always. That coupling is itself named in run.py's own comment: the
# absolute_cap check is deliberate defense in depth against a future change to
# that invariant, not currently the exclusive decider -- this test proves the
# boundary as a whole holds, which is what spec 09's third test asks for.
BUDGET = {
    "config_sha256": "0" * 64,
    "absolute_cap": 3,
    "fallback_recrop": 3,
    "page_level_reread": 0,
    "allowed": 3,
}


def _load_recensor():
    spec = importlib.util.spec_from_file_location(
        "recensor_absolute_cap_under_test", ROOT / "pipeline/5_recensor/run.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _publish_recovery_request(tree: RunTree, act_id: str, ordinal: int) -> None:
    """One recovery-request and its one matching recovery-requested review --
    `recovery_state` refuses a request with no matching review (a crash
    between the two publications), so a seeded request needs both."""
    perlectio_ref = {
        "relative_path": "4_perlector/artifacts/perlectio/none.json",
        "sha256": "0" * 64,
    }
    request_payload = {
        "act_key": "a1",
        "attempt_ordinal": ordinal,
        "recovery_kind": FALLBACK_RECROP,
        "reason": "test fixture",
        "budget_allowed": BUDGET["allowed"],
        "budget_used": ordinal - 1,
        "kind_budget_allowed": BUDGET["fallback_recrop"],
        "kind_budget_used": ordinal - 1,
        "coverage": {},
        "perlectio_ref": perlectio_ref,
        "recovery_policy": BUDGET,
    }
    request_id = artifact_id(
        RECENSOR, "recovery-request", act_id, attempt_id(act_id, "recover", ordinal)
    )
    request_envelope = build_envelope(
        run_id=tree.run_id,
        artifact_id=request_id,
        subject_id=act_id,
        stage=RECENSOR,
        kind="recovery-request",
        outcome="recovery-requested",
        config_digest=CONFIG_DIGEST,
        adapter_revision="fake-recensor-v0",
        inputs=[],
        payload=request_payload,
        attempt=attempt_id(act_id, "recover", ordinal),
    )
    tree.publish_artifact(request_envelope)
    tree.write_manifest(RECENSOR)

    request_ref = _MiniContext(tree).artifact_ref(RECENSOR, "recovery-request", request_id)
    review_envelope = build_envelope(
        run_id=tree.run_id,
        artifact_id=artifact_id(RECENSOR, "review", act_id, attempt_id(act_id, "recense", ordinal)),
        subject_id=act_id,
        stage=RECENSOR,
        kind="review",
        outcome="recovery-requested",
        config_digest=CONFIG_DIGEST,
        adapter_revision="fake-recensor-v0",
        inputs=[request_ref],
        payload={
            "act_key": "a1",
            "attempt_ordinal": ordinal,
            "recovery_kind": FALLBACK_RECROP,
            "coverage": {},
            "perlectio_ref": perlectio_ref,
            "recovery_request_ref": request_ref,
            "recovery_policy": BUDGET,
        },
        attempt=attempt_id(act_id, "recense", ordinal),
    )
    tree.publish_artifact(review_envelope)
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


def test_exactly_the_sealed_cap_reconciles_cleanly(tmp_path):
    recensor = _load_recensor()
    tree = _minimal_tree(tmp_path)
    for ordinal in range(1, BUDGET["absolute_cap"] + 1):
        _publish_recovery_request(tree, "act_1", ordinal)

    state = recensor.recovery_state(_MiniContext(tree), "act_1", BUDGET)
    assert len(state["requests"]) == BUDGET["absolute_cap"]


def test_a_request_above_the_sealed_cap_is_refused_at_the_accounting_boundary(tmp_path):
    recensor = _load_recensor()
    tree = _minimal_tree(tmp_path)
    for ordinal in range(1, BUDGET["absolute_cap"] + 2):  # one past the cap
        _publish_recovery_request(tree, "act_1", ordinal)

    with pytest.raises(FatalAccounting, match="above its sealed total budget"):
        recensor.recovery_state(_MiniContext(tree), "act_1", BUDGET)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
