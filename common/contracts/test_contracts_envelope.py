"""The envelope, and what a consumer must refuse.

These are the unit-level halves of the seven-handoff boundary test: the four
corruption kinds spec 01 names — schema, identity, input digest, duplicate
accounting — checked here against the validator directly, and driven again at every
one of the seven real handoffs by the boundary test in the orchestrator's suite. A
validator that refuses in isolation and is never called at a boundary would be
meta-invariant #89's vacuous pass, so both exist on purpose.
"""

import pytest

from common.contracts.canonical import SCHEMA_LABEL, digest_bytes
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
        validate_envelope(sound_envelope(schema="skeleton.v1"))
    assert "declares schema" in str(caught.value)


def test_every_required_field_is_actually_required():
    checked = 0
    for field in (
        "schema",
        "run_id",
        "artifact_id",
        "subject_id",
        "stage",
        "kind",
        "outcome",
        "config_digest",
        "producer",
        "inputs",
        "payload",
    ):
        envelope = sound_envelope()
        del envelope[field]
        with pytest.raises(SchemaRefusal):
            validate_envelope(envelope)
        checked += 1
    assert checked == 11


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


# --- Corruption kind 2: the identity ------------------------------------------


def test_a_malformed_artifact_id_is_refused():
    for bad in ("", "art_notlongenough", "proposal-1", None):
        with pytest.raises(SchemaRefusal):
            validate_envelope(sound_envelope(artifact_id=bad))


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
    assert validate_envelope(envelope)


def test_a_payload_that_is_not_an_object_is_refused():
    with pytest.raises(SchemaRefusal):
        validate_envelope(sound_envelope(payload=["proposals"]))
