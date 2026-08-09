"""Double-keying plus adjudicator, and the gaps a checked reference may carry.

Every string here is invented for arithmetic and wiring. None of it is register
content and none of it came from a real transcription.
"""

import pytest

from operations.spike_perlector.adjudication import (
    ILLEGIBLE,
    AdjudicationRecord,
    AdjudicationRefusal,
    TranscriptionDraft,
    disagreement_spans,
    reconcile,
)
from operations.spike_perlector.errors import MeasurementRefusal
from operations.spike_perlector.models import GapSpan, GroundTruth, ReferenceStatus, excise_gaps
from operations.spike_perlector.normalization import GRAPHEMIC_V1
from operations.spike_perlector.scoring import score_text

REVISION = "synthetic-reference-v1"


def drafts(first_text: str, second_text: str):
    return (
        TranscriptionDraft("transcriber-a", first_text),
        TranscriptionDraft("transcriber-b", second_text),
    )


def test_identical_drafts_reconcile_and_still_produce_two_distinct_digests():
    """The good case: two people read an easy act the same way.

    Lane B's ground-truth rule refuses two equal draft digests, which is right for
    one draft counted twice and wrong for two transcribers agreeing. Hashing the
    draft *record* rather than the bare text is what keeps both true.
    """
    first, second = drafts("Jean Baptiste", "Jean Baptiste")
    assert disagreement_spans(first.text, second.text) == ()
    record = reconcile(
        first=first,
        second=second,
        adjudicator_id="adjudicator-c",
        resolutions={},
        reference_revision=REVISION,
    )
    assert record.reconciled_text == "Jean Baptiste"
    assert record.gaps == ()
    truth = record.ground_truth()
    assert truth.status is ReferenceStatus.CHECKED
    assert len(set(truth.independent_draft_sha256s)) == 2


def test_the_same_draft_counted_twice_is_refused():
    same = TranscriptionDraft("transcriber-a", "Jean Baptiste")
    with pytest.raises(AdjudicationRefusal, match="one person's reading twice"):
        reconcile(
            first=same,
            second=same,
            adjudicator_id="adjudicator-c",
            resolutions={},
            reference_revision=REVISION,
        )


def test_an_oversized_draft_is_refused_rather_than_hung_on():
    """difflib.SequenceMatcher with autojunk=False is quadratic on repetitive input.

    A draft this large is a mis-pasted file, not one act's transcription; refusing
    it here keeps reconcile()/disagreement_spans() from ever being handed one.
    """

    oversized = "x" * (TranscriptionDraft.MAX_TEXT_LENGTH + 1)
    with pytest.raises(AdjudicationRefusal, match="exceeds"):
        TranscriptionDraft("transcriber-a", oversized)


def test_an_adjudicator_who_is_one_of_the_transcribers_is_refused():
    first, second = drafts("Jean", "Jehan")
    with pytest.raises(AdjudicationRefusal, match="one of the two transcribers"):
        reconcile(
            first=first,
            second=second,
            adjudicator_id="transcriber-a",
            resolutions=dict.fromkeys(disagreement_spans(first.text, second.text), "Jean"),
            reference_revision=REVISION,
        )


def test_a_resolved_disagreement_takes_the_adjudicators_text():
    first, second = drafts("Jean Baptiste", "Jehan Baptiste")
    spans = disagreement_spans(first.text, second.text)
    # The drafts differ by one inserted character, so the disputed span is
    # zero-width: the point in the first draft where the second has more.
    assert spans == ((2, 2),)
    record = reconcile(
        first=first,
        second=second,
        adjudicator_id="adjudicator-c",
        resolutions={spans[0]: "h"},
        reference_revision=REVISION,
    )
    assert record.reconciled_text == "Jehan Baptiste"
    assert record.gaps == ()


def test_a_missing_resolution_refuses_rather_than_choosing_a_draft():
    first, second = drafts("Jean Baptiste", "Jehan Baptiste")
    with pytest.raises(AdjudicationRefusal, match="missing"):
        reconcile(
            first=first,
            second=second,
            adjudicator_id="adjudicator-c",
            resolutions={},
            reference_revision=REVISION,
        )


def test_a_resolution_for_a_span_that_does_not_exist_refuses():
    first, second = drafts("Jean Baptiste", "Jehan Baptiste")
    spans = disagreement_spans(first.text, second.text)
    with pytest.raises(AdjudicationRefusal, match="unexpected"):
        reconcile(
            first=first,
            second=second,
            adjudicator_id="adjudicator-c",
            resolutions={spans[0]: "Jeh", (99, 101): "anything"},
            reference_revision=REVISION,
        )


def test_illegible_becomes_a_zero_width_gap_and_never_characters():
    first, second = drafts("Jean X Baptiste", "Jean Y Baptiste")
    spans = disagreement_spans(first.text, second.text)
    record = reconcile(
        first=first,
        second=second,
        adjudicator_id="adjudicator-c",
        resolutions={spans[0]: ILLEGIBLE},
        reference_revision=REVISION,
    )
    assert record.reconciled_text == "Jean  Baptiste"
    assert len(record.gaps) == 1
    gap = record.gaps[0]
    assert gap.start == gap.end
    assert excise_gaps(record.reconciled_text, record.gaps) == record.reconciled_text


def test_an_act_nobody_could_read_refuses_instead_of_a_blank_checked_reference():
    first, second = drafts("abc", "xyz")
    spans = disagreement_spans(first.text, second.text)
    with pytest.raises(AdjudicationRefusal, match="unresolved_gap"):
        reconcile(
            first=first,
            second=second,
            adjudicator_id="adjudicator-c",
            resolutions=dict.fromkeys(spans, ILLEGIBLE),
            reference_revision=REVISION,
        )


def test_the_digest_moves_when_a_resolution_moves():
    first, second = drafts("Jean Baptiste", "Jehan Baptiste")
    spans = disagreement_spans(first.text, second.text)
    one = reconcile(
        first=first,
        second=second,
        adjudicator_id="adjudicator-c",
        resolutions={spans[0]: "Jeh"},
        reference_revision=REVISION,
    )
    other = reconcile(
        first=first,
        second=second,
        adjudicator_id="adjudicator-c",
        resolutions={spans[0]: "Je"},
        reference_revision=REVISION,
    )
    assert one.digest != other.digest
    assert one.ground_truth().adjudication_digest == one.digest


def test_both_drafts_and_every_resolution_survive_on_the_record():
    first, second = drafts("Jean Baptiste", "Jehan Baptiste")
    spans = disagreement_spans(first.text, second.text)
    record = reconcile(
        first=first,
        second=second,
        adjudicator_id="adjudicator-c",
        resolutions={spans[0]: "Jeh"},
        reference_revision=REVISION,
    )
    assert record.first.text == "Jean Baptiste"
    assert record.second.text == "Jehan Baptiste"
    assert record.resolutions == ((spans[0], "Jeh"),)


def test_a_directly_constructed_adjudication_record_cannot_disagree_with_its_drafts():
    """The dataclass constructor must enforce the same identity-bearing trail as reconcile."""

    first, second = drafts("Jean Baptiste", "Jehan Baptiste")
    spans = disagreement_spans(first.text, second.text)
    with pytest.raises(AdjudicationRefusal, match="replayed against its drafts"):
        AdjudicationRecord(
            first=first,
            second=second,
            adjudicator_id="adjudicator-c",
            resolutions=((spans[0], "h"),),
            reference_revision=REVISION,
            reconciled_text="forged reading",
            gaps=(),
        )


# --- the gap span itself, once it is on a GroundTruth -------------------------


def checked(text: str, gaps: tuple[GapSpan, ...] = ()) -> GroundTruth:
    return GroundTruth(
        text=text,
        adjudication_digest="0" * 64,
        reference_revision=REVISION,
        gaps=gaps,
        independent_draft_sha256s=("1" * 64, "2" * 64),
    )


def test_a_gap_is_removed_from_the_reference_before_it_is_scored():
    truth = checked("Jean UNREAD Baptiste", (GapSpan(5, 12),))
    assert truth.scoreable_text == "Jean Baptiste"


def test_reference_gap_characters_are_removed_before_scoring():
    truth = checked("Jean UNREAD Baptiste", (GapSpan(5, 12),))
    perfect = score_text(truth.scoreable_text, "Jean Baptiste", profile=GRAPHEMIC_V1)
    assert perfect.cer.edits.errors == 0
    assert perfect.cer.reference_units == len("Jean Baptiste")


def test_text_at_a_gap_cannot_be_located_and_may_score_as_an_insertion():
    """The text-only scorer has no spatial alignment; this pins the named limitation."""

    truth = checked("Jean UNREAD Baptiste", (GapSpan(5, 12),))
    score = score_text(truth.scoreable_text, "Jean guessed Baptiste", profile=GRAPHEMIC_V1)
    assert score.cer.edits.errors > 0


def test_an_all_gap_checked_reference_is_refused():
    with pytest.raises(MeasurementRefusal, match="unresolved_gap"):
        checked("UNREAD", (GapSpan(0, 6),))


def test_unsorted_or_overlapping_gaps_are_refused():
    with pytest.raises(MeasurementRefusal, match="sorted and non-overlapping"):
        checked("Jean Baptiste", (GapSpan(5, 9), GapSpan(2, 4)))


def test_a_gap_past_the_end_of_the_reference_is_refused():
    with pytest.raises(MeasurementRefusal, match="reaches past"):
        checked("Jean", (GapSpan(2, 40),))


def test_an_unread_crop_carries_a_status_not_a_gap():
    with pytest.raises(MeasurementRefusal, match="an unread crop is a status"):
        GroundTruth(
            text=None,
            adjudication_digest="0" * 64,
            reference_revision=REVISION,
            status=ReferenceStatus.UNRESOLVED_GAP,
            gaps=(GapSpan(0, 0),),
        )


def test_the_gap_excised_form_is_bound_as_held_out_evidence():
    """Binding only the raw text would leave the comparison form free to travel."""
    from operations.spike_perlector.encoding import sha256_bytes

    truth = checked("Jean UNREAD Baptiste", (GapSpan(5, 12),))
    assert sha256_bytes(b"Jean Baptiste") in truth.held_out_evidence_sha256s
