"""Usable CPUs and disk-worker counts inside a container's share of its host."""

import pytest

from common import cpus


def test_a_cgroup_quota_below_the_affinity_mask_is_the_usable_count(tmp_path, monkeypatch):
    cpu_max = tmp_path / "cpu.max"
    cpu_max.write_text("250000 100000\n", encoding="ascii")
    monkeypatch.setattr(cpus.os, "sched_getaffinity", lambda pid: set(range(64)), raising=False)

    assert cpus.usable_cpus(cpu_max) == 3


@pytest.mark.parametrize("text", ["max 100000\n", "garbage\n", "0 100000\n", ""])
def test_no_quota_leaves_the_affinity_mask(tmp_path, monkeypatch, text):
    cpu_max = tmp_path / "cpu.max"
    cpu_max.write_text(text, encoding="ascii")
    monkeypatch.setattr(cpus.os, "sched_getaffinity", lambda pid: {0, 1, 2, 3}, raising=False)

    assert cpus.usable_cpus(cpu_max) == 4
    assert cpus.usable_cpus(tmp_path / "absent") == 4


def test_without_an_affinity_mask_the_machine_count_is_used(tmp_path, monkeypatch):
    monkeypatch.delattr(cpus.os, "sched_getaffinity", raising=False)
    monkeypatch.setattr(cpus.os, "cpu_count", lambda: 6)

    assert cpus.usable_cpus(tmp_path / "absent") == 6


@pytest.mark.parametrize(("usable", "workers"), [(1, 2), (8, 8), (200, 32)])
def test_io_workers_clamp_usable_cpus_to_two_through_thirty_two(
    tmp_path, monkeypatch, usable, workers
):
    monkeypatch.setattr(cpus, "usable_cpus", lambda cpu_max: usable)

    chosen = cpus.io_workers({})

    assert chosen == cpus.IoWorkers(workers, "usable-cpus")
    assert chosen.to_record() == {"workers": workers, "source": "usable-cpus"}


def test_the_operator_override_wins_and_is_named_as_the_source():
    chosen = cpus.io_workers({cpus.IO_WORKERS_ENV: "5"})

    assert chosen.to_record() == {"workers": 5, "source": "VERBATUS_IO_WORKERS"}


@pytest.mark.parametrize("raw", ["0", "33", "four", ""])
def test_an_override_that_is_not_a_worker_count_is_refused(raw):
    with pytest.raises(ValueError, match="VERBATUS_IO_WORKERS"):
        cpus.io_workers({cpus.IO_WORKERS_ENV: raw})


GIB = 1 << 30
MIB = 1 << 20


def _meminfo(tmp_path, available_bytes):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(
        f"MemTotal: 99999999 kB\nMemAvailable: {available_bytes // 1024} kB\n", encoding="ascii"
    )
    return meminfo


def _cgroup(tmp_path, files: dict[str, str]):
    root = tmp_path / "cgroup"
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="ascii")
    root.mkdir(exist_ok=True)
    return root


def test_pool_workers_are_bounded_by_cpus_tasks_and_memory_after_the_parent_s_reserve(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(cpus, "usable_cpus", lambda *args: 16)
    meminfo = _meminfo(tmp_path, 1536 * MIB)  # 1 GiB once the parent's 512 MiB is kept
    no_cgroup = _cgroup(tmp_path, {})

    def workers(tasks, per_task, meminfo=meminfo):
        return cpus.pool_workers(
            tasks, bytes_per_task=per_task, meminfo=meminfo, cgroup_root=no_cgroup
        )

    assert workers(4, 1) == 4
    assert workers(100, 1) == 16
    assert workers(100, 256 * MIB) == 4
    assert workers(100, 4 * GIB) == 1
    assert workers(100, 1, meminfo=tmp_path / "absent") == 16
    assert workers(0, 1) == 1
    assert (
        cpus.pool_workers(
            100, bytes_per_task=1, meminfo=_meminfo(tmp_path, 100 * MIB), cgroup_root=no_cgroup
        )
        == 1
    )


def test_a_named_ceiling_caps_the_pool_workers_and_a_bad_one_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(cpus, "usable_cpus", lambda *args: 16)
    no_cgroup = _cgroup(tmp_path, {})

    def workers():
        return cpus.pool_workers(
            100, bytes_per_task=1, meminfo=tmp_path / "absent", cgroup_root=no_cgroup
        )

    monkeypatch.setenv(cpus.POOL_WORKERS_ENV, "2")
    assert workers() == 2
    monkeypatch.setenv(cpus.POOL_WORKERS_ENV, "64")
    assert workers() == 16
    for bad in ("0", "two"):
        monkeypatch.setenv(cpus.POOL_WORKERS_ENV, bad)
        with pytest.raises(ValueError, match=cpus.POOL_WORKERS_ENV):
            workers()


def test_a_cgroup_v2_limit_below_the_host_s_memory_caps_the_workers(tmp_path, monkeypatch):
    monkeypatch.setattr(cpus, "usable_cpus", lambda *args: 32)
    host = _meminfo(tmp_path, 512 * GIB)
    cgroup = _cgroup(
        tmp_path, {"memory.max": f"{4 * GIB}\n", "memory.current": f"{1 * GIB + 512 * MIB}\n"}
    )

    assert cpus.cgroup_memory_bytes(cgroup) == 2 * GIB + 512 * MIB
    assert cpus.available_memory_bytes(host, cgroup) == 2 * GIB + 512 * MIB
    assert cpus.pool_workers(100, bytes_per_task=512 * MIB, meminfo=host, cgroup_root=cgroup) == 4


def test_a_cgroup_v1_limit_is_read_where_v2_names_none(tmp_path, monkeypatch):
    monkeypatch.setattr(cpus, "usable_cpus", lambda *args: 32)
    host = _meminfo(tmp_path, 512 * GIB)
    cgroup = _cgroup(
        tmp_path,
        {
            "memory/memory.limit_in_bytes": f"{3 * GIB}\n",
            "memory/memory.usage_in_bytes": f"{1 * GIB}\n",
        },
    )

    assert cpus.cgroup_memory_bytes(cgroup) == 2 * GIB
    assert cpus.pool_workers(100, bytes_per_task=512 * MIB, meminfo=host, cgroup_root=cgroup) == 3


def test_a_cgroup_that_sets_no_limit_leaves_the_host_s_memory(tmp_path):
    host = _meminfo(tmp_path, 8 * GIB)
    v2 = _cgroup(tmp_path / "v2", {"memory.max": "max\n", "memory.current": "12345\n"})
    v1 = _cgroup(
        tmp_path / "v1",
        {"memory/memory.limit_in_bytes": "9223372036854771712\n"},
    )

    for cgroup in (v2, v1):
        assert cpus.cgroup_memory_bytes(cgroup) is None
        assert cpus.available_memory_bytes(host, cgroup) == 8 * GIB


def test_a_cgroup_already_full_leaves_one_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(cpus, "usable_cpus", lambda *args: 32)
    cgroup = _cgroup(tmp_path, {"memory.max": f"{GIB}\n", "memory.current": f"{2 * GIB}\n"})

    assert cpus.cgroup_memory_bytes(cgroup) == 0
    assert (
        cpus.pool_workers(
            100, bytes_per_task=1, meminfo=_meminfo(tmp_path, 64 * GIB), cgroup_root=cgroup
        )
        == 1
    )
