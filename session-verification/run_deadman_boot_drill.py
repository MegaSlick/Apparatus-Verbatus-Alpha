#!/usr/bin/env python3
"""Run the embedded deadman boot path in its exact pinned image, without network.

This is a GitHub-runner diagnostic.  It changes only the terminal DELETE loop: a boot
refusal is written to the bind-mounted evidence directory and exits instead of making a
provider call.  The production helper is neither changed nor invoked as a controller.
"""

from __future__ import annotations

import argparse
import datetime as dt
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
        and isinstance(probe, dict)
        and probe.get("deadman_pid") == pid
        and all(probe.get(key) is True for key in required_probe)
        and summary.get("provider_key_removed_before_start") is True
        and summary.get("runtime_verified") is True
    )


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
    deadline = now + dt.timedelta(seconds=55)
    cleanup = now + dt.timedelta(seconds=45)
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

    evidence_root = mode_workspace / "session-evidence" / session
    try:
        run(command)
        end = time.monotonic() + 60
        while time.monotonic() < end:
            if (evidence_root / "runtime-receipt.json").is_file() or (
                evidence_root / "boot-drill-result.json"
            ).is_file():
                break
            state = run(
                ["docker", "inspect", "--format", "{{.State.Running}}", container_name],
                check=False,
                capture=True,
            )
            if state.stdout.strip() == "false":
                break
            time.sleep(1)
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
    controller = load_controller()
    if controller.PINNED_IMAGE != EXPECTED_IMAGE:
        raise RuntimeError("controller image is not the reviewed immutable image")

    workspace = arguments.workspace.resolve()
    if workspace.exists():
        shutil.rmtree(workspace)
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
        "schema": "verbatus-deadman-boot-drill-summary.v2",
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
