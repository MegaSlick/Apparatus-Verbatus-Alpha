"""How much of the machine a stage may use: CPUs it may run on and memory it may fill.

A pod's container sees every core of its host in `os.cpu_count()` and often in
its affinity mask too, while its cgroup quota allows only a share of them. Work
fanned out past the quota only queues, so every pool is sized here.
"""

from __future__ import annotations

import math
import os
from pathlib import Path

CGROUP_CPU_MAX = Path("/sys/fs/cgroup/cpu.max")
MEMINFO = Path("/proc/meminfo")


def _quota_cpus(cpu_max: Path) -> int | None:
    """The whole CPUs a cgroup v2 `cpu.max` quota allows, or None when it sets none."""
    try:
        fields = cpu_max.read_text().split()
    except OSError:
        return None
    if len(fields) != 2 or fields[0] == "max":
        return None
    try:
        quota, period = int(fields[0]), int(fields[1])
    except ValueError:
        return None
    if quota <= 0 or period <= 0:
        return None
    return max(1, math.floor(quota / period))


def usable_cpus(cpu_max: Path = CGROUP_CPU_MAX) -> int:
    """The CPUs this process can keep busy: its affinity mask, else the machine's
    count, never more than its cgroup quota allows, and one at the least."""
    if hasattr(os, "sched_getaffinity"):
        cpus = len(os.sched_getaffinity(0))
    else:
        cpus = os.cpu_count() or 1
    quota = _quota_cpus(cpu_max)
    if quota is not None:
        cpus = min(cpus, quota)
    return max(1, cpus)


def available_memory_bytes(meminfo: Path = MEMINFO) -> int | None:
    """The memory the kernel says can be taken without swapping, or None where it says nothing."""
    try:
        for line in meminfo.read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def pool_workers(tasks: int, *, bytes_per_task: int, meminfo: Path = MEMINFO) -> int:
    """Workers for `tasks` independent tasks each holding about `bytes_per_task`:
    the usable CPUs, no more than the tasks, no more than the available memory
    holds at once, and one at the least."""
    workers = min(usable_cpus(), tasks)
    memory = available_memory_bytes(meminfo)
    if memory is not None:
        workers = min(workers, memory // max(1, bytes_per_task))
    return max(1, workers)
