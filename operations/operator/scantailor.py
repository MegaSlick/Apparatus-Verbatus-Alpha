"""The custody-confined boundary to the external ScanTailor Advanced app.

Verbatus may name a project for the operator and import its saved split geometry;
it may not launch ScanTailor, prefer or complete geometry, or turn that geometry
into output pixels. Publication requires a preview followed by a digest-pinned
confined commit.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from common.contracts.canonical import is_sha256

from .custody import python_module_command, run_confined
from .errors import ErrorCode, OperatorError, strip_control_bytes


def instruction(project: Path, *, workspace: Path | None = None) -> str:
    """The surface must not imply that Verbatus can launch the external app."""

    if workspace is not None:
        project = _absolute_path(project, workspace)
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
    """Publication requires the confined commit to replay the previewed project digest."""

    project = _absolute_path(project, workspace)
    output_dir = _absolute_path(output_dir, workspace)
    request = {
        "project": str(project),
        "output_dir": str(output_dir),
        "expected_project_sha256": None,
    }
    preview = _call(request, "preview", writable=None, workspace=workspace)
    summary = _summary(preview, operation="preview", output_dir=output_dir)
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
    result = _summary(committed, operation="commit", output_dir=output_dir)
    printer(
        "Recorded ScanTailor geometry document: "
        f"{strip_control_bytes(str(result['document_path']))} ({result['document_sha256']}).\n"
        "It is a digest-bound record of the project geometry, not a selected or applied result."
    )


def _call(
    request: dict[str, Any], operation: str, *, writable: Path | None, workspace: Path
) -> dict[str, Any]:
    error_code = (
        ErrorCode.SCANTAILOR_REFUSED if operation == "preview" else ErrorCode.SCANTAILOR_UNRESOLVED
    )
    command = python_module_command("operations.operator.scantailor_worker", workspace)
    backend, completed = run_confined(
        command,
        writable=writable,
        cwd=workspace,
        input_text=json.dumps({"operation": operation, **request}),
    )
    if completed.returncode:
        # A failed custody launcher never read the project, so its recovery path
        # must not tell the operator to repair ScanTailor input.
        launcher = backend.launcher_failure(completed)
        if launcher is not None:
            raise OperatorError(ErrorCode.CONSOLE_CUSTODY_REFUSED, detail=launcher)
    try:
        response = json.loads(completed.stdout)
    except (TypeError, ValueError) as error:
        raise OperatorError(error_code, detail=completed.stderr or str(error)) from error
    expected_status = "preview" if operation == "preview" else "committed"
    if (
        completed.returncode
        or not isinstance(response, dict)
        or response.get("status") != expected_status
    ):
        detail = (
            response.get("reason", completed.stderr)
            if isinstance(response, dict)
            else completed.stderr
        )
        raise OperatorError(error_code, detail=str(detail))
    return response


def _summary(response: dict[str, Any], *, operation: str, output_dir: Path) -> dict[str, Any]:
    value = response.get("summary")
    required = {
        "project_sha256",
        "image_count",
        "geometry_count",
        "document_path",
        "document_sha256",
    }
    error_code = (
        ErrorCode.SCANTAILOR_REFUSED if operation == "preview" else ErrorCode.SCANTAILOR_UNRESOLVED
    )
    if (
        set(response) != {"status", "summary"}
        or not isinstance(value, dict)
        or set(value) != required
        or not is_sha256(value.get("project_sha256"))
        or not is_sha256(value.get("document_sha256"))
        or not _positive_int(value.get("image_count"))
        or not _positive_int(value.get("geometry_count"))
        or value["geometry_count"] > value["image_count"]
        or not isinstance(value.get("document_path"), str)
        or Path(value["document_path"])
        != output_dir / f"scantailor-geometry-{value['document_sha256']}.json"
    ):
        raise OperatorError(error_code, detail="the confined importer returned an invalid summary")
    return value


def _absolute_path(path: Path, workspace: Path) -> Path:
    """Anchor and normalize a UI path without following a selected symlink."""

    anchored = path if path.is_absolute() else workspace / path
    return Path(os.path.normpath(anchored))


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1
