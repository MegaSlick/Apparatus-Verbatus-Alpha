"""Tests for the serving-inventory audit the full gate runs."""

import importlib.util
import subprocess
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parent / "serving_audit.py"
_spec = importlib.util.spec_from_file_location("serving_audit_under_test", SOURCE)
serving_audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(serving_audit)

PYPROJECT = {
    "project": {"requires-python": ">=3.12"},
    "dependency-groups": {"pod": ["served==2.0; sys_platform == 'linux'"]},
}


def audited(monkeypatch, tmp_path, export):
    commands = []

    def record(command):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(serving_audit.subprocess, "run", record)
    layers = serving_audit.serving_inventory(export, PYPROJECT)
    assert serving_audit.audit(layers, tmp_path) == 0
    return [command[command.index("--strict") :] for command in commands]


@pytest.mark.parametrize(
    ("version", "ignored"),
    [("80.10.2", ["--ignore-vuln", "PYSEC-2026-3447"]), ("80.10.3", [])],
)
def test_a_reviewed_advisory_is_accepted_only_for_its_exact_pin(
    monkeypatch, tmp_path, capsys, version, ignored
):
    export = f"served==2.0 ; sys_platform == 'linux'\nsetuptools=={version}\n"

    (command,) = audited(monkeypatch, tmp_path, export)

    assert command == [
        "--strict",
        "--no-deps",
        "--disable-pip",
        *ignored,
        "--requirement",
        str(tmp_path / "serving-1.txt"),
    ]
    assert ("is reviewed and accepted" in capsys.readouterr().err) == bool(ignored)


def test_a_serving_package_the_pod_would_not_install_is_refused():
    export = "served==2.0 ; sys_platform == 'darwin'\n"

    with pytest.raises(serving_audit.InventoryError, match="served is missing"):
        serving_audit.serving_inventory(export, PYPROJECT)
