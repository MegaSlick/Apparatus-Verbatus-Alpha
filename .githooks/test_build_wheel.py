from __future__ import annotations

import importlib.util
import subprocess
import zipfile
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


def run_main_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, root: Path | None = None
) -> tuple[list[list[str]], list[str]]:
    """Drive main() end to end, recording every command it actually ran.

    The offline flags above only bind if main() runs the command that carries
    them; a caller rewritten to shell out to pip directly would leave that test
    green on dead code.
    """
    repository = root if root is not None else tmp_path / "repo"
    repository.mkdir(parents=True, exist_ok=True)
    write_environment(repository, backend="setuptools==83.0.0", development="setuptools==83.0.0\n")
    monkeypatch.setattr(build_wheel.metadata, "version", lambda _name: "83.0.0")

    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        commands.append(list(command))
        if command[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(command, 0, stdout=f"{repository}\n")
        if command[:2] == ["git", "ls-files"]:
            assert Path(kwargs["cwd"]) == repository, (
                f"listed {kwargs['cwd']!r}, not the repository root"
            )
            return subprocess.CompletedProcess(command, 0, stdout=b"pyproject.toml\0")
        # The staged copy lives in a temporary directory main() removes on the
        # way out, so what it packaged has to be observed here, mid-build.
        source = Path(command[command.index("wheel") + 1])
        packaged.extend(str(path.relative_to(source)) for path in sorted(source.rglob("*")))
        wheels = Path(command[command.index("--wheel-dir") + 1])
        with zipfile.ZipFile(wheels / "verbatus-0.0.0-py3-none-any.whl", "w") as archive:
            archive.writestr("common/__init__.py", "")
        return subprocess.CompletedProcess(command, 0)

    packaged: list[str] = []
    monkeypatch.setattr(build_wheel.subprocess, "run", fake_run)
    assert build_wheel.main() == 0
    return commands, packaged


def test_the_build_main_runs_actually_uses_the_offline_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    commands, _ = run_main_offline(tmp_path, monkeypatch)
    capsys.readouterr()

    builds = [command for command in commands if "wheel" in command]
    assert len(builds) == 1, f"expected exactly one build invocation, got {builds}"
    build = builds[0]
    source = Path(build[build.index("wheel") + 1])
    wheels = Path(build[build.index("--wheel-dir") + 1])
    assert build == build_wheel.wheel_command(source, wheels), (
        "the wheel was built by a command other than wheel_command's"
    )
    assert "--no-index" in build


def test_a_repository_path_with_trailing_space_is_not_trimmed_away(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`git rev-parse` terminates its answer with a newline, not with whitespace."""
    root = tmp_path / "repo dir "
    commands, packaged = run_main_offline(tmp_path, monkeypatch, root=root)
    capsys.readouterr()

    assert [command for command in commands if command[:2] == ["git", "ls-files"]], (
        "main() never listed the repository"
    )
    assert packaged == ["pyproject.toml"], (
        f"the packaged copy is {packaged}: the repository root was mis-parsed"
    )


def test_entrypoint_reports_an_unexpected_failure_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail() -> int:
        raise FileNotFoundError(2, "No such file or directory", "pyproject.toml")

    monkeypatch.setattr(build_wheel, "main", fail)

    assert build_wheel.entrypoint() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("Wheel check failed: FileNotFoundError:")
    assert "Traceback" not in captured.err


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
