#!/usr/bin/env python3
"""Build the current tracked/unignored tree without dirtying the work folder."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from importlib import metadata
from pathlib import Path

EXACT_REQUIREMENT = re.compile(
    r"(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[A-Za-z0-9][A-Za-z0-9._+-]*)"
)


def normalized_distribution(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def exact_requirements(lines: list[str], source: Path) -> dict[str, tuple[str, str]]:
    requirements: dict[str, tuple[str, str]] = {}
    for raw_line in lines:
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        match = EXACT_REQUIREMENT.fullmatch(line)
        if match is None:
            raise RuntimeError(
                f"{source} contains a build requirement without an exact pin: {line}"
            )
        name = match.group("name")
        requirements[normalized_distribution(name)] = (name, match.group("version"))
    return requirements


def verify_build_environment(root: Path) -> None:
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    declared = exact_requirements(
        list(pyproject.get("build-system", {}).get("requires", [])),
        root / "pyproject.toml [build-system].requires",
    )
    development = exact_requirements(
        (root / "requirements-dev.txt").read_text(encoding="utf-8").splitlines(),
        root / "requirements-dev.txt",
    )

    for normalized, (name, expected_version) in declared.items():
        if development.get(normalized) != (name, expected_version):
            raise RuntimeError(
                f"build backend {name}=={expected_version} must have the same exact pin "
                "in requirements-dev.txt"
            )
        try:
            installed_version = metadata.version(name)
        except metadata.PackageNotFoundError as error:
            raise RuntimeError(
                f"build backend {name}=={expected_version} is not installed; "
                "run 'python3 -m pip install -r requirements-dev.txt' while online"
            ) from error
        if installed_version != expected_version:
            raise RuntimeError(
                f"build backend {name} is {installed_version}, expected {expected_version}; "
                "run 'python3 -m pip install -r requirements-dev.txt' while online"
            )


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


def wheel_command(source: Path, wheels: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "pip",
        "wheel",
        str(source),
        "--no-deps",
        "--no-build-isolation",
        "--no-index",
        "--disable-pip-version-check",
        "--wheel-dir",
        str(wheels),
    ]


def main() -> int:
    # git terminates the path with a newline and nothing else. Stripping general
    # whitespace would silently rename a repository whose directory ends in a
    # space, and the build would then package an empty tree from a path that
    # does not exist.
    root = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout.rstrip("\n")
    )
    verify_build_environment(root)
    with tempfile.TemporaryDirectory(prefix="verbatus-wheel-") as temporary:
        workspace = Path(temporary)
        source = workspace / "source"
        wheels = workspace / "wheels"
        source.mkdir()
        wheels.mkdir()
        copy_repository(root, source)
        subprocess.run(wheel_command(source, wheels), check=True)
        built = list(wheels.glob("*.whl"))
        if len(built) != 1:
            raise RuntimeError(f"expected one wheel, found {len(built)}")
        with zipfile.ZipFile(built[0]) as archive:
            if "common/__init__.py" not in archive.namelist():
                raise RuntimeError("wheel does not contain the common package")
        print(f"Wheel check passed: {built[0].name}")
    return 0


def entrypoint() -> int:
    try:
        return main()
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Wheel check failed: {error}", file=sys.stderr)
        return 1
    except Exception as error:
        # Anything else — a missing pyproject.toml, an unreadable file, a
        # malformed TOML — used to escape as a raw traceback. That still failed
        # the build, but it read as the checker breaking rather than the check
        # refusing. The class name is kept so nothing is lost by the shorter
        # report.
        print(f"Wheel check failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(entrypoint())
