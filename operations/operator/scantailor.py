"""The explicit, custody-confined ScanTailor handoff.

ScanTailor Advanced is a separate desktop application.  This module deliberately
does not pretend to launch or control it: it gives the operator the exact project
file and source paths to open, then imports only the saved split geometry.  The
import is a preview followed by a digest-pinned confined commit, like the other
operator writes.  No geometry is preferred, completed, or converted to output
pixels here.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .custody import python_module_command, run_confined
from .errors import ErrorCode, OperatorError, strip_control_bytes


def instruction(project: Path) -> str:
    """Describe the desktop seam without claiming an executable is available."""

    return (
        "ScanTailor seam — a separate desktop program does the splitting; Verbatus does not.\n"
        f"1. Open this existing ScanTailor Advanced project: {strip_control_bytes(str(project))}\n"
        "2. In ScanTailor, inspect and save its page-split geometry. Do not submit ScanTailor output images: "
        "the submitted masters remain the Exemplar.\n"
        "3. Return here and import that saved project. This records geometry only; it does not choose "
        "among pages or geometries, crop an image, or advance a stage."
    )


def import_in_custody(
    *, project: Path, output_dir: Path, workspace: Path, printer: Callable[[str], None]
) -> None:
    """Preview then append one immutable geometry document through the write boundary."""

    project = project if project.is_absolute() else workspace / project
    output_dir = output_dir if output_dir.is_absolute() else workspace / output_dir
    request = {
        "project": str(project),
        "output_dir": str(output_dir),
        "expected_project_sha256": None,
    }
    preview = _call(request, "preview", writable=None, workspace=workspace)
    summary = _summary(preview)
    printer(
        "ScanTailor geometry preview — no document has been written.\n"
        f"Project: {strip_control_bytes(str(project))}\n"
        f"Project digest: {summary['project_sha256']}\n"
        f"Source images in the project: {summary['image_count']}; "
        f"saved page-split geometry records: {summary['geometry_count']}.\n"
        "The commit is pinned to this exact project digest. If ScanTailor saves different bytes first, "
        "the import refuses and nothing is written."
    )
    committed = _call(
        {**request, "expected_project_sha256": summary["project_sha256"]},
        "commit",
        writable=output_dir.resolve(),
        workspace=workspace,
    )
    result = _summary(committed)
    printer(
        "Recorded ScanTailor geometry document: "
        f"{strip_control_bytes(str(result['document_path']))} ({result['document_sha256']}).\n"
        "It is a digest-bound record of the project geometry, not a selected or applied result."
    )


def _call(
    request: dict[str, Any], operation: str, *, writable: Path | None, workspace: Path
) -> dict[str, Any]:
    command = python_module_command("operations.operator.scantailor_worker", workspace)
    backend, completed = run_confined(
        command,
        writable=writable,
        cwd=workspace,
        input_text=json.dumps({"operation": operation, **request}),
    )
    if completed.returncode:
        # A boundary that never came up is not a ScanTailor refusal, and telling
        # the operator to correct their project file would send them to fix
        # something that was never read. `ingest._call_worker` separates the two
        # the same way; this path did not.
        launcher = backend.launcher_failure(completed)
        if launcher is not None:
            raise OperatorError(ErrorCode.CONSOLE_CUSTODY_REFUSED, detail=launcher)
    try:
        response = json.loads(completed.stdout)
    except (TypeError, ValueError) as error:
        raise OperatorError(
            ErrorCode.SCANTAILOR_REFUSED, detail=completed.stderr or str(error)
        ) from error
    if (
        completed.returncode
        or not isinstance(response, dict)
        or response.get("status") not in {"preview", "committed"}
    ):
        detail = (
            response.get("reason", completed.stderr)
            if isinstance(response, dict)
            else completed.stderr
        )
        raise OperatorError(ErrorCode.SCANTAILOR_REFUSED, detail=str(detail))
    return response


def _summary(response: dict[str, Any]) -> dict[str, Any]:
    value = response.get("summary")
    required = {
        "project_sha256",
        "image_count",
        "geometry_count",
        "document_path",
        "document_sha256",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise OperatorError(
            ErrorCode.SCANTAILOR_REFUSED, detail="the confined importer returned an invalid summary"
        )
    return value
