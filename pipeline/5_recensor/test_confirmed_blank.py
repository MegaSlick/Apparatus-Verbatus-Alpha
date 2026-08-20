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

from common.contracts.errors import FatalAccounting
from common.contracts.outcomes import witness_coverage
from common.contracts.stages import ATTESTATORES, RECENSOR
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


# One runner for both stop points: the stage list and exit-code contract live
# in exactly one place, so the two entry helpers below cannot drift apart.
_STAGES_THROUGH_RECENSOR = (
    "pipeline/1_exemplar/door.py",
    "pipeline/1_exemplar/run.py",
    "pipeline/2_designator/run.py",
    "pipeline/3_attestatores/run.py",
    "pipeline/4_perlector/run.py",
    "pipeline/5_recensor/run.py",
)


def _run_through(
    root: Path, run_id: str, scenario: str, programs: tuple[str, ...]
) -> subprocess.CompletedProcess:
    result = None
    for program in programs:
        result = _invoke(root, run_id, scenario, program)
        assert result.returncode in (0, 3), f"{program}: {result.stderr}"
    return result


def _run_through_recensor(root: Path, run_id: str, scenario: str) -> subprocess.CompletedProcess:
    return _run_through(root, run_id, scenario, _STAGES_THROUGH_RECENSOR)


def _run_through_perlector(root: Path, run_id: str, scenario: str) -> None:
    _run_through(root, run_id, scenario, _STAGES_THROUGH_RECENSOR[:-1])


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
    assert result.returncode == 0, result.stderr

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

    # The other act reads and accepts exactly as ever -- blank confirmation is
    # additive, not a change to the ordinary path. R6's own content-coverage
    # check (the other act's page Testimonium against its aligned attachments)
    # also finds no shortfall: a1's genuinely-empty contribution adds no
    # characters to the page text at all -- the join used to give it a leading
    # separator no act delivered (CodeRabbit W44) -- and the alignment correctly
    # maps a2's matched span back to that raw page text (not the
    # whitespace-collapsed comparison view `align_to_anchor` matches over) so
    # the join never reads as lost coverage.
    page_witness = next(
        record
        for record in (
            tree.read_artifact(ATTESTATORES, "page-testimonium", entry["artifact_id"])
            for entry in tree.build_manifest(ATTESTATORES)["artifacts"]
            if entry["kind"] == "page-testimonium"
        )
        if record["payload"]["page_ordinal"] == 1 and record["payload"]["chair"] == "attestator_1"
    )
    assert page_witness["payload"]["payload"] == "SYNTHETIC ACT TWO delta epsilon zeta eta"

    other = _review_of(tree, "a2")
    assert other["outcome"] == "accepted"
    content_coverage = other["payload"]["testimony_content_coverage"]
    measurement = content_coverage["by_chair"].get("attestator_1")
    assert measurement
    assert measurement["attached_spans"]
    assert measurement["uncovered_non_whitespace"] == {"ranges": [], "count": 0}
    assert content_coverage["shortfall"] is False


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


def test_no_readable_text_hold_names_the_testimony_shortfall_that_blocked_its_seal(
    tmp_path, monkeypatch
):
    """A route that closes the blank gate remains visible in the hold reason.

    The page-content instrument and route composer have their own focused tests;
    this test injects that instrument's measured-shortfall shape into a real
    unanimously corroborated blank run to isolate the terminal composition seam.
    """
    root = tmp_path / "runs"
    _run_through_perlector(root, "r", "confirmed-blank")

    measured_findings = RECENSOR_RUN.testimony_content_findings

    def findings_with_shortfall(context):
        findings = measured_findings(context)
        assert findings[1]["shortfall"] is False
        findings[1]["shortfall"] = True
        measurement = findings[1]["by_chair"]["attestator_1"]
        measurement["uncovered_non_whitespace"] = {
            "ranges": [{"start": 0, "end": 1}],
            "count": 1,
        }
        return findings

    monkeypatch.setattr(RECENSOR_RUN, "testimony_content_findings", findings_with_shortfall)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(ROOT / "pipeline/5_recensor/run.py"),
            "--run-root",
            str(root),
            "--run-id",
            "r",
            "--scenario",
            "confirmed-blank",
        ],
    )

    assert RECENSOR_RUN.main() == 3
    review = _review_of(RunTree(root, "r"), "a1")
    reason = review["payload"]["reason"]
    assert review["outcome"] == "held-for-review"
    assert review["payload"]["testimony_content_coverage"]["shortfall"] is True
    assert "no-readable-text" in reason
    assert "testimony coverage is incomplete at the whole-page level" in reason


def test_confirmed_blank_is_a_completed_class_terminal_outcome(tmp_path):
    """`confirmed-blank` does not hold the run -- unlike `held-for-review`, it is
    COMPLETED-class (`common/contracts/outcomes.py`) and the run reaches EXIT_COMPLETE
    when it is the only unusual act. R6's own content-coverage check runs over both
    acts' page Testimonia here too (`testimony_content_findings`) and finds nothing
    uncovered, so it adds no partial reason of its own."""
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
    assert result.returncode == 0, result.stdout + result.stderr
    assert "run r: complete" in result.stdout


# --- blank_corroboration: the pure gate, exercised directly -----------------


def _proved(outcomes: dict) -> dict:
    """`chair_read_evidence`'s shape for chairs whose attempts record a request.

    Stage 3's one write path sets the region inputs and the serving receipt
    together for every attempted outcome, so this is what the gate sees in every
    run that is not broken. The tests that forge its absence say so by name.
    """
    return {chair: {"regions": True, "receipt": True} for chair in outcomes}


def test_a_writer_impossible_record_is_fatal_on_the_witness_uncovered_path_too():
    """The two short-circuits return a quiet `None`; this does not.

    "This act cannot be confirmed blank" and "the run tree holds a record this
    pipeline's own writer could not have produced" are different findings, and
    ordering the first ahead of the second made the alarm conditional on the act
    being otherwise eligible. A recovery region is witness-uncovered by contract,
    so before the reorder it was one of the paths where a completed outcome with
    no request behind it travelled unexamined -- exactly the trees most likely to
    be malformed were the ones that never looked.
    """
    coverage = {"under_witnessed": False, "unresolved_chairs": 0, "floor": 3}
    outcomes = {
        "attestator_1": "genuinely-empty",
        "attestator_2": "genuinely-empty",
        "attestator_3": "genuinely-empty",
    }
    evidence = dict(_proved(outcomes), attestator_1={"regions": False, "receipt": False})

    with pytest.raises(FatalAccounting, match=r"attestator_1 has no region inputs and no"):
        RECENSOR_RUN.blank_corroboration(coverage, outcomes, {}, evidence, witness_uncovered=True)


def test_an_unresolved_chair_does_not_hide_a_writer_impossible_record_either():
    """The other half of the same short-circuit, and the same reasoning: a run
    that has not yet heard from every configured chair still may not carry a
    completed outcome nothing records having produced."""
    coverage = {"under_witnessed": False, "unresolved_chairs": 1, "floor": 3}
    outcomes = {"attestator_1": "genuinely-empty", "attestator_2": "genuinely-empty"}
    evidence = dict(_proved(outcomes), attestator_2={"regions": True, "receipt": False})

    with pytest.raises(FatalAccounting, match=r"attestator_2 has no serving receipt"):
        RECENSOR_RUN.blank_corroboration(coverage, outcomes, {}, evidence)


def test_an_uncovered_act_with_a_sound_record_still_returns_the_quiet_none():
    """The acceptance half beside the two refusals: the reorder moved the
    short-circuit, it did not remove it. A witness-uncovered act whose records
    are well formed is still simply not confirmable, with no alarm."""
    coverage = {"under_witnessed": False, "unresolved_chairs": 0, "floor": 3}
    outcomes = {
        "attestator_1": "genuinely-empty",
        "attestator_2": "genuinely-empty",
        "attestator_3": "genuinely-empty",
    }
    assert (
        RECENSOR_RUN.blank_corroboration(
            coverage, outcomes, {}, _proved(outcomes), witness_uncovered=True
        )
        is None
    )


def test_completed_reading_evidence_below_the_floor_never_corroborates_blank():
    coverage = {"under_witnessed": True, "unresolved_chairs": 0, "floor": 2}
    outcomes = {"attestator_1": "genuinely-empty"}
    assert RECENSOR_RUN.blank_corroboration(coverage, outcomes, {}, _proved(outcomes)) is None


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
    assert RECENSOR_RUN.blank_corroboration(coverage, outcomes, {}, _proved(outcomes)) is None


def test_an_unresolved_chair_never_corroborates_blank():
    # `floor` present, not merely omitted: this must fail on the
    # `unresolved_chairs` check specifically, not pass by accident because a
    # missing key was never read -- if the checks are ever reordered, this
    # should raise a clear `KeyError` rather than silently start asserting
    # the wrong branch.
    coverage = {"under_witnessed": False, "unresolved_chairs": 1, "floor": 2}
    outcomes = {"attestator_1": "genuinely-empty", "attestator_2": "not-run"}
    assert RECENSOR_RUN.blank_corroboration(coverage, outcomes, {}, _proved(outcomes)) is None


def test_testimonia_from_original_regions_cannot_confirm_a_recovery_region_blank():
    """Recovery ink is witness-uncovered; inherited testimony did not see it."""
    coverage = {"under_witnessed": False, "unresolved_chairs": 0, "floor": 3}
    outcomes = {
        "attestator_1": "genuinely-empty",
        "attestator_2": "genuinely-empty",
        "attestator_3": "genuinely-empty",
    }
    assert (
        RECENSOR_RUN.blank_corroboration(
            coverage, outcomes, {}, _proved(outcomes), witness_uncovered=True
        )
        is None
    )


def test_zero_completed_chairs_never_corroborates_blank():
    """A floor of zero must not let an empty completed set stand in for
    positive evidence of absence."""
    coverage = {"under_witnessed": False, "unresolved_chairs": 0, "floor": 0}
    outcomes = {"attestator_1": "failed", "attestator_2": "dead"}
    assert RECENSOR_RUN.blank_corroboration(coverage, outcomes, {}, _proved(outcomes)) is None


def test_one_dissenting_read_refuses_corroboration():
    coverage = {"under_witnessed": False, "unresolved_chairs": 0, "floor": 3}
    outcomes = {
        "attestator_1": "genuinely-empty",
        "attestator_2": "genuinely-empty",
        "attestator_3": "read",
    }
    assert RECENSOR_RUN.blank_corroboration(coverage, outcomes, {}, _proved(outcomes)) is None


def test_unanimous_genuinely_empty_corroborates_blank():
    coverage = {"under_witnessed": False, "unresolved_chairs": 0, "floor": 3}
    outcomes = {
        "attestator_1": "genuinely-empty",
        "attestator_2": "genuinely-empty",
        "attestator_3": "genuinely-empty",
    }
    assert RECENSOR_RUN.blank_corroboration(coverage, outcomes, {}, _proved(outcomes)) == [
        "attestator_1",
        "attestator_2",
        "attestator_3",
    ]


def test_a_completed_outcome_with_no_receipt_cannot_corroborate_a_blank():
    """A completed outcome with nothing recording a request cannot corroborate.

    Defence against a resealed or foreign record, stated precisely: Sol-S1's
    OWN fabricated records carried regions and a receipt (the buggy writer
    attached both), so this gate would not have caught them -- the repair for
    that is upstream, where the minting branch is deleted. What this refuses
    is the record shape neither the current nor even the buggy writer produced:
    a completed-class outcome that no request is on record as having produced.
    It is a presence check; the strong per-byte counterpart
    (`validate_serving_provenance`, region identity) runs at the Perlector over
    the same artifacts earlier in every run.
    """
    coverage = {"under_witnessed": False, "unresolved_chairs": 0, "floor": 3}
    outcomes = {
        "attestator_1": "genuinely-empty",
        "attestator_2": "genuinely-empty",
        "attestator_3": "genuinely-empty",
    }
    evidence = dict(_proved(outcomes), attestator_2={"regions": True, "receipt": False})

    with pytest.raises(FatalAccounting, match=r"attestator_2 has no serving receipt"):
        RECENSOR_RUN.blank_corroboration(coverage, outcomes, {}, evidence)


def test_a_completed_outcome_naming_no_regions_cannot_corroborate_a_blank():
    """The other half of the same record: a chair said to have read the exact
    regions, with no region named. `genuinely-empty` is a claim about a
    specific rectangle of ink, and it cannot be made about none."""
    coverage = {"under_witnessed": False, "unresolved_chairs": 0, "floor": 3}
    outcomes = {
        "attestator_1": "genuinely-empty",
        "attestator_2": "genuinely-empty",
        "attestator_3": "genuinely-empty",
    }
    evidence = dict(_proved(outcomes), attestator_3={"regions": False, "receipt": True})

    with pytest.raises(FatalAccounting, match=r"attestator_3 has no region inputs"):
        RECENSOR_RUN.blank_corroboration(coverage, outcomes, {}, evidence)


def test_missing_read_evidence_for_a_non_reading_chair_is_not_a_fault():
    """`dead` and `not-run` chairs legitimately carry neither fact. The gate
    reads evidence only for the chairs it would name as corroborators, so an
    absent chair beside a satisfied floor is refused on the floor arithmetic it
    was always refused on -- not on a fatal about a receipt it should not have."""
    coverage = {"under_witnessed": True, "unresolved_chairs": 0, "floor": 3}
    outcomes = {
        "attestator_1": "genuinely-empty",
        "attestator_2": "genuinely-empty",
        "attestator_3": "dead",
    }
    evidence = {
        "attestator_1": {"regions": True, "receipt": True},
        "attestator_2": {"regions": True, "receipt": True},
        "attestator_3": {"regions": False, "receipt": False},
    }

    assert RECENSOR_RUN.blank_corroboration(coverage, outcomes, {}, evidence) is None


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
    assert RECENSOR_RUN.blank_corroboration(coverage, outcomes, {}, _proved(outcomes)) == sorted(
        outcomes
    )
    # One chair that actually read text closes it, however satisfied the floor.
    contradicted = dict(outcomes, attestator_2="read")
    assert (
        RECENSOR_RUN.blank_corroboration(
            witness_coverage(contradicted, 3, attachments={c: True for c in contradicted}),
            contradicted,
            {},
            _proved(contradicted),
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
    assert RECENSOR_RUN.blank_corroboration(
        coverage, outcomes, anchored, _proved(outcomes)
    ) == sorted(outcomes)

    anchorless_page = {chair: _fact("no-page-anchor") for chair in outcomes}
    assert RECENSOR_RUN.blank_corroboration(
        coverage, outcomes, anchorless_page, _proved(outcomes)
    ) == sorted(outcomes)

    unlocated = dict(anchored, attestator_3=_fact("act-line-not-located"))
    assert (
        RECENSOR_RUN.blank_corroboration(coverage, outcomes, unlocated, _proved(outcomes)) is None
    )

    # A page witness whose fact carries NO basis at all is geometry nobody
    # checked. `act_attachment_facts` refuses such a record at the producer
    # boundary; this pins the gate's own defence in depth behind it.
    basisless_fact = _fact("act-anchor")
    del basisless_fact["anchor_basis"]
    basisless = dict(anchored, attestator_3=basisless_fact)
    assert (
        RECENSOR_RUN.blank_corroboration(coverage, outcomes, basisless, _proved(outcomes)) is None
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
