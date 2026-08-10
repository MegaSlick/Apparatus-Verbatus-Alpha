"""Canonical serialization and the approval record.

Determinism is not a nicety here. Spec 01's second test is "repeating the identical
command leaves all artifact bytes unchanged", and its resume test is "an
interrupted run resumes from valid artifacts without rewriting them". Both are
claims about bytes, so the serialization has to be the same on every machine before
either is worth asserting.
"""

import pytest

from common.contracts.approval import (
    APPROVER,
    build_approval_record,
    validate_approval_record,
)
from common.contracts.canonical import (
    canonical_bytes,
    digest_of,
    self_hash,
    verify_self_hash,
)
from common.contracts.errors import ApprovalRefusal


def test_key_order_does_not_change_the_bytes():
    assert canonical_bytes({"b": 1, "a": 2}) == canonical_bytes({"a": 2, "b": 1})


def test_the_bytes_carry_no_incidental_whitespace():
    assert canonical_bytes({"a": 1, "b": [1, 2]}) == b'{"a":1,"b":[1,2]}'


def test_text_is_stored_as_itself_rather_than_escaped():
    """This is a project about the very words. A name in a parish register is not
    ASCII, and the stored bytes should be the text."""
    assert canonical_bytes({"name": "Étienne"}) == '{"name":"Étienne"}'.encode()


def test_a_float_is_refused_rather_than_silently_rounded():
    """A float's JSON form is at the mercy of repr, so a float that reached an
    artifact would be a quiet determinism defect. It is a loud one instead."""
    for bad in ({"scale": 1.5}, {"box": [1, 2.0]}, {"nested": {"deep": 0.1}}):
        with pytest.raises(TypeError):
            canonical_bytes(bad)


def test_booleans_are_not_mistaken_for_numbers():
    assert canonical_bytes({"ok": True}) == b'{"ok":true}'


def test_a_non_string_key_is_refused():
    with pytest.raises(TypeError):
        canonical_bytes({1: "one"})


def test_equal_content_digests_equally():
    assert digest_of({"a": [1, 2]}) == digest_of({"a": [1, 2]})
    assert digest_of({"a": [1, 2]}) != digest_of({"a": [2, 1]})


# --- Self-hashing --------------------------------------------------------------


def test_a_record_verifies_its_own_hash():
    record = {"a": 1}
    record["self_hash"] = self_hash(record)
    assert verify_self_hash(record)


def test_an_edited_record_fails_its_own_hash():
    record = {"a": 1}
    record["self_hash"] = self_hash(record)
    record["a"] = 2
    assert not verify_self_hash(record)


def test_a_record_with_no_hash_does_not_pass_by_default():
    assert not verify_self_hash({"a": 1})


def test_a_deeply_nested_record_fails_its_hash_rather_than_crashing():
    """`common/runtree/store.py::_read_json`'s `RecursionError` guard protects the
    JSON scanner only. A record shallow enough to parse but deep enough to exhaust
    the recursion limit while `self_hash` recomputes it — `canonical_bytes` walks
    the structure again, to refuse floats — used to crash `read_artifact` and
    `build_manifest` with a traceback rather than refuse the record. Every caller
    of `verify_self_hash` already treats `False` as "refuse"; catching the
    RecursionError here and returning `False` fixes every one of them at once,
    the same way `_read_json`'s own fix closed the reader-side band."""
    nested: dict = {"leaf": 1}
    for _ in range(2000):
        nested = {"nested": nested}
    record = {"a": nested, "self_hash": "0" * 64}
    assert verify_self_hash(record) is False


# --- The approval record -------------------------------------------------------


def sound_approval(**overrides):
    record = build_approval_record(
        subject_ids=["act_0123456789abcdef"],
        action="exclusion",
        reason="printed index page, no acts; excluded on inspection",
        target_version_hash="d" * 64,
        timestamp="2026-07-30T23:40:00Z",
    )
    record.update(overrides)
    return record


def test_a_sound_approval_validates():
    assert validate_approval_record(sound_approval())["approver"] == APPROVER


def test_only_tyrel_approves():
    """GOVERNANCE: "No automated agent may act as the human in any rule here." The
    schema cannot stop an agent writing the file, but it can stop the file from
    passing under anyone else's name."""
    record = sound_approval()
    record["approver"] = "the session"
    record["self_hash"] = self_hash(record)
    with pytest.raises(ApprovalRefusal) as caught:
        validate_approval_record(record)
    assert "only Tyrel approves" in str(caught.value)


def test_an_approval_edited_after_sealing_is_refused():
    record = sound_approval()
    record["reason"] = "actually it was fine"
    with pytest.raises(ApprovalRefusal) as caught:
        validate_approval_record(record)
    assert "self-hash" in str(caught.value)


def test_an_approval_must_name_what_it_approved():
    for bad in (
        {"subject_ids": []},
        {"subject_ids": [""]},
        {"reason": "   "},
        {"target_version_hash": ""},
        {"target_version_hash": "not-a-policy-hash"},
    ):
        kwargs = {
            "subject_ids": ["act_0123456789abcdef"],
            "action": "exclusion",
            "reason": "because",
            "target_version_hash": "d" * 64,
            "timestamp": "2026-07-30T23:40:00Z",
        }
        kwargs.update(bad)
        with pytest.raises(ApprovalRefusal):
            build_approval_record(**kwargs)


def test_an_unknown_action_is_refused():
    with pytest.raises(ApprovalRefusal):
        build_approval_record(
            subject_ids=["act_0123456789abcdef"],
            action="merge",
            reason="because",
            target_version_hash="d" * 64,
            timestamp="2026-07-30T23:40:00Z",
        )


def test_missing_fields_are_refused():
    checked = 0
    for field in (
        "subject_ids",
        "action",
        "approver",
        "reason",
        "target_version_hash",
        "timestamp",
        "self_hash",
    ):
        record = sound_approval()
        del record[field]
        with pytest.raises(ApprovalRefusal):
            validate_approval_record(record)
        checked += 1
    assert checked == 7


def test_a_hand_written_record_with_blank_fields_is_refused():
    """The validator gates records read off disk, so it must be at least as
    strict as the builder. It was not: a record written by hand with an empty
    reason and an empty target version passed, and its self-hash verified —
    because a hash covers whatever bytes were sealed, not whether they meant
    anything."""
    checked = 0
    for field in ("reason", "target_version_hash", "timestamp"):
        record = sound_approval()
        record[field] = "   "
        record["self_hash"] = self_hash(record)
        with pytest.raises(ApprovalRefusal) as caught:
            validate_approval_record(record)
        assert field in str(caught.value)
        checked += 1
    assert checked == 3


@pytest.mark.parametrize(
    ("field", "value"),
    [("subject_ids", [""]), ("target_version_hash", "not-a-policy-hash")],
)
def test_a_hand_written_record_with_an_uncheckable_target_is_refused(field, value):
    record = sound_approval()
    record[field] = value
    record["self_hash"] = self_hash(record)

    with pytest.raises(ApprovalRefusal):
        validate_approval_record(record)


def test_subject_ids_are_stored_sorted():
    record = build_approval_record(
        subject_ids=["act_bbbbbbbbbbbbbbbb", "act_aaaaaaaaaaaaaaaa"],
        action="exclusion",
        reason="two subjects, one approval",
        target_version_hash="d" * 64,
        timestamp="2026-07-30T23:40:00Z",
    )
    assert record["subject_ids"] == ["act_aaaaaaaaaaaaaaaa", "act_bbbbbbbbbbbbbbbb"]
