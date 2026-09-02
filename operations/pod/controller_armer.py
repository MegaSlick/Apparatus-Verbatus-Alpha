"""The two-controller armer, and a report channel that cannot say "not yet" by mistake.

`arming.FailClosedControllerArmer` refuses every launch because no runtime
handshake existed.  This module is that handshake: it starts the durable laptop
supervisor (`supervise.py`) as a real process, then waits for the pod's own
timer to publish its first durable report where the laptop can read it, and
turns the two observations into the exact receipt
`lease._validate_controller_record` accepts.

**The channel is the load-bearing unknown.** The pod writes its report to the
mounted volume; the laptop has no filesystem there and must read it back
through the volume's network view.  Whether an object written through the
mount appears in that view, under which name, and after how long, is not
established anywhere in this repository -- so `TimerReportChannel` below is a
seam with exactly one rule: ``read`` returns bytes, or ``None`` **only when
the object is proven absent**.  "I could not tell" is an exception, never
``None``.  A channel that answered "not there yet" to a bad credential would
turn a broken read into a poll that expires, and the pod would be closed for a
report it had in fact written -- or, with the fail-closed rule inverted, a pod
would be left running on evidence nobody read.

**Arming order, and why the supervisor goes first.**  The supervisor is
started and recorded *before* the poll begins.  A launcher that dies during
the poll then leaves a live supervisor over an ``active`` lease whose
``controller_record`` is ``None``, which `controllers.LaptopSupervisor.run_once`
closes as soon as the launch owner's heartbeat goes stale.  The reverse order
-- poll first, start the supervisor on success -- leaves a billing pod with no
laptop-side controller at all for the whole polling window.

**The handover, which is this module's decision and not the spec's.**
`supervise.establish_identity` resumes the owner token in the identity file it
finds, and mints a fresh one when there is none.  A freshly minted token does
not match the lease, so that supervisor could only ever reach the pod through
`LeaseStore.claim_if_orphan` -- it would close the pod it was started to guard,
a heartbeat timeout after the launcher exits.  So the armer writes the identity
file itself, carrying **this launch's** owner token, before starting the
process: the supervisor resumes it, becomes the lease's legitimate heartbeating
owner, and guards the pod to its hard deadline.  The file is created with
`durable.exclusive_write` (0600, never overwriting), and the token reaches the
child only through that file -- never through argv, because ``ps`` is public.
Its ``pid`` field names the launcher that minted the handover; `supervise`
replaces it with the supervisor's own on resume, and that field is telemetry in
both processes, never the ownership decision.

**The armer heartbeats while it polls.**  The lease's heartbeat was last
refreshed when the pod was bound, and the supervisor is now live over an
unarmed lease -- so without a heartbeat the supervisor would close the pod
mid-poll, at ``laptop_heartbeat_timeout_seconds``, while the launcher was still
doing exactly what it is supposed to do.  `controllers.run_once` says as much
in the branch it takes here: *"its launch owner is still heartbeating"*.  When
that heartbeat fails -- the lease was claimed, closed, or damaged by something
else -- the poll stops at once and the launch is refused.

Nothing here names a provider, an endpoint, or a credential: the seam test in
`test_provider_runpod.py` keeps that vocabulary inside the adapter, and the
concrete channel lives beside the volume's other verbs in
`operations/operator/volume_s3.py`.

An operator supplies both implementations through an untracked
``module:callable`` given to ``cli.py --controller-armer-factory``: it
constructs a channel over the volume this launch mounts and the argv that
starts `supervise.py` (its own ``--provider-factory`` and ``--spend`` values),
and returns `ChannelControllerArmer` -- or `ObservingControllerArmer`, which
performs the identical read and never arms, for the first authorized boot.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Final, Protocol, Sequence

from . import durable, supervise
from .arming import ControllerArming, ControllerReadiness
from .lease import LeaseOwnershipError, LeaseStore, PodLease
from .models import (
    PodCreateRequest,
    PodRecord,
    require_utc,
    utc_now,
    validate_pod_report_identity,
)
from .spend import SpendPolicy

CONTROLLER_ARMING_TIMEOUT_SECONDS: Final = 300.0
"""How long a launch may wait for the pod's first durable report before refusing.

Code-owned, deliberately not a ``config/spend.toml`` key.  The loader refuses a
policy that is missing any documented key, so adding one bumps that schema and
hands Tyrel another number to choose -- and this is not a spend policy.  It is
a safety envelope: past it, the honest answer is that nothing proved the pod
can be closed, and the pod is closed.  It is clamped down to the lease's own
remaining lifetime, never up.

Three hundred seconds is a bound, not a measurement.  Nobody has yet observed
how long an object written through a pod's volume mount takes to appear in the
volume's network view -- that is what the first authorized boot's
`ObservingControllerArmer` drill exists to measure, and this number is expected
to be replaced by one derived from it.
"""

CONTROLLER_ARMING_POLL_SECONDS: Final = 5.0
"""Seconds between channel reads.

Short enough to stay well inside any sane ``laptop_heartbeat_timeout_seconds``
-- each poll also refreshes the launch owner's heartbeat, and a poll interval
longer than that timeout would let the supervisor close the pod between two
reads.
"""

ACKNOWLEDGEMENT_FUTURE_SKEW_SECONDS: Final = 5.0
"""How far ahead of this laptop the pod's acknowledgement stamp may be.

The receipt `lease._validate_controller_record` accepts forbids an observation
timestamped after the receipt that carries it, so a pod clock even slightly
ahead would otherwise refuse a perfectly good launch.  Inside this bound the
armer *waits for its own clock to pass the stamp* and then re-stamps the
receipt, rather than back-dating the pod or forward-dating itself: the receipt
then says the laptop observed the acknowledgement after it happened, which is
what actually occurred.  Beyond it, the two clocks disagree by more than a
safety receipt can absorb and the launch is refused.
"""

MAX_REPORT_BYTES: Final = 1_048_576
"""A pod report is a few hundred bytes; a megabyte is already pathological.

The bytes come off a network view of a volume the pod writes, so their size is
untrusted input like their content.  The channel bounds its own read as well;
this is the reader's independent bound.
"""

ARMING_DRILL_SCHEMA: Final = "pod-arming-drill.v1"

# Attempt states.  Only OBSERVED can arm; every other value is a named refusal.
OBSERVED: Final = "observed"
UNARMABLE_LAUNCH: Final = "unarmable-launch"
SUPERVISOR_FAILED: Final = "laptop-supervisor-failed"
BOUND_EXPIRED: Final = "report-absent-within-bound"
UNREADABLE_REPORT: Final = "report-unreadable"
CHANNEL_FAILED: Final = "channel-failed"
LAUNCH_OWNER_LOST: Final = "launch-owner-lost"
CLOCK_DISAGREEMENT: Final = "clock-disagreement"
HANDOVER_STORE_FAILED: Final = "supervisor-handover-store-failed"
LEASE_STORE_FAILED: Final = "lease-store-failed"


class TimerReportChannel(Protocol):
    """Read one object the pod wrote, or prove it is not there.

    ``read`` returns the object's bytes, or ``None`` **only** when the channel
    positively established that no object exists at ``key``.  Anything else --
    a refused credential, a network failure, an answer it cannot classify --
    raises.  ``None`` is evidence; an exception is the absence of evidence, and
    the armer treats the two differently.
    """

    def read(self, key: str) -> bytes | None:
        """Return the object's bytes, or ``None`` when it is proven absent."""


class SupervisorProcess(Protocol):
    """The little of `subprocess.Popen` this module depends on."""

    pid: int

    def poll(self) -> int | None:
        """``None`` while the process is running, else its exit status."""


def detached_supervisor(argv: Sequence[str]) -> SupervisorProcess:
    """Start `supervise.py` in its own session, outliving this launcher.

    The child's streams go nowhere on purpose: it is detached, nobody is
    watching its terminal, and `supervise` attempts a durable final record on
    every exit path (GOVERNANCE 2) and names a failed attempt in its printed
    exit record -- though with stdout here going to `DEVNULL`, a volume that
    refuses the write itself leaves nothing behind for this detached child to
    hand back.
    """

    return subprocess.Popen(  # noqa: S603 - argv is built here, never shell-interpolated
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


def default_supervisor_argv(*, provider_factory: str, spend: str | Path) -> tuple[str, ...]:
    """The argv prefix an untracked factory usually wants, minus the lease.

    The armer appends ``--leases`` and ``--lease`` itself from the store it is
    handed, so a factory cannot bind the supervisor to a different lease than
    the one being armed.
    """

    return (
        sys.executable,
        "-m",
        "operations.pod.supervise",
        "--provider-factory",
        provider_factory,
        "--spend",
        str(spend),
    )


def _stamp(value: datetime) -> str:
    return require_utc(value, "controller arming timestamp").isoformat().replace("+00:00", "Z")


def _parse_stamp(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{label} is not an RFC3339 UTC stamp") from error
    return require_utc(parsed, label)


def report_path_of(request: PodCreateRequest) -> str:
    """The absolute in-pod path this launch told its timer to write.

    Read from the sealed request's own argv rather than recomputed, so the
    receipt names the exact string `launch._validate_arming_binding` compares
    against.
    """

    command = request.docker_start_cmd
    indexes = [index for index, item in enumerate(command) if item == "--report-path"]
    if len(indexes) != 1 or indexes[0] + 1 >= len(command):
        raise ValueError("the request does not carry exactly one --report-path value")
    value = command[indexes[0] + 1]
    if not value:
        raise ValueError("the request's --report-path value is blank")
    return value


def report_key(request: PodCreateRequest) -> str:
    """Derive the channel key from the sealed request, and from nothing else.

    ``PurePosixPath(report_path).relative_to(volume_mount_path)``: both values
    are sealed into the request and `models._required_timer_arguments` has
    already proved the report path lies inside the mount, so this cannot invent
    a key for an object outside the volume this launch attached.  Deriving it
    from the request rather than accepting it as configuration is what keeps
    the armer reading the object *this* pod was told to write.
    """

    raw = report_path_of(request)
    path = PurePosixPath(raw)
    mount = PurePosixPath(request.volume_mount_path)
    try:
        relative = path.relative_to(mount)
    except ValueError as error:
        raise ValueError(
            f"the timer report path {raw!r} is not inside the mounted volume "
            f"{request.volume_mount_path!r}"
        ) from error
    if not relative.parts or ".." in relative.parts:
        raise ValueError(f"the timer report path {raw!r} does not name an object on the volume")
    return str(relative)


class _HandoverStoreFailure(Exception):
    """The identity-file write (or the read that checks it) failed outright.

    Distinct from a foreign owner already holding the handover (that is
    ``FileExistsError`` naming another controller, refused separately) and
    from the supervisor process itself failing to start: this is a durable-
    store fault under the leases root, and it must be reported as that, not
    folded into ``SUPERVISOR_FAILED`` -- see the ``operations/pod/**`` review
    instruction this exists to satisfy.
    """


@dataclass(frozen=True, slots=True)
class _StartedSupervisor:
    """What the armer knows about the process it just started."""

    pid: int
    identity: str
    detail: str
    process: SupervisorProcess


@dataclass(frozen=True, slots=True)
class _ArmingAttempt:
    """Everything one arming attempt observed, before anyone decides what it means.

    Both armers below build one of these from the identical procedure; only
    the verdict differs.  That is what makes the drill a real rehearsal of the
    launch rather than a different code path that happens to read the same key.
    """

    action: str
    lease_id: str
    pod_id: str | None
    started_at: datetime
    observed_at: datetime
    state: str
    detail: str
    hard_deadline: str = ""
    key: str = ""
    report_path: str = ""
    bound_seconds: float = 0.0
    waited_seconds: float = 0.0
    supervisor_started: bool = False
    supervisor_identity: str | None = None
    supervisor_pid: int | None = None
    acknowledged_at: str | None = None

    def evidence(self, *, verdict: str) -> dict[str, object]:
        """The drill's durable record of what a real read actually saw."""

        return {
            "schema": ARMING_DRILL_SCHEMA,
            "verdict": verdict,
            "action": self.action,
            "lease_id": self.lease_id,
            "pod_id": self.pod_id,
            "hard_deadline": self.hard_deadline,
            "report_object": self.key,
            "report_path": self.report_path,
            "state": self.state,
            "detail": self.detail,
            "bound_seconds": self.bound_seconds,
            "waited_seconds": round(self.waited_seconds, 3),
            "acknowledged_at": self.acknowledged_at,
            "laptop_supervisor": {
                "started": self.supervisor_started,
                "identity": self.supervisor_identity,
                "pid": self.supervisor_pid,
            },
            "started_at": _stamp(self.started_at),
            "observed_at": _stamp(self.observed_at),
        }


class ChannelControllerArmer:
    """Start the laptop supervisor, read the pod's report, or refuse and close.

    Constructed by an untracked factory with a `TimerReportChannel` over the
    volume this launch mounts and the argv that starts `supervise.py`.  Every
    clock, sleeper and process start is injectable, so every drill below runs
    offline against fakes.
    """

    def __init__(
        self,
        *,
        channel: TimerReportChannel,
        supervisor_argv: Sequence[str],
        now=utc_now,
        sleeper=time.sleep,
        start_supervisor=detached_supervisor,
        timeout_seconds: float = CONTROLLER_ARMING_TIMEOUT_SECONDS,
        poll_seconds: float = CONTROLLER_ARMING_POLL_SECONDS,
        max_report_bytes: int = MAX_REPORT_BYTES,
    ) -> None:
        if not callable(getattr(channel, "read", None)):
            raise ValueError("controller armer channel must offer read(key) -> bytes | None")
        argv = tuple(str(part) for part in supervisor_argv)
        if not argv or not all(part.strip() for part in argv):
            raise ValueError("laptop supervisor argv must be a non-empty command")
        if timeout_seconds <= 0 or poll_seconds <= 0:
            raise ValueError("controller arming bound and poll interval must be positive")
        if max_report_bytes <= 0:
            raise ValueError("controller arming report bound must be positive")
        self.channel = channel
        self.supervisor_argv = argv
        self.now = now
        self.sleeper = sleeper
        self.start_supervisor = start_supervisor
        self.timeout_seconds = float(timeout_seconds)
        self.poll_seconds = float(poll_seconds)
        self.max_report_bytes = int(max_report_bytes)

    # -- the pre-create half ------------------------------------------------

    def preflight(
        self, *, action: str, request: PodCreateRequest, policy: SpendPolicy
    ) -> ControllerReadiness:
        """Prove, before anything is paid for, that this arming can be attempted.

        The probe read is the whole point: a channel that cannot answer at all
        can never arm, and finding that out costs nothing before a create where
        finding it out afterwards costs a pod.  It proves the channel answers,
        not that the report is there -- for a create the key is not even the
        final one, because `launch._sealed_request` binds the report path to
        the launch token after this runs.
        """

        observed = self.now()
        receipt: dict[str, object] = {"action": action, "phase": "preflight"}
        if not policy.configured or policy.laptop_heartbeat_timeout_seconds is None:
            return ControllerReadiness(
                False,
                observed,
                "spend policy carries no laptop heartbeat timeout, so a supervisor started "
                "now could not be given the timeout it must run under",
                receipt,
            )
        # The poll below heartbeats the lease at most every `poll_seconds`; a
        # heartbeat timeout that does not comfortably outlast three polls lets
        # the supervisor started just below close the pod mid-poll, before the
        # launch owner's next heartbeat lands. Refusing here costs nothing;
        # discovering it mid-arming costs the create.
        if self.poll_seconds * 3 >= policy.laptop_heartbeat_timeout_seconds:
            return ControllerReadiness(
                False,
                observed,
                f"this armer's {self.poll_seconds:.1f}s poll interval does not stay well "
                f"inside the policy's {policy.laptop_heartbeat_timeout_seconds}s laptop "
                "heartbeat timeout; a supervisor started here would close every pod it "
                "was meant to guard during arming",
                receipt,
            )
        command = self.supervisor_argv[0]
        if not (shutil.which(command) or Path(command).is_file()):
            return ControllerReadiness(
                False,
                observed,
                f"the laptop supervisor command {command!r} is neither on the path nor a "
                "file that exists; a supervisor started from it could not run",
                receipt,
            )
        try:
            spend_index = self.supervisor_argv.index("--spend")
        except ValueError:
            spend_index = -1
        if 0 <= spend_index < len(self.supervisor_argv) - 1:
            spend_path = self.supervisor_argv[spend_index + 1]
            if not Path(spend_path).is_file():
                return ControllerReadiness(
                    False,
                    observed,
                    f"the laptop supervisor's --spend file {spend_path!r} does not exist",
                    receipt,
                )
        try:
            key = report_key(request)
        except ValueError as error:
            return ControllerReadiness(
                False,
                observed,
                f"this request has no readable timer report object: {error}",
                receipt,
            )
        receipt["report_object"] = key
        receipt["heartbeat_timeout_seconds"] = str(policy.laptop_heartbeat_timeout_seconds)
        receipt["supervisor_command"] = command
        try:
            payload = self.channel.read(key)
        except Exception as error:
            return ControllerReadiness(
                False,
                observed,
                f"the pod-report channel could not answer a probe read of {key!r}: {error}; "
                "an arming poll would have nothing to believe",
                receipt,
            )
        receipt["probe"] = "absent" if payload is None else "present"
        return ControllerReadiness(
            True,
            observed,
            f"the pod-report channel answered a probe read of {key!r} "
            f"({'no object yet' if payload is None else 'an object is already there'}) and a "
            f"laptop supervisor command is configured; the arming poll is bounded at "
            f"{self.timeout_seconds:.0f}s",
            receipt,
        )

    # -- the post-create half -----------------------------------------------

    def arm(
        self,
        *,
        action: str,
        request: PodCreateRequest,
        record: PodRecord,
        lease: PodLease,
        store: LeaseStore,
        owner_token: str,
        policy: SpendPolicy,
    ) -> ControllerArming:
        attempt = self._attempt(
            action=action,
            request=request,
            record=record,
            lease=lease,
            store=store,
            owner_token=owner_token,
            policy=policy,
        )
        return self._verdict(attempt)

    def _verdict(self, attempt: _ArmingAttempt) -> ControllerArming:
        """Arm only on a complete observation; every other state closes the pod."""

        if attempt.state != OBSERVED:
            return ControllerArming(
                attempt.supervisor_started,
                False,
                attempt.observed_at,
                attempt.detail,
                self._refusal_receipt(attempt),
            )
        return ControllerArming(
            True,
            True,
            attempt.observed_at,
            attempt.detail,
            {
                # Exactly the closed shape `lease._validate_controller_record`
                # accepts -- lease, pod, deadline, the two controllers -- and
                # nothing else.  Everything else this attempt learned is in the
                # detail above.  No field may be named for a key or a token:
                # `models.assert_nonsecret_receipt` refuses the whole receipt if
                # one is, and this record is durable evidence a human reads.
                "lease_id": attempt.lease_id,
                "pod_id": attempt.pod_id,
                "hard_deadline": attempt.hard_deadline,
                "laptop_supervisor": {
                    "identity": attempt.supervisor_identity,
                    "started_at": _stamp(attempt.started_at),
                },
                "pod_timer": {
                    "report_path": attempt.report_path,
                    "acknowledged_at": attempt.acknowledged_at,
                },
            },
        )

    @staticmethod
    def _refusal_receipt(attempt: _ArmingAttempt) -> dict[str, object]:
        """A refusal's receipt never reaches the lease; it reaches the operator.

        Values are strings throughout: a receipt is canonicalized wherever it
        is recorded, and `common/contracts/canonical.py` refuses a float
        outright rather than rounding one.
        """

        return {
            "action": attempt.action,
            "phase": "arm",
            "state": attempt.state,
            "report_object": attempt.key,
            "waited_seconds": f"{attempt.waited_seconds:.1f}",
            "bound_seconds": f"{attempt.bound_seconds:.1f}",
        }

    # -- the shared procedure both armers run -------------------------------

    def _attempt(
        self,
        *,
        action: str,
        request: PodCreateRequest,
        record: PodRecord,
        lease: PodLease,
        store: LeaseStore,
        owner_token: str,
        policy: SpendPolicy,
    ) -> _ArmingAttempt:
        del policy  # the bound is code-owned; the policy's ceilings are the runtime's business
        started_at = self.now()
        base = _ArmingAttempt(
            action=action,
            lease_id=lease.lease_id,
            pod_id=record.pod_id,
            started_at=started_at,
            observed_at=started_at,
            state=UNARMABLE_LAUNCH,
            detail="not yet attempted",
            hard_deadline=_stamp(lease.hard_deadline),
        )
        try:
            key = report_key(request)
            report_path = report_path_of(request)
            _assert_launch_is_coherent(request=request, record=record, lease=lease)
        except ValueError as error:
            return self._refuse(
                base,
                UNARMABLE_LAUNCH,
                f"this launch cannot be armed as it stands: {error}; nothing was started",
            )
        base = replace(base, key=key, report_path=report_path)

        # 1. The supervisor first, always: a launcher that dies during the poll
        #    below must leave something behind that can still close this pod.
        try:
            supervisor = self._start_supervisor(
                store=store, lease=lease, owner_token=owner_token, started_at=started_at
            )
        except _HandoverStoreFailure as error:
            return self._refuse(
                base,
                HANDOVER_STORE_FAILED,
                f"{error}; the supervisor command was never run; no pod-report read was attempted",
            )
        except Exception as error:
            return self._refuse(
                base,
                SUPERVISOR_FAILED,
                f"the durable laptop supervisor could not be started: {error}; "
                "no pod-report read was attempted",
            )
        base = replace(
            base,
            supervisor_started=True,
            supervisor_identity=supervisor.identity,
            supervisor_pid=supervisor.pid,
        )

        # 2. Then the poll, bounded by code and clamped to what is left of the
        #    lease -- waiting past the hard deadline for evidence of a close
        #    capability is waiting for the deadline to prove it instead.
        bound = min(self.timeout_seconds, (lease.hard_deadline - started_at).total_seconds())
        base = replace(base, bound_seconds=max(bound, 0.0))
        if bound <= 0:
            return self._refuse(
                base,
                BOUND_EXPIRED,
                "the lease's hard deadline has already passed, so there is no window in "
                "which a pod report could be believed",
            )
        attempt = self._poll(
            base,
            store=store,
            owner_token=owner_token,
            lease=lease,
            record=record,
            process=supervisor.process,
        )
        if attempt.state != OBSERVED:
            return attempt

        # 3. The receipt must be able to say it observed the acknowledgement
        #    after the acknowledgement happened.
        attempt = self._settle_clock(attempt)
        if attempt.state != OBSERVED:
            return attempt
        if attempt.observed_at > lease.hard_deadline:
            return self._refuse(
                attempt,
                BOUND_EXPIRED,
                "the pod report was read after this lease's hard deadline, so no receipt "
                "bound to that deadline can carry the observation",
            )
        # 4. The supervisor this receipt is about must still be alive to be it.
        exited = _exit_status(supervisor.process)
        if exited is not None:
            return self._refuse(
                attempt,
                SUPERVISOR_FAILED,
                f"the laptop supervisor exited with status {exited} while the pod report was "
                "being read; the receipt would name a controller that is not there",
            )
        return replace(
            attempt,
            detail=(
                f"laptop supervisor {supervisor.identity} started ({supervisor.detail}) and the "
                f"pod timer's report appeared at {attempt.key!r} after "
                f"{attempt.waited_seconds:.1f}s of a {attempt.bound_seconds:.0f}s bound, "
                f"acknowledged at {attempt.acknowledged_at} and bound to this exact lease, "
                f"pod and hard deadline"
            ),
        )

    def _refuse(self, attempt: _ArmingAttempt, state: str, detail: str) -> _ArmingAttempt:
        return replace(attempt, state=state, detail=detail, observed_at=self.now())

    def _start_supervisor(
        self, *, store: LeaseStore, lease: PodLease, owner_token: str, started_at: datetime
    ) -> _StartedSupervisor:
        leases_root = store.path.parent
        handover = supervise.identity_path(leases_root, lease.lease_id)
        detail = "identity handed over"
        try:
            durable.exclusive_write(
                handover,
                durable.canonical_json(
                    supervise.SupervisorIdentity(
                        owner_token=owner_token, started_at=started_at, pid=os.getpid()
                    ).to_record()
                ),
            )
        except FileExistsError:
            # Something already owns this lease's identity file.  Lease ids are
            # minted per launch, so this is not the ordinary path -- and
            # overwriting another controller's token is never the answer.  If
            # the file already names *this* launch's own token (a retried
            # arming attempt over the same lease, say), the supervisor started
            # below resumes it and that is fine.  If it names a different
            # token, the supervisor about to start would resume a foreign
            # owner and would only ever reach this pod through
            # `LeaseStore.claim_if_orphan` -- closing it, a heartbeat timeout
            # after this launcher exits, as an orphan.  A receipt saying "the
            # laptop supervisor started" would then be describing a controller
            # that does not own this lease, so that is refused here rather
            # than discovered later as a closed pod nobody meant to close.
            try:
                existing = supervise.read_identity(handover)
            except ValueError as error:
                # A read fault, not a foreign owner and not the supervisor
                # process -- same store fault this method's other OSError
                # branch names, just discovered one step later.
                raise _HandoverStoreFailure(
                    f"this lease's identity file at {handover} could not be read to check "
                    f"which controller it names: {error}"
                ) from error
            if existing is None or existing.owner_token != owner_token:
                raise RuntimeError(
                    "this lease's supervisor identity file names another controller; the "
                    "supervisor started here would not own this lease"
                ) from None
            detail = "an identity file already existed and was left untouched"
        except OSError as error:
            # Not a foreign owner (that is FileExistsError, caught above) and
            # not the supervisor process failing: the identity file under the
            # leases root itself could not be written. Filed as a store
            # fault, not folded into SUPERVISOR_FAILED.
            raise _HandoverStoreFailure(
                f"the identity file {handover} under the leases root could not be written: {error}"
            ) from error
        argv = [*self.supervisor_argv, "--leases", str(leases_root), "--lease", lease.lease_id]
        process = self.start_supervisor(argv)
        pid = getattr(process, "pid", None)
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise RuntimeError("the supervisor starter returned no usable process id")
        exited = _exit_status(process)
        if exited is not None:
            raise RuntimeError(f"the supervisor exited immediately with status {exited}")
        return _StartedSupervisor(
            pid=pid,
            identity=f"operations.pod.supervise pid {pid} over lease {lease.lease_id}",
            detail=detail,
            process=process,
        )

    def _poll(
        self,
        attempt: _ArmingAttempt,
        *,
        store: LeaseStore,
        owner_token: str,
        lease: PodLease,
        record: PodRecord,
        process: SupervisorProcess,
    ) -> _ArmingAttempt:
        """Read until the report appears, the bound expires, or something breaks.

        The supervisor is checked on every pass, not only once after the poll
        ends: a real ``Popen`` cannot report an exit status the instant it is
        started (the child has not even finished ``exec``), so a check made
        only before or after the loop cannot fire until the whole bound has
        run. Checking here turns a discovery that could take the full
        ``timeout_seconds`` into one bounded by a single ``poll_seconds``.
        """

        started = attempt.started_at
        while True:
            exited = _exit_status(process)
            if exited is not None:
                waited = (self.now() - started).total_seconds()
                return self._refuse(
                    replace(attempt, waited_seconds=waited),
                    SUPERVISOR_FAILED,
                    f"the laptop supervisor exited with status {exited} after {waited:.1f}s "
                    "while the pod report was being awaited; the receipt would name a "
                    "controller that is not there",
                )
            try:
                payload = self.channel.read(attempt.key)
            except Exception as error:
                waited = (self.now() - started).total_seconds()
                return self._refuse(
                    replace(attempt, waited_seconds=waited),
                    CHANNEL_FAILED,
                    f"the pod-report channel could not answer a read of {attempt.key!r} after "
                    f"{waited:.1f}s: {error}; an unreachable channel is never 'not yet'",
                )
            if payload is not None:
                waited = (self.now() - started).total_seconds()
                return self._read_report(
                    replace(attempt, waited_seconds=waited),
                    payload=payload,
                    lease=lease,
                    record=record,
                )
            waited = (self.now() - started).total_seconds()
            attempt = replace(attempt, waited_seconds=waited)
            if waited >= attempt.bound_seconds:
                return self._refuse(
                    attempt,
                    BOUND_EXPIRED,
                    f"no pod report appeared at {attempt.key!r} within the "
                    f"{attempt.bound_seconds:.0f}s arming bound (waited {waited:.1f}s); the pod "
                    "has proved no close capability and is closed rather than left billing",
                )
            # The launch owner is alive and working: say so, or the supervisor
            # started above closes this pod as an abandoned unarmed lease.
            try:
                store.heartbeat(owner_token=owner_token, now=self.now())
            except LeaseOwnershipError as error:
                return self._refuse(
                    attempt,
                    LAUNCH_OWNER_LOST,
                    f"this launch could not refresh its own lease heartbeat while polling: "
                    f"{error}; another controller now owns or has closed this lease",
                )
            except Exception as error:
                # A store fault (`OSError`, `LeaseFormatError`) and any
                # unclassified failure get the same posture: an ownership
                # change is not established either way, so this must not
                # carry the "another controller now owns" claim.
                return self._refuse(
                    attempt,
                    LEASE_STORE_FAILED,
                    f"this launch's own lease record could not be updated while polling: "
                    f"{error}; no ownership change is established by this failure",
                )
            self.sleeper(min(self.poll_seconds, attempt.bound_seconds - waited))

    def _read_report(
        self,
        attempt: _ArmingAttempt,
        *,
        payload: bytes,
        lease: PodLease,
        record: PodRecord,
    ) -> _ArmingAttempt:
        """Believe the object only if it is this launch's own pod-report.v1."""

        if not isinstance(payload, (bytes, bytearray)):
            return self._refuse(
                attempt, UNREADABLE_REPORT, "the channel answered with something that is not bytes"
            )
        if len(payload) > self.max_report_bytes:
            return self._refuse(
                attempt,
                UNREADABLE_REPORT,
                f"the object at {attempt.key!r} is {len(payload)} bytes, past the "
                f"{self.max_report_bytes}-byte bound for a pod report",
            )
        try:
            report = json.loads(bytes(payload).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            return self._refuse(
                attempt,
                UNREADABLE_REPORT,
                f"the object at {attempt.key!r} is not UTF-8 JSON: {error}",
            )
        if not isinstance(report, dict):
            return self._refuse(
                attempt,
                UNREADABLE_REPORT,
                f"the object at {attempt.key!r} is not a JSON object",
            )
        try:
            # One shared check with every other reader of a pod report: schema,
            # the closed identity block, this exact lease, pod and deadline, and
            # a parseable acknowledgement stamp.
            validate_pod_report_identity(
                report,
                lease_id=lease.lease_id,
                pod_id=record.pod_id,
                hard_deadline=lease.hard_deadline,
            )
            acknowledged_at = str(report["acknowledged_at"])
            acknowledged = _parse_stamp(acknowledged_at, "pod report acknowledged_at")
        except ValueError as error:
            return self._refuse(
                attempt,
                UNREADABLE_REPORT,
                f"the object at {attempt.key!r} is not this launch's evidence: {error}",
            )
        if acknowledged < lease.created_at or acknowledged > lease.hard_deadline:
            return self._refuse(
                attempt,
                UNREADABLE_REPORT,
                f"the pod report at {attempt.key!r} claims an acknowledgement at "
                f"{acknowledged_at}, outside this lease's lifetime "
                f"[{_stamp(lease.created_at)}, {_stamp(lease.hard_deadline)}]",
            )
        return replace(
            attempt,
            state=OBSERVED,
            detail="pod report observed",
            acknowledged_at=acknowledged_at,
            observed_at=self.now(),
        )

    def _settle_clock(self, attempt: _ArmingAttempt) -> _ArmingAttempt:
        """Wait out a pod clock that is a little ahead, or refuse one that is not.

        See `ACKNOWLEDGEMENT_FUTURE_SKEW_SECONDS`: the receipt may not claim an
        observation older than the thing observed, so either this laptop's own
        clock passes the stamp or there is no honest receipt to write.
        """

        acknowledged = _parse_stamp(str(attempt.acknowledged_at), "pod report acknowledged_at")
        observed_at = self.now()
        if acknowledged <= observed_at:
            return replace(attempt, observed_at=observed_at)
        ahead = (acknowledged - observed_at).total_seconds()
        if ahead > ACKNOWLEDGEMENT_FUTURE_SKEW_SECONDS:
            return self._refuse(
                attempt,
                CLOCK_DISAGREEMENT,
                f"the pod's acknowledgement stamp is {ahead:.1f}s ahead of this laptop's "
                f"clock, past the {ACKNOWLEDGEMENT_FUTURE_SKEW_SECONDS:.0f}s a receipt can "
                "absorb; the two clocks disagree too much for this evidence to be dated",
            )
        self.sleeper(ahead)
        observed_at = self.now()
        if acknowledged > observed_at:
            return self._refuse(
                attempt,
                CLOCK_DISAGREEMENT,
                "this laptop's clock did not advance past the pod's acknowledgement stamp, "
                "so no receipt could honestly say the acknowledgement was observed",
            )
        return replace(attempt, observed_at=observed_at)


class ObservingControllerArmer(ChannelControllerArmer):
    """The drill armer: the identical read, and never an armed verdict.

    This is what the first authorized boot runs.  Nothing offline can measure
    whether an object a pod writes through its volume mount appears in the
    volume's network view, under which name, or after how long -- and the
    honest way to find out is to do it once, on a cheap card, with an armer
    that cannot leave a pod running whatever it sees.

    It never reports the pod timer acknowledged: that flag is hard-coded
    ``False`` here, so `ControllerArming.armed` is ``False`` by construction and
    `launch._arm_or_close` closes the pod at once.  The laptop-supervisor flag
    stays honest -- the supervisor really was started, in the same order, and
    saying otherwise would put a false statement in a durable record.  What the
    read actually saw goes to an evidence file, which is the measurement this
    boot exists to take.
    """

    def __init__(self, *, evidence_root: Path, **kwargs) -> None:
        super().__init__(**kwargs)
        self.evidence_root = Path(evidence_root)

    def evidence_path(self, lease_id: str) -> Path:
        return self.evidence_root / f"controller-arming-drill-{lease_id}.json"

    def _verdict(self, attempt: _ArmingAttempt) -> ControllerArming:
        path = self.evidence_path(attempt.lease_id)
        try:
            durable.atomic_write(
                path, durable.canonical_json(attempt.evidence(verdict="never-armed"))
            )
            filed = f"what it saw is recorded at {path}"
        except Exception as error:
            # GOVERNANCE 2: the failure to record the measurement is itself the
            # finding, and it travels in the detail rather than vanishing.
            filed = f"its evidence file at {path} could not be written: {error}"
        return ControllerArming(
            attempt.supervisor_started,
            False,
            attempt.observed_at,
            (
                "observing drill armer: it performs the real read and never reports the pod "
                f"timer acknowledged, whatever it observed -- this launch closes now. It read "
                f"{attempt.key!r} for {attempt.waited_seconds:.1f}s of a "
                f"{attempt.bound_seconds:.0f}s bound and reached {attempt.state!r}; {filed}"
            ),
            {
                "action": attempt.action,
                "phase": "arm",
                "drill": "observing",
                "state": attempt.state,
                "report_object": attempt.key,
                "evidence_path": str(path),
            },
        )


def _assert_launch_is_coherent(
    *, request: PodCreateRequest, record: PodRecord, lease: PodLease
) -> None:
    """Refuse before starting anything when the three descriptions disagree.

    `launch._validate_arming_binding` compares the receipt against the request
    and `lease._validate_controller_record` compares it against the lease.  A
    request and lease that name different deadlines cannot satisfy both, and
    finding that out after starting a supervisor and polling for five minutes
    would waste the whole window on a launch that could never arm.
    """

    if record.pod_id is None or not str(record.pod_id).strip():
        raise ValueError("the created pod record names no pod id")
    if lease.pod_id != record.pod_id:
        raise ValueError(
            f"the lease names pod {lease.pod_id!r} and the provider record names {record.pod_id!r}"
        )
    if lease.hard_deadline != request.hard_deadline:
        raise ValueError(
            "the lease and the sealed request name different hard deadlines, so no receipt "
            "could be bound to both"
        )


def _exit_status(process: SupervisorProcess) -> int | None:
    """``None`` while the supervisor runs; its status once it is gone.

    A starter whose object cannot answer ``poll`` is treated as dead rather
    than assumed alive: this decides whether a receipt may claim a live
    controller.
    """

    poll = getattr(process, "poll", None)
    if not callable(poll):
        return -1
    try:
        status = poll()
    except Exception:
        return -1
    if status is None:
        return None
    return int(status) if isinstance(status, int) and not isinstance(status, bool) else -1


__all__ = [
    "ACKNOWLEDGEMENT_FUTURE_SKEW_SECONDS",
    "ARMING_DRILL_SCHEMA",
    "CONTROLLER_ARMING_POLL_SECONDS",
    "CONTROLLER_ARMING_TIMEOUT_SECONDS",
    "MAX_REPORT_BYTES",
    "ChannelControllerArmer",
    "ObservingControllerArmer",
    "SupervisorProcess",
    "TimerReportChannel",
    "default_supervisor_argv",
    "detached_supervisor",
    "report_key",
    "report_path_of",
]
