"""The operator's run notification carries the systemic alarm, through the real notify path.

`verbatus run` drives the real orchestrator over the fixture's `page-review`
scenario, which holds one of its two pages after the Recensor. The run seals a
policy of 1/50 from one held page (`_alarming`), so that one page sounds the
alarm. The notifier is the shell notifier, so the message goes through
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


def _alarming(surface: OperatorSurface, tmp_path: Path, monkeypatch) -> None:
    """Have every orchestrator run seal 1/50 from one held page."""
    config = tmp_path / "review.toml"
    config.write_text(
        '[review]\nmax_held_page_share = "1/50"\nmin_systemic_held_pages = 1\n', encoding="utf-8"
    )
    run = surface._run_orchestrator
    monkeypatch.setattr(
        surface,
        "_run_orchestrator",
        lambda command: run([*command, "--review-config", str(config)]),
    )


def test_a_systemic_hold_is_notified_as_one_through_the_notify_script(tmp_path, monkeypatch):
    messages: list[str] = []
    sent: list[tuple[str, str]] = []
    shell = notify_bridge.shell_notifier()

    def notifier(event: str, message: str):
        sent.append((event, message))
        return shell(event, message)

    surface = OperatorSurface(ROOT, tmp_path / "state", present=messages.append, notifier=notifier)
    _alarming(surface, tmp_path, monkeypatch)
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


def test_an_advance_past_a_systemic_hold_still_notifies_it_at_run_and_export(tmp_path, monkeypatch):
    """A person's advance passes the stop; the alarm still leads the run's and export's notices."""
    from conftest import advance_held_recensor

    sent: list[tuple[str, str]] = []
    shell = notify_bridge.shell_notifier()

    def notifier(event: str, message: str):
        sent.append((event, message))
        return shell(event, message)

    surface = OperatorSurface(
        ROOT, tmp_path / "state", present=lambda _line: None, notifier=notifier
    )
    _alarming(surface, tmp_path, monkeypatch)
    with pytest.raises(OperatorError):
        surface.run(run_id="systemic", scenario="page-review")
    advance_held_recensor(tmp_path / "state" / "runs", "systemic")
    sent.clear()
    with pytest.raises(OperatorError) as held:
        surface.run(run_id="systemic", scenario="page-review")
    assert held.value.code is ErrorCode.RUN_HELD
    share = (
        "1 of 2 page(s) are held after the recensor, more than the sealed limit of 1/50 "
        "(config/review.toml)"
    )
    [(event, message)] = sent
    assert event == "decision"
    assert message.startswith(
        f"Verbatus run systemic has a systemic problem and needs a decision: {share}"
    )
    # The run's other hold reasons follow the alarm.
    assert "act p2:1 is held-for-review" in message

    sent.clear()
    with pytest.raises(OperatorError) as partial:
        surface.export(run_id="systemic")
    assert partial.value.code is ErrorCode.EXPORT_PARTIAL
    [(event, message)] = sent
    assert event == "milestone"
    assert "not complete: " in message
    assert f"; the run has a systemic problem: {share}" in message
