"""A run's decoding policy is sealed at creation and named if it moves.

`config/decoding.toml` is every reading chair's sampling values, the
Perlector's whole-page output cap and Chandra's native recipe. It joins the sealing family: its
exact bytes are digested into `config_digest`, filed under `decoding` in
`sealed_config_digests`, and re-read at each point of use.

What a later reader recovers from a run tree is therefore the digest of the
policy bytes that governed it -- the same guarantee every other member of the
family gives, and the reason a run cannot be resumed under a different posture
without being told which policy moved.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from common.decoding import DEFAULT_DECODING_CONFIG_PATH, load_decoding_policy
from common.runtree.store import RunTree
from common.sealed_config import read_sealed_toml
from conftest import run_stage, run_through, tree_snapshot

CONSUMING_STAGES = ("pipeline/3_attestatores/run.py", "pipeline/4_perlector/run.py")


def invoke_stage(run_root: Path, program: str, **extra):
    return run_stage(run_root, "decoding", "happy", program, **extra)


def _through_designator(tmp_path: Path) -> tuple[Path, RunTree]:
    run_root = tmp_path / "runs"
    run_through(run_root, "decoding", "happy", "designator")
    return run_root, RunTree(run_root, "decoding")


def test_a_run_seals_the_exact_decoding_bytes_it_was_created_under(tmp_path):
    """The chain, end to end: file bytes -> digest -> this run's own record."""
    _run_root, tree = _through_designator(tmp_path)
    run = tree.read_run()

    _policy, digest = load_decoding_policy()
    assert digest == read_sealed_toml(DEFAULT_DECODING_CONFIG_PATH, "decoding")[1]
    # Filed under the name its points of use ask for, and inside the digest of
    # everything that shapes the run -- so a candidate policy file can be proved
    # against the tree without trusting its filename or parsed values.
    assert run["sealed_config_digests"]["decoding"] == digest
    assert run["config_digest"] != digest


@pytest.mark.parametrize(
    ("change", "what"),
    [
        pytest.param("malformed", "is not valid TOML", id="not-toml"),
        pytest.param(
            "sampling",
            "decoding chair_decoding.perlector has unknown field(s) ['do_sample']",
            id="unknown-sampling-field",
        ),
        pytest.param(
            "earlier",
            "unsupported schema",
            id="earlier-schema",
        ),
    ],
)
def test_a_run_refused_for_its_decoding_policy_creates_nothing(tmp_path, change: str, what: str):
    """The refusal at run creation is measured against the run root, not read.

    `common.decoding` tells the operator that "No run or stage artifact was
    written" and offers to let them correct the file and retry. That advice is
    only safe if it is true: a half-created run would leave a `run.json` sealing
    a policy the loader had already rejected, and the retry would then collide
    with it rather than proceed. The unit tests beside this one prove the loader
    refuses; this proves the Door refuses in the same breath, before it writes.
    """
    substitute = tmp_path / "decoding.toml"
    source = DEFAULT_DECODING_CONFIG_PATH.read_text(encoding="utf-8")
    body = {
        "malformed": "temperature = ",
        "sampling": source.replace(
            "[chair_decoding.perlector]\n", "[chair_decoding.perlector]\ndo_sample = true\n", 1
        ),
        "earlier": source.replace('schema = "decoding.v10"', 'schema = "decoding.v3"', 1),
    }[change]
    substitute.write_text(body, encoding="utf-8")
    run_root = tmp_path / "runs"

    refused = invoke_stage(run_root, "pipeline/1_exemplar/door.py", decoding_config=substitute)

    assert refused.returncode != 0, what
    # The named cause, not merely a refusal: an unparseable file and a policy
    # that parses but declares a field this build will not read are two
    # different operator problems, and the message has to say which one it is.
    assert what in refused.stderr, refused.stderr
    assert "No run or stage artifact was written" in refused.stderr, refused.stderr
    # The run root must not exist at all: an empty one is a run id claimed under
    # a rejected policy, and the retry the message invites would collide with
    # it. `lexists`, so a dangling link counts.
    assert not os.path.lexists(run_root), tree_snapshot(run_root)


@pytest.mark.parametrize("program", CONSUMING_STAGES)
def test_a_stage_refuses_a_run_resumed_under_a_different_decoding_policy(tmp_path, program: str):
    """Refused, and refused *by name*: the message says `decoding` moved.

    A moved page cap leaves a valid policy; the substitution is what is refused.

    Naming the policy matters as much as refusing it. "different config_digest,
    sealed_config_digests" is true whichever of the ten sealed files moved, and
    it sends an operator to read all of them.
    """
    run_root, _tree = _through_designator(tmp_path)
    substitute = tmp_path / "decoding.toml"
    source = DEFAULT_DECODING_CONFIG_PATH.read_text(encoding="utf-8")
    body = source.replace("page_max_tokens = 12288", "page_max_tokens = 12287", 1)
    substitute.write_text(body, encoding="utf-8")
    assert load_decoding_policy(substitute)[1] != load_decoding_policy()[1]

    before = tree_snapshot(run_root)
    refused = invoke_stage(run_root, program, decoding_config=substitute)

    assert refused.returncode != 0
    assert "sealed configuration decoding moved" in refused.stderr, refused.stderr
    assert "No stage work was written" in refused.stderr, refused.stderr
    assert "Resume with the original sealed inputs" in refused.stderr, refused.stderr
    # The sentence above is a claim about the run tree, so it is compared with
    # the run tree rather than believed.
    assert tree_snapshot(run_root) == before, "the refusal wrote to the run tree it disowned"


def test_a_stage_reading_the_run_s_own_decoding_policy_proceeds(tmp_path):
    """Each consuming stage handed the file the run was created under runs."""
    run_root, _tree = _through_designator(tmp_path)
    for stage in CONSUMING_STAGES:
        result = invoke_stage(run_root, stage, decoding_config=DEFAULT_DECODING_CONFIG_PATH)
        assert result.returncode == 0, f"{stage}: {result.stderr}"
