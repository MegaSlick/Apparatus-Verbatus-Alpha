"""The run tree's three promises, each asserted in both directions.

A seal that stops refusing bad things by refusing good things too is not a fix, so
every refusal here has an acceptance beside it: identical bytes are reused *and*
different bytes are refused; an unchanged run resumes *and* a changed one does not.

These write real files to a real temporary directory through the real store; there
is no in-memory stand-in, because the properties under test are properties of the
filesystem behaviour.
"""

import errno
import hashlib
import inspect
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from common import armarium_formats
from common.chairs.models import ChairIdentity, ServingDetails
from common.chairs.receipts import build_receipt
from common.contracts.approval import ApprovalRecordReference, build_approval_record
from common.contracts.canonical import canonical_bytes, digest_bytes, self_hash
from common.contracts.envelope import build_envelope, read_verified
from common.contracts.errors import ApprovalRefusal, IncompatibleReuse, SchemaRefusal
from common.contracts.identities import artifact_id
from common.contracts.stages import (
    ARMARIUM,
    DESIGNATOR,
    DOOR,
    EXEMPLAR,
    PERLECTOR,
    writing_directory,
)
from common.corpus_register import EMPTY_REGISTER_DIGEST, empty_register
from common.recensor_receipt import build_recensor_reading_receipt
from common.runtree import store as runtree_store
from common.runtree.store import (
    ARTIFACTS_DIR,
    BLOBS_DIR,
    INDEX_FILE,
    RECEIPTS_DIR,
    RUN_FILE,
    RunTree,
    _default_corpus_frame_membership,
)
from conftest import tree_snapshot

PAGE_BYTES = b"synthetic page one"
SOURCE = [{"relative_path": "proof/page-1.png", "sha256": digest_bytes(PAGE_BYTES), "ordinal": 1}]
CONFIG_DIGEST = "c" * 64
RECIPES = {"designator": "fake-designator-v0"}
CHAIRS = ["attestator_1", "attestator_2", "attestator_3"]


def _stray_writes(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """The paths a refusal moved, named -- a count would not say which file to look at."""
    return sorted(
        name for name in before.keys() | after.keys() if before.get(name) != after.get(name)
    )


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
        # A pin, not a label: `common/chairs/receipts.py` refuses a mutable name here
        # on the same grounds `common/chairs/config.py` refuses a branch name for the
        # model revision.
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


COMMIT = "a1b2c3d4" * 5


def _receipt_item(act_id: str = "act-1", act_key: str = "p1:1") -> dict:
    return {
        "act_id": act_id,
        "act_key": act_key,
        "page_disposition": "read",
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
            "health_unrecorded": 0,
            "shortfalls": {"failed": 0, "truncated": 0, "unaligned": 0},
        },
        "release_reason": None,
    }


def make_recensor_partition_receipt(
    items: list[dict] | None = None,
    config_digest: str = CONFIG_DIGEST,
    reading: str = "4_perlector/artifacts/page-reading.json",
):
    return build_recensor_reading_receipt(
        run_id="r1",
        config_digest=config_digest,
        pages=[
            {
                "page_ordinal": 1,
                "reading_ref": {"relative_path": reading, "sha256": "a" * 64},
                "reask_ref": None,
                "accounting_ref": {
                    "relative_path": "4_perlector/artifacts/page-accounting.json",
                    "sha256": "b" * 64,
                },
                "reask": None,
            }
        ],
        items=[_receipt_item()] if items is None else items,
    )


# --- The Recensor partition receipt: replace in place, but never a shrinking
# --- denominator under the same run authority ----------------------------------


def test_a_write_that_would_shrink_the_expected_unit_count_is_refused(tmp_path):
    """The unit denominator is sealed once by the page readings; two honest
    Recensor passes over the same run can never legitimately disagree about how
    many units it names. A write that would shrink it is not a fresher partition
    superseding a stale one -- it is a different, inconsistent claim about the
    same sealed denominator, and is refused rather than silently accepted as
    whichever write happened to land last."""
    tree = make_run(tmp_path)
    two_items = make_recensor_partition_receipt([_receipt_item(), _receipt_item("act-2", "p1:2")])
    tree.write_recensor_partition_receipt(two_items)

    with pytest.raises(SchemaRefusal, match="expected_unit_count"):
        tree.write_recensor_partition_receipt(make_recensor_partition_receipt())

    # The two-item receipt already on disk survives the refused write untouched.
    assert tree.read_recensor_partition_receipt()["expected_unit_count"] == 2


def test_a_write_that_grows_the_expected_unit_count_is_also_refused(tmp_path):
    """Grown or shrunk, either direction disagrees with an already-sealed
    denominator, so neither is treated as the fresher one."""
    tree = make_run(tmp_path)
    tree.write_recensor_partition_receipt(make_recensor_partition_receipt())

    grown = make_recensor_partition_receipt([_receipt_item(), _receipt_item("act-2", "p1:2")])
    with pytest.raises(SchemaRefusal, match="expected_unit_count"):
        tree.write_recensor_partition_receipt(grown)


def test_the_count_may_change_when_a_page_is_bound_to_another_current_reading(tmp_path):
    """An operator re-read gives its page a new current reading, which may count other
    units: a receipt binding that reading may change the count. One binding the same
    readings still may not."""
    tree = make_run(tmp_path)
    tree.write_recensor_partition_receipt(make_recensor_partition_receipt())
    reread = make_recensor_partition_receipt(
        [_receipt_item(), _receipt_item("act-2", "p1:2")],
        reading="4_perlector/artifacts/page-reading-attempt-3.json",
    )
    tree.write_recensor_partition_receipt(reread)
    assert tree.read_recensor_partition_receipt()["expected_unit_count"] == 2
    with pytest.raises(SchemaRefusal, match="over the same page readings"):
        tree.write_recensor_partition_receipt(
            make_recensor_partition_receipt(
                reading="4_perlector/artifacts/page-reading-attempt-3.json"
            )
        )


def test_repeating_an_identical_receipt_is_replaced_not_refused(tmp_path):
    tree = make_run(tmp_path)
    receipt = make_recensor_partition_receipt()
    tree.write_recensor_partition_receipt(receipt)
    path = tree.resolve(tree.recensor_partition_receipt_path())
    first_inode = path.stat().st_ino
    result = tree.write_recensor_partition_receipt(receipt)
    assert result.reused is False
    assert path.stat().st_ino != first_inode


# --- The run authority ---------------------------------------------------------


def test_creating_a_run_writes_a_self_hashed_authority(tmp_path):
    tree = make_run(tmp_path)
    record = tree.read_run()
    assert record["run_id"] == "r1"
    assert record["witness_chairs"] == CHAIRS
    assert record["source_manifest"] == SOURCE
    assert (tmp_path / "r1" / RUN_FILE).exists()


def test_run_authority_seals_a_content_addressed_corpus_register_snapshot(tmp_path):
    tree = make_run(tmp_path)
    run = tree.read_run()
    digest = run["register_digest"]
    assert run["register_required"] is False
    assert tree.read_bytes(tree.blob_path(DOOR, digest)) == empty_register()


def test_run_authority_distinguishes_an_explicit_empty_register_from_no_register(tmp_path):
    tree = make_run(tmp_path, register_bytes=empty_register())
    run = tree.read_run()
    assert run["register_digest"] == EMPTY_REGISTER_DIGEST
    assert run["register_required"] is True


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
    """Reusing a run ID with changed source/config/adapter revision fails before
    writing."""
    make_run(tmp_path)
    changed = [{"relative_path": "proof/page-1.png", "sha256": "b" * 64, "ordinal": 1}]
    with pytest.raises(IncompatibleReuse) as caught:
        make_run(tmp_path, source_manifest=changed)
    assert "source_manifest" in str(caught.value)


def test_a_source_manifest_repeating_an_ordinal_is_refused(tmp_path):
    """An ordinal names one page. Two rows sharing one leave the run unable to say
    how many pages arrived, and the Armarium's page census compares itself against
    these ordinals as a set, so the repeat would reduce two declared pages to one
    and let a run that lost one of them still reconcile as `complete`."""
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
    only thing wrong with it, and a digest check ahead of the ordinal check can
    never make this test pass for the wrong reason."""
    for bad in (
        {"relative_path": "p.png", "sha256": "a" * 64},
        {"sha256": "a" * 64, "ordinal": "1"},
        {"sha256": "a" * 64, "ordinal": True},
    ):
        with pytest.raises(SchemaRefusal):
            make_run(tmp_path, source_manifest=[{"relative_path": "p.png", **bad}])


def test_a_source_page_ordinal_below_one_is_refused(tmp_path):
    """An ordinal is a page number, and page numbers start at one here.

    The integer check above accepts 0 and -1, which every producer in this tree
    is incapable of writing: the Door increments before it assigns, and the
    fixture declarations follow it. Accepting one anyway seals it into the
    corpus frame membership, which gold re-derives from the same field, so the
    run would carry a page numbering nothing else in the system shares.

    Each row below is otherwise well formed, so the ordinal is the only thing
    the refusal can be about, and the message names the offending values rather
    than only the rule -- an operator holding a hand-built manifest needs to
    know which row to fix.
    """
    for bad_ordinal in (0, -1, -12):
        with pytest.raises(SchemaRefusal, match="counted from one"):
            make_run(
                tmp_path,
                source_manifest=[
                    {"relative_path": "p.png", "sha256": "a" * 64, "ordinal": bad_ordinal}
                ],
            )

    # A good row beside a bad one is still refused: the check is over the
    # manifest, not over whichever row happens to be read first.
    with pytest.raises(SchemaRefusal, match=r"ordinal\(s\) \[0\]"):
        make_run(
            tmp_path,
            source_manifest=[
                {"relative_path": "p1.png", "sha256": "a" * 64, "ordinal": 1},
                {"relative_path": "p0.png", "sha256": "b" * 64, "ordinal": 0},
            ],
        )


def test_a_well_formed_manifest_of_several_pages_is_still_accepted(tmp_path):
    """The refusals above do not buy their strictness by refusing good input too."""
    fine = [
        {"relative_path": "proof/page-1.png", "sha256": "a" * 64, "ordinal": 1},
        {"relative_path": "proof/page-2.png", "sha256": "b" * 64, "ordinal": 2},
    ]
    tree = make_run(tmp_path, source_manifest=fine)
    assert [page["ordinal"] for page in tree.read_run()["source_manifest"]] == [1, 2]


def test_door_computed_page_digests_distinguish_shards_with_the_same_ordinals(tmp_path):
    """Membership is about inspected bytes, not an ordinal-shaped page set."""
    first = make_run(
        tmp_path / "first",
        source_manifest=[
            {
                "relative_path": "first.png",
                "sha256": "a" * 64,
                "computed_sha256": "b" * 64,
                "ordinal": 1,
            }
        ],
    )
    second = make_run(
        tmp_path / "second",
        source_manifest=[
            {
                "relative_path": "second.png",
                "sha256": "a" * 64,
                "computed_sha256": "c" * 64,
                "ordinal": 1,
            }
        ],
    )
    assert (
        first.read_run()["corpus_frame_membership"] != second.read_run()["corpus_frame_membership"]
    )


def test_door_computed_page_digests_agree_for_the_same_bytes_at_the_same_ordinal(tmp_path):
    """Run identity and source path must not salt otherwise identical membership."""
    first = make_run(
        tmp_path / "first",
        run_id="shard-one",
        source_manifest=[
            {
                "relative_path": "first.png",
                "sha256": "a" * 64,
                "computed_sha256": "b" * 64,
                "ordinal": 1,
            }
        ],
    )
    second = make_run(
        tmp_path / "second",
        run_id="shard-two",
        source_manifest=[
            {
                "relative_path": "second.png",
                "sha256": "a" * 64,
                "computed_sha256": "b" * 64,
                "ordinal": 1,
            }
        ],
    )
    assert (
        first.read_run()["corpus_frame_membership"] == second.read_run()["corpus_frame_membership"]
    )


def test_membership_refuses_a_page_with_no_digest_of_any_kind(tmp_path):
    with pytest.raises(SchemaRefusal, match="inspected or declared sha256"):
        make_run(tmp_path, source_manifest=[{"relative_path": "page.png", "ordinal": 1}])


def test_membership_uses_the_declaration_when_computed_digest_is_explicitly_absent(tmp_path):
    manifest = [
        {
            "relative_path": "unreadable.png",
            "sha256": "a" * 64,
            "computed_sha256": None,
            "ordinal": 1,
        }
    ]
    tree = make_run(tmp_path, source_manifest=manifest)
    without_optional_field = [
        {key: value for key, value in manifest[0].items() if key != "computed_sha256"}
    ]
    assert tree.read_run()["corpus_frame_membership"] == _default_corpus_frame_membership(
        without_optional_field
    )


def test_a_caller_cannot_name_a_frame_its_own_pages_do_not_derive(tmp_path):
    """A caller-supplied membership would bypass derivation from the source pages."""
    assert "corpus_frame_membership" not in inspect.signature(RunTree.create).parameters
    tree = make_run(tmp_path / "derived")
    assert tree.read_run()["corpus_frame_membership"] == _default_corpus_frame_membership(SOURCE)


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


@pytest.mark.parametrize(
    ("overrides", "match"),
    (
        ({"config_digest": "not-a-digest"}, "config_digest must be a lowercase sha256"),
        ({"config_digest": "C" * 64}, "config_digest must be a lowercase sha256"),
        ({"ingress": {"mode": "maybe"}}, "closed fixture-or-real record"),
        ({"ingress": {"mode": "real", "extra": 1}}, "closed fixture-or-real record"),
    ),
)
def test_a_run_authority_every_later_read_would_refuse_is_never_sealed(tmp_path, overrides, match):
    """Refused at creation, before anything is written, not sealed and refused forever."""
    with pytest.raises(SchemaRefusal, match=match):
        make_run(tmp_path, **overrides)
    assert not (tmp_path / "r1").exists()


def test_resuming_under_a_different_commit_keeps_the_run_and_its_first_commit(tmp_path):
    """A run id names inputs and configuration, not a build: a resume after a fix
    is the same run, and the authority keeps the commit that created it."""
    make_run(tmp_path, repository_commit=COMMIT)
    resumed = make_run(tmp_path, repository_commit="f" * 40)

    assert resumed.read_run()["repository_commit"] == COMMIT


def test_reusing_a_run_id_with_changed_ingress_evidence_is_refused(tmp_path):
    """A run cannot turn a declared real ingress into a fixture on reuse."""
    make_run(tmp_path, ingress={"mode": "synthetic-fixture"})

    with pytest.raises(IncompatibleReuse, match="ingress"):
        make_run(tmp_path, ingress={"mode": "real"})


@pytest.mark.parametrize(
    ("overrides", "field"),
    (
        ({"config_digest": "d" * 64}, "config_digest"),
        ({"adapter_recipes": {"designator": "fake-designator-v1"}}, "adapter_recipes"),
        ({"witness_chairs": ["attestator_1", "attestator_2"]}, "witness_chairs"),
    ),
)
def test_an_incompatible_reuse_writes_nothing(tmp_path, overrides, field):
    """The refusal's own sentence, measured against the tree rather than read.

    "this is a different run wearing an old name. Nothing was written" is advice as
    much as a diagnosis: an operator who believes it re-runs under a fresh id and
    expects the old tree to be exactly what the earlier run left. A reuse that
    rewrote `run.json` under the new bindings before refusing would make the
    existing artifacts describe a configuration no longer recorded beside them,
    and the message would still print.

    Names alone are not enough -- the defect this closes overwrites a file that
    already exists, so the comparison is over content. Each parameter changes one
    bound field, so the refusal can only be about that field, and the assertion
    names the paths that moved rather than only counting them.
    """
    tree = make_run(tmp_path)
    tree.publish_artifact(make_envelope())
    before = tree_snapshot(tmp_path)

    with pytest.raises(IncompatibleReuse) as caught:
        make_run(tmp_path, **overrides)

    assert field in str(caught.value)
    assert "Nothing was written" in str(caught.value)
    assert _stray_writes(before, tree_snapshot(tmp_path)) == [], (
        "the refusal wrote to the run tree it disowned"
    )


def test_the_shared_snapshot_reports_what_a_refusal_leaves_that_is_not_a_file(tmp_path):
    """The comparison above is only as strong as what the snapshot can see.

    Filtered by `is_file()`, it saw regular files and nothing else, so a refusal
    that created a directory and stopped -- the ordinary shape of a half-done
    write -- left a tree that compared equal to one where nothing happened. Each
    case here is something a store can leave behind on the way to refusing, and
    each is asserted to move the snapshot.
    """
    root = tmp_path / "run"
    (root / "artifacts").mkdir(parents=True)
    (root / "artifacts" / "kept.json").write_bytes(b"{}")
    before = tree_snapshot(root)

    # A refusal that created a stage directory and
    # wrote nothing into it. Invisible to a file-only snapshot.
    (root / "blobs").mkdir()
    after = tree_snapshot(root)
    assert _stray_writes(before, after) == ["blobs"]
    assert after["blobs"] == "directory"

    # A dangling symlink: `is_file()` is False because the target is absent, so
    # a file-only snapshot could not report the name that was claimed.
    (root / "dangling").symlink_to(root / "never-written")
    assert _stray_writes(after, tree_snapshot(root)) == ["dangling"]

    # A symlink to a directory, recorded as the link rather than walked as the
    # directory: what matters is that a name inside the run tree now points out
    # of it, not what the target happens to contain today.
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "not-ours.json").write_bytes(b"{}")
    (root / "escape").symlink_to(outside)
    escaped = tree_snapshot(root)
    assert escaped["escape"] == f"symlink -> {outside}"
    assert "escape/not-ours.json" not in escaped

    # An irregular entry -- a fifo left by an interrupted write -- is reported as
    # one rather than skipped for not being a regular file.
    os.mkfifo(root / "half-written.fifo")
    fifos = tree_snapshot(root)
    assert fifos["half-written.fifo"] == "irregular entry"

    # The file that was there all along is still reported by content, so nothing
    # above was bought by loosening the original check.
    assert fifos["artifacts/kept.json"] == f"file {hashlib.sha256(b'{}').hexdigest()}"


def test_the_shared_snapshot_tells_a_missing_root_from_an_empty_one(tmp_path):
    """The blindness one level up from the ones above: the root itself.

    Describing only what was *under* the root meant `os.walk` of a path that
    does not exist yielded nothing, and an existing but empty root yielded
    nothing either -- so the two compared equal. A probe asserting that a
    refusal "wrote nothing" then passed on the state it was written to catch: a
    run root created, claimed, and left behind under inputs that were refused.

    `pipeline/test_decoding_seal.py` carried exactly that assertion, and this is
    the property that lets it be tightened to require absence rather than
    emptiness.
    """
    missing = tmp_path / "never-created"
    assert tree_snapshot(missing) == {}

    missing.mkdir()
    assert tree_snapshot(missing) == {".": "directory"}

    # The root is described the way any other entry is, so a root replaced by a
    # link out of the tree is reported as the link and not walked as what it
    # points at.
    outside = tmp_path / "elsewhere"
    (outside / "not-ours").mkdir(parents=True)
    linked = tmp_path / "linked-root"
    linked.symlink_to(outside)
    assert tree_snapshot(linked) == {".": f"symlink -> {outside}"}

    # And the entries under a real root are still reported beside it.
    (missing / "kept.json").write_bytes(b"{}")
    assert tree_snapshot(missing) == {
        ".": "directory",
        "kept.json": f"file {hashlib.sha256(b'{}').hexdigest()}",
    }


def test_run_authority_is_not_published_when_its_register_snapshot_write_fails(
    tmp_path, monkeypatch
):
    def fail_snapshot(_self, _stage, _data):
        raise OSError("simulated snapshot publication failure")

    monkeypatch.setattr(RunTree, "put_blob", fail_snapshot)
    with pytest.raises(OSError, match="snapshot publication failure"):
        make_run(tmp_path, register_bytes=empty_register())

    assert not (tmp_path / "r1" / RUN_FILE).exists()


def test_reuse_refuses_a_missing_register_snapshot_instead_of_reconstructing_it(tmp_path):
    tree = make_run(tmp_path, register_bytes=empty_register())
    run = tree.read_run()
    snapshot = tree.resolve(tree.blob_path(DOOR, run["register_digest"]))
    snapshot.unlink()

    with pytest.raises(IncompatibleReuse, match="could not be read"):
        make_run(tmp_path, register_bytes=empty_register())

    assert not snapshot.exists()


@pytest.mark.parametrize(
    ("damage", "named"),
    (
        ('{"scale": 1.5}', "float at"),
        ('{"name": "\\ud800"}', "unencodable character"),
        ('{"count": ' + "9" * 700 + "}", "integer at"),
    ),
)
def test_a_run_authority_with_uncanonical_current_content_names_that_cause(tmp_path, damage, named):
    """Bare run authority must name why its current contents cannot be compared.

    The fixture bypasses canonical_bytes because valid pipeline output cannot
    contain the malformed value this read boundary must still refuse.
    """
    tree = make_run(tmp_path)
    record = tree.read_run()
    record["render_settings"] = json.loads(damage)
    (tmp_path / "r1" / RUN_FILE).write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(IncompatibleReuse) as caught:
        tree.read_run()
    message = str(caught.value)
    assert named in message
    assert "was edited" not in message
    message.encode("utf-8")


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
    """Repeating the identical command leaves all artifact bytes unchanged, and an
    interrupted run resumes from valid artifacts without rewriting them."""
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


def _proposal(**overrides):
    fields = {
        "run_id": "r1",
        "artifact_id": artifact_id(DESIGNATOR, "proposal", "pg_0123456789abcdef"),
        "subject_id": "pg_0123456789abcdef",
        "stage": DESIGNATOR,
        "kind": "proposal",
        "outcome": "proposed",
        "config_digest": CONFIG_DIGEST,
        "adapter_revision": "fake-designator-v0",
        "inputs": [],
        "payload": {"proposals": 2},
    }
    fields.update(overrides)
    return build_envelope(**fields)


def test_publication_refuses_what_every_read_route_would_refuse(tmp_path):
    """An immutable artifact readers refuse could never be replaced, and would stop
    its stage's manifest for good, so the writer refuses it with nothing written."""
    tree = make_run(tmp_path)
    blob_digest, _ = tree.put_blob(DESIGNATOR, b"a crop")
    present = {"relative_path": tree.blob_path(DESIGNATOR, blob_digest), "sha256": blob_digest}
    dangling = {"relative_path": tree.blob_path(DESIGNATOR, "e" * 64), "sha256": "e" * 64}
    before = tree_snapshot(tree.root)

    with pytest.raises(SchemaRefusal, match="two runs may share a name"):
        tree.publish_artifact(_proposal(config_digest="d" * 64))
    with pytest.raises(SchemaRefusal, match="artifact input"):
        tree.publish_artifact(_proposal(inputs=[dangling]))
    assert _stray_writes(before, tree_snapshot(tree.root)) == []

    published = tree.publish_artifact(_proposal(inputs=[present]))
    reference = {
        "relative_path": published.relative_path,
        "sha256": digest_bytes(tree.read_bytes(published.relative_path)),
    }
    read_back = tree.read_artifact_reference(reference, stage=DESIGNATOR, kind="proposal")
    assert read_back["inputs"] == [present]


def test_manifest_verifies_shared_input_once_per_build_and_refuses_changed_bytes(
    tmp_path, monkeypatch
):
    tree = make_run(tmp_path)
    digest, blob = tree.put_blob(DESIGNATOR, b"shared page pixels")
    reference = {"relative_path": blob.relative_path, "sha256": digest}
    for subject in ("page-line-1", "page-line-2"):
        tree.publish_artifact(
            _proposal(
                subject_id=subject,
                artifact_id=artifact_id(DESIGNATOR, "proposal", subject),
                inputs=[reference],
            )
        )

    original_read_bytes = tree.read_bytes
    input_reads = 0

    def counted_read_bytes(relative_path, **options):
        nonlocal input_reads
        if relative_path == blob.relative_path:
            input_reads += 1
        return original_read_bytes(relative_path, **options)

    monkeypatch.setattr(tree, "read_bytes", counted_read_bytes)
    first = tree.build_manifest(DESIGNATOR)
    assert len(first["artifacts"]) == 2
    assert input_reads == 1

    assert tree.build_manifest(DESIGNATOR) == first
    assert input_reads == 2

    tree.resolve(blob.relative_path).write_bytes(b"changed page pixels")
    with pytest.raises(
        SchemaRefusal, match="artifact input .* bytes changed under a sealed reference"
    ):
        tree.build_manifest(DESIGNATOR)
    assert input_reads == 3


def test_record_reads_by_reference_are_bounded_by_the_record_ceiling(tmp_path, monkeypatch):
    """A referenced artifact, a serving receipt and an approval record are JSON
    records about to be parsed, so each is read under the record ceiling rather
    than the page-blob ceiling."""
    tree = make_run(tmp_path)
    published = tree.publish_artifact(make_envelope())
    artifact_ref = {
        "relative_path": published.relative_path,
        "sha256": digest_bytes(tree.read_bytes(published.relative_path)),
    }
    receipt_ref, _ = tree.write_run_receipt(make_receipt())
    approval_ref, _ = tree.write_approval_record(make_approval_record())
    monkeypatch.setattr(runtree_store, "MAX_RECORD_READ_BYTES", 4)

    with pytest.raises(SchemaRefusal, match="tree read limit"):
        tree.read_artifact_reference(artifact_ref, stage=DESIGNATOR, kind="proposal")
    with pytest.raises(SchemaRefusal, match="tree read limit"):
        tree.read_run_receipt(receipt_ref)
    with pytest.raises(SchemaRefusal, match="tree read limit"):
        tree.read_approval_record(approval_ref)


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


@pytest.mark.parametrize(
    "authority_state", ("missing", "self-hash-corrupt", "bad-digest", "not-an-object")
)
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
    elif authority_state == "not-an-object":
        run_file.write_bytes(b"[]")
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


def test_a_path_the_filesystem_cannot_resolve_is_a_named_refusal(tmp_path):
    """An embedded NUL is invalid at the OS boundary, not an interpreter failure."""
    tree = make_run(tmp_path)

    with pytest.raises(SchemaRefusal, match="could not be resolved inside the run tree"):
        tree.resolve("1_exemplar/artifacts/a\x00b.json")


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


def test_a_manifest_refuses_an_artifact_symlink_that_leaves_the_run_tree(tmp_path):
    """Containment, which is what this one actually pins — see the test below.

    `_raise_manifest_symlink` resolves the path first, and a target outside the
    tree fails that before any link-specific message is built. So this stays green
    with the `S_ISLNK` check deleted, and the link guard needs its own case.
    """
    tree = make_run(tmp_path)
    published = tree.publish_artifact(make_envelope())
    artifact_path = tree.resolve(published.relative_path)
    outside = tmp_path / "outside-artifact.json"
    artifact_path.replace(outside)
    artifact_path.symlink_to(outside)

    with pytest.raises(SchemaRefusal, match="outside the run tree"):
        tree.build_manifest(DESIGNATOR)


def test_a_manifest_refuses_an_artifact_symlink_that_stays_inside_the_run_tree(tmp_path):
    """The link guard itself, on the case containment cannot reach.

    A link to a sibling artifact resolves inside the tree, so only the `S_ISLNK`
    check stands between it and the inventory — and this is the case that matters,
    because it is how one artifact comes to answer for two rows.
    """
    tree = make_run(tmp_path)
    published = tree.publish_artifact(make_envelope())
    sibling = tree.publish_artifact(make_envelope(subject="pg_fedcba9876543210"))
    aliased = tree.resolve(published.relative_path)
    aliased.unlink()
    aliased.symlink_to(tree.resolve(sibling.relative_path))

    with pytest.raises(SchemaRefusal, match="never an alias"):
        tree.build_manifest(DESIGNATOR)


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


def test_shared_door_and_exemplar_evidence_keeps_one_manifest_per_producer(tmp_path):
    """One physical evidence directory must not imply one producer inventory.

    Completion-seal deletion is exposed by the last stored manifest that named
    the seal. If Door and Exemplar overwrite one file, the second producer can
    erase that trigger before a resume checks it.
    """
    tree = make_run(tmp_path)
    tree.write_manifest(DOOR)
    tree.write_manifest(EXEMPLAR)

    assert tree.manifest_path(DOOR) != tree.manifest_path(EXEMPLAR)
    assert json.loads(tree.read_bytes(tree.manifest_path(DOOR)))["stage"] == DOOR
    assert json.loads(tree.read_bytes(tree.manifest_path(EXEMPLAR)))["stage"] == EXEMPLAR


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


def test_a_manifest_refuses_a_stage_directory_replaced_by_a_regular_file(tmp_path):
    """Missing inventory leaves are not honest absence below an invalid parent."""
    tree = make_run(tmp_path)
    stage_root = tree.resolve(writing_directory(DESIGNATOR))
    stage_root.parent.mkdir(parents=True, exist_ok=True)
    stage_root.write_bytes(b"not a stage directory")

    with pytest.raises(SchemaRefusal, match="stage inventory.*not a directory"):
        tree.build_manifest(DESIGNATOR)


def test_a_manifest_refuses_a_derived_artifact_symlink_outside_the_run_tree(tmp_path):
    tree = make_run(tmp_path)
    envelope = make_envelope()
    outside = tmp_path / "outside-artifact.json"
    outside.write_bytes(canonical_bytes(envelope))
    artifact = tree.resolve(tree.artifact_path(DESIGNATOR, "proposal", envelope["artifact_id"]))
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.symlink_to(outside)

    with pytest.raises(SchemaRefusal, match="resolves outside the run tree"):
        tree.build_manifest(DESIGNATOR)


def test_a_manifest_refuses_a_derived_artifact_symlink_inside_the_run_tree(tmp_path):
    """A contained target does not make a file alias an artifact the store wrote."""
    tree = make_run(tmp_path)
    envelope = make_envelope()
    inside = tree.root / "aliased-artifact.json"
    inside.write_bytes(canonical_bytes(envelope))
    artifact = tree.resolve(tree.artifact_path(DESIGNATOR, "proposal", envelope["artifact_id"]))
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.symlink_to(inside)

    with pytest.raises(SchemaRefusal, match="is a link"):
        tree.build_manifest(DESIGNATOR)


def test_a_manifest_refuses_a_kind_directory_symlinked_outside_the_run_tree(tmp_path):
    """A symlinked kind must not disappear as an apparently empty producer."""
    tree = make_run(tmp_path)
    envelope = make_envelope()
    outside = tmp_path / "outside-kind-directory"
    outside.mkdir()
    (outside / f"{envelope['artifact_id']}.json").write_bytes(canonical_bytes(envelope))
    kind_directory = tree.resolve(
        tree.artifact_path(DESIGNATOR, "proposal", envelope["artifact_id"])
    ).parent
    kind_directory.parent.mkdir(parents=True, exist_ok=True)
    kind_directory.symlink_to(outside)

    with pytest.raises(SchemaRefusal, match="resolves outside the run tree"):
        tree.build_manifest(DESIGNATOR)


def test_a_manifest_refuses_a_symlink_cycle_inside_the_run_tree(tmp_path):
    """Containment alone cannot detect a cycle whose every target stays in-root."""
    tree = make_run(tmp_path)
    kind_directory = tree.resolve(tree.artifact_path(DESIGNATOR, "proposal", "placeholder")).parent
    kind_directory.mkdir(parents=True, exist_ok=True)
    (kind_directory / "loop").symlink_to(kind_directory)

    with pytest.raises(SchemaRefusal, match="symlink cycle"):
        tree.build_manifest(DESIGNATOR)


def test_a_manifest_refuses_a_filesystem_symlink_loop_instead_of_skipping_it(tmp_path):
    """A self-link is named whether ``resolve`` returns it or raises for its loop.

    Python 3.12 raises ``RuntimeError`` for the loop, while 3.13+ resolves as far
    as possible in non-strict mode. Both supported behaviours must be refusals.
    """
    tree = make_run(tmp_path)
    artifacts_root = tree.resolve(f"{writing_directory(DESIGNATOR)}/{ARTIFACTS_DIR}")
    artifacts_root.mkdir(parents=True)
    (artifacts_root / "loop").symlink_to("loop", target_is_directory=True)

    with pytest.raises(SchemaRefusal, match="is a link|could not be resolved inside the run tree"):
        tree.build_manifest(DESIGNATOR)


def test_a_manifest_names_a_path_resolution_failure(tmp_path, monkeypatch):
    """Platforms disagree on whether an unresolvable link raises OSError or RuntimeError."""
    tree = make_run(tmp_path)
    envelope = make_envelope()
    tree.publish_artifact(envelope)
    artifact = tree.resolve(tree.artifact_path(DESIGNATOR, "proposal", envelope["artifact_id"]))
    real_resolve = Path.resolve

    def fail_for_artifact(path, *args, **kwargs):
        if path == artifact:
            raise RuntimeError("symlink loop")
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", fail_for_artifact)

    with pytest.raises(SchemaRefusal, match="could not be resolved inside the run tree"):
        tree.build_manifest(DESIGNATOR)


def test_a_manifest_refuses_a_kind_directory_linked_to_nothing(tmp_path):
    """A dangling non-JSON link must not erase a producer directory from the walk."""
    tree = make_run(tmp_path)
    artifacts_root = tree.resolve(f"{writing_directory(DESIGNATOR)}/{ARTIFACTS_DIR}")
    artifacts_root.mkdir(parents=True)
    (artifacts_root / "proposal").symlink_to("gone", target_is_directory=True)

    with pytest.raises(SchemaRefusal, match="is a link"):
        tree.build_manifest(DESIGNATOR)


def test_a_manifest_refuses_a_unique_directory_link_inside_the_run_tree(tmp_path):
    """A link is not safe merely because its target has no second walked name."""
    tree = make_run(tmp_path)
    envelope = make_envelope()
    elsewhere = tree.root / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / f"{envelope['artifact_id']}.json").write_bytes(canonical_bytes(envelope))
    artifacts_root = tree.resolve(f"{writing_directory(DESIGNATOR)}/{ARTIFACTS_DIR}")
    artifacts_root.mkdir(parents=True)
    (artifacts_root / "proposal").symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(SchemaRefusal, match="is a link"):
        tree.build_manifest(DESIGNATOR)


@pytest.mark.parametrize("alias_name", ("also-proposal", "z-proposal-alias"))
def test_a_manifest_refuses_one_artifact_directory_reachable_at_two_paths(tmp_path, alias_name):
    """Alias refusal must not depend on which directory name sorts first.

    This covers the *symlink* alias and says so. The walk's other alias guard --
    two names resolving to one inode, `identity in walked` -- cannot be reached
    from here, because a symlink trips the link refusal first and a second
    non-symlink name for one directory needs a directory hard link or a bind
    mount, neither of which a portable test can make. Asserting the two messages
    as an alternation let this read as coverage of both; it never was.
    """
    tree = make_run(tmp_path)
    envelope = make_envelope()
    tree.publish_artifact(envelope)
    kind_directory = tree.resolve(
        tree.artifact_path(DESIGNATOR, "proposal", envelope["artifact_id"])
    ).parent
    (kind_directory.parent / alias_name).symlink_to(kind_directory)

    with pytest.raises(SchemaRefusal, match="is a link"):
        tree.build_manifest(DESIGNATOR)


def test_a_manifest_refuses_a_pipe_named_as_an_artifact(tmp_path):
    """A FIFO must be rejected before opening it, which would block for a writer."""
    tree = make_run(tmp_path)
    envelope = make_envelope()
    tree.publish_artifact(envelope)
    kind_directory = tree.resolve(
        tree.artifact_path(DESIGNATOR, "proposal", envelope["artifact_id"])
    ).parent
    os.mkfifo(kind_directory / "waiting.json")

    with pytest.raises(SchemaRefusal, match="neither a directory nor a regular file"):
        tree.build_manifest(DESIGNATOR)


def test_a_manifest_refuses_a_pipe_occupying_an_artifact_kind_name(tmp_path):
    """A non-JSON special file cannot erase the producer directory it occupies."""
    tree = make_run(tmp_path)
    artifacts_root = tree.resolve(f"{writing_directory(DESIGNATOR)}/{ARTIFACTS_DIR}")
    artifacts_root.mkdir(parents=True)
    os.mkfifo(artifacts_root / "proposal")

    with pytest.raises(SchemaRefusal, match="artifact kind directory"):
        tree.build_manifest(DESIGNATOR)


def test_a_manifest_refuses_a_directory_it_cannot_list(tmp_path):
    """An unreadable directory cannot count as an empty part of the inventory."""
    tree = make_run(tmp_path)
    envelope = make_envelope()
    tree.publish_artifact(envelope)
    kind_directory = tree.resolve(
        tree.artifact_path(DESIGNATOR, "proposal", envelope["artifact_id"])
    ).parent
    blocked = kind_directory / "unreadable"
    blocked.mkdir()
    blocked.chmod(0o000)
    try:
        if os.access(blocked, os.R_OK):  # pragma: no cover - running as root
            pytest.skip("this process can list a mode-000 directory; the case cannot be built")
        with pytest.raises(SchemaRefusal, match="could not be listed"):
            tree.build_manifest(DESIGNATOR)
    finally:
        blocked.chmod(0o700)


def test_the_artifact_walk_matches_the_glob_when_the_glob_matches_only_files(tmp_path):
    """Ordinary-tree membership and order stay compatible with the replaced glob."""
    tree = make_run(tmp_path)
    published = []
    # "Zeal" (not "Proposal") for the uppercase-ordering case: a kind differing from
    # a sibling only by case collapses into one directory on a case-insensitive
    # filesystem, and the walked path then contradicts the derived path.
    for kind in ("proposal", "Zeal", "proposal-b", "seal"):
        for index in range(3):
            subject = f"pg_{index:016x}"
            envelope = build_envelope(
                run_id="r1",
                artifact_id=artifact_id(DESIGNATOR, kind, subject),
                subject_id=subject,
                stage=DESIGNATOR,
                kind=kind,
                outcome="proposed",
                config_digest=CONFIG_DIGEST,
                adapter_revision="fake-designator-v0",
                inputs=[],
                payload={"proposals": index},
            )
            tree.publish_artifact(envelope)
            published.append(envelope["artifact_id"])

    artifacts_root = tree.resolve(f"{writing_directory(DESIGNATOR)}/{ARTIFACTS_DIR}")
    globbed = [str(path.relative_to(tree.root)) for path in sorted(artifacts_root.rglob("*.json"))]
    walked = list(tree._walk_artifact_json(artifacts_root))

    assert walked == globbed
    assert len(walked) == 12
    manifest = tree.build_manifest(DESIGNATOR)
    assert sorted(entry["artifact_id"] for entry in manifest["artifacts"]) == sorted(published)


def test_a_manifest_descends_a_store_written_kind_directory_ending_in_json(tmp_path):
    """A kind may end in ``.json`` even though artifact directories may not."""
    tree = make_run(tmp_path)
    kind = "proposal.json"
    envelope = build_envelope(
        run_id="r1",
        artifact_id=artifact_id(DESIGNATOR, kind, "pg_0123456789abcdef"),
        subject_id="pg_0123456789abcdef",
        stage=DESIGNATOR,
        kind=kind,
        outcome="proposed",
        config_digest=CONFIG_DIGEST,
        adapter_revision="fake-designator-v0",
        inputs=[],
        payload={"proposals": []},
    )
    tree.publish_artifact(envelope)

    manifest = tree.build_manifest(DESIGNATOR)

    assert [entry["artifact_id"] for entry in manifest["artifacts"]] == [envelope["artifact_id"]]


def test_a_manifest_refuses_an_artifact_file_replaced_by_an_empty_directory(tmp_path):
    """A glob-matching directory below the kind level is missing artifact bytes."""
    tree = make_run(tmp_path)
    envelope = make_envelope()
    tree.publish_artifact(envelope)
    artifact = tree.resolve(tree.artifact_path(DESIGNATOR, "proposal", envelope["artifact_id"]))
    artifact.unlink()
    artifact.mkdir()

    with pytest.raises(SchemaRefusal, match="named as an artifact but is a directory"):
        tree.build_manifest(DESIGNATOR)


def test_the_artifact_walk_matches_the_glob_on_a_name_that_is_only_a_suffix(tmp_path):
    """``.json`` matches the established glob even though ``Path.suffix`` is empty."""
    tree = make_run(tmp_path)
    envelope = make_envelope()
    tree.publish_artifact(envelope)
    kind_directory = tree.resolve(
        tree.artifact_path(DESIGNATOR, "proposal", envelope["artifact_id"])
    ).parent
    (kind_directory / ".json").write_bytes(canonical_bytes(envelope))

    with pytest.raises(SchemaRefusal, match="does not occupy its derived path"):
        tree.build_manifest(DESIGNATOR)


def test_the_artifact_walk_does_not_recurse_once_per_directory(tmp_path):
    """The walk's Python stack depth must not grow with filesystem depth."""
    tree = make_run(tmp_path)
    envelope = make_envelope()
    tree.publish_artifact(envelope)
    artifacts_root = tree.resolve(f"{writing_directory(DESIGNATOR)}/{ARTIFACTS_DIR}")
    deepest = artifacts_root / "deep"
    deepest.mkdir()
    for _ in range(200):
        deepest = deepest / "x"
        deepest.mkdir()

    limit = sys.getrecursionlimit()
    # Headroom well under the 200 directories built above, so a walk that
    # recursed once per directory still exhausts it and fails -- but far enough
    # above the walk's own constant needs (JSON validation, `Path.resolve`)
    # that incidental frames cannot raise `RecursionError` and be misread as
    # the defect this hunts.
    sys.setrecursionlimit(len(inspect.stack(0)) + 128)
    try:
        manifest = tree.build_manifest(DESIGNATOR)
    finally:
        sys.setrecursionlimit(limit)
        # Unwound here rather than left for the temporary directory's own cleanup,
        # which is itself a recursive walk of exactly the shape this test builds.
        while deepest != artifacts_root:
            deepest.rmdir()
            deepest = deepest.parent

    assert [entry["artifact_id"] for entry in manifest["artifacts"]] == [envelope["artifact_id"]]


def test_a_manifest_refuses_an_artifacts_directory_symlinked_out_of_the_run_tree(tmp_path):
    """Containment applies to the walk root even when its target is empty."""
    tree = make_run(tmp_path)
    outside = tmp_path / "outside-artifacts-directory"
    outside.mkdir()
    stage_root = tree.resolve(writing_directory(DESIGNATOR))
    stage_root.mkdir(parents=True, exist_ok=True)
    (stage_root / ARTIFACTS_DIR).symlink_to(outside)

    with pytest.raises(SchemaRefusal, match="resolves outside the run tree"):
        tree.build_manifest(DESIGNATOR)


def test_a_manifest_refuses_an_artifacts_directory_redirected_inside_the_run_tree(tmp_path):
    """Containment does not make an in-tree alias a store-written directory."""
    tree = make_run(tmp_path)
    elsewhere = tree.root / "elsewhere-in-tree"
    elsewhere.mkdir()
    stage_root = tree.resolve(writing_directory(DESIGNATOR))
    stage_root.mkdir(parents=True, exist_ok=True)
    (stage_root / ARTIFACTS_DIR).symlink_to(elsewhere)

    with pytest.raises(SchemaRefusal, match="is a link"):
        tree.build_manifest(DESIGNATOR)


def test_a_manifest_refuses_an_artifacts_directory_linked_to_nothing(tmp_path):
    """A dangling inventory link is not equivalent to an absent directory."""
    tree = make_run(tmp_path)
    stage_root = tree.resolve(writing_directory(DESIGNATOR))
    stage_root.mkdir(parents=True, exist_ok=True)
    (stage_root / ARTIFACTS_DIR).symlink_to("gone")

    with pytest.raises(SchemaRefusal, match="is a link"):
        tree.build_manifest(DESIGNATOR)


def test_a_manifest_refuses_an_artifacts_directory_replaced_by_a_regular_file(tmp_path):
    """An inventory root must be a directory, not merely an existing path."""
    tree = make_run(tmp_path)
    stage_root = tree.resolve(writing_directory(DESIGNATOR))
    stage_root.mkdir(parents=True, exist_ok=True)
    (stage_root / ARTIFACTS_DIR).write_bytes(b"not a directory")

    with pytest.raises(SchemaRefusal, match="is not a directory"):
        tree.build_manifest(DESIGNATOR)


def test_a_manifest_refuses_a_blobs_directory_symlinked_out_of_the_run_tree(tmp_path):
    """Blob and artifact inventory roots share the same containment requirement."""
    tree = make_run(tmp_path)
    outside = tmp_path / "outside-blobs-directory"
    outside.mkdir()
    (outside / ("a" * 64)).write_bytes(b"bytes this run never stored")
    stage_root = tree.resolve(writing_directory(DESIGNATOR))
    (stage_root / BLOBS_DIR).parent.mkdir(parents=True, exist_ok=True)
    (stage_root / BLOBS_DIR).symlink_to(outside)

    with pytest.raises(SchemaRefusal, match="resolves outside the run tree"):
        tree.build_manifest(DESIGNATOR)


def test_a_manifest_refuses_a_blob_that_points_out_of_the_run_tree(tmp_path):
    """A manifest cannot claim blob bytes that its own read route refuses."""
    tree = make_run(tmp_path)
    tree.put_blob(DESIGNATOR, b"a crop")
    outside = tmp_path / "outside-blob-bytes"
    outside.write_bytes(b"bytes this run never stored")
    blobs_root = tree.resolve(f"{writing_directory(DESIGNATOR)}/{BLOBS_DIR}")
    (blobs_root / ("b" * 64)).symlink_to(outside)

    with pytest.raises(SchemaRefusal, match="resolves outside the run tree"):
        tree.build_manifest(DESIGNATOR)


def test_a_manifest_refuses_a_blob_linked_to_bytes_inside_the_run_tree(tmp_path):
    tree = make_run(tmp_path)
    data = b"a crop"
    digest = digest_bytes(data)
    elsewhere = tree.root / "elsewhere.bin"
    elsewhere.write_bytes(data)
    blobs_root = tree.resolve(f"{writing_directory(DESIGNATOR)}/{BLOBS_DIR}")
    blobs_root.mkdir(parents=True)
    (blobs_root / digest).symlink_to(elsewhere)

    with pytest.raises(SchemaRefusal, match="is a link"):
        tree.build_manifest(DESIGNATOR)


def test_a_manifest_refuses_a_blob_name_that_is_not_stored_bytes(tmp_path):
    tree = make_run(tmp_path)
    tree.put_blob(DESIGNATOR, b"a crop")
    blobs_root = tree.resolve(f"{writing_directory(DESIGNATOR)}/{BLOBS_DIR}")
    os.mkfifo(blobs_root / ("c" * 64))

    with pytest.raises(SchemaRefusal, match="is not a regular file"):
        tree.build_manifest(DESIGNATOR)


def test_the_blob_inventory_lists_stored_bytes_and_not_a_publication_that_was_killed(tmp_path):
    """Same-directory publication residue is not an addressable blob."""
    tree = make_run(tmp_path)
    digest, _ = tree.put_blob(DESIGNATOR, b"a crop")
    blobs_root = tree.resolve(f"{writing_directory(DESIGNATOR)}/{BLOBS_DIR}")
    (blobs_root / f".{digest}.tmp-abcdef").write_bytes(b"a partly published crop")

    assert tree.build_manifest(DESIGNATOR)["blobs"] == [digest]


def test_a_manifest_refuses_unaddressable_blob_names(tmp_path):
    tree = make_run(tmp_path)
    blobs_root = tree.resolve(f"{writing_directory(DESIGNATOR)}/{BLOBS_DIR}")
    blobs_root.mkdir(parents=True)
    (blobs_root / "unknown").write_bytes(b"unaddressable")

    with pytest.raises(SchemaRefusal, match="noncanonical content address"):
        tree.build_manifest(DESIGNATOR)


def test_a_manifest_still_builds_over_the_directories_the_store_itself_wrote(tmp_path):
    tree = make_run(tmp_path)
    envelope = make_envelope()
    tree.publish_artifact(envelope)
    tree.put_blob(DESIGNATOR, b"a crop")

    manifest = tree.build_manifest(DESIGNATOR)

    assert [entry["artifact_id"] for entry in manifest["artifacts"]] == [envelope["artifact_id"]]
    assert manifest["blobs"] == [digest_bytes(b"a crop")]


def test_a_manifest_hashes_the_same_artifact_read_it_verified(tmp_path, monkeypatch):
    """Verified metadata and its digest must come from one filesystem snapshot."""
    tree = make_run(tmp_path)
    envelope = make_envelope()
    tree.publish_artifact(envelope)
    artifact = tree.resolve(tree.artifact_path(DESIGNATOR, "proposal", envelope["artifact_id"]))
    expected_bytes = canonical_bytes(envelope)
    artifact_identity = (artifact.stat().st_dev, artifact.stat().st_ino)
    real_fdopen = os.fdopen
    reads = 0

    def counted_fdopen(descriptor, *args, **kwargs):
        nonlocal reads
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) == artifact_identity:
            reads += 1
        return real_fdopen(descriptor, *args, **kwargs)

    monkeypatch.setattr(os, "fdopen", counted_fdopen)
    # `os.fdopen` is not the only way to read the file. Counting that route
    # alone, a change that hashed via `Path.read_bytes` would leave `reads` at
    # 1 and the digest still correct -- the file does not change during the
    # test -- so the manifest could hash bytes it never verified and this would
    # still report success.
    real_read_bytes = Path.read_bytes

    def counted_read_bytes(path):
        nonlocal reads
        named = path.stat()
        if (named.st_dev, named.st_ino) == artifact_identity:
            reads += 1
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", counted_read_bytes)

    entry = tree.build_manifest(DESIGNATOR)["artifacts"][0]

    assert reads == 1
    assert entry["sha256"] == digest_bytes(expected_bytes)


def test_a_manifest_refuses_an_artifact_replaced_by_a_link_at_open(tmp_path, monkeypatch):
    """The no-follow open closes the lstat-to-read replacement window."""
    tree = make_run(tmp_path)
    envelope = make_envelope()
    tree.publish_artifact(envelope)
    artifact = tree.resolve(tree.artifact_path(DESIGNATOR, "proposal", envelope["artifact_id"]))
    outside = tmp_path / "outside-at-open.json"
    outside.write_bytes(canonical_bytes(envelope))
    real_open = os.open
    replaced = False

    def replace_before_open(path, flags, *args, **kwargs):
        nonlocal replaced
        if path == artifact.name and kwargs.get("dir_fd") is not None and not replaced:
            replaced = True
            artifact.unlink()
            artifact.symlink_to(outside)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replace_before_open)

    with pytest.raises(SchemaRefusal, match="without following links"):
        tree.build_manifest(DESIGNATOR)
    assert replaced is True


def test_a_manifest_refuses_an_empty_inventory_directory_replaced_during_listing(
    tmp_path, monkeypatch
):
    """An opened empty directory cannot be renamed away and reported as current."""
    tree = make_run(tmp_path)
    artifacts_root = tree.resolve(f"{writing_directory(DESIGNATOR)}/{ARTIFACTS_DIR}")
    artifacts_root.mkdir(parents=True)
    original_identity = (artifacts_root.stat().st_dev, artifacts_root.stat().st_ino)
    parked = tree.root / "parked-artifacts"
    outside = tmp_path / "outside-empty-artifacts"
    outside.mkdir()
    real_scandir = os.scandir
    replaced = False

    def replace_after_listing(path):
        nonlocal replaced
        listing = real_scandir(path)
        opened = os.fstat(path)
        if (opened.st_dev, opened.st_ino) == original_identity and not replaced:
            replaced = True
            artifacts_root.rename(parked)
            artifacts_root.symlink_to(outside, target_is_directory=True)
        return listing

    monkeypatch.setattr(os, "scandir", replace_after_listing)

    with pytest.raises(SchemaRefusal, match="resolves outside the run tree|is a link"):
        tree.build_manifest(DESIGNATOR)
    assert replaced is True


def test_a_manifest_refuses_a_run_root_replaced_after_the_tree_was_opened(tmp_path):
    """Containment is bound to the opened run directory's device and inode."""
    tree = make_run(tmp_path)
    original = tmp_path / "original-r1"
    tree.root.rename(original)
    tree.root.mkdir()
    (tree.root / RUN_FILE).write_bytes((original / RUN_FILE).read_bytes())

    with pytest.raises(SchemaRefusal, match="device or inode changed"):
        tree.build_manifest(DESIGNATOR)


def test_a_manifest_refuses_case_variant_inventory_names(tmp_path):
    """A Linux-built tree must not silently collapse when moved to default APFS."""
    tree = make_run(tmp_path)
    artifacts_root = tree.resolve(f"{writing_directory(DESIGNATOR)}/{ARTIFACTS_DIR}")
    artifacts_root.mkdir(parents=True)
    lower = artifacts_root / "residue"
    upper = artifacts_root / "RESIDUE"
    lower.write_bytes(b"one")
    upper.write_bytes(b"two")
    if lower.samefile(upper):
        pytest.skip("the filesystem itself conflates case variants")

    with pytest.raises(SchemaRefusal, match="differ only in case or Unicode normalisation"):
        tree.build_manifest(DESIGNATOR)


def test_a_manifest_refuses_unicode_normalisation_variant_inventory_names(tmp_path):
    """APFS also stores a composed and a decomposed spelling as one name."""
    tree = make_run(tmp_path)
    artifacts_root = tree.resolve(f"{writing_directory(DESIGNATOR)}/{ARTIFACTS_DIR}")
    artifacts_root.mkdir(parents=True)
    composed = artifacts_root / "\u00e9tienne"
    decomposed = artifacts_root / "e\u0301tienne"
    composed.write_bytes(b"one")
    decomposed.write_bytes(b"two")
    if composed.samefile(decomposed):
        pytest.skip("the filesystem itself conflates normalisation variants")

    with pytest.raises(SchemaRefusal, match="differ only in case or Unicode normalisation"):
        tree.build_manifest(DESIGNATOR)


def test_a_manifest_refuses_an_artifact_too_large_to_read_safely(tmp_path):
    tree = make_run(tmp_path)
    envelope = make_envelope()
    tree.publish_artifact(envelope)
    artifact = tree.resolve(tree.artifact_path(DESIGNATOR, "proposal", envelope["artifact_id"]))
    os.truncate(artifact, runtree_store._MAX_MANIFEST_ARTIFACT_BYTES + 1)

    with pytest.raises(SchemaRefusal, match="manifest artifact limit"):
        tree.build_manifest(DESIGNATOR)


def test_read_bytes_refuses_a_file_grown_past_the_tree_read_limit(tmp_path, monkeypatch):
    """`RunTree.read_bytes` has a ceiling of its own, so a damaged or hostile run
    tree is refused before it is read whole into memory. The same shape as the
    manifest-artifact bound above, for the tree's general reader.
    """
    tree = make_run(tmp_path)
    envelope = make_envelope()
    tree.publish_artifact(envelope)
    relative = tree.artifact_path(DESIGNATOR, "proposal", envelope["artifact_id"])
    artifact = tree.resolve(relative)
    monkeypatch.setattr(runtree_store, "_MAX_TREE_READ_BYTES", 4)

    with pytest.raises(SchemaRefusal, match="tree read limit"):
        tree.read_bytes(relative)
    assert artifact.stat().st_size > 4  # the file itself was never truncated


def test_the_export_archive_is_read_under_its_own_ceiling_and_a_page_blob_under_the_page_one(
    tmp_path, monkeypatch
):
    """The export archive embeds pages and crops, so it may outgrow any one of them.

    Its blob is read under the archive ceiling; a page blob of the same size is
    still refused under the page ceiling, and an archive past its own ceiling is
    refused too.
    """
    tree = make_run(tmp_path)
    monkeypatch.setattr(runtree_store, "_MAX_TREE_READ_BYTES", 8)
    monkeypatch.setattr(armarium_formats, "MAX_EXPORT_ARCHIVE_BYTES", 16)
    archive = b"z" * 12
    _, stored_archive = tree.put_blob(ARMARIUM, archive)
    _, stored_page = tree.put_blob(EXEMPLAR, archive)
    _, oversized_archive = tree.put_blob(ARMARIUM, b"z" * 17)

    assert tree.read_bytes(stored_archive.relative_path) == archive
    with pytest.raises(SchemaRefusal, match="8-byte tree read limit"):
        tree.read_bytes(stored_page.relative_path)
    with pytest.raises(SchemaRefusal, match="16-byte tree read limit"):
        tree.read_bytes(oversized_archive.relative_path)


def test_a_verified_read_refusal_names_no_host_path(tmp_path, monkeypatch):
    """The refusal text becomes a sealed reason (a not-run Testimonium, a held act)."""
    tree = make_run(tmp_path)
    envelope = make_envelope()
    tree.publish_artifact(envelope)
    present = {
        "relative_path": tree.artifact_path(DESIGNATOR, "proposal", envelope["artifact_id"]),
        "sha256": "0" * 64,
    }
    missing = {"relative_path": tree.blob_path(DESIGNATOR, "1" * 64), "sha256": "1" * 64}
    monkeypatch.setattr(runtree_store, "_MAX_TREE_READ_BYTES", 4)
    for ref in (missing, present):
        with pytest.raises(SchemaRefusal) as refused:
            read_verified(tree.read_bytes, ref, "evidence")
        assert str(tmp_path) not in str(refused.value)


def test_read_run_refuses_a_run_authority_grown_past_the_record_read_limit(tmp_path, monkeypatch):
    """A bound reaches `read_run`, routed through `_read_json`, not only
    `read_bytes` -- a hostile or corrupted `run.json` must not be read whole
    either. It is the record ceiling here, not the blob one: everything
    `_read_json_with_bytes` opens is a JSON record, and the bytes are about to
    be handed to `json.loads`, which costs several times their size again."""
    tree = make_run(tmp_path)
    monkeypatch.setattr(runtree_store, "MAX_RECORD_READ_BYTES", 4)

    with pytest.raises(SchemaRefusal, match="tree read limit"):
        tree.read_run()


def test_read_bytes_takes_an_explicit_ceiling_when_a_caller_asks_for_one(tmp_path):
    """A caller reading a JSON record through `read_bytes`, such as the fetch
    verb's `_fetched_manifest`, can ask for the record-sized ceiling rather than
    the blob-sized default, so the bytes it is about to parse are bounded by what
    a record can legitimately be.
    """
    tree = make_run(tmp_path)
    envelope = make_envelope()
    tree.publish_artifact(envelope)
    relative = tree.artifact_path(DESIGNATOR, "proposal", envelope["artifact_id"])
    size = tree.resolve(relative).stat().st_size
    assert size > 4

    assert len(tree.read_bytes(relative, max_bytes=size)) == size
    with pytest.raises(SchemaRefusal, match="tree read limit"):
        tree.read_bytes(relative, max_bytes=4)


def test_a_manifest_refuses_an_unbounded_number_of_walk_entries(tmp_path, monkeypatch):
    tree = make_run(tmp_path)
    artifacts_root = tree.resolve(f"{writing_directory(DESIGNATOR)}/{ARTIFACTS_DIR}")
    artifacts_root.mkdir(parents=True)
    for name in ("one", "two", "three"):
        (artifacts_root / name).write_bytes(b"publication residue")
    monkeypatch.setattr(runtree_store, "_MAX_MANIFEST_WALK_ENTRIES", 2)

    with pytest.raises(SchemaRefusal, match="entry manifest walk limit") as refusal:
        tree.build_manifest(DESIGNATOR)
    # One listing carries every entry, so this is the per-directory guard in
    # `_listing_fd`. Named here so the cumulative case below cannot be mistaken
    # for a duplicate of it.
    assert not str(refusal.value).startswith("artifact inventory")


def test_a_manifest_bounds_the_whole_walk_not_only_each_directory(tmp_path, monkeypatch):
    """The cumulative walk bound is a guard in its own right.

    Every directory here lists under the limit, so the per-directory refusal
    never fires and only the running `examined` count in `_walk_artifact_json`
    can stop the walk. Both bounds read `_MAX_MANIFEST_WALK_ENTRIES`, so without
    a case that separates them an implementation that counted per directory and
    reset between them would satisfy the suite while an artifact tree of
    unbounded *total* size walked to the end.
    """
    tree = make_run(tmp_path)
    artifacts_root = tree.resolve(f"{writing_directory(DESIGNATOR)}/{ARTIFACTS_DIR}")
    for directory in ("first", "second"):
        nested = artifacts_root / directory
        nested.mkdir(parents=True)
        for name in ("one", "two"):
            (nested / name).write_bytes(b"publication residue")
    # Six entries in all; no single directory lists more than two.
    monkeypatch.setattr(runtree_store, "_MAX_MANIFEST_WALK_ENTRIES", 3)

    with pytest.raises(SchemaRefusal, match="artifact inventory exceeds"):
        tree.build_manifest(DESIGNATOR)


def test_a_manifest_rechecks_containment_after_collecting_walk_members(tmp_path, monkeypatch):
    """A path cannot become an out-of-tree link between the walk and its one read."""
    tree = make_run(tmp_path)
    envelope = make_envelope()
    tree.publish_artifact(envelope)
    artifact = tree.resolve(tree.artifact_path(DESIGNATOR, "proposal", envelope["artifact_id"]))
    outside = tmp_path / "outside-after-walk.json"
    outside.write_bytes(canonical_bytes(envelope))
    real_walk = tree._walk_artifact_json

    def replace_after_walk(directory):
        yield from real_walk(directory)
        artifact.unlink()
        artifact.symlink_to(outside)

    monkeypatch.setattr(tree, "_walk_artifact_json", replace_after_walk)

    with pytest.raises(SchemaRefusal, match="resolves outside the run tree"):
        tree.build_manifest(DESIGNATOR)


# --- Inventory scope -----------------------------------------------------------


def test_every_path_the_store_can_write_is_inside_the_inventory_scope(tmp_path):
    """Every managed output path the store can write resolves inside the inventory
    scope, naming the path that does not.

    Driven against real writes rather than a list of strings, so a new writer that
    forgot to extend the scope is caught by what it actually does.
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
    # Not just "in scope" like every other entry below: the receipt is a
    # replace-in-place write, so its exact location is pinned against the
    # module's own named constant.
    assert written[-1] == runtree_store.RECENSOR_PARTITION_RECEIPT_FILE

    assert len(written) == 8
    for path in written:
        assert any(path == prefix or path.startswith(prefix) for prefix in scope), (
            f"{path} is written by the store but falls outside the inventory scope"
        )


# --- The commit and the clock a tree carries ------------------------------------


def test_a_run_authority_seals_the_commit_the_code_that_created_it_ran_at(tmp_path):
    """A fetched tree could prove its config bytes and not say which code made them."""

    tree = make_run(tmp_path, repository_commit=COMMIT)

    assert tree.read_run()["repository_commit"] == COMMIT


def test_an_authority_without_a_commit_is_still_a_whole_authority(tmp_path):
    """A source export with no version control records no commit, not a placeholder."""

    assert "repository_commit" not in make_run(tmp_path).read_run()


# `"g" * 40` is the case the others cannot make: a validator checking only
# length and case would accept it, so without it nothing here establishes that
# the revision must be hexadecimal.
@pytest.mark.parametrize("value", ["a1b2c3d", "A" * 40, "g" * 40, "", COMMIT + "-dirty"])
def test_a_commit_that_is_not_a_full_lowercase_revision_is_refused(tmp_path, value):
    with pytest.raises(SchemaRefusal, match="forty lowercase hexadecimal"):
        make_run(tmp_path, repository_commit=value)


@pytest.mark.parametrize("writer", ["_publish_bytes", "_atomic_write"])
def test_store_writers_refuse_paths_outside_inventory(tmp_path, writer):
    tree = make_run(tmp_path)
    with pytest.raises(SchemaRefusal, match="outside this run tree's inventory scope"):
        getattr(tree, writer)("unmanaged/artifact.json", b"payload")
    assert not tree.resolve("unmanaged/artifact.json").exists()


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


def test_the_inventory_scope_names_the_serving_log_directory_the_launcher_writes(tmp_path):
    """The scope covers every managed path any code writes, not only this store's.

    A stage that serves a chair leaves the engine's launch log at
    `<stage>/serving-logs/<name>.log`, written by the serving launcher. A consumer
    reading the scope as the whole of what a run tree may hold, such as
    `operator.surface._fetch_run_tree`, would otherwise refuse the served run tree
    at the first log it listed.
    """
    from common.contracts.stages import WRITING_DIRECTORIES

    tree = make_run(tmp_path)
    scope = tree.inventory_scope()
    for directory in sorted(set(WRITING_DIRECTORIES.values())):
        prefix = f"{directory}/{runtree_store.SERVING_LOGS_DIR}/"
        assert prefix in scope, f"{prefix} is written in the tree but falls outside the scope"
        log = f"{prefix}vllm-attestator_1-0123456789ab.log"
        assert any(log.startswith(item) for item in scope)
    # And by the expression the stages actually call, per stage, not only by
    # directory: `serving_log_path` takes a *stage*, and the two differ, so a
    # stage name passed where the writing directory was wanted (`attestatores`
    # for `3_attestatores`) would put every engine log outside the scope.
    for stage in sorted(WRITING_DIRECTORIES):
        assert f"{tree.serving_log_path(stage)}/" in scope, (
            f"{stage} would write its engine log outside the inventory scope"
        )


def test_a_serving_log_is_not_inventoried_as_evidence(tmp_path):
    """In scope so it can be accounted for; in no manifest, because nothing digested it.

    `build_manifest` walks `<stage>/artifacts` and the blob inventory
    `<stage>/blobs`, so an engine still writing its log while the stage seals
    cannot make the witnessed inventory false -- which is the whole reason the
    log may sit inside the tree at all.
    """
    tree = make_run(tmp_path)
    tree.publish_artifact(make_envelope())
    before = tree.build_manifest(DESIGNATOR)

    logs = tree.resolve(f"{writing_directory(DESIGNATOR)}/{runtree_store.SERVING_LOGS_DIR}")
    logs.mkdir(parents=True)
    (logs / "vllm-designator-0123456789ab.log").write_bytes(b"INFO: engine started")

    assert tree.build_manifest(DESIGNATOR) == before
    assert not any(
        runtree_store.SERVING_LOGS_DIR in entry["relative_path"]
        for entry in before["artifacts"] + before["blobs"]
    )


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
    Evidence is what must never be overwritten; this is not evidence, and the
    refusal protected nothing while blocking recovery.

    The refusal that *does* matter — a valid receipt disagreeing about the sealed
    unit denominator — is pinned by the test above and is unaffected.
    """
    tree = make_run(tmp_path)
    receipt = make_recensor_partition_receipt()
    assert tree.write_recensor_partition_receipt(receipt).reused is False

    target = tree.resolve(tree.recensor_partition_receipt_path())
    # Several shapes reaching different refusal paths: the empty and truncated
    # files fail the JSON reader; the float and wrong-self-hash cases both reach
    # `verify_self_hash`, where the float is refused by strict canonicalization
    # with a `TypeError` and the latter by the validator's integrity refusal.
    valid = json.dumps(receipt).encode("utf-8")
    float_damaged = json.loads(valid)
    float_damaged["expected_unit_count"] = 1.0
    self_hash_damaged = json.loads(valid)
    self_hash_damaged["self_hash"] = "0" * 64
    damage = {
        "empty": b"",
        "truncated-json": b"{",
        "wrong-schema": b'{"schema": "nonsense"}',
        "non-utf8": b"\xff\xfe not utf-8",
        "float": json.dumps(float_damaged).encode("utf-8"),
        "wrong-self-hash": json.dumps(self_hash_damaged).encode("utf-8"),
        # The `RecursionError` branch of the writer's except clause: `json.loads`
        # raises it rather than `ValueError` at this depth.
        "deeply-nested": (b"[" * 200_000) + (b"]" * 200_000),
    }[damage_kind]
    target.write_bytes(damage)
    assert tree.write_recensor_partition_receipt(receipt).reused is False, (
        f"a receipt damaged as {damage_kind} blocked its own replacement"
    )
    assert tree.read_recensor_partition_receipt()["run_id"] == "r1"


def test_an_artifact_too_deeply_nested_for_the_json_reader_is_refused_not_a_crash(tmp_path):
    """`json`'s scanner recurses once per nesting level, so a deeply nested artifact
    is refused rather than raising a traceback through every caller; since
    `build_manifest` reads every artifact under a directory, one such file would
    otherwise stop the whole stage rather than its own record.

    The depth is driven deliberately deep rather than pinned to the scanner's
    failure depth, and which refusal fires is not asserted: some interpreters parse
    this depth and refuse the missing fields instead. Both are refusals, which is
    the guarantee."""
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
    already closed the door.
    """
    tree = make_run(tmp_path)
    envelope = make_envelope()
    tree.publish_artifact(envelope)
    path = tree.resolve(tree.artifact_path(DESIGNATOR, "proposal", envelope["artifact_id"]))

    # Built as raw text and spliced in, rather than handed to `json.dumps` as a
    # 2,000-deep object. The encoder recurses per level exactly as the scanner
    # does, so constructing the fixture that way makes the *setup* depend on the
    # interpreter's recursion limit — and a fixture that raises during setup is a
    # false failure reporting nothing about the code, the same family as the
    # interpreter dependence that broke the test above on a 3.14 host.
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
    # a skip is visible where a silent pass is not. So the premise
    # is asserted first, against the same walk the code uses.
    try:
        parsed_deep = json.loads(deep_text)
    except RecursionError:
        pytest.skip(
            f"this interpreter's JSON scanner cannot parse {nesting} levels, so this "
            "case would exercise the reader-side guard instead of the canonical walk"
        )
    try:
        canonical_bytes(parsed_deep)
    except TypeError as error:
        assert "nests too deeply" in str(error)
    else:
        pytest.skip(
            f"this interpreter's canonical walk absorbs {nesting} levels, so the "
            "guarded path is unreachable here and this test proves nothing"
        )

    with pytest.raises(SchemaRefusal, match="fails its self-hash"):
        tree.build_manifest(DESIGNATOR)


def test_the_shared_snapshot_fails_loudly_on_a_descendant_it_cannot_read(tmp_path):
    """A subtree the walk cannot open is reported, never silently omitted.

    `os.walk`'s default drops an unreadable descendant and moves on, so a
    refusal probe comparing two snapshots would see "no change" over entries
    it never examined.
    """
    root = tmp_path / "root"
    locked = root / "locked"
    locked.mkdir(parents=True)
    (locked / "inside").write_bytes(b"x")
    locked.chmod(0)
    try:
        if os.access(locked, os.R_OK):  # pragma: no cover - running as root
            pytest.skip(
                f"this process (uid={os.getuid()}) can read a mode-000 directory; "
                "the case cannot be built"
            )
        with pytest.raises(OSError):
            tree_snapshot(root)
    finally:
        locked.chmod(0o700)


# -- the directory entry a publication creates, and its durability --


def _fsync_kinds(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record whether each real fsync was of a regular file or of a directory."""

    observed: list[str] = []
    real_fsync = os.fsync

    def watching(descriptor: int) -> None:
        try:
            observed.append("dir" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file")
        except OSError:  # pragma: no cover - fstat on a live descriptor
            observed.append("unknown")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", watching)
    return observed


@pytest.mark.parametrize("publish", ["_replace_file", "_atomic_create"])
def test_publication_syncs_the_file_then_the_name(
    tmp_path: Path, publish: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bytes, then the final name, then the directory entry that points at it.

    `fsync` on the artifact persists its *bytes*; the entry naming them is a
    separate call, and without it a power cut can leave a published artifact
    with its data intact and its name gone.
    """

    observed = _fsync_kinds(monkeypatch)
    target = tmp_path / "artifact.json"

    getattr(runtree_store, publish)(target, b'{"a":1}')

    assert observed == ["file", "dir"]
    assert target.read_bytes() == b'{"a":1}'
    # Nothing is left behind for a resume to trip over.
    assert [entry.name for entry in tmp_path.iterdir()] == ["artifact.json"]


@pytest.mark.parametrize("publish", ["_replace_file", "_atomic_create"])
def test_a_filesystem_that_will_not_persist_a_name_refuses_the_publication(
    tmp_path: Path, publish: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A durability the store cannot prove is refused, never reported as success.

    Strict deliberately: this tree is the evidence, and a stage that says an
    artifact was published — with a resume that then trusts it — is exactly what
    a lost directory entry betrays. The refusal says the bytes *are* published,
    because the operator's repair is to move the run root, not to hunt for a
    partial file.
    """

    real_fsync = os.fsync

    def refusing(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(errno.EINVAL, "Invalid argument")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", refusing)
    target = tmp_path / "artifact.json"

    with pytest.raises(SchemaRefusal) as refused:
        getattr(runtree_store, publish)(target, b'{"a":1}')

    assert "will not persist a directory entry" in str(refused.value)
    assert "artifact.json is in the run root" in str(refused.value)
    # And it really is published: the caller may retry, and the retry publishes
    # identical bytes rather than finding a half-written file.
    assert target.read_bytes() == b'{"a":1}'


def test_a_directory_that_cannot_be_opened_refuses_the_publication_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other of `sync_directory`'s two failure points, named the same way."""

    real_open = os.open

    def refusing(path, flags, *arguments, **keywords):  # type: ignore[no-untyped-def]
        if Path(path) == tmp_path:
            raise OSError(errno.EACCES, "Permission denied")
        return real_open(path, flags, *arguments, **keywords)

    monkeypatch.setattr(os, "open", refusing)

    with pytest.raises(SchemaRefusal, match="will not persist a directory entry"):
        runtree_store._replace_file(tmp_path / "artifact.json", b'{"a":1}')


def test_a_tampered_run_receipt_is_refused_when_its_reference_is_read(tmp_path):
    tree = make_run(tmp_path)
    reference, _ = tree.write_run_receipt(make_receipt())
    tree.resolve(reference.relative_path).write_text("{}", encoding="utf-8")

    with pytest.raises(SchemaRefusal) as caught:
        tree.read_run_receipt(reference)
    assert "digest" in str(caught.value)


# --- Refusal paths: every one named, none a hang or a traceback -----------------


@pytest.fixture
def no_hang():
    """Fail a test that blocks, rather than hanging the suite, on a FIFO read."""
    signal = pytest.importorskip("signal")
    if not hasattr(signal, "setitimer"):
        pytest.skip("this platform has no interval timer to bound a blocking read")

    def expire(_signum, _frame):
        raise AssertionError("the read blocked on a non-regular file")

    previous = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, 5)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


requires_fifo = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no os.mkfifo here")


@requires_fifo
def test_an_artifact_input_replaced_by_a_fifo_is_refused_not_a_hang(tmp_path, no_hang):
    tree = make_run(tmp_path)
    digest, blob = tree.put_blob(DESIGNATOR, b"a crop")
    subject = "pg_0123456789abcdef"
    envelope = build_envelope(
        run_id="r1",
        artifact_id=artifact_id(DESIGNATOR, "proposal", subject),
        subject_id=subject,
        stage=DESIGNATOR,
        kind="proposal",
        outcome="proposed",
        config_digest=CONFIG_DIGEST,
        adapter_revision="fake-designator-v0",
        inputs=[{"relative_path": blob.relative_path, "sha256": digest}],
        payload={"proposals": 2},
    )
    tree.publish_artifact(envelope)
    blob_file = tree.resolve(blob.relative_path)
    blob_file.unlink()
    os.mkfifo(blob_file)

    with pytest.raises(SchemaRefusal, match="artifact input .* could not be read"):
        tree.read_artifact(DESIGNATOR, "proposal", envelope["artifact_id"])


@requires_fifo
def test_publishing_onto_a_fifo_is_refused_and_leaves_nothing_behind(tmp_path, no_hang):
    tree = make_run(tmp_path)
    data = b"a crop"
    fifo = tree.resolve(tree.blob_path(DESIGNATOR, digest_bytes(data)))
    fifo.parent.mkdir(parents=True)
    os.mkfifo(fifo)

    with pytest.raises(IncompatibleReuse, match="could not be read"):
        tree.put_blob(DESIGNATOR, data)

    assert [entry.name for entry in fifo.parent.iterdir()] == [fifo.name]
    assert stat.S_ISFIFO(fifo.lstat().st_mode)


DEEP_RECORD = (b"[" * 200_000) + (b"]" * 200_000)


def _plant(tree, relative_path, data):
    target = tree.resolve(relative_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def _requires_recursion_error():
    try:
        json.loads(DEEP_RECORD)
    except RecursionError:
        return
    pytest.skip("this interpreter's JSON scanner parses this depth, so the guard is unreachable")


def test_a_deeply_nested_run_receipt_is_a_named_refusal(tmp_path):
    _requires_recursion_error()
    tree = make_run(tmp_path)
    digest = digest_bytes(DEEP_RECORD)
    relative_path = tree.receipt_path(digest)
    _plant(tree, relative_path, DEEP_RECORD)

    with pytest.raises(SchemaRefusal, match="run receipt .* could not be read"):
        tree.read_run_receipt(runtree_store.RunReceiptReference(relative_path, digest))


def test_a_run_receipt_that_is_not_json_is_a_named_refusal(tmp_path):
    tree = make_run(tmp_path)
    data = b"\xff not json"
    digest = digest_bytes(data)
    relative_path = tree.receipt_path(digest)
    _plant(tree, relative_path, data)

    with pytest.raises(SchemaRefusal, match="run receipt .* could not be read"):
        tree.read_run_receipt({"relative_path": relative_path, "sha256": digest})


def test_a_deeply_nested_approval_record_is_a_named_refusal(tmp_path):
    _requires_recursion_error()
    tree = make_run(tmp_path)
    digest = digest_bytes(DEEP_RECORD)
    relative_path = tree.receipt_path(digest)
    _plant(tree, relative_path, DEEP_RECORD)

    with pytest.raises(ApprovalRefusal, match="approval record .* could not be read"):
        tree.read_approval_record(ApprovalRecordReference(relative_path, digest))


def test_a_deeply_nested_referenced_artifact_is_a_named_refusal(tmp_path):
    _requires_recursion_error()
    tree = make_run(tmp_path)
    relative_path = tree.artifact_path(DESIGNATOR, "proposal", "deep")
    _plant(tree, relative_path, DEEP_RECORD)
    reference = {"relative_path": relative_path, "sha256": digest_bytes(DEEP_RECORD)}

    with pytest.raises(SchemaRefusal, match="is not valid JSON evidence"):
        tree.read_artifact_reference(reference, stage=DESIGNATOR, kind="proposal")


def test_record_references_are_read_under_the_record_ceiling(tmp_path, monkeypatch):
    tree = make_run(tmp_path)
    receipt, _ = tree.write_run_receipt(make_receipt())
    approval, _ = tree.write_approval_record(make_approval_record())
    envelope = make_envelope()
    published = tree.publish_artifact(envelope)
    reference = {
        "relative_path": published.relative_path,
        "sha256": digest_bytes(canonical_bytes(envelope)),
    }
    monkeypatch.setattr(runtree_store, "MAX_RECORD_READ_BYTES", 4)

    with pytest.raises(SchemaRefusal, match="4-byte tree read limit"):
        tree.read_run_receipt(receipt)
    with pytest.raises(SchemaRefusal, match="4-byte tree read limit"):
        tree.read_approval_record(approval)
    with pytest.raises(SchemaRefusal, match="4-byte tree read limit"):
        tree.read_artifact_reference(reference, stage=DESIGNATOR, kind="proposal")


@pytest.mark.parametrize("config_digest", ("C" * 64, "c" * 63, "not-a-digest"))
def test_a_malformed_config_digest_is_refused_before_anything_is_written(tmp_path, config_digest):
    with pytest.raises(SchemaRefusal, match="config_digest must be a lowercase sha256"):
        make_run(tmp_path, config_digest=config_digest)
    assert not (tmp_path / "r1").exists()


@pytest.mark.parametrize(
    "chairs",
    (
        ["attestator_1", "attestator_1"],
        ["attestator_1", ""],
        ["attestator_1", 2],
        "attestator_1",
    ),
)
def test_a_malformed_witness_roster_is_refused_before_anything_is_written(tmp_path, chairs):
    with pytest.raises(SchemaRefusal, match="witness_chairs must be a list of distinct"):
        make_run(tmp_path, witness_chairs=chairs)
    assert not (tmp_path / "r1").exists()


@pytest.mark.parametrize("render_settings", ({}, "300dpi"))
def test_render_settings_must_be_a_non_empty_object(tmp_path, render_settings):
    with pytest.raises(SchemaRefusal, match="render_settings must be a non-empty object"):
        make_run(tmp_path, render_settings=render_settings)


@pytest.mark.parametrize(
    ("digests", "message"),
    (
        ({}, "sealed_config_digests must be a non-empty object"),
        (["a" * 64], "sealed_config_digests must be a non-empty object"),
        ({"": "a" * 64}, "every sealed configuration digest"),
        ({"policy": "A" * 64}, "every sealed configuration digest"),
    ),
)
def test_sealed_config_digests_must_name_lowercase_sha256s(tmp_path, digests, message):
    with pytest.raises(SchemaRefusal, match=message):
        make_run(tmp_path, sealed_config_digests=digests)


def test_reuse_refuses_a_run_sealed_under_another_seal_method(tmp_path):
    sealed = {"policy": "a" * 64}
    tree = make_run(tmp_path, sealed_config_digests=sealed)
    run_file = tree.root / RUN_FILE
    record = json.loads(run_file.read_bytes())
    del record["self_hash"]
    del record[runtree_store.SEAL_METHOD_FIELD]
    record["self_hash"] = self_hash(record)
    run_file.write_bytes(canonical_bytes(record))

    with pytest.raises(IncompatibleReuse, match="no recorded method"):
        make_run(tmp_path, sealed_config_digests=sealed)


def test_a_partition_receipt_from_another_run_authority_is_refused(tmp_path):
    tree = make_run(tmp_path)
    foreign = make_recensor_partition_receipt(config_digest="d" * 64)
    with pytest.raises(SchemaRefusal, match="does not belong to this run authority"):
        tree.write_recensor_partition_receipt(foreign)

    other = make_run(tmp_path / "other", config_digest="d" * 64)
    other.write_recensor_partition_receipt(foreign)
    _plant(
        tree,
        tree.recensor_partition_receipt_path(),
        other.resolve(other.recensor_partition_receipt_path()).read_bytes(),
    )
    with pytest.raises(SchemaRefusal, match="does not belong to this run authority"):
        tree.read_recensor_partition_receipt()


@pytest.mark.parametrize(
    ("relative_path", "sha256", "message"),
    (
        ("", "a" * 64, "has no relative_path"),
        (None, "a" * 64, "has no relative_path"),
        (f"{RECEIPTS_DIR}/{'a' * 64}.json", "A" * 64, "has no lowercase sha256"),
    ),
)
def test_an_approval_reference_without_a_path_or_digest_is_refused(
    tmp_path, relative_path, sha256, message
):
    tree = make_run(tmp_path)
    with pytest.raises(ApprovalRefusal, match=message):
        tree.read_approval_record(ApprovalRecordReference(relative_path, sha256))


# --- review_decision_records: every stored approval checked, none skipped ----------------


def _review_decision(decision="hold"):
    from common.contracts.approval import build_review_decision_record

    return build_review_decision_record(
        run_id="r1",
        scope="unit",
        subject_id="act_0123456789abcdef",
        page_id="pg_0123456789abcdef",
        decision=decision,
        finding="text-misread" if decision == "hold" else None,
        basis_digest="c" * 64,
        reason="held by the test",
        timestamp="2026-10-01T12:00:00Z",
    )


def test_review_decision_records_returns_decisions_and_passes_over_valid_receipts(tmp_path):
    tree = make_run(tmp_path)
    tree.write_run_receipt(make_receipt())
    tree.write_approval_record(make_approval_record())
    reference, _ = tree.write_approval_record(_review_decision())

    found = tree.review_decision_records()

    assert [ref.to_record() for ref, _record in found] == [reference.to_record()]
    assert found[0][1]["review"]["decision"] == "hold"


def test_an_edited_hold_decision_is_refused_not_skipped(tmp_path):
    """Edited in place to another schema, it no longer hashes to its name."""
    tree = make_run(tmp_path)
    reference, _ = tree.write_approval_record(_review_decision())
    path = tree.resolve(reference.relative_path)
    edited = json.loads(path.read_bytes())
    edited["schema"] = "approval-record.v9"
    path.write_bytes(canonical_bytes(edited))

    with pytest.raises(SchemaRefusal, match="digest"):
        tree.review_decision_records()


def test_a_renamed_edited_decision_that_is_no_approval_and_no_receipt_is_refused(tmp_path):
    """Re-addressed under its new digest, it is still neither record this directory holds."""
    tree = make_run(tmp_path)
    reference, _ = tree.write_approval_record(_review_decision())
    path = tree.resolve(reference.relative_path)
    edited = json.loads(path.read_bytes())
    edited["schema"] = "approval-record.v9"
    data = canonical_bytes(edited)
    path.unlink()
    tree.resolve(tree.receipt_path(digest_bytes(data))).write_bytes(data)

    with pytest.raises(ApprovalRefusal, match="neither a sound approval record"):
        tree.review_decision_records()


def test_a_decision_failing_its_self_hash_is_refused(tmp_path):
    tree = make_run(tmp_path)
    record = _review_decision()
    record["reason"] = "edited after it was recorded"
    data = canonical_bytes(record)
    target = tree.resolve(tree.receipt_path(digest_bytes(data)))
    target.parent.mkdir(parents=True)
    target.write_bytes(data)

    with pytest.raises(ApprovalRefusal):
        tree.review_decision_records()


def test_a_decision_not_stored_as_its_canonical_bytes_is_refused(tmp_path):
    """Its readers cite it by the digest of its canonical form, which must name its file."""
    tree = make_run(tmp_path)
    data = json.dumps(_review_decision(), indent=2).encode("utf-8")
    target = tree.resolve(tree.receipt_path(digest_bytes(data)))
    target.parent.mkdir(parents=True)
    target.write_bytes(data)

    with pytest.raises(ApprovalRefusal, match="canonical bytes"):
        tree.review_decision_records()


def test_a_stray_name_in_the_receipts_directory_is_refused_even_with_no_decision(tmp_path):
    """A renamed decision would otherwise vanish from every reader, releasing what it held."""
    tree = make_run(tmp_path)
    tree.write_run_receipt(make_receipt())
    reference, _ = tree.write_approval_record(_review_decision())
    path = tree.resolve(reference.relative_path)
    path.rename(path.with_name("held.json"))

    with pytest.raises(SchemaRefusal, match="not a content-addressed receipt"):
        tree.review_decision_records()


def test_a_decision_renamed_to_a_dot_file_is_refused(tmp_path):
    """Hidden by a leading dot, a decision is still no unfinished write to pass over."""
    tree = make_run(tmp_path)
    reference, _ = tree.write_approval_record(_review_decision())
    path = tree.resolve(reference.relative_path)
    path.rename(path.with_name(f".{path.name}"))

    with pytest.raises(SchemaRefusal, match="not a content-addressed receipt"):
        tree.review_decision_records()


def test_an_unfinished_write_in_the_receipts_directory_is_passed_over(tmp_path):
    """Only what an interrupted publication leaves, `.<name>.tmp-<unique>`, is skipped."""
    tree = make_run(tmp_path)
    reference, _ = tree.write_approval_record(_review_decision())
    path = tree.resolve(reference.relative_path)
    path.with_name(f".{path.name}.tmp-abc123").write_bytes(b"{")

    assert [ref.to_record() for ref, _ in tree.review_decision_records()] == [reference.to_record()]
