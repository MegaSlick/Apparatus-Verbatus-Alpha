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

import inspect
import json
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
from common.contracts.stages import DESIGNATOR, DOOR, EXEMPLAR
from common.runtree import store as runtree_store
from common.runtree.store import RECEIPTS_DIR, RUN_FILE, RunTree

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
        subject_ids=["data-handling-policy"],
        action="data-gate",
        reason="approved for this exact synthetic policy",
        target_version_hash="b" * 64,
        timestamp="2026-08-04T12:00:00Z",
    )
    record.update(overrides)
    return record


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
        make_run(
            tmp_path,
            ingress={
                "mode": "approval-gated-real",
                "data_gate_policy_hash": "a" * 64,
                "data_gate_approval_ref": {
                    "relative_path": f"{RECEIPTS_DIR}/{'b' * 64}.json",
                    "sha256": "b" * 64,
                },
            },
        )


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
    forgot to extend the scope is caught by what it actually does. Spec 03 adds a
    sixth real write — the approval record — and it is exercised here for the same
    reason as the other five, by writing one.
    """
    tree = make_run(tmp_path)
    scope = tree.inventory_scope()

    written: list[str] = [RUN_FILE]
    written.append(tree.publish_artifact(make_envelope()).relative_path)
    written.append(tree.put_blob(DESIGNATOR, b"a crop")[1].relative_path)
    written.append(tree.write_manifest(DESIGNATOR).relative_path)
    written.append(tree.write_run_receipt(make_receipt())[0].relative_path)
    written.append(tree.write_approval_record(make_approval_record())[0].relative_path)

    assert len(written) == 6
    for path in written:
        assert any(path == prefix or path.startswith(prefix) for prefix in scope), (
            f"{path} is written by the store but falls outside the inventory scope"
        )


def test_no_store_writer_reaches_a_path_the_inventory_scope_cannot_name():
    """The static half of harvest #13, read from source rather than from a fixture.

    The runtime test above proves the six writers we know about stay in scope. It
    cannot prove that a *seventh* writer added later was exercised at all — an
    un-called writer leaves no trace to check. So this reads every immutable
    publication in `RunTree` and requires it to route through one of the path
    constructors `inventory_scope()` is derived from. A new writer that invents a
    path fails here even though no test calls it.
    """
    source = inspect.getsource(runtree_store.RunTree)
    constructors = set(re.findall(r"self\._publish_bytes\(\s*self\.(\w+)\(", source))
    indirect = set(re.findall(r"(\w+)\s*=\s*self\.(?:artifact_path|manifest_path)\(", source))
    passed_through = set(re.findall(r"self\._publish_bytes\(\s*(\w+)\s*,", source))

    assert constructors <= {"blob_path", "receipt_path"}, (
        f"a store writer publishes through unknown path constructor(s) {sorted(constructors)}; "
        "inventory_scope() is derived from artifact_path/blob_path/manifest_path/receipt_path "
        "and cannot name a fifth"
    )
    assert passed_through <= indirect, (
        "a store writer publishes bytes at a path that did not come from "
        "artifact_path() or manifest_path(); harvest #13 requires every managed "
        f"path to be one the inventory scope can name (found {sorted(passed_through - indirect)})"
    )
    assert constructors and passed_through, (
        "no publication sites were found at all — this test would pass vacuously, "
        "which is the false green meta-invariant #88 refuses"
    )


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


def test_the_run_file_is_valid_json_a_human_can_read(tmp_path):
    make_run(tmp_path)
    assert json.loads((tmp_path / "r1" / RUN_FILE).read_text(encoding="utf-8"))["run_id"] == "r1"
