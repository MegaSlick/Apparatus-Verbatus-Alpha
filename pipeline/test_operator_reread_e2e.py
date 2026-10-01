"""A person sends a held page through the Perlector again, and the new reading is current.

The tree is `test_operator_actions_e2e.py`'s `reading_held`: page 1's two
entries are held on their own reading. A person records a page `re-ask` of
page 1; the next Perlector pass reads page 1 again as attempt 3, bound to that
decision, and the reader now answers it cleanly. That reading becomes the
page's current one: the denominator counts its entries, the Recensor reviews
them, the Coniector reconstructs over them and the Armarium delivers them,
labelled "read on operator re-read". The first reading and its records stay in
the run tree as read, marked superseded by the re-read that names them.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from test_live_reading_seam_e2e import (  # noqa: F401  (`designated` is a fixture)
    PAGE_ANSWERS,
    TIER,
    PageReaderWorld,
    designated,
    perlector,
    run_in_process,
)
from test_operator_actions_e2e import _members, reading_held  # noqa: F401
from test_review_decisions_e2e import (
    RUN_ID,
    _after_recensor,
    _copy,
    _decide,
    _decisions,
    _recense,
)

from common.contracts.stages import PERLECTOR
from common.page_path import OPERATOR_REREAD_FIELD, page_reading_attempt
from common.page_review import held_by_recensor, published_units, superseded_readings
from common.runtree.store import RunTree
from common.stage import EXIT_COMPLETE


def _page_readings(root) -> dict[int, dict]:
    """Page 1's page readings by attempt ordinal."""
    tree = RunTree(root, RUN_ID)
    found = {}
    for entry in tree.build_manifest(PERLECTOR)["artifacts"]:
        if entry["kind"] != "page-reading":
            continue
        record = tree.read_artifact(PERLECTOR, "page-reading", entry["artifact_id"])
        if record["payload"]["page_ordinal"] == 1:
            found[record["payload"]["attempt_ordinal"]] = {
                **record,
                "relative_path": entry["relative_path"],
            }
    return found


def _perlector_files(root) -> dict[str, bytes]:
    """The Perlector's records and blobs; its manifest lists them and changes with them."""
    base = root / RUN_ID / "4_perlector"
    return {
        str(path.relative_to(base)): path.read_bytes()
        for path in base.rglob("*")
        if path.is_file() and path.parent != base
    }


def _read_again(tree: SimpleNamespace, work) -> int:
    reader = PageReaderWorld(tree.catalogue, work, {1: PAGE_ANSWERS[1], 2: PAGE_ANSWERS[2]})
    return run_in_process(
        perlector, tree.root, tree.catalogue, placement_tier=TIER, serving_factory=reader.factory
    )


@pytest.fixture(scope="module")
def reread(reading_held, tmp_path_factory) -> SimpleNamespace:  # noqa: F811
    work = tmp_path_factory.mktemp("reread")
    tree = _copy(reading_held, work)
    before = _perlector_files(tree.root)
    decision = _decide(tree.root, "p1", "re-ask")
    assert _recense(tree) != EXIT_COMPLETE  # the request alone holds the page
    assert _read_again(tree, work / "reader") == EXIT_COMPLETE
    after_perlector = _perlector_files(tree.root)
    recensed = _recense(tree)
    _after_recensor(tree)
    return SimpleNamespace(
        tree=tree,
        work=work,
        decision=decision,
        before=before,
        after_perlector=after_perlector,
        recensed=recensed,
    )


def test_a_page_re_ask_becomes_attempt_3_bound_to_its_decision(reread):
    readings = _page_readings(reread.tree.root)
    assert max(readings) == 3
    third = readings[3]
    assert third["attempt_id"] == page_reading_attempt(third["subject_id"], 3)
    block = third["payload"][OPERATOR_REREAD_FIELD]
    [decision] = block["decisions"]
    assert decision["approval_ref"]["relative_path"] == reread.decision
    assert decision["approval_ref"] in third["inputs"]
    # It supersedes every earlier reading of the page, in attempt order.
    assert [ref["relative_path"] for ref in block["supersedes"]] == [
        readings[o]["relative_path"] for o in sorted(readings) if o < 3
    ]
    assert all(ref in third["inputs"] for ref in block["supersedes"])
    assert third["payload"]["reask"] is None


def test_earlier_readings_are_kept_as_read_and_marked_superseded(reread):
    for path, data in reread.before.items():
        assert reread.after_perlector[path] == data, path
    readings = _page_readings(reread.tree.root)
    superseded = superseded_readings(RunTree(reread.tree.root, RUN_ID))
    assert {readings[o]["relative_path"] for o in readings if o < 3} == superseded


def test_the_denominator_counts_the_current_reading(reread):
    """The Recensor's receipt binds the page to its re-read, and its units are the re-read's."""
    root = reread.tree.root / RUN_ID
    receipt = json.loads((root / "run-health" / "recensor-partition-receipt.json").read_text())
    [page] = [row for row in receipt["pages"] if row["page_ordinal"] == 1]
    third = _page_readings(reread.tree.root)[3]
    assert page["reading_ref"]["relative_path"] == third["relative_path"]
    assert (page["reask_ref"], page["reask"]) == (None, None)
    tree = RunTree(reread.tree.root, RUN_ID)
    units = {unit["payload"]["act_key"]: unit for unit in published_units(tree)}
    for key in ("p1:1", "p1:2"):
        assert units[key]["payload"]["page_reading_ref"]["relative_path"] == third["relative_path"]


def test_the_recensor_reviews_the_new_reading_and_the_re_ask_goes_stale(reread):
    assert reread.recensed == EXIT_COMPLETE
    assert held_by_recensor(RunTree(reread.tree.root, RUN_ID)) == []
    decisions = _decisions(reread.tree.root)
    assert [s["decision"] for s in decisions["stale"]] == ["re-ask"]
    assert decisions["requests"] == []


def test_the_export_delivers_the_re_read_labelled(reread):
    members = _members(reread.tree.root)
    acts = {
        row["act_key"]: row for row in map(json.loads, members["acts.jsonl"].decode().splitlines())
    }
    for key in ("p1:1", "p1:2"):
        assert acts[key]["category"] == "delivered"
        assert acts[key]["reading"] == "read on operator re-read"
    readings = {
        row["act_key"]: row["reading"]
        for row in json.loads(members["sources.json"])["act_readings"]
    }
    assert readings["p2:1"] == "first reading"


def test_another_pass_reads_no_page_again(reread, tmp_path):
    tree = _copy(reread.tree, tmp_path)
    before = _perlector_files(tree.root)
    assert _read_again(tree, tmp_path / "reader") == EXIT_COMPLETE
    assert _perlector_files(tree.root) == before
