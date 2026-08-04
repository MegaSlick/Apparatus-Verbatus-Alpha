"""The data-handling gate's digest-checked approval-record boundary."""

import pytest

from common.contracts.approval import (
    ApprovalRecordReference,
    approval_record_reference_from_record,
    build_approval_record,
    data_gate_policy_hash,
    require_current_data_gate_approval,
)
from common.contracts.canonical import canonical_bytes, digest_bytes
from common.contracts.errors import ApprovalRefusal

POLICY = {
    "storage_roots": ["private/"],
    "logs": {"names": False, "image_bytes": False},
    "version": 1,
}


def approval_bytes(*, target_hash: str | None = None, action: str = "data-gate") -> bytes:
    record = build_approval_record(
        subject_ids=["data-handling-policy"],
        action=action,
        reason="approved for real input under this exact policy",
        target_version_hash=target_hash or data_gate_policy_hash(POLICY),
        timestamp="2026-08-04T12:00:00Z",
    )
    return canonical_bytes(record)


def approval_path(data: bytes) -> str:
    return f"receipts/sha256/{digest_bytes(data)}.json"


def reference_for(data: bytes) -> ApprovalRecordReference:
    return ApprovalRecordReference(approval_path(data), digest_bytes(data))


def reader_holding(data: bytes):
    def read(relative_path: str) -> bytes:
        assert relative_path == approval_path(data)
        return data

    return read


def test_current_data_gate_approval_is_returned_only_through_its_checked_reference():
    data = approval_bytes()
    reference = reference_for(data)

    record = require_current_data_gate_approval(POLICY, reference, reader_holding(data))

    assert record["action"] == "data-gate"
    assert record["target_version_hash"] == data_gate_policy_hash(POLICY)
    assert reference.to_record() == {
        "relative_path": approval_path(data),
        "sha256": digest_bytes(data),
    }


def test_a_serialized_reference_is_not_accepted_as_the_gate_authority():
    """The door receives a typed reference, never a caller-supplied dict."""
    data = approval_bytes()

    with pytest.raises(ApprovalRefusal, match="ApprovalRecordReference"):
        require_current_data_gate_approval(
            POLICY,
            {"relative_path": approval_path(data), "sha256": digest_bytes(data)},
            reader_holding(data),
        )


def test_a_persisted_reference_is_decoded_once_before_a_consumer_reads_it():
    data = approval_bytes()

    parsed = approval_record_reference_from_record(reference_for(data).to_record())

    assert isinstance(parsed, ApprovalRecordReference)
    assert parsed.to_record() == reference_for(data).to_record()


def test_missing_data_gate_approval_is_a_named_refusal():
    with pytest.raises(ApprovalRefusal, match="data-gate approval is missing"):
        require_current_data_gate_approval(POLICY, None, reader_holding(b""))


def test_data_gate_approval_bound_to_an_old_policy_is_a_named_stale_refusal():
    data = approval_bytes(target_hash="a" * 64)

    with pytest.raises(ApprovalRefusal, match="data-gate approval is stale"):
        require_current_data_gate_approval(POLICY, reference_for(data), reader_holding(data))


def test_reference_digest_mismatch_is_refused_before_the_record_is_trusted():
    data = approval_bytes()
    wrong_reference = ApprovalRecordReference(f"receipts/sha256/{'b' * 64}.json", "b" * 64)

    with pytest.raises(ApprovalRefusal, match="approval reference digest mismatch"):
        require_current_data_gate_approval(POLICY, wrong_reference, lambda _path: data)


@pytest.mark.parametrize(
    "bad_reference",
    [
        {},
        {"relative_path": "receipts/sha256/missing.json"},
        {"relative_path": "../approval.json", "sha256": "a" * 64},
        {
            "relative_path": f"receipts/sha256/{'a' * 64}.json",
            "sha256": "NOT-A-DIGEST",
        },
    ],
)
def test_malformed_data_gate_approval_reference_is_refused(bad_reference):
    with pytest.raises(ApprovalRefusal, match="data-gate approval reference"):
        require_current_data_gate_approval(POLICY, bad_reference, reader_holding(b""))


def test_otherwise_safe_path_that_is_not_the_digest_named_receipt_path_is_refused():
    data = approval_bytes()
    reference = ApprovalRecordReference("approvals/current.json", digest_bytes(data))

    with pytest.raises(ApprovalRefusal, match="content-addressed path"):
        require_current_data_gate_approval(POLICY, reference, reader_holding(data))


def test_missing_referenced_approval_bytes_are_a_named_refusal():
    reference = ApprovalRecordReference(f"receipts/sha256/{'a' * 64}.json", "a" * 64)

    def missing(_relative_path: str) -> bytes:
        raise FileNotFoundError("approval record")

    with pytest.raises(ApprovalRefusal, match="could not be read"):
        require_current_data_gate_approval(POLICY, reference, missing)


def test_a_digest_checked_record_for_a_different_action_does_not_open_the_gate():
    data = approval_bytes(action="other")

    with pytest.raises(ApprovalRefusal, match="action 'other', not 'data-gate'"):
        require_current_data_gate_approval(POLICY, reference_for(data), reader_holding(data))


def test_an_invalid_self_hash_does_not_open_the_gate_even_when_the_reference_matches():
    record = build_approval_record(
        subject_ids=["data-handling-policy"],
        action="data-gate",
        reason="approved for real input under this exact policy",
        target_version_hash=data_gate_policy_hash(POLICY),
        timestamp="2026-08-04T12:00:00Z",
    )
    record["reason"] = "edited after approval"
    data = canonical_bytes(record)

    with pytest.raises(ApprovalRefusal, match="self-hash"):
        require_current_data_gate_approval(POLICY, reference_for(data), reader_holding(data))


def test_policy_content_that_cannot_be_canonicalized_fails_closed():
    data = approval_bytes()

    with pytest.raises(ApprovalRefusal, match="canonical policy hash could not be computed"):
        require_current_data_gate_approval(
            {"retention_days": 1.5}, reference_for(data), reader_holding(data)
        )
