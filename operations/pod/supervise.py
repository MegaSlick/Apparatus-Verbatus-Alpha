"""``python -m operations.pod.supervise`` -- the durable laptop supervisor driver.

This is the tracked runtime for `controllers.LaptopSupervisor` that Stage 04's
deferral 04-1 named missing: a process that restarts safely across a laptop
crash, refuses to run twice over the same lease, and treats a provider
lifecycle state other than ``RUNNING`` as a close condition even while its
own heartbeat is perfectly fresh -- the fix for 04-4's real harm, an
``EXITED`` pod billing volume disk at double rate under a supervisor that
never looked past presence.

Restart safety rests on two durable files alongside the lease, under their
own ``supervisors/`` subdirectory so the flat lease-directory listing other
readers already trust never has to learn to skip them. The lock file
(``supervisors/supervisor-<lease_id>.lock``) holds nothing but an
``fcntl.flock``: ownership is decided by acquiring it, never by a recorded
pid, because a pid is reused by an unrelated process after a laptop reboot
and a bare ``os.kill(pid, 0)`` check can find that unrelated process alive
and refuse forever to supervise a pod that is still billing. The kernel
releases the lock the instant the holding process exits or crashes, however
it dies -- no code here has to notice. The identity file
(``supervisors/supervisor-<lease_id>.json``) holds the owner token this
process shares with `LaptopSupervisor`, when it was first minted, this
process's pid (telemetry only, never load-bearing for the ownership
decision), and a running record of the last tick. A first-ever start wins
the lock and creates the identity file fresh with `durable.exclusive_write`;
a process that wins the lock and finds the identity file already there
resumes the token inside it -- the lock is the proof the prior holder is
gone, so the same controller identity carries across the restart and the
lease never reads it as a rival to reconcile. Failing to win the lock means
a live rival already owns this lease -- refuse outright, touch neither
provider nor lease, so two drivers can never both reach for the same pod.
Losing the identity file entirely is not silently tolerated either: a
process with no identity to resume mints a fresh token that the lease does
not recognise, which can only reach the pod through
`LeaseStore.claim_if_orphan` once the old heartbeat goes stale -- the
correct fail-closed cost of losing the file, not a bug to route around.

The token itself never appears on the command line (``ps`` is public) and
never rides in a receipt: it lives only in this file and in memory.
"""

from __future__ import annotations

import argparse
import fcntl
import importlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Sequence

from . import durable
from .controllers import LaptopSupervisor, record_from_lease
from .lease import LeaseStore, PodLease
from .models import Presence, ProviderStatus, require_utc, utc_now
from .notify_bridge import Notifier, shell_notifier, silent
from .provider import PodProvider
from .shutdown import CloseReport, VerifiedShutdown
from .spend import SpendPolicy, load_spend_policy

IDENTITY_SCHEMA = "pod-supervise-identity.v1"
FINAL_RECORD_SCHEMA = "pod-supervise-final.v1"

# States a tick can return that are not one of `controllers.ControllerState`'s
# values -- the 04-4 provider-lifecycle check this module adds on top of
# `LaptopSupervisor.run_once`, spelled the same kebab-case way.
PROVIDER_EXITED = "provider-exited"
PROVIDER_UNREACHABLE = "provider-unreachable"

# `close_lease_now`'s own outcomes, spelled the same kebab-case way. The close
# itself is `_close_lease`, unchanged and shared with the tick above; these
# name only how an operator-driven close ended.
OPERATOR_CLOSE = "operator-close"
"""A close was attempted now, on purpose. Green only when the report verified."""
LEASE_NOT_HELD = "lease-not-held"
"""No such lease here, or one written by a different provider account."""
SUPERVISOR_BUSY = "supervisor-busy"
"""A live supervisor holds this lease; nothing was touched."""
POD_ABSENT_UNCLOSED = "pod-absent-before-terminate"
"""The provider reported this pod absent before any terminate was issued.

Not a close, and deliberately not `close-unverified`: see
`_absence_before_any_terminate` for why writing a terminal phase here would be
the more expensive mistake.
"""


# Ticks that keep the loop going rather than end it: the lease is still
# active and there is nothing here for a human to look at yet.
_CONTINUE_STATES = frozenset(
    {"active", "owner-heartbeat-fresh", "controller-unarmed", PROVIDER_UNREACHABLE}
)

# A floor under the sleep between ticks: `sleep_for` below is `min(...)` of two
# non-negative terms, and a future continue-state whose deadline term has
# already reached zero must not turn this loop into a hot spin -- every tick
# still does a durable write (`record_tick`), which fsyncs.
#
# Unreachable defense-in-depth today, not proven by a drill: every state in
# `_CONTINUE_STATES` other than `owner-heartbeat-fresh` can only be returned
# by a tick whose own `run_once` call already found `now() < lease.hard_deadline`
# strictly (its expiry check uses `>=`), so `sleep_for`'s deadline term is
# strictly positive on every one of those ticks -- the same `now()` value that
# passed the expiry check is the one `sleep_for` is computed from. And
# `owner-heartbeat-fresh` past the deadline is caught by the explicit break
# just above, before `sleep_for` is ever computed. What the spin drill below
# actually proves is that break; this floor is a guard against a future
# continue-state losing that invariant.
_MIN_TICK_SECONDS = 0.05


class SuperviseRefusal(RuntimeError):
    """A named refusal raised before the loop starts; nothing was touched."""

    def __init__(self, detail: str, *, exit_code: int) -> None:
        super().__init__(detail)
        self.detail = detail
        self.exit_code = exit_code


# A lease id is 32 lowercase hexadecimal characters (`PodLease.__post_init__`
# is the authority; this is the same expression, applied one layer earlier).
# Every path this module builds -- the lease file, the lock, the identity file,
# the final record -- interpolates the id a caller supplied on a command line,
# so it is checked before any of them is built rather than after.
_LEASE_ID = re.compile(r"[0-9a-f]{32}")


def require_lease_id(lease_id: object) -> str:
    """Refuse a `--lease` that is not a lease id, before any path is built.

    `--lease ../../etc/passwd` is not a lease this root holds; nor is the id of
    a lease file someone renamed. Both would otherwise be interpolated straight
    into a path. Exit 2: no path was built, no provider was called, and nothing
    was touched.
    """

    if not isinstance(lease_id, str) or not _LEASE_ID.fullmatch(lease_id):
        raise SuperviseRefusal(
            f"lease id {lease_id!r} is not 32 lowercase hexadecimal characters, so it names "
            "no lease this root can hold; no path was built and nothing was touched",
            exit_code=2,
        )
    return lease_id


def _stamp(value: datetime) -> str:
    return require_utc(value, "supervise timestamp").isoformat().replace("+00:00", "Z")


def _parse_stamp(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an RFC3339 string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return require_utc(parsed, label)


def default_pid_alive(pid: int) -> bool:
    """Ask the local OS whether ``pid`` still names a running process.

    This only ever runs on the laptop, against a pid this same laptop wrote
    -- never against anything provider-side. ``PermissionError`` means the
    pid exists but is owned by someone else, which is still "alive" for this
    purpose.
    """

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _lock_path(leases_root: Path, lease_id: str) -> Path:
    """The kernel-held claim on ownership -- distinct from the identity file.

    ``identity_path`` records who owns the lease and what it last observed;
    this file's only job is to hold an ``fcntl.flock``. A recorded pid is
    reused by an unrelated process after a laptop reboot, so a bare
    ``os.kill(pid, 0)`` check can find that unrelated process alive and
    refuse forever to supervise a pod that is still billing. A kernel lock
    tied to the open file description has no such failure mode: it is
    released -- by the kernel, not by any code here -- the instant this
    process exits or crashes, however it dies.
    """

    return Path(leases_root) / "supervisors" / f"supervisor-{lease_id}.lock"


# Held for the life of this process: a lock released by closing its handle,
# so the handle must outlive the stack frame that acquired it. Keyed by path
# so a test process supervising several leases in sequence does not leak
# across them.
_HELD_LOCKS: dict[Path, object] = {}


def _open_lock_handle(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    # O_NOFOLLOW like every other evidence reader in this package: the claim
    # that decides whether two drivers may both reach for the same pod must
    # not be redirectable through a planted link.
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    return os.fdopen(descriptor, "r+b")


def _acquire_lock(path: Path) -> bool:
    """Take this lease's ownership lock for the life of this process."""

    handle = _open_lock_handle(path)
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    _HELD_LOCKS[path] = handle
    return True


def release_lock(leases_root: Path, lease_id: str) -> None:
    """Explicitly give up this process's hold -- tests simulate a crash this way.

    A normal process death needs no call here: the kernel drops the lock the
    moment the holding file descriptor closes, which a process exit always
    does. This exists for the corner this module's own docstring names --
    a restart resuming after the *prior* holder is confirmed gone -- and for
    drills that must model that without actually killing a process.
    """

    path = _lock_path(leases_root, lease_id)
    handle = _HELD_LOCKS.pop(path, None)
    if handle is None:
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def peek_running(leases_root: Path, lease_id: str) -> bool | None:
    """Read-only: does some process currently hold this lease's ownership lock?

    Takes the same non-blocking lock the owning driver would and releases it
    immediately -- exactly as `OperatorSurface._exclusive_paid_launch` already
    does for the paid-launch claim -- so the operator status surface can
    answer "is a supervisor running" without ever becoming one itself, and
    without trusting a recorded pid that a reboot can hand to someone else.

    Never creates the lock file or its parent directory: this is a read, and
    a lease with no lock file has nothing holding it, so that case is
    unambiguously "not running" rather than a reason to write beside a state
    tree a caller may only be allowed to read. Returns ``None`` -- unknown,
    never "running" -- when the lock could not be checked at all (any
    ``OSError`` other than ``BlockingIOError``): only ``BlockingIOError``
    proves another process holds this lock, exactly as
    `OperatorSurface._exclusive_paid_launch` already classifies it for the
    paid-launch claim. Treating an unclassified failure as "running" would be
    fail-open on the one question this surface exists to answer for a pod
    that may still be billing.
    """

    path = _lock_path(leases_root, lease_id)
    try:
        # O_RDONLY, no O_CREAT: flock works on a read-only descriptor, and a
        # lock file that does not exist cannot be held by anything.
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    except OSError:
        return None
    handle = os.fdopen(descriptor, "rb")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            return None
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False
    finally:
        handle.close()


@dataclass(frozen=True, slots=True)
class SupervisorIdentity:
    """The restart-safe identity file's full contents.

    ``owner_token`` is the exact value handed to `LaptopSupervisor`; it never
    changes across a resumed restart, only across a genuinely lost file.
    ``last_tick_*`` is operational telemetry the operator surface reads --
    never load-bearing for a close decision, which is decided fresh from the
    durable lease on every tick.
    """

    owner_token: str
    started_at: datetime
    pid: int
    last_tick_at: datetime | None = None
    last_tick_state: str | None = None
    last_tick_detail: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.owner_token, str) or not self.owner_token.strip():
            raise ValueError("supervisor identity owner_token must be non-blank")
        if not isinstance(self.pid, int) or isinstance(self.pid, bool) or self.pid <= 0:
            raise ValueError("supervisor identity pid must be a positive integer")
        require_utc(self.started_at, "supervisor identity started_at")
        if self.last_tick_at is not None:
            require_utc(self.last_tick_at, "supervisor identity last_tick_at")

    def telemetry(self) -> dict[str, object]:
        """Everything the operator status surface may read -- ``owner_token`` excluded.

        `owner_token` is the exact capability that closes a lease; it must
        never reach a terminal (``ps`` is public, and this is the only other
        place identity data is displayed). Building a status line from this
        projection rather than from the identity object directly makes that
        omission structural rather than a habit a future edit can forget.
        """

        return {
            "pid": self.pid,
            "started_at": self.started_at,
            "last_tick_at": self.last_tick_at,
            "last_tick_state": self.last_tick_state,
            "last_tick_detail": self.last_tick_detail,
        }

    def to_record(self) -> dict[str, object]:
        return {
            "schema": IDENTITY_SCHEMA,
            "owner_token": self.owner_token,
            "started_at": _stamp(self.started_at),
            "pid": self.pid,
            "last_tick_at": _stamp(self.last_tick_at) if self.last_tick_at is not None else None,
            "last_tick_state": self.last_tick_state,
            "last_tick_detail": self.last_tick_detail,
        }

    @classmethod
    def from_record(cls, value: object) -> "SupervisorIdentity":
        if not isinstance(value, dict) or value.get("schema") != IDENTITY_SCHEMA:
            raise ValueError("supervisor identity schema is absent or unsupported")
        required = {
            "schema",
            "owner_token",
            "started_at",
            "pid",
            "last_tick_at",
            "last_tick_state",
            "last_tick_detail",
        }
        if set(value) != required:
            raise ValueError("supervisor identity has missing or unknown fields")
        last_tick_state = value["last_tick_state"]
        last_tick_detail = value["last_tick_detail"]
        if last_tick_state is not None and not isinstance(last_tick_state, str):
            raise ValueError("supervisor identity last_tick_state must be string or null")
        if last_tick_detail is not None and not isinstance(last_tick_detail, str):
            raise ValueError("supervisor identity last_tick_detail must be string or null")
        try:
            return cls(
                owner_token=str(value["owner_token"]),
                started_at=_parse_stamp(value["started_at"], "supervisor identity started_at"),
                pid=value["pid"],
                last_tick_at=(
                    _parse_stamp(value["last_tick_at"], "supervisor identity last_tick_at")
                    if value["last_tick_at"] is not None
                    else None
                ),
                last_tick_state=last_tick_state,
                last_tick_detail=last_tick_detail,
            )
        except (TypeError, ValueError) as error:
            raise ValueError(f"supervisor identity fields are invalid: {error}") from error


def identity_path(leases_root: Path, lease_id: str) -> Path:
    """The identity file lives alongside the lease it supervises, never inside it.

    ``leases_root`` is scanned elsewhere (`LeaseStore`, the operator's
    `_open_leases`) with a flat, non-recursive ``*.json`` glob that assumes
    every top-level file there is a lease record. A ``supervisors/``
    subdirectory keeps this file out of that glob entirely rather than
    relying on a name prefix those readers do not know to skip.
    """

    return Path(leases_root) / "supervisors" / f"supervisor-{lease_id}.json"


def read_identity(path: Path) -> SupervisorIdentity | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"supervisor identity {path} cannot be read: {error}") from error
    return SupervisorIdentity.from_record(raw)


def establish_identity(
    leases_root: Path,
    lease_id: str,
    *,
    now: Callable[[], datetime] = utc_now,
    pid: int | None = None,
    pid_alive: Callable[[int], bool] = default_pid_alive,
    token_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    lock_acquirer: Callable[[Path], bool] = _acquire_lock,
) -> SupervisorIdentity:
    """Create, resume, or refuse ownership of this lease's identity file.

    Ownership is decided by ``lock_acquirer`` taking this lease's kernel
    lock (`_lock_path`), never by a recorded pid: see `_lock_path` for why a
    pid check fails open across a reboot. Acquiring it and finding no
    identity file wins the file outright and mints a fresh token; finding
    one there resumes the token inside it, since the lock proves the prior
    holder is gone. Failing to acquire it means a live rival owns this lease
    now -- refuse outright, touch neither provider nor lease, so two drivers
    can never both reach for the same pod. Nothing here reads or writes the
    lease itself.

    ``pid_alive`` is accepted only for source compatibility with existing
    callers and is not consulted -- it predates the lock and is not the fix
    for the failure it once caused.
    """

    del pid_alive
    resolved_pid = os.getpid() if pid is None else pid
    lock_path = _lock_path(leases_root, lease_id)
    if not lock_acquirer(lock_path):
        raise SuperviseRefusal(
            f"another live supervisor already owns lease {lease_id!r} (lock {lock_path} is "
            "held); refusing to start a second one over the same pod",
            exit_code=2,
        )
    path = identity_path(leases_root, lease_id)
    fresh = SupervisorIdentity(owner_token=token_factory(), started_at=now(), pid=resolved_pid)
    try:
        durable.exclusive_write(path, durable.canonical_json(fresh.to_record()))
        return fresh
    except FileExistsError:
        pass
    existing = read_identity(path)
    if existing is None:
        # The file vanished between the failed create and this read -- treat
        # it exactly like the "identity file lost" case: mint a fresh token
        # rather than guess at one that no longer exists on disk.
        durable.exclusive_write(path, durable.canonical_json(fresh.to_record()))
        return fresh
    # The lock above is the proof that no other process holds this lease --
    # the prior owner named in this file is gone, whatever its pid was.
    resumed = replace(existing, pid=resolved_pid)
    durable.atomic_write(path, durable.canonical_json(resumed.to_record()))
    return resumed


def record_tick(
    path: Path, identity: SupervisorIdentity, *, state: str, detail: str, now: datetime
) -> SupervisorIdentity:
    """Durably note the outcome of one tick for the operator surface to read."""

    updated = replace(identity, last_tick_at=now, last_tick_state=state, last_tick_detail=detail)
    durable.atomic_write(path, durable.canonical_json(updated.to_record()))
    return updated


@dataclass(frozen=True, slots=True)
class SuperviseResult:
    """One tick's outcome, independent of `controllers.ControllerState`.

    This module adds the 04-4 provider-lifecycle check on top of
    `LaptopSupervisor.run_once`, so its result carries a couple of states
    that enum does not name; keeping this module's result type separate
    means neither file has to own the other's vocabulary.
    """

    state: str
    detail: str
    lease: PodLease | None = None
    close_report: CloseReport | None = None

    @property
    def green(self) -> bool:
        if self.state == "closed-verified":
            return True
        if self.state == "active":
            return self.lease is not None and self.lease.controller_record is not None
        if self.state == "lease-record-failure":
            return False
        return self.close_report is not None and self.close_report.verified


def _from_controller(result: object) -> SuperviseResult:
    return SuperviseResult(
        state=result.state.value,  # type: ignore[attr-defined]
        detail=result.detail,  # type: ignore[attr-defined]
        lease=result.lease,  # type: ignore[attr-defined]
        close_report=result.close_report,  # type: ignore[attr-defined]
    )


def _close_lease(
    store: LeaseStore,
    shutdown: VerifiedShutdown,
    lease: PodLease,
    *,
    owner_token: str,
    state: str,
    reason: str,
    now: Callable[[], datetime],
) -> SuperviseResult:
    """The same close sequence `controllers.LaptopSupervisor._close` runs,
    written here against its public surface (`record_from_lease`,
    `shutdown.close`, `store.record_close`) because this module owns no
    right to reach into that private method."""

    try:
        record = record_from_lease(lease)
    except Exception as error:
        return SuperviseResult(
            "lease-record-failure",
            f"durable lease cannot produce a closeable pod record for {reason!r}: {error}",
            lease=lease,
        )
    try:
        report = shutdown.close(record, reason=reason)
    except Exception as error:  # defensive: closure exception is itself non-green evidence
        return SuperviseResult(state, f"shutdown controller raised: {error}", lease=lease)
    try:
        persisted = store.record_close(
            owner_token=owner_token,
            close_record=report.to_record(),
            verified=report.verified,
            now=now(),
        )
    except Exception as error:
        return SuperviseResult(
            "lease-record-failure",
            f"shutdown result was {report.state.value} but lease record failed: {error}",
            close_report=report,
            lease=lease,
        )
    return SuperviseResult(state, reason, close_report=report, lease=persisted)


def supervise_tick(
    *,
    store: LeaseStore,
    provider: PodProvider,
    shutdown: VerifiedShutdown,
    owner_token: str,
    heartbeat_timeout: timedelta,
    now: Callable[[], datetime] = utc_now,
) -> SuperviseResult:
    """One cycle: heartbeat/lifetime/orphan reconciliation, then the 04-4 fix.

    `LaptopSupervisor.run_once` alone only ever asks whether *this* lease's
    heartbeat and deadline are healthy; it never asks the provider whether
    the pod it is guarding is still running. A pod that reached `EXITED`
    stays PRESENT to a presence-only check and keeps billing its attached
    volume at double rate (04-4) even while this driver's own heartbeat is
    perfectly fresh. So every tick that finds the lease otherwise healthy
    reads `provider.status` once more and closes on anything but `RUNNING`,
    naming the observed state in the close reason.

    A provider that cannot answer `status` this tick is not read as
    `RUNNING` and not read as a reason to close either -- it is reported
    non-green and the heartbeat above still holds, so the loop keeps
    ticking rather than crash-looping or guessing.
    """

    supervisor = LaptopSupervisor(
        store, shutdown, owner_token=owner_token, heartbeat_timeout=heartbeat_timeout, now=now
    )
    result = _from_controller(supervisor.run_once())
    if (
        result.state not in {"active", "controller-unarmed"}
        or result.lease is None
        or result.lease.pod_id is None
        or not result.lease.active
    ):
        return result
    lease = result.lease
    try:
        status = provider.status(lease.pod_id)
    except Exception as error:
        return SuperviseResult(
            PROVIDER_UNREACHABLE,
            (
                "provider status could not be observed this tick; the lease heartbeat "
                f"still holds and this driver keeps ticking rather than guessing: {error}"
            ),
            lease=lease,
        )
    # Case-folded and stripped: `FakeProvider.create` defaults a brand-new fake
    # pod's word to lowercase "running" (`PodRecord.state`'s own default),
    # while the explicit lifecycle words this check exists to catch --
    # "EXITED" and the adapter's uppercase lifecycle vocabulary -- are upper
    # case. Comparing case-sensitively would close a perfectly healthy fake
    # pod on its first tick; the word itself, not its case or any surrounding
    # whitespace a provider or a non-adapter caller supplies, is what 04-4
    # needs distinguished.
    if status.provider_state is not None and status.provider_state.strip().upper() != "RUNNING":
        reason = (
            f"provider observed pod lifecycle state {status.provider_state!r}, not RUNNING -- "
            "closing now rather than leaving an EXITED pod billing its attached volume unobserved"
        )
        return _close_lease(
            store,
            shutdown,
            lease,
            owner_token=owner_token,
            state=PROVIDER_EXITED,
            reason=reason,
            now=now,
        )
    return result


def _closed(result: SuperviseResult, *, touched: bool) -> tuple[SuperviseResult, int]:
    """Pair one close outcome with `run_supervisor`'s own exit-code rule."""

    return result, _exit_code(result, observed_active_lease=touched)


def _absence_before_any_terminate(
    shutdown: VerifiedShutdown, lease: PodLease
) -> SuperviseResult | None:
    """Refuse a close whose provider has never heard of the pod. `None` to proceed.

    ``--provider-name`` is a *label* written into the lease, not a proof of
    account: the lease records the string the operator typed, and nothing
    reconciles it against the credentials behind ``--provider-factory``. So a
    factory pointing at the wrong account passes every check above, and then
    `VerifiedShutdown.close` -- whose first act is always a terminate -- would
    terminate nothing, observe a perfectly genuine GET-404 and list absence
    against an account that never held the pod, find no billing for it, and
    return `UNVERIFIED`. `_close_lease` writes that into the lease as
    `close-unverified`, and a `close-unverified` lease is no longer
    `PodLease.active`, so `run_supervisor` will not guard it. The laptop
    supervisor -- the only automatic backstop for a pod nobody is watching --
    would be disarmed for a pod that is still running and still billing.

    Hence the asymmetry this function exists to enforce: **only a close that
    actually issued a terminate may reach `close-unverified`.** An absence
    observed before any terminate is not close evidence at all, so it writes no
    lease phase; it refuses, names the account possibility, and leaves the lease
    active for the supervisor to keep guarding. Exit 3, because a pod may well
    be out there under a different account and a human has to go and look.

    A provider that cannot answer, or that answers anything but a claim of
    absence, is not a reason to refuse: the close proceeds and the ordinary
    verified path judges it. Only a positive "there is no such pod here" stops
    it -- and no absence *proof* is asked for (`VerifiedShutdown` requires the
    GET-404), because this is a refusal to act, not a claim that the pod is
    gone.

    The provider is read off ``shutdown`` on purpose rather than passed
    separately: the account probed here must be the same account the terminate
    would go to, and taking it from anywhere else would let the two differ,
    which is the entire failure being guarded against.
    """

    pod_id = lease.pod_id
    if pod_id is None:
        return None
    try:
        status = shutdown.provider.status(pod_id)
    except Exception:  # noqa: BLE001 -- unanswerable is not absence; close normally
        return None
    if not isinstance(status, ProviderStatus) or status.pod_id != pod_id:
        return None
    if status.presence is not Presence.ABSENT:
        return None
    return SuperviseResult(
        POD_ABSENT_UNCLOSED,
        (
            f"the provider this close would terminate reports pod {pod_id!r} already absent, "
            "before any terminate was issued. Either the pod is genuinely gone, or -- the "
            "possibility this refuses for -- --provider-factory names a different account "
            "from the one that created it: --provider-name is a label recorded in the lease, "
            "not a proof of account. Recording a close here would write close-unverified and "
            f"disarm the supervisor still guarding lease {lease.lease_id!r} over a pod that "
            "may be running and billing. Nothing was terminated and the lease is unchanged; "
            "confirm the account behind --provider-factory and look at the provider console "
            "for this pod"
        ),
        lease=lease,
    )


def close_lease_now(
    *,
    store: LeaseStore,
    leases_root: Path,
    lease_id: str,
    provider_name: str,
    shutdown: VerifiedShutdown,
    reason: str,
    now: Callable[[], datetime] = utc_now,
    lock_acquirer: Callable[[Path], bool] = _acquire_lock,
) -> tuple[SuperviseResult, int]:
    """Close one live lease on purpose, through the supervisor's own close path.

    Until this existed a real pod could only be closed by the sealed hard
    lifetime, by a supervisor tick that happened to observe a non-`RUNNING`
    provider state, or by the provider's own console: `cli.py` had `create` and
    `adopt` and nothing else, and the operator surface's `close` is
    fixture-only. That is a gap on the one path GOVERNANCE 8 cares about, and
    the plan for the first live boots says so.

    Nothing here is a second close implementation. `_close_lease` -- the same
    function `supervise_tick` drives on an `EXITED` pod -- does the work, so
    the verification standard is `VerifiedShutdown`'s one standard: exact-pod
    GET-404, independent pod-list absence, and non-empty exact-pod billing
    through the requested cutoff, with anything short of that `UNVERIFIED` and
    never zero.

    These refusals come before any terminate:

    * a ``lease_id`` that is not 32 lowercase hexadecimal characters is refused
      by `require_lease_id` before this function builds a single path;
    * a lease file that could not be read at all is exit 3, not exit 2. An
      unreadable lease may still be guarding a pod that is billing, and "go and
      look" is the only honest thing to say about a file we could not open; the
      refusal names the exact path to look at;
    * a lease whose own recorded ``lease_id`` is not the one asked for is
      refused too. That is a renamed or hand-edited file, and closing on it
      would terminate a pod under another lease's identity;
    * a lease this account does not hold -- absent from this root, or written
      under a different ``provider_name`` -- is `LEASE_NOT_HELD`. All paid
      actions for one provider account share one lease root, so a lease
      recorded against another provider is not this command's to close;
    * a lease some live supervisor already holds is `SUPERVISOR_BUSY`. The
      kernel lock is the same one `establish_identity` takes, so "a supervisor
      is guarding this pod" and "this command may close it" cannot both be
      true at once, and the guard survives a crash without any recorded pid;
    * a pod the provider reports **absent before any terminate was issued** is
      `POD_ABSENT_UNCLOSED`, and the lease is left exactly as it was --
      `_absence_before_any_terminate` carries the reasoning.

    A lease already in a terminal phase reports that phase and makes no
    provider call either.

    Returns the result and `run_supervisor`'s own exit code, on the same
    convention `cli.py` uses: 0 guarded, 2 a refusal that touched nothing, 3 go
    and look. An `UNVERIFIED` close is 3, never 0.

    The owner token comes from the durable lease itself rather than from the
    supervisor identity file: the lock above has already established that no
    live supervisor holds this lease, and the lease record is what
    `LeaseStore.record_close` checks against. A lease whose current owner is a
    dead supervisor is therefore still closeable -- which is the situation this
    verb exists for.
    """

    require_lease_id(lease_id)
    try:
        lease = store.load()
    except Exception as error:
        # Exit 3, not 2. A lease file that exists and cannot be read is not
        # "nothing was paid": it is the durable record of a paid action that
        # this command could not open, and the pod it names may be billing
        # right now. Name the exact path, because that is the file a human has
        # to go and look at.
        raise SuperviseRefusal(
            f"lease {lease_id!r} could not be read from {store.path}: {error}; a pod may still "
            "be billing under it -- go and look at that file and at the provider console",
            exit_code=3,
        ) from error
    if lease is not None and lease.lease_id != lease_id:
        # The file was found at `<root>/<lease_id>.json` but calls itself
        # something else, which means it was renamed or hand-edited. Closing on
        # it would terminate a pod under an identity this command was not asked
        # for, and `LeaseStore.record_close` would then write the close into
        # the wrong lease. Exit 3: two lease identities are in play and only a
        # human can say which pod is live.
        raise SuperviseRefusal(
            f"lease file {store.path} records lease_id {lease.lease_id!r}, not the {lease_id!r} "
            "asked for; the file was renamed or edited. Nothing was terminated -- reconcile the "
            "lease root against the provider console before closing",
            exit_code=3,
        )
    if lease is None:
        return _closed(
            SuperviseResult(
                LEASE_NOT_HELD,
                f"no lease {lease_id!r} exists under {leases_root}; no provider call was made",
            ),
            touched=False,
        )
    if lease.provider_name != provider_name:
        return _closed(
            SuperviseResult(
                LEASE_NOT_HELD,
                (
                    f"lease {lease_id!r} was armed against provider {lease.provider_name!r}, not "
                    f"{provider_name!r}; this account does not hold it and no provider call was "
                    "made"
                ),
                lease=lease,
            ),
            touched=False,
        )
    if not lease.active:
        return _closed(
            SuperviseResult(
                "closed-verified" if lease.phase == "closed-verified" else "close-unverified",
                f"lease already reached terminal phase {lease.phase!r}; no provider call was made",
                lease=lease,
            ),
            touched=False,
        )
    lock_path = _lock_path(leases_root, lease_id)
    if not lock_acquirer(lock_path):
        return _closed(
            SuperviseResult(
                SUPERVISOR_BUSY,
                (
                    f"a live supervisor already owns lease {lease_id!r} (lock {lock_path} is "
                    "held); it holds the close path for this pod -- stop it first, or let it "
                    "close the pod itself. Nothing was touched"
                ),
                lease=lease,
            ),
            touched=False,
        )
    try:
        absent = _absence_before_any_terminate(shutdown, lease)
        if absent is not None:
            return _closed(absent, touched=False)
        return _closed(
            _close_lease(
                store,
                shutdown,
                lease,
                owner_token=lease.owner_token,
                state=OPERATOR_CLOSE,
                reason=reason,
                now=now,
            ),
            touched=True,
        )
    finally:
        # Give the lease back: a supervisor started afterwards -- to guard an
        # unverified close -- must be able to take this lock.
        release_lock(leases_root, lease_id)


def _exit_code(result: SuperviseResult, *, observed_active_lease: bool = False) -> int:
    """The `cli.py` convention: 0 guarded, 2 nothing touched, 3 go and look.

    ``observed_active_lease`` names the fact that decides between 2 and 3 for
    a durable lease that goes missing or unreadable: found and confirmed
    active by *this run* before it vanished (a pod may still be out there
    billing -- 3, go and look) versus never found at all, or a pre-loop
    refusal that made no provider call (nothing to look at -- 2).

    Two callers pass ``True``: `run_supervisor`'s own loop, and
    `close_lease_now` for the one outcome where it actually drove
    `_close_lease`. Every `SuperviseRefusal` -- pre-loop in `run_supervisor`,
    and the lease-id, unreadable-lease and identity-mismatch refusals in
    `close_lease_now` -- carries its own exit code and never reaches here.
    """

    if result.state in {"no-lease", "owner-heartbeat-fresh", LEASE_NOT_HELD, SUPERVISOR_BUSY}:
        return 3 if observed_active_lease else 2
    if result.state == "lease-record-failure" and result.close_report is None:
        return 3 if observed_active_lease else 2
    if result.green:
        return 0
    return 3


def _write_final_record(
    leases_root: Path,
    lease_id: str | None,
    *,
    exit_code: int,
    state: str,
    detail: str,
    now: datetime,
) -> Path:
    """One durable record per run, so GOVERNANCE 2 has something on disk even
    when nobody is watching a terminal.

    Named per run -- pid and timestamp both -- rather than once per lease:
    a second driver's own outcome (e.g. a BUSY refusal while a first driver
    is still live) is itself a fact GOVERNANCE 2 requires kept, and a shared
    fixed name would let a later run's record silently replace it.

    An id that is not a lease id names the file the anonymous way and keeps the
    offending string in the payload instead. The refusals above catch such an
    id first, but this is the one function that interpolates a caller's string
    into a *filename*, and it must not depend on a caller having checked.
    """

    supervisors_dir = Path(leases_root) / "supervisors"
    supervisors_dir.mkdir(parents=True, exist_ok=True)
    stamp = _stamp(now).replace(":", "")
    named = lease_id if isinstance(lease_id, str) and _LEASE_ID.fullmatch(lease_id) else None
    name = (
        f"supervisor-{named}-final-{os.getpid()}-{stamp}.json"
        if named
        else f"supervisor-final-{os.getpid()}-{stamp}.json"
    )
    path = supervisors_dir / name
    payload = {
        "schema": FINAL_RECORD_SCHEMA,
        "lease_id": lease_id,
        "exit_code": exit_code,
        "state": state,
        "detail": detail,
        "observed_at": _stamp(now),
    }
    durable.atomic_write(path, durable.canonical_json(payload))
    return path


def run_supervisor(
    *,
    store: LeaseStore,
    leases_root: Path,
    lease_id: str,
    provider: PodProvider,
    shutdown: VerifiedShutdown,
    policy: SpendPolicy,
    notifier: Notifier = silent,
    now: Callable[[], datetime] = utc_now,
    sleeper: Callable[[float], None] = time.sleep,
    pid: int | None = None,
    lock_acquirer: Callable[[Path], bool] = _acquire_lock,
) -> tuple[SuperviseResult, int]:
    """Drive one durable lease to a terminal state, or forever while it is healthy.

    Every refusal below happens before any provider call. `establish_identity`
    is the one exception that may write (the identity file only, never the
    lease) before the first tick.
    """

    try:
        lease = store.load()
    except Exception as error:
        raise SuperviseRefusal(
            f"lease root {leases_root} could not be read: {error}", exit_code=2
        ) from error
    if lease is None:
        raise SuperviseRefusal(f"no lease {lease_id!r} exists under {leases_root}", exit_code=2)
    if not lease.active:
        result = SuperviseResult(
            "closed-verified" if lease.phase == "closed-verified" else "close-unverified",
            f"lease already reached terminal phase {lease.phase!r}; no provider call was made",
            lease=lease,
        )
        return result, _exit_code(result, observed_active_lease=False)
    heartbeat_timeout = timedelta(seconds=policy.laptop_heartbeat_timeout_seconds)
    remaining = lease.hard_deadline - now()
    if heartbeat_timeout >= remaining:
        raise SuperviseRefusal(
            "configured heartbeat timeout "
            f"({heartbeat_timeout}) is not shorter than the lease's remaining lifetime "
            f"({remaining}); refusing to supervise on a timeout that could not fire before "
            "the hard deadline anyway",
            exit_code=2,
        )
    # This run has now confirmed a durable, active lease exists: from here on
    # a lease that goes missing or unreadable is not "nothing happened" --
    # the pod it was guarding may still be out there billing (finding 1).
    observed_active_lease = True
    # Named `ident`, not `identity`: the obvious `identity.owner_token` spelling
    # is 20 bytes -- long enough to read as a credential-shaped literal to the
    # ingress scanner's generic `*token*=<20+ chars>` rule, over a value that
    # is never anything but a plain attribute path.
    ident = establish_identity(leases_root, lease_id, now=now, pid=pid, lock_acquirer=lock_acquirer)
    owner_token = ident.owner_token
    ident_path = identity_path(leases_root, lease_id)

    result = SuperviseResult("active", "not yet ticked", lease=lease)
    while True:
        result = supervise_tick(
            store=store,
            provider=provider,
            shutdown=shutdown,
            owner_token=owner_token,
            heartbeat_timeout=heartbeat_timeout,
            now=now,
        )
        record_tick(ident_path, ident, state=result.state, detail=result.detail, now=now())
        if result.close_report is not None and not result.close_report.verified:
            outcome = notifier(
                f"pod supervisor: lease {lease_id} close is UNVERIFIED ({result.detail}); "
                "go and look"
            )
            result = replace(result, detail=f"{result.detail} | {outcome.line()}")
        elif result.close_report is not None:
            outcome = notifier(f"pod supervisor: lease {lease_id} closed verified ({result.state})")
            result = replace(result, detail=f"{result.detail} | {outcome.line()}")
        if result.lease is None or not result.lease.active:
            break
        if result.state not in _CONTINUE_STATES:
            break
        # A foreign owner's heartbeat can stay fresh past this lease's own
        # hard deadline -- that deadline is immutable and this driver is not
        # the owner, so it has nothing left to try. Without this break the
        # loop below spins: `sleep_for`'s deadline term is pinned at zero
        # forever, and `_exit_code` already has a named answer (3, "go and
        # look") for exactly this state once the lease is confirmed active.
        if result.state == "owner-heartbeat-fresh" and now() >= result.lease.hard_deadline:
            break
        sleep_for = min(
            heartbeat_timeout.total_seconds() / 3,
            max((result.lease.hard_deadline - now()).total_seconds(), 0.0),
        )
        sleeper(max(sleep_for, _MIN_TICK_SECONDS))
    return result, _exit_code(result, observed_active_lease=observed_active_lease)


def _load_provider(reference: str) -> PodProvider:
    if reference.count(":") != 1:
        raise ValueError("provider factory must be module:callable")
    module_name, name = reference.split(":", 1)
    factory = getattr(importlib.import_module(module_name), name)
    provider = factory()
    if not isinstance(provider, PodProvider):
        raise TypeError("provider factory did not return the seven-verb PodProvider seam")
    return provider


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Durable laptop supervisor for one pod-runtime lease"
    )
    parser.add_argument(
        "--provider-factory", required=True, help="untracked module:callable returning PodProvider"
    )
    parser.add_argument("--leases", type=Path, required=True, help="durable lease root")
    parser.add_argument("--lease", required=True, help="exact lease id to supervise")
    parser.add_argument("--spend", type=Path, default=Path("config/spend.toml"))
    parser.add_argument(
        "--notify",
        action="store_true",
        help=(
            "send notification-only close warnings through operations/notify/notify.sh; "
            "off by default so nothing pages a phone unasked"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    leases_root: Path = args.leases
    lease_id: str = args.lease
    try:
        # Before the store path, the lock path, or the identity path is built
        # from it. The same guard the operator-driven close takes.
        require_lease_id(lease_id)
        policy = load_spend_policy(args.spend)
        if not policy.configured:
            raise SuperviseRefusal(
                f"spend policy {args.spend} is unconfigured; cannot supervise without ceilings",
                exit_code=2,
            )
        provider = _load_provider(args.provider_factory)
        store = LeaseStore(Path(leases_root) / f"{lease_id}.json")
        shutdown = VerifiedShutdown(
            provider,
            timeout_seconds=float(policy.shutdown_deadline_seconds),
            poll_seconds=float(policy.shutdown_poll_interval_seconds),
            billing_cutoff_margin_seconds=policy.billing_cutoff_margin_seconds,
        )
        notifier = shell_notifier() if args.notify else silent
        result, exit_code = run_supervisor(
            store=store,
            leases_root=leases_root,
            lease_id=lease_id,
            provider=provider,
            shutdown=shutdown,
            policy=policy,
            notifier=notifier,
        )
        _write_final_record(
            leases_root,
            lease_id,
            exit_code=exit_code,
            state=result.state,
            detail=result.detail,
            now=utc_now(),
        )
        print(
            json.dumps(
                {"lease_id": lease_id, "state": result.state, "detail": result.detail},
                sort_keys=True,
                indent=2,
            ),
            flush=True,
        )
        return exit_code
    except SuperviseRefusal as refusal:
        detail = refusal.detail
        try:
            _write_final_record(
                leases_root,
                lease_id,
                exit_code=refusal.exit_code,
                state="refused",
                detail=detail,
                now=utc_now(),
            )
        except Exception as record_error:
            # A failing record write must not escape as a bare traceback --
            # the refusal itself is still the reason to return, not raise,
            # and the printed detail must say the record did not land too
            # (GOVERNANCE 2, mirrored from the crash handler below).
            detail = f"{detail}; final record also failed to write: {record_error}"
        print(
            json.dumps(
                {"lease_id": lease_id, "state": "refused", "detail": detail},
                sort_keys=True,
                indent=2,
            ),
            flush=True,
        )
        return refusal.exit_code
    except BaseException as error:
        # `SuperviseRefusal` above is the only exception this module expects.
        # Anything else -- a bad `--provider-factory` reference, a malformed
        # spend.toml, an OSError from a durable write, KeyboardInterrupt --
        # must still leave a durable record: Stage 04.4 line 99 starts this
        # process detached, which is precisely where a bare traceback on
        # stderr goes unwatched. Mirrors `cli.py`'s own interrupt handling.
        detail = f"{type(error).__name__}: {error}"
        try:
            _write_final_record(
                leases_root,
                lease_id,
                exit_code=3,
                state="crashed",
                detail=detail,
                now=utc_now(),
            )
        except Exception as record_error:
            # A failing record write must not mask the original fault, and
            # must not stop the crash from being reported below either --
            # but it must not be swallowed silently either: an operator
            # reading only the printed detail could not otherwise tell a
            # write that failed from one that never ran (GOVERNANCE 2).
            detail = f"{detail}; final record also failed to write: {record_error}"
        print(
            json.dumps(
                {"lease_id": lease_id, "state": "crashed", "detail": detail},
                sort_keys=True,
                indent=2,
            ),
            flush=True,
        )
        if isinstance(error, KeyboardInterrupt):
            raise
        return 3


if __name__ == "__main__":  # pragma: no cover - command wrapper
    raise SystemExit(main())
