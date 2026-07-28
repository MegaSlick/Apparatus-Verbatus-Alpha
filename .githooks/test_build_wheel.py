from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("build_wheel.py")
SPEC = importlib.util.spec_from_file_location("build_wheel", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
build_wheel = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_wheel)


def write_environment(root: Path, *, backend: str, development: str) -> None:
    (root / "pyproject.toml").write_text(
        f'[build-system]\nrequires = ["{backend}"]\nbuild-backend = "setuptools.build_meta"\n',
        encoding="utf-8",
    )
    (root / "requirements-dev.txt").write_text(development, encoding="utf-8")


def test_build_backend_must_be_declared_in_development_environment(
    tmp_path: Path,
) -> None:
    write_environment(tmp_path, backend="setuptools==83.0.0", development="pytest==9.1.1\n")

    with pytest.raises(RuntimeError, match="same exact pin"):
        build_wheel.verify_build_environment(tmp_path)


def test_build_backend_must_be_installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_environment(
        tmp_path,
        backend="setuptools==83.0.0",
        development="setuptools==83.0.0\n",
    )

    def missing(_name: str) -> str:
        raise build_wheel.metadata.PackageNotFoundError

    monkeypatch.setattr(build_wheel.metadata, "version", missing)
    with pytest.raises(RuntimeError, match="is not installed"):
        build_wheel.verify_build_environment(tmp_path)


def test_installed_build_backend_must_match_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_environment(
        tmp_path,
        backend="setuptools==83.0.0",
        development="setuptools==83.0.0\n",
    )
    monkeypatch.setattr(build_wheel.metadata, "version", lambda _name: "82.0.0")

    with pytest.raises(RuntimeError, match="is 82.0.0, expected 83.0.0"):
        build_wheel.verify_build_environment(tmp_path)


def test_matching_build_backend_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_environment(
        tmp_path,
        backend="setuptools==83.0.0",
        development="# tools\nsetuptools==83.0.0  # backend\n",
    )
    monkeypatch.setattr(build_wheel.metadata, "version", lambda _name: "83.0.0")

    build_wheel.verify_build_environment(tmp_path)


def test_wheel_build_is_explicitly_offline(tmp_path: Path) -> None:
    command = build_wheel.wheel_command(tmp_path / "source", tmp_path / "wheels")

    assert "--no-build-isolation" in command
    assert "--no-index" in command
    assert "--disable-pip-version-check" in command


def test_entrypoint_reports_failure_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail() -> int:
        raise RuntimeError("backend missing")

    monkeypatch.setattr(build_wheel, "main", fail)

    assert build_wheel.entrypoint() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Wheel check failed: backend missing\n"
