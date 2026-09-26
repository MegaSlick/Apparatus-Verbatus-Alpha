"""The orchestrator's own recovery-round ceiling is a watched refusal."""

from types import SimpleNamespace

import pytest

from common.contracts.approval import real_ingress_record
from common.contracts.errors import ContractError
from conftest import load_stage

# A stand-in digest for the run-sealed recovery policy. The dispatcher proves the
# policy it reads against the digests the run authority recorded, so a stub that
# omitted either half would be testing a loop nothing bounds.
SEALED_RECOVERY_SHA = "1" * 64


def _sealed_run_tree(
    sealed_recovery_sha: str = SEALED_RECOVERY_SHA,
    *,
    ingress: dict | None = None,
    recovery_payload: dict | None = None,
):
    """A minimal run tree whose authority names the sealed recovery policy.

    `ingress` is the run authority's own ingress record when a test needs the
    real route; absent, `common.stage.is_real_ingress` reads the run as the
    synthetic walking skeleton, which is what every other test here means.
    """

    class _Tree:
        def read_run(self):
            run = {"sealed_config_digests": {"recovery": sealed_recovery_sha}}
            if ingress is not None:
                run["ingress"] = ingress
            return run

        def read_artifact(self, _stage, _kind, _artifact_id):
            if recovery_payload is None:
                raise AssertionError("fixture route must not read a recovery artifact")
            return {"payload": recovery_payload}

    return lambda *_args: _Tree()


def _sealed_policy(**fields):
    return {"absolute_cap": 3, "config_sha256": SEALED_RECOVERY_SHA, **fields}


def test_a_dispatch_under_a_policy_the_run_never_sealed_refuses(monkeypatch):
    """The unit half of the orchestrator's point-of-use recheck: the run
    authority names one digest, the file on disk carries another."""
    orchestrator = load_stage("orchestrator")
    monkeypatch.setattr(orchestrator, "RunTree", _sealed_run_tree("2" * 64))
    monkeypatch.setattr(orchestrator, "load_recovery_policy", lambda _path: _sealed_policy())
    args = SimpleNamespace(run_root="unused", run_id="unused", recovery_config="unused")

    with pytest.raises(ContractError, match="recovery configuration changed between"):
        orchestrator.drive_recovery(args, hard_failure_policy={})


def test_orchestrator_stops_when_recovery_remains_outstanding_at_the_absolute_cap(monkeypatch):
    """A persistent, already-accounted request must trip the top-level guard.

    The shipped deterministic scenario intentionally requests only one recovery,
    so this injects a durable outstanding record at the boundary the loop itself
    owns.  It proves the orchestrator cannot spin indefinitely if a future
    recovery producer legitimately asks again.
    """
    orchestrator = load_stage("orchestrator")
    calls = []
    monkeypatch.setattr(orchestrator, "RunTree", _sealed_run_tree())
    monkeypatch.setattr(orchestrator, "load_recovery_policy", lambda _path: _sealed_policy())
    monkeypatch.setattr(
        orchestrator,
        "pending_recoveries",
        lambda _tree, _policy: [("act_1", "request_1", "fallback-recrop")],
    )
    monkeypatch.setattr(
        orchestrator, "invoke", lambda program, _args, **extra: calls.append((program, extra))
    )
    # The run-level hard-failure checkpoint is a separate concern from the
    # recovery-round cap this test exercises; stub it to a permanent non-breach so
    # the fake `RunTree` above, which answers only `read_run`, is never asked to
    # behave like a whole tree.
    monkeypatch.setattr(orchestrator, "checkpoint", lambda *_args: None)

    args = SimpleNamespace(run_root="unused", run_id="unused", recovery_config="unused")
    with pytest.raises(ContractError, match="after 3 rounds"):
        orchestrator.drive_recovery(args, hard_failure_policy={})

    assert len(calls) == 9


def test_an_unimplemented_page_level_request_is_not_silently_dispatched_as_a_recrop(monkeypatch):
    """A kind nothing downstream can answer refuses before anything is invoked.

    Not after the Designator has already been asked for a crop it would have cut
    under the wrong name: the whole batch's kinds are checked first, so half a
    recovery round is never left behind by the refusal.
    """
    orchestrator = load_stage("orchestrator")
    calls = []
    monkeypatch.setattr(orchestrator, "RunTree", _sealed_run_tree())
    monkeypatch.setattr(orchestrator, "load_recovery_policy", lambda _path: _sealed_policy())
    monkeypatch.setattr(
        orchestrator,
        "pending_recoveries",
        lambda _tree, _policy: [("act_1", "request_1", "page-level-reread")],
    )
    monkeypatch.setattr(
        orchestrator, "invoke", lambda program, _args, **extra: calls.append((program, extra))
    )
    monkeypatch.setattr(orchestrator, "checkpoint", lambda *_args: None)

    args = SimpleNamespace(run_root="unused", run_id="unused", recovery_config="unused")
    with pytest.raises(ContractError, match="no dispatch for"):
        orchestrator.drive_recovery(args, hard_failure_policy={})

    assert calls == []


def test_a_recovery_checkpoint_waits_for_each_owner_stage_batch(monkeypatch):
    """A recovery round is three sections, and the cap is judged between them.

    The shape for the run-level cap is "if errors happened in chandra stage
    it finishes that section but pauses". So every outstanding act's recrop is cut
    before any reread is asked for, and the checkpoint sits at each of the three
    section boundaries — never between two acts of the same batch, where a second
    already-approved request would be stranded without its owning stage's answer.
    """
    orchestrator = load_stage("orchestrator")
    calls = []
    checkpoints = []
    outstanding = iter(
        (
            [
                ("act_1", "request_1", "fallback-recrop"),
                ("act_2", "request_2", "fallback-recrop"),
            ],
            [],
        )
    )
    monkeypatch.setattr(orchestrator, "RunTree", _sealed_run_tree())
    monkeypatch.setattr(orchestrator, "load_recovery_policy", lambda _path: _sealed_policy())
    monkeypatch.setattr(orchestrator, "pending_recoveries", lambda *_args: next(outstanding))
    monkeypatch.setattr(
        orchestrator, "invoke", lambda program, _args, **extra: calls.append((program, extra))
    )
    monkeypatch.setattr(
        orchestrator,
        "checkpoint",
        lambda _args, checkpoint_name, _policy: checkpoints.append(checkpoint_name) and None,
    )

    args = SimpleNamespace(run_root="unused", run_id="unused", recovery_config="unused")
    assert orchestrator.drive_recovery(args, hard_failure_policy={}) is None
    assert [program for program, _extra in calls] == [
        orchestrator.STAGE_PROGRAMS["designator"],
        orchestrator.STAGE_PROGRAMS["designator"],
        orchestrator.STAGE_PROGRAMS["perlector"],
        orchestrator.STAGE_PROGRAMS["perlector"],
        orchestrator.STAGE_PROGRAMS["recensor"],
    ]
    assert checkpoints == ["designator", "perlector", "recensor"]


def test_a_measured_real_recovery_dispatches_each_owner_stage(monkeypatch):
    """A real measured request passes the retained payload into the screen."""
    orchestrator = load_stage("orchestrator")
    calls = []
    payload = {
        "origin": "coverage-observation",
        "recovery_bounds": {"x": 1, "y": 2, "w": 3, "h": 4},
        "coverage_observation": {"bounds": {"x": 1, "y": 2, "w": 3, "h": 4}},
        "ink_map_ref": {"relative_path": "ink.json", "sha256": "a" * 64},
    }
    outstanding = iter(([("act_1", "request_1", "fallback-recrop")], []))
    monkeypatch.setattr(
        orchestrator,
        "RunTree",
        _sealed_run_tree(ingress=real_ingress_record(), recovery_payload=payload),
    )
    monkeypatch.setattr(orchestrator, "load_recovery_policy", lambda _path: _sealed_policy())
    monkeypatch.setattr(orchestrator, "pending_recoveries", lambda *_args: next(outstanding))
    monkeypatch.setattr(
        orchestrator, "invoke", lambda program, _args, **extra: calls.append((program, extra))
    )
    monkeypatch.setattr(orchestrator, "checkpoint", lambda *_args: None)
    args = SimpleNamespace(run_root="unused", run_id="unused", recovery_config="unused")
    assert orchestrator.drive_recovery(args, hard_failure_policy={}) is None
    assert [program for program, _extra in calls] == [
        orchestrator.STAGE_PROGRAMS["designator"],
        orchestrator.STAGE_PROGRAMS["perlector"],
        orchestrator.STAGE_PROGRAMS["recensor"],
    ]


def test_a_breached_checkpoint_ends_the_recovery_round_where_it_was_found(monkeypatch):
    """The tally travels back to `main`, and the rest of the round is not run.

    Every other test in this file stubs the checkpoint to a permanent non-breach,
    so the three `return tally` paths inside a recovery round were never taken.
    The Designator section here finishes — its two recrops were already dispatched
    — and the reread and re-review that would have followed never happen.
    """
    orchestrator = load_stage("orchestrator")
    calls = []
    breach = {"threshold": 2, "count": 3, "breached": True, "by_kind": {}, "checkpoint": None}
    monkeypatch.setattr(orchestrator, "RunTree", _sealed_run_tree())
    monkeypatch.setattr(orchestrator, "load_recovery_policy", lambda _path: _sealed_policy())
    monkeypatch.setattr(
        orchestrator,
        "pending_recoveries",
        lambda *_args: [
            ("act_1", "request_1", "fallback-recrop"),
            ("act_2", "request_2", "fallback-recrop"),
        ],
    )
    monkeypatch.setattr(
        orchestrator, "invoke", lambda program, _args, **extra: calls.append((program, extra))
    )
    monkeypatch.setattr(
        orchestrator,
        "checkpoint",
        lambda _args, checkpoint_name, _policy: dict(breach, checkpoint=checkpoint_name),
    )

    args = SimpleNamespace(run_root="unused", run_id="unused", recovery_config="unused")
    tally = orchestrator.drive_recovery(args, hard_failure_policy={})
    assert tally is not None and tally["checkpoint"] == "designator"
    assert [program for program, _extra in calls] == [
        orchestrator.STAGE_PROGRAMS["designator"],
        orchestrator.STAGE_PROGRAMS["designator"],
    ]


def test_a_legacy_real_ingress_recrop_is_refused_and_recorded_before_anything_is_invoked(
    monkeypatch, capsys
):
    """F068/F083: the cause is named here, not discovered from a stage's exit code.

    A fixture-era request has no measured bounds or retained Ink Map evidence,
    so the orchestrator refuses it before invoking any owner stage. The retained
    request remains visible rather than being silently treated as a modern real
    recrop.
    """
    orchestrator = load_stage("orchestrator")
    calls = []
    monkeypatch.setattr(
        orchestrator,
        "RunTree",
        _sealed_run_tree(ingress=real_ingress_record(), recovery_payload={"origin": "legacy"}),
    )
    monkeypatch.setattr(orchestrator, "load_recovery_policy", lambda _path: _sealed_policy())
    monkeypatch.setattr(
        orchestrator,
        "pending_recoveries",
        lambda _tree, _policy: [
            ("act_1", "request_1", "fallback-recrop"),
            ("act_2", "request_2", "fallback-recrop"),
        ],
    )
    monkeypatch.setattr(
        orchestrator, "invoke", lambda program, _args, **extra: calls.append((program, extra))
    )
    monkeypatch.setattr(orchestrator, "checkpoint", lambda *_args: None)

    args = SimpleNamespace(run_root="unused", run_id="real-ingress-recovery", recovery_config="x")
    with pytest.raises(ContractError, match="legacy fixture-only"):
        orchestrator.drive_recovery(args, hard_failure_policy={})

    assert calls == []
    streams = capsys.readouterr()
    # On stderr, and asserted as stderr. The operator surface records a failed
    # run's detail as `completed.stderr or completed.stdout`, and the
    # `ContractError` this raises is itself printed to stderr, so a listing on
    # stdout would be dropped from the receipt and this refusal would be
    # recorded nowhere a human reads (principle 2).
    assert streams.out == ""
    printed = streams.err
    # Both acts, not only the one the raised exception carries.
    assert "recovery cannot be dispatched for 2 outstanding request(s)" in printed
    assert "act act_1 (request request_1, kind fallback-recrop)" in printed
    assert "act act_2 (request request_2, kind fallback-recrop)" in printed
    assert "nothing in the run tree was changed" in printed


def test_the_dispatch_screen_answers_each_cause_by_its_own_name():
    """The screen must not have closed the route recovery actually works on.

    A fixture-route recrop is dispatchable and the screen says nothing about it,
    which is what keeps `test_a_recovery_checkpoint_waits_for_each_owner_stage_
    batch` above — a whole round driven through `drive_recovery` with no ingress
    record — dispatching all five sections. The kind is asked before the route,
    so a kind nothing can dispatch is reported as that on either route rather
    than blamed on the submission carrying it.
    """
    orchestrator = load_stage("orchestrator")
    assert orchestrator.undispatchable_recovery_reason("fallback-recrop", real_route=False) is None
    assert "no dispatch for" in orchestrator.undispatchable_recovery_reason(
        "page-level-reread", real_route=False
    )
    assert "no dispatch for" in orchestrator.undispatchable_recovery_reason(
        "page-level-reread", real_route=True
    )


def test_real_measured_recrop_is_dispatchable_but_legacy_real_request_is_refused():
    orchestrator = load_stage("orchestrator")
    measured = {
        "origin": "coverage-observation",
        "recovery_bounds": {"x": 1, "y": 2, "w": 3, "h": 4},
        "coverage_observation": {"bounds": {"x": 1, "y": 2, "w": 3, "h": 4}},
        "ink_map_ref": {"relative_path": "ink-map.json", "sha256": "a" * 64},
    }
    assert (
        orchestrator.undispatchable_recovery_reason(
            "fallback-recrop", real_route=True, request_payload=measured
        )
        is None
    )
    legacy = orchestrator.undispatchable_recovery_reason(
        "fallback-recrop", real_route=True, request_payload={"origin": "coverage-observation"}
    )
    assert "legacy fixture-only" in legacy


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
