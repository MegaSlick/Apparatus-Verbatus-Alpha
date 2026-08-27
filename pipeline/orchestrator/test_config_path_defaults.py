"""Sealed-path defaults must work outside the repository's cwd.

`--models-config` deliberately matches `stage_parser`'s relative default.
`--data-gate-policy` has no parser default because real ingress binds it at the
caller's boundary; fixture ingress must leave it absent.
"""

import argparse
import importlib.util
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# Every listed value is sealed by digest; exceptions are classified below.
SEALED_CONFIG_FLAGS = (
    "--decoding-config",
    "--pdf-render-config",
    "--designator-padding-config",
    "--designator-geometry-config",
    "--alignment-config",
    "--perlector-protocol-config",
    "--perlector-audit-config",
    "--serving-recipes-config",
    "--formats-config",
    "--recovery-config",
    "--hard-failure-config",
    "--witness-context-config",
)

# This exception is constrained by `stage_parser`'s matching default.
DECLARED_RELATIVE_CONFIG_FLAGS = ("--models-config",)

# These paths bind at the caller boundary and therefore cannot have a relative default.
RESOLVED_AT_BOUNDARY_FLAGS = ("--data-gate-policy",)

# Both suffixes name files whose bytes enter the run's sealed config digests.
SEALED_PATH_FLAG_SUFFIXES = ("-config", "-policy")


def _orchestrator_defaults() -> dict[str, object]:
    """Capture every default from `main`'s otherwise inaccessible parser."""
    spec = importlib.util.spec_from_file_location(
        "orchestrator_config_defaults", ROOT / "pipeline" / "orchestrator" / "run.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    captured: dict[str, object] = {}
    real_parse_args = argparse.ArgumentParser.parse_args

    def capture(self, *_args, **_kwargs):
        captured.update(
            {
                action.option_strings[0]: action.default
                for action in self._actions
                if action.option_strings
            }
        )
        raise SystemExit(0)

    argparse.ArgumentParser.parse_args = capture  # type: ignore[method-assign]
    try:
        with pytest.raises(SystemExit):
            module.main()
    finally:
        argparse.ArgumentParser.parse_args = real_parse_args  # type: ignore[method-assign]
    return captured


def test_no_config_or_policy_flag_escapes_this_suite():
    """Every sealed-path flag must be checked or explicitly classified."""
    flags = set(_orchestrator_defaults())
    sealed_path_flags = {flag for flag in flags if flag.endswith(SEALED_PATH_FLAG_SUFFIXES)}
    uncovered = sorted(
        sealed_path_flags
        - set(SEALED_CONFIG_FLAGS)
        - set(DECLARED_RELATIVE_CONFIG_FLAGS)
        - set(RESOLVED_AT_BOUNDARY_FLAGS)
    )
    assert not uncovered, (
        f"the orchestrator declares config/policy flag(s) {uncovered} that this suite "
        "neither checks nor declares as a deliberate exception; a relative default there "
        "would break every run that does not start at the repository root"
    )


@pytest.mark.parametrize("flag", RESOLVED_AT_BOUNDARY_FLAGS)
def test_every_boundary_resolved_flag_declares_no_relative_parser_default(flag):
    """A string default here would be resolved against the caller's cwd."""
    defaults = _orchestrator_defaults()
    assert flag in defaults, f"{flag} is no longer an orchestrator flag"
    assert defaults[flag] is None, (
        f"{flag} declares the default {defaults[flag]!r}; a value here is resolved against "
        "the caller's cwd and would name a file beside the caller, not the repository's own"
    )


@pytest.mark.parametrize("flag", SEALED_CONFIG_FLAGS)
def test_every_sealed_config_default_resolves_from_any_working_directory(
    flag, tmp_path, monkeypatch
):
    defaults = {
        option: str(default)
        for option, default in _orchestrator_defaults().items()
        if isinstance(default, (str, os.PathLike))
    }
    assert flag in defaults, f"{flag} is no longer an orchestrator flag with a string default"
    monkeypatch.chdir(tmp_path)
    assert Path(defaults[flag]).is_file(), (
        f"{flag} defaults to {defaults[flag]!r}, which does not resolve outside the repository "
        "root; the orchestrator passes this value on to every stage it invokes"
    )
