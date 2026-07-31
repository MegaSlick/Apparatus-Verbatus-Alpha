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
from common.contracts.errors import IdentityRefusal

SOURCE = "a" * 64
BOUNDS_ORIGINAL = {"x": 10, "y": 20, "w": 300, "h": 90}
BOUNDS_RECROPPED = {"x": 4, "y": 14, "w": 320, "h": 110}


def test_the_same_bindings_always_derive_the_same_identity():
    first = identities.page_id(SOURCE, 1)
    second = identities.page_id(SOURCE, 1)
    assert first == second
    assert identities.is_well_formed(first)


def test_a_different_ordinal_is_a_different_page():
    assert identities.page_id(SOURCE, 1) != identities.page_id(SOURCE, 2)


def test_a_different_source_is_a_different_page():
    assert identities.page_id(SOURCE, 1) != identities.page_id("b" * 64, 1)


def test_act_identity_survives_recropping():
    """ARCHITECTURE invariant 1. The act id binds the ORIGINAL proposal, so a
    recrop cannot reach its bindings at all — stability is the only thing the
    derivation is able to do, rather than something code must remember."""
    page = identities.page_id(SOURCE, 1)
    act = identities.act_id(page, 0, BOUNDS_ORIGINAL)

    first_region = identities.region_id(act, {"crop": BOUNDS_ORIGINAL, "scale": 1})
    recropped_region = identities.region_id(act, {"crop": BOUNDS_RECROPPED, "scale": 1})

    assert identities.act_id(page, 0, BOUNDS_ORIGINAL) == act
    assert first_region != recropped_region


def test_region_identity_changes_on_recrop_and_on_any_transform_change():
    act = identities.act_id(identities.page_id(SOURCE, 1), 0, BOUNDS_ORIGINAL)
    base = identities.region_id(act, {"crop": BOUNDS_ORIGINAL, "scale": 1})
    rescaled = identities.region_id(act, {"crop": BOUNDS_ORIGINAL, "scale": 2})
    assert base != rescaled


def test_two_acts_on_one_page_are_distinct():
    page = identities.page_id(SOURCE, 1)
    assert identities.act_id(page, 0, BOUNDS_ORIGINAL) != identities.act_id(
        page, 1, BOUNDS_ORIGINAL
    )


def test_attempts_are_distinct_per_ordinal_and_per_operation():
    act = identities.act_id(identities.page_id(SOURCE, 1), 0, BOUNDS_ORIGINAL)
    first = identities.attempt_id(act, "read", 1)
    second = identities.attempt_id(act, "read", 2)
    other_operation = identities.attempt_id(act, "recrop", 1)
    assert len({first, second, other_operation}) == 3


def test_artifacts_of_one_subject_differ_by_attempt():
    """Spec 07's retention ruling, 2026-07-30: attempts are append-only and nothing
    overwrites attempt 1 to record attempt 2. Two attempts colliding onto one
    artifact id is exactly how the old stage lost testimony."""
    act = identities.act_id(identities.page_id(SOURCE, 1), 0, BOUNDS_ORIGINAL)
    first = identities.artifact_id("attestatores", "testimonium", act, "att_1")
    second = identities.artifact_id("attestatores", "testimonium", act, "att_2")
    once = identities.artifact_id("attestatores", "testimonium", act, None)
    assert len({first, second, once}) == 3


def test_kinds_with_identical_bindings_do_not_collide():
    """The kind is hashed alongside the bindings, so two kinds that happened to
    bind the same facts cannot produce the same string."""
    same_bindings = {"page_id": "pg_0000000000000000", "proposal_ordinal": 0}
    assert identities.derive("act", same_bindings) != identities.derive("region", same_bindings)


# --- Verification: a forged or drifted identity is detectable ------------------


def test_verify_accepts_an_identity_that_recomputes():
    page = identities.page_id(SOURCE, 3)
    identities.verify(page, "page", identities.page_bindings(SOURCE, 3))


def test_verify_refuses_an_identity_whose_bindings_were_altered():
    """This is what makes identity checkable rather than trusted: an artifact that
    arrived with a good-looking id but edited bindings is refused at the boundary."""
    page = identities.page_id(SOURCE, 3)
    with pytest.raises(IdentityRefusal) as caught:
        identities.verify(page, "page", identities.page_bindings(SOURCE, 4))
    assert "does not verify" in str(caught.value)


def test_verify_refuses_a_malformed_identity():
    for bad in ("", "pg_nothex0000000", "pg_" + "0" * 15, "unknown_0123456789abcdef", None, 7):
        with pytest.raises(IdentityRefusal):
            identities.verify(bad, "page", identities.page_bindings(SOURCE, 1))


def test_an_unknown_identity_kind_is_refused():
    with pytest.raises(IdentityRefusal):
        identities.derive("witness_winner", {"anything": 1})


def test_is_well_formed_checks_shape_only():
    assert identities.is_well_formed("pg_0123456789abcdef")
    assert not identities.is_well_formed("pg_0123456789ABCDEF")
    assert not identities.is_well_formed("pg_0123456789abcde")
    assert not identities.is_well_formed(None)


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
