"""The `--controller-armer-factory` seam, and what it will and will not accept.

`cli.main`'s gate, preview and exit statuses are drilled in
`test_pod_runtime.py`, which owns the whole launch. This file covers one
boundary: the untracked factory that supplies the two-controller handshake.
Until this branch there was no tracked implementation that could pass through
it -- `arming.FailClosedControllerArmer` refuses every launch by design -- so
"the real armer satisfies the seam it is loaded through" was a claim nothing
checked.

The factories below are module-level on purpose: `cli._controller_armer`
imports a module by name and calls an attribute of it, which is exactly what an
operator's untracked module does.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from . import cli
from .arming import FailClosedControllerArmer
from .controller_armer import ChannelControllerArmer, ObservingControllerArmer
from .models import utc_now


class _SilentChannel:
    """Answers every read with a proven absence; nothing here reads anything."""

    def read(self, key: str) -> bytes | None:
        del key
        return None


_SUPERVISOR_ARGV = ("python", "-m", "operations.pod.supervise", "--spend", "config/spend.toml")


def channel_armer() -> ChannelControllerArmer:
    return ChannelControllerArmer(channel=_SilentChannel(), supervisor_argv=_SUPERVISOR_ARGV)


def observing_armer() -> ObservingControllerArmer:
    return ObservingControllerArmer(
        evidence_root=Path("workbench/raw"),
        channel=_SilentChannel(),
        supervisor_argv=_SUPERVISOR_ARGV,
    )


def fail_closed_armer() -> FailClosedControllerArmer:
    return FailClosedControllerArmer(now=utc_now)


def not_an_armer() -> object:
    return object()


class _HalfAnArmer:
    def preflight(self, *, action, request, policy):  # type: ignore[no-untyped-def]
        raise AssertionError("never called")


def half_an_armer() -> _HalfAnArmer:
    return _HalfAnArmer()


@pytest.mark.parametrize(
    ("factory", "expected"),
    [
        ("operations.pod.test_cli:channel_armer", ChannelControllerArmer),
        ("operations.pod.test_cli:observing_armer", ObservingControllerArmer),
        ("operations.pod.test_cli:fail_closed_armer", FailClosedControllerArmer),
    ],
)
def test_the_shipped_armers_all_load_through_the_launcher_seam(
    factory: str, expected: type
) -> None:
    assert isinstance(cli._controller_armer(factory), expected)


@pytest.mark.parametrize(
    "factory", ["operations.pod.test_cli:not_an_armer", "operations.pod.test_cli:half_an_armer"]
)
def test_an_object_that_cannot_arm_is_refused_at_load(factory: str) -> None:
    with pytest.raises(TypeError, match="two-controller arming seam"):
        cli._controller_armer(factory)


@pytest.mark.parametrize("reference", ["no-colon", "too:many:colons"])
def test_a_factory_reference_of_the_wrong_shape_is_refused(reference: str) -> None:
    with pytest.raises(ValueError, match="module:callable"):
        cli._controller_armer(reference)


def test_the_help_text_names_both_shipped_armers(capsys: pytest.CaptureFixture[str]) -> None:
    """The drill exists to be found: an armer that cannot leave a pod running
    is only useful to an operator who knows it is there."""

    with pytest.raises(SystemExit) as exit_status:
        cli.main(["--help"])

    assert exit_status.value.code == 0
    printed = capsys.readouterr().out
    assert "ChannelControllerArmer" in printed
    assert "ObservingControllerArmer" in printed


@pytest.mark.parametrize("command", ["create", "adopt"])
def test_a_missing_armer_factory_refuses_in_this_surface_s_own_record(
    command: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The refusal is a JSON record on stdout and exit 2, not argparse usage.

    `--controller-armer-factory` became optional when `close` landed, and
    `create` and `adopt` check for it themselves. Reporting that through
    `parser.error` would have exited 2 as well -- but with usage text on
    stderr, in a shape nothing that reads this command's records can parse.
    Every other refusal here is `{"state": "refused", "green": false,
    "detail": ...}` on stdout, and a refusal only argparse can explain is a
    refusal half lost (GOVERNANCE 2).

    Nothing is loaded before the check: `--provider-factory` below names a
    module that does not exist, and the refusal is still this one.
    """

    argv = [
        "--provider-factory",
        "no.such.module:factory",
        "--leases",
        str(tmp_path / "leases"),
        "--provider-name",
        "fake",
        command,
    ]
    argv += ["--request", str(tmp_path / "request.json")]
    if command == "adopt":
        argv += ["--pod-id", "fake-pod-1"]

    exit_code = cli.main(argv)

    assert exit_code == 2
    printed = json.loads(capsys.readouterr().out)
    assert printed["state"] == "refused"
    assert printed["green"] is False
    assert "--controller-armer-factory" in printed["detail"]
    assert "no paid action occurred" in printed["detail"]
    assert not (tmp_path / "leases").exists()
