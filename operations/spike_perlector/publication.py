"""The framework's single supported writer for public Spec 05 findings.

It accepts only a sealed, non-synthetic :class:`MeasurementRun`, projects the
closed aggregate shape, validates it immediately before writing, and uses a
date-only filename.  It cannot make a manually authored history file safe; the
repository-wide history policy remains a separate governance boundary.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from .errors import PublicSafetyRefusal
from .redaction import project_public_finding, validate_public_finding
from .runner import MeasurementRun


def write_public_finding(
    run: MeasurementRun, *, history_directory: Path, finding_date: date
) -> Path:
    """Write one validated aggregate-only JSON finding without overwriting history."""

    if not isinstance(history_directory, Path):
        raise PublicSafetyRefusal("history_directory must be a Path")
    # datetime is a subclass of date; accepting one would embed a time-of-day into
    # the "date-only filename" this docstring promises and let two calls on the
    # same real day each pick a distinct microsecond-precision name, defeating the
    # write-once duplicate guard below.
    if not isinstance(finding_date, date) or isinstance(finding_date, datetime):
        raise PublicSafetyRefusal("finding_date must be a date, not a datetime")
    finding = project_public_finding(run)
    validate_public_finding(finding)
    payload = (
        json.dumps(finding, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        + b"\n"
    )
    # Built from the integer fields rather than finding_date.isoformat(): a date
    # subclass could override isoformat() to return an arbitrary string (e.g. one
    # containing "/" or "..") and write outside history_directory.
    target = history_directory / (
        f"{finding_date.year:04d}-{finding_date.month:02d}-{finding_date.day:02d}"
        "_reading_claim_metrics.json"
    )
    try:
        history_directory.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as handle:
            handle.write(payload)
    except FileExistsError as error:
        raise PublicSafetyRefusal("public finding already exists; history is write-once") from error
    return target
