"""Operator review decisions take effect through the Recensor and reach the export.

The tree is the live reading seam's (`pipeline/test_live_reading_seam_e2e.py`):
live witnesses, then a live page reader answering the fixture's `happy` pages,
except that Churro's page 1 reading is cut off. A truncated reading does not
count toward the witness floor, so the real Recensor holds p1:1 and p1:2 on
`under-witnessed` alone, a hold of its own measured per page that a person may
clear with a page decision; p2:1 is accepted. Decisions are written as
`approval-record.v1` into the run's `receipts/sha256/` with fixed timestamps,
the Recensor runs again, and the Archetypus, Coniector and Armarium follow.
Every bundle is checked the way a recipient would, by `verify_delivered_bundle`
on a clean directory.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from test_live_reading_seam_e2e import (  # noqa: F401  (`designated` is a fixture)
    ROOT,
    TIER,
    PageReaderWorld,
    designated,
    perlector,
    read_by_live_witnesses,
    run_in_process,
    stage_argv,
    witness_scripts,
)

from common.contracts.approval import build_review_decision_record
from common.contracts.canonical import digest_bytes
from common.contracts.stages import ARCHETYPUS, ARMARIUM
from common.page_review import held_by_recensor, held_pages_after_review
from common.review_decisions import (
    READING_HELD,
    aggregate_clearances,
    basis_digest,
    page_basis_digest,
)
from common.runtree.store import RunTree
from common.stage import EXIT_COMPLETE, EXIT_HELD
from conftest import advance_held_recensor, build_page_tree, load_stage, run_stage
from conftest import file_digest_snapshot as snapshot
from operations.serving.fakes import ScriptedAnswer

RUN_ID = "r"
UNDER_WITNESSED = "under-witnessed"
TIMESTAMP = "2026-10-01T12:00:00Z"
# A basis no review of this run has: the facts the decision looked at have changed.
EARLIER_BASIS = digest_bytes(b"an earlier review of this subject")
TAIL = (
    "pipeline/6_archetypus/run.py",
    "pipeline/4b_coniector/run.py",
    "pipeline/7_armarium/run.py",
)
verify_delivered_bundle = load_stage(
    "7_armarium", "armarium_export", isolate_path=True
).verify_delivered_bundle


@pytest.fixture(scope="module")
def held(designated, tmp_path_factory) -> SimpleNamespace:  # noqa: F811
    """The tree through the Recensor's first pass, page 1's units held on `under-witnessed` alone."""
    work = tmp_path_factory.mktemp("under-witnessed")
    root = work / "runs"
    shutil.copytree(designated.run_root, root)
    scripts = witness_scripts()
    scripts["attestator_3"][0] = ScriptedAnswer(
        content=scripts["attestator_3"][0].content, finish_reason="length"
    )
    read_by_live_witnesses(designated, root, work / "witnesses", scripts)
    reader = PageReaderWorld(designated.catalogue, work / "reader")
    assert (
        run_in_process(
            perlector,
            root,
            designated.catalogue,
            placement_tier=TIER,
            serving_factory=reader.factory,
        )
        == EXIT_COMPLETE
    )
    tree = SimpleNamespace(root=root, catalogue=designated.catalogue)
    assert _run(tree, "pipeline/5_recensor/run.py").returncode == EXIT_HELD
    reviews = _reviews(root)
    for key in ("p1:1", "p1:2"):
        assert (reviews[key]["outcome"], reviews[key]["payload"]["hold_codes"]) == (
            "held-for-review",
            [UNDER_WITNESSED],
        )
    assert reviews["p2:1"]["outcome"] == "accepted"
    return tree


def _copy(held: SimpleNamespace, base: Path) -> SimpleNamespace:
    shutil.copytree(held.root, base / "runs")
    return SimpleNamespace(root=base / "runs", catalogue=held.catalogue)


def _run(tree: SimpleNamespace, program: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            "-I",
            str(ROOT / program),
            *stage_argv(tree.root, tree.catalogue, placement_tier=TIER),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def _recense(tree: SimpleNamespace) -> int:
    result = _run(tree, "pipeline/5_recensor/run.py")
    assert result.returncode in (EXIT_COMPLETE, EXIT_HELD), result.stderr
    return result.returncode


def _after_recensor(tree: SimpleNamespace) -> None:
    """The Archetypus, the Coniector and the Armarium, each required to finish.

    A Recensor that still holds anything is first advanced, as a person would
    to export with every hold named; neither stage runs past it otherwise.
    """
    if held_by_recensor(RunTree(tree.root, RUN_ID)):
        advance_held_recensor(tree.root, RUN_ID)
    for program in TAIL:
        result = _run(tree, program)
        assert result.returncode in (EXIT_COMPLETE, EXIT_HELD), f"{program}: {result.stderr}"


def _records(root: Path, kind: str) -> list[dict]:
    found = []
    for path in sorted((root / RUN_ID / "5_recensor").rglob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(record, dict) and record.get("kind") == kind:
            found.append(record)
    return found


def _latest(records: list[dict]) -> dict:
    return max(records, key=lambda record: record["payload"]["attempt_ordinal"])


def _reviews(root: Path) -> dict[str, dict]:
    """Each unit's current Recensor review, by act key."""
    by_key: dict[str, list[dict]] = {}
    for record in _records(root, "review"):
        by_key.setdefault(record["payload"]["act_key"], []).append(record)
    return {key: _latest(records) for key, records in by_key.items()}


def _decisions(root: Path) -> dict | None:
    records = _records(root, "review-decisions")
    return _latest(records)["payload"] if records else None


def _machine_basis(review: dict) -> str:
    return basis_digest(
        {name: value for name, value in review["payload"].items() if name != "attempt_ordinal"}
    )


def _page_id(root: Path, review: dict) -> str:
    """The page a review's unit is on, as its page reading names it."""
    reading = root / RUN_ID / review["payload"]["page_reading_ref"]["relative_path"]
    return json.loads(reading.read_text(encoding="utf-8"))["subject_id"]


def _decide(
    root: Path,
    subject: str,
    decision: str,
    *,
    basis: str | None = None,
    finding=None,
    text: str | None = None,
    note: str | None = None,
) -> str:
    """Record one decision about a unit (`p1:2`) or a page (`p1`); its stored path.

    It binds to the subject's basis in the Recensor's current machine reviews,
    or to `basis`; an edit names its `text` and `note`.
    """
    reviews = _reviews(root)
    if ":" in subject:
        review = reviews[subject]
        scope, page_id, subject_id = "unit", _page_id(root, review), review["subject_id"]
        current = _machine_basis(review)
    else:
        on_page = [r for key, r in reviews.items() if key.startswith(f"{subject}:")]
        scope, page_id = "page", _page_id(root, on_page[0])
        subject_id = page_id
        current = page_basis_digest(
            page_id, {r["subject_id"]: _machine_basis(r) for r in on_page}, ()
        )
    record = build_review_decision_record(
        run_id=RUN_ID,
        scope=scope,
        subject_id=subject_id,
        page_id=page_id,
        decision=decision,
        finding=finding,
        basis_digest=current if basis is None else basis,
        reason=f"{decision} on {subject}, recorded by the test",
        timestamp=TIMESTAMP,
        text=text,
        note=note,
    )
    reference, _ = RunTree(root, RUN_ID).write_approval_record(record)
    return reference.relative_path


def _bundle(root: Path, clean: Path) -> dict:
    tree = RunTree(root, RUN_ID)
    [export] = [
        tree.read_artifact(ARMARIUM, "export", entry["artifact_id"])
        for entry in tree.build_manifest(ARMARIUM)["artifacts"]
        if entry["kind"] == "export"
    ]
    data = tree.read_bytes(export["payload"]["bundle"]["reference"]["relative_path"])
    with ZipFile(BytesIO(data)) as archive:
        acts = [
            json.loads(line)
            for line in archive.read("acts.jsonl").decode("utf-8").splitlines()
            if line
        ]
    established = {
        tree.read_artifact(ARCHETYPUS, "archetypus", entry["artifact_id"])["payload"]["act_key"]
        for entry in tree.build_manifest(ARCHETYPUS)["artifacts"]
        if entry["kind"] == "archetypus"
    }
    return {
        "manifest": verify_delivered_bundle(data, clean),
        "acts": {row["act_key"]: row for row in acts},
        "established": established,
    }


def test_a_current_decision_releases_held_units_and_the_export_records_it(held, tmp_path):
    tree = _copy(held, tmp_path)
    path = _decide(tree.root, "p1", "no-missed-act")

    assert _recense(tree) == EXIT_COMPLETE
    reviews = _reviews(tree.root)
    for key in ("p1:1", "p1:2"):
        assert (reviews[key]["outcome"], reviews[key]["payload"]["hold_codes"]) == ("accepted", [])
        block = reviews[key]["payload"]["operator_review"]
        assert block["cleared"] == {"unit": [], "page": [UNDER_WITNESSED]}
        assert block["machine_hold_codes"] == [UNDER_WITNESSED]
    assert "operator_review" not in reviews["p2:1"]["payload"]
    decisions = _decisions(tree.root)
    [applied] = decisions["applied"]
    assert f"receipts/sha256/{applied['record_sha256']}.json" == path
    assert decisions["stale"] == decisions["unkept"] == []
    assert [row["cleared"] for row in decisions["clearances"]] == [[UNDER_WITNESSED]]

    _after_recensor(tree)
    bundle = _bundle(tree.root, tmp_path / "clean")
    for key in ("p1:1", "p1:2"):
        assert bundle["acts"][key]["category"] == "delivered"
        assert bundle["acts"][key]["canonical_clean_text"]
        assert key in bundle["established"]
    aggregate = bundle["manifest"]["aggregate"]
    # A person's decision is a reason, never a machine check: the run stays partial.
    assert aggregate["status"] == "partial"
    assert (
        f"page 1 was cleared by an operator review decision (no-missed-act), clearing "
        f"{UNDER_WITNESSED}; a person's decision, not a machine check"
    ) in aggregate["reasons"]


def test_a_stale_decision_raises_its_hold_and_is_not_applied(held, tmp_path):
    tree = _copy(held, tmp_path)
    _decide(tree.root, "p1", "no-missed-act", basis=EARLIER_BASIS)
    _decide(tree.root, "p2:1", "hold", basis=EARLIER_BASIS, finding="text-misread")

    assert _recense(tree) == EXIT_HELD
    reviews = _reviews(tree.root)
    # The stale page decision releases nothing.
    for key in ("p1:1", "p1:2"):
        assert reviews[key]["outcome"] == "held-for-review"
        assert reviews[key]["payload"]["hold_codes"] == [UNDER_WITNESSED]
    # The stale hold still holds the unit it named, carried.
    assert reviews["p2:1"]["outcome"] == "held-for-review"
    assert reviews["p2:1"]["payload"]["hold_codes"] == ["review-hold-carried"]
    decisions = _decisions(tree.root)
    assert decisions["applied"] == [] and decisions["clearances"] == []
    assert sorted(s["decision"] for s in decisions["stale"]) == ["hold", "no-missed-act"]
    assert {s["stale_because"] for s in decisions["stale"]} == {"basis-changed"}
    assert [s["decision"] for s in decisions["carried"]] == ["hold"]

    _after_recensor(tree)
    bundle = _bundle(tree.root, tmp_path / "clean")
    for key in ("p1:1", "p1:2", "p2:1"):
        assert bundle["acts"][key]["category"] == "held-for-review"
        assert bundle["acts"][key]["canonical_clean_text"] is None
        assert key not in bundle["established"]
    assert "act p2:1 is held-for-review" in bundle["manifest"]["aggregate"]["reasons"]


def test_an_exclusion_keeps_the_unit_out_of_the_delivered_text_but_recorded(held, tmp_path):
    tree = _copy(held, tmp_path)
    path = _decide(tree.root, "p1:1", "exclude")

    assert _recense(tree) == EXIT_HELD
    reviews = _reviews(tree.root)
    assert (reviews["p1:1"]["outcome"], reviews["p1:1"]["approval_ref"]) == ("excluded", path)
    # Excluding one act clears no page hold: the page and its other act stay held.
    assert reviews["p1:1"]["payload"]["hold_codes"] == [UNDER_WITNESSED]
    assert reviews["p1:2"]["outcome"] == "held-for-review"

    _after_recensor(tree)
    bundle = _bundle(tree.root, tmp_path / "clean")
    act = bundle["acts"]["p1:1"]
    assert (act["category"], act["approval_ref"]) == ("excluded-with-approval", path)
    assert act["canonical_clean_text"] is None
    assert "p1:1" not in bundle["established"]
    assert bundle["acts"]["p2:1"]["category"] == "delivered"
    reasons = bundle["manifest"]["aggregate"]["reasons"]
    assert (
        "act p1:1 on page 1 was cleared by an operator review decision (exclude), clearing "
        "no machine hold; a person's decision, not a machine check"
    ) in reasons
    assert f"page 1 is still held by {UNDER_WITNESSED}" in reasons


def test_a_page_held_after_every_unit_on_it_was_excluded_holds_the_recensor(held, tmp_path):
    """Excluding both acts on under-witnessed page 1 leaves the page itself held."""
    tree = _copy(held, tmp_path)
    _decide(tree.root, "p1:1", "exclude")
    _decide(tree.root, "p1:2", "exclude")

    assert _recense(tree) == EXIT_HELD
    reviews = _reviews(tree.root)
    assert reviews["p1:1"]["outcome"] == reviews["p1:2"]["outcome"] == "excluded"
    [page] = _decisions(tree.root)["page_holds"]
    assert page["page_ordinal"] == 1 and UNDER_WITNESSED in page["hold_codes"]
    assert held_by_recensor(RunTree(tree.root, RUN_ID)) == [
        {"subject_id": "operator-review", "what": "page 1", "hold_codes": page["hold_codes"]}
    ]
    # The run-health receipt says so too: every unit is completed, the page is not.
    receipt = RunTree(tree.root, RUN_ID).read_recensor_partition_receipt()
    assert receipt["recensor_status"] == "partial"
    assert (
        f"page 1 is held after operator review ({', '.join(page['hold_codes'])})"
        in receipt["reasons"]
    )


def test_a_held_canary_keeps_the_receipt_partial_but_not_the_systemic_count(
    held, tmp_path, monkeypatch
):
    """A canary is a known-answer page, so a held canary is a real signal about the
    run: its page hold stays in the receipt and keeps it partial. It is a control,
    not a page of the register, so the systemic alarm's share leaves it out."""
    tree = _copy(held, tmp_path)
    _decide(tree.root, "p1:1", "exclude")
    _decide(tree.root, "p1:2", "exclude")
    monkeypatch.setattr("common.stage.canary_ordinals", lambda _run: {1})
    monkeypatch.setattr("common.page_review.canary_ordinals", lambda _run: {1})
    recensor = load_stage("5_recensor")
    monkeypatch.setattr(
        sys,
        "argv",
        [recensor.__file__, *stage_argv(tree.root, tree.catalogue, placement_tier=TIER)],
    )

    assert recensor.main() == EXIT_HELD
    [page] = _decisions(tree.root)["page_holds"]
    assert page["page_ordinal"] == 1
    run_tree = RunTree(tree.root, RUN_ID)
    receipt = run_tree.read_recensor_partition_receipt()
    assert receipt["page_holds"] == [page]
    assert receipt["recensor_status"] == "partial"
    assert held_pages_after_review(run_tree) == ([], 1)


def test_decisions_are_re_applied_identically_on_a_re_run(held, tmp_path):
    tree = _copy(held, tmp_path)
    _decide(tree.root, "p1", "no-missed-act")
    _decide(tree.root, "p2:1", "hold", basis=EARLIER_BASIS, finding="text-misread")
    assert _recense(tree) == EXIT_HELD
    _after_recensor(tree)
    reviews = _reviews(tree.root)
    assert (reviews["p1:1"]["outcome"], reviews["p1:1"]["payload"]["attempt_ordinal"]) == (
        "accepted",
        2,
    )
    assert reviews["p2:1"]["payload"]["hold_codes"] == ["review-hold-carried"]
    assert _decisions(tree.root)["attempt_ordinal"] == 1
    before = snapshot(tree.root)

    assert _recense(tree) == EXIT_HELD
    _after_recensor(tree)
    assert snapshot(tree.root) == before


def test_a_decision_recorded_after_the_archetypus_established_is_refused(held, tmp_path):
    tree = _copy(held, tmp_path)
    _after_recensor(tree)
    _decide(tree.root, "p1", "no-missed-act")
    before = snapshot(tree.root / RUN_ID / "5_recensor")

    result = _run(tree, "pipeline/5_recensor/run.py")
    assert result.returncode not in (EXIT_COMPLETE, EXIT_HELD)
    assert "after the Archetypus established 1 reading(s)" in result.stderr
    assert "Record them in a new run of this submission" in result.stderr
    assert snapshot(tree.root / RUN_ID / "5_recensor") == before


def test_a_decision_after_an_archetypus_that_established_nothing_is_applied(held, tmp_path):
    """The Archetypus's seal and index establish nothing, so they do not stop a decision."""
    tree = _copy(held, tmp_path)
    _decide(tree.root, "p2:1", "hold", finding="text-misread")
    assert _recense(tree) == EXIT_HELD
    advance_held_recensor(tree.root, RUN_ID)
    assert _run(tree, TAIL[0]).returncode in (EXIT_COMPLETE, EXIT_HELD)
    assert _bundle_established(tree.root) == set()

    _decide(tree.root, "p1", "no-missed-act")
    assert _recense(tree) == EXIT_HELD
    _after_recensor(tree)
    assert _bundle_established(tree.root) == {"p1:1", "p1:2"}


@pytest.mark.parametrize("program", TAIL[::2])
def test_a_stage_after_the_recensor_refuses_a_decision_its_last_pass_did_not_apply(
    held, tmp_path, program
):
    """A decision recorded after the Recensor's last pass is never silently ignored."""
    tree = _copy(held, tmp_path)
    _after_recensor(tree)
    _decide(tree.root, "p2:1", "hold", finding="text-misread")

    result = _run(tree, program)
    assert result.returncode not in (EXIT_COMPLETE, EXIT_HELD)
    assert "re-run the Recensor" in result.stderr


def test_the_archetypus_refuses_a_held_recensor_no_advance_passes(held, tmp_path):
    """Run directly, the Archetypus establishes nothing over a hold no person has passed."""
    tree = _copy(held, tmp_path)
    _decide(tree.root, "p1:1", "exclude")
    _decide(tree.root, "p1:2", "exclude")
    assert _recense(tree) == EXIT_HELD

    result = _run(tree, TAIL[0])
    assert result.returncode not in (EXIT_COMPLETE, EXIT_HELD)
    assert "no advance record passes its current seal" in result.stderr
    assert not (tree.root / RUN_ID / "6_archetypus").exists()

    _after_recensor(tree)
    assert _bundle_established(tree.root) == {"p2:1"}


def test_the_armarium_refuses_a_held_recensor_no_advance_passes(held, tmp_path):
    """Run directly, the Armarium exports nothing over a hold no person has passed.

    The Archetypus and the Coniector ran under an advance that is then removed,
    so only the Armarium's own entry stands between the hold and an export.
    """
    tree = _copy(held, tmp_path)
    advance_held_recensor(tree.root, RUN_ID)
    [advance] = [
        reference
        for reference, record in RunTree(tree.root, RUN_ID).approval_records()
        if record["action"] == "advance"
    ]
    for program in TAIL[:2]:
        assert _run(tree, program).returncode in (EXIT_COMPLETE, EXIT_HELD)
    (tree.root / RUN_ID / advance.relative_path).unlink()

    result = _run(tree, TAIL[2])
    assert result.returncode not in (EXIT_COMPLETE, EXIT_HELD)
    assert "no advance record passes its current seal" in result.stderr
    assert not (tree.root / RUN_ID / "7_armarium").exists()


def _bundle_established(root: Path) -> set[str]:
    tree = RunTree(root, RUN_ID)
    return {
        tree.read_artifact(ARCHETYPUS, "archetypus", entry["artifact_id"])["payload"]["act_key"]
        for entry in tree.build_manifest(ARCHETYPUS)["artifacts"]
        if entry["kind"] == "archetypus"
    }


def test_a_decision_that_would_release_an_unplaced_reading_keeps_it_held(tmp_path):
    """page-review's p2:1 is unplaced: no region on its page, so no decision can export it.

    The release is stored by hand, since `decide` refuses to record it. The pass
    goes on: the unit stays held under `review-reading-held` with its reading's
    codes and why, the rest of the run's reviews are published, and nothing that
    still holds it is reported as cleared.
    """
    root, options = build_page_tree(tmp_path, "page-review")
    recensor = "pipeline/5_recensor/run.py"
    assert run_stage(root, RUN_ID, "page-review", recensor, **options).returncode == EXIT_HELD
    _decide(root, "p2:1", "release")
    _decide(root, "p2", "no-missed-act")

    result = run_stage(root, RUN_ID, "page-review", recensor, **options)
    assert result.returncode == EXIT_HELD, result.stderr
    review = _reviews(root)["p2:1"]
    assert review["outcome"] == "held-for-review"
    assert READING_HELD in review["payload"]["hold_codes"]
    # The reading's own holds stay. The two ink checks are review flags under the
    # committed `[flags]`, so they were never holds for a decision to clear.
    codes = review["payload"]["hold_codes"]
    assert "reading-unplaced" in codes and "residual-ink" not in codes
    assert review["payload"]["flag_codes"] == ["residual-ink", "unread-ink"]
    assert READING_HELD in review["payload"]["operator_review"]["added"]
    assert "no decision can send that reading to export" in review["payload"]["reason"]
    assert "reading-unplaced row, which has no region on its page" in review["payload"]["reason"]
    decisions = _decisions(root)
    assert decisions["applied"], "the decisions were applied, not refused"
    # Only what took effect is reported cleared: the unit release cleared nothing
    # that still holds it, so no unit clearance reaches the aggregate. The page
    # decision cleared every code of p2's, yet every one of them still holds its
    # reading (the ink checks were flags, not holds), so nothing is reported cleared.
    cleared = review["payload"]["operator_review"]["cleared"]
    assert not set(cleared["unit"] + cleared["page"]) & set(codes)
    assert cleared["page"] == []
    rows = aggregate_clearances(decisions, unit_key="act_key")
    assert rows == []


def test_an_edit_of_an_unplaced_reading_keeps_it_held(tmp_path):
    """A person's text has no region to cite either: the edit corrects nothing, and says why."""
    root, options = build_page_tree(tmp_path, "page-review")
    recensor = "pipeline/5_recensor/run.py"
    assert run_stage(root, RUN_ID, "page-review", recensor, **options).returncode == EXIT_HELD
    _decide(root, "p2:1", "edit", text="SYNTHETIC ACT TWO as a person reads it")
    _decide(root, "p2", "no-missed-act")

    result = run_stage(root, RUN_ID, "page-review", recensor, **options)
    assert result.returncode == EXIT_HELD, result.stderr
    review = _reviews(root)["p2:1"]
    assert review["outcome"] == "held-for-review"
    assert {READING_HELD, "reading-unplaced"} <= set(review["payload"]["hold_codes"])
    assert "reading-unplaced row, which has no region on its page" in review["payload"]["reason"]
    decisions = _decisions(root)
    assert [s["decision"] for s in decisions["applied"] if s["scope"] == "unit"] == ["edit"]
    assert decisions["corrections"] == []
