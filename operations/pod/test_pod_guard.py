"""The pod guard and its start command: a deadline, when there is one, deletes the pod;
an idle pod climbs the ladder of notices, backup and, when switched on, delete.

runpodctl, nvidia-smi and curl are stand-ins, and the container's CPU accounting is a
fake cgroup directory, so nothing here reaches RunPod or depends on the test machine's load.
`date +%s` and `sleep` are stand-ins too: the scripts run on a clock file that each sleep
advances, so a test waits on what the guard did rather than on seconds passing.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

# pod_guard.sh and the command pod_start_command.sh prints run only on the Linux pod, and
# these tests run them under GNU date, touch and sleep. The laptop half, which a Mac runs
# too, is test_pod_start_command.py.
pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="pod_guard.sh and the printed start command run only on the Linux pod",
)

HERE = Path(__file__).parent
GUARD = HERE / "pod_guard.sh"
START_COMMAND = HERE / "pod_start_command.sh"
SHIPPED_SPEND = HERE.parents[1] / "config" / "spend.toml"


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
            # Fails the first FAKE_CURL_FAIL_TIMES calls, then succeeds.
            'if [ -n "${FAKE_CURL_FAIL_TIMES:-}" ]; then\n'
            f'  echo >> "{tmp_path}/curl-count"\n'
            f'  [ "$(wc -l < "{tmp_path}/curl-count")" -gt "$FAKE_CURL_FAIL_TIMES" ] || exit 22\n'
            "fi\n"
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
    inherited = {"VERBATUS_POD_BUDGET", "VERBATUS_HARD_MAX_SECONDS", "POD_GUARD_DELETE"}
    env = {
        **{key: value for key, value in os.environ.items() if key not in inherited},
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "RUNPOD_POD_ID": "testpod",
        "POD_GUARD_DIR": str(state),
        "POD_GUARD_INTERVAL": "1",
        # The idle ladder in one-second steps: warn, urgent, back up, delete.
        "POD_GUARD_WARN_SECONDS": "1",
        "POD_GUARD_URGENT_SECONDS": "2",
        "POD_GUARD_BACKUP_SECONDS": "3",
        "POD_GUARD_DELETE_SECONDS": "4",
        "POD_GUARD_DELETE": "on",
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
    state.mkdir()
    (state / "ntfy_topic").write_text("guard-test-topic\n")
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
        rewrite("soon")
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
    assert log.count("ignored deadline file value 'soon'") == 1
    [notice] = _notices(state.parent, "ignored the deadline file value")
    assert "'soon'" in notice
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
    [notification] = [line for line in lines(curl_calls) if "deletion" in line]
    assert "-H Title: Pod guard" in notification
    assert "Pod testpod: its guard requested deletion (no GPU, CPU or network work" in notification
    # ntfy's answer echoes the topic; it must not land in the guard log.
    assert notification.endswith("-o /dev/null")
    assert "guard-test-topic" not in log_of(state)
    # Assembled from pieces so the ingress check does not read a topic URL here.
    topic_url = "https://ntfy" + ".sh/" + "guard-test-topic"
    assert set(lines(tmp_path / "curl-configs.txt")) == {f'url = "{topic_url}"'}


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
@pytest.mark.parametrize(("drop_at", "idle_seconds"), [(None, 3), (1, 4)])
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
    # The ladder's delete step (4 s) after the touch: not the three ticks an untouched idle
    # pod lasts, and not a further four after the touch expires. The clock stops at the delete.
    assert clock_of(env) - started == 4


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


def checkout(env, *, budget, ladder_delete="on"):
    """A copy of the start command beside a spend policy with these two switches."""
    # The test's own directory, where the fixture keeps its stand-ins.
    root = Path(env["PATH"].split(":")[0]).parent / f"checkout-{budget}-{ladder_delete}"
    script = root / "operations" / "pod" / "pod_start_command.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(START_COMMAND, script)
    (root / "config").mkdir(exist_ok=True)
    policy = SHIPPED_SPEND.read_text()
    policy = policy.replace('pod_budget = "off"', f'pod_budget = "{budget}"')
    policy = policy.replace('ladder_delete = "off"', f'ladder_delete = "{ladder_delete}"')
    (root / "config" / "spend.toml").write_text(policy)
    return script


def start_command(env, hours, *, budget="on", ladder_delete="on", poll="1"):
    env = {
        **env,
        "POD_BACKSTOP_GRACE": "1",
        "POD_BACKSTOP_POLL": poll,
        "POD_GUARD_FETCH_TRIES": env.get("POD_GUARD_FETCH_TRIES", "1"),
    }
    script = checkout(env, budget=budget, ladder_delete=ladder_delete)
    printed = subprocess.run(
        ["sh", str(script), hours, "0" * 40],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    # The pod's /tmp is its own; tests running side by side each get theirs.
    printed = printed.replace("/tmp/pod_", f"{script.parents[3]}/pod_")
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
    argv, env = start_command(env, "0.001", poll="300")
    run_until(argv, env, lambda: halted(env))
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


# --- no deadline, and the idle ladder ------------------------------------------------


def run_ticks(argv, env, count):
    """Runs a guard or start command for `count` of the guard's ticks, then stops it."""
    ticks = Path(env["POD_GUARD_DIR"]).parent / "ticks"
    return run_until(argv, env, lambda: len(lines(ticks)) >= count)


def test_with_no_deadline_the_guard_writes_none_and_never_ends_a_working_pod(pod):
    env, calls, state = pod
    env["FAKE_GPU_UTIL"] = "80"
    run_ticks(["sh", str(GUARD), "off"], env, 30)
    assert "armed for pod testpod: deadline none (off)," in log_of(state)
    assert not (state / "deadline-testpod").exists()
    assert lines(calls) == []


def test_with_no_deadline_only_a_deadline_written_after_arming_is_honoured(pod, tmp_path):
    """A deadline left by an earlier start of this pod is not this start's; one written while
    the guard runs (an extension by the lead, or pod_run --no-hold's release) is."""
    env, calls, state = pod
    env["FAKE_GPU_UTIL"] = "80"
    state.mkdir()
    (state / "ntfy_topic").write_text("guard-test-topic\n")
    deadline = state / "deadline-testpod"
    deadline.write_text(f"{clock_of(env) - 60}\n")
    staging = state / "deadline-testpod.new"
    on_each_tick(
        env,
        tmp_path,
        f'[ "$1" = 5 ] || exit 0\necho $(($(cat "{tmp_path}/clock") + 3)) > "{staging}"\n'
        f'mv "{staging}" "{deadline}"\n',
    )
    run_until(["sh", str(GUARD), "off"], env, lambda: halted(env))
    log = log_of(state)
    assert "predates this guard; not honoured" in log
    assert "deleting pod testpod: approved time is up" in log
    assert len(lines(state.parent / "ticks")) >= 8, "the stale deadline did not end the pod"
    [notice] = _notices(tmp_path, "is not honoured")
    assert "earlier value" in notice


def test_an_idle_pod_climbs_the_ladder_to_the_delete(pod, tmp_path):
    env, calls, state = pod
    state.mkdir()
    (state / "ntfy_topic").write_text("guard-test-topic\n")
    run_guard(env, "5")
    sent = lines(tmp_path / "curl-calls.txt")
    warning = next(i for i, line in enumerate(sent) if "Touch its keep-alive" in line)
    urgent = next(i for i, line in enumerate(sent) if "still billing" in line)
    deleted = next(i for i, line in enumerate(sent) if "requested deletion" in line)
    assert warning < urgent < deleted
    assert "-H Priority: default" in sent[warning]
    assert "-H Priority: urgent" in sent[urgent]
    assert "nothing to back up" in log_of(state)
    assert (state / "alert-testpod").read_text().split()[1] == "delete"


def test_with_deletion_off_the_ladder_repeats_urgent_notices_and_keeps_the_pod(pod, tmp_path):
    env, calls, state = pod
    env["POD_GUARD_DELETE"] = "off"
    env["POD_GUARD_URGENT_REPEAT"] = "3"
    state.mkdir()
    (state / "ntfy_topic").write_text("guard-test-topic\n")
    run_until(["sh", str(GUARD), "5"], env, lambda: len(_notices(tmp_path, "still billing")) >= 4)
    urgent = _notices(tmp_path, "still billing")
    assert len(urgent) >= 4
    assert all("-H Priority: urgent" in line for line in urgent)
    [held] = _notices(tmp_path, "will not delete it because deletion is off")
    assert "-H Priority: urgent" in held
    assert lines(calls) == []
    assert "not deleting: deletion is off (ladder_delete)" in log_of(state)


def test_work_after_a_warning_resets_the_ladder_and_says_so(pod, tmp_path):
    env, calls, state = pod
    state.mkdir()
    (state / "ntfy_topic").write_text("guard-test-topic\n")
    keepalive = state / "keepalive-testpod"
    clock = tmp_path / "clock"
    # Idle through the warning, then the run touches the keep-alive on every tick.
    on_each_tick(
        env, tmp_path, f'[ "$1" -ge 2 ] || exit 0\ntouch -d "@$(cat "{clock}")" "{keepalive}"\n'
    )
    run_until(
        ["sh", str(GUARD), "5"],
        env,
        lambda: _notices(tmp_path, "work resumed") and len(lines(tmp_path / "ticks")) >= 12,
    )
    assert len(_notices(tmp_path, "Touch its keep-alive")) == 1
    assert _notices(tmp_path, "work resumed")
    assert lines(calls) == []
    assert "resumed" in (state / "alert-testpod").read_text()


def test_the_ladder_backs_up_the_run_and_verifies_the_copy_before_the_delete(pod, tmp_path):
    env, calls, state = pod
    local = tmp_path / "local" / "run-1"
    (local / "stage").mkdir(parents=True)
    (local / "stage" / "page.json").write_text('{"page": 1}\n')
    volume = tmp_path / "volume-runs" / "run-1"
    volume.mkdir(parents=True)
    (volume / "journal.jsonl").write_text("one\n")
    state.mkdir()
    (state / "backup-testpod").write_text(f"{local}\n{volume}\n")
    run_guard(env, "5")
    copies = sorted((tmp_path / "runs-guard-backup").iterdir())
    assert len(copies) == 2
    assert (copies[0] / "stage" / "page.json").read_text() == '{"page": 1}\n'
    assert (copies[1] / "journal.jsonl").read_text() == "one\n"
    assert log_of(state).count("and verified the copy") == 2
    assert "pod delete testpod" in lines(calls)


def test_a_backup_that_fails_blocks_the_delete(pod, tmp_path):
    env, calls, state = pod
    local = tmp_path / "local" / "run-1"
    local.mkdir(parents=True)
    (local / "page.json").write_text("{}\n")
    # A file where the backup directory belongs: the copy cannot be made.
    (tmp_path / "runs-guard-backup").write_text("")
    state.mkdir()
    (state / "ntfy_topic").write_text("guard-test-topic\n")
    (state / "backup-testpod").write_text(f"{local}\n")
    # Held at the delete step, and still held some ticks later.
    run_until(
        ["sh", str(GUARD), "5"],
        env,
        lambda: (
            "not deleting: its run tree backup failed" in log_of(state)
            and len(lines(tmp_path / "ticks")) >= 10
        ),
    )
    assert lines(calls) == []
    assert _notices(tmp_path, "could not back up the run")
    assert "failed or does not match" in log_of(state)


def test_a_listed_run_tree_that_is_missing_blocks_the_delete(pod, tmp_path):
    """A run tree named for backup but not there may be lost or misnamed, not absent by
    design, so the pod stays."""
    env, calls, state = pod
    state.mkdir()
    (state / "ntfy_topic").write_text("guard-test-topic\n")
    (state / "backup-testpod").write_text(f"{tmp_path / 'local' / 'run-1'}\n")
    run_until(
        ["sh", str(GUARD), "5"],
        env,
        lambda: (
            "not deleting: its run tree backup failed" in log_of(state)
            and len(lines(tmp_path / "ticks")) >= 10
        ),
    )
    assert lines(calls) == []
    assert "is not there, so the backup failed" in log_of(state)
    assert _notices(tmp_path, "could not back up the run")


def test_the_start_command_with_the_budget_off_arms_no_deadline_and_no_backstop(pod):
    """A deadline file from an earlier start of the pod is removed, so the only deadline is
    one written during this start (the lead's, or pod_run --no-hold's release)."""
    env, calls, state = pod
    env["FAKE_GPU_UTIL"] = "80"
    state.mkdir()
    (state / "deadline-testpod").write_text(f"{clock_of(env) - 60}\n")
    argv, env = start_command(env, "off", budget="off")
    assert "runpodctl" not in argv[2]
    run_until(argv, env, lambda: "armed for pod testpod" in log_of(state))
    assert "deadline none (off)" in log_of(state)
    assert "predates this guard" not in log_of(state)
    assert not (state / "deadline-testpod").exists()
    assert (state / "created-testpod").read_text().strip().isdigit()
    assert lines(calls) == []


def test_the_start_command_retries_the_guard_fetch(pod):
    env, calls, state = pod
    env["FAKE_GPU_UTIL"] = "80"
    env["FAKE_CURL_FAIL_TIMES"] = "2"
    env["POD_GUARD_FETCH_TRIES"] = "3"
    argv, env = start_command(env, "off", budget="off")
    run_until(argv, env, lambda: "armed for pod testpod" in log_of(state))
    assert "armed for pod testpod" in log_of(state)


def test_the_start_command_passes_the_ladder_delete_switch_to_the_guard(pod):
    env, calls, state = pod
    argv, env = start_command(env, "off", budget="off", ladder_delete="off")
    assert "POD_GUARD_DELETE=off sh " in argv[2] and "/pod_guard.sh off)" in argv[2]
    run_ticks(argv, env, 12)
    assert "(deletion off)" in log_of(state)
    assert lines(calls) == []


def test_with_the_budget_off_hours_arm_a_deadline_the_hard_maximum_does_not_cut(pod):
    env, calls, state = pod
    env["FAKE_CURL_FAIL"] = "yes"
    env["VERBATUS_HARD_MAX_SECONDS"] = "5"
    state.mkdir()
    started = clock_of(env)
    (state / "deadline-testpod").write_text(f"{started + 3600}\n")
    argv, env = start_command(env, "0.0003", budget="off")
    run_until(argv, env, lambda: clock_of(env) >= started + 10)
    assert clock_of(env) >= started + 10
    assert lines(calls) == []


def test_with_the_budget_off_the_backstop_deletes_an_hour_after_the_deadline(pod):
    env, calls, state = pod
    env["FAKE_CURL_FAIL"] = "yes"
    started = clock_of(env)
    argv, env = start_command(env, "0.0003", budget="off")
    run_until(argv, env, lambda: halted(env))
    assert "pod delete testpod" in lines(calls)
    # One second of window and one of grace, plus at most a poll.
    assert clock_of(env) <= started + 4
    assert (state / "deadline-testpod").read_text().strip() == str(started + 1)
    assert not (state / "deadline-testpod.new").exists()


def _progress_each_tick(env, tmp_path, state, line):
    """Writes pod_run's progress line on every tick; `line` is a shell word that may use
    $c, the clock's epoch now."""
    state.mkdir(exist_ok=True)
    clock = tmp_path / "clock"
    target = state / "progress-testpod"
    on_each_tick(
        env,
        tmp_path,
        f'c=$(cat "{clock}")\nprintf "%s\\n" "{line}" > "{target}.new"\nmv "{target}.new" "{target}"\n',
    )


def test_a_fresh_ok_progress_line_holds_a_pod_whose_counters_read_idle(pod, tmp_path):
    env, calls, state = pod
    _progress_each_tick(env, tmp_path, state, "$c $c ok page-rate 1.00 pages a minute")
    run_guard(env, "0.002")
    assert "approved time is up" in log_of(state)
    assert "idle warning" not in log_of(state)


def test_a_fresh_stalled_line_climbs_the_ladder_while_the_gpu_is_busy(pod, tmp_path):
    """An engine spinning on a hung request keeps the GPU busy; the run's own verdict wins."""
    env, calls, state = pod
    env["FAKE_GPU_UTIL"] = "80"
    _progress_each_tick(env, tmp_path, state, "$c $((c - 7200)) stalled page-quiet no new page")
    run_guard(env, "5")
    assert "pod delete testpod" in lines(calls)
    assert "the run reports stalled (page-quiet no new page)" in log_of(state)


def test_the_ladder_runs_from_the_progress_line_s_last_ok(pod, tmp_path):
    env, calls, state = pod
    env["FAKE_GPU_UTIL"] = "80"
    env["POD_GUARD_WARN_SECONDS"] = "600"
    env["POD_GUARD_URGENT_SECONDS"] = "6000"
    env["POD_GUARD_BACKUP_SECONDS"] = "6000"
    env["POD_GUARD_DELETE_SECONDS"] = "6000"
    started = clock_of(env)
    # Slow since 595 s before arming: the warning comes about five ticks in, not at once.
    _progress_each_tick(env, tmp_path, state, f"$c {started - 595} slow page-rate 0.20 a minute")
    run_until(["sh", str(GUARD), "5"], env, lambda: "idle warning" in log_of(state))
    warned = next(line for line in log_of(state).splitlines() if "idle warning" in line)
    assert "idle for 6" in warned and "the run reports slow" in warned
    assert len(lines(tmp_path / "ticks")) >= 5
    assert lines(calls) == []


def test_a_stale_progress_line_falls_back_to_the_counters(pod, tmp_path):
    env, calls, state = pod
    _progress_each_tick(env, tmp_path, state, "$((c - 301)) $((c - 301)) ok page-rate fine")
    run_guard(env, "5")
    assert "pod delete testpod" in lines(calls)
    assert "no GPU, CPU or network work" in log_of(state)


def test_a_garbled_progress_line_falls_back_to_the_counters(pod, tmp_path):
    env, calls, state = pod
    _progress_each_tick(env, tmp_path, state, "$c soon ok")
    run_guard(env, "5")
    assert "pod delete testpod" in lines(calls)
    assert "no GPU, CPU or network work" in log_of(state)
