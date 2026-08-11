import pytest

from operations.spike_perlector.errors import MeasurementRefusal
from operations.spike_perlector.models import OutputStatus
from operations.spike_perlector.normalization import GRAPHEMIC_V1
from operations.spike_perlector.scoring import score_response, score_text


@pytest.mark.parametrize(
    ("hypothesis", "substitutions", "insertions", "deletions", "cer"),
    [
        ("abc", 0, 0, 0, 0.0),
        ("axc", 1, 0, 0, 1 / 3),
        ("ab", 0, 0, 1, 1 / 3),
        ("abxc", 0, 1, 0, 1 / 3),
        ("", 0, 0, 3, 1.0),
    ],
)
def test_hand_worked_character_error_examples(
    hypothesis, substitutions, insertions, deletions, cer
):
    score = score_text("abc", hypothesis, profile=GRAPHEMIC_V1).cer
    assert score.edits.substitutions == substitutions
    assert score.edits.insertions == insertions
    assert score.edits.deletions == deletions
    assert score.rate == pytest.approx(cer)


def test_hand_worked_word_error_example():
    score = score_text("un deux trois", "un trois quatre", profile=GRAPHEMIC_V1).wer
    assert score.edits.errors == 2
    assert score.reference_units == 3
    assert score.rate == pytest.approx(2 / 3)


def test_nfc_and_whitespace_are_applied_equally_to_reference_and_hypothesis():
    score = score_text("e\u0301\tA\nB", "é A B", profile=GRAPHEMIC_V1)
    assert score.cer.rate == 0
    assert score.wer.rate == 0


@pytest.mark.parametrize(
    ("reference", "hypothesis", "distinction"),
    (
        ("Marie", "marie", "case"),
        ("a, b", "a b", "punctuation"),
        ("marié", "marie", "diacritic"),
    ),
)
def test_each_historical_distinction_separately_remains_an_error(
    reference, hypothesis, distinction
):
    """One combined assertion could not tell which distinction had regressed.

    `score_text("A, é", "a e")` differs in all three at once, so folding any
    single one away left the rate above zero and the test green. Each is now
    isolated, with the others held equal.
    """

    assert score_text(reference, hypothesis, profile=GRAPHEMIC_V1).cer.rate > 0, distinction


@pytest.mark.parametrize(
    "status",
    [
        OutputStatus.NO_READABLE_TEXT,
        OutputStatus.REFUSED,
        OutputStatus.MISSING,
        OutputStatus.UNAVAILABLE,
        OutputStatus.MALFORMED,
    ],
)
def test_non_answers_score_as_empty_hypotheses(status):
    score = score_response("abc", status=status, text="would be ignored", profile=GRAPHEMIC_V1)
    assert score.cer.edits.deletions == 3
    assert score.cer.rate == 1


def test_truncation_scores_the_observed_partial_text():
    score = score_response("abcd", status=OutputStatus.TRUNCATED, text="ab", profile=GRAPHEMIC_V1)
    assert score.cer.edits.deletions == 2
    assert score.cer.rate == 0.5


def test_blank_checked_reference_is_not_given_a_perfect_score():
    with pytest.raises(MeasurementRefusal, match="blank checked"):
        score_text("", "", profile=GRAPHEMIC_V1)


def test_the_textbook_levenshtein_example():
    """kitten -> sitting: three edits, the standard worked example.

    Grafted from lane A. Its value is that a reader can check it against the
    literature rather than against this repository: two substitutions (k->s, e->i)
    and one insertion (g), over a six-character reference.
    """
    score = score_text("kitten", "sitting", profile=GRAPHEMIC_V1).cer
    assert (score.edits.substitutions, score.edits.insertions, score.edits.deletions) == (2, 1, 0)
    assert score.reference_units == 6
    assert score.rate == pytest.approx(3 / 6)


def test_the_textbook_word_error_rate_example():
    """ "this is a test" -> "this is test": one deletion of four words, WER 0.25."""
    score = score_text("this is a test", "this is test", profile=GRAPHEMIC_V1).wer
    assert (score.edits.substitutions, score.edits.insertions, score.edits.deletions) == (0, 0, 1)
    assert score.reference_units == 4
    assert score.rate == pytest.approx(0.25)
