"""The named/blinded toggle, driven end to end through the real orchestrator
rather than only unit-tested against `common/page_feed.py` directly.
"""

import subprocess
import sys
from pathlib import Path

from common.contracts.canonical import canonical_text
from common.contracts.stages import PERLECTOR
from common.runtree.store import RunTree
from common.witness_regime import pseudonym_for

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"


def orchestrate(run_root: Path, run_id: str, scenario: str, *, witness_context: str = "named"):
    command = [
        sys.executable,
        str(ORCHESTRATOR),
        "--fixture",
        "synthetic-two-page-v0",
        "--scenario",
        scenario,
        "--run-id",
        run_id,
        "--run-root",
        str(run_root),
        "--witness-context",
        witness_context,
    ]
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True)


def _records(tree: RunTree, kind: str) -> list[dict]:
    return [
        tree.read_artifact(PERLECTOR, kind, entry["artifact_id"])
        for entry in tree.build_manifest(PERLECTOR)["artifacts"]
        if entry["kind"] == kind
    ]


def test_a_blinded_run_completes_and_seals_the_regime_on_every_page_record(tmp_path):
    root = tmp_path / "runs"
    result = orchestrate(root, "r", "page-unbroken", witness_context="blinded")
    assert result.returncode == 0, result.stderr
    tree = RunTree(root, "r")
    feeds = _records(tree, "page-feed")
    readings = _records(tree, "page-reading")
    perlectiones = _records(tree, "perlectio")
    assert feeds and readings and perlectiones
    assert {feed["payload"]["witness_regime"] for feed in feeds} == {"blinded"}
    for record in readings + perlectiones:
        assert record["payload"]["provenance"]["witness_regime"] == "blinded"


def test_a_blinded_run_leaks_no_configured_chair_name_into_any_page_feed(tmp_path):
    root = tmp_path / "runs"
    result = orchestrate(root, "r", "page-unbroken", witness_context="blinded")
    assert result.returncode == 0, result.stderr
    tree = RunTree(root, "r")
    configured_chairs = set(tree.read_run()["witness_chairs"])
    assert configured_chairs, "the run must actually have configured witnesses to test blinding"
    feeds = _records(tree, "page-feed")
    assert any(feed["payload"]["witnesses"] for feed in feeds)
    # Reversal is recomputing the same deterministic function over the public roster
    # in `run.json`, never a second stored copy of it.
    run = tree.read_run()
    recomputed = {
        pseudonym_for(chair, run_id="r", config_digest=run["config_digest"])
        for chair in configured_chairs
    }
    for feed in feeds:
        labels = [row["witness_label"] for row in feed["payload"]["witnesses"]]
        assert len(set(labels)) == len(labels) and set(labels) <= recomputed
        feed_text = canonical_text(feed["payload"])
        for chair in configured_chairs:
            assert chair not in feed_text, (
                f"blinded run leaked configured chair name {chair!r} into page feed "
                f"{feed['artifact_id']}"
            )


def test_named_and_blinded_runs_of_the_same_scenario_produce_different_config_digests(tmp_path):
    """The regime is a real sealed fact, not a decoration: two runs that
    differ only in this flag are bound to different configurations, exactly
    like `pdf_target_dpi`."""
    named_root = tmp_path / "named"
    blinded_root = tmp_path / "blinded"
    assert orchestrate(named_root, "r", "page-unbroken", witness_context="named").returncode == 0
    assert (
        orchestrate(blinded_root, "r", "page-unbroken", witness_context="blinded").returncode == 0
    )
    named_digest = RunTree(named_root, "r").read_run()["config_digest"]
    blinded_digest = RunTree(blinded_root, "r").read_run()["config_digest"]
    assert named_digest != blinded_digest


def test_a_named_run_still_carries_the_real_chair_names(tmp_path):
    """The default regime is unaffected: a named feed shows each witness by its chair."""
    root = tmp_path / "runs"
    result = orchestrate(root, "r", "page-unbroken", witness_context="named")
    assert result.returncode == 0, result.stderr
    tree = RunTree(root, "r")
    configured_chairs = set(tree.read_run()["witness_chairs"])
    feed = next(feed for feed in _records(tree, "page-feed") if feed["payload"]["witnesses"])
    rows = feed["payload"]["witnesses"]
    assert {row["witness_label"] for row in rows} == {row["chair"] for row in rows}
    assert {row["chair"] for row in rows} <= configured_chairs
