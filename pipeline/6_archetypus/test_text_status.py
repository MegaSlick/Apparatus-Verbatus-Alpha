"""Status honesty at the schema.

An empty `text` with `established` status, and any `no_readable_text` record, are
refused directly at the pure validation function, not only observed as a side
effect of a full run. The status derivation itself is tested beside it in
`common/contracts/test_contracts_algebra.py`.
"""

import pytest

from common.contracts.errors import SchemaRefusal
from conftest import load_stage

archetypus = load_stage("6_archetypus")


def test_established_may_not_carry_empty_text():
    with pytest.raises(SchemaRefusal, match="may not carry empty"):
        archetypus.validate_text_status("", "established")


def test_established_may_not_carry_all_whitespace_text():
    with pytest.raises(SchemaRefusal, match="may not carry empty"):
        archetypus.validate_text_status("   ", "established")


def test_established_and_partial_with_real_text_are_fine():
    archetypus.validate_text_status("some real ink", "established")
    archetypus.validate_text_status("some ink, some gaps", "partial")


def test_an_empty_reading_is_never_established():
    """No page review carries a blank proof, so a reading with no text and no gap
    is held upstream; a record of one would deliver nothing as a reading."""
    with pytest.raises(SchemaRefusal, match="never establishes a reading with no text"):
        archetypus.validate_text_status("", "no_readable_text")


def test_an_unknown_status_is_refused():
    with pytest.raises(SchemaRefusal, match="not one of"):
        archetypus.validate_text_status("some real ink", "probably-fine")
