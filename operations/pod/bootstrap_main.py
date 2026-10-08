"""``python -m operations.pod.bootstrap_main`` -- bootstrap-and-hold, a service not a script.

Steps run through :class:`~operations.pod.bootstrap.Bootstrapper`.  On green this
process does **not** exit: it holds until the pod is destroyed, re-journaling a
liveness line at the monitoring interval.  Exiting after a green bootstrap is
exactly what ``pod_timer.run_with_bootstrap`` calls ``completed-early`` and
punishes with an immediate close -- see that function before changing the
hold loop here.  A red bootstrap step exits non-zero at once, which is the
correct immediate close for pod_timer to act on.

Composition is deliberately **tracked**: every pinned input this process needs
is an explicit flag, except the placement table, which is always the checkout's
own ``config/pod_placement.toml`` because that is the table the stages seal.
``CHAIR_CACHE`` records the role source plan of each Hugging Face chair without
copying its bytes, and copies each local-repository chair's verified bundle
from the store to where the roster binds it.

**What ``PREFLIGHT`` measures, and through what.**  The chair-cache half is
:class:`RegistryChairCacheVerifier`: ``ChairRegistry.ensure`` over the plan's
``--models-config``, using the pinned volume store as each role is needed,
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
the loopback transport, the package inspector, the cache-source fetcher --
has an injection point in :class:`PreflightSeams`, which is how
``test_bootstrap_main.py`` proves the wiring green against the serving fakes
without a card.

**How the first real preflight closes its proof loop.**  Ordinary
``ServingManager.start`` still refuses every ``preflight_state = "unproven"``
row.  Only this smoke-preflight assembly receives the private qualification
purpose that permits such a row to launch while retaining every snapshot,
runtime, request, shutdown, and evidence check.  A green report can then be
verified offline by ``operations.serving.qualify`` to render the identity and
profile digests for review; the verifier edits no catalogue.  The stack pins
``vllm 0.30.0`` / ``transformers 5.14.1`` beside the project's
``huggingface_hub==1.31.0``, and ``bootstrap.py``'s ``uv sync`` carries
``--group pod``.  That the wheels install and the weights load on real
silicon is still unproven; only a boot proves it.

**TRANSFER's direction is named, not defaulted.**  A pod that is *consuming* a
submission already on its volume passes neither ``--submission-manifest`` nor
``--transfer-target-factory``, and TRANSFER is a vacuous success requiring no
object-store client at all (``TransferExecutor.resume`` returns
``nothing-to-transfer`` when the manifest is absent).  A pod that is producing
one passes both.  Half of that pair -- either half -- is refused at plan time,
before the ten-gigabyte environment sync, rather than as a red TRANSFER step
after it.  A manifest present with no configured target is still a refusal,
never a silently skipped upload.

**The image this process assumes is written down.**  ``operations/pod/README.md``
carries the pod image contract -- a checkout with an HTTPS ``origin`` that
the scrubbed git environment can fetch, a pre-built ``<repository>/.venv`` this
process is started from, and git/uv at the pinned absolute paths -- and
``bootstrap.verify_image_contract``, wired into REPOSITORY by ``build_actions``,
refuses by name when the image does not meet it.  It runs before ``git fetch``,
so an image mistake costs the boot and nothing downloaded.

**The hard deadline governs the hold, not a flag.**  Both the ordinary
bootstrap-and-hold path and the ``--hold-only`` drill hold until
``VERBATUS_HARD_DEADLINE`` (the same environment spelling
``provider_runpod.timer_context_from_environment`` reads) is reached, so this
process's own exit approximately coincides with the pod-side timer closing the
pod at the same instant regardless.  A missing or unparseable deadline is a
startup refusal: holding with no bound cannot be tested and cannot be trusted.
``VERBATUS_HARD_DEADLINE=none`` says there is no deadline, for a caller that will
not hold (``pod_run --no-hold`` on a pod whose budget is off); this module, which
always holds, refuses it.

**A refusal leaves a durable reason, not just a stderr line nobody can read
after the container is gone.**  Once ``--report-path`` has passed containment,
every later refusal, an unparseable argv included, best-effort writes its
reason there before exiting.  These
stay stderr-only: the credential-argv scan (before argv is parsed), a missing
or relative ``--volume-mount-path`` or missing ``--report-path``, and a
``--report-path`` that fails containment or lacks this launch's token (it may
name another launch's report, which must not be overwritten).

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
import re
import secrets
import shutil
import stat
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping, MutableMapping, NoReturn, Sequence

from common.chairs.config import parse_models_config
from common.chairs.errors import ChairRefusal
from common.chairs.manifests import CopyLedger, copy_pool, verify_snapshot
from common.chairs.model_store import (
    StoreRoleFetcher,
    artifacts_for_roles,
    pending_local_artifacts,
)
from common.chairs.models import (
    ChairIdentity,
    DigestManifest,
    ModelsConfig,
    ServingReceipt,
    VerifiedSnapshot,
)
from common.chairs.receipts import receipt_record
from common.chairs.registry import (
    ChairRegistry,
    HuggingFaceFetcher,
    HuggingFaceMaterializationFetcher,
    SnapshotFetcher,
)
from common.contracts.canonical import canonical_bytes, digest_bytes
from common.contracts.errors import ContractError
from common.credentials import (
    CREDENTIAL_VALUE_PREFIXES,
    argv_credential_piece,
    looks_like_credential_field,
)
from common.decoding import READING_CHAIRS, chair_decoding, load_decoding_policy
from common.durability import atomic_create
from common.sealed_config import parse_sealed_toml
from common.stage import DEFAULT_POD_PLACEMENT_CONFIG_PATH
from operations.serving.assembly import ProfileProbe, assemble_serving_smoke_reader
from operations.serving.capacity import plan_for_measured_card
from operations.serving.config import (
    ServingConfigInputs,
    SubprocessProfile,
    load_serving_recipes,
    parse_serving_recipes,
)
from operations.serving.errors import ServingConfigurationError
from operations.serving.http import HttpTransport
from operations.serving.manager import PackageInspector, ReceiptPublication
from operations.serving.process import ProcessLauncher
from operations.serving.recordgold_smoke import (
    RECORDGOLD_SMOKE_RECORD,
    RecordGoldSmokeRecord,
    RecordGoldSmokeRefusal,
    committed_recordgold_bytes,
    fetch_recordgold_smoke_page,
    recordgold_smoke_chairs,
)
from operations.serving.residency import POD_RESIDENCY_LOCK_PATH, FileResidencyLease
from operations.serving.smoke import (
    NvidiaSmiUtilization,
    VisionSmokeCall,
    fresh_page_witness,
    render_golden_page,
)
from operations.serving.surya_detector import SuryaBundleFetcher

from .bootstrap import (
    CONFIGURATION_RECEIPT_SCHEMA,
    SURYA_ENVIRONMENT,
    BootstrapActions,
    BootstrapJournal,
    Bootstrapper,
    BootstrapPlan,
    BootstrapReport,
    BootstrapStep,
    BootstrapStepFailure,
    ModelStoreBootstrapAction,
    SubprocessBootstrapActions,
    verify_image_contract,
)
from .chair_order import in_stage_need_order
from .chair_prefill import ChairCachePrefill, PrefillChairs
from .durable import atomic_write, canonical_json, exclusive_write
from .models import (
    POD_ID_ENVIRONMENT,
    POD_VOLUME_MOUNT_PATH,
    REQUESTED_GPU_COUNT,
    require_utc,
    utc_now,
)
from .preflight import (
    PlacementRefusal,
    PreflightRunner,
    SubprocessChecker,
    SystemGpuProbe,
    UtilizationSample,
    check_subprocess_environment,
    load_placement_table,
)
from .run_exits import EXIT_BOOTSTRAP_RED, EXIT_REFUSED
from .transfer import ChecksummedTransfer, TransferReport

HARD_DEADLINE_ENV = "VERBATUS_HARD_DEADLINE"
"""The same environment spelling the RunPod pod-timer factory reads.  Naming it
here does not make this file provider vocabulary -- the value is a Verbatus
launch fact, not a RunPod one."""
NO_HARD_DEADLINE = "none"
"""The ``VERBATUS_HARD_DEADLINE`` value that says there is none."""

HOLD_SCHEMA = "pod-bootstrap-hold.v1"
BOOTSTRAP_RESULT_SCHEMA = "pod-bootstrap-result.v1"
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
    "cache_root",
    "fixture",
    "page_witness_file",
    "serving_recipes_config",
    "submission_manifest",
    "transfer_source_root",
    "transfer_prefix",
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
    itself has passed containment and the launch-token check, so ``main`` can best-effort leave the reason
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
    cache_root: Path | None = None
    preflight_roles: tuple[str, ...] | None = None
    fixture: Path | None = None
    page_witness_file: Path | None = None
    serving_recipes_config: Path | None = None
    submission_manifest: Path | None = None
    transfer_source_root: Path | None = None
    transfer_prefix: str = "pod-transfer"
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
            "cache_root": str(self.cache_root) if self.cache_root else None,
            "preflight_roles": list(self.preflight_roles)
            if self.preflight_roles is not None
            else None,
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
            "transfer_target_factory": self.transfer_target_factory,
        }


class RegistryChairCacheVerifier:
    """The production ``ChairCacheVerifier``: one ``ensure`` per configured chair.

    ``ChairRegistry.ensure`` verifies the exact pinned snapshot in the chair
    cache, filling it from the retained store if needed, and returns the
    verified snapshot or raises the chair's named refusal. A mismatch is
    reported once, by chair, with its original cause; no automatic repair is
    attempted.
    """

    def __init__(self, registry: ChairRegistry) -> None:
        self.registry = registry

    def verify(self, identity: ChairIdentity) -> dict[str, object]:
        return self._receipt(identity, self.registry.ensure(identity))

    def prefetch(self, identity: ChairIdentity) -> dict[str, object]:
        """`verify` without evicting any other cache, for a fill beside a running smoke."""

        return self._receipt(identity, self.registry.ensure(identity, evict=False))

    @staticmethod
    def _receipt(identity: ChairIdentity, snapshot: VerifiedSnapshot) -> dict[str, object]:
        receipt: dict[str, object] = {
            "chair": identity.role,
            "manifest_digest": snapshot.manifest_digest,
            "root": str(snapshot.root),
        }
        if snapshot.verification is not None:
            receipt["verification"] = dict(snapshot.verification)
        return receipt

    def manifest(self, identity: ChairIdentity) -> DigestManifest:
        return self.registry.manifest(identity)


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
    directory instead. The page witness is retained there as well so
    the offline qualifier can recompute its digest and expected semantic output.
    Each artifact is named by its content digest and references are relative to
    that directory. Same bytes twice is a no-op; different bytes at one address
    is a refusal, so a repeated preflight can add evidence but never replace it.
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

    def publish_page_witness(self, witness: str) -> dict[str, str]:
        """Retain the witness token without putting its plaintext in the report."""

        return self._write_bytes("page-witnesses", witness.encode("ascii"), suffix=".txt")

    def publish_smoke_exchange(
        self, request: bytes, response: bytes
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Retain one fixture-bound request and its exact raw response bytes.

        These are separate content-addressed artifacts because a model may
        return an answer that is structurally parseable yet fails the smoke's
        exact witness-format rule.  The receipt names both bytes in that red
        outcome; neither is reconstructed from parsed fields.
        """

        return (
            self._write_bytes("smoke-requests", request, suffix=".json"),
            self._write_bytes("smoke-responses", response, suffix=".bin"),
        )

    def _write(self, kind: str, value: Mapping[str, object]) -> dict[str, str]:
        return self._write_bytes(kind, canonical_bytes(value), suffix=".json")

    def _write_bytes(self, kind: str, data: bytes, *, suffix: str) -> dict[str, str]:
        digest = digest_bytes(data)
        relative_path = f"{kind}/sha256/{digest}{suffix}"
        target = self.root / relative_path
        self._validate_target(target, kind)
        try:
            exclusive_write(target, data, strict=True)
        except FileExistsError:
            self._validate_target(target, kind)
            if target.read_bytes() != data:
                raise RuntimeError(
                    f"preflight {kind} evidence at {target} exists with different bytes; "
                    "evidence is not overwritten"
                ) from None
        return {"relative_path": relative_path, "sha256": digest}

    def _validate_target(self, target: Path, kind: str) -> None:
        try:
            resolved_root = self.root.resolve(strict=False)
            resolved_target = target.resolve(strict=False)
        except (OSError, RuntimeError) as error:
            raise RuntimeError(
                f"preflight {kind} evidence path at {target} cannot be inspected: {error}"
            ) from error
        if not resolved_target.is_relative_to(resolved_root):
            raise RuntimeError(
                f"preflight {kind} evidence path at {target} escapes its publication root"
            )

        candidate = self.root
        try:
            for part in (kind, "sha256"):
                if candidate.is_symlink():
                    raise RuntimeError(
                        f"preflight {kind} evidence path at {target} contains a symlink"
                    )
                if candidate.exists() and not candidate.is_dir():
                    raise RuntimeError(
                        f"preflight {kind} evidence path at {target} has a non-directory parent"
                    )
                candidate = candidate / part
            if candidate.is_symlink():
                raise RuntimeError(f"preflight {kind} evidence path at {target} contains a symlink")
            if candidate.exists() and not candidate.is_dir():
                raise RuntimeError(
                    f"preflight {kind} evidence path at {target} has a non-directory parent"
                )
            if target.is_symlink():
                raise RuntimeError(f"preflight {kind} evidence path at {target} is a symlink")
            if target.exists() and not stat.S_ISREG(target.lstat().st_mode):
                raise RuntimeError(
                    f"preflight {kind} evidence path at {target} is not a regular file"
                )
        except RuntimeError:
            raise
        except OSError as error:
            raise RuntimeError(
                f"preflight {kind} evidence path at {target} cannot be inspected: {error}"
            ) from error


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
    # How the DAI chair's RecordGold smoke record is fetched, and which record it
    # is (`operations/serving/recordgold_smoke.py`); a test pins a record of its
    # own and answers the fetch from memory.
    recordgold_fetch: Callable[[str], bytes] = committed_recordgold_bytes
    recordgold_record: RecordGoldSmokeRecord = RECORDGOLD_SMOKE_RECORD
    # The one lock every serving manager on this card must share; on
    # container-local disk, because an advisory lock on a network volume is
    # not something the mount is known to honour. The pipeline stages that
    # serve a chair take the same constant, so the preflight and the run it
    # precedes contend for one lease rather than two disjoint ones.
    residency_lock: Path = POD_RESIDENCY_LOCK_PATH
    # How a subprocess chair's (Surya's) own environment is asked for its versions.
    subprocess_checker: SubprocessChecker = check_subprocess_environment


_FLAG_NAME = re.compile(r"--[a-z0-9-]+")


class RefusingParser(argparse.ArgumentParser):
    """Argv errors become PlanRefusals naming flags, never values; there is no ``-h`` exit."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(allow_abbrev=False, add_help=False, **kwargs)  # type: ignore[arg-type]

    def error(self, message: str) -> NoReturn:
        named = message.split(":", 1)[0] if message.startswith("argument ") else message
        raise PlanRefusal("argv: missing or malformed " + ", ".join(_FLAG_NAME.findall(named)))

    def parse_args(self, args=None, namespace=None):  # type: ignore[no-untyped-def,override]
        return self.parse_flags(sys.argv[1:] if args is None else args, None)

    def parse_flags(self, argv: Sequence[str], report_path: Path | None) -> argparse.Namespace:
        try:
            args, unknown = self.parse_known_args(argv)
        except PlanRefusal as refusal:
            refusal.report_path = report_path
            raise
        if unknown:
            names = {token.split("=", 1)[0] for token in unknown}
            shown = sorted(name if _FLAG_NAME.fullmatch(name) else "(value)" for name in names)
            raise PlanRefusal(
                "argv: unrecognized argument(s) " + ", ".join(dict.fromkeys(shown)),
                report_path=report_path,
            )
        return args


def build_parser() -> RefusingParser:
    parser = RefusingParser()
    parser.add_argument("--volume-mount-path", required=True)
    parser.add_argument("--report-path", type=Path, required=True)
    parser.add_argument("--interval-seconds", type=float, default=15.0)
    parser.add_argument(
        "--keep-env",
        action="append",
        default=[],
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--hold-only",
        action="store_true",
    )
    parser.add_argument("--repository", type=Path)
    parser.add_argument("--repository-commit")
    parser.add_argument("--lockfile", type=Path)
    parser.add_argument("--journal", type=Path)
    parser.add_argument("--store-root", type=Path)
    parser.add_argument("--models-config", type=Path)
    parser.add_argument(
        "--cache-root",
        type=Path,
    )
    parser.add_argument(
        "--fixture",
        type=Path,
    )
    parser.add_argument(
        "--page-witness-file",
        type=Path,
    )
    parser.add_argument(
        "--serving-recipes-config",
        type=Path,
    )
    parser.add_argument(
        "--submission-manifest",
        type=Path,
    )
    parser.add_argument(
        "--transfer-source-root",
        type=Path,
    )
    parser.add_argument("--transfer-prefix", default=None)
    parser.add_argument(
        "--transfer-target-factory",
    )
    return parser


def _factory(reference: str) -> Callable[[], object]:
    if reference.count(":") != 1:
        raise PlanRefusal("a factory reference must be module:callable")
    module_name, name = reference.split(":", 1)
    import importlib

    return getattr(importlib.import_module(module_name), name)


def _launch_report_path(args: argparse.Namespace, launch_token: str | None) -> Path:
    if not PurePosixPath(args.volume_mount_path).is_absolute():
        raise PlanRefusal("--volume-mount-path must be an absolute path")
    report_path = _require_contained(
        args.report_path, Path(args.volume_mount_path), "--report-path"
    )
    _require_launch_token_named(report_path, launch_token, "--report-path", report_path=None)
    return report_path


def resolve_plan(args: argparse.Namespace, environment: Mapping[str, str] | None = None) -> Plan:
    """Validate every argument and combination before anything runs or holds.

    ``environment`` is read only for ``VERBATUS_LAUNCH_TOKEN`` -- the launch
    token binds ``--report-path`` (and, for a full plan, ``--journal``) to this
    launch, mirroring ``models._required_timer_arguments``'s guard against a
    second launch on the same retained volume silently overwriting the first
    launch's evidence.  It must be read here, before ``main``
    scrubs the environment, because the token's own name is credential-shaped
    and would otherwise be popped before this ever ran.
    """

    volume_mount_path = Path(args.volume_mount_path)
    launch_token = (environment or {}).get("VERBATUS_LAUNCH_TOKEN") or None
    report_path = _launch_report_path(args, launch_token)

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

    store_root = _require_contained(
        args.store_root, volume_mount_path, "--store-root", report_path=report_path
    )
    models_config = _require_contained(
        _repository_config_path(args.models_config, repository),
        repository,
        "--models-config",
        base_label="the checked-out repository",
        report_path=report_path,
    )

    cache_root = (args.cache_root or Path("/var/tmp/verbatus-chair-cache")).resolve()
    if cache_root.is_relative_to(volume_mount_path.resolve()):
        raise PlanRefusal(
            f"--cache-root {cache_root} is on the network volume; use container-local disk "
            "for the one active chair cache",
            report_path=report_path,
        )
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
    # This path may not exist until REPOSITORY checks out the pinned commit.
    # Plan and dry-run therefore validate containment and selection only. The
    # journaled CONFIGURATION step parses its content immediately
    # after checkout, before uv, model materialization, cache work, or serving.
    serving_recipes_config = _require_contained(
        _repository_config_path(
            args.serving_recipes_config or Path("config/serving_recipes.toml"), repository
        ),
        repository,
        "--serving-recipes-config",
        base_label="the checked-out repository",
        report_path=report_path,
    )
    # TRANSFER's direction, stated rather than assumed: `<volume>/submission/
    # manifest.json` is where `verbatus upload` puts a real submission, so
    # defaulting to it would make TRANSFER re-upload a submission the pod
    # already has. A consuming pod names no manifest and TRANSFER is a
    # vacuous success; a producing pod names both halves. Half a pair is a
    # plan-time refusal after pod creation, before the environment sync,
    # whichever half is missing.
    submission_manifest = args.submission_manifest
    if submission_manifest is not None:
        submission_manifest = _require_contained(
            submission_manifest,
            volume_mount_path,
            "--submission-manifest",
            report_path=report_path,
        )
    if submission_manifest is not None and args.transfer_target_factory is None:
        raise PlanRefusal(
            "--submission-manifest names a submission to send but --transfer-target-factory "
            "names nowhere to send it; a pod that is only consuming a submission already on "
            "the volume should name neither",
            report_path=report_path,
        )
    if submission_manifest is None and args.transfer_target_factory is not None:
        raise PlanRefusal(
            "--transfer-target-factory configures somewhere to send a submission but "
            "--submission-manifest names none; TRANSFER would send nothing and report "
            "success",
            report_path=report_path,
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
        cache_root=cache_root,
        fixture=fixture,
        page_witness_file=page_witness_file,
        serving_recipes_config=serving_recipes_config,
        submission_manifest=submission_manifest,
        transfer_source_root=transfer_source_root,
        transfer_prefix=args.transfer_prefix or "pod-transfer",
        transfer_target_factory=args.transfer_target_factory,
    )


def _repository_config_path(path: Path, repository: Path) -> Path:
    """Interpret a config selection relative to its checked-out repository."""

    return path if path.is_absolute() else repository / path


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
    path: Path, launch_token: str | None, flag: str, *, report_path: Path | None
) -> None:
    """Mirror ``models._required_timer_arguments``'s guard, on the bootstrap side.

    A volume is retained across pods by design: an unbound
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


def _credential_shape(value: str) -> str | None:
    """Say what made the value look like a secret, without repeating any of it."""

    if looks_like_credential_field(value):
        return "it reads as a secret's own name"
    piece = argv_credential_piece(value)
    if piece is None:
        return None
    if piece.startswith(CREDENTIAL_VALUE_PREFIXES):
        return "a known provider key prefix"
    return f"an opaque run of {len(piece)} mixed alphanumeric characters"


def refuse_credential_looking_argv(argv: Sequence[str]) -> None:
    """Refuse before parsing spends a look at any value that reads like a secret.

    Two independent checks: ``looks_like_credential_field`` asks whether the
    *name* implied by the value looks like a secret's name (a marker word);
    ``argv_credential_piece`` asks whether the value, or a path segment in it, is *shaped* like
    an opaque token, regardless of what it is named. Neither is a proof --
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
            previous = ""
        elif token.startswith("--"):
            flag, value, previous = "", token, token
        else:
            flag, value, previous = previous, token, ""
        if flag == "--keep-env":
            continue
        shape = _credential_shape(value) if value else None
        if shape is not None:
            # The value itself is deliberately not repeated. This refusal is
            # printed to the pod's own transcript and `pod_run` prints it to
            # stderr as well, and `refuse` can write it into a report on the
            # retained volume -- so echoing a value that was refused *because it
            # looks like a credential* would put the suspected secret in three
            # more places. The flag and the shape are what an operator needs.
            where = f"the value after {flag}" if flag else "an argv token"
            raise PlanRefusal(
                f"{where} looks like a credential and was refused "
                f"({shape}); the value is not repeated here, because this "
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


def _hard_deadline(environment: MutableMapping[str, str]) -> datetime | None:
    raw = environment.get(HARD_DEADLINE_ENV)
    if not raw:
        raise PlanRefusal(
            f"{HARD_DEADLINE_ENV} is not set; holding needs a hard deadline "
            f"({NO_HARD_DEADLINE!r} for a caller that will not hold)"
        )
    if raw == NO_HARD_DEADLINE:
        return None
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
    if manifest is None:
        # A consuming pod: the submission it reads is already on the volume and
        # there is nothing to send anywhere. `resolve_plan` has already refused
        # the half-configured shapes (a manifest with no target, a target with
        # no manifest), so "no manifest" here means exactly "no transfer", and
        # TRANSFER records that rather than inventing a manifest path or
        # refusing a run that needs no upload.
        return lambda: TransferReport((), (), submission_manifest_present=False).to_record()

    def _transfer() -> dict[str, object]:
        if not manifest.is_file():
            # A configured manifest that is not there is a failure, not a
            # vacuous success. The no-op above belongs to the consuming pod
            # that configured neither value; returning it here recorded the
            # TRANSFER step complete and let a producing pod continue without
            # uploading the submission it declared.
            raise BootstrapStepFailure(
                BootstrapStep.TRANSFER,
                f"configured submission manifest {manifest} is missing",
                "Restore the sealed submission manifest on the volume, then resume the "
                "bootstrap; or start this pod with no --submission-manifest if it has "
                "nothing to send.",
            )
        if plan.transfer_target_factory is None:
            # Not reachable through `resolve_plan`, which refuses this pair at
            # plan time before anything is spent. Kept as the named contract
            # for a `Plan` built directly, the same way this module keeps its
            # other unreachable-through-main refusals rather than assuming the
            # only caller.
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


def _bundle_fetcher() -> SuryaBundleFetcher:
    """The model store's fetcher for every local-repository artifact: Surya's own
    prefetch, run in the environment UV_ENVIRONMENT syncs whenever the store
    still lacks a bundle (``_store_environments``)."""
    return SuryaBundleFetcher(SURYA_ENVIRONMENT)


def _build_model_store(plan: Plan) -> ModelStoreBootstrapAction:
    return ModelStoreBootstrapAction(
        plan.store_root,  # type: ignore[arg-type]
        HuggingFaceMaterializationFetcher.from_huggingface_hub(),
        _bundle_fetcher(),
        roles=plan.preflight_roles,
        hashed_at_copy=_cached_roles(plan),
    )


def _selected(plan: Plan, role: str) -> bool:
    """Whether this pod's stage selection uses `role`; no selection means every chair."""

    return plan.preflight_roles is None or role in plan.preflight_roles


def _cached_roles(plan: Plan) -> tuple[str, ...]:
    """The configured, selected chairs CHAIR_CACHE and PREFLIGHT copy from the store
    into the chair cache, each copy hashing the bytes against the pinned manifest."""

    if plan.repository is None or plan.models_config is None:
        return ()
    models = _checked_out_roster(plan)
    return tuple(
        sorted(
            role
            for role, chair in models.chairs.items()
            if isinstance(chair, ChairIdentity) and _selected(plan, role)
        )
    )


def _build_cache(plan: Plan) -> dict[str, object]:
    registry = ChairRegistry.from_toml(
        plan.models_config,  # type: ignore[arg-type]
        cache_root=plan.cache_root,
    )
    fetcher = StoreRoleFetcher(plan.store_root)  # type: ignore[arg-type]
    chairs: list[dict[str, object]] = []
    for role, identity in sorted(registry.config.chairs.items()):
        if not _selected(plan, role):
            chairs.append({"chair": role, "state": "not-selected"})
        elif isinstance(identity, ChairIdentity) and identity.source == "huggingface":
            source = fetcher.plan(identity)
            chairs.append(
                {"chair": role, "state": "source-planned", "snapshot": source["snapshot"]}
            )
        elif isinstance(identity, ChairIdentity):
            chairs.append(_place_local_chair(registry, fetcher, identity))
        else:
            chairs.append({"chair": role, "state": "not-cached"})
    return {"chairs": chairs, "cache_root": str(plan.cache_root)}


def _prefill_chairs(plan: Plan) -> PrefillChairs | None:
    """The selected Hugging Face chairs a background fill copies from the store, in stage order.

    A chair whose store artifact is not present yet, or whose store row differs
    from its pin, is left out: MODEL_STORE fetches or refuses it, and PREFLIGHT
    fills it afterwards as it always has.
    """

    if plan.models_config is None or plan.cache_root is None or plan.store_root is None:
        return None
    # One pool of copy workers across every file of every chair, largest first.
    fetcher = StoreRoleFetcher(plan.store_root, pool=copy_pool(chair="chair-cache"))
    registry = ChairRegistry.from_toml(plan.models_config, cache_root=plan.cache_root)
    registry.fetcher = fetcher
    chairs: list[ChairIdentity] = []
    deferred: list[dict[str, str]] = []
    for role in in_stage_need_order(list(registry.config.chairs)):
        identity = registry.config.chairs[role]
        if (
            not _selected(plan, role)
            or not isinstance(identity, ChairIdentity)
            or identity.source != "huggingface"
        ):
            continue
        try:
            fetcher.plan(identity)
        except ChairRefusal as refusal:
            deferred.append({"chair": role, "reason": f"store source not ready: {refusal}"})
            continue
        chairs.append(identity)
    return PrefillChairs(registry, tuple(chairs), tuple(deferred), fetcher.pool)


def _place_local_chair(
    registry: ChairRegistry, fetcher: StoreRoleFetcher, identity: ChairIdentity
) -> dict[str, object]:
    """Copy a local-repository chair's verified store bundle to where the roster binds it.

    The roster binds such a chair under its ``model_root``, beside the roster on
    container-local disk; the store holds it on the volume. A copy already there
    that verifies against the roster's manifest is kept. Otherwise every file the
    manifest names is copied from the verified store snapshot into a fresh
    sibling, which takes the chair's place only once it verifies, so the path the
    roster names holds a verified bundle or nothing. The copy hashes each file as it
    writes it, so neither check after it reads those bytes again.
    """
    config = registry.config
    if config.model_root is None or config.source_path is None or identity.path is None:
        raise BootstrapStepFailure(
            BootstrapStep.CHAIR_CACHE,
            f"chair {identity.role} is a local repository with no model_root to place it in",
            "Name model_root in the roster the pod runs with, then resume.",
        )
    model_root = config.source_path.parent / config.model_root
    target = model_root / identity.path
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise BootstrapStepFailure(
            BootstrapStep.CHAIR_CACHE,
            f"chair {identity.role} is bound at {target}, which is a link or not a directory",
            "Remove what is at that path, so the verified bundle can be placed there, then resume.",
        )
    if target.is_dir():
        try:
            snapshot = registry.ensure(identity)
        except ChairRefusal:
            shutil.rmtree(target)
        else:
            return {"chair": identity.role, "state": "local-verified", "root": str(snapshot.root)}
    source = fetcher.plan(identity)
    model_root.mkdir(parents=True, exist_ok=True)
    staged = model_root / f".{identity.path}.placing"
    shutil.rmtree(staged, ignore_errors=True)
    staged.mkdir()
    try:
        manifest = registry.manifest(identity)
        ledger = fetcher.fetch(identity, staged, tuple(row.path for row in manifest.rows))
        copied = dict(ledger.digests) if isinstance(ledger, CopyLedger) else {}
        verify_snapshot(identity, staged, manifest, copied=copied)
        os.replace(staged, target)
    finally:
        shutil.rmtree(staged, ignore_errors=True)
    registry.verify_local_copy(identity, copied)
    receipt: dict[str, object] = {
        "chair": identity.role,
        "state": "local-placed",
        "snapshot": source["snapshot"],
    }
    if isinstance(ledger, CopyLedger):
        receipt["verification"] = {"bytes": "hashed while copying", **ledger.to_record()}
    return receipt


PREFLIGHT_DTYPE = "bfloat16"
"""The dtype preflight measures the card for.  Every vLLM row in both shipped
catalogues is ``dtype = "bfloat16"`` and ``ServingSmokeReader`` refuses a
profile whose dtype is not exactly the measured one, so any other value here
would make every real smoke red before it launched."""


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
    # digest. Evidence is added, never replaced, exactly as
    # `PodPreflightReceiptPublisher` does for the serving records beside it. The
    # witness is URL-safe by construction (`secrets.token_urlsafe`), so it is a
    # filename as it stands.
    page = plan.preflight_root / "golden-page" / f"{witness}.png"
    page_bytes = render_golden_page(page, witness)
    return page, witness, page_bytes


def _recordgold_page(
    plan: Plan, seams: PreflightSeams, chairs: frozenset[str]
) -> tuple[Path, str, str]:
    """The RecordGold record the DAI chairs read: its PNG on the volume, gold text, digest.

    Fetched and verified against the pins before any chair starts, so a
    network failure or a moved dataset is a named PREFLIGHT refusal here and
    never a chair's ``smoke-read-failed``. The page is named by its own digest,
    so a second preflight under the launch adds a page rather than writing
    over one earlier receipts name.
    """

    record = seams.recordgold_record
    try:
        fetched = fetch_recordgold_smoke_page(record, fetch=seams.recordgold_fetch)
    except RecordGoldSmokeRefusal as refusal:
        raise BootstrapStepFailure(
            BootstrapStep.PREFLIGHT,
            f"the RecordGold smoke record for {sorted(chairs)} could not be prepared: {refusal}",
            "A fetch failure is the network, not the chair: check the pod can reach "
            f"{record.record_url} and {record.rows_url}. A digest mismatch means the "
            "served record changed; re-pin it in operations/serving/recordgold_smoke.py.",
        ) from refusal
    digest = digest_bytes(fetched.png)
    page = plan.preflight_root / "recordgold-smoke" / f"{record.record_id}-{digest[:16]}.png"
    page.parent.mkdir(parents=True, exist_ok=True)
    try:
        atomic_create(page, fetched.png, strict=False)
    except FileExistsError:
        if page.read_bytes() != fetched.png:
            raise BootstrapStepFailure(
                BootstrapStep.PREFLIGHT,
                f"a different page already exists at {page}; preflight evidence is added, "
                "never written over",
                "Remove nothing; resume under a fresh launch so the record gets its own name.",
            ) from None
    return page, fetched.text, digest


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
    page in that failure's detail, except for an intentionally empty stage
    selection, which has no chair to smoke-read.
    """

    digests = {
        receipt["supplied_fixture_sha256"]
        for receipt in smoke_receipts
        # The DAI chair's receipt names the RecordGold record it read, not the
        # golden page; it carries that page's own digest under its own name.
        if receipt.get("smoke_page") != "recordgold-record"
    }
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
    plan: Plan,
    seams: PreflightSeams | None = None,
    prefill: ChairCachePrefill | None = None,
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
        recipes_config = plan.serving_recipes_config
        if models_config is None or recipes_config is None:
            raise PlanRefusal(
                "bootstrap plan reached PREFLIGHT without its models or serving recipes "
                "configuration; resolve_plan fills both for every full plan"
            )
        registry = ChairRegistry.from_toml(models_config, cache_root=plan.cache_root)
        if seams is None:
            registry.fetcher = StoreRoleFetcher(plan.store_root)  # type: ignore[arg-type]
        else:
            registry.fetcher = chosen.fetcher_factory()
        if prefill is not None and prefill.registry is not None:
            # The chairs the background fill verified in this process are not
            # read again here, nor by the smoke's own ensure.
            registry.adopt_verifications(prefill.registry)
        # One read each, sealed from the table that is parsed: the serving
        # assembly re-reads both files and refuses if what it parses does not
        # seal to what is sealed here, so a substitution between the two
        # reads is a named refusal rather than a table the run never sealed.
        recipes = load_serving_recipes(recipes_config)
        try:
            placement_bytes = DEFAULT_POD_PLACEMENT_CONFIG_PATH.read_bytes()
            placement = load_placement_table(
                DEFAULT_POD_PLACEMENT_CONFIG_PATH, source_bytes=placement_bytes
            )
            _, placement_sha256 = parse_sealed_toml(placement_bytes, "placement table")
        except (OSError, PlacementRefusal, ContractError) as error:
            raise BootstrapStepFailure(
                BootstrapStep.PREFLIGHT,
                f"placement table {DEFAULT_POD_PLACEMENT_CONFIG_PATH} could not be read: {error}",
                "Restore the reviewed placement table at the pinned commit, then resume.",
            ) from error
        if recipes.source_sha256 is None:  # load_serving_recipes always digests; stated
            raise BootstrapStepFailure(
                BootstrapStep.PREFLIGHT,
                f"serving catalogue {recipes_config} was loaded without a source digest",
                "Load the catalogue from its file so its bytes can be sealed.",
            )
        config_inputs = ServingConfigInputs(recipes.source_sha256, placement_sha256)
        context = _PreflightContext(config_inputs.to_record(), registry)
        preflight_root = plan.preflight_root
        publisher = PodPreflightReceiptPublisher(preflight_root, context)
        probe = chosen.gpu_probe or SystemGpuProbe(disk_path=plan.volume_mount_path)
        profile = probe.profile(PREFLIGHT_DTYPE, expected_gpu_count=REQUESTED_GPU_COUNT)
        # The width each selected chair runs at on this card; the smoke below reads
        # at it, and pod_run forwards it to the stages. None on an unmeasured card.
        capacity_plan = plan_for_measured_card(
            profile=profile,
            placement=placement,
            recipes=recipes,
            chairs={
                role: chair
                for role, chair in registry.config.chairs.items()
                if plan.preflight_roles is None or role in plan.preflight_roles
            },
            serving_config_inputs=config_inputs,
        )
        fixture, witness, page_bytes_at_render = _golden_page(plan, chosen)
        try:
            decoding_policy, _decoding_sha256 = load_decoding_policy()
        except ContractError as error:
            raise BootstrapStepFailure(
                BootstrapStep.PREFLIGHT,
                f"the decoding policy could not be read: {error}",
                "Restore the reviewed decoding policy at the pinned commit, then resume.",
            ) from error
        selected_roles = (
            frozenset(plan.preflight_roles) if plan.preflight_roles is not None else None
        )
        recordgold_chairs = recordgold_smoke_chairs(registry.config)
        if selected_roles is not None:
            recordgold_chairs &= selected_roles
        chair_fixtures: dict[str, str | Path] = {}
        recordgold_text: str | None = None
        recordgold_page_sha256: str | None = None
        if recordgold_chairs:
            recordgold_page, recordgold_text, recordgold_page_sha256 = _recordgold_page(
                plan, chosen, recordgold_chairs
            )
            chair_fixtures = {chair: recordgold_page for chair in recordgold_chairs}
        smoke_call = VisionSmokeCall(
            witness,
            utilization=chosen.utilization or NvidiaSmiUtilization(),
            raw_exchange_publisher=publisher.publish_smoke_exchange,
            chair_sampling={
                chair: chair_decoding(decoding_policy, chair) for chair in READING_CHAIRS
            },
            recordgold_chairs=recordgold_chairs,
            recordgold_record=chosen.recordgold_record,
            recordgold_text=recordgold_text,
            recordgold_page_sha256=recordgold_page_sha256,
        )
        witness_reference = publisher.publish_page_witness(witness)
        smoke_call = replace(smoke_call, page_witness_reference=witness_reference)
        reader = assemble_serving_smoke_reader(
            registry=registry,
            stage_context=context,
            receipt_publisher=publisher,
            smoke_call=smoke_call,
            gpu_profile=profile,
            log_root=preflight_root / "serving-logs",
            recipes_path=recipes_config,
            placement_path=DEFAULT_POD_PLACEMENT_CONFIG_PATH,
            launcher=chosen.launcher,
            http=chosen.http,
            package_inspector=chosen.package_inspector,
            residency_lease=FileResidencyLease(chosen.residency_lock),
            producer="operations.pod.bootstrap_main",
            capacity_plan=capacity_plan,
        )
        cache_verifier = RegistryChairCacheVerifier(registry)
        runner = PreflightRunner(
            registry.config,
            placement,
            cache_verifier,
            reader,
            fixture,
            serving_recipes=recipes,
            subprocess_checker=chosen.subprocess_checker,
            selected_roles=selected_roles,
            chair_fixtures=chair_fixtures,
            cache_prefetcher=cache_verifier.prefetch,
        )
        report = runner.run(profile)
        record = report.to_record()
        record["serving_config_inputs"] = config_inputs.to_record()
        record["capacity_plan"] = capacity_plan.to_record() if capacity_plan is not None else None
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
                "Read the issues by chair: a cache mismatch names its chair, a launch or "
                "fixture-bound request failure names the affected serving profile, and an "
                "empty utilization sample means nvidia-smi could not be read. Do not "
                "substitute a fixture pass.",
            )
        return record

    return _run


class _LazyChairCache:
    """Defer ``_build_cache`` until CHAIR_CACHE plans pinned sources.

    ``build_actions`` runs before ``Bootstrapper.run`` -- before REPOSITORY has
    checked out ``--repository-commit`` and before UV_ENVIRONMENT has synced
    the lockfile.  ``_build_cache`` eagerly reads ``--models-config`` off disk
    and verifies the retained-store source plan; built eagerly, a
    CHAIR_CACHE receipt would attest to whatever ``models.toml`` happened to be
    on disk at container start, not to the commit the journal names.
    """

    def __init__(self, plan: Plan) -> None:
        self._plan = plan

    def verify(self) -> dict[str, object]:
        return _build_cache(self._plan)


def _build_configuration_validation(plan: Plan) -> Callable[[], dict[str, object]]:
    """Read the pinned roster and catalogue after checkout and before paid setup."""

    def _validate() -> dict[str, object]:
        if (
            plan.repository is None
            or plan.models_config is None
            or plan.serving_recipes_config is None
        ):
            raise BootstrapStepFailure(
                BootstrapStep.CONFIGURATION,
                "bootstrap plan reached CONFIGURATION without its repository, roster, or serving "
                "catalogue",
                "Supply the complete bootstrap plan and start a new schema-v3 journal.",
            )
        try:
            models_source = _read_configuration_source(
                plan.models_config, "model roster", plan.repository
            )
            parsed_models, models_sha256 = parse_sealed_toml(
                models_source, f"model roster {plan.models_config}"
            )
            parse_models_config(parsed_models, source_path=plan.models_config)
        except ContractError as error:
            raise BootstrapStepFailure(
                BootstrapStep.CONFIGURATION,
                f"model roster validation failed: {error}",
                "Repair the pinned roster selection, then resume this journal before any "
                "environment or model work.",
            ) from error
        placement = DEFAULT_POD_PLACEMENT_CONFIG_PATH.resolve()
        pinned = plan.repository.resolve() / "config" / "pod_placement.toml"
        if placement != pinned:
            raise BootstrapStepFailure(
                BootstrapStep.CONFIGURATION,
                f"placement table {placement} is not the checked-out repository's {pinned}; "
                "preflight and the stages would not read the pinned commit's table",
                "Start bootstrap_main from the checked-out repository's own environment, "
                "then resume this journal.",
            )
        try:
            serving_source = _read_configuration_source(
                plan.serving_recipes_config, "serving catalogue", plan.repository
            )
            placement_source = _read_configuration_source(
                placement, "placement table", plan.repository
            )
        except ContractError as error:
            raise BootstrapStepFailure(
                BootstrapStep.CONFIGURATION,
                f"a selected configuration source could not be read: {error}",
                "Repair or restore the named file at the pinned commit, then resume this journal "
                "before any environment or model work.",
            ) from error
        try:
            serving_raw, serving_sha256 = parse_sealed_toml(serving_source, "serving catalogue")
            parse_serving_recipes(
                serving_raw,
                source_path=plan.serving_recipes_config,
                source_sha256=serving_sha256,
            )
        except (ContractError, ServingConfigurationError) as error:
            raise BootstrapStepFailure(
                BootstrapStep.CONFIGURATION,
                f"selected serving catalogue {plan.serving_recipes_config} could not be parsed: {error}",
                "Repair or restore the named serving catalogue at the pinned commit, then resume "
                "this journal before any environment or model work.",
            ) from error
        try:
            load_placement_table(placement, source_bytes=placement_source)
            _, placement_sha256 = parse_sealed_toml(placement_source, "placement table")
        except (PlacementRefusal, ContractError) as error:
            raise BootstrapStepFailure(
                BootstrapStep.CONFIGURATION,
                f"placement table {placement} could not be parsed: {error}",
                "Repair or restore the named placement table at the pinned commit, then resume "
                "this journal before any environment or model work.",
            ) from error
        return {
            "schema": CONFIGURATION_RECEIPT_SCHEMA,
            "bindings": {
                "models_config": {"path": str(plan.models_config), "sha256": models_sha256},
                "serving_recipes_config": {
                    "path": str(plan.serving_recipes_config),
                    "sha256": serving_sha256,
                },
                "placement_config": {
                    "path": str(placement),
                    "sha256": placement_sha256,
                },
            },
        }

    return _validate


def _read_configuration_source(path: Path, label: str, repository: Path) -> bytes:
    if not path.is_absolute() or ".." in path.parts or not path.is_relative_to(repository):
        raise ContractError(
            f"{label} {path} escapes the checked-out repository; select a file inside it"
        )
    try:
        resolved_root = repository.resolve(strict=True)
        resolved_path = path.resolve(strict=True)
        if not resolved_path.is_relative_to(resolved_root):
            raise ContractError(
                f"{label} {path} escapes the checked-out repository; select a file inside it"
            )
        return resolved_path.read_bytes()
    except OSError as error:
        raise ContractError(f"{label} {path} could not be read: {error}") from error


def _subprocess_environments(plan: Plan) -> frozenset[str]:
    """The subprocess environments this pod syncs: each one a chair its selected
    stages run is served from, and the bundle fetcher's whenever the model store
    still lacks a bundle. Read after CONFIGURATION has validated the roster and
    the catalogue, so a pod syncs Surya's environment only when something runs in it."""

    return _stage_environments(plan) | _store_environments(plan)


def _store_environments(plan: Plan) -> frozenset[str]:
    """The bundle fetcher's environment, when MODEL_STORE will fetch a local bundle
    one of this pod's selected chairs needs."""

    if plan.store_root is None:
        return frozenset()
    needed = set(artifacts_for_roles(plan.preflight_roles))
    try:
        pending = needed.intersection(pending_local_artifacts(plan.store_root))
    except ChairRefusal as error:
        raise BootstrapStepFailure(
            BootstrapStep.UV_ENVIRONMENT,
            f"the model store at {plan.store_root} cannot say what it still needs: {error}",
            "Repair the model store or start a fresh one on the volume, then resume.",
        ) from error
    return frozenset({_bundle_fetcher().environment}) if pending else frozenset()


def _checked_out_roster(plan: Plan) -> ModelsConfig:
    """The selected roster, read from inside the checked-out repository."""

    return parse_models_config(
        parse_sealed_toml(
            _read_configuration_source(plan.models_config, "model roster", plan.repository),  # type: ignore[arg-type]
            f"model roster {plan.models_config}",
        )[0],
        source_path=plan.models_config,
    )


def _stage_environments(plan: Plan) -> frozenset[str]:
    """The environments the checked-out catalogue's subprocess rows run in, for
    the chairs the roster configures and this pod's preflight selects: every
    chair, unless a stage selection named fewer."""

    if plan.repository is None or plan.models_config is None:
        return frozenset()
    if plan.serving_recipes_config is None:
        return frozenset()
    models = _checked_out_roster(plan)
    raw, digest = parse_sealed_toml(
        _read_configuration_source(
            plan.serving_recipes_config, "serving catalogue", plan.repository
        ),
        "serving catalogue",
    )
    recipes = parse_serving_recipes(
        raw, source_path=plan.serving_recipes_config, source_sha256=digest
    )
    selected = plan.preflight_roles
    configured = {
        (identity.serving_recipe, role)
        for role, identity in models.chairs.items()
        if isinstance(identity, ChairIdentity) and (selected is None or role in selected)
    }
    return frozenset(
        profile.environment
        for profile in recipes.profiles
        if isinstance(profile, SubprocessProfile) and (profile.recipe, profile.chair) in configured
    )


def _local_bundles(plan: Plan) -> dict[Path, int]:
    """Where CHAIR_CACHE copies each local-repository chair the roster configures and
    this pod selects, and the bytes its pinned manifest names, so the container-disk
    check counts them."""

    if plan.repository is None or plan.models_config is None:
        return {}
    models = _checked_out_roster(plan)
    if models.model_root is None:
        return {}
    registry = ChairRegistry(models)
    model_root = plan.models_config.parent / models.model_root
    try:
        return {
            model_root / identity.path: sum(row.size for row in registry.manifest(identity).rows)
            for role, identity in models.chairs.items()
            if isinstance(identity, ChairIdentity)
            and identity.source == "local-repository"
            and identity.path is not None
            and _selected(plan, role)
        }
    except ChairRefusal as error:
        raise BootstrapStepFailure(
            BootstrapStep.UV_ENVIRONMENT,
            f"a local-repository chair's pinned manifest could not be read: {error}",
            "Restore the pinned checkout's manifests, then resume.",
        ) from error


def build_actions(plan: Plan) -> BootstrapActions:
    """The real composition; check image facts at REPOSITORY before paid setup."""

    prefill = ChairCachePrefill(lambda: _prefill_chairs(plan))
    return SubprocessBootstrapActions(
        subprocess_environments=lambda: _subprocess_environments(plan),
        local_bundles=lambda: _local_bundles(plan),
        repository=plan.repository,  # type: ignore[arg-type]
        configuration=_build_configuration_validation(plan),
        transfer=_build_transfer(plan),
        materialize_model_store=lambda: _build_model_store(plan).materialize(),
        cache=_LazyChairCache(plan),  # type: ignore[arg-type]
        preflight=_build_preflight(plan, prefill=prefill),
        prefill=prefill,
        image_contract=lambda: verify_image_contract(
            plan.repository,  # type: ignore[arg-type]
            interpreter=Path(sys.executable),
        ),
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
    unreachable from the laptop once the pod is destroyed. Best
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
    unmentioned -- this failure must be named too, not only the refusal it
    was recording.
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


def prepare(
    raw_argv: Sequence[str],
    environment: MutableMapping[str, str],
    *,
    now: Callable[[], datetime],
) -> tuple[Plan, datetime | None]:
    """Everything before any action: argv, plan, write probe, scrub, hard deadline.

    Split out of ``main`` so ``pod_run`` runs the identical preparation over
    the bootstrap half of its argv. A ``PlanRefusal`` propagates; the caller
    prints it and leaves the durable reason (``refuse``), because which report
    path the reason belongs on is the caller's fact.
    """

    argv = list(raw_argv)
    refuse_credential_looking_argv(argv)
    head = RefusingParser()
    head.add_argument("--volume-mount-path", required=True)
    head.add_argument("--report-path", type=Path, required=True)
    report_path = _launch_report_path(
        head.parse_known_args(argv)[0], environment.get("VERBATUS_LAUNCH_TOKEN") or None
    )
    args = build_parser().parse_flags(argv, report_path)
    plan = resolve_plan(args, environment)
    try:
        # A plain directory at the sealed mount path would put run state and the
        # model store on the pod's container disk instead of the network volume.
        if str(plan.volume_mount_path) == POD_VOLUME_MOUNT_PATH and not os.path.ismount(
            plan.volume_mount_path
        ):
            raise PlanRefusal(
                f"--volume-mount-path {plan.volume_mount_path} is the pod's expected "
                "network-volume mount point, but this machine does not have anything "
                "mounted there; an unmounted local directory is not the network volume",
                report_path=plan.report_path,
            )
        write_probe(plan.volume_mount_path)
        scrubbed = scrub_environment(environment, keep=plan.keep_env)
        environment.clear()
        environment.update(scrubbed)
        return plan, _hard_deadline(environment)
    except PlanRefusal as refusal:
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


@dataclass(frozen=True, slots=True)
class BootstrapRefused:
    """A bootstrap that ends in a refusal rather than a green or red report.

    ``reason`` says which refusal it was, so a caller's own record names it
    rather than guessing. ``report`` is the journal's report when the steps
    ran and only writing their result failed; ``None`` when nothing ran.
    """

    reason: str
    report: BootstrapReport | None = None


def run_bootstrap(
    plan: Plan,
    *,
    now: Callable[[], datetime],
    actions_factory: Callable[[Plan], BootstrapActions],
    environment: MutableMapping[str, str] | None = None,
    pod_id: str | None = None,
) -> BootstrapReport | BootstrapRefused:
    """Run the journaled steps and return the report, or the refusal that ended them.

    ``pod_id`` names the pod this runs on; a journal another pod wrote is then set
    aside rather than resumed (``BootstrapJournal``).

    A red step is a returned red report -- the caller decides its exit. An
    action factory that cannot be built, or a result that cannot be written
    after the steps ran, is a ``BootstrapRefused`` naming which; the first
    also leaves its reason on the volume where that is possible.
    """

    journal = BootstrapJournal(
        plan.journal,  # type: ignore[arg-type]
        BootstrapPlan(plan.repository_commit, plan.lockfile),  # type: ignore[arg-type]
        now=now,
        pod_id=pod_id,
    )
    try:
        actions = actions_factory(plan)
    except Exception as error:
        reason = f"bootstrap actions could not be built: {error}"
        print(f"bootstrap_main refused: {reason}", file=sys.stderr)
        failure = _write_refusal_report(plan.report_path, reason, now=now)
        if failure is not None:
            print(f"bootstrap_main refusal report could not be written: {failure}", file=sys.stderr)
        return BootstrapRefused(reason)
    report = Bootstrapper(journal, actions, environment=environment).run()
    result_record = {
        "schema": BOOTSTRAP_RESULT_SCHEMA,
        "state": "bootstrap-green" if report.green else "bootstrap-red",
        "at": now().isoformat().replace("+00:00", "Z"),
        "bootstrap": report.to_record(),
    }
    try:
        atomic_write(plan.report_path, canonical_json(result_record))
    except OSError as error:
        reason = (
            f"the bootstrap ran ({result_record['state']}) but its result could not be written "
            f"to {plan.report_path}: {error}"
        )
        print(reason, file=sys.stderr)
        return BootstrapRefused(reason, report)
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
    # Read before `prepare` scrubs the environment. This process is the pod's own
    # start command, so its environment is the container's.
    pod_id = environment.get(POD_ID_ENVIRONMENT) or None
    if pod_id is not None and not (pod_id.isascii() and pod_id.isalnum()):
        pod_id = None
    try:
        plan, hard_deadline = prepare(raw_argv, environment, now=now)
    except PlanRefusal as refusal:
        return refuse(refusal, plan=None, now=now, label="bootstrap_main")

    if plan.dry_run:
        print(json.dumps(plan.to_record(), sort_keys=True, indent=2))
        return 0

    if hard_deadline is None:
        refusal = PlanRefusal(
            f"{HARD_DEADLINE_ENV}={NO_HARD_DEADLINE} is for a caller that will not hold; "
            "bootstrap_main holds after the bootstrap, so it needs a hard deadline",
            report_path=plan.report_path,
        )
        return refuse(refusal, plan=plan, now=now, label="bootstrap_main")

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

    report = run_bootstrap(
        plan,
        now=now,
        actions_factory=actions_factory,
        environment=environment,
        pod_id=pod_id,
    )
    if isinstance(report, BootstrapRefused):
        return EXIT_REFUSED
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
