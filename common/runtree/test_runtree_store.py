"""The run tree's three promises, each asserted in both directions.

Harvest invariant #14, verbatim: "A seal that stops refusing bad things in order to
stop refusing good things is not a fix" — both directions are asserted. So every
refusal here has an acceptance beside it: identical bytes are reused *and*
different bytes are refused; an unchanged run resumes *and* a changed one does not.

Meta-invariant #86 — load-bearing tests drive real producers over real artifacts.
These write real files to a real temporary directory through the real store; there
is no in-memory stand-in, because the properties under test are properties of the
filesystem behaviour.
"""

import errno
import inspect
import json
import os
import re
from pathlib import Path

import pytest

from common.chairs.models import ChairIdentity, ServingDetails
from common.chairs.receipts import build_receipt
from common.contracts.approval import ApprovalRecordReference, build_approval_record
from common.contracts.canonical import canonical_bytes, digest_bytes, self_hash
from common.contracts.envelope import build_envelope
from common.contracts.errors import ApprovalRefusal, IncompatibleReuse, SchemaRefusal
from common.contracts.identities import artifact_id
from common.contracts.stages import DESIGNATOR, DOOR, EXEMPLAR, PERLECTOR
from common.recensor_receipt import build_recensor_partition_receipt
from common.runtree import store as runtree_store
from common.runtree.store import INDEX_FILE, RECEIPTS_DIR, RUN_FILE, RunTree

PAGE_BYTES = b"synthetic page one"
SOURCE = [{"relative_path": "proof/page-1.png", "sha256": digest_bytes(PAGE_BYTES), "ordinal": 1}]
CONFIG_DIGEST = "c" * 64
RECIPES = {"designator": "fake-designator-v0"}
CHAIRS = ["attestator_1", "attestator_2", "attestator_3"]


def make_run(tmp_path, run_id="r1", **overrides):
    kwargs = {
        "source_manifest": SOURCE,
        "config_digest": CONFIG_DIGEST,
        "adapter_recipes": RECIPES,
        "witness_chairs": CHAIRS,
    }
    kwargs.update(overrides)
    return RunTree.create(tmp_path, run_id, **kwargs)


def make_envelope(run_id="r1", subject="pg_0123456789abcdef", outcome="proposed", **payload):
    return build_envelope(
        run_id=run_id,
        artifact_id=artifact_id(DESIGNATOR, "proposal", subject),
        subject_id=subject,
        stage=DESIGNATOR,
        kind="proposal",
        outcome=outcome,
        config_digest=CONFIG_DIGEST,
        adapter_revision="fake-designator-v0",
        inputs=[],
        payload=payload or {"proposals": 2},
    )


def make_receipt(*, endpoint="http://fixture.invalid/seat", started_at="2026-08-03T00:00:00Z"):
    identity = ChairIdentity(
        role="attestator_1",
        source="local-repository",
        repo=None,
        path="fixture/attestator_1",
        revision=None,
        digest_manifest="a" * 64,
        manifest="manifests/attestator_1.json",
        adapter_of=None,
        serving_recipe="fake-attestator-v0",
        license_note="fixture only",
    )
    details = ServingDetails(
        # A pin, not a label: `receipts.py` refuses a mutable name here on the same
        # grounds `config.py` refuses a branch name for the model revision.
        tokenizer_revision="a" * 64,
        seed=0,
        context_cap=4096,
        pixel_cap=1_000_000,
        engine="fixture-engine",
        engine_version="v0",
        dtype="float32",
        adapter_identity=None,
        endpoint=endpoint,
        started_at=started_at,
    )
    return build_receipt(identity, details)


def make_approval_record(**overrides):
    record = build_approval_record(
        subject_ids=["some-exclusion-subject"],
        action="exclusion",
        reason="approved for this exact synthetic record",
        target_version_hash="b" * 64,
        timestamp="2026-08-04T12:00:00Z",
    )
    record.update(overrides)
    return record


def make_recensor_partition_receipt():
    return build_recensor_partition_receipt(
        run_id="r1",
        config_digest=CONFIG_DIGEST,
        proposal_seal_ref={
            "relative_path": "2_designator/artifacts/proposal-seal.json",
            "sha256": "a" * 64,
        },
        items=[
            {
                "act_id": "act-1",
                "act_key": "a1",
                "designator_outcome": "proposed",
                "review_ref": {
                    "relative_path": "5_recensor/artifacts/review.json",
                    "sha256": "b" * 64,
                },
                "review_outcome": "accepted",
                "partition_class": "completed",
                "coverage": {
                    "configured": 3,
                    "floor": 3,
                    "by_outcome": {"read": 3},
                    "by_class": {"completed": 3, "unresolved": 0, "failed": 0},
                    "under_witnessed": False,
                    "unresolved_chairs": 0,
                    "page_granularity_only": 0,
                    "health_unrecorded": 0,
                    "shortfalls": {"failed": 0, "truncated": 0, "unaligned": 0},
                    "granularity_basis": "act-outcome-proxy-before-alignment",
                },
            }
        ],
    )


# --- The Recensor partition receipt: replace in place, but never a shrinking
# --- denominator under the same run authority ----------------------------------


def test_a_write_that_would_shrink_the_expected_act_count_is_refused(tmp_path):
    """The proposal-act denominator is sealed once by the Designator; two honest
    Recensor passes over the same run can never legitimately disagree about how
    many acts it names. A write that would shrink it is not a fresher partition
    superseding a stale one -- it is a different, inconsistent claim about the
    same sealed denominator, and is refused rather than silently accepted as
    whichever write happened to land last."""
    tree = make_run(tmp_path)
    two_items = make_recensor_partition_receipt()
    second_item = dict(two_items["items"][0], act_id="act-2", act_key="a2")
    two_items = build_recensor_partition_receipt(
        run_id=two_items["run_id"],
        config_digest=two_items["config_digest"],
        proposal_seal_ref=two_items["proposal_seal_ref"],
        items=[two_items["items"][0], second_item],
    )
    tree.write_recensor_partition_receipt(two_items)

    with pytest.raises(SchemaRefusal, match="expected_act_count"):
        tree.write_recensor_partition_receipt(make_recensor_partition_receipt())

    # The two-item receipt already on disk survives the refused write untouched.
    assert tree.read_recensor_partition_receipt()["expected_act_count"] == 2


def test_a_write_that_grows_the_expected_act_count_is_also_refused(tmp_path):
    """Grown or shrunk, either direction disagrees with an already-sealed
    denominator, so neither is treated as the fresher one."""
    tree = make_run(tmp_path)
    tree.write_recensor_partition_receipt(make_recensor_partition_receipt())

    two_items = make_recensor_partition_receipt()
    second_item = dict(two_items["items"][0], act_id="act-2", act_key="a2")
    grown = build_recensor_partition_receipt(
        run_id=two_items["run_id"],
        config_digest=two_items["config_digest"],
        proposal_seal_ref=two_items["proposal_seal_ref"],
        items=[two_items["items"][0], second_item],
    )
    with pytest.raises(SchemaRefusal, match="expected_act_count"):
        tree.write_recensor_partition_receipt(grown)


def test_repeating_an_identical_receipt_is_reused_not_refused(tmp_path):
    """The unchanged-denominator guard must not itself turn an idempotent
    replay -- the ordinary case of resuming or re-running a finished pass --
    into a refusal."""
    tree = make_run(tmp_path)
    receipt = make_recensor_partition_receipt()
    tree.write_recensor_partition_receipt(receipt)
    result = tree.write_recensor_partition_receipt(receipt)
    assert result.reused is True


# --- The run authority ---------------------------------------------------------


def test_creating_a_run_writes_a_self_hashed_authority(tmp_path):
    tree = make_run(tmp_path)
    record = tree.read_run()
    assert record["run_id"] == "r1"
    assert record["witness_chairs"] == CHAIRS
    assert record["source_manifest"] == SOURCE
    assert (tmp_path / "r1" / RUN_FILE).exists()


def test_the_run_authority_does_not_predeclare_acts(tmp_path):
    """Pages are given; acts are discovered. The Designator's proposal seal is the
    downstream expected-act authority, so run.json naming acts would make the
    orchestrator expect what nobody had found yet."""
    assert "acts" not in make_run(tmp_path).read_run()


def test_reopening_an_unchanged_run_is_allowed(tmp_path):
    make_run(tmp_path)
    reopened = make_run(tmp_path)
    assert reopened.read_run()["config_digest"] == CONFIG_DIGEST


def test_reusing_a_run_id_with_changed_source_is_refused(tmp_path):
    """Spec 01 test 3: reusing a run ID with changed source/config/adapter revision
    fails before writing."""
    make_run(tmp_path)
    changed = [{"relative_path": "proof/page-1.png", "sha256": "b" * 64, "ordinal": 1}]
    with pytest.raises(IncompatibleReuse) as caught:
        make_run(tmp_path, source_manifest=changed)
    assert "source_manifest" in str(caught.value)


def test_a_source_manifest_repeating_an_ordinal_is_refused(tmp_path):
    """An ordinal names one page. Two rows sharing one leave the run unable to say
    how many pages arrived — and the Armarium's page census compares itself against
    these ordinals as a set, so the repeat would silently reduce two declared pages
    to one and let a run that lost one of them still reconcile as `complete`. That
    is the lost-page defect four reviewers filed, one level down."""
    twice = [
        {"relative_path": "proof/page-1.png", "sha256": "a" * 64, "ordinal": 1},
        {"relative_path": "proof/page-1-again.png", "sha256": "b" * 64, "ordinal": 1},
    ]
    with pytest.raises(SchemaRefusal) as caught:
        make_run(tmp_path, source_manifest=twice)
    assert "[1]" in str(caught.value)
    assert not (tmp_path / "r1" / RUN_FILE).exists(), "refused before anything was written"


def test_a_source_page_without_an_integer_ordinal_is_refused(tmp_path):
    """The other direction of the same rule: a run cannot account for pages it
    cannot count. `True` is excluded explicitly because `isinstance(True, int)`.

    Every case but the first carries a well-formed `sha256`, so the ordinal is the
    only thing wrong with it. Nothing validates a source page's digest today, so
    the refusals below can only be the ordinal check — but a digest check added
    ahead of it would otherwise leave this test passing for the wrong reason."""
    for bad in (
        {"relative_path": "p.png", "sha256": "a" * 64},
        {"sha256": "a" * 64, "ordinal": "1"},
        {"sha256": "a" * 64, "ordinal": True},
    ):
        with pytest.raises(SchemaRefusal):
            make_run(tmp_path, source_manifest=[{"relative_path": "p.png", **bad}])


def test_a_well_formed_manifest_of_several_pages_is_still_accepted(tmp_path):
    """Invariant #14: the refusals above must not have bought their strictness by
    refusing good input too."""
    fine = [
        {"relative_path": "proof/page-1.png", "sha256": "a" * 64, "ordinal": 1},
        {"relative_path": "proof/page-2.png", "sha256": "b" * 64, "ordinal": 2},
    ]
    tree = make_run(tmp_path, source_manifest=fine)
    assert [page["ordinal"] for page in tree.read_run()["source_manifest"]] == [1, 2]


def test_reusing_a_run_id_with_changed_config_is_refused(tmp_path):
    make_run(tmp_path)
    with pytest.raises(IncompatibleReuse):
        make_run(tmp_path, config_digest="d" * 64)


def test_render_settings_are_an_explicit_run_binding_not_only_an_opaque_digest(tmp_path):
    first = {"pdf": {"configured_target_dpi": 300, "target_dpi": 300, "minimum_dpi": 72}}
    second = {"pdf": {"configured_target_dpi": 400, "target_dpi": 400, "minimum_dpi": 72}}
    make_run(tmp_path, render_settings=first)
    before = (tmp_path / "r1" / "run.json").read_bytes()

    with pytest.raises(IncompatibleReuse, match="render_settings"):
        make_run(tmp_path, render_settings=second)

    assert (tmp_path / "r1" / "run.json").read_bytes() == before


def test_reusing_a_run_id_with_changed_adapter_recipes_is_refused(tmp_path):
    make_run(tmp_path)
    with pytest.raises(IncompatibleReuse):
        make_run(tmp_path, adapter_recipes={"designator": "fake-designator-v1"})


def test_reusing_a_run_id_with_a_changed_chair_roster_is_refused(tmp_path):
    """A run that silently dropped a configured chair would under-witness every act
    in it while looking like the run that was authorized."""
    make_run(tmp_path)
    with pytest.raises(IncompatibleReuse):
        make_run(tmp_path, witness_chairs=["attestator_1", "attestator_2"])


def test_reusing_a_run_id_with_changed_ingress_evidence_is_refused(tmp_path):
    """A run cannot turn a declared real ingress into a fixture on reuse."""
    make_run(tmp_path, ingress={"mode": "synthetic-fixture"})

    with pytest.raises(IncompatibleReuse, match="ingress"):
        make_run(tmp_path, ingress={"mode": "real"})


def test_an_incompatible_reuse_writes_nothing(tmp_path):
    tree = make_run(tmp_path)
    tree.publish_artifact(make_envelope())
    before = sorted(path.name for path in (tmp_path / "r1").rglob("*"))
    with pytest.raises(IncompatibleReuse):
        make_run(tmp_path, config_digest="d" * 64)
    assert sorted(path.name for path in (tmp_path / "r1").rglob("*")) == before


def test_an_edited_run_authority_is_refused(tmp_path):
    tree = make_run(tmp_path)
    record = tree.read_run()
    record["config_digest"] = "e" * 64
    (tmp_path / "r1" / RUN_FILE).write_bytes(canonical_bytes(record))
    with pytest.raises(IncompatibleReuse) as caught:
        tree.read_run()
    assert "self-hash" in str(caught.value)


def test_an_old_schema_run_authority_is_refused_before_a_stage_can_use_it(tmp_path):
    tree = make_run(tmp_path)
    record = tree.read_run()
    record["schema"] = "skeleton.v0"
    record["self_hash"] = self_hash(record)
    (tmp_path / "r1" / RUN_FILE).write_bytes(canonical_bytes(record))

    with pytest.raises(IncompatibleReuse, match="old run cannot be reinterpreted"):
        tree.read_run()


def test_reading_a_run_that_does_not_exist_is_refused(tmp_path):
    with pytest.raises(IncompatibleReuse):
        RunTree(tmp_path, "never-created").read_run()


def test_a_run_id_symlink_cannot_redirect_a_new_run_outside_its_requested_root(tmp_path):
    requested_root = tmp_path / "requested-runs"
    requested_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (requested_root / "r1").symlink_to(outside, target_is_directory=True)

    with pytest.raises(SchemaRefusal, match="outside the requested run root"):
        make_run(requested_root)

    assert not (outside / RUN_FILE).exists()


# --- Immutability, and reuse in both directions -------------------------------


def test_publishing_an_artifact_writes_it_once(tmp_path):
    tree = make_run(tmp_path)
    result = tree.publish_artifact(make_envelope())
    assert result.reused is False
    assert tree.resolve(result.relative_path).exists()


def test_republishing_identical_bytes_is_reuse_not_a_rewrite(tmp_path):
    """Spec 01 test 2 and 4: repeating the identical command leaves all artifact
    bytes unchanged, and an interrupted run resumes from valid artifacts without
    rewriting them."""
    tree = make_run(tmp_path)
    first = tree.publish_artifact(make_envelope())
    written = tree.resolve(first.relative_path)
    stamp = written.stat().st_mtime_ns

    second = tree.publish_artifact(make_envelope())

    assert second.reused is True
    assert second.relative_path == first.relative_path
    assert written.stat().st_mtime_ns == stamp


def test_republishing_different_bytes_under_one_identity_is_refused(tmp_path):
    tree = make_run(tmp_path)
    tree.publish_artifact(make_envelope())
    with pytest.raises(IncompatibleReuse) as caught:
        tree.publish_artifact(make_envelope(proposals=3))
    assert "immutable" in str(caught.value)


def test_a_refused_republish_leaves_the_original_bytes_intact(tmp_path):
    tree = make_run(tmp_path)
    result = tree.publish_artifact(make_envelope())
    original = tree.read_bytes(result.relative_path)
    with pytest.raises(IncompatibleReuse):
        tree.publish_artifact(make_envelope(proposals=99))
    assert tree.read_bytes(result.relative_path) == original


def test_an_artifact_for_another_run_is_refused(tmp_path):
    tree = make_run(tmp_path)
    with pytest.raises(SchemaRefusal):
        tree.publish_artifact(make_envelope(run_id="r2"))


def test_every_artifact_read_route_refuses_bytes_from_another_run(tmp_path):
    tree = make_run(tmp_path)
    foreign = make_envelope(run_id="r2")
    relative = tree.artifact_path(DESIGNATOR, "proposal", foreign["artifact_id"])
    path = tree.resolve(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = canonical_bytes(foreign)
    path.write_bytes(data)

    with pytest.raises(SchemaRefusal, match="belongs to run 'r2'"):
        tree.read_artifact(DESIGNATOR, "proposal", foreign["artifact_id"])
    with pytest.raises(SchemaRefusal, match="belongs to run 'r2'"):
        tree.read_artifact_reference(
            {"relative_path": relative, "sha256": digest_bytes(data)},
            stage=DESIGNATOR,
            kind="proposal",
        )
    with pytest.raises(SchemaRefusal, match="belongs to run 'r2'"):
        tree.build_manifest(DESIGNATOR)


def test_a_same_named_run_in_another_root_cannot_lend_this_one_its_evidence(tmp_path):
    """The run id is a name an operator types, and `--run-root` and `--run-id` are
    independent flags, so two runs may both be called `r1` and a name comparison
    cannot tell their artifacts apart. A reading produced under one configuration
    was accepted as the other run's, and that run's manifest, review and export all
    reconciled around it. A tree restored from a partial backup is enough.

    The config_digest is the run authority's own binding to the source manifest,
    model roster and adapter recipes it was created with. It is integrity rather
    than authentication -- anything holding this repository's API can re-seal a
    forgery, because every input to the hash is inside the record -- but it is the
    difference between "the same name" and "the same run".
    """
    ours = make_run(tmp_path / "a")
    theirs = make_run(tmp_path / "b", config_digest="d" * 64)
    foreign = make_envelope()
    foreign = build_envelope(
        run_id="r1",
        artifact_id=foreign["artifact_id"],
        subject_id=foreign["subject_id"],
        stage=DESIGNATOR,
        kind="proposal",
        outcome="proposed",
        config_digest="d" * 64,
        adapter_revision="fake-designator-v0",
        inputs=[],
        payload={"proposals": 2},
    )
    theirs.publish_artifact(foreign)

    relative = ours.artifact_path(DESIGNATOR, "proposal", foreign["artifact_id"])
    path = ours.resolve(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = canonical_bytes(foreign)
    path.write_bytes(data)

    assert foreign["run_id"] == ours.run_id, "the point of the case is that the names match"
    with pytest.raises(SchemaRefusal, match="two runs may share a name"):
        ours.read_artifact(DESIGNATOR, "proposal", foreign["artifact_id"])
    with pytest.raises(SchemaRefusal, match="two runs may share a name"):
        ours.read_artifact_reference(
            {"relative_path": relative, "sha256": digest_bytes(data)},
            stage=DESIGNATOR,
            kind="proposal",
        )
    with pytest.raises(SchemaRefusal, match="two runs may share a name"):
        ours.build_manifest(DESIGNATOR)


@pytest.mark.parametrize("authority_state", ("missing", "self-hash-corrupt", "bad-digest"))
def test_every_generic_store_route_fails_closed_without_a_valid_run_authority(
    tmp_path, authority_state
):
    """`run.json` is the binding, not an optional optimization for a fresh reader."""
    tree = make_run(tmp_path)
    envelope = make_envelope()
    result = tree.publish_artifact(envelope)
    reference = {
        "relative_path": result.relative_path,
        "sha256": digest_bytes(tree.read_bytes(result.relative_path)),
    }
    run_file = tmp_path / "r1" / RUN_FILE

    if authority_state == "missing":
        run_file.unlink()
    else:
        authority = tree.read_run()
        if authority_state == "self-hash-corrupt":
            authority["config_digest"] = "d" * 64
        else:
            authority["config_digest"] = "not-a-digest"
            authority["self_hash"] = self_hash(authority)
        run_file.write_bytes(canonical_bytes(authority))

    fresh = RunTree(tmp_path, "r1")
    with pytest.raises(IncompatibleReuse):
        fresh.read_artifact(DESIGNATOR, "proposal", envelope["artifact_id"])
    with pytest.raises(IncompatibleReuse):
        fresh.read_artifact_reference(reference, stage=DESIGNATOR, kind="proposal")
    with pytest.raises(IncompatibleReuse):
        fresh.build_manifest(PERLECTOR)
    with pytest.raises(IncompatibleReuse):
        fresh.read_index(DESIGNATOR)
    with pytest.raises(IncompatibleReuse):
        fresh.write_index(DESIGNATOR, {"schema": "test-index", "rows": []})


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("ingress", {"mode": "synthetic-fixture"}),
        ("render_settings", {"target_dpi": 400}),
    ),
)
def test_reuse_with_a_now_absent_optional_bound_field_is_named_not_a_keyerror(
    tmp_path, field, value
):
    make_run(tmp_path, **{field: value})

    with pytest.raises(IncompatibleReuse, match=field):
        make_run(tmp_path)


def test_a_published_artifact_reads_back_and_revalidates(tmp_path):
    tree = make_run(tmp_path)
    envelope = make_envelope()
    tree.publish_artifact(envelope)
    assert tree.read_artifact(DESIGNATOR, "proposal", envelope["artifact_id"]) == envelope


def test_a_corrupted_artifact_on_disk_is_refused_on_read(tmp_path):
    tree = make_run(tmp_path)
    envelope = make_envelope()
    result = tree.publish_artifact(envelope)
    tree.resolve(result.relative_path).write_text("{not json", encoding="utf-8")
    with pytest.raises(SchemaRefusal):
        tree.read_artifact(DESIGNATOR, "proposal", envelope["artifact_id"])


def test_a_valid_envelope_copied_to_a_different_artifact_path_is_refused(tmp_path):
    """The producer directory and filename are part of the artifact's identity."""
    tree = make_run(tmp_path)
    envelope = make_envelope()
    result = tree.publish_artifact(envelope)
    forged_id = "art_" + "0" * 16
    forged_path = tree.resolve(tree.artifact_path(DESIGNATOR, "proposal", forged_id))
    forged_path.parent.mkdir(parents=True, exist_ok=True)
    forged_path.write_bytes(tree.read_bytes(result.relative_path))

    with pytest.raises(SchemaRefusal, match="contents do not match"):
        tree.read_artifact(DESIGNATOR, "proposal", forged_id)
    with pytest.raises(SchemaRefusal, match="derived path"):
        tree.build_manifest(DESIGNATOR)


def test_reading_an_artifact_rechecks_every_direct_input_bytes(tmp_path):
    tree = make_run(tmp_path)
    data = b"the source evidence"
    digest, blob = tree.put_blob(DESIGNATOR, data)
    envelope = build_envelope(
        run_id="r1",
        artifact_id=artifact_id(DESIGNATOR, "proposal", "pg_0123456789abcdef"),
        subject_id="pg_0123456789abcdef",
        stage=DESIGNATOR,
        kind="proposal",
        outcome="proposed",
        config_digest=CONFIG_DIGEST,
        adapter_revision="fake-designator-v0",
        inputs=[{"relative_path": blob.relative_path, "sha256": digest}],
        payload={"proposals": 2},
    )
    tree.publish_artifact(envelope)
    tree.resolve(blob.relative_path).write_bytes(b"altered after publication")

    with pytest.raises(SchemaRefusal, match="changed under a sealed reference"):
        tree.read_artifact(DESIGNATOR, "proposal", envelope["artifact_id"])


# --- Blobs ---------------------------------------------------------------------


def test_a_blob_is_stored_under_its_own_digest_and_reused(tmp_path):
    tree = make_run(tmp_path)
    digest, first = tree.put_blob(EXEMPLAR, PAGE_BYTES)
    assert digest == digest_bytes(PAGE_BYTES)
    assert first.reused is False
    assert tree.read_bytes(first.relative_path) == PAGE_BYTES

    _, second = tree.put_blob(EXEMPLAR, PAGE_BYTES)
    assert second.reused is True


def test_different_blobs_do_not_collide(tmp_path):
    tree = make_run(tmp_path)
    _, first = tree.put_blob(EXEMPLAR, b"one")
    _, second = tree.put_blob(EXEMPLAR, b"two")
    assert first.relative_path != second.relative_path


# --- Run receipts are moments, never stage artifacts --------------------------


def test_a_run_receipt_is_content_addressed_and_reads_back(tmp_path):
    tree = make_run(tmp_path)
    reference, result = tree.write_run_receipt(make_receipt())

    assert result.reused is False
    assert reference.relative_path.startswith(f"{RECEIPTS_DIR}/")
    assert reference.relative_path.endswith(f"{reference.sha256}.json")
    record = tree.read_run_receipt(reference)
    assert record["chair"] == "attestator_1"
    assert record["revision"] == "a" * 64
    assert tree.build_manifest(DESIGNATOR)["artifacts"] == []


def test_identical_run_receipt_reuses_its_immutable_bytes(tmp_path):
    tree = make_run(tmp_path)
    receipt = make_receipt()
    first, first_result = tree.write_run_receipt(receipt)
    second, second_result = tree.write_run_receipt(receipt)

    assert first_result.reused is False
    assert second_result.reused is True
    assert second.to_record() == first.to_record()


def test_a_tampered_run_receipt_is_refused_when_its_reference_is_read(tmp_path):
    tree = make_run(tmp_path)
    reference, _ = tree.write_run_receipt(make_receipt())
    tree.resolve(reference.relative_path).write_text("{}", encoding="utf-8")

    with pytest.raises(SchemaRefusal) as caught:
        tree.read_run_receipt(reference)
    assert "digest" in str(caught.value)


def test_distinct_serving_moments_are_not_collapsed_by_model_identity(tmp_path):
    tree = make_run(tmp_path)
    first, _ = tree.write_run_receipt(make_receipt())
    second, _ = tree.write_run_receipt(
        make_receipt(endpoint="http://fixture.invalid/seat-2", started_at="2026-08-03T00:01:00Z")
    )

    assert first.to_record() != second.to_record()
    assert tree.read_run_receipt(first)["endpoint"] == "http://fixture.invalid/seat"
    assert tree.read_run_receipt(second)["endpoint"] == "http://fixture.invalid/seat-2"


# --- Approval records use the same receipt shape -----------------------------


def test_an_approval_record_is_content_addressed_and_reads_back(tmp_path):
    tree = make_run(tmp_path)
    record = make_approval_record()

    reference, result = tree.write_approval_record(record)

    assert result.reused is False
    assert isinstance(reference, ApprovalRecordReference)
    assert reference.relative_path == f"{RECEIPTS_DIR}/{reference.sha256}.json"
    assert tree.read_approval_record(reference) == record
    assert tree.build_manifest(DESIGNATOR)["artifacts"] == []


def test_identical_approval_record_reuses_its_immutable_bytes(tmp_path):
    tree = make_run(tmp_path)
    record = make_approval_record()

    first, first_result = tree.write_approval_record(record)
    second, second_result = tree.write_approval_record(record)

    assert first_result.reused is False
    assert second_result.reused is True
    assert second.to_record() == first.to_record()


def test_an_invalid_approval_record_is_refused_before_any_receipt_write(tmp_path):
    tree = make_run(tmp_path)
    record = make_approval_record(reason="edited after approval")

    with pytest.raises(ApprovalRefusal, match="self-hash"):
        tree.write_approval_record(record)

    assert not (tree.root / RECEIPTS_DIR).exists()


def test_an_approval_schema_is_refused_before_any_receipt_write(tmp_path):
    tree = make_run(tmp_path)
    record = make_approval_record(schema="approval-record.v9")
    record["self_hash"] = self_hash(record)

    with pytest.raises(ApprovalRefusal, match="schema"):
        tree.write_approval_record(record)

    assert not (tree.root / RECEIPTS_DIR).exists()


def test_an_approval_reference_must_name_the_bytes_and_path_it_claims(tmp_path):
    tree = make_run(tmp_path)
    reference, _ = tree.write_approval_record(make_approval_record())
    forged = ApprovalRecordReference(f"{RECEIPTS_DIR}/{'a' * 64}.json", reference.sha256)

    with pytest.raises(ApprovalRefusal, match="content-addressed path"):
        tree.read_approval_record(forged)


def test_an_approval_read_refuses_an_untyped_reference(tmp_path):
    tree = make_run(tmp_path)
    reference, _ = tree.write_approval_record(make_approval_record())

    with pytest.raises(ApprovalRefusal, match="ApprovalRecordReference"):
        tree.read_approval_record(reference.to_record())


def test_an_approval_reference_refuses_replaced_bytes(tmp_path):
    tree = make_run(tmp_path)
    reference, _ = tree.write_approval_record(make_approval_record())
    tree.resolve(reference.relative_path).write_bytes(b"{}")

    with pytest.raises(ApprovalRefusal, match="digest"):
        tree.read_approval_record(reference)


def test_a_self_hash_invalid_approval_record_is_refused_after_digest_checks(tmp_path):
    tree = make_run(tmp_path)
    record = make_approval_record(reason="edited after approval")
    data = canonical_bytes(record)
    digest = digest_bytes(data)
    relative_path = tree.receipt_path(digest)
    target = tree.resolve(relative_path)
    target.parent.mkdir(parents=True)
    target.write_bytes(data)

    with pytest.raises(ApprovalRefusal, match="self-hash"):
        tree.read_approval_record(ApprovalRecordReference(relative_path, digest))


# --- The door writes into the Exemplar's directory ----------------------------


def test_the_door_writes_where_the_exemplar_can_account_for_it(tmp_path):
    """The door owns no directory. Its refusals belong inside the record of what
    arrived, not in a drawer no downstream stage reads."""
    tree = make_run(tmp_path)
    assert tree.artifact_path(DOOR, "refusal", "art_0123456789abcdef").startswith("1_exemplar/")


# --- Paths cannot escape --------------------------------------------------------


def test_a_kind_or_identity_naming_a_directory_is_refused(tmp_path):
    tree = make_run(tmp_path)
    for bad in ("../escape", "a/b", "", ".", "..", ".hidden"):
        with pytest.raises(SchemaRefusal):
            tree.artifact_path(DESIGNATOR, bad, "art_0123456789abcdef")
        with pytest.raises(SchemaRefusal):
            tree.artifact_path(DESIGNATOR, "proposal", bad)


def test_resolving_a_path_outside_the_tree_is_refused(tmp_path):
    tree = make_run(tmp_path)
    for bad in ("/etc/passwd", "../r2/run.json", "1_exemplar/../../elsewhere"):
        with pytest.raises(SchemaRefusal):
            tree.resolve(bad)


def test_a_symlink_leading_out_of_the_tree_is_refused(tmp_path):
    """The `..` check cannot see this one: the path has no `..` in it, and only
    resolving it against the real filesystem shows where it lands.

    Containment is also asserted as a path relationship rather than a string
    prefix — with a root of `.../r1`, a prefix test accepts the sibling
    `.../r1-scratch`, which is a different run's tree.
    """
    outside = tmp_path / "r1-scratch"
    outside.mkdir()
    (outside / "stolen.json").write_text("{}", encoding="utf-8")

    tree = make_run(tmp_path)
    (tree.root / "1_exemplar").mkdir(parents=True, exist_ok=True)
    (tree.root / "1_exemplar" / "elsewhere").symlink_to(outside)

    with pytest.raises(SchemaRefusal) as caught:
        tree.resolve("1_exemplar/elsewhere/stolen.json")
    assert "outside the run tree" in str(caught.value)


# --- Manifests are derived ------------------------------------------------------


def test_a_manifest_describes_what_the_tree_actually_holds(tmp_path):
    tree = make_run(tmp_path)
    envelope = make_envelope()
    tree.publish_artifact(envelope)
    tree.put_blob(DESIGNATOR, b"a crop")

    manifest = tree.build_manifest(DESIGNATOR)

    assert len(manifest["artifacts"]) == 1
    assert manifest["artifacts"][0]["artifact_id"] == envelope["artifact_id"]
    assert manifest["artifacts"][0]["outcome"] == "proposed"
    assert manifest["blobs"] == [digest_bytes(b"a crop")]


def test_a_deleted_manifest_rebuilds_identically(tmp_path):
    """A manifest is a rebuildable inventory, never the only evidence that
    something happened."""
    tree = make_run(tmp_path)
    tree.publish_artifact(make_envelope())
    tree.write_manifest(DESIGNATOR)
    stored = tree.read_bytes(tree.manifest_path(DESIGNATOR))

    tree.resolve(tree.manifest_path(DESIGNATOR)).unlink()
    tree.write_manifest(DESIGNATOR)

    assert tree.read_bytes(tree.manifest_path(DESIGNATOR)) == stored


def test_a_stale_manifest_is_detectable(tmp_path):
    """If the manifest disagrees with the artifacts, the artifacts are right. The
    point of the check is that the disagreement is visible rather than silent."""
    tree = make_run(tmp_path)
    tree.publish_artifact(make_envelope())
    tree.write_manifest(DESIGNATOR)
    assert tree.manifest_agrees_with_disk(DESIGNATOR) is True

    tree.publish_artifact(make_envelope(subject="pg_fedcba9876543210"))
    assert tree.manifest_agrees_with_disk(DESIGNATOR) is False

    tree.write_manifest(DESIGNATOR)
    assert tree.manifest_agrees_with_disk(DESIGNATOR) is True


def test_a_missing_manifest_does_not_pass_as_agreeing(tmp_path):
    tree = make_run(tmp_path)
    tree.publish_artifact(make_envelope())
    assert tree.manifest_agrees_with_disk(DESIGNATOR) is False


def test_an_empty_stage_manifest_is_honest_rather_than_absent(tmp_path):
    tree = make_run(tmp_path)
    manifest = tree.build_manifest(DESIGNATOR)
    assert manifest["artifacts"] == []
    assert manifest["blobs"] == []


# --- Inventory scope: harvest invariant #13 ------------------------------------


def test_every_path_the_store_can_write_is_inside_the_inventory_scope(tmp_path):
    """Harvest #13: every managed output path any code can write must resolve
    inside the inventory scope; adding a managed path without extending the scope
    fails a static drift test, loudly, naming the path.

    Driven against real writes rather than a list of strings, so a new writer that
    forgot to extend the scope is caught by what it actually does. Spec 03 adds
    the approval record, System 09 the Recensor partition receipt, and spec 10
    the rebuildable stage index; each is exercised here through its real writer
    rather than a guessed path.
    """
    tree = make_run(tmp_path)
    scope = tree.inventory_scope()

    written: list[str] = [RUN_FILE]
    written.append(tree.publish_artifact(make_envelope()).relative_path)
    written.append(tree.put_blob(DESIGNATOR, b"a crop")[1].relative_path)
    written.append(tree.write_manifest(DESIGNATOR).relative_path)
    written.append(tree.write_index(DESIGNATOR, {"schema": "test-index", "rows": []}).relative_path)
    written.append(tree.write_run_receipt(make_receipt())[0].relative_path)
    written.append(tree.write_approval_record(make_approval_record())[0].relative_path)
    written.append(
        tree.write_recensor_partition_receipt(make_recensor_partition_receipt()).relative_path
    )
    # Not just "in scope" like every other entry below -- the receipt is a
    # replace-in-place write, so its exact published location is worth
    # pinning against the module's own named constant rather than only its
    # source text (which `test_no_store_writer_reaches_a_path_the_inventory_
    # scope_cannot_name` checks separately, for a different reason).
    assert written[-1] == runtree_store.RECENSOR_PARTITION_RECEIPT_FILE

    assert len(written) == 8
    for path in written:
        assert any(path == prefix or path.startswith(prefix) for prefix in scope), (
            f"{path} is written by the store but falls outside the inventory scope"
        )


def test_no_store_writer_reaches_a_path_the_inventory_scope_cannot_name():
    """The static half of harvest #13, read from source rather than from a fixture.

    The runtime test above proves the eight writers we know about stay in scope.
    It cannot prove that a *ninth* writer added later was exercised at all — an
    un-called writer leaves no trace to check. So this reads every immutable
    publication in `RunTree` and requires it to route through one of the path
    constructors `inventory_scope()` is derived from. A new writer that invents a
    path fails here even though no test calls it.
    """
    source = inspect.getsource(runtree_store.RunTree)
    constructors = set(re.findall(r"self\._publish_bytes\(\s*self\.(\w+)\(", source))
    indirect = set(
        re.findall(r"(\w+)\s*=\s*self\.(?:artifact_path|manifest_path|index_path)\(", source)
    )
    passed_through = set(re.findall(r"self\._publish_bytes\(\s*(\w+)\s*,", source))

    assert constructors <= {"blob_path", "receipt_path"}, (
        f"a store writer publishes through unknown path constructor(s) {sorted(constructors)}; "
        "inventory_scope() is derived from artifact_path/blob_path/manifest_path/index_path/"
        "receipt_path and cannot name another"
    )
    receipt_writer = inspect.getsource(runtree_store.RunTree.write_recensor_partition_receipt)
    assert (
        "recensor_partition_receipt_path" in receipt_writer and "_atomic_write" in receipt_writer
    ), (
        "the mutable Recensor partition receipt must use its named run-health path and atomic "
        "publication; a replace-in-place writer is invisible to the _publish_bytes scan above, "
        "so this is the only thing that keeps it from becoming a hidden unscoped write"
    )
    index_writer = inspect.getsource(runtree_store.RunTree.write_index)
    assert "index_path" in index_writer and "_atomic_write" in index_writer, (
        "the rewritable derived index must use its named path constructor and atomic "
        "publication; write_index is the third replace-in-place writer the _publish_bytes "
        "scan above cannot see, so this is what keeps it from becoming a hidden unscoped write"
    )
    assert passed_through <= indirect, (
        "a store writer publishes bytes at a path that did not come from "
        "artifact_path(), manifest_path() or index_path(); harvest #13 requires every managed "
        f"path to be one the inventory scope can name (found {sorted(passed_through - indirect)})"
    )
    assert constructors and passed_through, (
        "no publication sites were found at all — this test would pass vacuously, "
        "which is the false green meta-invariant #88 refuses"
    )


def test_a_rebuildable_index_may_be_replaced(tmp_path):
    """An index may be rewritten. That the artifacts it summarizes may not be
    is proven by the write-once tests above, not named here and left unmeasured."""
    tree = make_run(tmp_path)
    first = {"schema": "test-index", "rows": [{"act_id": "a1"}]}
    tree.write_index(DESIGNATOR, first)
    assert tree.index_path(DESIGNATOR).endswith(f"/{INDEX_FILE}")
    assert tree.read_index(DESIGNATOR) == first

    second = {"schema": "test-index", "rows": [{"act_id": "a1"}, {"act_id": "a2"}]}
    tree.write_index(DESIGNATOR, second)
    assert tree.read_index(DESIGNATOR) == second


@pytest.mark.parametrize("stage", [DOOR, EXEMPLAR])
def test_a_stage_sharing_its_directory_cannot_write_an_index(tmp_path, stage):
    """Door and Exemplar share one directory, so one index file cannot account
    for both producers: the second writer would silently erase the first's rows
    and `read_index` would return a complete-looking summary of one of two
    stages. Refused at the write, not documented and left as a trap."""
    tree = make_run(tmp_path)
    with pytest.raises(SchemaRefusal, match="shares run-tree directory"):
        tree.write_index(stage, {"schema": "test-index", "rows": []})


def test_a_derived_index_that_is_not_an_object_is_refused(tmp_path):
    tree = make_run(tmp_path)
    with pytest.raises(SchemaRefusal, match="must be an object"):
        tree.write_index(DESIGNATOR, ["not", "an", "object"])


def test_a_derived_index_that_is_not_canonically_serializable_is_refused(tmp_path):
    """A measured ratio in a row must be a named refusal, not a traceback out of
    canonical_bytes: a stage that dies here takes every act's accounting with it."""
    tree = make_run(tmp_path)
    with pytest.raises(SchemaRefusal, match="canonically serializable"):
        tree.write_index(DESIGNATOR, {"schema": "test-index", "coverage": 0.5})


def test_a_self_referencing_derived_index_is_refused(tmp_path):
    tree = make_run(tmp_path)
    index: dict = {"schema": "test-index"}
    index["rows"] = [index]
    with pytest.raises(SchemaRefusal, match="canonically serializable"):
        tree.write_index(DESIGNATOR, index)


def test_a_stored_index_that_is_not_an_object_is_refused_on_the_way_out(tmp_path):
    """The read half of the same refusal: a hand-edited index.json holding a
    JSON array is refused, not handed to a consumer as an index."""
    tree = make_run(tmp_path)
    tree.write_index(DESIGNATOR, {"schema": "test-index", "rows": []})
    tree.resolve(tree.index_path(DESIGNATOR)).write_bytes(b'["not","an","object"]')
    with pytest.raises(SchemaRefusal, match="not an object"):
        tree.read_index(DESIGNATOR)


def test_the_inventory_scope_covers_every_producer(tmp_path):
    """A stage added later without a scope entry would write outside the inventory
    and be invisible to it."""
    from common.contracts.stages import WRITING_DIRECTORIES

    scope = make_run(tmp_path).inventory_scope()
    for directory in set(WRITING_DIRECTORIES.values()):
        assert any(prefix.startswith(f"{directory}/") for prefix in scope)


# --- Atomic publication ---------------------------------------------------------


def test_publication_leaves_no_temporary_files_behind(tmp_path):
    tree = make_run(tmp_path)
    tree.publish_artifact(make_envelope())
    tree.write_manifest(DESIGNATOR)
    leftovers = [path.name for path in (tmp_path / "r1").rglob(".*tmp*")]
    assert leftovers == []


def test_first_publication_never_overwrites_a_competing_writer(tmp_path, monkeypatch):
    tree = make_run(tmp_path)
    envelope = make_envelope()
    target = tree.resolve(tree.artifact_path(DESIGNATOR, "proposal", envelope["artifact_id"]))
    original_link = runtree_store.os.link

    def competing_link(source, destination, *args, **kwargs):
        Path(destination).write_bytes(b"competing bytes")
        return original_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(runtree_store.os, "link", competing_link)

    with pytest.raises(IncompatibleReuse, match="already holds different bytes"):
        tree.publish_artifact(envelope)

    assert target.read_bytes() == b"competing bytes"


@pytest.mark.parametrize("code", [errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS])
def test_a_filesystem_that_refuses_hard_links_is_named_not_a_raw_oserror(
    tmp_path, monkeypatch, code
):
    """Publication is an atomic `os.link`, so the run root has to be on a filesystem
    that supports one — exFAT, FAT32, some network mounts and some container bind
    mounts do not. Those answer `EPERM`, `EOPNOTSUPP` or `ENOSYS`, which escaped as a
    bare `OSError` and surfaced as a traceback about `link` rather than as a statement
    about where the run root was put. It is a setup fact, so it is named as one.
    """
    tree = make_run(tmp_path)

    def refusing_link(source, destination, *args, **kwargs):
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(runtree_store.os, "link", refusing_link)

    with pytest.raises(SchemaRefusal, match="refuses hard links"):
        tree.publish_artifact(make_envelope())


def test_an_existing_target_is_still_a_reuse_check_not_a_hard_link_complaint(tmp_path):
    """`FileExistsError` is an `OSError` too, and the translation above must not
    swallow it: an identical republication is a true no-op, not a filesystem fault."""
    tree = make_run(tmp_path)
    envelope = make_envelope()
    tree.publish_artifact(envelope)

    assert tree.publish_artifact(envelope).reused is True


def test_the_run_file_is_valid_json_a_human_can_read(tmp_path):
    make_run(tmp_path)
    assert json.loads((tmp_path / "r1" / RUN_FILE).read_text(encoding="utf-8"))["run_id"] == "r1"


@pytest.mark.parametrize(
    "damage_kind",
    [
        "empty",
        "truncated-json",
        "wrong-schema",
        "non-utf8",
        "float",
        "wrong-self-hash",
        "deeply-nested",
    ],
)
def test_a_damaged_partition_receipt_does_not_block_the_valid_one_replacing_it(
    tmp_path, damage_kind
):
    """The receipt is derived, not evidence, so damage must not be a dead end.

    It is reconstructed from the immutable review and request records beside it,
    and it is explicitly replaced in place rather than published as an immutable
    artifact. Validating the *existing* file before writing the new one meant a
    torn write, a truncated file, or a receipt from an older schema left the run
    permanently unable to record a partition it could recompute perfectly well.
    GOVERNANCE 4 protects evidence; this is not evidence, and the refusal
    protected nothing while blocking recovery.

    The refusal that *does* matter — a valid receipt disagreeing about the sealed
    proposal-act denominator — is pinned by the test above and is unaffected.
    Found by CodeRabbit.
    """
    tree = make_run(tmp_path)
    receipt = make_recensor_partition_receipt()
    assert tree.write_recensor_partition_receipt(receipt).reused is False

    target = tree.resolve(tree.recensor_partition_receipt_path())
    # Six shapes reaching four different refusal paths, not four reaching two: an
    # empty file and a truncated one both fail the JSON reader, so the first draft
    # of this test looked broader than it was. The float and wrong-self-hash cases
    # both reach `verify_self_hash`; the float is refused by strict canonicalization
    # with the `TypeError` class that escaped the first fix, while the latter pins
    # the validator's own integrity refusal.
    valid = json.dumps(receipt).encode("utf-8")
    float_damaged = json.loads(valid)
    float_damaged["expected_act_count"] = 1.0
    self_hash_damaged = json.loads(valid)
    self_hash_damaged["self_hash"] = "0" * 64
    damage = {
        "empty": b"",
        "truncated-json": b"{",
        "wrong-schema": b'{"schema": "nonsense"}',
        "non-utf8": b"\xff\xfe not utf-8",
        "float": json.dumps(float_damaged).encode("utf-8"),
        "wrong-self-hash": json.dumps(self_hash_damaged).encode("utf-8"),
        # The `RecursionError` branch of the writer's except clause, which
        # nothing else drives. `json.loads` really does raise it rather than a
        # `ValueError` at this depth (measured here: 100,000 levels still parse,
        # 200,000 raise), which is precisely why `_read_json` cannot translate
        # it and why it is named separately in that clause. Without this case
        # that branch was untested and could have been deleted green.
        # Found by CodeRabbit.
        "deeply-nested": (b"[" * 200_000) + (b"]" * 200_000),
    }[damage_kind]
    target.write_bytes(damage)
    assert tree.write_recensor_partition_receipt(receipt).reused is False, (
        f"a receipt damaged as {damage_kind} blocked its own replacement"
    )
    assert tree.read_recensor_partition_receipt()["run_id"] == "r1"


def test_an_artifact_too_deeply_nested_for_the_json_reader_is_refused_not_a_crash(tmp_path):
    """`_read_json` names the ways a file can fail to be read, and `RecursionError`
    was not among them: `json`'s scanner recurses once per nesting level, so a
    deeply nested artifact raised straight through every caller. A stage that
    should have refused the file and held died with a traceback instead, and
    because `build_manifest` reads every artifact under a directory, one such
    file stopped the whole stage rather than its own record. 30,000 is driven
    deliberately deep rather than pinned to the scanner's exact failure depth,
    which is an interpreter fact, not one this suite should assert.

    **Which refusal fires is that same interpreter fact, so it is not asserted
    either.** This test pinned the reader's own message and passed on Python 3.12
    and 3.13 while failing on 3.14, where the scanner absorbs this depth: the file
    then parses cleanly and is refused one step later for the fields it does not
    have. Both are refusals and neither is a traceback, which is the whole of what
    this test exists to prove. Pinning the message asserted the mechanism instead
    of the guarantee, and the mechanism belongs to CPython. Found by running the
    gate on a 3.14 host after the rebase; the chambers run 3.13 and CI runs 3.12,
    so nothing else in the ladder would have shown it."""
    tree = make_run(tmp_path)
    envelope = make_envelope()
    tree.publish_artifact(envelope)
    path = tree.resolve(tree.artifact_path(DESIGNATOR, "proposal", envelope["artifact_id"]))
    nesting = 30_000
    deep_text = f'{{"deep": {"[" * nesting}"leaf"{"]" * nesting}}}'
    path.write_text(deep_text, encoding="utf-8")

    # Without this premise the assertion below can pass through the missing-field
    # refusal alone, leaving the reader-side RecursionError guard uncovered.
    try:
        json.loads(deep_text)
    except RecursionError:
        pass
    else:
        pytest.skip(
            f"this interpreter's JSON scanner absorbs {nesting} levels, so the "
            "guarded path is unreachable here and this test proves nothing"
        )

    # Both refusals named, rather than any `SchemaRefusal` at all: the point is
    # that one of two known doors closes, not that something somewhere objected.
    with pytest.raises(SchemaRefusal, match="could not be read as an artifact|missing required"):
        tree.build_manifest(DESIGNATOR)


def test_an_artifact_parseable_but_too_deep_for_its_self_hash_walk_is_refused_not_a_crash(
    tmp_path,
):
    """A second, deeper band of the same defect the test above pins.

    `_read_json`'s guard protects `json.loads`, whose C scanner tolerates far
    deeper nesting than the pure-Python walk `canonical_bytes` makes to refuse
    floats ahead of hashing. A record shallow enough to parse cleanly but deep
    enough to exhaust the recursion limit during that second walk reached
    `verify_self_hash` and crashed one call past where the reader-side guard
    already closed the door — found by blind audit against this same tree
    (two independent audits, one depth each) rather than by this suite.
    """
    tree = make_run(tmp_path)
    envelope = make_envelope()
    tree.publish_artifact(envelope)
    path = tree.resolve(tree.artifact_path(DESIGNATOR, "proposal", envelope["artifact_id"]))

    # Built as raw text and spliced in, rather than handed to `json.dumps` as a
    # 2,000-deep object. The encoder recurses per level exactly as the scanner
    # does, so constructing the fixture that way makes the *setup* depend on the
    # interpreter's recursion limit — and a fixture that raises during setup is a
    # false failure reporting nothing about the code. Flagged by CodeRabbit on the
    # rebased branch, and the same family as the interpreter dependence that broke
    # the test above on a 3.14 host.
    nesting = 2000
    deep_text = '{"nested": ' * nesting + '"leaf"' + "}" * nesting
    tampered = dict(envelope)
    tampered["payload"] = {"deep": "__DEEP__"}
    tampered["self_hash"] = "0" * 64
    path.write_text(
        json.dumps(tampered).replace('"__DEEP__"', deep_text),
        encoding="utf-8",
    )

    # **This test would go vacuous rather than red on an interpreter whose walk
    # absorbs 2,000 levels**, because the deliberately wrong `self_hash` earns the
    # same refusal whether or not the deep walk was ever the thing that failed. A
    # test that stops testing without saying so is worse than one that breaks, and
    # a skip is visible where a silent pass is not (GOVERNANCE 2). So the premise
    # is asserted first, against the same walk the code uses. Found by the Opus
    # read of this branch.
    try:
        parsed_deep = json.loads(deep_text)
    except RecursionError:
        pytest.skip(
            f"this interpreter's JSON scanner cannot parse {nesting} levels, so this "
            "case would exercise the reader-side guard instead of the canonical walk"
        )
    try:
        canonical_bytes(parsed_deep)
    except RecursionError:
        pass
    else:
        pytest.skip(
            f"this interpreter's canonical walk absorbs {nesting} levels, so the "
            "guarded path is unreachable here and this test proves nothing"
        )

    with pytest.raises(SchemaRefusal, match="fails its self-hash"):
        tree.build_manifest(DESIGNATOR)
