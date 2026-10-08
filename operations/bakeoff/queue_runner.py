"""Run a manifest of bake-off arms back to back on one pod, then copy, verify and end the pod.

On the pod, detached:

    python -m operations.bakeoff.queue_runner run --manifest FILE [--smoke-only] [--dry-run]
    python -m operations.bakeoff.queue_runner validate --manifest FILE
    python -m operations.bakeoff.queue_runner status --manifest FILE
    python -m operations.bakeoff.queue_runner end-pod --manifest FILE

On the Mac:

    python -m operations.bakeoff.queue_runner watch --ssh "ssh -p PORT root@IP" --status PATH
    python -m operations.bakeoff.queue_runner watch --ntfy [--queue NAME]
    python -m operations.bakeoff.queue_runner fetch --ssh "ssh -p PORT root@IP" --remote DIR --into DIR

The pod runs the whole queue and ends itself, so no laptop has to notice when a job
ends. Each arm runs a smoke of `smoke_pages` pages first, then the full run; the next
arm's install and preparation run on the CPU while this arm's command holds the card.
`status.json` beside the cache is rewritten every 30 s and at every change, the queue's
events join the arms' `events.jsonl`, and the lead's phone hears of each milestone once.
At the end the cache is copied to `sync_to`, every file's sha256 is compared, `DONE.json`
is written to both, and the pod is ended through its guard (or `pod_delete.sh`).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import re
import shlex
import shutil
import signal
import statistics
import subprocess
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from operations.bakeoff.witness_run import cached_ok, event, list_pages, write_json

QUEUE_SCHEMA = "bakeoff-queue.v1"
STATUS_SCHEMA = "bakeoff-queue-status.v1"
DONE_SCHEMA = "bakeoff-queue-done.v1"
CUTS = ("never", "overrun", "behind-schedule", "install-failed")
END_ACTIONS = ("delete", "none")
ROOT = Path(__file__).resolve().parents[2]
POD_DELETE = ROOT / "operations" / "pod" / "pod_delete.sh"
NTFY_CONF = ROOT / "private" / "ntfy.conf"
DEFAULT_GUARD_DIR = "/workspace/private/.pod_guard"
# Secrets the arms never need: kept out of their environment, so no arm log can show them.
ARM_ENV_DROPPED = frozenset({"RUNPOD_API_KEY", "NTFY_TOPIC"})
OFFLINE_ENV = {"HF_HUB_OFFLINE": "1", "VLLM_NO_USAGE_STATS": "1", "DO_NOT_TRACK": "1"}
HEARTBEAT_FRESH_SECONDS = 300
STATUS_EVERY_SECONDS = 30
STALE_SECONDS = 600
TERM_GRACE_SECONDS = 120
# Files the queue keeps rewriting after the copy; they are copied but not digested.
LIVE_FILES = frozenset({"status.json", "status.tmp", "events.jsonl", "DONE.json", "DONE.tmp"})
DONE_TITLE = "Session complete"
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_MANIFEST_KEYS = {
    "schema",
    "name",
    "pages",
    "out",
    "sync_to",
    "own_disk",
    "smoke_pages",
    "behind_schedule_min",
    "end_pod",
    "hard_stop_min",
    "arms",
}
_ARM_KEYS = {"name", "time_box_min", "cut", "gpu", "install", "prepare", "command"}


class ManifestError(ValueError):
    pass


class Terminated(BaseException):
    """SIGTERM reached the runner; a BaseException so no broad handler swallows it."""


# --- the manifest -------------------------------------------------------------------


@dataclass(frozen=True)
class ArmSpec:
    name: str
    time_box_min: float
    cut: str
    gpu: bool
    command: tuple[str, ...]
    install: tuple[str, ...] | None = None
    prepare: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Manifest:
    name: str
    pages: Path
    out: Path
    sync_to: Path
    own_disk: bool
    smoke_pages: int
    behind_schedule_min: float
    end_pod: str
    hard_stop_min: float | None
    arms: tuple[ArmSpec, ...]

    @property
    def planned_min(self) -> float:
        return sum(arm.time_box_min for arm in self.arms if arm.gpu)


def _argv(value: Any, where: str, required: bool) -> tuple[str, ...] | None:
    if value is None and not required:
        return None
    if not isinstance(value, list) or not value or not all(isinstance(v, str) for v in value):
        raise ManifestError(f"{where} must be a non-empty list of strings")
    if not value[0]:
        raise ManifestError(f"{where} starts with an empty program")
    return tuple(value)


def _number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ManifestError(f"{where} must be a positive number")
    return float(value)


def _flag_value(argv: tuple[str, ...], flag: str) -> str | None:
    for index, item in enumerate(argv):
        if item == flag and index + 1 < len(argv):
            return argv[index + 1]
        if item.startswith(flag + "="):
            return item.split("=", 1)[1]
    return None


def _runs_witness(argv: tuple[str, ...]) -> bool:
    return any(
        item == "operations.bakeoff.witness_run" or item.endswith("witness_run.py") for item in argv
    )


def _parse_arm(raw: Any, index: int, out: Path, pages: Path) -> ArmSpec:
    where = f"arms[{index}]"
    if not isinstance(raw, dict):
        raise ManifestError(f"{where} must be a table")
    unknown = set(raw) - _ARM_KEYS
    if unknown:
        raise ManifestError(f"{where} has unknown keys {sorted(unknown)}")
    name = raw.get("name")
    if not isinstance(name, str) or not _NAME.match(name):
        raise ManifestError(f"{where}.name must be a plain folder name")
    where = f"arm {name!r}"
    cut = raw.get("cut", "never")
    if cut not in CUTS:
        raise ManifestError(f"{where}: cut must be one of {', '.join(CUTS)}")
    gpu = raw.get("gpu", True)
    if not isinstance(gpu, bool):
        raise ManifestError(f"{where}: gpu must be true or false")
    command = _argv(raw.get("command"), f"{where}: command", True)
    assert command is not None
    if any(item == "--limit" or item.startswith("--limit=") for item in command):
        raise ManifestError(f"{where}: command must not carry --limit; the queue adds it")
    named_out = _flag_value(command, "--out")
    if named_out is not None and Path(named_out) != out:
        raise ManifestError(f"{where}: command writes to {named_out}, not the queue's out {out}")
    named_pages = _flag_value(command, "--pages")
    if named_pages is not None and Path(named_pages) != pages:
        raise ManifestError(f"{where}: command reads {named_pages}, not the queue's pages {pages}")
    label = _flag_value(command, "--label")
    if label is not None and label != name:
        raise ManifestError(f"{where}: command's --label {label!r} is not the arm's name")
    if _runs_witness(command):
        # witness_run's cache folder is --label, else --model; run-all writes several.
        if "run-all" in command:
            raise ManifestError(f"{where}: witness_run run-all writes several folders; use run")
        model = _flag_value(command, "--model")
        if label is None and model is not None and model != name:
            raise ManifestError(
                f"{where}: command caches under --model {model!r}; add --label {name}"
            )
    if cut == "install-failed" and raw.get("install") is None:
        raise ManifestError(f"{where}: cut = install-failed needs an install command")
    return ArmSpec(
        name=name,
        time_box_min=_number(raw.get("time_box_min"), f"{where}: time_box_min"),
        cut=cut,
        gpu=gpu,
        command=command,
        install=_argv(raw.get("install"), f"{where}: install", False),
        prepare=_argv(raw.get("prepare"), f"{where}: prepare", False),
    )


def parse_manifest(data: dict[str, Any]) -> Manifest:
    if data.get("schema") != QUEUE_SCHEMA:
        raise ManifestError(f"schema must be {QUEUE_SCHEMA!r}")
    unknown = set(data) - _MANIFEST_KEYS
    if unknown:
        raise ManifestError(f"unknown keys {sorted(unknown)}")
    name = data.get("name")
    if not isinstance(name, str) or not _NAME.match(name):
        raise ManifestError("name must be a plain name (letters, digits, . _ -)")
    paths = {}
    for key in ("pages", "out", "sync_to"):
        value = data.get(key)
        if not isinstance(value, str) or not value.startswith("/"):
            raise ManifestError(f"{key} must be an absolute path")
        paths[key] = Path(value)
    out_path, sync_path = paths["out"], paths["sync_to"]
    if out_path == sync_path or out_path in sync_path.parents or sync_path in out_path.parents:
        raise ManifestError("sync_to and out must be separate folders, neither inside the other")
    own_disk = data.get("own_disk", False)
    if not isinstance(own_disk, bool):
        raise ManifestError("own_disk must be true or false")
    smoke = data.get("smoke_pages", 2)
    if isinstance(smoke, bool) or not isinstance(smoke, int) or smoke < 1:
        raise ManifestError("smoke_pages must be a whole number of at least 1")
    end_pod = data.get("end_pod", "delete")
    if end_pod not in END_ACTIONS:
        raise ManifestError(f"end_pod must be one of {', '.join(END_ACTIONS)}")
    hard = data.get("hard_stop_min")
    raw_arms = data.get("arms")
    if not isinstance(raw_arms, list) or not raw_arms:
        raise ManifestError("arms must be a non-empty list of [[arms]] tables")
    arms = tuple(_parse_arm(raw, i, paths["out"], paths["pages"]) for i, raw in enumerate(raw_arms))
    names = [arm.name for arm in arms]
    if len(set(names)) != len(names):
        raise ManifestError("two arms share a name (each is a cache folder)")
    return Manifest(
        name=name,
        pages=paths["pages"],
        out=paths["out"],
        sync_to=paths["sync_to"],
        own_disk=own_disk,
        smoke_pages=smoke,
        behind_schedule_min=_number(data.get("behind_schedule_min", 30), "behind_schedule_min"),
        end_pod=end_pod,
        hard_stop_min=None if hard is None else _number(hard, "hard_stop_min"),
        arms=arms,
    )


def load_manifest(path: Path) -> Manifest:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as failure:
        raise ManifestError(f"cannot read {path}: {failure}") from failure
    return parse_manifest(data)


def skip_reason(arm: ArmSpec, behind_min: float, behind_schedule_min: float) -> str | None:
    """Why the schedule cuts this arm before it starts, or None to run it."""
    if arm.cut == "overrun" and behind_min > arm.time_box_min:
        return f"queue {behind_min:.0f} min behind, more than this arm's {arm.time_box_min:g} min"
    if arm.cut == "behind-schedule" and behind_min > behind_schedule_min:
        return f"queue {behind_min:.0f} min behind, past {behind_schedule_min:g} min"
    return None


# --- small helpers ------------------------------------------------------------------


def iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_digests(root: Path) -> dict[str, str]:
    """sha256 of every file under root, keyed by relative path, the live files left out."""
    found = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_file() and not path.is_symlink() and relative not in LIVE_FILES:
            found[relative] = sha256_file(path)
    return found


def copy_tree(source: Path, target: Path, run: Callable[..., Any] = subprocess.run) -> str:
    """Copy source into target, keeping times but not owners (FUSE volumes refuse chown)."""
    target.mkdir(parents=True, exist_ok=True)
    if shutil.which("rsync"):
        run(["rsync", "-rt", f"{source}/", f"{target}/"], check=True)
        return "rsync -rt"
    for path in sorted(source.rglob("*")):
        destination = target / path.relative_to(source)
        if path.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
            stat = path.stat()
            os.utime(destination, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    return "python copy"


def compare_digests(expected: dict[str, str], root: Path) -> list[str]:
    """Relative paths whose copy under root is missing or differs."""
    bad = []
    for relative, digest in expected.items():
        path = root / relative
        if not path.is_file() or sha256_file(path) != digest:
            bad.append(relative)
    return bad


def _atomic_text(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="ascii")
    os.replace(tmp, path)


def is_pod_id(value: str | None) -> bool:
    return bool(value) and value.isascii() and value.isalnum()


def heartbeat_age(guard_dir: Path, pod_id: str, instant: float) -> float | None:
    try:
        return max(0.0, instant - (guard_dir / f"heartbeat-{pod_id}").stat().st_mtime)
    except OSError:
        return None


def release_guard(guard_dir: Path, pod_id: str, note: str, instant: float) -> int:
    """Leave the release note, then move the guard's deadline to now (never later)."""
    stamp = int(instant)
    _atomic_text(guard_dir / f"released-{pod_id}", note.strip() + "\n")
    path = guard_dir / f"deadline-{pod_id}"
    try:
        current = int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        current = None
    if current is not None and current <= stamp:
        return current
    _atomic_text(path, f"{stamp}\n")
    return stamp


def _send_ping(kind: str, message: str) -> Any:
    from operations.notify import client

    return client.send(kind, message)


def _run_returncode(argv: list[str]) -> int:
    return subprocess.run(argv, check=False).returncode


# --- the queue ----------------------------------------------------------------------


class Queue:
    """One manifest's run on one pod. `clock`, `notifier` and `runner` are injectable."""

    def __init__(
        self,
        manifest: Manifest,
        *,
        clock: Callable[[], float] = time.time,
        notifier: Callable[[str, str], Any] = _send_ping,
        runner: Callable[[list[str]], int] = _run_returncode,
        environ: dict[str, str] | None = None,
        root: Path = ROOT,
        poll_seconds: float = 1.0,
        status_every: float = STATUS_EVERY_SECONDS,
        smoke_only: bool = False,
    ) -> None:
        self.m = manifest
        self.clock = clock
        self.notifier = notifier
        self.runner = runner
        self.env = dict(os.environ if environ is None else environ)
        self.root = root
        self.poll_seconds = poll_seconds
        self.status_every = status_every
        self.smoke_only = smoke_only
        self.pod_id = self.env.get("RUNPOD_POD_ID") or None
        self.guard_dir = Path(self.env.get("POD_GUARD_DIR") or DEFAULT_GUARD_DIR)
        self.lock = threading.RLock()
        self.sent: list[str] = []
        self.procs: set[subprocess.Popen] = set()
        self.page_list: list[Path] = []
        self.started_at: float | None = None
        self.planned_done = 0.0
        self.main_started: float | None = None
        self.main_box = 0.0
        self.lanes: dict[str, dict[str, Any]] = {}
        self.state = "starting"
        self.phase_override: str | None = None
        self.errors: list[str] = []
        self.skipped: list[dict[str, str]] = []
        self.finished: list[dict[str, Any]] = []
        self.retry: list[ArmSpec] = []
        self.end_action: str | None = None
        self.ready: dict[int, Future] = {}
        self.ready_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ready")
        self.cpu_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cpu-arm")
        self.hard_stopped = False
        self.terminated = False
        self._ok_cache: dict[Path, tuple[int, bool]] = {}
        self._stop_ticker = threading.Event()
        self._ticker_thread: threading.Thread | None = None

    def restore(self, status: dict[str, Any]) -> None:
        """Carry an earlier run's record into a manual end-pod, so its status keeps it."""
        with self.lock:
            self.errors = list(status.get("errors") or [])
            self.skipped = list(status.get("skipped") or [])
            self.finished = list(status.get("finished_arms") or [])
            ended = {"done", "end-pod", "end-pod-failed"}
            self.sent = [key for key in status.get("pings") or [] if key not in ended]

    # status ------------------------------------------------------------------------

    def _ok_pages(self, label: str, pages: list[Path] | None = None) -> list[float]:
        """Modification times of the label's pages (of `pages`, default all) cached without
        an error; stray records of pages outside the queue's page list never count."""
        times = []
        folder = self.m.out / label
        stems = {p.stem for p in (self.page_list if pages is None else pages)}
        for path in folder.glob("*.json") if folder.is_dir() else []:
            if path.stem not in stems:
                continue
            try:
                mtime = path.stat().st_mtime_ns
            except OSError:
                continue
            seen = self._ok_cache.get(path)
            if seen is None or seen[0] != mtime:
                seen = (mtime, cached_ok(path))
                self._ok_cache[path] = seen
            if seen[1]:
                times.append(mtime / 1e9)
        return sorted(times)

    def behind_min(self) -> float:
        if self.started_at is None:
            return 0.0
        now = self.clock()
        planned = self.planned_done
        if self.main_started is not None:
            planned += min((now - self.main_started) / 60, self.main_box)
        return (now - self.started_at) / 60 - planned

    def _lane(self) -> dict[str, Any] | None:
        return self.lanes.get("gpu") or self.lanes.get("cpu")

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            lane = self._lane()
            done = total = 0
            last = eta = eta_min = None
            if lane is not None:
                smoke = lane["phase"] == "smoke"
                counted = self.page_list[: self.m.smoke_pages] if smoke else self.page_list
                times = self._ok_pages(lane["arm"], counted)
                done, total = len(times), len(counted)
                last = iso(times[-1]) if times else None
                recent = [t for t in times if t >= lane["wall0"]]
                if len(recent) >= 2:
                    per_page = statistics.median(
                        b - a for a, b in zip(recent, recent[1:], strict=False)
                    )
                    left = max(0, total - done) * per_page
                    eta, eta_min = iso(time.time() + left), round(left / 60, 1)
            cpu = self.lanes.get("cpu")
            return {
                "schema": STATUS_SCHEMA,
                "queue": self.m.name,
                "pod_id": self.pod_id,
                "state": self.state,
                "arm": lane["arm"] if lane else None,
                "arm_index": lane["index"] if lane else None,
                "arms_total": len(self.m.arms),
                "phase": self.phase_override or (lane["phase"] if lane else None),
                "pages_done": done,
                "pages_total": total,
                "last_page_time": last,
                "started": iso(self.started_at),
                "arm_started": iso(lane["t0"]) if lane else None,
                "eta": eta,
                "eta_min": eta_min,
                "behind_min": round(self.behind_min(), 1),
                "errors": list(self.errors),
                "skipped": list(self.skipped),
                "finished_arms": list(self.finished),
                "retry": [arm.name for arm in self.retry],
                "cpu_arm": cpu["arm"] if cpu and lane is not cpu else None,
                "end_action": self.end_action,
                "pings": list(self.sent),
                "updated": iso(self.clock()),
            }

    def write_status(self) -> None:
        with self.lock:
            self.m.out.mkdir(parents=True, exist_ok=True)
            write_json(self.m.out / "status.json", self.snapshot())

    def _ticker(self) -> None:
        while not self._stop_ticker.wait(self.status_every):
            try:
                self.write_status()
            except OSError as failure:
                print(f"status write failed: {failure}", flush=True)

    def _event(self, name: str, **facts: Any) -> None:
        event(self.m.out, name, queue=self.m.name, **facts)

    def _error(self, text: str) -> None:
        with self.lock:
            self.errors.append(text)
        self.write_status()

    # pings ------------------------------------------------------------------------

    def ping(self, key: str, kind: str, message: str) -> None:
        """Send once per key; a failed send is an event, never a stop."""
        with self.lock:
            if key in self.sent:
                return
            self.sent.append(key)
        line = " ".join(f"{self.m.name}: {message}".split())
        try:
            outcome = self.notifier(kind, line)
            delivered = getattr(outcome, "delivered", bool(outcome))
            suppressed = getattr(outcome, "suppressed", False)
            detail = getattr(outcome, "detail", "")
        except Exception as failure:  # noqa: BLE001 -- a ping never stops the queue
            delivered, suppressed, detail = False, False, f"{type(failure).__name__}"
        if not delivered and not suppressed:
            self._event("queue-notify-failed", key=key, detail=str(detail)[:160])
        self.write_status()

    # processes --------------------------------------------------------------------

    def _spawn(self, arm: ArmSpec, phase: str, argv: list[str]) -> subprocess.Popen:
        if self.terminated:
            raise Terminated()
        folder = self.m.out / arm.name
        folder.mkdir(parents=True, exist_ok=True)
        with open(folder / "queue-arm.log", "ab") as log:
            process = subprocess.Popen(
                argv,
                cwd=self.root,
                env={
                    **{k: v for k, v in self.env.items() if k not in ARM_ENV_DROPPED},
                    **OFFLINE_ENV,
                },
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        with self.lock:
            self.procs.add(process)
        self._event("queue-command-start", arm=arm.name, phase=phase, argv=argv)
        return process

    def _finish_process(self, arm: ArmSpec, phase: str, process: subprocess.Popen, t0: float):
        with self.lock:
            self.procs.discard(process)
        seconds = round(time.monotonic() - t0, 1)
        self._event(
            "queue-command-end", arm=arm.name, phase=phase, exit=process.returncode, seconds=seconds
        )
        return process.returncode

    def _run_plain(self, arm: ArmSpec, phase: str, argv: list[str]) -> int:
        t0 = time.monotonic()
        process = self._spawn(arm, phase, argv)
        while process.poll() is None:
            time.sleep(self.poll_seconds)
        return self._finish_process(arm, phase, process, t0)

    def _run_watched(self, index: int, arm: ArmSpec, lane: str, phase: str, argv: list[str]):
        t0 = time.monotonic()
        process = self._spawn(arm, phase, argv)
        with self.lock:
            self.lanes[lane]["phase"] = phase
        self._ready(index + 1)
        self.write_status()
        while process.poll() is None:
            self._check_overrun(arm, lane)
            self._check_hard_stop()
            time.sleep(self.poll_seconds)
        return self._finish_process(arm, phase, process, t0)

    @staticmethod
    def _signal_group(process: subprocess.Popen, signum: int) -> None:
        try:
            os.killpg(process.pid, signum)
        except (ProcessLookupError, PermissionError):
            pass

    def _stop_all(self, grace: float) -> None:
        with self.lock:
            live = [p for p in self.procs if p.poll() is None]
        for process in live:
            self._signal_group(process, signal.SIGTERM)
        deadline = time.monotonic() + grace
        for process in live:
            try:
                process.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                self._signal_group(process, signal.SIGKILL)

    def _check_overrun(self, arm: ArmSpec, lane: str) -> None:
        started = self.lanes[lane]["t0"]
        over = (self.clock() - started) / 60
        key = f"overrun:{arm.name}"
        if over > arm.time_box_min and key not in self.sent:
            self._event("queue-overrun", arm=arm.name, minutes=round(over, 1))
            self.ping(key, "milestone", f"{arm.name} past its {arm.time_box_min:g} min box")

    def hard_stop_passed(self) -> bool:
        if self.m.hard_stop_min is None or self.started_at is None:
            return False
        return (self.clock() - self.started_at) / 60 >= self.m.hard_stop_min

    def _check_hard_stop(self) -> None:
        if self.hard_stopped or not self.hard_stop_passed():
            return
        self.hard_stopped = True
        self._event("queue-hard-stop", minutes=self.m.hard_stop_min)
        self.ping("hard-stop", "milestone", f"hard stop at {self.m.hard_stop_min:g} min reached")
        threading.Thread(target=self._stop_all, args=(TERM_GRACE_SECONDS,), daemon=True).start()

    # preparation ------------------------------------------------------------------

    def _ready(self, index: int) -> Future | None:
        """Start the arm's install and preparation on the CPU, once."""
        if index >= len(self.m.arms):
            return None
        with self.lock:
            if index not in self.ready:
                self.ready[index] = self.ready_pool.submit(self._prepare_arm, self.m.arms[index])
            return self.ready[index]

    def _prepare_arm(self, arm: ArmSpec) -> bool:
        """Install then prepare; False only when the install failed."""
        if arm.install is not None and self._run_plain(arm, "install", list(arm.install)) != 0:
            return False
        if arm.prepare is not None:
            code = self._run_plain(arm, "prepare", list(arm.prepare))
            if code != 0:
                self._error(f"{arm.name}: prepare exit {code}")
        return True

    # arms -------------------------------------------------------------------------

    def _pages_ok(self, label: str, pages: list[Path]) -> bool:
        return all(cached_ok(self.m.out / label / f"{p.stem}.json") for p in pages)

    def _end_lane(self, lane: str) -> None:
        with self.lock:
            entry = self.lanes.pop(lane, None)
            if lane == "gpu" and entry is not None and not entry.get("retry"):
                self.planned_done += self.main_box
                self.main_started = None
        self.write_status()

    def _record(self, arm: ArmSpec, status: str, t0: float) -> None:
        pages = len(self._ok_pages(arm.name))
        minutes = (time.monotonic() - t0) / 60
        with self.lock:
            self.finished.append(
                {
                    "label": arm.name,
                    "pages": pages,
                    "wall_seconds": round(minutes * 60, 1),
                    "status": status,
                }
            )
        self._event("queue-arm-end", arm=arm.name, status=status, pages=pages)
        if status in ("ok", "smoke-ok"):
            self.ping(
                f"arm-end:{arm.name}",
                "milestone",
                f"{arm.name} {status}: {pages} pages in {minutes:.0f} min",
            )
        elif status == "failed":
            self.ping(f"failed:{arm.name}", "decision", f"{arm.name} failed after its retry")
        elif status == "smoke-failed":
            self.ping(f"failed:{arm.name}", "decision", f"{arm.name} smoke failed (smoke only)")

    def _arm_error(self, arm: ArmSpec, what: str) -> None:
        self._error(f"{arm.name}: {what}")
        self._event("queue-arm-error", arm=arm.name, detail=what)
        later = "reported at the end" if self.smoke_only else "retried at the end"
        self.ping(f"error:{arm.name}", "decision", f"{arm.name} {what}; {later}")
        with self.lock:
            self.retry.append(arm)

    def skip(self, arm: ArmSpec, reason: str) -> None:
        with self.lock:
            self.skipped.append({"arm": arm.name, "reason": reason})
            pending = self.ready.get(self.m.arms.index(arm))
        if pending is not None:
            pending.cancel()  # a skipped arm's preparation, if not yet started, never runs
        self._event("queue-arm-skipped", arm=arm.name, reason=reason)
        self.ping(f"skip:{arm.name}", "milestone", f"{arm.name} skipped: {reason}")

    def run_arm(self, index: int, arm: ArmSpec, lane: str) -> None:
        t0 = time.monotonic()
        with self.lock:
            self.lanes[lane] = {
                "arm": arm.name,
                "index": index,
                "phase": "install" if arm.install else "prepare",
                "t0": self.clock(),
                "wall0": time.time(),
            }
            if lane == "gpu":
                self.main_started, self.main_box = self.clock(), arm.time_box_min
        self._event("queue-arm-start", arm=arm.name, index=index, lane=lane)
        self.ping(
            f"arm-start:{arm.name}",
            "milestone",
            f"{arm.name} started ({index + 1}/{len(self.m.arms)})",
        )
        try:
            self._run_arm_phases(index, arm, lane, t0)
        except Exception as failure:  # noqa: BLE001 -- a missing program must not end the queue
            self._arm_error(arm, f"crashed ({type(failure).__name__}: {failure})"[:200])
        finally:
            self._end_lane(lane)

    def _run_arm_phases(self, index: int, arm: ArmSpec, lane: str, t0: float) -> None:
        future = self._ready(index)
        assert future is not None
        if not future.result():
            if arm.cut == "install-failed":
                self.skip(arm, "install failed")
            else:
                self._arm_error(arm, "install failed")
            return
        smoke_pages = self.page_list[: self.m.smoke_pages]
        smoke = [*arm.command, "--limit", str(self.m.smoke_pages)]
        code = self._run_watched(index, arm, lane, "smoke", smoke)
        if self.hard_stopped:
            self._record(arm, "hard-stopped", t0)
            return
        if code != 0 or not self._pages_ok(arm.name, smoke_pages):
            self._arm_error(arm, f"smoke failed (exit {code})")
            return
        if self.smoke_only:
            self._record(arm, "smoke-ok", t0)
            return
        code = self._run_watched(index, arm, lane, "run", list(arm.command))
        if self.hard_stopped:
            self._record(arm, "hard-stopped", t0)
        elif code != 0 or not self._pages_ok(arm.name, self.page_list):
            self._arm_error(arm, f"run incomplete (exit {code})")
        else:
            self._record(arm, "ok", t0)

    def _retry_arm(self, arm: ArmSpec) -> None:
        t0 = time.monotonic()
        index = self.m.arms.index(arm)
        with self.lock:
            self.lanes["gpu"] = {
                "arm": arm.name,
                "index": index,
                "phase": "retry",
                "t0": self.clock(),
                "wall0": time.time(),
                "retry": True,
            }
        try:
            if arm.install is not None and self._run_plain(arm, "retry", list(arm.install)) != 0:
                self._record(arm, "failed", t0)
                return
            # The smoke again first (cached pages are skipped, so a passed smoke costs nothing).
            smoke = [*arm.command, "--limit", str(self.m.smoke_pages)]
            code = self._run_watched(index, arm, "gpu", "retry", smoke)
            if code == 0 and self._pages_ok(arm.name, self.page_list[: self.m.smoke_pages]):
                code = self._run_watched(index, arm, "gpu", "retry", list(arm.command))
            elif code == 0:
                code = -1
            if self.hard_stopped:
                self._record(arm, "hard-stopped", t0)
            elif code == 0 and self._pages_ok(arm.name, self.page_list):
                self._record(arm, "ok", t0)
            else:
                self._record(arm, "failed", t0)
        except Exception as failure:  # noqa: BLE001 -- reported, never dropped
            self._error(f"{arm.name}: retry crashed ({type(failure).__name__}: {failure})"[:200])
            self._record(arm, "failed", t0)
        finally:
            self._end_lane("gpu")

    # the whole queue ---------------------------------------------------------------

    def run(self) -> int:
        self.started_at = self.clock()
        try:
            self.page_list = list_pages(self.m.pages) if self.m.pages.is_dir() else []
        except SystemExit as refusal:
            self.page_list = []
            self._error(str(refusal))
        if not self.page_list:
            self.state = "failed"
            self._error(f"no page images under {self.m.pages}")
            self.ping("start", "decision", "not started: no page images found")
            return 1
        self.state = "running"
        self._event("queue-start", arms=len(self.m.arms), pages=len(self.page_list))
        self.ping(
            "start",
            "milestone",
            f"queue started: {len(self.m.arms)} arms, {len(self.page_list)} pages, plan "
            f"{self.m.planned_min:.0f} min",
        )
        self._ticker_thread = threading.Thread(target=self._ticker, daemon=True)
        self._ticker_thread.start()
        try:
            self._run_arms()
            if self.smoke_only:
                for arm in list(self.retry):
                    self._record(arm, "smoke-failed", time.monotonic())
            else:
                for arm in list(self.retry):
                    if self.hard_stopped or self.hard_stop_passed():
                        self._record(arm, "hard-stopped", time.monotonic())
                        continue
                    self._retry_arm(arm)
            with self.lock:
                self.retry.clear()
            return self.finish()
        except Terminated:
            return self.terminate()
        finally:
            self._stop_ticker.set()
            self.ready_pool.shutdown(wait=False, cancel_futures=True)
            self.cpu_pool.shutdown(wait=False, cancel_futures=True)

    def _run_arms(self) -> None:
        cpu_job: Future | None = None
        self._ready(0)
        for index, arm in enumerate(self.m.arms):
            if self.hard_stopped or self.hard_stop_passed():
                self.skip(arm, "hard stop reached")
                continue
            reason = skip_reason(arm, self.behind_min(), self.m.behind_schedule_min)
            if reason:
                self.skip(arm, reason)
                continue
            if arm.gpu:
                self.run_arm(index, arm, "gpu")
                continue
            if cpu_job is not None:
                cpu_job.result()
            cpu_job = self.cpu_pool.submit(self.run_arm, index, arm, "cpu")
        if cpu_job is not None:
            cpu_job.result()

    def terminate(self) -> int:
        self.terminated = True
        self._stop_all(30)
        with self.lock:
            self.state = "failed"
            self.errors.append("stopped by SIGTERM")
        self._event("queue-terminated")
        self.write_status()
        self.ping("terminated", "decision", "queue stopped by SIGTERM; pod NOT ended")
        return 143

    # the end ----------------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for entry in self.finished:
            counts[entry["status"]] = counts.get(entry["status"], 0) + 1
        return {
            "arms": counts,
            "skipped": list(self.skipped),
            "errors": list(self.errors),
            "finished_arms": list(self.finished),
        }

    def sync(self) -> dict[str, Any]:
        """Copy out to sync_to, compare every digest, and write DONE.json to both."""
        with self.lock:
            self.state, self.phase_override = "syncing", "sync"
        self.write_status()
        # Nothing may write into the cache while it is copied and digested: a preparation
        # still running for a skipped arm would change its log after the copy, and the
        # ticker's status.tmp would vanish under rsync (exit 24).
        self._stop_ticker.set()
        if self._ticker_thread is not None:
            self._ticker_thread.join()
        self.ready_pool.shutdown(wait=True, cancel_futures=True)
        self.cpu_pool.shutdown(wait=True, cancel_futures=True)
        method, mismatched = None, []
        try:
            method = copy_tree(self.m.out, self.m.sync_to)
            digests = tree_digests(self.m.out)
            mismatched = compare_digests(digests, self.m.sync_to)
            failure = None
        except (OSError, subprocess.CalledProcessError) as error:
            digests, failure = {}, f"{type(error).__name__}: {error}"
        verified = failure is None and not mismatched
        done = {
            "schema": DONE_SCHEMA,
            "queue": self.m.name,
            "pod_id": self.pod_id,
            "finished": iso(time.time()),
            "copy": method,
            "verified": verified,
            "mismatched": mismatched,
            "failure": failure,
            "files": len(digests),
            "digests": digests,
            "status": self.summary(),
        }
        for folder in (self.m.out, self.m.sync_to):
            try:
                folder.mkdir(parents=True, exist_ok=True)
                write_json(folder / "DONE.json", done)
            except OSError as error:
                self._error(f"DONE.json not written to {folder}: {error}")
        self._event("queue-sync", verified=verified, files=len(digests), mismatched=mismatched)
        if verified:
            self.ping("sync", "milestone", f"copy verified: {len(digests)} files")
        else:
            self._error(f"copy not verified: {failure or f'{len(mismatched)} files differ'}")
            self.ping("sync", "decision", f"copy NOT verified ({len(mismatched)} files differ)")
        return done

    def finish(self) -> int:
        if self.smoke_only:
            with self.lock:
                self.state, self.end_action = "done", "kept (smoke only)"
            counts = self.summary()["arms"]
            self.ping("done", "done", f"smoke run finished {counts}; pod kept")
            self.write_status()
            return 0
        done = self.sync()
        return self.end_pod(done["verified"])

    def _plan_end(self, verified: bool) -> str:
        if self.m.end_pod == "none":
            return "keep"
        if self.m.own_disk and not verified:
            return "refuse"
        if not is_pod_id(self.pod_id):
            return "no-pod-id"
        assert self.pod_id is not None
        age = heartbeat_age(self.guard_dir, self.pod_id, time.time())
        if age is not None and age < HEARTBEAT_FRESH_SECONDS:
            return "guard"
        return "delete"

    def end_pod(self, verified: bool) -> int:
        plan = self._plan_end(verified)
        words = {
            "keep": "kept (end_pod = none)",
            "refuse": "NOT ended: own disk and the copy is not verified",
            "no-pod-id": "NOT ended: RUNPOD_POD_ID is not set",
            "guard": "its guard was asked to delete it",
            "delete": "pod_delete.sh asked to delete it",
        }
        with self.lock:
            self.phase_override, self.end_action = "end-pod", words[plan]
            self.state = "failed" if plan == "refuse" else "done"
        self._event("queue-end-pod", plan=plan, pod_id=self.pod_id)
        counts = self.summary()["arms"]
        if plan in ("refuse", "no-pod-id"):
            self.ping("end-pod", "decision", f"pod {words[plan]}; delete it by hand")
        self.ping(
            "done",
            "done",
            f"queue finished {counts}, {len(self.skipped)} skipped; pod {words[plan]}",
        )
        self.write_status()
        if plan == "guard":
            plan = self._release() or "delete"
        if plan == "delete":
            assert self.pod_id is not None
            code = self.runner(["sh", str(POD_DELETE), self.pod_id])
            if code != 0:
                with self.lock:
                    self.state = "failed"
                    self.end_action = f"pod_delete.sh exit {code}"
                self._error(f"pod_delete.sh exit {code}")
                self.ping("end-pod-failed", "decision", f"pod_delete.sh exit {code}; check RunPod")
                return 1
        return 1 if plan == "refuse" else 0

    def _release(self) -> str | None:
        assert self.pod_id is not None
        note = f"queue {self.m.name} ended {self.state}"
        try:
            deadline = release_guard(self.guard_dir, self.pod_id, note, time.time())
        except OSError as error:
            self._error(f"guard release failed: {error}")
            return None
        self._event("queue-guard-released", deadline=deadline)
        return "guard"


# --- the Mac side -------------------------------------------------------------------

_MARK = "--bakeoff-queue-done--"
_SSH_VALUED = set("bcDEeFIiJLlmOoPpQRSWw")


def split_ssh(ssh: str) -> tuple[list[str], str]:
    """`ssh -p 22 root@1.2.3.4` -> (['ssh', '-p', '22'], 'root@1.2.3.4')."""
    tokens = shlex.split(ssh)
    if not tokens:
        raise SystemExit("--ssh is empty")
    options, host, index = [tokens[0]], None, 1
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("-") and len(token) == 2 and token[1] in _SSH_VALUED:
            options += tokens[index : index + 2]
            index += 2
            continue
        if token.startswith("-"):
            options.append(token)
        elif host is None:
            host = token
        index += 1
    if host is None:
        raise SystemExit("--ssh names no host")
    return options, host


def status_line(status: dict[str, Any]) -> str:
    index = status.get("arm_index")
    where = f"({index + 1}/{status.get('arms_total')})" if isinstance(index, int) else ""
    eta = status.get("eta_min")
    behind = status.get("behind_min") or 0
    errors = status.get("errors") or []
    parts = [
        f"{status.get('queue')} {status.get('state')}",
        f"arm {status.get('arm')} {where}".strip(),
        f"{status.get('phase')}",
        f"{status.get('pages_done')}/{status.get('pages_total')} pages",
        f"eta {eta:.0f} min" if isinstance(eta, (int, float)) else "eta -",
        f"behind {behind:+.0f} min",
        f"errors {len(errors)}" + (f" (last: {errors[-1]})" if errors else ""),
    ]
    return ", ".join(parts)


def watch_ssh(
    ssh: str,
    status_path: str,
    interval: float,
    *,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
    say: Callable[[str], None] = print,
) -> int:
    """Read the status file over SSH; one line per change; 0 on DONE, 1 on failure.

    Staleness is judged on this machine's clock: the pod rewrites `updated` every 30 s,
    so a value unchanged (or unreadable) for STALE_SECONDS means the queue or the pod
    has stopped, whatever the two clocks say. It warns once per stale spell."""
    argv = shlex.split(ssh)
    done_path = posixpath.join(posixpath.dirname(status_path), "DONE.json")
    remote = (
        f"cat {shlex.quote(status_path)}; echo {_MARK}; "
        f"cat {shlex.quote(done_path)} 2>/dev/null; true"
    )
    last, warned = None, False
    seen_updated, fresh_at = None, now()
    while True:
        try:
            result = run([*argv, remote], capture_output=True, text=True, timeout=120)
            output, code = result.stdout or "", result.returncode
        except subprocess.TimeoutExpired:
            output, code = "", -1
        status_text, _, done_text = output.partition(_MARK)
        status = _json_or_none(status_text)
        done = _json_or_none(done_text)
        if status is None and done is None:
            line = f"no status readable over ssh (exit {code})"
        else:
            line = status_line(status) if status else "status unreadable; DONE.json present"
        if line != last:
            say(f"{datetime.now().strftime('%H:%M')} {line}")
            last = line
        updated = (status or {}).get("updated")
        if status is not None and updated != seen_updated:
            seen_updated, fresh_at, warned = updated, now(), False
        quiet = now() - fresh_at
        if quiet > STALE_SECONDS and not warned and done is None:
            warned = True
            what = "not updated" if status is not None else "not readable"
            say(f"WARNING: status {what} for {quiet / 60:.0f} min; the queue or pod may be gone")
        if status is not None:
            if status.get("state") == "failed":
                say(
                    f"queue failed: {status.get('end_action') or (status.get('errors') or [''])[-1]}"
                )
                return 1
        if done is not None or (status or {}).get("state") == "done":
            verified = (done or {}).get("verified")
            files = (done or {}).get("files")
            action = (status or {}).get("end_action")
            say(f"queue done: copy verified {verified}, {files} files; pod {action}")
            return 0
        sleep(interval)


def _json_or_none(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def ntfy_topic(environ: dict[str, str] | None = None, conf: Path = NTFY_CONF) -> str:
    env = os.environ if environ is None else environ
    topic = env.get("NTFY_TOPIC", "")
    if not topic and conf.is_file():
        for line in conf.read_text(encoding="utf-8").splitlines():
            if line.startswith("NTFY_TOPIC="):
                topic = line.split("=", 1)[1].strip().strip("\"'")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", topic or ""):
        raise SystemExit("no usable ntfy topic in NTFY_TOPIC or private/ntfy.conf")
    return topic


def watch_ntfy(
    queue: str | None,
    *,
    topic: str | None = None,
    opener: Callable[..., Any] = urllib.request.urlopen,
    sleep: Callable[[float], None] = time.sleep,
    say: Callable[[str], None] = print,
    reconnects: int | None = None,
) -> int:
    """Follow the topic's JSON stream; 0 on the queue's done ping. The topic is never shown."""
    topic = topic or ntfy_topic()
    since, attempts = "", 0
    while True:
        url = f"https://ntfy.sh/{topic}/json" + (f"?since={since}" if since else "")
        try:
            with opener(urllib.request.Request(url), timeout=600) as stream:
                for raw in stream:
                    message = _json_or_none(raw.decode("utf-8", "replace"))
                    if not message or message.get("event") != "message":
                        continue
                    since = str(message.get("id") or since)
                    text, title = message.get("message", ""), message.get("title", "")
                    if queue and not text.startswith(f"{queue}:"):
                        continue
                    say(f"{title}: {text}")
                    if title == DONE_TITLE:
                        return 0
            say("ntfy stream ended; reconnecting")
        except (OSError, urllib.error.URLError) as failure:
            say(f"ntfy stream failed ({type(failure).__name__}); reconnecting")
        attempts += 1
        if reconnects is not None and attempts > reconnects:
            return 1
        sleep(30)


def verify_fetched(into: Path, say: Callable[[str], None] = print) -> int:
    try:
        done = json.loads((into / "DONE.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        say(f"no readable DONE.json in {into}; nothing verified")
        return 1
    digests = done.get("digests") or {}
    unsafe = [r for r in digests if r.startswith("/") or ".." in Path(r).parts]
    if unsafe:
        say(f"DONE.json names paths outside the cache: {unsafe[:3]}")
        return 1
    bad = compare_digests(digests, into)
    for relative in bad:
        say(f"MISMATCH {relative}")
    say(f"verified {len(digests) - len(bad)} of {len(digests)} files against DONE.json")
    if not done.get("verified"):
        say("note: the pod's own copy check did not pass (DONE.json verified = false)")
    return 1 if bad else 0


def fetch(
    ssh: str,
    remote: str,
    into: Path,
    *,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    say: Callable[[str], None] = print,
) -> int:
    """Copy the remote cache home over SSH, then check every file against DONE.json."""
    options, host = split_ssh(ssh)
    source = remote.rstrip("/")
    copied = False
    if shutil.which("rsync"):
        into.mkdir(parents=True, exist_ok=True)
        argv = ["rsync", "-rt", "-e", shlex.join(options), f"{host}:{source}/", f"{into}/"]
        copied = run(argv, check=False).returncode == 0
        if not copied:
            say("rsync failed; trying scp")
    if not copied:
        copied = _scp(options, host, source, into, run, say)
    if not copied:
        say("copy failed; nothing verified")
        return 1
    return verify_fetched(into, say)


def _scp(options, host, source, into: Path, run, say) -> bool:
    scp = ["scp", "-r", "-q"]
    pairs = iter(options[1:])
    for option in pairs:
        if len(option) == 2 and option[1] in _SSH_VALUED:
            value = next(pairs, "")
            if option == "-p":
                scp += ["-P", value]
            elif option == "-l":
                host = f"{value}@{host}"
            elif option in ("-i", "-o", "-F", "-J"):
                scp += [option, value]
        else:
            scp.append(option)
    staging = into.parent / f".{into.name}.fetching"
    if staging.exists():
        shutil.rmtree(staging)
    into.parent.mkdir(parents=True, exist_ok=True)
    if run([*scp, f"{host}:{source}", str(staging)], check=False).returncode != 0:
        return False
    into.mkdir(parents=True, exist_ok=True)
    for path in sorted(staging.rglob("*")):
        target = into / path.relative_to(staging)
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            os.replace(path, target)
    shutil.rmtree(staging)
    return True


# --- the command line ---------------------------------------------------------------


def _dry_run(manifest: Manifest) -> int:
    print(f"queue {manifest.name}: {len(manifest.arms)} arms, plan {manifest.planned_min:g} min")
    for index, arm in enumerate(manifest.arms):
        lane = "gpu" if arm.gpu else "cpu (beside the gpu arm)"
        print(f"{index + 1}. {arm.name}: {arm.time_box_min:g} min, cut {arm.cut}, {lane}")
        for what, argv in (("install", arm.install), ("prepare", arm.prepare)):
            if argv:
                print(f"   {what}: {shlex.join(argv)}")
        print(f"   smoke: {shlex.join([*arm.command, '--limit', str(manifest.smoke_pages)])}")
        print(f"   run:   {shlex.join(arm.command)}")
    print(f"then copy {manifest.out} -> {manifest.sync_to}, verify, end pod: {manifest.end_pod}")
    return 0


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "status", "end-pod", "validate"):
        p = sub.add_parser(name)
        p.add_argument("--manifest", type=Path, required=True)
        if name == "run":
            p.add_argument("--smoke-only", action="store_true")
            p.add_argument("--dry-run", action="store_true")
    w = sub.add_parser("watch")
    route = w.add_mutually_exclusive_group(required=True)
    route.add_argument("--ssh", help='e.g. "ssh -p PORT root@IP"')
    route.add_argument("--ntfy", action="store_true", help="follow the phone topic instead")
    w.add_argument("--status", default="/workspace/private/bakeoff/witness-cache/status.json")
    w.add_argument("--interval", type=float, default=30)
    w.add_argument("--queue", help="with --ntfy: only this queue's pings")
    f = sub.add_parser("fetch")
    f.add_argument("--ssh", required=True)
    f.add_argument("--remote", required=True)
    f.add_argument("--into", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "watch":
        if args.ntfy:
            return watch_ntfy(args.queue)
        return watch_ssh(args.ssh, args.status, args.interval)
    if args.command == "fetch":
        return fetch(args.ssh, args.remote, args.into)
    try:
        manifest = load_manifest(args.manifest)
    except ManifestError as failure:
        print(f"manifest refused: {failure}", file=sys.stderr)
        return 2
    if args.command == "validate":
        found = "found" if manifest.pages.is_dir() else "not found here"
        print(f"ok: {manifest.name}, {len(manifest.arms)} arms, plan {manifest.planned_min:g} min")
        print(f"pages folder {manifest.pages}: {found}")
        return 0
    if args.command == "status":
        path = manifest.out / "status.json"
        if not path.is_file():
            print(f"no status yet at {path}", file=sys.stderr)
            return 1
        print(path.read_text(encoding="utf-8"))
        return 0
    if args.command == "end-pod":
        try:
            done = json.loads((manifest.out / "DONE.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            done = {}
        queue = Queue(manifest)
        try:
            queue.restore(json.loads((manifest.out / "status.json").read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
        return queue.end_pod(bool(done.get("verified")))
    if args.dry_run:
        return _dry_run(manifest)
    queue = Queue(manifest, smoke_only=args.smoke_only)

    def on_term(*_: Any) -> None:
        raise Terminated()

    signal.signal(signal.SIGTERM, on_term)
    return queue.run()


if __name__ == "__main__":
    code = main()
    if code == 143:
        # Worker threads may still be unwinding; the status file already says failed.
        sys.stdout.flush()
        os._exit(143)
    sys.exit(code)
