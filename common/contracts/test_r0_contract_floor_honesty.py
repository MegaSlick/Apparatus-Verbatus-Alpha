"""R0 contract tests: floor honesty (D2/D3, brief priority 2).

Written blind, from /out/R0_CONTRACT_NOTE.md (v2) before the R0 build chamber ran, so
every test here failed red on the chamber's base commit. The behaviour has since landed:
`_validate_coverage` carries the granularity fields and `witness_coverage` takes
attachment facts. The file now guards that behaviour instead of falsifying its absence.

D2 (interim floor arithmetic): fixture-declared attachments COUNT toward the act floor
in the skeleton. On any path where a page-witness has NO attachment for an act, that
act's floor accounting records a page-granularity-only contribution, which never
satisfies an act-level floor: acts stay visibly under-witnessed at act granularity,
runs stay partial, and the run report states page-granularity completion as
page-granularity. The floor is never lowered.

D3 (shortfall classes): no new outcome member in the witness vocabulary. Shortfalls
derive as: `failed` from the existing outcome vocabulary; `truncated` from
content_health.truncated == True; `unaligned` from attachment absence/uncovered spans.
content_health.truncated == None (not recorded) is not a recorded failure and is not a
shortfall, but the receipt records it as health-unrecorded. Receipt rederivation
(common/recensor_receipt.py::_validate_coverage) follows the same derivation.
"""

from __future__ import annotations

from typing import Final

import pytest

from common.contracts import outcomes
from common.contracts.errors import SchemaRefusal
from common.recensor_receipt import _validate_coverage


def _base_coverage(**overrides) -> dict:
    coverage = {
        "configured": 3,
        "floor": 3,
        "by_outcome": {"read": 3},
        "by_class": {"completed": 3, "unresolved": 0, "failed": 0},
        "under_witnessed": False,
        "unresolved_chairs": 0,
    }
    coverage.update(overrides)
    return coverage


# --- Real inputs to the real writer -------------------------------------------
#
# The three D2/D3 tests below used to hand `_validate_coverage` a hand-built
# record and assert only that the field was admitted. A regression that kept the
# field names and ignored their values passed all three (CodeRabbit, PR #92).
# They now generate the record from `witness_coverage` itself and assert the
# value it computes, so ignoring an attachment fact, an unrecorded health flag or
# an uncovered span turns a test red.

CHAIRS: Final = {"chair_1": "read", "chair_2": "read", "chair_3": "read"}


def _fact(**overrides) -> dict:
    """One chair's act-level attachment fact: attached, comparable, healthy.

    The default is the honest best case -- the chair attached to this act on
    presented-region evidence, its reported span is comparable with the act's,
    and its content health was recorded as untruncated. Each test below varies
    exactly one field of one chair away from this baseline and asserts the
    difference that variation makes to the coverage record.
    """
    fact = {
        "attached": True,
        "comparable": True,
        "truncated": False,
        "attachment_basis": "presented-region",
    }
    fact.update(overrides)
    return fact


def _coverage(**chair_facts) -> dict:
    """Generate an act's coverage record: three reading chairs, a floor of 3."""
    attachments = {chair: _fact() for chair in CHAIRS}
    attachments.update(chair_facts)
    return outcomes.witness_coverage(CHAIRS, 3, attachments=attachments)


def test_coverage_schema_has_room_for_a_page_granularity_only_contribution():
    """D2: an act-level floor met only by page-granularity contributions is named
    as such in the receipt, distinct from a genuine act-level completed read.

    The v2 coverage schema carries `page_granularity_only`, so `_validate_coverage`
    accepts the field and checks it against the rest of the record rather than
    refusing the whole record for naming it. This is what "the floor is never
    lowered" means structurally: the honest, narrower claim has somewhere to live.
    """
    # under_witnessed=True is the internally-consistent value here (audit fix,
    # F-S4): with 3 completed chairs and 1 of them page-granularity-only, only 2
    # act-level reads meet the floor of 3, so the act IS under-witnessed. The
    # original fixture left `under_witnessed=False` (from `_base_coverage`'s
    # default) uncorrected, which happened to validate before this audit's fix
    # only because the rederivation check was itself skippable for a record
    # naming just one of the three granularity fields -- exactly the gap F-S4
    # closes. This test's own assertion (a new field is accepted, not refused)
    # is unchanged; only its previously-inconsistent input is corrected.
    coverage = _base_coverage(page_granularity_only=1, under_witnessed=True)
    try:
        _validate_coverage(coverage)
    except SchemaRefusal as error:
        pytest.fail(
            "the Recensor partition receipt's coverage schema refused a "
            f"page-granularity-only field ({error}); D2 requires floor accounting "
            "to record a page-granularity-only contribution that never satisfies "
            "an act-level floor, and the v2 schema carries that field"
        )


def test_health_unrecorded_is_counted_and_is_not_a_shortfall():
    """D3: content_health.truncated == None (not recorded) is not a shortfall, but
    the receipt still records it as health-unrecorded -- distinct from an ordinary
    healthy completed read.

    Generated by `witness_coverage` from real chair facts, and asserted as a
    difference against the same three chairs with recorded, healthy content: the
    unrecorded chair moves `health_unrecorded` from 0 to 1 and moves nothing else.
    A writer that admitted the field but ignored the flag would leave both
    records identical, which is the regression this asserts against.
    """
    healthy = _coverage()
    unrecorded = _coverage(chair_1=_fact(truncated=None, health_unrecorded=True))

    assert healthy["health_unrecorded"] == 0
    assert unrecorded["health_unrecorded"] == 1
    # Not a shortfall, and not a demotion: unrecorded health says nobody measured
    # whether the reading was whole, which is a visible gap in the receipt and
    # never an accusation against the chair.
    assert (
        unrecorded["shortfalls"]
        == healthy["shortfalls"]
        == {
            "failed": 0,
            "truncated": 0,
            "unaligned": 0,
        }
    )
    assert unrecorded["under_witnessed"] is healthy["under_witnessed"] is False
    assert unrecorded["page_granularity_only"] == healthy["page_granularity_only"] == 0

    # And the v2 receipt schema carries the record the writer actually produced.
    for coverage in (healthy, unrecorded):
        try:
            _validate_coverage(coverage)
        except SchemaRefusal as error:
            pytest.fail(
                "the Recensor partition receipt's coverage schema refused a record "
                f"`witness_coverage` itself produced ({error}); D3 requires "
                "content_health.truncated == None to be visibly distinguished from a "
                "healthy read, and the v2 schema carries that field"
            )


def test_an_unaligned_shortfall_is_counted_from_attachment_and_span_evidence():
    """D3: `unaligned` derives from attachment absence or an uncovered span, and
    is a named shortfall class alongside `failed` and `truncated` -- no new member
    is added to the closed ATTESTATORES OUTCOME vocabulary.

    Both derivations are exercised against the real writer: a chair that never
    attached to this act, and a chair that attached but whose reported span is not
    comparable with the act's. Each raises `unaligned` by exactly one from the
    aligned baseline of zero, and neither invents a witness outcome -- `by_outcome`
    still reads three `read` chairs in every case.
    """
    aligned = _coverage()
    unattached = _coverage(
        chair_3=_fact(attached=False, comparable=False, attachment_basis="unattached")
    )
    uncovered_span = _coverage(chair_2=_fact(comparable=False))

    assert aligned["shortfalls"] == {"failed": 0, "truncated": 0, "unaligned": 0}
    assert unattached["shortfalls"] == {"failed": 0, "truncated": 0, "unaligned": 1}
    assert uncovered_span["shortfalls"] == {"failed": 0, "truncated": 0, "unaligned": 1}
    # The shortfall lives in the coverage accounting, not in the vocabulary: the
    # chairs are still three ordinary `read` outcomes.
    for coverage in (aligned, unattached, uncovered_span):
        assert coverage["by_outcome"] == {"read": 3}
        try:
            _validate_coverage(coverage)
        except SchemaRefusal as error:
            pytest.fail(
                "the Recensor partition receipt's coverage schema refused a record "
                f"`witness_coverage` itself produced ({error}); D3 requires "
                "failed/truncated/unaligned to be named shortfall classes the "
                "receipt can carry, and the v2 schema carries those counts"
            )


# --- CodeRabbit (pre-push CLI, R0 PR loop): the acceptance halves above prove the
# schema has room for each honest fact; these prove the validator still argues
# with a dishonest value of the same fact, so deleting the validation cannot
# leave all six tests green.


def test_a_page_granularity_count_beyond_the_configured_chairs_is_refused():
    """More page-only contributions than chairs is an arithmetic impossibility."""
    coverage = _base_coverage(page_granularity_only=4, under_witnessed=True)
    with pytest.raises(SchemaRefusal):
        _validate_coverage(coverage)


def test_a_health_unrecorded_count_beyond_the_configured_chairs_is_refused():
    """Unrecorded health is counted per chair; a count above three chairs lies."""
    coverage = _base_coverage(by_outcome={"read": 2, "genuinely-empty": 1}, health_unrecorded=4)
    with pytest.raises(SchemaRefusal):
        _validate_coverage(coverage)


def test_a_non_integer_unaligned_shortfall_is_refused():
    """A shortfall class carries a count, and only a count."""
    coverage = _base_coverage(shortfalls={"failed": 0, "truncated": 0, "unaligned": -1})
    with pytest.raises(SchemaRefusal):
        _validate_coverage(coverage)


def test_an_unattached_chair_does_not_satisfy_the_act_level_floor():
    """D2: the floor computation reads the attachment facts, so a
    completed-but-unattached chair contributes page granularity and never an
    act-level read. Asserted as the difference the contract names, not as the
    presence of a parameter: three `read` chairs against a floor of 3 meet it
    when all three attached, and fall short the moment one did not.

    "The floor is never lowered" is exactly this: the unattached chair is still
    completed business, still counted in `by_class`, and still visible in the
    receipt -- it simply does not buy a place at the act-level floor.
    """
    attached = _coverage()
    unattached = _coverage(
        chair_3=_fact(attached=False, comparable=False, attachment_basis="unattached")
    )

    assert attached["under_witnessed"] is False
    assert unattached["under_witnessed"] is True
    assert attached["page_granularity_only"] == 0
    assert unattached["page_granularity_only"] == 1
    assert attached["shortfalls"]["unaligned"] == 0
    assert unattached["shortfalls"]["unaligned"] == 1
    # The chair is not deleted or demoted out of the partition by falling short.
    assert (
        unattached["by_class"]
        == attached["by_class"]
        == {
            "completed": 3,
            "unresolved": 0,
            "failed": 0,
        }
    )
    assert unattached["configured"] == 3


# --- Audit-and-repair regression (F-S4) ------------------------------------------
#
# Sonnet audit-and-repair seat 1, R0. S4 prime suspect: "the permissive partial-
# granularity path (has_complete_granularity guard) skips the under_witnessed
# rederivation for records carrying SOME granularity fields. Can a dishonest
# record thread that needle?" -- yes, confirmed before this audit's fix.


def test_an_under_witnessed_act_cannot_claim_otherwise_by_omitting_two_of_three_granularity_fields():
    """F-S4 (tampering battery): "an under-witnessed act whose receipt says
    otherwise" must be refused, even when the record supplies only
    `page_granularity_only` and omits `health_unrecorded`/`shortfalls`.

    Before this audit's fix, `_validate_coverage`'s under_witnessed rederivation
    ran only when a coverage record carried ALL THREE granularity fields
    (`has_complete_granularity = granularity_fields <= set(coverage)`) or NONE of
    them. A record naming exactly one or two -- itself never produced by the real
    writer (`witness_coverage()` always emits all three together), but nothing
    stopped a hand-built or tampered record from doing so -- skipped the check
    entirely. This record claims `under_witnessed=False` with 3 completed chairs,
    a floor of 3, and 1 of them page-granularity-only: only 2 act-level reads
    actually met the floor, so the act IS under-witnessed, and the record is lying.
    """
    coverage = _base_coverage(page_granularity_only=1, under_witnessed=False)
    with pytest.raises(SchemaRefusal, match="under_witnessed"):
        _validate_coverage(coverage)
