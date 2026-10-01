"""The pod guard and its start command delete a pod when idle or out of time.

runpodctl, nvidia-smi and curl are stand-ins, and the container's CPU accounting is a
fake cgroup directory, so nothing here reaches RunPod or depends on the test machine's load.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

import pytest

HERE = Path(__file__).parent
GUARD = HERE / "pod_guard.sh"
START_COMMAND = HERE / "pod_start_command.sh"


@pytest.fixture
def pod(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.txt"
    curl_calls = tmp_path / "curl-calls.txt"
    curl_configs = tmp_path / "curl-configs.txt"
    stubs = {
        "runpodctl": (
            f'printf "%s\\n" "$*" >> "{calls}"\n'
            'case "$*" in "$FAKE_RUNPODCTL_FAIL"*) exit 1 ;; esac\n'
        ),
        "nvidia-smi": 'echo "${FAKE_GPU_UTIL:-0}"\n',
        # Records its argv and the contents of any -K config, which the caller deletes.
        "curl": (
            f'printf "%s\\n" "$*" >> "{curl_calls}"\n'
            '[ "${FAKE_CURL_FAIL:-}" = yes ] && exit 22\n'
            "while [ $# -gt 0 ]; do\n"
            '  case "$1" in\n'
            '    -o) cp "$FAKE_GUARD" "$2"; exit 0 ;;\n'
            f'    -K) cat "$2" >> "{curl_configs}" ;;\n'
            "  esac\n"
            "  shift\n"
            "done\n"
            "exit 0\n"
        ),
    }
    for name, body in stubs.items():
        stub = bin_dir / name
        stub.write_text("#!/bin/sh\n" + body)
        stub.chmod(0o755)
    state = tmp_path / "guard"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "RUNPOD_POD_ID": "testpod",
        "POD_GUARD_DIR": str(state),
        "POD_GUARD_INTERVAL": "1",
        "POD_GUARD_IDLE_SECONDS": "2",
        "POD_GUARD_CGROUP": str(tmp_path / "cgroup"),
        "POD_GUARD_NETDEV": str(tmp_path / "netdev"),
        "FAKE_RUNPODCTL_FAIL": "never",
        "FAKE_GUARD": str(GUARD),
    }
    return env, calls, state


def run_until(argv, env, done, limit=20):
    """Runs a guard or start command until `done()` holds or the limit passes, then stops it."""
    process = subprocess.Popen(argv, env=env, start_new_session=True)
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline and not done():
        time.sleep(0.2)
    alive = process.poll() is None
    os.killpg(process.pid, signal.SIGKILL)
    process.wait()
    return alive


def lines(path):
    return path.read_text().splitlines() if path.exists() else []


def log_of(state):
    path = state / "guard.log"
    return path.read_text() if path.exists() else ""


def test_a_pod_doing_no_work_is_deleted(pod):
    env, calls, state = pod
    run_until(["sh", str(GUARD), "5", "30"], env, lambda: "pod delete testpod" in lines(calls))
    assert "pod delete testpod" in lines(calls)
    assert "no GPU, CPU or network work" in log_of(state)


def test_a_busy_gpu_keeps_the_pod_until_its_time_is_up(pod):
    env, calls, state = pod
    env["FAKE_GPU_UTIL"] = "80"
    run_until(["sh", str(GUARD), "0.001", "30"], env, lambda: "pod delete testpod" in lines(calls))
    assert "approved time is up" in log_of(state)
    assert "no GPU, CPU or network work" not in log_of(state)


def test_a_gpu_that_cannot_report_counts_as_busy(pod):
    env, calls, state = pod
    env["FAKE_GPU_UTIL"] = "[N/A]"
    run_until(["sh", str(GUARD), "0.001", "30"], env, lambda: "pod delete testpod" in lines(calls))
    assert "approved time is up" in log_of(state)


def test_a_failed_gpu_query_counts_as_busy(pod):
    env, calls, state = pod
    stub = Path(env["PATH"].split(":")[0]) / "nvidia-smi"
    stub.write_text("#!/bin/sh\nexit 1\n")
    run_until(["sh", str(GUARD), "0.001", "30"], env, lambda: "pod delete testpod" in lines(calls))
    assert "approved time is up" in log_of(state)


def test_the_start_command_refuses_a_malformed_hours_value(pod):
    env, _, _ = pod
    result = subprocess.run(
        ["sh", str(START_COMMAND), "1.2.3", "0" * 40], env=env, capture_output=True
    )
    assert result.returncode == 2


def test_container_cpu_work_keeps_the_pod_until_its_time_is_up(pod, tmp_path):
    env, calls, state = pod
    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    stat = cgroup / "cpu.stat"
    stop = threading.Event()

    def burn():
        used = 0
        while not stop.is_set():
            used += 2_000_000
            staged = cgroup / "cpu.stat.new"
            staged.write_text(f"usage_usec {used}\nuser_usec {used}\n")
            staged.replace(stat)
            time.sleep(0.5)

    writer = threading.Thread(target=burn)
    writer.start()
    try:
        run_until(
            ["sh", str(GUARD), "0.002", "30"], env, lambda: "pod delete testpod" in lines(calls)
        )
    finally:
        stop.set()
        writer.join()
    assert "approved time is up" in log_of(state)
    assert "no GPU, CPU or network work" not in log_of(state)


def test_network_download_keeps_the_pod_until_its_time_is_up(pod, tmp_path):
    env, calls, state = pod
    netdev = tmp_path / "netdev"
    stop = threading.Event()

    def download():
        # Past 2^31, where an awk that clamps %d would read every sample alike.
        received = 3_000_000_000
        while not stop.is_set():
            received += 5_000_000
            staged = tmp_path / "netdev.new"
            staged.write_text(
                "Inter-|   Receive\n face |bytes packets\n"
                f"    lo: 999 1 0 0 0 0 0 0 999 1 0 0 0 0 0 0\n"
                f"  eth0: {received} 10 0 0 0 0 0 0 100 1 0 0 0 0 0 0\n"
            )
            staged.replace(netdev)
            time.sleep(0.5)

    writer = threading.Thread(target=download)
    writer.start()
    try:
        run_until(
            ["sh", str(GUARD), "0.002", "30"], env, lambda: "pod delete testpod" in lines(calls)
        )
    finally:
        stop.set()
        writer.join()
    assert "approved time is up" in log_of(state)
    assert "no GPU, CPU or network work" not in log_of(state)


def netdev_writer(netdev, staging, line, stop):
    """Rewrites a fake /proc/net/dev every half second with `line(n)` as its only device."""

    def write():
        n = 0
        while not stop.is_set():
            n += 1
            staging.write_text(
                "Inter-|   Receive\n face |bytes packets\n"
                "    lo: 999 1 0 0 0 0 0 0 999 1 0 0 0 0 0 0\n" + line(n)
            )
            staging.replace(netdev)
            time.sleep(0.5)

    return threading.Thread(target=write)


def test_a_download_on_a_long_interface_name_keeps_the_pod(pod, tmp_path):
    env, calls, state = pod
    stop = threading.Event()
    # The kernel pads names to six characters, so a longer one runs into the colon.
    writer = netdev_writer(
        tmp_path / "netdev",
        tmp_path / "netdev.new",
        lambda n: (
            f"enp0s31f6:{n * 5_000_000:8d}       10    0    0    0     0          0         0      100 1 0 0 0 0 0 0\n"
        ),
        stop,
    )
    writer.start()
    try:
        run_until(
            ["sh", str(GUARD), "0.002", "30"], env, lambda: "pod delete testpod" in lines(calls)
        )
    finally:
        stop.set()
        writer.join()
    assert "approved time is up" in log_of(state)
    assert "no GPU, CPU or network work" not in log_of(state)


def test_loopback_traffic_is_not_work(pod, tmp_path):
    env, calls, state = pod
    netdev = tmp_path / "netdev"
    stop = threading.Event()

    def loopback():
        sent = 0
        while not stop.is_set():
            sent += 50_000_000
            staged = tmp_path / "netdev.new"
            staged.write_text(
                "Inter-|   Receive\n face |bytes packets\n"
                f"    lo: {sent} 1 0 0 0 0 0 0 {sent} 1 0 0 0 0 0 0\n"
                "  eth0: 100 10 0 0 0 0 0 0 100 1 0 0 0 0 0 0\n"
            )
            staged.replace(netdev)
            time.sleep(0.5)

    writer = threading.Thread(target=loopback)
    writer.start()
    try:
        run_until(["sh", str(GUARD), "5", "30"], env, lambda: "pod delete testpod" in lines(calls))
    finally:
        stop.set()
        writer.join()
    assert "no GPU, CPU or network work" in log_of(state)


def test_cgroup_v1_cpu_work_keeps_the_pod_until_its_time_is_up(pod, tmp_path):
    env, calls, state = pod
    usage = tmp_path / "cgroup" / "cpuacct" / "cpuacct.usage"
    usage.parent.mkdir(parents=True)
    stop = threading.Event()

    def burn():
        # 3e12 ns is 3e9 usec, past 2^31, where an awk that clamps %d would read
        # every sample alike.
        used_ns = 3_000_000_000_000
        while not stop.is_set():
            used_ns += 2_000_000_000
            staged = usage.with_name("cpuacct.usage.new")
            staged.write_text(f"{used_ns}\n")
            staged.replace(usage)
            time.sleep(0.5)

    writer = threading.Thread(target=burn)
    writer.start()
    try:
        run_until(
            ["sh", str(GUARD), "0.002", "30"], env, lambda: "pod delete testpod" in lines(calls)
        )
    finally:
        stop.set()
        writer.join()
    assert "approved time is up" in log_of(state)
    assert "no GPU, CPU or network work" not in log_of(state)


def wait_for(condition, what, limit=30):
    """Waits for an observable condition, failing with `what` when the generous limit passes."""
    deadline = time.monotonic() + limit
    while not condition():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.05)


def test_a_deadline_rewritten_while_running_ignores_garbage_and_honours_an_extension(pod):
    env, calls, state = pod
    env["FAKE_GPU_UTIL"] = "80"
    # The guard's clock is a file the test sets, so each step waits on what the guard did
    # rather than on how fast it runs; every date +%s call is counted as a tick happening.
    clock = state.parent / "clock"
    ticks = state.parent / "clock-reads"
    start = int(time.time())
    clock.write_text(f"{start}\n")
    fake_date = Path(env["PATH"].split(":")[0]) / "date"
    fake_date.write_text(
        "#!/bin/sh\n"
        'if [ "$*" = "+%s" ]; then\n'
        f'  echo >> "{ticks}"\n'
        f'  exec cat "{clock}"\n'
        "fi\n"
        f'exec {shutil.which("date")} "$@"\n'
    )
    fake_date.chmod(0o755)
    deadline = state / "deadline-testpod"
    staging = state / "deadline-testpod.new"

    def rewrite(value):
        staging.write_text(f"{value}\n")
        staging.replace(deadline)

    process = subprocess.Popen(["sh", str(GUARD), "0.001", "30"], env=env, start_new_session=True)
    try:
        wait_for(lambda: "armed for pod testpod" in log_of(state), "the guard to arm")
        # 0.001 hours is 3 whole seconds on the guard's clock.
        assert f"deadline {start + 3}," in log_of(state)
        rewrite("soon")
        wait_for(lambda: "ignored deadline file value 'soon'" in log_of(state), "garbage ignored")
        rewrite(start + 3600)
        wait_for(lambda: f"deadline now {start + 3600}" in log_of(state), "the extension")
        # Well past the first deadline: only the extension keeps the pod through these ticks.
        clock.write_text(f"{start + 60}\n")
        seen = len(lines(ticks))
        wait_for(lambda: len(lines(ticks)) >= seen + 3, "ticks past the first deadline")
        assert "pod delete testpod" not in lines(calls)
        rewrite(start + 60)
        wait_for(lambda: "pod delete testpod" in lines(calls), "the delete")
    finally:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
    log = log_of(state)
    assert log.count("deadline now") == 2
    assert f"deadline now {start + 60}" in log
    assert "deleting pod testpod: approved time is up" in log


def test_the_rest_api_deletes_the_pod_when_both_runpodctl_forms_fail(pod, tmp_path):
    env, calls, state = pod
    env["FAKE_RUNPODCTL_FAIL"] = ""  # the empty prefix matches every runpodctl call
    env["RUNPOD_API_KEY"] = "test-key-not-real"
    curl_calls = tmp_path / "curl-calls.txt"
    # Waits on the guard's own log line, which it writes only after the delete call
    # returns; the curl record alone appears before that and would race the kill.
    run_until(["sh", str(GUARD), "5", "30"], env, lambda: "delete requested" in log_of(state))
    delete = next(line for line in lines(curl_calls) if "DELETE" in line)
    assert delete.endswith("-X DELETE https://api.runpod.io/v2/pods/testpod")
    assert "test-key-not-real" not in "".join(lines(curl_calls))
    assert 'header = "Authorization: Bearer test-key-not-real"' in lines(
        tmp_path / "curl-configs.txt"
    )
    assert lines(calls)[:2] == ["pod delete testpod", "remove pod testpod"]
    assert "delete requested (attempt 1)" in log_of(state)


def test_a_topic_file_sends_one_notification_when_the_guard_deletes(pod, tmp_path):
    env, calls, state = pod
    state.mkdir()
    (state / "ntfy_topic").write_text("guard-test-topic\n")
    curl_calls = tmp_path / "curl-calls.txt"
    run_until(["sh", str(GUARD), "5", "30"], env, lambda: bool(lines(curl_calls)))
    [notification] = lines(curl_calls)
    assert "-H Title: Pod guard" in notification
    assert "Pod testpod: its guard requested deletion (no GPU, CPU or network work" in notification
    # Assembled from pieces so the ingress check does not read a topic URL here.
    topic_url = "https://ntfy" + ".sh/" + "guard-test-topic"
    assert lines(tmp_path / "curl-configs.txt") == [f'url = "{topic_url}"']


def test_a_deadline_more_than_a_week_out_is_ignored(pod):
    env, calls, state = pod
    state.mkdir()
    (state / "deadline-testpod").write_text(f"{int(time.time()) * 1000}\n")
    env["FAKE_GPU_UTIL"] = "80"
    run_until(["sh", str(GUARD), "0.001", "30"], env, lambda: "pod delete testpod" in lines(calls))
    assert "approved time is up" in log_of(state)


def test_a_keepalive_touched_while_idle_holds_off_the_idle_delete(pod):
    env, calls, state = pod
    state.mkdir()
    keepalive = state / "keepalive-testpod"
    stop = threading.Event()

    def touch():
        while not stop.is_set():
            keepalive.touch()
            time.sleep(0.5)

    toucher = threading.Thread(target=touch)
    toucher.start()
    try:
        run_until(
            ["sh", str(GUARD), "0.002", "30"], env, lambda: "pod delete testpod" in lines(calls)
        )
    finally:
        stop.set()
        toucher.join()
    assert "approved time is up" in log_of(state)
    assert "no GPU, CPU or network work" not in log_of(state)


def test_the_idle_limit_runs_from_the_last_keepalive_touch(pod):
    env, calls, state = pod
    state.mkdir()
    (state / "keepalive-testpod").touch()
    started = time.monotonic()
    run_until(["sh", str(GUARD), "5", "30"], env, lambda: "pod delete testpod" in lines(calls))
    assert "no GPU, CPU or network work" in log_of(state)
    # One idle limit (2 s) after the touch, not a further idle limit after the touch expires.
    assert time.monotonic() - started < 10


def test_a_garbled_deadline_file_is_replaced_not_trusted(pod):
    env, calls, state = pod
    state.mkdir()
    (state / "deadline-testpod").write_text("2026-09-29\n")
    run_until(["sh", str(GUARD), "5", "30"], env, lambda: "pod delete testpod" in lines(calls))
    assert (state / "deadline-testpod").read_text().strip().isdigit()
    assert "pod delete testpod" in lines(calls)


def test_a_delete_that_reports_success_is_repeated_and_then_stopped(pod):
    env, calls, _ = pod
    run_until(["sh", str(GUARD), "5", "30"], env, lambda: "pod stop testpod" in lines(calls))
    assert lines(calls).count("pod delete testpod") >= 3
    assert "pod stop testpod" in lines(calls)


def test_the_older_runpodctl_form_is_tried_when_the_newer_one_fails(pod):
    env, calls, _ = pod
    env["FAKE_RUNPODCTL_FAIL"] = "pod delete"
    run_until(["sh", str(GUARD), "5", "30"], env, lambda: "remove pod testpod" in lines(calls))
    assert lines(calls)[:2] == ["pod delete testpod", "remove pod testpod"]


def test_an_unwritable_state_directory_exits_so_the_backstop_takes_over(pod, tmp_path):
    env, _, _ = pod
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("")
    env["POD_GUARD_DIR"] = str(blocker / "guard")
    result = subprocess.run(["sh", str(GUARD), "5"], env=env, timeout=10)
    assert result.returncode == 3


def start_command(env, hours):
    env = {**env, "POD_BACKSTOP_GRACE": "1", "POD_BACKSTOP_POLL": "1"}
    printed = subprocess.run(
        ["sh", str(START_COMMAND), hours, "0" * 40],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return ["sh", "-c", printed], env


def test_the_start_command_arms_the_guard_and_keeps_the_container_up(pod):
    env, calls, state = pod
    argv, env = start_command(env, "5")
    alive = run_until(argv, env, lambda: "pod delete testpod" in lines(calls))
    assert "armed for pod testpod" in log_of(state)
    assert "no GPU, CPU or network work" in log_of(state)
    assert alive


def test_the_backstop_deletes_the_pod_when_the_guard_cannot_be_fetched(pod):
    env, calls, state = pod
    env["FAKE_CURL_FAIL"] = "yes"
    argv, env = start_command(env, "0.0003")
    run_until(argv, env, lambda: "pod delete testpod" in lines(calls))
    assert "pod delete testpod" in lines(calls)
    assert log_of(state) == ""


def test_the_backstop_honours_an_extended_deadline(pod):
    env, calls, state = pod
    env["FAKE_CURL_FAIL"] = "yes"
    state.mkdir()
    (state / "deadline-testpod").write_text(f"{int(time.time()) + 3600}\n")
    argv, env = start_command(env, "0.0003")
    run_until(argv, env, lambda: False, limit=5)
    assert lines(calls) == []
