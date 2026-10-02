"""An operator re-read's record: the stored page re-asks it answers and the readings it supersedes."""

from __future__ import annotations

import pytest

from common.contracts.approval import ApprovalRecordReference, build_review_decision_record
from common.contracts.canonical import canonical_bytes, digest_bytes
from common.contracts.errors import ContractError, FatalAccounting
from common.page_path import (
    is_whole_page_reading,
    operator_reread_record,
    page_reading_attempt,
    require_operator_reread,
)

RUN = "run-1"
PAGE = "page-2"
FIRST = {"relative_path": "4_perlector/artifacts/page-reading/a.json", "sha256": "1" * 64}
REASK = {"relative_path": "4_perlector/artifacts/page-reading/b.json", "sha256": "2" * 64}


def _stored(decision="re-ask", scope="page", subject=PAGE, run=RUN):
    record = build_review_decision_record(
        run_id=run,
        scope=scope,
        subject_id=subject,
        page_id=subject if scope == "page" else PAGE,
        decision=decision,
        finding="other" if decision == "hold" else None,
        basis_digest="3" * 64,
        reason="read the page again",
        timestamp="2026-10-01T12:00:00Z",
    )
    digest = digest_bytes(canonical_bytes(record))
    return digest, (ApprovalRecordReference(f"receipts/sha256/{digest}.json", digest), record)


def _check(block, stored, supersedes=(FIRST, REASK)):
    return require_operator_reread(
        block,
        run_id=RUN,
        page_id=PAGE,
        stored=stored,
        supersedes=list(supersedes),
        what="the re-read",
    )


def test_a_re_read_is_attempt_3_on_and_a_whole_page_reading():
    assert page_reading_attempt(PAGE, 3) != page_reading_attempt(PAGE, 4)
    assert is_whole_page_reading(1) and is_whole_page_reading(3)
    assert not is_whole_page_reading(2)
    with pytest.raises(ContractError, match="never 0"):
        page_reading_attempt(PAGE, 0)


def test_a_re_read_answers_stored_page_re_asks_and_supersedes_every_earlier_reading():
    digest, (reference, record) = _stored()
    block = operator_reread_record([(reference.to_record(), record)], [FIRST, REASK])
    assert _check(block, {digest: (reference, record)}) == [record["self_hash"]]


@pytest.mark.parametrize(
    "stored",
    [
        _stored(decision="hold"),
        _stored(scope="unit", subject="act-1"),
        _stored(subject="page-9"),
        _stored(run="run-2"),
    ],
    ids=["not-a-re-ask", "a-unit-re-ask", "another-page", "another-run"],
)
def test_a_re_read_answering_anything_but_a_page_re_ask_of_its_page_is_refused(stored):
    digest, (reference, record) = stored
    block = operator_reread_record([(reference.to_record(), record)], [FIRST, REASK])
    with pytest.raises(FatalAccounting, match="not a stored page re-ask of this page"):
        _check(block, {digest: (reference, record)})


def test_a_re_read_naming_a_decision_the_run_does_not_store_is_refused():
    _digest, (reference, record) = _stored()
    block = operator_reread_record([(reference.to_record(), record)], [FIRST, REASK])
    with pytest.raises(FatalAccounting, match="not a stored page re-ask"):
        _check(block, {})
    with pytest.raises(FatalAccounting, match="names no operator decision"):
        _check({"decisions": [], "supersedes": [FIRST, REASK]}, {})


def test_a_re_read_that_does_not_supersede_exactly_the_earlier_readings_is_refused():
    digest, (reference, record) = _stored()
    block = operator_reread_record([(reference.to_record(), record)], [FIRST])
    with pytest.raises(FatalAccounting, match="does not supersede exactly"):
        _check(block, {digest: (reference, record)})
