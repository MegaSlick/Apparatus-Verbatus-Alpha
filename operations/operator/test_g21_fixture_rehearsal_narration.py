"""Regression coverage for G21 (F001/F030/F106): fixture-rehearsal narration
must never name a page or act the running scenario did not actually touch,
and never disagree with the total printed beside it.

`_declared_work` (operations/operator/surface.py) used to read every
`[[page]]`/`[[act]]` row out of the single fixed fixture declaration file
(`proof/skeleton_fixture.toml`) regardless of which `--scenario` was actually
running. Two problems followed from that, and both are fixed now:

1. The opening "Checking ..." and "Working next: ..." lines named a page or
   act a scenario-gated fixture row never activates for this scenario (e.g.
   page 3, gated to `ink-free-page`). Fixed by filtering `_declared_work`
   through `pipeline/1_exemplar/door.py::fixture_pages_for_scenario` -- the
   same question a real door application answers.
2. The closing "Pages/Acts accounted for: ..." line named the *same* static,
   pre-run declaration beside a *real*, post-run total -- so it disagreed
   with its own total whenever a run minted an act the fixture cannot
   declare (`ink-free-page`'s fallback act) or a page was refused after being
   declared (`refused-page`). A static declaration can never answer for a
   real outcome. Fixed by reading `_exported_work` from the completed run's
   own Armarium export record instead, once one exists.

This module is intentionally standalone (not appended to test_surface.py)
because `operations/operator/surface.py` was, at the time this test was
written, owned by another seat -- the fix landed later, in this diff.
"""

from __future__ import annotations

from pathlib import Path

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


def test_ink_free_page_scenarios_closing_line_names_its_minted_fallback_act(
    tmp_path: Path,
) -> None:
    """`ink-free-page` mints a fallback act (`page-fallback:3`) at runtime that
    no fixture declaration can name in advance. F030: the closing line must
    still name it, from the real export, or it silently omits an act that was
    actually part of the run while its own total counts it."""

    messages = _rehearsal_messages(tmp_path, scenario="ink-free-page")

    accounted = next(line for line in messages if line.startswith("Pages accounted for"))
    assert accounted == (
        "Pages accounted for: page 1, page 2, page 3 (3 total). "
        "Acts accounted for: act a1, act a2, act page-fallback:3 (3 total)."
    ), accounted


def test_refused_page_scenarios_closing_line_matches_its_own_total(
    tmp_path: Path,
) -> None:
    """`refused-page` has the Door refuse page 2's declared bytes. F030: a
    refused page is still part of the run's own page census (its outcome is
    refused, not absent) and the closing line must name it consistently with
    the total beside it -- and it must not name page 3, which this scenario
    never touches."""

    messages = _rehearsal_messages(tmp_path, scenario="refused-page")

    accounted = next(line for line in messages if line.startswith("Pages accounted for"))
    assert accounted == (
        "Pages accounted for: page 1, page 2 (2 total). "
        "Acts accounted for: act a1, act a2 (2 total)."
    ), accounted
