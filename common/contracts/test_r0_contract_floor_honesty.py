"""R0 falsification tests: floor honesty (D2/D3, brief priority 2).

Written blind, from /out/R0_CONTRACT_NOTE.md (v2) before the R0 build chamber runs.
Every test here must fail RED on the chamber's base commit (main 176b09e) because the
behaviour it checks is not yet built.

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

import inspect

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


def test_coverage_schema_has_no_room_for_a_page_granularity_only_contribution():
    """D2: an act-level floor met only by page-granularity contributions must be
    named as such in the receipt, distinct from a genuine act-level completed read.

    `_validate_coverage`'s coverage schema is a CLOSED set today (exactly six
    pre-R0 fields; `set(coverage) != required` refuses anything else outright), so
    a coverage record that tries to carry this fact is refused wholesale rather
    than validated against it. This is what "the floor is never lowered" has to
    mean structurally: there is nowhere yet for the honest, narrower claim to live,
    so a build that lands D2 must widen this exact schema.
    """
    coverage = _base_coverage(page_granularity_only=1)
    try:
        _validate_coverage(coverage)
    except SchemaRefusal as error:
        pytest.fail(
            "the Recensor partition receipt's coverage schema refuses a "
            f"page-granularity-only field outright ({error}); D2 requires floor "
            "accounting to be able to record a page-granularity-only contribution "
            "that never satisfies an act-level floor, and this closed schema has no "
            "field for it yet"
        )


def test_coverage_schema_distinguishes_health_unrecorded_from_a_healthy_read():
    """D3: content_health.truncated == None (not recorded) is not a shortfall, but
    the receipt must still record it as health-unrecorded -- distinct from an
    ordinary healthy completed read, which the six pre-R0 fields cannot express.
    """
    coverage = _base_coverage(by_outcome={"read": 2, "genuinely-empty": 1}, health_unrecorded=1)
    try:
        _validate_coverage(coverage)
    except SchemaRefusal as error:
        pytest.fail(
            "the Recensor partition receipt's coverage schema refuses a "
            f"health_unrecorded field outright ({error}); D3 requires "
            "content_health.truncated == None to be visibly distinguished from a "
            "healthy read, and this closed schema has no field for it yet"
        )


def test_coverage_schema_admits_an_unaligned_shortfall_class():
    """D3: `unaligned` derives from attachment absence/uncovered spans and is a
    named shortfall class alongside `failed` and `truncated` -- no new member is
    added to the witness OUTCOME vocabulary itself, so this must live in the
    coverage/receipt accounting rather than in `common/contracts/outcomes.py`'s
    closed ATTESTATORES vocabulary.
    """
    coverage = _base_coverage(shortfalls={"failed": 0, "truncated": 0, "unaligned": 1})
    try:
        _validate_coverage(coverage)
    except SchemaRefusal as error:
        pytest.fail(
            "the Recensor partition receipt's coverage schema refuses a "
            f"shortfalls field outright ({error}); D3 requires failed/truncated/"
            "unaligned to be named shortfall classes the receipt can carry, and "
            "this closed schema has no field for them yet"
        )


def test_witness_coverage_has_no_signature_room_for_per_act_attachment_facts():
    """D2: the floor computation itself must take attachment facts into account
    to tell a genuinely act-witnessing chair from a completed-but-unattached
    (page-granularity-only) one. `witness_coverage`'s signature today is
    `(chair_outcomes, configured_floor)` and carries nothing about attachments.
    """
    signature = inspect.signature(outcomes.witness_coverage)
    attachment_aware = {"attachments", "act_attachments", "attached_chairs"} & set(
        signature.parameters
    )
    assert attachment_aware, (
        "common/contracts/outcomes.py::witness_coverage has no parameter carrying "
        "per-chair act-level attachment facts (checked for one of "
        f"{sorted({'attachments', 'act_attachments', 'attached_chairs'})} in "
        f"{sorted(signature.parameters)}); D2 requires the floor arithmetic to "
        "distinguish a completed-but-unattached (page-granularity-only) chair from "
        "one that actually attached to this act, and nothing here does that yet"
    )
