"""Observable synthetic cleanup checks for the submit door.

The drill's threat model is limited deliberately: it detects a declared target
path, temporary path, log marker, or listed volume object left after a synthetic
cleanup.  It cannot establish forensic unrecoverability from storage media,
snapshots, or provider backups, so it makes no such claim.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from common.contracts.errors import ContractError


class CleanupDrillRefusal(ContractError):
    """The synthetic cleanup drill could not verify one declared bound."""


@dataclass(frozen=True)
class CleanupDrillResult:
    """Counts that make one synthetic cleanup drill reviewable.

    ``volume_objects_seen`` is ``None``, not ``0``, when no volume applied to this
    drill. Zero for both would mean "there was no volume to check" and "a volume was
    checked and found empty" produce identical records, and a reader could not tell
    them apart — a cleanup drill must keep unknown distinct from zero.
    """

    target_paths_checked: int
    temporary_paths_checked: int
    logs_scanned: int
    volume_objects_seen: int | None


def verify_synthetic_cleanup(
    *,
    target_paths: Iterable[Path],
    temporary_paths: Iterable[Path],
    log_paths: Iterable[Path],
    forbidden_markers: Iterable[bytes],
    volume_objects: Iterable[str] | None,
) -> CleanupDrillResult:
    """Refuse unless every declared, observable cleanup bound is clean.

    ``volume_objects`` is ``None`` only when no volume applies to this drill;
    otherwise the caller supplies its object listing and it must be empty.  The
    function never deletes a path itself.  It verifies an already-completed,
    explicitly synthetic cleanup rather than making a destructive action look
    like successful disposal.
    """
    targets = tuple(Path(path) for path in target_paths)
    temporary = tuple(Path(path) for path in temporary_paths)
    logs = tuple(Path(path) for path in log_paths)
    markers = tuple(forbidden_markers)
    if not targets:
        raise CleanupDrillRefusal("cleanup drill has no declared target paths to check")
    if not temporary:
        raise CleanupDrillRefusal("cleanup drill has no declared temporary paths to check")
    if not logs:
        raise CleanupDrillRefusal("cleanup drill has no declared logs to scan")
    if not markers or any(not isinstance(marker, bytes) or not marker for marker in markers):
        raise CleanupDrillRefusal("cleanup drill has no non-empty log markers to scan")

    if any(not _is_absent(path) for path in targets):
        raise CleanupDrillRefusal("cleanup drill target path remains")
    if any(not _is_absent(path) for path in temporary):
        raise CleanupDrillRefusal("cleanup drill temporary path remains")

    for path in logs:
        try:
            content = path.read_bytes()
        except OSError as error:
            raise CleanupDrillRefusal("cleanup drill log could not be scanned") from error
        if any(marker in content for marker in markers):
            raise CleanupDrillRefusal("cleanup drill log contains a forbidden marker")

    objects = None if volume_objects is None else tuple(volume_objects)
    if objects:
        raise CleanupDrillRefusal("cleanup drill volume object listing is not empty")
    return CleanupDrillResult(
        target_paths_checked=len(targets),
        temporary_paths_checked=len(temporary),
        logs_scanned=len(logs),
        volume_objects_seen=None if objects is None else len(objects),
    )


def _is_absent(path: Path) -> bool:
    """Check the directory entry itself, including a dangling symlink."""
    try:
        path.lstat()
    except FileNotFoundError:
        return True
    except OSError as error:
        raise CleanupDrillRefusal("cleanup drill path could not be checked") from error
    return False
