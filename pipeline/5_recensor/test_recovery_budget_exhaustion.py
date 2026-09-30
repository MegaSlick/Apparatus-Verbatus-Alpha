"""A spent fallback budget holds the act without borrowing another operation's allowance."""

from pathlib import Path

import pytest

from common.contracts.stages import RECENSOR
from common.runtree.store import RunTree
from conftest import programs_through, run_stage


@pytest.mark.act_path
@pytest.mark.parametrize(
    ("page_level_allowance", "reason"),
    [
        (0, "budget of 0"),
        (1, "page-level reread is not a substitute"),
    ],
)
def test_no_fallback_capacity_holds_every_act_without_a_recovery_request(
    tmp_path: Path, page_level_allowance: int, reason: str
) -> None:
    root = tmp_path / "runs"
    run_id = f"page-level-{page_level_allowance}"
    recovery_config = tmp_path / "recovery.toml"
    recovery_config.write_text(
        "absolute_cap = 3\n[budget]\nfallback_recrop = 0\n"
        f"page_level_reread = {page_level_allowance}\n",
        encoding="utf-8",
    )
    for program in programs_through("perlector"):
        result = run_stage(root, run_id, "review", program, recovery_config=recovery_config)
        assert result.returncode == 0, f"{program}: {result.stderr}"

    result = run_stage(
        root, run_id, "review", "pipeline/5_recensor/run.py", recovery_config=recovery_config
    )
    assert result.returncode == 3, result.stderr

    tree = RunTree(root, run_id)
    records = [
        tree.read_artifact(RECENSOR, entry["kind"], entry["artifact_id"])
        for entry in tree.build_manifest(RECENSOR)["artifacts"]
        if entry["kind"] in {"review", "recovery-request"}
    ]
    assert not [record for record in records if record["kind"] == "recovery-request"]
    reviews = {record["payload"]["act_key"]: record for record in records}
    assert set(reviews) == {"a1", "a2"}
    assert all(record["outcome"] == "held-for-review" for record in reviews.values())
    assert reason in reviews["a1"]["payload"]["reason"]
