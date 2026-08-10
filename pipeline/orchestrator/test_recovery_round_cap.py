"""The orchestrator's own recovery-round ceiling is a watched refusal."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.contracts.errors import ContractError

ROOT = Path(__file__).resolve().parents[2]


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
    monkeypatch.setattr(orchestrator, "RunTree", lambda *_args: object())
    monkeypatch.setattr(orchestrator, "load_recovery_policy", lambda _path: {"absolute_cap": 3})
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
    # the fake `RunTree` above (a bare `object()`) is never asked to behave like one.
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
    monkeypatch.setattr(orchestrator, "RunTree", lambda *_args: object())
    monkeypatch.setattr(orchestrator, "load_recovery_policy", lambda _path: {"absolute_cap": 3})
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
    monkeypatch.setattr(orchestrator, "RunTree", lambda *_args: object())
    monkeypatch.setattr(orchestrator, "load_recovery_policy", lambda _path: {"absolute_cap": 3})
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
    monkeypatch.setattr(orchestrator, "RunTree", lambda *_args: object())
    monkeypatch.setattr(orchestrator, "load_recovery_policy", lambda _path: {"absolute_cap": 3})
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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
