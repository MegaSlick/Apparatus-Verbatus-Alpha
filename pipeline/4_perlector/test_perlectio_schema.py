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
    perlector.validate_reading_payload(payload, outcome=outcome, fields=perlector._PERLECTIO_FIELDS)


def test_a_real_published_perlectio_satisfies_the_closed_schema(published_payload):
    """The check is not vacuous in the other direction either: what the stage
    actually writes must pass, or the schema is describing a record nobody
    produces."""
    _validate(copy.deepcopy(published_payload))


@pytest.mark.parametrize(
    "field", ["text", "basis", "dossier", "prompt", "dissent", "truncation", "provenance"]
)
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


def test_an_unexpected_field_is_refused_as_loudly_as_a_missing_one(published_payload):
    """A field nothing validates is a field nothing can trust -- the same
    reasoning `common/stage.py`'s provenance allowlist already gives."""
    payload = copy.deepcopy(published_payload)
    payload["preferred_witness"] = "attestator_1"
    with pytest.raises(SchemaRefusal, match="unexpected"):
        _validate(payload)
