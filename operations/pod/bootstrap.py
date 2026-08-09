"""Idempotent, journaled pod bootstrap at one exact repository commit."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Callable, Protocol

from common.chairs.models import AbsentChair, ChairIdentity
from common.chairs.registry import ChairRegistry

from .models import require_utc, utc_now
from .preflight import _is_cache_mismatch

BOOTSTRAP_SCHEMA = "pod-bootstrap.v1"


class BootstrapStep(StrEnum):
    """Named, resumable steps; completion is recorded only after its action returns."""

    REPOSITORY = "repository"
    UV_ENVIRONMENT = "uv-environment"
    TRANSFER = "transfer"
    CHAIR_CACHE = "chair-cache"
    PREFLIGHT = "preflight"


ORDERED_STEPS = tuple(BootstrapStep)


class BootstrapStepFailure(RuntimeError):
    """A named non-green terminal bootstrap result, with a plain-language remedy."""

    def __init__(self, step: BootstrapStep, detail: str, remediation: str) -> None:
        self.step = step
        self.detail = detail
        self.remediation = remediation
        super().__init__(f"{step.value}: {detail}")


@dataclass(frozen=True, slots=True)
class BootstrapPlan:
    """Immutable pinned inputs; no branch or lockfile inference is permitted."""

    repository_commit: str
    lockfile: Path

    def __post_init__(self) -> None:
        if len(self.repository_commit) != 40 or any(
            character not in "0123456789abcdef" for character in self.repository_commit
        ):
            raise ValueError("bootstrap repository commit must be a full lowercase Git SHA-1")
        object.__setattr__(self, "lockfile", Path(self.lockfile))


@dataclass(frozen=True, slots=True)
class BootstrapReport:
    """The journal projection: either green, or a named red step and remedy."""

    color: str
    completed: tuple[BootstrapStep, ...]
    receipts: dict[str, object]
    failure_step: BootstrapStep | None = None
    detail: str | None = None
    remediation: str | None = None

    @property
    def green(self) -> bool:
        return self.color == "green"

    def to_record(self) -> dict[str, object]:
        return {
            "color": self.color,
            "completed": [step.value for step in self.completed],
            "receipts": self.receipts,
            "failure_step": self.failure_step.value if self.failure_step else None,
            "detail": self.detail,
            "remediation": self.remediation,
        }


class BootstrapActions(Protocol):
    """Injected effects, so tests prove resumes without cloning or running uv."""

    def checkout_commit(self, commit: str) -> dict[str, object]:
        """Materialize and verify exactly this commit."""

    def sync_uv_environment(self, lockfile: Path) -> dict[str, object]:
        """Build the environment from the existing lockfile without resolution drift."""

    def resume_transfer(self) -> dict[str, object]:
        """Resume checksum-aware transfer, or raise a named partial-transfer failure."""

    def verify_chair_cache(self) -> dict[str, object]:
        """Verify every configured chair pin, with at most one same-pin re-fetch each."""

    def run_preflight(self) -> dict[str, object]:
        """Return one green preflight receipt or raise with its named red reason."""


class BootstrapJournal:
    """Durable bootstrap progress.  A crash leaves the current step incomplete."""

    def __init__(self, path: str | Path, plan: BootstrapPlan, *, now: Callable[[], datetime] = utc_now) -> None:
        self.path = Path(path)
        self.plan = plan
        self.now = now

    def load_or_create(self) -> dict[str, object]:
        if not self.path.exists():
            record = {
                "schema": BOOTSTRAP_SCHEMA,
                "repository_commit": self.plan.repository_commit,
                "lockfile": str(self.plan.lockfile),
                "started_at": _stamp(self.now()),
                "completed": [],
                "receipts": {},
                "status": "running",
                "failure": None,
            }
            self._write(record)
            return record
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise BootstrapStepFailure(
                BootstrapStep.REPOSITORY,
                f"bootstrap journal cannot be read: {error}",
                "Preserve the broken journal for review, then start a new explicit bootstrap.",
            ) from error
        self._validate(raw)
        if raw["repository_commit"] != self.plan.repository_commit or raw["lockfile"] != str(self.plan.lockfile):
            raise BootstrapStepFailure(
                BootstrapStep.REPOSITORY,
                "existing bootstrap journal names different pinned inputs",
                "Do not reuse this journal for another commit or lockfile; create a new run directory.",
            )
        return raw

    def mark_complete(self, record: dict[str, object], step: BootstrapStep, receipt: dict[str, object]) -> None:
        completed = list(record["completed"])
        if step.value not in completed:
            completed.append(step.value)
        record["completed"] = completed
        receipts = dict(record["receipts"])
        receipts[step.value] = receipt
        record["receipts"] = receipts
        record["status"] = "running"
        record["failure"] = None
        self._write(record)

    def mark_green(self, record: dict[str, object]) -> None:
        record["status"] = "green"
        record["failure"] = None
        self._write(record)

    def mark_failure(self, record: dict[str, object], failure: BootstrapStepFailure) -> None:
        record["status"] = "red"
        record["failure"] = {
            "step": failure.step.value,
            "detail": failure.detail,
            "remediation": failure.remediation,
            "at": _stamp(self.now()),
        }
        self._write(record)

    def _validate(self, raw: object) -> None:
        if not isinstance(raw, dict) or raw.get("schema") != BOOTSTRAP_SCHEMA:
            raise BootstrapStepFailure(
                BootstrapStep.REPOSITORY,
                "bootstrap journal schema is absent or unsupported",
                "Preserve it for review and create a new explicit bootstrap journal.",
            )
        required = {
            "schema",
            "repository_commit",
            "lockfile",
            "started_at",
            "completed",
            "receipts",
            "status",
            "failure",
        }
        if set(raw) != required:
            raise BootstrapStepFailure(
                BootstrapStep.REPOSITORY,
                "bootstrap journal has missing or unknown fields",
                "Preserve it for review and create a new explicit bootstrap journal.",
            )
        if not isinstance(raw["completed"], list) or not all(
            item in {step.value for step in ORDERED_STEPS} for item in raw["completed"]
        ):
            raise BootstrapStepFailure(
                BootstrapStep.REPOSITORY,
                "bootstrap journal completion list is invalid",
                "Preserve it for review and create a new explicit bootstrap journal.",
            )
        if not isinstance(raw["receipts"], dict) or raw["status"] not in {"running", "green", "red"}:
            raise BootstrapStepFailure(
                BootstrapStep.REPOSITORY,
                "bootstrap journal state is invalid",
                "Preserve it for review and create a new explicit bootstrap journal.",
            )

    def _write(self, record: dict[str, object]) -> None:
        _atomic_write(self.path, _json(record))


class Bootstrapper:
    """Run only unfinished steps.  A crash re-enters the current idempotent step."""

    def __init__(self, journal: BootstrapJournal, actions: BootstrapActions) -> None:
        self.journal = journal
        self.actions = actions

    def run(self) -> BootstrapReport:
        record = self.journal.load_or_create()
        if record["status"] == "green":
            return _report_from_record(record)
        completed = set(record["completed"])
        for step in ORDERED_STEPS:
            if step.value in completed:
                continue
            try:
                receipt = self._execute(step)
            except BootstrapStepFailure as failure:
                self.journal.mark_failure(record, failure)
                return _report_from_record(record)
            except Exception as error:
                failure = BootstrapStepFailure(
                    step,
                    str(error),
                    "Inspect the named bootstrap step, correct it, then resume this same journal.",
                )
                self.journal.mark_failure(record, failure)
                return _report_from_record(record)
            # A process killed between _execute and this line keeps the step absent;
            # retrying calls only that action, whose contract is idempotent.
            self.journal.mark_complete(record, step, receipt)
            completed.add(step.value)
        self.journal.mark_green(record)
        return _report_from_record(record)

    def _execute(self, step: BootstrapStep) -> dict[str, object]:
        if step is BootstrapStep.REPOSITORY:
            return self.actions.checkout_commit(self.journal.plan.repository_commit)
        if step is BootstrapStep.UV_ENVIRONMENT:
            return self.actions.sync_uv_environment(self.journal.plan.lockfile)
        if step is BootstrapStep.TRANSFER:
            return self.actions.resume_transfer()
        if step is BootstrapStep.CHAIR_CACHE:
            return self.actions.verify_chair_cache()
        if step is BootstrapStep.PREFLIGHT:
            return self.actions.run_preflight()
        raise AssertionError(f"unhandled bootstrap step {step}")  # pragma: no cover


class ChairCacheBootstrapAction:
    """Spec-02 registry adapter with one explicitly supplied same-pin repair attempt."""

    def __init__(
        self,
        registry: ChairRegistry,
        *,
        refetch_same_pin: Callable[[ChairIdentity], None] | None = None,
    ) -> None:
        self.registry = registry
        self.refetch_same_pin = refetch_same_pin

    def verify(self) -> dict[str, object]:
        receipts: list[dict[str, object]] = []
        for role, configured in sorted(self.registry.config.chairs.items()):
            if isinstance(configured, AbsentChair):
                receipts.append({"chair": role, "state": "absent", "reason": configured.reason})
                continue
            repaired = False
            try:
                snapshot = self.registry.ensure(configured)
            except Exception as initial_error:
                if not _is_cache_mismatch(initial_error) or self.refetch_same_pin is None:
                    raise BootstrapStepFailure(
                        BootstrapStep.CHAIR_CACHE,
                        f"chair {role} cache verification failed: {initial_error}",
                        "Repair the exact pinned cache; no alternate chair or revision is allowed.",
                    ) from initial_error
                repaired = True
                try:
                    self.refetch_same_pin(configured)
                    snapshot = self.registry.ensure(configured)
                except Exception as retry_error:
                    raise BootstrapStepFailure(
                        BootstrapStep.CHAIR_CACHE,
                        f"chair {role} differs from its pin after one re-fetch: {retry_error}",
                        "Inspect the named cache and manifest; do not retry indefinitely or substitute a pin.",
                    ) from retry_error
            receipts.append(
                {
                    "chair": role,
                    "state": "verified",
                    "manifest_digest": snapshot.manifest_digest,
                    "root": str(snapshot.root),
                    "repaired_once": repaired,
                }
            )
        return {"chairs": receipts}


class SubprocessBootstrapActions:
    """Production-effect adapter; its commands are explicit argv, never shell text.

    It has no credential loading behavior.  Any ephemeral Git/provider credential
    delivery is an external, separately reviewed runtime concern.
    """

    def __init__(
        self,
        *,
        repository: str | Path,
        transfer: Callable[[], dict[str, object]],
        cache: ChairCacheBootstrapAction,
        preflight: Callable[[], dict[str, object]],
        runner: Callable[[list[str], Path], subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self.repository = Path(repository)
        self.transfer = transfer
        self.cache = cache
        self.preflight = preflight
        self.runner = runner or self._run

    def checkout_commit(self, commit: str) -> dict[str, object]:
        self._command(["git", "fetch", "--no-tags", "origin", commit], BootstrapStep.REPOSITORY)
        self._command(["git", "checkout", "--detach", "--force", commit], BootstrapStep.REPOSITORY)
        result = self._command(["git", "rev-parse", "HEAD"], BootstrapStep.REPOSITORY)
        observed = result.stdout.strip()
        if observed != commit:
            raise BootstrapStepFailure(
                BootstrapStep.REPOSITORY,
                f"checked out {observed!r}, not pinned commit {commit!r}",
                "Repair repository access or commit pin; do not continue on a branch tip.",
            )
        return {"commit": observed}

    def sync_uv_environment(self, lockfile: Path) -> dict[str, object]:
        expected_lockfile = (self.repository / "uv.lock").resolve()
        supplied_lockfile = lockfile.resolve()
        if supplied_lockfile != expected_lockfile:
            raise BootstrapStepFailure(
                BootstrapStep.UV_ENVIRONMENT,
                f"supplied lockfile {supplied_lockfile} is not repository uv.lock {expected_lockfile}",
                "Use the exact checked-out repository uv.lock; no adjacent lockfile can attest this environment.",
            )
        if not supplied_lockfile.is_file():
            raise BootstrapStepFailure(
                BootstrapStep.UV_ENVIRONMENT,
                f"lockfile {supplied_lockfile} is missing",
                "Restore the pinned uv.lock before creating an environment.",
            )
        self._command(["uv", "sync", "--locked", "--frozen"], BootstrapStep.UV_ENVIRONMENT)
        return {
            "lockfile": str(supplied_lockfile),
            "sha256": hashlib.sha256(supplied_lockfile.read_bytes()).hexdigest(),
            "mode": "locked-frozen",
        }

    def resume_transfer(self) -> dict[str, object]:
        return self.transfer()

    def verify_chair_cache(self) -> dict[str, object]:
        return self.cache.verify()

    def run_preflight(self) -> dict[str, object]:
        result = self.preflight()
        if result.get("color") != "green":
            raise BootstrapStepFailure(
                BootstrapStep.PREFLIGHT,
                "preflight returned red",
                "Read the preflight issues and repair the named environment, chair, or smoke-read failure.",
            )
        return result

    def _command(self, argv: list[str], step: BootstrapStep) -> subprocess.CompletedProcess[str]:
        result = self.runner(argv, self.repository)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
            raise BootstrapStepFailure(
                step,
                f"command {argv[0]!r} failed: {detail}",
                "Repair the named bootstrap dependency and resume; no fallback checkout or unlocked environment is used.",
            )
        return result

    @staticmethod
    def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(argv, cwd=cwd, text=True, capture_output=True, check=False)


def _report_from_record(record: dict[str, object]) -> BootstrapReport:
    completed = tuple(BootstrapStep(item) for item in record["completed"])
    failure = record["failure"]
    if record["status"] == "green":
        return BootstrapReport("green", completed, dict(record["receipts"]))
    if isinstance(failure, dict):
        return BootstrapReport(
            "red",
            completed,
            dict(record["receipts"]),
            BootstrapStep(failure["step"]),
            failure["detail"],
            failure["remediation"],
        )
    return BootstrapReport("red", completed, dict(record["receipts"]), detail="bootstrap journal is incomplete")


def _stamp(value: datetime) -> str:
    return require_utc(value, "bootstrap timestamp").isoformat().replace("+00:00", "Z")


def _json(value: dict[str, object]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode()


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
