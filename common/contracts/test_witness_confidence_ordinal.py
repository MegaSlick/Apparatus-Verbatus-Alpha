"""A witness's self-reported confidence is a closed ordinal, refused outside it.

Floats are refused everywhere by `common/contracts/canonical.py::_refuse_floats`, so
a test that only checked a float would pass for the generic no-floats rule. The rule
guarded here is narrower: a confidence claim anywhere in a witness's retained
self-report must name one of a closed set of levels, and an out-of-set string or a
raw integer is a named recording problem, not a reading kept verbatim. Each test
attempts the write through the Attestatores stage's real validation surface and
requires the refusal, rather than checking that no such value happens to appear.
"""

from __future__ import annotations

from conftest import load_stage

attestatores_run = load_stage("3_attestatores")


def test_a_witness_confidence_value_outside_the_closed_ordinal_set_is_refused():
    """A confidence claim outside the closed ordinal set is a named refusal.

    `prepared_response` in the Attestatores stage program is where a witness's
    self-report is validated. This out-of-set value ("more confident than confident
    itself") is exactly what a closed ordinal exists to exclude.
    """
    row = {
        "payload": "SYNTHETIC ACT ONE alpha beta gamma",
        "witness_reported": {"confidence": "more-confident-than-confident-itself"},
    }
    native_payload, witness_reported, capabilities, health, recording_problem = (
        attestatores_run.prepared_response(row)
    )
    assert recording_problem is not None, (
        "a witness_reported.confidence value outside the closed ordinal set was accepted "
        "as a normal reading instead of being refused by the Attestatores self-report path"
    )
    # The refusal must be the confidence rule's own, not some unrelated
    # recording problem.
    assert "confidence" in recording_problem


def test_a_witness_confidence_integer_ordinal_outside_any_declared_scale_is_refused():
    """A bare out-of-range integer confidence is refused the same way a bad string is.

    A closed ordinal is a fixed, named set of levels -- not "any integer", which is
    exactly the unbounded scale a closed ordinal exists to rule out.
    """
    row = {
        "payload": "SYNTHETIC ACT ONE alpha beta gamma",
        "witness_reported": {"confidence": 999_999},
    }
    native_payload, witness_reported, capabilities, health, recording_problem = (
        attestatores_run.prepared_response(row)
    )
    assert recording_problem is not None, (
        "an integer confidence value with no declared closed-ordinal scale was accepted "
        "verbatim instead of being refused by the closed-ordinal confidence rule"
    )
    assert "confidence" in recording_problem


def test_a_nested_confidence_cannot_bypass_the_closed_ordinal_rule():
    """The rule applies to confidence claims anywhere in retained self-report JSON."""
    row = {
        "payload": "SYNTHETIC ACT ONE alpha beta gamma",
        "witness_reported": {"metadata": {"confidence": 999_999}},
    }
    *_, recording_problem = attestatores_run.prepared_response(row)
    assert recording_problem is not None, (
        "a confidence ordinal nested below witness_reported.metadata bypassed the "
        "top-level closed-set check"
    )
    assert "confidence" in recording_problem
