"""A fed run under a tiny sealed dissent budget, carried to the export.

`--blind-read fed` makes a Pass-A draft the establishing reading is shown, so
each reading's `self_revision` is measured against it. Sealed with
`[dissent] max_comparison_steps = 1`, every such comparison stops: the
Perlectio records the explicit non-verdict, the Archetypus projects it to a
null `self_revisions`, and the Armarium re-derives that null into
`acts.jsonl` -- never 0, never `[]` -- and counts it in the not-measured
ledger. The Archetypus and the Armarium each check the recorded budget
against the one the run sealed.
"""

from __future__ import annotations

import io
import json
import shutil
import sys
import zipfile
from pathlib import Path

import pytest

from common.alignment import DEFAULT_ALIGNMENT_CONFIG_PATH, load_dissent_limits
from common.contracts.errors import ContractError
from common.contracts.prior_draft import unmeasured_comparison
from common.contracts.stages import ARCHETYPUS, ARMARIUM, PERLECTOR
from common.runtree.store import RunTree
from conftest import load_stage, programs_through, run_stage, stage_artifacts

ROOT = Path(__file__).resolve().parents[1]
RUN_ID = "fed-budget"
SCENARIO = "happy"


def _tiny_budget(directory: Path) -> Path:
    shipped = DEFAULT_ALIGNMENT_CONFIG_PATH.read_text(encoding="utf-8")
    sealed_line = f"max_comparison_steps = {load_dissent_limits()[0].max_comparison_steps}\n"
    assert shipped.count(sealed_line) == 1
    config = directory / "alignment.toml"
    config.write_text(shipped.replace(sealed_line, "max_comparison_steps = 1\n"), encoding="utf-8")
    return config


def _options(config: Path) -> dict[str, object]:
    return {"blind_read": "fed", "alignment_config": config}


def _run(root: Path, config: Path, last: str) -> None:
    for program in programs_through(last):
        result = run_stage(root, RUN_ID, SCENARIO, program, **_options(config))
        assert result.returncode == 0, f"{program}: {result.stderr}"


def _in_process(monkeypatch, stage: str, config: Path, root: Path, sealed_budget: int):
    """The stage's main in this process, told the run sealed `sealed_budget`."""
    module = load_stage(stage)
    monkeypatch.setattr(module, "sealed_dissent_budget", lambda _context: sealed_budget)
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(
        sys,
        "argv",
        [str(ROOT / "pipeline" / stage / "run.py"), "--run-root", str(root), "--run-id", RUN_ID]
        + ["--scenario", SCENARIO, "--blind-read", "fed", "--alignment-config", str(config)],
    )
    return module.main()


@pytest.fixture(scope="module")
def through_recensor(tmp_path_factory):
    """One fed run under a one-step budget, stopped before the Archetypus."""
    directory = tmp_path_factory.mktemp("fed-budget")
    config = _tiny_budget(directory)
    root = directory / "runs"
    _run(root, config, "recensor")
    return root, config


def test_a_budget_stopped_self_revision_reaches_acts_jsonl_as_null(through_recensor, tmp_path):
    source, config = through_recensor
    root = tmp_path / "runs"
    shutil.copytree(source, root)
    tree = RunTree(root, RUN_ID)

    readings = stage_artifacts(tree, PERLECTOR, "perlectio")
    assert readings
    for reading in readings:
        payload = reading["payload"]
        assert payload["lectio_kind"] == "primed-with-prior"
        assert payload["self_revision"] == unmeasured_comparison(1)

    for program in programs_through("armarium")[-2:]:
        result = run_stage(root, RUN_ID, SCENARIO, program, **_options(config))
        assert result.returncode == 0, f"{program}: {result.stderr}"

    established = stage_artifacts(tree, ARCHETYPUS, "archetypus")
    assert established
    assert all(record["payload"]["uncertainty"]["self_revisions"] is None for record in established)

    (export,) = stage_artifacts(tree, ARMARIUM, "export")
    bundle = tree.read_bytes(export["payload"]["bundle"]["reference"]["relative_path"])
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        rows = [json.loads(line) for line in archive.read("acts.jsonl").splitlines()]
        manifest = json.loads(archive.read("EXPORT_MANIFEST.json"))
    delivered = [row for row in rows if row["category"] == "delivered"]
    assert delivered
    for row in delivered:
        assert row["uncertainty"]["lectio_kind"] == "primed-with-prior"
        assert "self_revisions" in row["uncertainty"]
        assert row["uncertainty"]["self_revisions"] is None

    (bounds,) = [
        entry
        for entry in manifest["claims"]["not_measured"]["entries"]
        if entry["instrument"] == "comparison-bounds"
    ]
    assert bounds["status"] == "not-measured"
    assert bounds["detail"]["delivered_self_revisions_stopped"] == len(delivered)
    assert bounds["detail"]["delivered_dissent_rows_stopped"] > 0


@pytest.mark.parametrize("stage", ["6_archetypus", "7_armarium"])
def test_a_stopped_record_naming_a_budget_the_run_never_sealed_is_refused(
    through_recensor, tmp_path, monkeypatch, stage
):
    """The Perlectio records a one-step budget; told the run sealed two, the
    re-deriving stage refuses rather than carrying a budget nobody bound."""
    source, config = through_recensor
    root = tmp_path / "runs"
    shutil.copytree(source, root)
    if stage == "7_armarium":
        result = run_stage(
            root, RUN_ID, SCENARIO, programs_through("archetypus")[-1], **_options(config)
        )
        assert result.returncode == 0, result.stderr

    with pytest.raises(ContractError, match="stopped on a 1-step dissent budget, but this run"):
        _in_process(monkeypatch, stage, config, root, sealed_budget=2)
