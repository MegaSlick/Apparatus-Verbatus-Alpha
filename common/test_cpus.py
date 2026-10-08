from common import cpus


def _cpu_max(tmp_path, text):
    path = tmp_path / "cpu.max"
    path.write_text(text)
    return path


def test_a_cgroup_quota_caps_the_cpus_the_affinity_mask_offers(tmp_path, monkeypatch):
    monkeypatch.setattr(cpus.os, "sched_getaffinity", lambda pid: set(range(32)), raising=False)

    assert cpus.usable_cpus(_cpu_max(tmp_path, "800000 100000\n")) == 8
    assert cpus.usable_cpus(_cpu_max(tmp_path, "250000 100000\n")) == 2
    assert cpus.usable_cpus(_cpu_max(tmp_path, "50000 100000\n")) == 1


def test_no_quota_leaves_the_affinity_mask(tmp_path, monkeypatch):
    monkeypatch.setattr(cpus.os, "sched_getaffinity", lambda pid: {0, 1, 2}, raising=False)

    assert cpus.usable_cpus(_cpu_max(tmp_path, "max 100000\n")) == 3
    assert cpus.usable_cpus(tmp_path / "absent") == 3
    assert cpus.usable_cpus(_cpu_max(tmp_path, "garbage")) == 3


def test_without_an_affinity_mask_the_machine_count_is_used(tmp_path, monkeypatch):
    monkeypatch.delattr(cpus.os, "sched_getaffinity", raising=False)
    monkeypatch.setattr(cpus.os, "cpu_count", lambda: None)

    assert cpus.usable_cpus(tmp_path / "absent") == 1


def test_pool_workers_are_bounded_by_cpus_tasks_and_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(cpus, "usable_cpus", lambda: 16)
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 99999999 kB\nMemAvailable: 1048576 kB\n")  # 1 GiB

    assert cpus.pool_workers(4, bytes_per_task=1, meminfo=meminfo) == 4
    assert cpus.pool_workers(100, bytes_per_task=1, meminfo=meminfo) == 16
    assert cpus.pool_workers(100, bytes_per_task=256 << 20, meminfo=meminfo) == 4
    assert cpus.pool_workers(100, bytes_per_task=4 << 30, meminfo=meminfo) == 1
    assert cpus.pool_workers(100, bytes_per_task=1, meminfo=tmp_path / "absent") == 16
