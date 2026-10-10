"""The Recensor's page path: one review per unit a page-read run counts.

The trees are the synthetic fixture's `happy`, `page-review` and `page-no-act`
scenarios, read page by page. The fixture seals three page
witnesses (Chandra, DAI and Churro) on the page-read roster, against a floor
of 3; a floor of 4 is the shortfall case. DAI reads the records of the
fixture's record detector, so page accounting rule (i) is measured on every
page.
"""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from common import page_types
from common.contracts.canonical import canonical_bytes, self_hash
from common.contracts.errors import FatalAccounting
from common.contracts.identities import artifact_id
from common.contracts.stages import EXEMPLAR, PERLECTOR, RECENSOR
from common.page_review import (
    CONTINUATION_LINK_FIELDS,
    continuation_links,
    held_pages_after_review,
    page_breaks,
    reviewed_rows,
    run_page_breaks,
)
from common.runtree.store import RunTree
from common.stage import (
    NO_ACT_ON_PAGE_HOLD,
    PAGE_BLANK_HOLD,
    open_context,
    page_readings,
    reading_acts,
    reading_denominator,
    stage_parser,
)
from conftest import (
    build_page_tree,
    file_bytes_snapshot,
    load_stage,
    rewitness_stage_boundary,
    run_stage,
)

ROOT = Path(__file__).resolve().parents[2]
RUN_ID = "r"
RECENSOR_PROGRAM = "pipeline/5_recensor/run.py"
page_review = load_stage("5_recensor", "page_review")
RECENSOR_RUN = load_stage("5_recensor")


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


def _tree(base: Path, scenario: str, floor: int = 3, reask: int = 0) -> Tree:
    root, options = build_page_tree(base, scenario, RUN_ID, floor=floor, reask=reask)
    return Tree(root, scenario, options)


@pytest.fixture(scope="module")
def happy(tmp_path_factory) -> Tree:
    return _tree(tmp_path_factory.mktemp("happy"), "happy")


@pytest.fixture(scope="module")
def review(tmp_path_factory) -> Tree:
    return _tree(tmp_path_factory.mktemp("page-review"), "page-review")


@pytest.fixture(scope="module")
def no_act(tmp_path_factory) -> Tree:
    return _tree(tmp_path_factory.mktemp("page-no-act"), "page-no-act")


@pytest.fixture(scope="module")
def under_floor(tmp_path_factory) -> Tree:
    return _tree(tmp_path_factory.mktemp("floor"), "happy", floor=4)


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
            3,
            3,
            {"read": 3},
        )
        assert coverage["under_witnessed"] is False
        assert payload["continuation"] == {
            "continues_from_previous_page": row["continues_from_previous_page"],
            "continues_to_next_page": row["continues_to_next_page"],
        }
        assert payload["uncertainty_assessment"]["state"] is not None
        assert row["perlectio_ref"] in review["inputs"]

    receipt = tree.receipt()
    assert receipt["schema"] == "recensor-partition-receipt.v6"
    assert receipt["recensor_status"] == "complete" and receipt["reasons"] == []
    assert [
        (page["page_ordinal"], page["reask_ref"], page["reask"]) for page in receipt["pages"]
    ] == [
        (1, None, None),
        (2, None, None),
    ]
    assert receipt["expected_unit_count"] == 3 and "expected_act_count" not in receipt
    assert {item["page_disposition"] for item in receipt["items"]} == {"read"}
    [link] = receipt["continuation_links"]
    assert (link["subject_id"], link["outcome"]) == ("page-break:1:2", "accepted")

    # A second pass reuses every record byte for byte.
    before = file_bytes_snapshot(tree.root)
    assert tree.recensor().returncode == 0
    assert file_bytes_snapshot(tree.root) == before


def test_review_loop_builds_one_prior_manifest_for_all_units(happy, tmp_path, monkeypatch):
    tree = happy.copy(tmp_path)
    context = tree.context()
    denominator = reading_denominator(context)
    manifest_calls = []
    original_build_manifest = context.tree.build_manifest

    def build_manifest(stage, **kwargs):
        if stage == RECENSOR:
            manifest_calls.append(kwargs.get("verify_inputs", True))
        return original_build_manifest(stage, **kwargs)

    monkeypatch.setattr(context.tree, "build_manifest", build_manifest)
    calls_at_publish = []

    def publish_review(*args, **kwargs):
        calls_at_publish.append(list(manifest_calls))
        return RECENSOR_RUN.publish_review(*args, **kwargs)

    assert (
        page_review.review_pages(
            context,
            denominator,
            page_coverage_findings=RECENSOR_RUN.page_coverage_findings,
            publish_review=publish_review,
        )
        == 0
    )
    assert len(calls_at_publish) == len(denominator["acts"]) == 3
    assert calls_at_publish == [[False]] * 3


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
        "record-not-read",
        "truncation-not-classified",
        "unaccounted-witness-unit",
        "unread-line",
    ]
    # The two ink checks are review flags under the committed `[flags]`: measured,
    # named, holding nothing more.
    assert held["flag_codes"] == ["residual-ink", "unread-ink"]
    assert held["review_priority"] == 1
    assert held["page_coverage"]["flagged_pages"] == [2]
    for code in ("reading-unplaced", "truncation-not-classified", "unread-ink", "unread-line"):
        assert code in held["reason"]
    assert "flagged for review, not held" in held["reason"]
    assert "ink outside every reading region" in held["reason"]
    summary = json.loads(
        (tree.root / RUN_ID / "run-health" / "recensor-review-summary.json").read_text("utf-8")
    )
    assert summary["held_pages"] == [2] and summary["flagged_pages"] == []
    assert summary["by_code"]["unread-ink"] == {"as": "flag", "pages": 1, "units": 1}
    assert summary["by_code"]["reading-unplaced"] == {"as": "hold", "pages": 1, "units": 1}
    assert [row["act_key"] for row in summary["queue"]] == ["p2:1"]
    assert summary["by_page_type"] == {
        "register-acts": {"pages": 2, "held_pages": 1, "flagged_pages": 0, "clean_pages": 1}
    }
    receipt = tree.receipt()
    assert receipt["recensor_status"] == "partial"
    assert receipt["reasons"] == [
        f"unit {reviews['p2:1']['subject_id']} was held by its page reading and is not released",
        f"unit {reviews['p2:1']['subject_id']} is unresolved at the Recensor",
    ]


def test_a_page_under_the_witness_floor_holds_every_unit_on_it(under_floor, tmp_path):
    tree = under_floor.copy(tmp_path)
    assert tree.recensor().returncode == 3
    for review in tree.reviews().values():
        assert review["outcome"] == "held-for-review"
        assert review["payload"]["hold_codes"] == ["under-witnessed"]
        assert "3 page witness(es) read page" in review["payload"]["reason"]
        assert "against a floor of 4" in review["payload"]["reason"]
    reasons = tree.receipt()["reasons"]
    assert sum("under-witnessed (3 page reads of a floor of 4)" in r for r in reasons) == 3


def test_an_unread_page_is_one_held_unit_and_its_break_is_one_sided(tmp_path):
    # Page 2's reply is not JSON; page 1's last act still says it runs on.
    tree = _tree(tmp_path, "page-unread")
    assert tree.recensor().returncode == 3
    reviews = tree.reviews()
    assert sorted(reviews) == ["p1:1", "p1:2", "p2:unread"]
    unread = reviews["p2:unread"]
    assert unread["outcome"] == "held-for-review"
    payload = unread["payload"]
    assert payload["unit_class"] == "page-unread"
    assert payload["hold_codes"] == ["not-json", "page-answer-incomplete", "page-unread"]
    # The Recensor's residual-ink check is a review flag under the committed `[flags]`.
    assert payload["flag_codes"] == ["residual-ink"] and payload["review_priority"] == 1
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


def test_a_page_read_as_blank_with_ink_and_witness_text_is_not_confirmed(tmp_path):
    # Page 2 read as blank paper, every id on it set aside.
    tree = _tree(tmp_path, "page-blank")
    assert tree.recensor().returncode == 3
    review = tree.reviews()["p2:blank"]
    payload = review["payload"]
    assert review["outcome"] == "held-for-review" and payload["unit_class"] == "page-blank"
    rows = {row["act_key"]: row for row in reading_acts(tree.context())}
    assert PAGE_BLANK_HOLD in rows["p2:blank"]["hold_codes"]
    assert payload["hold_codes"] == rows["p2:blank"]["hold_codes"]
    assert payload["flag_codes"] == sorted({*rows["p2:blank"]["flag_codes"], "residual-ink"})
    assert payload["release"] is None
    confirmation = payload["confirmation"]
    assert confirmation["confirms"] == "page-blank" and confirmation["confirmed"] is False
    assert "Surya detected 3 line(s) on the page" in confirmation["failures"]
    chairs = {"attestator_1", "attestator_2", "attestator_3"}
    assert {w["chair"] for w in confirmation["witnesses"]} == chairs
    assert all(
        f"witness {chair} read the page and its retained text is not blank"
        in confirmation["failures"]
        for chair in chairs
    )
    # DAI's record on the page was set aside, not read: rule (i) holds, a failure too.
    assert confirmation["rules"]["i"] == "hold"
    assert (
        "page accounting rule (i) is hold, not flag or not-applicable or pass"
        in confirmation["failures"]
    )
    assert "the page is not confirmed to hold no act" in payload["reason"]


def test_a_page_of_other_entries_dai_saw_nothing_on_is_confirmed_holding_no_act(no_act, tmp_path):
    tree = no_act.copy(tmp_path)
    assert tree.recensor().returncode == 3
    reviews = tree.reviews()
    assert {key: r["outcome"] for key, r in reviews.items()} == {
        "p1:1": "accepted",
        "p1:2": "accepted",
        "p2:1": "accepted",
    }
    payload = reviews["p2:1"]["payload"]
    assert (payload["unit_class"], payload["kind"]) == ("reading", "other")
    # The record detector found no record on page 2 below its cap, so DAI's page
    # there is blank testimony: it counts toward the floor, and rule (e) records
    # it as a witness that read blank rather than holding it unread.
    assert payload["coverage"]["under_witnessed"] is False
    assert payload["hold_codes"] == []
    assert payload["release"]["hold_codes"] == [NO_ACT_ON_PAGE_HOLD]
    confirmation = payload["confirmation"]
    assert confirmation["confirms"] == "no-act-on-page" and confirmation["confirmed"] is True
    assert confirmation["rules"] == {"d": "pass", "e": "pass", "f": "pass", "i": "pass"}
    assert confirmation["failures"] == []
    [dai] = [w for w in confirmation["witnesses"] if w["chair"] == "attestator_2"]
    assert dai == {"chair": "attestator_2", "outcome": "genuinely-empty", "blank": True}
    # The `other` entry's own continuation flag is a note, never a side of a break.
    assert payload["notes"] == [
        {"code": page_review.CONTINUATION_ON_OTHER, "flags": ["continues_from_previous_page"]}
    ]
    # Page 1's last act says it runs on and page 2 has no act: the break stays held.
    [link] = tree.records("5_recensor", "continuation-link")
    assert link["outcome"] == "held-for-review"
    assert (link["payload"]["from_act_key"], link["payload"]["to_act_id"]) == ("p1:2", None)
    assert tree.receipt()["recensor_status"] == "partial"


def test_the_detectors_census_is_named_to_the_confirmation(no_act, tmp_path, monkeypatch):
    # Page 2's DAI record is its detector's look, so the confirmation knows it as a census.
    tree = no_act.copy(tmp_path)
    seen = {}
    real = page_review.confirmation

    def spy(accounting, records, *, blank, census):
        seen[records[0]["subject_id"]] = census
        return real(accounting, records, blank=blank, census=census)

    monkeypatch.setattr(page_review, "confirmation", spy)
    context = tree.context()
    page_review.plan_reviews(
        context, reading_denominator(context), RECENSOR_RUN.page_coverage_findings
    )
    assert list(seen.values()) == [frozenset({"attestator_2"})]


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
        _write_receipt(context)


def _write_receipt(context) -> None:
    page_review.write_reading_receipt(
        context, page_coverage_findings=RECENSOR_RUN.page_coverage_findings
    )


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
            "its outcome differ",
        ),
        (
            lambda tree, context, reviews: _later_review(
                context, reviews["p1:1"], hold_codes=["under-witnessed"], outcome="held-for-review"
            ),
            "its outcome, hold_codes",
        ),
        (
            lambda tree, context, reviews: _later_review(
                context,
                reviews["p1:1"],
                page_coverage={**reviews["p1:1"]["payload"]["page_coverage"], "checked_pages": []},
            ),
            r"not the review disk measures: its page_coverage differ",
        ),
        (lambda tree, context, reviews: _drop(tree, "continuation-link"), "no continuation-link"),
        (lambda tree, context, reviews: _stray_link(context, reviews), "no answer of this run"),
    ],
    ids=[
        "missing-review",
        "coverage",
        "outcome",
        "own-code",
        "residual-ink",
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
        _write_receipt(context)


def test_the_receipt_refuses_a_release_resting_on_a_confirmation_disk_does_not_give(
    no_act, tmp_path
):
    """A release is only as good as its confirmation, so the receipt measures that again too."""
    tree = no_act.copy(tmp_path)
    assert tree.recensor().returncode == 3
    context = tree.context()
    released = tree.reviews()["p2:1"]
    confirmation = {**released["payload"]["confirmation"], "witnesses": []}
    _later_review(context, released, confirmation=confirmation)
    context.finish()
    with pytest.raises(FatalAccounting, match="its confirmation differ"):
        _write_receipt(context)


def test_the_receipt_refuses_an_accepted_review_of_a_held_row_with_no_release(review, tmp_path):
    tree = review.copy(tmp_path)
    assert tree.recensor().returncode == 3
    context = tree.context()
    held = tree.reviews()["p2:1"]
    _later_review(context, held, outcome="accepted", hold_codes=[])
    context.finish()
    with pytest.raises(FatalAccounting, match="its outcome, hold_codes"):
        _write_receipt(context)


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
        "flag_codes": [],
        "continues_from_previous_page": False,
        "continues_to_next_page": False,
        "reading_attempt": 1,
        "reading_n": 1,
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


def test_a_routed_witness_counts_toward_the_floor_on_its_own_pages_only():
    """dots.mocr seated by `[witness_routing]` on a page: four chairs, floor still 3.

    The page's roster is the floor's denominator (`page_witness_chairs`): a
    routed page counts the routed chair like any witness, so it fills a seat
    DAI leaves empty; an unrouted page counts the three as it always did.
    """
    routed = {"chandra", "dai", "churro", "dots"}
    # DAI not run (its detector found nothing and stated no cap): dots fills its seat.
    filled = _coverage(
        _testimonium("chandra", truncated=False),
        _testimonium("dai", "not-run"),
        _testimonium("churro", truncated=False),
        _testimonium("dots", truncated=False),
        floor=3,
        chairs=routed,
    )
    assert (filled["configured"], filled["under_witnessed"]) == (4, False)
    # dots.mocr cut off as well: two readings of four is under the floor.
    short = _coverage(
        _testimonium("chandra", truncated=False),
        _testimonium("dai", "not-run"),
        _testimonium("churro", truncated=False),
        _testimonium("dots", "failed"),
        floor=3,
        chairs=routed,
    )
    assert (short["configured"], short["under_witnessed"]) == (4, True)
    assert short["shortfalls"]["failed"] == 1
    # An unrouted page's roster leaves dots out: three of three, as before.
    act_page = _coverage(
        _testimonium("chandra", truncated=False),
        _testimonium("dai", truncated=False),
        _testimonium("churro", truncated=False),
        floor=3,
        chairs={"chandra", "dai", "churro"},
    )
    assert (act_page["configured"], act_page["under_witnessed"]) == (3, False)


def test_blankness_is_measured_from_the_retained_text_not_the_health_report():
    assert page_review.retained_text_blank(_testimonium("a", text=" \n\t")) is True
    assert page_review.retained_text_blank(_testimonium("a", text="ink", blank=True)) is False
    assert page_review.retained_text_blank(_testimonium("a", text={"records": []})) is None


# `b` is a record reader whose page record is its detector's census: it found no record.
BLANK_WITNESSES = [
    _testimonium("a", text="", blank=True),
    _testimonium("b", "genuinely-empty", text=""),
]
CENSUS = frozenset({"b"})


def _without(rule: str, status: str) -> dict:
    return {**PASSING, "rules": {**PASSING["rules"], rule: {"status": status}}}


def test_a_blank_page_meeting_every_condition_is_confirmed_and_released():
    confirmed = page_review.confirmation(PASSING, BLANK_WITNESSES, blank=True, census=CENSUS)
    assert confirmed["confirmed"] is True and confirmed["failures"] == []
    # With no record detector, rule (i) does not apply to a blank page.
    assert page_review.confirmation(
        _without("i", "not-applicable"), BLANK_WITNESSES, blank=True, census=CENSUS
    )["confirmed"]
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
        (
            _without("d", "not-measured"),
            BLANK_WITNESSES,
            "rule (d) is not-measured, not flag or pass",
        ),
        (_without("e", "hold"), BLANK_WITNESSES, "rule (e) is hold, not flag or pass"),
        (_without("f", "hold"), BLANK_WITNESSES, "rule (f) is hold, not flag or pass"),
        (
            _without("i", "not-measured"),
            BLANK_WITNESSES,
            "rule (i) is not-measured, not flag or not-applicable or pass",
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
        (
            PASSING,
            [_testimonium("a", "failed", text=None), BLANK_WITNESSES[1]],
            "only a record detector's census found the page blank",
        ),
    ],
    ids=["d", "e", "f", "i", "lines", "witness-text", "text-unmeasurable", "no-reader", "census"],
)
def test_a_blank_page_failing_one_condition_is_held_naming_it(accounting, records, failure):
    refused = page_review.confirmation(accounting, records, blank=True, census=CENSUS)
    assert refused["confirmed"] is False
    assert len(refused["failures"]) == 1 and failure in refused["failures"][0]


def test_a_confirmed_blank_page_still_holds_on_the_floor_or_residual_ink():
    witnesses = [_testimonium("a", text="")]
    row = _row(n=None, **{"class": "page-blank"}, hold_codes=[PAGE_BLANK_HOLD])
    confirmed = page_review.confirmation(PASSING, witnesses, blank=True, census=frozenset())
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
    row = _row(kind="other", hold_codes=[NO_ACT_ON_PAGE_HOLD], disposition="held")
    witnesses = [_testimonium("a"), _testimonium("b")]
    confirmed = page_review.confirmation(PASSING, witnesses, blank=False, census=frozenset())
    assert confirmed["confirmed"] is True
    outcome, payload = page_review.review_of(
        row,
        coverage=_coverage(*witnesses),
        page_coverage=CLEAN,
        assessment=None,
        confirmed=confirmed,
    )
    assert outcome == "accepted" and payload["release"]["hold_codes"] == [NO_ACT_ON_PAGE_HOLD]
    assert "so does rule (i)" in payload["release"]["reason"]


@pytest.mark.parametrize(
    ("rule", "status"),
    [("d", "hold"), ("e", "hold"), ("f", "not-measured"), ("i", "hold"), ("i", "not-applicable")],
)
def test_a_page_of_only_other_readings_stays_held_on_any_rule_not_passing(rule, status):
    row = _row(kind="other", hold_codes=[NO_ACT_ON_PAGE_HOLD], disposition="held")
    witnesses = [_testimonium("a"), _testimonium("b")]
    unconfirmed = page_review.confirmation(
        _without(rule, status), witnesses, blank=False, census=frozenset()
    )
    assert unconfirmed["failures"] == [
        f"page accounting rule ({rule}) is {status}, not flag or pass"
    ]
    outcome, payload = page_review.review_of(
        row,
        coverage=_coverage(*witnesses),
        page_coverage=CLEAN,
        assessment=None,
        confirmed=unconfirmed,
    )
    assert outcome == "held-for-review" and payload["hold_codes"] == [NO_ACT_ON_PAGE_HOLD]


def _typed(accounting: dict, page_type: str, writing: str) -> dict:
    """`accounting` with the page type block the page accounting records for a stated type."""
    return {
        **accounting,
        "page_type": {"applicability": page_types.applicability(page_type, writing)},
    }


@pytest.mark.parametrize("status", ["hold", "not-measured", "not-applicable"])
def test_a_page_of_other_entries_is_confirmed_when_its_type_switches_rule_i_off(status):
    """Rule (i) does not apply to an index page: whatever its status, it confirms."""
    row = _row(kind="other", hold_codes=[NO_ACT_ON_PAGE_HOLD], disposition="held")
    witnesses = [_testimonium("a"), _testimonium("b")]
    confirmed = page_review.confirmation(
        _typed(_without("i", status), "index", "typed"),
        witnesses,
        blank=False,
        census=frozenset(),
    )
    assert confirmed["confirmed"] is True and confirmed["failures"] == []
    assert confirmed["rules"]["i"] == status
    outcome, payload = page_review.review_of(
        row,
        coverage=_coverage(*witnesses),
        page_coverage=CLEAN,
        assessment=None,
        confirmed=confirmed,
    )
    assert outcome == "accepted" and payload["release"]["hold_codes"] == [NO_ACT_ON_PAGE_HOLD]
    assert "rule (i) does not apply to the page's stated type" in payload["release"]["reason"]


@pytest.mark.parametrize(("rule", "status"), [("d", "hold"), ("e", "hold"), ("f", "not-measured")])
def test_a_typed_page_of_other_entries_still_needs_every_rule_that_applies(rule, status):
    unconfirmed = page_review.confirmation(
        _typed(_without(rule, status), "index", "typed"),
        [_testimonium("a"), _testimonium("b")],
        blank=False,
        census=frozenset(),
    )
    assert unconfirmed["failures"] == [
        f"page accounting rule ({rule}) is {status}, not flag or pass"
    ]


@pytest.mark.parametrize(
    "accounting",
    [
        _without("i", "hold"),
        _typed(_without("i", "hold"), "register-acts", "handwritten"),
        _typed(_without("i", "hold"), "register-acts", "mixed"),
    ],
    ids=["untyped", "handwritten-acts", "mixed-acts"],
)
def test_rule_i_still_holds_a_page_of_other_entries_where_it_applies(accounting):
    unconfirmed = page_review.confirmation(
        accounting, [_testimonium("a"), _testimonium("b")], blank=False, census=frozenset()
    )
    assert unconfirmed["failures"] == ["page accounting rule (i) is hold, not flag or pass"]


def test_a_refused_pages_row_is_not_a_counted_unit():
    rows = [
        _row(act_id="a1"),
        _row(
            act_id=None, act_key="p2:refused", page_ordinal=2, n=None, **{"class": "page-refused"}
        ),
    ]
    assert [row["act_id"] for row in reviewed_rows(rows)] == ["a1"]


def test_a_unit_on_a_page_of_acts_the_detector_found_nothing_on_is_held():
    """DAI's blank testimony counts as a reading, but the row's rule (i) hold keeps the unit."""
    row = _row(hold_codes=["no-detector-record-on-act-page"], disposition="read")
    witnesses = [
        _testimonium("a", truncated=False),
        _testimonium("b", "genuinely-empty", "", truncated=False),
        _testimonium("c", truncated=False),
    ]
    coverage = page_review.page_witness_coverage(witnesses, 3, {"a", "b", "c"})
    assert coverage["under_witnessed"] is False
    outcome, payload = page_review.review_of(
        row, coverage=coverage, page_coverage=CLEAN, assessment=None, confirmed=None
    )
    assert outcome == "held-for-review"
    assert payload["hold_codes"] == ["no-detector-record-on-act-page"]
    assert payload["release"] is None


def test_a_held_row_is_never_released_by_this_stage():
    row = _row(
        hold_codes=[PAGE_BLANK_HOLD, "unread-ink"], disposition="held", **{"class": "page-blank"}
    )
    witnesses = [_testimonium("a", text="")]
    confirmed = page_review.confirmation(PASSING, witnesses, blank=True, census=frozenset())
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


FLOORED = _coverage(_testimonium("a", truncated=False), floor=1)


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


def test_an_act_the_re_ask_recovered_moves_no_page_edge_and_is_no_side():
    pages = {1: "pg_1", 2: "pg_2"}
    recovered = {**_link_row(1, 2), "reading_attempt": 2}
    rows = [_link_row(1, 1, following=True), recovered, _link_row(2, 1, previous=True)]
    [(subject, link)] = page_breaks(pages, rows)
    assert subject == "page-break:1:2"
    assert (link["from_act_key"], link["to_act_key"], link["agreed"]) == ("p1:1", "p2:1", True)
    assert page_review.continuation_off_edge(rows) == {}
    # Recovered before a page's first reading, it moves the page's first edge no more.
    rows = [_link_row(1, 1, following=True), {**_link_row(2, 1), "reading_attempt": 2}]
    rows.append(_link_row(2, 2, previous=True))
    [(_subject, link)] = page_breaks(pages, rows)
    assert link["to_act_key"] == "p2:2" and page_review.continuation_off_edge(rows) == {}


def test_continuation_links_record_each_flagged_break_agreed_or_one_sided():
    pages = {1: "pg_1", 2: "pg_2", 3: "pg_3"}
    rows = [
        _link_row(1, 1, previous=True),
        _link_row(1, 2, following=True),
        _link_row(2, 1, previous=True),
        _link_row(2, 2, following=True),
        _link_row(3, 1),
    ]
    links = dict(page_breaks(pages, rows))
    assert sorted(links) == ["page-break:0:1", "page-break:1:2", "page-break:2:3"]
    assert links["page-break:1:2"]["agreed"] is True
    before = links["page-break:0:1"]
    assert (before["agreed"], before["from_act_id"], before["to_act_key"]) == (False, None, "p1:1")
    after = links["page-break:2:3"]
    assert (after["agreed"], after["from_act_key"], after["to_act_key"]) == (False, "p2:2", "p3:1")
    assert (after["continues_to_next_page"], after["continues_from_previous_page"]) == (True, False)
    assert page_breaks(pages, [_link_row(1, 1), _link_row(2, 1)]) == []
    assert set(links["page-break:1:2"]) == CONTINUATION_LINK_FIELDS
    assert page_review.continuation_off_edge(rows) == {}


def test_a_canary_page_is_never_a_side_of_a_page_break(monkeypatch):
    """The Door appends canary pages after the real ones, so the last real page
    and the first canary are adjacent ordinals. Both flag an act running across
    that break here, and two canary pages flag one between them: no link may name
    a canary act, and the real page's flag is a one-sided break, as it would be
    with no canary beside it."""
    canary_ledger = "c" * 64
    run = {
        "sealed_config_digests": {"canary-ledger": canary_ledger},
        "source_manifest": [
            {"ordinal": 1, "ledger_sha256": "a" * 64},
            {"ordinal": 2, "ledger_sha256": "a" * 64},
            {"ordinal": 3, "ledger_sha256": canary_ledger},
            {"ordinal": 4, "ledger_sha256": canary_ledger},
        ],
    }
    pages = {1: "pg_1", 2: "pg_2", 3: "pg_3", 4: "pg_4"}
    monkeypatch.setattr("common.page_review.exemplar_page_ids", lambda _context: pages)
    rows = [
        _link_row(1, 1),
        _link_row(2, 1, following=True),
        _link_row(3, 1, previous=True, following=True),
        _link_row(4, 1, previous=True),
    ]
    links = dict(run_page_breaks(SimpleNamespace(run=run), rows))
    assert list(links) == ["page-break:2:3"]
    link = links["page-break:2:3"]
    assert (link["from_act_key"], link["to_act_id"], link["agreed"]) == ("p2:1", None, False)
    without_canaries = {1: "pg_1", 2: "pg_2"}
    assert links == dict(page_breaks(without_canaries, rows[:2]))


def test_the_recensor_run_links_no_canary_page(happy, tmp_path, monkeypatch):
    """The whole Recensor pass with `happy`'s page 2 sealed as a canary: the act
    running from page 1 links to no canary act, so its break is one-sided and held,
    and the published link, the receipt's recomputation and the link verifier
    agree. Each derives its breaks through `run_page_breaks`; one that took them
    over every page would join page 1 to the canary or refuse the others. The
    link's evidence is what a run without the canary records: page 1's act alone,
    never the canary page's reading for the null side."""
    tree = happy.copy(tmp_path)
    context = tree.context()
    monkeypatch.setattr("common.stage.canary_ordinals", lambda _run: {2})
    denominator = reading_denominator(context)
    canary_reading = denominator["pages"][2]["reading_ref"]
    assert RECENSOR_RUN.review_a_page_read_run(context, denominator) == 3

    [link] = tree.records("5_recensor", "continuation-link")
    assert link["subject_id"] == "page-break:1:2" and link["outcome"] == "held-for-review"
    payload = link["payload"]
    assert (payload["from_act_key"], payload["to_act_id"], payload["agreed"]) == (
        "p1:2",
        None,
        False,
    )
    context = tree.context()
    [verified] = continuation_links(context, reading_acts(context))
    assert (verified["from_page_ordinal"], verified["to_page_ordinal"]) == (1, 2)
    assert verified["tail_act_id"] is None and verified["agreed"] is False
    # Its evidence is page 1's act alone, as on a run whose last page is page 1:
    # the canary page's reading is no null side's evidence.
    rows = {row["act_key"]: row for row in reading_acts(context)}
    assert link["inputs"] == [rows["p1:2"]["perlectio_ref"]]
    assert canary_reading not in link["inputs"]
    [receipt_link] = tree.receipt()["continuation_links"]
    assert receipt_link["subject_id"] == "page-break:1:2"
    assert receipt_link["link_ref"] == verified["ref"]


def test_the_systemic_alarm_counts_no_canary_page(review, tmp_path, monkeypatch):
    """`page-review` holds page 2 of its two; sealed as a canary, page 2 is neither
    held nor counted, as the Armarium leaves canary pages out of its page holds."""
    tree = review.copy(tmp_path)
    assert tree.recensor().returncode == 3
    run_tree = RunTree(tree.root, RUN_ID)
    assert held_pages_after_review(run_tree) == ([2], 2)
    monkeypatch.setattr("common.page_review.canary_ordinals", lambda _run: {2})
    assert held_pages_after_review(run_tree) == ([], 1)


def test_a_link_joins_act_entries_past_a_catchword_and_notes_the_catchword_flag():
    pages = {1: "pg_1", 2: "pg_2"}
    rows = [
        _link_row(1, 1, following=True),
        _link_row(1, 2, kind="other", following=True),
        _link_row(2, 1, kind="other", previous=True),
        _link_row(2, 2, previous=True),
    ]
    [(subject, link)] = page_breaks(pages, rows)
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
    assert page_breaks({1: "pg_1", 2: "pg_2"}, rows) == []
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
    [(_subject, link)] = page_breaks({1: "pg_1", 2: "pg_2"}, rows)
    pages = {1: {"reading_ref": "reading-1"}, 2: {"reading_ref": "reading-2"}}
    by_id = {row["act_id"]: row for row in rows}
    assert page_review.link_inputs(link, by_id, pages) == [rows[0]["perlectio_ref"], "reading-2"]
    assert page_review.link_inputs(link, by_id, {1: pages[1]}) == [rows[0]["perlectio_ref"]]


def test_residual_ink_is_measured_against_each_region_box_not_their_rectangle():
    """An entry citing lines in two columns covers those lines, never the gutter between."""
    left, right = {"x": 10, "y": 10, "w": 40, "h": 10}, {"x": 150, "y": 30, "w": 40, "h": 10}
    payloads = {
        "a": {
            "schema": "perlector-act-region.v2",
            "region_boxes_px": [left, right],
            "union_box_px": {"x": 10, "y": 10, "w": 180, "h": 30},
        },
        "b": {"schema": "perlector-act-region.v2", "region_boxes_px": [], "union_box_px": None},
    }

    class _Tree:
        def read_artifact_reference(self, ref, *, stage, kind, subject_id):
            assert (stage, kind) == (PERLECTOR, "act-region")
            return {"payload": payloads[subject_id]}

    context = type("Context", (), {"tree": _Tree()})()
    acts = [
        {"act_id": "a", "page_ordinal": 1, "region_ref": {"ref": "a"}},
        {"act_id": "b", "page_ordinal": 1, "region_ref": {"ref": "b"}},
        {"act_id": "c", "page_ordinal": 2, "region_ref": None},
    ]

    regions = page_review.reading_regions_by_page(context, {1: {}, 2: {}}, acts)

    assert regions == {1: [left, right], 2: []}

    # A region of another schema carries no region boxes to read; it is refused by name.
    payloads["a"] = {
        "schema": "perlector-act-region.v1",
        "union_box_px": payloads["a"]["union_box_px"],
    }
    with pytest.raises(FatalAccounting, match="act a's act-region is 'perlector-act-region.v1'"):
        page_review.reading_regions_by_page(context, {1: {}, 2: {}}, acts)


# --- a re-asked page -------------------------------------------------------------------


def test_a_re_asked_page_keeps_its_first_readings_edges_and_counts_its_recovered_act(tmp_path):
    """reask-continuation with the re-ask on: page 1's one first-reading act runs on to
    page 2, and its re-ask recovers another act, numbered after it. The recovered act
    is reviewed, is never a side of the page break and moves no page edge."""
    tree = _tree(tmp_path, "reask-continuation", reask=1)
    result = tree.recensor()
    assert result.returncode == 3, result.stderr
    context = tree.context()
    rows = {row["act_key"]: row for row in reading_acts(context)}
    assert [(key, rows[key]["reading_attempt"]) for key in sorted(rows)] == [
        ("p1:1", 1),
        ("p1:2", 2),
        ("p2:1", 1),
    ]
    assert rows["p1:1"]["continues_to_next_page"] is True
    reviews = tree.reviews()
    assert sorted(reviews) == sorted(rows)
    assert not any(
        "continuation-off-page-edge" in review["payload"]["hold_codes"]
        for review in reviews.values()
    )
    assert {key: review["payload"]["recoveries_used"] for key, review in reviews.items()} == {
        "p1:1": 1,
        "p1:2": 1,
        "p2:1": 0,
    }
    [link] = tree.records("5_recensor", "continuation-link")
    payload = link["payload"]
    assert (payload["from_act_key"], payload["to_act_key"], payload["agreed"]) == (
        "p1:1",
        "p2:1",
        True,
    )
    assert link["outcome"] == "accepted"

    receipt = tree.receipt()
    assert receipt["schema"] == "recensor-partition-receipt.v6"
    first, second = receipt["pages"]
    assert first["reask_ref"] == page_readings(context)[1]["reask_ref"] is not None
    assert first["accounting_ref"] == rows["p1:2"]["accounting_ref"]
    assert first["reask"]["named"] and first["reask"]["duplicate"] == []
    assert sorted(
        first["reask"]["cleared"]
        + first["reask"]["set_aside"]
        + first["reask"]["held"]
        + first["reask"]["unread"]
    ) == sorted(first["reask"]["named"])
    assert (second["reask_ref"], second["reask"]) == (None, None)
    [link_row] = receipt["continuation_links"]
    assert link_row["outcome"] == "accepted"

    # A second pass reuses every record byte for byte.
    before = file_bytes_snapshot(tree.root)
    assert tree.recensor().returncode == result.returncode
    assert file_bytes_snapshot(tree.root) == before


def test_a_continuation_link_naming_a_recovered_act_is_refused(tmp_path):
    tree = _tree(tmp_path, "reask-continuation", reask=1)
    result = tree.recensor()
    assert result.returncode == 3, result.stderr
    context = tree.context()
    rows = {row["act_key"]: row for row in reading_acts(context)}
    recovered = rows["p1:2"]
    [path] = (tree.root / RUN_ID / "5_recensor" / "artifacts" / "continuation-link").glob("*.json")
    link = json.loads(path.read_text("utf-8"))
    link["payload"].update(from_act_id=recovered["act_id"], from_act_key="p1:2")
    link["inputs"].append(recovered["perlectio_ref"])
    link["self_hash"] = self_hash({k: v for k, v in link.items() if k != "self_hash"})
    path.write_bytes(canonical_bytes(link))
    rewitness_stage_boundary(RunTree(tree.root, RUN_ID), RECENSOR)
    with pytest.raises(FatalAccounting, match="an entry the re-ask recovered"):
        continuation_links(tree.context(), reading_acts(tree.context()))


def test_the_reviews_and_the_receipt_measure_residual_ink_once(happy, tmp_path, monkeypatch):
    """Both plans of a pass are taken from one residual-ink measurement of the same
    regions, and each gets its own copy; the receipt still matches every review on disk."""
    tree = happy.copy(tmp_path)
    context = tree.context()
    measured = []
    real = RECENSOR_RUN.page_coverage_findings

    def counting(context, *, regions):
        measured.append(regions)
        return real(context, regions=regions)

    monkeypatch.setattr(RECENSOR_RUN, "page_coverage_findings", counting)
    RECENSOR_RUN.review_a_page_read_run(context, reading_denominator(context))

    assert len(measured) == 1

    once = RECENSOR_RUN.measured_once(lambda _context, *, regions: {1: {"regions": regions}})
    first = once(context, regions={1: []})
    first[1]["regions"][1] = ["changed"]
    assert once(context, regions={1: []}) == {1: {"regions": {1: []}}}


def test_a_flagged_finding_of_this_stage_is_recorded_and_holds_nothing():
    """Residual ink under the sealed flags: named, prioritised, and the unit is accepted."""
    witnesses = [_testimonium("a", truncated=False)]
    flagged_ink = {**CLEAN, "flagged_pages": [1]}
    outcome, payload = page_review.review_of(
        _row(flag_codes=["unread-ink"]),
        coverage=_coverage(*witnesses, floor=1),
        page_coverage=flagged_ink,
        assessment=None,
        confirmed=None,
        flag_codes=frozenset({"residual-ink", "unread-ink"}),
    )
    assert outcome == "accepted" and payload["hold_codes"] == []
    assert payload["flag_codes"] == ["residual-ink", "unread-ink"]
    assert payload["review_priority"] == 1
    assert "flagged for review, not held" in payload["reason"]
    assert "ink outside every reading region" in payload["reason"]
    # Without the flag the same finding holds, as before.
    held, strict = page_review.review_of(
        _row(),
        coverage=_coverage(*witnesses, floor=1),
        page_coverage=flagged_ink,
        assessment=None,
        confirmed=None,
    )
    assert held == "held-for-review" and strict["hold_codes"] == ["residual-ink"]
    assert strict["flag_codes"] == [] and strict["review_priority"] == 1
    # A clean unit has no priority: nothing to review.
    _accepted, clean = page_review.review_of(
        _row(),
        coverage=_coverage(*witnesses, floor=1),
        page_coverage=CLEAN,
        assessment=None,
        confirmed=None,
    )
    assert clean["review_priority"] is None and clean["flag_codes"] == []


def test_a_no_act_page_is_confirmed_over_a_flagged_rule():
    """A rule whose every finding is a review flag confirms a page of other entries."""
    flagged = {**PASSING, "rules": {**PASSING["rules"], "f": {"status": "flag"}}}
    confirmed = page_review.confirmation(
        flagged, [_testimonium("a")], blank=False, census=frozenset()
    )
    assert confirmed["confirmed"] is True and confirmed["failures"] == []


def test_the_review_summary_counts_a_page_by_the_type_its_accounting_records():
    named = {"payload": {"page_type": {"stated": "index", "writing": "typed"}}}
    assert page_review.page_type_of(named) == "index"
    # An answer that named a type but failed validation: the accounting states none.
    held = {"payload": {"page_type": {"stated": None, "writing": None}}}
    assert page_review.page_type_of(held) == page_review.UNTYPED_PAGE
