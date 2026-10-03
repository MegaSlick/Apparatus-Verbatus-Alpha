"""Audit the locked serving inventory exactly as the GPU pod installs it.

Usage: serving_audit.py EXPORT PYPROJECT WORK_DIRECTORY

EXPORT is `uv export --group pod` output. It keeps the lock's environment markers, and
pip-audit silently drops every line whose marker is false on the machine running it,
so a gate on a Mac would audit none of the Linux-only serving stack. Each marker is
evaluated here for the pod's target, CPython on Linux x86_64, at every Python version
the markers distinguish, and the pins are audited without markers. A package locked at
two versions (one per Python) is audited from a further file, because pip-audit refuses
a name listed twice. Every pip-audit run is `--strict`, so an advisory lookup that
cannot complete fails the gate.
"""

import re
import subprocess
import sys
import tomllib
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

PYTHON_NAME = re.compile(r"python(?:_full)?_version")
# The only Python comparisons uv writes into a lock. Evaluating at the floor and at each
# literal covers both sides of every one; any other form could be true only between the
# tested versions, so it is refused rather than evaluated.
PYTHON_COMPARISON = re.compile(
    r"python(?:_full)?_version\s*(?:<|>=|==)\s*[\"']([0-9]+(?:\.[0-9]+)*)(?:\.\*)?[\"']"
)

# Advisories reviewed and accepted for one exact locked pin. Each applies only while
# that pin is in the inventory, so a lock change ends it and the advisory, or any new
# one, fails the gate again.
REVIEWED_ADVISORIES: dict[tuple[str, str], dict[str, str]] = {
    ("setuptools", "80.10.2"): {
        "PYSEC-2026-3447": (
            "vllm 0.30.0 requires setuptools<81 and the fix is 83.0.0. The flaw is in "
            "building a source distribution on a Unicode-normalizing (macOS) filesystem; "
            "the pod installs wheels on Linux and builds no sdist, and this project's "
            "own build uses setuptools 84.0.0."
        ),
    },
}


class InventoryError(Exception):
    pass


def target_environments(pins: list[Requirement], requires_python: str) -> list[dict[str, str]]:
    """The pod's platform at the floor Python and at each Python version a marker names."""
    floor = re.fullmatch(r"\s*>=\s*([0-9.]+)\s*", requires_python)
    if floor is None:
        raise InventoryError(f"requires-python {requires_python!r} is not a plain '>=' floor")
    versions = {Version(floor.group(1))}
    for pin in pins:
        if pin.marker is not None:
            marker = str(pin.marker)
            literals = PYTHON_COMPARISON.findall(marker)
            if len(literals) != len(PYTHON_NAME.findall(marker)):
                raise InventoryError(
                    f"{pin.name}'s marker {marker!r} compares the Python version in a form "
                    "this audit does not evaluate"
                )
            versions.update(Version(literal) for literal in literals)
    environments = []
    for version in sorted(v for v in versions if v >= Version(floor.group(1))):
        full = ".".join(str(part) for part in (*version.release, 0, 0)[:3])
        environments.append(
            {
                "implementation_name": "cpython",
                "implementation_version": full,
                "os_name": "posix",
                "platform_machine": "x86_64",
                "platform_python_implementation": "CPython",
                "platform_release": "",
                "platform_system": "Linux",
                "platform_version": "",
                "python_full_version": full,
                "python_version": ".".join(full.split(".")[:2]),
                "sys_platform": "linux",
            }
        )
    return environments


def read_pins(export: str) -> list[Requirement]:
    pins = []
    for line in export.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            pin = Requirement(line)
        except InvalidRequirement as error:
            raise InventoryError(f"export line {line!r} is not a requirement: {error}") from error
        specifiers = list(pin.specifier)
        if pin.url or len(specifiers) != 1 or specifiers[0].operator != "==":
            raise InventoryError(f"export line {line!r} is not an exact version pin")
        pins.append(pin)
    return pins


def serving_inventory(export: str, pyproject: dict) -> list[list[tuple[str, str]]]:
    """The pod's pins as (name, version), in layers that each name a package once."""
    pins = read_pins(export)
    environments = target_environments(pins, pyproject["project"]["requires-python"])
    versions: dict[str, set[str]] = {}
    for environment in environments:
        installed = set()
        for pin in pins:
            if pin.marker is None or pin.marker.evaluate(environment):
                name = canonicalize_name(pin.name)
                installed.add(name)
                versions.setdefault(name, set()).add(next(iter(pin.specifier)).version)
        for declared in pyproject["dependency-groups"]["pod"]:
            name = canonicalize_name(Requirement(declared).name)
            if name not in installed:
                raise InventoryError(
                    f"the serving group's {name} is missing from the export for Python "
                    f"{environment['python_full_version']} on Linux x86_64"
                )
    layers: list[list[tuple[str, str]]] = []
    for name in sorted(versions):
        for depth, version in enumerate(sorted(versions[name], key=Version)):
            if depth == len(layers):
                layers.append([])
            layers[depth].append((name, version))
    return layers


def audit(layers: list[list[tuple[str, str]]], directory: Path) -> int:
    status = 0
    for index, layer in enumerate(layers, start=1):
        requirements = directory / f"serving-{index}.txt"
        requirements.write_text(
            "".join(f"{name}=={version}\n" for name, version in layer), encoding="utf-8"
        )
        # pip-audit applies --ignore-vuln to the whole file; a layer names each package
        # once, and every reviewed advisory belongs to one package.
        ignored = []
        for pin in layer:
            for advisory, reason in REVIEWED_ADVISORIES.get(pin, {}).items():
                print(
                    f"serving_audit: {pin[0]} {pin[1]} {advisory} is reviewed and accepted: "
                    f"{reason}",
                    file=sys.stderr,
                )
                ignored += ["--ignore-vuln", advisory]
        command = [sys.executable, "-m", "pip_audit", "--strict", "--no-deps", "--disable-pip"]
        result = subprocess.run([*command, *ignored, "--requirement", str(requirements)])
        status = status or result.returncode
    return status


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: serving_audit.py EXPORT PYPROJECT WORK_DIRECTORY", file=sys.stderr)
        return 2
    export, pyproject, directory = Path(argv[0]), Path(argv[1]), Path(argv[2])
    try:
        layers = serving_inventory(
            export.read_text(encoding="utf-8"), tomllib.loads(pyproject.read_text("utf-8"))
        )
    except KeyError as error:
        print(f"serving_audit: pyproject.toml declares no {error}", file=sys.stderr)
        return 1
    except InventoryError as error:
        print(f"serving_audit: {error}", file=sys.stderr)
        return 1
    return audit(layers, directory)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
