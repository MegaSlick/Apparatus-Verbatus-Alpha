"""Fixture-rehearsal narration names only what the running scenario touched.

The opening "Checking ..." line names only the pages the running scenario
activates (page 3 of the synthetic fixture is gated to other scenarios), and
the closing "accounted for" line names the pages and acts the run's own export
record carries, with a total that matches the names beside it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from .errors import ErrorCode, OperatorError
from .surface import OperatorSurface

ROOT = Path(__file__).resolve().parents[2]


def _rehearsal_messages(tmp_path: Path, *, scenario: str) -> list[str]:
    """Run one real fixture rehearsal (no submission folder, no live pod) and
    return every line the operator surface printed.

    Uses the real runner (the actual orchestrator subprocess over the tiny
    synthetic fixture), not a stub, because the closing narration reads the
    export a real run writes. Both scenarios used here end held for review
    (a continuation join across the page break, and for `page-review` a held
    act too), so the run must raise exactly `RUN_HELD`, after every narration
    line is printed; any other refusal fails the test.
    """

    messages: list[str] = []
    surface = OperatorSurface(
        ROOT,
        tmp_path / "operator-state",
        present=messages.append,
    )
    with pytest.raises(OperatorError) as raised:
        surface.run(run_id=f"g21-{scenario}", scenario=scenario)
    assert raised.value.code is ErrorCode.RUN_HELD, messages
    return messages


def _assert_totals_match_names(messages: list[str]) -> None:
    """Every "X accounted for: a, b (N total)" clause names exactly N things."""
    closing = [line for line in messages if line.startswith("Pages accounted for: ")]
    assert len(closing) == 1, messages
    clauses = re.findall(r"(\w+) accounted for: ([^()]*) \((\d+) total\)", closing[0])
    assert [what for what, _names, _total in clauses] == ["Pages", "Acts"], closing
    for _what, names, total in clauses:
        assert len(names.split(", ")) == int(total), closing


@pytest.mark.parametrize("scenario", ["happy", "page-review"])
def test_narration_names_only_the_two_pages_the_scenario_touches(
    tmp_path: Path, scenario: str
) -> None:
    """Both scenarios touch pages 1 and 2 only; page 3 exists solely for other
    scenarios (`ink-free-page`, `ink-free-page-unwitnessed`). The opening and
    closing lines name exactly pages 1 and 2, and no line names page 3.
    """

    messages = _rehearsal_messages(tmp_path, scenario=scenario)

    assert "Run started. Checking page 1, page 2." in messages, messages
    assert any(
        line.startswith("Pages accounted for: page 1, page 2 (2 total).") for line in messages
    ), messages
    _assert_totals_match_names(messages)
    assert not any("page 3" in line for line in messages), messages
