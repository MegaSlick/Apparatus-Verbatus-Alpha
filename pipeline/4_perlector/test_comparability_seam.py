"""The Perlector's half of the comparability derivation, forged end to end.

Unit 14A retired `dissent_against`'s crash on a completed Testimonium carrying
no comparable text, and paid for the retirement with a `comparable` boolean on
every act attachment: `witness_coverage` counts a chair toward the witness floor
only where it is attached AND comparable (`/out/CONSULT_REPORT.md` 3).

Everything else on that record is re-derived by both readers -- the geometric
attachment, `page_role`, `content_health`, the page-witness scope, the current
attempt.  This module pins that `comparable` is too.  The attack it refuses is
cheap and quiet: seal an attachment whose page alignment honestly failed, leave
the boolean saying comparable, and the chair satisfies the floor while its own
dissent row says `compared: "unknown"` -- a satisfied floor, empty reasons and
an export that calls itself complete over testimony nothing compared.  That is
the exact record the consult's strongest objection describes.

Forged directly onto a real run's Attestatores boundary, the way
`pipeline/orchestrator/test_r0_contract_vertical_slice.py` forges its attachment
records: no producer writes this combination, and the reader's job is to refuse
it whatever wrote it.
"""

import copy
import subprocess
import sys
from pathlib import Path

from common.contracts.canonical import canonical_bytes, self_hash
from common.contracts.stages import ATTESTATORES
from common.runtree.store import RunTree

ROOT = Path(__file__).resolve().parents[2]
PAGE_CHAIR = "attestator_1"
ACT_CHAIR = "attestator_2"


def _invoke(run_root: Path, run_id: str, program: str):
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / program),
            "--run-root",
            str(run_root),
            "--run-id",
            run_id,
            "--scenario",
            "happy",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def _through_attestatores(run_root: Path, run_id: str) -> RunTree:
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/1_ink_map/run.py",
        "pipeline/2_designator/run.py",
        "pipeline/3_attestatores/run.py",
    ):
        result = _invoke(run_root, run_id, program)
        assert result.returncode == 0, f"{program}: {result.stderr}"
    return RunTree(run_root, run_id)


def _forge_attachments(tree: RunTree, rebind_stage_seal, change) -> None:
    """Apply `change` to every act-attachment row it accepts, and reseal.

    Rebinding the stage seal models the producer that wrote the bad record and
    honestly witnessed it, which is the only state in which the reader's own
    check is reachable at all (see the shared `rebind_stage_seal` fixture).
    """
    changed_any = False
    for entry in tree.build_manifest(ATTESTATORES)["artifacts"]:
        if entry["kind"] != "act-attachment":
            continue
        path = tree.resolve(entry["relative_path"])
        record = tree.read_artifact(ATTESTATORES, "act-attachment", entry["artifact_id"])
        forged = copy.deepcopy(record)
        # Listed, never `any(...)`: a short-circuit would leave later rows
        # unforged while reporting that the forgery ran.
        if not [row for row in forged["payload"]["attachments"] if change(row)]:
            continue
        changed_any = True
        forged["self_hash"] = self_hash(
            {key: value for key, value in forged.items() if key != "self_hash"}
        )
        path.write_bytes(canonical_bytes(forged))
    assert changed_any, "the forgery changed no attachment row"
    rebind_stage_seal(tree, ATTESTATORES)


def test_a_failed_page_alignment_may_not_keep_its_comparable_claim(tmp_path, rebind_stage_seal):
    """An alignment that did not place this act's text supplies no comparison.

    The forgery is the honest half of a real outcome -- bounded alignment
    failing against a page reading is ordinary, and the producer records it with
    a named reason -- while the boolean beside it still claims text to compare.
    """
    root = tmp_path / "runs"
    tree = _through_attestatores(root, "comparable-alignment")

    def unalign(row):
        if not (
            row["chair"] == PAGE_CHAIR
            and row["attached"]
            and isinstance(row["alignment"], dict)
            and row["alignment"].get("status") == "aligned"
        ):
            return False
        row["alignment"] = {"status": "unaligned", "reason": "forged-alignment-failure"}
        row["span"] = None
        return True

    _forge_attachments(tree, rebind_stage_seal, unalign)

    result = _invoke(root, "comparable-alignment", "pipeline/4_perlector/run.py")
    assert result.returncode != 0
    assert "claims a comparability its own recorded alignment does not support" in result.stderr


def test_an_act_scoped_chair_may_not_lose_its_comparable_claim_silently(
    tmp_path, rebind_stage_seal
):
    """The derivation is an equality, not a one-way implication.

    Understating comparability is also a producer/reader disagreement: it drops
    a chair that did report comparable text below the floor, and an act held for
    a witness shortfall that did not happen is as much a false record as one
    counted for evidence it never had.
    """
    root = tmp_path / "runs"
    tree = _through_attestatores(root, "comparable-understated")

    def uncompare(row):
        if not (row["chair"] == ACT_CHAIR and row["attached"] and row["comparable"]):
            return False
        row["comparable"] = False
        return True

    _forge_attachments(tree, rebind_stage_seal, uncompare)

    result = _invoke(root, "comparable-understated", "pipeline/4_perlector/run.py")
    assert result.returncode != 0
    assert "claims a comparability its own retained derived testimony does not support" in (
        result.stderr
    )


def test_an_unattached_row_may_never_claim_comparable_text(tmp_path, rebind_stage_seal):
    """Comparability implies attachment, and the implication is checked first.

    Driven on the page witness because the happy run has an unattached row for
    it -- act a1's continuation page, which carries no anchor for that act.
    Every act-scoped chair in this scenario reads and attaches, so there is no
    unattached act-scoped row to forge without changing the fixture.
    """
    root = tmp_path / "runs"
    tree = _through_attestatores(root, "comparable-unattached")

    def claim(row):
        if row["chair"] != PAGE_CHAIR or row["attached"] or row["comparable"]:
            return False
        row["comparable"] = True
        return True

    _forge_attachments(tree, rebind_stage_seal, claim)

    result = _invoke(root, "comparable-unattached", "pipeline/4_perlector/run.py")
    assert result.returncode != 0
    assert "cannot claim comparable text" in result.stderr


def test_recensor_independently_names_a_forged_comparable_boolean(tmp_path, rebind_stage_seal):
    """The floor reader does not inherit the Perlector's earlier verdict.

    Run the Perlector over the honest boundary first, then reseal a single false
    ``comparable`` boolean into that boundary.  The Recensor must read the exact
    current Testimonium itself and name the disagreement before it counts the
    floor; otherwise the two consumers are one reader in two costumes.
    """
    root = tmp_path / "runs"
    tree = _through_attestatores(root, "recensor-comparable-forgery")
    result = _invoke(root, "recensor-comparable-forgery", "pipeline/4_perlector/run.py")
    assert result.returncode == 0, result.stderr

    def uncompare(row):
        if not (row["chair"] == ACT_CHAIR and row["attached"] and row["comparable"]):
            return False
        row["comparable"] = False
        return True

    _forge_attachments(tree, rebind_stage_seal, uncompare)

    result = _invoke(root, "recensor-comparable-forgery", "pipeline/5_recensor/run.py")
    assert result.returncode != 0
    assert "claims a comparability its own retained derived testimony does not support" in (
        result.stderr
    )
