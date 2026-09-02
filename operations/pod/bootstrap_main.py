"""``python -m operations.pod.bootstrap_main`` -- bootstrap-and-hold, a service not a script.

Steps run through :class:`~operations.pod.bootstrap.Bootstrapper`.  On green this
process does **not** exit: it holds until the pod is destroyed, re-journaling a
liveness line at the monitoring interval.  Exiting after a green bootstrap is
exactly what ``pod_timer.run_with_bootstrap`` calls ``completed-early``
(``pod_timer.py:150-158``) and punishes with an immediate close -- see that
function before changing the hold loop here.  A red bootstrap step exits
non-zero at once, which is the correct immediate close for pod_timer to act on.

Composition is deliberately **tracked**: every pinned input this process needs
is an explicit flag, never an inferred default, so a request file that built
this command names exactly what ran.  ``ChairCacheBootstrapAction`` is
constructed here for the first time in the tracked tree, closing deferral 04-8.

**What ``PREFLIGHT`` honestly cannot do yet.**  No production
``ChairCacheVerifier`` or ``SmokeReader`` exists anywhere in this repository --
only fixture adapters in tests and the operator's offline surface.  Spec 05
owns the real chair-serving assembly those protocols measure.  Rather than
borrow a fixture pass into a live pod's preflight (a fabricated green,
GOVERNANCE 10 forbids exactly that), this module wires
:class:`_UnimplementedChairCacheVerifier` and :class:`_UnimplementedSmokeReader`,
which name the gap and make ``PREFLIGHT`` red until Spec 05 ships a real one.
A live pod cannot pass real preflight today, and this file does not pretend
otherwise.

**The transfer target stays optional.**  With no submission manifest on the
volume, transfer is a vacuous success and this process needs no object-store
client at all (``transfer.py:103-104``).  A manifest present with no configured
target is a refusal, never a silently skipped upload.

**The hard deadline governs the hold, not a flag.**  Both the ordinary
bootstrap-and-hold path and the ``--hold-only`` drill hold until
``VERBATUS_HARD_DEADLINE`` (the same environment spelling
``provider_runpod.timer_context_from_environment`` reads) is reached, so this
process's own exit approximately coincides with the pod-side timer closing the
pod at the same instant regardless.  A missing or unparseable deadline is a
startup refusal: holding with no bound cannot be tested and cannot be trusted.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Callable, MutableMapping, Sequence

from common.chairs.config import load_models_toml
from common.chairs.models import ChairIdentity
from common.chairs.registry import (
    ChairRegistry,
    HuggingFaceFetcher,
    HuggingFaceMaterializationFetcher,
)

from .bootstrap import (
    BootstrapActions,
    BootstrapJournal,
    Bootstrapper,
    BootstrapPlan,
    BootstrapStep,
    BootstrapStepFailure,
    ChairCacheBootstrapAction,
    ModelStoreBootstrapAction,
    SubprocessBootstrapActions,
)
from .durable import atomic_write, canonical_json
from .models import looks_like_credential_field, require_utc, utc_now
from .preflight import (
    PlacementTier,
    PreflightRunner,
    SystemGpuProbe,
    load_placement_table,
)
from .transfer import ChecksummedTransfer, TransferReport

HARD_DEADLINE_ENV = "VERBATUS_HARD_DEADLINE"
"""The same environment spelling the RunPod pod-timer factory reads.  Naming it
here does not make this file provider vocabulary -- the value is a Verbatus
launch fact, not a RunPod one."""

HOLD_SCHEMA = "pod-bootstrap-hold.v1"
DEFAULT_PROOF_FIXTURE = "synthetic-two-page-v0"
"""Matches ``operations/operator/surface.py``'s ``DEFAULT_FIXTURE``; duplicated
rather than imported to avoid a pod-side dependency on the operator layer."""

_PLAN_ONLY_FLAGS = (
    "repository",
    "repository_commit",
    "lockfile",
    "journal",
    "store_root",
    "models_config",
    "placement_config",
    "cache_root",
    "fixture",
    "submission_manifest",
    "transfer_source_root",
    "transfer_prefix",
    "model_store_capacity_json",
    "transfer_target_factory",
)
"""Arguments that name a bootstrap step's inputs.  ``--hold-only`` refuses if
any of these is supplied, because a drill runs no bootstrap step at all."""


class PlanRefusal(ValueError):
    """A named, pre-execution refusal; nothing has been fetched, cloned, or held."""


@dataclass(frozen=True, slots=True)
class Plan:
    """Every explicit, tracked input this process was given."""

    volume_mount_path: Path
    report_path: Path
    interval_seconds: float
    keep_env: tuple[str, ...]
    dry_run: bool
    hold_only: bool
    repository: Path | None = None
    repository_commit: str | None = None
    lockfile: Path | None = None
    journal: Path | None = None
    store_root: Path | None = None
    models_config: Path | None = None
    placement_config: Path | None = None
    cache_root: Path | None = None
    fixture: Path | None = None
    submission_manifest: Path | None = None
    transfer_source_root: Path | None = None
    transfer_prefix: str = "pod-transfer"
    model_store_capacity: dict[str, object] = field(default_factory=dict)
    transfer_target_factory: str | None = None

    def to_record(self) -> dict[str, object]:
        return {
            "volume_mount_path": str(self.volume_mount_path),
            "report_path": str(self.report_path),
            "interval_seconds": self.interval_seconds,
            "keep_env": list(self.keep_env),
            "dry_run": self.dry_run,
            "hold_only": self.hold_only,
            "repository": str(self.repository) if self.repository else None,
            "repository_commit": self.repository_commit,
            "lockfile": str(self.lockfile) if self.lockfile else None,
            "journal": str(self.journal) if self.journal else None,
            "store_root": str(self.store_root) if self.store_root else None,
            "models_config": str(self.models_config) if self.models_config else None,
            "placement_config": str(self.placement_config) if self.placement_config else None,
            "cache_root": str(self.cache_root) if self.cache_root else None,
            "fixture": str(self.fixture) if self.fixture else None,
            "submission_manifest": str(self.submission_manifest)
            if self.submission_manifest
            else None,
            "transfer_source_root": str(self.transfer_source_root)
            if self.transfer_source_root
            else None,
            "transfer_prefix": self.transfer_prefix,
            "model_store_capacity": self.model_store_capacity,
            "transfer_target_factory": self.transfer_target_factory,
        }


class _UnimplementedChairCacheVerifier:
    """Names the real gap instead of borrowing a fixture pass into a live preflight."""

    def verify(self, identity: ChairIdentity) -> dict[str, object]:
        raise RuntimeError(
            f"no production chair-cache verifier exists for preflight (chair {identity.role}); "
            "Spec 05 owns the real chair-serving assembly this measures"
        )

    def refetch_once(self, identity: ChairIdentity) -> None:
        raise RuntimeError(
            f"no production chair-cache repair path exists for preflight (chair {identity.role})"
        )


class _UnimplementedSmokeReader:
    """Reachable only if a future cache verifier starts returning ``verified``."""

    def read(self, identity: ChairIdentity, fixture: Path, placement: PlacementTier):  # type: ignore[no-untyped-def]
        raise RuntimeError(
            f"no production smoke reader exists for preflight (chair {identity.role}); "
            "Spec 05 owns the real chair-serving assembly this measures"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verbatus pod-side bootstrap-and-hold service",
        allow_abbrev=False,
    )
    parser.add_argument("--volume-mount-path", required=True)
    parser.add_argument("--report-path", type=Path, required=True)
    parser.add_argument("--interval-seconds", type=float, default=15.0)
    parser.add_argument(
        "--keep-env",
        action="append",
        default=[],
        metavar="NAME",
        help="exact environment variable name to keep despite the credential-shaped scrub",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--hold-only",
        action="store_true",
        help="named drill mode: no bootstrap steps, journal a hold-only record, hold to the "
        "deadline; refuses if any plan argument is supplied",
    )
    parser.add_argument("--repository", type=Path)
    parser.add_argument("--repository-commit")
    parser.add_argument("--lockfile", type=Path)
    parser.add_argument("--journal", type=Path)
    parser.add_argument("--store-root", type=Path)
    parser.add_argument("--models-config", type=Path)
    parser.add_argument("--placement-config", type=Path)
    parser.add_argument(
        "--cache-root",
        type=Path,
        help="defaults to <volume-mount-path>/chair-cache",
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        help="defaults to <repository>/proof/fixtures/%s/page-1.png" % DEFAULT_PROOF_FIXTURE,
    )
    parser.add_argument(
        "--submission-manifest",
        type=Path,
        help="defaults to <volume-mount-path>/submission/manifest.json",
    )
    parser.add_argument(
        "--transfer-source-root",
        type=Path,
        help="defaults to --volume-mount-path",
    )
    parser.add_argument("--transfer-prefix", default=None)
    parser.add_argument("--model-store-capacity-json", default=None)
    parser.add_argument(
        "--transfer-target-factory",
        help="untracked module:callable returning a TransferTarget; omit when no submission "
        "manifest is expected on this volume",
    )
    return parser


def _factory(reference: str) -> Callable[[], object]:
    if reference.count(":") != 1:
        raise PlanRefusal("a factory reference must be module:callable")
    module_name, name = reference.split(":", 1)
    import importlib

    return getattr(importlib.import_module(module_name), name)


def resolve_plan(args: argparse.Namespace) -> Plan:
    """Validate every argument and combination before anything runs or holds."""

    volume_mount_path = Path(args.volume_mount_path)
    if not PurePosixPath(args.volume_mount_path).is_absolute():
        raise PlanRefusal("--volume-mount-path must be an absolute path")
    report_path = _require_contained(args.report_path, volume_mount_path, "--report-path")

    plan_supplied = [
        name for name in _PLAN_ONLY_FLAGS if getattr(args, name, None) not in (None, [])
    ]
    if args.hold_only:
        if plan_supplied:
            raise PlanRefusal(
                "--hold-only refuses a plan argument: " + ", ".join(sorted(plan_supplied))
            )
        return Plan(
            volume_mount_path=volume_mount_path,
            report_path=report_path,
            interval_seconds=_positive_interval(args.interval_seconds),
            keep_env=tuple(args.keep_env),
            dry_run=args.dry_run,
            hold_only=True,
        )

    missing = [
        flag
        for flag, value in (
            ("--repository", args.repository),
            ("--repository-commit", args.repository_commit),
            ("--lockfile", args.lockfile),
            ("--journal", args.journal),
            ("--store-root", args.store_root),
            ("--models-config", args.models_config),
            ("--placement-config", args.placement_config),
        )
        if value is None
    ]
    if missing:
        raise PlanRefusal("missing required plan argument(s): " + ", ".join(missing))

    repository = args.repository.resolve()
    lockfile = args.lockfile.resolve()
    expected_lockfile = (repository / "uv.lock").resolve()
    if lockfile != expected_lockfile:
        raise PlanRefusal(
            f"--lockfile {lockfile} is not the checked-out repository uv.lock {expected_lockfile}"
        )
    journal = _require_contained(args.journal, volume_mount_path, "--journal")
    commit = args.repository_commit
    if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
        raise PlanRefusal("--repository-commit must be a full lowercase Git SHA-1")

    try:
        capacity = json.loads(args.model_store_capacity_json or "{}")
    except json.JSONDecodeError as error:
        raise PlanRefusal(f"--model-store-capacity-json is not valid JSON: {error}") from error
    if not isinstance(capacity, dict):
        raise PlanRefusal("--model-store-capacity-json must decode to a JSON object")

    cache_root = args.cache_root or (volume_mount_path / "chair-cache")
    fixture = args.fixture or (
        repository / "proof" / "fixtures" / DEFAULT_PROOF_FIXTURE / "page-1.png"
    )
    submission_manifest = args.submission_manifest or (
        volume_mount_path / "submission" / "manifest.json"
    )
    transfer_source_root = args.transfer_source_root or volume_mount_path

    return Plan(
        volume_mount_path=volume_mount_path,
        report_path=report_path,
        interval_seconds=_positive_interval(args.interval_seconds),
        keep_env=tuple(args.keep_env),
        dry_run=args.dry_run,
        hold_only=False,
        repository=repository,
        repository_commit=commit,
        lockfile=lockfile,
        journal=journal,
        store_root=args.store_root,
        models_config=args.models_config,
        placement_config=args.placement_config,
        cache_root=cache_root,
        fixture=fixture,
        submission_manifest=submission_manifest,
        transfer_source_root=transfer_source_root,
        transfer_prefix=args.transfer_prefix or "pod-transfer",
        model_store_capacity=capacity,
        transfer_target_factory=args.transfer_target_factory,
    )


def _positive_interval(value: float) -> float:
    if not isinstance(value, (int, float)) or value <= 0:
        raise PlanRefusal("--interval-seconds must be a positive number")
    return float(value)


def _require_contained(path: Path, volume_mount_path: Path, flag: str) -> Path:
    raw = str(path)
    posix_path = PurePosixPath(raw)
    posix_volume = PurePosixPath(str(volume_mount_path))
    if (
        ".." in raw.split("/")
        or not posix_path.is_absolute()
        or posix_path == posix_volume
        or not posix_path.is_relative_to(posix_volume)
    ):
        raise PlanRefusal(f"{flag} {raw!r} must be inside the mounted volume {volume_mount_path}")
    return path


def refuse_credential_looking_argv(argv: Sequence[str]) -> None:
    """Refuse before parsing spends a look at any value that reads like a secret.

    ``--keep-env`` values are environment variable *names* being retained, not
    discovered secrets -- ``HF_TOKEN`` is exactly the kind of name this flag
    exists to name, and it would otherwise refuse itself on sight.
    """

    previous = ""
    for token in argv:
        if token.startswith("--") and "=" in token:
            flag, _, value = token.partition("=")
        elif token.startswith("--"):
            previous = token
            continue
        else:
            flag, value = previous, token
        previous = ""
        if flag == "--keep-env":
            continue
        if value and looks_like_credential_field(value):
            raise PlanRefusal(f"argv value looks like a credential and was refused: {value!r}")


def scrub_environment(
    environment: MutableMapping[str, str], *, keep: Sequence[str]
) -> dict[str, str]:
    """Pop every credential-shaped name except an explicit ``--keep-env`` allowlist.

    The predicate does the work, not a literal vendor name -- the seam test
    forbids RunPod's own environment spellings in this file, and a predicate
    scrub never needs to name them to remove them.
    """

    keep_set = set(keep)
    return {
        name: value
        for name, value in environment.items()
        if name in keep_set or not looks_like_credential_field(name)
    }


def _hard_deadline(environment: MutableMapping[str, str]) -> datetime:
    raw = environment.get(HARD_DEADLINE_ENV)
    if not raw:
        raise PlanRefusal(f"{HARD_DEADLINE_ENV} is not set; holding needs a hard deadline")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as error:
        raise PlanRefusal(
            f"{HARD_DEADLINE_ENV} is not an RFC3339 UTC timestamp: {error}"
        ) from error
    try:
        return require_utc(parsed, HARD_DEADLINE_ENV)
    except ValueError as error:
        raise PlanRefusal(str(error)) from error


def write_probe(volume_mount_path: Path) -> None:
    """Probe the volume with a real write, not a stat -- a mount can exist and refuse writes."""

    marker = volume_mount_path / f".bootstrap-write-probe-{os.getpid()}-{secrets.token_hex(4)}"
    try:
        atomic_write(marker, b"bootstrap write probe\n")
    except Exception as error:
        raise PlanRefusal(f"volume write probe failed at {volume_mount_path}: {error}") from error
    finally:
        try:
            marker.unlink(missing_ok=True)
        except OSError:
            pass


def _build_transfer(plan: Plan) -> Callable[[], dict[str, object]]:
    manifest = plan.submission_manifest
    assert manifest is not None  # a plan (not hold-only) always resolves this

    def _transfer() -> dict[str, object]:
        if not manifest.is_file():
            return TransferReport((), (), submission_manifest_present=False).to_record()
        if plan.transfer_target_factory is None:
            raise BootstrapStepFailure(
                BootstrapStep.TRANSFER,
                f"submission manifest {manifest} is present but no transfer target was configured",
                "Supply --transfer-target-factory so the sealed submission rows can be sent, "
                "or confirm no manifest should exist on this volume.",
            )
        target = _factory(plan.transfer_target_factory)()
        if not callable(getattr(target, "inspect", None)) or not callable(
            getattr(target, "put_file", None)
        ):
            raise BootstrapStepFailure(
                BootstrapStep.TRANSFER,
                "transfer target factory did not return the inspect/put_file storage seam",
                "Point --transfer-target-factory at a callable returning a TransferTarget.",
            )
        return (
            ChecksummedTransfer(
                source_root=plan.transfer_source_root,
                submission_manifest=manifest,
                target=target,  # type: ignore[arg-type]
                prefix=plan.transfer_prefix,
                journal_path=plan.volume_mount_path / "pod-transfer-journal.json",
            )
            .resume()
            .to_record()
        )

    return _transfer


def _build_model_store(plan: Plan) -> ModelStoreBootstrapAction:
    return ModelStoreBootstrapAction(
        plan.store_root,  # type: ignore[arg-type]
        HuggingFaceMaterializationFetcher.from_huggingface_hub(),
        capacity=plan.model_store_capacity,
    )


def _build_cache(plan: Plan) -> ChairCacheBootstrapAction:
    registry = ChairRegistry.from_toml(
        plan.models_config,  # type: ignore[arg-type]
        cache_root=plan.cache_root,
        fetcher=HuggingFaceFetcher.from_huggingface_hub(),
    )
    # No same-pin repair is wired: `ChairRegistry` has no public "clear this
    # chair's cache" verb today, and inventing one to satisfy this optional
    # callback risks corrupting a cache silently rather than leaving a named,
    # single-attempt refusal for a human to repair. `ChairCacheBootstrapAction`
    # already treats `refetch_same_pin=None` as "one failed verification is
    # terminal", which is the honest behavior until that verb exists.
    return ChairCacheBootstrapAction(registry, refetch_same_pin=None)


def _build_preflight(plan: Plan) -> Callable[[], dict[str, object]]:
    def _run() -> dict[str, object]:
        models = load_models_toml(plan.models_config)  # type: ignore[arg-type]
        placement = load_placement_table(plan.placement_config)  # type: ignore[arg-type]
        profile = SystemGpuProbe(disk_path=plan.volume_mount_path).profile(dtype="float16")
        runner = PreflightRunner(
            models,
            placement,
            _UnimplementedChairCacheVerifier(),
            _UnimplementedSmokeReader(),
            plan.fixture,  # type: ignore[arg-type]
        )
        report = runner.run(profile)
        record = report.to_record()
        if report.color != "green":
            raise BootstrapStepFailure(
                BootstrapStep.PREFLIGHT,
                "preflight returned red: "
                + json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True),
                "No production chair-cache verifier or smoke reader exists yet (Spec 05); "
                "this preflight cannot go green until one does. Do not substitute a fixture pass.",
            )
        return record

    return _run


def build_actions(plan: Plan) -> BootstrapActions:
    """The real, tracked composition. Tests inject a fake instead of calling this."""

    return SubprocessBootstrapActions(
        repository=plan.repository,  # type: ignore[arg-type]
        transfer=_build_transfer(plan),
        materialize_model_store=lambda: _build_model_store(plan).materialize(),
        cache=_build_cache(plan),
        preflight=_build_preflight(plan),
    )


def hold(
    *,
    report_path: Path,
    hard_deadline: datetime,
    state: str,
    bootstrap: dict[str, object] | None,
    now: Callable[[], datetime],
    sleeper: Callable[[float], None],
    interval_seconds: float,
) -> dict[str, object]:
    """Re-journal a liveness line until the shared hard deadline is reached.

    The loop's own end coincides with the moment the pod-side timer (reading
    the same ``VERBATUS_HARD_DEADLINE``) begins its own close, so an ordinary
    return here is not "completed early" in any sense pod_timer would need to
    guard against -- the pod is being taken down regardless. It is what lets
    this be tested at all, since nothing here waits on the container dying.
    """

    tick = 0
    while True:
        current = now()
        record = {
            "schema": HOLD_SCHEMA,
            "state": state,
            "bootstrap": bootstrap,
            "tick": tick,
            "at": current.isoformat().replace("+00:00", "Z"),
            "hard_deadline": hard_deadline.isoformat().replace("+00:00", "Z"),
        }
        atomic_write(report_path, canonical_json(record))
        if current >= hard_deadline:
            return record
        remaining = (hard_deadline - current).total_seconds()
        sleeper(min(interval_seconds, max(0.01, remaining)))
        tick += 1


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: MutableMapping[str, str] | None = None,
    now: Callable[[], datetime] = utc_now,
    sleeper: Callable[[float], None] = time.sleep,
    actions_factory: Callable[[Plan], BootstrapActions] = build_actions,
) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    environment = os.environ if environ is None else environ
    parser = build_parser()
    try:
        refuse_credential_looking_argv(raw_argv)
        args = parser.parse_args(raw_argv)
        plan = resolve_plan(args)
        write_probe(plan.volume_mount_path)
        scrubbed = scrub_environment(environment, keep=plan.keep_env)
        environment.clear()
        environment.update(scrubbed)
        hard_deadline = _hard_deadline(environment)
    except PlanRefusal as refusal:
        print(f"bootstrap_main refused: {refusal}", file=sys.stderr)
        return 2

    if plan.dry_run:
        print(json.dumps(plan.to_record(), sort_keys=True, indent=2))
        return 0

    if plan.hold_only:
        hold(
            report_path=plan.report_path,
            hard_deadline=hard_deadline,
            state="hold-only",
            bootstrap=None,
            now=now,
            sleeper=sleeper,
            interval_seconds=plan.interval_seconds,
        )
        return 0

    journal = BootstrapJournal(
        plan.journal,  # type: ignore[arg-type]
        BootstrapPlan(plan.repository_commit, plan.lockfile),  # type: ignore[arg-type]
        now=now,
    )
    try:
        actions = actions_factory(plan)
    except Exception as error:
        print(f"bootstrap_main could not build its actions: {error}", file=sys.stderr)
        return 2
    report = Bootstrapper(journal, actions).run()
    if not report.green:
        print(
            f"bootstrap step {report.failure_step}: {report.detail}",
            file=sys.stderr,
        )
        return 3

    hold(
        report_path=plan.report_path,
        hard_deadline=hard_deadline,
        state="holding",
        bootstrap=report.to_record(),
        now=now,
        sleeper=sleeper,
        interval_seconds=plan.interval_seconds,
    )
    # In production the container is destroyed at or before this instant by
    # the pod-side timer sharing the same hard deadline; returning here only
    # matters to a drill or a test that outlives that destruction.
    return 0


if __name__ == "__main__":  # pragma: no cover - command wrapper
    raise SystemExit(main())
