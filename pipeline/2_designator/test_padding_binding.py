"""The padding policy is part of the run's sealed configuration, not a loose file.

Capture padding decides how many pixels of page a witness is actually shown
around each act, so two runs under different padding cut *different crop bytes*.
Until this build the policy was read from a fixed path and never entered
`run.json`'s `config_digest`, which meant one run id could be reused across a
padding change and quietly hold two geometries under one name. The stage's own
`config/README.md` said so plainly; this file is the check that it no longer can.

The lane-B build of this stage reached the same conclusion about its own
Designator configuration and sealed it the same way. What is bound here is the
padding policy specifically, because that is the file this build's crops
actually depend on.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from common.chairs.registry import ChairRegistry
from common.contracts.errors import ContractError
from common.stage import EXIT_FATAL, load_fixture, run_config_bindings

ROOT = Path(__file__).resolve().parents[2]
SHIPPED_PADDING = ROOT / "config" / "designator_padding.toml"


def _bindings(padding_path):
    registry = ChairRegistry.from_toml(str(ROOT / "config" / "models.toml"))
    fixture = load_fixture(str(ROOT / "proof"))
    return run_config_bindings(
        registry.config,
        fixture,
        "happy",
        designator_padding_config_path=padding_path,
    )


def _widened(tmp_path: Path) -> Path:
    """The shipped policy with one edge widened, and nothing else touched."""
    text = SHIPPED_PADDING.read_text(encoding="utf-8").replace("left_bp = 500", "left_bp = 1000")
    assert "left_bp = 1000" in text, "the shipped padding config no longer declares left_bp = 500"
    path = tmp_path / "widened_padding.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_a_different_padding_policy_is_a_different_run_configuration(tmp_path):
    assert (
        _bindings(SHIPPED_PADDING)["config_digest"]
        != _bindings(_widened(tmp_path))["config_digest"]
    )


def test_an_unreadable_padding_policy_is_refused_rather_than_defaulted(tmp_path):
    """A padding file nobody can read is not a run with no padding."""
    with pytest.raises(ContractError, match="padding configuration binding"):
        _bindings(tmp_path / "absent.toml")


def _invoke(program: str, root: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / program),
            "--run-root",
            str(root),
            "--run-id",
            "r",
            "--scenario",
            "happy",
            *extra,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def test_reusing_a_run_id_under_changed_padding_is_refused_before_a_crop_is_cut(tmp_path):
    """The whole point: the second geometry never reaches the tree at all.

    A crop cut under widened padding is a different rectangle of the same page.
    Publishing one into a run sealed under the old policy would leave two
    geometries in one run with nothing recording that they differ — and the
    region payload's own `padding` block would not help, because a reader
    comparing two crops has no reason to suspect the policy moved between them.
    """
    root = tmp_path / "runs"
    for program in ("pipeline/1_exemplar/door.py", "pipeline/1_exemplar/run.py"):
        result = _invoke(program, root)
        assert result.returncode == 0, f"{program}: {result.stderr}"

    result = _invoke(
        "pipeline/2_designator/run.py",
        root,
        "--designator-padding-config",
        str(_widened(tmp_path)),
    )
    assert result.returncode == EXIT_FATAL, result.stdout
    assert "config_digest" in result.stderr
    assert not (root / "r" / "2_designator" / "artifacts").exists(), (
        "the refusal must land before the first region write, not after it"
    )
