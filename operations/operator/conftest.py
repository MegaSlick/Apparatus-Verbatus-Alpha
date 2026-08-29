"""Whether this host can actually establish the OS boundary these tests measure.

A boundary test that runs where the kernel or the launcher cannot establish the
boundary reports nothing about this repository. It either fails for a host
reason (which reads as a defect that is not there) or, worse, passes because the
guarded child never ran at all — the shape
`test_the_landlock_boundary_refuses_a_confined_write_to_evidence` had, where a
`setpriv` that rejected its own option satisfied "the write was refused".

The probe below asks the host tool directly, with a literal minimal argument
vector rather than through `custody.LandlockConfinement.command`. That is
deliberate: a defect in the command this repository builds must never be able to
present itself as an absent host capability and skip the tests that would have
caught it. Only two recognised host gaps produce a skip — a `setpriv` that
predates Landlock support, and a kernel that refuses to create a ruleset. Any
other failure returns no gap, so the tests run and fail loudly.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest

# The reviewed system location `LandlockConfinement` pins; a host without it
# cannot reach Landlock through this repository's one permitted launcher.
_SETPRIV: Final = Path("/usr/bin/setpriv")

# `setpriv` exits with this when it establishes no privilege change at all,
# which is how a kernel without Landlock surfaces (util-linux
# `SETPRIV_EXIT_PRIVERR`; see the note beside the same constant in custody.py).
_PRIVILEGE_FAILURE_EXIT: Final = 127


def _linux_landlock_gap() -> str | None:
    """Name why this host cannot reach Landlock, or return ``None`` when it can.

    Returns ``None`` off Linux as well: the Linux-only marker below states that
    separately, and the cross-platform marker must not skip a macOS host whose
    own Seatbelt backend is present and working.
    """

    if sys.platform != "linux":
        return None
    if not _SETPRIV.is_file():
        return f"this host has no {_SETPRIV}"
    probe = subprocess.run(
        [str(_SETPRIV), "--no-new-privs", "--landlock-access", "fs:write-file", "--", "true"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode == 0:
        return None
    diagnostic = (probe.stderr or probe.stdout or "").strip()
    if "unrecognized option" in diagnostic:
        return (
            f"{_SETPRIV} predates Landlock support, so this host cannot establish the "
            "boundary at all (util-linux added --landlock-access in 2.40)"
        )
    if probe.returncode == _PRIVILEGE_FAILURE_EXIT:
        return f"this kernel refused to create a Landlock ruleset: {diagnostic or 'no diagnostic'}"
    return None


LANDLOCK_GAP: Final[str | None] = _linux_landlock_gap()

_NOT_LINUX: Final[str | None] = None if sys.platform == "linux" else "Landlock is Linux-only"


def requires_landlock(what: str):
    """Mark a test that can only be measured on a Linux host with real Landlock."""

    gap = _NOT_LINUX or LANDLOCK_GAP
    return pytest.mark.skipif(gap is not None, reason=f"{what}: {gap}")


# For the tests that launch a real confined child on whichever backend this host
# has. macOS keeps them (Seatbelt's absence is its own test); Linux drops them
# only where the launcher above proved Landlock is out of reach.
requires_host_boundary: Final = pytest.mark.skipif(
    LANDLOCK_GAP is not None,
    reason=f"this host cannot establish its confinement boundary: {LANDLOCK_GAP}",
)

# The converse: a host that cannot reach Landlock is the only place the
# fail-shut path can be proven against a real launcher instead of a stub.
requires_absent_landlock: Final = pytest.mark.skipif(
    LANDLOCK_GAP is None,
    reason="this host can establish its confinement boundary, so nothing fails shut here",
)
