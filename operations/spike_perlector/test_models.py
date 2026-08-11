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


# A valid ``str`` Python will not encode, built without depending on the JSON
# scanner's handling of lone-surrogate escape sequences during test collection.
LONE_SURROGATE = "alpha " + chr(0xD800) + " beta"


def test_candidate_response_refuses_text_python_cannot_encode():
    """A vendor JSON body can carry an unpaired surrogate, and this one does.

    It passes every "is it a non-blank string" check, then raises a bare
    UnicodeEncodeError inside the first digest or score -- losing every cell
    already measured, with an error naming neither the act nor the reason.
    Refused at the boundary, an adapter can record `malformed` for that cell
    and the matrix survives.
    """

    with pytest.raises(MeasurementRefusal, match="unpaired surrogate"):
        CandidateResponse(
            status=OutputStatus.COMPLETE,
            text=LONE_SURROGATE,
            elapsed_ms=None,
            cost_usd=None,
            observed_prompt_sha256=digest("prompt"),
            observed_dossier_sha256=digest("dossier"),
            observed_delivery_sha256=digest("delivery"),
        )


def test_ground_truth_refuses_a_reference_python_cannot_encode():
    with pytest.raises(MeasurementRefusal, match="unpaired surrogate"):
        models.GroundTruth(
            text=LONE_SURROGATE,
            adjudication_digest=digest("adjudication"),
            reference_revision="rev-1",
        )


def test_a_stack_of_combining_marks_is_bounded_where_segmentation_is_quadratic():
    """uniseg segments one long grapheme cluster in time quadratic in its length.

    Measured against uniseg 0.10.1 on 2026-08-09: 4,000 marks on one base
    character take 5.9s and 8,000 take 23.5s, so the 20,000-character bound
    alone would admit minutes of CPU per scored cell from one response. The cap
    is UAX #15's stream-safe limit; three marks is already unusual in polytonic
    Greek, so nothing a transcriber writes comes near it.
    """

    def response(text):
        return CandidateResponse(
            status=OutputStatus.COMPLETE,
            text=text,
            elapsed_ms=None,
            cost_usd=None,
            observed_prompt_sha256=digest("prompt"),
            observed_dossier_sha256=digest("dossier"),
            observed_delivery_sha256=digest("delivery"),
        )

    at_the_cap = "a" + "́" * models.MAX_COMBINING_RUN
    assert response(at_the_cap).text == at_the_cap
    with pytest.raises(MeasurementRefusal, match="combining marks"):
        response("a" + "́" * (models.MAX_COMBINING_RUN + 1))


def test_combining_mark_bound_uses_the_pinned_unicode_table_across_python_versions():
    """U+0897 is a Unicode-16 combining mark but unassigned in Python 3.13's UCD."""

    with pytest.raises(MeasurementRefusal, match="combining marks"):
        CandidateResponse(
            status=OutputStatus.COMPLETE,
            text="a" + chr(0x0897) * (models.MAX_COMBINING_RUN + 1),
            elapsed_ms=None,
            cost_usd=None,
            observed_prompt_sha256=digest("prompt"),
            observed_dossier_sha256=digest("dossier"),
            observed_delivery_sha256=digest("delivery"),
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


def test_perlectio_refuses_an_identity_that_is_not_a_resolved_identity():
    """``public_slot`` is read off this field and aggregated into the public finding.

    Every other field here is type-checked; a stand-in accepted for this one would
    put a caller-chosen slot number into a history record.
    """

    with pytest.raises(MeasurementRefusal, match="checked ResolvedIdentity"):
        Perlectio(
            identity="private-model-name-one",
            opaque_act_id="act-1",
            condition=Condition.LECTIO_NUDA,
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


def _witness_primed_perlectio(**overrides):
    fields = dict(
        identity=identity("candidate", 1),
        opaque_act_id="act-1",
        condition=Condition.WITNESS_PRIMED,
        status=OutputStatus.COMPLETE,
        text="a reading",
        dossier_sha256=digest("dossier"),
        prompt_format_sha256=digest("prompt"),
        delivery_sha256=digest("delivery"),
        image_present=True,
        testimonia_count=1,
        dissent=DissentSummary(0, 0, 0),
        elapsed_ms=None,
        cost_usd=None,
    )
    fields.update(overrides)
    return Perlectio(**fields)


def test_a_perlectio_testimonia_count_must_be_a_count_before_it_is_read_as_a_flag():
    """Every condition rule reads this field for truthiness alone.

    So `-1` and `True` both passed as "saw Testimonia" and were then retained as
    evidence — and the number travels into the dissent and parroting measures,
    where a count that is not a count is a measurement claim about something that
    never happened (GOVERNANCE 10). Found by CodeRabbit.
    """

    for wrong in (True, -1, 2.0, "3"):
        with pytest.raises(MeasurementRefusal, match="testimonia_count"):
            _witness_primed_perlectio(testimonia_count=wrong)


def test_a_real_testimonia_count_is_still_accepted():
    """Invariant #14: a valid count is retained unchanged, not only accepted."""

    assert _witness_primed_perlectio(testimonia_count=3).testimonia_count == 3
