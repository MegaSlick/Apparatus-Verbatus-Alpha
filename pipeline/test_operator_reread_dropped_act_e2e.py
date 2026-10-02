"""An operator re-read that drops an act the reading it supersedes named, and what holds.

An operator re-read becomes the page's current reading, and the act records of the
readings it supersedes are no longer counted. Nothing compares the re-read's entries
with the superseded ones; the re-read is accounted against the page's evidence like
any reading. On this tree, which has witnesses, Surya lines, a record detector and
an ink map, every way of dropping act 2 holds the page:

- left out: its witness units, lines, records and ink go unaccounted for;
- its ids set aside: a substantial set-aside, and its ink unread;
- merged into act 1 without its text: the record detector's merged detection, and
  the witness text the reading lacks;
- merged into act 1 with its text: the record detector's merged detection alone;
- relabelled `other`: the record detector's record read as other.

The last two hold only through the record detector: a run without one does not hold
them.

The tree is the operator re-read end-to-end tree: page 1's two acts held on their
first reading, and a person's page re-ask of page 1.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_live_reading_seam_e2e import PAGE_ANSWERS, designated  # noqa: F401
from test_operator_actions_e2e import reading_held  # noqa: F401
from test_operator_reread_e2e import _page_readings, _read_again
from test_review_decisions_e2e import RUN_ID, _copy, _decide, _recense

from common.contracts.stages import PERLECTOR
from common.page_review import held_by_recensor, published_units
from common.runtree.store import RunTree
from common.stage import EXIT_COMPLETE, EXIT_HELD


def _without_act_two(how: str) -> str:
    """Page 1's answer with its second act dropped in the way `how` names."""
    answer = json.loads(PAGE_ANSWERS[1])
    if how == "other":
        answer["acts"][1]["kind"] = "other"
        return json.dumps(answer)
    first, dropped = answer["acts"]
    answer["acts"] = [first]
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
        ("omitted", {"unaccounted-witness-unit", "unread-ink"}),
        ("set-aside", {"set-aside-substantial", "unread-ink"}),
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
    acts = [e for e in current["payload"]["answer"]["acts"] if e["kind"] == "act"]
    assert len(acts) == 1
    holds = set(_accounting_holds(tree.root, current))
    assert expected <= holds

    # The Recensor accepts the denominator over the re-read and holds the page.
    assert _recense(tree) == EXIT_HELD
    run = RunTree(tree.root, RUN_ID)
    [page] = [item for item in held_by_recensor(run) if item["what"] == "page 1"]
    assert expected <= set(page["hold_codes"])
    # The page hold is what carries the dropped act to a person.
    keys = {unit["payload"]["act_key"] for unit in published_units(run)}
    assert "p1:1" in keys and ("p1:2" in keys) == (how == "other")
