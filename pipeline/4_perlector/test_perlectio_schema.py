"""The Perlectio's own closed record schema, refused at the moment it is written.

Spec 08's schema test: a Perlectio "missing identity, missing dissent, missing
regime record, or with annotation spans outside text bounds" is refused. Three
of those four are *absent* fields rather than wrong ones, which is the failure
mode a per-field type check never sees -- so the check is a closed field set,
and these tests take a payload that really is published by the running stage
and remove one thing from it at a time.
"""

import copy
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from common.contracts.errors import SchemaRefusal
from common.contracts.stages import PERLECTOR
from common.runtree.store import RunTree

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"


def _load_perlector():
    path = Path(__file__).resolve().parent / "run.py"
    spec = importlib.util.spec_from_file_location("perlector_schema_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


perlector = _load_perlector()


@pytest.fixture(scope="module")
def published_payload(tmp_path_factory):
    """A real published Perlectio payload, not a hand-built stand-in.

    A hand-built one would prove only that the checker agrees with whatever
    this test file thinks a Perlectio looks like; taking the real record makes
    the closed field set and the producer provably the same shape.
    """
    root = tmp_path_factory.mktemp("perlectio-schema") / "runs"
    result = subprocess.run(
        [
            sys.executable,
            str(ORCHESTRATOR),
            "--fixture",
            "synthetic-two-page-v0",
            "--scenario",
            "happy",
            "--run-id",
            "r",
            "--run-root",
            str(root),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    tree = RunTree(root, "r")
    entry = next(
        entry
        for entry in tree.build_manifest(PERLECTOR)["artifacts"]
        if entry["kind"] == "perlectio"
    )
    return tree.read_artifact(PERLECTOR, "perlectio", entry["artifact_id"])["payload"]


def _validate(payload, outcome="read"):
    protocol_config, protocol_sha256 = perlector.protocol.load(
        ROOT / "config" / "perlector_protocol.toml"
    )
    perlector.validate_reading_payload(
        payload,
        outcome=outcome,
        fields=perlector._PERLECTIO_FIELDS,
        protocol_config=protocol_config,
        protocol_sha256=protocol_sha256,
    )


def _reblinded(payload, *, run_id, config_digest):
    """The published named payload, rewritten as its blinded twin.

    Labels are re-derived through the same pseudonym rule the producer uses, and
    the dossier digest is re-sealed, so the only difference from a genuine
    blinded record is that this one was made by a test."""
    blinded = copy.deepcopy(payload)
    blinded["provenance"]["witness_regime"] = "blinded"
    reading_dossier = blinded["dossier"]
    reading_dossier["witness_regime"] = "blinded"
    for row, basis_row in zip(
        reading_dossier["testimonia"], blinded["basis"]["testimonia"], strict=True
    ):
        row["witness_label"] = perlector.regime.pseudonym_for(
            basis_row["chair"], run_id=run_id, config_digest=config_digest
        )
        row["model_name"] = None
        row["resolved_provenance"] = None
        row["training_domain"] = None
    reading_dossier["testimonia"].sort(key=lambda row: row["witness_label"])
    body = {key: value for key, value in reading_dossier.items() if key != "dossier_digest"}
    reading_dossier["dossier_digest"] = perlector.digest_of(body)
    identity = perlector.ChairIdentity(**blinded["provenance"]["resolved_identity"])
    protocol_config, protocol_sha256 = perlector.protocol.load(
        ROOT / "config" / "perlector_protocol.toml"
    )
    blinded["prompt"] = perlector.prompts.prompt_evidence(
        identity, reading_dossier, protocol_config, protocol_sha256
    )
    return blinded, protocol_config, protocol_sha256


def test_a_blinded_dossier_with_a_wrong_witness_label_is_refused(published_payload):
    """The blinded branch of the label guard: pseudonyms are re-derived from the
    run's own identity, so a swapped or foreign label — invisible to a reader by
    construction — still refuses against the Testimonium basis."""
    run_id, config_digest = "schema-blind-run", "c" * 64
    blinded, protocol_config, protocol_sha256 = _reblinded(
        published_payload, run_id=run_id, config_digest=config_digest
    )
    perlector.validate_reading_payload(
        blinded,
        outcome="read",
        fields=perlector._PERLECTIO_FIELDS,
        run_id=run_id,
        config_digest=config_digest,
        protocol_config=protocol_config,
        protocol_sha256=protocol_sha256,
    )

    tampered = copy.deepcopy(blinded)
    tampered["dossier"]["testimonia"][0]["witness_label"] = "pseudo-witness-99"
    body = {key: value for key, value in tampered["dossier"].items() if key != "dossier_digest"}
    tampered["dossier"]["dossier_digest"] = perlector.digest_of(body)
    with pytest.raises(SchemaRefusal, match="witness labels do not match"):
        perlector.validate_reading_payload(
            tampered,
            outcome="read",
            fields=perlector._PERLECTIO_FIELDS,
            run_id=run_id,
            config_digest=config_digest,
            protocol_config=protocol_config,
            protocol_sha256=protocol_sha256,
        )


def test_a_real_published_perlectio_satisfies_the_closed_schema(published_payload):
    """The check is not vacuous in the other direction either: what the stage
    actually writes must pass, or the schema is describing a record nobody
    produces."""
    _validate(copy.deepcopy(published_payload))


@pytest.mark.parametrize("field", sorted(perlector._PERLECTIO_FIELDS))
def test_a_perlectio_missing_any_field_of_its_record_is_refused(published_payload, field):
    payload = copy.deepcopy(published_payload)
    del payload[field]
    with pytest.raises(SchemaRefusal, match="not its closed schema"):
        _validate(payload)


def test_an_empty_dissent_record_cannot_hide_the_whole_witness_denominator(published_payload):
    """Agreement is a row with no departure spans, never an omitted row."""
    payload = copy.deepcopy(published_payload)
    payload["dissent"] = []
    with pytest.raises(SchemaRefusal, match="exactly every witness"):
        _validate(payload)

    payload["dissent"] = None
    with pytest.raises(SchemaRefusal, match="no dissent record"):
        _validate(payload)


def test_a_dissent_record_cannot_duplicate_one_witness_over_another(published_payload):
    payload = copy.deepcopy(published_payload)
    payload["dissent"][1]["chair"] = payload["dissent"][0]["chair"]
    with pytest.raises(SchemaRefusal, match="repeats a witness"):
        _validate(payload)


def test_a_dissent_reading_span_must_index_the_perlectio_text(published_payload):
    payload = copy.deepcopy(published_payload)
    row = next(row for row in payload["dissent"] if row["compared"] is True)
    row["departures"] = [
        {
            "reading_span": {"start": 0, "end": len(payload["text"]) + 1},
            "testimonium_span": {"start": 0, "end": 1},
        }
    ]
    row["departed"] = True
    row["departed_raw"] = True
    with pytest.raises(SchemaRefusal, match="invalid bounds"):
        _validate(payload)


def test_dissent_cannot_call_a_failed_witness_compared(published_payload):
    payload = copy.deepcopy(published_payload)
    payload["basis"]["testimonia"][0]["outcome"] = "failed"
    with pytest.raises(SchemaRefusal, match="did not report"):
        _validate(payload)


def test_dissent_booleans_must_reconcile_with_their_spans(published_payload):
    payload = copy.deepcopy(published_payload)
    row = next(row for row in payload["dissent"] if row["compared"] is True)
    row["departed_raw"] = not bool(row["departures"])
    with pytest.raises(SchemaRefusal, match="contradicts its own"):
        _validate(payload)


def test_a_perlectio_with_no_regime_record_is_refused(published_payload):
    payload = copy.deepcopy(published_payload)
    payload["provenance"] = {
        key: value for key, value in payload["provenance"].items() if key != "witness_regime"
    }
    with pytest.raises(SchemaRefusal, match="no witness regime"):
        _validate(payload)


def test_a_perlectio_claiming_an_impossible_regime_is_refused(published_payload):
    payload = copy.deepcopy(published_payload)
    payload["provenance"]["witness_regime"] = "half-blinded"
    with pytest.raises(SchemaRefusal, match="no witness regime"):
        _validate(payload)


def test_a_perlectio_regime_must_match_the_dossier_it_was_shown(published_payload):
    payload = copy.deepcopy(published_payload)
    payload["dossier"]["witness_regime"] = "blinded"
    with pytest.raises(SchemaRefusal, match="dossier's witness regime"):
        _validate(payload)


def test_a_perlectio_refuses_a_stale_dossier_digest(published_payload):
    payload = copy.deepcopy(published_payload)
    payload["dossier"]["act_key"] = "another-act"
    payload["act_key"] = "another-act"
    with pytest.raises(SchemaRefusal, match="dossier digest"):
        _validate(payload)


def test_a_perlectio_prompt_must_reproduce_from_its_dossier(published_payload):
    payload = copy.deepcopy(published_payload)
    payload["prompt"]["rendered_sha256"] = "0" * 64
    with pytest.raises(SchemaRefusal, match="does not reproduce"):
        _validate(payload)


def test_a_perlectio_dossier_cannot_drop_one_basis_witness(published_payload):
    payload = copy.deepcopy(published_payload)
    payload["dossier"]["testimonia"].pop()
    dossier_body = {
        key: value for key, value in payload["dossier"].items() if key != "dossier_digest"
    }
    payload["dossier"]["dossier_digest"] = perlector.digest_of(dossier_body)
    with pytest.raises(SchemaRefusal, match="exactly its Testimonium basis"):
        _validate(payload)


def test_a_configured_chair_with_no_resolved_identity_is_refused(published_payload):
    payload = copy.deepcopy(published_payload)
    assert payload["provenance"]["chair_state"] == "configured"
    payload["provenance"]["resolved_identity"] = None
    with pytest.raises(SchemaRefusal, match="no resolved identity"):
        _validate(payload)


def test_a_perlectio_with_no_truncation_classification_is_refused(published_payload):
    payload = copy.deepcopy(published_payload)
    payload["truncation"] = {"signals": {}}
    with pytest.raises(SchemaRefusal, match="no truncation classification"):
        _validate(payload)


def test_a_truncated_classification_cannot_publish_as_a_completed_read(published_payload):
    payload = copy.deepcopy(published_payload)
    payload["truncation"]["classification"] = "truncated"
    with pytest.raises(SchemaRefusal, match="cannot carry the completed outcome"):
        _validate(payload)


def test_a_complete_classification_cannot_publish_as_a_truncated_outcome(published_payload):
    """The reverse direction of the check above. `outcome == 'truncated'` means
    'not established complete' (HANDOFF.md, verbatim); a published record
    claiming both at once is the exact self-contradiction the truncation field
    exists to rule out, whichever direction it is written in."""
    payload = copy.deepcopy(published_payload)
    assert payload["truncation"]["classification"] == "complete"
    with pytest.raises(SchemaRefusal, match="cannot carry a 'complete' truncation"):
        _validate(payload, outcome="truncated")


def test_an_empty_text_cannot_publish_as_a_completed_read(published_payload):
    payload = copy.deepcopy(published_payload)
    payload["text"] = ""
    with pytest.raises(SchemaRefusal, match="cannot establish an empty text"):
        _validate(payload)


def test_an_annotation_span_outside_the_text_is_refused(published_payload):
    payload = copy.deepcopy(published_payload)
    payload["uncertain_spans"] = [
        {
            "start": 0,
            "end": len(payload["text"]) + 1,
            "alternatives": ["something"],
            "confidence": "low",
        }
    ]
    with pytest.raises(SchemaRefusal, match="outside text bounds"):
        _validate(payload)


def test_a_perlectio_smuggling_a_none_basis_cannot_take_the_nuda_branch(published_payload):
    """The discriminator is the caller's field set, never the payload's own
    claim: a Perlectio with `basis: None` must refuse as a missing witness
    basis rather than validate as a Lectio nuda with every witness gone."""
    payload = copy.deepcopy(published_payload)
    payload["basis"] = None
    with pytest.raises(SchemaRefusal, match="no Testimonium basis"):
        _validate(payload)


def test_a_zero_width_annotation_span_is_refused(published_payload):
    payload = copy.deepcopy(published_payload)
    payload["uncertain_spans"] = [
        {"start": 1, "end": 1, "alternatives": ["something"], "confidence": "low"}
    ]
    with pytest.raises(SchemaRefusal, match="must cover at least one character"):
        _validate(payload)


def test_an_unexpected_field_is_refused_as_loudly_as_a_missing_one(published_payload):
    """A field nothing validates is a field nothing can trust -- the same
    reasoning `common/stage.py`'s provenance allowlist already gives."""
    payload = copy.deepcopy(published_payload)
    payload["preferred_witness"] = "attestator_1"
    with pytest.raises(SchemaRefusal, match="unexpected"):
        _validate(payload)


# --- D-6: the two not-run shapes get the same closed-schema guard ------------


@pytest.fixture(scope="module")
def held_not_run_payload(tmp_path_factory):
    """A real published held-act Perlectio, not a hand-built stand-in.

    `refused-page` loses page 2 at the door, so a2's proposal never completes
    and the Perlector acknowledges it without reading -- the "held" not-run
    shape (`_NOT_RUN_HELD_FIELDS`).
    """
    root = tmp_path_factory.mktemp("perlectio-not-run-held") / "runs"
    result = subprocess.run(
        [
            sys.executable,
            str(ORCHESTRATOR),
            "--fixture",
            "synthetic-two-page-v0",
            "--scenario",
            "refused-page",
            "--run-id",
            "r",
            "--run-root",
            str(root),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 3, result.stderr
    tree = RunTree(root, "r")
    entry = next(
        entry
        for entry in tree.build_manifest(PERLECTOR)["artifacts"]
        if entry["kind"] == "perlectio"
    )
    record = tree.read_artifact(PERLECTOR, "perlectio", entry["artifact_id"])
    assert record["outcome"] == "not-run"
    return record["payload"]


def test_a_real_held_not_run_perlectio_satisfies_the_closed_not_run_schema(
    held_not_run_payload,
):
    perlector.validate_not_run_payload(
        copy.deepcopy(held_not_run_payload), fields=perlector._NOT_RUN_HELD_FIELDS
    )


@pytest.mark.parametrize("field", sorted(perlector._NOT_RUN_HELD_FIELDS))
def test_a_held_not_run_perlectio_missing_any_field_is_refused(held_not_run_payload, field):
    payload = copy.deepcopy(held_not_run_payload)
    del payload[field]
    with pytest.raises(SchemaRefusal, match="not its closed schema"):
        perlector.validate_not_run_payload(payload, fields=perlector._NOT_RUN_HELD_FIELDS)


def test_a_held_not_run_perlectio_with_an_unexpected_field_is_refused(held_not_run_payload):
    payload = copy.deepcopy(held_not_run_payload)
    payload["basis"] = {"regions": [], "testimonia": []}
    with pytest.raises(SchemaRefusal, match="unexpected"):
        perlector.validate_not_run_payload(payload, fields=perlector._NOT_RUN_HELD_FIELDS)


def _absent_chair_not_run_payload():
    """The literal shape `run.py` publishes for an explicitly absent chair --
    mirrored here rather than driven, because standing up an absent-Perlector-
    chair fixture run is out of proportion to a low-severity schema-coverage
    gap; the shape itself is unchanged by this fix and is exercised in
    production by `run.py`'s own absent-chair branch."""
    return {
        "act_key": "a1",
        "attempt_ordinal": 1,
        "reason": "the Perlector chair is explicitly absent: fixture removes this witness",
        "basis": {"regions": [], "testimonia": []},
        "dissent": [],
        "provenance": {"chair_state": "absent"},
    }


def test_an_absent_chair_not_run_perlectio_satisfies_the_closed_not_run_schema():
    perlector.validate_not_run_payload(
        _absent_chair_not_run_payload(), fields=perlector._NOT_RUN_ABSENT_FIELDS
    )


def test_the_mirrored_absent_chair_shape_matches_the_closed_field_set():
    """The hand-written payload below and run.py's constant must be the same
    set, or the parametrized refusals exercise a shape production never
    writes."""
    assert set(_absent_chair_not_run_payload()) == set(perlector._NOT_RUN_ABSENT_FIELDS)


@pytest.mark.parametrize("field", sorted(perlector._NOT_RUN_ABSENT_FIELDS))
def test_an_absent_chair_not_run_perlectio_missing_any_field_is_refused(field):
    payload = _absent_chair_not_run_payload()
    del payload[field]
    with pytest.raises(SchemaRefusal, match="not its closed schema"):
        perlector.validate_not_run_payload(payload, fields=perlector._NOT_RUN_ABSENT_FIELDS)


def test_an_absent_chair_not_run_perlectio_with_an_unexpected_field_is_refused():
    payload = _absent_chair_not_run_payload()
    payload["text"] = ""
    with pytest.raises(SchemaRefusal, match="unexpected"):
        perlector.validate_not_run_payload(payload, fields=perlector._NOT_RUN_ABSENT_FIELDS)


def test_an_unknown_lectio_kind_is_refused_at_publication_not_one_stage_later(
    published_payload,
):
    """A misspelt or future kind matched neither prior-draft branch, so its
    prior-draft evidence published uninspected and the defect surfaced at the
    Archetypus — one stage after the validator that promises write-time checks."""
    run_id, config_digest = "schema-blind-run", "c" * 64
    blinded, protocol_config, protocol_sha256 = _reblinded(
        published_payload, run_id=run_id, config_digest=config_digest
    )
    tampered = copy.deepcopy(blinded)
    tampered["lectio_kind"] = "primed_with_prior"
    with pytest.raises(SchemaRefusal, match="unknown lectio kind"):
        perlector.validate_reading_payload(
            tampered,
            outcome="read",
            fields=perlector._PERLECTIO_FIELDS,
            run_id=run_id,
            config_digest=config_digest,
            protocol_config=protocol_config,
            protocol_sha256=protocol_sha256,
        )
