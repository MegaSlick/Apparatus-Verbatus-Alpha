"""The named/blinded toggle, driven end to end through the real orchestrator
rather than only unit-tested against `dossier.build_dossier` directly.
"""

import subprocess
import sys
from pathlib import Path

from common.contracts.canonical import canonical_text
from common.contracts.stages import PERLECTOR
from common.runtree.store import RunTree

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


def test_a_blinded_run_completes_and_seals_the_regime_on_every_reading(tmp_path):
    root = tmp_path / "runs"
    result = orchestrate(root, "r", "happy", witness_context="blinded")
    assert result.returncode == 0, result.stderr
    tree = RunTree(root, "r")
    readings = [
        tree.read_artifact(PERLECTOR, "perlectio", entry["artifact_id"])
        for entry in tree.build_manifest(PERLECTOR)["artifacts"]
        if entry["kind"] == "perlectio"
    ]
    assert len(readings) == 2
    for reading in readings:
        assert reading["payload"]["provenance"]["witness_regime"] == "blinded"
        assert reading["payload"]["dossier"]["witness_regime"] == "blinded"


def test_a_blinded_run_leaks_no_configured_chair_name_anywhere_in_the_dossier(tmp_path):
    root = tmp_path / "runs"
    result = orchestrate(root, "r", "happy", witness_context="blinded")
    assert result.returncode == 0, result.stderr
    tree = RunTree(root, "r")
    configured_chairs = set(tree.read_run()["witness_chairs"])
    assert configured_chairs, "the run must actually have configured witnesses to test blinding"

    for entry in tree.build_manifest(PERLECTOR)["artifacts"]:
        if entry["kind"] != "perlectio":
            continue
        reading = tree.read_artifact(PERLECTOR, "perlectio", entry["artifact_id"])
        dossier_text = canonical_text(reading["payload"]["dossier"])
        for chair in configured_chairs:
            assert chair not in dossier_text, (
                f"blinded run leaked configured chair name {chair!r} into the dossier "
                f"of {entry['artifact_id']}"
            )


def test_named_and_blinded_runs_of_the_same_scenario_produce_different_config_digests(tmp_path):
    """The regime is a real sealed fact, not a decoration: two runs that
    differ only in this flag are bound to different configurations, exactly
    like `pdf_target_dpi`."""
    named_root = tmp_path / "named"
    blinded_root = tmp_path / "blinded"
    assert orchestrate(named_root, "r", "happy", witness_context="named").returncode == 0
    assert orchestrate(blinded_root, "r", "happy", witness_context="blinded").returncode == 0
    named_digest = RunTree(named_root, "r").read_run()["config_digest"]
    blinded_digest = RunTree(blinded_root, "r").read_run()["config_digest"]
    assert named_digest != blinded_digest


def test_a_named_run_still_carries_the_real_chair_names(tmp_path):
    """The default regime is unaffected: named dossiers still show real chair
    identity, exactly as the walking skeleton always has."""
    root = tmp_path / "runs"
    result = orchestrate(root, "r", "happy", witness_context="named")
    assert result.returncode == 0, result.stderr
    tree = RunTree(root, "r")
    configured_chairs = set(tree.read_run()["witness_chairs"])
    entry = next(
        entry
        for entry in tree.build_manifest(PERLECTOR)["artifacts"]
        if entry["kind"] == "perlectio"
    )
    reading = tree.read_artifact(PERLECTOR, "perlectio", entry["artifact_id"])
    labels = {row["witness_label"] for row in reading["payload"]["dossier"]["testimonia"]}
    assert labels == configured_chairs, (
        "a named dossier must show every configured chair; a short roster is a lost witness"
    )
