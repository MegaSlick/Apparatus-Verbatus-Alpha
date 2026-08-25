#!/usr/bin/env python3
"""Print the exact third-party inventory imported by this interpreter.

The full gate passes this projection to pip-audit with dependency resolution
disabled.  Auditing requirements-dev.txt would resolve its transitive closure
again and can therefore examine versions other than the ones uv.lock installed.
"""

from __future__ import annotations

import re
import sys
from importlib.metadata import distributions

PROJECT_DISTRIBUTION = "verbatus"


def installed_pins() -> dict[str, str]:
    """Return one unambiguous name/version pair per installed distribution."""

    pins: dict[str, str] = {}
    for distribution in distributions():
        raw_name = distribution.metadata.get("Name")
        if not raw_name:
            raise ValueError("an installed distribution has no Name metadata")
        name = re.sub(r"[-_.]+", "-", raw_name).lower()
        if name == PROJECT_DISTRIBUTION:
            continue
        version = distribution.version
        if not version:
            raise ValueError(f"installed distribution {name!r} has no version metadata")
        previous = pins.setdefault(name, version)
        if previous != version:
            raise ValueError(
                f"installed distribution {name!r} has conflicting versions "
                f"{previous!r} and {version!r}"
            )
    return pins


def main() -> int:
    if len(sys.argv) != 1:
        print("frozen-audit-requirements: no arguments are accepted", file=sys.stderr)
        return 2
    try:
        pins = installed_pins()
    except ValueError as error:
        print(f"frozen-audit-requirements: {error}", file=sys.stderr)
        return 1
    for name, version in sorted(pins.items()):
        print(f"{name}=={version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
