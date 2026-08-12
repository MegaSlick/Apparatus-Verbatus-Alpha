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
        data = path.read_bytes()
        value.update(relative.encode("utf-8"))
        value.update(b"\0")
        value.update(len(data).to_bytes(8, "big"))
        value.update(data)
    return value.hexdigest()


def main() -> int:
    root = Path(sys.argv[1]).resolve() if len(sys.argv) == 2 else Path(__file__).parents[2]
    try:
        print(digest(root))
    except OSError as error:
        print(f"fingerprint: cannot read an image input ({type(error).__name__})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
