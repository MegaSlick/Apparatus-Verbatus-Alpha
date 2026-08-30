"""The corpus register declares correspondence without choosing a capture."""

import json
import pathlib
import sys

import pytest

import common.corpus_register as corpus_register
from common.contracts.canonical import canonical_bytes, digest_of
from common.contracts.errors import ContractError, IncompatibleReuse, SchemaRefusal
from common.contracts.identities import act_id, page_id, physical_act_id, physical_page_id
from common.corpus_register import (
    EMPTY_REGISTER_DIGEST,
    SCHEMA,
    append_records,
    confirm_unchanged_head,
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
CAPTURE_PAGE = page_id({"kind": "source", "sha256": "c" * 64}, {"operation": "whole"})
CAPTURE_BOUNDS = {"x": 10, "y": 20, "w": 30, "h": 40}
CAPTURE_ACT = act_id(CAPTURE_PAGE, "proposal", CAPTURE_BOUNDS)


def _declaration(
    page=PAGE, *, corpus="synthetic", volume="volume-1", designation="12r", run="triage-1"
):
    return {
        "kind": "physical-page",
        "corpus_id": corpus,
        "volume_id": volume,
        "designation": designation,
        "physical_page_id": page,
        "appending_run": run,
    }


def _membership(members, *, page=PAGE, predecessor=None, run="triage-1"):
    return {
        "kind": "membership",
        "physical_page_id": page,
        "members": sorted(set(members)),
        "predecessor": predecessor,
        "appending_run": run,
    }


def _correspondence(
    page_identity,
    act,
    bounds,
    *,
    act_class="proposal",
    physical_page=PAGE,
    physical_act=ACT,
):
    return {
        "kind": "correspondence",
        "page_id": page_identity,
        "act_id": act,
        "act_class": act_class,
        "act_bounds": bounds,
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
                _correspondence(CAPTURE_PAGE, CAPTURE_ACT, CAPTURE_BOUNDS),
                *extra,
            ],
        }
    )


def _record(register, kind):
    return next(row for row in json.loads(register)["records"] if row["kind"] == kind)


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
    canonical-order gate before either reaches a run tree.

    The run-tree half is what this proves. `_membership` sorts its argument, so
    the two register byte strings are already identical here and comparing them
    would be comparing the helper with itself — the ordering guarantee is the
    register's own, and it is asserted at its boundary in the test above.
    """
    forward = _register(members=["a" * 64, "b" * 64])
    reversed_order = _register(members=["b" * 64, "a" * 64])

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


def test_a_membership_record_naming_no_capture_is_refused():
    """A record that asserts nothing cannot be told from no record at all.

    `members_of` reports `[]` for a page with no membership record, so an empty
    one is invisible — and being immutable, it cannot be retracted either, since
    a retraction names an assertion that was never made.
    """
    value = {"schema": SCHEMA, "records": [_declaration(), _membership([])]}
    with pytest.raises(SchemaRefusal, match="names no capture"):
        validate_register_bytes(canonical_bytes(value))


def test_preference_field_is_refused_at_the_register_boundary():
    value = json.loads(_register(members=["a" * 64]))
    value["records"][0]["preferred"] = "a" * 64
    with pytest.raises(SchemaRefusal, match="preference"):
        validate_register_bytes(canonical_bytes(value))


def test_declared_correspondence_resolves_two_capture_proposals_to_one_physical_act():
    value = json.loads(_register(members=["a" * 64, "b" * 64]))
    first = _record(_register(members=["a" * 64]), "correspondence")
    second_page = page_id({"kind": "source", "sha256": "d" * 64}, {"operation": "whole"})
    second_bounds = {"x": 11, "y": 21, "w": 31, "h": 41}
    second_act = act_id(second_page, "proposal", second_bounds)
    second = _correspondence(second_page, second_act, second_bounds)
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
    """Two captures of one act resolve by declaration, never by hash coincidence.

    Capture A shows acts {1,2,3,4} of physical page P. Capture B is a re-shoot
    of the same opening: it shows only act 4 of P (at different bounds -- a
    different photograph) plus an act of the facing physical page Q. Nothing
    about deriving `page_id`/`act_id` from either capture can collide them
    (they bind distinct source digests), so reconciling A's act 4 and B's act
    4 into one physical act is exactly the declared correspondence this
    register exists to carry -- never a hash coincidence.

    The shared act's two image-local proposals resolve to one physical act, the
    facing page's act stays under its own physical page, and neither collides
    with the other's identity.
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
                    page_a,
                    acts_a[3],
                    {"x": 40, "y": 0, "w": 8, "h": 8},
                    physical_page=physical_p,
                    physical_act=physical_p4,
                ),
                _correspondence(
                    page_b,
                    act_4b,
                    {"x": 41, "y": 1, "w": 9, "h": 9},
                    physical_page=physical_p,
                    physical_act=physical_p4,
                ),
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
    # caller-side policy the physical-act partition builder owns.
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
    with pytest.raises(SchemaRefusal, match="which no earlier correspondence or membership"):
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


def test_a_physical_page_declaration_must_name_the_run_that_appended_it():
    """A folio typed against the wrong volume stands for ever -- the register is
    append-only, and correctly so. What the operator then needs is the rest of
    what that same pass entered, and only this field can be searched on. An
    absent one is a closed-record refusal; a present but empty one is refused by
    name, so a writer cannot satisfy the shape while naming nothing."""
    declaration = _declaration()
    without = {key: value for key, value in declaration.items() if key != "appending_run"}
    with pytest.raises(SchemaRefusal, match="closed record"):
        validate_register_bytes(canonical_bytes({"schema": SCHEMA, "records": [without]}))
    with pytest.raises(SchemaRefusal, match="physical-page record names no appending run"):
        validate_register_bytes(
            canonical_bytes({"schema": SCHEMA, "records": [{**declaration, "appending_run": ""}]})
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
    declaration = _declaration()
    register = canonical_bytes({"schema": SCHEMA, "records": [declaration, first, second]})
    validated = validate_register_bytes(register)
    assert first in validated["records"], "the superseded link is retained, not rewritten"
    assert members_of(register, PAGE) == sorted(["a" * 64, "b" * 64])
    # The record the docstring is actually about, read back out of the validated
    # register. `_declaration()["physical_page_id"] == PAGE` used to stand here,
    # which compares the helper's own default with the constant it puts there and
    # holds however the register treats the declaration.
    assert declaration in validated["records"], "the declaration is retained unedited"
    assert [row for row in validated["records"] if row["kind"] == "physical-page"] == [
        declaration
    ], "the append neither rewrote the declaration nor added a second one"


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
                    CAPTURE_PAGE,
                    CAPTURE_ACT,
                    CAPTURE_BOUNDS,
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


def test_a_correspondence_cannot_pair_an_act_with_a_different_capture_page():
    register = json.loads(_register(members=["a" * 64]))
    correspondence = next(
        record for record in register["records"] if record["kind"] == "correspondence"
    )
    correspondence["page_id"] = page_id(
        {"kind": "source", "sha256": "e" * 64}, {"operation": "whole"}
    )
    with pytest.raises(SchemaRefusal, match="does not bind the page, class, and bounds"):
        validate_register_bytes(canonical_bytes(register))

    correspondence["evidence"] = ["declared-fixture"]
    correspondence["page_id"] = "not-a-page"
    with pytest.raises(SchemaRefusal, match="well-formed pg_ identity"):
        validate_register_bytes(canonical_bytes(register))


def test_a_retraction_may_only_name_a_correspondence_or_a_membership_head():
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
    with pytest.raises(SchemaRefusal, match="no earlier correspondence or membership"):
        validate_register_bytes(value)


def test_register_lookups_refuse_malformed_identity_tokens():
    register = _register(members=["a" * 64])
    with pytest.raises(SchemaRefusal, match="well-formed act_ identity"):
        resolve_proposal(register, "not-an-act")
    with pytest.raises(SchemaRefusal, match="well-formed ppg_ identity"):
        members_of(register, "ppg_not-a-digest")


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


def test_append_records_refuses_a_symlinked_lock_path(tmp_path):
    """A predictable lock name is safe only if opening it never follows a symlink.

    Anything able to plant `.register.json.lock` before the first writer arrives
    would otherwise redirect every future writer's exclusion lock onto a file of
    its choosing -- defeating the serialization `append_records` exists to
    provide, and doing so with no trace beyond the symlink itself.
    """
    path = tmp_path / "register.json"
    target = tmp_path / "elsewhere"
    target.write_bytes(b"untouched")
    lock_path = path.with_name(f".{path.name}.lock")
    lock_path.symlink_to(target)

    with pytest.raises(SchemaRefusal, match="lock could not be opened without following"):
        append_records(path, [_declaration()], expected_digest=EMPTY_REGISTER_DIGEST)

    assert target.read_bytes() == b"untouched"
    assert lock_path.is_symlink()
    assert not path.exists()


def test_a_stale_writer_cannot_overwrite_a_concurrent_append(tmp_path):
    path = tmp_path / "register.json"
    current = append_records(path, [_declaration()], expected_digest=EMPTY_REGISTER_DIGEST)
    before = path.read_bytes()

    with pytest.raises(IncompatibleReuse, match="changed after this writer read it"):
        append_records(path, [_membership(["a" * 64])], expected_digest=EMPTY_REGISTER_DIGEST)

    assert path.read_bytes() == before
    assert register_digest(before) == current


def test_a_path_swap_after_predecessor_read_is_refused_by_device_and_inode(tmp_path, monkeypatch):
    path = tmp_path / "register.json"
    path.write_bytes(empty_register())
    replacement = tmp_path / "replacement.json"
    foreign = canonical_bytes({"schema": SCHEMA, "records": [_declaration()]})
    replacement.write_bytes(foreign)
    real_digest = corpus_register.register_digest
    calls = 0

    def swap_after_observed(data):
        nonlocal calls
        calls += 1
        digest = real_digest(data)
        if calls == 1:
            corpus_register.os.replace(replacement, path)
        return digest

    monkeypatch.setattr(corpus_register, "register_digest", swap_after_observed)
    with pytest.raises(IncompatibleReuse, match="path changed after its predecessor"):
        append_records(path, [_declaration()], expected_digest=EMPTY_REGISTER_DIGEST)
    assert path.read_bytes() == foreign


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


def test_a_register_symlink_is_never_read_or_replaced(tmp_path):
    target = tmp_path / "outside.json"
    target.write_bytes(empty_register())
    linked = tmp_path / "register.json"
    linked.symlink_to(target)

    with pytest.raises(SchemaRefusal, match="direct, readable regular file"):
        append_records(linked, [_declaration()], expected_digest=EMPTY_REGISTER_DIGEST)

    assert linked.is_symlink()
    assert target.read_bytes() == empty_register()


def test_a_symlinked_register_lock_cannot_disable_writer_serialization(tmp_path):
    path = tmp_path / "register.json"
    path.write_bytes(empty_register())
    victim = tmp_path / "victim"
    victim.write_bytes(b"unchanged")
    path.with_name(".register.json.lock").symlink_to(victim)

    with pytest.raises(SchemaRefusal, match="lock could not be opened"):
        append_records(path, [_declaration()], expected_digest=EMPTY_REGISTER_DIGEST)

    assert path.read_bytes() == empty_register()
    assert victim.read_bytes() == b"unchanged"


def test_a_platform_without_flock_refuses_the_append_rather_than_running_unserialized(
    tmp_path, monkeypatch
):
    """No lock means no append. The compare-and-swap alone cannot stand in for it.

    Two writers that both read digest D both satisfy the `expected_digest` check,
    and the second replace discards the first's records. Proceeding unserialized
    would lose an append behind a successful return, so the platform gap is named.
    """
    monkeypatch.setitem(sys.modules, "fcntl", None)
    path = tmp_path / "register.json"
    path.write_bytes(empty_register())

    with pytest.raises(SchemaRefusal, match="cannot lock a corpus register"):
        append_records(path, [_declaration()], expected_digest=EMPTY_REGISTER_DIGEST)

    assert path.read_bytes() == empty_register()


def test_register_bytes_and_replay_counts_are_bounded_before_amplification(monkeypatch):
    monkeypatch.setattr(corpus_register, "MAX_REGISTER_BYTES", len(empty_register()) - 1)
    with pytest.raises(SchemaRefusal, match="byte validation bound"):
        validate_register_bytes(empty_register())

    monkeypatch.setattr(corpus_register, "MAX_REGISTER_BYTES", 1024 * 1024)
    monkeypatch.setattr(corpus_register, "MAX_REGISTER_RECORDS", 0)
    one_record = canonical_bytes({"schema": SCHEMA, "records": [_declaration()]})
    with pytest.raises(SchemaRefusal, match="record replay bound"):
        validate_register_bytes(one_record)


def test_pathologically_nested_json_is_refused_for_its_depth_not_its_encoding():
    """The refusal has to send an operator to the problem it actually has.

    A deeply nested register is valid UTF-8 and valid JSON; only its structure
    defeats the parser. Reported as "not UTF-8 JSON", it sent whoever read it to
    check the file's encoding, where there is nothing wrong.

    The depth must beat the parser's C recursion allowance on every platform:
    10,000 exhausted it on macOS but parsed cleanly on the Linux CI runners
    (CPython's C recursion limit is platform-dependent), where validation then
    reached an unrelated per-record refusal instead of the depth refusal.
    """
    depth = 1_000_000
    data = b'{"schema":"corpus-register-v1","records":' + b"[" * depth + b"]" * depth + b"}"
    with pytest.raises(SchemaRefusal, match="nested too deeply") as caught:
        validate_register_bytes(data)
    assert "not UTF-8 JSON" not in str(caught.value)


def test_a_lock_symlink_refuses_before_a_register_is_created_at_all(tmp_path):
    """The register-symlink half of this case is covered above, with a stronger match.

    What is only here is the first append to a path that does not exist yet: the lock
    is opened before the register is created, so a redirected lock must refuse without
    leaving a register behind for the next writer to extend.
    """
    register = tmp_path / "register.json"
    lock_target = tmp_path / "outside.lock"
    lock_target.write_bytes(b"untouched")
    (tmp_path / ".register.json.lock").symlink_to(lock_target)
    with pytest.raises(SchemaRefusal, match="lock could not be opened"):
        append_records(register, [_declaration()], expected_digest=EMPTY_REGISTER_DIGEST)
    assert lock_target.read_bytes() == b"untouched"
    assert not register.exists()


def test_register_hardlink_is_refused_as_an_aliased_mutable_head(tmp_path):
    target = tmp_path / "outside.json"
    target.write_bytes(empty_register())
    register = tmp_path / "register.json"
    register.hardlink_to(target)
    with pytest.raises(SchemaRefusal, match="unaliased regular file"):
        append_records(register, [_declaration()], expected_digest=EMPTY_REGISTER_DIGEST)
    assert target.read_bytes() == empty_register()


def test_a_directory_fsync_failure_reports_a_complete_but_not_proven_durable_successor(
    tmp_path, monkeypatch
):
    """A crash window after rename is not misreported as an untouched register."""
    path = tmp_path / "register.json"
    path.write_bytes(empty_register())
    real_fsync = corpus_register.os.fsync
    calls = 0

    def fail_second_fsync(descriptor):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated crash before directory durability")
        return real_fsync(descriptor)

    monkeypatch.setattr(corpus_register.os, "fsync", fail_second_fsync)
    with pytest.raises(ContractError, match="was replaced but its directory entry is not proven"):
        append_records(path, [_declaration()], expected_digest=EMPTY_REGISTER_DIGEST)

    # Rename is atomic: even in the uncertain-durability window, a live process
    # sees the complete successor rather than a torn JSON document.
    validate_register_bytes(path.read_bytes())
    assert json.loads(path.read_bytes())["records"] == [_declaration()]
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
    run = {
        "register_digest": register_digest(_register(members=["a" * 64])),
        "register_required": True,
    }
    with pytest.raises(IncompatibleReuse, match="must be given --corpus-register"):
        verify_snapshot_is_current(run, None)


def test_an_explicitly_empty_register_still_requires_the_live_register_flag():
    """An empty live register can grow after ingress, so its presence is a bound fact."""
    run = {"register_digest": EMPTY_REGISTER_DIGEST, "register_required": True}
    with pytest.raises(IncompatibleReuse, match="must be given --corpus-register"):
        verify_snapshot_is_current(run, None)


def test_a_run_bound_to_no_register_needs_no_flag():
    verify_snapshot_is_current(
        {"register_digest": EMPTY_REGISTER_DIGEST, "register_required": False}, None
    )
    assert register_digest(empty_register()) == EMPTY_REGISTER_DIGEST


def test_a_run_created_without_a_register_refuses_one_introduced_later(tmp_path):
    register_path = tmp_path / "register.json"
    register_path.write_bytes(empty_register())
    run = {"register_digest": EMPTY_REGISTER_DIGEST, "register_required": False}
    with pytest.raises(IncompatibleReuse, match="may not introduce one"):
        verify_snapshot_is_current(run, str(register_path))


def test_a_stage_never_follows_a_live_register_symlink(tmp_path):
    target = tmp_path / "outside.json"
    target.write_bytes(empty_register())
    linked = tmp_path / "register.json"
    linked.symlink_to(target)
    run = {"register_digest": EMPTY_REGISTER_DIGEST, "register_required": True}

    with pytest.raises(IncompatibleReuse, match="could not be read"):
        verify_snapshot_is_current(run, str(linked))


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


def _retraction(target, *, reason="a human confirmed two blank forms as one page", run="triage-2"):
    return {"kind": "retraction", "retracts": target, "reason": reason, "appending_run": run}


def test_a_wrong_membership_is_corrected_by_retracting_the_head_not_by_editing_it():
    """The instrument's blindness case, made answerable.

    Two blank forms agree everywhere because neither carries ink, so a human can
    confirm them as one physical page and be wrong. Membership grows and is never
    edited, so the correction is a retraction of the newest link: the record stays
    in the register as evidence of what was declared, and stops being the answer
    to what shows this page.
    """
    first = _membership(["a" * 64, "b" * 64])
    second = _membership(["a" * 64, "b" * 64, "c" * 64], predecessor=digest_of(first))
    retraction = _retraction(f"membership:{digest_of(second)}")
    records = [_declaration(), first, second, retraction]
    register = canonical_bytes({"schema": SCHEMA, "records": records})
    validated = validate_register_bytes(register)
    assert retraction in validated["records"]
    assert second in validated["records"], "the retracted link is still present as evidence"
    assert members_of(register, PAGE) == sorted(["a" * 64, "b" * 64])

    # Unwinding continues link by link; a page back to no link at all is the empty
    # list its declaration always meant, not a deleted page.
    both = records + [_retraction(f"membership:{digest_of(first)}", run="triage-3")]
    assert members_of(canonical_bytes({"schema": SCHEMA, "records": both}), PAGE) == []


def test_a_membership_retraction_must_name_the_head_of_its_chain():
    """Every successor contains its predecessor's captures, so only the head can go."""
    first = _membership(["a" * 64, "b" * 64])
    second = _membership(["a" * 64, "b" * 64, "c" * 64], predecessor=digest_of(first))
    with pytest.raises(SchemaRefusal, match="not the current head"):
        validate_register_bytes(
            canonical_bytes(
                {
                    "schema": SCHEMA,
                    "records": [
                        _declaration(),
                        first,
                        second,
                        _retraction(f"membership:{digest_of(first)}"),
                    ],
                }
            )
        )


def test_a_membership_retraction_naming_no_link_in_this_register_is_refused():
    # The membership branch's own wording, not the tail every refusal shares and not
    # the sentence the generic branch also raises: an operator told "already retracted"
    # for a digest that was never here looks for a retraction that does not exist
    # instead of for the typo, and a test matching the shared half would pass with the
    # `membership:` routing deleted.
    with pytest.raises(SchemaRefusal, match="membership link .* never declares"):
        validate_register_bytes(
            canonical_bytes(
                {
                    "schema": SCHEMA,
                    "records": [_declaration(), _retraction(f"membership:{'0' * 64}")],
                }
            )
        )


def test_membership_grows_again_from_the_link_that_survived_a_retraction():
    """A corrected page is not a frozen page: the chain continues from the survivor."""
    first = _membership(["a" * 64, "b" * 64])
    wrong = _membership(["a" * 64, "b" * 64, "c" * 64], predecessor=digest_of(first))
    corrected = _membership(
        ["a" * 64, "b" * 64, "d" * 64], predecessor=digest_of(first), run="triage-3"
    )
    register = canonical_bytes(
        {
            "schema": SCHEMA,
            "records": [
                _declaration(),
                first,
                wrong,
                _retraction(f"membership:{digest_of(wrong)}"),
                corrected,
            ],
        }
    )
    assert members_of(register, PAGE) == sorted(["a" * 64, "b" * 64, "d" * 64])
    # A successor that still names the retracted link as its predecessor is refused:
    # the chain is what makes the correction visible rather than an edit.
    with pytest.raises(SchemaRefusal, match="does not name the digest"):
        validate_register_bytes(
            canonical_bytes(
                {
                    "schema": SCHEMA,
                    "records": [
                        _declaration(),
                        first,
                        wrong,
                        _retraction(f"membership:{digest_of(wrong)}"),
                        _membership(
                            ["a" * 64, "b" * 64, "c" * 64, "e" * 64],
                            predecessor=digest_of(wrong),
                            run="triage-3",
                        ),
                    ],
                }
            )
        )


def test_retracting_one_link_twice_names_that_it_was_already_retracted():
    """The withdrawn record stays evidence but is neither a head nor reusable identity."""
    first = _membership(["a" * 64, "b" * 64])
    withdrawal = _retraction(f"membership:{digest_of(first)}")
    twice = canonical_bytes(
        {
            "schema": SCHEMA,
            "records": [
                _declaration(),
                first,
                withdrawal,
                _retraction(f"membership:{digest_of(first)}", run="triage-3"),
            ],
        }
    )
    with pytest.raises(SchemaRefusal, match="already retracted"):
        validate_register_bytes(twice)


def test_a_fresh_act_may_reappend_the_members_of_a_retracted_link():
    first = _membership(["a" * 64, "b" * 64])
    withdrawal = _retraction(f"membership:{digest_of(first)}")

    # Reasserting the same members is a new operator act, not resurrection of the
    # withdrawn immutable link, so its appending-run identity makes a new record.
    reasserted = _membership(["a" * 64, "b" * 64], run="triage-3")
    register = canonical_bytes(
        {
            "schema": SCHEMA,
            "records": [_declaration(), first, withdrawal, reasserted],
        }
    )
    assert members_of(register, PAGE) == sorted(["a" * 64, "b" * 64])


def test_a_correspondence_declared_twice_without_a_retraction_is_refused():
    """A second identical declaration records nothing new and is not evidence."""
    duplicate = _correspondence(CAPTURE_PAGE, CAPTURE_ACT, CAPTURE_BOUNDS)
    with pytest.raises(SchemaRefusal, match="declaring it twice records nothing new"):
        validate_register_bytes(_register(members=["a" * 64], extra=[duplicate]))


def test_a_retracted_correspondence_is_reasserted_by_a_new_run_not_resurrected():
    """A retraction made in error must not be a corpus-lifetime fact.

    Membership already works this way: reasserting withdrawn members is a new
    operator act with its own appending run. A correspondence that could not be
    reasserted at all would leave a wrongly corrected act with only one route
    back to its physical act -- minting a second one for it, which is the
    duplication the whole correspondence step exists to prevent.
    """
    act = CAPTURE_ACT
    declaration = _correspondence(CAPTURE_PAGE, act, CAPTURE_BOUNDS)
    withdrawal = {
        "kind": "retraction",
        "retracts": f"{act}->{ACT}",
        "reason": "a person confirmed two frames as one page and was wrong",
        "appending_run": "triage-2",
    }
    withdrawn = _register(members=["a" * 64], extra=[withdrawal])
    assert resolve_proposal(withdrawn, act) == {
        "outcome": "finding",
        "code": "retracted-physical-act",
        "act_id": act,
    }
    reasserted = {**declaration, "appending_run": "triage-3"}
    restored = _register(members=["a" * 64], extra=[withdrawal, reasserted])
    resolution = resolve_proposal(restored, act)
    assert resolution["outcome"] == "resolved"
    assert resolution["physical_act_id"] == ACT
    assert json.loads(restored)["records"][-3:] == [declaration, withdrawal, reasserted]
    with pytest.raises(SchemaRefusal, match="a retraction that corrects nothing"):
        validate_register_bytes(
            _register(
                members=["a" * 64],
                extra=[withdrawal, {**withdrawal, "appending_run": "triage-4"}],
            )
        )


def test_a_reasserted_correspondence_is_retractable_again(tmp_path):
    """The lifecycle closes: declare, withdraw, reassert, withdraw again."""
    act = CAPTURE_ACT
    withdrawal = {
        "kind": "retraction",
        "retracts": f"{act}->{ACT}",
        "reason": "wrong capture",
        "appending_run": "triage-2",
    }
    reasserted = {
        **_correspondence(CAPTURE_PAGE, act, CAPTURE_BOUNDS),
        "appending_run": "triage-3",
    }
    again = {**withdrawal, "reason": "wrong a second time", "appending_run": "triage-4"}
    register = _register(members=["a" * 64], extra=[withdrawal, reasserted, again])
    assert resolve_proposal(register, act)["code"] == "retracted-physical-act"
    assert len(json.loads(register)["records"]) == 7


def test_replaying_a_withdrawn_correspondence_verbatim_is_refused():
    """Named for what it measures. The refusal here is the immutable-record one.

    It was called "must be a new operator run", but the identity this replay
    collides with is the declaration's, held in `seen` from the first
    declaration -- so the refusal fires whether or not the run qualification
    exists. The run qualification is proved by
    `test_a_retracted_correspondence_is_reasserted_by_a_new_run_not_resurrected`,
    which needs the suffix for its reassertion to be accepted at all.
    """
    act = CAPTURE_ACT
    declaration = _correspondence(CAPTURE_PAGE, act, CAPTURE_BOUNDS)
    withdrawal = {
        "kind": "retraction",
        "retracts": f"{act}->{ACT}",
        "reason": "wrong capture",
        "appending_run": "triage-2",
    }
    replayed = {**declaration, "evidence": ["operator:looked-again"]}
    with pytest.raises(SchemaRefusal, match="repeats immutable record"):
        validate_register_bytes(_register(members=["a" * 64], extra=[withdrawal, replayed]))


def test_a_cleanup_only_failure_says_the_register_was_already_published(tmp_path, monkeypatch):
    """The head moved, so the refusal may not read as "nothing was written".

    `os.replace` and the directory fsync have both succeeded by the time the leftover
    is removed. A caller that reads a bare "temporary could not be removed" as a failed
    append rebuilds it against the previous digest, and the moved head then refuses that
    as a concurrent change — two refusals, no explanation, for one durable publish.
    """
    path = tmp_path / "register.json"
    first = append_records(path, [_declaration()], expected_digest=EMPTY_REGISTER_DIGEST)
    original_unlink = pathlib.Path.unlink

    def refuse_temporary_unlink(self, *args, **kwargs):
        if ".tmp-" in self.name:
            raise OSError("simulated cleanup refusal")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "unlink", refuse_temporary_unlink)
    membership = _membership(["a" * 64])
    with pytest.raises(SchemaRefusal, match="was replaced and is durable") as refusal:
        append_records(path, [membership], expected_digest=first)
    assert "do not retry this append against the previous digest" in str(refusal.value).lower()
    # The current digest travels in the message, so an operator holding only the error
    # text does not have to re-read the file to build the next append.
    assert register_digest(path.read_bytes()) in str(refusal.value)

    # The published half of the claim, not just its wording: the append is on disk.
    assert members_of(path.read_bytes(), PAGE) == ["a" * 64]
    assert register_digest(path.read_bytes()) != first


def test_both_writers_refuse_a_malformed_expected_digest_before_touching_the_register(tmp_path):
    """The compare-and-swap's other half: what the writer claims it observed.

    `append_records` and `confirm_unchanged_head` take one door to the register now,
    and nothing had ever asserted that door refuses a digest that is not a digest —
    so the check could be deleted from the shared helper without a failure, which is
    exactly the risk of moving a safety rule into one place. An uppercase or truncated
    digest can never equal a computed head, so accepting one would let a writer sail
    past the comparison into a locked read on a claim that means nothing.
    """
    path = tmp_path / "register.json"
    first = append_records(path, [_declaration()], expected_digest=EMPTY_REGISTER_DIGEST)
    original = path.read_bytes()
    for malformed in ("", EMPTY_REGISTER_DIGEST.upper(), EMPTY_REGISTER_DIGEST[:63], "z" * 64):
        with pytest.raises(SchemaRefusal, match="lowercase SHA-256"):
            append_records(path, [_membership(["a" * 64])], expected_digest=malformed)
        with pytest.raises(SchemaRefusal, match="lowercase SHA-256"):
            confirm_unchanged_head(path, expected_digest=malformed)
    assert path.read_bytes() == original
    assert confirm_unchanged_head(path, expected_digest=first) == first
