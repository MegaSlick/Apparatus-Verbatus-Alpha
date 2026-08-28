"""Lectio nuda: sampled, carries no testimonia, and structurally invisible to
every consumer that establishes text.

Spec_08: nuda "never establishes text: it is an instrument record with no path
to the Archetypus constructor" -- enforced here at the module boundary, not by
convention.
"""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from nuda import LECTIO_NUDA_KIND, SELECTION_RULE, is_nuda_sampled, sampling_design
from run import (
    NUDA_APPROVAL_SUBJECT,
    PERLECTOR_INSTRUMENT_APPROVAL_SUBJECT,
    resolve_sampling_approval,
)

from common.chairs import load_models_toml
from common.contracts.approval import (
    ApprovalRecordBinding,
    ApprovalRecordReference,
    build_approval_record,
)
from common.contracts.canonical import canonical_bytes, digest_bytes, self_hash
from common.contracts.errors import ContractError, SchemaRefusal
from common.contracts.identities import attempt_id
from common.contracts.stages import ARCHETYPUS, PERLECTOR
from common.runtree.store import RunTree
from common.stage import load_fixture, run_config_bindings

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"


# A stand-in for the reference Tyrel's own approval would carry. It is required
# whenever anything is sampled, so these tests must supply one exactly as a real
# run would; a test that could sample without it would be testing a pipeline
# nobody is allowed to run.
APPROVAL = NUDA_APPROVAL_SUBJECT


def nuda_approval_record(scenario: str, nuda_per_mille: int) -> dict:
    """The exact record a real experiment would predeclare for this run.

    Deterministic in its inputs, so a test can rebuild it and name the reference
    the Perlector is *supposed* to have resolved rather than reading whichever
    reference the run happened to publish.  The rate is threaded rather than
    assumed: it is inside `config_digest`, so a record built for 1000/1000 does
    not approve a run drawn at 500/1000.
    """
    bindings = run_config_bindings(
        load_models_toml(ROOT / "config" / "models.toml"),
        load_fixture(str(ROOT / "proof")),
        scenario,
        nuda_per_mille=nuda_per_mille,
        nuda_approval_ref=APPROVAL,
    )
    return build_approval_record(
        subject_ids=[APPROVAL],
        action="other",
        reason="test-only Lectio nuda sampling design",
        target_version_hash=bindings["config_digest"],
        timestamp="2026-08-21T00:00:00Z",
    )


def orchestrate(
    run_root: Path,
    run_id: str,
    scenario: str,
    *,
    nuda_per_mille: int = 0,
    approval_ref: str = APPROVAL,
):
    if nuda_per_mille and approval_ref == APPROVAL:
        RunTree(run_root, run_id).write_approval_record(
            nuda_approval_record(scenario, nuda_per_mille)
        )
    command = [
        sys.executable,
        str(ORCHESTRATOR),
        "--fixture",
        "synthetic-two-page-v0",
        "--scenario",
        scenario,
        "--run-id",
        run_id,
        "--run-root",
        str(run_root),
        "--nuda-per-mille",
        str(nuda_per_mille),
        "--nuda-approval-ref",
        approval_ref,
    ]
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True)


def perlectio_kind_artifacts(tree, act_id):
    """The exact query every real consumer runs: filtered to kind == 'perlectio'."""
    return [
        tree.read_artifact(PERLECTOR, "perlectio", entry["artifact_id"])
        for entry in tree.build_manifest(PERLECTOR)["artifacts"]
        if entry["kind"] == "perlectio" and entry["subject_id"] == act_id
    ]


@pytest.fixture(scope="module")
def nuda_run(tmp_path_factory):
    root = tmp_path_factory.mktemp("nuda") / "runs"
    result = orchestrate(root, "r", "happy", nuda_per_mille=1000)
    assert result.returncode == 0, result.stderr
    return RunTree(root, "r")


def test_nuda_per_mille_1000_samples_every_act(nuda_run):
    """1000 per mille means every act is sampled -- proof the sampling design
    itself is real, not merely that a switch exists."""
    entries = [
        entry
        for entry in nuda_run.build_manifest(PERLECTOR)["artifacts"]
        if entry["kind"] == LECTIO_NUDA_KIND
    ]
    subjects = {entry["subject_id"] for entry in entries}
    assert len(subjects) == 2, "both acts in the happy fixture should be nuda-sampled at 1000/1000"


def test_a_nuda_reading_carries_no_testimonia_at_all(nuda_run):
    entries = [
        entry
        for entry in nuda_run.build_manifest(PERLECTOR)["artifacts"]
        if entry["kind"] == LECTIO_NUDA_KIND
    ]
    assert entries, "a sampled run must publish nuda records before their contents can be tested"
    for entry in entries:
        record = nuda_run.read_artifact(PERLECTOR, LECTIO_NUDA_KIND, entry["artifact_id"])
        assert record["payload"]["dossier"]["testimonia"] == []
        assert record["payload"]["dissent"] == []


def test_a_nuda_attempt_uses_its_own_operation_never_perlegere(nuda_run):
    entries = [
        entry
        for entry in nuda_run.build_manifest(PERLECTOR)["artifacts"]
        if entry["kind"] == LECTIO_NUDA_KIND
    ]
    assert entries
    for entry in entries:
        record = nuda_run.read_artifact(PERLECTOR, LECTIO_NUDA_KIND, entry["artifact_id"])
        ordinal = record["payload"]["attempt_ordinal"]
        subject = record["subject_id"]
        assert record["attempt_id"] == attempt_id(subject, "lectio-nuda", ordinal)
        assert record["attempt_id"] != attempt_id(subject, "perlegere", ordinal)


def test_nuda_records_are_structurally_invisible_to_the_perlectio_kind_query(nuda_run):
    """The module boundary itself: every real consumer (Recensor, Archetypus,
    Armarium, the orchestrator's own recovery dispatch) filters on
    kind == "perlectio" before reading anything. Run the identical query and
    show the nuda record never appears in the result set."""
    subjects = {
        entry["subject_id"]
        for entry in nuda_run.build_manifest(PERLECTOR)["artifacts"]
        if entry["kind"] == LECTIO_NUDA_KIND
    }
    assert subjects
    for act_id in subjects:
        readings = perlectio_kind_artifacts(nuda_run, act_id)
        # Exactly the one real establishing Perlectio -- the nuda record for
        # the same act is not merely absent from a filtered subset, it never
        # entered the population the filter ran over.
        assert len(readings) == 1
        assert readings[0]["kind"] == "perlectio"


def test_nuda_never_disturbs_normal_establishment(nuda_run):
    """Both acts still reach the Archetypus exactly as the happy path always
    does; the instrument reading runs alongside establishment, never inside it."""
    established = [
        entry
        for entry in nuda_run.build_manifest(ARCHETYPUS)["artifacts"]
        if entry["kind"] == "archetypus"
    ]
    assert len(established) == 2


def test_a_forged_review_naming_a_nuda_artifact_as_its_perlectio_is_refused(nuda_run):
    """The negative path: even if some future code forged a Recensor-style
    reference pointing at a `lectio-nuda` artifact and called it a Perlectio,
    the digest-checked reference read is refused by kind, because
    `read_artifact_reference` requires an exact `kind="perlectio"` match."""
    entry = next(
        entry
        for entry in nuda_run.build_manifest(PERLECTOR)["artifacts"]
        if entry["kind"] == LECTIO_NUDA_KIND
    )
    reference = {
        "relative_path": entry["relative_path"],
        "sha256": entry["sha256"],
    }
    with pytest.raises(SchemaRefusal, match="not required 'perlector'/'perlectio'"):
        nuda_run.read_artifact_reference(
            reference, stage=PERLECTOR, kind="perlectio", subject_id=entry["subject_id"]
        )


def test_a_sampled_nuda_records_the_design_it_was_drawn_under(nuda_run):
    """GOVERNANCE 10: a sample of unknown design measures nothing. Each record
    names the rate, the selection rule, and the approval it was drawn under."""
    entry = next(
        entry
        for entry in nuda_run.build_manifest(PERLECTOR)["artifacts"]
        if entry["kind"] == LECTIO_NUDA_KIND
    )
    record = nuda_run.read_artifact(PERLECTOR, LECTIO_NUDA_KIND, entry["artifact_id"])
    # Deriving the expectation from the predeclared record keeps a wrong approval
    # reference in the payload from validating itself.
    approval_digest = digest_bytes(canonical_bytes(nuda_approval_record("happy", 1000)))
    assert record["payload"]["sampling"] == {
        "nuda_per_mille": 1000,
        "selection_rule": SELECTION_RULE,
        "approval_ref": {
            "relative_path": f"receipts/sha256/{approval_digest}.json",
            "sha256": approval_digest,
        },
    }
    assert record["payload"]["sampling"]["approval_ref"] in record["inputs"]


def test_a_sampled_nuda_refuses_when_its_bound_approval_receipt_is_replaced(tmp_path):
    root = tmp_path / "runs"
    result = orchestrate(root, "r", "happy", nuda_per_mille=1000)
    assert result.returncode == 0, result.stderr
    tree = RunTree(root, "r")
    entry = next(
        entry
        for entry in tree.build_manifest(PERLECTOR)["artifacts"]
        if entry["kind"] == LECTIO_NUDA_KIND
    )
    record = tree.read_artifact(PERLECTOR, LECTIO_NUDA_KIND, entry["artifact_id"])
    approval_ref = record["payload"]["sampling"]["approval_ref"]
    tree.resolve(approval_ref["relative_path"]).write_bytes(b"{}")

    with pytest.raises(SchemaRefusal, match="digest"):
        tree.read_artifact(PERLECTOR, LECTIO_NUDA_KIND, entry["artifact_id"])


def test_a_run_may_not_sample_nuda_without_tyrels_predeclared_design(tmp_path):
    """Hard rule 1: the sampling design is his to approve. A rate with no
    approval reference refuses before anything is written, rather than drawing
    an instrument sample nobody asked for."""
    root = tmp_path / "runs"
    result = orchestrate(root, "r", "happy", nuda_per_mille=1000, approval_ref="")
    assert result.returncode != 0
    assert "sampling design selector" in result.stderr
    assert not (root / "r").exists()


def test_sampling_design_refuses_an_arbitrary_string_instead_of_an_approval_record():
    with pytest.raises(ValueError, match="arbitrary string is not an approval record"):
        sampling_design(nuda_per_mille=1000, approval_ref="not-a-record-or-digest")


def test_sampling_design_refuses_an_approval_for_the_other_experiment():
    reference = ApprovalRecordReference("receipts/sha256/" + "a" * 64 + ".json", "a" * 64)
    approval = ApprovalRecordBinding(
        reference,
        PERLECTOR_INSTRUMENT_APPROVAL_SUBJECT,
        "b" * 64,
    )
    with pytest.raises(ValueError, match="but its approval record names"):
        sampling_design(nuda_per_mille=1000, approval_ref=approval)


def _approval_context(tmp_path):
    config_digest = "a" * 64
    tree = RunTree.create(
        tmp_path,
        "approval-run",
        source_manifest=[],
        config_digest=config_digest,
        adapter_recipes={},
        witness_chairs=[],
    )
    return SimpleNamespace(tree=tree, config_digest=config_digest)


def _approval_record_for_digest(config_digest):
    """A record bound to a caller-supplied digest, for a synthetic RunTree.

    Distinct from `nuda_approval_record` above, which derives the real
    config_digest from `run_config_bindings` for an orchestrated run. Reaching
    for the wrong one binds the record to the wrong digest; the resolution gate
    then refuses, so the cost is debugging rather than a test that passes falsely."""
    return build_approval_record(
        subject_ids=[NUDA_APPROVAL_SUBJECT],
        action="other",
        reason="test-only Lectio nuda sampling design",
        target_version_hash=config_digest,
        timestamp="2026-08-21T00:00:00Z",
    )


def test_nuda_approval_binds_its_experiment_subject_and_sealed_config_digest(tmp_path):
    context = _approval_context(tmp_path)
    record = _approval_record_for_digest(context.config_digest)
    expected, _ = context.tree.write_approval_record(record)

    actual = resolve_sampling_approval(
        context, approval_ref=NUDA_APPROVAL_SUBJECT, subject=NUDA_APPROVAL_SUBJECT
    )

    assert actual.reference.to_record() == expected.to_record()
    assert actual.subject == NUDA_APPROVAL_SUBJECT
    assert actual.target_version_hash == context.config_digest


def test_a_missing_approval_names_where_the_record_goes_and_what_it_must_approve(tmp_path):
    """Late refusal must name both the receipt location and required version."""
    context = _approval_context(tmp_path)

    with pytest.raises(ContractError) as refusal:
        resolve_sampling_approval(
            context, approval_ref=NUDA_APPROVAL_SUBJECT, subject=NUDA_APPROVAL_SUBJECT
        )

    message = str(refusal.value)
    assert "receipts/sha256/" in message
    assert context.config_digest in message
    assert "action 'other'" in message


def test_nuda_approval_refuses_a_record_for_a_different_sealed_config_digest(tmp_path):
    context = _approval_context(tmp_path)
    context.tree.write_approval_record(_approval_record_for_digest("b" * 64))

    with pytest.raises(ContractError, match="not this run's sealed config_digest"):
        resolve_sampling_approval(
            context, approval_ref=NUDA_APPROVAL_SUBJECT, subject=NUDA_APPROVAL_SUBJECT
        )


def test_nuda_approval_refuses_a_record_approved_for_a_different_action(tmp_path):
    """A record naming this exact subject and config digest still is not a sampling
    approval if it was filed under a different governed action (GOVERNANCE 1's
    exclusion/salvage-promotion are different sign-offs than a sampling design)."""
    context = _approval_context(tmp_path)
    record = build_approval_record(
        subject_ids=[NUDA_APPROVAL_SUBJECT],
        action="exclusion",
        reason="test-only: not actually a sampling design approval",
        target_version_hash=context.config_digest,
        timestamp="2026-08-21T00:00:00Z",
    )
    context.tree.write_approval_record(record)

    with pytest.raises(ContractError, match="not 'other'"):
        resolve_sampling_approval(
            context, approval_ref=NUDA_APPROVAL_SUBJECT, subject=NUDA_APPROVAL_SUBJECT
        )


@pytest.mark.parametrize(
    "corruption, refusal",
    [("self-hash", "self-hash"), ("approver", "approver"), ("schema", "schema")],
)
def test_nuda_approval_refuses_a_corrupt_typed_record(tmp_path, corruption, refusal):
    context = _approval_context(tmp_path)
    record = _approval_record_for_digest(context.config_digest)
    if corruption == "self-hash":
        record["reason"] = "edited after approval"
    elif corruption == "approver":
        record["approver"] = "not-Tyrel"
        record["self_hash"] = self_hash(record)
    else:
        record["schema"] = "approval-record.v9"
        record["self_hash"] = self_hash(record)
    data = canonical_bytes(record)
    digest = digest_bytes(data)
    path = context.tree.resolve(context.tree.receipt_path(digest))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)

    with pytest.raises(ContractError, match=refusal):
        resolve_sampling_approval(
            context, approval_ref=NUDA_APPROVAL_SUBJECT, subject=NUDA_APPROVAL_SUBJECT
        )


def test_nuda_per_mille_zero_produces_no_nuda_records_at_all(tmp_path):
    root = tmp_path / "runs"
    result = orchestrate(root, "r", "happy", nuda_per_mille=0)
    assert result.returncode == 0, result.stderr
    tree = RunTree(root, "r")
    entries = [
        entry
        for entry in tree.build_manifest(PERLECTOR)["artifacts"]
        if entry["kind"] == LECTIO_NUDA_KIND
    ]
    assert entries == []


def test_is_nuda_sampled_rejects_an_out_of_range_per_mille():
    with pytest.raises(ValueError, match=r"\[0, 1000\]"):
        is_nuda_sampled("act_1", run_id="r", nuda_per_mille=1001)
    with pytest.raises(ValueError, match=r"\[0, 1000\]"):
        is_nuda_sampled("act_1", run_id="r", nuda_per_mille=-1)


def test_a_partial_rate_samples_some_acts_and_not_others():
    """The two endpoint tests pin 0 and 1000 per mille; this pins the middle.

    A sampler regressed to a constant — never sampling, or always sampling —
    passes determinism and one endpoint. Five hundred per mille over two
    hundred act ids must select some and skip others, deterministically."""
    drawn = [
        is_nuda_sampled(f"act-{index:03d}", run_id="partial-rate-run", nuda_per_mille=500)
        for index in range(200)
    ]
    assert any(drawn), "a 500-per-mille rate that samples nothing is a silent instrument"
    assert not all(drawn), "a 500-per-mille rate that samples everything is not a sample"


def test_is_nuda_sampled_is_deterministic_for_the_same_act_and_run():
    first = is_nuda_sampled("act_1", run_id="r", nuda_per_mille=500)
    second = is_nuda_sampled("act_1", run_id="r", nuda_per_mille=500)
    assert first == second
