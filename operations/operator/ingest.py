"""The confined pre-Door ingest console flow.

The parent is deliberately a narrow presenter.  It never opens submitted images,
reads a provider credential, or writes a submission record.  A confined child first
builds the complete plan without writing it; only after that plan is printed does a
second confined child create the immutable files in the one output folder the person
selected.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .custody import python_module_command, run_confined
from .errors import ErrorCode, OperatorError, strip_control_bytes


def ingest_in_custody(
    *,
    source: Path,
    output_dir: Path,
    policy_path: Path | None,
    corpus_id: str,
    mode: str,
    confirmation_file: Path | None,
    workspace: Path,
    printer: Callable[[str], None],
) -> None:
    """Preview and then commit a pre-Door folder through the custody seam."""

    source_path = _absolute_path(source, workspace)
    output_path = _absolute_path(output_dir, workspace)
    request = {
        # Do not resolve operator-selected paths here. `resolve()` follows a
        # symlink before the data gate can reject that redirection, converting
        # the gate's deliberate no-symlink rule into an invisible bypass.
        "source": str(source_path),
        "output_dir": str(output_path),
        "policy": str(
            _absolute_path(
                policy_path or workspace / "config" / "data_handling_policy.json", workspace
            )
        ),
        "corpus_id": corpus_id,
        "mode": mode,
        "confirmation_file": None
        if confirmation_file is None
        else str(_absolute_path(confirmation_file, workspace)),
        "expected_submission_manifest_sha256": None,
        "expected_confirmation_sha256": None,
        "expected_instrument_config_sha256": None,
        "expected_data_handling_policy_sha256": None,
    }
    preview = _call_worker(request, "preview", writable=None, workspace=workspace)
    preview_summary = _summary(preview, ErrorCode.INGEST_PREVIEW_UNRESOLVED)
    _print_preview(preview_summary, printer)
    # Preview and commit are separate confined launches, so the commit worker
    # re-reads the source folder, confirmation file, triage instrument configuration,
    # and caller-selected data-handling policy rather than reusing the preview's
    # own reads. Corpus id, mode, and the policy path travel in this request and are
    # therefore identical by construction; the mutable bytes behind the paths are
    # not. Pin all four
    # to exactly what was just shown and validated: the commit worker refuses
    # rather than write different bytes than these if any of them moved.
    commit_request = {
        **request,
        "expected_submission_manifest_sha256": preview_summary["submission_manifest_sha256"],
        "expected_confirmation_sha256": preview_summary["confirmation_sha256"],
        "expected_instrument_config_sha256": preview_summary["instrument_config_sha256"],
        "expected_data_handling_policy_sha256": preview_summary["data_handling_policy_sha256"],
    }
    # The preview's gate check has already rejected a symlinked output path.
    # Resolve only for the kernel allowance, and make the worker recheck the
    # original spelling in case it was replaced between preview and commit.
    committed = _call_worker(
        commit_request, "commit", writable=output_path.resolve(), workspace=workspace
    )
    summary = _summary(committed, ErrorCode.INGEST_UNRESOLVED)
    printer(
        "Ready-to-submit folder: "
        f"{strip_control_bytes(str(output_path))}\n"
        "What the Door will see: "
        f"{summary['submission_files']} submitted file(s), ledger self-hash "
        f"{summary['submission_ledger_self_hash']}, "
        f"triage mode {summary['mode']}, {summary['candidate_count']} candidate evidence record(s), "
        f"{summary['confirmed_cluster_count']} confirmed cluster(s).\n"
        "No pod was started, confirmed, or billed. The observation-based full-run exit remains Tyrel's."
    )


def _absolute_path(path: Path, workspace: Path) -> Path:
    """Anchor a UI path without resolving an operator-controlled symlink."""

    return path if path.is_absolute() else workspace / path


def _call_worker(
    request: dict[str, Any], operation: str, *, writable: Path | None, workspace: Path
) -> dict[str, Any]:
    # Which stage failed decides what the operator is told about their output
    # folder, and the two facts are not the same. The commit child may have
    # created immutable records before an interruption, so its copy says to
    # preserve the folder and retry into a new one. The preview child is launched
    # with no write allowance at all, so the same copy applied to a preview
    # failure asserts records that provably cannot exist and costs the operator a
    # usable empty folder they were told not to reuse (GOVERNANCE 10).
    unresolved = (
        ErrorCode.INGEST_PREVIEW_UNRESOLVED
        if operation == "preview"
        else ErrorCode.INGEST_UNRESOLVED
    )
    command = python_module_command("operations.operator.ingest_worker", workspace)
    payload = json.dumps({"operation": operation, **request}, sort_keys=True)
    backend, completed = run_confined(
        command,
        writable=writable,
        cwd=workspace,
        input_text=payload,
    )
    if completed.returncode != 0:
        launcher = backend.launcher_failure(completed)
        if launcher is not None:
            raise OperatorError(ErrorCode.CONSOLE_CUSTODY_REFUSED, detail=launcher)
        response = _decode_response(completed.stdout)
        if response is not None and response.get("status") == "refusal":
            raise OperatorError(ErrorCode.INGEST_REFUSED, detail=str(response.get("reason", "")))
        if response is not None and response.get("status") == "uncertain":
            # The commit child failed partway through its writes: the worker already
            # extracted its own reason for `main()`'s `{"status": "uncertain", ...}`
            # reply, and showing the raw JSON dict here instead would bury that reason
            # in braces and quoting the operator does not need to parse.
            raise OperatorError(unresolved, detail=str(response.get("reason", "")))
        detail = (
            completed.stderr or completed.stdout or "the confined ingest worker returned no result"
        )
        raise OperatorError(unresolved, detail=detail)
    response = _decode_response(completed.stdout)
    if response is None or response.get("status") not in {"preview", "committed"}:
        raise OperatorError(
            unresolved,
            detail="the confined ingest worker returned an invalid result",
        )
    return response


def _decode_response(value: str) -> dict[str, Any] | None:
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _summary(response: dict[str, Any], unresolved: ErrorCode) -> dict[str, Any]:
    summary = response.get("summary")
    required = {
        "submission_files",
        "submission_manifest_sha256",
        "submission_ledger_self_hash",
        "confirmation_sha256",
        "instrument_config_sha256",
        "data_handling_policy_sha256",
        "mode",
        "candidate_count",
        "confirmed_cluster_count",
        "confirmed_clusters",
        "planned_files",
        "candidates",
    }
    if not isinstance(summary, dict) or set(summary) != required:
        raise OperatorError(unresolved, detail="the ingest summary has an invalid shape")
    return summary


def _print_preview(summary: dict[str, Any], printer: Callable[[str], None]) -> None:
    candidate_lines = [
        "  " + strip_control_bytes(str(candidate)) for candidate in summary["candidates"]
    ] or ["  none"]
    planned_lines = ["  " + strip_control_bytes(str(path)) for path in summary["planned_files"]]
    printer(
        "Ingest preview — no files have been written.\n"
        f"Submission ledger: {summary['submission_files']} file(s), self-hash "
        f"{summary['submission_ledger_self_hash']}.\n"
        f"Data gate: approved policy {summary['data_handling_policy_sha256']} checked. "
        f"Triage mode: {summary['mode']}.\n"
        "Instrument candidates (digest prefixes, verdict, reason):\n"
        + "\n".join(candidate_lines)
        + "\nConfirmation: "
        + (
            "the supplied confirmation will be validated and retained. "
            "Confirmed clusters (page designations, member digest prefixes):\n"
            + "\n".join(
                "  " + strip_control_bytes(str(line)) for line in summary["confirmed_clusters"]
            )
            if summary["confirmed_cluster_count"]
            else "no cluster confirmation file was supplied; every cluster field stays null."
        )
        + "\nThe following immutable files will be written only now:\n"
        + "\n".join(planned_lines)
        + "\nThis exact submission ledger, the data-handling policy"
        + (
            ", the triage instrument settings, and this confirmation are"
            if summary["confirmation_sha256"] is not None
            else " and the triage instrument settings are"
        )
        + " sealed by digest: if any of them changes before commit runs, commit refuses "
        "rather than write something other than what is shown above."
    )
