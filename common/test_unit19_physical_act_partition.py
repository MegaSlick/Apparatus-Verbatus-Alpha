"""Unit 19A's total, geometry-only correspondence boundary."""

import json
from pathlib import Path

import pytest

import common.physical_act_partition as _partition_module
from common.contracts.canonical import digest_bytes, self_hash
from common.contracts.errors import IncompatibleReuse, SchemaRefusal
from common.contracts.identities import act_id, physical_act_id, physical_page_id
from common.corpus_register import (
    append_records,
    empty_register,
    members_of,
    membership_heads,
    register_digest,
    resolve_proposal,
)
from common.physical_act_partition import (
    PARTITION_SCHEMA,
    PROPOSAL_SCHEMA,
    append_correspondence_proposal,
    build_correspondence_proposal,
    build_physical_act_partition,
    validate_correspondence_proposal,
    validate_physical_act_partition,
)

SOURCE_A = "a" * 64
SOURCE_B = "b" * 64
PAGE = physical_page_id("fixture", "book", "12r")
ACT_A = act_id("pg_" + "1" * 16, "proposal", {"x": 0, "y": 0, "w": 10, "h": 10})
ACT_B = act_id("pg_" + "2" * 16, "proposal", {"x": 0, "y": 0, "w": 10, "h": 10})
# The rest of the shared constants, above their first use rather than halfway
# down the file. Reading them from function bodies worked only because those
# reads happen at call time, after import; a later module-level use, such as a
# parametrize argument, would have failed the import and taken every test in
# this file out of the run instead of failing visibly.
SOURCE_C = "c" * 64
PAGE_13R = physical_page_id("fixture", "book", "13r")
PG1 = "pg_" + "1" * 16
PG2 = "pg_" + "2" * 16
PG3 = "pg_" + "3" * 16
SEAL = {"relative_path": "designator/proposal.json", "sha256": digest_bytes(b"seal")}


def _local(act, page, source, key, bounds=None):
    # The bounds must be the ones the act_id was minted from: the partition
    # re-derives the identity from page, class, and bounds and refuses a row
    # whose lineage does not close.
    return {
        "act_id": act,
        "act_key": key,
        "act_class": "proposal",
        "act_bounds": dict(bounds or {"x": 0, "y": 0, "w": 10, "h": 10}),
        "page_id": page,
        "page_ordinal": 1,
        "source_sha256": source,
        "proposal_refs": [f"proposal:{key}"],
    }


def _register(tmp_path: Path):
    path = tmp_path / "register.json"
    before = register_digest(empty_register())
    append_records(
        path,
        [
            {
                "kind": "physical-page",
                "corpus_id": "fixture",
                "volume_id": "book",
                "designation": "12r",
                "physical_page_id": PAGE,
                "appending_run": "triage",
            },
            {
                "kind": "membership",
                "physical_page_id": PAGE,
                "members": [SOURCE_A, SOURCE_B],
                "predecessor": None,
                "appending_run": "triage",
            },
        ],
        expected_digest=before,
    )
    return path


def test_discovery_append_uses_sealed_predecessor_then_the_old_run_is_stale(tmp_path):
    path = _register(tmp_path)
    predecessor = register_digest(path.read_bytes())
    proposal = build_correspondence_proposal(
        register=path.read_bytes(),
        register_digest=predecessor,
        discovery_run_id="discovery",
        components=[
            {
                "physical_page_id": PAGE,
                "physical_act_id": None,
                "local_acts": [_local(ACT_A, "pg_" + "1" * 16, SOURCE_A, "a")],
                "evidence": ["geometry:pair-a-b"],
                "finding": None,
            }
        ],
    )
    successor = append_correspondence_proposal(
        register_path=str(path), proposal=proposal, discovery_register_digest=predecessor
    )
    assert successor == register_digest(path.read_bytes())
    with pytest.raises(IncompatibleReuse, match="changed after this run"):
        from common.corpus_register import verify_snapshot_is_current

        verify_snapshot_is_current(
            {"register_digest": predecessor, "register_required": True}, str(path)
        )


def test_every_local_expected_act_occurs_in_exactly_one_physical_act_partition_row(tmp_path):
    path = _register(tmp_path)
    predecessor = register_digest(path.read_bytes())
    proposal = build_correspondence_proposal(
        register=path.read_bytes(),
        register_digest=predecessor,
        discovery_run_id="discovery",
        components=[
            {
                "physical_page_id": PAGE,
                "physical_act_id": None,
                "local_acts": [
                    _local(ACT_A, "pg_" + "1" * 16, SOURCE_A, "a"),
                    _local(ACT_B, "pg_" + "2" * 16, SOURCE_B, "b"),
                ],
                "evidence": ["geometry:pair-a-b"],
                "finding": None,
            }
        ],
    )
    append_correspondence_proposal(
        register_path=str(path), proposal=proposal, discovery_register_digest=predecessor
    )
    partition = build_physical_act_partition(
        register=path.read_bytes(),
        register_digest=register_digest(path.read_bytes()),
        proposal_seal_ref={
            "relative_path": "designator/proposal.json",
            "sha256": digest_bytes(b"seal"),
        },
        local_acts=[
            _local(ACT_A, "pg_" + "1" * 16, SOURCE_A, "a"),
            _local(ACT_B, "pg_" + "2" * 16, SOURCE_B, "b"),
        ],
        capture_alignments=[
            {
                "page_id": PG1,
                "source_sha256": SOURCE_A,
                "physical_page_id": PAGE,
                "alignment_ref": "align:a-b",
            },
            {
                "page_id": "pg_" + "2" * 16,
                "source_sha256": SOURCE_B,
                "physical_page_id": PAGE,
                "alignment_ref": "align:a-b",
            },
        ],
        source_ledger={SOURCE_A, SOURCE_B},
    )
    validate_physical_act_partition(partition)
    assert partition["logical_expected_count"] == 1
    assert {row["act_id"] for row in partition["local_to_logical"]} == {ACT_A, ACT_B}
    assert partition["findings"] == []


def test_clustered_unresolved_correspondence_is_a_named_finding_not_a_singleton(tmp_path):
    path = _register(tmp_path)
    partition = build_physical_act_partition(
        register=path.read_bytes(),
        register_digest=register_digest(path.read_bytes()),
        proposal_seal_ref={
            "relative_path": "designator/proposal.json",
            "sha256": digest_bytes(b"seal"),
        },
        local_acts=[_local(ACT_A, "pg_" + "1" * 16, SOURCE_A, "a")],
        capture_alignments=[
            {
                "page_id": PG1,
                "source_sha256": SOURCE_A,
                "physical_page_id": PAGE,
                "alignment_ref": "align:a-b",
            }
        ],
        source_ledger={SOURCE_A, SOURCE_B},
    )
    assert partition["logical_acts"] == []
    assert partition["findings"] == [{"code": "unresolved-physical-act", "act_id": ACT_A}]


def test_image_local_singleton_remains_total_when_no_physical_page_alignment_exists(tmp_path):
    """An ordinary act, on a capture no membership record clusters, stays local.

    The capture is deliberately not `SOURCE_A`: a capture the register *does*
    cluster has an alignment or a finding, never a singleton (see
    `test_a_clustered_capture_without_an_alignment_row_is_held_not_made_a_singleton`).
    """
    path = _register(tmp_path)
    unclustered = "e" * 64
    partition = build_physical_act_partition(
        register=path.read_bytes(),
        register_digest=register_digest(path.read_bytes()),
        proposal_seal_ref={
            "relative_path": "designator/proposal.json",
            "sha256": digest_bytes(b"seal"),
        },
        local_acts=[_local(ACT_A, "pg_" + "1" * 16, unclustered, "a")],
        capture_alignments=[],
        source_ledger={unclustered},
    )
    assert partition["logical_acts"][0]["logical_act_id"] == ACT_A
    assert partition["logical_acts"][0]["identity_scope"] == "image-local-singleton"


def test_image_local_act_absent_from_the_source_ledger_is_held_by_name(tmp_path):
    """A local act cannot establish lineage to source bytes the run did not seal."""
    path = _register(tmp_path)
    unclustered = "e" * 64
    partition = build_physical_act_partition(
        register=path.read_bytes(),
        register_digest=register_digest(path.read_bytes()),
        proposal_seal_ref={
            "relative_path": "designator/proposal.json",
            "sha256": digest_bytes(b"seal"),
        },
        local_acts=[_local(ACT_A, "pg_" + "1" * 16, unclustered, "a")],
        capture_alignments=[],
        source_ledger={SOURCE_A, SOURCE_B},
    )
    assert partition["logical_acts"] == []
    assert partition["findings"] == [{"code": "local-source-absent", "act_id": ACT_A}]


def test_textual_evidence_and_preference_are_refused_at_correspondence_boundary():
    with pytest.raises(SchemaRefusal, match="preference"):
        build_physical_act_partition(
            register=empty_register(),
            register_digest=register_digest(empty_register()),
            proposal_seal_ref={"relative_path": "x", "sha256": "0" * 64},
            local_acts=[{**_local(ACT_A, "pg_" + "1" * 16, SOURCE_A, "a"), "preferred": True}],
            capture_alignments=[],
            source_ledger=set(),
        )
    with pytest.raises(SchemaRefusal, match="textual evidence cannot match"):
        build_correspondence_proposal(
            register=empty_register(),
            register_digest=register_digest(empty_register()),
            discovery_run_id="d",
            components=[
                {
                    "physical_page_id": PAGE,
                    "physical_act_id": None,
                    "local_acts": [_local(ACT_A, "pg_" + "1" * 16, SOURCE_A, "a")],
                    "evidence": ["geometry:only"],
                    "finding": None,
                    "text": "forbidden",
                }
            ],
        )
    with pytest.raises(SchemaRefusal, match="textual evidence cannot match"):
        build_correspondence_proposal(
            register=empty_register(),
            register_digest=register_digest(empty_register()),
            discovery_run_id="d",
            components=[
                {
                    "physical_page_id": PAGE,
                    "physical_act_id": None,
                    "local_acts": [
                        {
                            **_local(ACT_A, "pg_" + "1" * 16, SOURCE_A, "a"),
                            "ocr": "forbidden",
                        }
                    ],
                    "evidence": ["geometry:only"],
                    "finding": None,
                }
            ],
        )


def test_partition_input_collections_refuse_their_own_malformed_shape():
    """Caller shape failures are contract refusals, not incidental TypeErrors."""
    with pytest.raises(SchemaRefusal, match="capture alignments must be a list"):
        build_physical_act_partition(
            register=empty_register(),
            register_digest=register_digest(empty_register()),
            proposal_seal_ref={"relative_path": "x", "sha256": "0" * 64},
            local_acts=[_local(ACT_A, "pg_" + "1" * 16, SOURCE_A, "a")],
            capture_alignments=None,
            source_ledger={SOURCE_A},
        )
    with pytest.raises(SchemaRefusal, match="source ledger must be a set"):
        build_physical_act_partition(
            register=empty_register(),
            register_digest=register_digest(empty_register()),
            proposal_seal_ref={"relative_path": "x", "sha256": "0" * 64},
            local_acts=[_local(ACT_A, "pg_" + "1" * 16, SOURCE_A, "a")],
            capture_alignments=[],
            source_ledger=[SOURCE_A],
        )
    with pytest.raises(SchemaRefusal, match="escapes the run tree"):
        build_physical_act_partition(
            register=empty_register(),
            register_digest=register_digest(empty_register()),
            proposal_seal_ref={"relative_path": "../proposal.json", "sha256": "0" * 64},
            local_acts=[_local(ACT_A, "pg_" + "1" * 16, SOURCE_A, "a")],
            capture_alignments=[],
            source_ledger={SOURCE_A},
        )


def test_prefix_only_strings_are_not_accepted_as_derived_identities():
    malformed = _local("act_not-a-derived-id", "pg_not-a-derived-id", SOURCE_A, "a")
    with pytest.raises(SchemaRefusal, match="identities are malformed"):
        build_physical_act_partition(
            register=empty_register(),
            register_digest=register_digest(empty_register()),
            proposal_seal_ref={"relative_path": "x", "sha256": "0" * 64},
            local_acts=[malformed],
            capture_alignments=[],
            source_ledger={SOURCE_A},
        )
    payload = {
        "schema": PARTITION_SCHEMA,
        "register_digest": "0" * 64,
        "proposal_seal_ref": {"relative_path": "x", "sha256": "0" * 64},
        "local_expected_count": 1,
        "logical_expected_count": 0,
        "logical_acts": [],
        "local_to_logical": [],
        "findings": [{"code": "unresolved-physical-act", "act_id": "act_not-derived"}],
    }
    payload["self_hash"] = self_hash(payload)
    with pytest.raises(SchemaRefusal, match="finding is not closed"):
        validate_physical_act_partition(payload)


@pytest.mark.parametrize(
    ("validator", "schema", "subject"),
    (
        (validate_physical_act_partition, PARTITION_SCHEMA, "physical-act partition"),
        (validate_correspondence_proposal, PROPOSAL_SCHEMA, "correspondence proposal"),
    ),
)
def test_unhashable_artifact_values_are_refused_by_cause(validator, schema, subject):
    payload = {"schema": schema, "unsupported_number": 1.5, "self_hash": "0" * 64}
    with pytest.raises(SchemaRefusal, match=rf"{subject}: self hash.*float at"):
        validator(payload)


def test_a_deeply_nested_local_act_becomes_a_refusal_not_a_recursion_crash():
    """`_refuse_preference` walks `local_acts`/`components` before `_act` proves
    their shape (module docstring), so an unvalidated caller can nest either
    past Python's recursion limit. That must become a `SchemaRefusal`, never an
    uncaught `RecursionError` that would crash the whole stage process."""
    nested: object = "leaf"
    for _ in range(5000):
        nested = {"acts": nested}
    # The composed register's preference screen walks iteratively (14A), so a
    # deep nest is either named by the recursion guard (a recursive walk) or
    # walked whole and refused at the closed-shape check -- never an uncaught
    # RecursionError. Both named refusals prove the boundary.
    with pytest.raises(SchemaRefusal, match="nests too deeply|closed lineage shape"):
        build_physical_act_partition(
            register=empty_register(),
            register_digest=register_digest(empty_register()),
            proposal_seal_ref={"relative_path": "x", "sha256": "0" * 64},
            local_acts=[nested],
            capture_alignments=[],
            source_ledger=set(),
        )
    nested = "leaf"
    for _ in range(5000):
        nested = {"components": nested}
    with pytest.raises(SchemaRefusal, match="nests too deeply|lacks page or local acts|component"):
        build_correspondence_proposal(
            register=empty_register(),
            register_digest=register_digest(empty_register()),
            discovery_run_id="d",
            components=[nested],
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"act_key": "act:e\u0301"}, "printable NFC"),
        ({"page_ordinal": True}, "page ordinal is negative, boolean"),
        ({"page_ordinal": -1}, "page ordinal is negative, boolean"),
    ],
)
def test_partition_constructor_refuses_unstable_keys_and_invalid_boundary_ordinals(
    mutation, message
):
    local = {**_local(ACT_A, "pg_" + "1" * 16, SOURCE_A, "a"), **mutation}
    with pytest.raises(SchemaRefusal, match=message):
        build_physical_act_partition(
            register=empty_register(),
            register_digest=register_digest(empty_register()),
            proposal_seal_ref={"relative_path": "x", "sha256": "0" * 64},
            local_acts=[local],
            capture_alignments=[],
            source_ledger={SOURCE_A},
        )


def test_two_local_acts_cannot_share_one_export_key():
    with pytest.raises(SchemaRefusal, match="act key occurs more than once.*partition is refused"):
        build_physical_act_partition(
            register=empty_register(),
            register_digest=register_digest(empty_register()),
            proposal_seal_ref={"relative_path": "x", "sha256": "0" * 64},
            local_acts=[
                _local(ACT_A, "pg_" + "1" * 16, SOURCE_A, "same-key"),
                _local(ACT_B, "pg_" + "2" * 16, SOURCE_B, "same-key"),
            ],
            capture_alignments=[],
            source_ledger={SOURCE_A, SOURCE_B},
        )


# --- Sonnet audit seat: driving the identity-totality edges ---------------------


def test_an_alignment_naming_a_capture_outside_the_cluster_is_a_named_finding(tmp_path):
    """The register declares SOURCE_A and SOURCE_B for PAGE; a caller alignment

    naming a different, unregistered source for that same physical page must not
    be trusted into the group -- it is exactly "a correspondence naming a capture
    outside the cluster" that the totality claim in the consult (§2.1 item 5)
    forbids resolving silently.
    """
    path = _register(tmp_path)
    outsider = "c" * 64
    partition = build_physical_act_partition(
        register=path.read_bytes(),
        register_digest=register_digest(path.read_bytes()),
        proposal_seal_ref={
            "relative_path": "designator/proposal.json",
            "sha256": digest_bytes(b"seal"),
        },
        local_acts=[_local(ACT_A, "pg_" + "1" * 16, outsider, "a")],
        capture_alignments=[
            {
                "page_id": PG1,
                "source_sha256": outsider,
                "physical_page_id": PAGE,
                "alignment_ref": "align:outsider",
            }
        ],
        source_ledger={outsider},
    )
    assert partition["logical_acts"] == []
    assert partition["findings"] == [{"code": "capture-page-alignment-unresolved", "act_id": ACT_A}]


def test_an_outside_capture_cannot_append_correspondence_into_a_cluster(tmp_path):
    """Geometry cannot make an unregistered capture a member of a physical page."""
    path = _register(tmp_path)
    outsider = "c" * 64
    register_bytes = path.read_bytes()
    proposal = build_correspondence_proposal(
        register=register_bytes,
        register_digest=register_digest(register_bytes),
        discovery_run_id="discovery",
        components=[
            {
                "physical_page_id": PAGE,
                "physical_act_id": None,
                "local_acts": [_local(ACT_A, "pg_" + "1" * 16, outsider, "a")],
                "evidence": ["geometry:outsider"],
                "finding": None,
            }
        ],
    )
    assert proposal["accepted_records"] == []
    assert proposal["findings"] == [{"code": "capture-page-alignment-unresolved", "act_id": ACT_A}]
    before = path.read_bytes()
    with pytest.raises(SchemaRefusal, match="contains no accepted append records"):
        append_correspondence_proposal(
            register_path=str(path),
            proposal=proposal,
            discovery_register_digest=register_digest(before),
        )
    assert path.read_bytes() == before


def test_a_local_act_source_mismatched_with_its_page_alignment_is_refused(tmp_path):
    """A local act's own declared source and its page's capture alignment must

    agree. Trusting either one alone over the other would let a single
    malformed input silently attribute a physical page's presentation to the
    wrong capture.
    """
    path = _register(tmp_path)
    with pytest.raises(SchemaRefusal, match="does not match its"):
        build_physical_act_partition(
            register=path.read_bytes(),
            register_digest=register_digest(path.read_bytes()),
            proposal_seal_ref={
                "relative_path": "designator/proposal.json",
                "sha256": digest_bytes(b"seal"),
            },
            local_acts=[_local(ACT_A, "pg_" + "1" * 16, SOURCE_A, "a")],
            capture_alignments=[
                {
                    "page_id": PG1,
                    "source_sha256": SOURCE_B,
                    "physical_page_id": PAGE,
                    "alignment_ref": "align:mismatch",
                }
            ],
            source_ledger={SOURCE_A, SOURCE_B},
        )


def test_a_capture_in_two_proposed_correspondences_is_ambiguous_not_appended(tmp_path):
    """One local act named by two different components of the same discovery

    run is never resolved by which component the resolver listed first
    (consult §2.2 item 4, §7 shape 2/13): both components holding that act are
    withheld as an ambiguous finding, and neither mints or appends.
    """
    other_page = physical_page_id("fixture", "book", "13r")
    path = tmp_path / "register.json"
    append_records(
        path,
        [
            {
                "kind": "physical-page",
                "corpus_id": "fixture",
                "volume_id": "book",
                "designation": "12r",
                "physical_page_id": PAGE,
                "appending_run": "triage",
            },
            {
                "kind": "physical-page",
                "corpus_id": "fixture",
                "volume_id": "book",
                "designation": "13r",
                "physical_page_id": other_page,
                "appending_run": "triage",
            },
        ],
        expected_digest=register_digest(empty_register()),
    )
    register_bytes = path.read_bytes()
    proposal = build_correspondence_proposal(
        register=register_bytes,
        register_digest=register_digest(register_bytes),
        discovery_run_id="discovery",
        components=[
            {
                "physical_page_id": PAGE,
                "physical_act_id": None,
                "local_acts": [_local(ACT_A, "pg_" + "1" * 16, SOURCE_A, "a")],
                "evidence": ["geometry:pair-x"],
                "finding": None,
            },
            {
                "physical_page_id": other_page,
                "physical_act_id": None,
                "local_acts": [_local(ACT_A, "pg_" + "1" * 16, SOURCE_A, "a")],
                "evidence": ["geometry:pair-y"],
                "finding": None,
            },
        ],
    )
    assert proposal["accepted_records"] == []
    assert proposal["findings"] == [{"code": "ambiguous-physical-act", "act_id": ACT_A}]


def test_retracted_correspondence_reaches_the_partition_as_a_named_finding(tmp_path):
    path = _register(tmp_path)
    predecessor = register_digest(path.read_bytes())
    proposal = build_correspondence_proposal(
        register=path.read_bytes(),
        register_digest=predecessor,
        discovery_run_id="discovery",
        components=[
            {
                "physical_page_id": PAGE,
                "physical_act_id": None,
                "local_acts": [_local(ACT_A, "pg_" + "1" * 16, SOURCE_A, "a")],
                "evidence": ["geometry:solo"],
                "finding": None,
            }
        ],
    )
    after_append = append_correspondence_proposal(
        register_path=str(path), proposal=proposal, discovery_register_digest=predecessor
    )
    minted = next(
        row["physical_act_id"]
        for row in proposal["accepted_records"]
        if row["kind"] == "correspondence"
    )
    append_records(
        path,
        [
            {
                "kind": "retraction",
                "retracts": f"{ACT_A}->{minted}",
                "reason": "declared against the wrong capture",
                "appending_run": "triage-2",
            }
        ],
        expected_digest=after_append,
    )
    register_bytes = path.read_bytes()
    partition = build_physical_act_partition(
        register=register_bytes,
        register_digest=register_digest(register_bytes),
        proposal_seal_ref={
            "relative_path": "designator/proposal.json",
            "sha256": digest_bytes(b"seal"),
        },
        local_acts=[_local(ACT_A, "pg_" + "1" * 16, SOURCE_A, "a")],
        capture_alignments=[
            {
                "page_id": PG1,
                "source_sha256": SOURCE_A,
                "physical_page_id": PAGE,
                "alignment_ref": "align:solo",
            }
        ],
        source_ledger={SOURCE_A, SOURCE_B},
    )
    assert partition["logical_acts"] == []
    assert partition["findings"] == [{"code": "retracted-physical-act", "act_id": ACT_A}]


def test_ambiguous_correspondence_reaches_the_partition_as_a_named_finding(tmp_path):
    """A register can carry operator-declared links from one act to two acts.

    The partition builder must not guess between the conflicting declarations:
    it names the ambiguity, exactly as a resolver's own colliding components do.
    """
    other_page = physical_page_id("fixture", "book", "13r")
    physical_p = physical_act_id(PAGE, "entry-a")
    physical_q = physical_act_id(other_page, "entry-a")
    path = tmp_path / "register.json"
    before = register_digest(empty_register())
    append_records(
        path,
        [
            {
                "kind": "physical-page",
                "corpus_id": "fixture",
                "volume_id": "book",
                "designation": "12r",
                "physical_page_id": PAGE,
                "appending_run": "triage",
            },
            {
                "kind": "physical-page",
                "corpus_id": "fixture",
                "volume_id": "book",
                "designation": "13r",
                "physical_page_id": other_page,
                "appending_run": "triage",
            },
            {
                "kind": "membership",
                "physical_page_id": PAGE,
                "members": [SOURCE_A],
                "predecessor": None,
                "appending_run": "triage",
            },
            {
                "kind": "membership",
                "physical_page_id": other_page,
                "members": [SOURCE_A],
                "predecessor": None,
                "appending_run": "triage",
            },
            {
                "kind": "physical-act",
                "physical_page_id": PAGE,
                "mint_designation": "entry-a",
                "physical_act_id": physical_p,
                "evidence": ["fixture"],
                "appending_run": "run-1",
            },
            {
                "kind": "physical-act",
                "physical_page_id": other_page,
                "mint_designation": "entry-a",
                "physical_act_id": physical_q,
                "evidence": ["fixture"],
                "appending_run": "run-2",
            },
            {
                "kind": "correspondence",
                "page_id": PG1,
                "act_id": ACT_A,
                "act_class": "proposal",
                "act_bounds": {"x": 0, "y": 0, "w": 10, "h": 10},
                "physical_page_id": PAGE,
                "physical_act_id": physical_p,
                "evidence": ["fixture"],
                "appending_run": "run-1",
            },
            {
                "kind": "correspondence",
                "page_id": PG1,
                "act_id": ACT_A,
                "act_class": "proposal",
                "act_bounds": {"x": 0, "y": 0, "w": 10, "h": 10},
                "physical_page_id": other_page,
                "physical_act_id": physical_q,
                "evidence": ["fixture"],
                "appending_run": "run-2",
            },
        ],
        expected_digest=before,
    )
    register_bytes = path.read_bytes()
    assert resolve_proposal(register_bytes, ACT_A)["code"] == "ambiguous-physical-act"
    partition = build_physical_act_partition(
        register=register_bytes,
        register_digest=register_digest(register_bytes),
        proposal_seal_ref={
            "relative_path": "designator/proposal.json",
            "sha256": digest_bytes(b"seal"),
        },
        local_acts=[_local(ACT_A, "pg_" + "1" * 16, SOURCE_A, "a")],
        capture_alignments=[
            {
                "page_id": PG1,
                "source_sha256": SOURCE_A,
                "physical_page_id": PAGE,
                "alignment_ref": "align:solo",
            }
        ],
        source_ledger={SOURCE_A},
    )
    assert partition["logical_acts"] == []
    assert partition["findings"] == [{"code": "ambiguous-physical-act", "act_id": ACT_A}]


def test_append_racing_a_retraction_is_refused_and_a_retried_retraction_is_precise(tmp_path):
    """A retraction that read a since-superseded register digest must be refused

    exactly like any other stale writer (GOVERNANCE 4/7): the correction is
    never silently dropped, and it is never silently applied against a register
    it did not actually observe. Retried against the live digest, it retracts
    only the correspondence it named -- the racing append survives untouched.
    """
    path = _register(tmp_path)
    predecessor = register_digest(path.read_bytes())
    proposal_a = build_correspondence_proposal(
        register=path.read_bytes(),
        register_digest=predecessor,
        discovery_run_id="discovery-a",
        components=[
            {
                "physical_page_id": PAGE,
                "physical_act_id": None,
                "local_acts": [_local(ACT_A, "pg_" + "1" * 16, SOURCE_A, "a")],
                "evidence": ["geometry:solo-a"],
                "finding": None,
            }
        ],
    )
    after_first = append_correspondence_proposal(
        register_path=str(path), proposal=proposal_a, discovery_register_digest=predecessor
    )
    minted_a = next(
        row["physical_act_id"]
        for row in proposal_a["accepted_records"]
        if row["kind"] == "correspondence"
    )
    correspondence_identity = f"{ACT_A}->{minted_a}"

    proposal_b = build_correspondence_proposal(
        register=path.read_bytes(),
        register_digest=after_first,
        discovery_run_id="discovery-b",
        components=[
            {
                "physical_page_id": PAGE,
                "physical_act_id": None,
                "local_acts": [_local(ACT_B, "pg_" + "2" * 16, SOURCE_B, "b")],
                "evidence": ["geometry:solo-b"],
                "finding": None,
            }
        ],
    )
    append_correspondence_proposal(
        register_path=str(path), proposal=proposal_b, discovery_register_digest=after_first
    )

    with pytest.raises(IncompatibleReuse, match="changed after"):
        append_records(
            path,
            [
                {
                    "kind": "retraction",
                    "retracts": correspondence_identity,
                    "reason": "declared against the wrong capture",
                    "appending_run": "triage-2",
                }
            ],
            expected_digest=after_first,
        )

    current = register_digest(path.read_bytes())
    append_records(
        path,
        [
            {
                "kind": "retraction",
                "retracts": correspondence_identity,
                "reason": "declared against the wrong capture",
                "appending_run": "triage-2",
            }
        ],
        expected_digest=current,
    )
    final_bytes = path.read_bytes()
    assert resolve_proposal(final_bytes, ACT_A)["code"] == "retracted-physical-act"
    assert resolve_proposal(final_bytes, ACT_B)["outcome"] == "resolved"


def test_reversed_local_act_and_alignment_submission_reproduces_the_same_partition_bytes(
    tmp_path,
):
    path = _register(tmp_path)
    predecessor = register_digest(path.read_bytes())
    proposal = build_correspondence_proposal(
        register=path.read_bytes(),
        register_digest=predecessor,
        discovery_run_id="discovery",
        components=[
            {
                "physical_page_id": PAGE,
                "physical_act_id": None,
                "local_acts": [
                    _local(ACT_A, "pg_" + "1" * 16, SOURCE_A, "a"),
                    _local(ACT_B, "pg_" + "2" * 16, SOURCE_B, "b"),
                ],
                "evidence": ["geometry:pair-a-b"],
                "finding": None,
            }
        ],
    )
    append_correspondence_proposal(
        register_path=str(path), proposal=proposal, discovery_register_digest=predecessor
    )
    register_bytes = path.read_bytes()
    digest = register_digest(register_bytes)
    seal_ref = {"relative_path": "designator/proposal.json", "sha256": digest_bytes(b"seal")}
    local_acts = [
        _local(ACT_A, "pg_" + "1" * 16, SOURCE_A, "a"),
        _local(ACT_B, "pg_" + "2" * 16, SOURCE_B, "b"),
    ]
    alignments = [
        {
            "page_id": PG1,
            "source_sha256": SOURCE_A,
            "physical_page_id": PAGE,
            "alignment_ref": "align:a-b",
        },
        {
            "page_id": "pg_" + "2" * 16,
            "source_sha256": SOURCE_B,
            "physical_page_id": PAGE,
            "alignment_ref": "align:a-b",
        },
    ]
    forward = build_physical_act_partition(
        register=register_bytes,
        register_digest=digest,
        proposal_seal_ref=seal_ref,
        local_acts=local_acts,
        capture_alignments=alignments,
        source_ledger={SOURCE_A, SOURCE_B},
    )
    reversed_partition = build_physical_act_partition(
        register=register_bytes,
        register_digest=digest,
        proposal_seal_ref=seal_ref,
        local_acts=list(reversed(local_acts)),
        capture_alignments=list(reversed(alignments)),
        source_ledger={SOURCE_A, SOURCE_B},
    )
    assert forward == reversed_partition


def test_a_missing_required_capture_is_named_and_blocks_that_local_act(tmp_path):
    path = _register(tmp_path)
    partition = build_physical_act_partition(
        register=path.read_bytes(),
        register_digest=register_digest(path.read_bytes()),
        proposal_seal_ref={
            "relative_path": "designator/proposal.json",
            "sha256": digest_bytes(b"seal"),
        },
        local_acts=[_local(ACT_A, "pg_" + "1" * 16, SOURCE_A, "a")],
        capture_alignments=[
            {
                "page_id": PG1,
                "source_sha256": SOURCE_A,
                "physical_page_id": PAGE,
                "alignment_ref": "align:a-b",
            }
        ],
        # SOURCE_B is a registered member of PAGE (see `_register`) but is not
        # in this run's source ledger: the required capture cannot be
        # materialized, and the reader must never be reached for this act.
        source_ledger={SOURCE_A},
    )
    assert partition["logical_acts"] == []
    assert partition["findings"] == [{"code": "cluster-member-absent", "act_id": ACT_A}]


def test_a_partition_left_partial_mid_append_is_refused_not_silently_accepted():
    """A payload that drops a local act from both `local_to_logical` and

    `findings` looks superficially closed but is not total. The conservation
    check must catch this on its own arithmetic, not merely rely on every
    honest builder never producing it.
    """
    payload = {
        "schema": PARTITION_SCHEMA,
        "register_digest": "0" * 64,
        "proposal_seal_ref": {"relative_path": "x", "sha256": "0" * 64},
        "local_expected_count": 2,
        "logical_expected_count": 1,
        "logical_acts": [
            {
                "logical_act_id": ACT_A,
                "identity_scope": "image-local-singleton",
                "physical_act_id": None,
                "physical_page_components": [],
                "member_local_acts": [_local(ACT_A, "pg_" + "1" * 16, SOURCE_A, "a")],
                "capture_presentations": [],
            }
        ],
        "local_to_logical": [{"act_id": ACT_A, "logical_act_id": ACT_A}],
        "findings": [],
    }
    payload["self_hash"] = self_hash(payload)
    with pytest.raises(SchemaRefusal, match="not total"):
        validate_physical_act_partition(payload)


def test_partition_mapping_must_equal_the_members_its_logical_groups_publish():
    """A count is not conservation when the two tables count different acts."""
    payload = {
        "schema": PARTITION_SCHEMA,
        "register_digest": "0" * 64,
        "proposal_seal_ref": {"relative_path": "x", "sha256": "0" * 64},
        "local_expected_count": 1,
        "logical_expected_count": 1,
        "logical_acts": [
            {
                "logical_act_id": ACT_A,
                "identity_scope": "image-local-singleton",
                "physical_act_id": None,
                "physical_page_components": [],
                "member_local_acts": [_local(ACT_A, "pg_" + "1" * 16, SOURCE_A, "a")],
                "capture_presentations": [],
            }
        ],
        "local_to_logical": [{"act_id": ACT_B, "logical_act_id": ACT_A}],
        "findings": [],
    }
    payload["self_hash"] = self_hash(payload)
    with pytest.raises(SchemaRefusal, match="does not equal"):
        validate_physical_act_partition(payload)


def test_partition_validator_refuses_a_dropped_required_capture_presentation(tmp_path):
    """A valid hash cannot turn a partial physical-act presentation into totality."""
    path = _register(tmp_path)
    _mint(
        path,
        PAGE,
        [
            _local(ACT_A, "pg_" + "1" * 16, SOURCE_A, "a"),
            _local(ACT_B, "pg_" + "2" * 16, SOURCE_B, "b"),
        ],
    )
    partition = build_physical_act_partition(
        register=path.read_bytes(),
        register_digest=register_digest(path.read_bytes()),
        proposal_seal_ref=SEAL,
        local_acts=[
            _local(ACT_A, "pg_" + "1" * 16, SOURCE_A, "a"),
            _local(ACT_B, "pg_" + "2" * 16, SOURCE_B, "b"),
        ],
        capture_alignments=[
            _align("pg_" + "1" * 16, SOURCE_A, PAGE, "align:a-b"),
            _align("pg_" + "2" * 16, SOURCE_B, PAGE, "align:a-b"),
        ],
        source_ledger={SOURCE_A, SOURCE_B},
    )
    partition["logical_acts"][0]["capture_presentations"].pop()
    partition["self_hash"] = self_hash(partition)
    with pytest.raises(SchemaRefusal, match="presentation set"):
        validate_physical_act_partition(partition)


def test_a_mixed_type_capture_list_is_refused_by_name_not_a_sort_typeerror(tmp_path):
    """`sorted(set(...))` cannot order a str against an int: a resealed payload

    with one non-string entry must become a named `SchemaRefusal`, never a raw
    `TypeError` out of the validator.
    """
    path = _register(tmp_path)
    _mint(
        path,
        PAGE,
        [
            _local(ACT_A, "pg_" + "1" * 16, SOURCE_A, "a"),
            _local(ACT_B, "pg_" + "2" * 16, SOURCE_B, "b"),
        ],
    )
    partition = build_physical_act_partition(
        register=path.read_bytes(),
        register_digest=register_digest(path.read_bytes()),
        proposal_seal_ref=SEAL,
        local_acts=[
            _local(ACT_A, "pg_" + "1" * 16, SOURCE_A, "a"),
            _local(ACT_B, "pg_" + "2" * 16, SOURCE_B, "b"),
        ],
        capture_alignments=[
            _align("pg_" + "1" * 16, SOURCE_A, PAGE, "align:a-b"),
            _align("pg_" + "2" * 16, SOURCE_B, PAGE, "align:a-b"),
        ],
        source_ledger={SOURCE_A, SOURCE_B},
    )
    component = partition["logical_acts"][0]["physical_page_components"][0]
    component["required_capture_sha256s"] = [SOURCE_A, 5]
    partition["self_hash"] = self_hash(partition)
    with pytest.raises(SchemaRefusal, match="malformed identity"):
        validate_physical_act_partition(partition)


def test_a_mixed_type_page_id_list_is_refused_by_name_not_a_sort_typeerror(tmp_path):
    """The same canonical-order check on `page_ids` must refuse by name too."""
    path = _register(tmp_path)
    _mint(
        path,
        PAGE,
        [
            _local(ACT_A, "pg_" + "1" * 16, SOURCE_A, "a"),
            _local(ACT_B, "pg_" + "2" * 16, SOURCE_B, "b"),
        ],
    )
    partition = build_physical_act_partition(
        register=path.read_bytes(),
        register_digest=register_digest(path.read_bytes()),
        proposal_seal_ref=SEAL,
        local_acts=[
            _local(ACT_A, "pg_" + "1" * 16, SOURCE_A, "a"),
            _local(ACT_B, "pg_" + "2" * 16, SOURCE_B, "b"),
        ],
        capture_alignments=[
            _align("pg_" + "1" * 16, SOURCE_A, PAGE, "align:a-b"),
            _align("pg_" + "2" * 16, SOURCE_B, PAGE, "align:a-b"),
        ],
        source_ledger={SOURCE_A, SOURCE_B},
    )
    presentation = partition["logical_acts"][0]["capture_presentations"][0]
    presentation["page_ids"] = [presentation["page_ids"][0], 5]
    partition["self_hash"] = self_hash(partition)
    with pytest.raises(SchemaRefusal, match="capture presentation is malformed"):
        validate_physical_act_partition(partition)


def _partition(register_bytes, local_acts, alignments, ledger, digest=None):
    return build_physical_act_partition(
        register=register_bytes,
        register_digest=digest or register_digest(register_bytes),
        proposal_seal_ref=SEAL,
        local_acts=local_acts,
        capture_alignments=alignments,
        source_ledger=ledger,
    )


def _align(page_id, source, physical_page, ref):
    return {
        "page_id": page_id,
        "source_sha256": source,
        "physical_page_id": physical_page,
        "alignment_ref": ref,
    }


def _mint(path, page, acts, run="discovery", existing=None):
    predecessor = register_digest(path.read_bytes())
    proposal = build_correspondence_proposal(
        register=path.read_bytes(),
        register_digest=predecessor,
        discovery_run_id=run,
        components=[
            {
                "physical_page_id": page,
                "physical_act_id": existing,
                "local_acts": acts,
                "evidence": [f"geometry:{run}"],
                "finding": None,
            }
        ],
    )
    append_correspondence_proposal(
        register_path=str(path), proposal=proposal, discovery_register_digest=predecessor
    )
    return next(
        row["physical_act_id"]
        for row in proposal["accepted_records"]
        if row["kind"] == "correspondence"
    )


def test_a_register_digest_that_does_not_describe_the_register_bytes_is_refused(tmp_path):
    """The sealed `register_digest` is this partition's provenance, so it must be

    the digest of the bytes that produced the grouping. A caller that reads the
    digest before a correspondence append and the register bytes after it would
    otherwise publish a partition grouped by one register and attributed to
    another -- a cluster re-registered mid-lifecycle, with nothing downstream
    able to see that the two disagree.
    """
    path = _register(tmp_path)
    stale = register_digest(path.read_bytes())
    _mint(path, PAGE, [_local(ACT_A, PG1, SOURCE_A, "a")])
    with pytest.raises(IncompatibleReuse, match="register moved"):
        _partition(
            path.read_bytes(),
            [_local(ACT_A, PG1, SOURCE_A, "a")],
            [_align(PG1, SOURCE_A, PAGE, "align:a")],
            {SOURCE_A, SOURCE_B},
            digest=stale,
        )


def _two_page_member_register(tmp_path, w):
    """SOURCE_B shows both pages; the fixture's physical act belongs only to 12r."""
    act_b = act_id(PG2, "proposal", {"x": 0, "y": 0, "w": w, "h": 10})
    path = tmp_path / "register.json"
    append_records(
        path,
        [
            {
                "kind": "physical-page",
                "corpus_id": "fixture",
                "volume_id": "book",
                "designation": "12r",
                "physical_page_id": PAGE,
                "appending_run": "triage",
            },
            {
                "kind": "physical-page",
                "corpus_id": "fixture",
                "volume_id": "book",
                "designation": "13r",
                "physical_page_id": PAGE_13R,
                "appending_run": "triage",
            },
            {
                "kind": "membership",
                "physical_page_id": PAGE,
                "members": sorted([SOURCE_A, SOURCE_B]),
                "predecessor": None,
                "appending_run": "triage",
            },
            {
                "kind": "membership",
                "physical_page_id": PAGE_13R,
                "members": [SOURCE_B],
                "predecessor": None,
                "appending_run": "triage",
            },
        ],
        expected_digest=register_digest(empty_register()),
    )
    _mint(
        path,
        PAGE,
        [
            _local(ACT_A, PG1, SOURCE_A, "a"),
            _local(act_b, PG2, SOURCE_B, "b", {"x": 0, "y": 0, "w": w, "h": 10}),
        ],
    )
    return path, act_b


def test_a_member_aligned_to_another_physical_page_is_held_in_either_sort_order(tmp_path):
    """A physical act is minted on one physical page. A member capture whose

    alignment names a *different* physical page contradicts the register, and
    which of the two the group is checked against must not depend on which
    member's `act_id` happens to sort first: reading the page off the first
    aligned member made one and the same input either hold loudly or publish a
    physical act spanning two physical pages (consult §7 shape 2). Both orders
    hold, and they hold identically.
    """
    outcomes = {}
    for index, w in enumerate(range(1, 40)):
        path, act_b = _two_page_member_register(tmp_path / f"run{index}", w)
        partition = _partition(
            path.read_bytes(),
            [
                _local(ACT_A, PG1, SOURCE_A, "a"),
                _local(act_b, PG2, SOURCE_B, "b", {"x": 0, "y": 0, "w": w, "h": 10}),
            ],
            [
                _align(PG1, SOURCE_A, PAGE, "align:12r"),
                _align(PG2, SOURCE_B, PAGE_13R, "align:13r"),
            ],
            {SOURCE_A, SOURCE_B},
        )
        assert partition["logical_acts"] == []
        outcomes["A<B" if ACT_A < act_b else "B<A"] = {f["code"] for f in partition["findings"]}
    assert set(outcomes) == {"A<B", "B<A"}, "the fixture must exercise both sort orders"
    assert outcomes["A<B"] == outcomes["B<A"] == {"capture-page-alignment-unresolved"}


def test_every_capture_page_reaching_one_physical_page_stays_in_the_presentation(tmp_path):
    """One capture may reach a physical page through more than one rendered page

    -- a whole opening and the split half of it are two `page_id`s over identical
    source bytes. The presentation row carries `page_ids[]` for exactly that, and
    keeping whichever row was seen first would delete a page from the record that
    a reading has to be traceable back to (GOALS 5).
    """
    path = _register(tmp_path)
    _mint(path, PAGE, [_local(ACT_A, PG1, SOURCE_A, "a")])
    register_bytes = path.read_bytes()
    forward = _partition(
        register_bytes,
        [_local(ACT_A, PG1, SOURCE_A, "a")],
        [
            _align(PG1, SOURCE_A, PAGE, "align:a"),
            _align(PG3, SOURCE_A, PAGE, "align:a"),
            _align(PG2, SOURCE_B, PAGE, "align:b"),
        ],
        {SOURCE_A, SOURCE_B},
    )
    presentation = next(
        row
        for row in forward["logical_acts"][0]["capture_presentations"]
        if row["source_sha256"] == SOURCE_A
    )
    assert presentation["page_ids"] == sorted([PG1, PG3])
    assert presentation["local_act_ids"] == [ACT_A]
    reversed_partition = _partition(
        register_bytes,
        [_local(ACT_A, PG1, SOURCE_A, "a")],
        [
            _align(PG2, SOURCE_B, PAGE, "align:b"),
            _align(PG3, SOURCE_A, PAGE, "align:a"),
            _align(PG1, SOURCE_A, PAGE, "align:a"),
        ],
        {SOURCE_A, SOURCE_B},
    )
    assert forward == reversed_partition


def test_two_alignment_references_for_one_capture_of_one_physical_page_are_refused(tmp_path):
    path = _register(tmp_path)
    _mint(path, PAGE, [_local(ACT_A, PG1, SOURCE_A, "a")])
    with pytest.raises(SchemaRefusal, match="two different alignment references"):
        _partition(
            path.read_bytes(),
            [_local(ACT_A, PG1, SOURCE_A, "a")],
            [
                _align(PG1, SOURCE_A, PAGE, "align:one"),
                _align(PG3, SOURCE_A, PAGE, "align:another"),
            ],
            {SOURCE_A, SOURCE_B},
        )


def test_a_clustered_capture_without_an_alignment_row_is_held_not_made_a_singleton(tmp_path):
    """The register declares this capture a member of a physical page, and the

    run's alignment table says nothing about it. That is a missing alignment,
    not an image-local act: publishing it as a singleton would republish it
    beside the logical act it belongs to, which is the duplicate the whole
    correspondence step exists to prevent (consult §2.1).
    """
    path = _register(tmp_path)
    partition = _partition(
        path.read_bytes(),
        [_local(ACT_A, PG1, SOURCE_A, "a")],
        [],
        {SOURCE_A, SOURCE_B},
    )
    assert partition["logical_acts"] == []
    assert partition["findings"] == [{"code": "capture-page-alignment-unresolved", "act_id": ACT_A}]


def test_a_capture_shared_by_two_physical_pages_requires_explicit_page_alignment(tmp_path):
    """Consult §8.2 test 16. One capture is a declared member of two physical

    pages, so its source digest alone cannot say which one an act on it belongs
    to. Without an alignment row the act is held; with one, the named page is
    the one that resolves, and the other physical page is never reached.
    """
    path, act_b = _two_page_member_register(tmp_path, 10)
    register_bytes = path.read_bytes()
    unaligned = _partition(
        register_bytes,
        [_local(act_b, PG2, SOURCE_B, "b", {"x": 0, "y": 0, "w": 10, "h": 10})],
        [_align(PG1, SOURCE_A, PAGE, "align:12r")],
        {SOURCE_A, SOURCE_B},
    )
    assert unaligned["logical_acts"] == []
    assert unaligned["findings"] == [{"code": "capture-page-alignment-unresolved", "act_id": act_b}]
    aligned = _partition(
        register_bytes,
        [
            _local(ACT_A, PG1, SOURCE_A, "a"),
            _local(act_b, PG2, SOURCE_B, "b", {"x": 0, "y": 0, "w": 10, "h": 10}),
        ],
        [_align(PG1, SOURCE_A, PAGE, "align:12r"), _align(PG2, SOURCE_B, PAGE, "align:12r")],
        {SOURCE_A, SOURCE_B},
    )
    assert aligned["findings"] == []
    assert aligned["logical_expected_count"] == 1
    assert [
        component["physical_page_id"]
        for component in aligned["logical_acts"][0]["physical_page_components"]
    ] == [PAGE]


def test_a_vanished_local_act_cannot_be_covered_by_another_acts_finding():
    """Conservation is a partition, not a sum. A record that maps one act and

    holds that same act reaches the expected count while a second act has
    disappeared entirely -- exactly the arithmetic an interrupted build would
    leave behind, and GOVERNANCE 2 does not allow it to read as complete.
    """
    payload = {
        "schema": PARTITION_SCHEMA,
        "register_digest": "0" * 64,
        "proposal_seal_ref": {"relative_path": "x", "sha256": "0" * 64},
        "local_expected_count": 2,
        "logical_expected_count": 1,
        "logical_acts": [
            {
                "logical_act_id": ACT_A,
                "identity_scope": "image-local-singleton",
                "physical_act_id": None,
                "physical_page_components": [],
                "member_local_acts": [_local(ACT_A, PG1, SOURCE_A, "a")],
                "capture_presentations": [],
            }
        ],
        "local_to_logical": [{"act_id": ACT_A, "logical_act_id": ACT_A}],
        "findings": [{"code": "unresolved-physical-act", "act_id": ACT_A}],
    }
    payload["self_hash"] = self_hash(payload)
    with pytest.raises(SchemaRefusal, match="one or the other"):
        validate_physical_act_partition(payload)


def test_a_logical_act_the_record_does_not_publish_cannot_be_mapped_to():
    payload = {
        "schema": PARTITION_SCHEMA,
        "register_digest": "0" * 64,
        "proposal_seal_ref": {"relative_path": "x", "sha256": "0" * 64},
        "local_expected_count": 1,
        "logical_expected_count": 0,
        "logical_acts": [],
        "local_to_logical": [{"act_id": ACT_A, "logical_act_id": ACT_A}],
        "findings": [],
    }
    payload["self_hash"] = self_hash(payload)
    with pytest.raises(SchemaRefusal, match="does not publish"):
        validate_physical_act_partition(payload)


def _three_member_register(tmp_path):
    """One physical page whose membership chain grew from {A} to {A, B, C}."""
    path = tmp_path / "register.json"
    append_records(
        path,
        [
            {
                "kind": "physical-page",
                "corpus_id": "fixture",
                "volume_id": "book",
                "designation": "12r",
                "physical_page_id": PAGE,
                "appending_run": "triage",
            },
            {
                "kind": "membership",
                "physical_page_id": PAGE,
                "members": [SOURCE_A],
                "predecessor": None,
                "appending_run": "triage",
            },
        ],
        expected_digest=register_digest(empty_register()),
    )
    head = membership_heads(path.read_bytes())[PAGE][0]
    append_records(
        path,
        [
            {
                "kind": "membership",
                "physical_page_id": PAGE,
                "members": sorted([SOURCE_A, SOURCE_B, SOURCE_C]),
                "predecessor": head,
                "appending_run": "triage-2",
            }
        ],
        expected_digest=register_digest(path.read_bytes()),
    )
    return path, head


ACT_C = act_id(PG3, "proposal", {"x": 0, "y": 0, "w": 10, "h": 10})


def test_an_overlapping_component_grows_the_existing_physical_act_in_either_order(tmp_path):
    """Two honest discovery runs whose components share a local act describe one

    physical act, and the register already holds the first run's half of it. A
    resolver that mints from geometry alone cannot see that, so it minted a
    second physical act over the overlap: the shared act became ambiguous while
    the two acts on either side of it resolved perfectly well to *different*
    physical acts, publishing one physical act as two. Reading the register makes
    the second component grow the first act instead -- and which run went first
    stops deciding whether the corpus merges or splits (consult §2.2 items 4-5,
    §8.2 test 18).
    """
    resolved = {}
    for order, first, second in (
        ("ab-then-bc", ["a", "b"], ["b", "c"]),
        ("bc-then-ab", ["b", "c"], ["a", "b"]),
    ):
        path, _head = _three_member_register(tmp_path / order)
        rows = {
            "a": _local(ACT_A, PG1, SOURCE_A, "a"),
            "b": _local(ACT_B, PG2, SOURCE_B, "b"),
            "c": _local(ACT_C, PG3, SOURCE_C, "c"),
        }
        _mint(path, PAGE, [rows[key] for key in first], run=f"{order}-1")
        _mint(path, PAGE, [rows[key] for key in second], run=f"{order}-2")
        register_bytes = path.read_bytes()
        acts = {
            resolve_proposal(register_bytes, row["act_id"])["physical_act_id"]
            for row in rows.values()
        }
        assert len(acts) == 1, f"{order} split one physical act into {len(acts)}"
        partition = _partition(
            register_bytes,
            list(rows.values()),
            [
                _align(PG1, SOURCE_A, PAGE, "align:12r"),
                _align(PG2, SOURCE_B, PAGE, "align:12r"),
                _align(PG3, SOURCE_C, PAGE, "align:12r"),
            ],
            {SOURCE_A, SOURCE_B, SOURCE_C},
        )
        assert partition["findings"] == []
        assert partition["logical_expected_count"] == 1
        assert partition["local_expected_count"] == 3
        resolved[order] = acts.pop()
    # The mint designation binds the *initial* component, so the two orders name
    # the same act differently. What must not differ is that there is one of it.
    assert all(name.startswith("pac_") for name in resolved.values())


def test_a_component_reaching_two_existing_physical_acts_is_held_not_merged(tmp_path):
    """Consult §2.2 item 4. A later component spanning two already-declared

    physical acts is the resolver proposing a merge; the register cannot express
    one, and choosing either act would be a silent re-identification of the
    other's members.
    """
    path, _head = _three_member_register(tmp_path)
    _mint(path, PAGE, [_local(ACT_A, PG1, SOURCE_A, "a")], run="run-1")
    _mint(path, PAGE, [_local(ACT_B, PG2, SOURCE_B, "b")], run="run-2")
    register_bytes = path.read_bytes()
    predecessor = register_digest(register_bytes)
    proposal = build_correspondence_proposal(
        register=register_bytes,
        register_digest=predecessor,
        discovery_run_id="run-3",
        components=[
            {
                "physical_page_id": PAGE,
                "physical_act_id": None,
                "local_acts": [
                    _local(ACT_A, PG1, SOURCE_A, "a"),
                    _local(ACT_B, PG2, SOURCE_B, "b"),
                    _local(ACT_C, PG3, SOURCE_C, "c"),
                ],
                "evidence": ["geometry:merge"],
                "finding": None,
            }
        ],
    )
    assert proposal["accepted_records"] == []
    assert {row["code"] for row in proposal["findings"]} == {"ambiguous-physical-act"}
    assert {row["act_id"] for row in proposal["findings"]} == {ACT_A, ACT_B, ACT_C}


def test_two_components_of_one_run_reaching_one_physical_act_are_held(tmp_path):
    """Two components of one run, over disjoint local acts, that each resolve to

    the same already-declared physical act. That run is merging them by listing
    order rather than producing the exact-one admissible component the consult
    requires, so both are withheld -- neither is appended because it was listed
    first.
    """
    path, _head = _three_member_register(tmp_path)
    second_c = act_id(PG3, "proposal", {"x": 0, "y": 60, "w": 10, "h": 10})
    _mint(path, PAGE, [_local(ACT_A, PG1, SOURCE_A, "a"), _local(ACT_B, PG2, SOURCE_B, "b")])
    register_bytes = path.read_bytes()
    proposal = build_correspondence_proposal(
        register=register_bytes,
        register_digest=register_digest(register_bytes),
        discovery_run_id="run-2",
        components=[
            {
                "physical_page_id": PAGE,
                "physical_act_id": None,
                "local_acts": [
                    _local(ACT_A, PG1, SOURCE_A, "a"),
                    _local(ACT_C, PG3, SOURCE_C, "c"),
                ],
                "evidence": ["geometry:one"],
                "finding": None,
            },
            {
                "physical_page_id": PAGE,
                "physical_act_id": None,
                "local_acts": [
                    _local(ACT_B, PG2, SOURCE_B, "b"),
                    _local(second_c, PG3, SOURCE_C, "c2", {"x": 0, "y": 60, "w": 10, "h": 10}),
                ],
                "evidence": ["geometry:two"],
                "finding": None,
            },
        ],
    )
    assert proposal["accepted_records"] == []
    assert {row["act_id"] for row in proposal["findings"]} == {
        ACT_A,
        ACT_B,
        ACT_C,
        second_c,
    }


def test_a_local_act_corresponding_to_a_physical_act_minted_elsewhere_is_held(tmp_path):
    """A resolved physical act is checked against the component's physical page."""
    path = tmp_path / "register.json"
    append_records(
        path,
        [
            {
                "kind": "physical-page",
                "corpus_id": "fixture",
                "volume_id": "book",
                "designation": "12r",
                "physical_page_id": PAGE,
                "appending_run": "triage",
            },
            {
                "kind": "physical-page",
                "corpus_id": "fixture",
                "volume_id": "book",
                "designation": "13r",
                "physical_page_id": PAGE_13R,
                "appending_run": "triage",
            },
            {
                "kind": "membership",
                "physical_page_id": PAGE,
                "members": [SOURCE_A],
                "predecessor": None,
                "appending_run": "triage",
            },
            {
                "kind": "membership",
                "physical_page_id": PAGE_13R,
                "members": [SOURCE_A],
                "predecessor": None,
                "appending_run": "triage",
            },
        ],
        expected_digest=register_digest(empty_register()),
    )
    _mint(path, PAGE_13R, [_local(ACT_A, PG1, SOURCE_A, "a")])
    register_bytes = path.read_bytes()
    proposal = build_correspondence_proposal(
        register=register_bytes,
        register_digest=register_digest(register_bytes),
        discovery_run_id="run-1",
        components=[
            {
                "physical_page_id": PAGE,
                "physical_act_id": None,
                "local_acts": [_local(ACT_A, PG1, SOURCE_A, "a")],
                "evidence": ["geometry:late"],
                "finding": None,
            }
        ],
    )
    assert proposal["accepted_records"] == []
    assert proposal["findings"] == [{"code": "ambiguous-physical-act", "act_id": ACT_A}]


def test_correspondence_page_lineage_mismatch_is_refused_at_the_register(tmp_path):
    """An act-to-physical-act link cannot discard the page it was declared from.

    Disposition recorded at composition: the register now re-derives every
    correspondence's act_id from the page, class, and bounds beside it, so a
    declaration that moves an act onto another page is refused at the append --
    before either downstream consumer could be asked to hold it. The
    `correspondence-page-mismatch` findings in the proposal builder and the
    partition remain as defense in depth behind this refusal.
    """
    path = _register(tmp_path)
    physical = physical_act_id(PAGE, "entry-a")
    before = path.read_bytes()
    with pytest.raises(SchemaRefusal, match="does not bind the page, class, and bounds"):
        append_records(
            path,
            [
                {
                    "kind": "physical-act",
                    "physical_page_id": PAGE,
                    "mint_designation": "entry-a",
                    "physical_act_id": physical,
                    "evidence": ["fixture"],
                    "appending_run": "run-1",
                },
                {
                    "kind": "correspondence",
                    "page_id": PG2,
                    "act_id": ACT_A,
                    "act_class": "proposal",
                    "act_bounds": {"x": 0, "y": 0, "w": 10, "h": 10},
                    "physical_page_id": PAGE,
                    "physical_act_id": physical,
                    "evidence": ["fixture"],
                    "appending_run": "run-1",
                },
            ],
            expected_digest=register_digest(path.read_bytes()),
        )
    assert path.read_bytes() == before


def test_a_component_cannot_claim_an_unreached_existing_act_on_the_same_page(tmp_path):
    """A caller-supplied id is not proof that geometry touched that act.

    Every local member here is unresolved.  Naming an existing physical act on
    the right page used to append the component to it anyway, allowing a false
    merge with no shared registered act and no finding -- consult §2.2's
    highest-risk failure.
    """
    path, _head = _three_member_register(tmp_path)
    existing = _mint(path, PAGE, [_local(ACT_A, PG1, SOURCE_A, "a")], run="run-1")
    unrelated = act_id(PG2, "proposal", {"x": 0, "y": 50, "w": 10, "h": 10})
    register_bytes = path.read_bytes()
    proposal = build_correspondence_proposal(
        register=register_bytes,
        register_digest=register_digest(register_bytes),
        discovery_run_id="run-2",
        components=[
            {
                "physical_page_id": PAGE,
                "physical_act_id": existing,
                "local_acts": [
                    _local(
                        unrelated, PG2, SOURCE_B, "unrelated", {"x": 0, "y": 50, "w": 10, "h": 10}
                    )
                ],
                "evidence": ["geometry:asserted-only"],
                "finding": None,
            }
        ],
    )
    assert proposal["accepted_records"] == []
    assert proposal["findings"] == [{"code": "ambiguous-physical-act", "act_id": unrelated}]


def test_a_retracted_correspondence_is_not_re_declared_behind_the_retraction(tmp_path):
    """Retraction is a person correcting the register. A later discovery run

    proposing the same component again must not quietly undo it: the local act
    is held under the code the register already gives it.
    """
    path, _head = _three_member_register(tmp_path)
    minted = _mint(path, PAGE, [_local(ACT_A, PG1, SOURCE_A, "a")], run="run-1")
    append_records(
        path,
        [
            {
                "kind": "retraction",
                "retracts": f"{ACT_A}->{minted}",
                "reason": "a person confirmed two frames as one page and was wrong",
                "appending_run": "triage-3",
            }
        ],
        expected_digest=register_digest(path.read_bytes()),
    )
    register_bytes = path.read_bytes()
    proposal = build_correspondence_proposal(
        register=register_bytes,
        register_digest=register_digest(register_bytes),
        discovery_run_id="run-2",
        components=[
            {
                "physical_page_id": PAGE,
                "physical_act_id": None,
                "local_acts": [
                    _local(ACT_A, PG1, SOURCE_A, "a"),
                    _local(ACT_B, PG2, SOURCE_B, "b"),
                ],
                "evidence": ["geometry:again"],
                "finding": None,
            }
        ],
    )
    assert proposal["accepted_records"] == []
    # The retracted member is named for what it is; its sibling is named too,
    # because this run gave it no correspondence and an unnamed member is a lost
    # one (GOVERNANCE 2).
    assert proposal["findings"] == [
        {"code": "retracted-physical-act", "act_id": ACT_A},
        {"code": "unresolved-physical-act", "act_id": ACT_B},
    ]


def test_a_proposal_register_digest_that_does_not_describe_its_register_is_refused(tmp_path):
    path, _head = _three_member_register(tmp_path)
    with pytest.raises(IncompatibleReuse, match="not the digest of the register bytes"):
        build_correspondence_proposal(
            register=path.read_bytes(),
            register_digest="0" * 64,
            discovery_run_id="run-1",
            components=[
                {
                    "physical_page_id": PAGE,
                    "physical_act_id": None,
                    "local_acts": [_local(ACT_A, PG1, SOURCE_A, "a")],
                    "evidence": ["geometry:one"],
                    "finding": None,
                }
            ],
        )


def test_shared_and_capture_unique_acts_conserve_local_rows_and_count_logical_acts_once(tmp_path):
    """Consult §8.2 test 20. One physical act shared by two captures, plus an act

    each capture saw on its own, conserve every local row and count the shared
    act once.
    """
    path, _head = _three_member_register(tmp_path)
    unique_a = act_id(PG1, "proposal", {"x": 0, "y": 40, "w": 10, "h": 10})
    unique_b = act_id(PG2, "proposal", {"x": 0, "y": 80, "w": 10, "h": 10})
    _mint(path, PAGE, [_local(ACT_A, PG1, SOURCE_A, "a"), _local(ACT_B, PG2, SOURCE_B, "b")])
    _mint(
        path,
        PAGE,
        [_local(unique_a, PG1, SOURCE_A, "ua", {"x": 0, "y": 40, "w": 10, "h": 10})],
        run="run-2",
    )
    _mint(
        path,
        PAGE,
        [_local(unique_b, PG2, SOURCE_B, "ub", {"x": 0, "y": 80, "w": 10, "h": 10})],
        run="run-3",
    )
    partition = _partition(
        path.read_bytes(),
        [
            _local(ACT_A, PG1, SOURCE_A, "a"),
            _local(ACT_B, PG2, SOURCE_B, "b"),
            _local(unique_a, PG1, SOURCE_A, "ua", {"x": 0, "y": 40, "w": 10, "h": 10}),
            _local(unique_b, PG2, SOURCE_B, "ub", {"x": 0, "y": 80, "w": 10, "h": 10}),
        ],
        [
            _align(PG1, SOURCE_A, PAGE, "align:a"),
            _align(PG2, SOURCE_B, PAGE, "align:b"),
            # A third registered capture of the page that proposed no act at all:
            # it still owes a presentation row wherever the page is presented.
            _align(PG3, SOURCE_C, PAGE, "align:c"),
        ],
        {SOURCE_A, SOURCE_B, SOURCE_C},
    )
    validate_physical_act_partition(partition)
    assert partition["findings"] == []
    assert partition["local_expected_count"] == 4
    assert partition["logical_expected_count"] == 3
    assert len(partition["local_to_logical"]) == 4
    shared = next(
        group for group in partition["logical_acts"] if len(group["member_local_acts"]) == 2
    )
    assert {row["act_id"] for row in shared["member_local_acts"]} == {ACT_A, ACT_B}
    # A capture-unique act still presents every member capture of its physical
    # page: the capture that did not propose it has a row with no local act.
    solo = next(
        group
        for group in partition["logical_acts"]
        if [row["act_id"] for row in group["member_local_acts"]] == [unique_a]
    )
    assert [row["local_act_ids"] for row in solo["capture_presentations"]].count([]) == 2


def test_a_split_or_merge_match_is_ambiguous_not_resolved_by_highest_overlap(tmp_path):
    """Consult §8.2 test 21 and §7 shape 13. A split (one local act in two

    components) and a merge (two local acts of one capture in one component) are
    both held. There is nothing to rank them by: the component surface carries
    geometric evidence references and no number at all.
    """
    path, _head = _three_member_register(tmp_path)
    second = act_id(PG1, "proposal", {"x": 0, "y": 60, "w": 10, "h": 10})
    register_bytes = path.read_bytes()
    merge = build_correspondence_proposal(
        register=register_bytes,
        register_digest=register_digest(register_bytes),
        discovery_run_id="run-merge",
        components=[
            {
                "physical_page_id": PAGE,
                "physical_act_id": None,
                "local_acts": [
                    _local(ACT_A, PG1, SOURCE_A, "a"),
                    _local(second, PG1, SOURCE_A, "a2", {"x": 0, "y": 60, "w": 10, "h": 10}),
                ],
                "evidence": ["geometry:overlap-0.9"],
                "finding": None,
            }
        ],
    )
    assert merge["accepted_records"] == []
    assert {row["act_id"] for row in merge["findings"]} == {ACT_A, second}
    split = build_correspondence_proposal(
        register=register_bytes,
        register_digest=register_digest(register_bytes),
        discovery_run_id="run-split",
        components=[
            {
                "physical_page_id": PAGE,
                "physical_act_id": None,
                "local_acts": [
                    _local(ACT_A, PG1, SOURCE_A, "a"),
                    _local(ACT_B, PG2, SOURCE_B, "b"),
                ],
                "evidence": ["geometry:overlap-0.9"],
                "finding": None,
            },
            {
                "physical_page_id": PAGE,
                "physical_act_id": None,
                "local_acts": [
                    _local(ACT_A, PG1, SOURCE_A, "a"),
                    _local(ACT_C, PG3, SOURCE_C, "c"),
                ],
                "evidence": ["geometry:overlap-0.4"],
                "finding": None,
            },
        ],
    )
    assert split["accepted_records"] == []
    assert {row["act_id"] for row in split["findings"]} == {ACT_A, ACT_B, ACT_C}


def test_retracting_a_capture_changes_evidence_and_coverage_not_physical_act_identity(tmp_path):
    """Consult §8.1 test 9, at the partition boundary. Withdrawing the head of a

    page's membership chain removes a capture from the cluster. The physical act
    keeps its identity and the surviving capture keeps its presentation; the act
    proposed on the withdrawn capture is held by name rather than dropped, and
    nothing anywhere becomes a winner.
    """
    path, first_link = _three_member_register(tmp_path)
    minted = _mint(
        path, PAGE, [_local(ACT_A, PG1, SOURCE_A, "a"), _local(ACT_B, PG2, SOURCE_B, "b")]
    )
    local_acts = [_local(ACT_A, PG1, SOURCE_A, "a"), _local(ACT_B, PG2, SOURCE_B, "b")]
    alignments = [
        _align(PG1, SOURCE_A, PAGE, "align:a"),
        _align(PG2, SOURCE_B, PAGE, "align:b"),
        _align(PG3, SOURCE_C, PAGE, "align:c"),
    ]
    ledger = {SOURCE_A, SOURCE_B, SOURCE_C}
    before = _partition(path.read_bytes(), local_acts, alignments, ledger)
    assert before["findings"] == []
    assert before["logical_acts"][0]["logical_act_id"] == minted
    assert len(before["logical_acts"][0]["capture_presentations"]) == 3

    head = membership_heads(path.read_bytes())[PAGE][0]
    append_records(
        path,
        [
            {
                "kind": "retraction",
                "retracts": f"membership:{head}",
                "reason": "two blank forms agreed everywhere and a person confirmed them",
                "appending_run": "triage-4",
            }
        ],
        expected_digest=register_digest(path.read_bytes()),
    )
    assert members_of(path.read_bytes(), PAGE) == [SOURCE_A]
    assert membership_heads(path.read_bytes())[PAGE][0] == first_link
    after = _partition(path.read_bytes(), local_acts, alignments, ledger)
    surviving = after["logical_acts"][0]
    assert surviving["logical_act_id"] == minted, "identity must not move with the evidence"
    assert surviving["identity_scope"] == "physical-act"
    assert [row["source_sha256"] for row in surviving["capture_presentations"]] == [SOURCE_A]
    assert after["findings"] == [{"code": "capture-page-alignment-unresolved", "act_id": ACT_B}]
    validate_physical_act_partition(after)


def test_the_register_lifecycle_closes_proposal_append_retraction_reproposal(tmp_path):
    """Proposal, append, retraction, re-proposal -- end to end, at this boundary.

    Each transition is checked for what it leaves behind, because a lifecycle
    that cannot complete is one an operator eventually completes some other way.
    The resolver refuses to re-propose behind a person's correction; the person
    reasserting it restores the same physical act rather than minting a second
    one; and the partition publishes the same logical identity it did before the
    correction, with the withdrawn declaration still in the register.
    """
    path, _head = _three_member_register(tmp_path)
    local_acts = [_local(ACT_A, PG1, SOURCE_A, "a"), _local(ACT_B, PG2, SOURCE_B, "b")]
    alignments = [
        _align(PG1, SOURCE_A, PAGE, "align:a"),
        _align(PG2, SOURCE_B, PAGE, "align:b"),
        _align(PG3, SOURCE_C, PAGE, "align:c"),
    ]
    ledger = {SOURCE_A, SOURCE_B, SOURCE_C}

    minted = _mint(path, PAGE, local_acts)
    first = _partition(path.read_bytes(), local_acts, alignments, ledger)
    assert first["findings"] == []
    assert first["logical_acts"][0]["logical_act_id"] == minted

    withdrawal = {
        "kind": "retraction",
        "retracts": f"{ACT_B}->{minted}",
        "reason": "declared against the wrong capture",
        "appending_run": "triage-3",
    }
    append_records(path, [withdrawal], expected_digest=register_digest(path.read_bytes()))
    held = _partition(path.read_bytes(), local_acts, alignments, ledger)
    assert held["findings"] == [{"code": "retracted-physical-act", "act_id": ACT_B}]
    assert held["logical_acts"][0]["logical_act_id"] == minted, "A keeps its identity"
    validate_physical_act_partition(held)

    register_bytes = path.read_bytes()
    reproposal = build_correspondence_proposal(
        register=register_bytes,
        register_digest=register_digest(register_bytes),
        discovery_run_id="run-again",
        components=[
            {
                "physical_page_id": PAGE,
                "physical_act_id": minted,
                "local_acts": local_acts,
                "evidence": ["geometry:again"],
                "finding": None,
            }
        ],
    )
    assert reproposal["accepted_records"] == []
    assert reproposal["findings"] == [{"code": "retracted-physical-act", "act_id": ACT_B}]

    append_records(
        path,
        [
            {
                "kind": "correspondence",
                "page_id": PG2,
                "act_id": ACT_B,
                "act_class": "proposal",
                "act_bounds": {"x": 0, "y": 0, "w": 10, "h": 10},
                "physical_page_id": PAGE,
                "physical_act_id": minted,
                "evidence": ["operator:reviewed-the-frames"],
                "appending_run": "triage-4",
            }
        ],
        expected_digest=register_digest(path.read_bytes()),
    )
    restored = _partition(path.read_bytes(), local_acts, alignments, ledger)
    assert restored["findings"] == []
    assert restored["logical_acts"][0]["logical_act_id"] == minted
    assert restored["logical_expected_count"] == 1
    assert withdrawal in json.loads(path.read_bytes())["records"]


def test_a_correspondence_appended_without_its_physical_act_is_refused(tmp_path):
    """A torn append -- correspondences published without the mint they name --

    refuses loudly rather than resolving against a physical act nobody declared.
    The register writes one atomic list, so this is unreachable through
    `append_correspondence_proposal`; it is the check that makes that atomicity
    load-bearing rather than assumed.
    """
    path, _head = _three_member_register(tmp_path)
    orphan = physical_act_id(PAGE, "never-minted")
    with pytest.raises(SchemaRefusal, match="before any earlier record declares it"):
        append_records(
            path,
            [
                {
                    "kind": "correspondence",
                    "page_id": PG1,
                    "act_id": ACT_A,
                    "act_class": "proposal",
                    "act_bounds": {"x": 0, "y": 0, "w": 10, "h": 10},
                    "physical_page_id": PAGE,
                    "physical_act_id": orphan,
                    "evidence": ["geometry:torn"],
                    "appending_run": "run-torn",
                }
            ],
            expected_digest=register_digest(path.read_bytes()),
        )
    assert resolve_proposal(path.read_bytes(), ACT_A)["code"] == "unresolved-physical-act"


def test_append_refuses_a_self_hashed_but_non_closed_correspondence_proposal(tmp_path):
    """The writer validates the sealed artifact, not only its hash and records."""
    path, _head = _three_member_register(tmp_path)
    predecessor = register_digest(path.read_bytes())
    proposal = build_correspondence_proposal(
        register=path.read_bytes(),
        register_digest=predecessor,
        discovery_run_id="run-1",
        components=[
            {
                "physical_page_id": PAGE,
                "physical_act_id": None,
                "local_acts": [_local(ACT_A, PG1, SOURCE_A, "a")],
                "evidence": ["geometry:one"],
                "finding": None,
            }
        ],
    )
    proposal["unrecorded_side_channel"] = "the schema did not authorize this"
    proposal["self_hash"] = self_hash(proposal)
    before = path.read_bytes()
    with pytest.raises(SchemaRefusal, match="not closed"):
        append_correspondence_proposal(
            register_path=str(path),
            proposal=proposal,
            discovery_register_digest=predecessor,
        )
    assert path.read_bytes() == before


def test_one_source_split_into_two_pages_resolves_each_page_to_its_own_physical_page(tmp_path):
    """Consult §10.2, the split-page case. A two-up scan is one capture rendered

    as two pages that show two different physical pages, so source-digest
    equality alone cannot say which physical page an act belongs to: the
    alignment is keyed by rendered page, and each half resolves to its own
    physical page's act.
    """
    left, right = "pg_" + "a" * 16, "pg_" + "b" * 16
    left_act = act_id(left, "proposal", {"x": 0, "y": 0, "w": 10, "h": 10})
    right_act = act_id(right, "proposal", {"x": 0, "y": 0, "w": 10, "h": 10})
    path = tmp_path / "register.json"
    append_records(
        path,
        [
            {
                "kind": "physical-page",
                "corpus_id": "fixture",
                "volume_id": "book",
                "designation": "12r",
                "physical_page_id": PAGE,
                "appending_run": "triage",
            },
            {
                "kind": "physical-page",
                "corpus_id": "fixture",
                "volume_id": "book",
                "designation": "13r",
                "physical_page_id": PAGE_13R,
                "appending_run": "triage",
            },
            {
                "kind": "membership",
                "physical_page_id": PAGE,
                "members": [SOURCE_A],
                "predecessor": None,
                "appending_run": "triage",
            },
            {
                "kind": "membership",
                "physical_page_id": PAGE_13R,
                "members": [SOURCE_A],
                "predecessor": None,
                "appending_run": "triage",
            },
        ],
        expected_digest=register_digest(empty_register()),
    )
    _mint(path, PAGE, [_local(left_act, left, SOURCE_A, "l")], run="run-left")
    _mint(path, PAGE_13R, [_local(right_act, right, SOURCE_A, "r")], run="run-right")
    partition = _partition(
        path.read_bytes(),
        [_local(left_act, left, SOURCE_A, "l"), _local(right_act, right, SOURCE_A, "r")],
        [
            _align(left, SOURCE_A, PAGE, "align:left"),
            _align(right, SOURCE_A, PAGE_13R, "align:right"),
        ],
        {SOURCE_A},
    )
    assert partition["findings"] == []
    assert partition["logical_expected_count"] == 2
    pages = sorted(
        component["physical_page_id"]
        for group in partition["logical_acts"]
        for component in group["physical_page_components"]
    )
    assert pages == sorted([PAGE, PAGE_13R])
    assert all(len(group["physical_page_components"]) == 1 for group in partition["logical_acts"])


# --- Sonnet security seat: path-handling on the digest-bound seal reference -----


@pytest.mark.parametrize("escaping_path", ["../outside", "a/../../outside", "/etc/passwd"])
def test_a_traversal_proposal_seal_ref_is_refused_at_the_builder(escaping_path):
    with pytest.raises(SchemaRefusal, match="escapes the run tree"):
        build_physical_act_partition(
            register=empty_register(),
            register_digest=register_digest(empty_register()),
            proposal_seal_ref={"relative_path": escaping_path, "sha256": "0" * 64},
            local_acts=[_local(ACT_A, "pg_" + "1" * 16, SOURCE_A, "a")],
            capture_alignments=[],
            source_ledger=set(),
        )


def test_a_traversal_proposal_seal_ref_is_refused_at_the_validator():
    payload = {
        "schema": PARTITION_SCHEMA,
        "register_digest": register_digest(empty_register()),
        "proposal_seal_ref": {"relative_path": "../outside", "sha256": "0" * 64},
        "local_expected_count": 0,
        "logical_expected_count": 0,
        "logical_acts": [],
        "local_to_logical": [],
        "findings": [],
    }
    payload["self_hash"] = self_hash(payload)
    with pytest.raises(SchemaRefusal, match="escapes the run tree"):
        validate_physical_act_partition(payload)


@pytest.mark.parametrize("path_value", [None, 7, True, ["a"], {"a": 1}, ""])
def test_the_builder_refuses_a_non_string_proposal_seal_path_by_name(path_value):
    """The builder's key-set check proves the field is present, not that it is a string.

    The validator already types this field, so the gap was on the build side
    only: `relative_path` arriving as None or a number reached `startswith` and
    killed the stage with an AttributeError, where this module's whole contract
    is to refuse by name. Driven through the builder for that reason -- routed
    at the validator this case never reaches the check it is about.
    """
    register = empty_register()
    with pytest.raises(SchemaRefusal, match="path is not a non-empty string"):
        build_physical_act_partition(
            register=register,
            register_digest=register_digest(register),
            proposal_seal_ref={"relative_path": path_value, "sha256": "0" * 64},
            local_acts=[_local(ACT_A, PG1, SOURCE_A, "a1")],
            capture_alignments=[],
            source_ledger={SOURCE_A},
        )


def test_the_textual_screen_walks_a_deep_payload_instead_of_the_interpreter_stack():
    """The sibling of the preference screens, and it had the same defect.

    This guard runs over caller-supplied proposal payloads before any shape
    check closes them, so a deeply nested one walked recursively exhausted the
    stack and raised RecursionError -- a crash naming nothing, in a module whose
    entire purpose is to refuse textual evidence by name.
    """
    deep = {"leaf": 1}
    for _ in range(50_000):
        deep = {"nested": deep}
    # Clean to the bottom: depth alone must not stop the walk.
    _partition_module._refuse_textual(deep)

    buried = {"ocr": "a reading"}
    for _ in range(50_000):
        buried = {"nested": [buried]}
    with pytest.raises(SchemaRefusal, match="textual evidence cannot match physical acts"):
        _partition_module._refuse_textual(buried)
