import json
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path

import pytest

from operations.spike_perlector.errors import PublicSafetyRefusal
from operations.spike_perlector.fakes import FakeCandidate, FakeReply
from operations.spike_perlector.gates import RunAuthorization
from operations.spike_perlector.models import (
    ALL_CONDITIONS,
    Condition,
    DeliveryMode,
    LimitationDisclosureState,
    MaterialClass,
    OutputStatus,
    PublicLimitationCode,
    RunLimitations,
)
from operations.spike_perlector.normalization import GRAPHEMIC_V1, PROFILES
from operations.spike_perlector.publication import write_public_finding
from operations.spike_perlector.redaction import (
    _BASELINE_KEYS,
    _DELTA_KEYS,
    _MATRIX_KEYS,
    _ROOT_KEYS,
    MEASURE_QUOTES,
    SCHEMA,
    project_public_finding,
    validate_public_finding,
)
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


def declared_fixture_run(
    *,
    elapsed_ms: float | None = 1.0,
    cost_usd: float | None = 0.0,
    limitations: RunLimitations | None = None,
):
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
            OutputStatus.COMPLETE,
            "synthetic secret transcription",
            elapsed_ms=elapsed_ms,
            cost_usd=cost_usd,
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
        limitations=limitations,
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


def test_a_coded_run_specific_limitation_appears_in_the_public_finding():
    limitation = PublicLimitationCode.CHECKED_REFERENCE_GAPS_PRESENT
    finding = project_public_finding(
        declared_fixture_run(
            limitations=RunLimitations(
                disclosure_state=LimitationDisclosureState.CODED,
                codes=(limitation,),
            )
        )
    )

    assert finding["limitations"] == [limitation.value]
    assert "transcription" not in json.dumps(finding)


def test_an_unenumerable_run_specific_limitation_refuses_publication():
    run = declared_fixture_run(
        limitations=RunLimitations(
            disclosure_state=LimitationDisclosureState.UNPUBLISHABLE,
        )
    )

    with pytest.raises(PublicSafetyRefusal, match="outside the closed public vocabulary"):
        project_public_finding(run)


def test_public_validator_refuses_a_protocol_digest_that_is_not_predeclared():
    """A well-formed but wrong digest must fail, not just a malformed one.

    Checking only ``is_sha256`` would let a finding claim any 64-hex-character
    string as its protocol digest; a MeasurementRun not built through the
    sealed entry point could carry exactly such a mismatch.
    """

    finding = project_public_finding(declared_fixture_run())
    tampered = deepcopy(finding)
    tampered["protocol_sha256"] = "0" * 64
    with pytest.raises(PublicSafetyRefusal, match="predeclared Spec 05 protocol"):
        validate_public_finding(tampered)


def test_public_validator_refuses_a_profile_digest_that_does_not_match_its_declared_name():
    """A finding naming "graphemic-v1" must carry that profile's real digest.

    Checking only that the name is one of the two known names, and separately
    that the digest looks like a SHA-256, would accept a finding claiming
    "graphemic-v1" while carrying a fingerprint for something else entirely.
    """

    finding = project_public_finding(declared_fixture_run())
    tampered = deepcopy(finding)
    tampered["normalization_profile_sha256"] = "0" * 64
    with pytest.raises(PublicSafetyRefusal, match="does not match its declared profile"):
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
    with pytest.raises(PublicSafetyRefusal, match="every measurable act"):
        validate_public_finding(unmeasured)


def test_public_validator_refuses_a_condition_delta_naming_an_unmatched_subject():
    """subject_index 4 is nonnegative and >= 1, but names no row in the sealed

    three-subject matrix; the lookup that recomputes the expected deltas must
    refuse cleanly rather than raise a raw KeyError.
    """

    finding = project_public_finding(declared_fixture_run())
    tampered = deepcopy(finding)
    tampered["condition_deltas"][0]["subject_index"] = 4
    with pytest.raises(PublicSafetyRefusal, match="outside the sealed matrix"):
        validate_public_finding(tampered)


@pytest.mark.parametrize(("elapsed_ms", "cost_usd"), [(None, 0.0), (1.0, None)])
def test_a_run_missing_wall_time_or_cost_cannot_become_a_finding(elapsed_ms, cost_usd):
    """Section 8 predeclares an observation of both for every candidate cell.

    An interface exercise may leave either unknown; what it may not do is publish.
    Without this, an unmeasured cell would reach the validator as a null mean and
    be reported as a measured run with a missing number in it.
    """

    run = declared_fixture_run(elapsed_ms=elapsed_ms, cost_usd=cost_usd)
    with pytest.raises(PublicSafetyRefusal, match="wall time and cost"):
        project_public_finding(run)


def test_the_published_schema_and_the_stricter_validator_describe_one_shape():
    """The schema is what an outside reader checks a `history/` finding against.

    Nothing executes it -- the validator in `redaction.py` is what runs, and it is
    deliberately stricter -- so the two can only be kept honest by pinning them to
    the same closed key sets and enumerations here. Drift shows up as a schema that
    accepts a finding the code would never write, or refuses one it does.
    """

    schema = json.loads(
        Path(__file__).with_name("reading_claim_public_finding.schema.json").read_text("utf-8")
    )
    metric_keys = _MATRIX_KEYS - {"subject_index", "condition"}
    definitions = schema["$defs"]

    assert schema["properties"]["schema"]["const"] == SCHEMA
    assert set(schema["required"]) == set(schema["properties"]) == _ROOT_KEYS
    assert (
        set(definitions["metrics"]["required"])
        == set(definitions["metrics"]["properties"])
        == metric_keys
    )
    assert _row_keys(definitions["matrix_row"], metric_keys) == _MATRIX_KEYS
    assert _row_keys(definitions["baseline_row"], metric_keys) == _BASELINE_KEYS
    assert set(definitions["delta_row"]["required"]) == _DELTA_KEYS

    conditions = {condition.value for condition in Condition}
    matrix_row = definitions["matrix_row"]["allOf"][1]["properties"]
    assert set(matrix_row["condition"]["enum"]) == conditions
    assert set(schema["properties"]["normalization_profile_id"]["enum"]) == set(PROFILES)
    assert set(schema["properties"]["measure_quotes"]["items"]["enum"]) == set(MEASURE_QUOTES)
    assert set(schema["properties"]["limitations"]["items"]["enum"]) == {
        code.value for code in PublicLimitationCode
    }
    # The validator requires the exact fixed list (redaction.py's `!= list(MEASURE_QUOTES)`);
    # the schema can only express that as cardinality plus uniqueness, not order, but it
    # must at least refuse an empty or duplicated measure_quotes array.
    assert schema["properties"]["measure_quotes"]["minItems"] == len(MEASURE_QUOTES)
    assert schema["properties"]["measure_quotes"]["maxItems"] == len(MEASURE_QUOTES)
    assert schema["properties"]["measure_quotes"]["uniqueItems"] is True
    # The sealed three slots by three conditions, as a count the schema can express.
    assert schema["properties"]["matrix"]["minItems"] == len(conditions) * 3
    assert schema["properties"]["matrix"]["maxItems"] == len(conditions) * 3


def _row_keys(row_schema: dict, metric_keys: set[str]) -> set[str]:
    """Every property a metric row may carry: the shared metrics plus its own index."""

    assert row_schema["unevaluatedProperties"] is False
    assert row_schema["allOf"][0]["$ref"] == "#/$defs/metrics"
    return metric_keys | set(row_schema["allOf"][1]["properties"])


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


def test_a_history_path_that_is_a_file_is_named_as_such_not_as_a_prior_finding(tmp_path):
    """`tmp_path` is always a directory, so nothing covered the repaired path.

    With `mkdir` inside the write-once guard, pointing the writer at a file
    reported "public finding already exists". An operator reads that as work
    already done and stops looking, and no finding was ever written.
    """

    not_a_directory = tmp_path / "history"
    not_a_directory.write_bytes(b"this is a file")
    with pytest.raises(PublicSafetyRefusal, match="not a directory"):
        write_public_finding(
            declared_fixture_run(),
            history_directory=not_a_directory,
            finding_date=date(2026, 8, 8),
        )


def test_history_writer_refuses_a_datetime_because_it_is_not_date_only(tmp_path):
    """``datetime`` subclasses ``date``; accepting one would defeat write-once-per-day."""

    with pytest.raises(PublicSafetyRefusal, match="datetime"):
        write_public_finding(
            declared_fixture_run(),
            history_directory=tmp_path,
            finding_date=datetime(2026, 8, 8, 13, 45, 9),
        )


def test_history_writer_ignores_a_hostile_isoformat_override(tmp_path):
    """The filename is built from year/month/day, not isoformat().

    A date subclass overriding isoformat() to return a path-traversal string
    (or anything else) must not be able to steer the write outside
    history_directory or otherwise change the target name.
    """

    class _HostileDate(date):
        def isoformat(self):
            return "../../escaped"

    target = write_public_finding(
        declared_fixture_run(),
        history_directory=tmp_path,
        finding_date=_HostileDate(2026, 8, 8),
    )
    assert target.parent == tmp_path
    assert target.name == "2026-08-08_reading_claim_metrics.json"


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
