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

from common.contracts.canonical import is_sha256

from .custody import python_module_command, run_confined
from .errors import ErrorCode, OperatorError, strip_control_bytes
from .ingest_protocol import (
    MAX_INGEST_CANDIDATE_PAIRS,
    MAX_INGEST_FRAMES,
    MAX_PREVIEW_LINE_CHARACTERS,
)


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
        "expected_output_device": None,
        "expected_output_inode": None,
    }
    preview = _call_worker(request, "preview", writable=None)
    preview_summary = _summary(preview, ErrorCode.INGEST_PREVIEW_UNRESOLVED)
    preview_output_identity = _output_identity(preview, ErrorCode.INGEST_PREVIEW_UNRESOLVED)
    _print_preview(preview_summary, printer)
    # The second confined launch re-reads four mutable inputs. Pin their bytes and
    # the selected output directory's identity to the preview; corpus id, mode,
    # and path spellings already stay identical in this request.
    commit_request = {
        **request,
        "expected_submission_manifest_sha256": preview_summary["submission_manifest_sha256"],
        "expected_confirmation_sha256": preview_summary["confirmation_sha256"],
        "expected_instrument_config_sha256": preview_summary["instrument_config_sha256"],
        "expected_data_handling_policy_sha256": preview_summary["data_handling_policy_sha256"],
        "expected_output_device": preview_output_identity[0],
        "expected_output_inode": preview_output_identity[1],
    }
    # The preview's gate check has already rejected a symlinked output path.
    # Resolve only for the kernel allowance, and make the worker recheck the
    # original spelling in case it was replaced between preview and commit.
    committed = _call_worker(commit_request, "commit", writable=output_path.resolve())
    summary = _summary(committed, ErrorCode.INGEST_UNRESOLVED)
    if _output_identity(committed, ErrorCode.INGEST_UNRESOLVED) != preview_output_identity:
        raise OperatorError(
            ErrorCode.INGEST_UNRESOLVED,
            detail="the confined worker reported a different output directory identity",
        )
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
    request: dict[str, Any], operation: str, *, writable: Path | None
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
    # `--workspace` selects project data; it is not authority to replace this
    # custody worker's code or its working directory. Every path the worker
    # acts on arrives absolute in the JSON request, the import root is pinned
    # by `python_module_command` to the checkout that loaded this module, and
    # the cwd stays that same pinned root (the backup worker's precedent) so
    # no caller-nominated tree is in play at all. This function therefore takes
    # no workspace at all: it used to accept one and never read it, which left
    # the next reader a parameter that looks like the authority this comment
    # spends six lines denying.
    worker_root = Path(__file__).resolve().parents[2]
    command = python_module_command("operations.operator.ingest_worker")
    payload = json.dumps({"operation": operation, **request}, sort_keys=True)
    backend, completed = run_confined(
        command,
        writable=writable,
        cwd=worker_root,
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
            # Preserve the worker's actionable reason; raw protocol JSON would
            # obscure a failure that may have happened after immutable writes began.
            raise OperatorError(unresolved, detail=str(response.get("reason", "")))
        detail = (
            completed.stderr or completed.stdout or "the confined ingest worker returned no result"
        )
        raise OperatorError(unresolved, detail=detail)
    response = _decode_response(completed.stdout)
    expected_status = "preview" if operation == "preview" else "committed"
    if (
        response is None
        or set(response) != {"status", "summary", "output_identity"}
        or response.get("status") != expected_status
    ):
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
    """Validate every child-controlled value before display or commit reuse."""

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
    counts = (
        summary["submission_files"],
        summary["candidate_count"],
        summary["confirmed_cluster_count"],
    )
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in counts):
        raise OperatorError(unresolved, detail="the ingest summary has an invalid count")
    submission_files, candidate_count, confirmed_cluster_count = counts
    if (
        not 1 <= submission_files <= MAX_INGEST_FRAMES
        or candidate_count > MAX_INGEST_CANDIDATE_PAIRS
        or confirmed_cluster_count > submission_files // 2
    ):
        raise OperatorError(unresolved, detail="the ingest summary exceeds its bounded counts")
    for name in (
        "submission_manifest_sha256",
        "submission_ledger_self_hash",
        "instrument_config_sha256",
        "data_handling_policy_sha256",
    ):
        if not is_sha256(summary[name]):
            raise OperatorError(unresolved, detail=f"the ingest summary has an invalid {name}")
    confirmation_sha256 = summary["confirmation_sha256"]
    if confirmation_sha256 is not None and not is_sha256(confirmation_sha256):
        raise OperatorError(
            unresolved, detail="the ingest summary has an invalid confirmation digest"
        )
    if (confirmation_sha256 is None) != (confirmed_cluster_count == 0):
        raise OperatorError(
            unresolved, detail="the ingest summary confirmation count and digest disagree"
        )
    if summary["mode"] not in {"manual", "semi", "auto"}:
        raise OperatorError(unresolved, detail="the ingest summary has an invalid triage mode")
    candidates = summary["candidates"]
    clusters = summary["confirmed_clusters"]
    planned = summary["planned_files"]
    if (
        not isinstance(candidates, list)
        or len(candidates) != candidate_count
        or not isinstance(clusters, list)
        or len(clusters) != confirmed_cluster_count
        or not isinstance(planned, list)
    ):
        raise OperatorError(unresolved, detail="the ingest summary list counts do not reconcile")
    expected_planned = 6 + 2 * submission_files + candidate_count
    if confirmation_sha256 is not None:
        expected_planned += 3
    if len(planned) != expected_planned or len(planned) != len(set(planned)):
        raise OperatorError(unresolved, detail="the ingest summary planned files do not reconcile")
    if any(
        not isinstance(value, str) or not value or len(value) > MAX_PREVIEW_LINE_CHARACTERS
        for value in (*candidates, *clusters)
    ):
        raise OperatorError(unresolved, detail="the ingest summary carries an invalid preview line")
    if any(
        not isinstance(value, str)
        or not value
        or len(value) > MAX_PREVIEW_LINE_CHARACTERS
        or Path(value).name != value
        for value in planned
    ):
        raise OperatorError(unresolved, detail="the ingest summary carries an unsafe planned name")
    return summary


def _output_identity(response: dict[str, Any], unresolved: ErrorCode) -> tuple[int, int]:
    value = response.get("output_identity")
    if not isinstance(value, dict) or set(value) != {"device", "inode"}:
        raise OperatorError(unresolved, detail="the ingest result has no output directory identity")
    identity = (value["device"], value["inode"])
    if any(not isinstance(part, int) or isinstance(part, bool) or part < 0 for part in identity):
        raise OperatorError(unresolved, detail="the ingest result has an invalid output identity")
    return identity


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
