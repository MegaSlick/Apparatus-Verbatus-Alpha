#!/usr/bin/env python3
"""Build the current tracked/unignored tree without dirtying the work folder."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


def repository_files(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
    )
    return [Path(os.fsdecode(item)) for item in result.stdout.split(b"\0") if item]


def copy_repository(root: Path, destination: Path) -> None:
    for relative in repository_files(root):
        source = root / relative
        target = destination / relative
        if not source.exists() and not source.is_symlink():
            # Reflect a working-tree deletion even before it is staged.
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            target.symlink_to(os.readlink(source))
        elif source.is_file():
            shutil.copy2(source, target)
        else:
            raise RuntimeError(f"cannot package unsupported path: {relative}")


def main() -> int:
    root = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout.strip()
    )
    with tempfile.TemporaryDirectory(prefix="verbatus-wheel-") as temporary:
        workspace = Path(temporary)
        source = workspace / "source"
        wheels = workspace / "wheels"
        source.mkdir()
        wheels.mkdir()
        copy_repository(root, source)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                str(source),
                "--no-deps",
                "--wheel-dir",
                str(wheels),
            ],
            check=True,
        )
        built = list(wheels.glob("*.whl"))
        if len(built) != 1:
            raise RuntimeError(f"expected one wheel, found {len(built)}")
        with zipfile.ZipFile(built[0]) as archive:
            if "common/__init__.py" not in archive.namelist():
                raise RuntimeError("wheel does not contain the common package")
        print(f"Wheel check passed: {built[0].name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
