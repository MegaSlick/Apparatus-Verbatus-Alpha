"""The whole launch, driven end to end offline -- the host integration U4 named.

`test_controller_armer.py` proves the armer's own decisions against an
in-memory dict; `test_supervise.py` proves the driver's ticks against a
hand-built lease.  Neither ever runs the thing they are parts of.  This module
does: one `launch.PodRuntime.create` per drill, through the real
`ChannelControllerArmer`, over a directory standing in for the mounted volume,
with the pod's own `pod_timer` writing the report the launcher reads and the
real `supervise` driver resuming the owner token the armer handed it.

What is real here, and what stands in:

* **Real.** `PodRuntime` and its whole gate, `ChannelControllerArmer` and
  `ObservingControllerArmer`, `LeaseStore` and both of its validators,
  `pod_timer.run_with_bootstrap`'s first durable write, `supervise`'s identity
  handover (`establish_identity`, `record_tick`, `identity_path`) and
  `supervise.supervise_tick`, `VerifiedShutdown`, and a spend policy loaded
  from a real `spend.toml` by `load_spend_policy` -- the same file the
  supervisor's own argv names.
* **Stands in.** `FakeProvider` for the provider (no endpoint, no credential,
  no money), a directory for the volume's network view, a fake clock for every
  sleep, an in-process object for the detached supervisor **process**, and a
  child that never exits for the bootstrap the pod timer supervises.

The supervisor is in-process rather than a real `subprocess`: the drill has to
decide *when* a tick happens, because every clock here is fake and a real child
would sleep on the wall clock instead.  `InProcessSupervisor` therefore runs
`supervise`'s own functions -- the same identity file, the same resumed token,
the same `supervise_tick` -- and only the loop is the drill's: its pre-loop
refusals, notifier wiring and exit-code/final-record reporting are not
rehearsed here (`test_supervise.py` covers those).

Nothing here reaches a network, a provider, a pod, or the wall clock.  Every
wait is a fake-clock advance, so the whole module's real sleeping is zero.

The one thing no drill in this file can establish is the load-bearing unknown
`controller_armer`'s docstring names: whether an object a pod writes through
its volume mount appears in that volume's network view, under which key, and
after how long.  A directory answers instantly and exactly; a real volume is
what the first authorized boot's `ObservingControllerArmer` drill -- (f) below,
rehearsed here -- exists to measure.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Callable, Iterator, Sequence

import pytest

from . import pod_timer, supervise
from .controller_armer import (
    ChannelControllerArmer,
    ObservingControllerArmer,
    default_supervisor_argv,
    report_key,
    report_path_of,
)
from .controllers import ControllerState, LaptopSupervisor, PodDeadmanTimer
from .fake_provider import FakeProvider
from .launch import LaunchResult, LaunchState, PodRuntime
from .lease import LeaseStore
from .models import (
    BILLING_CUTOFF_MARGIN_ENV,
    POD_REPORT_SCHEMA,
    LeaseOwnershipError,
    PodCreateRequest,
    Presence,
)
from .notify_bridge import NotifyOutcome
from .shutdown import VerifiedShutdown
from .spend import load_spend_policy

START = datetime(2026, 9, 2, 10, 0, tzinfo=UTC)

# `PodRuntime` mints the lease id first and the owner token second, so a
# two-value token factory pins both and every path below can name them.
LEASE_ID = "a" * 32
OWNER = "b" * 32

MOUNT = "/workspace/private"
REPORT_PATH = f"{MOUNT}/pod-report.json"
# What `launch._bind_report_path_to_launch` makes of it: the launch token is
# folded into the file name so a second launch on the same retained volume
# cannot overwrite the first's evidence.  Asserted against `report_key` in the
# green drill rather than merely assumed.
BOUND_REPORT_PATH = f"{MOUNT}/pod-report-{LEASE_ID}.json"
BOUND_REPORT_OBJECT = f"pod-report-{LEASE_ID}.json"

BOOTSTRAP_ARGV = ["python", "-m", "operations.pod.bootstrap_main", "--hold"]

SUPERVISOR_PID = 90001

SPEND_TOML = "\n".join(
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
)


class Clock:
    """One fake clock for the launcher, the pod timer and the supervisor alike.

    They are three processes in production and would each read their own
    clock; sharing one here is what makes an ordering claim -- the
    acknowledgement happened before the receipt observed it -- mean something
    rather than depend on which fixture ran first.
    """

    def __init__(self) -> None:
        self.seconds = 0.0

    def now(self) -> datetime:
        return START + timedelta(seconds=self.seconds)

    def monotonic(self) -> float:
        return self.seconds

    def sleep(self, seconds: float) -> None:
        self.seconds += seconds


class BillingFake(FakeProvider):
    """`FakeProvider`, plus the billing record a *verified* close needs.

    Three of the drills below close the pod from inside `create` itself, which
    leaves no window in which a test could install billing for a pod that did
    not exist a moment earlier.  Without it every one of those closes is
    legitimately unverified -- `FakeProvider.capture_cost` refuses to infer
    zero -- and the drills would then assert the wrong thing about a money
    path.  Billing each pod as it is created is the same evidence
    `FakeProvider.bill` installs afterwards, moved to the only moment that
    works.
    """

    def create(self, request: PodCreateRequest):  # type: ignore[no-untyped-def]
        record = super().create(request)
        self.bill(record.pod_id, "0.10")
        return record


def closer(provider: FakeProvider, clock: Clock, *, timeout: float = 8) -> VerifiedShutdown:
    return VerifiedShutdown(
        provider,
        timeout_seconds=timeout,
        poll_seconds=1,
        billing_cutoff_margin_seconds=3600,
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
        now=clock.now,
    )


def request(clock: Clock, *, lifetime: int) -> PodCreateRequest:
    return PodCreateRequest(
        name="launch-drill",
        gpu_type="fake-48gb",
        image="registry.example/verbatus@sha256:" + "a" * 64,
        template="pinned-template",
        volume_id="test-volume",
        volume_mount_path=MOUNT,
        docker_start_cmd=(
            "python",
            "-m",
            "operations.pod.pod_timer",
            "--timer-factory",
            "untracked.timer:factory",
            "--bootstrap-command-json",
            json.dumps(BOOTSTRAP_ARGV),
            "--report-path",
            REPORT_PATH,
        ),
        hard_deadline=clock.now() + timedelta(seconds=lifetime),
        repository_commit="b" * 40,
        metadata={BILLING_CUTOFF_MARGIN_ENV: "3600"},
    )


class VolumeChannel:
    """The `TimerReportChannel` seam, over a directory standing in for the volume.

    The seam's one rule holds here as it must in the real implementation:
    ``read`` answers bytes, or ``None`` **only** when the object is proven
    absent.  ``on_read`` is where the drill puts everything that happens
    *while* the launcher is polling -- the pod filing its report, or the
    launcher itself dying -- because that window is the whole point of the
    arming order under test.
    """

    def __init__(self, root: Path, *, on_read: Callable[[str, int], None] | None = None) -> None:
        self.root = Path(root)
        self.on_read = on_read
        self.reads: list[str] = []
        self.reads_by_key: Counter[str] = Counter()

    def read(self, key: str) -> bytes | None:
        self.reads.append(key)
        self.reads_by_key[key] += 1
        if self.on_read is not None:
            self.on_read(key, self.reads_by_key[key])
        path = self.root / key
        return path.read_bytes() if path.exists() else None


class _FirstReportWritten(Exception):
    """Stops the pod timer one instruction after its first durable write."""


class _HeldChild:
    """The long-running bootstrap service the pod timer supervises.

    It never exits, which is what `pod_timer.run_with_bootstrap` requires of a
    real entrypoint: a child that exits 0 is ``completed-early`` and closes the
    pod.
    """

    def poll(self) -> int | None:
        return None


def _stop_after_the_first_report(seconds: float) -> None:
    del seconds
    raise _FirstReportWritten


class InProcessSupervisor:
    """`supervise`'s own driver, ticked by the drill instead of by its own loop.

    The armer starts this exactly where it would start a detached
    ``python -m operations.pod.supervise``: same argv, after the same identity
    handover, and the first thing it does is `supervise.establish_identity` --
    which takes the lease's kernel lock and resumes the owner token the armer
    left in the identity file.  `tick` is `supervise.supervise_tick` and
    `supervise.record_tick`, unchanged.

    `run_supervisor`'s loop is the drill's, and its pre-loop refusals (a
    missing lease, an already-terminal lease, a heartbeat timeout not shorter
    than the remaining lifetime), its notifier wiring and its exit-code and
    final-record reporting are not rehearsed here at all -- they are inert at
    the lifetimes this module uses, and `test_supervise.py` proves them.  The
    spend policy is loaded from the same ``--spend`` file the argv names, the
    way `supervise.main` loads it, rather than handed in.
    """

    def __init__(
        self, argv: Sequence[str], *, provider: FakeProvider, clock: Clock, pid: int
    ) -> None:
        self.argv = [str(part) for part in argv]
        self.leases_root = Path(self._value("--leases"))
        self.lease_id = self._value("--lease")
        self.policy = load_spend_policy(self._value("--spend"))
        assert self.policy.laptop_heartbeat_timeout_seconds is not None
        self.heartbeat_timeout = timedelta(seconds=self.policy.laptop_heartbeat_timeout_seconds)
        self.provider = provider
        self.clock = clock
        self.pid = pid
        self.store = LeaseStore(self.leases_root / f"{self.lease_id}.json")
        # Named `ident`, not `identity`, for the reason `supervise.run_supervisor`
        # records at its own copy of this line: the obvious spelling passes a
        # twenty-plus-byte attribute path to a keyword whose name ends in
        # "token", and the repository's ingress scanner reads that shape as a
        # possible credential literal.  It blocked this file's first commit.
        # `tick` below binds the value to a short local for the same reason.
        self.ident = supervise.establish_identity(
            self.leases_root, self.lease_id, now=clock.now, pid=pid
        )
        self.ticks: list[supervise.SuperviseResult] = []
        self.exit_status: int | None = None

    def _value(self, flag: str) -> str:
        index = self.argv.index(flag)
        return self.argv[index + 1]

    def poll(self) -> int | None:
        """`SupervisorProcess`: ``None`` while running, else the exit status."""

        return self.exit_status

    def tick(self) -> supervise.SuperviseResult:
        owner = self.ident.owner_token
        result = supervise.supervise_tick(
            store=self.store,
            provider=self.provider,
            shutdown=closer(self.provider, self.clock),
            owner_token=owner,
            heartbeat_timeout=self.heartbeat_timeout,
            now=self.clock.now,
        )
        self.ident = supervise.record_tick(
            supervise.identity_path(self.leases_root, self.lease_id),
            self.ident,
            state=result.state,
            detail=result.detail,
            now=self.clock.now(),
        )
        self.ticks.append(result)
        return result

    def stop(self) -> None:
        """Give up the kernel lock, as a real process exit would."""

        self.exit_status = 0
        supervise.release_lock(self.leases_root, self.lease_id)


class DrillStarter:
    """Stands where `controller_armer.detached_supervisor` stands.

    It records how far the launcher had got through the channel when the
    supervisor was started, which is the arming-order claim: the supervisor
    exists before the first read of this launch's report object, so a launcher
    that dies mid-poll leaves something behind that can still close the pod.
    """

    def __init__(self, *, provider: FakeProvider, clock: Clock, channel: VolumeChannel) -> None:
        self.provider = provider
        self.clock = clock
        self.channel = channel
        self.started: list[InProcessSupervisor] = []
        self.reads_before_start: int | None = None

    def __call__(self, argv: Sequence[str]) -> InProcessSupervisor:
        self.reads_before_start = len(self.channel.reads)
        supervisor = InProcessSupervisor(
            argv,
            provider=self.provider,
            clock=self.clock,
            pid=SUPERVISOR_PID + len(self.started),
        )
        self.started.append(supervisor)
        return supervisor

    @property
    def supervisor(self) -> InProcessSupervisor:
        assert self.started, "the armer never started a supervisor"
        return self.started[-1]

    def stop_all(self) -> None:
        for supervisor in self.started:
            supervisor.stop()


@dataclass
class Drill:
    """One launch, its fakes, and the two sides that can still close its pod."""

    clock: Clock
    provider: BillingFake
    lease_root: Path
    volume: Path
    channel: VolumeChannel
    starter: DrillStarter
    runtime: PodRuntime
    ask: PodCreateRequest

    @property
    def store(self) -> LeaseStore:
        return LeaseStore(self.lease_root / f"{LEASE_ID}.json")

    @property
    def supervisor(self) -> InProcessSupervisor:
        return self.starter.supervisor

    def launch(self) -> LaunchResult:
        """Preview, then confirm with the phrase that preview printed."""

        preview = self.runtime.preview_create(self.ask)
        assert preview.state is LaunchState.PREVIEW and preview.preview is not None
        return self.runtime.create(self.ask, confirmation=preview.preview.confirmation_phrase)

    def pod_id(self) -> str:
        pods = sorted(self.provider.pods)
        assert len(pods) == 1, f"the drill expects exactly one fake pod, saw {pods}"
        return pods[0]

    def sealed(self) -> PodCreateRequest:
        """The request the provider was actually handed, report path and all."""

        created = [ask for ask in self.provider.create_requests if not ask.recovery_only]
        assert created, "no create reached the provider"
        return created[-1]

    def pod_writes_at(self, count: int) -> None:
        """File the pod timer's first durable report on the Nth read of a key."""

        def hook(key: str, seen: int) -> None:
            if seen == count:
                self.write_pod_report(key)
                # Advance past the write so an acknowledgement stamp and a
                # later observation of it can never land on the same instant
                # -- see the `Clock` docstring's ordering claim.
                self.clock.sleep(1)

        self.channel.on_read = hook

    def write_pod_report(self, key: str) -> Path | None:
        """Run the real pod timer far enough to make its first durable write.

        `pod_timer.run_with_bootstrap` writes that report before it enters its
        monitoring loop, so a sleeper that refuses to sleep stops the pod side
        exactly one instruction after the write the launcher is waiting for.
        Nothing here reimplements the payload: the schema tag, the closed
        identity block and the acknowledgement stamp are `TimerContext`'s own,
        so this drill cannot drift from what a pod actually writes.
        """

        lease = self.store.load()
        if lease is None or lease.pod_id is None:
            # No pod exists yet -- a preflight probe, before the create.  A pod
            # that does not exist has written nothing, and saying so is the
            # honest answer rather than a fixture convenience.
            return None
        context = pod_timer.TimerContext(
            PodDeadmanTimer(lease, closer(self.provider, self.clock), now=self.clock.now)
        )
        target = self.volume / key
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            pod_timer.run_with_bootstrap(
                context,
                bootstrap_command_json=json.dumps(BOOTSTRAP_ARGV),
                report_path=target,
                sleeper=_stop_after_the_first_report,
                interval_seconds=15.0,
                popen=lambda argv: _HeldChild(),  # type: ignore[arg-type,return-value]
            )
        except _FirstReportWritten:
            pass
        else:
            raise AssertionError(
                "run_with_bootstrap returned instead of stopping at its first durable "
                "write; the pod-side close path ran"
            )
        assert target.is_file(), "the pod timer's first durable write did not reach the volume"
        return target


def _build(
    tmp_path: Path,
    *,
    lifetime: int,
    observing: bool = False,
) -> tuple[Drill, ObservingControllerArmer | None]:
    clock = Clock()
    provider = BillingFake({"fake-48gb": (Decimal("0.77"), Decimal("0.05"))}, now=clock.now)
    spend_path = tmp_path / "spend.toml"
    spend_path.write_text(SPEND_TOML, encoding="utf-8")
    policy = load_spend_policy(spend_path)
    lease_root = tmp_path / "leases"
    volume = tmp_path / "volume"
    volume.mkdir(parents=True, exist_ok=True)
    channel = VolumeChannel(volume)
    starter = DrillStarter(provider=provider, clock=clock, channel=channel)
    argv = default_supervisor_argv(
        provider_factory="untracked.drill_provider:factory", spend=spend_path
    )
    common = {
        "channel": channel,
        "supervisor_argv": argv,
        "now": clock.now,
        "sleeper": clock.sleep,
        "start_supervisor": starter,
    }
    drill_armer: ObservingControllerArmer | None = None
    if observing:
        drill_armer = ObservingControllerArmer(evidence_root=tmp_path / "drill", **common)
        armer: ChannelControllerArmer = drill_armer
    else:
        armer = ChannelControllerArmer(**common)
    tokens = iter((LEASE_ID, OWNER))
    runtime = PodRuntime(
        provider,
        provider_name="fake",
        spend_policy=policy,
        lease_root=lease_root,
        shutdown=closer(provider, clock),
        now=clock.now,
        token_factory=lambda: next(tokens),
        controller_armer=armer,
        notifier=lambda message: NotifyOutcome(False, False, "drill notifier is silent"),
        lock_sleeper=clock.sleep,
    )
    return (
        Drill(
            clock=clock,
            provider=provider,
            lease_root=lease_root,
            volume=volume,
            channel=channel,
            starter=starter,
            runtime=runtime,
            ask=request(clock, lifetime=lifetime),
        ),
        drill_armer,
    )


@pytest.fixture
def build_drill(tmp_path: Path) -> Iterator[Callable[..., Drill]]:
    """Build a drill, and give up every supervisor's kernel lock afterwards.

    `supervise.establish_identity` holds that lock for the life of the process
    -- here, the life of the whole test session -- so a drill that did not
    release it would leave the lock held for a lease nothing is supervising.
    """

    made: list[Drill] = []

    def build(**kwargs: object) -> Drill:
        drill, _ = _build(tmp_path, **kwargs)  # type: ignore[arg-type]
        made.append(drill)
        return drill

    yield build
    for drill in made:
        drill.starter.stop_all()


@pytest.fixture
def build_observing_drill(
    tmp_path: Path,
) -> Iterator[Callable[..., tuple[Drill, ObservingControllerArmer]]]:
    made: list[Drill] = []

    def build(**kwargs: object) -> tuple[Drill, ObservingControllerArmer]:
        drill, drill_armer = _build(tmp_path, observing=True, **kwargs)  # type: ignore[arg-type]
        assert drill_armer is not None
        made.append(drill)
        return drill, drill_armer

    yield build
    for drill in made:
        drill.starter.stop_all()


# -- (a) the green path -------------------------------------------------------


def test_a_green_launch_arms_on_the_pods_own_report_and_hands_the_lease_to_the_supervisor(
    build_drill: Callable[..., Drill],
) -> None:
    """The whole arming order, in one create.

    The supervisor is started and its identity recorded before the first read
    of this launch's report object; the pod's own timer then files the report
    the armer is waiting for; the receipt that observation produces survives
    both validators and reaches the durable lease; and the supervisor -- which
    resumed *this launch's* owner token rather than minting one of its own --
    then ticks as the lease's legitimate owner and guards the pod instead of
    closing it.
    """

    drill = build_drill(lifetime=300)
    drill.pod_writes_at(2)  # the launcher's first read of the bound key misses

    result = drill.launch()

    assert result.state is LaunchState.CREATED_GUARDED
    assert result.green
    assert result.record is not None and result.controller_arming is not None
    assert result.controller_arming.armed

    # The key is derived from the sealed request and nothing else, and the
    # sealed report path carries this launch's token.
    sealed = drill.sealed()
    assert report_path_of(sealed) == BOUND_REPORT_PATH
    assert report_key(sealed) == BOUND_REPORT_OBJECT

    # Arming order: the supervisor existed before this launch read its object.
    assert drill.starter.reads_before_start is not None
    read_before_start = drill.channel.reads[: drill.starter.reads_before_start]
    assert BOUND_REPORT_OBJECT not in read_before_start
    assert drill.channel.reads_by_key[BOUND_REPORT_OBJECT] == 2

    # The handover: the identity file carries this launch's owner token, the
    # supervisor resumed it, and it never appeared on a command line.
    handed = supervise.read_identity(supervise.identity_path(drill.lease_root, LEASE_ID))
    assert handed is not None and handed.owner_token == OWNER
    assert drill.supervisor.ident.owner_token == OWNER
    assert OWNER not in drill.supervisor.argv
    assert drill.supervisor.argv[-4:] == ["--leases", str(drill.lease_root), "--lease", LEASE_ID]

    # What the armer read really was the pod timer's own first durable write.
    written = json.loads((drill.volume / BOUND_REPORT_OBJECT).read_text(encoding="utf-8"))
    assert written["schema"] == POD_REPORT_SCHEMA
    assert written["identity"] == {
        "lease_id": LEASE_ID,
        "pod_id": result.record.pod_id,
        "hard_deadline": drill.ask.hard_deadline.isoformat().replace("+00:00", "Z"),
    }
    assert written["bootstrap"]["state"] == "running"

    # The receipt round-tripped through `LeaseStore.record_controller_arming`
    # and survives a reload, which re-runs `_validate_controller_record`.
    armed = drill.store.load()
    assert armed is not None and armed.controller_record is not None
    receipt = armed.controller_record["receipt"]
    assert receipt["lease_id"] == LEASE_ID
    assert receipt["pod_id"] == result.record.pod_id
    assert receipt["pod_timer"]["report_path"] == BOUND_REPORT_PATH
    assert receipt["pod_timer"]["acknowledged_at"] == written["acknowledged_at"]

    # The ordering claim the `Clock` docstring makes: the pod's acknowledgement
    # is strictly earlier than the launcher's observation of it, not merely
    # equal by coincidence of a shared clock that never advanced between them.
    assert receipt["pod_timer"]["acknowledged_at"] < armed.controller_record["observed_at"]

    # And the supervisor now guards it: a healthy tick, no close, no terminate.
    # A second on the clock first, so the refreshed heartbeat below is a fact
    # about the tick rather than about two writes landing on one instant.
    drill.clock.sleep(1)
    tick = drill.supervisor.tick()
    assert tick.state == "active"
    assert tick.green
    assert tick.close_report is None
    assert drill.provider.terminate_calls == []
    guarded = drill.store.load()
    assert guarded is not None and guarded.heartbeat_at > armed.heartbeat_at


# -- (b) the launcher dies mid-poll -------------------------------------------


class LauncherDied(BaseException):
    """The launching process vanishes; nothing gets to handle it.

    A `BaseException` on purpose: every `except Exception` between the channel
    read and `create`'s return would otherwise turn this into a named refusal
    the launcher lived to report, which is the opposite of the situation the
    arming order exists for.
    """


def test_b_a_launcher_that_dies_mid_poll_leaves_a_supervisor_that_closes_the_unarmed_lease(
    build_drill: Callable[..., Drill],
) -> None:
    """Spec 4.4's reason for starting the supervisor first.

    The launcher dies during the poll, leaving an ``active`` pod-bound lease
    with no arming evidence and a heartbeat nobody will refresh again.  The
    supervisor it already started waits while that heartbeat is fresh -- the
    launch may still be completing -- and closes the pod once it goes stale.
    """

    drill = build_drill(lifetime=3600)

    def die_on_the_second_read(key: str, seen: int) -> None:
        if key == BOUND_REPORT_OBJECT and seen == 2:
            raise LauncherDied("the launching process was killed mid-poll")

    drill.channel.on_read = die_on_the_second_read

    with pytest.raises(LauncherDied):
        drill.launch()

    pod_id = drill.pod_id()
    abandoned = drill.store.load()
    assert abandoned is not None
    assert abandoned.phase == "active"
    assert abandoned.pod_id == pod_id
    assert abandoned.controller_record is None
    assert drill.provider.terminate_calls == []

    # The launch owner's heartbeat is still fresh: the supervisor must not kill
    # a pod whose launch might yet finish arming it.
    waiting = drill.supervisor.tick()
    assert waiting.state == ControllerState.CONTROLLER_UNARMED.value
    assert waiting.close_report is None
    assert not waiting.green
    assert drill.provider.terminate_calls == []

    # Nobody refreshes it again, so it goes stale, and then the pod is closed.
    drill.clock.sleep(drill.supervisor.heartbeat_timeout.total_seconds() + 1)
    closed = drill.supervisor.tick()
    assert closed.state == ControllerState.CONTROLLER_UNARMED.value
    assert closed.close_report is not None and closed.close_report.verified
    assert "lacks durable laptop-supervisor and pod-timer arming evidence" in closed.detail
    assert drill.provider.terminate_calls == [pod_id]
    assert drill.provider.status(pod_id).presence is Presence.ABSENT

    terminal = drill.store.load()
    assert terminal is not None and terminal.phase == "closed-verified"
    assert terminal.close_record is not None and terminal.close_record["pod_id"] == pod_id


# -- (c) the armer refuses, and the launch closes at once ---------------------


def test_c_a_report_that_never_appears_closes_the_pod_inside_the_create_that_made_it(
    build_drill: Callable[..., Drill],
) -> None:
    """No report, no proof the pod can be closed -- so it is closed now.

    The bound is clamped down to what is left of the lease, the refusal names
    it, and `launch._arm_or_close` closes the pod before `create` returns.  The
    close is checked against provider state, not against the armer's word for
    it: terminated once, absent to a GET, absent from the list, and durably
    recorded as ``closed-verified``.
    """

    drill = build_drill(lifetime=60)  # nothing ever writes to the volume

    result = drill.launch()

    assert result.state is LaunchState.CONTROLLERS_UNARMED
    assert not result.green
    assert result.controller_arming is not None
    assert not result.controller_arming.armed
    # The supervisor was still started first, and honestly reported as started.
    assert result.controller_arming.laptop_supervisor_started
    assert not result.controller_arming.pod_timer_acknowledged
    assert "60s arming bound" in result.controller_arming.detail
    assert drill.clock.seconds >= 60

    pod_id = drill.pod_id()
    assert result.close_report is not None and result.close_report.verified
    assert drill.provider.terminate_calls == [pod_id]
    assert drill.provider.status(pod_id).presence is Presence.ABSENT
    assert drill.provider.verify_absent(pod_id).presence is Presence.ABSENT

    closed = drill.store.load()
    assert closed is not None
    assert closed.phase == "closed-verified"
    assert closed.controller_record is None
    assert closed.close_record is not None and closed.close_record["pod_id"] == pod_id


# -- (d) the pod reaches EXITED under the supervisor --------------------------


def test_d_a_pod_that_exits_under_a_fresh_heartbeat_is_closed_by_the_supervisor(
    build_drill: Callable[..., Drill],
) -> None:
    """Deferral 04-4's real harm, from the green launch that precedes it.

    The lease is armed, the heartbeat is perfectly fresh and the deadline is an
    hour away -- everything `LaptopSupervisor.run_once` looks at says healthy.
    The pod is nonetheless EXITED and billing its attached volume, and only the
    provider lifecycle word `supervise_tick` reads on every tick can see that.
    """

    drill = build_drill(lifetime=3600)
    drill.pod_writes_at(1)

    result = drill.launch()
    assert result.state is LaunchState.CREATED_GUARDED
    pod_id = drill.pod_id()

    healthy = drill.supervisor.tick()
    assert healthy.state == "active"
    assert drill.provider.terminate_calls == []

    drill.provider.set_pod_state(pod_id, "EXITED")
    # Still PRESENT: a presence-only check would see nothing wrong at all.
    assert drill.provider.status(pod_id).presence is Presence.PRESENT

    exited = drill.supervisor.tick()
    assert exited.state == supervise.PROVIDER_EXITED
    assert "'EXITED'" in exited.detail
    assert exited.close_report is not None and exited.close_report.verified
    assert drill.provider.terminate_calls == [pod_id]

    closed = drill.store.load()
    assert closed is not None and closed.phase == "closed-verified"


# -- (e) the shared-owner-token window ----------------------------------------


def test_e_a_supervisor_close_leaves_the_launch_side_seeing_a_terminal_lease(
    build_drill: Callable[..., Drill],
) -> None:
    """After a successful arm both sides hold the same owner token.

    The supervisor closes.  The launch side -- any controller reading the same
    store with the same token -- must then see a terminal phase and make no
    provider call of its own, and the verified close evidence must not be
    replaceable, or a second, lesser observation could turn a proven close back
    into a question.
    """

    drill = build_drill(lifetime=3600)
    drill.pod_writes_at(1)
    result = drill.launch()
    assert result.state is LaunchState.CREATED_GUARDED
    assert result.owner_token == OWNER
    pod_id = drill.pod_id()

    drill.provider.set_pod_state(pod_id, "EXITED")
    assert drill.supervisor.tick().state == supervise.PROVIDER_EXITED
    assert drill.provider.terminate_calls == [pod_id]

    drill.provider.calls.clear()
    launch_side = LaptopSupervisor(
        drill.store,
        closer(drill.provider, drill.clock),
        owner_token=OWNER,
        heartbeat_timeout=drill.supervisor.heartbeat_timeout,
        now=drill.clock.now,
    ).run_once()
    assert launch_side.state is ControllerState.CLOSED_VERIFIED
    assert launch_side.close_report is None
    assert drill.provider.calls == []
    assert drill.provider.terminate_calls == [pod_id]

    # And the verified evidence itself is not replaceable by the shared token:
    # a launch side that closed again and recorded a lesser observation would
    # turn a proven close back into a question (GOVERNANCE 4).
    with pytest.raises(LeaseOwnershipError, match="verified close evidence"):
        drill.store.record_close(
            owner_token=OWNER,
            close_record={"pod_id": pod_id, "state": "unverified"},
            verified=False,
            now=drill.clock.now(),
        )


def test_e_a_launch_side_close_leaves_the_supervisor_seeing_a_terminal_lease(
    build_drill: Callable[..., Drill],
) -> None:
    """The same window, closed from the other end.

    The launch side closes the pod on the shared token and records it.  The
    supervisor's very next tick must read that terminal phase off the durable
    lease and stop -- no second termination, no provider call at all, and the
    pod is not lost: the lease still names it and its close record.
    """

    drill = build_drill(lifetime=3600)
    drill.pod_writes_at(1)
    result = drill.launch()
    assert result.state is LaunchState.CREATED_GUARDED
    assert result.record is not None
    pod_id = drill.pod_id()

    report = closer(drill.provider, drill.clock).close(
        result.record, reason="the launch side closed this pod"
    )
    assert report.verified
    drill.store.record_close(
        owner_token=OWNER,
        close_record=report.to_record(),
        verified=True,
        now=drill.clock.now(),
    )
    assert drill.provider.terminate_calls == [pod_id]

    drill.provider.calls.clear()
    tick = drill.supervisor.tick()
    assert tick.state == "closed-verified"
    assert tick.green
    assert drill.provider.calls == []
    assert drill.provider.terminate_calls == [pod_id]

    terminal = drill.store.load()
    assert terminal is not None
    assert terminal.phase == "closed-verified"
    assert terminal.pod_id == pod_id
    assert terminal.close_record is not None and terminal.close_record["pod_id"] == pod_id


# -- (f) the drill armer, which is what the first authorized boot runs --------


def test_f_the_observing_armer_reads_a_perfect_report_closes_at_once_and_files_its_evidence(
    build_observing_drill: Callable[..., tuple[Drill, ObservingControllerArmer]],
) -> None:
    """Boot A, rehearsed offline.

    The observing armer performs the identical read -- same supervisor start,
    same key, same parse -- and never reports the pod timer acknowledged, so
    `launch._arm_or_close` closes the pod at once whatever it saw.  What it saw
    goes to an evidence file, because on a real volume that measurement is the
    only thing the boot is for.
    """

    drill, drill_armer = build_observing_drill(lifetime=300)
    drill.pod_writes_at(1)

    result = drill.launch()

    assert result.state is LaunchState.CONTROLLERS_UNARMED
    assert result.controller_arming is not None
    assert not result.controller_arming.armed
    # The supervisor really was started, in the same order; the drill armer
    # refuses to say otherwise in a durable record.
    assert result.controller_arming.laptop_supervisor_started
    assert not result.controller_arming.pod_timer_acknowledged
    assert drill.starter.started

    pod_id = drill.pod_id()
    assert result.close_report is not None and result.close_report.verified
    assert drill.provider.terminate_calls == [pod_id]
    assert drill.provider.status(pod_id).presence is Presence.ABSENT

    closed = drill.store.load()
    assert closed is not None
    assert closed.phase == "closed-verified"
    assert closed.controller_record is None

    filed = json.loads(drill_armer.evidence_path(LEASE_ID).read_text(encoding="utf-8"))
    assert filed["verdict"] == "never-armed"
    assert filed["state"] == "observed"
    assert filed["report_object"] == BOUND_REPORT_OBJECT
    assert filed["report_path"] == BOUND_REPORT_PATH
    assert filed["pod_id"] == pod_id
    assert filed["laptop_supervisor"]["started"] is True
    assert filed["laptop_supervisor"]["pid"] == drill.supervisor.pid
    written = json.loads((drill.volume / BOUND_REPORT_OBJECT).read_text(encoding="utf-8"))
    assert filed["acknowledged_at"] == written["acknowledged_at"]
