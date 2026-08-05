"""The shared Exemplar boundary check, exercised at its own interface.

`common/exemplar_boundary.py` is what the Designator runs before it crops and what
the Armarium runs before it exports. `pipeline/2_designator/test_exemplar_boundary.py`
covers it end to end by damaging a real run tree, which is the right test for the
cases damage can reach: a deleted page, a rewritten corpus seal, altered or missing
pixels.

Two of its checks cannot be reached that way, and were landed with nothing
exercising them at all — found by deleting each and watching the whole suite stay
green. Both compare the sealed evidence against arguments the *caller* supplies:
the `run.json` ledger row for this ordinal, and the run authority itself. Damaging
the tree cannot produce that disagreement, because every artifact in the chain is
pinned by a digest one level up, so the outer check fires first. A caller passing
the wrong row can, and that is a stage-integration bug rather than tampering — the
kind that ships quietly because everything downstream still validates.

So they are tested here, at the boundary function's own interface, which is where
the disagreement actually lives.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from common.contracts.errors import ContractError
from common.contracts.stages import EXEMPLAR
from common.exemplar_boundary import verify_sealed_page_pixels
from common.runtree.store import RunTree

ROOT = Path(__file__).resolve().parents[1]
ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"


@pytest.fixture
def sealed(tmp_path):
    """One real synthetic run, and its first sealed page with its ledger row."""
    result = subprocess.run(
        [
            sys.executable,
            str(ORCHESTRATOR),
            "--fixture",
            "synthetic-two-page-v0",
            "--scenario",
            "happy",
            "--run-root",
            str(tmp_path / "runs"),
            "--run-id",
            "boundary-unit",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    tree = RunTree(tmp_path / "runs", "boundary-unit")
    run = tree.read_run()
    page = next(
        record
        for record in (
            tree.read_artifact(EXEMPLAR, "page", entry["artifact_id"])
            for entry in tree.build_manifest(EXEMPLAR)["artifacts"]
            if entry["kind"] == "page"
        )
        if record["outcome"] == "sealed"
    )
    source = next(
        row for row in run["source_manifest"] if row["ordinal"] == page["payload"]["ordinal"]
    )
    return tree, run, source, page


def test_the_undamaged_page_and_its_own_ledger_row_verify(sealed):
    """The check passes on the real thing, so a failure below means what it says."""
    tree, run, source, page = sealed

    verify_sealed_page_pixels(tree, run, source, page)


def test_a_page_checked_against_another_filename_ledger_row_refuses(sealed):
    """The filename is the citation link (ruling 1), so it is checked, not carried.

    A stage that walks its own list of sources and its own list of pages, and pairs
    them up wrongly, produces exactly this: a sealed page verified against a row
    naming a different file. Everything downstream of it still validates, because
    every digest in the chain is intact — it is only the *link back to Tyrel's own
    file* that is now wrong, which is the one thing no later check looks at.
    """
    tree, run, source, page = sealed
    other_name = dict(source, relative_path="a-different-scan.png")

    with pytest.raises(ContractError, match="submitted filename ledger entry"):
        verify_sealed_page_pixels(tree, run, other_name, page)


def test_a_page_checked_against_another_ledger_digest_refuses(sealed):
    tree, run, source, page = sealed
    other_digest = dict(source, sha256="f" * 64)

    with pytest.raises(ContractError, match="submitted filename ledger entry"):
        verify_sealed_page_pixels(tree, run, other_digest, page)


def test_a_ledger_fact_the_page_never_carried_refuses(sealed):
    """A ledger row can gain a fact as well as change one, and both must refuse.

    `bytes` and `ledger_sha256` are optional on a source row — the fixture path has
    no local ledger. A row that claims one where the sealed page carries none is a
    row from a different submission, not a richer description of this one.
    """
    tree, run, source, page = sealed
    invented = dict(source, ledger_sha256="a" * 64)

    with pytest.raises(ContractError, match="submitted filename ledger entry"):
        verify_sealed_page_pixels(tree, run, invented, page)


def test_a_door_admission_bound_to_a_different_run_refuses(sealed):
    """The Door admission is re-read and re-checked, not trusted for existing.

    The page names its admission by digest, so the bytes are certainly the bytes the
    Exemplar sealed. What that proves is that nobody swapped the file — not that the
    admission inside it belongs to *this* run and *this* source. A stage handed the
    wrong run authority would otherwise crop pixels admitted under a configuration
    that no longer describes them.
    """
    tree, run, source, page = sealed
    other_run = dict(run, run_id="some-other-run")

    with pytest.raises(ContractError, match="Door admission does not match this source"):
        verify_sealed_page_pixels(tree, other_run, source, page)
