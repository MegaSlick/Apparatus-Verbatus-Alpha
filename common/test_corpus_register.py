"""The corpus register declares correspondence without choosing a capture."""

import json

import pytest

import common.corpus_register as corpus_register
from common.contracts.canonical import canonical_bytes, digest_of
from common.contracts.errors import ContractError, IncompatibleReuse, SchemaRefusal
from common.contracts.identities import act_id, page_id, physical_act_id, physical_page_id
from common.corpus_register import (
    EMPTY_REGISTER_DIGEST,
    SCHEMA,
    append_records,
    empty_register,
    members_of,
    read_snapshot,
    register_digest,
    resolve_proposal,
    validate_register_bytes,
    verify_snapshot_is_current,
)
from common.runtree.store import RunTree

PAGE = physical_page_id("synthetic", "volume-1", "12r")
ACT = physical_act_id(PAGE, "entry-4")


def _declaration(page=PAGE, *, corpus="synthetic", volume="volume-1", designation="12r"):
    return {
        "kind": "physical-page",
        "corpus_id": corpus,
        "volume_id": volume,
        "designation": designation,
        "physical_page_id": page,
    }


def _membership(members, *, page=PAGE, predecessor=None, run="triage-1"):
    return {
        "kind": "membership",
        "physical_page_id": page,
        "members": sorted(set(members)),
        "predecessor": predecessor,
        "appending_run": run,
    }


def _correspondence(page_identity, act, *, physical_page=PAGE, physical_act=ACT):
    return {
        "kind": "correspondence",
        "page_id": page_identity,
        "act_id": act,
        "physical_page_id": physical_page,
        "physical_act_id": physical_act,
        "evidence": ["declared-fixture"],
        "appending_run": "triage-1",
    }


def _register(*, members, extra=()):
    return canonical_bytes(
        {
            "schema": SCHEMA,
            "records": [
                _declaration(),
                _membership(members),
                {
                    "kind": "physical-act",
                    "physical_page_id": PAGE,
                    "mint_designation": "entry-4",
                    "physical_act_id": ACT,
                    "evidence": ["declared-fixture"],
                    "appending_run": "triage-1",
                },
                _correspondence("pg_0123456789abcdef", "act_0123456789abcdef"),
                *extra,
            ],
        }
    )


def _record(register, kind):
    return next(row for row in json.loads(register)["records"] if row["kind"] == kind)


def test_members_are_set_serialized_in_digest_order_not_preference_order():
    first = _register(members=["b" * 64, "a" * 64])
    second = _register(members=["a" * 64, "b" * 64])
    assert first == second
    assert register_digest(first) == register_digest(second)


def test_register_refuses_a_members_list_not_already_in_canonical_order():
    """The order-reversal property, at its actual boundary.

    `_membership` above pre-sorts before serializing, so reversing its argument
    proves nothing about the system -- `sorted()` erases the reversal before any
    system code sees it. The real guarantee is that the register itself refuses
    a members list a caller wrote in any other order, rather than silently
    re-sorting one for them: a canonically sorted list is the field a picker
    would otherwise fill via ``members[0]``, so accepting an unsorted list would
    reopen exactly that door one layer down.
    """
    value = {
        "schema": SCHEMA,
        "records": [
            _declaration(),
            {**_membership([]), "members": ["b" * 64, "a" * 64]},
        ],
    }
    with pytest.raises(SchemaRefusal, match="sorted unique source digests"):
        validate_register_bytes(canonical_bytes(value))


def test_reversed_submission_order_reaches_byte_identical_run_artifacts(tmp_path):
    """Two runs seeded by registers whose members were *discovered* in opposite
    order (capture B before capture A, or the reverse) still snapshot to the
    identical blob and `register_digest`, because both pass through the one
    canonical-order gate before either reaches a run tree."""
    forward = _register(members=["a" * 64, "b" * 64])
    reversed_order = _register(members=["b" * 64, "a" * 64])
    assert forward == reversed_order

    shared = {
        "source_manifest": [{"ordinal": 1, "relative_path": "fixture.png", "sha256": "a" * 64}],
        "config_digest": "c" * 64,
        "adapter_recipes": {"designator": "fixture"},
        "witness_chairs": [],
    }
    first = RunTree.create(tmp_path, "forward", register_bytes=forward, **shared)
    second = RunTree.create(tmp_path, "reversed", register_bytes=reversed_order, **shared)
    assert first.read_run()["register_digest"] == second.read_run()["register_digest"]
    first_bytes = first.read_bytes(first.blob_path("door", first.read_run()["register_digest"]))
    second_bytes = second.read_bytes(second.blob_path("door", second.read_run()["register_digest"]))
    assert first_bytes == second_bytes


def test_preference_field_is_refused_at_the_register_boundary():
    value = json.loads(_register(members=["a" * 64]))
    value["records"][0]["preferred"] = "a" * 64
    with pytest.raises(SchemaRefusal, match="preference"):
        validate_register_bytes(canonical_bytes(value))


def test_declared_correspondence_resolves_two_capture_proposals_to_one_physical_act():
    value = json.loads(_register(members=["a" * 64, "b" * 64]))
    first = _record(_register(members=["a" * 64]), "correspondence")
    second = {**first, "page_id": "pg_fedcba9876543210", "act_id": "act_fedcba9876543210"}
    value["records"].append(second)
    snapshot = canonical_bytes(value)
    assert (
        resolve_proposal(snapshot, first["act_id"])["physical_act_id"] == first["physical_act_id"]
    )
    assert (
        resolve_proposal(snapshot, second["act_id"])["physical_act_id"] == first["physical_act_id"]
    )
    assert resolve_proposal(snapshot, "act_0000000000000000") == {
        "outcome": "finding",
        "code": "unresolved-physical-act",
        "act_id": "act_0000000000000000",
    }


def test_a_hard_reshoot_unions_two_captures_shared_act_into_one_physical_act():
    """Unit 18 consult report, failure mode (a), at the register's own boundary.

    Capture A shows acts {1,2,3,4} of physical page P. Capture B is a re-shoot
    of the same opening: it shows only act 4 of P (at different bounds -- a
    different photograph) plus an act of the facing physical page Q. Nothing
    about deriving `page_id`/`act_id` from either capture can collide them
    (they bind distinct source digests), so reconciling A's act 4 and B's act
    4 into one physical act is exactly the declared correspondence this
    register exists to carry -- never a hash coincidence.

    This does not extend `proof/build_fixture.py` into a real Designator-side
    two-capture scenario (a materially larger change to a fixture every stage
    program shares); it proves the register-level mechanism the consult's
    failure mode names: the shared act's two image-local proposals resolve to
    one physical act, the facing page's act stays under its own physical page,
    and neither collides with the other's identity.
    """
    source_a = "a" * 64
    source_b = "b" * 64
    page_a = page_id({"kind": "source", "sha256": source_a}, {"operation": "whole"})
    page_b = page_id({"kind": "source", "sha256": source_b}, {"operation": "whole"})

    # Capture A: page P's own acts 1-4, each a distinct rectangle.
    acts_a = [
        act_id(page_a, "proposal", {"x": 10 * n, "y": 0, "w": 8, "h": 8}) for n in range(1, 5)
    ]
    # Capture B: the same act 4, re-shot at different bounds (a different
    # photograph, so a different act_id even though it is the same ink) --
    # plus one act that belongs to the facing page Q, not P.
    act_4b = act_id(page_b, "proposal", {"x": 41, "y": 1, "w": 9, "h": 9})
    act_q1 = act_id(page_b, "proposal", {"x": 200, "y": 0, "w": 8, "h": 8})
    assert len({*acts_a, act_4b, act_q1}) == 6, "no coincidental collision among any of them"

    physical_p = physical_page_id("corpus", "volume-1", "12r")
    physical_q = physical_page_id("corpus", "volume-1", "12v")
    physical_p4 = physical_act_id(physical_p, "entry-4")

    register = canonical_bytes(
        {
            "schema": SCHEMA,
            "records": [
                _declaration(physical_p, corpus="corpus", designation="12r"),
                _declaration(physical_q, corpus="corpus", designation="12v"),
                _membership([source_a, source_b], page=physical_p),
                _membership([source_b], page=physical_q),
                {
                    "kind": "physical-act",
                    "physical_page_id": physical_p,
                    "mint_designation": "entry-4",
                    "physical_act_id": physical_p4,
                    "evidence": ["declared-fixture"],
                    "appending_run": "triage-1",
                },
                _correspondence(
                    page_a, acts_a[3], physical_page=physical_p, physical_act=physical_p4
                ),
                _correspondence(page_b, act_4b, physical_page=physical_p, physical_act=physical_p4),
            ],
        }
    )

    # The shared act, seen from either capture, resolves to the one physical
    # act P minted once -- this is "P's act set is the union" made concrete:
    # a consumer merging by physical_act_id sees one entry, not two.
    assert resolve_proposal(register, acts_a[3])["physical_act_id"] == physical_p4
    assert resolve_proposal(register, act_4b)["physical_act_id"] == physical_p4

    # Both captures show P; only capture B shows Q. Membership says so without
    # ranking either, and P's list is the union rather than a chosen one.
    assert members_of(register, physical_p) == sorted([source_a, source_b])
    assert members_of(register, physical_q) == [source_b]

    # `resolve_proposal` is a lookup, not an inference: acts 1-3 (single-
    # capture, page P) and Q's act (single-capture, the facing page) have no
    # declared correspondence, so asking it about them names a finding rather
    # than guessing one. Unit 18 declares this shape; deciding *whether* a
    # single-capture act needs resolving at all -- so it is never asked in the
    # first place, and "single-capture" never reads as "unresolved" -- is the
    # caller-side policy Unit 19 supplies. No such caller exists yet.
    for solo_act in (*acts_a[:3], act_q1):
        assert resolve_proposal(register, solo_act)["outcome"] == "finding"


# --- Retraction is the correction mechanism, and it has to reach the reader ------


def test_a_retraction_names_what_it_retracts_and_never_deletes_it():
    """Correcting a wrong correspondence appends a retraction; nothing is removed."""
    correspondence = _record(_register(members=["a" * 64]), "correspondence")
    identity = f"{correspondence['act_id']}->{correspondence['physical_act_id']}"
    register = _register(
        members=["a" * 64, "b" * 64],
        extra=[
            {
                "kind": "retraction",
                "retracts": identity,
                "reason": "the correspondence was declared against the wrong capture",
                "appending_run": "triage-2",
            }
        ],
    )
    validated = validate_register_bytes(register)
    assert correspondence in validated["records"], "the retracted record is still present"


def test_a_retracted_correspondence_stops_resolving_and_says_which_finding_it_is():
    """GOVERNANCE 2 and 4 at once: the declaration is retained as evidence and
    stops answering. A retraction the reader ignored would leave the register's
    only correction mechanism inert -- the wrong physical act would keep
    resolving, with `outcome: resolved`, and nothing anywhere would be a
    finding."""
    correspondence = _record(_register(members=["a" * 64]), "correspondence")
    identity = f"{correspondence['act_id']}->{correspondence['physical_act_id']}"
    before = _register(members=["a" * 64])
    assert resolve_proposal(before, correspondence["act_id"])["outcome"] == "resolved"

    after = _register(
        members=["a" * 64],
        extra=[
            {
                "kind": "retraction",
                "retracts": identity,
                "reason": "declared against the wrong capture",
                "appending_run": "triage-2",
            }
        ],
    )
    assert resolve_proposal(after, correspondence["act_id"]) == {
        "outcome": "finding",
        "code": "retracted-physical-act",
        "act_id": correspondence["act_id"],
    }


def test_a_retraction_naming_no_earlier_record_is_refused():
    """A retraction that corrects nothing reads like a correction that happened."""
    with pytest.raises(SchemaRefusal, match="which no earlier correspondence"):
        validate_register_bytes(
            _register(
                members=["a" * 64],
                extra=[
                    {
                        "kind": "retraction",
                        "retracts": "act_0000000000000000->pac_0000000000000000",
                        "reason": "wrong capture",
                        "appending_run": "triage-2",
                    }
                ],
            )
        )


def test_a_retraction_record_refuses_an_extra_or_missing_field():
    correspondence = _record(_register(members=["a" * 64]), "correspondence")
    base = {
        "kind": "retraction",
        "retracts": f"{correspondence['act_id']}->{correspondence['physical_act_id']}",
        "reason": "wrong capture",
        "appending_run": "triage-2",
    }
    with pytest.raises(SchemaRefusal, match="closed record"):
        validate_register_bytes(
            _register(members=["a" * 64], extra=[{**base, "note": "unexpected extra field"}])
        )
    incomplete = {key: value for key, value in base.items() if key != "reason"}
    with pytest.raises(SchemaRefusal, match="closed record"):
        validate_register_bytes(_register(members=["a" * 64], extra=[incomplete]))


# --- Membership grows by appending a chained record, never by an edit ------------


def test_a_late_found_capture_is_appended_and_leaves_the_declaration_untouched():
    """The founding complaint, one level up. A fourth capture found next month
    must not require editing a record that is already evidence, and must not
    re-derive `physical_page_id` under everything beneath it."""
    first = _membership(["a" * 64])
    second = _membership(["a" * 64, "b" * 64], predecessor=digest_of(first), run="triage-2")
    register = canonical_bytes({"schema": SCHEMA, "records": [_declaration(), first, second]})
    validated = validate_register_bytes(register)
    assert first in validated["records"], "the superseded link is retained, not rewritten"
    assert members_of(register, PAGE) == sorted(["a" * 64, "b" * 64])
    assert _declaration()["physical_page_id"] == PAGE, "the declaration cannot have moved"


def test_a_membership_link_that_does_not_name_its_predecessor_is_refused():
    """The chain is verified on read, not merely written: a link forged onto the
    end without naming what it succeeds is what a truncate-and-rewrite looks
    like from inside the file."""
    first = _membership(["a" * 64])
    forged = _membership(["a" * 64, "b" * 64], predecessor=digest_of(_membership(["z" * 64])))
    with pytest.raises(SchemaRefusal, match="does not name the digest"):
        validate_register_bytes(
            canonical_bytes({"schema": SCHEMA, "records": [_declaration(), first, forged]})
        )


def test_removing_a_middle_membership_link_breaks_every_successor():
    first = _membership(["a" * 64])
    second = _membership(["a" * 64, "b" * 64], predecessor=digest_of(first))
    third = _membership(["a" * 64, "b" * 64, "c" * 64], predecessor=digest_of(second))
    whole = [_declaration(), first, second, third]
    validate_register_bytes(canonical_bytes({"schema": SCHEMA, "records": whole}))
    with pytest.raises(SchemaRefusal, match="does not name the digest"):
        validate_register_bytes(
            canonical_bytes({"schema": SCHEMA, "records": [_declaration(), first, third]})
        )


def test_a_membership_record_may_not_withdraw_a_capture():
    """A capture declared to show a page is evidence. Correcting that is a
    retraction with a reason, never a shorter members list in a later link."""
    first = _membership(["a" * 64, "b" * 64])
    shrunk = _membership(["a" * 64], predecessor=digest_of(first), run="triage-2")
    with pytest.raises(SchemaRefusal, match="does not add a capture"):
        validate_register_bytes(
            canonical_bytes({"schema": SCHEMA, "records": [_declaration(), first, shrunk]})
        )


def test_membership_for_an_undeclared_physical_page_is_refused():
    with pytest.raises(SchemaRefusal, match="before any earlier record declares it"):
        validate_register_bytes(
            canonical_bytes({"schema": SCHEMA, "records": [_membership(["a" * 64])]})
        )


def test_membership_refuses_a_value_that_only_has_a_digest_length():
    value = canonical_bytes(
        {"schema": SCHEMA, "records": [_declaration(), _membership(["z" * 64])]}
    )
    with pytest.raises(SchemaRefusal, match="sorted unique source digests"):
        validate_register_bytes(value)


def test_a_correspondence_may_not_name_a_physical_act_nobody_minted():
    value = json.loads(_register(members=["a" * 64]))
    value["records"] = [row for row in value["records"] if row["kind"] != "physical-act"]
    with pytest.raises(SchemaRefusal, match="physical act"):
        validate_register_bytes(canonical_bytes(value))


def test_a_correspondence_cannot_move_an_act_minted_for_another_physical_page():
    other_page = physical_page_id("synthetic", "volume-1", "12v")
    other_act = physical_act_id(other_page, "entry-1")
    value = canonical_bytes(
        {
            "schema": SCHEMA,
            "records": [
                _declaration(),
                _declaration(
                    other_page,
                    corpus="synthetic",
                    volume="volume-1",
                    designation="12v",
                ),
                {
                    "kind": "physical-act",
                    "physical_page_id": other_page,
                    "mint_designation": "entry-1",
                    "physical_act_id": other_act,
                    "evidence": ["declared-fixture"],
                    "appending_run": "triage-1",
                },
                _correspondence(
                    "pg_0123456789abcdef",
                    "act_0123456789abcdef",
                    physical_page=PAGE,
                    physical_act=other_act,
                ),
            ],
        }
    )
    with pytest.raises(SchemaRefusal, match="minted for a different physical page"):
        validate_register_bytes(value)


def test_resolution_records_must_name_evidence_and_well_formed_local_identities():
    register = json.loads(_register(members=["a" * 64]))
    correspondence = next(
        record for record in register["records"] if record["kind"] == "correspondence"
    )
    correspondence["evidence"] = []
    with pytest.raises(SchemaRefusal, match="must name one or more evidence"):
        validate_register_bytes(canonical_bytes(register))

    correspondence["evidence"] = ["declared-fixture"]
    correspondence["page_id"] = "not-a-page"
    with pytest.raises(SchemaRefusal, match="well-formed pg_ identity"):
        validate_register_bytes(canonical_bytes(register))


def test_a_retraction_may_only_name_an_earlier_correspondence():
    value = canonical_bytes(
        {
            "schema": SCHEMA,
            "records": [
                _declaration(),
                {
                    "kind": "retraction",
                    "retracts": PAGE,
                    "reason": "this target is not a correspondence",
                    "appending_run": "triage-2",
                },
            ],
        }
    )
    with pytest.raises(SchemaRefusal, match="no earlier correspondence"):
        validate_register_bytes(value)


def test_two_unicode_spellings_of_one_designation_declare_one_physical_page():
    """The only identities in this system bound to text a person types.

    NFC and NFD spell the same folio label in different bytes, and
    `canonical_bytes` hashes bytes -- so without normalisation triage declares
    one physical page twice, each valid, with nothing anywhere to reconcile the
    two. Normalised, the second declaration collides with the first and is
    refused out loud instead."""
    composed = "folio-12ré"  # é
    decomposed = "folio-12ré"  # e + combining acute
    assert composed != decomposed
    page = physical_page_id("corpus", "volume-1", composed)
    assert physical_page_id("corpus", "volume-1", decomposed) == page
    with pytest.raises(SchemaRefusal, match="repeats immutable record"):
        validate_register_bytes(
            canonical_bytes(
                {
                    "schema": SCHEMA,
                    "records": [
                        _declaration(page, corpus="corpus", designation=composed),
                        _declaration(page, corpus="corpus", designation=decomposed),
                    ],
                }
            )
        )


# --- The only writer preserves a complete predecessor or a complete successor -----


def test_append_records_creates_and_extends_one_valid_register(tmp_path):
    path = tmp_path / "register.json"
    first_digest = append_records(path, [_declaration()], expected_digest=EMPTY_REGISTER_DIGEST)
    assert first_digest == register_digest(path.read_bytes())

    membership = _membership(["a" * 64])
    second_digest = append_records(path, [membership], expected_digest=first_digest)
    assert second_digest == register_digest(path.read_bytes())
    assert members_of(path.read_bytes(), PAGE) == ["a" * 64]


def test_a_stale_writer_cannot_overwrite_a_concurrent_append(tmp_path):
    path = tmp_path / "register.json"
    current = append_records(path, [_declaration()], expected_digest=EMPTY_REGISTER_DIGEST)
    before = path.read_bytes()

    with pytest.raises(IncompatibleReuse, match="changed after this writer read it"):
        append_records(path, [_membership(["a" * 64])], expected_digest=EMPTY_REGISTER_DIGEST)

    assert path.read_bytes() == before
    assert register_digest(before) == current


def test_a_failed_atomic_publish_leaves_the_complete_predecessor(tmp_path, monkeypatch):
    path = tmp_path / "register.json"
    path.write_bytes(empty_register())

    def fail_replace(_source, _target):
        raise OSError("simulated crash before rename")

    monkeypatch.setattr(corpus_register.os, "replace", fail_replace)
    with pytest.raises(ContractError, match="was not replaced"):
        append_records(path, [_declaration()], expected_digest=EMPTY_REGISTER_DIGEST)

    assert path.read_bytes() == empty_register()
    assert list(tmp_path.glob(".register.json.tmp-*")) == []


# --- The sealed snapshot, and the check that cannot be skipped -------------------


def test_physical_page_record_survives_two_run_snapshots(tmp_path):
    snapshot = _register(members=["a" * 64])
    shared = {
        "source_manifest": [{"ordinal": 1, "relative_path": "fixture.png", "sha256": "a" * 64}],
        "config_digest": "c" * 64,
        "adapter_recipes": {"designator": "fixture"},
        "witness_chairs": [],
        "register_bytes": snapshot,
    }
    first = RunTree.create(tmp_path, "first", **shared)
    second = RunTree.create(tmp_path, "second", **shared)
    first_bytes = first.read_bytes(first.blob_path("door", first.read_run()["register_digest"]))
    second_bytes = second.read_bytes(second.blob_path("door", second.read_run()["register_digest"]))
    assert first_bytes == second_bytes == snapshot
    assert read_snapshot(first, first.read_run()) == snapshot
    assert read_snapshot(second, second.read_run()) == snapshot


def test_a_tampered_run_snapshot_is_refused_by_the_register_reader(tmp_path):
    snapshot = _register(members=["a" * 64])
    tree = RunTree.create(
        tmp_path,
        "r1",
        source_manifest=[{"ordinal": 1, "relative_path": "fixture.png", "sha256": "a" * 64}],
        config_digest="c" * 64,
        adapter_recipes={"designator": "fixture"},
        witness_chairs=[],
        register_bytes=snapshot,
    )
    run = tree.read_run()
    snapshot_path = tree.root / tree.blob_path("door", run["register_digest"])
    snapshot_path.write_bytes(empty_register())

    with pytest.raises(IncompatibleReuse, match="do not match run.json's register_digest"):
        read_snapshot(tree, run)


def test_a_run_bound_to_a_register_refuses_a_stage_that_was_given_none():
    """A check an operator disables by forgetting a flag is not a check: the
    appended correspondence would otherwise reach half a run's stages."""
    run = {"register_digest": register_digest(_register(members=["a" * 64]))}
    with pytest.raises(IncompatibleReuse, match="must be given --corpus-register"):
        verify_snapshot_is_current(run, None)


def test_a_run_bound_to_no_register_needs_no_flag():
    verify_snapshot_is_current({"register_digest": EMPTY_REGISTER_DIGEST}, None)
    assert register_digest(empty_register()) == EMPTY_REGISTER_DIGEST


def test_a_refused_register_reuse_leaves_no_bytes_in_the_existing_run(tmp_path):
    """`create` promises a rejected reuse leaves the tree exactly as it found
    it, and its own refusal says "Nothing was written". Snapshotting the
    register before the authority accepted it made both false: an incompatible
    register's bytes landed in the existing run's blob store on the way out."""
    shared = {
        "source_manifest": [{"ordinal": 1, "relative_path": "fixture.png", "sha256": "a" * 64}],
        "config_digest": "c" * 64,
        "adapter_recipes": {"designator": "fixture"},
        "witness_chairs": [],
    }
    tree = RunTree.create(tmp_path, "r1", register_bytes=empty_register(), **shared)
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))

    foreign = _register(members=["a" * 64])
    with pytest.raises(IncompatibleReuse, match="register_digest"):
        RunTree.create(tmp_path, "r1", register_bytes=foreign, **shared)
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == before
    assert not (tree.root / tree.blob_path("door", register_digest(foreign))).exists()
