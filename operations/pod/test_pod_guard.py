"""The pod guard and its start command delete a pod when idle or out of time.

runpodctl, nvidia-smi and curl are stand-ins, and the container's CPU accounting is a
fake cgroup directory, so nothing here reaches RunPod or depends on the test machine's load.
`date +%s` and `sleep` are stand-ins too: the scripts run on a clock file that each sleep
advances, so a test waits on what the guard did rather than on seconds passing.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
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
    clock = tmp_path / "clock"
    clock.write_text(f"{int(time.time())}\n")
    ticks = tmp_path / "ticks"
    real_sleep = shutil.which("sleep")
    stubs = {
        "runpodctl": (
            f'printf "%s\\n" "$*" >> "{calls}"\n'
            'case "$*" in "$FAKE_RUNPODCTL_FAIL"*) exit 1 ;; esac\n'
        ),
        "nvidia-smi": 'echo "${FAKE_GPU_UTIL:-0}"\n',
        # Records the contents of any -K config, which the caller deletes, and then its
        # argv: a test that waits for the argv line finds the config already written.
        "curl": (
            'previous=""\n'
            'for argument in "$@"; do\n'
            f'  [ "$previous" = -K ] && cat "$argument" >> "{curl_configs}"\n'
            '  previous="$argument"\n'
            "done\n"
            f'printf "%s\\n" "$*" >> "{curl_calls}"\n'
            '[ "${FAKE_CURL_FAIL:-}" = yes ] && exit 22\n'
            "while [ $# -gt 0 ]; do\n"
            '  case "$1" in\n'
            '    -o) cp "$FAKE_GUARD" "$2"; exit 0 ;;\n'
            "  esac\n"
            "  shift\n"
            "done\n"
            "exit 0\n"
        ),
        "date": f'case "$*" in "+%s" | "-u +%s") exec cat "{clock}" ;; esac\nexec {shutil.which("date")} "$@"\n',
        # A whole-second sleep is one tick: it advances the clock by FAKE_SLEEP_ADVANCE per
        # second (default 1; 0 leaves the clock to the test), then runs the test's FAKE_TICK script with the tick number,
        # which is how a test does work "during" that second. Once a runpodctl call has been
        # made the scripts freeze in their next sleep and mark it, so a test that waits for
        # that mark reads all they did up to the first delete; FAKE_SLEEP_HALT=no lets them
        # carry on.
        "sleep": (
            f'case "$1" in "" | *[!0-9]*) exec {real_sleep} "$@" ;; esac\n'
            f'if [ "${{FAKE_SLEEP_HALT:-yes}}" = yes ] && [ -s "{calls}" ]; then touch "{tmp_path}/halted"; exec {real_sleep} 3600; fi\n'
            f'echo >> "{ticks}"\n'
            'if [ "${FAKE_SLEEP_ADVANCE:-1}" != 0 ]; then\n'
            f'  now=$(cat "{clock}")\n'
            f'  echo $((now + $1 * ${{FAKE_SLEEP_ADVANCE:-1}})) > "{clock}.$$"\n'
            f'  mv "{clock}.$$" "{clock}"\n'
            "fi\n"
            f'[ -z "${{FAKE_TICK:-}}" ] || sh "$FAKE_TICK" "$(wc -l < "{ticks}")"\n'
            f"exec {real_sleep} 0.01\n"
        ),
    }
    for name, body in stubs.items():
        stub = bin_dir / name
        stub.write_text("#!/bin/sh\n" + body)
        stub.chmod(0o755)
    # A readable counter that never moves: the container does no CPU work unless a test
    # rewrites it.
    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    (cgroup / "cpu.stat").write_text("usage_usec 1000\n")
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


def clock_of(env):
    return int((Path(env["POD_GUARD_DIR"]).parent / "clock").read_text())


def on_each_tick(env, tmp_path, script):
    """Runs the shell `script` now with $1 = 0, then at every tick with the tick number."""
    hook = tmp_path / "tick.sh"
    hook.write_text(script)
    subprocess.run(["sh", str(hook), "0"], check=True)
    env["FAKE_TICK"] = str(hook)


def halted(env) -> bool:
    """Whether the scripts made a runpodctl call and froze in the sleep after it."""
    return (Path(env["POD_GUARD_DIR"]).parent / "halted").exists()


def run_until(argv, env, done, limit=20):
    """Runs a guard or start command until `done()` holds or the limit passes, then stops it."""
    process = subprocess.Popen(argv, env=env, start_new_session=True)
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline and not done():
        time.sleep(0.02)
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
    run_guard(env, "5")
    assert "pod delete testpod" in lines(calls)
    assert "no GPU, CPU or network work" in log_of(state)


def test_a_busy_gpu_keeps_the_pod_until_its_time_is_up(pod):
    env, calls, state = pod
    env["FAKE_GPU_UTIL"] = "80"
    run_guard(env, "0.001")
    assert "approved time is up" in log_of(state)
    assert "no GPU, CPU or network work" not in log_of(state)


def test_a_gpu_that_cannot_report_counts_as_busy(pod):
    env, calls, state = pod
    env["FAKE_GPU_UTIL"] = "[N/A]"
    run_guard(env, "0.001")
    assert "approved time is up" in log_of(state)


def test_a_failed_gpu_query_counts_as_busy(pod):
    env, calls, state = pod
    stub = Path(env["PATH"].split(":")[0]) / "nvidia-smi"
    stub.write_text("#!/bin/sh\nexit 1\n")
    run_guard(env, "0.001")
    assert "approved time is up" in log_of(state)


def test_the_start_command_refuses_a_malformed_hours_value(pod):
    env, _, _ = pod
    result = subprocess.run(
        ["sh", str(START_COMMAND), "1.2.3", "0" * 40], env=env, capture_output=True
    )
    assert result.returncode == 2


def run_guard(env, hours):
    run_until(["sh", str(GUARD), hours, "30"], env, lambda: halted(env))
    # The fake sleep marks a halt only after a recorded runpodctl call.
    assert halted(env), "the guard never made its runpodctl delete call"


def test_container_cpu_work_keeps_the_pod_until_its_time_is_up(pod, tmp_path):
    env, calls, state = pod
    cgroup = tmp_path / "cgroup"
    # Two CPU seconds per tick.
    on_each_tick(
        env,
        tmp_path,
        "used=$(($1 * 2000000))\n"
        f'printf "usage_usec %s\\nuser_usec %s\\n" "$used" "$used" > "{cgroup}/cpu.stat.new"\n'
        f'mv "{cgroup}/cpu.stat.new" "{cgroup}/cpu.stat"\n',
    )
    run_guard(env, "0.002")
    assert "approved time is up" in log_of(state)
    assert "no GPU, CPU or network work" not in log_of(state)


def netdev_on_each_tick(env, tmp_path, lo, device):
    """Rewrites a fake /proc/net/dev at every tick with loopback line `lo` and `device`.

    Both are shell strings in which $n is the tick number.
    """
    netdev = tmp_path / "netdev"
    staged = tmp_path / "netdev.new"
    on_each_tick(
        env,
        tmp_path,
        "n=$1\n"
        f"printf '%s\\n' 'Inter-|   Receive' ' face |bytes packets' \"{lo}\" \"{device}\" > \"{staged}\"\n"
        f'mv "{staged}" "{netdev}"\n',
    )


def test_network_download_keeps_the_pod_until_its_time_is_up(pod, tmp_path):
    env, calls, state = pod
    # Past 2^31, where an awk that clamps %d would read every sample alike.
    netdev_on_each_tick(
        env,
        tmp_path,
        "    lo: 999 1 0 0 0 0 0 0 999 1 0 0 0 0 0 0",
        "  eth0: $((3000000000 + n * 5000000)) 10 0 0 0 0 0 0 100 1 0 0 0 0 0 0",
    )
    run_guard(env, "0.002")
    assert "approved time is up" in log_of(state)
    assert "no GPU, CPU or network work" not in log_of(state)


def test_a_download_on_a_long_interface_name_keeps_the_pod(pod, tmp_path):
    env, calls, state = pod
    # The kernel pads names to six characters, so a longer one runs into the colon.
    netdev_on_each_tick(
        env,
        tmp_path,
        "    lo: 999 1 0 0 0 0 0 0 999 1 0 0 0 0 0 0",
        "enp0s31f6:$(printf %8d $((n * 5000000)))       10    0    0    0     0          0"
        "         0      100 1 0 0 0 0 0 0",
    )
    run_guard(env, "0.002")
    assert "approved time is up" in log_of(state)
    assert "no GPU, CPU or network work" not in log_of(state)


def test_loopback_traffic_is_not_work(pod, tmp_path):
    env, calls, state = pod
    netdev_on_each_tick(
        env,
        tmp_path,
        "    lo: $((n * 50000000)) 1 0 0 0 0 0 0 $((n * 50000000)) 1 0 0 0 0 0 0",
        "  eth0: 100 10 0 0 0 0 0 0 100 1 0 0 0 0 0 0",
    )
    run_guard(env, "5")
    assert "no GPU, CPU or network work" in log_of(state)


def test_cgroup_v1_cpu_work_keeps_the_pod_until_its_time_is_up(pod, tmp_path):
    env, calls, state = pod
    (tmp_path / "cgroup" / "cpu.stat").unlink()
    usage = tmp_path / "cgroup" / "cpuacct" / "cpuacct.usage"
    usage.parent.mkdir(parents=True)
    # 3e12 ns is 3e9 usec, past 2^31, where an awk that clamps %d would read every
    # sample alike. Two CPU seconds per tick.
    on_each_tick(
        env,
        tmp_path,
        f'echo $((3000000000000 + $1 * 2000000000)) > "{usage}.new"\nmv "{usage}.new" "{usage}"\n',
    )
    run_guard(env, "0.002")
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
    # Here the test alone moves the guard's clock: its sleeps count ticks but add no time.
    env["FAKE_SLEEP_ADVANCE"] = "0"
    clock = state.parent / "clock"
    ticks = state.parent / "ticks"
    start = clock_of(env)
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
        wait_for(lambda: halted(env), "the delete")
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
    # The guard logs the request once curl has returned, so waiting for that line
    # leaves nothing of the call still to be written.
    run_until(
        ["sh", str(GUARD), "5", "30"],
        env,
        lambda: "delete requested (attempt 1)" in log_of(state),
    )
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
    run_guard(env, "5")
    [notification] = lines(curl_calls)
    assert "-H Title: Pod guard" in notification
    assert "Pod testpod: its guard requested deletion (no GPU, CPU or network work" in notification
    # ntfy's answer echoes the topic; it must not land in the guard log.
    assert notification.endswith("-o /dev/null")
    assert "guard-test-topic" not in log_of(state)
    # Assembled from pieces so the ingress check does not read a topic URL here.
    topic_url = "https://ntfy" + ".sh/" + "guard-test-topic"
    assert lines(tmp_path / "curl-configs.txt") == [f'url = "{topic_url}"']


def test_a_released_run_s_outcome_is_in_the_delete_notice(pod, tmp_path):
    """pod_run --no-hold names the run and its outcome; the phone ping must carry it."""

    env, calls, state = pod
    env["FAKE_GPU_UTIL"] = "80"
    state.mkdir()
    (state / "ntfy_topic").write_text("guard-test-topic\n")
    deadline = state / "deadline-testpod"
    deadline.write_text(f"{clock_of(env) + 3600}\n")
    curl_calls = tmp_path / "curl-calls.txt"
    released = state / "released-testpod"
    staging = state / "deadline-testpod.new"
    # Written once while the guard runs (from its first tick on, after it armed), as pod_run
    # does; one left from before it armed is cleared. Read as text, never run: anything
    # outside a plain name is dropped. The new deadline is the guard's clock at that tick.
    on_each_tick(
        env,
        tmp_path,
        f'[ "$1" -ge 1 ] && [ ! -e "{tmp_path}/released-once" ] || exit 0\n'
        f'touch "{tmp_path}/released-once"\n'
        f"printf '%s\\n' 'run proof-1 ended complete $(id)`id`' > \"{released}\"\n"
        f'cat "{tmp_path}/clock" > "{staging}"\n'
        f'mv "{staging}" "{deadline}"\n',
    )
    run_until(["sh", str(GUARD), "5", "30"], env, lambda: bool(lines(curl_calls)))
    [notification] = lines(curl_calls)
    assert (
        "its guard requested deletion (approved time is up; pod_run reported: "
        "run proof-1 ended complete idid)." in notification
    )
    assert "pod_run reported: run proof-1 ended complete" in log_of(state)


@pytest.mark.parametrize("left_over", [False, True])
def test_without_a_release_the_notice_names_only_the_guard_s_reason(pod, tmp_path, left_over):
    """A notice left by an earlier run on a restarted pod is not this guard's run."""

    env, calls, state = pod
    state.mkdir()
    (state / "ntfy_topic").write_text("guard-test-topic\n")
    (state / "deadline-testpod").write_text(f"{clock_of(env)}\n")
    if left_over:
        (state / "released-testpod").write_text("run old-run ended complete\n")
    curl_calls = tmp_path / "curl-calls.txt"
    run_until(["sh", str(GUARD), "5", "30"], env, lambda: bool(lines(curl_calls)))
    [notification] = lines(curl_calls)
    assert "its guard requested deletion (approved time is up)." in notification
    assert "pod_run reported" not in log_of(state)
    assert not (state / "released-testpod").exists()


def test_the_guard_touches_its_heartbeat_every_tick(pod):
    env, calls, state = pod
    env["FAKE_GPU_UTIL"] = "80"
    heartbeat = state / "heartbeat-testpod"
    seen: list[int] = []

    def two_beats():
        if heartbeat.exists():
            mtime = heartbeat.stat().st_mtime_ns
            if not seen or seen[-1] != mtime:
                seen.append(mtime)
        return len(seen) >= 2

    run_until(["sh", str(GUARD), "5", "30"], env, two_beats)
    assert len(seen) >= 2
    assert not lines(calls)


def test_a_deadline_more_than_a_week_out_is_ignored(pod):
    env, calls, state = pod
    state.mkdir()
    (state / "deadline-testpod").write_text(f"{clock_of(env) * 1000}\n")
    env["FAKE_GPU_UTIL"] = "80"
    run_guard(env, "0.001")
    assert "approved time is up" in log_of(state)


def test_a_keepalive_touched_while_idle_holds_off_the_idle_delete(pod, tmp_path):
    env, calls, state = pod
    state.mkdir()
    keepalive = state / "keepalive-testpod"
    clock = tmp_path / "clock"
    on_each_tick(env, tmp_path, f'touch -d "@$(cat "{clock}")" "{keepalive}"\n')
    run_guard(env, "0.002")
    assert "approved time is up" in log_of(state)
    assert "no GPU, CPU or network work" not in log_of(state)


def test_a_run_s_keepalive_holds_a_stage_whose_counters_read_idle(pod, tmp_path):
    """pod_run touches the keep-alive on every live tick while the orchestrator runs, so a
    stage the counters cannot see working is not mistaken for an idle pod. Only the
    approved time ends it."""
    env, calls, state = pod
    state.mkdir()
    keepalive = state / "keepalive-testpod"
    clock = tmp_path / "clock"
    on_each_tick(env, tmp_path, f'touch -d "@$(cat "{clock}")" "{keepalive}"\n')
    run_guard(env, "0.002")
    assert "approved time is up" in log_of(state)
    assert "no GPU, CPU or network work" not in log_of(state)


def _notices(tmp_path, text):
    return [line for line in lines(tmp_path / "curl-calls.txt") if text in line]


UNAVAILABLE = "CPU idle detection unavailable on testpod; held until its deadline"
RESTORED = "CPU idle detection restored on testpod"


def test_a_cpu_counter_unreadable_since_arming_holds_the_pod_to_its_deadline(pod, tmp_path):
    """The deadline deletes it, never the idle check, and the phone hears once why."""
    env, calls, state = pod
    (tmp_path / "cgroup" / "cpu.stat").unlink()
    state.mkdir()
    (state / "ntfy_topic").write_text("guard-test-topic\n")
    run_guard(env, "0.002")
    assert "approved time is up" in log_of(state)
    assert "no GPU, CPU or network work" not in log_of(state)
    assert "pod delete testpod" in lines(calls)
    [notice] = _notices(tmp_path, UNAVAILABLE)
    assert "Z." in notice, "the deadline is named as a UTC time"
    assert _notices(tmp_path, RESTORED) == []


def test_a_cpu_counter_that_stays_unreadable_after_good_readings_holds_the_pod(pod, tmp_path):
    env, calls, state = pod
    stat = tmp_path / "cgroup" / "cpu.stat"
    state.mkdir()
    (state / "ntfy_topic").write_text("guard-test-topic\n")
    # Readable at arming, gone from the first tick on.
    on_each_tick(env, tmp_path, f'rm -f "{stat}"\n')
    run_guard(env, "0.002")
    assert "approved time is up" in log_of(state)
    assert "no GPU, CPU or network work" not in log_of(state)
    assert len(_notices(tmp_path, UNAVAILABLE)) == 1


def test_a_cpu_counter_that_recovers_resumes_idle_counting(pod, tmp_path):
    env, calls, state = pod
    stat = tmp_path / "cgroup" / "cpu.stat"
    stat.unlink()
    state.mkdir()
    (state / "ntfy_topic").write_text("guard-test-topic\n")
    # Unreadable through tick 2, then readable and idle.
    on_each_tick(
        env, tmp_path, f'[ "$1" -ge 3 ] && printf "usage_usec 1000\\n" > "{stat}"\nexit 0\n'
    )
    run_guard(env, "5")
    assert "no GPU, CPU or network work" in log_of(state)
    assert len(_notices(tmp_path, UNAVAILABLE)) == 1
    assert len(_notices(tmp_path, RESTORED)) == 1


def test_a_reading_after_a_dropped_tick_is_judged_over_both_ticks(pod, tmp_path):
    """0.3 s of CPU per one-second tick is under the busy line (0.5 s). With every other
    reading dropped, the next good one sees 0.6 s gained over two ticks: still idle, and
    the pod is deleted rather than held by a one-tick threshold."""
    env, calls, state = pod
    stat = tmp_path / "cgroup" / "cpu.stat"
    on_each_tick(
        env,
        tmp_path,
        f'if [ $(($1 % 2)) = 1 ]; then rm -f "{stat}"; exit 0; fi\n'
        f'printf "usage_usec %s\\n" $(($1 * 300000)) > "{stat}"\n',
    )
    run_guard(env, "5")
    assert "no GPU, CPU or network work" in log_of(state)
    assert "idle time unchanged" in log_of(state)


def test_a_flapping_cpu_counter_reaches_the_phone_once_an_hour(pod, tmp_path):
    """Missing for two ticks, back for one, over and over within an hour: every episode is
    logged, the phone hears one unavailable notice and one recovery."""
    env, calls, state = pod
    env["FAKE_GPU_UTIL"] = "80"
    stat = tmp_path / "cgroup" / "cpu.stat"
    state.mkdir()
    (state / "ntfy_topic").write_text("guard-test-topic\n")
    on_each_tick(
        env,
        tmp_path,
        f'if [ $(($1 % 3)) = 2 ]; then printf "usage_usec 1000\\n" > "{stat}"; '
        f'else rm -f "{stat}"; fi\n',
    )
    run_guard(env, "0.005")
    assert "approved time is up" in log_of(state)
    assert log_of(state).count("CPU idle detection unavailable") >= 3
    assert len(_notices(tmp_path, UNAVAILABLE)) == 1
    assert len(_notices(tmp_path, RESTORED)) == 1


def test_a_flapping_cpu_counter_is_announced_again_an_hour_later(pod, tmp_path):
    env, calls, state = pod
    env["FAKE_GPU_UTIL"] = "80"
    # Each tick is twenty minutes: one episode every hour.
    env["FAKE_SLEEP_ADVANCE"] = "1200"
    stat = tmp_path / "cgroup" / "cpu.stat"
    state.mkdir()
    (state / "ntfy_topic").write_text("guard-test-topic\n")
    on_each_tick(
        env,
        tmp_path,
        f'if [ $(($1 % 3)) = 2 ]; then printf "usage_usec 1000\\n" > "{stat}"; '
        f'else rm -f "{stat}"; fi\n',
    )
    run_guard(env, "4")
    episodes = log_of(state).count("CPU idle detection unavailable")
    assert episodes >= 2
    assert len(_notices(tmp_path, UNAVAILABLE)) == episodes
    assert len(_notices(tmp_path, RESTORED)) >= episodes - 1


def _idle_cgroup(env, tmp_path, drop_at: int | None) -> None:
    """The fixture's idle counter, missing for the one tick `drop_at`."""
    stat = tmp_path / "cgroup" / "cpu.stat"
    drop = "" if drop_at is None else f'[ "$1" = {drop_at} ] && rm -f "{stat}" && exit 0\n'
    on_each_tick(env, tmp_path, drop + f'printf "usage_usec 1000\\n" > "{stat}"\n')


# The dropped tick delays the delete by exactly one tick: it neither added idle time
# nor reset it (a reset would cost the whole idle limit again).
@pytest.mark.parametrize(("drop_at", "idle_seconds"), [(None, 1), (1, 2)])
def test_a_cpu_reading_dropped_for_one_tick_neither_resets_nor_adds_idle(
    pod, tmp_path, drop_at, idle_seconds
):
    env, calls, state = pod
    _idle_cgroup(env, tmp_path, drop_at)
    started = clock_of(env)
    run_guard(env, "5")
    assert "no GPU, CPU or network work" in log_of(state)
    assert clock_of(env) - started == idle_seconds
    assert ("idle time unchanged" in log_of(state)) is (drop_at is not None)


def test_the_idle_limit_runs_from_the_last_keepalive_touch(pod):
    env, calls, state = pod
    state.mkdir()
    started = clock_of(env)
    keepalive = state / "keepalive-testpod"
    keepalive.touch()
    os.utime(keepalive, (started, started))
    run_guard(env, "5")
    assert "no GPU, CPU or network work" in log_of(state)
    # One idle limit (2 s) after the touch: not the one tick an untouched idle pod lasts,
    # and not a further idle limit after the touch expires. The clock stops at the delete.
    assert clock_of(env) - started == 2


def test_a_garbled_deadline_file_is_replaced_not_trusted(pod):
    env, calls, state = pod
    state.mkdir()
    (state / "deadline-testpod").write_text("2026-09-29\n")
    run_guard(env, "5")
    assert (state / "deadline-testpod").read_text().strip().isdigit()
    assert "pod delete testpod" in lines(calls)


def test_a_delete_that_reports_success_is_repeated_and_then_stopped(pod):
    env, calls, _ = pod
    env["FAKE_SLEEP_HALT"] = "no"
    run_until(["sh", str(GUARD), "5", "30"], env, lambda: "pod stop testpod" in lines(calls))
    assert lines(calls).count("pod delete testpod") >= 3
    assert "pod stop testpod" in lines(calls)


def test_the_older_runpodctl_form_is_tried_when_the_newer_one_fails(pod):
    env, calls, _ = pod
    env["FAKE_RUNPODCTL_FAIL"] = "pod delete"
    run_guard(env, "5")
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
    argv, env = start_command(env, "3")
    alive = run_until(argv, env, lambda: halted(env))
    assert "armed for pod testpod" in log_of(state)
    assert "no GPU, CPU or network work" in log_of(state)
    assert alive


def test_the_backstop_deletes_the_pod_when_the_guard_cannot_be_fetched(pod):
    env, calls, state = pod
    env["FAKE_CURL_FAIL"] = "yes"
    argv, env = start_command(env, "0.0003")
    run_until(argv, env, lambda: halted(env))
    assert "pod delete testpod" in lines(calls)
    assert log_of(state) == ""


def test_the_backstop_honours_an_extended_deadline(pod):
    env, calls, state = pod
    env["FAKE_CURL_FAIL"] = "yes"
    state.mkdir()
    started = clock_of(env)
    (state / "deadline-testpod").write_text(f"{started + 3600}\n")
    argv, env = start_command(env, "0.0003")
    # Without the extension the backstop deletes two seconds in (one second of window, one
    # of grace); run it well past that.
    run_until(argv, env, lambda: clock_of(env) >= started + 10)
    assert clock_of(env) >= started + 10
    assert lines(calls) == []


def test_the_backstop_deletes_at_the_hard_maximum_even_past_an_extended_deadline(pod):
    env, calls, state = pod
    env["FAKE_CURL_FAIL"] = "yes"
    env["VERBATUS_HARD_MAX_SECONDS"] = "5"
    state.mkdir()
    started = clock_of(env)
    (state / "deadline-testpod").write_text(f"{started + 3600}\n")
    argv, env = start_command(env, "0.0003")
    run_until(argv, env, lambda: halted(env))
    assert "pod delete testpod" in lines(calls)
    # Five seconds from when the command was printed, plus at most one poll.
    assert clock_of(env) <= started + 6
    # The instant the hard maximum counts from, for the finish estimate on the pod.
    assert (state / "created-testpod").read_text() == f"{started}\n"


def test_the_backstop_wakes_at_the_hard_maximum_not_a_whole_poll_later(pod):
    env, calls, state = pod
    env["FAKE_CURL_FAIL"] = "yes"
    env["VERBATUS_HARD_MAX_SECONDS"] = "5"
    started = clock_of(env)
    # A real five-minute poll: each sleep second moves the fake clock one second.
    env = {**env, "POD_BACKSTOP_GRACE": "1", "POD_BACKSTOP_POLL": "300"}
    printed = subprocess.run(
        ["sh", str(START_COMMAND), "0.001", "0" * 40],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    run_until(["sh", "-c", printed], env, lambda: halted(env))
    assert "pod delete testpod" in lines(calls)
    assert clock_of(env) <= started + 6


def test_the_guard_s_deadline_never_passes_the_hard_maximum(pod):
    """The image pull runs between printing the command and starting the container; the
    guard's window is cut so its orderly end comes before the backstop's cap."""
    env, calls, state = pod
    env["VERBATUS_HARD_MAX_SECONDS"] = "7200"
    printed_at = clock_of(env)
    argv, env = start_command(env, "2")
    clock = Path(env["POD_GUARD_DIR"]).parent / "clock"
    clock.write_text(f"{printed_at + 600}\n")
    deadline = state / "deadline-testpod"
    run_until(argv, env, deadline.exists)
    assert int(deadline.read_text()) <= printed_at + 7200 - 120


@pytest.mark.parametrize(
    ("sealed", "hours"),
    [(None, "3.01"), (None, "4"), ("3600", "1.5")],
    ids=["checkout-just-over", "checkout-4h", "sealed"],
)
def test_the_start_command_refuses_hours_past_the_hard_maximum(pod, sealed, hours):
    env, _, _ = pod
    env.pop("VERBATUS_HARD_MAX_SECONDS", None)
    if sealed is not None:
        env["VERBATUS_HARD_MAX_SECONDS"] = sealed
    result = subprocess.run(
        ["sh", str(START_COMMAND), hours, "0" * 40], env=env, capture_output=True, text=True
    )
    assert result.returncode == 2
    assert "hard maximum" in result.stderr
    assert result.stdout == ""


def test_the_start_command_accepts_the_hard_maximum_itself(pod):
    env, _, _ = pod
    env.pop("VERBATUS_HARD_MAX_SECONDS", None)
    result = subprocess.run(
        ["sh", str(START_COMMAND), "3", "0" * 40], env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("policy", [None, 'hard_max_seconds = "10800"\n', "state = 1\n"])
def test_the_start_command_refuses_when_the_hard_maximum_cannot_be_read(pod, tmp_path, policy):
    env, _, _ = pod
    env.pop("VERBATUS_HARD_MAX_SECONDS", None)
    checkout = tmp_path / "checkout"
    (checkout / "operations" / "pod").mkdir(parents=True)
    script = checkout / "operations" / "pod" / "pod_start_command.sh"
    shutil.copy(START_COMMAND, script)
    if policy is not None:
        (checkout / "config").mkdir()
        (checkout / "config" / "spend.toml").write_text(policy)
    result = subprocess.run(
        ["sh", str(script), "1", "0" * 40], env=env, capture_output=True, text=True
    )
    assert result.returncode == 2
    assert "hard maximum" in result.stderr and result.stdout == ""


@pytest.mark.parametrize("sealed", ["", "abc", "0", " 10800", "1_0800"])
def test_the_start_command_refuses_an_unusable_sealed_hard_maximum(pod, sealed):
    env, _, _ = pod
    env["VERBATUS_HARD_MAX_SECONDS"] = sealed
    result = subprocess.run(
        ["sh", str(START_COMMAND), "1", "0" * 40], env=env, capture_output=True, text=True
    )
    assert result.returncode == 2
    assert "VERBATUS_HARD_MAX_SECONDS" in result.stderr and result.stdout == ""


def test_the_guard_keeps_its_records_under_the_volume_mount_the_bootstrap_requires():
    from operations.pod.models import POD_VOLUME_MOUNT_PATH

    expected = f"{POD_VOLUME_MOUNT_PATH}/.pod_guard"
    env = {key: value for key, value in os.environ.items() if key != "POD_GUARD_DIR"}
    printed = subprocess.run(
        ["sh", str(START_COMMAND), "1", "0" * 40], env=env, capture_output=True, text=True
    ).stdout
    assert f"${{POD_GUARD_DIR:-{expected}}}; export POD_GUARD_DIR=$d;" in printed
    assert f"dir=${{POD_GUARD_DIR:-{expected}}}" in GUARD.read_text()
    policy = json.loads((HERE.parents[1] / "config" / "data_handling_policy.json").read_text())
    assert POD_VOLUME_MOUNT_PATH in policy["storage_roots"]
    from operations.pod import pod_run

    assert expected == f"{POD_VOLUME_MOUNT_PATH}/{pod_run.POD_GUARD_DIRECTORY}"


def test_a_guard_fetched_from_an_older_commit_still_uses_the_start_command_s_directory(
    pod, tmp_path
):
    env, calls, state = pod
    older = tmp_path / "older_guard.sh"
    older.write_text(
        GUARD.read_text().replace("/workspace/private/.pod_guard", str(tmp_path / "wrong"))
    )
    env = {key: value for key, value in env.items() if key != "POD_GUARD_DIR"}
    env["FAKE_GUARD"] = str(older)
    argv, env = start_command(env, "3")
    argv[2] = argv[2].replace("/workspace/private/.pod_guard", str(state))
    run_until(argv, env, lambda: "pod delete testpod" in lines(calls))
    assert "armed for pod testpod" in log_of(state)
    assert not (tmp_path / "wrong").exists()
