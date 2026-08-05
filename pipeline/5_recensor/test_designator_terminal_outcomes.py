"""Every terminal Designator outcome is accounted for, or named as unhandled.

`held` is not the only Designator outcome that ends an act. `common/contracts/
outcomes.py` also makes `excluded` and `failed` terminal, and both entry points in
`pipeline/5_recensor/run.py` tested only for `held` — so either would fall through
to the reading path and be reported as "reached the Recensor with no reading at
all". That message is false, and it is the shape invariant #10 exists to catch: an
imbalance invented by the check rather than found by it.

The Designator emits neither outcome today, which is what made this latent. The
outcome table already promises them, so the gap is named rather than left to be
discovered by whoever teaches the Designator to use one.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from common.contracts.errors import FatalAccounting
from common.contracts.outcomes import terminal_category
from common.contracts.stages import DESIGNATOR

ROOT = Path(__file__).resolve().parents[2]


def _load_recensor():
    path = ROOT / "pipeline/5_recensor/run.py"
    spec = importlib.util.spec_from_file_location("recensor_run_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    spec.loader.exec_module(module)
    return module


recensor = _load_recensor()


@pytest.mark.parametrize("outcome", ["excluded", "failed"])
def test_a_terminal_designator_outcome_is_named_rather_than_called_a_missing_reading(outcome):
    with pytest.raises(FatalAccounting) as caught:
        recensor._refuse_an_unhandled_designator_terminal({"act_id": "a1", "outcome": outcome})

    message = str(caught.value)
    assert outcome in message
    assert "a1" in message
    assert "no reading at all" not in message, (
        "the old, false message survived: this act was never going to have a reading"
    )


def test_the_outcome_that_flows_onward_is_left_alone():
    """`proposed` is the ordinary case and terminates nothing."""
    assert (
        recensor._refuse_an_unhandled_designator_terminal({"act_id": "a1", "outcome": "proposed"})
        is None
    )


def test_held_is_terminal_too_and_is_the_one_this_stage_actually_handles():
    """Guards the claim in the refusal message. If `held` ever stopped being terminal,
    or another outcome became terminal, this test says so before a run does."""
    terminal = {
        outcome
        for outcome in ("proposed", "excluded", "held", "failed")
        if terminal_category(DESIGNATOR, outcome) is not None
    }
    assert terminal == {"excluded", "held", "failed"}, (
        "the Designator's terminal set moved; the Recensor's handling must move with it"
    )
