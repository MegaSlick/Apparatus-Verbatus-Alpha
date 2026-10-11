"""Idempotent, journaled pod bootstrap at one exact repository commit."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Callable, Mapping, MutableMapping, Protocol
from urllib.parse import urlsplit

from common.chairs.errors import ChairRefusal
from common.chairs.model_store import (
    BundleFetcher,
    MaterializationFetcher,
    materialize_real_roster,
)
from common.contracts.canonical import is_sha256

from .durable import atomic_write, canonical_json
from .models import require_utc, utc_now

BOOTSTRAP_SCHEMA = "pod-bootstrap.v4"
"""v4 inserts CUDA compatibility before the costly environment and serving steps."""
CONFIGURATION_RECEIPT_SCHEMA = "pod-bootstrap-configuration.v3"
_RAW_BYTE_RECEIPT_SCHEMA = "pod-bootstrap-configuration.v1"
"""The checked-out selections re-read before any completed bootstrap is reused.

v2 binds each file's seal (`common/sealed_config.py`) where v1 bound raw bytes, so
a v1 journal is refused by schema rather than as a changed configuration. v3 binds
the roster, catalogue and placement table only.
"""
_CONFIGURATION_BINDINGS = {
    "models_config",
    "serving_recipes_config",
    "placement_config",
}
BOOTSTRAP_EXECUTABLES = {
    "git": "/usr/bin/git",
    "uv": "/usr/local/bin/uv",
    "nvidia-smi": "/usr/bin/nvidia-smi",
    "apt-cache": "/usr/bin/apt-cache",
    "apt-get": "/usr/bin/apt-get",
    "dpkg-query": "/usr/bin/dpkg-query",
}
GIT_COMMAND_TIMEOUT_SECONDS = 600
CUDA_COMPAT_PATH = "/usr/local/cuda-13.0/compat"
CUDA_COMPAT_PACKAGE = "cuda-compat-13-0"
CUDA_COMPAT_VERSION = "580.178.04-1ubuntu1"
CUDA_13_MIN_DRIVER = (580, 65, 6)
CUDA_DRIVER_LIBRARY = "libcuda.so.1"
BOOTSTRAP_ENVIRONMENT = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    # uv resolves its cache through UV_CACHE_DIR, then XDG_CACHE_HOME, then
    # $HOME. This environment is explicit and supplies none of those, so the
    # cache is named here rather than left to whatever uv infers from a passwd
    # entry.
    #
    # `/tmp` because it is the one absolute path writable by whatever user the
    # pod image runs as. The cost is honest and bounded: the cache does not
    # survive a pod, so a fresh pod re-downloads the locked wheels once. It is
    # deliberately not under the checkout, which must stay exactly the pinned
    # commit, and not on the model volume, whose contents are evidence.
    "UV_CACHE_DIR": "/tmp/verbatus-uv-cache",
}


REPOSITORY_VENV_DIRECTORY = ".venv"
"""Where ``uv sync`` puts the environment every later step runs against.

``ServingManager`` launches vLLM as ``sys.executable -m vllm.entrypoints.cli.main``
and verifies the pinned versions with ``importlib.metadata`` on this same
interpreter, so a pod whose primary process is the system python reaches
PREFLIGHT and fails on a missing pin *after* the ten-gigabyte download.
"""

# Roughly what the two container-local copies of the serving stack need before
# `uv sync --locked --group pod` starts: the wheel cache under UV_CACHE_DIR,
# which this module's own comment sizes at "on the order of ten gigabytes", and
# the unpacked install in `<repository>/.venv`, which is larger again. Both are
# bounds rather than measurements, to be replaced by what a live boot observes.
# They are deliberately checked against *free* space, which is the one fact the pod can
# measure for itself, rather than against the requested container disk, which is
# only what was asked for.
UV_CACHE_REQUIRED_BYTES = 12 * 1024**3
REPOSITORY_VENV_REQUIRED_BYTES = 20 * 1024**3

SURYA_ENVIRONMENT = "operations/serving/surya"
"""Surya's own uv project, synced beside the project's environment when a chair
the pod's stages run is served from it, or when the model store still lacks
Surya's weight bundle, which Surya's own prefetch fetches in it.

Surya pins Pillow and OpenCV versions the project cannot share, so the
Designator runs it through this environment's interpreter
(``operations/serving/surya/README.md``); preflight runs that interpreter once
on the golden page.
"""
# What syncing each subprocess environment adds to the container disk: its uv
# cache and its installed venv. Surya's lock sums to 2.9 GiB of linux x86_64
# wheels (torch 2.14.0 and its CUDA libraries are most of it), but uv's cache
# keeps wheels unpacked, so both copies are about the installed size: 5.6 GiB,
# measured on a synced environment on a development machine. The bounds allow
# for no sharing with the project's own cache, like the two above.
SUBPROCESS_ENVIRONMENT_REQUIRED_BYTES = {
    SURYA_ENVIRONMENT: {"uv_cache": 7 * 1024**3, "venv": 7 * 1024**3},
}


def _existing_ancestor(path: Path) -> Path:
    """The nearest existing directory at or above ``path``.

    ``UV_CACHE_DIR`` and ``<repository>/.venv`` usually do not exist yet when
    the sync is about to create them; the filesystem that will hold them is the
    one their nearest existing parent is on.
    """

    candidate = Path(path)
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            return candidate
        candidate = parent
    return candidate


def _free_bytes(path: Path) -> int:
    return shutil.disk_usage(_existing_ancestor(path)).free


def _filesystem_key(path: Path) -> int:
    """Which filesystem a path will land on, so two paths on one disk share it."""

    return os.stat(_existing_ancestor(path)).st_dev


def _gib(value: int) -> str:
    return f"{value / 1024**3:.1f}"


class ImageContractRefusal(RuntimeError):
    """The pod image does not meet what the bootstrap assumes of it.

    Named separately from :class:`BootstrapStepFailure` because it is a fact
    about the *machine the bootstrap was started on*, not about a step's work:
    the image contract in ``operations/pod/README.md`` is what a request author
    has to satisfy before create, and this is that contract enforced where it
    can still be enforced cheaply -- on the pod, before ``git fetch``, and long
    before anything is downloaded.
    """


def verify_image_contract(
    repository: Path,
    *,
    interpreter: Path,
    interpreter_prefix: Path | None = None,
    executables: Mapping[str, str] = BOOTSTRAP_EXECUTABLES,
    environment: Mapping[str, str] = BOOTSTRAP_ENVIRONMENT,
) -> dict[str, object]:
    """Refuse, by name, an image that cannot run this bootstrap.

    The image contract requires:

    * Bootstrap tools at exactly the configured absolute paths. Nothing here
      uses PATH lookup, and the default uv installer puts ``uv`` in
      ``~/.local/bin`` rather than ``/usr/local/bin``.
    * ``--repository`` already a checkout with an ``origin`` remote. This module
      *fetches and checks out*; it has never cloned, so an image with no
      checkout in it fails at the first git call with the volume already
      attached and the card already billing.
    * Git can fetch from an HTTPS origin under the scrubbed environment.
      Local config cannot include another file or invoke external credential routes.
    * The running interpreter and its ``sys.prefix`` inside ``<repository>/.venv``,
      because that is the environment ``uv sync`` fills and the one ServingManager
      later inspects.  A standard virtual environment's ``bin/python`` is usually
      a symlink to its base Python, so this checks the invoked path and prefix,
      not the symlink's target.

    Returns what it verified, for the REPOSITORY receipt. Never returns the
    origin URL or any part of it: a URL is one of the three places a credential
    may legitimately sit, and this record is written to the volume.
    """

    verified: dict[str, object] = {}
    for name in sorted(executables):
        candidate = Path(executables[name])
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise ImageContractRefusal(
                f"the pod image does not carry an executable {name} at {candidate}; the "
                "bootstrap runs its tools by absolute path and never searches PATH, so "
                "the image must place them exactly there (see the image contract in "
                "operations/pod/README.md)"
            )
    verified["executables"] = {name: executables[name] for name in sorted(executables)}

    repository = Path(repository)
    config_path = _git_config_path(repository)
    if config_path is None or not config_path.is_file():
        raise ImageContractRefusal(
            f"{repository} is not a git checkout; the bootstrap fetches and checks out a "
            "pinned commit inside a checkout the image already carries -- it has never "
            "cloned one, so there is nothing here for the pinned commit to land in"
        )
    entries = _git_config_entries(config_path, environment, repository, executables["git"])
    for key, _value in entries:
        lowered = key.lower()
        parts = lowered.split(".")
        forbidden = (
            parts[0] in {"include", "includeif", "credential"}
            or lowered in {"core.askpass", "core.sshcommand"}
            or (
                parts[0] == "http"
                and parts[-1] in {"cookiefile", "sslkey", "sslcert", "proxysslkey", "proxysslcert"}
            )
            or lowered == "extensions.worktreeconfig"
            or (parts[0] == "url" and parts[-1] in {"insteadof", "pushinsteadof"})
        )
        if forbidden:
            raise ImageContractRefusal(
                f"the checkout at {repository} configures a forbidden git key in {parts[0]}; "
                "remove the external credential or configuration route before bootstrap"
            )
    origins = [value for key, value in entries if key.lower() == "remote.origin.url"]
    if not origins:
        raise ImageContractRefusal(
            f"the checkout at {repository} names no origin remote; the pinned commit is "
            "fetched from origin, and a checkout with no remote cannot be advanced to it"
        )
    verified["origin_remote"] = "present"

    home_is_visible = bool(environment.get("HOME"))
    if home_is_visible:
        raise ImageContractRefusal(
            "the bootstrap git environment exposes HOME, which may supply credentials "
            "outside the checked-out repository"
        )
    try:
        valid_https = all(
            urlsplit(origin).scheme.lower() == "https" and urlsplit(origin).hostname
            for origin in origins
        )
    except ValueError:
        valid_https = False
    if not valid_https:
        raise ImageContractRefusal(
            f"the checkout at {repository} has a non-HTTPS origin; use an HTTPS origin "
            "before bootstrap"
        )
    origin = origins[0]
    embedded_credential = "@" in urlsplit(origin).netloc
    local_credential_route = any(
        key.lower().startswith("http.")
        and key.lower().endswith(".extraheader")
        or key.lower() == "http.extraheader"
        for key, _value in entries
    )
    # The pinned fetch proves reachability under the scrubbed environment.
    verified["credential_route"] = (
        "embedded-in-url"
        if embedded_credential
        else "repository-local"
        if local_credential_route
        else "anonymous-https"
    )

    expected_venv = Path(os.path.abspath(repository / REPOSITORY_VENV_DIRECTORY))
    expected_bin = expected_venv / "bin"
    invoked_interpreter = Path(os.path.abspath(interpreter))
    if (
        invoked_interpreter.parent != expected_bin
        or re.fullmatch(r"python(?:3(?:\.\d+)*)?", invoked_interpreter.name) is None
    ):
        raise ImageContractRefusal(
            f"this bootstrap is running under {interpreter}, not a repository virtual environment "
            f"interpreter below {expected_bin}; `uv sync` fills that environment "
            "and every later step -- "
            "the chair cache, the vLLM launch, the installed-version check -- reads the "
            "interpreter it is running under, so a system python gets as far as "
            "PREFLIGHT and then fails on a missing pin, after the download has been paid "
            "for. The image must start this process from the repository's own .venv"
        )
    venv_marker = expected_venv / "pyvenv.cfg"
    try:
        marker_text = venv_marker.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        raise ImageContractRefusal(
            f"the repository virtual environment at {expected_venv} does not carry a readable "
            "pyvenv.cfg; a path named .venv is not enough to establish that the invoked "
            "interpreter receives that environment's installed packages"
        ) from error
    marker_entries: dict[str, str] = {}
    for line in marker_text.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            marker_entries[key.strip().lower()] = value.strip()
    if not marker_entries.get("home"):
        raise ImageContractRefusal(
            f"the repository virtual environment at {expected_venv} has a pyvenv.cfg without "
            "a base Python home; a malformed marker cannot establish a usable virtual environment"
        )
    if interpreter_prefix is None and invoked_interpreter == Path(os.path.abspath(sys.executable)):
        interpreter_prefix = Path(sys.prefix)
    if interpreter_prefix is not None:
        observed_prefix = Path(os.path.abspath(interpreter_prefix))
        if observed_prefix != expected_venv:
            raise ImageContractRefusal(
                f"this bootstrap is running under {interpreter}, but reports sys.prefix "
                f"{interpreter_prefix} instead of its repository virtual environment "
                f"{expected_venv}; the image must start the process through .venv/bin/python"
            )
    verified["interpreter"] = str(interpreter)
    return verified


def _read_pointer_or_refuse(path: Path, repository: Path) -> str:
    """One git pointer file, read as an image fact or refused as one.

    An unreadable pointer file (for example the `.git` file of a linked
    worktree) is refused as an `ImageContractRefusal`, which `checkout_commit`
    catches, so the operator gets the named refusal and its remedy instead of
    a bare `OSError`.
    """

    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        raise ImageContractRefusal(
            f"the checkout at {repository} carries a git pointer file this process cannot "
            f"read ({path}: {error.strerror or error}); the bootstrap reads it to find the "
            "configuration that proves there is an origin to fetch from"
        ) from error


def _git_config_path(repository: Path) -> Path | None:
    """The config file that governs ``repository``, following a worktree pointer.

    A linked worktree's ``.git`` is a file naming its git directory, and the
    shared config lives in the common directory that git directory points at.
    Read directly rather than through ``git config``, so this costs no
    subprocess and stays honest about reading only what a HOME-less git would.
    """

    marker = repository / ".git"
    if marker.is_dir():
        return marker / "config"
    if not marker.is_file():
        return None
    text = _read_pointer_or_refuse(marker, repository).strip()
    if not text.startswith("gitdir:"):
        return None
    git_dir = Path(text.split(":", 1)[1].strip())
    if not git_dir.is_absolute():
        git_dir = (repository / git_dir).resolve()
    common = git_dir / "commondir"
    if common.is_file():
        relative = _read_pointer_or_refuse(common, repository).strip()
        if relative:
            candidate = Path(relative)
            git_dir = candidate if candidate.is_absolute() else (git_dir / candidate).resolve()
    return git_dir / "config"


def _git_config_entries(
    config_path: Path, environment: Mapping[str, str], repository: Path, git_executable: str
) -> list[tuple[str, str]]:
    """Ask Git to parse only the checkout config, preserving every key and value."""

    try:
        result = subprocess.run(
            [git_executable, "config", "--file", str(config_path), "--no-includes", "--list", "-z"],
            env=dict(environment),
            capture_output=True,
            check=False,
            timeout=GIT_COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ImageContractRefusal(
            f"the checkout at {repository} config could not be inspected by git; "
            "repair the image's git installation or config"
        ) from error
    if result.returncode != 0:
        raise ImageContractRefusal(
            f"the checkout at {repository} has git config that git cannot parse; repair it "
            "before bootstrap"
        )
    entries = []
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        key, separator, value = record.partition(b"\n")
        if not separator:
            value = b"true"
        entries.append(
            (key.decode("utf-8", errors="replace"), value.decode("utf-8", errors="replace"))
        )
    return entries


class BootstrapStep(StrEnum):
    """Named, resumable steps; completion is recorded only after its action returns."""

    REPOSITORY = "repository"
    CONFIGURATION = "configuration"
    CUDA_COMPAT = "cuda-compat"
    UV_ENVIRONMENT = "uv-environment"
    TRANSFER = "transfer"
    MODEL_STORE = "model-store"
    # Plans each Hugging Face chair's store source and places each local-repository
    # chair's verified bundle where the roster binds it.
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

    def validate_configuration(self) -> dict[str, object]:
        """Validate the checked-out roster and catalogue before paid setup."""

    def configure_cuda_compat(self) -> dict[str, object]:
        """Detect the card and driver, then enable forward compatibility if needed."""

    def sync_uv_environment(self, lockfile: Path) -> dict[str, object]:
        """Build the environment from the existing lockfile without resolution drift."""

    def resume_transfer(self) -> dict[str, object]:
        """Resume checksum-aware transfer, or raise a named partial-transfer failure."""

    def materialize_model_store(self) -> dict[str, object]:
        """Fetch real pinned snapshots and publish their measured store evidence."""

    def verify_chair_cache(self) -> dict[str, object]:
        """Plan each configured chair's pinned source without filling its cache."""

    def run_preflight(self) -> dict[str, object]:
        """Return one green preflight receipt or raise with its named red reason."""


class BootstrapJournal:
    """Durable bootstrap progress.  A crash leaves the current step incomplete.

    The journal sits on a network volume that outlives the pod, so a second pod
    can be handed the same path.  Its completed steps -- the CUDA receipt above
    all -- describe the first pod's GPU and disk, not the second's, and resuming
    them would fail.  So, when ``pod_id`` is known, the
    journal records it, and a journal written by any other pod (or by one that
    did not record its id) is set aside under a pod-named sibling and a fresh
    one started.  The same pod resumes where it stopped.
    """

    def __init__(
        self,
        path: str | Path,
        plan: BootstrapPlan,
        *,
        now: Callable[[], datetime] = utc_now,
        pod_id: str | None = None,
    ) -> None:
        self.path = Path(path)
        self.plan = plan
        self.now = now
        self.pod_id = pod_id

    def load_or_create(self) -> dict[str, object]:
        if not self.path.exists():
            return self._create()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise BootstrapStepFailure(
                BootstrapStep.REPOSITORY,
                f"bootstrap journal cannot be read: {error}",
                "Preserve the broken journal for review, then start a new explicit bootstrap.",
            ) from error
        self._validate(raw)
        if self.pod_id is not None and raw.get("pod_id") != self.pod_id:
            aside = self._set_aside(raw.get("pod_id"))
            print(
                f"bootstrap journal {self.path} was written by pod {raw.get('pod_id')!r}, not "
                f"this pod {self.pod_id!r}; kept as {aside} and starting a fresh journal",
                file=sys.stderr,
            )
            return self._create()
        if raw["repository_commit"] != self.plan.repository_commit or raw["lockfile"] != str(
            self.plan.lockfile
        ):
            raise BootstrapStepFailure(
                BootstrapStep.REPOSITORY,
                "existing bootstrap journal names different pinned inputs",
                "Do not reuse this journal for another commit or lockfile; create a new run directory.",
            )
        return raw

    def _create(self) -> dict[str, object]:
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
        if self.pod_id is not None:
            record["pod_id"] = self.pod_id
        self._write(record)
        return record

    def _set_aside(self, recorded: object) -> Path:
        """Rename another pod's journal to a free sibling; never overwrite or delete it."""

        label = recorded if isinstance(recorded, str) and recorded else "unrecorded"
        stem, suffix = self.path.stem, self.path.suffix
        for attempt in range(1, 1000):
            extra = "" if attempt == 1 else f"-{attempt}"
            aside = self.path.with_name(f"{stem}.pod-{label}{extra}{suffix}")
            if aside.exists():
                continue
            self.path.rename(aside)
            return aside
        raise BootstrapStepFailure(
            BootstrapStep.REPOSITORY,
            f"no free name to set aside the other pod's journal {self.path}",
            "Move the old journals aside by hand, then rerun.",
        )

    def mark_complete(
        self, record: dict[str, object], step: BootstrapStep, receipt: dict[str, object]
    ) -> None:
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

    def mark_cuda_recheck(self, record: dict[str, object], receipt: dict[str, object]) -> None:
        original = record["receipts"][BootstrapStep.CUDA_COMPAT.value]
        original.setdefault("rechecks", []).append({"at": _stamp(self.now()), **receipt})
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
        if not required <= set(raw) <= required | {"pod_id"} or not isinstance(
            raw.get("pod_id", ""), str
        ):
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
        completed = raw["completed"]
        expected_prefix = [step.value for step in ORDERED_STEPS[: len(completed)]]
        if completed != expected_prefix:
            raise BootstrapStepFailure(
                BootstrapStep.REPOSITORY,
                "bootstrap journal completion is duplicated, reordered, or skips a step",
                "Preserve it for review and create a new explicit bootstrap journal.",
            )
        if not isinstance(raw["receipts"], dict) or raw["status"] not in {
            "running",
            "green",
            "red",
        }:
            raise BootstrapStepFailure(
                BootstrapStep.REPOSITORY,
                "bootstrap journal state is invalid",
                "Preserve it for review and create a new explicit bootstrap journal.",
            )
        if set(raw["receipts"]) != set(completed) or not all(
            isinstance(receipt, dict) for receipt in raw["receipts"].values()
        ):
            raise BootstrapStepFailure(
                BootstrapStep.REPOSITORY,
                "bootstrap journal receipts do not reconcile with completed steps",
                "Preserve it for review and create a new explicit bootstrap journal.",
            )
        if raw["status"] == "green" and (
            completed != [step.value for step in ORDERED_STEPS] or raw["failure"] is not None
        ):
            raise BootstrapStepFailure(
                BootstrapStep.REPOSITORY,
                "green bootstrap journal does not account for every step without failure",
                "Preserve it for review and rerun the missing bootstrap work.",
            )

    def _write(self, record: dict[str, object]) -> None:
        atomic_write(self.path, canonical_json(record))


class Bootstrapper:
    """Revalidate configuration, then run only unfinished effectful steps."""

    def __init__(
        self,
        journal: BootstrapJournal,
        actions: BootstrapActions,
        *,
        environment: MutableMapping[str, str] | None = None,
    ) -> None:
        # Static typing is not a repository gate, so structural fixtures must
        # prove every Protocol effect at construction too.
        missing = sorted(
            name
            for name, member in vars(BootstrapActions).items()
            if not name.startswith("_")
            and callable(member)
            and not callable(getattr(actions, name, None))
        )
        if missing:
            raise TypeError(
                f"bootstrap actions {type(actions).__name__} lacks callable Protocol methods: "
                f"{missing}"
            )
        self.journal = journal
        self.actions = actions
        self.environment = os.environ if environment is None else environment

    def run(self) -> BootstrapReport:
        record = self.journal.load_or_create()
        completed = set(record["completed"])
        if BootstrapStep.CONFIGURATION.value in completed:
            failure = self._revalidate_completed_configuration(record)
            if failure is not None:
                # Keep the original completed receipt intact. It is the evidence
                # of what the paid steps ran under, not a slot for the new
                # selection to overwrite during a failed resume.
                self.journal.mark_failure(record, failure)
                return _report_from_record(record)
        if BootstrapStep.CUDA_COMPAT.value in completed:
            try:
                current = self.actions.configure_cuda_compat()
                recorded = record["receipts"][BootstrapStep.CUDA_COMPAT.value]
                if any(
                    current.get(key) != recorded.get(key)
                    for key in ("driver", "gpus", "compat_path")
                ):
                    raise BootstrapStepFailure(
                        BootstrapStep.CUDA_COMPAT,
                        "GPU or driver differs from the completed CUDA compatibility receipt",
                        "Start a new bootstrap journal for this host.",
                    )
                _apply_cuda_compat(current, self.environment)
                self.journal.mark_cuda_recheck(record, current)
            except BootstrapStepFailure as failure:
                self.journal.mark_failure(record, failure)
                return _report_from_record(record)
            except Exception as error:
                failure = BootstrapStepFailure(
                    BootstrapStep.CUDA_COMPAT,
                    f"completed CUDA compatibility could not be rechecked: {error}",
                    "Inspect the pod GPU and compatibility package, then resume this journal.",
                )
                self.journal.mark_failure(record, failure)
                return _report_from_record(record)
        if record["status"] == "green":
            return _report_from_record(record)
        for step in ORDERED_STEPS:
            if step.value in completed:
                continue
            try:
                receipt = self._execute(step)
            except BootstrapStepFailure as failure:
                self.journal.mark_failure(record, failure)
                return _report_from_record(record)
            except ChairRefusal as refusal:
                failure = BootstrapStepFailure(
                    step,
                    f"security refusal {refusal.code}: {refusal}",
                    "Correct the named chair evidence mismatch, then resume this same "
                    "journal; do not substitute an artifact, revision, or refusal.",
                )
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
            if step is BootstrapStep.CUDA_COMPAT:
                _apply_cuda_compat(receipt, self.environment)
        self.journal.mark_green(record)
        return _report_from_record(record)

    def _revalidate_completed_configuration(
        self, record: dict[str, object]
    ) -> BootstrapStepFailure | None:
        """Re-read the cheap binding before skipping completed or green work."""

        remediation = (
            "Restore the roster, serving catalogue, and placement selection "
            "recorded by this journal, or preserve the journal and start a new one for the "
            "new selection. A completed configuration receipt is never rewritten."
        )
        try:
            current = self.actions.validate_configuration()
        except BootstrapStepFailure as error:
            return BootstrapStepFailure(
                BootstrapStep.CONFIGURATION,
                f"completed configuration could not be revalidated: {error.detail}",
                remediation,
            )
        except ChairRefusal as error:
            return BootstrapStepFailure(
                BootstrapStep.CONFIGURATION,
                f"completed configuration security refusal {error.code}: {error}",
                remediation,
            )
        except Exception as error:
            return BootstrapStepFailure(
                BootstrapStep.CONFIGURATION,
                f"completed configuration could not be revalidated: {error}",
                remediation,
            )

        receipts = record["receipts"]
        if not isinstance(receipts, dict):  # already guarded by journal validation
            recorded = None
        else:
            recorded = receipts.get(BootstrapStep.CONFIGURATION.value)
        if isinstance(recorded, dict) and recorded.get("schema") == _RAW_BYTE_RECEIPT_SCHEMA:
            return BootstrapStepFailure(
                BootstrapStep.CONFIGURATION,
                f"this journal predates seal method v2: its configuration receipt is "
                f"{_RAW_BYTE_RECEIPT_SCHEMA!r}, which bound raw file bytes, so no current "
                "seal can be compared with it",
                f"Start a new journal: move {self.journal.path} aside and rerun boot. A "
                "completed configuration receipt is never rewritten.",
            )
        if isinstance(recorded, dict) and recorded.get("schema") != CONFIGURATION_RECEIPT_SCHEMA:
            return BootstrapStepFailure(
                BootstrapStep.CONFIGURATION,
                f"this journal's configuration receipt is {recorded.get('schema')!r}, not "
                f"{CONFIGURATION_RECEIPT_SCHEMA!r}, so it cannot be compared with the current "
                "selection",
                f"Start a new journal: move {self.journal.path} aside and rerun boot. A "
                "completed configuration receipt is never rewritten.",
            )
        current_problem = _configuration_receipt_problem(current)
        recorded_problem = _configuration_receipt_problem(recorded)
        if current_problem is not None or recorded_problem is not None:
            problem = current_problem or recorded_problem
            source = "current" if current_problem is not None else "completed"
            return BootstrapStepFailure(
                BootstrapStep.CONFIGURATION,
                f"{source} configuration receipt lacks the required binding: {problem}",
                remediation,
            )
        if current != recorded:
            return BootstrapStepFailure(
                BootstrapStep.CONFIGURATION,
                "current configuration paths or source digests differ from the completed "
                "configuration receipt, or that receipt lacks the required binding",
                remediation,
            )
        return None

    def _execute(self, step: BootstrapStep) -> dict[str, object]:
        if step is BootstrapStep.REPOSITORY:
            return self.actions.checkout_commit(self.journal.plan.repository_commit)
        if step is BootstrapStep.CONFIGURATION:
            receipt = self.actions.validate_configuration()
            problem = _configuration_receipt_problem(receipt)
            if problem is not None:
                raise BootstrapStepFailure(
                    BootstrapStep.CONFIGURATION,
                    f"configuration receipt lacks the required binding: {problem}",
                    "Correct the configuration validation action before any paid step runs.",
                )
            return receipt
        if step is BootstrapStep.CUDA_COMPAT:
            return self.actions.configure_cuda_compat()
        if step is BootstrapStep.UV_ENVIRONMENT:
            return self.actions.sync_uv_environment(self.journal.plan.lockfile)
        if step is BootstrapStep.TRANSFER:
            return self.actions.resume_transfer()
        if step is BootstrapStep.MODEL_STORE:
            return self.actions.materialize_model_store()
        if step is BootstrapStep.CHAIR_CACHE:
            return self.actions.verify_chair_cache()
        if step is BootstrapStep.PREFLIGHT:
            return self.actions.run_preflight()
        raise AssertionError(f"unhandled bootstrap step {step}")  # pragma: no cover


def _configuration_receipt_problem(receipt: object) -> str | None:
    """Return why a receipt cannot bind a resume, without reading any selected file."""

    if not isinstance(receipt, dict):
        return "receipt is not an object"
    required = {"schema", "bindings"}
    if set(receipt) != required or receipt.get("schema") != CONFIGURATION_RECEIPT_SCHEMA:
        return f"receipt must be a closed {CONFIGURATION_RECEIPT_SCHEMA!r} object"
    bindings = receipt.get("bindings")
    if not isinstance(bindings, dict) or set(bindings) != _CONFIGURATION_BINDINGS:
        return "receipt does not name all three selected configuration sources"
    for name in sorted(_CONFIGURATION_BINDINGS):
        binding = bindings[name]
        if not isinstance(binding, dict) or set(binding) != {"path", "sha256"}:
            return f"{name!r} must have only path and sha256"
        path = binding.get("path")
        digest = binding.get("sha256")
        if not isinstance(path, str) or not path.strip():
            return f"{name!r} path is not a non-blank string"
        if not is_sha256(digest):
            return f"{name!r} sha256 is not a lowercase digest"
    return None


class ChairCachePlan(Protocol):
    def verify(self) -> dict[str, object]: ...


class ChairCachePrefillPlan(Protocol):
    """A background fill of the chair cache (`chair_prefill.ChairCachePrefill`)."""

    def start(self, step: str, reserved: Mapping[int, int] | None = None) -> None: ...

    def wait(self, step: BootstrapStep) -> dict[str, object] | None: ...


class ModelStoreBootstrapAction:
    """Launch-time acquisition of the real roster onto the mounted model volume."""

    def __init__(
        self,
        store_root: str | Path,
        fetcher: MaterializationFetcher,
        bundle_fetcher: BundleFetcher,
        *,
        roles: tuple[str, ...] | None = None,
        hashed_at_copy: tuple[str, ...] = (),
    ) -> None:
        self.store_root = Path(store_root)
        self.fetcher = fetcher
        self.bundle_fetcher = bundle_fetcher
        # The chairs this pod's selected stages use; None for every chair.
        self.roles = roles
        # Roles whose store bytes CHAIR_CACHE or PREFLIGHT copy into the chair
        # cache, hashing them against the same pinned manifest as they copy.
        self.hashed_at_copy = hashed_at_copy

    def materialize(self) -> dict[str, object]:
        return materialize_real_roster(
            self.store_root,
            self.fetcher,
            self.bundle_fetcher,
            roles=self.roles,
            hashed_at_copy=self.hashed_at_copy,
        )


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
        configuration: Callable[[], dict[str, object]],
        materialize_model_store: Callable[[], dict[str, object]],
        cache: ChairCachePlan,
        preflight: Callable[[], dict[str, object]],
        runner: Callable[[list[str], Path], subprocess.CompletedProcess[str]] | None = None,
        executables: Mapping[str, str] = BOOTSTRAP_EXECUTABLES,
        environment: Mapping[str, str] = BOOTSTRAP_ENVIRONMENT,
        image_contract: Callable[[], dict[str, object]] | None = None,
        free_bytes: Callable[[Path], int] | None = None,
        subprocess_environments: Callable[[], frozenset[str]] = frozenset,
        local_bundles: Callable[[], Mapping[Path, int]] = dict,
        prefill: ChairCachePrefillPlan | None = None,
    ) -> None:
        self.repository = Path(repository)
        # Copies the selected chairs into the chair cache while uv syncs;
        # MODEL_STORE and CHAIR_CACHE wait for it before they complete.
        self.prefill = prefill
        # Read after checkout, like the roster the stages read: the environments
        # the checked-out catalogue's subprocess rows run in, for configured chairs.
        self.subprocess_environments = subprocess_environments
        # Also read after checkout: where CHAIR_CACHE will copy each
        # local-repository chair on container-local disk, and its manifest's bytes.
        self.local_bundles = local_bundles
        self.transfer = transfer
        self.configuration = configuration
        self.materialize = materialize_model_store
        self.cache = cache
        self.preflight = preflight
        if set(executables) != set(BOOTSTRAP_EXECUTABLES) or any(
            not isinstance(value, str) or not Path(value).is_absolute()
            for value in executables.values()
        ):
            raise ValueError(
                "bootstrap executables must give absolute paths for every bootstrap command"
            )
        if any(
            not isinstance(key, str)
            or not key
            or "=" in key
            or "\x00" in key
            or not isinstance(value, str)
            or "\x00" in value
            for key, value in environment.items()
        ):
            raise ValueError("bootstrap environment must contain valid explicit text entries")
        self.executables = dict(executables)
        self.environment = dict(environment)
        self.runner = runner or self._run
        # The tracked composition (`bootstrap_main.build_actions`) always
        # supplies the image-contract check; it is optional here because this
        # constructor is also driven directly, with a synthetic repository and
        # an injected runner, to prove the command shape and the explicit
        # environment. Those callers are asserting something else and have no
        # image to hold to a contract.
        self.image_contract = image_contract
        self.free_bytes = free_bytes or _free_bytes

    def checkout_commit(self, commit: str) -> dict[str, object]:
        contract: dict[str, object] | None = None
        if self.image_contract is not None:
            try:
                contract = self.image_contract()
            except ImageContractRefusal as refusal:
                raise BootstrapStepFailure(
                    BootstrapStep.REPOSITORY,
                    f"pod image contract: {refusal}",
                    "Repair the image (or the request's --repository) to meet the image "
                    "contract in operations/pod/README.md, then boot again; nothing here "
                    "can be fetched or installed around it.",
                ) from refusal
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
        receipt: dict[str, object] = {"commit": observed}
        if contract is not None:
            receipt["image_contract"] = contract
        return receipt

    def validate_configuration(self) -> dict[str, object]:
        return self.configuration()

    def configure_cuda_compat(self) -> dict[str, object]:
        result = self._command(
            ["nvidia-smi", "--query-gpu=driver_version,name", "--format=csv,noheader"],
            BootstrapStep.CUDA_COMPAT,
        )
        cards = [
            [field.strip() for field in line.split(",", 1)]
            for line in result.stdout.strip().splitlines()
        ]
        if not cards or any(len(card) != 2 or not card[0] or not card[1] for card in cards):
            raise BootstrapStepFailure(
                BootstrapStep.CUDA_COMPAT,
                "nvidia-smi did not report a driver and GPU name",
                "Check the pod GPU and NVIDIA driver before booting again.",
            )
        drivers = {card[0] for card in cards}
        if len(drivers) != 1 or not all(
            re.fullmatch(r"\d+\.\d+\.\d+", driver) for driver in drivers
        ):
            raise BootstrapStepFailure(
                BootstrapStep.CUDA_COMPAT,
                "nvidia-smi reported inconsistent or invalid driver versions",
                "Check the pod driver before booting again.",
            )
        driver = drivers.pop()
        gpus = [card[1] for card in cards]
        receipt: dict[str, object] = {
            "driver": driver,
            "gpus": gpus,
            "compat_path": None,
            "action": "not-needed",
            "package": None,
        }
        if tuple(int(part) for part in driver.split(".")) >= CUDA_13_MIN_DRIVER:
            receipt["cuda_devices"] = _initialise_cuda(CUDA_DRIVER_LIBRARY, driver, gpus)
            return receipt
        if any("GeForce" in name for name in gpus):
            raise BootstrapStepFailure(
                BootstrapStep.CUDA_COMPAT,
                f"driver {driver} is below 580.65.06 and GeForce GPU {gpus} cannot use CUDA forward compatibility",
                "Use a driver at least 580.65.06 or a supported professional RTX/data-center card.",
            )
        if not Path(CUDA_COMPAT_PATH).is_dir():
            available = self._command(
                ["apt-cache", "policy", CUDA_COMPAT_PACKAGE], BootstrapStep.CUDA_COMPAT
            ).stdout
            candidate = re.search(r"^\s*Candidate:\s*(\S+)\s*$", available, re.MULTILINE)
            if candidate is None or candidate.group(1) == "(none)":
                raise BootstrapStepFailure(
                    BootstrapStep.CUDA_COMPAT,
                    f"apt lists empty or package absent for {CUDA_COMPAT_PACKAGE}",
                    "Supply image apt lists with the NVIDIA CUDA repository and resume.",
                )
            if not re.search(
                rf"^\s+{re.escape(CUDA_COMPAT_VERSION)}\s+\d+\s*$", available, re.MULTILINE
            ):
                raise BootstrapStepFailure(
                    BootstrapStep.CUDA_COMPAT,
                    f"apt does not offer pinned {CUDA_COMPAT_PACKAGE}={CUDA_COMPAT_VERSION}",
                    "Supply the pinned NVIDIA CUDA package in the image apt repository and resume.",
                )
            self._command(
                ["apt-get", "install", "-y", f"{CUDA_COMPAT_PACKAGE}={CUDA_COMPAT_VERSION}"],
                BootstrapStep.CUDA_COMPAT,
            )
            receipt["action"] = "installed"
        else:
            receipt["action"] = "already-present"
        installed_version = self._command(
            ["dpkg-query", "-W", "-f=${Version}", CUDA_COMPAT_PACKAGE], BootstrapStep.CUDA_COMPAT
        ).stdout.strip()
        receipt["package"] = CUDA_COMPAT_PACKAGE
        receipt["installed_version"] = installed_version
        if installed_version != CUDA_COMPAT_VERSION:
            raise BootstrapStepFailure(
                BootstrapStep.CUDA_COMPAT,
                f"installed {CUDA_COMPAT_PACKAGE} version {installed_version!r} differs from pinned {CUDA_COMPAT_VERSION}",
                "Install the pinned CUDA compatibility package and resume this journal.",
            )
        receipt["cuda_devices"] = _initialise_cuda(
            f"{CUDA_COMPAT_PATH}/{CUDA_DRIVER_LIBRARY}", driver, gpus
        )
        receipt["compat_path"] = CUDA_COMPAT_PATH
        return receipt

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
        environments = sorted(self.subprocess_environments())
        unknown = [
            name for name in environments if name not in SUBPROCESS_ENVIRONMENT_REQUIRED_BYTES
        ]
        if unknown:
            raise BootstrapStepFailure(
                BootstrapStep.UV_ENVIRONMENT,
                f"subprocess environment(s) {unknown} have no disk bound in this bootstrap",
                "Measure the environment and add its bound before syncing it on a pod.",
            )
        lockfiles = {name: self.repository / name / "uv.lock" for name in environments}
        for name, environment_lockfile in lockfiles.items():
            if not environment_lockfile.is_file():
                raise BootstrapStepFailure(
                    BootstrapStep.UV_ENVIRONMENT,
                    f"the lockfile of subprocess environment {name}, {environment_lockfile}, "
                    "is missing",
                    "Restore the pinned checkout; a subprocess environment is synced from its "
                    "own uv.lock.",
                )
        reserved = self._require_container_disk(environments)
        if self.prefill is not None:
            self.prefill.start(BootstrapStep.UV_ENVIRONMENT.value, reserved)
        # `--group pod` is the serving stack: vLLM, transformers, qwen-vl-utils and
        # everything they drag in, including torch and the CUDA libraries. It is
        # named here and nowhere else, because the pod is the only machine that may
        # install it -- the group's Linux/x86_64 markers make a laptop sync resolve
        # none of it. Without this flag `ServingManager` refuses every real chair on
        # a missing pin, after the pod has already billed for the boot.
        #
        # The cost is real and lands here: on the order of ten gigabytes of wheels,
        # downloaded on the billing card into the container-local `UV_CACHE_DIR`
        # above, paid once per pod because that cache does not survive one.
        # `--locked` alone: uv's own CLI refuses `--locked` and `--frozen`
        # together (`conflicts_with_all` on `--locked` in the pinned uv
        # 0.12.1), so passing both would fail the sync -- billing the pod for
        # the failure -- before a single wheel downloaded. `--locked` still
        # gets the property this step wants: uv refuses to run if uv.lock is
        # out of date.
        self._command(
            ["uv", "sync", "--locked", "--group", "pod"],
            BootstrapStep.UV_ENVIRONMENT,
        )
        # Each subprocess environment, from its own committed lock, so preflight
        # finds the interpreter its row runs.
        for name in environments:
            self._command(
                ["uv", "sync", "--locked", "--project", name],
                BootstrapStep.UV_ENVIRONMENT,
            )
        return {
            "lockfile": str(supplied_lockfile),
            "sha256": hashlib.sha256(supplied_lockfile.read_bytes()).hexdigest(),
            "mode": "locked",
            "groups": ["pod"],
            "subprocess_environments": [
                {
                    "environment": name,
                    "lockfile": str(lockfiles[name]),
                    "sha256": hashlib.sha256(lockfiles[name].read_bytes()).hexdigest(),
                }
                for name in environments
            ],
        }

    def _require_container_disk(self, environments: list[str]) -> dict[int, int]:
        """Refuse a sync the container-local disk cannot hold, before it starts.

        The create request states a container disk size, but nothing proves the
        pod got one: it is a documented field. What the pod *can* do is read the
        free space actually under the directories the sync fills -- the wheel
        cache and the venv, and those of each subprocess environment -- and
        under each place CHAIR_CACHE later copies a local-repository chair, and
        say so in one sentence naming both figures. Without this the
        failure is uv's own ENOSPC part way through a ten-gigabyte download
        that was already paid for, which must never happen silently: a
        cost with nothing to show and no named reason.

        Returns the bytes required per filesystem (`st_dev`), which a chair-cache
        prefill running beside the sync must leave free.

        Measured on the container-local disk deliberately. The preflight's GPU
        probe measures ``disk_path=volume_mount_path`` -- the network volume --
        which is a different filesystem and says nothing about this one.
        """

        cache_directory = self.environment.get("UV_CACHE_DIR")
        venv_path = self.repository / REPOSITORY_VENV_DIRECTORY
        wanted: dict[Path, int] = {}
        if cache_directory:
            wanted[Path(cache_directory)] = UV_CACHE_REQUIRED_BYTES
        # No UV_CACHE_DIR means uv infers its cache from HOME or XDG_CACHE_HOME;
        # this environment supplies neither, so wherever it lands is somewhere
        # this check cannot name. The venv is still checked, and it is the
        # larger of the two.
        wanted[venv_path] = wanted.get(venv_path, 0) + REPOSITORY_VENV_REQUIRED_BYTES
        for name in environments:
            bound = SUBPROCESS_ENVIRONMENT_REQUIRED_BYTES[name]
            if cache_directory:
                wanted[Path(cache_directory)] += bound["uv_cache"]
            environment_venv = self.repository / name / REPOSITORY_VENV_DIRECTORY
            wanted[environment_venv] = wanted.get(environment_venv, 0) + bound["venv"]
        for bundle, size in self.local_bundles().items():
            wanted[bundle] = wanted.get(bundle, 0) + size
        shared: dict[int, int] = {}
        for path, required in wanted.items():
            try:
                free = self.free_bytes(path)
                key = _filesystem_key(path)
            except OSError as error:
                raise BootstrapStepFailure(
                    BootstrapStep.UV_ENVIRONMENT,
                    f"free space under {path} could not be read: {error}",
                    "Give the pod a container disk this bootstrap can measure; the "
                    "serving stack is installed onto container-local disk twice over.",
                ) from error
            # Two directories on one filesystem share its free space, so their
            # requirements add rather than each passing on the same bytes.
            shared[key] = shared.get(key, 0) + required
            if free < shared[key]:
                raise BootstrapStepFailure(
                    BootstrapStep.UV_ENVIRONMENT,
                    f"container-local disk is too small for the serving stack: "
                    f"{_gib(free)} GiB free under {path}, and this sync needs about "
                    f"{_gib(shared[key])} GiB there (the wheel cache and the installed "
                    f"{REPOSITORY_VENV_DIRECTORY}, and those of any subprocess environment, "
                    "are two copies of it, beside any bundle CHAIR_CACHE copies)",
                    "Create the pod with a larger container disk (container_disk_gb in "
                    "the pod request) and boot again; uv would otherwise fill this disk "
                    "part way through the download and fail with no space left.",
                )
        return shared

    def resume_transfer(self) -> dict[str, object]:
        return self.transfer()

    def materialize_model_store(self) -> dict[str, object]:
        if self.prefill is not None:
            # On a resume past UV_ENVIRONMENT the sync has already written its bytes.
            self.prefill.start(BootstrapStep.MODEL_STORE.value)
        result = self.materialize()
        if result.get("selection_complete") is not True:
            raise BootstrapStepFailure(
                BootstrapStep.MODEL_STORE,
                "model-store materialization did not verify every repository this pod's "
                "selected chairs need",
                "Resume the same pinned materialization; do not advance to chair-source "
                "planning while a real-roster artifact is absent or unverified.",
            )
        if self.prefill is not None:
            # A copy that refused (a store byte differing from its pin) fails this
            # step, whose receipt says those bytes are verified at copy.
            self.prefill.wait(BootstrapStep.MODEL_STORE)
        return result

    def verify_chair_cache(self) -> dict[str, object]:
        prefilled = (
            self.prefill.wait(BootstrapStep.CHAIR_CACHE) if self.prefill is not None else None
        )
        receipt = self.cache.verify()
        if prefilled is not None:
            receipt = {**receipt, "prefill": prefilled}
        return receipt

    def run_preflight(self) -> dict[str, object]:
        result = self.preflight()
        if result.get("color") != "green":
            raise BootstrapStepFailure(
                BootstrapStep.PREFLIGHT,
                "preflight returned red: "
                + json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=True),
                "Read the preflight issues and repair the named environment, chair, or smoke-read failure.",
            )
        return result

    def _command(self, argv: list[str], step: BootstrapStep) -> subprocess.CompletedProcess[str]:
        executable = self.executables.get(argv[0])
        if executable is None:
            raise BootstrapStepFailure(
                step,
                f"no trusted executable is configured for command {argv[0]!r}",
                "Configure the exact absolute bootstrap executable; PATH lookup is not used.",
            )
        command = [executable, *argv[1:]]
        try:
            result = self.runner(command, self.repository)
        except OSError as error:
            raise BootstrapStepFailure(
                step,
                f"command {argv[0]!r} could not start from {executable!r}: {error}",
                "Repair the exact trusted bootstrap executable and resume.",
            ) from error
        except subprocess.TimeoutExpired as error:
            raise BootstrapStepFailure(
                step,
                f"command {argv[0]!r} timed out after {error.timeout} seconds",
                "Repair the named bootstrap dependency and resume.",
            ) from error
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
            raise BootstrapStepFailure(
                step,
                f"command {argv[0]!r} failed: {detail}",
                "Repair the named bootstrap dependency and resume; no fallback checkout or unlocked environment is used.",
            )
        return result

    def _run(self, argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        timeout = (
            30
            if argv[0].endswith(("/nvidia-smi", "/dpkg-query"))
            else 300
            if argv[0].endswith(("/apt-cache", "/apt-get"))
            else GIT_COMMAND_TIMEOUT_SECONDS
            if argv[0] == self.executables["git"]
            else 3600
            if argv[0] == self.executables["uv"]
            else None
        )
        return subprocess.run(
            argv,
            cwd=cwd,
            env=self.environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )


def probe_cuda(library: str) -> dict[str, object]:
    """Load `library`, call cuInit(0) and cuDeviceGetCount, and say how far it got.

    ``failure`` is None on success, ``"library"`` when the library or one of its calls is
    missing, ``"init"`` or ``"count"`` when the call returned the nonzero ``code``.
    Runs in its own process (``_run_cuda_probe``), since a driver call can hang.
    """

    try:
        cuda = ctypes.CDLL(library)
        cuda.cuInit.argtypes = [ctypes.c_uint]
        cuda.cuInit.restype = ctypes.c_int
        cuda.cuDeviceGetCount.argtypes = [ctypes.POINTER(ctypes.c_int)]
        cuda.cuDeviceGetCount.restype = ctypes.c_int
        result = cuda.cuInit(0)
        if result != 0:
            return {"failure": "init", "code": result}
        count = ctypes.c_int(0)
        result = cuda.cuDeviceGetCount(ctypes.pointer(count))
    except (OSError, AttributeError) as error:
        return {"failure": "library", "error": str(error)}
    if result != 0:
        return {"failure": "count", "code": result}
    return {"failure": None, "devices": count.value}


# A healthy cuInit answers in seconds; a driver that has not answered in two minutes is
# hung, and would otherwise hold the pod until its hard deadline.
CUDA_PROBE_TIMEOUT_SECONDS = 120
_CUDA_PROBE_SOURCE = (
    "import json, sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from operations.pod.bootstrap import probe_cuda\n"
    "print(json.dumps(probe_cuda(sys.argv[2])))\n"
)


def _run_cuda_probe(library: str, timeout: float) -> dict[str, object] | None:
    """`probe_cuda` in a child process; None when it did not answer within `timeout`."""

    root = str(Path(__file__).resolve().parents[2])
    child = subprocess.Popen(
        [sys.executable, "-I", "-c", _CUDA_PROBE_SOURCE, root, library],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        out, err = child.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        child.kill()
        try:
            child.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass  # Stuck in the driver: left to the kernel rather than waited on.
        return None
    try:
        result = json.loads(out.strip().splitlines()[-1])
    except (IndexError, ValueError):
        result = None
    if not isinstance(result, dict):
        detail = (err.strip().splitlines() or [f"exit {child.returncode}"])[-1]
        return {"failure": "library", "error": f"the probe process failed: {detail[:300]}"}
    return result


def _initialise_cuda(library: str, driver: str, gpus: list[str]) -> int:
    """Initialise CUDA through `library` the way every GPU stage will, and count its
    devices. A host whose driver lists its cards but cannot initialise CUDA is refused
    here, before the environment install and the model store spend most of an hour; a
    library or call that is missing is the image's fault, not the host's."""

    where = (
        f"host {socket.gethostname()} (driver {driver}, {gpus}) cannot use CUDA through {library}"
    )
    replace_host = "Delete this pod and start one on another host; this one cannot run a GPU stage."
    result = _run_cuda_probe(library, CUDA_PROBE_TIMEOUT_SECONDS)
    if result is None:
        what, remedy = (
            f"cuInit or cuDeviceGetCount did not answer within {CUDA_PROBE_TIMEOUT_SECONDS} s",
            replace_host,
        )
    elif result.get("failure") == "library":
        what, remedy = (
            f"the library could not be loaded or called: {result.get('error')}",
            "Repair the image or the CUDA compatibility package: another host with this "
            "image would fail the same way.",
        )
    elif result.get("failure") == "init":
        what, remedy = f"cuInit(0) failed with code {result.get('code')}", replace_host
    elif result.get("failure") == "count":
        what, remedy = f"cuDeviceGetCount failed with code {result.get('code')}", replace_host
    elif not isinstance(result.get("devices"), int) or result["devices"] < 1:
        what, remedy = "it reports no CUDA device", replace_host
    else:
        return result["devices"]
    raise BootstrapStepFailure(BootstrapStep.CUDA_COMPAT, f"{where}: {what}", remedy)


def _apply_cuda_compat(receipt: dict[str, object], environment: MutableMapping[str, str]) -> None:
    if receipt.get("compat_path") != CUDA_COMPAT_PATH:
        return
    paths = [path for path in environment.get("LD_LIBRARY_PATH", "").split(":") if path]
    environment["LD_LIBRARY_PATH"] = ":".join(
        [CUDA_COMPAT_PATH, *(path for path in paths if path != CUDA_COMPAT_PATH)]
    )


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
    return BootstrapReport(
        "red", completed, dict(record["receipts"]), detail="bootstrap journal is incomplete"
    )


def _stamp(value: datetime) -> str:
    return require_utc(value, "bootstrap timestamp").isoformat().replace("+00:00", "Z")
