"""Blank confirmation: the Recensor's other terminal outcome for a non-completed
reading, alongside `held-for-review`.

ARCHITECTURE and spec 09 both name this: "a zero-output unit is diagnosed, then
either sealed confirmed-blank with evidence or held unresolved-with-evidence.
Never quietly completed." Before this build, `confirmed-blank` was a real member
of the outcome algebra (`common/contracts/outcomes.py`) that nothing ever
produced -- every `no-readable-text` Perlectio was held for review forever, with
no path to ever close the act.

The window pass (2026-08-05) found the old pipeline's own hard-won rule for
this: a blank verdict may never rest on fewer than several
genuinely independent completed reads, and never on a reader's own second
opinion. This is unanimity about an absence, never a selection among presences
(GOVERNANCE 3) -- the Perlector's own direct reading of the ink already found
nothing; the witnesses only corroborate or contradict that finding, and a single
dissenting witness holds the act for a human rather than being outvoted.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from common.contracts.outcomes import witness_coverage
from common.contracts.stages import RECENSOR
from common.runtree.store import RunTree

ROOT = Path(__file__).resolve().parents[2]


def _load_module(relative_path: str, name: str):
    path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RECENSOR_RUN = _load_module("pipeline/5_recensor/run.py", "recensor_run_confirmed_blank")


def _invoke(root: Path, run_id: str, scenario: str, program: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / program),
            "--run-root",
            str(root),
            "--run-id",
            run_id,
            "--scenario",
            scenario,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def _run_through_recensor(root: Path, run_id: str, scenario: str) -> subprocess.CompletedProcess:
    result = None
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/2_designator/run.py",
        "pipeline/3_attestatores/run.py",
        "pipeline/4_perlector/run.py",
        "pipeline/5_recensor/run.py",
    ):
        result = _invoke(root, run_id, scenario, program)
        assert result.returncode in (0, 3), f"{program}: {result.stderr}"
    return result


def _review_of(tree: RunTree, act_key: str) -> dict:
    reviews = [
        tree.read_artifact(RECENSOR, "review", entry["artifact_id"])
        for entry in tree.build_manifest(RECENSOR)["artifacts"]
        if entry["kind"] == "review"
    ]
    matches = [review for review in reviews if review["payload"]["act_key"] == act_key]
    assert len(matches) == 1
    return matches[0]


def test_unanimous_absence_seals_confirmed_blank(tmp_path):
    """The Perlector's own `no-readable-text`, corroborated by every one of the
    three configured witnesses independently reporting `genuinely-empty`."""
    root = tmp_path / "runs"
    result = _run_through_recensor(root, "r", "confirmed-blank")
    # R6 also checks the other act's page Testimonium against its aligned
    # attachments.  Its uncovered trailing text is a separate coverage finding,
    # so the run is partial while this act can still be correctly sealed blank.
    assert result.returncode == 3, result.stderr

    tree = RunTree(root, "r")
    review = _review_of(tree, "a1")
    assert review["outcome"] == "confirmed-blank"
    assert review["payload"]["coverage"]["by_outcome"] == {"genuinely-empty": 3}
    assert "attestator_1" in review["payload"]["reason"]
    assert "attestator_2" in review["payload"]["reason"]
    assert "attestator_3" in review["payload"]["reason"]

    # Spec 09 seals a blank "with evidence", and a sentence is not evidence a
    # consumer can read. The same facts are recorded as data beside the prose.
    assert review["payload"]["blank_evidence"] == {
        "perlector_outcome": "no-readable-text",
        "corroborating_chairs": ["attestator_1", "attestator_2", "attestator_3"],
        "pages_without_residual_ink_outside_coverage": [1],
    }

    # The other act is held by the independent testimony coverage diff, not by
    # blank confirmation or a text-quality judgment.
    other = _review_of(tree, "a2")
    assert other["outcome"] == "held-for-review"
    assert other["payload"]["testimony_content_coverage"]["shortfall"] is True


def test_a_dissenting_witness_holds_instead_of_confirming_blank(tmp_path):
    """Same Perlector finding (`no-readable-text`), but only two of three chairs
    agree -- the third reads real text. GOALS 1: a single dissent is never
    silently resolved, so the act is held for a human, never outvoted."""
    root = tmp_path / "runs"
    result = _run_through_recensor(root, "r", "blank-with-dissent")
    assert result.returncode == 3, result.stderr

    tree = RunTree(root, "r")
    review = _review_of(tree, "a1")
    assert review["outcome"] == "held-for-review"
    assert "no-readable-text" in review["payload"]["reason"]
    assert review["payload"]["coverage"]["by_outcome"] == {"read": 1, "genuinely-empty": 2}
    # No evidence field at all, rather than an empty one: nothing was sealed, so
    # there is nothing this act was sealed on.
    assert "blank_evidence" not in review["payload"]


def test_confirmed_blank_remains_a_completed_terminal_when_another_act_is_held(tmp_path):
    """The R6 content shortfall makes this two-act fixture partial, but does not
    recategorize the independently established `confirmed-blank` outcome."""
    root = tmp_path / "runs"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline" / "orchestrator" / "run.py"),
            "--fixture",
            "synthetic-two-page-v0",
            "--scenario",
            "confirmed-blank",
            "--run-id",
            "r",
            "--run-root",
            str(root),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 3, result.stdout + result.stderr
    assert "act a2 is held-for-review" in result.stdout
    assert "act a1 is under-witnessed" in result.stdout


# --- blank_corroboration: the pure gate, exercised directly -----------------


def test_completed_reading_evidence_below_the_floor_never_corroborates_blank():
    coverage = {"under_witnessed": True, "unresolved_chairs": 0, "floor": 2}
    outcomes = {"attestator_1": "genuinely-empty"}
    assert RECENSOR_RUN.blank_corroboration(coverage, outcomes, {}) is None


def test_an_excluded_chair_cannot_stand_in_for_a_witness_that_never_read(tmp_path):
    """The floor is met by real ATTESTATORES `witness_coverage` accounting (two
    genuinely-empty reads plus one Tyrel-approved `excluded` chair, which
    classifies COMPLETED for ATTESTATORES -- common/contracts/outcomes.py), so
    `under_witnessed` is `False`. `excluded` is not a reading, though: the
    chair never looked at the ink, and `blank_corroboration` must not let it
    stand in for the floor's third genuinely independent read."""
    outcomes = {
        "attestator_1": "genuinely-empty",
        "attestator_2": "genuinely-empty",
        "attestator_3": "excluded",
    }
    coverage = witness_coverage(outcomes, configured_floor=3)
    assert coverage["under_witnessed"] is False
    assert RECENSOR_RUN.blank_corroboration(coverage, outcomes, {}) is None


def test_an_unresolved_chair_never_corroborates_blank():
    # `floor` present, not merely omitted: this must fail on the
    # `unresolved_chairs` check specifically, not pass by accident because a
    # missing key was never read -- if the checks are ever reordered, this
    # should raise a clear `KeyError` rather than silently start asserting
    # the wrong branch.
    coverage = {"under_witnessed": False, "unresolved_chairs": 1, "floor": 2}
    outcomes = {"attestator_1": "genuinely-empty", "attestator_2": "not-run"}
    assert RECENSOR_RUN.blank_corroboration(coverage, outcomes, {}) is None


def test_testimonia_from_original_regions_cannot_confirm_a_recovery_region_blank():
    """Recovery ink is witness-uncovered; inherited testimony did not see it."""
    coverage = {"under_witnessed": False, "unresolved_chairs": 0, "floor": 3}
    outcomes = {
        "attestator_1": "genuinely-empty",
        "attestator_2": "genuinely-empty",
        "attestator_3": "genuinely-empty",
    }
    assert RECENSOR_RUN.blank_corroboration(coverage, outcomes, {}, witness_uncovered=True) is None


def test_zero_completed_chairs_never_corroborates_blank():
    """A floor of zero must not let an empty completed set stand in for
    positive evidence of absence."""
    coverage = {"under_witnessed": False, "unresolved_chairs": 0, "floor": 0}
    outcomes = {"attestator_1": "failed", "attestator_2": "dead"}
    assert RECENSOR_RUN.blank_corroboration(coverage, outcomes, {}) is None


def test_one_dissenting_read_refuses_corroboration():
    coverage = {"under_witnessed": False, "unresolved_chairs": 0, "floor": 3}
    outcomes = {
        "attestator_1": "genuinely-empty",
        "attestator_2": "genuinely-empty",
        "attestator_3": "read",
    }
    assert RECENSOR_RUN.blank_corroboration(coverage, outcomes, {}) is None


def test_unanimous_genuinely_empty_corroborates_blank():
    coverage = {"under_witnessed": False, "unresolved_chairs": 0, "floor": 3}
    outcomes = {
        "attestator_1": "genuinely-empty",
        "attestator_2": "genuinely-empty",
        "attestator_3": "genuinely-empty",
    }
    assert RECENSOR_RUN.blank_corroboration(coverage, outcomes, {}) == [
        "attestator_1",
        "attestator_2",
        "attestator_3",
    ]


def test_a_floor_met_only_by_trivially_attached_empty_readings_completes_only_as_a_blank():
    """R4 audit, composition seam: the trivial zero-length attachment (F-G2) x
    the rederived coverage count x dissent's zero-span departure record.

    Named for what it measures: the floor arithmetic and the blank door over
    trivially attached `genuinely-empty` chairs. Spans are not inputs to
    `witness_coverage` at all -- the zero-length SPAN shape itself is pinned
    where spans live, by the acceptance suite's genuinely-empty-witness
    scenario ({"start": 0, "end": 0}) and the vertical slice's span checks.

    An act CAN reach a satisfied floor with no chair having placed one
    character of real text -- every chair `genuinely-empty`, every attachment
    zero-length, `under_witnessed` False. That is the intended reading of a
    blank act and the whole basis of `confirmed-blank`: `genuinely-empty` is a
    reading outcome, not a recorded failure, and "read the page, found nothing"
    is exactly the independent corroboration this outcome exists to recognize.

    What keeps a NON-blank act out of that door is the Perlector's own
    autopsia, not the witness count: `blank_corroboration` is consulted only
    when the reading itself is `no-readable-text`, and one chair that actually
    read text collapses it to `None` and holds the act for a human. An act
    whose reading DID establish text and whose witnesses were all
    genuinely-empty is not refused -- it is delivered, with `by_outcome`
    retaining `{genuinely-empty: 3}` and every dissent row recording
    `departed: True` over the whole reading. That is a recorded contradiction
    rather than a silent one (GOVERNANCE 2), and flagging it belongs to R6's
    named per-witness content diff (page text against the ordered union of that
    witness's own act attachments), not to a coverage floor. Pinned here so the
    answer travels with the code.
    """
    outcomes = {
        "attestator_1": "genuinely-empty",
        "attestator_2": "genuinely-empty",
        "attestator_3": "genuinely-empty",
    }
    coverage = witness_coverage(outcomes, 3, attachments={chair: True for chair in outcomes})

    assert coverage["under_witnessed"] is False
    assert coverage["by_outcome"] == {"genuinely-empty": 3}
    assert coverage["shortfalls"] == {"failed": 0, "truncated": 0, "unaligned": 0}
    # The blank door, open only because the Perlector itself found no ink.
    assert RECENSOR_RUN.blank_corroboration(coverage, outcomes, {}) == sorted(outcomes)
    # One chair that actually read text closes it, however satisfied the floor.
    contradicted = dict(outcomes, attestator_2="read")
    assert (
        RECENSOR_RUN.blank_corroboration(
            witness_coverage(contradicted, 3, attachments={c: True for c in contradicted}),
            contradicted,
            {},
        )
        is None
    )


def test_an_unlocated_act_line_never_corroborates_a_terminal_blank():
    """CR round 3 on R4's PR loop, ruled here with one refinement over the
    finding: a page witness whose trivial attach discloses `anchor_basis:
    "act-line-not-located"` (the page's anchor EXISTS yet locates no line for
    this act) still counts toward the floor, but confirmed-blank is a PROVED
    absence and geometry that does not reconcile may not seal one -- the act
    holds for a human (GOVERNANCE 2/9). `no-page-anchor` is the different
    fact of a page with no Chandra anchor at all: the ink-free-page scenario's
    Designator-minted fallback act lives exactly there, and refusing blank on
    it would make the intended blank-page path unreachable (the acceptance
    suite pins that scenario end-to-end at exit 0). The not-located case has
    no shipped scenario (`chandra_anchor` is scenario-global and both act
    lines always locate), so the gate is exercised here directly, on forged
    facts, exactly as the rest of this section forges coverage records."""
    outcomes = {
        "attestator_1": "genuinely-empty",
        "attestator_2": "genuinely-empty",
        "attestator_3": "genuinely-empty",
    }
    coverage = witness_coverage(outcomes, 3, attachments={c: True for c in outcomes})

    def _fact(anchor_basis: str) -> dict:
        # The full shape `act_attachment_facts` emits, not only the key the
        # gate reads today -- a slimmer forgery would keep passing if the gate
        # started requiring another producer key.
        return {
            "attached": True,
            "truncated": False,
            "health_unrecorded": False,
            "page_witness": True,
            "content_health": {"truncated": False, "characters": 0},
            "anchor_basis": anchor_basis,
        }

    anchored = {chair: _fact("act-anchor") for chair in outcomes}
    assert RECENSOR_RUN.blank_corroboration(coverage, outcomes, anchored) == sorted(outcomes)

    anchorless_page = {chair: _fact("no-page-anchor") for chair in outcomes}
    assert RECENSOR_RUN.blank_corroboration(coverage, outcomes, anchorless_page) == sorted(outcomes)

    unlocated = dict(anchored, attestator_3=_fact("act-line-not-located"))
    assert RECENSOR_RUN.blank_corroboration(coverage, outcomes, unlocated) is None

    # A page witness whose fact carries NO basis at all is geometry nobody
    # checked. `act_attachment_facts` refuses such a record at the producer
    # boundary; this pins the gate's own defence in depth behind it.
    basisless_fact = _fact("act-anchor")
    del basisless_fact["anchor_basis"]
    basisless = dict(anchored, attestator_3=basisless_fact)
    assert RECENSOR_RUN.blank_corroboration(coverage, outcomes, basisless) is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
