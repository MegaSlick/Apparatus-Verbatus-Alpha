"""Spec 10, test 1: the only constructor accepts a Recensor-accepted, primed
Perlectio, and nothing else can reach it.

`RunTree.read_artifact_reference` checks a reference's declared stage and kind
against its actual bytes, which makes "a Testimonium cannot reach it" and "a
salvage-tier piece cannot reach it" one proven property rather than two. Nothing
in this build publishes a salvage-tier artifact (`common/contracts/approval.py`
names `salvage-promotion` as a future approval action and nothing more), so the
second test forges a plausible one — otherwise the check could be passing by
special-casing Testimonium and nobody would know.

**A Lectio nuda, and what can honestly be claimed about it.** ARCHITECTURE and
GLOSSARY define one as an unprimed Perlectio, an instrument record that must
never establish. Today's Perlectio schema records no primed/unprimed
discriminator at all — adding it is Perlector-lane scope — so the boundary
refuses an *explicitly* unprimed reading and treats a retained Testimonium basis
as the transitional indication that a reading was primed. The refusals are real
and tested; the positive claim "this reading was primed" rests on the producer
eventually writing the field, and is named as an assumption rather than dressed
up as a proof.
"""

import json
import subprocess
import sys
from pathlib import Path

from common.contracts.canonical import canonical_bytes, digest_bytes, self_hash
from common.contracts.envelope import build_envelope
from common.contracts.identities import artifact_id
from common.contracts.stages import ARCHETYPUS, ATTESTATORES, RECENSOR
from common.runtree.store import RunTree

ROOT = Path(__file__).resolve().parents[2]


def invoke(
    root: Path, run_id: str, scenario: str, program: str, **extra
) -> subprocess.CompletedProcess:
    command = [
        sys.executable,
        str(ROOT / program),
        "--run-root",
        str(root),
        "--run-id",
        run_id,
        "--scenario",
        scenario,
    ]
    for key, value in extra.items():
        command.extend((f"--{key.replace('_', '-')}", str(value)))
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True)


def run_through_recensor(root: Path, run_id: str, scenario: str = "happy") -> None:
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/2_designator/run.py",
        "pipeline/3_attestatores/run.py",
        "pipeline/4_perlector/run.py",
        "pipeline/5_recensor/run.py",
    ):
        result = invoke(root, run_id, scenario, program)
        assert result.returncode in (0, 3), f"{program}: {result.stderr}"


def accepted_review(tree: RunTree) -> dict:
    entry = next(
        entry
        for entry in tree.build_manifest(RECENSOR)["artifacts"]
        if entry["kind"] == "review" and entry["outcome"] == "accepted"
    )
    return tree.read_artifact(RECENSOR, "review", entry["artifact_id"])


def _repoint_review(tree: RunTree, review: dict, forged_ref: dict) -> None:
    """Rewrite an accepted review's perlectio_ref to a forged reference, sealed."""
    review_path = tree.resolve(tree.artifact_path(RECENSOR, "review", review["artifact_id"]))
    old_ref = review["payload"]["perlectio_ref"]
    review["inputs"] = [
        forged_ref if reference == old_ref else reference for reference in review["inputs"]
    ]
    review["payload"]["perlectio_ref"] = forged_ref
    review["self_hash"] = self_hash(review)
    review_path.write_bytes(canonical_bytes(review))


def test_a_testimonium_cannot_substitute_for_a_perlectio_reference(tmp_path):
    """A real, validly-sealed Testimonium is refused as an establishing reading."""
    root = tmp_path / "runs"
    run_through_recensor(root, "r")
    tree = RunTree(root, "r")
    review = accepted_review(tree)

    testimonium_entry = next(
        entry
        for entry in tree.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] == "testimonium" and entry["outcome"] == "read"
    )
    testimonium_path = tree.resolve(testimonium_entry["relative_path"])
    forged_ref = {
        "relative_path": testimonium_entry["relative_path"],
        "sha256": digest_bytes(testimonium_path.read_bytes()),
    }
    _repoint_review(tree, review, forged_ref)

    result = invoke(root, "r", "happy", "pipeline/6_archetypus/run.py")
    assert result.returncode == 2, result.stderr
    assert "Traceback" not in result.stderr
    assert "not required 'perlector'/'perlectio'" in result.stderr


def test_a_wrong_stage_artifact_cannot_substitute_for_a_perlectio_reference(tmp_path):
    """A plausible salvage-tier-shaped artifact is refused the same way.

    No concrete salvage-tier artifact kind exists in this build (see module
    docstring), so this forges a well-formed envelope under a stage/kind this
    pipeline never actually writes -- proving `read_artifact_reference`'s
    stage/kind check is a structural property of the reference boundary, not a
    Testimonium special case.
    """
    root = tmp_path / "runs"
    run_through_recensor(root, "r")
    tree = RunTree(root, "r")
    review = accepted_review(tree)
    run = tree.read_run()

    forged_act_id = review["subject_id"]
    forged_payload = {"note": "a hypothetical salvage-tier piece, never an establishing read"}
    forged_payload["self_hash"] = self_hash(forged_payload)
    envelope = build_envelope(
        run_id="r",
        artifact_id=artifact_id(RECENSOR, "salvage-piece", forged_act_id),
        subject_id=forged_act_id,
        stage=RECENSOR,
        kind="salvage-piece",
        outcome="accepted",
        config_digest=run["config_digest"],
        adapter_revision=run["adapter_recipes"][RECENSOR],
        inputs=[],
        payload=forged_payload,
    )
    forged_path = tree.resolve(
        tree.artifact_path(RECENSOR, "salvage-piece", envelope["artifact_id"])
    )
    forged_path.parent.mkdir(parents=True, exist_ok=True)
    forged_path.write_bytes(canonical_bytes(envelope))
    forged_ref = {
        "relative_path": tree.artifact_path(RECENSOR, "salvage-piece", envelope["artifact_id"]),
        "sha256": digest_bytes(forged_path.read_bytes()),
    }
    _repoint_review(tree, review, forged_ref)

    result = invoke(root, "r", "happy", "pipeline/6_archetypus/run.py")
    assert result.returncode == 2, result.stderr
    assert "Traceback" not in result.stderr
    assert "not required 'perlector'/'perlectio'" in result.stderr


def test_only_an_accepted_review_ever_produces_an_archetypus_record(tmp_path):
    """Every non-`accepted` current review in a run -- `held-for-review`, in the
    review scenario -- leaves its act with no Archetypus record at all."""
    root = tmp_path / "runs"
    run_through_recensor(root, "r", scenario="review")
    result = invoke(root, "r", "review", "pipeline/6_archetypus/run.py")
    assert result.returncode == 0, result.stderr
    tree = RunTree(root, "r")

    reviews = [
        tree.read_artifact(RECENSOR, "review", entry["artifact_id"])
        for entry in tree.build_manifest(RECENSOR)["artifacts"]
        if entry["kind"] == "review"
    ]
    non_accepted_subjects = {
        record["subject_id"] for record in reviews if record["outcome"] != "accepted"
    }
    assert non_accepted_subjects, "the review scenario must exercise a non-accepted outcome"

    established_subjects = {
        entry["subject_id"]
        for entry in tree.build_manifest(ARCHETYPUS)["artifacts"]
        if entry["kind"] == "archetypus"
    }
    assert non_accepted_subjects.isdisjoint(established_subjects)


# --- The grafted half: refusals that do not depend on the reference's stage/kind


def _reseal_reading(tree: RunTree, review: dict, mutate) -> None:
    """Mutate the reviewed Perlectio's payload and reseal the chain around it."""
    review_path = tree.resolve(tree.artifact_path(RECENSOR, "review", review["artifact_id"]))
    old_ref = review["payload"]["perlectio_ref"]
    reading_path = tree.resolve(old_ref["relative_path"])
    reading = json.loads(reading_path.read_text(encoding="utf-8"))
    mutate(reading["payload"])
    reading["self_hash"] = self_hash(reading)
    reading_path.write_bytes(canonical_bytes(reading))

    new_ref = {
        "relative_path": old_ref["relative_path"],
        "sha256": digest_bytes(reading_path.read_bytes()),
    }
    review["inputs"] = [
        new_ref if reference == old_ref else reference for reference in review["inputs"]
    ]
    review["payload"]["perlectio_ref"] = new_ref
    review["self_hash"] = self_hash(review)
    review_path.write_bytes(canonical_bytes(review))


def _archetypus_after(tmp_path: Path, mutate) -> subprocess.CompletedProcess:
    root = tmp_path / "runs"
    run_through_recensor(root, "r")
    tree = RunTree(root, "r")
    _reseal_reading(tree, accepted_review(tree), mutate)
    return invoke(root, "r", "happy", "pipeline/6_archetypus/run.py")


def test_an_explicitly_unprimed_lectio_kind_cannot_establish(tmp_path):
    result = _archetypus_after(tmp_path, lambda payload: payload.update(lectio_kind="nuda"))
    assert result.returncode == 2, result.stderr
    assert "Traceback" not in result.stderr
    assert "Lectio nuda is an instrument record" in result.stderr


def test_an_unrecognised_lectio_kind_cannot_establish_either(tmp_path):
    """Unlabeled is tolerated while the producer has no field; *mislabeled* is not."""
    result = _archetypus_after(tmp_path, lambda payload: payload.update(lectio_kind="unlabeled"))
    assert result.returncode == 2, result.stderr
    assert "only an explicitly primed" in result.stderr


def test_a_primed_false_flag_cannot_establish(tmp_path):
    result = _archetypus_after(tmp_path, lambda payload: payload.update(primed=False))
    assert result.returncode == 2, result.stderr
    assert "non-primed Lectio" in result.stderr


def test_salvage_tier_material_can_never_establish(tmp_path):
    """Invariant #31's boundary, refused by name at the last stage that could
    turn it into text. Nothing publishes a salvage tier today, so this proves the
    refusal exists rather than that it currently fires on real material."""
    for field in ("tier", "source_tier", "reading_tier"):
        result = _archetypus_after(
            tmp_path / field, lambda payload, f=field: payload.update({f: "salvage"})
        )
        assert result.returncode == 2, result.stderr
        assert "salvage-tier material" in result.stderr


def test_a_reading_with_no_retained_witness_basis_at_all_cannot_establish(tmp_path):
    def strip_witnesses(payload):
        payload["basis"] = dict(payload["basis"], testimonia=[])

    result = _archetypus_after(tmp_path, strip_witnesses)
    assert result.returncode == 2, result.stderr
    assert "Lectio nuda by any other name" in result.stderr


def test_a_witness_basis_reference_the_reading_never_input_cannot_establish(tmp_path):
    """A basis entry naming a Testimonium the reading does not directly bind is
    testimony nobody can prove was shown to that reader."""

    def detach(payload):
        testimonia = [dict(item) for item in payload["basis"]["testimonia"]]
        testimonia[0]["reference"] = {
            "relative_path": "3_attestatores/artifacts/testimonium/art_ffffffffffffffff.json",
            "sha256": "f" * 64,
        }
        payload["basis"] = dict(payload["basis"], testimonia=testimonia)

    result = _archetypus_after(tmp_path, detach)
    assert result.returncode == 2, result.stderr
    assert "not a digest-checked direct input" in result.stderr


def test_a_region_carrying_an_extra_field_cannot_enter_the_record(tmp_path):
    """The closed field set has to reach inside `regions`, or it answers nothing.

    A region is embedded from the reading verbatim, self-hashed into the record,
    and copied field-for-field into the terminal export. So the record's own
    top-level closed schema — advertised as the mechanical answer to "is there a
    second text-bearing field?" — used to say nothing about the one sub-object it
    embeds whole. `consolidated_literal` is the first name in the old pipeline's
    dead fallback chain, and it travelled: into the sealed record, past the
    Armarium, into the delivered export beside the established text.
    """

    def smuggle(payload):
        regions = [dict(region) for region in payload["basis"]["regions"]]
        regions[0]["consolidated_literal"] = "A SECOND READING NOBODY ESTABLISHED"
        payload["basis"] = dict(payload["basis"], regions=regions)

    result = _archetypus_after(tmp_path, smuggle)
    assert result.returncode == 2, result.stderr
    assert "Traceback" not in result.stderr
    assert "outside the closed region schema" in result.stderr
    assert "consolidated_literal" in result.stderr


def test_a_region_missing_one_of_its_crop_facts_cannot_enter_the_record(tmp_path):
    """Closed both ways: an absent field is refused as loudly as an extra one."""

    def strip(payload):
        regions = [dict(region) for region in payload["basis"]["regions"]]
        del regions[0]["verified_dimensions"]
        payload["basis"] = dict(payload["basis"], regions=regions)

    result = _archetypus_after(tmp_path, strip)
    assert result.returncode == 2, result.stderr
    assert "verified_dimensions" in result.stderr


def test_a_region_declaring_a_digest_its_crop_does_not_have_cannot_establish(tmp_path):
    """The one stage that makes the record immutable used to check neither side.

    The Recensor checks a declared crop digest against the Designator's own
    region record; the Armarium checks it against the crop bytes at export. In
    between, this stage sealed whatever the reading declared — so a record could
    be written, write-once, naming ink it does not point at, and the run could
    then only be abandoned rather than repaired.
    """

    def relabel(payload):
        regions = [dict(region) for region in payload["basis"]["regions"]]
        regions[0]["image_sha256"] = "f" * 64
        payload["basis"] = dict(payload["basis"], regions=regions)

    result = _archetypus_after(tmp_path, relabel)
    assert result.returncode == 2, result.stderr
    assert "Traceback" not in result.stderr
    assert "naming ink it does not point at" in result.stderr


def test_one_crop_named_by_two_regions_is_refused_before_the_seal(tmp_path):
    """Two regions naming one crop path is refused here, not accommodated.

    Blobs are content-addressed, so a recovery crop whose pixels match its
    proposal crop *is* the same file, and this stage could combine the two
    regions' evidence by path and seal a record either way. But the Armarium's
    frozen `verify_established_record` builds its own expected input set as one
    reference **per region**, undeduplicated, and compares full sorted-list
    equality — so a record naming fewer distinct paths than it has regions
    establishes here (`archetypus` exit 0) and is then refused at export
    (`FatalAccounting: an Archetypus input set does not reconcile to its parent
    evidence`), after the write-once seal, where it can only be abandoned. The
    Perlector already refuses this shape at publish
    (`validate_input_refs`, "input reference ... is listed twice"); refusing it
    here too closes the gap between the two real refusals instead of sealing an
    unexportable record in between them.
    """

    def name_the_same_crop_twice(payload):
        regions = payload["basis"]["regions"]
        payload["basis"] = dict(payload["basis"], regions=regions + [dict(regions[0])])

    root = tmp_path / "runs"
    run_through_recensor(root, "r")
    tree = RunTree(root, "r")
    review = accepted_review(tree)
    _reseal_reading(tree, review, name_the_same_crop_twice)

    result = invoke(root, "r", "happy", "pipeline/6_archetypus/run.py")
    assert result.returncode == 2, result.stderr
    assert "Traceback" not in result.stderr
    assert "already named by region" in result.stderr


def test_one_testimonium_cannot_be_repeated_to_make_the_basis_look_larger(tmp_path):
    def repeat(payload):
        testimonia = [dict(item) for item in payload["basis"]["testimonia"]]
        testimonia.append(dict(testimonia[0]))
        payload["basis"] = dict(payload["basis"], testimonia=testimonia)

    result = _archetypus_after(tmp_path, repeat)
    assert result.returncode == 2, result.stderr
    assert "repeats Testimonium basis" in result.stderr


def test_two_groups_naming_one_crop_path_collapse_to_a_single_input():
    """`3618414` claimed removing this deduplication fails a test. It did not.

    That commit's closing line said "every simplification above was
    mutation-checked afterwards: deleting the read-back delegation, the
    readable-span rule, the deduplication, or the witness-roster branch each
    fails a test." Measured on 2026-08-11 by replacing `_direct_inputs`'s
    dedup-by-path with a plain concatenation: `pytest pipeline/6_archetypus
    pipeline/orchestrator` stayed **entirely green**. The claim was false and the
    guard did not exist.

    What it guards is narrower than first claimed, and the function's own
    docstring says so: `_crop_references` already refuses two *regions* naming
    one crop path before this function runs, so the identical-pixels recovery
    crop aborts the stage either way and the dedup cannot rescue it. What the
    collapse actually covers is the cross-group case -- a review or Perlectio
    reference coinciding with a crop path -- which the run tree's layout makes
    structurally impossible today. The dedup is the cheap defensive form of
    that layout guarantee, and this test pins the collapse plus the
    no-distinct-input-dropped half so the defence cannot rot unnoticed.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "archetypus_direct_inputs_under_test", ROOT / "pipeline" / "6_archetypus" / "run.py"
    )
    archetypus = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(archetypus)

    shared = {"relative_path": "2_designator/blobs/ab/cdef", "sha256": "a" * 64}
    other = {"relative_path": "4_perlector/artifacts/reading.json", "sha256": "b" * 64}

    combined = archetypus._direct_inputs([shared, other], [shared])

    paths = [reference["relative_path"] for reference in combined]
    assert len(paths) == len(set(paths)), (
        f"one crop path reached the envelope twice: {paths}; build_envelope refuses "
        "a path listed twice, so the defensive collapse must hold"
    )
    assert set(paths) == {shared["relative_path"], other["relative_path"]}, (
        "collapsing duplicates must not drop a distinct input"
    )
