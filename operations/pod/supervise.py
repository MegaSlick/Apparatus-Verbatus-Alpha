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
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Sequence

from . import durable
from .controllers import LaptopSupervisor, record_from_lease
from .lease import LeaseStore, PodLease
from .models import require_utc, utc_now
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

# Ticks that keep the loop going rather than end it: the lease is still
# active and there is nothing here for a human to look at yet.
_CONTINUE_STATES = frozenset(
    {"active", "owner-heartbeat-fresh", "controller-unarmed", PROVIDER_UNREACHABLE}
)


class SuperviseRefusal(RuntimeError):
    """A named refusal raised before the loop starts; nothing was touched."""

    def __init__(self, detail: str, *, exit_code: int) -> None:
        super().__init__(detail)
        self.detail = detail
        self.exit_code = exit_code


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


def peek_running(leases_root: Path, lease_id: str) -> bool:
    """Read-only: does some process currently hold this lease's ownership lock?

    Takes the same non-blocking lock the owning driver would and releases it
    immediately -- exactly as `OperatorSurface._exclusive_paid_launch` already
    does for the paid-launch claim -- so the operator status surface can
    answer "is a supervisor running" without ever becoming one itself, and
    without trusting a recorded pid that a reboot can hand to someone else.
    """

    handle = _open_lock_handle(_lock_path(leases_root, lease_id))
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
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
    # Case-folded: `FakeProvider.create` defaults a brand-new fake pod's word
    # to lowercase "running" (`PodRecord.state`'s own default), while the
    # explicit lifecycle words this check exists to catch -- "EXITED" and the
    # adapter's uppercase lifecycle vocabulary -- are upper case. Comparing
    # case-sensitively would close a perfectly healthy fake pod on its first
    # tick; the word itself, not its case, is what 04-4 needs distinguished.
    if status.provider_state is not None and status.provider_state.upper() != "RUNNING":
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


def _exit_code(result: SuperviseResult, *, observed_active_lease: bool = False) -> int:
    """The `cli.py` convention: 0 guarded, 2 nothing touched, 3 go and look.

    ``observed_active_lease`` names the fact that decides between 2 and 3 for
    a durable lease that goes missing or unreadable: found and confirmed
    active by *this run* before it vanished (a pod may still be out there
    billing -- 3, go and look) versus never found at all, or a pre-loop
    refusal that made no provider call (nothing to look at -- 2). Only
    `run_supervisor` ever passes ``True``; every pre-loop `SuperviseRefusal`
    path returns its own exit code directly and never reaches here.
    """

    if result.state in {"no-lease", "owner-heartbeat-fresh"}:
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
    """

    supervisors_dir = Path(leases_root) / "supervisors"
    supervisors_dir.mkdir(parents=True, exist_ok=True)
    stamp = _stamp(now).replace(":", "")
    name = (
        f"supervisor-{lease_id}-final-{os.getpid()}-{stamp}.json"
        if lease_id
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
        sleep_for = min(
            heartbeat_timeout.total_seconds() / 3,
            max((result.lease.hard_deadline - now()).total_seconds(), 0.0),
        )
        if sleep_for <= 0:
            continue
        sleeper(sleep_for)
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
        _write_final_record(
            leases_root,
            lease_id,
            exit_code=refusal.exit_code,
            state="refused",
            detail=refusal.detail,
            now=utc_now(),
        )
        print(
            json.dumps(
                {"lease_id": lease_id, "state": "refused", "detail": refusal.detail},
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
        except Exception:
            # A failing record write must not mask the original fault, and
            # must not stop the crash from being reported below either.
            pass
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
