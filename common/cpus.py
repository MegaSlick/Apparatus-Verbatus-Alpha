"""How many CPUs this process may really use, and how many disk or process workers to run.

A container sees its host's `os.cpu_count()`, often far above its own share. Its
share shows in the affinity mask, or in the cgroup v2 quota in `cpu.max`.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

IO_WORKERS_ENV = "VERBATUS_IO_WORKERS"
IO_WORKERS_MIN = 2
IO_WORKERS_MAX = 32
CGROUP_CPU_MAX = Path("/sys/fs/cgroup/cpu.max")
MEMINFO = Path("/proc/meminfo")


def usable_cpus(cpu_max: Path = CGROUP_CPU_MAX) -> int:
    """The smaller of the affinity mask and the cgroup CPU quota, at least one."""

    if hasattr(os, "sched_getaffinity"):
        count = len(os.sched_getaffinity(0))
    else:
        count = os.cpu_count() or 1
    quota = _cgroup_quota(cpu_max)
    if quota is not None:
        count = min(count, quota)
    return max(1, count)


def _cgroup_quota(path: Path) -> int | None:
    """Whole CPUs granted by `cpu.max` ("<quota> <period>"), or None for no limit."""

    try:
        fields = path.read_text(encoding="ascii").split()
    except (OSError, UnicodeDecodeError):
        return None
    if len(fields) != 2 or fields[0] == "max":
        return None
    try:
        quota, period = int(fields[0]), int(fields[1])
    except ValueError:
        return None
    if quota <= 0 or period <= 0:
        return None
    return max(1, math.ceil(quota / period))


@dataclass(frozen=True, slots=True)
class IoWorkers:
    """A worker count for a pool of file copies or hashes, and where it came from."""

    count: int
    source: str

    def to_record(self) -> dict[str, object]:
        return {"workers": self.count, "source": self.source}


def io_workers(
    environ: Mapping[str, str] | None = None, *, cpu_max: Path = CGROUP_CPU_MAX
) -> IoWorkers:
    """Usable CPUs clamped to 2..32, unless `VERBATUS_IO_WORKERS` names a count.

    Reading model files is disk-bound, so even a one-CPU share keeps two reads in
    flight, and past a few dozen the disk, not the CPU, is the limit. An override
    outside 1..32 or not an integer is refused rather than silently clamped.
    """

    environment = os.environ if environ is None else environ
    raw = environment.get(IO_WORKERS_ENV)
    if raw is not None:
        try:
            count = int(raw)
        except ValueError:
            count = 0
        if not 1 <= count <= IO_WORKERS_MAX:
            raise ValueError(
                f"{IO_WORKERS_ENV}={raw!r} is not a worker count from 1 to {IO_WORKERS_MAX}"
            )
        return IoWorkers(count, IO_WORKERS_ENV)
    count = min(max(usable_cpus(cpu_max), IO_WORKERS_MIN), IO_WORKERS_MAX)
    return IoWorkers(count, "usable-cpus")


def available_memory_bytes(meminfo: Path = MEMINFO) -> int | None:
    """The memory the kernel says can be taken without swapping, or None where it says nothing."""

    try:
        for line in meminfo.read_text(encoding="ascii").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, UnicodeDecodeError, ValueError, IndexError):
        return None
    return None


def pool_workers(tasks: int, *, bytes_per_task: int, meminfo: Path = MEMINFO) -> int:
    """Processes for `tasks` independent CPU-bound tasks each holding about
    `bytes_per_task`: the usable CPUs, no more than the tasks, no more than the
    available memory holds at once, and one at the least."""

    workers = min(usable_cpus(), tasks)
    memory = available_memory_bytes(meminfo)
    if memory is not None:
        workers = min(workers, memory // max(1, bytes_per_task))
    return max(1, workers)
