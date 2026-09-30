"""The Recensor's page path: one review per unit a page-read run counts.

The trees are the synthetic fixture's `happy` and `page-review` scenarios read
with `reading_unit = "page"`. The fixture seals two page witnesses (Chandra and
Churro; DAI is act-scoped), so the trees that should be accepted are sealed
with a witness floor of 2, and the default floor of 3 is the shortfall case.
"""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any, Callable

import pytest

from common.contracts.canonical import canonical_bytes, self_hash
from common.contracts.errors import FatalAccounting
from common.contracts.stages import PERLECTOR, RECENSOR
from common.runtree.store import RunTree
from common.stage import PAGE_BLANK_HOLD, open_context, reading_acts, stage_parser
from conftest import (
    file_bytes_snapshot,
    load_stage,
    programs_through,
    rewitness_stage_boundary,
    run_stage,
)

ROOT = Path(__file__).resolve().parents[2]
RUN_ID = "r"
RECENSOR_PROGRAM = "pipeline/5_recensor/run.py"
page_review = load_stage("5_recensor", "page_review")


# --- trees -------------------------------------------------------------------------


def _page_protocol(directory: Path) -> Path:
    text = (ROOT / "config" / "perlector_protocol.toml").read_text(encoding="utf-8")
    assert 'reading_unit = "act"' in text
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "perlector_protocol.toml"
    path.write_text(text.replace('reading_unit = "act"', 'reading_unit = "page"'), "utf-8")
    return path


def _floor_config(directory: Path, floor: int) -> Path:
    """The live model config with its witness floor set to `floor`."""
    shutil.copytree(ROOT / "config" / "model-fixtures", directory / "model-fixtures")
    shutil.copytree(ROOT / "config" / "manifests", directory / "manifests")
    live = (ROOT / "config" / "models.toml").read_text(encoding="utf-8")
    assert "\nwitness_floor = 3\n" in live
    path = directory / "models.toml"
    path.write_text(live.replace("\nwitness_floor = 3\n", f"\nwitness_floor = {floor}\n"), "utf-8")
    assert tomllib.loads(path.read_text(encoding="utf-8"))["witness_floor"] == floor
    return path


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
    options = {"perlector_protocol_config": _page_protocol(base / "config")}
    if floor is not None:
        options["models_config"] = _floor_config(base / "models", floor)
    root = base / "runs"
    for program in programs_through("perlector"):
        result = run_stage(root, RUN_ID, scenario, program, **options)
        assert result.returncode == 0, f"{program}: {result.stderr}"
    return Tree(root, scenario, options)


@pytest.fixture(scope="module")
def happy(tmp_path_factory) -> Tree:
    return _tree(tmp_path_factory.mktemp("happy"), "happy", floor=2)


@pytest.fixture(scope="module")
def review(tmp_path_factory) -> Tree:
    return _tree(tmp_path_factory.mktemp("page-review"), "page-review", floor=2)


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


def _drop_act_records(tree: Tree, ordinal: int) -> None:
    for kind in ("act-region", "perlectio"):
        directory = tree.root / RUN_ID / "4_perlector" / "artifacts" / kind
        for path in directory.glob("*.json"):
            if json.loads(path.read_text("utf-8"))["payload"]["page_ordinal"] == ordinal:
                path.unlink()


def _forge_page_two(tree: Tree, reading: Callable[[dict], None], holds: list[str]) -> None:
    """Replace page 2's answer, its accounting's holds, and drop its act records."""
    _drop_act_records(tree, 2)
    _forge(tree, "page-reading", 2, reading)
    _forge(
        tree,
        "page-accounting",
        2,
        lambda record: (
            record.update(outcome="held" if holds else "read"),
            record["payload"].update(holds=holds),
        ),
    )
    rewitness_stage_boundary(RunTree(tree.root, RUN_ID), PERLECTOR)


# --- accepted -----------------------------------------------------------------------


def test_a_happy_page_tree_accepts_every_unit_with_its_evidence(happy, tmp_path):
    tree = happy.copy(tmp_path)
    result = tree.recensor()
    assert result.returncode == 0, result.stderr
    reviews = tree.reviews()
    rows = {row["act_key"]: row for row in reading_acts(tree.context())}
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
        assert payload["page_coverage"] == {
            "checked_pages": [row["page_ordinal"]],
            "flagged_pages": [],
            "unmeasurable_pages": [],
        }
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
    assert {item["page_disposition"] for item in receipt["items"]} == {"read"}

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
        f"act {reviews['p2:1']['subject_id']} is unresolved at the Recensor"
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

    _forge_page_two(tree, malformed, ["page-answer-incomplete"])
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
    # The link holds no act.
    assert reviews["p1:2"]["outcome"] == "accepted"


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

    _forge_page_two(tree, blank, [])
    assert tree.recensor().returncode == 3
    review = tree.reviews()["p2:blank"]
    payload = review["payload"]
    assert review["outcome"] == "held-for-review" and payload["unit_class"] == "page-blank"
    assert payload["hold_codes"] == [PAGE_BLANK_HOLD, "residual-ink"]
    assert payload["release"] is None
    confirmation = payload["confirmation"]
    assert confirmation["confirms"] == "page-blank" and confirmation["confirmed"] is False
    assert "Surya detected 3 line(s) on the page" in confirmation["failures"]
    assert {w["chair"] for w in confirmation["witnesses"]} == {"attestator_1", "attestator_3"}
    assert all(
        f"witness {chair} read the page and does not report it blank" in confirmation["failures"]
        for chair in ("attestator_1", "attestator_3")
    )
    assert "the page is not confirmed to hold no act" in payload["reason"]


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


def _testimonium(chair: str, outcome: str = "read", **health) -> dict[str, Any]:
    return {"outcome": outcome, "payload": {"chair": chair, "content_health": health}}


CLEAN = {"checked_pages": [1], "flagged_pages": [], "unmeasurable_pages": []}
PASSING = {"rules": {rule: {"status": "pass"} for rule in "def"}, "lines": []}


def _coverage(*records, floor: int = 2) -> dict[str, Any]:
    return page_review.page_witness_coverage(list(records), floor)


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
    empty = _coverage(floor=2)
    assert (empty["configured"], empty["under_witnessed"]) == (0, True)


def test_a_blank_page_is_confirmed_only_on_every_condition():
    witnesses = [_testimonium("a", blank=True), _testimonium("b", "genuinely-empty")]
    confirmed = page_review.confirmation(PASSING, witnesses, blank=True)
    assert confirmed["confirmed"] is True and confirmed["failures"] == []
    outcome, payload = page_review.review_of(
        _row(act_key="p1:blank", n=None, **{"class": "page-blank"}, hold_codes=[PAGE_BLANK_HOLD]),
        coverage=_coverage(*witnesses),
        page_coverage=CLEAN,
        assessment=None,
        confirmed=confirmed,
    )
    assert outcome == "confirmed-blank" and payload["hold_codes"] == []
    assert payload["release"]["hold_codes"] == [PAGE_BLANK_HOLD]
    assert "every witness that read the page reports it blank" in payload["release"]["reason"]

    for accounting, records, failure in (
        (
            {**PASSING, "rules": {**PASSING["rules"], "f": {"status": "hold"}}},
            witnesses,
            "page accounting rule (f) is hold, not pass",
        ),
        (
            {**PASSING, "rules": {**PASSING["rules"], "d": {"status": "not-measured"}}},
            witnesses,
            "page accounting rule (d) is not-measured, not pass",
        ),
        ({**PASSING, "lines": [{"id": "L1"}]}, witnesses, "Surya detected 1 line(s) on the page"),
        (
            PASSING,
            [_testimonium("a", blank=False)],
            "witness a read the page and does not report it blank",
        ),
        (PASSING, [_testimonium("a", "failed")], "no witness read the page"),
    ):
        refused = page_review.confirmation(accounting, records, blank=True)
        assert refused["confirmed"] is False and failure in refused["failures"]


def test_a_confirmed_blank_page_still_holds_on_the_floor_or_residual_ink():
    witnesses = [_testimonium("a", blank=True)]
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
    witnesses = [_testimonium("a", blank=False), _testimonium("b", blank=False)]
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
    unconfirmed = page_review.confirmation(
        {**PASSING, "rules": {**PASSING["rules"], "e": {"status": "hold"}}}, witnesses, blank=False
    )
    outcome, payload = page_review.review_of(
        row,
        coverage=_coverage(*witnesses),
        page_coverage=CLEAN,
        assessment=None,
        confirmed=unconfirmed,
    )
    assert outcome == "held-for-review" and payload["hold_codes"] == [page_review.NO_ACT_HOLD]


def test_a_held_row_is_never_released_by_this_stage():
    row = _row(
        hold_codes=[PAGE_BLANK_HOLD, "unread-ink"], disposition="held", **{"class": "page-blank"}
    )
    confirmed = page_review.confirmation(PASSING, [_testimonium("a", blank=True)], blank=True)
    outcome, payload = page_review.review_of(
        row,
        coverage=_coverage(_testimonium("a", blank=True), floor=1),
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
        (
            {"checked_pages": [], "flagged_pages": [], "unmeasurable_pages": []},
            None,
            "residual-ink-not-measured",
        ),
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


def _link_row(ordinal: int, n: int, *, previous: bool = False, following: bool = False) -> dict:
    return _row(
        act_id=f"act_{ordinal:08d}{n:08d}",
        act_key=f"p{ordinal}:{n}",
        page_ordinal=ordinal,
        n=n,
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
    assert set(links["page-break:1:2"]) == page_review.CONTINUATION_LINK_FIELDS
