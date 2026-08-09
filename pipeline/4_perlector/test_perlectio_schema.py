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


def test_an_empty_dissent_record_is_accepted_but_a_missing_one_is_not(published_payload):
    """The distinction the closed set exists for. Every witness agreeing is a
    real answer and an ordinary one; nobody having computed dissent at all is a
    blinded instrument, and the two must never look the same in the record."""
    payload = copy.deepcopy(published_payload)
    payload["dissent"] = []
    _validate(payload)

    payload["dissent"] = None
    with pytest.raises(SchemaRefusal, match="no dissent record"):
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
