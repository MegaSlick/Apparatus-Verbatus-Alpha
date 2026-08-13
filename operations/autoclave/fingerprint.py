#!/usr/bin/env python3
"""Print the digest of every repository input that defines an autoclave image."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

INPUTS = (
    ".dockerignore",
    "requirements-dev.txt",
    "operations/autoclave/Dockerfile",
    "operations/autoclave/agent-brief.md",
    "operations/autoclave/fingerprint.py",
    "operations/autoclave/refresh_claude_token.py",
)


def digest(root: Path) -> str:
    value = hashlib.sha256()
    for relative in INPUTS:
        path = root / relative
        try:
            data = path.read_bytes()
        except OSError as error:
            # These are tracked repository paths, never secrets, so naming the one that
            # failed costs nothing. `build` and `new` both stop here; without the name
            # the operator is told that one of six files might be the problem.
            raise OSError(
                error.errno, f"cannot read the image input {relative}: {error.strerror}"
            ) from error
        value.update(relative.encode("utf-8"))
        value.update(b"\0")
        value.update(len(data).to_bytes(8, "big"))
        value.update(data)
    return value.hexdigest()


def main() -> int:
    # Both branches resolve: a relative invocation from another directory would
    # otherwise produce a relative root, and the two spellings would disagree.
    root = (
        Path(sys.argv[1]).resolve() if len(sys.argv) == 2 else Path(__file__).resolve().parents[2]
    )
    try:
        print(digest(root))
    except OSError as error:
        print(f"fingerprint: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
