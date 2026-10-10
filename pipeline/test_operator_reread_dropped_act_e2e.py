"""An operator re-read that drops an act the reading it supersedes named holds the page.

An operator re-read becomes the page's current reading, and the act records of the
readings it supersedes are no longer counted. Every way of dropping act 2 here holds
every entry of the re-read `superseded-act-not-read`, which compares the re-read's
acts with the ones it replaces and reads no detection, so it holds the same on a run
with no record detector. On this tree, which has witnesses, Surya lines, a record
detector and an ink map, the page accounting holds too:

- left out: its witness units, lines, records and ink go unaccounted for;
- its ids set aside: a substantial set-aside, and its ink unread;
- merged into act 1 without its text: the record detector's merged detection, and
  the witness text the reading lacks;
- merged into act 1 with its text: the record detector's merged detection;
- relabelled `other`: the record detector's record read as other.

A re-read that keeps both acts holds nothing for them. The tree is the operator
re-read end-to-end tree: page 1's two acts held on their first reading, and a
person's page re-ask of page 1.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_live_reading_seam_e2e import PAGE_ANSWERS, designated  # noqa: F401
from test_operator_actions_e2e import reading_held  # noqa: F401
from test_operator_reread_e2e import _ask_again, _page_readings, _read_again
from test_review_decisions_e2e import RUN_ID, _copy, _decide, _recense

from common.contracts.stages import PERLECTOR
from common.page_path import SUPERSEDED_ACT_NOT_READ
from common.page_review import held_by_recensor, published_units
from common.runtree.store import RunTree
from common.stage import EXIT_COMPLETE, EXIT_HELD


def _without_act_two(how: str) -> str:
    """Page 1's answer with its second act dropped in the way `how` names."""
    answer = json.loads(PAGE_ANSWERS[1])
    if how == "other":
        answer["entries"][1]["kind"] = "other"
        return json.dumps(answer)
    first, dropped = answer["entries"]
    answer["entries"] = [first]
    if how == "set-aside":
        answer["set_aside"] = [{"id": i, "reason": "not an act"} for i in dropped["cites"]]
    elif how.startswith("merged"):
        first["cites"] = [*first["cites"], *dropped["cites"]]
        if how == "merged-with-text":
            first["text"] = f"{first['text']} {dropped['text']}"
            first["continues_to_next_page"] = dropped["continues_to_next_page"]
    return json.dumps(answer)


def _accounting_holds(root: Path, reading: dict) -> list[str]:
    tree = RunTree(root, RUN_ID)
    for entry in tree.build_manifest(PERLECTOR)["artifacts"]:
        if entry["kind"] != "page-accounting":
            continue
        record = tree.read_artifact(PERLECTOR, "page-accounting", entry["artifact_id"])
        if record["attempt_id"] == reading["attempt_id"]:
            return record["payload"]["holds"]
    raise AssertionError("the re-read has no page accounting")


@pytest.mark.parametrize(
    ("how", "expected"),
    [
        # `unread-ink` is measured on both pages too, as a review flag that holds nothing.
        ("omitted", {"unaccounted-witness-unit"}),
        ("set-aside", {"set-aside-substantial"}),
        ("merged", {"merged-detection", "witness-text-not-read"}),
        ("merged-with-text", {"merged-detection"}),
        ("other", {"record-read-as-other"}),
    ],
)
def test_a_re_read_that_drops_an_act_holds_its_page_for_review(
    reading_held,  # noqa: F811
    tmp_path,
    how,
    expected,
):
    tree = _copy(reading_held, tmp_path)
    _decide(tree.root, "p1", "re-ask")
    assert _recense(tree) == EXIT_HELD
    assert _read_again(tree, tmp_path / "reader", _without_act_two(how)) == EXIT_COMPLETE

    current = _page_readings(tree.root)[3]
    acts = [e for e in current["payload"]["answer"]["entries"] if e["kind"] == "act"]
    assert len(acts) == 1
    holds = set(_accounting_holds(tree.root, current))
    assert expected <= holds
    # Every entry of the re-read holds for the act it did not keep, whatever the
    # detector measured.
    perlectios = _reread_perlectios(tree.root, current)
    assert perlectios
    assert all(SUPERSEDED_ACT_NOT_READ in p["payload"]["holds"] for p in perlectios)

    # The Recensor accepts the denominator over the re-read and holds the page.
    assert _recense(tree) == EXIT_HELD
    run = RunTree(tree.root, RUN_ID)
    held = {item["what"]: set(item["hold_codes"]) for item in held_by_recensor(run)}
    assert expected <= held["page 1"]
    assert SUPERSEDED_ACT_NOT_READ in held["p1:1"]
    keys = {unit["payload"]["act_key"] for unit in published_units(run)}
    assert "p1:1" in keys and ("p1:2" in keys) == (how == "other")


def test_a_re_read_that_keeps_every_act_does_not_hold_for_it(reading_held, tmp_path):  # noqa: F811
    tree = _copy(reading_held, tmp_path)
    _decide(tree.root, "p1", "re-ask")
    assert _recense(tree) == EXIT_HELD
    assert _read_again(tree, tmp_path / "reader") == EXIT_COMPLETE
    current = _page_readings(tree.root)[3]
    perlectios = _reread_perlectios(tree.root, current)
    assert len(perlectios) == 2
    assert not any(SUPERSEDED_ACT_NOT_READ in p["payload"]["holds"] for p in perlectios)
    assert _recense(tree) == EXIT_COMPLETE


def _reread_perlectios(root: Path, reading: dict) -> list[dict]:
    tree = RunTree(root, RUN_ID)
    found = []
    for entry in tree.build_manifest(PERLECTOR)["artifacts"]:
        if entry["kind"] != "perlectio":
            continue
        record = tree.read_artifact(PERLECTOR, "perlectio", entry["artifact_id"])
        if record["payload"]["page_reading_ref"]["relative_path"] == reading["relative_path"]:
            found.append(record)
    return found


@pytest.mark.parametrize("second", ["dropped-again", "restored"])
def test_a_second_re_read_is_planned_against_the_last_reading_that_kept_its_acts(
    reading_held,  # noqa: F811
    tmp_path,
    second,
):
    """A held re-read never becomes the baseline: a second re-read that drops the same
    act holds again, and one that reads it again holds nothing for it."""
    tree = _copy(reading_held, tmp_path)
    _ask_again(tree)
    answer = _without_act_two("merged-with-text")
    assert _read_again(tree, tmp_path / "reader-3", answer) == EXIT_COMPLETE
    third = _page_readings(tree.root)[3]
    assert all(
        SUPERSEDED_ACT_NOT_READ in p["payload"]["holds"]
        for p in _reread_perlectios(tree.root, third)
    )
    assert _recense(tree) == EXIT_HELD

    _ask_again(tree)
    again = answer if second == "dropped-again" else PAGE_ANSWERS[1]
    assert _read_again(tree, tmp_path / "reader-4", again) == EXIT_COMPLETE
    fourth = _page_readings(tree.root)[4]
    perlectios = _reread_perlectios(tree.root, fourth)
    assert perlectios
    held = [SUPERSEDED_ACT_NOT_READ in p["payload"]["holds"] for p in perlectios]
    assert held == [second == "dropped-again"] * len(perlectios)
    assert _recense(tree) == (EXIT_HELD if second == "dropped-again" else EXIT_COMPLETE)
