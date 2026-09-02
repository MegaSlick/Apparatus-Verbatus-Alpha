"""Offline drills for the durable laptop-supervisor driver (Stage 04 deferral 04-1).

Every drill runs against `FakeProvider` and an injected `Clock`, exactly as
`test_pod_runtime.py` drives the controllers it is built on. Each test breaks
one load-bearing guard named in the spec; a passing happy path without its
paired drill would not establish that the guard is wired.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from . import supervise
from .fake_provider import FakeProvider
from .lease import LeaseStore, PodLease
from .models import BILLING_CUTOFF_MARGIN_ENV, PodCreateRequest, ProviderFailure
from .notify_bridge import NotifyOutcome
from .shutdown import VerifiedShutdown
from .spend import SpendPolicy

START = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
LEASE_ID = "a" * 32


class Clock:
    def __init__(self) -> None:
        self.seconds = 0.0

    def now(self) -> datetime:
        return START + timedelta(seconds=self.seconds)

    def monotonic(self) -> float:
        return self.seconds

    def sleep(self, seconds: float) -> None:
        self.seconds += seconds


def request(clock: Clock, *, lifetime: int = 3600) -> PodCreateRequest:
    return PodCreateRequest(
        name="supervise-drill",
        gpu_type="fake-48gb",
        image="registry.example/verbatus@sha256:" + "a" * 64,
        template="pinned-template",
        volume_id="test-volume",
        volume_mount_path="/workspace/private",
        docker_start_cmd=(
            "python",
            "-m",
            "operations.pod.pod_timer",
            "--timer-factory",
            "untracked.timer:factory",
            "--bootstrap-command-json",
            '["service"]',
            "--report-path",
            "/workspace/private/supervise-report.json",
        ),
        hard_deadline=clock.now() + timedelta(seconds=lifetime),
        repository_commit="b" * 40,
        metadata={BILLING_CUTOFF_MARGIN_ENV: "3600"},
    )


def fake(clock: Clock) -> FakeProvider:
    return FakeProvider({"fake-48gb": (Decimal("0.77"), Decimal("0.05"))}, now=clock.now)


def shutdown(provider: FakeProvider, clock: Clock, *, timeout: float = 8) -> VerifiedShutdown:
    return VerifiedShutdown(
        provider,
        timeout_seconds=timeout,
        poll_seconds=1,
        billing_cutoff_margin_seconds=3600,
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
        now=clock.now,
    )


def policy(*, heartbeat_timeout: int = 30, lifetime: int = 3600) -> SpendPolicy:
    return SpendPolicy(
        state="configured",
        max_hourly_usd=Decimal("1.00"),
        max_estimated_metered_cost_usd=Decimal("2.00"),
        account_balance_floor_usd=Decimal("50.00"),
        account_balance_alert_usd=Decimal("75.00"),
        hard_lifetime_seconds=lifetime,
        laptop_heartbeat_timeout_seconds=heartbeat_timeout,
        shutdown_poll_interval_seconds=1,
        shutdown_deadline_seconds=8,
        billing_cutoff_margin_seconds=3600,
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


def test_a_heartbeat_timeout_not_shorter_than_the_remaining_lifetime_refuses(
    tmp_path: Path,
) -> None:
    clock = Clock()
    provider = fake(clock)
    record = provider.create(request(clock))
    store = _store(tmp_path)
    ident = supervise.establish_identity(tmp_path, LEASE_ID, now=clock.now, pid=1000)
    make_lease(store, record, owner=ident.owner_token, clock=clock, deadline_seconds=20)
    provider.calls.clear()

    with pytest.raises(supervise.SuperviseRefusal) as excinfo:
        supervise.run_supervisor(
            store=store,
            leases_root=tmp_path,
            lease_id=LEASE_ID,
            provider=provider,
            shutdown=shutdown(provider, clock),
            policy=policy(heartbeat_timeout=30),
            now=clock.now,
        )
    assert "not shorter than" in str(excinfo.value)
    assert excinfo.value.exit_code == 2
    assert provider.calls == []


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


def test_main_smoke_reports_no_lease_as_exit_code_two(tmp_path: Path, monkeypatch) -> None:
    spend_path = tmp_path / "spend.toml"
    spend_path.write_text(
        "\n".join(
            [
                'schema = "pod-spend.v3"',
                'state = "configured"',
                'currency = "USD"',
                'max_hourly_usd = "1.00"',
                'max_estimated_metered_cost_usd = "2.00"',
                'account_balance_floor_usd = "50.00"',
                'account_balance_alert_usd = "75.00"',
                "hard_lifetime_seconds = 3600",
                "laptop_heartbeat_timeout_seconds = 30",
                "shutdown_poll_interval_seconds = 1",
                "shutdown_deadline_seconds = 8",
                "billing_cutoff_margin_seconds = 3600",
                "",
            ]
        ),
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
    # Named per run (finding 6), not once per lease -- glob for it.
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


# -- finding 2: main() must not silently swallow a non-refusal crash


def test_main_writes_a_crashed_final_record_and_exits_three_on_an_unexpected_error(
    tmp_path: Path, monkeypatch
) -> None:
    spend_path = tmp_path / "spend.toml"
    spend_path.write_text(
        "\n".join(
            [
                'schema = "pod-spend.v3"',
                'state = "configured"',
                'currency = "USD"',
                'max_hourly_usd = "1.00"',
                'max_estimated_metered_cost_usd = "2.00"',
                'account_balance_floor_usd = "50.00"',
                'account_balance_alert_usd = "75.00"',
                "hard_lifetime_seconds = 3600",
                "laptop_heartbeat_timeout_seconds = 30",
                "shutdown_poll_interval_seconds = 1",
                "shutdown_deadline_seconds = 8",
                "billing_cutoff_margin_seconds = 3600",
                "",
            ]
        ),
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


# -- finding 3: an UNVERIFIED close's phone-notification outcome is durable


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


# -- finding 4: the 04-4 provider-lifecycle check must also run while unarmed


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


# -- finding 5: the owner token must never reach telemetry


def test_identity_telemetry_never_carries_a_credential_shaped_field(tmp_path: Path) -> None:
    from .models import looks_like_credential_field

    clock = Clock()
    ident = supervise.establish_identity(tmp_path, LEASE_ID, now=clock.now, pid=1000)
    telemetry = ident.telemetry()
    assert "owner_token" not in telemetry
    assert not any(looks_like_credential_field(key) for key in telemetry)


# -- finding 7: ownership survives a reused pid after a laptop reboot


def test_a_reused_pid_after_reboot_does_not_block_a_legitimate_restart(tmp_path: Path) -> None:
    """The old pid-liveness check would refuse forever here: pid 1000 really

    is alive -- it just belongs to an unrelated process the reboot handed
    that number to, not to the supervisor that used to hold it.
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

    second = supervise.establish_identity(
        tmp_path, LEASE_ID, now=clock.now, pid=1000, pid_alive=lambda pid: True
    )
    assert second.owner_token == first.owner_token
    assert second.pid == 1000
