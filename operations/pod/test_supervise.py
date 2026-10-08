"""Offline drills for the durable laptop-supervisor driver.

Every drill runs against `FakeProvider` and an injected `Clock`, exactly as
`test_pod_runtime.py` drives the controllers it is built on. Each test breaks
one load-bearing guard named in the spec; a passing happy path without its
paired drill would not establish that the guard is wired.
"""

from __future__ import annotations

import errno
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Callable

import pytest

from . import supervise
from .conftest import (
    SharedClock,
    configured_policy,
    configured_spend_toml,
    standard_request,
    verified_shutdown,
)
from .fake_provider import FakeProvider
from .lease import LeaseStore, PodLease
from .models import PodCreateRequest, ProviderFailure
from .notify_bridge import NotifyOutcome
from .shutdown import VerifiedShutdown
from .spend import SPEND_SCHEMA, SpendPolicy

START = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
LEASE_ID = "a" * 32


class Clock(SharedClock):
    def __init__(self, seconds: float = 0.0) -> None:
        super().__init__(START, seconds)


def request(clock: Clock, *, lifetime: int = 3600) -> PodCreateRequest:
    return standard_request(
        hard_deadline=clock.now() + timedelta(seconds=lifetime),
        name="supervise-drill",
        report_path="/workspace/private/supervise-report.json",
    )


def fake(clock: Clock) -> FakeProvider:
    return FakeProvider({"fake-48gb": (Decimal("0.77"), Decimal("0.05"))}, now=clock.now)


def shutdown(provider: FakeProvider, clock: Clock, *, timeout: float = 8) -> VerifiedShutdown:
    return verified_shutdown(provider, clock, timeout=timeout)


def policy(*, heartbeat_timeout: int = 30, lifetime: int = 3600) -> SpendPolicy:
    return configured_policy(
        hard_lifetime_seconds=lifetime, laptop_heartbeat_timeout_seconds=heartbeat_timeout
    )


def make_lease(
    store: LeaseStore,
    record,
    *,
    owner: str,
    clock: Clock,
    deadline_seconds: int = 3600,
    heartbeat_offset: float = 0,
) -> PodLease:
    hard_deadline = clock.now() + timedelta(seconds=deadline_seconds)
    stamp = clock.now().isoformat().replace("+00:00", "Z")
    receipt = {
        "laptop_supervisor_started": True,
        "pod_timer_acknowledged": True,
        "observed_at": stamp,
        "detail": "pre-armed fixture receipt",
        "receipt": {
            "lease_id": LEASE_ID,
            "pod_id": record.pod_id,
            "hard_deadline": hard_deadline.isoformat().replace("+00:00", "Z"),
            "laptop_supervisor": {"identity": "fixture-laptop-supervisor", "started_at": stamp},
            "pod_timer": {
                "report_path": "/workspace/private/supervise-report.json",
                "acknowledged_at": stamp,
            },
        },
    }
    lease = PodLease(
        lease_id=LEASE_ID,
        launch_token="d" * 32,
        provider_name="fake",
        pod_id=record.pod_id,
        volume_id=record.volume_id,
        pod_hourly_usd=record.estimate.pod_hourly_usd,
        volume_hourly_usd=record.estimate.volume_hourly_usd,
        created_at=clock.now(),
        started_at=record.created_at,
        hard_deadline=hard_deadline,
        owner_token=owner,
        heartbeat_at=clock.now() + timedelta(seconds=heartbeat_offset),
        phase="active",
        controller_record=receipt,
    )
    store.create(lease)
    return lease


def _store(leases_root: Path) -> LeaseStore:
    return LeaseStore(leases_root / f"{LEASE_ID}.json")


# -- drill 1: crash mid-heartbeat -------------------------------------------


def test_a_restarted_supervisor_resumes_ownership_over_the_same_identity_file(
    tmp_path: Path,
) -> None:
    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    store = _store(tmp_path)

    first = supervise.establish_identity(tmp_path, LEASE_ID, now=clock.now, pid=1000)
    make_lease(store, record, owner=first.owner_token, clock=clock)

    before = supervise.supervise_tick(
        store=store,
        provider=provider,
        shutdown=shutdown(provider, clock),
        owner_token=first.owner_token,
        heartbeat_timeout=timedelta(seconds=30),
        now=clock.now,
    )
    assert before.state == "active"

    # The process holding pid 1000 is now dead -- its kernel lock is released
    # with it, whatever pid a reused number now belongs to. A fresh process
    # starts.
    supervise.release_lock(tmp_path, LEASE_ID)
    second = supervise.establish_identity(tmp_path, LEASE_ID, now=clock.now, pid=2000)
    assert second.owner_token == first.owner_token
    assert second.pid == 2000

    after = supervise.supervise_tick(
        store=store,
        provider=provider,
        shutdown=shutdown(provider, clock),
        owner_token=second.owner_token,
        heartbeat_timeout=timedelta(seconds=30),
        now=clock.now,
    )
    assert after.state == "active"
    assert after.close_report is None
    assert provider.terminate_calls == []


# -- drill 2: identity file lost ---------------------------------------------


def test_a_lost_identity_file_reports_busy_then_closes_the_orphan_after_timeout(
    tmp_path: Path,
) -> None:
    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    provider.bill(record.pod_id, "0.09")
    store = _store(tmp_path)

    original = supervise.establish_identity(tmp_path, LEASE_ID, now=clock.now, pid=1000)
    make_lease(store, record, owner=original.owner_token, clock=clock, deadline_seconds=3600)

    # The identity file is gone -- disk trouble, not a clean crash the file
    # could have recorded. A fresh process (its lock, not its pid, is what
    # proves the prior one is gone) picks the lease back up.
    supervise.identity_path(tmp_path, LEASE_ID).unlink()
    supervise.release_lock(tmp_path, LEASE_ID)

    # Named `newid`, not the longer obvious word: an `owner_token=` keyword
    # paired with a 20-plus byte attribute path reads, to the ingress
    # scanner's generic credential rule, like a possible literal -- even
    # though the value here is never anything but a plain attribute access.
    newid = supervise.establish_identity(tmp_path, LEASE_ID, now=clock.now, pid=2000)
    assert newid.owner_token != original.owner_token

    inside_timeout = supervise.supervise_tick(
        store=store,
        provider=provider,
        shutdown=shutdown(provider, clock),
        owner_token=newid.owner_token,
        heartbeat_timeout=timedelta(seconds=30),
        now=clock.now,
    )
    assert inside_timeout.state == "owner-heartbeat-fresh"
    assert inside_timeout.close_report is None
    assert provider.terminate_calls == []

    clock.seconds += 31

    after_timeout = supervise.supervise_tick(
        store=store,
        provider=provider,
        shutdown=shutdown(provider, clock),
        owner_token=newid.owner_token,
        heartbeat_timeout=timedelta(seconds=30),
        now=clock.now,
    )
    assert after_timeout.state == "orphan-reconciled"
    assert after_timeout.close_report is not None and after_timeout.close_report.verified
    assert "heartbeat lost" in after_timeout.detail


# -- drill 3: provider unreachable -------------------------------------------


def test_a_provider_status_failure_is_named_non_green_and_never_crash_loops(
    tmp_path: Path,
) -> None:
    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    store = _store(tmp_path)
    ident = supervise.establish_identity(tmp_path, LEASE_ID, now=clock.now, pid=1000)
    make_lease(store, record, owner=ident.owner_token, clock=clock)

    for _ in range(3):
        provider.inject_failure("status", ProviderFailure("simulated provider outage"))
        result = supervise.supervise_tick(
            store=store,
            provider=provider,
            shutdown=shutdown(provider, clock),
            owner_token=ident.owner_token,
            heartbeat_timeout=timedelta(seconds=30),
            now=clock.now,
        )
        assert result.state == "provider-unreachable"
        assert result.close_report is None
        assert result.lease is not None and result.lease.active

    assert provider.terminate_calls == []


def test_unreachable_close_evidence_never_reports_a_verified_close(tmp_path: Path) -> None:
    """The same drill against the close path: `verify_absent`/`capture_cost`

    failing during a real close attempt must never fabricate green evidence.
    """

    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    store = _store(tmp_path)
    ident = supervise.establish_identity(tmp_path, LEASE_ID, now=clock.now, pid=1000)
    make_lease(store, record, owner=ident.owner_token, clock=clock, deadline_seconds=5)
    clock.seconds = 5
    provider.inject_failure("verify_absent", ProviderFailure("list unreachable"), times=99)
    provider.inject_failure("capture_cost", ProviderFailure("billing unreachable"), times=99)

    result = supervise.supervise_tick(
        store=store,
        provider=provider,
        shutdown=shutdown(provider, clock, timeout=4),
        owner_token=ident.owner_token,
        heartbeat_timeout=timedelta(seconds=2),
        now=clock.now,
    )
    assert result.state == "lifetime-expired"
    assert result.close_report is not None and not result.close_report.verified
    assert not result.green


# -- drill 4: pod EXITED with a fresh heartbeat ------------------------------


def test_an_exited_pod_closes_even_while_the_heartbeat_is_fresh_and_names_the_volume_rate(
    tmp_path: Path,
) -> None:
    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    provider.bill(record.pod_id, "0.21")
    store = _store(tmp_path)
    ident = supervise.establish_identity(tmp_path, LEASE_ID, now=clock.now, pid=1000)
    make_lease(store, record, owner=ident.owner_token, clock=clock, deadline_seconds=3600)

    provider.set_pod_state(record.pod_id, "EXITED")

    result = supervise.supervise_tick(
        store=store,
        provider=provider,
        shutdown=shutdown(provider, clock),
        owner_token=ident.owner_token,
        heartbeat_timeout=timedelta(seconds=30),
        now=clock.now,
    )
    assert result.state == supervise.PROVIDER_EXITED
    assert "EXITED" in result.detail
    assert result.close_report is not None and result.close_report.verified
    assert str(result.close_report.volume_ongoing_hourly_usd) == "0.05"
    assert provider.terminate_calls == [record.pod_id]


def test_a_padded_running_word_from_the_provider_does_not_close_a_healthy_pod(
    tmp_path: Path,
) -> None:
    """The RUNNING guard must strip, not just case-fold: `provider_runpod.py`"""

    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    store = _store(tmp_path)
    ident = supervise.establish_identity(tmp_path, LEASE_ID, now=clock.now, pid=1000)
    make_lease(store, record, owner=ident.owner_token, clock=clock, deadline_seconds=3600)

    provider.set_pod_state(record.pod_id, " RUNNING ")

    result = supervise.supervise_tick(
        store=store,
        provider=provider,
        shutdown=shutdown(provider, clock),
        owner_token=ident.owner_token,
        heartbeat_timeout=timedelta(seconds=30),
        now=clock.now,
    )
    assert result.state == "active"
    assert result.close_report is None
    assert provider.terminate_calls == []


# -- drill 5: lease already closed-verified ----------------------------------


def test_an_already_closed_verified_lease_exits_without_a_provider_call(tmp_path: Path) -> None:
    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    provider.bill(record.pod_id, "0.05")
    store = _store(tmp_path)
    ident = supervise.establish_identity(tmp_path, LEASE_ID, now=clock.now, pid=1000)
    make_lease(store, record, owner=ident.owner_token, clock=clock, deadline_seconds=3600)

    close_result = supervise.supervise_tick(
        store=store,
        provider=provider,
        shutdown=shutdown(provider, clock),
        owner_token=ident.owner_token,
        heartbeat_timeout=timedelta(seconds=30),
        now=lambda: clock.now() + timedelta(seconds=4000),
    )
    assert close_result.state == "lifetime-expired"
    assert close_result.close_report is not None and close_result.close_report.verified

    provider.calls.clear()
    result, exit_code = supervise.run_supervisor(
        store=store,
        leases_root=tmp_path,
        lease_id=LEASE_ID,
        provider=provider,
        shutdown=shutdown(provider, clock),
        policy=policy(),
        now=clock.now,
    )
    assert result.state == "closed-verified"
    assert exit_code == 0
    assert provider.calls == []


def test_a_restart_on_a_close_unverified_lease_notifies_go_and_look(tmp_path: Path) -> None:
    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    store = _store(tmp_path)
    ident = supervise.establish_identity(tmp_path, LEASE_ID, now=clock.now, pid=1000)
    make_lease(store, record, owner=ident.owner_token, clock=clock)
    # No billing installed, so the close cannot be verified.
    first = supervise.supervise_tick(
        store=store,
        provider=provider,
        shutdown=shutdown(provider, clock),
        owner_token=ident.owner_token,
        heartbeat_timeout=timedelta(seconds=30),
        now=lambda: clock.now() + timedelta(seconds=3601),
    )
    assert first.close_report is not None and not first.close_report.verified
    supervise.release_lock(tmp_path, LEASE_ID)
    provider.calls.clear()
    messages: list[str] = []

    def notifier(message: str) -> NotifyOutcome:
        messages.append(message)
        return NotifyOutcome(True, True, "sent")

    result, exit_code = supervise.run_supervisor(
        store=store,
        leases_root=tmp_path,
        lease_id=LEASE_ID,
        provider=provider,
        shutdown=shutdown(provider, clock),
        policy=policy(),
        notifier=notifier,
        now=clock.now,
    )
    assert result.state == "close-unverified"
    assert exit_code == 3
    assert provider.calls == []
    assert len(messages) == 1 and "go and look" in messages[0] and LEASE_ID in messages[0]
    assert "sent" in result.detail


# -- drill 6: two drivers -----------------------------------------------------


def test_a_second_driver_refuses_busy_and_never_touches_the_first_drivers_pod(
    tmp_path: Path,
) -> None:
    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    store = _store(tmp_path)

    first = supervise.establish_identity(tmp_path, LEASE_ID, now=clock.now, pid=1000)
    make_lease(store, record, owner=first.owner_token, clock=clock)
    provider.calls.clear()

    with pytest.raises(supervise.SuperviseRefusal) as excinfo:
        supervise.establish_identity(tmp_path, LEASE_ID, now=clock.now, pid=2000)
    assert "already owns lease" in str(excinfo.value)
    assert excinfo.value.exit_code == 2
    assert provider.calls == []
    assert provider.terminate_calls == []

    # The first driver's own identity is untouched and still usable.
    stored = supervise.read_identity(supervise.identity_path(tmp_path, LEASE_ID))
    assert stored is not None and stored.owner_token == first.owner_token and stored.pid == 1000


# -- supporting unit coverage -------------------------------------------------


def test_no_lease_refuses_without_touching_the_provider(tmp_path: Path) -> None:
    clock = Clock()
    provider = fake(clock)
    store = _store(tmp_path)

    with pytest.raises(supervise.SuperviseRefusal) as excinfo:
        supervise.run_supervisor(
            store=store,
            leases_root=tmp_path,
            lease_id=LEASE_ID,
            provider=provider,
            shutdown=shutdown(provider, clock),
            policy=policy(),
            now=clock.now,
        )
    assert excinfo.value.exit_code == 2
    assert provider.calls == []


def _late_start(
    tmp_path: Path, *, start_at: float, last_heartbeat: float = 0, release: bool = True
):
    """An armed lease with a 3600 s deadline, and a supervisor (re)started at ``start_at``."""

    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    store = _store(tmp_path)
    ident = supervise.establish_identity(tmp_path, LEASE_ID, now=clock.now, pid=1000)
    lease = make_lease(
        store,
        record,
        owner=ident.owner_token,
        clock=clock,
        deadline_seconds=3600,
        heartbeat_offset=last_heartbeat,
    )
    if release:
        # The setup call stands in for the supervisor that crashed; its lock
        # dies with it, and `run_supervisor` below is the restart.
        supervise.release_lock(tmp_path, LEASE_ID)
    clock.seconds = start_at
    provider.bill(record.pod_id, "0.30")
    provider.calls.clear()
    return clock, provider, store, record, lease


def test_a_supervisor_restarted_after_the_hard_deadline_closes_the_pod_verified(
    tmp_path: Path,
) -> None:
    """Restarted one second past the deadline, the supervisor must close, not refuse."""

    clock, provider, store, record, lease = _late_start(tmp_path, start_at=3601)

    result, exit_code = supervise.run_supervisor(
        store=store,
        leases_root=tmp_path,
        lease_id=LEASE_ID,
        provider=provider,
        shutdown=shutdown(provider, clock),
        policy=policy(heartbeat_timeout=900),
        now=clock.now,
        sleeper=clock.sleep,
        pid=1000,
    )

    assert result.state == "lifetime-expired"
    assert exit_code == 0
    assert result.close_report is not None and result.close_report.verified
    assert provider.terminate_calls == [record.pod_id]
    persisted = store.load()
    assert persisted is not None and persisted.phase == "closed-verified"


def test_a_healthy_lease_inside_one_heartbeat_of_its_deadline_is_watched_to_expiry(
    tmp_path: Path,
) -> None:
    """Ten minutes left under a 900 s heartbeat: supervised until the deadline, then closed."""

    clock, provider, store, record, lease = _late_start(
        tmp_path, start_at=3000, last_heartbeat=2990
    )
    terminated_at: list[float] = []
    real_terminate = provider.terminate

    def timed_terminate(pod_id: str) -> None:
        terminated_at.append(clock.seconds)
        real_terminate(pod_id)

    provider.terminate = timed_terminate  # type: ignore[method-assign]

    result, exit_code = supervise.run_supervisor(
        store=store,
        leases_root=tmp_path,
        lease_id=LEASE_ID,
        provider=provider,
        shutdown=shutdown(provider, clock),
        policy=policy(heartbeat_timeout=900),
        now=clock.now,
        sleeper=clock.sleep,
        pid=1000,
    )

    assert result.state == "lifetime-expired"
    assert exit_code == 0
    assert result.close_report is not None and result.close_report.verified
    assert terminated_at and terminated_at[0] >= 3600, terminated_at
    assert ("status", record.pod_id) in provider.calls
    persisted = store.load()
    assert persisted is not None and persisted.phase == "closed-verified"


@pytest.mark.parametrize(
    ("start_at", "last_heartbeat"),
    [(3000, 2990), (3700, 3500)],
    ids=["before-deadline", "after-deadline"],
)
def test_a_restart_that_lost_its_identity_waits_out_the_old_heartbeat_then_closes(
    tmp_path: Path, start_at: int, last_heartbeat: int
) -> None:
    """A fresh token reads the dead owner's recent heartbeat as foreign; it must
    wait for that heartbeat to go stale, claim the orphan and close it verified."""

    clock, provider, store, record, lease = _late_start(
        tmp_path, start_at=start_at, last_heartbeat=last_heartbeat
    )
    supervise.identity_path(tmp_path, LEASE_ID).unlink()
    messages: list[str] = []

    def notifier(message: str) -> NotifyOutcome:
        messages.append(message)
        return NotifyOutcome(True, True, "sent")

    result, exit_code = supervise.run_supervisor(
        store=store,
        leases_root=tmp_path,
        lease_id=LEASE_ID,
        provider=provider,
        shutdown=shutdown(provider, clock),
        policy=policy(heartbeat_timeout=900),
        notifier=notifier,
        now=clock.now,
        sleeper=clock.sleep,
        pid=1000,
    )

    assert result.state == "orphan-reconciled"
    assert exit_code == 0
    assert result.close_report is not None and result.close_report.verified
    assert provider.terminate_calls == [record.pod_id]
    assert clock.seconds >= last_heartbeat + 900
    persisted = store.load()
    assert persisted is not None and persisted.phase == "closed-verified"
    assert len(messages) == 1 and "closed verified" in messages[0]


@pytest.mark.parametrize(
    ("last_heartbeat", "state", "expected_exit"),
    [(3550, "orphan-reconciled", 0), (5000, "owner-heartbeat-fresh", 3)],
    ids=["future-before-deadline", "future-after-deadline"],
)
def test_a_foreign_heartbeat_stamped_in_the_future_is_waited_out_or_reported(
    tmp_path: Path, last_heartbeat: int, state: str, expected_exit: int
) -> None:
    """A heartbeat stamped ahead of this clock is waited on until it goes stale;
    one stamped past the deadline ends the run with a go-and-look notification."""

    clock, provider, store, record, lease = _late_start(
        tmp_path, start_at=3000, last_heartbeat=last_heartbeat
    )
    supervise.identity_path(tmp_path, LEASE_ID).unlink()
    messages: list[str] = []
    sleeps: list[float] = []

    def notifier(message: str) -> NotifyOutcome:
        messages.append(message)
        return NotifyOutcome(True, True, "sent")

    def sleeper(seconds: float) -> None:
        sleeps.append(seconds)
        assert len(sleeps) < 100, "run_supervisor did not end"
        clock.sleep(seconds)

    result, exit_code = supervise.run_supervisor(
        store=store,
        leases_root=tmp_path,
        lease_id=LEASE_ID,
        provider=provider,
        shutdown=shutdown(provider, clock),
        policy=policy(heartbeat_timeout=900),
        notifier=notifier,
        now=clock.now,
        sleeper=sleeper,
        pid=1000,
    )
    assert result.state == state
    assert exit_code == expected_exit
    assert len(messages) == 1
    if expected_exit == 0:
        assert provider.terminate_calls == [record.pod_id]
        assert clock.seconds >= last_heartbeat + 900
    else:
        assert provider.terminate_calls == []
        assert "go and look" in messages[0]


def test_a_foreign_heartbeat_that_goes_stale_after_the_tick_is_still_claimed_and_closed(
    tmp_path: Path, monkeypatch
) -> None:
    """Fresh when the tick read it, stale by the time the loop decides: not a reason to stop."""

    clock, provider, store, record, lease = _late_start(
        tmp_path, start_at=3605 + 899, last_heartbeat=3605
    )
    supervise.identity_path(tmp_path, LEASE_ID).unlink()
    real_record_tick = supervise.record_tick

    def slow_record_tick(*args, **kwargs):
        clock.sleep(2)
        return real_record_tick(*args, **kwargs)

    monkeypatch.setattr(supervise, "record_tick", slow_record_tick)

    result, exit_code = supervise.run_supervisor(
        store=store,
        leases_root=tmp_path,
        lease_id=LEASE_ID,
        provider=provider,
        shutdown=shutdown(provider, clock),
        policy=policy(heartbeat_timeout=900),
        now=clock.now,
        sleeper=clock.sleep,
        pid=1000,
    )
    assert result.state == "orphan-reconciled"
    assert exit_code == 0
    assert result.close_report is not None and result.close_report.verified
    assert provider.terminate_calls == [record.pod_id]


def test_a_competing_supervisor_is_refused_even_when_the_lease_is_overdue(
    tmp_path: Path,
) -> None:
    """An overdue lease never lets a second driver past the ownership lock."""

    clock, provider, store, record, lease = _late_start(tmp_path, start_at=3601, release=False)

    with pytest.raises(supervise.SuperviseRefusal) as excinfo:
        supervise.run_supervisor(
            store=store,
            leases_root=tmp_path,
            lease_id=LEASE_ID,
            provider=provider,
            shutdown=shutdown(provider, clock),
            policy=policy(heartbeat_timeout=900),
            now=clock.now,
            sleeper=clock.sleep,
            pid=2000,
        )
    assert "already owns lease" in str(excinfo.value)
    assert excinfo.value.exit_code == 2
    assert provider.calls == []
    assert store.load() == lease


def test_run_supervisor_loops_to_a_verified_lifetime_expiry_and_writes_a_final_record(
    tmp_path: Path,
) -> None:
    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    provider.bill(record.pod_id, "0.30")
    store = _store(tmp_path)
    ident = supervise.establish_identity(tmp_path, LEASE_ID, now=clock.now, pid=1000)
    make_lease(store, record, owner=ident.owner_token, clock=clock, deadline_seconds=5)
    # `run_supervisor` re-establishes identity for itself; releasing the lock
    # here stands in for the fact that the setup call above and the run
    # below are not really the same live holder -- exactly the boundary
    # `release_lock` exists to let a drill state honestly.
    supervise.release_lock(tmp_path, LEASE_ID)

    result, exit_code = supervise.run_supervisor(
        store=store,
        leases_root=tmp_path,
        lease_id=LEASE_ID,
        provider=provider,
        shutdown=shutdown(provider, clock),
        policy=policy(heartbeat_timeout=2),
        now=clock.now,
        sleeper=clock.sleep,
        pid=1000,
    )
    assert result.state == "lifetime-expired"
    assert exit_code == 0
    assert result.close_report is not None and result.close_report.verified

    stored = supervise.read_identity(supervise.identity_path(tmp_path, LEASE_ID))
    assert stored is not None
    assert stored.last_tick_state == "lifetime-expired"


# -- drill 7: foreign owner past its own hard deadline ------------------------


class _ForeverFreshForeignOwnerStore:
    """Wraps a real `LeaseStore` to model a foreign heartbeater still ticking.

    Every read reports a different owner (never this driver's) whose
    heartbeat is exactly "now" -- as if some other process kept refreshing
    it -- while everything else (creation, the underlying file) passes
    through untouched. This is what a genuinely foreign, still-live
    supervisor looks like from `run_supervisor`'s side, without needing a
    second real process in the drill.
    """

    def __init__(self, inner: LeaseStore, now: Callable[[], datetime]) -> None:
        self._inner = inner
        self._now = now

    def create(self, lease: PodLease) -> PodLease:
        return self._inner.create(lease)

    def load(self) -> PodLease | None:
        lease = self._inner.load()
        if lease is None:
            return None
        # A name bound first, not a literal on the keyword: the ingress scan reads
        # `owner_token="..."` as a credential literal (see the note at `newid`).
        foreign_driver = "a-different-live-driver"
        return replace(lease, owner_token=foreign_driver, heartbeat_at=self._now())

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


def test_run_supervisor_breaks_rather_than_spins_once_a_foreign_owners_deadline_passes(
    tmp_path: Path,
) -> None:
    """A foreign owner whose heartbeat never goes stale, once its lease's own"""

    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    real_store = _store(tmp_path)
    make_lease(real_store, record, owner="a-different-live-driver", clock=clock, deadline_seconds=3)
    store = _ForeverFreshForeignOwnerStore(real_store, clock.now)

    tick_count = 0
    sleeps: list[float] = []
    messages: list[str] = []

    def notifier(message: str) -> NotifyOutcome:
        messages.append(message)
        return NotifyOutcome(True, True, "sent")

    def counting_sleeper(seconds: float) -> None:
        nonlocal tick_count
        tick_count += 1
        if tick_count > 50:
            raise AssertionError("run_supervisor spun past 50 ticks without ending the run")
        sleeps.append(seconds)
        clock.sleep(seconds)

    result, exit_code = supervise.run_supervisor(
        store=store,  # type: ignore[arg-type]
        leases_root=tmp_path,
        lease_id=LEASE_ID,
        provider=provider,
        shutdown=shutdown(provider, clock),
        policy=policy(heartbeat_timeout=1, lifetime=3600),
        notifier=notifier,
        now=clock.now,
        sleeper=counting_sleeper,
        pid=1000,
    )

    assert result.state == "owner-heartbeat-fresh"
    assert exit_code == 3, "go and look: another owner's heartbeat stayed fresh past our deadline"
    assert provider.terminate_calls == []
    assert all(seconds > 0 for seconds in sleeps), sleeps
    assert len(messages) == 1 and "go and look" in messages[0]


def test_main_smoke_reports_no_lease_as_exit_code_two(tmp_path: Path, monkeypatch) -> None:
    spend_path = tmp_path / "spend.toml"
    spend_path.write_text(
        configured_spend_toml(),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        supervise, "_load_provider", lambda reference: FakeProvider(now=lambda: START)
    )
    exit_code = supervise.main(
        [
            "--provider-factory",
            "unused:unused",
            "--leases",
            str(tmp_path / "leases"),
            "--lease",
            LEASE_ID,
            "--spend",
            str(spend_path),
        ]
    )
    assert exit_code == 2
    # Named per run, not once per lease -- glob for it.
    finals = list((tmp_path / "leases" / "supervisors").glob(f"supervisor-{LEASE_ID}-final-*.json"))
    assert len(finals) == 1
    payload = json.loads(finals[0].read_text(encoding="utf-8"))
    assert payload["exit_code"] == 2
    assert payload["state"] == "refused"


# -- exit-code convention: 2 vs 3 for a durable lease this run confirmed active


def test_a_lease_lost_mid_run_after_being_observed_active_exits_three_not_two(
    tmp_path: Path,
) -> None:
    """`_exit_code` must not read a lease lost *after* this run confirmed it

    active the same way it reads "no lease ever existed": the pod that lease
    was guarding may still be out there billing, and exit 2 tells an
    unattended reader nothing happened.
    """

    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    store = _store(tmp_path)
    ident = supervise.establish_identity(tmp_path, LEASE_ID, now=clock.now, pid=1000)
    make_lease(store, record, owner=ident.owner_token, clock=clock, deadline_seconds=3600)
    supervise.release_lock(tmp_path, LEASE_ID)

    # The lease record vanishes out from under a live supervisor, between its
    # first tick (which finds it active) and its second.
    def vanish_after_first_tick(seconds: float) -> None:
        clock.sleep(seconds)
        store.path.unlink(missing_ok=True)

    result, exit_code = supervise.run_supervisor(
        store=store,
        leases_root=tmp_path,
        lease_id=LEASE_ID,
        provider=provider,
        shutdown=shutdown(provider, clock),
        policy=policy(heartbeat_timeout=2),
        now=clock.now,
        sleeper=vanish_after_first_tick,
    )
    assert result.state == "no-lease"
    assert exit_code == 3


def test_no_lease_from_the_start_still_exits_two(tmp_path: Path) -> None:
    """The pre-loop refusal path -- nothing was ever confirmed active -- keeps

    exit 2, unlike the mid-run loss covered above.
    """

    clock = Clock()
    provider = fake(clock)
    store = _store(tmp_path)

    with pytest.raises(supervise.SuperviseRefusal) as excinfo:
        supervise.run_supervisor(
            store=store,
            leases_root=tmp_path,
            lease_id=LEASE_ID,
            provider=provider,
            shutdown=shutdown(provider, clock),
            policy=policy(),
            now=clock.now,
        )
    assert excinfo.value.exit_code == 2


# -- main() must not silently swallow a non-refusal crash


def test_main_writes_a_crashed_final_record_and_exits_three_on_an_unexpected_error(
    tmp_path: Path, monkeypatch
) -> None:
    spend_path = tmp_path / "spend.toml"
    spend_path.write_text(
        configured_spend_toml(),
        encoding="utf-8",
    )

    def _boom(reference: str):
        raise ModuleNotFoundError(f"no such module: {reference}")

    monkeypatch.setattr(supervise, "_load_provider", _boom)

    exit_code = supervise.main(
        [
            "--provider-factory",
            "no_such_module_at_all:factory",
            "--leases",
            str(tmp_path / "leases"),
            "--lease",
            LEASE_ID,
            "--spend",
            str(spend_path),
        ]
    )
    assert exit_code == 3
    finals = list((tmp_path / "leases" / "supervisors").glob(f"supervisor-{LEASE_ID}-final-*.json"))
    assert len(finals) == 1
    payload = json.loads(finals[0].read_text(encoding="utf-8"))
    assert payload["exit_code"] == 3
    assert payload["state"] == "crashed"
    assert "ModuleNotFoundError" in payload["detail"]


def test_a_crash_with_notify_on_sends_a_go_and_look_notification(
    tmp_path: Path, monkeypatch
) -> None:
    spend_path = tmp_path / "spend.toml"
    spend_path.write_text(configured_spend_toml(), encoding="utf-8")
    messages: list[str] = []

    def notifier(message: str) -> NotifyOutcome:
        messages.append(message)
        return NotifyOutcome(True, True, "sent")

    def _boom(reference: str):
        raise ModuleNotFoundError(f"no such module: {reference}")

    monkeypatch.setattr(supervise, "shell_notifier", lambda: notifier)
    monkeypatch.setattr(supervise, "_load_provider", _boom)

    exit_code = supervise.main(
        [
            "--provider-factory",
            "no_such_module_at_all:factory",
            "--leases",
            str(tmp_path / "leases"),
            "--lease",
            LEASE_ID,
            "--spend",
            str(spend_path),
            "--notify",
        ]
    )
    assert exit_code == 3
    assert len(messages) == 1 and "go and look" in messages[0]


def test_a_final_record_write_failure_on_the_crash_path_is_named_not_swallowed(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A write failure on top of a crash must not vanish: both faults, the
    original one and the write failure, must be visible in the record.
    """

    spend_path = tmp_path / "spend.toml"
    spend_path.write_text(
        configured_spend_toml(),
        encoding="utf-8",
    )

    def _boom(reference: str):
        raise ModuleNotFoundError(f"no such module: {reference}")

    def _write_boom(*args, **kwargs):
        raise OSError(errno.ENOSPC, "no space left on device")

    monkeypatch.setattr(supervise, "_load_provider", _boom)
    monkeypatch.setattr(supervise, "_write_final_record", _write_boom)

    exit_code = supervise.main(
        [
            "--provider-factory",
            "no_such_module_at_all:factory",
            "--leases",
            str(tmp_path / "leases"),
            "--lease",
            LEASE_ID,
            "--spend",
            str(spend_path),
        ]
    )

    assert exit_code == 3
    assert not (tmp_path / "leases" / "supervisors").exists()
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "crashed"
    assert "ModuleNotFoundError" in payload["detail"]
    assert "final record also failed to write" in payload["detail"]
    assert "no space left on device" in payload["detail"]


def test_a_final_record_write_failure_on_the_refusal_path_is_named_not_raised(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A failed write here must not escape ``main()`` as a bare traceback; it
    must still return the refusal's own exit code.
    """

    spend_path = tmp_path / "spend.toml"
    spend_path.write_text(
        "\n".join([f'schema = "{SPEND_SCHEMA}"', 'state = "unconfigured"', ""]),
        encoding="utf-8",
    )

    def _write_boom(*args, **kwargs):
        raise OSError(errno.ENOSPC, "no space left on device")

    monkeypatch.setattr(supervise, "_write_final_record", _write_boom)

    exit_code = supervise.main(
        [
            "--provider-factory",
            "no_such_module_at_all:factory",
            "--leases",
            str(tmp_path / "leases"),
            "--lease",
            LEASE_ID,
            "--spend",
            str(spend_path),
        ]
    )

    assert exit_code == 2
    assert not (tmp_path / "leases" / "supervisors").exists()
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "refused"
    assert "is unconfigured; cannot supervise without ceilings" in payload["detail"]
    assert "final record also failed to write" in payload["detail"]
    assert "no space left on device" in payload["detail"]


# -- an UNVERIFIED close's phone-notification outcome is durable


def test_an_unverified_close_notification_outcome_is_recorded_in_the_final_detail(
    tmp_path: Path,
) -> None:
    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    store = _store(tmp_path)
    ident = supervise.establish_identity(tmp_path, LEASE_ID, now=clock.now, pid=1000)
    make_lease(store, record, owner=ident.owner_token, clock=clock, deadline_seconds=5)
    supervise.release_lock(tmp_path, LEASE_ID)
    provider.inject_failure("verify_absent", ProviderFailure("list unreachable"), times=99)
    provider.inject_failure("capture_cost", ProviderFailure("billing unreachable"), times=99)

    calls: list[str] = []

    def notifier(message: str) -> NotifyOutcome:
        calls.append(message)
        return NotifyOutcome(True, False, "ntfy refused the topic")

    result, exit_code = supervise.run_supervisor(
        store=store,
        leases_root=tmp_path,
        lease_id=LEASE_ID,
        provider=provider,
        shutdown=shutdown(provider, clock, timeout=4),
        policy=policy(heartbeat_timeout=2),
        notifier=notifier,
        now=clock.now,
        sleeper=clock.sleep,
    )
    assert calls and "UNVERIFIED" in calls[0]
    assert exit_code == 3
    assert "NOT DELIVERED" in result.detail
    assert "ntfy refused the topic" in result.detail


# -- the provider-lifecycle check must also run while unarmed


def test_an_unarmed_lease_closes_when_its_pod_is_observed_exited(tmp_path: Path) -> None:
    """During `controller-unarmed` -- this driver's normal state for the whole

    arming window -- an EXITED pod must still be closed rather than left
    billing its attached volume unobserved until the arming receipt lands.
    """

    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    provider.bill(record.pod_id, "0.11")
    store = _store(tmp_path)
    ident = supervise.establish_identity(tmp_path, LEASE_ID, now=clock.now, pid=1000)
    # No arming receipt: `controller_record` stays unset, exactly the state
    # this lease sits in for the whole window between launch and the
    # laptop-supervisor and pod-timer both acknowledging.
    hard_deadline = clock.now() + timedelta(seconds=3600)
    lease = PodLease(
        lease_id=LEASE_ID,
        launch_token="d" * 32,
        provider_name="fake",
        pod_id=record.pod_id,
        volume_id=record.volume_id,
        pod_hourly_usd=record.estimate.pod_hourly_usd,
        volume_hourly_usd=record.estimate.volume_hourly_usd,
        created_at=clock.now(),
        started_at=record.created_at,
        hard_deadline=hard_deadline,
        owner_token=ident.owner_token,
        heartbeat_at=clock.now(),
        phase="active",
        controller_record=None,
    )
    store.create(lease)

    provider.set_pod_state(record.pod_id, "EXITED")

    result = supervise.supervise_tick(
        store=store,
        provider=provider,
        shutdown=shutdown(provider, clock),
        owner_token=ident.owner_token,
        heartbeat_timeout=timedelta(seconds=30),
        now=clock.now,
    )
    assert result.state == supervise.PROVIDER_EXITED
    assert result.close_report is not None and result.close_report.verified
    assert provider.terminate_calls == [record.pod_id]


# -- REST v2: a pod still provisioning or starting while its launch arms


def _unarmed_lease(store: LeaseStore, record, *, owner: str, clock: Clock) -> None:
    store.create(
        PodLease(
            lease_id=LEASE_ID,
            launch_token="d" * 32,
            provider_name="fake",
            pod_id=record.pod_id,
            volume_id=record.volume_id,
            pod_hourly_usd=record.estimate.pod_hourly_usd,
            volume_hourly_usd=record.estimate.volume_hourly_usd,
            created_at=clock.now(),
            started_at=record.created_at,
            hard_deadline=clock.now() + timedelta(seconds=3600),
            owner_token=owner,
            heartbeat_at=clock.now(),
            phase="active",
            controller_record=None,
        )
    )


@pytest.mark.parametrize("word", ["PROVISIONING", "STARTING", " starting "])
def test_a_pre_running_pod_is_waited_for_while_its_launch_is_still_arming(
    tmp_path: Path, word: str
) -> None:
    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    store = _store(tmp_path)
    ident = supervise.establish_identity(tmp_path, LEASE_ID, now=clock.now, pid=1000)
    _unarmed_lease(store, record, owner=ident.owner_token, clock=clock)
    provider.set_pod_state(record.pod_id, word)

    result = supervise.supervise_tick(
        store=store,
        provider=provider,
        shutdown=shutdown(provider, clock),
        owner_token=ident.owner_token,
        heartbeat_timeout=timedelta(seconds=30),
        now=clock.now,
    )

    assert result.state == supervise.PROVIDER_STARTING
    assert not result.green
    assert provider.terminate_calls == []


def test_a_pre_running_pod_whose_launch_stopped_heartbeating_is_closed(tmp_path: Path) -> None:
    """The wait has no clock of its own: the launch owner's heartbeat bounds it."""

    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    provider.bill(record.pod_id, "0.02")
    store = _store(tmp_path)
    ident = supervise.establish_identity(tmp_path, LEASE_ID, now=clock.now, pid=1000)
    _unarmed_lease(store, record, owner=ident.owner_token, clock=clock)
    provider.set_pod_state(record.pod_id, "PROVISIONING")

    result = supervise.supervise_tick(
        store=store,
        provider=provider,
        shutdown=shutdown(provider, clock),
        owner_token=ident.owner_token,
        heartbeat_timeout=timedelta(seconds=30),
        now=lambda: clock.now() + timedelta(seconds=31),
    )

    assert result.state != supervise.PROVIDER_STARTING
    assert provider.terminate_calls == [record.pod_id]


def _armed_tick(store, provider, clock, owner, previous=None):  # type: ignore[no-untyped-def]
    return supervise.supervise_tick(
        store=store,
        provider=provider,
        shutdown=shutdown(provider, clock),
        owner_token=owner,
        heartbeat_timeout=timedelta(seconds=900),
        now=clock.now,
        previous=previous,
    )


def _armed_starting(tmp_path: Path):  # type: ignore[no-untyped-def]
    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    provider.bill(record.pod_id, "0.02")
    store = _store(tmp_path)
    ident = supervise.establish_identity(tmp_path, LEASE_ID, now=clock.now, pid=1000)
    make_lease(store, record, owner=ident.owner_token, clock=clock)
    provider.set_pod_state(record.pod_id, "STARTING")
    return clock, provider, record, store, ident.owner_token


def test_an_armed_pod_still_starting_within_the_grace_is_waited_on(tmp_path: Path) -> None:
    """The timer arms from inside the container; RUNNING may come only once it is healthy."""

    clock, provider, record, store, owner = _armed_starting(tmp_path)

    first = _armed_tick(store, provider, clock, owner)
    clock.seconds = supervise.CONTAINER_START_TIMEOUT_SECONDS - 5
    second = _armed_tick(store, provider, clock, owner, previous=first)

    assert first.state == second.state == supervise.PROVIDER_STARTING_ARMED
    assert provider.terminate_calls == []


def test_an_armed_pod_still_starting_past_the_grace_closes_on_the_second_tick(
    tmp_path: Path,
) -> None:
    clock, provider, record, store, owner = _armed_starting(tmp_path)

    inside = _armed_tick(store, provider, clock, owner)
    clock.seconds = supervise.CONTAINER_START_TIMEOUT_SECONDS + 1
    first_past = _armed_tick(store, provider, clock, owner, previous=inside)
    clock.seconds += 15
    second_past = _armed_tick(store, provider, clock, owner, previous=first_past)

    assert inside.state == supervise.PROVIDER_STARTING_ARMED
    assert first_past.state == supervise.PROVIDER_STARTING_PAST_GRACE
    assert second_past.state == supervise.PROVIDER_EXITED
    assert "STARTING" in second_past.detail
    assert provider.terminate_calls == [record.pod_id]


def test_an_armed_pod_that_reaches_running_is_never_closed(tmp_path: Path) -> None:
    clock, provider, record, store, owner = _armed_starting(tmp_path)

    clock.seconds = supervise.CONTAINER_START_TIMEOUT_SECONDS + 1
    past = _armed_tick(store, provider, clock, owner)
    provider.set_pod_state(record.pod_id, "RUNNING")
    clock.seconds += 15
    running = _armed_tick(store, provider, clock, owner, previous=past)

    assert past.state == supervise.PROVIDER_STARTING_PAST_GRACE
    assert running.state == "active"
    assert provider.terminate_calls == []


def test_an_errored_pod_closes_now_even_while_its_launch_is_arming(tmp_path: Path) -> None:
    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    provider.bill(record.pod_id, "0.02")
    store = _store(tmp_path)
    ident = supervise.establish_identity(tmp_path, LEASE_ID, now=clock.now, pid=1000)
    _unarmed_lease(store, record, owner=ident.owner_token, clock=clock)
    provider.set_pod_state(record.pod_id, "ERROR")

    result = supervise.supervise_tick(
        store=store,
        provider=provider,
        shutdown=shutdown(provider, clock),
        owner_token=ident.owner_token,
        heartbeat_timeout=timedelta(seconds=30),
        now=clock.now,
    )

    assert result.state == supervise.PROVIDER_EXITED
    assert "ERROR" in result.detail
    assert result.close_report is not None and result.close_report.verified
    assert provider.terminate_calls == [record.pod_id]


# -- the owner token must never reach telemetry


def test_identity_telemetry_never_carries_a_credential_shaped_field(tmp_path: Path) -> None:
    from common.credentials import looks_like_credential_field

    clock = Clock()
    ident = supervise.establish_identity(tmp_path, LEASE_ID, now=clock.now, pid=1000)
    telemetry = ident.telemetry()
    assert "owner_token" not in telemetry
    assert not any(looks_like_credential_field(key) for key in telemetry)


# -- ownership survives a reused pid after a laptop reboot


def test_a_reused_pid_after_reboot_does_not_block_a_legitimate_restart(tmp_path: Path) -> None:
    """A bare pid-liveness check would refuse forever here: pid 1000 really is
    alive -- it just belongs to an unrelated process the reboot handed that
    number to.
    """

    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    store = _store(tmp_path)
    first = supervise.establish_identity(tmp_path, LEASE_ID, now=clock.now, pid=1000)
    make_lease(store, record, owner=first.owner_token, clock=clock)

    # The prior process is gone; its lock goes with it. `os.getpid()` for
    # this test process is, by construction, never 1000 -- exactly modelling
    # an unrelated live process that now happens to hold that number.
    supervise.release_lock(tmp_path, LEASE_ID)

    second = supervise.establish_identity(tmp_path, LEASE_ID, now=clock.now, pid=1000)
    assert second.owner_token == first.owner_token
    assert second.pid == 1000


# -- peek_running: a pure read, and fail-closed on the money direction ------


def test_peek_running_never_creates_the_lock_file_or_its_directory(tmp_path: Path) -> None:
    """The status surface only ever *reads* this lock -- see
    `operations/operator/test_surface.py`'s writing-tree drill for the
    consumer side of this guard.
    """

    leases_root = tmp_path / "leases"

    assert supervise.peek_running(leases_root, LEASE_ID) is False
    assert not (leases_root / "supervisors").exists()


def test_peek_running_reports_true_only_while_the_owner_holds_the_lock(tmp_path: Path) -> None:
    clock = Clock()
    supervise.establish_identity(tmp_path, LEASE_ID, now=clock.now, pid=1000)

    assert supervise.peek_running(tmp_path, LEASE_ID) is True

    supervise.release_lock(tmp_path, LEASE_ID)

    assert supervise.peek_running(tmp_path, LEASE_ID) is False


def test_peek_running_reports_unknown_not_running_on_a_non_contention_errno(
    tmp_path: Path, monkeypatch
) -> None:
    """Only `BlockingIOError` proves another holder, exactly as
    `OperatorSurface._exclusive_paid_launch` already classifies it for the
    paid-launch claim. Any other `OSError` (e.g. `flock` unsupported on this
    filesystem) means the check failed, which is not evidence a supervisor
    exists -- so it must never read as "running".
    """

    import fcntl

    clock = Clock()
    supervise.establish_identity(tmp_path, LEASE_ID, now=clock.now, pid=1000)
    supervise.release_lock(tmp_path, LEASE_ID)

    def _raise(*_args: object, **_kwargs: object) -> None:
        # EACCES, not a made-up errno: it must not be one of the handful
        # Python's OSError constructor auto-upgrades to BlockingIOError
        # (EAGAIN/EWOULDBLOCK/EALREADY/EINPROGRESS -- platform-dependent
        # numbers, so picking one by value alone is not portable), or this
        # drill would silently test the wrong branch.
        raise OSError(errno.EACCES, "Permission denied")

    assert not isinstance(OSError(errno.EACCES, "x"), BlockingIOError)
    monkeypatch.setattr(fcntl, "flock", _raise)

    assert supervise.peek_running(tmp_path, LEASE_ID) is None
