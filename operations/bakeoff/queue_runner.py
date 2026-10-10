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
ends. Each arm runs a smoke of `smoke_pages` pages first, then the full run. One arm at a
time holds the card (the GPU lane, in manifest order); `gpu = false` arms run beside it,
as many at once as `cpu_threads` allows; an arm named in another's `after` must finish
first. The next arm's install and preparation run while the current arm's command runs.
`status.json` beside the cache is rewritten every 30 s and at every change, the queue's
events join the arms' `events.jsonl`, and the lead's phone hears of each milestone once.
At the end the cache is copied to `sync_to`, every file's sha256 is compared, `DONE.json`
is written to both, and the pod is ended through its guard (or `pod_delete.sh`). With
`--own-disk --keep-pod` the copy is not read back on the pod: `fetch` checks every file
against `DONE.json` at home.
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
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from operations.bakeoff.witness_run import (
    cached_ok,
    event,
    list_pages,
    settled,
    terminal_failure,
    write_json,
)

QUEUE_SCHEMA = "bakeoff-queue.v1"
STATUS_SCHEMA = "bakeoff-queue-status.v1"
# Arm outcomes whose answers a dependent arm may build on.
DEPENDENCY_MET = frozenset({"ok", "ok-with-failures", "smoke-ok"})
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
# CPUs left to the GPU lane's own processes (server, client, DAI's detector) when the
# CPU arms' thread budget is "auto".
GPU_LANE_CPUS = 2
# The pod sizes `validate` and `run --dry-run` estimate the day for.
ESTIMATE_CPUS = (8, 16, 32)
# Files the queue keeps rewriting after the copy; they are copied but not digested.
LIVE_FILES = frozenset({"status.json", "status.tmp", "events.jsonl", "DONE.json", "DONE.tmp"})
# The phone's word for the queue's last ping (`operations/notify`, event `queue-done`).
DONE_EVENT = "queue-done"
DONE_TITLE = "Queue finished"
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
    "cpu_threads",
    "arms",
}
_ARM_KEYS = {
    "name",
    "time_box_min",
    "cut",
    "gpu",
    "install",
    "prepare",
    "command",
    "after",
    "threads",
    "writes",
}


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
    after: tuple[str, ...] = ()  # arms that must finish ok before this one starts
    threads: int = 1  # a CPU arm's share of `cpu_threads`
    writes: str = ""  # folder under `out` with one <stem>.json per page; default the name

    @property
    def records(self) -> str:
        return self.writes or self.name

    @property
    def keeps_server(self) -> bool:
        """A GPU witness_run arm: its smoke's server is kept for its full run."""
        return self.gpu and _runs_witness(self.command)


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
    cpu_threads: int | str | None = None  # None: one CPU arm at a time

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


def _whole(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ManifestError(f"{where} must be a whole number of at least 1")
    return value


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


def _parse_arm(raw: Any, index: int, out: Path, pages: Path, earlier: list[str]) -> ArmSpec:
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
    for flag in ("--limit", "--keep-server", "--adopt-server"):
        if any(item == flag or item.startswith(flag + "=") for item in command):
            raise ManifestError(f"{where}: command must not carry {flag}; the queue adds it")
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
    from operations.bakeoff import weights

    unknown_weights = [n for n in weights.names_in(raw.get("prepare")) if n not in weights.known()]
    if unknown_weights:
        raise ManifestError(f"{where}: prepare fetches unknown weights {unknown_weights}")
    after = raw.get("after", [])
    if not isinstance(after, list) or not all(isinstance(a, str) for a in after):
        raise ManifestError(f"{where}: after must be a list of arm names")
    unknown = [a for a in after if a not in earlier]
    if unknown:
        raise ManifestError(f"{where}: after names {unknown}, which are not earlier arms")
    named_threads = _flag_value(command, "--threads")
    threads = raw.get("threads", int(named_threads) if (named_threads or "").isdigit() else 1)
    writes = raw.get("writes", "")
    if not isinstance(writes, str) or writes.startswith("/") or ".." in Path(writes).parts:
        raise ManifestError(f"{where}: writes must be a folder inside out")
    return ArmSpec(
        name=name,
        time_box_min=_number(raw.get("time_box_min"), f"{where}: time_box_min"),
        cut=cut,
        gpu=gpu,
        command=command,
        install=_argv(raw.get("install"), f"{where}: install", False),
        prepare=_argv(raw.get("prepare"), f"{where}: prepare", False),
        after=tuple(after),
        threads=_whole(threads, f"{where}: threads"),
        writes=writes,
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
    arms: list[ArmSpec] = []
    for i, raw in enumerate(raw_arms):
        arms.append(_parse_arm(raw, i, paths["out"], paths["pages"], [a.name for a in arms]))
    names = [arm.name for arm in arms]
    if len(set(names)) != len(names):
        raise ManifestError("two arms share a name (each is a cache folder)")
    cpu_threads = data.get("cpu_threads")
    if cpu_threads is not None and cpu_threads != "auto":
        cpu_threads = _whole(cpu_threads, 'cpu_threads (a number or "auto")')
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
        arms=tuple(arms),
        cpu_threads=cpu_threads,
    )


def override_manifest(
    manifest: Manifest, sync_to: str | None, own_disk: bool, keep_pod: bool = False
) -> Manifest:
    """One manifest for both storage routes: the command line names where the copy goes.

    `--sync-to` must be absolute and separate from `out`, as in the manifest;
    `--own-disk` only ever adds the refusal, never removes it; `--keep-pod` is
    `end_pod = "none"` for this run, so the session fetches from the pod's own
    disk and deletes the pod itself. The same flags go on every command that
    reads the manifest, so `end-pod` sees the copy `run` made.
    """
    if sync_to is not None:
        path = Path(sync_to)
        if not path.is_absolute():
            raise ManifestError("--sync-to must be an absolute path")
        if path == manifest.out or path in manifest.out.parents or manifest.out in path.parents:
            raise ManifestError(
                "--sync-to and out must be separate folders, neither inside the other"
            )
        manifest = replace(manifest, sync_to=path)
    if own_disk:
        manifest = replace(manifest, own_disk=True)
    if keep_pod:
        manifest = replace(manifest, end_pod="none")
    return manifest


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


# --- lanes --------------------------------------------------------------------------


def available_cpus() -> int:
    """The CPUs this process may use: its affinity, capped by a container's CPU quota
    (a pod's container sees every CPU of its host but is limited by its cgroup)."""
    try:
        count = len(os.sched_getaffinity(0))
    except AttributeError:
        count = os.cpu_count() or 1
    quotas = (
        ("/sys/fs/cgroup/cpu.max", None),
        ("/sys/fs/cgroup/cpu/cpu.cfs_quota_us", "/sys/fs/cgroup/cpu/cpu.cfs_period_us"),
    )
    for quota_file, period_file in quotas:
        try:
            fields = Path(quota_file).read_text(encoding="ascii").split()
            if period_file is not None:
                fields.append(Path(period_file).read_text(encoding="ascii").strip())
            quota, period = float(fields[0]), float(fields[1])
        except (OSError, ValueError, IndexError):
            continue
        if quota > 0 and period > 0:
            count = min(count, max(1, int(quota // period)))
    return count


def cpu_budget(setting: int | str | None, cpus: int) -> int | None:
    """Threads the CPU arms may use at once; None means one CPU arm at a time."""
    if setting == "auto":
        return max(1, cpus - GPU_LANE_CPUS)
    return setting if isinstance(setting, int) else None


def _fits(share: int, running: list[int], budget: int | None) -> bool:
    """A CPU arm fits beside the running ones, and always when none runs."""
    if not running:
        return True
    return budget is not None and sum(running) + share <= budget


def pick(
    arms: tuple[ArmSpec, ...],
    ready: list[int],
    gpu_free: bool,
    running: list[int],
    budget: int | None,
) -> list[int]:
    """Which ready arms start now: the first ready GPU arm when the card is free, and the
    ready CPU arms in manifest order while each fits the budget. A CPU arm that does not
    fit holds back the ones after it, so a large arm is never starved by small ones."""
    chosen, shares, cpu_held = [], list(running), False
    for index in ready:
        arm = arms[index]
        if arm.gpu:
            if gpu_free:
                chosen.append(index)
                gpu_free = False
        elif not cpu_held and _fits(arm.threads, shares, budget):
            chosen.append(index)
            shares.append(arm.threads)
        else:
            cpu_held = True
    return chosen


def plan(manifest: Manifest, budget: int | None) -> dict[str, float]:
    """The day as the time boxes say it goes, every arm ok, under the same rules the
    queue starts arms by: when the GPU lane and the CPU arms each end, and how long the
    card waits on a dependency."""
    arms = manifest.arms
    pending = list(range(len(arms)))
    running: dict[int, float] = {}  # index -> end minute
    ended: set[str] = set()
    now = gpu_end = cpu_end = gpu_wait = 0.0
    while pending or running:
        ready = [i for i in pending if all(a in ended for a in arms[i].after)]
        gpu_free = not any(arms[i].gpu for i in running)
        shares = [arms[i].threads for i in running if not arms[i].gpu]
        for index in pick(arms, ready, gpu_free, shares, budget):
            pending.remove(index)
            running[index] = now + arms[index].time_box_min
        if gpu_free and not any(arms[i].gpu for i in running) and any(arms[i].gpu for i in pending):
            waiting_until = min(running.values(), default=now)
            gpu_wait += waiting_until - now
        if not running:
            break
        now = min(running.values())
        for index in [i for i, end in running.items() if end <= now]:
            del running[index]
            ended.add(arms[index].name)
            if arms[index].gpu:
                gpu_end = max(gpu_end, now)
            else:
                cpu_end = max(cpu_end, now)
    return {"gpu_end": gpu_end, "cpu_end": cpu_end, "gpu_wait": gpu_wait, "end": now}


def download_line(manifest: Manifest) -> str:
    """What the arms' preparations download onto an empty volume, each name once."""
    from operations.bakeoff import weights

    names = sorted({n for arm in manifest.arms for n in weights.names_in(arm.prepare)})
    if not names:
        return "downloads onto an empty volume: none"
    sizes = {name: weights.size_of(name) for name in names}
    largest = max(sizes, key=sizes.__getitem__)
    return (
        f"downloads onto an empty volume: {sum(sizes.values()) / 1e9:.1f} GB in {len(names)}"
        f" snapshots (largest {largest}, {sizes[largest] / 1e9:.1f} GB)"
    )


def estimate_lines(manifest: Manifest, here: int | None = None) -> list[str]:
    """One line per pod size: when the GPU lane and the CPU arms end by the time boxes."""
    gpu_box = manifest.planned_min
    cpu_box = sum(arm.time_box_min for arm in manifest.arms if not arm.gpu)
    lines = [f"time boxes: GPU arms {gpu_box:g} min, CPU arms {cpu_box:g} min one after another"]
    sizes = [(f"{cpus} vCPU", cpus) for cpus in ESTIMATE_CPUS]
    if here is not None:
        sizes.insert(0, (f"here, {here} CPUs", here))
    for label, cpus in sizes:
        budget = cpu_budget(manifest.cpu_threads, cpus)
        day = plan(manifest, budget)
        threads = "one CPU arm at a time" if budget is None else f"{budget} CPU threads"
        lines.append(
            f"  {label} ({threads}): GPU lane ends {day['gpu_end']:.0f} min"
            f" (waits {day['gpu_wait']:.0f}), CPU arms end {day['cpu_end']:.0f} min,"
            f" day {day['end'] / 60:.1f} h"
        )
    return lines


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


# rsync forms in the order tried: times kept (a network volume), then contents only,
# written in place with no temporary file to rename (a global volume is object storage:
# no permission bits, no atomic rename, times may be refused).
_RSYNC_FORMS = (("rsync -rt", ["-rt"]), ("rsync -r --inplace", ["-r", "--inplace"]))


def copy_tree(source: Path, target: Path, run: Callable[..., Any] = subprocess.run) -> str:
    """Copy source into target by whichever form the target accepts; the digests prove it.

    Owners are never copied (FUSE volumes refuse chown). When every rsync form fails,
    or there is no rsync, a plain Python copy writes each file directly.
    """
    target.mkdir(parents=True, exist_ok=True)
    if shutil.which("rsync"):
        for method, flags in _RSYNC_FORMS:
            try:
                run(["rsync", *flags, f"{source}/", f"{target}/"], check=True)
            except (subprocess.CalledProcessError, OSError):
                continue
            return method
    for path in sorted(source.rglob("*")):
        destination = target / path.relative_to(source)
        if path.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
            stat = path.stat()
            try:
                os.utime(destination, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            except OSError:
                pass  # object storage keeps no times; the sha256 compare is the proof
    return "python copy"


def with_threads(command: tuple[str, ...], threads: int | None) -> tuple[str, ...]:
    """The command with its `--threads` value set to `threads` (unchanged without either)."""
    if threads is None:
        return command
    out = list(command)
    for index, item in enumerate(out):
        if item == "--threads" and index + 1 < len(out):
            out[index + 1] = str(threads)
        elif item.startswith("--threads="):
            out[index] = f"--threads={threads}"
    return tuple(out)


def write_json_anywhere(path: Path, value: Any) -> None:
    """write_json's atomic write, or a direct write where the folder refuses a rename."""
    try:
        write_json(path, value)
    except OSError:
        path.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")


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
        # Installs and preparations run one at a time per lane (several CPU arms share an
        # environment), so a CPU arm's long install never keeps the card waiting.
        self.ready_pools = {
            lane: ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"ready-{lane}")
            for lane in ("gpu", "cpu")
        }
        self.gpu_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gpu-arm")
        self.cpu_pool = ThreadPoolExecutor(
            max_workers=max(1, len(manifest.arms)), thread_name_prefix="cpu-arm"
        )
        self.started: set[int] = set()
        # Each arm's last outcome: ok, ok-with-failures, smoke-ok, smoke-failed, failed,
        # skipped, deferred, hard-stopped.
        self.outcome: dict[str, str] = {}
        # Arms recorded failed with no retry to come (they answered no page).
        self.final_failed: set[str] = set()
        self.budget: int | None = None
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
        if "gpu" in self.lanes:
            return self.lanes["gpu"]
        return next((lane for key, lane in self.lanes.items() if key.startswith("cpu:")), None)

    def _counted(self, lane: dict[str, Any]) -> tuple[list[float], int]:
        """The lane's arm's pages cached ok, of the smoke's pages or of all, and how many."""
        counted = (
            self.page_list[: self.m.smoke_pages] if lane["phase"] == "smoke" else self.page_list
        )
        return self._ok_pages(lane.get("records", lane["arm"]), counted), len(counted)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            lane = self._lane()
            done = total = 0
            last = eta = eta_min = None
            if lane is not None:
                times, total = self._counted(lane)
                done = len(times)
                last = iso(times[-1]) if times else None
                recent = [t for t in times if t >= lane["wall0"]]
                if len(recent) >= 2:
                    per_page = statistics.median(
                        b - a for a, b in zip(recent, recent[1:], strict=False)
                    )
                    left = max(0, total - done) * per_page
                    eta, eta_min = iso(time.time() + left), round(left / 60, 1)
            cpu_arms = []
            for key, entry in self.lanes.items():
                if key.startswith("cpu:"):
                    ok, of = self._counted(entry)
                    cpu_arms.append(
                        {
                            "arm": entry["arm"],
                            "phase": entry["phase"],
                            "pages_done": len(ok),
                            "pages_total": of,
                            "started": iso(entry["t0"]),
                        }
                    )
            beside = [c["arm"] for c in cpu_arms if lane is None or c["arm"] != lane["arm"]]
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
                "cpu_arm": beside[0] if beside else None,
                "cpu_arms": cpu_arms,
                "cpu_threads": self.budget,
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

    def _arm_env(self, phase: str) -> dict[str, str]:
        """The arm's environment without the queue's secrets. Its commands run offline;
        its install and preparation fetch environments and weights, so they never are."""
        env = {k: v for k, v in self.env.items() if k not in ARM_ENV_DROPPED}
        if phase in ("install", "prepare"):
            for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"):
                env.pop(name, None)
            return env
        return {**env, **OFFLINE_ENV}

    def _spawn(self, arm: ArmSpec, phase: str, argv: list[str]) -> subprocess.Popen:
        if self.terminated:
            raise Terminated()
        folder = self.m.out / arm.name
        folder.mkdir(parents=True, exist_ok=True)
        with open(folder / "queue-arm.log", "ab") as log:
            process = subprocess.Popen(
                argv,
                cwd=self.root,
                env=self._arm_env(phase),
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
        self._ready_next(index)
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
        arm = self.m.arms[index]
        with self.lock:
            if index not in self.ready:
                if arm.install is None and arm.prepare is None:
                    self.ready[index] = Future()
                    self.ready[index].set_result(True)
                else:
                    pool = self.ready_pools["gpu" if arm.gpu else "cpu"]
                    self.ready[index] = pool.submit(self._prepare_arm, arm)
            return self.ready[index]

    def _ready_next(self, index: int) -> None:
        """Prepare the next arm of the same lane that has not started."""
        arm = self.m.arms[index]
        for later in range(index + 1, len(self.m.arms)):
            if self.m.arms[later].gpu == arm.gpu and later not in self.started:
                if self.m.arms[later].name not in self.outcome:
                    self._ready(later)
                return

    def _prepare_arm(self, arm: ArmSpec) -> bool:
        """Install then prepare; False only when the install failed."""
        if arm.install is not None and self._run_plain(arm, "install", list(arm.install)) != 0:
            return False
        if arm.prepare is not None:
            code = self._run_plain(arm, "prepare", list(arm.prepare))
            if code != 0:
                self._error(f"{arm.name}: prepare exit {code}")
        return True

    # dependencies -------------------------------------------------------------------

    def _blocker(self, arm: ArmSpec, final: bool) -> tuple[str, str | None]:
        """What the arm's `after` says: ready, wait, defer (to the end, behind a
        dependency's retry) or skip, with the reason. `final`: no retry is still to come."""
        words = {
            "failed": "failed" if final else "failed (retried at the end)",
            "deferred": "never ran",
            "skipped": "was skipped",
            "hard-stopped": "was hard-stopped",
            "smoke-failed": "failed its smoke",
        }
        found = []
        for name in arm.after:
            state = self.outcome.get(name)
            if state in DEPENDENCY_MET:
                continue
            if state is None:
                found.append(("wait", None))
            elif state in ("failed", "deferred") and not final and name not in self.final_failed:
                found.append(("defer", f"needs {name}, which {words[state]}"))
            else:
                word = "failed" if name in self.final_failed else words.get(state, state)
                found.append(("skip", f"needs {name}, which {word}"))
        for verdict in ("skip", "defer", "wait"):
            for kind, reason in found:
                if kind == verdict:
                    return kind, reason
        return "ready", None

    def _defer(self, arm: ArmSpec, reason: str) -> None:
        """The arm waits for its dependency's retry at the end; it is not an error."""
        with self.lock:
            self.outcome[arm.name] = "deferred"
            self.retry.append(arm)
        self._event("queue-arm-deferred", arm=arm.name, reason=reason)
        self.write_status()

    # arms -------------------------------------------------------------------------

    def _handoff(self, arm: ArmSpec) -> Path:
        return self.m.out / arm.name / "server-handoff.json"

    def _phase_commands(self, arm: ArmSpec, command: tuple[str, ...]) -> tuple[list, list]:
        """(smoke, full run). A witness_run arm's smoke leaves its server up and the full
        run takes it over, so the model loads once per arm, not twice."""
        limit = ["--limit", str(self.m.smoke_pages)]
        if not arm.keeps_server or self.smoke_only:
            return [*command, *limit], list(command)
        handoff = str(self._handoff(arm))
        return [*command, "--keep-server", handoff, *limit], [*command, "--adopt-server", handoff]

    def _release_server(self, arm: ArmSpec) -> None:
        """Stop a server a smoke kept that no full run took over (the hand-off left)."""
        path = self._handoff(arm)
        if not path.is_file():
            return
        from operations.bakeoff.witness_run import Server

        try:
            server = Server.adopted(json.loads(path.read_text("utf-8")))
        except (OSError, ValueError, KeyError, TypeError):
            server = None
        if server is not None:
            server.stop()
        path.unlink(missing_ok=True)
        self._event("queue-server-released", arm=arm.name)

    def _release_servers(self) -> None:
        for arm in self.m.arms:
            if arm.keeps_server:
                self._release_server(arm)

    def _pages_ok(self, arm: ArmSpec, pages: list[Path]) -> bool:
        """Every page read, or failed in a way that sending it again at the same settings
        would repeat (a request timeout or a loop stop); such a page is never retried."""
        return all(settled(self.m.out / arm.records / f"{p.stem}.json") for p in pages)

    def _smoke_verdict(self, arm: ArmSpec, pages: list[Path]) -> str:
        """ "ok" when every smoke page settled and at least one gave an answer; "useless"
        when every page settled but none answered (each ran into a timeout or a loop: the
        full run would repeat that at the same settings, so it is not started, and the
        arm is not retried); "failed" otherwise (an ordinary error, retried at the end)."""
        paths = [self.m.out / arm.records / f"{p.stem}.json" for p in pages]
        if not all(settled(path) for path in paths):
            return "failed"
        return "ok" if any(cached_ok(path) for path in paths) else "useless"

    def _run_status(self, arm: ArmSpec) -> str:
        """A finished run's outcome: "failed" when no page gave an answer (every page ran
        into a timeout or a loop, and sending them again at the same settings would repeat
        it), "ok-with-failures" when some pages failed that way, else "ok"."""
        folder = self.m.out / arm.records
        if not any(cached_ok(folder / f"{p.stem}.json") for p in self.page_list):
            return "failed"
        return "ok-with-failures" if self._failed_pages(arm) else "ok"

    def _no_answers(self, arm: ArmSpec, t0: float, what: str) -> None:
        """Record the arm failed, not retried: its dependents must not run on nothing."""
        self._error(f"{arm.name}: {what}")
        self._event("queue-arm-error", arm=arm.name, detail=what)
        self.ping(f"error:{arm.name}", "milestone", f"arm failed: {arm.name} {what}")
        with self.lock:
            self.final_failed.add(arm.name)
        self._record(arm, "failed", t0)

    def _smoke_useless(self, arm: ArmSpec, t0: float) -> None:
        failed = self._failed_pages(arm)
        self._no_answers(
            arm,
            t0,
            f"smoke gave no answer: all {len(failed)} smoke page(s) failed terminally "
            f"({', '.join(sorted({r for f in failed for r in f['reasons']}))}); "
            "full run not started, not retried at the same settings",
        )

    def _run_useless(self, arm: ArmSpec, t0: float) -> None:
        failed = self._failed_pages(arm)
        self._no_answers(
            arm,
            t0,
            f"run gave no answer: all {len(failed)} page(s) failed terminally "
            f"({', '.join(sorted({r for f in failed for r in f['reasons']}))}); "
            "not retried at the same settings",
        )

    def _failed_pages(self, arm: ArmSpec) -> list[dict[str, Any]]:
        failed = []
        for page in self.page_list:
            failure = terminal_failure(self.m.out / arm.records / f"{page.stem}.json")
            if failure is not None:
                failed.append({"page": page.stem, "reasons": failure.get("reasons") or []})
        return failed

    def _end_lane(self, lane: str) -> None:
        with self.lock:
            entry = self.lanes.pop(lane, None)
            if lane == "gpu" and entry is not None and not entry.get("retry"):
                self.planned_done += self.main_box
                self.main_started = None
        self.write_status()

    def _record(self, arm: ArmSpec, status: str, t0: float) -> None:
        pages = len(self._ok_pages(arm.records))
        failed = self._failed_pages(arm)
        minutes = (time.monotonic() - t0) / 60
        with self.lock:
            self.outcome[arm.name] = status
            self.finished.append(
                {
                    "label": arm.name,
                    "pages": pages,
                    "failed_pages": failed,
                    "wall_seconds": round(minutes * 60, 1),
                    "status": status,
                }
            )
        self._event("queue-arm-end", arm=arm.name, status=status, pages=pages, failed=failed)
        if status in DEPENDENCY_MET:
            reasons: dict[str, int] = {}
            for entry in failed:
                for reason in entry["reasons"]:
                    reasons[reason] = reasons.get(reason, 0) + 1
            note = ", ".join(f"{n} {reason}" for reason, n in sorted(reasons.items()))
            self.ping(
                f"arm-end:{arm.name}",
                "milestone",
                f"{arm.name} {status}: {pages} pages in {minutes:.0f} min"
                + (f"; {len(failed)} failed, not retried ({note})" if failed else ""),
            )
        elif status == "failed":
            after = "not retried" if arm.name in self.final_failed else "after its retry"
            self.ping(f"failed:{arm.name}", "milestone", f"arm failed: {arm.name}, {after}")
        elif status == "smoke-failed":
            self.ping(
                f"failed:{arm.name}", "milestone", f"arm failed: {arm.name} smoke (smoke only)"
            )

    def _arm_error(self, arm: ArmSpec, what: str) -> None:
        """An arm's failure is the queue's own business (it retries the arm at the end), so
        the phone hears a milestone; a decision is asked only when the queue cannot go on."""
        self._error(f"{arm.name}: {what}")
        self._event("queue-arm-error", arm=arm.name, detail=what)
        later = "reported at the end" if self.smoke_only else "retried at the end"
        self.ping(f"error:{arm.name}", "milestone", f"arm failed: {arm.name} {what}; {later}")
        with self.lock:
            self.outcome[arm.name] = "failed"
            self.retry.append(arm)

    def skip(self, arm: ArmSpec, reason: str) -> None:
        with self.lock:
            self.outcome[arm.name] = "skipped"
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
                "records": arm.records,
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
            if arm.keeps_server:
                self._release_server(arm)
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
        smoke, full = self._phase_commands(arm, arm.command)
        code = self._run_watched(index, arm, lane, "smoke", smoke)
        if self.hard_stopped:
            self._record(arm, "hard-stopped", t0)
            return
        verdict = self._smoke_verdict(arm, smoke_pages)
        if code == 0 and verdict == "useless":
            self._smoke_useless(arm, t0)
            return
        if code != 0 or verdict != "ok":
            self._arm_error(arm, f"smoke failed (exit {code})")
            return
        if self.smoke_only:
            self._record(arm, "smoke-ok", t0)
            return
        code = self._run_watched(index, arm, lane, "run", full)
        if self.hard_stopped:
            self._record(arm, "hard-stopped", t0)
        elif self._pages_ok(arm, self.page_list) and self._run_status(arm) == "failed":
            self._run_useless(arm, t0)  # whatever the exit code: a retry would repeat it
        elif code != 0 or not self._pages_ok(arm, self.page_list):
            self._arm_error(arm, f"run incomplete (exit {code})")
        else:
            self._record(arm, self._run_status(arm), t0)

    def _retry_arm(self, arm: ArmSpec, lane: str = "gpu", threads: int | None = None) -> None:
        """The arm's one retry, in its own lane; a CPU arm's `--threads` becomes `threads`."""
        t0 = time.monotonic()
        index = self.m.arms.index(arm)
        never_ran = self.outcome.get(arm.name) == "deferred"
        command = with_threads(arm.command, threads)
        with self.lock:
            self.lanes[lane] = {
                "arm": arm.name,
                "records": arm.records,
                "index": index,
                "phase": "retry",
                "t0": self.clock(),
                "wall0": time.time(),
                "retry": True,
                "threads": threads,
            }
        self._event("queue-arm-retry", arm=arm.name, lane=lane, threads=threads)
        try:
            if never_ran:
                # Its first attempt: install and prepare as any arm does (once).
                ready = self._ready(index)
                if ready is None or not ready.result():
                    self._record(arm, "failed", t0)
                    return
            elif arm.install is not None and self._run_plain(arm, "retry", list(arm.install)):
                self._record(arm, "failed", t0)
                return
            # The smoke again first (cached pages are skipped, so a passed smoke costs nothing).
            smoke, full = self._phase_commands(arm, command)
            code = self._run_watched(index, arm, lane, "retry", smoke)
            verdict = self._smoke_verdict(arm, self.page_list[: self.m.smoke_pages])
            if code == 0 and verdict == "ok":
                code = self._run_watched(index, arm, lane, "retry", full)
            elif code == 0:
                code = -1
            if self.hard_stopped:
                self._record(arm, "hard-stopped", t0)
            elif code == 0 and self._pages_ok(arm, self.page_list):
                self._record(arm, self._run_status(arm), t0)
            else:
                self._record(arm, "failed", t0)
        except Exception as failure:  # noqa: BLE001 -- reported, never dropped
            self._error(f"{arm.name}: retry crashed ({type(failure).__name__}: {failure})"[:200])
            self._record(arm, "failed", t0)
        finally:
            if arm.keeps_server:
                self._release_server(arm)
            self._end_lane(lane)

    def _retry_threads(self, arm: ArmSpec, cpu_retries: int) -> int | None:
        """A CPU retry's share: the lane's whole budget split among the CPU retries, never
        less than the arm's own; None (the arm's own setting) when there is no budget.
        `_run_retries` starts a retry only while the shares running fit the budget."""
        if arm.gpu or self.budget is None:
            return None
        return max(arm.threads, self.budget // max(1, cpu_retries))

    def _run_retries(self) -> None:
        """Every arm retried at the end, once: GPU arms one at a time on the card, CPU arms
        beside them in the CPU lane sharing its whole thread budget. An arm whose `after`
        names an arm still being retried waits for that retry."""
        pending = sorted(self.retry, key=self.m.arms.index)
        cpu_retries = sum(1 for arm in pending if not arm.gpu)
        running: dict[Future, ArmSpec] = {}
        shares: dict[Future, int] = {}  # each running CPU retry's threads
        while pending or running:
            for future in [f for f in running if f.done()]:
                running.pop(future)
                shares.pop(future, None)
                future.result()
            busy = list(running.values())
            unsettled = {a.name for a in pending} | {a.name for a in busy}
            for arm in list(pending):
                if any(name in unsettled for name in arm.after):
                    continue
                state, reason = self._blocker(arm, final=True)
                if state != "ready":
                    pending.remove(arm)
                    self.skip(arm, reason or "a dependency did not finish")
                elif self.smoke_only:
                    pending.remove(arm)
                    self._record(arm, "smoke-failed", time.monotonic())
                elif self.hard_stopped or self.hard_stop_passed():
                    pending.remove(arm)
                    if self.outcome.get(arm.name) == "deferred":
                        self.skip(arm, "hard stop reached")
                    else:
                        self._record(arm, "hard-stopped", time.monotonic())
                elif arm.gpu and any(a.gpu for a in busy):
                    continue
                elif not arm.gpu and self.budget is None and any(not a.gpu for a in busy):
                    continue
                else:
                    threads = self._retry_threads(arm, cpu_retries)
                    if (
                        not arm.gpu
                        and threads is not None
                        and not _fits(threads, list(shares.values()), self.budget)
                    ):
                        continue  # the budget is spent; it starts when a retry ends
                    pending.remove(arm)
                    busy.append(arm)
                    if arm.gpu:
                        future = self.gpu_pool.submit(self._retry_arm, arm, "gpu", None)
                    else:
                        lane = f"cpu:{arm.name}"
                        future = self.cpu_pool.submit(self._retry_arm, arm, lane, threads)
                        if threads is not None:
                            shares[future] = threads
                    running[future] = arm
            if running:
                wait(list(running), timeout=self.poll_seconds, return_when=FIRST_COMPLETED)
            else:
                for arm in pending:  # nothing runs and nothing can start: never loop forever
                    self.skip(arm, "could not start (no lane free)")
                pending.clear()

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
            self._run_retries()
            with self.lock:
                self.retry.clear()
            return self.finish()
        except Terminated:
            return self.terminate()
        finally:
            self._stop_ticker.set()
            for pool in (*self.ready_pools.values(), self.gpu_pool, self.cpu_pool):
                pool.shutdown(wait=False, cancel_futures=True)

    def _run_arms(self) -> None:
        """Start arms as the GPU lane, the CPU budget and their dependencies allow, until
        every arm has run, been skipped or been deferred to the end; returns only when no
        arm is running."""
        self.budget = cpu_budget(self.m.cpu_threads, available_cpus())
        self._event("queue-lanes", cpu_threads=self.budget, cpus=available_cpus())
        pending = list(range(len(self.m.arms)))
        running: dict[Future, int] = {}
        while True:
            for future in [f for f in running if f.done()]:
                running.pop(future)
                future.result()
            while pending and self._start_ready(pending, running):
                pass
            if not running:
                if pending:  # nothing runs and nothing can start: never loop forever
                    for index in list(pending):
                        self.skip(self.m.arms[index], "could not start (no lane free)")
                return
            wait(list(running), timeout=self.poll_seconds, return_when=FIRST_COMPLETED)

    def _start_ready(self, pending: list[int], running: dict[Future, int]) -> bool:
        """Settle what can be settled now; True when an arm left `pending` (a skip frees
        its lane, so the caller asks again)."""
        before = len(pending)
        stop = self.hard_stopped or self.hard_stop_passed()
        ready = []
        for index in list(pending):
            arm = self.m.arms[index]
            state, reason = self._blocker(arm, final=self.smoke_only)
            if stop or state in ("skip", "defer"):
                pending.remove(index)
                if stop:
                    self.skip(arm, "hard stop reached")
                elif state == "skip":
                    self.skip(arm, reason or "a dependency did not finish")
                else:
                    self._defer(arm, reason or "a dependency failed")
            elif state == "ready":
                ready.append(index)
        busy = [running[f] for f in running]
        gpu_free = not any(self.m.arms[i].gpu for i in busy)
        shares = [self.m.arms[i].threads for i in busy if not self.m.arms[i].gpu]
        for index in pick(self.m.arms, ready, gpu_free, shares, self.budget):
            arm = self.m.arms[index]
            pending.remove(index)
            reason = skip_reason(arm, self.behind_min(), self.m.behind_schedule_min)
            if reason:
                self.skip(arm, reason)
                continue
            self.started.add(index)
            if arm.gpu:
                running[self.gpu_pool.submit(self.run_arm, index, arm, "gpu")] = index
            else:
                lane = f"cpu:{arm.name}"
                running[self.cpu_pool.submit(self.run_arm, index, arm, lane)] = index
        return len(pending) < before

    def terminate(self) -> int:
        self.terminated = True
        self._stop_all(30)
        self._release_servers()
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

    def verify_at_home(self) -> bool:
        """An own-disk cache whose pod is kept: `fetch` copies it home from that disk and
        checks every file against DONE.json, so the copy on the volume is not read back
        here (35,408 small files on a network disk took longer than the whole copy)."""
        return self.m.own_disk and self.m.end_pod == "none"

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
        for pool in (*self.ready_pools.values(), self.gpu_pool, self.cpu_pool):
            pool.shutdown(wait=True, cancel_futures=True)
        method, mismatched = None, []
        at_home = self.verify_at_home()
        try:
            method = copy_tree(self.m.out, self.m.sync_to)
            digests = tree_digests(self.m.out)
            if not at_home:
                mismatched = compare_digests(digests, self.m.sync_to)
            failure = None
        except (OSError, subprocess.CalledProcessError) as error:
            digests, failure = {}, f"{type(error).__name__}: {error}"
        # None: not checked on the pod; `fetch` checks every file at home.
        verified: bool | None = failure is None and not mismatched
        if at_home and failure is None:
            verified = None
        done = {
            "schema": DONE_SCHEMA,
            "queue": self.m.name,
            "pod_id": self.pod_id,
            "finished": iso(time.time()),
            "copy": method,
            "verified": verified,
            "verify": "at home, by fetch" if at_home else "on the pod",
            "mismatched": mismatched,
            "failure": failure,
            "files": len(digests),
            "digests": digests,
            "status": self.summary(),
        }
        for folder in (self.m.out, self.m.sync_to):
            try:
                folder.mkdir(parents=True, exist_ok=True)
                write_json_anywhere(folder / "DONE.json", done)
            except OSError as error:
                self._error(f"DONE.json not written to {folder}: {error}")
        self._event("queue-sync", verified=verified, files=len(digests), mismatched=mismatched)
        if verified is None:
            self.ping("sync", "milestone", f"copy made: {len(digests)} files; verify at home")
        elif verified:
            self.ping("sync", "milestone", f"copy verified: {len(digests)} files")
        else:
            self._error(f"copy not verified: {failure or f'{len(mismatched)} files differ'}")
            # Not a decision by itself: an own-disk pod is then kept and that end asks one.
            self.ping("sync", "milestone", f"copy NOT verified ({len(mismatched)} files differ)")
        return done

    def finish(self) -> int:
        if self.smoke_only:
            with self.lock:
                self.state, self.end_action = "done", "kept (smoke only)"
            counts = self.summary()["arms"]
            self.ping("done", DONE_EVENT, f"smoke run finished {counts}; pod kept")
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
            DONE_EVENT,
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
    beside = [
        f"{c.get('arm')} {c.get('pages_done')}/{c.get('pages_total')}"
        for c in status.get("cpu_arms") or []
        if c.get("arm") != status.get("arm")
    ]
    if beside:
        parts.append("cpu: " + "; ".join(beside))
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
            checked = "at home by fetch" if verified is None and done else verified
            say(f"queue done: copy verified {checked}, {files} files; pod {action}")
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
    if done.get("verified") is False:
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
    for line in estimate_lines(manifest, available_cpus()):
        print(line)
    print(download_line(manifest))
    for index, arm in enumerate(manifest.arms):
        lane = "gpu" if arm.gpu else f"cpu, {arm.threads} threads"
        after = f", after {', '.join(arm.after)}" if arm.after else ""
        print(f"{index + 1}. {arm.name}: {arm.time_box_min:g} min, cut {arm.cut}, {lane}{after}")
        if arm.writes:
            print(f"   pages counted in: {manifest.out / arm.writes}")
        for what, argv in (("install", arm.install), ("prepare", arm.prepare)):
            if argv:
                print(f"   {what}: {shlex.join(argv)}")
        print(f"   smoke: {shlex.join([*arm.command, '--limit', str(manifest.smoke_pages)])}")
        print(f"   run:   {shlex.join(arm.command)}")
    disk = " (own disk: the pod is kept unless the copy verifies)" if manifest.own_disk else ""
    print(
        f"then copy {manifest.out} -> {manifest.sync_to}, verify, end pod: {manifest.end_pod}{disk}"
    )
    return 0


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "status", "end-pod", "validate"):
        p = sub.add_parser(name)
        p.add_argument("--manifest", type=Path, required=True)
        p.add_argument(
            "--sync-to",
            help="copy the cache here instead of the manifest's sync_to (a global volume's mount)",
        )
        p.add_argument(
            "--own-disk",
            action="store_true",
            help="the cache is on the pod's own disk: refuse to end the pod unless the copy verified",
        )
        p.add_argument(
            "--keep-pod",
            action="store_true",
            help="end_pod = none for this run: the pod is kept for a fetch and deleted by hand",
        )
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
        manifest = override_manifest(
            load_manifest(args.manifest), args.sync_to, args.own_disk, args.keep_pod
        )
    except ManifestError as failure:
        print(f"manifest refused: {failure}", file=sys.stderr)
        return 2
    if args.command == "validate":
        found = "found" if manifest.pages.is_dir() else "not found here"
        print(f"ok: {manifest.name}, {len(manifest.arms)} arms, plan {manifest.planned_min:g} min")
        print(f"pages folder {manifest.pages}: {found}")
        for line in estimate_lines(manifest):
            print(line)
        print(download_line(manifest))
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
