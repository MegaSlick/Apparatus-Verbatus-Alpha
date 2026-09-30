"""The Archetypus on a page-read run: every accepted reading, act or other, established once.

The trees are the fixture's `happy`, `page-review` and `page-other` scenarios
read with `reading_unit = "page"` and reviewed by the real Recensor, and
`page-no-act-unbroken` (`page-no-act` with nothing running across the page
break) read by Chandra and Churro alone. Two tests forge a review the
Recensor does not write on the fixture (`conftest.forge_page_review`), each
saying why.
"""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest

from common.contracts.canonical import canonical_bytes, self_hash
from common.contracts.stages import ARCHETYPUS, RECENSOR
from common.exemplar_boundary import verify_reading_region_lineage
from common.page_review import current_page_reviews
from common.page_testimonia import current_page_testimonia, sealed_proposal_regions
from common.runtree.store import RunTree
from common.stage import NO_ACT_ON_PAGE_HOLD, reading_acts
from conftest import (
    build_page_tree,
    forge_page_review,
    load_stage,
    page_context,
    rewitness_stage_boundary,
    run_stage,
)

RUN_ID = "r"
archetypus = load_stage("6_archetypus")


@pytest.fixture(scope="module")
def happy(tmp_path_factory) -> tuple[Path, dict]:
    return build_page_tree(tmp_path_factory.mktemp("happy"), "happy")


@pytest.fixture(scope="module")
def page_other(tmp_path_factory) -> tuple[Path, dict]:
    """a1 read as `other`, over which the record detector finds no record."""
    return build_page_tree(tmp_path_factory.mktemp("page-other"), "page-other")


@pytest.fixture(scope="module")
def page_review(tmp_path_factory) -> tuple[Path, dict]:
    return build_page_tree(tmp_path_factory.mktemp("page-review"), "page-review")


def _copy(tree: tuple[Path, dict], tmp_path: Path) -> tuple[Path, dict]:
    root, options = tree
    shutil.copytree(root, tmp_path / "runs")
    return tmp_path / "runs", options


def _recense(root: Path, options: dict, scenario: str) -> None:
    result = run_stage(root, RUN_ID, scenario, "pipeline/5_recensor/run.py", **options)
    assert result.returncode in (0, 3), result.stderr


def _archetypus(root: Path, options: dict, scenario: str):
    return run_stage(root, RUN_ID, scenario, "pipeline/6_archetypus/run.py", **options)


def _establish(root: Path, options: dict, scenario: str):
    """The real Recensor, then the Archetypus."""
    _recense(root, options, scenario)
    return _archetypus(root, options, scenario)


def _records(root: Path) -> dict[str, dict]:
    tree = RunTree(root, RUN_ID)
    return {
        record["payload"]["act_key"]: record
        for record in (
            tree.read_artifact(ARCHETYPUS, "archetypus", entry["artifact_id"])
            for entry in tree.build_manifest(ARCHETYPUS)["artifacts"]
            if entry["kind"] == "archetypus"
        )
    }


def test_every_accepted_page_reading_is_established_from_its_own_act_region(page_other, tmp_path):
    root, options = _copy(page_other, tmp_path)
    result = _establish(root, options, "page-other")
    assert result.returncode == 0, result.stderr
    records = _records(root)
    assert sorted(records) == ["p1:1", "p1:2", "p2:1"]
    tree = RunTree(root, RUN_ID)
    for key, record in records.items():
        payload = archetypus.validate_record(record["payload"])
        assert payload["kind"] == ("other" if key == "p1:1" else "act")
        reading = tree.read_artifact_reference(
            payload["perlectio_ref"], stage="perlector", kind="perlectio"
        )
        assert payload["text"] == reading["payload"]["text"]
        region = tree.read_artifact_reference(
            reading["payload"]["act_region_ref"], stage="perlector", kind="act-region"
        )
        assert payload["regions"] == [verify_reading_region_lineage(tree, tree.read_run(), region)]
        assert reading["payload"]["act_region_ref"] in record["inputs"]
        # A page reader saw no Pass-A draft: its self-revisions were not measured.
        assert payload["uncertainty"]["self_revisions"] is None
    # The doubt mark in p1:1's answer travels as its uncertain span.
    assert records["p1:1"]["payload"]["uncertainty"]["uncertain_spans"]
    index = json.loads(tree.resolve(tree.index_path(ARCHETYPUS)).read_text(encoding="utf-8"))
    assert {row["act_key"] for row in index["rows"]} == {"p1:1", "p1:2", "p2:1"}


def test_a_held_reading_and_its_page_reach_no_record(page_review, tmp_path):
    root, options = _copy(page_review, tmp_path)
    result = _establish(root, options, "page-review")
    assert result.returncode == 0, result.stderr
    assert sorted(_records(root)) == ["p1:1", "p1:2"]


def _accept_held(root: Path, act_key: str) -> None:
    """The review of a held reading accepting it with no release: never the Recensor's."""
    forge_page_review(root, RUN_ID, act_key, "accepted", hold_codes=[], release=None)


def test_the_recensor_accepting_a_held_reading_is_refused(page_review, tmp_path):
    root, options = _copy(page_review, tmp_path)
    _recense(root, options, "page-review")
    _accept_held(root, "p2:1")
    result = _archetypus(root, options, "page-review")
    assert result.returncode == 2
    assert "may not resurrect a held reading" in result.stderr
    assert _records(root) == {}


def test_the_index_reconciles_with_the_recensors_accepted_readings_only(page_review, tmp_path):
    root, options = _copy(page_review, tmp_path)
    result = _establish(root, options, "page-review")
    assert result.returncode == 0, result.stderr
    tree = RunTree(root, RUN_ID)
    context = page_context(root, RUN_ID, "page-review", options)
    rows = reading_acts(context)
    reviews = current_page_reviews(context, rows)
    accepted = sorted(
        row["act_key"] for row in rows if reviews[row["act_id"]]["outcome"] == "accepted"
    )
    assert accepted == ["p1:1", "p1:2"]
    index = json.loads(tree.resolve(tree.index_path(ARCHETYPUS)).read_text(encoding="utf-8"))
    assert sorted(row["act_key"] for row in index["rows"]) == accepted


def test_a_page_record_is_closed_on_its_own_region_fields(happy, tmp_path):
    root, options = _copy(happy, tmp_path)
    assert _establish(root, options, "happy").returncode == 0
    payload = _records(root)["p1:2"]["payload"]
    region = {**payload["regions"][0], "witness_covered": True}
    with pytest.raises(archetypus.SchemaRefusal, match="closed region schema"):
        archetypus.validate_record(_resealed(payload, regions=[region]))
    with pytest.raises(archetypus.SchemaRefusal, match="neither act nor other"):
        archetypus.validate_record(_resealed(payload, kind="stamp"))


def _resealed(payload: dict, **changes) -> dict:
    record = {key: value for key, value in payload.items() if key != "self_hash"}
    record.update(changes)
    record["self_hash"] = self_hash(record)
    return record


def test_index_rows_name_each_reading_s_kind(page_other, tmp_path):
    root, options = _copy(page_other, tmp_path)
    assert _establish(root, options, "page-other").returncode == 0
    tree = RunTree(root, RUN_ID)
    index = json.loads(tree.resolve(tree.index_path(ARCHETYPUS)).read_text(encoding="utf-8"))
    assert {row["act_key"]: row["kind"] for row in index["rows"]} == {
        "p1:1": "other",
        "p1:2": "act",
        "p2:1": "act",
    }


def test_a_perlectio_carrying_a_layer_no_page_reading_records_stops_the_review(happy, tmp_path):
    root, options = _copy(happy, tmp_path)
    # p2:1's Perlectio carries a layer a page reading never records. The
    # page-read denominator every later stage reads refuses it, so the review
    # stops and no reading is established.
    directory = root / RUN_ID / "4_perlector" / "artifacts" / "perlectio"
    for path in directory.glob("*.json"):
        record = json.loads(path.read_text("utf-8"))
        if record["payload"]["page_ordinal"] == 2:
            record["payload"]["annotations"] = []
            record["self_hash"] = self_hash(
                {key: value for key, value in record.items() if key != "self_hash"}
            )
            path.write_bytes(canonical_bytes(record))
    rewitness_stage_boundary(RunTree(root, RUN_ID), "perlector")
    result = run_stage(root, RUN_ID, "happy", "pipeline/5_recensor/run.py", **options)
    assert result.returncode == 2
    assert "Perlectio carries fields other than its closed schema" in result.stderr
    assert _records(root) == {}


def test_a_confirmed_no_act_page_establishes_its_other_reading(tmp_path):
    # `page-no-act` with nothing running across the break, read by Chandra and
    # Churro alone, the page witnesses a confirmed no-act page can have.
    root, options = build_page_tree(
        tmp_path, "page-no-act-unbroken", floor=2, absent=("attestator_2",)
    )
    result = _establish(root, options, "page-no-act-unbroken")
    assert result.returncode == 0, result.stderr
    context = page_context(root, RUN_ID, "page-no-act-unbroken", options)
    reviews = current_page_reviews(context, reading_acts(context)).values()
    [review] = [review for review in reviews if review["payload"]["act_key"] == "p2:1"]
    assert review["outcome"] == "accepted"
    assert review["payload"]["release"]["hold_codes"] == [NO_ACT_ON_PAGE_HOLD]
    assert _records(root)["p2:1"]["payload"]["kind"] == "other"


def test_a_blinded_run_proves_custody_by_each_witness_s_testimonium(tmp_path_factory):
    root, options = build_page_tree(
        tmp_path_factory.mktemp("blinded"), "happy", witness_context="blinded"
    )
    result = _establish(root, options, "happy")
    assert result.returncode == 0, result.stderr
    assert sorted(_records(root)) == ["p1:1", "p1:2", "p2:1"]


# --- the constructor, called directly on a tree the Recensor reviewed ---------------


class _FeedTree:
    """The run tree, with the page feed a reading names read through `change`."""

    def __init__(self, tree, change):
        self._tree, self._change = tree, change

    def __getattr__(self, name):
        return getattr(self._tree, name)

    def read_artifact_reference(self, reference, **where):
        record = self._tree.read_artifact_reference(reference, **where)
        if where.get("kind") == "page-feed":
            record = copy.deepcopy(record)
            self._change(record["payload"])
        return record


def _constructor(tree_dir, tmp_path, scenario="happy", accept_held=()):
    root, options = _copy(tree_dir, tmp_path)
    _recense(root, options, scenario)
    for key in accept_held:
        _accept_held(root, key)
    context = page_context(root, RUN_ID, scenario, options)
    rows = {row["act_key"]: row for row in reading_acts(context)}
    reviews = current_page_reviews(context, list(rows.values()))
    testimonia = current_page_testimonia(context, sealed_proposal_regions(context))

    def establish(key, **row_changes):
        row = {**rows[key], **row_changes}
        review = reviews[rows[key]["act_id"]]
        return archetypus.establish_from_accepted_page_reading(
            context,
            row=row,
            review_ref=context.artifact_ref(RECENSOR, "review", review["artifact_id"]),
            page_testimonia=testimonia.get(row["page_id"], []),
        )

    return context, rows, establish


def test_the_constructor_refuses_a_reading_that_carries_holds(page_review, tmp_path):
    # The row is told it was read; the reading itself still says it is held.
    _context, _rows, establish = _constructor(
        page_review, tmp_path, "page-review", accept_held=["p2:1"]
    )
    with pytest.raises(archetypus.FatalAccounting, match="a held reading is never written"):
        establish("p2:1", disposition="read", hold_codes=[])


def test_the_constructor_refuses_a_region_the_row_did_not_count(happy, tmp_path):
    _context, rows, establish = _constructor(happy, tmp_path)
    with pytest.raises(archetypus.FatalAccounting, match="act-region the denominator counted"):
        establish("p1:2", region_ref=rows["p1:1"]["region_ref"])


@pytest.mark.parametrize(
    ("change", "refusal"),
    [
        (lambda feed: feed.update(witnesses=[]), "Lectio nuda"),
        (
            lambda feed: feed["witnesses"][0].update(
                testimonium_ref={"relative_path": "3_attestatores/x.json", "sha256": "0" * 64}
            ),
            "not its chair's current page Testimonium",
        ),
        (
            lambda feed: feed["witnesses"][0].update(witness_label="someone-else"),
            "not the label this run's regime gives",
        ),
        (
            lambda feed: feed["witnesses"][0].update(letter="Z"),
            "dissent against exactly the witnesses its feed showed",
        ),
    ],
    ids=["no-witness", "superseded", "label", "dissent-letters"],
)
def test_the_constructor_refuses_a_reading_whose_witness_custody_fails(
    happy, tmp_path, change, refusal
):
    context, _rows, establish = _constructor(happy, tmp_path)
    context.tree = _FeedTree(context.tree, change)
    with pytest.raises(archetypus.FatalAccounting, match=refusal):
        establish("p1:2")
