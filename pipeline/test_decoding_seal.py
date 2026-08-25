"""Unit 2: a run's decoding posture is sealed at creation and named if it moves.

`config/decoding.toml` is the reading-of-record's temperature and the labelled
variance experiment's seed and pass count. It joins the sealing family: its
exact bytes are digested into `config_digest`, filed under `decoding` in
`sealed_config_digests`, and re-read at each point of use.

What a later reader recovers from a run tree is therefore the digest of the
policy bytes that governed it -- the same guarantee every other member of the
family gives, and the reason a run cannot be resumed under a different posture
without being told which policy moved.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from common.contracts.canonical import digest_bytes
from common.decoding import DEFAULT_DECODING_CONFIG_PATH, load_decoding_policy
from common.runtree.store import RunTree

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "proof"

CONSUMING_STAGES = ("pipeline/3_attestatores/run.py", "pipeline/4_perlector/run.py")


def invoke_stage(run_root: Path, program: str, **extra) -> subprocess.CompletedProcess:
    command = [
        sys.executable,
        str(ROOT / program),
        "--run-root",
        str(run_root),
        "--run-id",
        "decoding",
        "--scenario",
        "happy",
        "--fixture-root",
        str(FIXTURE_ROOT),
    ]
    for key, value in extra.items():
        command.extend((f"--{key.replace('_', '-')}", str(value)))
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True)


def _through_designator(tmp_path: Path) -> tuple[Path, RunTree]:
    run_root = tmp_path / "runs"
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/1_ink_map/run.py",
        "pipeline/2_designator/run.py",
    ):
        result = invoke_stage(run_root, program)
        assert result.returncode == 0, f"{program}: {result.stderr}"
    return run_root, RunTree(run_root, "decoding")


def test_a_run_seals_the_exact_decoding_bytes_it_was_created_under(tmp_path):
    """The chain, end to end: file bytes -> digest -> this run's own record."""
    _run_root, tree = _through_designator(tmp_path)
    run = tree.read_run()

    _policy, digest = load_decoding_policy()
    assert digest == digest_bytes(DEFAULT_DECODING_CONFIG_PATH.read_bytes())
    # Filed under the name its points of use ask for, and inside the digest of
    # everything that shapes the run -- so the bytes are provable from the tree
    # alone, without the file that produced them still being on disk.
    assert run["sealed_config_digests"]["decoding"] == digest
    assert run["config_digest"] != digest


@pytest.mark.parametrize(
    ("body", "what"),
    [
        pytest.param(
            '# a differently worded comment\nschema = "decoding.v1"\n\n'
            "[reading_of_record]\ntemperature = 0\n\n"
            '[variance_experiment]\nlabel = "variance.v1"\nseed = 20260820\npasses = 2\n',
            "comment-only",
            id="comment-only",
        ),
        pytest.param(
            'schema = "decoding.v1"\n\n[reading_of_record]\ntemperature = 0\n\n'
            '[variance_experiment]\nlabel = "variance.v1"\nseed = 20260821\npasses = 2\n',
            "a moved variance seed",
            id="moved-seed",
        ),
    ],
)
@pytest.mark.parametrize("program", CONSUMING_STAGES)
def test_a_stage_refuses_a_run_resumed_under_a_different_decoding_policy(
    tmp_path, body: str, what: str, program: str
):
    """Refused, and refused *by name*: the message says `decoding` moved.

    Both variants are policies this build accepts on their own -- temperature
    stays 0 and the experiment stays two passes -- so what is being refused is
    the substitution, not an invalid file. The comment-only case is the sharper
    one: the seal is over bytes, and a run may not continue under a file that
    reads the same to a person and hashes differently.

    Naming the policy matters as much as refusing it. "different config_digest,
    sealed_config_digests" is true whichever of the ten sealed files moved, and
    it sends an operator to read all of them.
    """
    run_root, _tree = _through_designator(tmp_path)
    substitute = tmp_path / "decoding.toml"
    substitute.write_text(body, encoding="utf-8")
    assert load_decoding_policy(substitute)[1] != load_decoding_policy()[1], what

    refused = invoke_stage(run_root, program, decoding_config=substitute)

    assert refused.returncode != 0
    assert "sealed configuration decoding moved" in refused.stderr, refused.stderr


@pytest.mark.parametrize("program", CONSUMING_STAGES)
def test_a_stage_reading_the_run_s_own_decoding_policy_proceeds(tmp_path, program):
    """The other half of the same check: the sealed bytes are not merely refused
    at every value. A stage handed the file the run was created under runs."""
    run_root, _tree = _through_designator(tmp_path)
    for stage in CONSUMING_STAGES:
        result = invoke_stage(run_root, stage, decoding_config=DEFAULT_DECODING_CONFIG_PATH)
        assert result.returncode == 0, f"{stage}: {result.stderr}"
        if stage == program:
            break
