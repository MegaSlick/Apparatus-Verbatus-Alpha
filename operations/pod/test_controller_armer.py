"""Offline drills for the two-controller armer and its report channel.

Every test here runs against an in-memory `TimerReportChannel`, a fake
supervisor starter and an injected `Clock`. Nothing reaches a provider, a
network, or a real process: the armer's whole job is deciding what an
observation means, and each drill below breaks exactly one of the conditions
that decision rests on.

The two that matter most are the last kind: the receipt this armer produces
has to satisfy *both* validators that stand between it and a green launch --
`launch.PodRuntime._validate_arming_binding` and, through
`LeaseStore.record_controller_arming`, `lease._validate_controller_record` --
and the observing drill armer must refuse a perfect report.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from . import durable, supervise
from .arming import ControllerArming
from .controller_armer import (
    ACKNOWLEDGEMENT_FUTURE_SKEW_SECONDS,
    ARMING_DRILL_SCHEMA,
    CONTROLLER_ARMING_TIMEOUT_SECONDS,
    ChannelControllerArmer,
    ObservingControllerArmer,
    report_key,
    report_path_of,
)
from .fake_provider import FakeProvider
from .launch import PodRuntime
from .lease import LeaseStore, PodLease
from .models import (
    BILLING_CUTOFF_MARGIN_ENV,
    POD_REPORT_SCHEMA,
    PodCreateRequest,
    looks_like_credential_field,
)
from .spend import SpendPolicy

SPEND_FILE = str(Path(__file__).resolve().parent / "spend.py")
START = datetime(2026, 9, 2, 9, 0, tzinfo=UTC)
LEASE_ID = "c" * 32
OWNER = "e" * 32
REPORT_PATH = "/workspace/private/pod-report-launch.json"
REPORT_OBJECT = "pod-report-launch.json"


class Clock:
    def __init__(self) -> None:
        self.seconds = 0.0

    def now(self) -> datetime:
        return START + timedelta(seconds=self.seconds)

    def sleep(self, seconds: float) -> None:
        self.seconds += seconds


def stamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def request(clock: Clock, *, lifetime: int = 3600) -> PodCreateRequest:
    return PodCreateRequest(
        name="armer-drill",
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
            REPORT_PATH,
        ),
        hard_deadline=clock.now() + timedelta(seconds=lifetime),
        repository_commit="b" * 40,
        metadata={BILLING_CUTOFF_MARGIN_ENV: "3600"},
    )


def policy() -> SpendPolicy:
    return SpendPolicy(
        state="configured",
        max_hourly_usd=Decimal("1.00"),
        max_estimated_metered_cost_usd=Decimal("2.00"),
        account_balance_floor_usd=Decimal("50.00"),
        account_balance_alert_usd=Decimal("75.00"),
        hard_lifetime_seconds=3600,
        laptop_heartbeat_timeout_seconds=30,
        shutdown_poll_interval_seconds=1,
        shutdown_deadline_seconds=8,
        billing_cutoff_margin_seconds=3600,
    )


class InMemoryChannel:
    """The seam, in a dict: bytes, a proven absence, or a refusal to answer."""

    def __init__(
        self,
        objects: dict[str, bytes] | None = None,
        *,
        error: Exception | None = None,
        appears_after: int | None = None,
        deferred: tuple[str, bytes] | None = None,
    ) -> None:
        self.objects = dict(objects or {})
        self.error = error
        self.reads: list[str] = []
        self.appears_after = appears_after
        self.deferred = deferred

    def read(self, key: str) -> bytes | None:
        self.reads.append(key)
        if self.error is not None:
            raise self.error
        if (
            self.deferred is not None
            and self.appears_after is not None
            and len(self.reads) > self.appears_after
        ):
            name, payload = self.deferred
            self.objects[name] = payload
            self.deferred = None
        return self.objects.get(key)


class FakeProcess:
    """``exit_status`` is fixed unless ``dies_after_polls`` says otherwise.

    A real ``Popen.poll()`` answers ``None`` on the first call every time --
    the child has not even finished ``exec`` -- and only reports a status
    later. ``dies_after_polls`` lets a drill rehearse that: ``poll()`` returns
    ``None`` for that many calls, then the exit status on every call after.
    """

    def __init__(
        self,
        pid: int,
        exit_status: int | None,
        *,
        dies_after_polls: int | None = None,
    ) -> None:
        self.pid = pid
        self.exit_status = exit_status
        self.dies_after_polls = dies_after_polls
        self.poll_calls = 0

    def poll(self) -> int | None:
        self.poll_calls += 1
        if self.dies_after_polls is not None and self.poll_calls <= self.dies_after_polls:
            return None
        return self.exit_status


class FakeStarter:
    """Records the argv it was given and when, relative to the channel reads."""

    def __init__(
        self,
        channel: InMemoryChannel,
        *,
        pid: int = 4242,
        exit_status: int | None = None,
        fail: Exception | None = None,
        dies_after_polls: int | None = None,
    ) -> None:
        self.channel = channel
        self.pid = pid
        self.exit_status = exit_status
        self.fail = fail
        self.dies_after_polls = dies_after_polls
        self.argv: list[str] | None = None
        self.reads_before_start: int | None = None
        self.process: FakeProcess | None = None

    def __call__(self, argv):  # type: ignore[no-untyped-def]
        self.argv = list(argv)
        self.reads_before_start = len(self.channel.reads)
        if self.fail is not None:
            raise self.fail
        self.process = FakeProcess(
            self.pid, self.exit_status, dies_after_polls=self.dies_after_polls
        )
        return self.process


def armer(clock: Clock, channel: InMemoryChannel, starter: FakeStarter, **kwargs):  # type: ignore[no-untyped-def]
    return ChannelControllerArmer(
        channel=channel,
        supervisor_argv=(
            sys.executable,
            "-m",
            "operations.pod.supervise",
            "--provider-factory",
            "untracked.provider:factory",
            "--spend",
            SPEND_FILE,
        ),
        now=clock.now,
        sleeper=clock.sleep,
        start_supervisor=starter,
        **kwargs,
    )


def fake(clock: Clock) -> FakeProvider:
    return FakeProvider({"fake-48gb": (Decimal("0.77"), Decimal("0.05"))}, now=clock.now)


def unarmed_lease(store: LeaseStore, record, ask: PodCreateRequest, clock: Clock) -> PodLease:
    """An active, pod-bound lease with no arming evidence yet -- what `arm` is handed."""

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
        hard_deadline=ask.hard_deadline,
        owner_token=OWNER,
        heartbeat_at=clock.now(),
        phase="active",
    )
    store.create(lease)
    return lease


def report(
    lease: PodLease,
    record,
    *,
    acknowledged_at: datetime | None = None,
    lease_id: str | None = None,
    pod_id: str | None = None,
    hard_deadline: str | None = None,
    schema: str = POD_REPORT_SCHEMA,
) -> bytes:
    """The pod timer's first durable write, exactly as `pod_timer` shapes it."""

    payload = {
        "schema": schema,
        "identity": {
            "lease_id": lease_id or lease.lease_id,
            "pod_id": pod_id or record.pod_id,
            "hard_deadline": hard_deadline or stamp(lease.hard_deadline),
        },
        "acknowledged_at": stamp(acknowledged_at or lease.created_at),
        "bootstrap": {"argv": ["service"], "state": "running"},
        "close": None,
        "green": False,
    }
    return json.dumps(payload).encode("utf-8")


def scene(tmp_path: Path, clock: Clock, *, lifetime: int = 3600):  # type: ignore[no-untyped-def]
    provider = fake(clock)
    ask = request(clock, lifetime=lifetime)
    record = provider.create(ask)
    store = LeaseStore(tmp_path / f"{LEASE_ID}.json")
    lease = unarmed_lease(store, record, ask, clock)
    return ask, record, store, lease


def arm(armer_under_test, ask, record, store, lease, *, action: str = "create") -> ControllerArming:  # type: ignore[no-untyped-def]
    return armer_under_test.arm(
        action=action,
        request=ask,
        record=record,
        lease=lease,
        store=store,
        owner_token=OWNER,
        policy=policy(),
    )


# -- the key, derived and from nowhere else ---------------------------------


def test_the_channel_key_is_the_report_path_relative_to_the_mounted_volume() -> None:
    clock = Clock()
    ask = request(clock)

    assert report_path_of(ask) == REPORT_PATH
    assert report_key(ask) == REPORT_OBJECT


class _Elsewhere:
    """The two fields `report_key` reads, wired to a path off the volume.

    `PodCreateRequest` refuses this shape at construction, so the armer's own
    derivation is checked against a stand-in: it is the last thing between a
    request and a read of some other volume's object, and a check that can only
    be reached through a constructor that already refuses is no check at all.
    """

    def __init__(self, command: tuple[str, ...], mount: str) -> None:
        self.docker_start_cmd = command
        self.volume_mount_path = mount


def test_a_report_path_outside_the_mount_has_no_key_at_all() -> None:
    elsewhere = _Elsewhere(("--report-path", "/elsewhere/report.json"), "/workspace/private")

    with pytest.raises(ValueError, match="not inside the mounted volume"):
        report_key(elsewhere)  # type: ignore[arg-type]


def test_a_request_naming_no_report_path_has_no_key_either() -> None:
    with pytest.raises(ValueError, match="exactly one --report-path"):
        report_path_of(_Elsewhere(("python", "-m", "operations.pod.pod_timer"), "/workspace"))  # type: ignore[arg-type]


# -- the arming order -------------------------------------------------------


def test_the_supervisor_starts_before_the_first_read_and_carries_this_launchs_lease(
    tmp_path: Path,
) -> None:
    """Spec 4.4's arming order: a launcher that dies mid-poll leaves a closer."""

    clock = Clock()
    ask, record, store, lease = scene(tmp_path, clock)
    channel = InMemoryChannel({REPORT_OBJECT: report(lease, record)})
    starter = FakeStarter(channel)

    result = arm(armer(clock, channel, starter), ask, record, store, lease)

    assert result.armed
    assert starter.reads_before_start == 0
    assert starter.argv is not None
    assert starter.argv[-4:] == ["--leases", str(tmp_path), "--lease", LEASE_ID]
    # `ps` is public: the owner token reaches the supervisor through its 0600
    # identity file and never through the command line.
    assert OWNER not in starter.argv


def test_the_supervisor_is_handed_this_launchs_owner_token_through_its_identity_file(
    tmp_path: Path,
) -> None:
    """Without the handover the supervisor mints a token the lease does not know,
    and could only ever reach this pod by closing it as an orphan."""

    clock = Clock()
    ask, record, store, lease = scene(tmp_path, clock)
    channel = InMemoryChannel({REPORT_OBJECT: report(lease, record)})

    arm(armer(clock, channel, FakeStarter(channel)), ask, record, store, lease)

    identity = supervise.read_identity(supervise.identity_path(tmp_path, LEASE_ID))
    assert identity is not None
    assert identity.owner_token == OWNER


def test_a_handover_that_finds_its_own_token_already_there_still_arms(tmp_path: Path) -> None:
    """A retried arming attempt over the same lease writes the identical
    handover it would have written the first time -- `FileExistsError` here
    names no foreign controller, and the launch proceeds."""

    clock = Clock()
    ask, record, store, lease = scene(tmp_path, clock)
    channel = InMemoryChannel({REPORT_OBJECT: report(lease, record)})
    handover = supervise.identity_path(tmp_path, LEASE_ID)
    durable.exclusive_write(
        handover,
        durable.canonical_json(
            supervise.SupervisorIdentity(
                owner_token=OWNER, started_at=clock.now(), pid=999
            ).to_record()
        ),
    )

    result = arm(armer(clock, channel, FakeStarter(channel)), ask, record, store, lease)

    assert result.armed


def test_a_handover_that_finds_a_foreign_token_already_there_refuses(tmp_path: Path) -> None:
    """The identity file already names another controller's owner token. The
    supervisor about to start would resume that foreign token and could only
    ever reach this pod through `claim_if_orphan`, closing it once this
    launcher exits -- so a receipt claiming a live laptop controller for
    *this* lease must never be written."""

    clock = Clock()
    ask, record, store, lease = scene(tmp_path, clock)
    channel = InMemoryChannel({REPORT_OBJECT: report(lease, record)})
    handover = supervise.identity_path(tmp_path, LEASE_ID)
    durable.exclusive_write(
        handover,
        durable.canonical_json(
            supervise.SupervisorIdentity(
                owner_token="f" * 32, started_at=clock.now(), pid=999
            ).to_record()
        ),
    )
    starter = FakeStarter(channel)

    result = arm(armer(clock, channel, starter), ask, record, store, lease)

    assert not result.armed
    assert "names another controller" in result.detail
    assert starter.argv is None
    assert channel.reads == []


def test_a_supervisor_that_cannot_start_refuses_before_any_read(tmp_path: Path) -> None:
    clock = Clock()
    ask, record, store, lease = scene(tmp_path, clock)
    channel = InMemoryChannel({REPORT_OBJECT: report(lease, record)})
    starter = FakeStarter(channel, fail=OSError("no such interpreter"))

    result = arm(armer(clock, channel, starter), ask, record, store, lease)

    assert not result.armed
    assert not result.laptop_supervisor_started
    assert "no pod-report read was attempted" in result.detail
    assert channel.reads == []


def test_a_supervisor_that_exits_immediately_refuses_before_any_read(tmp_path: Path) -> None:
    clock = Clock()
    ask, record, store, lease = scene(tmp_path, clock)
    channel = InMemoryChannel({REPORT_OBJECT: report(lease, record)})
    starter = FakeStarter(channel, exit_status=1)

    result = arm(armer(clock, channel, starter), ask, record, store, lease)

    assert not result.armed
    assert "exited immediately with status 1" in result.detail
    assert channel.reads == []


def test_a_supervisor_that_dies_mid_poll_is_caught_within_a_poll_interval_not_the_whole_bound(
    tmp_path: Path,
) -> None:
    """A real `Popen.poll()` answers `None` right after start every time -- the
    child has not even finished `exec` -- so the only production-reachable
    guard is one that checks on every pass of the poll loop. This proves it
    fires there, not only after the whole `timeout_seconds` bound runs out."""

    clock = Clock()
    ask, record, store, lease = scene(tmp_path, clock)
    channel = InMemoryChannel()  # never answers, so the poll would otherwise run to the bound
    starter = FakeStarter(channel, exit_status=17, dies_after_polls=2)

    result = arm(armer(clock, channel, starter), ask, record, store, lease)

    assert not result.armed
    assert "exited with status 17" in result.detail
    assert result.laptop_supervisor_started
    # Caught within a couple of poll intervals, nowhere near the 300s bound.
    assert clock.seconds < CONTROLLER_ARMING_TIMEOUT_SECONDS / 2


# -- the happy path, and the two validators it has to satisfy ---------------


def test_a_good_report_arms_and_its_receipt_survives_both_binding_checks(
    tmp_path: Path,
) -> None:
    clock = Clock()
    ask, record, store, lease = scene(tmp_path, clock)
    acknowledged = clock.now() + timedelta(seconds=4)
    channel = InMemoryChannel({REPORT_OBJECT: report(lease, record, acknowledged_at=acknowledged)})
    clock.sleep(10)

    result = arm(armer(clock, channel, FakeStarter(channel)), ask, record, store, lease)

    assert result.armed
    assert result.laptop_supervisor_started and result.pod_timer_acknowledged
    assert result.receipt["lease_id"] == LEASE_ID
    assert result.receipt["pod_id"] == record.pod_id
    assert result.receipt["pod_timer"]["report_path"] == REPORT_PATH  # type: ignore[index]
    assert result.receipt["pod_timer"]["acknowledged_at"] == stamp(acknowledged)  # type: ignore[index]

    # Both gates a green launch has to pass, run exactly as `launch` runs them.
    PodRuntime._validate_arming_binding(arming=result, request=ask, record=record, lease=lease)
    armed = store.record_controller_arming(
        owner_token=OWNER, controller_record=result.to_record(), now=clock.now()
    )
    assert armed.controller_record is not None
    reloaded = store.load()
    assert reloaded is not None and reloaded.controller_record is not None


def test_the_receipt_names_no_field_that_could_hold_capability_material(
    tmp_path: Path,
) -> None:
    clock = Clock()
    ask, record, store, lease = scene(tmp_path, clock)
    channel = InMemoryChannel({REPORT_OBJECT: report(lease, record)})

    result = arm(armer(clock, channel, FakeStarter(channel)), ask, record, store, lease)

    names: list[str] = []

    def walk(value: object) -> None:
        if isinstance(value, dict):
            for name, item in value.items():
                names.append(str(name))
                walk(item)

    walk(dict(result.receipt))
    assert names
    assert [name for name in names if looks_like_credential_field(name)] == []


def test_a_report_that_appears_late_still_arms_within_the_bound(tmp_path: Path) -> None:
    clock = Clock()
    ask, record, store, lease = scene(tmp_path, clock)
    channel = InMemoryChannel(appears_after=3, deferred=(REPORT_OBJECT, report(lease, record)))

    result = arm(armer(clock, channel, FakeStarter(channel)), ask, record, store, lease)

    assert result.armed
    assert len(channel.reads) == 4
    # Each wait refreshed the launch owner's heartbeat, which is what stops the
    # supervisor started above from closing this pod mid-poll.
    polled = store.load()
    assert polled is not None and polled.heartbeat_at > lease.heartbeat_at


# -- the refusals -----------------------------------------------------------


def test_an_absent_report_refuses_at_the_bound_and_names_it(tmp_path: Path) -> None:
    clock = Clock()
    ask, record, store, lease = scene(tmp_path, clock)
    channel = InMemoryChannel()

    result = arm(armer(clock, channel, FakeStarter(channel)), ask, record, store, lease)

    assert not result.armed
    assert result.laptop_supervisor_started and not result.pod_timer_acknowledged
    assert f"{CONTROLLER_ARMING_TIMEOUT_SECONDS:.0f}s arming bound" in result.detail
    assert clock.seconds >= CONTROLLER_ARMING_TIMEOUT_SECONDS


def test_the_bound_is_clamped_down_to_what_is_left_of_the_lease(tmp_path: Path) -> None:
    clock = Clock()
    ask, record, store, lease = scene(tmp_path, clock, lifetime=120)
    channel = InMemoryChannel()

    result = arm(armer(clock, channel, FakeStarter(channel)), ask, record, store, lease)

    assert not result.armed
    assert "120s arming bound" in result.detail
    assert clock.seconds <= 120


def test_a_channel_that_cannot_answer_refuses_at_once_rather_than_polling(
    tmp_path: Path,
) -> None:
    """The rule the whole seam turns on: unreachable is never 'not yet'."""

    clock = Clock()
    ask, record, store, lease = scene(tmp_path, clock)
    channel = InMemoryChannel(error=RuntimeError("the volume refused this credential"))

    result = arm(armer(clock, channel, FakeStarter(channel)), ask, record, store, lease)

    assert not result.armed
    assert "never 'not yet'" in result.detail
    assert len(channel.reads) == 1
    assert clock.seconds == 0


@pytest.mark.parametrize(
    ("name", "payload_kwargs", "expected"),
    [
        ("wrong-lease", {"lease_id": "f" * 32}, "does not name this exact lease"),
        ("wrong-pod", {"pod_id": "another-pod"}, "does not name this exact lease"),
        (
            "wrong-deadline",
            {"hard_deadline": "2027-01-01T00:00:00Z"},
            "does not name this exact lease",
        ),
        ("wrong-schema", {"schema": "pod-report.v0"}, "schema is absent or unsupported"),
    ],
)
def test_a_report_that_is_not_this_launchs_evidence_refuses(
    tmp_path: Path, name: str, payload_kwargs: dict, expected: str
) -> None:
    clock = Clock()
    ask, record, store, lease = scene(tmp_path, clock)
    channel = InMemoryChannel({REPORT_OBJECT: report(lease, record, **payload_kwargs)})

    result = arm(armer(clock, channel, FakeStarter(channel)), ask, record, store, lease)

    assert not result.armed, name
    assert "is not this launch's evidence" in result.detail
    assert expected in result.detail


@pytest.mark.parametrize(
    "payload",
    [b"{not json", b"\xff\xfe not utf-8", b"[]", b"null"],
    ids=["torn", "bytes", "array", "null"],
)
def test_an_unparseable_object_refuses_rather_than_waiting(tmp_path: Path, payload: bytes) -> None:
    clock = Clock()
    ask, record, store, lease = scene(tmp_path, clock)
    channel = InMemoryChannel({REPORT_OBJECT: payload})

    result = arm(armer(clock, channel, FakeStarter(channel)), ask, record, store, lease)

    assert not result.armed
    assert len(channel.reads) == 1


def test_an_oversized_object_is_refused_unread(tmp_path: Path) -> None:
    clock = Clock()
    ask, record, store, lease = scene(tmp_path, clock)
    channel = InMemoryChannel({REPORT_OBJECT: b"x" * 4096})

    result = arm(
        armer(clock, channel, FakeStarter(channel), max_report_bytes=1024),
        ask,
        record,
        store,
        lease,
    )

    assert not result.armed
    assert "past the 1024-byte bound" in result.detail


def test_an_acknowledgement_outside_the_lease_lifetime_refuses(tmp_path: Path) -> None:
    clock = Clock()
    ask, record, store, lease = scene(tmp_path, clock)
    stale = lease.created_at - timedelta(seconds=1)
    channel = InMemoryChannel({REPORT_OBJECT: report(lease, record, acknowledged_at=stale)})

    result = arm(armer(clock, channel, FakeStarter(channel)), ask, record, store, lease)

    assert not result.armed
    assert "outside this lease's lifetime" in result.detail


def test_a_pod_clock_a_little_ahead_is_waited_out_rather_than_refused(
    tmp_path: Path,
) -> None:
    clock = Clock()
    ask, record, store, lease = scene(tmp_path, clock)
    ahead = clock.now() + timedelta(seconds=ACKNOWLEDGEMENT_FUTURE_SKEW_SECONDS - 1)
    channel = InMemoryChannel({REPORT_OBJECT: report(lease, record, acknowledged_at=ahead)})

    result = arm(armer(clock, channel, FakeStarter(channel)), ask, record, store, lease)

    assert result.armed
    assert result.observed_at >= ahead


def test_a_pod_clock_far_ahead_refuses_rather_than_dating_evidence_it_cannot(
    tmp_path: Path,
) -> None:
    clock = Clock()
    ask, record, store, lease = scene(tmp_path, clock)
    ahead = clock.now() + timedelta(seconds=ACKNOWLEDGEMENT_FUTURE_SKEW_SECONDS + 30)
    channel = InMemoryChannel({REPORT_OBJECT: report(lease, record, acknowledged_at=ahead)})

    result = arm(armer(clock, channel, FakeStarter(channel)), ask, record, store, lease)

    assert not result.armed
    assert "ahead of this laptop's clock" in result.detail


def test_a_lease_the_launch_no_longer_owns_stops_the_poll(tmp_path: Path) -> None:
    """Another controller closed or claimed this lease while the launch polled."""

    clock = Clock()
    ask, record, store, lease = scene(tmp_path, clock)
    channel = InMemoryChannel()

    class HijackedStore(LeaseStore):
        def heartbeat(self, *, owner_token: str, now=None):  # type: ignore[no-untyped-def]
            raise RuntimeError("lease belongs to a different controller")

    hijacked = HijackedStore(store.path)
    result = arm(armer(clock, channel, FakeStarter(channel)), ask, record, hijacked, lease)

    assert not result.armed
    assert "could not refresh its own lease heartbeat" in result.detail


def test_a_request_and_lease_that_disagree_refuse_before_anything_starts(
    tmp_path: Path,
) -> None:
    clock = Clock()
    ask, record, store, lease = scene(tmp_path, clock)
    channel = InMemoryChannel({REPORT_OBJECT: report(lease, record)})
    starter = FakeStarter(channel)

    moved = replace(ask, hard_deadline=ask.hard_deadline + timedelta(seconds=60))
    result = arm(armer(clock, channel, starter), moved, record, store, lease)

    assert not result.armed
    assert "different hard deadlines" in result.detail
    assert starter.argv is None and channel.reads == []


# -- preflight --------------------------------------------------------------


def test_preflight_probes_the_channel_and_is_ready_when_it_answers() -> None:
    clock = Clock()
    ask = request(clock)
    channel = InMemoryChannel()

    readiness = armer(clock, channel, FakeStarter(channel)).preflight(
        action="create", request=ask, policy=policy()
    )

    assert readiness.ready
    assert channel.reads == [REPORT_OBJECT]
    assert readiness.receipt["probe"] == "absent"


def test_preflight_refuses_a_channel_that_cannot_answer_before_anything_is_paid_for() -> None:
    clock = Clock()
    ask = request(clock)
    channel = InMemoryChannel(error=RuntimeError("no such bucket"))

    readiness = armer(clock, channel, FakeStarter(channel)).preflight(
        action="create", request=ask, policy=policy()
    )

    assert not readiness.ready
    assert "could not answer a probe read" in readiness.detail


def test_preflight_refuses_an_unconfigured_policy() -> None:
    clock = Clock()
    ask = request(clock)
    channel = InMemoryChannel()

    readiness = armer(clock, channel, FakeStarter(channel)).preflight(
        action="create", request=ask, policy=SpendPolicy(state="unconfigured")
    )

    assert not readiness.ready
    assert "heartbeat timeout" in readiness.detail


def test_preflight_refuses_a_poll_interval_that_does_not_stay_inside_the_heartbeat_timeout() -> (
    None
):
    """`policy.laptop_heartbeat_timeout_seconds = 1` is a legal `spend.toml`.
    Under the default 5s poll interval the supervisor started during arming
    would wake and close the pod before the launch owner's next heartbeat --
    every launch under that policy would die mid-arming. Catching it here
    costs nothing before the create."""

    clock = Clock()
    ask = request(clock)
    channel = InMemoryChannel()
    short_heartbeat = replace(policy(), laptop_heartbeat_timeout_seconds=1)

    readiness = armer(clock, channel, FakeStarter(channel)).preflight(
        action="create", request=ask, policy=short_heartbeat
    )

    assert not readiness.ready
    assert "poll interval" in readiness.detail
    assert channel.reads == []


def test_preflight_refuses_a_supervisor_command_that_is_not_startable() -> None:
    """An interpreter that is not on the path, or a missing `--spend` file,
    passes today's checks and fails only after a paid create -- or, worse,
    after the whole arming bound. Both are free reads before that create."""

    clock = Clock()
    ask = request(clock)
    channel = InMemoryChannel()
    unstartable = ChannelControllerArmer(
        channel=channel,
        supervisor_argv=(
            "/no/such/interpreter-fpuxeu",
            "-m",
            "operations.pod.supervise",
        ),
        now=clock.now,
        sleeper=clock.sleep,
        start_supervisor=FakeStarter(channel),
    )

    readiness = unstartable.preflight(action="create", request=ask, policy=policy())

    assert not readiness.ready
    assert "interpreter-fpuxeu" in readiness.detail
    assert channel.reads == []


def test_preflight_refuses_a_supervisor_argv_naming_a_spend_file_that_does_not_exist() -> None:
    clock = Clock()
    ask = request(clock)
    channel = InMemoryChannel()
    missing_spend = ChannelControllerArmer(
        channel=channel,
        supervisor_argv=(
            sys.executable,
            "-m",
            "operations.pod.supervise",
            "--spend",
            "/no/such/spend-fpuxeu.toml",
        ),
        now=clock.now,
        sleeper=clock.sleep,
        start_supervisor=FakeStarter(channel),
    )

    readiness = missing_spend.preflight(action="create", request=ask, policy=policy())

    assert not readiness.ready
    assert "spend-fpuxeu.toml" in readiness.detail
    assert channel.reads == []


# -- the drill armer --------------------------------------------------------


def test_the_observing_armer_never_arms_on_a_perfect_report_and_files_what_it_saw(
    tmp_path: Path,
) -> None:
    clock = Clock()
    ask, record, store, lease = scene(tmp_path, clock)
    channel = InMemoryChannel({REPORT_OBJECT: report(lease, record)})
    starter = FakeStarter(channel)
    evidence_root = tmp_path / "drill"
    drill = ObservingControllerArmer(
        evidence_root=evidence_root,
        channel=channel,
        supervisor_argv=("python", "-m", "operations.pod.supervise"),
        now=clock.now,
        sleeper=clock.sleep,
        start_supervisor=starter,
    )

    result = arm(drill, ask, record, store, lease)

    assert not result.armed
    assert not result.pod_timer_acknowledged
    # The supervisor really did start, in the same order; saying otherwise
    # would put a false statement in a durable record.
    assert result.laptop_supervisor_started
    assert channel.reads == [REPORT_OBJECT]

    filed = json.loads(drill.evidence_path(LEASE_ID).read_text(encoding="utf-8"))
    assert filed["schema"] == ARMING_DRILL_SCHEMA
    assert filed["verdict"] == "never-armed"
    assert filed["state"] == "observed"
    assert filed["report_object"] == REPORT_OBJECT
    assert filed["acknowledged_at"] == stamp(lease.created_at)
    assert filed["laptop_supervisor"]["pid"] == starter.pid


def test_the_observing_armer_files_a_refusal_too(tmp_path: Path) -> None:
    clock = Clock()
    ask, record, store, lease = scene(tmp_path, clock, lifetime=60)
    channel = InMemoryChannel()
    drill = ObservingControllerArmer(
        evidence_root=tmp_path / "drill",
        channel=channel,
        supervisor_argv=("python", "-m", "operations.pod.supervise"),
        now=clock.now,
        sleeper=clock.sleep,
        start_supervisor=FakeStarter(channel),
    )

    result = arm(drill, ask, record, store, lease)

    assert not result.armed
    filed = json.loads(drill.evidence_path(LEASE_ID).read_text(encoding="utf-8"))
    assert filed["state"] == "report-absent-within-bound"
    assert filed["bound_seconds"] == 60.0
    assert filed["waited_seconds"] >= 60.0


def test_the_observing_armer_is_still_ready_at_preflight() -> None:
    """A drill that refused before the create would measure nothing at all."""

    clock = Clock()
    ask = request(clock)
    channel = InMemoryChannel()
    drill = ObservingControllerArmer(
        evidence_root=Path("/does-not-need-to-exist"),
        channel=channel,
        supervisor_argv=(sys.executable, "-m", "operations.pod.supervise"),
        now=clock.now,
        sleeper=clock.sleep,
        start_supervisor=FakeStarter(channel),
    )

    assert drill.preflight(action="create", request=ask, policy=policy()).ready


# -- construction refusals --------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"supervisor_argv": ()},
        {"supervisor_argv": ("python", "  ")},
        {"timeout_seconds": 0},
        {"poll_seconds": -1},
        {"max_report_bytes": 0},
    ],
)
def test_an_armer_that_could_not_do_its_job_refuses_to_exist(kwargs: dict) -> None:
    base = {
        "channel": InMemoryChannel(),
        "supervisor_argv": ("python", "-m", "operations.pod.supervise"),
    }
    with pytest.raises(ValueError):
        ChannelControllerArmer(**{**base, **kwargs})


def test_an_object_that_is_not_a_channel_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="read"):
        ChannelControllerArmer(channel=object(), supervisor_argv=("python",))
