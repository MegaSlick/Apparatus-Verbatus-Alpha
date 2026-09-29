"""The stage timing journal's GPU reading, driven by an injected runner, never nvidia-smi."""

import threading
from types import SimpleNamespace

from pipeline.orchestrator.run import GPU_QUERY, GpuSampler


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


def test_samples_are_recorded_with_mean_max_and_busy_fraction():
    run, calls = _runner([_ok("100, 10\n"), _ok("50, 20\n"), _ok("96, 30\n"), _ok("94, 40\n")])
    with GpuSampler(run=run, interval=0.001) as sampler:
        while len(calls) < 4:
            pass
    calls_at_stop = len(calls)
    result, reason = sampler.result()

    assert reason is None and calls[0] == list(GPU_QUERY)
    count = result["sample_count"]
    assert count >= 4 and count <= calls_at_stop + 1
    assert result["samples"][:2] == [
        {"utilization_percent": 100, "memory_used_mib": 10},
        {"utilization_percent": 50, "memory_used_mib": 20},
    ]
    assert result["max"] == 100
    assert result["mean"] == sum(s["utilization_percent"] for s in result["samples"]) / count
    assert result["busy_fraction_over_95"] == (
        sum(s["utilization_percent"] > 95 for s in result["samples"]) / count
    )


def test_absent_nvidia_smi_is_not_measured_rather_than_zero():
    run, _ = _runner([FileNotFoundError("nvidia-smi")])
    with GpuSampler(run=run, interval=0.001) as sampler:
        pass

    result, reason = sampler.result()
    assert result is None and "FileNotFoundError" in reason


def test_a_failing_query_is_not_measured_and_a_torn_reading_is_counted_apart():
    run, _ = _runner([SimpleNamespace(returncode=9, stdout="", stderr="no driver")])
    with GpuSampler(run=run, interval=0.001) as sampler:
        pass
    result, reason = sampler.result()
    assert result is None and "exited 9" in reason and "no driver" in reason

    run, calls = _runner([_ok("garbage\n"), _ok("80, 5\n")])
    with GpuSampler(run=run, interval=0.001) as sampler:
        while len(calls) < 3:
            pass
    result, _ = sampler.result()
    assert result["failed_reads"] == 1 and result["max"] == 80


def test_the_stored_list_is_capped_but_the_statistics_cover_every_read(monkeypatch):
    monkeypatch.setattr("pipeline.orchestrator.run.GPU_SAMPLES_KEPT", 2)
    run, calls = _runner([_ok("10, 1\n"), _ok("20, 1\n"), _ok("30, 1\n")])
    with GpuSampler(run=run, interval=0.001) as sampler:
        while len(calls) < 3:
            pass
    result, _ = sampler.result()
    assert len(result["samples"]) == 2 and result["sample_count"] >= 3
    assert result["mean"] <= 30 and result["max"] == 30


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
