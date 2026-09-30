"""The Recensor's page path: one review per unit a page-read run counts.

The trees are the synthetic fixture's `happy`, `page-review` and `page-no-act`
scenarios read with `reading_unit = "page"`. The fixture seals two page
witnesses (Chandra and Churro; DAI is act-scoped), so the trees that should be
accepted are sealed with a witness floor of 2, and the default floor of 3 is the
shortfall case. The fixture has no record detector, so page accounting rule (i)
does not apply on any page.
"""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

import pytest

from common.contracts.canonical import canonical_bytes, self_hash
from common.contracts.errors import FatalAccounting
from common.contracts.identities import artifact_id
from common.contracts.stages import EXEMPLAR, PERLECTOR, RECENSOR
from common.page_review import CONTINUATION_LINK_FIELDS
from common.runtree.store import RunTree
from common.stage import PAGE_BLANK_HOLD, open_context, reading_acts, stage_parser
from conftest import (
    build_page_tree,
    file_bytes_snapshot,
    load_stage,
    reaccount_page,
    rewitness_stage_boundary,
    rewrite_page_reading,
    run_stage,
)

ROOT = Path(__file__).resolve().parents[2]
RUN_ID = "r"
RECENSOR_PROGRAM = "pipeline/5_recensor/run.py"
page_review = load_stage("5_recensor", "page_review")


# --- trees -------------------------------------------------------------------------


class Tree:
    """A page-read run through the Perlector, and the options every stage of it takes."""

    def __init__(self, root: Path, scenario: str, options: dict[str, Path]) -> None:
        self.root, self.scenario, self.options = root, scenario, options

    def copy(self, directory: Path) -> "Tree":
        shutil.copytree(self.root, directory / "runs")
        return Tree(directory / "runs", self.scenario, self.options)

    def recensor(self) -> subprocess.CompletedProcess[str]:
        return run_stage(self.root, RUN_ID, self.scenario, RECENSOR_PROGRAM, **self.options)

    def context(self, stage: str = RECENSOR):
        argv = ["--run-root", str(self.root), "--run-id", RUN_ID, "--scenario", self.scenario]
        for name, value in self.options.items():
            argv += [f"--{name.replace('_', '-')}", str(value)]
        return open_context(stage_parser("page review").parse_args(argv), stage)

    def records(self, stage_dir: str, kind: str) -> list[dict[str, Any]]:
        directory = self.root / RUN_ID / stage_dir / "artifacts" / kind
        return [json.loads(path.read_text("utf-8")) for path in sorted(directory.glob("*.json"))]

    def reviews(self) -> dict[str, dict[str, Any]]:
        return {r["payload"]["act_key"]: r for r in self.records("5_recensor", "review")}

    def receipt(self) -> dict[str, Any]:
        path = self.root / RUN_ID / "run-health" / "recensor-partition-receipt.json"
        return json.loads(path.read_text("utf-8"))


def _tree(base: Path, scenario: str, floor: int | None) -> Tree:
    root, options = build_page_tree(base, scenario, RUN_ID, floor=floor)
    return Tree(root, scenario, options)


@pytest.fixture(scope="module")
def happy(tmp_path_factory) -> Tree:
    return _tree(tmp_path_factory.mktemp("happy"), "happy", floor=2)


@pytest.fixture(scope="module")
def review(tmp_path_factory) -> Tree:
    return _tree(tmp_path_factory.mktemp("page-review"), "page-review", floor=2)


@pytest.fixture(scope="module")
def no_act(tmp_path_factory) -> Tree:
    return _tree(tmp_path_factory.mktemp("page-no-act"), "page-no-act", floor=2)


@pytest.fixture(scope="module")
def under_floor(tmp_path_factory) -> Tree:
    return _tree(tmp_path_factory.mktemp("floor"), "happy", floor=None)


def _forge(tree: Tree, kind: str, ordinal: int, change: Callable[[dict], None]) -> None:
    directory = tree.root / RUN_ID / "4_perlector" / "artifacts" / kind
    [(path, record)] = [
        (path, record)
        for path in directory.glob("*.json")
        if (record := json.loads(path.read_text("utf-8")))["payload"]["page_ordinal"] == ordinal
    ]
    forged = copy.deepcopy(record)
    change(forged)
    forged["self_hash"] = self_hash({k: v for k, v in forged.items() if k != "self_hash"})
    path.write_bytes(canonical_bytes(forged))


def _forge_page_two(tree: Tree, reading: Callable[[dict], None]) -> None:
    """Replace page 2's answer, drop its act records, and measure its accounting again.

    The accounting is measured as the denominator measures it, so the page
    carries the accounting stage 4 would have written for the forged answer.
    """
    rewrite_page_reading(tree.root, RUN_ID, 2, reading)
    reaccount_page(tree.root, RUN_ID, tree.scenario, tree.options, 2)


def _on(record: dict, ordinal: int) -> bool:
    return record["payload"]["page_ordinal"] == ordinal


# --- accepted -----------------------------------------------------------------------


def test_a_happy_page_tree_accepts_every_unit_with_its_evidence(happy, tmp_path):
    tree = happy.copy(tmp_path)
    result = tree.recensor()
    assert result.returncode == 0, result.stderr
    reviews = tree.reviews()
    context = tree.context()
    ink_map_digest = context.sealed_config_digests["ink-map"]
    rows = {row["act_key"]: row for row in reading_acts(context)}
    assert sorted(reviews) == sorted(rows) == ["p1:1", "p1:2", "p2:1"]
    for key, review in reviews.items():
        payload, row = review["payload"], rows[key]
        assert review["outcome"] == "accepted" and review["subject_id"] == row["act_id"]
        assert set(payload) == page_review.PAGE_REVIEW_FIELDS | {"attempt_ordinal"}
        assert (payload["hold_codes"], payload["release"], payload["confirmation"]) == (
            [],
            None,
            None,
        )
        assert payload["recoveries_used"] == 0 and payload["attempt_ordinal"] == 1
        assert (payload["unit_class"], payload["kind"]) == ("reading", "act")
        assert payload["perlectio_ref"] == row["perlectio_ref"]
        assert payload["act_region_ref"] == row["region_ref"]
        assert payload["page_reading_ref"] == row["reading_ref"]
        assert payload["page_accounting_ref"] == row["accounting_ref"]
        page_coverage = payload["page_coverage"]
        assert {key: page_coverage[key] for key in page_coverage if key != "ink"} == {
            "checked_pages": [row["page_ordinal"]],
            "flagged_pages": [],
            "unmeasurable_pages": [],
        }
        ink = page_coverage["ink"]
        assert ink["outside_ink_pixels"] == 0 < ink["total_ink_pixels"] <= ink["page_ink_pixels"]
        assert isinstance(ink["background_level"], int)
        assert ink["ink_map_config_sha256"] == ink_map_digest
        exemplar_page = context.artifact_ref(
            EXEMPLAR, "page", artifact_id(EXEMPLAR, "page", row["page_id"])
        )
        assert exemplar_page in review["inputs"]
        assert payload["notes"] == []
        coverage = payload["coverage"]
        assert (coverage["configured"], coverage["floor"], coverage["by_outcome"]) == (
            2,
            2,
            {"read": 2},
        )
        assert coverage["under_witnessed"] is False
        assert payload["continuation"] == {
            "continues_from_previous_page": row["continues_from_previous_page"],
            "continues_to_next_page": row["continues_to_next_page"],
        }
        assert payload["uncertainty_assessment"]["state"] is not None
        assert row["perlectio_ref"] in review["inputs"]
    assert not (tree.root / RUN_ID / "5_recensor" / "artifacts" / "recovery-request").exists()

    receipt = tree.receipt()
    assert receipt["schema"] == "recensor-partition-receipt.v3"
    assert receipt["recensor_status"] == "complete" and receipt["reasons"] == []
    assert len(receipt["page_reading_refs"]) == 2
    assert receipt["expected_unit_count"] == 3 and "expected_act_count" not in receipt
    assert {item["page_disposition"] for item in receipt["items"]} == {"read"}
    [link] = receipt["continuation_links"]
    assert (link["subject_id"], link["outcome"]) == ("page-break:1:2", "accepted")

    # A second pass reuses every record byte for byte.
    before = file_bytes_snapshot(tree.root)
    assert tree.recensor().returncode == 0
    assert file_bytes_snapshot(tree.root) == before


def test_an_agreed_page_break_is_one_accepted_continuation_link(happy, tmp_path):
    tree = happy.copy(tmp_path)
    assert tree.recensor().returncode == 0
    [link] = tree.records("5_recensor", "continuation-link")
    reviews = tree.reviews()
    assert link["outcome"] == "accepted" and link["subject_id"] == "page-break:1:2"
    assert link["payload"] == {
        "schema": "recensor-continuation-link.v1",
        "from_page_ordinal": 1,
        "to_page_ordinal": 2,
        "from_act_id": reviews["p1:2"]["subject_id"],
        "from_act_key": "p1:2",
        "to_act_id": reviews["p2:1"]["subject_id"],
        "to_act_key": "p2:1",
        "continues_to_next_page": True,
        "continues_from_previous_page": True,
        "agreed": True,
    }
    assert link["inputs"] == [
        reviews["p1:2"]["payload"]["perlectio_ref"],
        reviews["p2:1"]["payload"]["perlectio_ref"],
    ]


# --- held -----------------------------------------------------------------------------


def test_the_page_review_scenario_holds_the_unplaced_entry_naming_every_reason(review, tmp_path):
    tree = review.copy(tmp_path)
    assert tree.recensor().returncode == 3
    reviews = tree.reviews()
    assert {key: r["outcome"] for key, r in reviews.items()} == {
        "p1:1": "accepted",
        "p1:2": "accepted",
        "p2:1": "held-for-review",
    }
    held = reviews["p2:1"]["payload"]
    assert held["unit_class"] == "reading-unplaced" and held["act_region_ref"] is not None
    assert held["hold_codes"] == [
        "reading-unplaced",
        "residual-ink",
        "truncation-not-classified",
        "unread-ink",
        "unread-line",
    ]
    assert held["page_coverage"]["flagged_pages"] == [2]
    for code in ("reading-unplaced", "truncation-not-classified", "unread-ink", "unread-line"):
        assert code in held["reason"]
    assert "ink outside every reading region" in held["reason"]
    receipt = tree.receipt()
    assert receipt["recensor_status"] == "partial"
    assert receipt["reasons"] == [
        f"unit {reviews['p2:1']['subject_id']} was held by its page reading",
        f"unit {reviews['p2:1']['subject_id']} is unresolved at the Recensor",
    ]


def test_a_page_under_the_witness_floor_holds_every_unit_on_it(under_floor, tmp_path):
    tree = under_floor.copy(tmp_path)
    assert tree.recensor().returncode == 3
    for review in tree.reviews().values():
        assert review["outcome"] == "held-for-review"
        assert review["payload"]["hold_codes"] == ["under-witnessed"]
        assert "2 page witness(es) read page" in review["payload"]["reason"]
        assert "against a floor of 3" in review["payload"]["reason"]
    reasons = tree.receipt()["reasons"]
    assert sum("under-witnessed (2 page reads of a floor of 3)" in r for r in reasons) == 3


def test_an_unread_page_is_one_held_unit_and_its_break_is_one_sided(happy, tmp_path):
    tree = happy.copy(tmp_path)

    def malformed(record):
        record["outcome"] = "held"
        record["payload"].update(
            parse_state="malformed",
            answer=None,
            problems=[{"code": "json-invalid", "detail": "the reply is not JSON"}],
            disposition="held",
        )

    _forge_page_two(tree, malformed)
    assert tree.recensor().returncode == 3
    reviews = tree.reviews()
    assert sorted(reviews) == ["p1:1", "p1:2", "p2:unread"]
    unread = reviews["p2:unread"]
    assert unread["outcome"] == "held-for-review"
    payload = unread["payload"]
    assert payload["unit_class"] == "page-unread"
    assert payload["hold_codes"] == [
        "json-invalid",
        "page-answer-incomplete",
        "page-unread",
        "residual-ink",
    ]
    assert payload["perlectio_ref"] is None and payload["act_region_ref"] is None
    assert payload["continuation"] == {
        "continues_from_previous_page": None,
        "continues_to_next_page": None,
    }
    # Page 1's last act says it runs on; page 2 has no entry to agree.
    [link] = tree.records("5_recensor", "continuation-link")
    assert link["outcome"] == "held-for-review"
    assert link["payload"]["agreed"] is False
    assert (link["payload"]["from_act_key"], link["payload"]["to_act_id"]) == ("p1:2", None)
    # Its null side is bound by that page's reading.
    assert sorted(link["inputs"], key=str) == sorted(
        [reviews["p1:2"]["payload"]["perlectio_ref"], payload["page_reading_ref"]], key=str
    )
    # The link holds no act, but the run cannot report complete over it.
    assert reviews["p1:2"]["outcome"] == "accepted"
    receipt = tree.receipt()
    assert receipt["recensor_status"] == "partial"
    assert receipt["continuation_links"][0]["outcome"] == "held-for-review"
    assert (
        "page-break:1:2 is held-for-review: only one side says the text runs across the page "
        "break" in receipt["reasons"]
    )


def test_a_page_read_as_blank_with_ink_and_witness_text_is_not_confirmed(happy, tmp_path):
    tree = happy.copy(tmp_path)
    feed_dir = tree.root / RUN_ID / "4_perlector" / "artifacts" / "page-feed"
    [feed] = [
        record
        for path in feed_dir.glob("*.json")
        if (record := json.loads(path.read_text("utf-8")))["payload"]["page_ordinal"] == 2
    ]
    ids = [unit["id"] for witness in feed["payload"]["witnesses"] for unit in witness["units"]]
    ids += [line["id"] for line in feed["payload"]["surya"]["lines"]]
    ids += [block["id"] for block in feed["payload"]["surya"]["blocks"]]

    def blank(record):
        record["payload"]["answer"] = {
            "acts": [],
            "set_aside": [{"id": identifier, "reason": "blank paper"} for identifier in ids],
        }

    _forge_page_two(tree, blank)
    assert tree.recensor().returncode == 3
    review = tree.reviews()["p2:blank"]
    payload = review["payload"]
    assert review["outcome"] == "held-for-review" and payload["unit_class"] == "page-blank"
    rows = {row["act_key"]: row for row in reading_acts(tree.context())}
    assert PAGE_BLANK_HOLD in rows["p2:blank"]["hold_codes"]
    assert payload["hold_codes"] == sorted({*rows["p2:blank"]["hold_codes"], "residual-ink"})
    assert payload["release"] is None
    confirmation = payload["confirmation"]
    assert confirmation["confirms"] == "page-blank" and confirmation["confirmed"] is False
    assert "Surya detected 3 line(s) on the page" in confirmation["failures"]
    assert {w["chair"] for w in confirmation["witnesses"]} == {"attestator_1", "attestator_3"}
    assert all(
        f"witness {chair} read the page and its retained text is not blank"
        in confirmation["failures"]
        for chair in ("attestator_1", "attestator_3")
    )
    # No record detector: rule (i) does not apply to a blank page, and is no failure.
    assert confirmation["rules"]["i"] == "not-applicable"
    assert not any("rule (i)" in failure for failure in confirmation["failures"])
    assert "the page is not confirmed to hold no act" in payload["reason"]


def test_a_page_of_other_entries_is_held_unconfirmed_without_a_record_detector(no_act, tmp_path):
    tree = no_act.copy(tmp_path)
    assert tree.recensor().returncode == 3
    reviews = tree.reviews()
    assert {key: r["outcome"] for key, r in reviews.items()} == {
        "p1:1": "accepted",
        "p1:2": "accepted",
        "p2:1": "held-for-review",
    }
    payload = reviews["p2:1"]["payload"]
    assert (payload["unit_class"], payload["kind"]) == ("reading", "other")
    assert payload["hold_codes"] == [page_review.NO_ACT_HOLD]
    assert payload["release"] is None
    confirmation = payload["confirmation"]
    assert confirmation["confirms"] == "no-act-on-page" and confirmation["confirmed"] is False
    assert confirmation["rules"]["i"] == "not-applicable"
    assert confirmation["failures"] == ["page accounting rule (i) is not-applicable, not pass"]
    # The `other` entry's own continuation flag is a note, never a side of a break.
    assert payload["notes"] == [
        {"code": page_review.CONTINUATION_ON_OTHER, "flags": ["continues_from_previous_page"]}
    ]
    [link] = tree.records("5_recensor", "continuation-link")
    assert link["outcome"] == "held-for-review"
    assert (link["payload"]["from_act_key"], link["payload"]["to_act_id"]) == ("p1:2", None)
    receipt = tree.receipt()
    assert receipt["recensor_status"] == "partial"
    assert (
        f"unit {reviews['p2:1']['subject_id']} is unresolved at the Recensor"
        in (receipt["reasons"])
    )


# --- refusals ---------------------------------------------------------------------------


def test_a_forged_page_accounting_is_refused_before_any_review(review, tmp_path):
    tree = review.copy(tmp_path)
    _forge(
        tree,
        "page-accounting",
        2,
        lambda record: (record.update(outcome="read"), record["payload"].update(holds=[])),
    )
    rewitness_stage_boundary(RunTree(tree.root, RUN_ID), PERLECTOR)
    result = tree.recensor()
    assert result.returncode == 2
    assert "FatalAccounting" in result.stderr
    assert not (tree.root / RUN_ID / "5_recensor" / "artifacts").exists()


def test_the_receipt_refuses_a_review_outside_the_reading_acts(happy, tmp_path):
    tree = happy.copy(tmp_path)
    assert tree.recensor().returncode == 0
    context = tree.context()
    stray = "act_00000000000000aa"
    context.publish(
        kind="review",
        subject_id=stray,
        outcome="accepted",
        attempt=page_review.attempt_id(stray, "recense", 1),
        payload={"act_key": "p9:1"},
    )
    context.finish()
    with pytest.raises(FatalAccounting, match="outside this page-read run's reading_acts"):
        page_review.write_reading_receipt(context)


def _later_review(context, review: dict, **changes) -> None:
    """Publish attempt 2 of a unit's review, its payload changed as given."""
    subject = review["subject_id"]
    payload = {**copy.deepcopy(review["payload"]), "attempt_ordinal": 2}
    outcome = changes.pop("outcome", review["outcome"])
    payload.update(changes)
    context.publish(
        kind="review",
        subject_id=subject,
        outcome=outcome,
        attempt=page_review.attempt_id(subject, "recense", 2),
        inputs=review["inputs"],
        payload=payload,
    )


def _drop(tree: Tree, kind: str) -> None:
    directory = tree.root / RUN_ID / "5_recensor" / "artifacts" / kind
    next(iter(sorted(directory.glob("*.json")))).unlink()


def _stray_link(context, reviews) -> None:
    context.publish(
        kind="continuation-link",
        subject_id="page-break:2:3",
        outcome="held-for-review",
        attempt=page_review.attempt_id("page-break:2:3", "link", 1),
        payload={"agreed": False},
    )


@pytest.mark.parametrize(
    ("forge", "refusal"),
    [
        (
            lambda tree, context, reviews: context.publish(
                kind="recovery-request",
                subject_id=reviews["p1:1"]["subject_id"],
                outcome="recovery-requested",
                attempt=page_review.attempt_id(reviews["p1:1"]["subject_id"], "recover", 1),
                payload={},
            ),
            "asks for no recovery",
        ),
        (lambda tree, context, reviews: _drop(tree, "review"), "has no Recensor review"),
        (
            lambda tree, context, reviews: _later_review(
                context,
                reviews["p1:1"],
                coverage={**reviews["p1:1"]["payload"]["coverage"], "floor": 1},
            ),
            "witness coverage recomputed from disk",
        ),
        (
            lambda tree, context, reviews: _later_review(
                context, reviews["p1:1"], outcome="held-for-review"
            ),
            "derive 'accepted'",
        ),
        (
            lambda tree, context, reviews: _later_review(
                context, reviews["p1:1"], hold_codes=["under-witnessed"], outcome="held-for-review"
            ),
            "but disk derives",
        ),
        (lambda tree, context, reviews: _drop(tree, "continuation-link"), "no continuation-link"),
        (lambda tree, context, reviews: _stray_link(context, reviews), "no answer of this run"),
    ],
    ids=[
        "recovery-request",
        "missing-review",
        "coverage",
        "outcome",
        "own-code",
        "missing-link",
        "stray-link",
    ],
)
def test_the_receipt_refuses_a_review_set_disk_does_not_derive(happy, tmp_path, forge, refusal):
    tree = happy.copy(tmp_path)
    assert tree.recensor().returncode == 0
    context = tree.context()
    forge(tree, context, tree.reviews())
    context.finish()
    with pytest.raises(FatalAccounting, match=refusal):
        page_review.write_reading_receipt(context)


def test_the_receipt_refuses_an_accepted_review_of_a_held_row_with_no_release(review, tmp_path):
    tree = review.copy(tmp_path)
    assert tree.recensor().returncode == 3
    context = tree.context()
    held = tree.reviews()["p2:1"]
    _later_review(context, held, outcome="accepted", hold_codes=[])
    context.finish()
    with pytest.raises(FatalAccounting, match="without naming a release"):
        page_review.write_reading_receipt(context)


# --- the review and its parts, over rows ------------------------------------------------


def _row(**overrides) -> dict[str, Any]:
    row = {
        "act_id": "act_0000000000000001",
        "act_key": "p1:1",
        "page_id": "pg_0000000000000001",
        "page_ordinal": 1,
        "n": 1,
        "kind": "act",
        "class": "reading",
        "disposition": "read",
        "region_ref": {"relative_path": "region", "sha256": "a" * 64},
        "reading_ref": {"relative_path": "reading", "sha256": "b" * 64},
        "perlectio_ref": {"relative_path": "perlectio", "sha256": "c" * 64},
        "accounting_ref": {"relative_path": "accounting", "sha256": "d" * 64},
        "hold_codes": [],
        "continues_from_previous_page": False,
        "continues_to_next_page": False,
    }
    row.update(overrides)
    return row


def _testimonium(chair: str, outcome: str = "read", text: Any = "text", **health) -> dict:
    return {
        "outcome": outcome,
        "payload": {"chair": chair, "payload": text, "content_health": health},
    }


CLEAN = {"checked_pages": [1], "flagged_pages": [], "unmeasurable_pages": [], "ink": None}
PASSING = {"rules": {rule: {"status": "pass"} for rule in "defi"}, "lines": []}


def _coverage(*records, floor: int = 2, chairs=None) -> dict[str, Any]:
    roster = {r["payload"]["chair"] for r in records} if chairs is None else set(chairs)
    return page_review.page_witness_coverage(list(records), floor, roster)


def test_the_page_witness_coverage_counts_page_reads_against_the_floor():
    coverage = _coverage(
        _testimonium("a", truncated=False),
        _testimonium("b", "genuinely-empty"),
        _testimonium("c", "failed"),
        floor=3,
    )
    assert coverage["under_witnessed"] is True
    assert coverage["by_outcome"] == {"read": 1, "genuinely-empty": 1, "failed": 1}
    assert coverage["shortfalls"] == {"failed": 1, "truncated": 0, "unaligned": 0}
    assert coverage["health_unrecorded"] == 1


def test_a_truncated_page_reading_does_not_count_toward_the_floor():
    records = [_testimonium("a", truncated=False), _testimonium("b", truncated=True)]
    coverage = _coverage(*records, floor=2)
    assert coverage["by_outcome"] == {"read": 2}
    assert coverage["shortfalls"]["truncated"] == 1
    assert coverage["under_witnessed"] is True
    [(code, sentence)] = page_review.coverage_findings(coverage, 1)
    assert code == "under-witnessed"
    assert sentence.startswith("1 page witness(es) read page 1 against a floor of 2")
    assert "(1 truncated reading(s) not counted)" in sentence
    assert _coverage(*records, floor=1)["under_witnessed"] is False


def test_the_configured_count_is_the_sealed_page_roster_not_the_chairs_that_testified():
    coverage = _coverage(_testimonium("a", truncated=False), floor=1, chairs={"a", "b"})
    assert coverage["configured"] == 2
    assert coverage["by_outcome"] == {"read": 1, "not-run": 1}
    assert coverage["unresolved_chairs"] == 1
    # A page no witness testified to: every roster chair is unresolved there.
    empty = _coverage(floor=2, chairs={"a", "b"})
    assert (empty["configured"], empty["unresolved_chairs"], empty["under_witnessed"]) == (
        2,
        2,
        True,
    )


def test_blankness_is_measured_from_the_retained_text_not_the_health_report():
    assert page_review.retained_text_blank(_testimonium("a", text=" \n\t")) is True
    assert page_review.retained_text_blank(_testimonium("a", text="ink", blank=True)) is False
    assert page_review.retained_text_blank(_testimonium("a", text={"records": []})) is None


BLANK_WITNESSES = [
    _testimonium("a", text="", blank=True),
    _testimonium("b", "genuinely-empty", text=""),
]


def _without(rule: str, status: str) -> dict:
    return {**PASSING, "rules": {**PASSING["rules"], rule: {"status": status}}}


def test_a_blank_page_meeting_every_condition_is_confirmed_and_released():
    confirmed = page_review.confirmation(PASSING, BLANK_WITNESSES, blank=True)
    assert confirmed["confirmed"] is True and confirmed["failures"] == []
    # With no record detector, rule (i) does not apply to a blank page.
    assert page_review.confirmation(_without("i", "not-applicable"), BLANK_WITNESSES, blank=True)[
        "confirmed"
    ]
    outcome, payload = page_review.review_of(
        _row(act_key="p1:blank", n=None, **{"class": "page-blank"}, hold_codes=[PAGE_BLANK_HOLD]),
        coverage=_coverage(*BLANK_WITNESSES),
        page_coverage=CLEAN,
        assessment=None,
        confirmed=confirmed,
    )
    assert outcome == "confirmed-blank" and payload["hold_codes"] == []
    assert payload["release"]["hold_codes"] == [PAGE_BLANK_HOLD]
    assert "every witness that read the page retained blank text" in payload["release"]["reason"]


@pytest.mark.parametrize(
    ("accounting", "records", "failure"),
    [
        (_without("d", "not-measured"), BLANK_WITNESSES, "rule (d) is not-measured, not pass"),
        (_without("e", "hold"), BLANK_WITNESSES, "rule (e) is hold, not pass"),
        (_without("f", "hold"), BLANK_WITNESSES, "rule (f) is hold, not pass"),
        (
            _without("i", "not-measured"),
            BLANK_WITNESSES,
            "rule (i) is not-measured, not not-applicable or pass",
        ),
        ({**PASSING, "lines": [{"id": "L1"}]}, BLANK_WITNESSES, "Surya detected 1 line(s)"),
        (
            PASSING,
            [BLANK_WITNESSES[0], _testimonium("b", text="SYNTHETIC", blank=True)],
            "witness b read the page and its retained text is not blank",
        ),
        (
            PASSING,
            [BLANK_WITNESSES[0], _testimonium("b", text={"records": []})],
            "witness b read the page and its retained text is not blank",
        ),
        (
            PASSING,
            [_testimonium("a", "failed", text=None), _testimonium("b", "not-run", text=None)],
            "no witness read the page",
        ),
    ],
    ids=["d", "e", "f", "i", "lines", "witness-text", "text-unmeasurable", "no-reader"],
)
def test_a_blank_page_failing_one_condition_is_held_naming_it(accounting, records, failure):
    refused = page_review.confirmation(accounting, records, blank=True)
    assert refused["confirmed"] is False
    assert len(refused["failures"]) == 1 and failure in refused["failures"][0]


def test_a_confirmed_blank_page_still_holds_on_the_floor_or_residual_ink():
    witnesses = [_testimonium("a", text="")]
    row = _row(n=None, **{"class": "page-blank"}, hold_codes=[PAGE_BLANK_HOLD])
    confirmed = page_review.confirmation(PASSING, witnesses, blank=True)
    for coverage, page_coverage, code in (
        (_coverage(*witnesses, floor=2), CLEAN, "under-witnessed"),
        (_coverage(*witnesses, floor=1), {**CLEAN, "flagged_pages": [1]}, "residual-ink"),
    ):
        outcome, payload = page_review.review_of(
            row,
            coverage=coverage,
            page_coverage=page_coverage,
            assessment=None,
            confirmed=confirmed,
        )
        assert outcome == "held-for-review" and payload["release"] is None
        assert payload["hold_codes"] == sorted([PAGE_BLANK_HOLD, code])


def test_a_page_of_only_other_readings_is_confirmed_as_holding_no_act():
    row = _row(kind="other", hold_codes=[page_review.NO_ACT_HOLD], disposition="held")
    witnesses = [_testimonium("a"), _testimonium("b")]
    confirmed = page_review.confirmation(PASSING, witnesses, blank=False)
    assert confirmed["confirmed"] is True
    outcome, payload = page_review.review_of(
        row,
        coverage=_coverage(*witnesses),
        page_coverage=CLEAN,
        assessment=None,
        confirmed=confirmed,
    )
    assert outcome == "accepted" and payload["release"]["hold_codes"] == [page_review.NO_ACT_HOLD]
    assert "so does rule (i)" in payload["release"]["reason"]


@pytest.mark.parametrize(
    ("rule", "status"),
    [("d", "hold"), ("e", "hold"), ("f", "not-measured"), ("i", "hold"), ("i", "not-applicable")],
)
def test_a_page_of_only_other_readings_stays_held_on_any_rule_not_passing(rule, status):
    row = _row(kind="other", hold_codes=[page_review.NO_ACT_HOLD], disposition="held")
    witnesses = [_testimonium("a"), _testimonium("b")]
    unconfirmed = page_review.confirmation(_without(rule, status), witnesses, blank=False)
    assert unconfirmed["failures"] == [f"page accounting rule ({rule}) is {status}, not pass"]
    outcome, payload = page_review.review_of(
        row,
        coverage=_coverage(*witnesses),
        page_coverage=CLEAN,
        assessment=None,
        confirmed=unconfirmed,
    )
    assert outcome == "held-for-review" and payload["hold_codes"] == [page_review.NO_ACT_HOLD]


def test_a_refused_pages_row_is_not_a_counted_unit():
    rows = [
        _row(act_id="a1"),
        _row(
            act_id=None, act_key="p2:refused", page_ordinal=2, n=None, **{"class": "page-refused"}
        ),
    ]
    assert [row["act_id"] for row in page_review.counted_units({"acts": rows})] == ["a1"]


def test_a_held_row_is_never_released_by_this_stage():
    row = _row(
        hold_codes=[PAGE_BLANK_HOLD, "unread-ink"], disposition="held", **{"class": "page-blank"}
    )
    witnesses = [_testimonium("a", text="")]
    confirmed = page_review.confirmation(PASSING, witnesses, blank=True)
    outcome, payload = page_review.review_of(
        row,
        coverage=_coverage(*witnesses, floor=1),
        page_coverage=CLEAN,
        assessment=None,
        confirmed=confirmed,
    )
    assert outcome == "held-for-review" and payload["release"] is None
    assert payload["hold_codes"] == [PAGE_BLANK_HOLD, "unread-ink"]


def test_every_finding_of_this_stage_is_named():
    witnesses = [_testimonium("a", truncated=False)]
    for page_coverage, assessment, code in (
        (
            {**CLEAN, "checked_pages": [], "unmeasurable_pages": [1]},
            None,
            "residual-ink-not-measurable",
        ),
        ({**CLEAN, "checked_pages": []}, None, "residual-ink-not-measured"),
        (CLEAN, {"state": "malformed", "problem": "bad"}, "uncertainty-assessment-malformed"),
        (CLEAN, None, "unresolved-witness"),
    ):
        records = (
            witnesses
            if code != "unresolved-witness"
            else [*witnesses, _testimonium("b", "not-run")]
        )
        outcome, payload = page_review.review_of(
            _row(),
            coverage=_coverage(*records, floor=1),
            page_coverage=page_coverage,
            assessment=assessment,
            confirmed=None,
        )
        assert outcome == "held-for-review" and payload["hold_codes"] == [code]


def test_the_page_coverage_carries_the_ink_measured_and_its_policy():
    measured = {
        "background": {"background_level": 231},
        "page_ink_pixels": 900,
        "page_spanning_ink_pixels": 100,
        "total_ink_pixels": 800,
        "outside_ink_pixels": 0,
        "flagged": False,
        "background_config_sha256": "e" * 64,
    }
    assert page_review.page_coverage_of(1, {1: measured})["ink"] == {
        "page_ink_pixels": 900,
        "page_spanning_ink_pixels": 100,
        "total_ink_pixels": 800,
        "outside_ink_pixels": 0,
        "background_level": 231,
        "ink_map_config_sha256": "e" * 64,
    }
    refused = {
        "ink_measurable": False,
        "background_refusal": "no paper value",
        "background_config_sha256": "e" * 64,
    }
    coverage = page_review.page_coverage_of(1, {1: refused})
    assert coverage["unmeasurable_pages"] == [1] and coverage["ink"] == {
        "background_refusal": "no paper value",
        "ink_map_config_sha256": "e" * 64,
    }
    assert page_review.page_coverage_of(2, {1: measured})["ink"] is None


class _Context:
    def artifact_ref(self, stage, kind, artifact) -> dict[str, str]:
        return {"relative_path": f"{stage}/{kind}/{artifact}.json", "sha256": "f" * 64}


def test_the_floor_counts_only_the_testimonia_the_page_accounting_measured():
    records = [
        {"artifact_id": "t1", "payload": {"chair": "a"}},
        {"artifact_id": "t2", "payload": {"chair": "b"}},
    ]
    context = _Context()
    measured = [context.artifact_ref(page_review.ATTESTATORES, "page-testimonium", "t1")]
    page_review._require_accounted_testimonia(
        context,
        _row(),
        {"inputs": [*measured, context.artifact_ref("x", "page-testimonium", "t2")]},
        records[:1],
    )
    with pytest.raises(FatalAccounting, match="current page Testimonium from b is not one"):
        page_review._require_accounted_testimonia(context, _row(), {"inputs": measured}, records)


# --- the outcome the receipt recomputes ------------------------------------------------------


def _review(outcome: str, **payload) -> dict[str, Any]:
    base = {"hold_codes": [], "release": None, "confirmation": None}
    return {"outcome": outcome, "payload": {**base, **payload}}


FLOORED = _coverage(_testimonium("a", truncated=False), floor=1)


def test_the_receipt_derives_each_outcome_from_the_row_and_the_release():
    derive = page_review.require_derived_outcome
    derive(_row(), _review("accepted"), FLOORED, [])
    held_row = _row(hold_codes=["unread-ink"])
    derive(held_row, _review("held-for-review", hold_codes=["unread-ink"]), FLOORED, [])
    with pytest.raises(FatalAccounting, match="without naming a release"):
        derive(held_row, _review("accepted"), FLOORED, [])
    no_act = _row(kind="other", hold_codes=[page_review.NO_ACT_HOLD])
    release = {"hold_codes": [page_review.NO_ACT_HOLD], "reason": "confirmed"}
    derive(
        no_act, _review("accepted", release=release, confirmation={"confirmed": True}), FLOORED, []
    )
    with pytest.raises(FatalAccounting, match="releases hold codes"):
        derive(
            no_act,
            _review("accepted", release=release, confirmation={"confirmed": False}),
            FLOORED,
            [],
        )
    with pytest.raises(FatalAccounting, match="releases hold codes"):
        derive(
            held_row,
            _review(
                "accepted", release={"hold_codes": ["unread-ink"]}, confirmation={"confirmed": True}
            ),
            FLOORED,
            [],
        )
    with pytest.raises(FatalAccounting, match="derive 'held-for-review'"):
        derive(held_row, _review("accepted", hold_codes=["unread-ink"]), FLOORED, [])
    with pytest.raises(FatalAccounting, match="neither its row nor this stage names"):
        derive(_row(), _review("held-for-review", hold_codes=["made-up"]), FLOORED, [])
    with pytest.raises(FatalAccounting, match="but disk derives"):
        derive(_row(), _review("accepted"), FLOORED, ["continues_to_next_page"])


# --- continuation --------------------------------------------------------------------------


def _link_row(
    ordinal: int,
    n: int,
    *,
    previous: bool = False,
    following: bool = False,
    kind: str = "act",
) -> dict:
    return _row(
        act_id=f"act_{ordinal:08d}{n:08d}",
        act_key=f"p{ordinal}:{n}",
        page_ordinal=ordinal,
        n=n,
        kind=kind,
        continues_from_previous_page=previous,
        continues_to_next_page=following,
    )


def test_continuation_links_record_each_flagged_break_agreed_or_one_sided():
    pages = {1: "pg_1", 2: "pg_2", 3: "pg_3"}
    rows = [
        _link_row(1, 1, previous=True),
        _link_row(1, 2, following=True),
        _link_row(2, 1, previous=True),
        _link_row(2, 2, following=True),
        _link_row(3, 1),
    ]
    links = dict(page_review.continuation_links(pages, rows))
    assert sorted(links) == ["page-break:0:1", "page-break:1:2", "page-break:2:3"]
    assert links["page-break:1:2"]["agreed"] is True
    before = links["page-break:0:1"]
    assert (before["agreed"], before["from_act_id"], before["to_act_key"]) == (False, None, "p1:1")
    after = links["page-break:2:3"]
    assert (after["agreed"], after["from_act_key"], after["to_act_key"]) == (False, "p2:2", "p3:1")
    assert (after["continues_to_next_page"], after["continues_from_previous_page"]) == (True, False)
    assert page_review.continuation_links(pages, [_link_row(1, 1), _link_row(2, 1)]) == []
    assert set(links["page-break:1:2"]) == CONTINUATION_LINK_FIELDS
    assert page_review.continuation_off_edge(rows) == {}


def test_a_link_joins_act_entries_past_a_catchword_and_notes_the_catchword_flag():
    pages = {1: "pg_1", 2: "pg_2"}
    rows = [
        _link_row(1, 1, following=True),
        _link_row(1, 2, kind="other", following=True),
        _link_row(2, 1, kind="other", previous=True),
        _link_row(2, 2, previous=True),
    ]
    [(subject, link)] = page_review.continuation_links(pages, rows)
    assert subject == "page-break:1:2" and link["agreed"] is True
    assert (link["from_act_key"], link["to_act_key"]) == ("p1:1", "p2:2")
    assert page_review.continuation_off_edge(rows) == {}
    notes = [page_review.continuation_notes(row) for row in rows]
    assert notes == [
        [],
        [{"code": page_review.CONTINUATION_ON_OTHER, "flags": ["continues_to_next_page"]}],
        [{"code": page_review.CONTINUATION_ON_OTHER, "flags": ["continues_from_previous_page"]}],
        [],
    ]
    outcome, payload = page_review.review_of(
        rows[1], coverage=FLOORED, page_coverage=CLEAN, assessment=None, confirmed=None
    )
    assert outcome == "accepted" and payload["notes"] == notes[1]


def test_a_continuation_flag_off_the_act_edge_holds_its_entry():
    rows = [
        _link_row(1, 1, following=True),
        _link_row(1, 2),
        _link_row(2, 1),
        _link_row(2, 2, previous=True),
    ]
    off_edge = page_review.continuation_off_edge(rows)
    assert off_edge == {
        rows[0]["act_id"]: ["continues_to_next_page"],
        rows[3]["act_id"]: ["continues_from_previous_page"],
    }
    # Neither flag sits on a side of the break, so no link records it.
    assert page_review.continuation_links({1: "pg_1", 2: "pg_2"}, rows) == []
    outcome, payload = page_review.review_of(
        rows[0],
        coverage=FLOORED,
        page_coverage=CLEAN,
        assessment=None,
        confirmed=None,
        off_edge=off_edge[rows[0]["act_id"]],
    )
    assert outcome == "held-for-review"
    assert payload["hold_codes"] == [page_review.CONTINUATION_OFF_EDGE]
    assert "continues_to_next_page but is not at the edge of page 1" in payload["reason"]


def test_a_links_inputs_bind_a_null_sides_page_reading():
    rows = [_link_row(1, 1, following=True)]
    [(_subject, link)] = page_review.continuation_links({1: "pg_1", 2: "pg_2"}, rows)
    pages = {1: {"reading_ref": "reading-1"}, 2: {"reading_ref": "reading-2"}}
    by_id = {row["act_id"]: row for row in rows}
    assert page_review.link_inputs(link, by_id, pages) == [rows[0]["perlectio_ref"], "reading-2"]
    assert page_review.link_inputs(link, by_id, {1: pages[1]}) == [rows[0]["perlectio_ref"]]
