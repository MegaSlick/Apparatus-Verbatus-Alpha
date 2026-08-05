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
        lambda _tree, _policy: [("act_1", "request_1")],
    )
    monkeypatch.setattr(
        orchestrator, "invoke", lambda program, _args, **extra: calls.append((program, extra))
    )

    args = SimpleNamespace(run_root="unused", run_id="unused", recovery_config="unused")
    with pytest.raises(ContractError, match="after 3 rounds"):
        orchestrator.drive_recovery(args)

    assert len(calls) == 9
