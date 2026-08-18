"""The orchestrator's sealed-config defaults may not depend on the caller's cwd.

The orchestrator invokes every stage as a real subprocess and passes *its own*
value for each sealed config path on the child's argv. Its default therefore
overrides the absolute default `common.stage.stage_parser` already gives the
stage: a relative default here does not merely fail for the orchestrator, it
replaces a working absolute path with one that resolves only when the run
happens to start at the repository root. `run_config_bindings` seals each of
these files by digest, so the whole run then refuses for a reason that has
nothing to do with the corpus.

`--models-config` is the one deliberate exception, and it is not this file's to
change: `stage_parser` itself declares `config/models.toml` relative, so the
orchestrator agrees with the stage rather than overriding it. Fixing that means
moving the models-config surface, which is not an orchestrator concern.
"""

import argparse
import importlib.util
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# Every orchestrator flag whose value is read as a file this run seals by digest,
# minus the declared `--models-config` exception above.
# `test_no_config_flag_escapes_this_suite` fails when a new `-config` flag is
# added to the orchestrator without a decision here: cover it or declare it.
SEALED_CONFIG_FLAGS = (
    "--pdf-render-config",
    "--designator-padding-config",
    "--designator-geometry-config",
    "--alignment-config",
    "--perlector-protocol-config",
    "--formats-config",
    "--recovery-config",
    "--hard-failure-config",
    "--witness-context-config",
)

# Flags deliberately not required to resolve from every working directory, each
# with its reason on record (module docstring for `--models-config`).
DECLARED_RELATIVE_CONFIG_FLAGS = ("--models-config",)


def _orchestrator_string_defaults() -> dict[str, str]:
    """Read the orchestrator parser's own defaults without running a pipeline.

    `main()` builds its parser and parses in one breath, so the parser is not
    reachable any other way. Intercepting `parse_args` takes the defaults off the
    real, fully constructed parser rather than off a second copy in this file
    that could drift from it.
    """
    spec = importlib.util.spec_from_file_location(
        "orchestrator_config_defaults", ROOT / "pipeline" / "orchestrator" / "run.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    captured: dict[str, str] = {}
    real_parse_args = argparse.ArgumentParser.parse_args

    def capture(self, *_args, **_kwargs):
        captured.update(
            {
                action.option_strings[0]: str(action.default)
                for action in self._actions
                if action.option_strings and isinstance(action.default, (str, os.PathLike))
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


def test_no_config_flag_escapes_this_suite():
    """SEALED_CONFIG_FLAGS is hand-maintained; this derives the ground truth from
    the real parser so a new `-config` flag with a relative default cannot land
    silently uncovered -- it must either join the sealed tuple or be declared a
    deliberate exception with its reason."""
    defaults = _orchestrator_string_defaults()
    config_flags = {flag for flag in defaults if flag.endswith("-config")}
    uncovered = sorted(
        config_flags - set(SEALED_CONFIG_FLAGS) - set(DECLARED_RELATIVE_CONFIG_FLAGS)
    )
    assert not uncovered, (
        f"the orchestrator declares config flag(s) {uncovered} that this suite neither "
        "checks nor declares as a deliberate exception; a relative default there would "
        "break every run that does not start at the repository root"
    )


@pytest.mark.parametrize("flag", SEALED_CONFIG_FLAGS)
def test_every_sealed_config_default_resolves_from_any_working_directory(
    flag, tmp_path, monkeypatch
):
    defaults = _orchestrator_string_defaults()
    assert flag in defaults, f"{flag} is no longer an orchestrator flag with a string default"
    monkeypatch.chdir(tmp_path)
    assert Path(defaults[flag]).is_file(), (
        f"{flag} defaults to {defaults[flag]!r}, which does not resolve outside the repository "
        "root; the orchestrator passes this value on to every stage it invokes"
    )
