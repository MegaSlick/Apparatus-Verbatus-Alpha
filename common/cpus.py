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
# A ceiling on CPU-bound pool workers, for a machine that must not run every core
# flat out (a laptop that overheats); unset, the usable CPUs decide alone.
POOL_WORKERS_ENV = "VERBATUS_POOL_WORKERS"
IO_WORKERS_MIN = 2
IO_WORKERS_MAX = 32
CGROUP_CPU_MAX = Path("/sys/fs/cgroup/cpu.max")
MEMINFO = Path("/proc/meminfo")
CGROUP_ROOT = Path("/sys/fs/cgroup")
# cgroup v1 writes "no limit" as 2**63 rounded down to a page.
_CGROUP_UNLIMITED = 1 << 62
# Kept for the process that starts a pool: its own pages, records and results.
PARENT_RESERVE_BYTES = 512 << 20


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


def _cgroup_value(path: Path) -> int | None:
    """One cgroup memory file's byte count, or None where it is absent, unreadable or
    says there is no limit (`max` in v2, a page-rounded 2**63 in v1)."""

    try:
        text = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None
    if text == "max":
        return None
    try:
        value = int(text)
    except ValueError:
        return None
    return value if 0 <= value < _CGROUP_UNLIMITED else None


def cgroup_memory_bytes(cgroup_root: Path = CGROUP_ROOT) -> int | None:
    """What this container's memory limit leaves free, or None where it sets none.

    cgroup v2 names the limit and the use in `memory.max` and `memory.current`;
    v1 in `memory.limit_in_bytes` and `memory.usage_in_bytes`, under the
    `memory` controller's own mount or the root itself.
    """

    candidates = (
        (cgroup_root / "memory.max", cgroup_root / "memory.current"),
        (
            cgroup_root / "memory" / "memory.limit_in_bytes",
            cgroup_root / "memory" / "memory.usage_in_bytes",
        ),
        (cgroup_root / "memory.limit_in_bytes", cgroup_root / "memory.usage_in_bytes"),
    )
    for limit_path, usage_path in candidates:
        limit = _cgroup_value(limit_path)
        if limit is not None:
            return max(0, limit - (_cgroup_value(usage_path) or 0))
    return None


def available_memory_bytes(meminfo: Path = MEMINFO, cgroup_root: Path = CGROUP_ROOT) -> int | None:
    """The memory this process can still take: the kernel's MemAvailable, never
    more than the container's cgroup limit leaves, or None where neither says."""

    budgets = [
        budget
        for budget in (_meminfo_available(meminfo), cgroup_memory_bytes(cgroup_root))
        if budget is not None
    ]
    return min(budgets) if budgets else None


def _meminfo_available(meminfo: Path) -> int | None:
    """MemAvailable, which on a container is its host's, not its own."""

    try:
        for line in meminfo.read_text(encoding="ascii").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, UnicodeDecodeError, ValueError, IndexError):
        return None
    return None


def _pool_worker_ceiling() -> int:
    """`POOL_WORKERS_ENV` as a positive count, or no ceiling when it is unset."""
    raw = os.environ.get(POOL_WORKERS_ENV)
    if raw is None:
        return 1 << 30
    try:
        count = int(raw)
    except ValueError:
        count = 0
    if count < 1:
        raise ValueError(f"{POOL_WORKERS_ENV}={raw!r} is not a positive worker count")
    return count


def pool_workers(
    tasks: int,
    *,
    bytes_per_task: int,
    meminfo: Path = MEMINFO,
    cgroup_root: Path = CGROUP_ROOT,
) -> int:
    """Processes for `tasks` independent CPU-bound tasks each holding about
    `bytes_per_task`: the usable CPUs, no more than the tasks, no more than the
    available memory holds at once after `PARENT_RESERVE_BYTES` is kept for the
    process that starts them, no more than `POOL_WORKERS_ENV` when it is set, and
    one at the least. One worker is what the
    caller runs in its own process, without a pool."""

    workers = min(usable_cpus(), tasks, _pool_worker_ceiling())
    memory = available_memory_bytes(meminfo, cgroup_root)
    if memory is not None:
        workers = min(workers, max(0, memory - PARENT_RESERVE_BYTES) // max(1, bytes_per_task))
    return max(1, workers)
