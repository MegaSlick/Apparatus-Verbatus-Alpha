"""Over-capacity presentations become explicit holds without killing the stage."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from common.cross_capture_autopsia import OVER_CAPACITY  # noqa: E402

ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"
PROTOCOL = ROOT / "config" / "perlector_protocol.toml"


@pytest.fixture(scope="module")
def over_capacity_run(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("over-capacity")
    protocol = tmp / "perlector_protocol.toml"
    protocol.write_text(PROTOCOL.read_text().replace("max_images = 32", "max_images = 1"))
    root = tmp / "runs"
    result = subprocess.run(
        [
            sys.executable,
            str(ORCHESTRATOR),
            "--fixture",
            "synthetic-two-page-v0",
            "--scenario",
            "happy",
            "--run-id",
            "r",
            "--run-root",
            str(root),
            "--perlector-protocol-config",
            str(protocol),
        ],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    return result, root


def _perlectiones(root):
    return [
        json.loads(path.read_text())
        for path in sorted((root / "r" / "4_perlector" / "artifacts" / "perlectio").iterdir())
    ]


def test_an_over_capacity_presentation_holds_its_act_without_killing_the_stage(over_capacity_run):
    result, root = over_capacity_run
    # Exit 2 is fatal; a capacity hold is a run outcome rather than a crash.
    assert result.returncode != 2, result.stderr
    records = _perlectiones(root)
    assert records, "the Perlector published nothing at all"
    for record in records:
        assert record["outcome"] == "not-run"
        assert record["payload"]["reason"].startswith(OVER_CAPACITY)
        assert "max_images provides 1" in record["payload"]["reason"]
        assert record["payload"]["logical_act_id"]
        assert (
            record["payload"]["cross_capture_autopsia"]["logical_act_id"]
            == record["payload"]["logical_act_id"]
        )
        partition_ref = record["payload"]["cross_capture_autopsia"]["partition_ref"]
        assert partition_ref in record["inputs"]
        assert record["payload"]["basis"] == {"regions": [], "testimonia": []}
        assert record["payload"]["dissent"] == []
        assert "text" not in record["payload"]


def test_no_reader_pass_is_published_for_an_act_that_never_fit(over_capacity_run):
    """Capacity refusal must precede every arm, including the retained prior."""
    _result, root = over_capacity_run
    stage = root / "r" / "4_perlector" / "artifacts"
    assert not (stage / "lectio-prior").exists()
    assert not (stage / "lectio-nuda").exists()
