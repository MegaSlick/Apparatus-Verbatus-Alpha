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

    def record(command, **_):
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


@pytest.mark.parametrize("operator", [">", "<=", "!=", "~=", "==="])
def test_a_python_comparison_the_audit_does_not_evaluate_is_refused(operator):
    export = (
        "served==2.0 ; sys_platform == 'linux'\n"
        f"numpy==1.0 ; python_full_version {operator} '3.13' and sys_platform == 'linux'\n"
    )

    with pytest.raises(serving_audit.InventoryError, match="numpy's marker"):
        serving_audit.serving_inventory(export, PYPROJECT)


def test_a_wildcard_python_version_is_evaluated_on_its_minor():
    export = (
        "served==2.0 ; sys_platform == 'linux'\n"
        "numpy==1.0 ; python_full_version == '3.13.*' and sys_platform == 'linux'\n"
    )

    assert serving_audit.serving_inventory(export, PYPROJECT) == [
        [("numpy", "1.0"), ("served", "2.0")]
    ]


TWO_LAYERS = [[("numpy", "1.0"), ("served", "2.0")], [("numpy", "2.0")]]


def test_a_failed_earlier_layer_fails_the_audit_though_a_later_one_passes(monkeypatch, tmp_path):
    statuses = iter([1, 0])
    monkeypatch.setattr(
        serving_audit.subprocess,
        "run",
        lambda command, **_: subprocess.CompletedProcess(command, next(statuses)),
    )

    assert serving_audit.audit(TWO_LAYERS, tmp_path) == 1


def test_an_audit_that_does_not_finish_in_time_fails(monkeypatch, tmp_path, capsys):
    timeouts = []

    def hang_first(command, **options):
        timeouts.append(options.get("timeout"))
        if len(timeouts) == 1:
            raise subprocess.TimeoutExpired(command, options["timeout"])
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(serving_audit.subprocess, "run", hang_first)

    assert serving_audit.audit(TWO_LAYERS, tmp_path) == 1
    assert timeouts == [serving_audit.AUDIT_TIMEOUT_SECONDS] * 2
    assert "did not finish" in capsys.readouterr().err
