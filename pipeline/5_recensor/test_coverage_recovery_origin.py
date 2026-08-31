"""The coverage-triggered recovery origin, isolated from every other cause.

Unit 10C gave the Recensor a second reason to spend a bounded fallback-recrop:
a page witness's own native observation of ink outside every sealed proposal
(`partition_disagreement.unclaimed_observations`).  That observation is a
witness's report -- a *pointer*, in the consult's word -- never itself the
evidence.  Unit 14B's binding consult (`/out/CONSULT_REPORT.md` §4.5) requires
a second, independent conjunct before it may spend a bounded recovery or hold
an act: Unit 9's own ink map must confirm real ink under that pointer.
Without it, an Attestator's own mis-reported or hallucinated box could spend
real recovery budget, or hold a real act, on zero actual ink -- the witness
picking a pipeline action for itself, which GOVERNANCE 3 forbids by name.

An earlier build pass wired the coverage route straight to
`unclaimed_observations` with no ink confirmation at all. The
`coverage-recovery` scenario's own marginal witness box (page 1,
x 0..10 / y 200..240) is a measured, empirical proof this was live: the
fixture's underlying page pixels there are entirely background -- zero ink --
confirmed directly against `proof/synthetic_pages.py` (`common.residual_ink.
ink_runs`); yet the unconfirmed route spent a real fallback-recrop, and with
the allowance at zero, held a real act, on that empty box alone. This file now
proves the corrected boundary: the same witness-alone stimulus asks for
nothing, in the same shape, whatever the recovery budget is, while the raw
finding stays visibly retained (GOVERNANCE 2) for a human to weigh. The
ink-confirmed *positive* path -- where the pointer *is* backed by real ink --
is proven at the unit level in `test_unit14b_trigger_contract.py`, against
synthetic ink-map evidence, rather than by adding real ink to this shared
fixture image: every other scenario's pinned run-tree digest is measured
against these same page bytes, and widening this file's own claim should not
silently move every one of them.

A third fact survives this correction untouched, seeded directly rather than
driven through a scenario (`test_recovery_absolute_cap.py` gives the reason at
length): a coverage-origin request and a declared-origin request draw on ONE
bounded pool. Three of them in any mixture reconcile; a fourth is refused at
the accounting boundary (`RULED_ABSOLUTE_CAP`, "PURE ABSOLUTE, STOP AT 3").
`wants_recovery` grants an act at most one request in its lifetime
(`used_total == 0`), so no scenario can drive one act to a second, third or
fourth request through the ordinary loop; the arithmetic still has to hold for
any caller that got there, and mixing the two origins is exactly the way a
per-origin allowance would hide inside it.
"""

import copy
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from common.contracts.canonical import digest_bytes
from common.contracts.envelope import build_envelope
from common.contracts.errors import FatalAccounting
from common.contracts.identities import artifact_id, attempt_id
from common.contracts.stages import ATTESTATORES, DESIGNATOR, RECENSOR
from common.native_witness import partition_disagreement
from common.recovery import FALLBACK_RECROP
from common.runtree.store import RunTree

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "pipeline/orchestrator/run.py"
FIXTURE = "synthetic-two-page-v0"
SCENARIO = "coverage-recovery"

# Both origins describe coverage and must never become reading-quality claims.
COVERAGE_ORIGIN = "a page witness reported ink outside every sealed proposal"
DECLARED_ORIGIN = "the crop may be incomplete"

CONFIG_DIGEST = "f" * 64
RECIPES = {"recensor": "fake-recensor-v0"}
# absolute_cap == allowed: the tightest policy `load_recovery_policy` permits,
# so a fourth request in any origin mixture trips the sealed bound.
BUDGET = {
    "config_sha256": "0" * 64,
    "absolute_cap": 3,
    "fallback_recrop": 3,
    "page_level_reread": 0,
    "allowed": 3,
}


def _orchestrate(
    run_root: Path, run_id: str, recovery_config: Path | None = None, scenario: str = SCENARIO
):
    command = [
        sys.executable,
        str(ORCHESTRATOR),
        "--fixture",
        FIXTURE,
        "--scenario",
        scenario,
        "--run-id",
        run_id,
        "--run-root",
        str(run_root),
    ]
    if recovery_config is not None:
        command.extend(("--recovery-config", str(recovery_config)))
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True)


def _artifacts(tree: RunTree, stage: str, kind: str) -> list[dict]:
    return [
        tree.read_artifact(stage, kind, entry["artifact_id"])
        for entry in tree.build_manifest(stage)["artifacts"]
        if entry["kind"] == kind
    ]


def _retained_observations(tree: RunTree) -> list[dict]:
    return [
        observation
        for record in _artifacts(tree, ATTESTATORES, "page-testimonium")
        for observation in (record["payload"].get("partition_disagreement") or {}).get(
            "unclaimed_observations", []
        )
    ]


def _contains(outer: dict, inner: dict) -> bool:
    return (
        outer["x"] <= inner["x"]
        and outer["y"] <= inner["y"]
        and outer["x"] + outer["w"] >= inner["x"] + inner["w"]
        and outer["y"] + outer["h"] >= inner["y"] + inner["h"]
    )


def _overlaps(left: dict, right: dict) -> bool:
    return min(left["x"] + left["w"], right["x"] + right["w"]) > max(left["x"], right["x"]) and min(
        left["y"] + left["h"], right["y"] + right["h"]
    ) > max(left["y"], right["y"])


def _assert_observations_are_marginal(tree: RunTree, observations: list[dict]) -> None:
    """Stored labels are not authority; the stimulus must miss every proposal."""
    proposals = [
        region["payload"]["transform"]["bounds"]
        for region in _artifacts(tree, DESIGNATOR, "region")
        if region["payload"]["origin"] == "proposal"
    ]
    assert observations, "the isolating scenario produced no unclaimed observation to route"
    assert all(
        not any(_overlaps(observation["bounds"], proposal) for proposal in proposals)
        for observation in observations
    ), "the coverage-recovery stimulus overlaps the sealed proposal denominator"


def _stage_seals(tree: RunTree, stage: str) -> list[dict]:
    return sorted(
        _artifacts(tree, stage, "stage-seal"),
        key=lambda seal: seal["payload"]["attempt_ordinal"],
    )


def test_an_unclaimed_observation_alone_spends_nothing_without_ink_confirmation(tmp_path):
    """A witness's own unclaimed box, unconfirmed by real ink, asks for nothing.

    The `coverage-recovery` fixture's marginal box is a deliberately
    uncalibrated stimulus with no drawn ink beneath it (verified below,
    directly against the page pixels). Under consult §4.5 that is a pointer
    with no evidence, and the pointer alone may not spend a bounded recrop or
    hold an act -- otherwise a page witness's own report, right or wrong,
    would be picking a pipeline action for itself.
    """
    root = tmp_path / "runs"
    result = _orchestrate(root, "r")
    assert result.returncode == 0, result.stderr
    assert "run r: complete" in result.stdout

    tree = RunTree(root, "r")
    retained = _retained_observations(tree)
    _assert_observations_are_marginal(tree, retained)
    observed_bounds = [observation["bounds"] for observation in retained]

    # The retained `unclaimed` label cannot substitute for geometric proof.
    mutated = copy.deepcopy(retained)
    proposal = next(
        region
        for region in _artifacts(tree, DESIGNATOR, "region")
        if region["payload"]["origin"] == "proposal"
    )
    mutated[0]["bounds"] = copy.deepcopy(proposal["payload"]["transform"]["bounds"])
    with pytest.raises(AssertionError, match="overlaps the sealed proposal denominator"):
        _assert_observations_are_marginal(tree, mutated)

    # The independent fact that makes the refusal below correct rather than
    # merely convenient: Unit 9's own ink map records zero ink in every one of
    # the retained boxes. A regression that started flagging real ink there
    # would falsify this control and must not be read as this test passing.
    from common.residual_ink import ink_runs
    from proof.synthetic_pages import page_bytes

    evidence = ink_runs(page_bytes(1))
    for bounds in observed_bounds:
        x0, x1 = bounds["x"], bounds["x"] + bounds["w"]
        ink_here = sum(
            max(0, min(x1, start + length) - max(x0, start))
            for row in evidence["rows"][bounds["y"] : bounds["y"] + bounds["h"]]
            for start, length in row
        )
        assert ink_here == 0, "the marginal stimulus is no longer an empty-ink control"

    # No pipeline resource is spent on the unconfirmed pointer: no recovery
    # request, no expanded crop, no hold.
    assert not _artifacts(tree, RECENSOR, "recovery-request")
    assert not [
        region
        for region in _artifacts(tree, DESIGNATOR, "region")
        if region["payload"]["origin"] == "recovery"
    ]
    assert not [
        review
        for review in _artifacts(tree, RECENSOR, "review")
        if review["outcome"] == "held-for-review"
    ]

    # Nothing is lost silently (GOVERNANCE 2): the witness's own report is
    # still there for a human to weigh, even though it triggers no action.
    assert retained

    receipt = tree.read_recensor_partition_receipt()
    assert receipt["recensor_status"] == "complete"
    assert receipt["expected_act_count"] == len(receipt["items"]) == 2
    assert {item["review_outcome"] for item in receipt["items"]} == {"accepted"}
    expected_seals = {DESIGNATOR: [1], "perlector": [1], RECENSOR: [1]}
    for stage, expected_ordinals in expected_seals.items():
        seals = _stage_seals(tree, stage)
        assert [seal["payload"]["attempt_ordinal"] for seal in seals] == expected_ordinals


def test_observation_inside_only_a_recovery_crop_stays_unattached_in_floor_accounting(
    tmp_path, monkeypatch
):
    """A later crop cannot assign an earlier page observation to an act.

    The marginal box is physically inside a2's expanded crop, but overlaps no
    sealed a2 proposal. Its only honest attachment basis is `unattached`; the
    native-granularity floor must exclude that chair as well. Including recovery
    regions in the Recensor denominator makes this test fail at the drift alarm.

    The recovery crop this test needs is driven through `continuation-recovery`
    (`recover_acts = ["a2"]`), a scenario-DECLARED recrop untouched by consult
    §4.5's ink confirmation -- that gate governs the witness-pointer coverage
    route this test is not exercising. The marginal witness box itself is
    injected directly below, exactly as before; only the scenario supplying a2's
    real expanded crop has changed, because `coverage-recovery` no longer
    produces one for an unconfirmed witness pointer with no ink beneath it.
    """
    origin_scenario = "continuation-recovery"
    root = tmp_path / "runs"
    result = _orchestrate(root, "recovery-only", scenario=origin_scenario)
    assert result.returncode == 0, result.stderr
    tree = RunTree(root, "recovery-only")
    recensor = _load_recensor()
    args = recensor.stage_parser("coverage attachment test").parse_args(
        [
            "--run-root",
            str(root),
            "--run-id",
            "recovery-only",
            "--scenario",
            origin_scenario,
            "--fixture-root",
            str(ROOT / "proof"),
        ]
    )
    context = recensor.open_context(args, RECENSOR)
    act = next(act for act in recensor.expected_acts(context) if act["act_key"] == "a2")
    marginal = {"x": 0, "y": 200, "w": 10, "h": 40}
    recovery = next(
        region
        for region in _artifacts(tree, DESIGNATOR, "region")
        if region["subject_id"] == act["act_id"] and region["payload"]["origin"] == "recovery"
    )
    assert _contains(recovery["payload"]["transform"]["bounds"], marginal)

    original_artifact = context.tree.read_artifact

    def recovery_only_attachment(stage, kind, artifact_id):
        record = original_artifact(stage, kind, artifact_id)
        if (
            stage == ATTESTATORES
            and kind == "act-attachment"
            and record["subject_id"] == act["act_id"]
        ):
            record = copy.deepcopy(record)
            row = next(
                row
                for row in record["payload"]["attachments"]
                if row["chair"] == "attestator_1" and row["page_ordinal"] == 1
            )
            row["attached"] = False
            # The retired act-scoped bridge no longer supplies a separate
            # comparable text once this native page observation is unattached.
            # Keep the forged row internally coherent so the test reaches the
            # recovery-origin floor assertion below.
            row["comparable"] = False
            row["attachment_basis"] = "unattached"
            row["span"] = None
        return record

    original_reference = context.tree.read_artifact_reference

    def recovery_only_geometry(reference, *, stage, kind, subject_id):
        record = original_reference(reference, stage=stage, kind=kind, subject_id=subject_id)
        if (
            kind == "page-testimonium"
            and record["payload"]["chair"] == "attestator_1"
            and record["payload"]["page_ordinal"] == 1
        ):
            record = copy.deepcopy(record)
            record["payload"]["observed"] = [
                {
                    "ordinal": 0,
                    "bounds": dict(marginal),
                    "bounds_source": "native",
                    "span": None,
                }
            ]
            proposal_boxes = record["payload"]["partition_disagreement"]["proposal_boxes"]
            partition_proposals = [
                {
                    "payload": {
                        "origin": "proposal",
                        "transform": {
                            "source_page_id": record["payload"]["presented"]["source_page_id"],
                            "bounds": box,
                        },
                    }
                }
                for box in proposal_boxes
            ]
            record["payload"]["partition_disagreement"] = partition_disagreement(
                record, partition_proposals
            )
        return record

    monkeypatch.setattr(context.tree, "read_artifact", recovery_only_attachment)
    monkeypatch.setattr(context.tree, "read_artifact_reference", recovery_only_geometry)
    current = recensor.chair_current_attempts(context, act["act_id"])
    outcomes = recensor.chair_outcomes(current)
    facts = recensor.act_attachment_facts(context, act["act_id"], current)
    assert facts["attestator_1"]["attached"] is False
    assert facts["attestator_1"]["attachment_basis"] == "unattached"
    coverage = recensor.witness_coverage(outcomes, context.witness_floor, attachments=facts)
    assert coverage["granularity_basis"] == "native-observation-overlap"
    assert coverage["page_granularity_only"] == 1
    assert coverage["under_witnessed"] is True


@pytest.mark.parametrize(
    ("policy", "run_id"),
    [
        ("absolute_cap = 3\n[budget]\nfallback_recrop = 3\npage_level_reread = 0\n", "normal"),
        ("absolute_cap = 3\n[budget]\nfallback_recrop = 0\npage_level_reread = 0\n", "off"),
        # A page-level allowance is a different, unimplemented operation; it may
        # not be spent as though it were a crop, whatever the request's origin.
        ("absolute_cap = 3\n[budget]\nfallback_recrop = 0\npage_level_reread = 1\n", "page-only"),
    ],
)
def test_the_refusal_does_not_depend_on_the_recovery_budget(tmp_path, policy, run_id):
    """The unconfirmed pointer asks for nothing whether or not there is budget.

    Before consult §4.5's ink confirmation was restored, an available
    allowance spent it and an exhausted one held the act on it -- the budget
    state was the only thing separating "spends real work" from "holds a real
    act" on a witness's own unconfirmed box. Now neither happens at any
    budget: the refusal is about missing evidence, not about running out of
    room to act on missing evidence.
    """
    root = tmp_path / "runs"
    recovery_config = tmp_path / f"{run_id}.toml"
    recovery_config.write_text(policy, encoding="utf-8")
    result = _orchestrate(root, run_id, recovery_config=recovery_config)
    assert result.returncode == 0, result.stderr
    assert f"run {run_id}: complete" in result.stdout

    tree = RunTree(root, run_id)
    assert not _artifacts(tree, RECENSOR, "recovery-request")
    assert not [
        region
        for region in _artifacts(tree, DESIGNATOR, "region")
        if region["payload"]["origin"] == "recovery"
    ]
    assert not [
        review
        for review in _artifacts(tree, RECENSOR, "review")
        if review["outcome"] == "held-for-review"
    ]

    # The witness's own report is still visible (GOVERNANCE 2), whatever the
    # budget was, even though no budget decision was ever reached over it.
    assert _retained_observations(tree)


def _load_recensor():
    spec = importlib.util.spec_from_file_location(
        "recensor_coverage_origin_under_test", ROOT / "pipeline/5_recensor/run.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _MiniContext:
    def __init__(self, tree: RunTree):
        self.tree = tree

    def artifact_ref(self, stage: str, kind: str, artifact_id_: str) -> dict:
        relative_path = self.tree.artifact_path(stage, kind, artifact_id_)
        return {
            "relative_path": relative_path,
            "sha256": digest_bytes(self.tree.read_bytes(relative_path)),
        }


def _seed_request(tree: RunTree, act_id: str, ordinal: int, reason: str) -> None:
    """Every seeded request requires its matching append-only review."""
    perlectio_ref = {
        "relative_path": "4_perlector/artifacts/perlectio/none.json",
        "sha256": "0" * 64,
    }
    request_id = artifact_id(
        RECENSOR, "recovery-request", act_id, attempt_id(act_id, "recover", ordinal)
    )
    tree.publish_artifact(
        build_envelope(
            run_id=tree.run_id,
            artifact_id=request_id,
            subject_id=act_id,
            stage=RECENSOR,
            kind="recovery-request",
            outcome="recovery-requested",
            config_digest=CONFIG_DIGEST,
            adapter_revision="fake-recensor-v0",
            inputs=[],
            payload={
                "act_key": "a1",
                "attempt_ordinal": ordinal,
                "recovery_kind": FALLBACK_RECROP,
                "reason": reason,
                "budget_allowed": BUDGET["allowed"],
                # The counter under test: every earlier request counts here,
                # whatever origin it had.
                "budget_used": ordinal - 1,
                "kind_budget_allowed": BUDGET["fallback_recrop"],
                "kind_budget_used": ordinal - 1,
                "coverage": {},
                "perlectio_ref": perlectio_ref,
                "recovery_policy": BUDGET,
            },
            attempt=attempt_id(act_id, "recover", ordinal),
        )
    )
    tree.write_manifest(RECENSOR)
    request_ref = _MiniContext(tree).artifact_ref(RECENSOR, "recovery-request", request_id)
    tree.publish_artifact(
        build_envelope(
            run_id=tree.run_id,
            artifact_id=artifact_id(
                RECENSOR, "review", act_id, attempt_id(act_id, "recense", ordinal)
            ),
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
    )
    tree.write_manifest(RECENSOR)


def _minimal_tree(tmp_path) -> RunTree:
    return RunTree.create(
        tmp_path,
        "r1",
        source_manifest=[{"relative_path": "proof/page-1.png", "sha256": "e" * 64, "ordinal": 1}],
        config_digest=CONFIG_DIGEST,
        adapter_recipes=RECIPES,
        witness_chairs=["attestator_1"],
    )


# A mixture, deliberately not grouped by origin: a per-origin allowance would
# survive any ordering that let one origin's requests run out first.
_MIXED_ORIGINS = (COVERAGE_ORIGIN, DECLARED_ORIGIN, COVERAGE_ORIGIN, DECLARED_ORIGIN)


def test_both_recovery_origins_spend_the_same_bounded_pool(tmp_path):
    """Mixed origins must reconcile against one cap of three."""
    recensor = _load_recensor()
    tree = _minimal_tree(tmp_path)
    for ordinal, reason in enumerate(_MIXED_ORIGINS[: BUDGET["absolute_cap"]], start=1):
        _seed_request(tree, "act_1", ordinal, reason)

    state = recensor.recovery_state(_MiniContext(tree), "act_1", BUDGET)
    assert len(state["requests"]) == BUDGET["absolute_cap"]
    reasons = [request["payload"]["reason"] for request in state["requests"]]
    assert sum(reason == COVERAGE_ORIGIN for reason in reasons) == 2
    assert sum(reason == DECLARED_ORIGIN for reason in reasons) == 1
    # One pool, counted once: the fourth request's own recorded `budget_used`
    # is the total of both origins before it, not this origin's own tally.
    assert [request["payload"]["budget_used"] for request in state["requests"]] == [0, 1, 2]


def test_a_fourth_request_of_either_origin_is_refused_above_the_cap(tmp_path):
    """A coverage origin buys no allowance above the shared cap."""
    recensor = _load_recensor()
    tree = _minimal_tree(tmp_path)
    for ordinal, reason in enumerate(_MIXED_ORIGINS, start=1):
        _seed_request(tree, "act_1", ordinal, reason)

    with pytest.raises(FatalAccounting, match="above its sealed total budget"):
        recensor.recovery_state(_MiniContext(tree), "act_1", BUDGET)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
