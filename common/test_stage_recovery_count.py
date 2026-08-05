"""The one recovery-reread denominator, and the drift that made it three.

`recovery_region_count` is the shared reader three stages ask for the same fact.
Before it existed, `pipeline/5_recensor/run.py::recovery_state` refused a region
whose `origin` fell outside `{"proposal", "recovery"}`, while the Archetypus and
Armarium copies asked only whether it equalled `"recovery"` and counted anything
else as zero — so one tree was fatal at the Recensor and reconciled two stages
later, with the unplaceable region silently out of the denominator at exactly the
two stages that decide whether a recrop was reread before its text is established.
"""

from __future__ import annotations

import pytest

from common.contracts.errors import FatalAccounting
from common.stage import recovery_region_count


def region(origin):
    return {"payload": {"origin": origin}}


def test_only_recovery_regions_are_counted():
    regions = [region("proposal"), region("recovery"), region("proposal"), region("recovery")]

    assert recovery_region_count("a1", regions) == 2


def test_no_regions_at_all_is_a_denominator_of_zero_not_an_error():
    assert recovery_region_count("a1", []) == 0


@pytest.mark.parametrize("origin", ["recrop", "", None, "RECOVERY", 7, {"origin": "recovery"}])
def test_an_unrecognized_origin_is_fatal_rather_than_counted_as_zero(origin):
    """The whole point of the shared reader. Counting an unknown origin as zero is
    what let the same tree reconcile at one stage and fail at another."""
    with pytest.raises(FatalAccounting, match="unrecognized origin"):
        recovery_region_count("a1", [region("proposal"), region(origin)])


@pytest.mark.parametrize("payload", [None, [], "recovery", 3])
def test_a_region_with_no_object_payload_is_fatal(payload):
    with pytest.raises(FatalAccounting, match="no object payload"):
        recovery_region_count("a1", [{"payload": payload}])


def test_the_act_id_is_named_in_every_refusal():
    """A denominator failure that does not say which act it is about sends a reader
    hunting through the whole run tree."""
    with pytest.raises(FatalAccounting, match="act-42"):
        recovery_region_count("act-42", [region("nonsense")])
