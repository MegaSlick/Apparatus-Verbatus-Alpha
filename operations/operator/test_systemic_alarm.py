"""The operator's run notification carries the systemic alarm, through the real notify path.

`verbatus run` drives the real orchestrator over the fixture's `page-review`
scenario, which holds one of its two pages after the Recensor: more than the
sealed 1/50. The notifier is the shell notifier, so the message goes through
`operations/notify/notify.sh`, which the suite's conftest points at the
reserved test sink: attempted, suppressed, never delivered.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from operations.operator import notify_bridge
from operations.operator.errors import ErrorCode, OperatorError
from operations.operator.surface import OperatorSurface

ROOT = Path(__file__).resolve().parents[2]


def test_a_systemic_hold_is_notified_as_one_through_the_notify_script(tmp_path):
    messages: list[str] = []
    sent: list[tuple[str, str]] = []
    shell = notify_bridge.shell_notifier()

    def notifier(event: str, message: str):
        sent.append((event, message))
        return shell(event, message)

    surface = OperatorSurface(ROOT, tmp_path / "state", present=messages.append, notifier=notifier)
    with pytest.raises(OperatorError) as held:
        surface.run(run_id="systemic", scenario="page-review")

    assert held.value.code is ErrorCode.RUN_HELD
    [(event, message)] = sent
    assert event == "decision"
    assert message.startswith(
        "Verbatus run systemic has a systemic problem and needs a decision: 1 of 2 page(s) are "
        "held after the recensor, more than the sealed limit of 1/50"
    )
    assert "Phone notification: suppressed (test sink)." in messages
