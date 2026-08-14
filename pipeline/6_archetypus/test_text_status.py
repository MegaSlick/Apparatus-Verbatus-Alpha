"""Spec 10, test 3: status honesty at the schema.

`no_readable_text` requires its evidence reference; an empty `text` with
`established` status is refused at the schema — directly, at the pure validation
function, not only observed as a side effect of a full run. The end-to-end version
of the empty-reading case lives in
`pipeline/orchestrator/test_orchestrator_acceptance.py::
test_archetypus_refuses_to_call_an_accepted_empty_reading_blank_without_proof`:
an accepted review over an empty-text reading is refused, not established, because
the current Recensor never supplies the blank-proof evidence reference this status
requires (HANDOFF.md's named cross-stage gap). The success path — a forged review
that does carry that evidence — is exercised beside it, in
`test_archetypus_establishes_no_readable_text_once_the_review_retains_real_blank_proof`.
"""

import importlib.util
from pathlib import Path

import pytest

from common.contracts.errors import SchemaRefusal


def _load_archetypus():
    path = Path(__file__).resolve().parent / "run.py"
    spec = importlib.util.spec_from_file_location("archetypus_run_under_test_status", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


archetypus = _load_archetypus()

REF = {"relative_path": "5_recensor/artifacts/review/art_0000000000000000.json", "sha256": "a" * 64}


def _gap(position: int = 0) -> dict:
    return {"kind": "illegible", "start": position, "end": position, "witness_evidence": []}


# --- derive_text_status: the pure derivation --------------------------------


def test_non_empty_text_with_no_gaps_is_established():
    assert archetypus.derive_text_status("some real ink", []) == "established"


def test_empty_text_with_no_gaps_is_no_readable_text():
    assert archetypus.derive_text_status("", []) == "no_readable_text"


def test_all_whitespace_text_with_no_gaps_is_no_readable_text():
    assert archetypus.derive_text_status("   \n\t ", []) == "no_readable_text"


def test_any_gap_forces_partial_even_with_full_text():
    assert archetypus.derive_text_status("some real ink", [_gap(3)]) == "partial"


def test_any_gap_forces_partial_even_with_empty_text():
    """A whole-act gap: ink believed present, wholly unread. This must never
    read as `no_readable_text` -- Tyrel, 2026-08-05: "we could not read it"
    must never quietly become "there was nothing to read"."""
    assert archetypus.derive_text_status("", [_gap(0)]) == "partial"


def test_an_uncertain_span_alone_does_not_prevent_established():
    """Uncertainty flags characters that ARE present; it is not a gap."""
    annotations = [
        {"kind": "uncertain", "start": 0, "end": 4, "certainty": "low", "alternatives": ["Tyre"]}
    ]
    assert archetypus.derive_text_status("Tyrl is here", annotations) == "established"


# --- validate_text_status: the schema refusal --------------------------------


def test_established_may_not_carry_empty_text():
    with pytest.raises(SchemaRefusal, match="may not carry empty"):
        archetypus.validate_text_status("", "established", None)


def test_established_may_not_carry_all_whitespace_text():
    with pytest.raises(SchemaRefusal, match="may not carry empty"):
        archetypus.validate_text_status("   ", "established", None)


def test_established_with_real_text_and_no_evidence_ref_is_fine():
    archetypus.validate_text_status("some real ink", "established", None)


def test_no_readable_text_requires_its_evidence_reference():
    with pytest.raises(SchemaRefusal, match="requires its evidence reference"):
        archetypus.validate_text_status("", "no_readable_text", None)


def test_no_readable_text_refuses_a_malformed_evidence_reference():
    with pytest.raises(SchemaRefusal, match="requires its evidence reference"):
        archetypus.validate_text_status("", "no_readable_text", {"relative_path": "x"})


def test_no_readable_text_with_a_well_formed_reference_is_fine():
    archetypus.validate_text_status("", "no_readable_text", REF)


def test_no_readable_text_may_not_carry_non_empty_text():
    with pytest.raises(SchemaRefusal, match="must carry empty"):
        archetypus.validate_text_status("actually some text", "no_readable_text", REF)


def test_partial_may_not_carry_a_no_readable_text_evidence_reference():
    with pytest.raises(SchemaRefusal, match="only that status may carry"):
        archetypus.validate_text_status("some ink, some gaps", "partial", REF)


def test_established_may_not_carry_a_no_readable_text_evidence_reference():
    with pytest.raises(SchemaRefusal, match="only that status may carry"):
        archetypus.validate_text_status("some real ink", "established", REF)


def test_an_unknown_status_is_refused():
    with pytest.raises(SchemaRefusal, match="not one of"):
        archetypus.validate_text_status("some real ink", "probably-fine", None)


def test_the_status_enum_is_exactly_the_three_the_spec_names():
    assert archetypus.TEXT_STATUSES == frozenset({"established", "partial", "no_readable_text"})
