"""Spec 10, test 1: the only constructor accepts a Recensor-accepted, primed
Perlectio, and nothing else can reach it.

`RunTree.read_artifact_reference` checks a reference's declared stage and kind
against its actual bytes, which makes "a Testimonium cannot reach it" and "a
salvage-tier piece cannot reach it" one proven property rather than two. Nothing
in this build publishes a salvage-tier artifact (`common/contracts/approval.py`
names `salvage-promotion` as a future approval action and nothing more), so the
second test forges a plausible one — otherwise the check could be passing by
special-casing Testimonium and nobody would know.

The constructor accepts the production kind for the recorded draft view;
controls and un-fed instruments are distinct artifact kinds and cannot
establish by a relabel.
"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
import reseal_chain
import stage_driver

from common.contracts.canonical import canonical_bytes, digest_bytes, digest_of, self_hash
from common.contracts.errors import FatalAccounting
from common.contracts.stages import ARCHETYPUS, PERLECTOR, RECENSOR
from common.runtree.store import RunTree
from common.stage import EXIT_HELD
from common.witness_regime import pseudonym_for
from conftest import load_stage

ROOT = Path(__file__).resolve().parents[2]


# One shared subprocess driver for this directory's test files, for the same
# reason as the shared reseal chain below: two private copies drift.
invoke = stage_driver.invoke
run_through_recensor = stage_driver.run_through_recensor


def accepted_review(tree: RunTree) -> dict:
    entry = next(
        entry
        for entry in tree.build_manifest(RECENSOR)["artifacts"]
        if entry["kind"] == "review" and entry["outcome"] == "accepted"
    )
    return tree.read_artifact(RECENSOR, "review", entry["artifact_id"])


# One shared reseal chain for every forgery in this directory; three private
# copies would drift apart the first time the envelope gains a bound field.
_repoint_review = reseal_chain.repoint_review


def _orchestrate(
    root: Path, run_id: str, scenario: str, *, blind_read: str = "off"
) -> subprocess.CompletedProcess:
    """A whole run, recovery drain included, for the one test that needs a
    second reading attempt to exist before it can forge anything."""
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline" / "orchestrator" / "run.py"),
            "--fixture",
            "synthetic-two-page-v0",
            "--scenario",
            scenario,
            "--run-id",
            run_id,
            "--run-root",
            str(root),
            *(("--blind-read", blind_read) if blind_read != "off" else ()),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


@pytest.mark.act_path
def test_only_an_accepted_review_ever_produces_an_archetypus_record(tmp_path):
    """Every non-`accepted` current review in a run leaves its act with no
    Archetypus record at all.

    Run straight through the Recensor with no orchestrator recovery drain, the
    review scenario's held act sits at `recovery-requested` when stage 6 runs —
    an unresolved outcome, so the stage reports EXIT_HELD rather than a green
    exit over an act nobody has decided. The property under test is unchanged:
    no non-accepted review produces a record.
    """
    root = tmp_path / "runs"
    run_through_recensor(root, "r", scenario="review")
    result = invoke(root, "r", "review", "pipeline/6_archetypus/run.py")
    assert result.returncode == 3, result.stderr
    assert "outstanding recovery request" in result.stderr
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


@pytest.mark.act_path
def test_an_outstanding_recovery_request_is_held_not_silently_skipped(tmp_path):
    """An unresolved act must not disappear into a green exit.

    `held-for-review` and `confirmed-blank` are terminal at the Recensor -- the
    outcome algebra resolves their category with no Archetypus record, and
    skipping them IS the correct accounting. `recovery-requested` is different
    in kind: the algebra leaves it unresolved ("flows onward, nobody has
    decided yet") and the Armarium treats the same state as fatal. A stage that
    skipped it and exited 0 would report success over an act whose reread never
    happened -- ARCHITECTURE invariant 6 by way of an exit code.
    """
    root = tmp_path / "runs"
    run_through_recensor(root, "r")
    tree = RunTree(root, "r")
    review = accepted_review(tree)
    review_path = tree.resolve(tree.artifact_path(RECENSOR, "review", review["artifact_id"]))
    review["outcome"] = "recovery-requested"
    review["self_hash"] = self_hash(review)
    review_path.write_bytes(canonical_bytes(review))
    reseal_chain._rebind_stage_seal(tree, RECENSOR)

    result = invoke(root, "r", "happy", "pipeline/6_archetypus/run.py")
    assert result.returncode == 3, result.stderr
    assert "Traceback" not in result.stderr
    assert "outstanding recovery request" in result.stderr
    # Establishment for still-accepted acts is real; only the unresolved act
    # has no record. The exit code, not silence, carries "not finished".
    established_subjects = {
        entry["subject_id"]
        for entry in tree.build_manifest(ARCHETYPUS)["artifacts"]
        if entry["kind"] == "archetypus"
    }
    assert review["subject_id"] not in established_subjects


# --- The grafted half: refusals that do not depend on the reference's stage/kind


_reseal_reading = reseal_chain.reseal_reviewed_reading


def _archetypus_after(tmp_path: Path, mutate, *, fed: bool = False) -> subprocess.CompletedProcess:
    root = tmp_path / "runs"
    run_through_recensor(root, "r", blind_read="fed" if fed else "off")
    tree = RunTree(root, "r")
    _reseal_reading(tree, accepted_review(tree), mutate)
    return invoke(
        root, "r", "happy", "pipeline/6_archetypus/run.py", **({"blind_read": "fed"} if fed else {})
    )


@pytest.mark.act_path
def test_an_explicitly_unprimed_lectio_kind_cannot_establish(tmp_path):
    result = _archetypus_after(tmp_path, lambda payload: payload.update(lectio_kind="nuda"))
    assert result.returncode == 2, result.stderr
    assert "Traceback" not in result.stderr
    assert "Lectio nuda is an instrument record" in result.stderr


@pytest.mark.act_path
def test_an_unrecognised_lectio_kind_cannot_establish_either(tmp_path):
    result = _archetypus_after(tmp_path, lambda payload: payload.update(lectio_kind="unlabeled"))
    assert result.returncode == 2, result.stderr
    assert "Traceback" not in result.stderr
    assert "only an explicitly primed" in result.stderr


@pytest.mark.act_path
def test_withheld_draft_cannot_establish_with_self_revisions(tmp_path):
    def invent_revision(payload):
        assert payload["lectio_kind"] == "primed-draft-withheld"
        payload["self_revision"] = [
            {"reading_span": {"start": 0, "end": 1}, "testimonium_span": {"start": 0, "end": 1}}
        ]

    result = _archetypus_after(tmp_path, invent_revision)
    assert result.returncode == 2, result.stderr
    assert "against a draft withheld" in result.stderr


@pytest.mark.act_path
def test_fed_kind_cannot_relabel_a_withheld_reading(tmp_path):
    result = _archetypus_after(
        tmp_path, lambda payload: payload.update(lectio_kind="primed-with-prior")
    )
    assert result.returncode == 2, result.stderr
    assert "without a fed prior-draft view" in result.stderr


@pytest.mark.act_path
def test_withheld_kind_cannot_contradict_its_protocol_record(tmp_path):
    result = _archetypus_after(
        tmp_path, lambda payload: payload["protocol"].update(blind_read="fed")
    )
    assert result.returncode == 2, result.stderr
    assert "contrary to its prior-draft protocol" in result.stderr


@pytest.mark.act_path
def test_fed_claim_without_a_prior_reference_cannot_establish(tmp_path):
    result = _archetypus_after(
        tmp_path, _reseal_dossier(lambda dossier: dossier.pop("prior_draft")), fed=True
    )
    assert result.returncode == 2, result.stderr
    assert "Traceback" not in result.stderr
    assert "claims primed-with-prior but carries no prior-draft reference" in result.stderr


@pytest.mark.act_path
def test_withheld_claim_carrying_a_prior_reference_cannot_establish(tmp_path):
    """A withheld run makes no Pass A, so a prior beside that view is a defect."""

    def add_prior_reference(payload):
        prior = {"reference": payload["basis"]["testimonia"][0]["reference"], "text": "x"}
        _reseal_dossier(lambda dossier: dossier.update(prior_draft=prior))(payload)

    result = _archetypus_after(tmp_path, add_prior_reference)
    assert result.returncode == 2, result.stderr
    assert "Traceback" not in result.stderr
    assert "claims primed-draft-withheld but carries a prior-draft reference" in result.stderr
    assert "must be re-read" in result.stderr


@pytest.mark.act_path
def test_a_withheld_reading_listing_a_lectio_prior_among_its_inputs_cannot_establish(tmp_path):
    root = tmp_path / "runs"
    run_through_recensor(root, "r", blind_read="saved")
    tree = RunTree(root, "r")
    prior = next(
        entry
        for entry in tree.build_manifest(PERLECTOR)["artifacts"]
        if entry["kind"] == "lectio-prior"
    )
    path = f"4_perlector/artifacts/lectio-prior/{prior['artifact_id']}.json"
    prior_ref = {
        "relative_path": path,
        "sha256": hashlib.sha256(tree.resolve(path).read_bytes()).hexdigest(),
    }
    _reseal_reading(
        tree,
        accepted_review(tree),
        lambda payload: None,
        lambda reading: reading["inputs"].append(prior_ref),
    )
    result = invoke(root, "r", "happy", "pipeline/6_archetypus/run.py", blind_read="saved")
    assert result.returncode == 2, result.stderr
    assert "lists a lectio-prior among its inputs" in result.stderr


@pytest.mark.act_path
def test_a_withheld_claim_carrying_an_empty_prior_key_cannot_establish(tmp_path):
    """Key presence decides, as in the producer, not the value under it."""
    result = _archetypus_after(
        tmp_path, _reseal_dossier(lambda dossier: dossier.update(prior_draft=None))
    )
    assert result.returncode == 2, result.stderr
    assert "carries a prior-draft reference" in result.stderr


def test_a_withheld_run_establishes_with_no_lectio_prior_on_disk(tmp_path):
    root = tmp_path / "runs"
    run_through_recensor(root, "r")
    tree = RunTree(root, "r")
    assert not any(
        entry["kind"] == "lectio-prior" for entry in tree.build_manifest(PERLECTOR)["artifacts"]
    )
    result = invoke(root, "r", "happy", "pipeline/6_archetypus/run.py")
    assert result.returncode == 0, result.stderr


@pytest.mark.act_path
def test_a_saved_run_establishes_beside_its_lectio_prior_without_citing_it(tmp_path):
    root = tmp_path / "runs"
    run_through_recensor(root, "r", blind_read="saved")
    tree = RunTree(root, "r")
    assert any(
        entry["kind"] == "lectio-prior" for entry in tree.build_manifest(PERLECTOR)["artifacts"]
    )
    result = invoke(root, "r", "happy", "pipeline/6_archetypus/run.py", blind_read="saved")
    assert result.returncode == 0, result.stderr
    reading = json.loads(
        tree.resolve(accepted_review(tree)["payload"]["perlectio_ref"]["relative_path"]).read_text(
            encoding="utf-8"
        )
    )
    assert reading["payload"]["protocol"]["blind_read"] == "saved"
    assert "prior_draft" not in reading["payload"]["dossier"]
    assert not any("lectio-prior" in ref["relative_path"] for ref in reading["inputs"])


@pytest.mark.act_path
def test_a_fed_run_establishes_and_references_its_lectio_prior(tmp_path):
    root = tmp_path / "runs"
    run_through_recensor(root, "r", blind_read="fed")
    result = invoke(root, "r", "happy", "pipeline/6_archetypus/run.py", blind_read="fed")
    assert result.returncode == 0, result.stderr
    tree = RunTree(root, "r")
    reading = json.loads(
        tree.resolve(accepted_review(tree)["payload"]["perlectio_ref"]["relative_path"]).read_text(
            encoding="utf-8"
        )
    )
    assert reading["payload"]["dossier"]["prior_draft"]["reference"] in reading["inputs"]


@pytest.mark.act_path
def test_primed_without_prior_claim_with_a_prior_reference_cannot_establish(tmp_path):
    result = _archetypus_after(
        tmp_path,
        lambda payload: payload.update(lectio_kind="primed-without-prior"),
        fed=True,
    )
    assert result.returncode == 2, result.stderr
    assert "Traceback" not in result.stderr
    assert "claims primed-without-prior but carries a prior-draft reference" in result.stderr


@pytest.mark.act_path
def test_embedded_prior_text_must_match_the_referenced_lectio_prior(tmp_path):
    def diverge_prior_text(payload):
        def forge(dossier):
            dossier["prior_draft"] = {**dossier["prior_draft"], "text": "forged"}

        _reseal_dossier(forge)(payload)

    result = _archetypus_after(tmp_path, diverge_prior_text, fed=True)
    assert result.returncode == 2, result.stderr
    assert "Traceback" not in result.stderr
    assert "disagrees with its referenced lectio-prior" in result.stderr


@pytest.mark.act_path
def test_embedded_page_witness_count_must_match_the_act_attachment(tmp_path):
    def forge_count(payload):
        dossier = dict(payload["dossier"])
        attachment = dict(dossier["act_attachment"])
        attachment["page_witness_count"] += 1
        dossier["act_attachment"] = attachment
        dossier["dossier_digest"] = digest_of(
            {key: value for key, value in dossier.items() if key != "dossier_digest"}
        )
        payload["dossier"] = dossier

    result = _archetypus_after(tmp_path, forge_count)
    assert result.returncode == 2, result.stderr
    assert "embedded page-witness count disagrees with its attachment" in result.stderr


def _reseal_dossier(mutate_dossier):
    def mutate(payload):
        dossier = dict(payload["dossier"])
        mutate_dossier(dossier)
        dossier["dossier_digest"] = digest_of(
            {key: value for key, value in dossier.items() if key != "dossier_digest"}
        )
        payload["dossier"] = dossier

    return mutate


@pytest.mark.act_path
def test_a_dossier_under_an_unrecognized_witness_regime_cannot_establish(tmp_path):
    """An unknown regime has no safe chair-to-label rule or default."""
    result = _archetypus_after(
        tmp_path, _reseal_dossier(lambda dossier: dossier.update(witness_regime="anonymous"))
    )
    assert result.returncode == 2, result.stderr
    assert "Traceback" not in result.stderr
    # Pinned to the message this check owns: "which is not one of" is also
    # produced by the provenance-regime and established-text-status refusals,
    # so the bare fragment could be satisfied by a refusal about neither.
    assert "embeds a dossier under witness regime" in result.stderr


@pytest.mark.act_path
def test_a_blinded_comparison_view_may_not_wear_a_label_the_dossier_never_carried(tmp_path):
    """A syntactically plausible pseudonym is not evidence of a dossier witness.

    The regime is blinded and the testimonia are blinded with it, so the refusal
    can only come from the forged label. Written the short way -- regime flipped
    to blinded while the rows stayed named -- every real pseudonym was already
    absent from `labels`, the check fired on that alone, and the forgery was
    inert: the assertion passed without ever testing what the name claims.
    """
    root = tmp_path / "runs"
    run_through_recensor(root, "r")
    tree = RunTree(root, "r")
    config_digest = tree.read_run()["config_digest"]

    def blind_and_forge(payload):
        dossier = dict(payload["dossier"])
        dossier["witness_regime"] = "blinded"
        rows = []
        for row in dossier["testimonia"]:
            copied = dict(row)
            copied["witness_label"] = pseudonym_for(
                row["witness_label"], run_id="r", config_digest=config_digest
            )
            rows.append(copied)
        attachment = dict(dossier["act_attachment"])
        named_views = attachment["comparison_views"]
        # Forge the row for a chair the attachment actually names. The testimonia
        # are the wider list, so blanking an arbitrary row proves nothing: the
        # attachment's labels would all still be carried and the run establishes.
        attached = {
            pseudonym_for(chair, run_id="r", config_digest=config_digest) for chair in named_views
        }
        forged = next(row for row in rows if row["witness_label"] in attached)
        forged["witness_label"] = "witness-000000000000"
        dossier["testimonia"] = sorted(rows, key=lambda row: row["witness_label"])

        attachment["comparison_views"] = {
            pseudonym_for(chair, run_id="r", config_digest=config_digest): text
            for chair, text in named_views.items()
        }
        dossier["act_attachment"] = attachment
        dossier["dossier_digest"] = digest_of(
            {key: value for key, value in dossier.items() if key != "dossier_digest"}
        )
        payload["dossier"] = dossier

    _reseal_reading(tree, accepted_review(tree), blind_and_forge)
    result = invoke(root, "r", "happy", "pipeline/6_archetypus/run.py")

    assert result.returncode == 2, result.stderr
    assert "Traceback" not in result.stderr
    assert "name no witness this dossier carries" in result.stderr


@pytest.mark.act_path
def test_blinded_comparison_views_cannot_exchange_valid_witness_labels(tmp_path):
    """A text multiset cannot prove attribution when valid pseudonyms are swapped."""
    root = tmp_path / "runs"
    run_through_recensor(root, "r")
    tree = RunTree(root, "r")
    config_digest = tree.read_run()["config_digest"]

    def blind_and_swap(payload):
        dossier = dict(payload["dossier"])
        dossier["witness_regime"] = "blinded"
        rows = []
        for row in dossier["testimonia"]:
            copied = dict(row)
            copied["witness_label"] = pseudonym_for(
                row["witness_label"], run_id="r", config_digest=config_digest
            )
            rows.append(copied)
        dossier["testimonia"] = sorted(rows, key=lambda row: row["witness_label"])

        attachment = dict(dossier["act_attachment"])
        named_views = attachment["comparison_views"]
        blinded_views = {
            pseudonym_for(chair, run_id="r", config_digest=config_digest): text
            for chair, text in named_views.items()
        }
        labels = sorted(blinded_views)
        assert len(labels) >= 2, "the fixture must carry two attached page witnesses"
        # If the two carried the same text the swap would be a no-op and the
        # refusal below would have to come from somewhere else, leaving the
        # attribution claim this test names unproven.
        assert blinded_views[labels[0]] != blinded_views[labels[1]], (
            "the swap must actually exchange two different readings"
        )
        blinded_views[labels[0]], blinded_views[labels[1]] = (
            blinded_views[labels[1]],
            blinded_views[labels[0]],
        )
        attachment["comparison_views"] = blinded_views
        dossier["act_attachment"] = attachment
        dossier["dossier_digest"] = digest_of(
            {key: value for key, value in dossier.items() if key != "dossier_digest"}
        )
        payload["dossier"] = dossier

    _reseal_reading(tree, accepted_review(tree), blind_and_swap)
    result = invoke(root, "r", "happy", "pipeline/6_archetypus/run.py")

    assert result.returncode == 2, result.stderr
    assert "Traceback" not in result.stderr
    assert "embedded comparison views disagree with its attachment" in result.stderr


@pytest.mark.act_path
def test_a_reading_may_not_be_accounted_to_a_page_none_of_its_regions_cites(tmp_path):
    """The attachment subject must be among the pages the reading actually cites."""

    def repage_regions(payload):
        basis = dict(payload["basis"])
        basis["regions"] = [
            {**region, "source_page_id": "page_ffffffffffffffff"} for region in basis["regions"]
        ]
        payload["basis"] = basis

    result = _archetypus_after(tmp_path, repage_regions)
    assert result.returncode == 2, result.stderr
    assert "Traceback" not in result.stderr
    assert "none of its basis regions cites" in result.stderr


@pytest.mark.act_path
def test_a_prior_draft_from_another_reading_attempt_cannot_establish(tmp_path):
    """The last unbound relation in the series above.

    The prior reference is bound to stage, kind, subject and digest, and its
    embedded text must equal the referenced record's. None of that bound the
    *attempt*. A recovered act carries one Pass-A draft per attempt, and the two
    drafts ordinarily read alike -- recovery recovers coverage, not text -- so a
    Perlectio citing the superseded draft satisfied every other check while
    publishing `self_revision` against a draft its reader never saw. This uses
    the `review` scenario because it is the one that genuinely reads an act
    twice; the stale reference here is a real sibling artifact, not a forgery
    of a shape the pipeline never writes.
    """
    root = tmp_path / "runs"
    # The orchestrator, not the raw stage sequence: `review`'s second attempt
    # only exists after the recovery drain, and that drain is the orchestrator's.
    orchestrated = _orchestrate(root, "r", "review", blind_read="fed")
    assert orchestrated.returncode == EXIT_HELD, orchestrated.stderr
    tree = RunTree(root, "r")
    # The orchestrator already established this act honestly. Clear that record
    # so stage 6 re-derives from the forged chain on a clean slate: otherwise a
    # regression here would be stopped by artifact immutability rather than by
    # the refusal under test, and the red proof would not distinguish them.
    for entry in tree.build_manifest(ARCHETYPUS)["artifacts"]:
        tree.resolve(entry["relative_path"]).unlink()
    review = accepted_review(tree)
    act_id = review["subject_id"]

    reading_ref = review["payload"]["perlectio_ref"]
    reading_path = tree.resolve(reading_ref["relative_path"])
    reading = json.loads(reading_path.read_text(encoding="utf-8"))
    assert reading["payload"]["attempt_ordinal"] == 2, "review's reviewed reading is the reread"

    priors = [
        tree.read_artifact(PERLECTOR, "lectio-prior", entry["artifact_id"])
        for entry in tree.build_manifest(PERLECTOR)["artifacts"]
        if entry["kind"] == "lectio-prior" and entry["subject_id"] == act_id
    ]
    stale = next(prior for prior in priors if prior["payload"]["attempt_ordinal"] == 1)
    stale_path = tree.resolve(tree.artifact_path(PERLECTOR, "lectio-prior", stale["artifact_id"]))
    stale_ref = {
        "relative_path": tree.artifact_path(PERLECTOR, "lectio-prior", stale["artifact_id"]),
        "sha256": digest_bytes(stale_path.read_bytes()),
    }
    embedded = reading["payload"]["dossier"]["prior_draft"]
    assert stale["payload"]["text"] == embedded["text"], (
        "the two attempts' drafts must read alike here, or the text-equality check "
        "would fire and this test would prove nothing about the attempt binding"
    )

    current_ref = embedded["reference"]
    embedded["reference"] = stale_ref
    dossier = reading["payload"]["dossier"]
    dossier["dossier_digest"] = digest_of(
        {key: value for key, value in dossier.items() if key != "dossier_digest"}
    )
    reading["inputs"] = [
        stale_ref if reference == current_ref else reference for reference in reading["inputs"]
    ]
    reading["self_hash"] = self_hash(reading)
    reading_path.write_bytes(canonical_bytes(reading))
    _repoint_review(
        tree,
        review,
        {
            "relative_path": reading_ref["relative_path"],
            "sha256": digest_bytes(reading_path.read_bytes()),
        },
    )

    result = invoke(root, "r", "review", "pipeline/6_archetypus/run.py", blind_read="fed")
    assert result.returncode == 2, result.stderr
    assert "Traceback" not in result.stderr
    assert "cites a prior draft from reading attempt 1, not its own 2" in result.stderr


@pytest.mark.act_path
def test_a_prior_draft_with_no_attempt_ordinal_cannot_bind(tmp_path):
    """Two absent ordinals comparing None == None must not pass the attempt binding.

    The reading's own ordinal is proven by `latest_attempt`, but the constructor
    documents itself as the whole of the boundary, so the prior's side is held
    to be an integer by name rather than compared as whatever it is.
    """
    root = tmp_path / "runs"
    orchestrated = _orchestrate(root, "r", "review", blind_read="fed")
    assert orchestrated.returncode == EXIT_HELD, orchestrated.stderr
    tree = RunTree(root, "r")
    for entry in tree.build_manifest(ARCHETYPUS)["artifacts"]:
        tree.resolve(entry["relative_path"]).unlink()
    review = accepted_review(tree)
    act_id = review["subject_id"]

    reading_ref = review["payload"]["perlectio_ref"]
    reading_path = tree.resolve(reading_ref["relative_path"])
    reading = json.loads(reading_path.read_text(encoding="utf-8"))
    embedded = reading["payload"]["dossier"]["prior_draft"]

    current_ref = embedded["reference"]
    prior_path = tree.resolve(current_ref["relative_path"])
    prior = json.loads(prior_path.read_text(encoding="utf-8"))
    del prior["payload"]["attempt_ordinal"]
    prior["self_hash"] = self_hash(prior)
    prior_path.write_bytes(canonical_bytes(prior))
    resealed_ref = {
        "relative_path": current_ref["relative_path"],
        "sha256": digest_bytes(prior_path.read_bytes()),
    }

    embedded["reference"] = resealed_ref
    dossier = reading["payload"]["dossier"]
    dossier["dossier_digest"] = digest_of(
        {key: value for key, value in dossier.items() if key != "dossier_digest"}
    )
    reading["inputs"] = [
        resealed_ref if reference == current_ref else reference for reference in reading["inputs"]
    ]

    # Since R5b the audit chain seals its own references to the prior: the
    # audit draft binds it as an input, the finding binds the draft, and the
    # reading binds both. A forgery that stops at the reading is now caught
    # earlier by byte integrity ("the bytes changed under a sealed
    # reference") -- the right behaviour, but not the boundary THIS test
    # holds -- so the whole chain reseals to keep the ordinal check
    # reachable.
    audit_record = reading["payload"]["audit"]
    draft_ref = audit_record["draft_ref"]
    draft_path = tree.resolve(draft_ref["relative_path"])
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    draft["inputs"] = [
        resealed_ref if reference == current_ref else reference for reference in draft["inputs"]
    ]
    draft["self_hash"] = self_hash(draft)
    draft_path.write_bytes(canonical_bytes(draft))
    resealed_draft_ref = {
        "relative_path": draft_ref["relative_path"],
        "sha256": digest_bytes(draft_path.read_bytes()),
    }

    finding_ref = audit_record["finding_ref"]
    finding_path = tree.resolve(finding_ref["relative_path"])
    finding = json.loads(finding_path.read_text(encoding="utf-8"))
    finding["inputs"] = [
        resealed_draft_ref if reference == draft_ref else reference
        for reference in finding["inputs"]
    ]
    finding["self_hash"] = self_hash(finding)
    finding_path.write_bytes(canonical_bytes(finding))
    resealed_finding_ref = {
        "relative_path": finding_ref["relative_path"],
        "sha256": digest_bytes(finding_path.read_bytes()),
    }

    audit_record["draft_ref"] = resealed_draft_ref
    audit_record["finding_ref"] = resealed_finding_ref
    reading["inputs"] = [
        resealed_draft_ref
        if reference == draft_ref
        else (resealed_finding_ref if reference == finding_ref else reference)
        for reference in reading["inputs"]
    ]
    reading["self_hash"] = self_hash(reading)
    reading_path.write_bytes(canonical_bytes(reading))
    _repoint_review(
        tree,
        review,
        {
            "relative_path": reading_ref["relative_path"],
            "sha256": digest_bytes(reading_path.read_bytes()),
        },
    )

    result = invoke(root, "r", "review", "pipeline/6_archetypus/run.py", blind_read="fed")
    assert result.returncode == 2, result.stderr
    assert "Traceback" not in result.stderr
    assert f"act {act_id} carries a lectio-prior payload with no integer attempt ordinal" in (
        result.stderr
    )


@pytest.mark.act_path
def test_a_primed_false_flag_cannot_establish(tmp_path):
    result = _archetypus_after(tmp_path, lambda payload: payload.update(primed=False))
    assert result.returncode == 2, result.stderr
    assert "Traceback" not in result.stderr
    assert "non-primed Lectio" in result.stderr


@pytest.mark.act_path
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


@pytest.mark.act_path
def test_a_reading_with_no_retained_witness_basis_at_all_cannot_establish(tmp_path):
    def strip_witnesses(payload):
        payload["basis"] = dict(payload["basis"], testimonia=[])

    result = _archetypus_after(tmp_path, strip_witnesses)
    assert result.returncode == 2, result.stderr
    assert "Lectio nuda by any other name" in result.stderr


@pytest.mark.act_path
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


@pytest.mark.act_path
def test_a_region_carrying_an_extra_field_cannot_enter_the_record(tmp_path):
    """The closed field set has to reach inside `regions`, or it answers nothing.

    A region is embedded from the reading verbatim, self-hashed into the record,
    and copied field-for-field into the terminal export. The record's own
    top-level closed schema is the mechanical answer to "is there a second
    text-bearing field?", but that answer is worthless if it says nothing
    about the one sub-object the record embeds whole: a smuggled field there
    would still travel into the sealed record, past the Armarium, into the
    delivered export beside the established text.
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


@pytest.mark.act_path
def test_a_region_missing_one_of_its_crop_facts_cannot_enter_the_record(tmp_path):
    """Closed both ways: an absent field is refused as loudly as an extra one."""

    def strip(payload):
        regions = [dict(region) for region in payload["basis"]["regions"]]
        del regions[0]["verified_dimensions"]
        payload["basis"] = dict(payload["basis"], regions=regions)

    result = _archetypus_after(tmp_path, strip)
    assert result.returncode == 2, result.stderr
    assert "verified_dimensions" in result.stderr


@pytest.mark.act_path
def test_a_region_declaring_a_digest_its_crop_does_not_have_cannot_establish(tmp_path):
    """The one stage that makes the record immutable must check both sides too.

    The Recensor checks a declared crop digest against the Designator's own
    region record; the Armarium checks it against the crop bytes at export.
    Sealing whatever the reading declares in between would let a record be
    written, write-once, naming ink it does not point at, and the run could
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


@pytest.mark.act_path
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


@pytest.mark.act_path
def test_one_testimonium_cannot_be_repeated_to_make_the_basis_look_larger(tmp_path):
    def repeat(payload):
        testimonia = [dict(item) for item in payload["basis"]["testimonia"]]
        testimonia.append(dict(testimonia[0]))
        payload["basis"] = dict(payload["basis"], testimonia=testimonia)

    result = _archetypus_after(tmp_path, repeat)
    assert result.returncode == 2, result.stderr
    assert "repeats Testimonium basis" in result.stderr


def test_two_groups_naming_one_crop_path_collapse_to_a_single_input():
    """`_direct_inputs`'s dedup-by-path guards the cross-group case -- a review
    or Perlectio reference coinciding with a crop path -- which the run tree's
    layout makes structurally impossible today; `_crop_references` already
    refuses two *regions* naming one crop path before this function runs. The
    dedup is the cheap defensive form of that layout guarantee, and this test
    pins the collapse plus the no-distinct-input-dropped half so the defence
    cannot rot unnoticed.
    """
    archetypus = load_stage("6_archetypus")

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

    # The other half of the same function: one path cannot hold two sets of
    # bytes, so collapsing them silently would seal a record whose inputs the
    # Armarium cannot reconcile at export.
    conflicting = {"relative_path": shared["relative_path"], "sha256": "c" * 64}
    with pytest.raises(FatalAccounting, match="different digests"):
        archetypus._direct_inputs([shared], [conflicting])
