"""The framework's single supported writer for public Spec 05 findings.

It accepts only a sealed, non-synthetic :class:`MeasurementRun` and projects the
closed aggregate shape, which `redaction.project_public_finding` validates before
it returns; the filename is date-only.  It cannot make a manually authored history
file safe; the repository-wide history policy remains a separate governance
boundary.
"""

from __future__ import annotations

import json
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
    try:
        history_directory.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as handle:
            handle.write(payload)
    except FileExistsError as error:
        raise PublicSafetyRefusal("public finding already exists; history is write-once") from error
    return target
