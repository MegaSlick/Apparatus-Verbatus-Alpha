import pytest

import operations.spike_perlector.models as models
from operations.spike_perlector.errors import MatrixRefusal, MeasurementRefusal
from operations.spike_perlector.models import (
    CandidateResponse,
    Condition,
    DeliveryMode,
    DissentSummary,
    DossierTestimonium,
    OutputStatus,
    Perlectio,
    ResolvedIdentity,
)
from operations.spike_perlector.testkit import digest, evaluation_act, identity

# Referenced as models.Testimonium at each call site rather than imported by its
# bare name: pytest's default collection treats any module-level name starting
# with "Test" as a candidate test class, and this dataclass's name collides.

NON_READING_STATUSES = (
    OutputStatus.NO_READABLE_TEXT,
    OutputStatus.REFUSED,
    OutputStatus.MISSING,
    OutputStatus.UNAVAILABLE,
    OutputStatus.MALFORMED,
)


@pytest.mark.parametrize("status", NON_READING_STATUSES)
def test_testimonium_refuses_stray_text_on_a_non_reading_status(status):
    with pytest.raises(MeasurementRefusal, match="carries status, not text"):
        models.Testimonium(
            private_source_id="w1", public_source_index=1, text="stray", status=status
        )


@pytest.mark.parametrize("status", NON_READING_STATUSES)
def test_dossier_testimonium_refuses_stray_text_on_a_non_reading_status(status):
    """A refused witness slot must not deliver stray text into a candidate's dossier."""

    with pytest.raises(MeasurementRefusal, match="carries status, not text"):
        DossierTestimonium(public_source_index=1, text="stray", status=status)


@pytest.mark.parametrize("status", NON_READING_STATUSES)
def test_candidate_response_refuses_stray_text_on_a_non_reading_status(status):
    with pytest.raises(MeasurementRefusal, match="carries status, not text"):
        CandidateResponse(
            status=status,
            text="stray",
            elapsed_ms=None,
            cost_usd=None,
            observed_prompt_sha256=digest("prompt"),
            observed_dossier_sha256=digest("dossier"),
            observed_delivery_sha256=digest("delivery"),
        )


@pytest.mark.parametrize("status", NON_READING_STATUSES)
def test_perlectio_refuses_stray_text_on_a_non_reading_status(status):
    resolved = identity("candidate", 1)
    with pytest.raises(MeasurementRefusal, match="carries status, not text"):
        Perlectio(
            identity=resolved,
            opaque_act_id="act-1",
            condition=Condition.WITNESS_PRIMED,
            status=status,
            text="stray",
            dossier_sha256=digest("dossier"),
            prompt_format_sha256=digest("prompt"),
            delivery_sha256=digest("delivery"),
            image_present=True,
            testimonia_count=1,
            dissent=DissentSummary(0, 0, 0),
            elapsed_ms=None,
            cost_usd=None,
        )


def test_candidate_response_refuses_text_over_the_one_act_bound():
    """scoring.py's Levenshtein.editops is worse-than-linear in the product of its

    two input lengths; an unbounded adapter response is a denial-of-service
    surface the same way an unbounded transcription draft is (adjudication.py).
    """

    oversized = "x" * (models.MAX_TEXT_LENGTH + 1)
    with pytest.raises(MeasurementRefusal, match="exceeds"):
        CandidateResponse(
            status=OutputStatus.COMPLETE,
            text=oversized,
            elapsed_ms=None,
            cost_usd=None,
            observed_prompt_sha256=digest("prompt"),
            observed_dossier_sha256=digest("dossier"),
            observed_delivery_sha256=digest("delivery"),
        )


def test_ground_truth_refuses_text_over_the_one_act_bound():
    oversized = "x" * (models.MAX_TEXT_LENGTH + 1)
    with pytest.raises(MeasurementRefusal, match="exceeds"):
        models.GroundTruth(
            text=oversized,
            adjudication_digest=digest("adjudication"),
            reference_revision="rev-1",
        )


def test_dossier_refuses_a_condition_that_is_not_a_condition():
    """Condition is a StrEnum, so a plain matching string passes an `in` check

    silently; only isinstance catches it.
    """

    act = evaluation_act()
    with pytest.raises(MatrixRefusal, match="Condition"):
        models.Dossier("lectio_nuda", "lectio_nuda", act.image, ())


@pytest.mark.parametrize("condition", [Condition.LECTIO_NUDA, Condition.WITNESS_PRIMED])
def test_perlectio_refuses_a_reading_condition_that_claims_no_image(condition):
    resolved = identity("candidate", 1)
    with pytest.raises(MeasurementRefusal, match="claims no image was present"):
        Perlectio(
            identity=resolved,
            opaque_act_id="act-1",
            condition=condition,
            status=OutputStatus.COMPLETE,
            text="a reading",
            dossier_sha256=digest("dossier"),
            prompt_format_sha256=digest("prompt"),
            delivery_sha256=digest("delivery"),
            image_present=False,
            testimonia_count=1,
            dissent=DissentSummary(0, 0, 0),
            elapsed_ms=None,
            cost_usd=None,
        )


def test_perlectio_refuses_a_witness_primed_cell_that_claims_no_testimonia():
    resolved = identity("candidate", 1)
    with pytest.raises(MeasurementRefusal, match="claims it saw no Testimonia"):
        Perlectio(
            identity=resolved,
            opaque_act_id="act-1",
            condition=Condition.WITNESS_PRIMED,
            status=OutputStatus.COMPLETE,
            text="a reading",
            dossier_sha256=digest("dossier"),
            prompt_format_sha256=digest("prompt"),
            delivery_sha256=digest("delivery"),
            image_present=True,
            testimonia_count=0,
            dissent=DissentSummary(0, 0, 0),
            elapsed_ms=None,
            cost_usd=None,
        )


def test_resolved_identity_refuses_a_delivery_mode_that_is_not_a_delivery_mode():
    with pytest.raises(MeasurementRefusal, match="DeliveryMode"):
        ResolvedIdentity(
            candidate_key="k",
            public_slot=1,
            source_ref="ref",
            revision="rev",
            artifact_digest=digest("artifact"),
            delivery="local",  # a plain string, not DeliveryMode.LOCAL
        )


def test_resolved_identity_accepts_a_genuine_delivery_mode():
    resolved = ResolvedIdentity(
        candidate_key="k",
        public_slot=1,
        source_ref="ref",
        revision="rev",
        artifact_digest=digest("artifact"),
        delivery=DeliveryMode.LOCAL,
    )
    assert resolved.delivery is DeliveryMode.LOCAL
