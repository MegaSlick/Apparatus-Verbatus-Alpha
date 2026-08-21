"""The envelope, and what a consumer must refuse.

These are the unit-level halves of the seven-handoff boundary test: the four
corruption kinds spec 01 names — schema, identity, input digest, duplicate
accounting — checked here against the validator directly, and driven again at every
one of the seven real handoffs by the boundary test in the orchestrator's suite. A
validator that refuses in isolation and is never called at a boundary would be
meta-invariant #89's vacuous pass, so both exist on purpose.
"""

import contextlib
import io
import json

import pytest

from common.contracts.canonical import SCHEMA_LABEL, digest_bytes, self_hash
from common.contracts.envelope import (
    build_envelope,
    validate_envelope,
    verify_input_bytes,
)
from common.contracts.errors import ApprovalRefusal, FatalAccounting, SchemaRefusal
from common.contracts.identities import artifact_id

PAGE_BYTES = b"synthetic page bytes"
PAGE_REF = {"relative_path": "1_exemplar/blobs/sha256/page-1", "sha256": digest_bytes(PAGE_BYTES)}


def sound_envelope(**overrides):
    envelope = build_envelope(
        run_id="r1",
        artifact_id=artifact_id("designator", "proposal", "pg_0123456789abcdef"),
        subject_id="pg_0123456789abcdef",
        stage="designator",
        kind="proposal",
        outcome="proposed",
        config_digest="c" * 64,
        adapter_revision="fake-designator-v0",
        inputs=[PAGE_REF],
        payload={"proposals": 2},
    )
    envelope.update(overrides)
    return envelope


def reseal(envelope):
    """Model a syntactically resealed edit so semantic checks do real work."""
    envelope["self_hash"] = self_hash(envelope)
    return envelope


def test_a_sound_envelope_validates():
    assert validate_envelope(sound_envelope())["schema"] == SCHEMA_LABEL


def test_building_validates_on_the_way_out_too():
    """A producer that writes an unreadable artifact has already lost the work, and
    finding out at the consumer puts the evidence a stage away from the cause."""
    with pytest.raises(FatalAccounting):
        build_envelope(
            run_id="r1",
            artifact_id=artifact_id("designator", "proposal", "pg_0123456789abcdef"),
            subject_id="pg_0123456789abcdef",
            stage="designator",
            kind="proposal",
            outcome="mostly-fine",
            config_digest="c" * 64,
            adapter_revision="fake-designator-v0",
            inputs=[PAGE_REF],
            payload={},
        )


# --- Corruption kind 1: the schema --------------------------------------------


def test_an_unknown_schema_label_is_refused():
    with pytest.raises(SchemaRefusal) as caught:
        validate_envelope(sound_envelope(schema="skeleton.v0"))
    assert "declares schema" in str(caught.value)


def test_every_required_field_is_actually_required():
    checked = 0
    for field in (
        "schema",
        "run_id",
        "artifact_id",
        "attempt_id",
        "subject_id",
        "stage",
        "kind",
        "outcome",
        "config_digest",
        "producer",
        "inputs",
        "payload",
        "self_hash",
    ):
        envelope = sound_envelope()
        del envelope[field]
        with pytest.raises(SchemaRefusal):
            validate_envelope(envelope)
        checked += 1
    assert checked == 13


def test_a_non_object_artifact_is_refused():
    for bad in (None, [], "artifact", 3):
        with pytest.raises(SchemaRefusal):
            validate_envelope(bad)


def test_an_unknown_stage_is_refused():
    with pytest.raises(SchemaRefusal):
        validate_envelope(sound_envelope(stage="consolidator"))


def test_a_producer_disagreeing_with_the_stage_is_refused():
    envelope = sound_envelope()
    envelope["producer"] = {"stage": "perlector", "adapter_revision": "x"}
    with pytest.raises(SchemaRefusal):
        validate_envelope(envelope)


def test_a_missing_adapter_revision_is_refused():
    """GOVERNANCE 6 — every stored reading carries the resolved identity and
    revision of the model that produced it, at the moment it was produced."""
    envelope = sound_envelope()
    envelope["producer"] = {"stage": "designator", "adapter_revision": ""}
    with pytest.raises(SchemaRefusal):
        validate_envelope(envelope)


@pytest.mark.parametrize(
    "producer",
    (
        {"stage": "designator", "adapter_revision": 17},
        {"stage": "designator", "adapter_revision": "   "},
        {"stage": "designator", "adapter_revision": "v1", "unvalidated": "x"},
    ),
)
def test_producer_provenance_is_a_closed_string_revision_schema(producer):
    envelope = sound_envelope()
    envelope["producer"] = producer

    with pytest.raises(SchemaRefusal):
        validate_envelope(reseal(envelope))


# --- Corruption kind 2: the identity ------------------------------------------


def test_a_malformed_artifact_id_is_refused():
    for bad in ("", "art_notlongenough", "proposal-1", None):
        with pytest.raises(SchemaRefusal):
            validate_envelope(sound_envelope(artifact_id=bad))


def test_a_well_formed_but_wrong_artifact_identity_is_refused_after_resealing():
    """Shape-only identity checks would accept this forged handoff binding."""
    forged = reseal(sound_envelope(artifact_id="art_" + "0" * 16))
    with pytest.raises(SchemaRefusal, match="does not verify against"):
        validate_envelope(forged)


def test_an_unresealed_payload_change_is_refused_when_the_identity_is_unchanged():
    """The self-hash protects ordinary payload bytes, not only derived IDs."""
    changed = sound_envelope()
    changed["payload"] = {"proposals": 99}
    with pytest.raises(SchemaRefusal, match="self-hash"):
        validate_envelope(changed)
    assert validate_envelope(reseal(changed))["payload"] == {"proposals": 99}


@pytest.mark.parametrize("payload", ({"scale": 1.5}, {"box": [1, 2.0]}, {"nested": {"deep": 0.1}}))
def test_a_parsed_artifact_with_a_float_is_refused_by_name_at_the_envelope_boundary(payload):
    """Unsupported JSON numbers must not escape the consumer as a serializer traceback."""
    parsed = json.loads(json.dumps(sound_envelope()))
    parsed["payload"] = payload

    with pytest.raises(SchemaRefusal, match="self-hash"):
        validate_envelope(parsed)


def test_the_float_refusal_names_the_offending_field_and_value():
    """The point of recomputing on the refusal path is the diagnostic. A test that
    only asserts `SchemaRefusal` passes just as happily on the generic "changed
    after publication" sentence, which is the wrong story for a record that was
    never hashable — so the assertion has to be on what it says."""
    parsed = json.loads(json.dumps(sound_envelope()))
    parsed["payload"] = {"nested": {"scale": 1.5}}

    with pytest.raises(SchemaRefusal) as caught:
        validate_envelope(parsed)
    message = str(caught.value)
    assert "float at $.payload.nested.scale" in message
    assert "changed after publication" not in message


@pytest.mark.parametrize(
    "payload",
    (
        {"name": "\ud800"},
        {"box": ["ok", "\udfff"]},
        {"nested": {"deep": "\ud800"}},
        {"\ud800": "in a key"},
    ),
)
def test_a_parsed_artifact_with_a_lone_surrogate_is_refused_by_name(payload):
    """The float's sibling, and it arrives the same way: `json.loads` builds a lone
    surrogate from a `\\udXXX` escape without complaint, so a damaged artifact on
    disk parses cleanly and then has no UTF-8 form to hash. It used to leave this
    boundary as an uncaught `UnicodeEncodeError` — `UnicodeEncodeError` is a
    `ValueError`, so neither `verify_self_hash`'s catch nor the recovery below
    named it."""
    parsed = json.loads(json.dumps(sound_envelope()))
    parsed["payload"] = payload

    with pytest.raises(SchemaRefusal) as caught:
        validate_envelope(parsed)
    message = str(caught.value)
    assert "unencodable character" in message
    assert "changed after publication" not in message
    # The consumer prints this. A message carrying the offender raw would raise
    # the same error again out of the report of the first one.
    message.encode("utf-8")


def test_a_record_too_deep_to_hash_is_not_accused_of_having_been_edited():
    """`verify_self_hash` returns False for a record whose recursive walk exhausts
    the stack. That is the right answer to the check and the wrong sentence for an
    operator: nobody edited it, and no machine here could ever have verified it."""
    nested: dict = {"leaf": 1}
    for _ in range(2000):
        nested = {"nested": nested}
    parsed = sound_envelope()
    parsed["payload"] = {"deep": nested}

    try:
        self_hash(parsed)
    except TypeError as error:
        assert "nests too deeply" in str(error)
    else:
        pytest.skip("this interpreter absorbs 2000 levels, so the guarded band is unreachable")

    with pytest.raises(SchemaRefusal) as caught:
        validate_envelope(parsed)
    assert "nests too deeply" in str(caught.value)


def test_a_huge_integer_is_named_at_the_envelope_boundary():
    parsed = sound_envelope()
    parsed["payload"] = {"count": 10**640}

    with pytest.raises(SchemaRefusal) as caught:
        validate_envelope(parsed)
    assert "integer at $.payload.count exceeds 640 decimal digits" in str(caught.value)


def test_the_refusal_an_operator_sees_carries_the_diagnostic_through_run_stage():
    """`common/stage.py::run_stage` is the last thing between a refusal and stderr.
    It catches `ContractError` and prints `type: message`, so the recovered
    diagnostic survives — asserted here rather than assumed, because a boundary
    that names the offending field into a handler that discards it has fixed
    nothing an operator can see."""
    from common.stage import EXIT_FATAL, run_stage

    parsed = json.loads(json.dumps(sound_envelope()))
    parsed["payload"] = {"scale": 1.5}

    def main():
        validate_envelope(parsed)

    stream = io.StringIO()
    with contextlib.redirect_stderr(stream):
        assert run_stage(main) == EXIT_FATAL
    printed = stream.getvalue()
    assert printed.startswith(
        "SchemaRefusal: artifact fails its self-hash: float at $.payload.scale"
    )


# --- Corruption kind 3: the input digest --------------------------------------


def test_an_input_reference_without_a_digest_is_refused():
    with pytest.raises(SchemaRefusal):
        validate_envelope(sound_envelope(inputs=[{"relative_path": "a/b"}]))


def test_a_short_or_non_hex_digest_is_refused():
    for bad in ("abc", "z" * 64, 1234):
        with pytest.raises(SchemaRefusal):
            validate_envelope(sound_envelope(inputs=[{"relative_path": "a/b", "sha256": bad}]))


def test_a_reference_escaping_the_run_tree_is_refused():
    """References are relative to the run root so a tree stays movable and
    verifiable. An absolute path or a `..` makes both untrue."""
    for bad in ("/etc/passwd", "../../elsewhere/page", "1_exemplar/../../out"):
        with pytest.raises(SchemaRefusal):
            validate_envelope(sound_envelope(inputs=[{"relative_path": bad, "sha256": "a" * 64}]))


def test_bytes_that_do_not_match_the_sealed_reference_are_refused():
    verify_input_bytes(PAGE_REF, PAGE_BYTES)
    with pytest.raises(SchemaRefusal) as caught:
        verify_input_bytes(PAGE_REF, b"different bytes")
    assert "changed under a sealed reference" in str(caught.value)


# --- Corruption kind 4: duplicate accounting ----------------------------------


def test_the_same_input_listed_twice_is_refused():
    """A duplicate reference is how one page gets counted twice and a conservation
    check passes over a page nobody read."""
    with pytest.raises(SchemaRefusal) as caught:
        validate_envelope(sound_envelope(inputs=[PAGE_REF, dict(PAGE_REF)]))
    assert "listed twice" in str(caught.value)


def test_the_same_path_with_two_different_digests_is_refused():
    """One file cannot hold two sets of bytes. Keying the duplicate check on
    (path, digest) let the contradiction through, and it splits consumers:
    whichever reference a reader verifies first decides what it believes."""
    with pytest.raises(SchemaRefusal) as caught:
        validate_envelope(
            sound_envelope(
                inputs=[
                    PAGE_REF,
                    {"relative_path": PAGE_REF["relative_path"], "sha256": "a" * 64},
                ]
            )
        )
    assert "two digests" in str(caught.value)


def test_inputs_are_stored_in_a_stable_order():
    """Two runs that gathered the same inputs in different orders must produce
    identical bytes, or byte-for-byte reuse on resume is not testable."""
    other = {"relative_path": "1_exemplar/blobs/sha256/page-0", "sha256": "b" * 64}
    forwards = build_envelope(
        run_id="r1",
        artifact_id=artifact_id("designator", "proposal", "pg_0123456789abcdef"),
        subject_id="pg_0123456789abcdef",
        stage="designator",
        kind="proposal",
        outcome="proposed",
        config_digest="c" * 64,
        adapter_revision="fake-designator-v0",
        inputs=[PAGE_REF, other],
        payload={},
    )
    backwards = build_envelope(
        run_id="r1",
        artifact_id=artifact_id("designator", "proposal", "pg_0123456789abcdef"),
        subject_id="pg_0123456789abcdef",
        stage="designator",
        kind="proposal",
        outcome="proposed",
        config_digest="c" * 64,
        adapter_revision="fake-designator-v0",
        inputs=[other, PAGE_REF],
        payload={},
    )
    assert forwards == backwards


# --- The approval binding travels with the envelope ---------------------------


def test_an_excluded_artifact_without_an_approval_reference_is_refused():
    with pytest.raises(ApprovalRefusal):
        validate_envelope(sound_envelope(outcome="excluded"))


def test_an_excluded_artifact_with_an_approval_reference_validates():
    envelope = sound_envelope(outcome="excluded")
    envelope["approval_ref"] = artifact_id("designator", "approval", "pg_0123456789abcdef")
    envelope["self_hash"] = self_hash(envelope)
    assert validate_envelope(envelope)


def test_a_payload_that_is_not_an_object_is_refused():
    with pytest.raises(SchemaRefusal):
        validate_envelope(sound_envelope(payload=["proposals"]))
