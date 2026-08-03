"""Spec 02, test 4 — Absence.

"A `state = "absent"` seat produces the explicit absence record — not an
exception, not a silent skip — and counts against the witness floor."

Both halves matter and they pull in opposite directions. An absence that raised
would be indistinguishable from a broken configuration; an absence that was
simply left out of the roster would be a silent skip, and the run would look
fully witnessed while a seat nobody configured quietly stopped counting. So an
absent seat stays in the roster, resolves to a value rather than an exception,
and is one fewer configured witness against a floor that does not shrink to match.

The end of that sentence — that a run carries the absence all the way into its
export — is `test_an_explicit_absent_witness_is_a_visible_not_run_and_counts_against_floor`
in `pipeline/orchestrator/test_orchestrator_acceptance.py`, over the real stage
programs. This file covers the registry's half.
"""

import pytest

from common.contracts.outcomes import witness_coverage
from common.seats.errors import ConfigurationRefusal, UnresolvedSeatRefusal
from common.seats.models import AbsentSeat, SeatIdentity

from .conftest import absent_seat, config_of, hf_seat, registry_for, serving_details

DIGEST = "d" * 64
REASON = "no fourth witness is configured for alpha"


@pytest.fixture
def roster(tmp_path):
    """Three configured witnesses and one explicit absence, against a floor of 4."""
    seats = {
        "attestator_1": hf_seat("attestator_1", DIGEST),
        "attestator_2": hf_seat("attestator_2", DIGEST),
        "attestator_3": hf_seat("attestator_3", DIGEST),
        "attestator_4": absent_seat(REASON),
    }
    return registry_for(config_of(tmp_path, seats, witness_floor=4), tmp_path)


# --- Not an exception ----------------------------------------------------------------


def test_resolving_an_absent_seat_returns_its_record_rather_than_raising(roster):
    # Called directly, with no pytest.raises: that is the whole point.
    absence = roster.resolve("attestator_4")

    assert absence == AbsentSeat(role="attestator_4", reason=REASON)
    assert absence.to_record() == {
        "role": "attestator_4",
        "state": "absent",
        "reason": REASON,
    }


def test_an_absent_seat_must_carry_a_reason(tmp_path):
    """ "Absent" with no reason is a seat that vanished, not one that was retired."""
    for table in ({"state": "absent"}, {"state": "absent", "reason": "  "}):
        with pytest.raises(ConfigurationRefusal, match="reason"):
            config_of(tmp_path, {"attestator_4": table})


def test_an_absent_seat_may_not_carry_a_pin_field(tmp_path):
    """A half-filled absence is the shape that later gets read as configured."""
    table = {"state": "absent", "reason": REASON, "repo": "fixture-org/attestator_4"}
    with pytest.raises(ConfigurationRefusal, match="forbidden"):
        config_of(tmp_path, {"attestator_4": table})


# --- Not a silent skip ---------------------------------------------------------------


def test_an_absent_witness_stays_in_the_roster_the_run_binds(roster):
    """`run.json`'s `witness_seats` is the roster, absences included. A seat
    dropped from it would make an under-witnessed run look fully witnessed, and
    reopening the same run id after the drop would not even be refused."""
    assert roster.config.witness_seats == (
        "attestator_1",
        "attestator_2",
        "attestator_3",
        "attestator_4",
    )


def test_an_absent_witness_is_a_deficit_against_the_floor(roster):
    status = roster.config.witness_floor_status()

    assert status.configured_roles == ("attestator_1", "attestator_2", "attestator_3")
    assert status.absent_roles == ("attestator_4",)
    assert status.configured_count == 3
    assert status.floor == 4
    assert status.deficit == 1
    assert status.meets_floor is False


def test_the_floor_is_met_when_every_seat_it_counts_is_configured(tmp_path):
    seats = {
        "attestator_1": hf_seat("attestator_1", DIGEST),
        "attestator_2": hf_seat("attestator_2", DIGEST),
        "attestator_3": hf_seat("attestator_3", DIGEST),
    }
    status = config_of(tmp_path, seats, witness_floor=3).witness_floor_status()

    assert status.deficit == 0
    assert status.meets_floor is True


def test_the_absence_reaches_the_coverage_record_as_an_unresolved_seat(roster):
    """The roster's own arithmetic and the act-level arithmetic have to agree.

    `witness_coverage` counts completed-class outcomes against the same floor, so
    an absent seat recorded `not-run` for an act lands as unresolved and forces
    `under_witnessed` — which is what "counts against the witness floor" means
    once the run is running rather than only being configured.
    """
    status = roster.config.witness_floor_status()
    outcomes = {role: "read" for role in status.configured_roles}
    outcomes.update({role: "not-run" for role in status.absent_roles})

    coverage = witness_coverage(outcomes, configured_floor=status.floor)

    assert coverage["configured"] == 4
    assert coverage["by_class"]["completed"] == 3
    assert coverage["unresolved_seats"] == 1
    assert coverage["under_witnessed"] is True


# --- An absence has no artifact, so it has nothing to ensure or receipt --------------


def test_ensure_and_receipt_both_refuse_an_absent_seat_naming_it(roster):
    """Neither call may fall through to some other seat's snapshot. Both take a
    `SeatIdentity`, so the only way to ask is to hand over another seat's — which
    is exactly the substitution the refusal exists to name."""
    stolen = SeatIdentity(**{**roster.resolve("attestator_1").to_record(), "role": "attestator_4"})

    with pytest.raises(UnresolvedSeatRefusal) as ensured:
        roster.ensure(stolen)
    assert ensured.value.seat == "attestator_4"
    assert REASON in str(ensured.value)

    with pytest.raises(UnresolvedSeatRefusal) as receipted:
        roster.receipt(stolen, serving_details())
    assert receipted.value.seat == "attestator_4"


def test_an_absence_is_never_promoted_into_a_configured_seat_by_resolving_it_twice(roster):
    assert roster.resolve("attestator_4") == roster.resolve("attestator_4")
    assert not isinstance(roster.resolve("attestator_4"), SeatIdentity)
