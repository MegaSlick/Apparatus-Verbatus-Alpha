"""Produce the human-readable Spec 12 rehearsal transcript from the real fake flow.

This is deliberately not a second implementation of the six words.  It prepares
small local fixture files, invokes the operator surface, and captures the same
lines a person sees.  The resulting file is the acceptance artifact Tyrel can
read without needing a configured provider account.
"""

from __future__ import annotations

import argparse
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from common.contracts.canonical import canonical_bytes
from operations.pod.fake_provider import FakeProvider
from operations.pod.models import PodCreateRequest
from operations.submit.submit import build_manifest, walk_folder

from .errors import OperatorError
from .surface import OPERATOR_CLOSE_PREFIX, OperatorSurface

UTC = timezone.utc
ROOT = Path(__file__).resolve().parents[2]
START = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


def make_transcript(output: str | Path) -> Path:
    """Run all six words, one intentional refusal, and read-only status."""

    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = [
        "Verbatus dry-run transcript",
        "This is an offline rehearsal. It contacts no cloud provider and creates no bill.",
        "",
    ]
    with tempfile.TemporaryDirectory(prefix="verbatus-dry-run-") as temporary_name:
        temporary = Path(temporary_name)
        state = temporary / "operator-records"
        source = temporary / "submitted-pages"
        source.mkdir()
        (source / "page-one.bin").write_bytes(b"synthetic submission page one\n")
        manifest = temporary / "sealed-submission.json"
        submission = build_manifest(
            walk_folder(source),
            authorized_by={
                "relative_path": "receipts/sha256/" + "a" * 64 + ".json",
                "sha256": "a" * 64,
            },
        )
        manifest.write_bytes(canonical_bytes(submission))
        spend = temporary / "reviewed-spend.toml"
        spend.write_text(
            "\n".join(
                (
                    'schema = "pod-spend.v1"',
                    'state = "configured"',
                    'currency = "USD"',
                    'max_hourly_usd = "1.00"',
                    'max_estimated_metered_cost_usd = "2.00"',
                    "hard_lifetime_seconds = 900",
                    "laptop_heartbeat_timeout_seconds = 60",
                    "shutdown_poll_interval_seconds = 1",
                    "shutdown_deadline_seconds = 5",
                    "",
                )
            ),
            encoding="utf-8",
        )
        request = _request()
        provider = FakeProvider(now=lambda: START)

        def present(value: str = "") -> None:
            lines.append(_friendly_path(value, temporary))

        surface = OperatorSurface(
            ROOT,
            state,
            provider=provider,
            now=lambda: START,
            present=present,
        )

        _heading(lines, "1. launch — price first, then confirmation")
        prepared = surface.prepare_launch(request, policy_path=spend)
        try:
            surface.launch(prepared, "not the confirmation")
        except OperatorError as error:
            lines.extend(error.render().splitlines())
        lines.append("The refusal above made no paid provider call.")
        lines.append("")
        prepared = surface.prepare_launch(request, policy_path=spend)
        launched = surface.launch(prepared, prepared.confirmation_phrase)

        _heading(lines, "2. boot — clear green or red report")
        surface.boot()

        _heading(lines, "3. upload — no pod is needed")
        lines.append(
            "This transcript follows the six-word list. In a normal run, upload can happen before launch."
        )
        surface.upload(source, sealed_manifest=manifest)

        _heading(lines, "4. run — named pages and acts, resumable evidence")
        surface.run(run_id="tyrel-dry-run")

        _heading(lines, "5. export — local evidence bundle and reconciliation")
        surface.export(run_id="tyrel-dry-run")

        _heading(lines, "6. close — separate confirmation and captured cost")
        if launched.record is None:  # defensive: the surface would already have refused.
            raise RuntimeError("the dry-run launch did not provide a fixture pod")
        surface.close(f"{OPERATOR_CLOSE_PREFIX} {launched.record.pod_id}")

        _heading(lines, "status — read saved records only")
        surface.status()

    target.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return target


def _request() -> PodCreateRequest:
    return PodCreateRequest(
        name="tyrel-dry-run",
        gpu_type="fake-48gb",
        image="registry.example/verbatus@sha256:" + "a" * 64,
        volume_id="fixture-volume",
        volume_mount_path="/workspace/private",
        docker_start_cmd=(
            "python",
            "-m",
            "operations.pod.pod_timer",
            "--timer-factory",
            "operations.pod.provider_runpod:timer_context_from_environment",
            "--bootstrap-command-json",
            '["python","-m","operations.pod.bootstrap"]',
            "--report-path",
            "/workspace/private/pod-runtime-report.json",
        ),
        hard_deadline=START + timedelta(seconds=900),
        repository_commit="b" * 40,
        template="fixture-template",
    )


def _heading(lines: list[str], title: str) -> None:
    if lines[-1]:
        lines.append("")
    lines.append(title)


def _friendly_path(value: str, temporary: Path) -> str:
    """Keep temporary test paths out of the operator-facing acceptance reading."""

    friendly = re.sub(re.escape(str(temporary)), "[rehearsal folder]", value)
    return re.sub(
        r"/receipts/([a-z-]+)-[0-9a-f]{64}\.json",
        r"/receipts/\1 receipt",
        friendly,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, required=True, help="where to save the transcript")
    args = parser.parse_args(argv)
    make_transcript(args.output)
    return 0


if __name__ == "__main__":  # pragma: no cover - command-line helper
    raise SystemExit(main())
