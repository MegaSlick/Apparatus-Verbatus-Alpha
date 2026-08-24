"""The coverage-triggered recovery origin, isolated from every other cause.

Unit 10C gave the Recensor a second reason to spend a bounded fallback-recrop:
a page witness's own native observation of ink outside every sealed proposal
(`partition_disagreement.unclaimed_observations`).  In the `review` scenario
that stimulus arrives beside a scenario-declared recrop on a1 and a scenario
hold on a2, so nothing observable there separates the coverage route from the
declared one -- an assertion about the coverage origin made against `review`
is really an assertion about whichever cause happened to fire first.

The `coverage-recovery` scenario exists for that separation.  It declares
`recover_acts = []` and `hold_acts = []` and is otherwise the reference run, so
the witness's unclaimed observation is the ONLY thing in it that can ask for a
recovery or hold an act.  Three facts are proven here against that path:

1. With the policy's fallback-recrop allowance available, the request is made,
   it names its origin as an observation rather than a doubt about the reading,
   and the expanded crop the Designator cuts genuinely reaches the ink that
   asked for it.  Recovery recovers coverage (GOVERNANCE 11).
2. With the allowance at zero -- the policy turned off -- the act is held for
   review, visibly, naming the spent budget; the finding survives on the page
   Testimonium AND in the review record, and the run reports `partial`.  Nothing
   disappears inside the loop (GOVERNANCE 2, ARCHITECTURE invariant 4).
3. A coverage-origin request and a declared-origin request draw on ONE bounded
   pool.  Three of them in any mixture reconcile; a fourth is refused at the
   accounting boundary (`RULED_ABSOLUTE_CAP`, "PURE ABSOLUTE, STOP AT 3").

Fact 3 is seeded directly rather than driven through a scenario, for the reason
`test_recovery_absolute_cap.py` gives at length: `wants_recovery` grants an act
at most one request in its lifetime (`used_total == 0`), so no scenario can
drive one act to a second, third or fourth request through the ordinary loop.
The arithmetic still has to hold for any caller that got there, and mixing the
two origins is exactly the way a per-origin allowance would hide inside it.
"""

import copy
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

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

# The origin phrase the Recensor writes when the request came from a witness's
# unclaimed geometry, and the one it writes for a declared incomplete crop
# (`pipeline/5_recensor/run.py`, the `reason` field of the recovery-request).
# Both are statements about coverage; neither judges the reading.
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


def _orchestrate(run_root: Path, run_id: str, recovery_config: Path | None = None):
    command = [
        sys.executable,
        str(ORCHESTRATOR),
        "--fixture",
        FIXTURE,
        "--scenario",
        SCENARIO,
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
    """Every unclaimed observation still readable on the page testimonia."""
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
    """Prove the declared stimulus is outside, not merely labelled unclaimed."""
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


def test_an_unclaimed_observation_alone_spends_a_recrop_that_names_its_origin(tmp_path):
    """(a) Budget available: the recrop happens and the record says why."""
    root = tmp_path / "runs"
    result = _orchestrate(root, "r")
    assert result.returncode == 0, result.stderr
    assert "run r: complete" in result.stdout

    tree = RunTree(root, "r")
    retained = _retained_observations(tree)
    _assert_observations_are_marginal(tree, retained)
    observed_bounds = [observation["bounds"] for observation in retained]

    # Non-vacuity control: if the load-bearing marginal observation moves onto
    # proposal ink, the scenario's own evidence check fails. Merely trusting the
    # stored `unclaimed_observations` label would leave this mutation green.
    mutated = copy.deepcopy(retained)
    proposal = next(
        region
        for region in _artifacts(tree, DESIGNATOR, "region")
        if region["payload"]["origin"] == "proposal"
    )
    mutated[0]["bounds"] = copy.deepcopy(proposal["payload"]["transform"]["bounds"])
    with pytest.raises(AssertionError, match="overlaps the sealed proposal denominator"):
        _assert_observations_are_marginal(tree, mutated)

    requests = _artifacts(tree, RECENSOR, "recovery-request")
    assert requests, "an available fallback-recrop allowance was never spent on the finding"
    for request in requests:
        payload = request["payload"]
        assert payload["recovery_kind"] == FALLBACK_RECROP
        # The origin is named as an observation, not as a doubt about the
        # reading: this request could not have come from the scenario, which
        # declares no recovery at all.
        assert COVERAGE_ORIGIN in payload["reason"]
        assert DECLARED_ORIGIN not in payload["reason"]
        # And it is named as data, not only as prose -- the observation that
        # asked for the recrop travels inside the request that answers it.
        carried = payload["testimony_content_coverage"]["unclaimed_observations"]
        assert carried, "the request does not carry the observation it originated in"
        for observation in carried:
            assert observation["kind"] == "unrouted-observation"
            assert observation["bounds"] in observed_bounds
            assert observation["testimonium_id"], "the origin names no reporting Testimonium"

    # Coverage, actually recovered: the expanded crop reaches the ink that asked
    # for it.  A recovery request answered by a crop that still misses the
    # observation would be a loop that recorded itself and recovered nothing.
    recovery_regions = [
        region
        for region in _artifacts(tree, DESIGNATOR, "region")
        if region["payload"]["origin"] == "recovery"
    ]
    assert recovery_regions
    for bounds in observed_bounds:
        assert any(
            _contains(region["payload"]["transform"]["bounds"], bounds)
            for region in recovery_regions
        ), f"no expanded recrop reaches the observation at {bounds}"

    # The recovery crops expand from proposal ink, so the ordinary containing
    # page observation overlaps both denominators. Attachment remains the
    # proposal-derived fact and the recovery region adds no second basis.
    page_records = _artifacts(tree, ATTESTATORES, "page-testimonium")
    attachments = _artifacts(tree, ATTESTATORES, "act-attachment")
    proposals_by_act = {
        act_id: [
            region
            for region in _artifacts(tree, DESIGNATOR, "region")
            if region["subject_id"] == act_id and region["payload"]["origin"] == "proposal"
        ]
        for act_id in {region["subject_id"] for region in recovery_regions}
    }
    for recovery in recovery_regions:
        act_id = recovery["subject_id"]
        observation = next(
            observed
            for record in page_records
            if record["payload"]["chair"] == "attestator_1"
            for observed in record["payload"]["observed"]
            if observed["bounds_source"] in {"native", "derived"}
            and any(
                _overlaps(observed["bounds"], proposal["payload"]["transform"]["bounds"])
                for proposal in proposals_by_act[act_id]
            )
            and _overlaps(observed["bounds"], recovery["payload"]["transform"]["bounds"])
        )
        assert observation
        attachment = next(record for record in attachments if record["subject_id"] == act_id)
        row = next(
            row
            for row in attachment["payload"]["attachments"]
            if row["chair"] == "attestator_1" and row["page_ordinal"] == 1
        )
        assert row["attached"] is True
        assert row["attachment_basis"] == "geometric-overlap"

    # Receipt items and the latest Recensor seal census describe the same two
    # accepted acts after re-entry. Every stage that re-entered has an exact
    # contiguous seal chain; no intermediate round disappears.
    receipt = tree.read_recensor_partition_receipt()
    assert receipt["recensor_status"] == "complete"
    assert receipt["expected_act_count"] == len(receipt["items"]) == 2
    assert {item["review_outcome"] for item in receipt["items"]} == {"accepted"}
    expected_seals = {DESIGNATOR: [1, 2, 3], "perlector": [1, 2, 3], RECENSOR: [1, 2]}
    for stage, expected_ordinals in expected_seals.items():
        seals = _stage_seals(tree, stage)
        assert [seal["payload"]["attempt_ordinal"] for seal in seals] == expected_ordinals
    latest_census = _stage_seals(tree, RECENSOR)[-1]["payload"]["census"]
    assert {tuple(sorted(row.items())) for row in latest_census} >= {
        tuple(sorted({"count": 2, "kind": "review", "outcome": "accepted"}.items())),
        tuple(
            sorted(
                {
                    "count": len(requests),
                    "kind": "recovery-request",
                    "outcome": "recovery-requested",
                }.items()
            )
        ),
    }


def test_observation_inside_only_a_recovery_crop_stays_unattached_in_floor_accounting(
    tmp_path, monkeypatch
):
    """A later crop cannot assign an earlier page observation to an act.

    The marginal box is physically inside a2's expanded crop, but overlaps no
    sealed a2 proposal. Its only honest attachment basis is `unattached`; the
    native-granularity floor must exclude that chair as well. Including recovery
    regions in the Recensor denominator makes this test fail at the drift alarm.
    """
    root = tmp_path / "runs"
    result = _orchestrate(root, "recovery-only")
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
            SCENARIO,
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
    facts = recensor.act_attachment_facts(context, act["act_id"], outcomes)
    assert facts["attestator_1"]["attached"] is False
    assert facts["attestator_1"]["attachment_basis"] == "unattached"
    coverage = recensor.witness_coverage(outcomes, context.witness_floor, attachments=facts)
    assert coverage["granularity_basis"] == "native-observation-overlap"
    assert coverage["page_granularity_only"] == 1
    assert coverage["under_witnessed"] is True


@pytest.mark.parametrize(
    ("policy", "run_id"),
    [
        ("absolute_cap = 3\n[budget]\nfallback_recrop = 0\npage_level_reread = 0\n", "off"),
        # A page-level allowance is a different, unimplemented operation; it may
        # not be spent as though it were a crop, whatever the request's origin.
        ("absolute_cap = 3\n[budget]\nfallback_recrop = 0\npage_level_reread = 1\n", "page-only"),
    ],
)
def test_without_the_allowance_the_coverage_finding_is_held_visibly_not_lost(
    tmp_path, policy, run_id
):
    """(b) Policy off: visible-but-pending, never a silent drop."""
    root = tmp_path / "runs"
    recovery_config = tmp_path / f"{run_id}.toml"
    recovery_config.write_text(policy, encoding="utf-8")
    result = _orchestrate(root, run_id, recovery_config=recovery_config)
    # Partial, and it says so: a held act may never appear behind a complete run.
    assert result.returncode == 3, result.stderr
    assert f"run {run_id}: partial" in result.stdout

    tree = RunTree(root, run_id)
    assert not _artifacts(tree, RECENSOR, "recovery-request"), (
        "a spent request appeared with no allowance to spend"
    )
    assert not [
        region
        for region in _artifacts(tree, DESIGNATOR, "region")
        if region["payload"]["origin"] == "recovery"
    ]

    # The finding survives in both places it is written: on the witness's own
    # page Testimonium, which no budget decision touches, and inside the review
    # that held the act, which is where a reviewer will look.
    assert _retained_observations(tree)
    reviews = _artifacts(tree, RECENSOR, "review")
    assert reviews
    for review in reviews:
        assert review["outcome"] == "held-for-review"
        assert "fallback-recrops use 0 of their budget of 0" in review["payload"]["reason"]
        assert review["payload"]["testimony_content_coverage"]["unclaimed_observations"]


def _load_recensor():
    spec = importlib.util.spec_from_file_location(
        "recensor_coverage_origin_under_test", ROOT / "pipeline/5_recensor/run.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def _seed_request(tree: RunTree, act_id: str, ordinal: int, reason: str) -> None:
    """One recovery-request of a named origin, with the matching review
    `recovery_state` requires beside it."""
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
    """(c) Three requests of mixed origin reconcile against one cap of 3."""
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
    """(c) A coverage origin buys no allowance of its own above the cap."""
    recensor = _load_recensor()
    tree = _minimal_tree(tmp_path)
    for ordinal, reason in enumerate(_MIXED_ORIGINS, start=1):  # one past the cap
        _seed_request(tree, "act_1", ordinal, reason)

    with pytest.raises(FatalAccounting, match="above its sealed total budget"):
        recensor.recovery_state(_MiniContext(tree), "act_1", BUDGET)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
