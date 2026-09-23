#!/usr/bin/env python3
"""Run the embedded deadman boot path in its exact pinned image, without network.

This is a GitHub-runner diagnostic.  It changes only the terminal DELETE loop: a boot
refusal is written to the container-private evidence directory and exits instead of making
a provider call.  The production helper is neither changed nor invoked as a controller.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid


HERE = Path(__file__).resolve().parent
CONTROLLER_PATH = HERE / "live_trial_controller_20260922.py"
LIVE_PAYLOAD_LIB_PATH = HERE / "live_payload_lib.sh"
EXPECTED_IMAGE = (
    "runpod/pytorch@sha256:"
    "0a360022e8de4375af99430f84e8b38951acc397252163a37ceac7204d01be35"
)
SESSION = "offline-deadman-boot-drill"


def load_controller():
    spec = importlib.util.spec_from_file_location("deadman_drill_controller", CONTROLLER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("controller module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def drill_source(source: str) -> str:
    marker = "try: boot()"
    if source.count(marker) != 1:
        raise RuntimeError("embedded deadman did not have the expected single boot tail")
    definitions, production_tail = source.rsplit(marker, 1)
    harness = r'''
SAFE_REASONS={
 'deadman-not-root','deadman-proc-identity-unverified','invalid-deadline',
 'worker-user-creation-failed','worker-probe-unreadable',
 'worker-separation-unverified','default-start-service-exited',
 'controller-ack-unreadable','controller-ack-mismatch',
}
def safe_drill_reason(reason):
 if reason in SAFE_REASONS: return reason
 if reason.startswith('unhandled-boot-or-timer-'): return reason
 return 'unrecognized-termination-reason'
def drill_result(outcome,reason):
 best_effort_write(root/'boot-drill-result.json',{
  'schema':'verbatus-deadman-boot-drill.v1',
  'outcome':outcome,
  'reason':reason,
  'pid':os.getpid(),
  'ppid':os.getppid(),
  'uid':os.geteuid(),
 })
def terminate_for_drill(reason):
 drill_result('terminate-requested',safe_drill_reason(reason))
 os._exit(97)
terminate_forever=terminate_for_drill
'''
    # Keep the production `try: boot()` / `except BaseException` tail byte-for-byte.
    # Only its terminal provider DELETE function is replaced, immediately before use.
    return definitions + harness + marker + production_tail


def run(command: list[str], *, check: bool = True, capture: bool = False):
    return subprocess.run(
        command,
        check=check,
        text=True,
        capture_output=capture,
    )


def safe_summary(evidence_root: Path, container_name: str) -> dict[str, object]:
    result_path = evidence_root / "boot-drill-result.json"
    receipt_path = evidence_root / "runtime-receipt.json"
    if receipt_path.is_file():
        receipt = json.loads(receipt_path.read_text())
        probe = receipt.get("worker_probe")
        allowed_probe = (
            "deadman_pid",
            "provider_env_absent",
            "proc1_environ_denied",
            "deadman_environ_denied",
            "root_receipt_denied",
            "sudo_unavailable",
            "cap_eff_zero",
            "no_new_privs",
        )
        return {
            "schema": "verbatus-deadman-boot-drill-summary.v1",
            "outcome": "runtime-receipt-created",
            "pid": receipt.get("pid"),
            "uid": receipt.get("uid"),
            "default_start_pid": receipt.get("default_start_pid"),
            "worker_uid": receipt.get("worker_uid"),
            "worker_gid": receipt.get("worker_gid"),
            "deadman_proc_identity_verified": receipt.get(
                "deadman_proc_identity_verified"
            ),
            "provider_key_removed_before_start": receipt.get(
                "provider_key_removed_before_start"
            ),
            "runtime_verified": receipt.get("runtime_verified"),
            "worker_probe": {
                key: probe.get(key) if isinstance(probe, dict) else None
                for key in allowed_probe
            },
        }
    if result_path.is_file():
        result = json.loads(result_path.read_text())
        return {
            "schema": "verbatus-deadman-boot-drill-summary.v1",
            "outcome": result.get("outcome"),
            "reason": result.get("reason"),
            "pid": result.get("pid"),
            "ppid": result.get("ppid"),
            "uid": result.get("uid"),
        }
    inspected = run(
        ["docker", "inspect", "--format", "{{.State.Status}} {{.State.ExitCode}}", container_name],
        check=False,
        capture=True,
    )
    return {
        "schema": "verbatus-deadman-boot-drill-summary.v1",
        "outcome": "no-safe-result",
        "container_state": inspected.stdout.strip() or "unavailable",
    }


def receipt_matches_mode(summary: dict[str, object], *, expect_pid1: bool) -> bool:
    pid = summary.get("pid")
    probe = summary.get("worker_probe")
    required_probe = (
        "provider_env_absent",
        "proc1_environ_denied",
        "deadman_environ_denied",
        "root_receipt_denied",
        "sudo_unavailable",
        "cap_eff_zero",
        "no_new_privs",
    )
    return bool(
        summary.get("outcome") == "runtime-receipt-created"
        and isinstance(pid, int)
        and not isinstance(pid, bool)
        and pid > 0
        and ((pid == 1) if expect_pid1 else (pid != 1))
        and summary.get("uid") == 0
        and summary.get("deadman_proc_identity_verified") is True
        and summary.get("receipt_copied_while_running") is True
        and summary.get("worker_gate_refused_before_ack") is True
        and summary.get("acknowledgement_observed_while_running") is True
        and summary.get("worker_launcher_smoke_verified") is True
        and summary.get("worker_timeout_cleanup_verified") is True
        and summary.get("root_supervisor_survived_worker_timeout_cleanup") is True
        and summary.get("final_evidence_copied_while_running") is True
        and isinstance(probe, dict)
        and probe.get("deadman_pid") == pid
        and all(probe.get(key) is True for key in required_probe)
        and summary.get("provider_key_removed_before_start") is True
        and summary.get("runtime_verified") is True
    )


def exercise_worker_handoff(
    *,
    container_name: str,
    container_evidence_root: str,
    evidence_root: Path,
    session: str,
) -> tuple[bool, bool, bool]:
    worker_probe_source = r'''import json,os
cap_eff_zero=False; no_new_privs=False
for line in open('/proc/self/status'):
 if line.startswith('CapEff:'): cap_eff_zero=int(line.split()[1],16)==0
 if line.startswith('NoNewPrivs:'): no_new_privs=line.split()[1]=='1'
print(json.dumps({'uid':os.geteuid(),'cap_eff_zero':cap_eff_zero,'no_new_privs':no_new_privs},sort_keys=True))'''
    worker_command = [
        "docker",
        "exec",
        container_name,
        "/usr/bin/env",
        "-i",
        f"VERBATUS_SESSION_ID={session}",
        "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "/usr/sbin/runuser",
        "-u",
        "verbatus-worker",
        "--",
        "/usr/local/bin/verbatus-worker-exec",
        "/usr/bin/python3",
        "-c",
        worker_probe_source,
    ]
    refused = run(worker_command, check=False, capture=True)
    gate_refused = bool(
        refused.returncode != 0 and "worker gate refused" in refused.stderr
    )

    receipt_path = evidence_root / "runtime-receipt.json"
    receipt_bytes = receipt_path.read_bytes()
    receipt = json.loads(receipt_bytes)
    ack = {
        "schema": "verbatus-controller-ack.v1",
        "session_id": receipt["session_id"],
        "pod_id": receipt["pod_id"],
        "controller_challenge": receipt["controller_challenge"],
        "hard_deadline_epoch": receipt["hard_deadline_epoch"],
        "runtime_receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
    }
    ack_bytes = (json.dumps(ack, sort_keys=True, separators=(",", ":")) + "\n").encode()
    ack_upload_path = evidence_root.parent / ".controller-ack.json"
    descriptor = os.open(
        ack_upload_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
    )
    try:
        os.write(descriptor, ack_bytes)
    finally:
        os.close(descriptor)
    try:
        run(
            [
                "docker",
                "cp",
                str(ack_upload_path),
                f"{container_name}:{container_evidence_root}/controller-ack.json",
            ]
        )
    finally:
        ack_upload_path.unlink()

    acknowledgement_observed = False
    ack_end = time.monotonic() + 20
    while time.monotonic() < ack_end:
        acknowledged = run(
            [
                "docker",
                "exec",
                container_name,
                "test",
                "-f",
                f"{container_evidence_root}/controller-acknowledged.json",
            ],
            check=False,
            capture=True,
        )
        if acknowledged.returncode == 0:
            acknowledgement_observed = True
            break
        state = run(
            ["docker", "inspect", "--format", "{{.State.Running}}", container_name],
            check=False,
            capture=True,
        )
        if state.stdout.strip() != "true":
            break
        time.sleep(1)

    launcher_verified = False
    if acknowledgement_observed:
        worker = run(worker_command, check=False, capture=True)
        try:
            worker_result = json.loads(worker.stdout)
        except json.JSONDecodeError:
            worker_result = None
        launcher_verified = bool(
            worker.returncode == 0
            and isinstance(worker_result, dict)
            and worker_result.get("uid") == receipt.get("worker_uid")
            and worker_result.get("uid") != 0
            and worker_result.get("cap_eff_zero") is True
            and worker_result.get("no_new_privs") is True
        )
    return gate_refused, acknowledgement_observed, launcher_verified


def exercise_worker_timeout_cleanup(
    *, container_name: str, session: str, deadman_pid: int
) -> tuple[bool, bool]:
    """Exercise failure-only draining through the installed worker gate."""
    container_lib = "/tmp/verbatus-live-payload-lib.sh"
    run(["docker", "cp", str(LIVE_PAYLOAD_LIB_PATH), f"{container_name}:{container_lib}"])
    nonroot = run(
        [
            "docker", "exec", "--user", "verbatus-worker", container_name,
            "/bin/bash", "-ceu",
            (
                f"source {container_lib}; "
                "if drain_worker_uid verbatus-worker; then "
                "echo 'non-root cleanup unexpectedly succeeded' >&2; exit 1; fi"
            ),
        ],
        check=False,
        capture=True,
    )
    nonroot_refused = bool(
        nonroot.returncode == 0
        and "effective uid is not root" in nonroot.stderr
    )
    cleanup_script = r'''
source /tmp/verbatus-live-payload-lib.sh
if drain_worker_uid does-not-exist; then
  printf '%s\n' 'unresolved worker user was accepted' >&2
  exit 1
fi
if drain_worker_uid root; then
  printf '%s\n' 'uid zero worker cleanup was accepted' >&2
  exit 1
fi
escaped_pid_file=/tmp/escaped-worker.pid
stdout_link_file=/tmp/escaped-worker-stdout
rm -f "$escaped_pid_file" "$stdout_link_file"
run_worker "$TIMEOUT_SECONDS" /bin/sh -ceu '
  setsid /bin/sh -ceu '\''trap "" TERM; readlink /proc/self/fd/1 >"$1"; echo $$ >"$2"; while :; do :; done'\'' ignored '"$stdout_link_file"' '"$escaped_pid_file"' &
  child=$!
  wait "$child"
' &
wrapper_pid=$!
deadline=$((SECONDS + 10))
while [[ ! -s "$escaped_pid_file" ]]; do
  [[ "$SECONDS" -lt "$deadline" ]] || {
    printf '%s\n' 'detached worker did not publish its pid' >&2
    exit 1
  }
  /bin/sleep 1
done
escaped_pid="$(cat "$escaped_pid_file")"
[[ "$escaped_pid" =~ ^[1-9][0-9]*$ ]] || {
  printf '%s\n' 'detached worker published an invalid pid' >&2
  exit 1
}
kill -0 "$escaped_pid"
[[ "$(cat "$stdout_link_file")" == "$(readlink /proc/$$/fd/1)" ]] || {
  printf '%s\n' 'detached worker did not retain the caller stdout descriptor' >&2
  exit 1
}
if wait "$wrapper_pid"; then
  printf '%s\n' 'timed worker unexpectedly succeeded' >&2
  exit 1
else
  status=$?
fi
[[ "$status" -eq 124 ]] || {
  printf 'expected timeout status 124, got %s\n' "$status" >&2
  exit 1
}
if kill -0 "$escaped_pid" 2>/dev/null; then
  printf '%s\n' 'detached worker survived UID cleanup' >&2
  exit 1
fi
'''
    cleanup = run(
        [
            "docker", "exec", "--env", f"SESSION_ID={session}", "--env",
            "TIMEOUT_SECONDS=1", container_name, "/bin/bash", "-ceu", cleanup_script,
        ],
        check=False,
        capture=True,
    )
    supervisor = run(
        ["docker", "inspect", "--format", "{{.State.Running}}", container_name],
        check=False,
        capture=True,
    )
    supervisor_pid = run(
        ["docker", "exec", container_name, "/bin/kill", "-0", str(deadman_pid)],
        check=False,
        capture=True,
    )
    supervisor_survived = bool(
        supervisor.stdout.strip() == "true" and supervisor_pid.returncode == 0
    )
    return bool(
        nonroot_refused
        and cleanup.returncode == 0
        and supervisor_survived
    ), supervisor_survived


def run_mode(
    *,
    mode: str,
    workspace: Path,
    source: str,
    public_key: str,
    use_init: bool,
) -> tuple[dict[str, object], str]:
    mode_workspace = workspace / mode
    mode_workspace.mkdir(mode=0o700)
    session = f"{SESSION}-{mode}"
    now = dt.datetime.now(dt.timezone.utc)
    deadline = now + dt.timedelta(seconds=90)
    cleanup = now + dt.timedelta(seconds=75)
    container_name = "verbatus-deadman-drill-" + uuid.uuid4().hex[:12]
    command = ["docker", "run", "--detach", "--name", container_name]
    if use_init:
        command.append("--init")
    command.extend(
        [
            "--network",
            "none",
            "--mount",
            f"type=bind,src={mode_workspace},dst=/workspace/private",
            "--env",
            "VERBATUS_RUNPOD_API_KEY=offline-fake-key",
            "--env",
            "RUNPOD_POD_ID=offline-drill-pod",
            "--env",
            f"VERBATUS_SESSION_ID={session}",
            "--env",
            "VERBATUS_CONTROLLER_CHALLENGE=offline-drill-challenge",
            "--env",
            f"VERBATUS_HARD_DEADLINE_EPOCH={deadline.timestamp()}",
            "--env",
            f"VERBATUS_CLEANUP_EPOCH={cleanup.timestamp()}",
            "--env",
            f"PUBLIC_KEY={public_key}",
            EXPECTED_IMAGE,
            "python3",
            "-u",
            "-c",
            source,
        ]
    )

    container_evidence_root = f"/run/verbatus-live-private/{session}"
    evidence_root = (
        workspace.with_name(workspace.name + "-private-evidence") / mode / session
    )
    evidence_root.mkdir(parents=True, mode=0o700)
    receipt_observed_while_running = False
    running_before_copy = False
    running_after_copy = False
    worker_gate_refused_before_ack = False
    acknowledgement_observed_while_running = False
    worker_launcher_smoke_verified = False
    worker_timeout_cleanup_verified = False
    root_supervisor_survived_worker_timeout_cleanup = False
    final_evidence_copied_while_running = False
    try:
        run(command)
        end = time.monotonic() + 60
        while time.monotonic() < end:
            receipt_ready = run(
                [
                    "docker",
                    "exec",
                    container_name,
                    "test",
                    "-f",
                    f"{container_evidence_root}/runtime-receipt.json",
                ],
                check=False,
                capture=True,
            )
            result_ready = run(
                [
                    "docker",
                    "exec",
                    container_name,
                    "test",
                    "-f",
                    f"{container_evidence_root}/boot-drill-result.json",
                ],
                check=False,
                capture=True,
            )
            if receipt_ready.returncode == 0:
                receipt_observed_while_running = True
                break
            if result_ready.returncode == 0:
                break
            state = run(
                ["docker", "inspect", "--format", "{{.State.Running}}", container_name],
                check=False,
                capture=True,
            )
            if state.stdout.strip() == "false":
                break
            time.sleep(1)
        if receipt_observed_while_running:
            state = run(
                ["docker", "inspect", "--format", "{{.State.Running}}", container_name],
                check=False,
                capture=True,
            )
            running_before_copy = state.stdout.strip() == "true"
        run(
            [
                "docker",
                "cp",
                f"{container_name}:{container_evidence_root}/.",
                str(evidence_root),
            ]
        )
        if receipt_observed_while_running:
            state = run(
                ["docker", "inspect", "--format", "{{.State.Running}}", container_name],
                check=False,
                capture=True,
            )
            running_after_copy = state.stdout.strip() == "true"
        summary = safe_summary(evidence_root, container_name)
        receipt_ready_for_handoff = bool(
            summary.get("outcome") == "runtime-receipt-created"
            and receipt_observed_while_running
            and running_before_copy
            and running_after_copy
        )
        if receipt_ready_for_handoff:
            (
                worker_gate_refused_before_ack,
                acknowledgement_observed_while_running,
                worker_launcher_smoke_verified,
            ) = exercise_worker_handoff(
                container_name=container_name,
                container_evidence_root=container_evidence_root,
                evidence_root=evidence_root,
                session=session,
            )
            deadman_pid = summary.get("pid")
            if (
                acknowledgement_observed_while_running
                and worker_launcher_smoke_verified
                and isinstance(deadman_pid, int)
                and not isinstance(deadman_pid, bool)
            ):
                (
                    worker_timeout_cleanup_verified,
                    root_supervisor_survived_worker_timeout_cleanup,
                ) = exercise_worker_timeout_cleanup(
                    container_name=container_name,
                    session=session,
                    deadman_pid=deadman_pid,
                )
            state = run(
                ["docker", "inspect", "--format", "{{.State.Running}}", container_name],
                check=False,
                capture=True,
            )
            final_running_before_copy = state.stdout.strip() == "true"
            run(
                [
                    "docker",
                    "cp",
                    f"{container_name}:{container_evidence_root}/.",
                    str(evidence_root),
                ]
            )
            state = run(
                ["docker", "inspect", "--format", "{{.State.Running}}", container_name],
                check=False,
                capture=True,
            )
            final_evidence_copied_while_running = bool(
                final_running_before_copy and state.stdout.strip() == "true"
            )
            summary = safe_summary(evidence_root, container_name)
    except Exception as error:
        summary = {
            "schema": "verbatus-deadman-boot-drill-summary.v1",
            "outcome": "drill-infrastructure-error",
            "error_type": type(error).__name__,
        }
    finally:
        logs = run(["docker", "logs", container_name], check=False, capture=True)
        safe_logs = logs.stdout + logs.stderr
        for private_value in (
            "offline-fake-key",
            "offline-drill-challenge",
            public_key,
        ):
            safe_logs = safe_logs.replace(private_value, "[redacted]")
        run(["docker", "rm", "--force", container_name], check=False)
    summary["mode"] = mode
    summary["receipt_copied_while_running"] = bool(
        receipt_observed_while_running and running_before_copy and running_after_copy
    )
    summary["worker_gate_refused_before_ack"] = worker_gate_refused_before_ack
    summary["acknowledgement_observed_while_running"] = (
        acknowledgement_observed_while_running
    )
    summary["worker_launcher_smoke_verified"] = worker_launcher_smoke_verified
    summary["worker_timeout_cleanup_verified"] = worker_timeout_cleanup_verified
    summary["root_supervisor_survived_worker_timeout_cleanup"] = (
        root_supervisor_survived_worker_timeout_cleanup
    )
    summary["final_evidence_copied_while_running"] = (
        final_evidence_copied_while_running
    )
    summary["supervisor_layout_verified"] = receipt_matches_mode(
        summary, expect_pid1=not use_init
    )
    return summary, safe_logs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    arguments = parser.parse_args()

    if shutil.which("docker") is None or shutil.which("ssh-keygen") is None:
        raise RuntimeError("docker and ssh-keygen are required")
    if not LIVE_PAYLOAD_LIB_PATH.is_file():
        raise RuntimeError("reviewed live payload library is missing")
    controller = load_controller()
    if controller.PINNED_IMAGE != EXPECTED_IMAGE:
        raise RuntimeError("controller image is not the reviewed immutable image")

    workspace = arguments.workspace.resolve()
    if workspace.exists():
        shutil.rmtree(workspace)
    private_evidence_workspace = workspace.with_name(
        workspace.name + "-private-evidence"
    )
    if private_evidence_workspace.exists() or private_evidence_workspace.is_symlink():
        raise RuntimeError("private evidence workspace already exists")
    private_evidence_workspace.mkdir(parents=True, mode=0o700)
    workspace.mkdir(parents=True, mode=0o700)
    arguments.summary.parent.mkdir(parents=True, exist_ok=True)
    source_path = workspace.parent / "deadman-drill-source.py"
    source_path.write_text(drill_source(controller.POD_DEADMAN_SOURCE))

    key_path = workspace.parent / "drill_ssh_key"
    run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key_path)])
    public_key = key_path.with_suffix(".pub").read_text().strip()
    key_path.unlink()

    # Pulling this image can take minutes. Bind the live deadlines only after the image is
    # local so download time cannot manufacture an invalid-deadline boot refusal.
    run(["docker", "pull", EXPECTED_IMAGE])
    source = source_path.read_text()
    mode_summaries: dict[str, dict[str, object]] = {}
    mode_logs: list[str] = []
    for mode, use_init in (("pid1", False), ("init-child", True)):
        summary, safe_logs = run_mode(
            mode=mode,
            workspace=workspace,
            source=source,
            public_key=public_key,
            use_init=use_init,
        )
        mode_summaries[mode] = summary
        mode_logs.append(f"=== {mode} ===\n{safe_logs}")
    green = all(
        summary.get("supervisor_layout_verified") is True
        for summary in mode_summaries.values()
    )
    combined = {
        "schema": "verbatus-deadman-boot-drill-summary.v3",
        "outcome": "both-supervisor-layouts-verified" if green else "boot-drill-failed",
        "modes": mode_summaries,
    }
    arguments.summary.write_text(json.dumps(combined, sort_keys=True, indent=2) + "\n")
    (arguments.summary.parent / "deadman-boot-drill.log").write_text(
        "\n".join(mode_logs)
    )
    print(json.dumps(combined, sort_keys=True))
    return 0 if green else 1


if __name__ == "__main__":
    raise SystemExit(main())
