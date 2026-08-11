"""The framework's single supported writer for public Spec 05 findings.

It accepts only a sealed, non-synthetic :class:`MeasurementRun` and projects the
closed aggregate shape, which `redaction.project_public_finding` validates before
it returns; the filename is date-only.  It cannot make a manually authored history
file safe; the repository-wide history policy remains a separate governance
boundary.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path

from .errors import PublicSafetyRefusal
from .redaction import project_public_finding
from .runner import MeasurementRun


def write_public_finding(
    run: MeasurementRun, *, history_directory: Path, finding_date: date
) -> Path:
    """Write one validated aggregate-only JSON finding without overwriting history."""

    if not isinstance(history_directory, Path):
        raise PublicSafetyRefusal("history_directory must be a Path")
    # datetime subclasses date, so two calls on the same real day would pick
    # distinct microsecond-precision names and slip past the write-once guard.
    if not isinstance(finding_date, date) or isinstance(finding_date, datetime):
        raise PublicSafetyRefusal("finding_date must be a date, not a datetime")
    finding = project_public_finding(run)
    payload = (
        json.dumps(finding, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        + b"\n"
    )
    # Integer fields rather than isoformat(): a date subclass can override that
    # to return "../.." and steer the write outside history_directory.
    target = history_directory / (
        f"{finding_date.year:04d}-{finding_date.month:02d}-{finding_date.day:02d}"
        "_reading_claim_metrics.json"
    )
    # Outside the guard below: `mkdir` raises `FileExistsError` too, when the
    # history path is a file rather than a directory. Inside, that surfaced as
    # "this finding was already published" — an operator would read it as work
    # already done and stop looking, with nothing ever written.
    try:
        history_directory.mkdir(parents=True, exist_ok=True)
    except FileExistsError as error:
        raise PublicSafetyRefusal(
            f"the public finding history at {history_directory} is not a directory"
        ) from error
    if target.exists():
        raise PublicSafetyRefusal("public finding already exists; history is write-once")
    # Written beside the target and moved into place, so the dated path only ever
    # holds a complete finding. A direct `open("xb")` left a truncated JSON file
    # at that path if the process died or the disk filled mid-write — and every
    # later call then refused it as already published, so the half-written
    # finding could never be repaired by the writer that made it.
    # Per-process, so two writers attempting the same day cannot share a scratch
    # file — one would otherwise unlink the other's mid-write in the `finally`.
    scratch = target.with_name(f".{target.name}.{os.getpid()}.partial")
    try:
        with scratch.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(scratch, target)
        # The directory entry too, not only the file's contents: without it a
        # crash after the link can leave the finding's bytes on disk with no
        # name pointing at them, which reads afterwards as never published.
        directory_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except FileExistsError as error:
        raise PublicSafetyRefusal("public finding already exists; history is write-once") from error
    finally:
        scratch.unlink(missing_ok=True)
    return target
