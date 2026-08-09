import json
from copy import deepcopy
from datetime import date
from pathlib import Path

import pytest

from operations.spike_perlector.errors import PublicSafetyRefusal
from operations.spike_perlector.fakes import FakeCandidate, FakeReply
from operations.spike_perlector.gates import RunAuthorization
from operations.spike_perlector.models import (
    ALL_CONDITIONS,
    DeliveryMode,
    MaterialClass,
    OutputStatus,
)
from operations.spike_perlector.normalization import GRAPHEMIC_V1
from operations.spike_perlector.publication import write_public_finding
from operations.spike_perlector.redaction import project_public_finding, validate_public_finding
from operations.spike_perlector.roster import STOCK_BASE_SOURCE, CandidateRoster
from operations.spike_perlector.runner import run_declared_roster_matrix, run_matrix
from operations.spike_perlector.testkit import (
    cleared_public_authorization_for,
    digest,
    evaluation_act,
    identity,
    manifest_for,
    registry,
    witness_configuration_for,
)


def declared_fixture_run():
    roster = CandidateRoster(
        stock_base=identity("private-model-name-one", 1, source_ref=STOCK_BASE_SOURCE),
        vendor_unaltered=identity(
            "private-model-name-two",
            2,
            source_ref="private/vendor",
            delivery=DeliveryMode.EXTERNAL,
            provider="private-vendor",
        ),
        trained_checkpoint=identity("private-model-name-three", 3),
        vendor_unaltered_evidence_sha256=digest("vendor-evidence"),
        checkpoint_repository_evidence_sha256=digest("checkpoint-evidence"),
    )
    act = evaluation_act(
        text="synthetic secret transcription", material_class=MaterialClass.CLEARED_PUBLIC
    )
    replies = {
        (act.opaque_act_id, condition): FakeReply(
            OutputStatus.COMPLETE, "synthetic secret transcription"
        )
        for condition in ALL_CONDITIONS
    }
    witnesses = witness_configuration_for(act)
    manifest = manifest_for(act)
    prompts = registry(*roster.identities())
    return run_declared_roster_matrix(
        tuple(FakeCandidate(item, replies) for item in roster.identities()),
        (act,),
        roster=roster,
        witness_configuration=witnesses,
        manifest=manifest,
        prompt_registry=prompts,
        profile=GRAPHEMIC_V1,
        authorization=cleared_public_authorization_for(
            manifest=manifest,
            roster=roster,
            witness_configuration=witnesses,
            prompt_registry=prompts,
            profile=GRAPHEMIC_V1,
        ),
    )


def test_public_projector_omits_private_names_text_images_and_act_ids():
    finding = project_public_finding(declared_fixture_run())
    serialized = json.dumps(finding, sort_keys=True)
    for forbidden in (
        "synthetic secret transcription",
        "private-model-name-one",
        "private-model-name-two",
        "private-model-name-three",
        "synthetic-page",
        "synthetic-act",
        "synthetic-format",
    ):
        assert forbidden not in serialized
    assert len(finding["matrix"]) == 9
    assert finding["input_baselines"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("transcription", "synthetic secret transcription"),
        ("model_name", "private-model-name-one"),
        ("image_path", "/private/image.png"),
        ("resolved_identity", {"source_ref": "private/model"}),
    ],
)
def test_public_validator_refuses_planted_sensitive_fields(field, value):
    finding = project_public_finding(declared_fixture_run())
    tampered = deepcopy(finding)
    tampered[field] = value
    with pytest.raises(PublicSafetyRefusal, match="keys"):
        validate_public_finding(tampered)


def test_public_validator_refuses_free_text_in_a_closed_metric_row():
    finding = project_public_finding(declared_fixture_run())
    tampered = deepcopy(finding)
    tampered["matrix"][0]["note"] = "synthetic secret transcription"
    with pytest.raises(PublicSafetyRefusal, match="keys"):
        validate_public_finding(tampered)


def test_public_validator_refuses_partial_or_arithmetically_false_matrix():
    finding = project_public_finding(declared_fixture_run())
    partial = deepcopy(finding)
    partial["matrix"].pop()
    with pytest.raises(PublicSafetyRefusal, match="three-subject"):
        validate_public_finding(partial)
    inconsistent = deepcopy(finding)
    inconsistent["matrix"][0]["cer"] = 0.5
    with pytest.raises(PublicSafetyRefusal, match="CER"):
        validate_public_finding(inconsistent)
    unmeasured = deepcopy(finding)
    unmeasured["matrix"][0]["elapsed_observed_cells"] = 0
    unmeasured["matrix"][0]["mean_elapsed_ms"] = None
    with pytest.raises(PublicSafetyRefusal, match="every act"):
        validate_public_finding(unmeasured)


def test_synthetic_exercise_cannot_be_projected_as_a_public_finding():
    resolved = identity("synthetic-private", 1)
    run = run_matrix(
        (FakeCandidate(resolved),),
        (evaluation_act(),),
        prompt_registry=registry(resolved),
        profile=GRAPHEMIC_V1,
        authorization=RunAuthorization.synthetic_fixture(),
    )
    with pytest.raises(PublicSafetyRefusal, match="not eligible"):
        project_public_finding(run)


def test_supported_history_writer_validates_and_never_overwrites(tmp_path):
    target = write_public_finding(
        declared_fixture_run(), history_directory=tmp_path, finding_date=date(2026, 8, 8)
    )
    serialized = target.read_text(encoding="utf-8")
    assert "synthetic secret transcription" not in serialized
    with pytest.raises(PublicSafetyRefusal, match="write-once"):
        write_public_finding(
            declared_fixture_run(), history_directory=tmp_path, finding_date=date(2026, 8, 8)
        )


def test_public_schema_is_present_parseable_and_declares_no_free_text_field():
    # Resolved from this file, not from the working directory: a relative
    # `Path("history/...")` passes or fails on where pytest was invoked from,
    # which is not a property of the schema.
    path = Path(__file__).with_name("reading_claim_public_finding.schema.json")
    schema = json.loads(path.read_text(encoding="utf-8"))
    assert schema["additionalProperties"] is False
    assert schema["properties"]["matrix"]["minItems"] == 9
    assert "transcription" not in schema["properties"]
    assert "image_path" not in schema["properties"]
    assert "model_name" not in schema["properties"]
