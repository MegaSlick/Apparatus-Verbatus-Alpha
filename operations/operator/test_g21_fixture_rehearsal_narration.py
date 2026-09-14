"""Regression coverage for G21 (F001/F030): fixture-rehearsal narration must
never name a page or act the running scenario did not actually touch.

`_declared_work` (operations/operator/surface.py) reads every `[[page]]` row
out of the single fixed fixture declaration file (`proof/skeleton_fixture.toml`)
regardless of which `--scenario` is actually running, and three call sites
(the opening "Checking ..." line, the "Working next: ..." line, and the
closing "Pages accounted for: ..." line) print those names unconditionally.
`pipeline/1_exemplar/door.py::fixture_pages_for_scenario` already exists and
answers exactly the question these call sites need -- which pages this
scenario actually activates -- but `_declared_work` does not call it.

This module is intentionally standalone (not appended to test_surface.py)
because `operations/operator/surface.py` is owned by another seat this wave;
the fix and its exact diff are proposed in this build seat's report rather
than applied here. The test below is written against the *fixed* behavior and
is expected to fail until that diff lands, so it is marked `xfail` naming
G21 -- once `_declared_work` is scenario-aware, removing the `xfail` marker
is the whole job of picking this back up.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .errors import OperatorError
from .surface import OperatorSurface

ROOT = Path(__file__).resolve().parents[2]


def _rehearsal_messages(tmp_path: Path, *, scenario: str) -> list[str]:
    """Run one real fixture rehearsal (no submission folder, no live pod) and
    return every line the operator surface printed.

    Uses the real runner (the actual orchestrator subprocess over the tiny
    synthetic fixture), not a stub, because the defect is in what `_declared_work`
    prints against the *real* export a run produces -- a stubbed child never
    writes the artifacts the closing narration reads. A held scenario (e.g.
    `review`) raises `OperatorError(RUN_HELD)` *after* every narration line
    below is already printed and captured, so that outcome is not itself a
    test failure here -- only what was said before it is.
    """

    messages: list[str] = []
    surface = OperatorSurface(
        ROOT,
        tmp_path / "operator-state",
        present=messages.append,
    )
    try:
        surface.run(run_id=f"g21-{scenario}", scenario=scenario)
    except OperatorError:
        pass
    return messages


@pytest.mark.xfail(
    reason=(
        "G21: _declared_work names every fixture page regardless of scenario; "
        "awaiting the other seat's surface.py fix (see this build's report for "
        "the proposed diff, filtering through "
        "pipeline/1_exemplar/door.py::fixture_pages_for_scenario)"
    ),
    strict=False,
)
def test_happy_scenario_narration_never_names_a_page_it_never_touched(
    tmp_path: Path,
) -> None:
    """The default `happy` scenario touches pages 1 and 2 only; page 3 exists
    solely for other scenarios (`ink-free-page`, `ink-free-page-unwitnessed`).
    None of the three narration lines may name it.
    """

    messages = _rehearsal_messages(tmp_path, scenario="happy")

    assert messages, "the rehearsal produced no narration at all"
    assert not any("page 3" in line for line in messages), (
        "the happy scenario's narration named page 3, which this scenario never "
        f"touches: {messages!r}"
    )


@pytest.mark.xfail(
    reason=(
        "G21: _declared_work names every fixture page regardless of scenario; "
        "awaiting the other seat's surface.py fix (see this build's report for "
        "the proposed diff, filtering through "
        "pipeline/1_exemplar/door.py::fixture_pages_for_scenario)"
    ),
    strict=False,
)
def test_review_scenario_narration_never_names_a_page_it_never_touched(
    tmp_path: Path,
) -> None:
    """`review` is a two-page scenario (a1 recovered, a2 held) like `happy`;
    page 3 is equally absent from it and must stay off its narration too.
    """

    messages = _rehearsal_messages(tmp_path, scenario="review")

    assert messages, "the rehearsal produced no narration at all"
    assert not any("page 3" in line for line in messages), (
        "the review scenario's narration named page 3, which this scenario "
        f"never touches: {messages!r}"
    )
