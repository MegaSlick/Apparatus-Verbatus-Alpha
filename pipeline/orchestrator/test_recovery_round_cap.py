"""The orchestrator's own recovery-round ceiling is a watched refusal."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.contracts.approval import real_ingress_record
from common.contracts.errors import ContractError

ROOT = Path(__file__).resolve().parents[2]

# A stand-in digest for the run-sealed recovery policy. The dispatcher proves the
# policy it reads against the digests the run authority recorded, so a stub that
# omitted either half would be testing a loop nothing bounds.
SEALED_RECOVERY_SHA = "1" * 64


def _sealed_run_tree(
    sealed_recovery_sha: str = SEALED_RECOVERY_SHA, *, ingress: dict | None = None
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

    return lambda *_args: _Tree()


def _sealed_policy(**fields):
    return {"absolute_cap": 3, "config_sha256": SEALED_RECOVERY_SHA, **fields}


def test_a_dispatch_under_a_policy_the_run_never_sealed_refuses(monkeypatch):
    """The unit half of the orchestrator's point-of-use recheck: the run
    authority names one digest, the file on disk carries another."""
    orchestrator = _load_orchestrator()
    monkeypatch.setattr(orchestrator, "RunTree", _sealed_run_tree("2" * 64))
    monkeypatch.setattr(orchestrator, "load_recovery_policy", lambda _path: _sealed_policy())
    args = SimpleNamespace(run_root="unused", run_id="unused", recovery_config="unused")

    with pytest.raises(ContractError, match="recovery configuration changed between"):
        orchestrator.drive_recovery(args, hard_failure_policy={})


def _load_orchestrator():
    path = ROOT / "pipeline/orchestrator/run.py"
    spec = importlib.util.spec_from_file_location("orchestrator_round_cap_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_orchestrator_stops_when_recovery_remains_outstanding_at_the_absolute_cap(monkeypatch):
    """A persistent, already-accounted request must trip the top-level guard.

    The shipped deterministic scenario intentionally requests only one recovery,
    so this injects a durable outstanding record at the boundary the loop itself
    owns.  It proves the orchestrator cannot spin indefinitely if a future
    recovery producer legitimately asks again.
    """
    orchestrator = _load_orchestrator()
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
    orchestrator = _load_orchestrator()
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

    Tyrel's shape for the run-level cap is "if errors happened in chandra stage
    it finishes that section but pauses". So every outstanding act's recrop is cut
    before any reread is asked for, and the checkpoint sits at each of the three
    section boundaries — never between two acts of the same batch, where a second
    already-approved request would be stranded without its owning stage's answer.
    """
    orchestrator = _load_orchestrator()
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


def test_a_breached_checkpoint_ends_the_recovery_round_where_it_was_found(monkeypatch):
    """The tally travels back to `main`, and the rest of the round is not run.

    Every other test in this file stubs the checkpoint to a permanent non-breach,
    so the three `return tally` paths inside a recovery round were never taken.
    The Designator section here finishes — its two recrops were already dispatched
    — and the reread and re-review that would have followed never happen.
    """
    orchestrator = _load_orchestrator()
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


def test_a_real_ingress_recrop_is_refused_by_name_and_recorded_before_anything_is_invoked(
    monkeypatch, capsys
):
    """F068/F083: the cause is named here, not discovered from a stage's exit code.

    The Designator refuses `--operation recover` on a real submission, and
    dispatching it anyway surfaced to an operator as
    `ContractError: pipeline/2_designator/run.py exited 2` — a true statement
    with the reason a stage away. The Recensor no longer publishes such a
    request, so this is the backstop over a tree written before that gate landed:
    it refuses before any subprocess starts, and it records every refused act so
    the run says what it could not answer rather than only that something failed
    (GOVERNANCE 2).
    """
    orchestrator = _load_orchestrator()
    calls = []
    monkeypatch.setattr(orchestrator, "RunTree", _sealed_run_tree(ingress=real_ingress_record()))
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
    with pytest.raises(ContractError, match="fallback recrop on a real submission"):
        orchestrator.drive_recovery(args, hard_failure_policy={})

    assert calls == []
    streams = capsys.readouterr()
    # On stderr, and asserted as stderr. The operator surface records a failed
    # run's detail as `completed.stderr or completed.stdout`, and the
    # `ContractError` this raises is itself printed to stderr, so a listing on
    # stdout would be dropped from the receipt and this refusal would be
    # recorded nowhere a human reads (GOVERNANCE 2).
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
    orchestrator = _load_orchestrator()
    assert orchestrator.undispatchable_recovery_reason("fallback-recrop", real_route=False) is None
    assert "no dispatch for" in orchestrator.undispatchable_recovery_reason(
        "page-level-reread", real_route=False
    )
    assert "no dispatch for" in orchestrator.undispatchable_recovery_reason(
        "page-level-reread", real_route=True
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
