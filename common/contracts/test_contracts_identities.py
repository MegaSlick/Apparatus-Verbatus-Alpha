"""Identities are derived, so the properties are provable rather than promised.

The ruling this file enforces is ARCHITECTURE's first invariant, verbatim: "Act
identity survives recropping." The second and third follow from the same
mechanism — every region traces back to the Exemplar, and the exact image shown to
a model is reproducible from the Exemplar plus the recorded transforms.

Meta-invariant #86 — every derived or authored value is NAMED in the test. The
bindings below are written out in full at each call rather than hidden behind a
fixture helper, because the whole point of the test is which facts an identity
binds and which it does not.
"""

import pytest

from common.contracts import identities
from common.contracts.canonical import canonical_bytes
from common.contracts.errors import IdentityRefusal
from common.fixture_identity import page_identity

SOURCE = "a" * 64
BOUNDS_ORIGINAL = {"x": 10, "y": 20, "w": 300, "h": 90}
BOUNDS_RECROPPED = {"x": 4, "y": 14, "w": 320, "h": 110}
ORIGIN = {"kind": "source", "sha256": SOURCE}
WHOLE = {"operation": "whole"}


def test_the_same_bindings_always_derive_the_same_identity():
    first = identities.page_id(ORIGIN, WHOLE)
    second = identities.page_id(ORIGIN, WHOLE)
    assert first == second
    assert identities.is_well_formed(first)


def test_a_submission_ordinal_does_not_enter_page_identity():
    first = {"page": [{"ordinal": 1, "sha256": SOURCE}]}
    moved = {"page": [{"ordinal": 99, "sha256": SOURCE}]}
    assert page_identity(first, 1) == page_identity(moved, 99)


def test_inserting_an_earlier_source_leaves_existing_page_identities_unchanged():
    originals = ["b" * 64, "c" * 64]
    before = {
        digest: identities.page_id({"kind": "source", "sha256": digest}, WHOLE)
        for digest in originals
    }
    after = {
        digest: identities.page_id({"kind": "source", "sha256": digest}, WHOLE)
        for digest in ["a" * 64, *originals]
    }
    assert {digest: after[digest] for digest in originals} == before


def test_one_hand_computable_page_identity_golden():
    """The sole literal pin, reproducible without running any of this module.

    The canonical bytes are written out in full below so a reviewer can check
    the pin with `sha256sum` and nothing else: sorted keys, no whitespace, no
    ASCII escaping, `pg_` plus the first sixteen hex characters. If this ever
    disagrees with the string beside it, the encoder moved -- which is the one
    thing every other identity in the system would move with, silently.
    """
    import hashlib

    canonical = (
        '{"bindings":{"origin":{"kind":"source","sha256":"' + "a" * 64 + '"},'
        '"transform":{"operation":"whole"}},"kind":"page"}'
    )
    assert canonical.encode("utf-8") == canonical_bytes(
        {"kind": "page", "bindings": identities.page_bindings(ORIGIN, WHOLE)}
    )
    by_hand = "pg_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    assert by_hand == "pg_ea502c18af2fe79a"
    assert identities.page_id(ORIGIN, WHOLE) == by_hand


def test_a_different_source_is_a_different_page():
    assert identities.page_id(ORIGIN, WHOLE) != identities.page_id(
        {"kind": "source", "sha256": "b" * 64}, WHOLE
    )


def test_a_split_page_binds_its_parent_source_digest_and_parent_space_bounds():
    split = {"operation": "split", "bounds": BOUNDS_ORIGINAL}
    page = identities.page_id(ORIGIN, split)
    identities.verify(page, "page", identities.page_bindings(ORIGIN, split))
    with pytest.raises(IdentityRefusal):
        identities.verify(
            page,
            "page",
            identities.page_bindings({"kind": "source", "sha256": "b" * 64}, split),
        )


def test_act_identity_survives_recropping():
    """ARCHITECTURE invariant 1. The act id binds the ORIGINAL proposal, so a
    recrop cannot reach its bindings at all — stability is the only thing the
    derivation is able to do, rather than something code must remember."""
    page = identities.page_id(ORIGIN, WHOLE)
    act = identities.act_id(page, "proposal", BOUNDS_ORIGINAL)

    first_region = identities.region_id(act, {"crop": BOUNDS_ORIGINAL, "scale": 1})
    recropped_region = identities.region_id(act, {"crop": BOUNDS_RECROPPED, "scale": 1})

    assert identities.act_id(page, "proposal", BOUNDS_ORIGINAL) == act
    assert first_region != recropped_region


def test_region_identity_changes_on_recrop_and_on_any_transform_change():
    act = identities.act_id(identities.page_id(ORIGIN, WHOLE), "proposal", BOUNDS_ORIGINAL)
    base = identities.region_id(act, {"crop": BOUNDS_ORIGINAL, "scale": 1})
    rescaled = identities.region_id(act, {"crop": BOUNDS_ORIGINAL, "scale": 2})
    assert base != rescaled


def test_two_acts_on_one_page_are_distinct():
    page = identities.page_id(ORIGIN, WHOLE)
    assert identities.act_id(page, "proposal", BOUNDS_ORIGINAL) != identities.act_id(
        page, "proposal", {**BOUNDS_ORIGINAL, "x": BOUNDS_ORIGINAL["x"] + 1}
    )


def test_attempts_are_distinct_per_ordinal_and_per_operation():
    act = identities.act_id(identities.page_id(ORIGIN, WHOLE), "proposal", BOUNDS_ORIGINAL)
    first = identities.attempt_id(act, "read", 1)
    second = identities.attempt_id(act, "read", 2)
    other_operation = identities.attempt_id(act, "recrop", 1)
    assert len({first, second, other_operation}) == 3


def test_artifacts_of_one_subject_differ_by_attempt():
    """Spec 07's retention ruling, 2026-07-30: attempts are append-only and nothing
    overwrites attempt 1 to record attempt 2. Two attempts colliding onto one
    artifact id is exactly how the old stage lost testimony."""
    act = identities.act_id(identities.page_id(ORIGIN, WHOLE), "proposal", BOUNDS_ORIGINAL)
    first = identities.artifact_id("attestatores", "testimonium", act, "att_1")
    second = identities.artifact_id("attestatores", "testimonium", act, "att_2")
    once = identities.artifact_id("attestatores", "testimonium", act, None)
    assert len({first, second, once}) == 3


def test_kinds_with_identical_bindings_do_not_collide():
    """The kind is hashed alongside the bindings, so two kinds that happened to
    bind the same facts cannot produce the same string."""
    same_bindings = {
        "page_id": "pg_0000000000000000",
        "class": "proposal",
        "bounds": {"x": 0, "y": 0, "w": 1, "h": 1},
    }
    assert identities.derive("act", same_bindings) != identities.derive("region", same_bindings)


# --- Verification: a forged or drifted identity is detectable ------------------


def test_verify_accepts_an_identity_that_recomputes():
    page = identities.page_id(ORIGIN, WHOLE)
    identities.verify(page, "page", identities.page_bindings(ORIGIN, WHOLE))


def test_verify_refuses_an_identity_whose_bindings_were_altered():
    """This is what makes identity checkable rather than trusted: an artifact that
    arrived with a good-looking id but edited bindings is refused at the boundary."""
    page = identities.page_id(ORIGIN, WHOLE)
    with pytest.raises(IdentityRefusal) as caught:
        identities.verify(
            page,
            "page",
            identities.page_bindings(ORIGIN, {"operation": "split", "bounds": BOUNDS_ORIGINAL}),
        )
    assert "does not verify" in str(caught.value)


def test_verify_refuses_a_malformed_identity():
    for bad in ("", "pg_nothex0000000", "pg_" + "0" * 15, "unknown_0123456789abcdef", None, 7):
        with pytest.raises(IdentityRefusal):
            identities.verify(bad, "page", identities.page_bindings(ORIGIN, WHOLE))


def test_an_unknown_identity_kind_is_refused():
    with pytest.raises(IdentityRefusal):
        identities.derive("witness_winner", {"anything": 1})


def test_is_well_formed_checks_shape_only():
    assert identities.is_well_formed("pg_0123456789abcdef")
    assert not identities.is_well_formed("pg_0123456789ABCDEF")
    assert not identities.is_well_formed("pg_0123456789abcde")
    assert not identities.is_well_formed(None)


def test_act_bindings_refuse_a_shape_the_minting_path_could_never_produce():
    """`verify()` is handed bindings rebuilt from a payload a stage read back,
    so the validation has to live in `act_bindings` rather than in `act_id`.
    Otherwise the verify path hashes an open bounds record the minter would
    have refused, and reports a digest mismatch that says nothing about why."""
    page = identities.page_id(ORIGIN, WHOLE)
    with pytest.raises(IdentityRefusal, match="closed record"):
        identities.act_bindings(page, "proposal", {**BOUNDS_ORIGINAL, "note": "extra"})
    with pytest.raises(IdentityRefusal, match="act class must be"):
        identities.act_bindings(page, "proposals", BOUNDS_ORIGINAL)
    with pytest.raises(IdentityRefusal, match="closed record"):
        identities.verify(
            identities.act_id(page, "residual", BOUNDS_ORIGINAL),
            "act",
            identities.act_bindings(page, "residual", {**BOUNDS_ORIGINAL, "note": "extra"}),
        )


def test_page_and_act_bindings_refuse_malformed_identity_facts():
    with pytest.raises(IdentityRefusal, match="lowercase SHA-256"):
        identities.page_id({"kind": "source", "sha256": "not-a-digest"}, WHOLE)
    with pytest.raises(IdentityRefusal, match="non-negative integer x/y"):
        identities.page_id(
            ORIGIN,
            {"operation": "split", "bounds": {"x": 0, "y": 0, "w": 0, "h": 10}},
        )
    page = identities.page_id(ORIGIN, WHOLE)
    with pytest.raises(IdentityRefusal, match="well-formed pg_ identity"):
        identities.act_id("page-0000000000000001", "proposal", BOUNDS_ORIGINAL)
    with pytest.raises(IdentityRefusal, match="positive integer w/h"):
        identities.act_id(page, "proposal", {**BOUNDS_ORIGINAL, "h": -1})


def test_physical_act_refuses_a_prefix_only_physical_page_token():
    with pytest.raises(IdentityRefusal, match="well-formed ppg_ identity"):
        identities.physical_act_id("ppg_not-a-digest", "entry-1")


def test_two_unicode_spellings_of_one_declaration_are_one_physical_identity():
    """The physical identities are the only ones bound to text a person types.
    NFC and NFD are different bytes and `canonical_bytes` hashes bytes, so an
    unnormalised designation would declare one physical page twice."""
    composed, decomposed = "12ré", "12re\u0301"
    assert composed != decomposed
    assert identities.physical_page_id("c", "v", composed) == identities.physical_page_id(
        "c", "v", decomposed
    )
    page = identities.physical_page_id("c", "v", composed)
    assert identities.physical_act_id(page, "entrée-4") == identities.physical_act_id(
        page, "entre\u0301e-4"
    )


def test_physical_identities_bind_only_their_minting_facts():
    physical_page = identities.physical_page_id("corpus", "volume", "folio-12r")
    assert physical_page.startswith("ppg_")
    physical_act = identities.physical_act_id(physical_page, "act-4")
    assert physical_act.startswith("pac_")
    identities.verify(
        physical_act,
        "physical-act",
        identities.physical_act_bindings(physical_page, "act-4"),
    )


# --- run_id is the caller's, and is kept boring on purpose --------------------


def test_a_plain_run_id_is_accepted():
    for good in ("r1", "alpha-2026-07-30", "run.01", "a" * 64):
        assert identities.validate_run_id(good) == good


def test_a_run_id_that_would_misbehave_as_a_directory_is_refused():
    for bad in (
        "",
        ".hidden",
        "has space",
        "UPPER",
        "a/b",
        "../escape",
        "-leading",
        "a" * 65,
        None,
    ):
        with pytest.raises(IdentityRefusal):
            identities.validate_run_id(bad)
