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
constructed here for the first time in the tracked tree, closing the
"constructed nowhere" half of deferral 04-8.  The other half stays open: it is
wired with ``refetch_same_pin=None`` (see the comment beside that call below),
so the at-most-one same-pin re-fetch itself still does not ship.

**What ``PREFLIGHT`` measures, and through what.**  The chair-cache half is
:class:`RegistryChairCacheVerifier`: ``ChairRegistry.ensure`` over the plan's
``--models-config``, the same registry the ``CHAIR_CACHE`` step verified with,
so a chair whose cache differs from its pin is red by that chair's name.  The
smoke half is the serving package's own production seam --
``assemble_serving_smoke_reader`` around ``ServingManager`` -- fed
``VisionSmokeCall`` with a witness this process drew from the CSPRNG and
rendered onto a golden page on the volume moments before the read, so the
value a chair must read back was never in a committed file or a prompt.  The
serving receipt, launch audit and evidence manifest each smoke publishes land
content-addressed beside the report (:class:`PodPreflightReceiptPublisher`),
because at bootstrap time there is no run tree yet for a ``StageContext`` to
own them.  Every effect behind that seam -- the GPU probe, the vLLM launcher,
the loopback transport, the package inspector, the Hugging Face fetcher --
has an injection point in :class:`PreflightSeams`, which is how
``test_bootstrap_main.py`` proves the wiring green against the serving fakes
without a card.

**One thing the wiring cannot make green, said here rather than discovered on a
billing card.**  Every vLLM row in ``config/serving_recipes_real.toml`` is
``preflight_state = "unproven"``, and ``ServingManager.start`` refuses an
unproven row by name before it launches anything; so the first real
``PREFLIGHT`` is red at ``smoke-read-failed`` for every real chair until a
reviewer stamps those rows proven, which the serving README says happens
*after* a real-silicon preflight.  It is not this file's to fix; it is named so
nobody reads a red first preflight as a wiring fault.  The stack itself is no
longer the obstacle it was: the recipe's pins were re-planned onto
``vllm 0.27.1`` / ``transformers 5.14.1``, which lock beside the project's
``huggingface_hub==1.26.0``, and ``bootstrap.py``'s ``uv sync`` now carries
``--group pod``.  That the wheels install and the weights load on real silicon
is still unproven; only a boot proves it.

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

**A refusal leaves a durable reason, not just a stderr line nobody can read
after the container is gone.**  Once ``--report-path`` has passed containment,
every later refusal best-effort writes its reason there before exiting
(GOVERNANCE 2 -- nothing is lost silently).  Two refusals necessarily precede
a usable report path and stay stderr-only residue: the credential-argv scan
(before argv is even parsed) and ``--report-path`` itself failing containment.

**A gated chair repository needs its Hugging Face token kept.**  The
environment scrub pops anything credential-shaped, including ``HF_TOKEN`` and
``HUGGING_FACE_HUB_TOKEN``, before the chair cache or model store ever fetch
anything.  A launch that pins any gated repository must pass
``--keep-env HF_TOKEN`` (and/or ``--keep-env HUGGING_FACE_HUB_TOKEN``), or the
fetch fails on a pod that is already billing.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import secrets
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping, MutableMapping, Sequence

from common.chairs.models import ChairIdentity, ServingReceipt
from common.chairs.receipts import receipt_record
from common.chairs.registry import (
    ChairRegistry,
    HuggingFaceFetcher,
    HuggingFaceMaterializationFetcher,
    SnapshotFetcher,
)
from common.contracts.canonical import canonical_bytes, digest_bytes
from operations.serving.assembly import ProfileProbe, assemble_serving_smoke_reader
from operations.serving.config import ServingConfigInputs, load_serving_recipes
from operations.serving.http import HttpTransport
from operations.serving.manager import PackageInspector, ReceiptPublication
from operations.serving.process import ProcessLauncher
from operations.serving.residency import FileResidencyLease
from operations.serving.smoke import (
    NvidiaSmiUtilization,
    VisionSmokeCall,
    fresh_page_witness,
    render_golden_page,
)

from .bootstrap import (
    BootstrapActions,
    BootstrapJournal,
    Bootstrapper,
    BootstrapPlan,
    BootstrapReport,
    BootstrapStep,
    BootstrapStepFailure,
    ChairCacheBootstrapAction,
    ModelStoreBootstrapAction,
    SubprocessBootstrapActions,
)
from .durable import atomic_write, canonical_json, exclusive_write
from .models import (
    CREDENTIAL_VALUE_PREFIXES,
    looks_like_credential_field,
    looks_like_credential_value,
    require_utc,
    utc_now,
)
from .preflight import (
    PlacementRefusal,
    PreflightRunner,
    SystemGpuProbe,
    UtilizationSample,
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
    "page_witness_file",
    "serving_recipes_config",
    "submission_manifest",
    "transfer_source_root",
    "transfer_prefix",
    "model_store_capacity_json",
    "transfer_target_factory",
)
"""Arguments that name a bootstrap step's inputs.  ``--hold-only`` refuses if
any of these is supplied, because a drill runs no bootstrap step at all."""

PREFLIGHT_DIRECTORY = "preflight"
"""Under the volume: the golden page, the serving logs and the published smoke
receipts of one preflight, each in a directory named after the report's stem so
a second launch on the same retained volume (a different launch token, so a
different stem) never writes over the first launch's evidence."""


class PlanRefusal(ValueError):
    """A named, pre-execution refusal; nothing has been fetched, cloned, or held.

    ``report_path`` is set only when the refusal is raised after ``--report-path``
    itself has passed containment, so ``main`` can best-effort leave the reason
    durable on the volume even though ``resolve_plan`` never got to return a
    ``Plan``.
    """

    def __init__(self, message: str, *, report_path: Path | None = None) -> None:
        super().__init__(message)
        self.report_path = report_path


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
    page_witness_file: Path | None = None
    serving_recipes_config: Path | None = None
    submission_manifest: Path | None = None
    transfer_source_root: Path | None = None
    transfer_prefix: str = "pod-transfer"
    model_store_capacity: dict[str, object] = field(default_factory=dict)
    transfer_target_factory: str | None = None

    @property
    def preflight_root(self) -> Path:
        """Where this launch's preflight leaves its golden page, logs and receipts."""

        return self.volume_mount_path / PREFLIGHT_DIRECTORY / self.report_path.stem

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
            "page_witness_file": str(self.page_witness_file) if self.page_witness_file else None,
            "serving_recipes_config": str(self.serving_recipes_config)
            if self.serving_recipes_config
            else None,
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


class RegistryChairCacheVerifier:
    """The production ``ChairCacheVerifier``: one ``ensure`` per configured chair.

    ``ChairRegistry.ensure`` verifies the exact pinned snapshot in the cache
    the ``CHAIR_CACHE`` step already filled -- every row of the pinned
    manifest against the bytes on the volume -- and returns the verified
    snapshot, or raises the chair's own named refusal.  ``refetch_once`` is
    the same honest gap ``_build_cache`` records: the registry has no
    cache-clear verb, so a mismatch is reported once, by chair, and never
    repaired by a guess.  ``PreflightRunner`` turns that into
    ``cache-mismatch-after-refetch`` naming the chair.
    """

    def __init__(self, registry: ChairRegistry) -> None:
        self.registry = registry

    def verify(self, identity: ChairIdentity) -> dict[str, object]:
        snapshot = self.registry.ensure(identity)
        return {
            "chair": identity.role,
            "manifest_digest": snapshot.manifest_digest,
            "root": str(snapshot.root),
        }

    def refetch_once(self, identity: ChairIdentity) -> None:
        raise RuntimeError(
            f"chair {identity.role} cache differs from its pin and ChairRegistry has no "
            "cache-clear verb to stage one same-pin re-fetch; repair the named cache by hand"
        )


@dataclass(frozen=True, slots=True)
class _PreflightContext:
    """The two facts ``assemble_serving_smoke_reader`` reads off a ``StageContext``.

    There is no run tree at bootstrap time, so there is no ``StageContext``;
    the assembly seam only needs the sealed configuration digests and the
    registry, and refuses a publisher that does not belong to the same object.
    """

    serving_config_inputs: dict[str, str]
    registry: ChairRegistry


class PodPreflightReceiptPublisher:
    """Content-addressed serving evidence on the volume, for a preflight with no run.

    ``StageContextReceiptPublisher`` writes the receipt, the launch audit and
    the evidence manifest into a run tree.  A preflight runs before any run
    exists, so the same three records go under the launch's own preflight
    directory instead, each named by the SHA-256 of its canonical bytes, and
    the returned references are relative to that directory.  Same bytes twice
    is a no-op; different bytes at one address is a refusal, so a repeated
    preflight on a retained volume can add evidence but never replace it.
    """

    def __init__(self, root: Path, context: _PreflightContext) -> None:
        self.root = root
        self.context = context

    def publish(
        self, receipt: ServingReceipt, launch_audit: Mapping[str, object]
    ) -> ReceiptPublication:
        record = receipt_record(receipt)
        receipt_reference = self._write("receipts", record)
        audit_reference = self._write("launch-audits", dict(launch_audit))
        evidence_reference = self._write(
            "serving-evidence",
            {
                "schema": "serving-evidence.v1",
                "receipt_reference": receipt_reference,
                "launch_audit_reference": audit_reference,
            },
        )
        return ReceiptPublication(receipt_reference, audit_reference, evidence_reference)

    def _write(self, kind: str, value: Mapping[str, object]) -> dict[str, str]:
        data = canonical_bytes(value)
        digest = digest_bytes(data)
        relative_path = f"{kind}/sha256/{digest}.json"
        target = self.root / relative_path
        try:
            exclusive_write(target, data, strict=True)
        except FileExistsError:
            if target.read_bytes() != data:
                raise RuntimeError(
                    f"preflight {kind} evidence at {target} exists with different bytes; "
                    "evidence is not overwritten"
                ) from None
        return {"relative_path": relative_path, "sha256": digest}


@dataclass(frozen=True, slots=True)
class PreflightSeams:
    """Every effect behind ``PREFLIGHT``, with the production choice as each default.

    ``build_actions`` never passes one; ``_build_preflight(plan, seams)`` is
    how a test proves the wiring green against the serving package's fakes.
    A ``None`` launcher, transport or inspector means the serving assembly's
    own production default (a subprocess, urllib, ``importlib.metadata``).
    """

    page_witness: Callable[[], str] = fresh_page_witness
    utilization: Callable[[], tuple[UtilizationSample, ...]] | None = None
    gpu_probe: ProfileProbe | None = None
    launcher: ProcessLauncher | None = None
    http: HttpTransport | None = None
    package_inspector: PackageInspector | None = None
    fetcher_factory: Callable[[], SnapshotFetcher] = HuggingFaceFetcher.from_huggingface_hub
    # The one lock every serving manager on this card must share; on
    # container-local disk, because an advisory lock on a network volume is
    # not something the mount is known to honour.
    residency_lock: Path = Path("/tmp/verbatus-pod-gpu.lock")


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
        help="an operator-rendered golden page carrying the witness named by "
        "--page-witness-file; omit both and the pod renders its own page with a fresh "
        "CSPRNG witness under <volume-mount-path>/preflight/",
    )
    parser.add_argument(
        "--page-witness-file",
        type=Path,
        help="a file on the volume whose single line is the witness rendered on --fixture; "
        "required with --fixture and refused without it",
    )
    parser.add_argument(
        "--serving-recipes-config",
        type=Path,
        help="the serving-profile catalogue preflight smokes against; defaults to "
        "<repository>/config/serving_recipes.toml, the fixture-only catalogue, and may be "
        "defaulted only when --models-config is the shipped fixture roster "
        "config/models.toml -- any other roster must name its catalogue explicitly "
        "(config/serving_recipes_real.toml for the real roster) or the plan is refused",
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


def resolve_plan(args: argparse.Namespace, environment: Mapping[str, str] | None = None) -> Plan:
    """Validate every argument and combination before anything runs or holds.

    ``environment`` is read only for ``VERBATUS_LAUNCH_TOKEN`` -- the launch
    token binds ``--report-path`` (and, for a full plan, ``--journal``) to this
    launch, mirroring ``models._required_timer_arguments``'s guard against a
    second launch on the same retained volume silently overwriting the first
    launch's evidence (GOVERNANCE 4).  It must be read here, before ``main``
    scrubs the environment, because the token's own name is credential-shaped
    and would otherwise be popped before this ever ran.
    """

    volume_mount_path = Path(args.volume_mount_path)
    if not PurePosixPath(args.volume_mount_path).is_absolute():
        raise PlanRefusal("--volume-mount-path must be an absolute path")
    report_path = _require_contained(args.report_path, volume_mount_path, "--report-path")
    launch_token = (environment or {}).get("VERBATUS_LAUNCH_TOKEN") or None
    _require_launch_token_named(report_path, launch_token, "--report-path", report_path=report_path)

    plan_supplied = [
        name for name in _PLAN_ONLY_FLAGS if getattr(args, name, None) not in (None, [])
    ]
    if args.hold_only:
        if plan_supplied:
            raise PlanRefusal(
                "--hold-only refuses a plan argument: " + ", ".join(sorted(plan_supplied)),
                report_path=report_path,
            )
        return Plan(
            volume_mount_path=volume_mount_path,
            report_path=report_path,
            interval_seconds=_positive_interval(args.interval_seconds, report_path=report_path),
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
        raise PlanRefusal(
            "missing required plan argument(s): " + ", ".join(missing), report_path=report_path
        )

    repository = args.repository.resolve()
    lockfile = args.lockfile.resolve()
    expected_lockfile = (repository / "uv.lock").resolve()
    if lockfile != expected_lockfile:
        raise PlanRefusal(
            f"--lockfile {lockfile} is not the checked-out repository uv.lock {expected_lockfile}",
            report_path=report_path,
        )
    journal = _require_contained(
        args.journal, volume_mount_path, "--journal", report_path=report_path
    )
    _require_launch_token_named(journal, launch_token, "--journal", report_path=report_path)
    commit = args.repository_commit
    if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
        raise PlanRefusal(
            "--repository-commit must be a full lowercase Git SHA-1", report_path=report_path
        )

    try:
        capacity = json.loads(args.model_store_capacity_json or "{}")
    except json.JSONDecodeError as error:
        raise PlanRefusal(
            f"--model-store-capacity-json is not valid JSON: {error}", report_path=report_path
        ) from error
    if not isinstance(capacity, dict):
        raise PlanRefusal(
            "--model-store-capacity-json must decode to a JSON object", report_path=report_path
        )

    store_root = _require_contained(
        args.store_root, volume_mount_path, "--store-root", report_path=report_path
    )
    models_config = _require_contained(
        args.models_config,
        repository,
        "--models-config",
        base_label="the checked-out repository",
        report_path=report_path,
    )
    placement_config = _require_contained(
        args.placement_config,
        repository,
        "--placement-config",
        base_label="the checked-out repository",
        report_path=report_path,
    )

    cache_root = args.cache_root or (volume_mount_path / "chair-cache")
    if (args.fixture is None) != (args.page_witness_file is None):
        raise PlanRefusal(
            "--fixture and --page-witness-file name one golden page together: the page's "
            "pixels and the witness they carry; supply both or neither",
            report_path=report_path,
        )
    fixture = args.fixture
    page_witness_file = args.page_witness_file
    if page_witness_file is not None:
        page_witness_file = _require_contained(
            page_witness_file, volume_mount_path, "--page-witness-file", report_path=report_path
        )
    # The roster and the catalogue select one serving stack together -- which
    # chairs exist, and the vLLM profile each is served under -- so a plan that
    # names a roster other than the shipped fixture one and lets the catalogue
    # default would preflight the real chairs against the fixture-only
    # catalogue. `pipeline/orchestrator/run.py` says exactly that about its own
    # pair, and `operations/operator/surface._roster_argv` refuses the half-pair
    # outright. Here the mismatch is worse than a wrong answer: it is only
    # discovered after the pod has billed for the boot and the model fetch, so
    # it is refused at plan time, before anything is spent.
    default_roster = (repository / "config" / "models.toml").resolve()
    if args.serving_recipes_config is None and models_config != default_roster:
        raise PlanRefusal(
            f"--models-config {models_config} is not the shipped fixture roster "
            f"{default_roster}, and --serving-recipes-config was not supplied; the roster and "
            "the catalogue name one serving stack together, and defaulting the catalogue here "
            "would preflight this roster's chairs against the fixture-only catalogue. Name both",
            report_path=report_path,
        )
    serving_recipes_config = _require_contained(
        args.serving_recipes_config or (repository / "config" / "serving_recipes.toml"),
        repository,
        "--serving-recipes-config",
        base_label="the checked-out repository",
        report_path=report_path,
    )
    submission_manifest = args.submission_manifest or (
        volume_mount_path / "submission" / "manifest.json"
    )
    transfer_source_root = args.transfer_source_root or volume_mount_path

    return Plan(
        volume_mount_path=volume_mount_path,
        report_path=report_path,
        interval_seconds=_positive_interval(args.interval_seconds, report_path=report_path),
        keep_env=tuple(args.keep_env),
        dry_run=args.dry_run,
        hold_only=False,
        repository=repository,
        repository_commit=commit,
        lockfile=lockfile,
        journal=journal,
        store_root=store_root,
        models_config=models_config,
        placement_config=placement_config,
        cache_root=cache_root,
        fixture=fixture,
        page_witness_file=page_witness_file,
        serving_recipes_config=serving_recipes_config,
        submission_manifest=submission_manifest,
        transfer_source_root=transfer_source_root,
        transfer_prefix=args.transfer_prefix or "pod-transfer",
        model_store_capacity=capacity,
        transfer_target_factory=args.transfer_target_factory,
    )


def _positive_interval(value: float, *, report_path: Path | None = None) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise PlanRefusal(
            "--interval-seconds must be a positive finite number", report_path=report_path
        )
    return float(value)


def _require_contained(
    path: Path,
    base_path: Path,
    flag: str,
    *,
    base_label: str | None = None,
    report_path: Path | None = None,
) -> Path:
    """Refuse a path that escapes ``base_path`` -- a symlink or ``..`` included.

    ``base_label`` names the base in the refusal for a human; it defaults to
    "the mounted volume" so every existing volume-relative call keeps its
    established wording unchanged. ``report_path`` is passed through to the
    refusal wherever it is already known, so a durable reason can be left on
    the volume even for this refusal (see ``PlanRefusal.report_path``).

    The lexical check below refuses what ``argv`` literally says. That alone
    is not enough inside the pod: a symlinked directory component (or a
    symlinked leaf) can point a path that reads as contained at a location
    that is not, letting evidence land on container-local disk instead of the
    retained volume with the run still reporting success. So this also
    resolves both the candidate and the base and refuses again on where the
    path actually points, returning the resolved path so later writes go
    where the refusal was checked. ``Path.resolve()`` is non-strict, so this
    still works for a report or journal file that does not exist yet.
    """

    label = base_label if base_label is not None else "the mounted volume"
    raw = str(path)
    posix_path = PurePosixPath(raw)
    posix_base = PurePosixPath(str(base_path))
    resolved = path.resolve()
    resolved_base = base_path.resolve()
    if (
        ".." in raw.split("/")
        or not posix_path.is_absolute()
        or posix_path == posix_base
        or not posix_path.is_relative_to(posix_base)
        or resolved == resolved_base
        or not resolved.is_relative_to(resolved_base)
    ):
        raise PlanRefusal(
            f"{flag} {raw!r} must be inside {label} {base_path}", report_path=report_path
        )
    return resolved


def _require_launch_token_named(
    path: Path, launch_token: str | None, flag: str, *, report_path: Path
) -> None:
    """Mirror ``models._required_timer_arguments``'s guard, on the bootstrap side.

    A volume is retained across pods by design (GOVERNANCE 4): an unbound
    ``--report-path`` or ``--journal`` would let a second launch's evidence on
    the same volume silently replace the first's.  A launch with no token set
    gets no protection here, same as the pod-timer launch path when
    ``VERBATUS_LAUNCH_TOKEN`` is absent from its metadata.
    """

    if launch_token and launch_token not in path.name:
        raise PlanRefusal(
            f"{flag} must include this launch's token, "
            "so a second launch on the same volume cannot overwrite its evidence",
            report_path=report_path,
        )


def _credential_shape(value: str) -> str:
    """Say what made the value look like a secret, without repeating any of it."""

    if looks_like_credential_field(value):
        return "it reads as a secret's own name"
    if value.startswith(CREDENTIAL_VALUE_PREFIXES):
        return "a known provider key prefix"
    return f"an opaque run of {len(value)} mixed alphanumeric characters"


def refuse_credential_looking_argv(argv: Sequence[str]) -> None:
    """Refuse before parsing spends a look at any value that reads like a secret.

    Two independent checks: ``looks_like_credential_field`` asks whether the
    *name* implied by the value looks like a secret's name (a marker word);
    ``models.looks_like_credential_value`` asks whether the value's own *shape* looks
    like an opaque token, regardless of what it is named. Neither is a proof --
    a value can be a real secret without either marker, and this refusal cannot
    see into ``--transfer-target-factory``'s runtime capability at all.

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
        if value and (looks_like_credential_field(value) or looks_like_credential_value(value)):
            # The value itself is deliberately not repeated. This refusal is
            # printed to the pod's own transcript and `pod_run` prints it to
            # stderr as well, and `refuse` can write it into a report on the
            # retained volume -- so echoing a value that was refused *because it
            # looks like a credential* would put the suspected secret in three
            # more places. The flag and the shape are what an operator needs.
            where = f"the value after {flag}" if flag else "a bare argv value"
            raise PlanRefusal(
                f"{where} looks like a credential and was refused "
                f"({_credential_shape(value)}); the value is not repeated here, because this "
                "refusal reaches the transcript and the volume"
            )


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
    """Probe the volume with a real write, not a stat -- a mount can exist and refuse writes.

    Refuses first, without writing anything, if ``volume_mount_path`` is not
    already a directory -- ``atomic_write`` creates its target's parent
    directories, so routing the probe through it would let an *unmounted*
    volume pass by creating the very mount point the probe exists to require.
    The marker write and read-back bypass ``atomic_write``/``exclusive_write``
    for the same reason: neither may create ``volume_mount_path`` itself.
    """

    if not volume_mount_path.is_dir():
        raise PlanRefusal(
            f"volume write probe failed at {volume_mount_path}: not a mounted directory"
        )
    marker = volume_mount_path / f".bootstrap-write-probe-{os.getpid()}-{secrets.token_hex(4)}"
    payload = b"bootstrap write probe\n"
    try:
        descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError as error:
        raise PlanRefusal(f"volume write probe failed at {volume_mount_path}: {error}") from error
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        observed = marker.read_bytes()
        if observed != payload:
            raise PlanRefusal(
                f"volume write probe failed at {volume_mount_path}: read-back did not match"
            )
    except OSError as error:
        raise PlanRefusal(f"volume write probe failed at {volume_mount_path}: {error}") from error
    finally:
        try:
            marker.unlink(missing_ok=True)
        except OSError:
            pass


def _build_transfer(plan: Plan) -> Callable[[], dict[str, object]]:
    manifest = plan.submission_manifest
    # Not an assert: `assert` disappears under `python -O`, and the TRANSFER
    # step would then journal a bare AttributeError instead of the missing
    # manifest. The invariant is held by resolve_plan's default assignment
    # (bootstrap_main.py, `submission_manifest = args.submission_manifest or
    # ...`), not by any raise, so it needs one here.
    if manifest is None:
        raise PlanRefusal(
            "bootstrap plan reached TRANSFER with no submission manifest; "
            "--submission-manifest resolves for every non-hold-only plan"
        )

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


PREFLIGHT_DTYPE = "bfloat16"
"""The dtype preflight measures the card for.  Every vLLM row in both shipped
catalogues is ``dtype = "bfloat16"`` and ``ServingSmokeReader`` refuses a
profile whose dtype is not exactly the measured one, so ``float16`` here --
what this file used to pass -- made every real smoke red before it launched."""


def _golden_page(plan: Plan, seams: PreflightSeams) -> tuple[Path, str, bytes]:
    """The page the smoke sends and the witness it must read back, as one triple.

    The bytes are returned alongside the path so a caller that needs a
    digest before any chair has smoked the page (the ``no-chair-verified``
    fallback in ``_build_preflight``) reads it once, here, rather than
    taking a later, separate read of a file a live run's volume could have
    changed underneath it in the meantime.
    """

    if plan.fixture is not None:
        witness_file = plan.page_witness_file
        if witness_file is None:  # resolve_plan pairs them; stated for a hand-built Plan
            raise BootstrapStepFailure(
                BootstrapStep.PREFLIGHT,
                "a supplied --fixture has no --page-witness-file naming its witness",
                "Supply both, or neither so the pod renders its own golden page.",
            )
        try:
            witness = witness_file.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as error:
            raise BootstrapStepFailure(
                BootstrapStep.PREFLIGHT,
                f"the page witness file {witness_file} could not be read: {error}",
                "Restore the witness file beside the golden page it names, then resume.",
            ) from error
        try:
            page_bytes = plan.fixture.read_bytes()
        except OSError as error:
            raise BootstrapStepFailure(
                BootstrapStep.PREFLIGHT,
                f"the supplied golden page {plan.fixture} is missing",
                "Restore the named golden page, then resume this journal.",
            ) from error
        return plan.fixture, witness, page_bytes
    witness = seams.page_witness()
    # Named for the witness it carries, not `golden-page.png`: a second
    # PREFLIGHT under the same launch -- a resumed journal, a restarted
    # container -- draws a fresh CSPRNG witness, and a fixed name would put
    # those pixels over the page the first preflight's receipts already name by
    # digest. Evidence is added, never replaced (GOVERNANCE 4), exactly as
    # `PodPreflightReceiptPublisher` does for the three records beside it. The
    # witness is URL-safe by construction (`secrets.token_urlsafe`), so it is a
    # filename as it stands.
    page = plan.preflight_root / "golden-page" / f"{witness}.png"
    page_bytes = render_golden_page(page, witness)
    return page, witness, page_bytes


def _golden_page_digest(
    smoke_receipts: tuple[dict[str, object], ...], page_bytes_at_render: bytes
) -> str:
    """The digest of the bytes a chair actually read, not a later re-read of the file.

    Every smoke receipt's ``supplied_fixture_sha256`` is the digest of the
    exact bytes that chair was sent -- ``ServingManager`` re-digests the
    payload at request time and refuses if it does not match what
    ``preflight`` sealed. Taking the digest from there, instead of reading
    the page again after every chair has already read it, closes the window
    where a swap on the volume between the last smoke and this read could
    seal a digest naming bytes no chair ever saw. When every receipt agrees,
    that shared digest is the record's own. More than one distinct digest
    means this single preflight smoked more than one golden page -- refused
    by name rather than reported green, since one preflight must prove one
    page. No smoke receipts at all means ``no-chair-verified`` has already
    put this report red; the digest taken at page-render time still names a
    page in that failure's detail.
    """

    digests = {receipt["supplied_fixture_sha256"] for receipt in smoke_receipts}
    if len(digests) > 1:
        raise BootstrapStepFailure(
            BootstrapStep.PREFLIGHT,
            "preflight's own smoke receipts disagree on the golden page's digest: "
            + ", ".join(sorted(str(digest) for digest in digests)),
            "One preflight run must smoke exactly one golden page. A page that changed "
            "mid-run is refused rather than reported green.",
        )
    if digests:
        return str(next(iter(digests)))
    return digest_bytes(page_bytes_at_render)


def _build_preflight(
    plan: Plan, seams: PreflightSeams | None = None
) -> Callable[[], dict[str, object]]:
    """The real ``PREFLIGHT``: registry-backed cache verification and a served smoke.

    Everything below is deferred into ``_run`` for the same reason
    ``_LazyChairCache`` exists: ``build_actions`` runs before the REPOSITORY
    step has checked out the pinned commit, so nothing may read a config file
    at construction. ``_run`` reads the models roster, the serving catalogue
    and the placement table once each, digests the two the serving assembly
    seals, measures the card, renders the golden page, and only then hands
    ``PreflightRunner`` the verifier and the reader.
    """

    chosen = seams or PreflightSeams()

    def _run() -> dict[str, object]:
        models_config = plan.models_config
        placement_config = plan.placement_config
        recipes_config = plan.serving_recipes_config
        if models_config is None or placement_config is None or recipes_config is None:
            raise PlanRefusal(
                "bootstrap plan reached PREFLIGHT without its models, placement, or serving "
                "recipes configuration; resolve_plan fills all three for every full plan"
            )
        registry = ChairRegistry.from_toml(
            models_config, cache_root=plan.cache_root, fetcher=chosen.fetcher_factory()
        )
        # One read each, digested from the bytes that are parsed: the serving
        # assembly re-reads both files and refuses if what it parses does not
        # digest to what is sealed here, so a substitution between the two
        # reads is a named refusal rather than a table the run never sealed.
        recipes = load_serving_recipes(recipes_config)
        try:
            placement_bytes = placement_config.read_bytes()
            placement = load_placement_table(placement_config, source_bytes=placement_bytes)
        except (OSError, PlacementRefusal) as error:
            raise BootstrapStepFailure(
                BootstrapStep.PREFLIGHT,
                f"placement table {placement_config} could not be read: {error}",
                "Restore the reviewed placement table at the pinned commit, then resume.",
            ) from error
        if recipes.source_sha256 is None:  # load_serving_recipes always digests; stated
            raise BootstrapStepFailure(
                BootstrapStep.PREFLIGHT,
                f"serving catalogue {recipes_config} was loaded without a source digest",
                "Load the catalogue from its file so its bytes can be sealed.",
            )
        config_inputs = ServingConfigInputs(recipes.source_sha256, digest_bytes(placement_bytes))
        context = _PreflightContext(config_inputs.to_record(), registry)
        preflight_root = plan.preflight_root
        publisher = PodPreflightReceiptPublisher(preflight_root, context)
        probe = chosen.gpu_probe or SystemGpuProbe(disk_path=plan.volume_mount_path)
        profile = probe.profile(PREFLIGHT_DTYPE)
        fixture, witness, page_bytes_at_render = _golden_page(plan, chosen)
        smoke_call = VisionSmokeCall(
            witness, utilization=chosen.utilization or NvidiaSmiUtilization()
        )
        reader = assemble_serving_smoke_reader(
            registry=registry,
            stage_context=context,
            receipt_publisher=publisher,
            smoke_call=smoke_call,
            gpu_profile=profile,
            log_root=preflight_root / "serving-logs",
            recipes_path=recipes_config,
            placement_path=placement_config,
            launcher=chosen.launcher,
            http=chosen.http,
            package_inspector=chosen.package_inspector,
            residency_lease=FileResidencyLease(chosen.residency_lock),
            producer="operations.pod.bootstrap_main",
        )
        runner = PreflightRunner(
            registry.config,
            placement,
            RegistryChairCacheVerifier(registry),
            reader,
            fixture,
        )
        report = runner.run(profile)
        record = report.to_record()
        record["serving_config_inputs"] = config_inputs.to_record()
        record["preflight_root"] = str(preflight_root)
        try:
            record["golden_page_sha256"] = _golden_page_digest(
                report.smoke_receipts, page_bytes_at_render
            )
        except BootstrapStepFailure as digest_failure:
            # A digest disagreement can coincide with a genuinely red report --
            # embed the same full record the red path below reports, so this
            # refusal adds evidence rather than replacing the richer one.
            raise BootstrapStepFailure(
                digest_failure.step,
                digest_failure.detail
                + " -- record: "
                + json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True),
                digest_failure.remediation,
            ) from digest_failure
        if report.color != "green":
            raise BootstrapStepFailure(
                BootstrapStep.PREFLIGHT,
                "preflight returned red: "
                + json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True),
                "Read the issues by chair: an unproven serving row refuses launch until a "
                "reviewer stamps it proven, a cache mismatch names its chair, and an empty "
                "utilization sample means nvidia-smi could not be read. Do not substitute a "
                "fixture pass.",
            )
        return record

    return _run


class _LazyChairCache:
    """Defer ``_build_cache`` until the CHAIR_CACHE step actually verifies.

    ``build_actions`` runs before ``Bootstrapper.run`` -- before REPOSITORY has
    checked out ``--repository-commit`` and before UV_ENVIRONMENT has synced
    the lockfile.  ``_build_cache`` eagerly reads ``--models-config`` off disk
    and constructs the production Hugging Face fetcher; built eagerly, a
    CHAIR_CACHE receipt would attest to whatever ``models.toml`` happened to be
    on disk at container start, not to the commit the journal names
    (GOVERNANCE 6).  The transfer and model-store actions are already lazy this
    way (``materialize_model_store=lambda: ...``); this closes the one that
    was not.
    """

    def __init__(self, plan: Plan) -> None:
        self._plan = plan

    def verify(self) -> dict[str, object]:
        return _build_cache(self._plan).verify()


def build_actions(plan: Plan) -> BootstrapActions:
    """The real, tracked composition. Tests inject a fake instead of calling this."""

    return SubprocessBootstrapActions(
        repository=plan.repository,  # type: ignore[arg-type]
        transfer=_build_transfer(plan),
        materialize_model_store=lambda: _build_model_store(plan).materialize(),
        cache=_LazyChairCache(plan),  # type: ignore[arg-type]
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


REFUSAL_SCHEMA = "pod-bootstrap-refusal.v1"


def _write_refusal_report(
    report_path: Path | None, reason: str, *, now: Callable[[], datetime]
) -> str | None:
    """Best-effort: leave the refusal reason durable on the volume before exit.

    Without this, a refusal is a stderr line that dies with the container --
    unreachable from the laptop once the pod is destroyed (GOVERNANCE 2). Best
    effort because the volume that would hold this report may itself be the
    thing that just failed (an unwritable mount); a failed write here must not
    mask or replace the refusal already printed and returned.

    Never creates ``report_path``'s parent directory: ``atomic_write`` does,
    and calling it unconditionally here would let exactly the write-probe
    refusal this exists to record silently create the unmounted volume the
    probe just proved was not there.

    Returns ``None`` when the reason was written, and also when there was
    legitimately nowhere yet to write it (``report_path`` is ``None``); any
    other case returns a description of the failure, so the caller (``refuse``)
    can name it rather than letting the durable record's own absence go
    unmentioned -- GOVERNANCE 2 binds this failure too, not only the refusal
    it was trying to record.
    """

    if report_path is None:
        return None
    if not report_path.parent.is_dir():
        return f"{report_path.parent} does not exist"
    try:
        atomic_write(
            report_path,
            canonical_json(
                {
                    "schema": REFUSAL_SCHEMA,
                    "reason": reason,
                    "at": now().isoformat().replace("+00:00", "Z"),
                }
            ),
        )
    except OSError as error:
        return str(error)
    return None


EXIT_REFUSED = 2
EXIT_BOOTSTRAP_RED = 3


def prepare(
    raw_argv: Sequence[str],
    environment: MutableMapping[str, str],
    *,
    now: Callable[[], datetime],
) -> tuple[Plan, datetime]:
    """Everything before any action: argv, plan, write probe, scrub, hard deadline.

    Split out of ``main`` so ``pod_run`` runs the identical preparation over
    the bootstrap half of its argv. A ``PlanRefusal`` propagates; the caller
    prints it and leaves the durable reason (``refuse``), because which report
    path the reason belongs on is the caller's fact.
    """

    argv = list(raw_argv)
    refuse_credential_looking_argv(argv)
    args = build_parser().parse_args(argv)
    plan = resolve_plan(args, environment)
    try:
        write_probe(plan.volume_mount_path)
        scrubbed = scrub_environment(environment, keep=plan.keep_env)
        environment.clear()
        environment.update(scrubbed)
        return plan, _hard_deadline(environment)
    except PlanRefusal as refusal:
        # The plan exists, so its report path is where the reason belongs --
        # the same durable reason `main` has always left for these refusals.
        if refusal.report_path is None:
            refusal.report_path = plan.report_path
        raise


def refuse(
    refusal: PlanRefusal, *, plan: Plan | None, now: Callable[[], datetime], label: str
) -> int:
    """Print a refusal, leave its reason on the volume where that is possible, exit 2."""

    print(f"{label} refused: {refusal}", file=sys.stderr)
    report_path = plan.report_path if plan is not None else refusal.report_path
    failure = _write_refusal_report(report_path, str(refusal), now=now)
    if failure is not None:
        print(f"{label} refusal report could not be written: {failure}", file=sys.stderr)
    return EXIT_REFUSED


def run_bootstrap(
    plan: Plan,
    *,
    now: Callable[[], datetime],
    actions_factory: Callable[[Plan], BootstrapActions],
) -> BootstrapReport | int:
    """Run the journaled steps and return the report, or the exit code of a refusal.

    A red step is a returned red report -- the caller decides its exit -- and
    an action factory that cannot be built is ``EXIT_REFUSED`` with the reason
    left on the volume, exactly as ``main`` has always done.
    """

    journal = BootstrapJournal(
        plan.journal,  # type: ignore[arg-type]
        BootstrapPlan(plan.repository_commit, plan.lockfile),  # type: ignore[arg-type]
        now=now,
    )
    try:
        actions = actions_factory(plan)
    except Exception as error:
        print(f"bootstrap_main could not build its actions: {error}", file=sys.stderr)
        _write_refusal_report(plan.report_path, f"could not build actions: {error}", now=now)
        return EXIT_REFUSED
    report = Bootstrapper(journal, actions).run()
    if not report.green:
        print(f"bootstrap step {report.failure_step}: {report.detail}", file=sys.stderr)
    return report


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
    try:
        plan, hard_deadline = prepare(raw_argv, environment, now=now)
    except PlanRefusal as refusal:
        return refuse(refusal, plan=None, now=now, label="bootstrap_main")

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

    report = run_bootstrap(plan, now=now, actions_factory=actions_factory)
    if isinstance(report, int):
        return report
    if not report.green:
        return EXIT_BOOTSTRAP_RED

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
