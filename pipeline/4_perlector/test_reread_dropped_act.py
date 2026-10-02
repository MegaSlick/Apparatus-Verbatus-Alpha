"""An operator re-read that leaves out an act the reading it supersedes named holds the page.

An operator re-read becomes the page's current reading, and the act records of the
readings it supersedes are no longer counted. Nothing compares the re-read's entries
with the superseded ones; what keeps an act from vanishing is that the re-read is
accounted against the same page evidence as any reading: the ink, witness units,
detected lines and detector records the dropped act covered are no longer covered,
so the re-read's own page accounting holds, the page-read denominator measures that
again, and the Recensor holds the page as a review item.

The tree is the operator re-read end-to-end tree: page 1's two acts held on their
first reading, and a person's page re-ask of page 1.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_live_reading_seam_e2e import PAGE_ANSWERS, designated  # noqa: E402, F401
from test_operator_actions_e2e import reading_held  # noqa: E402, F401
from test_operator_reread_e2e import _page_readings, _read_again  # noqa: E402
from test_review_decisions_e2e import RUN_ID, _copy, _decide, _recense  # noqa: E402

from common.contracts.stages import PERLECTOR  # noqa: E402
from common.page_review import held_by_recensor, published_units  # noqa: E402
from common.runtree.store import RunTree  # noqa: E402
from common.stage import EXIT_COMPLETE, EXIT_HELD  # noqa: E402


def _without_act_two(how: str) -> str:
    """Page 1's answer with its second act left out, or its ids set aside."""
    answer = json.loads(PAGE_ANSWERS[1])
    dropped = answer["acts"].pop(1)
    if how == "set-aside":
        answer["set_aside"] = [{"id": i, "reason": "not an act"} for i in dropped["cites"]]
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
    assert len(current["payload"]["answer"]["acts"]) == 1
    holds = set(_accounting_holds(tree.root, current))
    assert expected <= holds

    # The Recensor accepts the denominator over the re-read and holds the page.
    assert _recense(tree) == EXIT_HELD
    run = RunTree(tree.root, RUN_ID)
    [page] = [item for item in held_by_recensor(run) if item["what"] == "page 1"]
    assert expected <= set(page["hold_codes"])
    # The dropped act is no current unit; the page hold is what carries it to a person.
    keys = {unit["payload"]["act_key"] for unit in published_units(run)}
    assert "p1:1" in keys and "p1:2" not in keys
