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

`--data-gate-policy` is the second kind: a sealed path the orchestrator resolves
against the *caller's* cwd rather than passing through, so its correct default is
no default at all. It is here because it is the flag that proved this suite's
derivation too narrow — see `SEALED_PATH_FLAG_SUFFIXES`.
"""

import argparse
import importlib.util
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# Every orchestrator flag whose value is read as a file this run seals by digest,
# minus the declared `--models-config` exception above.
# `test_no_flag_naming_a_sealed_path_escapes_this_suite` fails when a new flag
# naming a sealed path is added to the orchestrator without a decision here:
# cover it or declare it.
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

# Flags deliberately not required to resolve from every working directory, each
# with its reason on record (module docstring for `--models-config`).
DECLARED_RELATIVE_CONFIG_FLAGS = ("--models-config",)

# Sealed-config paths with no string default at all, because the orchestrator
# resolves them against the *caller's* cwd and a relative default resolved that
# way would name a file beside the caller. `None` means "the repository's own",
# filled in by `resolve_caller_paths`, and is checked there instead of here.
RESOLVED_AT_BOUNDARY_FLAGS = ("--data-gate-policy",)

# The suffixes that mark an orchestrator flag as naming a file the run seals by
# digest. `-policy` is here because of what it cost: `--data-gate-policy` seals
# as `sealed_config_digests["data-handling"]`, is exactly the family this suite
# guards, and shipped with a bare relative default that broke every real run
# started outside the repository root. This suite did not catch it — it derived
# its ground truth from the `-config` suffix alone, so a sealed path named for
# its subject rather than for its file type walked straight past.
SEALED_PATH_FLAG_SUFFIXES = ("-config", "-policy")


def _orchestrator_module():
    spec = importlib.util.spec_from_file_location(
        "orchestrator_config_defaults", ROOT / "pipeline" / "orchestrator" / "run.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _orchestrator_defaults() -> dict[str, object]:
    """Read the orchestrator parser's own defaults without running a pipeline.

    `main()` builds its parser and parses in one breath, so the parser is not
    reachable any other way. Intercepting `parse_args` takes the defaults off the
    real, fully constructed parser rather than off a second copy in this file
    that could drift from it.

    **Every option, whatever its default's type.** Filtering to string defaults
    here is what hid `--data-gate-policy` from `test_no_flag_naming_a_sealed_path
    _escapes_this_suite`: a flag with `default=None` was invisible to the check
    that decides which flags need a decision, so a sealed path could opt out of
    this suite by carrying no string default — which is also the shape a flag has
    while it is being fixed. The string filter belongs to the flags that are
    checked, below, not to the derivation of which flags exist.
    """
    module = _orchestrator_module()
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


def _orchestrator_string_defaults() -> dict[str, str]:
    """The subset with a string or path default, which is what resolves or does not."""
    return {
        flag: str(default)
        for flag, default in _orchestrator_defaults().items()
        if isinstance(default, (str, os.PathLike))
    }


def test_no_flag_naming_a_sealed_path_escapes_this_suite():
    """The tuples above are hand-maintained; this derives the ground truth from
    the real parser so a new flag naming a file the run seals cannot land silently
    uncovered -- it must join the sealed tuple, be declared a deliberate relative
    exception with its reason, or be declared resolved at the orchestration
    boundary."""
    flags = set(_orchestrator_defaults())
    sealed_path_flags = {flag for flag in flags if flag.endswith(SEALED_PATH_FLAG_SUFFIXES)}
    uncovered = sorted(
        sealed_path_flags
        - set(SEALED_CONFIG_FLAGS)
        - set(DECLARED_RELATIVE_CONFIG_FLAGS)
        - set(RESOLVED_AT_BOUNDARY_FLAGS)
    )
    assert not uncovered, (
        f"the orchestrator declares flag(s) {uncovered} naming a sealed path that this suite "
        "neither checks nor declares as a deliberate exception; a relative default there "
        "would break every run that does not start at the repository root"
    )


@pytest.mark.parametrize("flag", RESOLVED_AT_BOUNDARY_FLAGS)
def test_every_boundary_resolved_flag_has_no_relative_default_to_resolve_late(flag):
    """Declared `None`, and made absolute by `resolve_caller_paths`, from any cwd.

    The two halves have to hold together: a string default here would be resolved
    against the caller's cwd by that same function, which is the defect; and a
    `None` that nothing filled in would reach the Door as the literal `"None"`.
    """
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
    defaults = _orchestrator_string_defaults()
    assert flag in defaults, f"{flag} is no longer an orchestrator flag with a string default"
    monkeypatch.chdir(tmp_path)
    assert Path(defaults[flag]).is_file(), (
        f"{flag} defaults to {defaults[flag]!r}, which does not resolve outside the repository "
        "root; the orchestrator passes this value on to every stage it invokes"
    )
