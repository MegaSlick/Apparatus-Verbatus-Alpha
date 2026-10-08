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
