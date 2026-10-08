"""The stage timing journal's GPU reading, driven by an injected runner, never nvidia-smi."""

import subprocess
import threading
import time
from functools import partial
from types import SimpleNamespace

from conftest import load_stage

orchestrator = load_stage("orchestrator")
GPU_QUERY = orchestrator.GPU_QUERY
# Every test names the binary, so none depends on this host having one.
GpuSampler = partial(orchestrator.GpuSampler, nvidia_smi="nvidia-smi")


def _runner(outputs):
    calls = []

    def run(command, **_):
        calls.append(command)
        item = outputs[min(len(calls), len(outputs)) - 1]
        if isinstance(item, Exception):
            raise item
        return item

    return run, calls


def _ok(text):
    return SimpleNamespace(returncode=0, stdout=text, stderr="")


def _sample(outputs, *, reads=None):
    """Run a sampler until it has made `reads` calls (default: one per output)."""

    run, calls = _runner(outputs)
    wanted = reads or len(outputs)
    with GpuSampler(run=run, interval=0.001) as sampler:
        deadline = time.monotonic() + 10
        while len(calls) < wanted:
            assert time.monotonic() < deadline, "the sampler thread stopped reading"
            time.sleep(0.001)
    return sampler.result(), calls


def test_samples_are_recorded_with_mean_max_and_busy_fraction():
    (result, reason), calls = _sample([_ok("100, 10\n"), _ok("50, 20\n"), _ok("96, 30\n")])

    assert reason is None and calls[0] == list(GPU_QUERY)
    first = result["samples"][:3]
    assert [s["utilization_percent"] for s in first] == [100, 50, 96]
    assert [s["memory_used_mib"] for s in first] == [10, 20, 30]
    # The last output repeats after the third read, so check against the samples themselves.
    values = [s["utilization_percent"] for s in result["samples"]]
    assert result["mean"] == sum(values) / result["sample_count"]
    assert result["max"] == 100
    assert result["busy_fraction_over_95"] == sum(v > 95 for v in values) / result["sample_count"]


def test_exactly_95_is_not_busy():
    (result, _), _ = _sample([_ok("95, 1\n")])
    assert result["busy_fraction_over_95"] == 0.0 and result["max"] == 95


def test_multi_gpu_is_read_per_card_and_the_busiest_card_counts():
    (result, _), _ = _sample([_ok("10, 100\n99, 200\n")])
    sample = result["samples"][0]
    assert sample["utilization_percent"] == 99 and sample["memory_used_mib"] == 200
    assert sample["cards"] == [
        {"utilization_percent": 10, "memory_used_mib": 100},
        {"utilization_percent": 99, "memory_used_mib": 200},
    ]
    assert result["busy_fraction_over_95"] == 1.0


def test_an_unavailable_memory_field_keeps_the_good_utilisation():
    (result, reason), _ = _sample([_ok("80, [N/A]\n")])
    assert reason is None
    assert result["samples"][0]["utilization_percent"] == 80
    assert result["samples"][0]["memory_used_mib"] is None
    assert result["failed_reads"] == 0


def test_an_unavailable_utilisation_and_blank_output_are_failures_not_zeros():
    for text in ("[N/A], 5\n", "\n", ""):
        (result, reason), _ = _sample([_ok(text)], reads=2)
        assert result is None and "no utilisation reading" in reason


def test_absent_nvidia_smi_is_not_measured_rather_than_zero():
    (result, reason), _ = _sample([FileNotFoundError("nvidia-smi")])
    assert result is None and "FileNotFoundError" in reason


def test_an_nvidia_smi_timeout_is_not_measured():
    (result, reason), _ = _sample([subprocess.TimeoutExpired("nvidia-smi", 10)])
    assert result is None and "TimeoutExpired" in reason


def test_a_failing_query_is_not_measured():
    (result, reason), _ = _sample([SimpleNamespace(returncode=9, stdout="", stderr="no driver")])
    assert result is None and "exited 9" in reason and "no driver" in reason


def test_a_failed_read_among_good_ones_is_counted_and_its_first_reason_kept():
    (result, _), _ = _sample([_ok("garbage\n"), _ok("80, 5\n")], reads=3)
    assert result["failed_reads"] == 1 and result["max"] == 80
    assert "no utilisation reading" in result["first_failure_reason"]


def test_the_cap_keeps_every_nth_sample_but_the_statistics_cover_every_read(monkeypatch):
    monkeypatch.setattr(orchestrator, "GPU_SAMPLES_KEPT", 2)
    run, calls = _runner([_ok("10, 1\n"), _ok("100, 1\n"), _ok("30, 1\n")])
    # Drive reads by hand so the totals are exact.
    sampler = GpuSampler(run=run, interval=60)
    for _ in range(3):
        sampler._read()
    result, _ = sampler.result()

    # Three reads kept to two: every second read, from the stage's first.
    assert result["sample_stride"] == 2
    assert [s["utilization_percent"] for s in result["samples"]] == [10, 30]
    assert result["sample_count"] == 3 and result["mean"] == 140 / 3
    assert result["busy_fraction_over_95"] == 1 / 3 and result["max"] == 100


def test_the_sampler_thread_stops_when_the_stage_raises():
    run, _ = _runner([_ok("1, 1\n")])
    sampler = GpuSampler(run=run, interval=60)
    try:
        with sampler:
            raise RuntimeError("stage blew up")
    except RuntimeError:
        pass

    assert not sampler._thread.is_alive()
    assert "gpu-sampler" not in [t.name for t in threading.enumerate()]
    assert sampler.result()[0]["sample_count"] == 1


def test_a_thread_that_cannot_start_leaves_the_stage_running_and_says_so(monkeypatch):
    def refuse(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", refuse)
    with GpuSampler(run=_runner([_ok("1, 1\n")])[0]) as sampler:
        ran = True

    result, reason = sampler.result()
    assert ran and result is None and "sampling could not start" in reason


def test_a_sampler_thread_that_outlives_the_join_is_abandoned_not_published():
    release = threading.Event()
    calls = []

    def hung(command, **_):
        calls.append(command)
        if len(calls) == 1:
            return _ok("50, 1\n")
        release.wait(10)  # bounded, so a failing test cannot hang
        return _ok("50, 1\n")

    try:
        with GpuSampler(run=hung, interval=0.001, join_timeout=0.05) as sampler:
            deadline = time.monotonic() + 10
            while len(calls) < 2:
                assert time.monotonic() < deadline, "the sampler thread stopped reading"
                time.sleep(0.001)
        result, reason = sampler.result()
    finally:
        release.set()
        sampler._thread.join(timeout=10)

    assert result is None and "did not stop within" in reason


def test_a_host_without_nvidia_smi_starts_no_sampler_and_says_why():
    run, calls = _runner([_ok("1, 1\n")])
    with orchestrator.GpuSampler(run=run, interval=0.001, nvidia_smi=None) as sampler:
        time.sleep(0.01)
    result, reason = sampler.result()
    assert calls == [] and result is None and "not on PATH" in reason
    assert not sampler._started


def test_the_binary_is_run_by_the_path_found_for_it():
    run, calls = _runner([_ok("40, 1\n")])
    sampler = orchestrator.GpuSampler(run=run, interval=60, nvidia_smi="/opt/bin/nvidia-smi")
    sampler._read()
    assert calls == [["/opt/bin/nvidia-smi", *GPU_QUERY[1:]]]
    assert sampler.result()[0]["sample_stride"] == 1


def test_the_sampler_reads_every_fifteen_seconds_by_default():
    assert orchestrator.GPU_SAMPLE_INTERVAL_SECONDS == 15.0
