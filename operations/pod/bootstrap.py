"""Idempotent, journaled pod bootstrap at one exact repository commit."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Callable, Mapping, Protocol

from common.chairs.errors import ChairRefusal
from common.chairs.model_store import MaterializationFetcher, materialize_real_roster
from common.chairs.models import AbsentChair, ChairIdentity
from common.chairs.registry import ChairRegistry

from .durable import atomic_write, canonical_json
from .models import require_utc, utc_now
from .preflight import is_cache_mismatch

BOOTSTRAP_SCHEMA = "pod-bootstrap.v3"
"""Bumped when ``CONFIGURATION`` was inserted after ``REPOSITORY`` and before
``UV_ENVIRONMENT``. A v2 journal may already call the paid environment/model
steps complete without ever validating the checked-out roster/declaration
pair. It cannot be resumed under the stronger order and is refused by schema.
"""
CONFIGURATION_RECEIPT_SCHEMA = "pod-bootstrap-configuration.v2"
_RAW_BYTE_RECEIPT_SCHEMA = "pod-bootstrap-configuration.v1"
"""The checked-out selections re-read before any completed bootstrap is reused.

v2 binds each file's seal (`common/sealed_config.py`) where v1 bound raw bytes, so
a v1 journal is refused by schema rather than as a changed configuration.
"""
_CONFIGURATION_BINDINGS = {
    "models_config",
    "witness_context_config",
    "serving_recipes_config",
    "placement_config",
}
BOOTSTRAP_EXECUTABLES = {"git": "/usr/bin/git", "uv": "/usr/local/bin/uv"}
BOOTSTRAP_ENVIRONMENT = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    # uv resolves its cache through UV_CACHE_DIR, then XDG_CACHE_HOME, then
    # $HOME. This environment is explicit and supplies none of those, so where
    # `uv sync --locked` cached anything was left to whatever uv could
    # infer from a passwd entry -- on the money path, with the GPU billing while
    # it failed. Naming it makes the dependency visible instead of inferred.
    #
    # `/tmp` because it is the one absolute path writable by whatever user the
    # pod image runs as. The cost is honest and bounded: the cache does not
    # survive a pod, so a fresh pod re-downloads the locked wheels once. It is
    # deliberately not under the checkout, which must stay exactly the pinned
    # commit, and not on the model volume, whose contents are evidence.
    #
    # Unverified against a real pod image, like the spend template's "$50.00":
    # check it on the first live boot.
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
# bounds rather than measurements -- no pod has been booted from this tree -- and
# the first live boot replaces them with what it actually observes. They are
# deliberately checked against *free* space, which is the one fact the pod can
# measure for itself, rather than against the requested container disk, which is
# only what was asked for.
UV_CACHE_REQUIRED_BYTES = 12 * 1024**3
REPOSITORY_VENV_REQUIRED_BYTES = 20 * 1024**3


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

    Four assumptions rode unwritten in this module until a pre-launch review
    read them out of the code (see ``operations/pod/README.md``, "The pod image
    contract"):

    * ``git`` and ``uv`` at exactly the configured absolute paths. Nothing here
      uses PATH lookup, and the default uv installer puts ``uv`` in
      ``~/.local/bin`` rather than ``/usr/local/bin``.
    * ``--repository`` already a checkout with an ``origin`` remote. This module
      *fetches and checks out*; it has never cloned, so an image with no
      checkout in it fails at the first git call with the volume already
      attached and the card already billing.
    * That remote reachable with **no HOME**: ``BOOTSTRAP_ENVIRONMENT`` supplies
      none, so git reads no ``~/.gitconfig``, no global credential helper and no
      ``~/.git-credentials``. Only the repository's own config is visible.
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
    try:
        config_text = config_path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        # Named here rather than escaping as a bare OSError the step turns into
        # an unexplained red: a config this process cannot read is an image
        # fact like every other one on this list.
        raise ImageContractRefusal(
            f"the checkout at {repository} has a configuration this process cannot read "
            f"({error.strerror}); the bootstrap reads it to prove there is an origin to "
            "fetch from, and cannot proceed on the assumption that there is one"
        ) from error
    entries = _git_config_entries(config_text)
    origin = _config_value(entries, "remote", "origin", "url")
    if not origin:
        raise ImageContractRefusal(
            f"the checkout at {repository} names no origin remote; the pinned commit is "
            "fetched from origin, and a checkout with no remote cannot be advanced to it"
        )
    verified["origin_remote"] = "present"

    home_is_visible = bool(environment.get("HOME"))
    scheme = origin.split("://", 1)[0].lower() if "://" in origin else ""
    # Only over http/https does userinfo carry a credential. `user@host` in an
    # SSH remote is a login name, and recording that as an embedded credential
    # would put a false statement in the receipt.
    embedded_credential = (
        scheme in {"http", "https"} and "@" in origin.split("://", 1)[-1].split("/", 1)[0]
    )
    local_credential_route = bool(
        _config_value(entries, "credential", None, "helper")
        or _any_subsection_value(entries, "credential", "helper")
        or _any_subsection_value(entries, "http", "extraheader")
    )
    if (
        scheme in {"http", "https"}
        and not home_is_visible
        and not embedded_credential
        and not local_credential_route
    ):
        raise ImageContractRefusal(
            f"the checkout at {repository} fetches origin over {scheme} and carries no "
            "credential route of its own, while the bootstrap environment supplies no "
            "HOME -- so git will read no ~/.gitconfig, no global credential helper and "
            "no ~/.git-credentials, and a private fetch has nothing to authenticate "
            "with. Put the route in the repository's own config (a credential.helper, "
            "an http.<url>.extraheader, or credentials in the remote URL), or give the "
            "bootstrap environment a HOME whose configuration you have checked"
        )
    # What the receipt may honestly say. The last value is not "no credential is
    # needed" -- an SSH remote's key, or a public remote needing nothing, are
    # both outside what reading one config file can establish -- so it says
    # exactly that instead of a claim this check did not make (principle 8).
    verified["credential_route"] = (
        "embedded-in-url"
        if embedded_credential
        else "repository-local"
        if local_credential_route
        else "home-visible"
        if home_is_visible
        else "not-in-the-repository-config"
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

    The config read in `verify_image_contract` is already wrapped for this
    reason; these two were not, so a pod image whose checkout is a linked
    worktree with a `.git` file this process cannot read raised a bare
    `OSError`. `checkout_commit` catches only `ImageContractRefusal`, so the
    refusal lost its name and its remedy and the operator saw a traceback
    while the card billed.
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


_SECTION = re.compile(r'^\s*\[\s*([A-Za-z0-9.\-]+)\s*(?:"(.*)")?\s*\]\s*$')
_ENTRY = re.compile(r"^\s*([A-Za-z][A-Za-z0-9\-]*)\s*=\s*(.*?)\s*$")


def _git_config_entries(text: str) -> list[tuple[str, str | None, str, str]]:
    """``(section, subsection, key, value)`` for every plain entry in a config."""

    entries: list[tuple[str, str | None, str, str]] = []
    section: str | None = None
    subsection: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        header = _SECTION.match(line)
        if header is not None:
            section = header.group(1).lower()
            subsection = header.group(2)
            continue
        entry = _ENTRY.match(line)
        if entry is not None and section is not None:
            entries.append((section, subsection, entry.group(1).lower(), entry.group(2)))
    return entries


def _config_value(
    entries: list[tuple[str, str | None, str, str]],
    section: str,
    subsection: str | None,
    key: str,
) -> str | None:
    for entry_section, entry_subsection, entry_key, value in entries:
        if entry_section == section and entry_subsection == subsection and entry_key == key:
            return value
    return None


def _any_subsection_value(
    entries: list[tuple[str, str | None, str, str]], section: str, key: str
) -> str | None:
    for entry_section, _subsection, entry_key, value in entries:
        if entry_section == section and entry_key == key and value:
            return value
    return None


class BootstrapStep(StrEnum):
    """Named, resumable steps; completion is recorded only after its action returns."""

    REPOSITORY = "repository"
    CONFIGURATION = "configuration"
    UV_ENVIRONMENT = "uv-environment"
    TRANSFER = "transfer"
    MODEL_STORE = "model-store"
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
        """Validate the checked-out roster and declaration before paid setup."""

    def sync_uv_environment(self, lockfile: Path) -> dict[str, object]:
        """Build the environment from the existing lockfile without resolution drift."""

    def resume_transfer(self) -> dict[str, object]:
        """Resume checksum-aware transfer, or raise a named partial-transfer failure."""

    def materialize_model_store(self) -> dict[str, object]:
        """Fetch real pinned snapshots and publish their measured store evidence."""

    def verify_chair_cache(self) -> dict[str, object]:
        """Verify every configured chair pin, with at most one same-pin re-fetch each."""

    def run_preflight(self) -> dict[str, object]:
        """Return one green preflight receipt or raise with its named red reason."""


class BootstrapJournal:
    """Durable bootstrap progress.  A crash leaves the current step incomplete."""

    def __init__(
        self, path: str | Path, plan: BootstrapPlan, *, now: Callable[[], datetime] = utc_now
    ) -> None:
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
        if raw["repository_commit"] != self.plan.repository_commit or raw["lockfile"] != str(
            self.plan.lockfile
        ):
            raise BootstrapStepFailure(
                BootstrapStep.REPOSITORY,
                "existing bootstrap journal names different pinned inputs",
                "Do not reuse this journal for another commit or lockfile; create a new run directory.",
            )
        return raw

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

    def __init__(self, journal: BootstrapJournal, actions: BootstrapActions) -> None:
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
        self.journal.mark_green(record)
        return _report_from_record(record)

    def _revalidate_completed_configuration(
        self, record: dict[str, object]
    ) -> BootstrapStepFailure | None:
        """Re-read the cheap binding before skipping completed or green work."""

        remediation = (
            "Restore the roster, declaration, serving catalogue, and placement selection "
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
    required = {"schema", "bindings", "witness_context_validation"}
    if set(receipt) != required or receipt.get("schema") != CONFIGURATION_RECEIPT_SCHEMA:
        return f"receipt must be a closed {CONFIGURATION_RECEIPT_SCHEMA!r} object"
    bindings = receipt.get("bindings")
    if not isinstance(bindings, dict) or set(bindings) != _CONFIGURATION_BINDINGS:
        return "receipt does not name all four selected configuration sources"
    for name in sorted(_CONFIGURATION_BINDINGS):
        binding = bindings[name]
        if not isinstance(binding, dict) or set(binding) != {"path", "sha256"}:
            return f"{name!r} must have only path and sha256"
        path = binding.get("path")
        digest = binding.get("sha256")
        if not isinstance(path, str) or not path.strip():
            return f"{name!r} path is not a non-blank string"
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            return f"{name!r} sha256 is not a lowercase digest"
    if not isinstance(receipt.get("witness_context_validation"), dict):
        return "witness_context_validation is not an object"
    return None


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
                if not is_cache_mismatch(initial_error) or self.refetch_same_pin is None:
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


class ModelStoreBootstrapAction:
    """Launch-time acquisition of the real roster onto the mounted model volume."""

    def __init__(
        self,
        store_root: str | Path,
        fetcher: MaterializationFetcher,
        *,
        capacity: dict[str, object],
    ) -> None:
        self.store_root = Path(store_root)
        self.fetcher = fetcher
        self.capacity = capacity

    def materialize(self) -> dict[str, object]:
        return materialize_real_roster(self.store_root, self.fetcher, capacity=self.capacity)


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
        cache: ChairCacheBootstrapAction,
        preflight: Callable[[], dict[str, object]],
        runner: Callable[[list[str], Path], subprocess.CompletedProcess[str]] | None = None,
        executables: Mapping[str, str] = BOOTSTRAP_EXECUTABLES,
        environment: Mapping[str, str] = BOOTSTRAP_ENVIRONMENT,
        image_contract: Callable[[], dict[str, object]] | None = None,
        free_bytes: Callable[[Path], int] | None = None,
    ) -> None:
        self.repository = Path(repository)
        self.transfer = transfer
        self.configuration = configuration
        self.materialize = materialize_model_store
        self.cache = cache
        self.preflight = preflight
        if set(executables) != {"git", "uv"} or any(
            not isinstance(value, str) or not Path(value).is_absolute()
            for value in executables.values()
        ):
            raise ValueError("bootstrap executables must give absolute git and uv paths")
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
        self._require_container_disk()
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
        return {
            "lockfile": str(supplied_lockfile),
            "sha256": hashlib.sha256(supplied_lockfile.read_bytes()).hexdigest(),
            "mode": "locked",
            "groups": ["pod"],
        }

    def _require_container_disk(self) -> None:
        """Refuse a sync the container-local disk cannot hold, before it starts.

        The create request states a container disk size, but nothing proves the
        pod got one: it is a documented field, and this is the first boot from
        this tree. What the pod *can* do is read the free space actually under
        the two directories the sync fills -- the wheel cache and the venv --
        and say so in one sentence naming both figures. Without this the
        failure is uv's own ENOSPC part way through a ten-gigabyte download
        that was already paid for, which is the shape principle 2 forbids: a
        cost with nothing to show and no named reason.

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
                    f"{REPOSITORY_VENV_DIRECTORY} are two copies of it)",
                    "Create the pod with a larger container disk (container_disk_gb in "
                    "the pod request) and boot again; uv would otherwise fill this disk "
                    "part way through the download and fail with no space left.",
                )

    def resume_transfer(self) -> dict[str, object]:
        return self.transfer()

    def materialize_model_store(self) -> dict[str, object]:
        result = self.materialize()
        if result.get("real_roster_complete") is not True:
            raise BootstrapStepFailure(
                BootstrapStep.MODEL_STORE,
                "model-store materialization did not verify every real-roster repository",
                "Resume the same pinned materialization; do not advance to chair-cache "
                "verification while a real-roster artifact is absent or unverified.",
            )
        return result

    def verify_chair_cache(self) -> dict[str, object]:
        return self.cache.verify()

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
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
            raise BootstrapStepFailure(
                step,
                f"command {argv[0]!r} failed: {detail}",
                "Repair the named bootstrap dependency and resume; no fallback checkout or unlocked environment is used.",
            )
        return result

    def _run(self, argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv,
            cwd=cwd,
            env=self.environment,
            text=True,
            capture_output=True,
            check=False,
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
