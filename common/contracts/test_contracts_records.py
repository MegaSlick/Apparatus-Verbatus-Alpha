"""Canonical serialization and the approval record.

Determinism is not a nicety here. Spec 01's second test is "repeating the identical
command leaves all artifact bytes unchanged", and its resume test is "an
interrupted run resumes from valid artifacts without rewriting them". Both are
claims about bytes, so the serialization has to be the same on every machine before
either is worth asserting.
"""

import json

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
    self_hash_refusal,
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


@pytest.mark.parametrize("bad", (1.5, float("nan"), float("inf"), float("-inf")))
def test_a_float_is_refused_rather_than_silently_rounded(bad):
    """A float's JSON form is at the mercy of repr, so a float that reached an
    artifact would be a quiet determinism defect. It is a loud one instead."""
    with pytest.raises(TypeError, match=r"float at \$\.nested\.value"):
        canonical_bytes({"nested": {"value": bad}})


def test_booleans_are_not_mistaken_for_numbers():
    assert canonical_bytes({"ok": True}) == b'{"ok":true}'


def test_nan_spelled_as_text_and_null_characters_remain_lossless_text():
    """Quoted NaN is testimony, not the non-standard JSON number NaN. A NUL is
    escaped in stored JSON and restored on read, so accepting both loses no bytes
    and does not admit a float."""
    encoded = canonical_bytes({"reported": "NaN\0verbatim"})
    assert encoded == b'{"reported":"NaN\\u0000verbatim"}'
    assert json.loads(encoded)["reported"] == "NaN\0verbatim"


@pytest.mark.parametrize("spelling", ("NaN", "Infinity", "-Infinity"))
def test_nonstandard_nan_spellings_parse_to_floats_and_are_refused(spelling):
    parsed = json.loads(spelling)
    with pytest.raises(TypeError, match=r"float at \$"):
        canonical_bytes(parsed)


def test_an_integer_above_the_portable_decimal_limit_is_refused_by_path():
    with pytest.raises(TypeError, match=r"integer at \$\.count exceeds 640 decimal digits"):
        canonical_bytes({"count": 10**640})

    # The largest accepted magnitude has 640 digits and therefore behaves the
    # same even when a host lowers CPython's configurable conversion limit to its
    # documented floor.
    assert canonical_bytes({"count": 10**640 - 1}).startswith(b'{"count":999')


def test_deep_nesting_is_a_named_serializer_refusal_not_recursion_error():
    nested: object = "leaf"
    for _ in range(2000):
        nested = [nested]
    with pytest.raises(TypeError, match="recursive or nests too deeply"):
        canonical_bytes(nested)


# json.loads accepts this value, while canonical UTF-8 cannot encode it.
SURROGATE = "\ud800"


@pytest.mark.parametrize(
    "damaged",
    (
        {"name": SURROGATE},
        {"box": ["ok", SURROGATE]},
        {"nested": {"deep": SURROGATE}},
        {SURROGATE: "in a key"},
    ),
)
def test_a_lone_surrogate_is_refused_by_name_rather_than_crashing(damaged):
    """Values outside the canonical vocabulary use its established TypeError boundary."""
    with pytest.raises(TypeError) as caught:
        canonical_bytes(damaged)
    assert "unencodable character" in str(caught.value)


def test_a_legitimate_non_ascii_key_is_still_named_as_itself_in_a_path():
    """Making surrogate paths printable must not escape ordinary Unicode keys."""
    with pytest.raises(TypeError) as caught:
        canonical_bytes({"Étienne": {"scale": 1.5}})
    assert "$.Étienne.scale" in str(caught.value)


def test_a_surrogate_refusal_is_itself_printable():
    """A stderr refusal must not embed an unencodable offender raw."""
    with pytest.raises(TypeError) as caught:
        canonical_bytes({SURROGATE: {"scale": SURROGATE}})
    str(caught.value).encode("utf-8")


def test_a_surrogate_refusal_pairs_the_offender_with_its_canonical_path():
    """The encoder sees sorted keys, so the diagnostic locator must too.

    Insertion order puts ``z`` first here while canonical JSON puts ``a`` first.
    Walking the former used to report the encoder's ``\\ud800`` offender at
    ``$.z``, where the different ``\\udfff`` character actually lives.
    """
    with pytest.raises(TypeError) as caught:
        canonical_bytes({"z": "\udfff", "a": SURROGATE})
    assert "unencodable character '\\ud800' at $.a" in str(caught.value)


def test_a_record_carrying_a_surrogate_fails_its_hash_rather_than_crashing():
    record = {"a": SURROGATE, "self_hash": "0" * 64}
    assert verify_self_hash(record) is False


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

    # Assert the premise against the same walk `verify_self_hash` guards. On an
    # interpreter that can absorb this depth, the deliberately wrong hash below
    # would make the test pass without ever exercising the RecursionError path.
    try:
        canonical_bytes(nested)
    except TypeError as error:
        assert "nests too deeply" in str(error)
    else:
        pytest.skip(
            "this interpreter's canonical walk absorbs 2000 levels, so the "
            "guarded path is unreachable here and this test proves nothing"
        )

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


def test_unhashable_current_content_is_told_apart_from_a_digest_mismatch():
    """Only canonical edited contents provide a digest that can mismatch."""
    edited = sound_approval()
    edited["reason"] = "actually it was fine"
    assert self_hash_refusal(edited) is None

    assert "float" in (self_hash_refusal({"scale": 1.5, "self_hash": "0" * 64}) or "")
    assert "unencodable" in (self_hash_refusal({"a": SURROGATE, "self_hash": "0" * 64}) or "")


def test_an_approval_with_unhashable_current_content_names_that_cause():
    """Current bytes cannot prove when an unhashable approval became malformed."""
    record = sound_approval()
    record["reason"] = SURROGATE
    with pytest.raises(ApprovalRefusal) as caught:
        validate_approval_record(record)
    message = str(caught.value)
    assert "unencodable character" in message
    assert "edited after it was sealed" not in message
    message.encode("utf-8")


def test_an_approval_with_a_huge_integer_is_refused_by_a_printable_name():
    """An unbounded integer reaches a named, printable refusal, never a crash.

    It used to reach the canonical writer's own "exceeds 640 decimal digits"
    refusal through the self-hash. work/approval-record-binding then closed this
    record's schema, so an unexpected field is refused before its value is ever
    examined -- the stricter of the two, and the reason the name changed. The
    canonical refusal itself is unchanged and still proven, at
    test_an_integer_above_the_portable_decimal_limit_is_refused_by_path and
    test_contracts_envelope.py.
    """
    record = sound_approval()
    record["unexpected_count"] = 10**640
    with pytest.raises(ApprovalRefusal) as caught:
        validate_approval_record(record)
    message = str(caught.value)
    assert "unexpected_count" in message and "schema is closed" in message
    assert len(message) < 1000, "a refusal may not print the whole offending integer"
    message.encode("utf-8")


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


def test_the_approval_builder_does_not_split_one_string_into_character_subjects():
    with pytest.raises(ApprovalRefusal, match="names no subject"):
        build_approval_record(
            subject_ids="act_0123456789abcdef",
            action="exclusion",
            reason="because",
            target_version_hash="d" * 64,
            timestamp="2026-07-30T23:40:00Z",
        )


def test_duplicate_approval_subjects_are_refused_on_write_and_read():
    kwargs = {
        "subject_ids": ["act_0123456789abcdef", "act_0123456789abcdef"],
        "action": "exclusion",
        "reason": "because",
        "target_version_hash": "d" * 64,
        "timestamp": "2026-07-30T23:40:00Z",
    }
    with pytest.raises(ApprovalRefusal, match="only once"):
        build_approval_record(**kwargs)

    record = sound_approval()
    record["subject_ids"] *= 2
    record["self_hash"] = self_hash(record)
    with pytest.raises(ApprovalRefusal, match="more than once"):
        validate_approval_record(record)


def test_a_huge_non_string_action_reaches_a_printable_approval_refusal():
    record = sound_approval()
    record["action"] = 10**5000
    with pytest.raises(ApprovalRefusal, match="action is not an exact string") as caught:
        validate_approval_record(record)
    str(caught.value).encode("utf-8")


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
