"""The truncation detector's signals, classification, and the mandatory record.

ARCHITECTURE: "It reads through to the end -- truncation is a failure, not an
output." Spec_08: the detector's signals are declared, each response classified
`complete | truncated | unknown`, and `unknown` holds -- never passed as
complete.
"""

import pytest
import truncation


def test_a_clean_short_reading_over_a_small_region_is_complete():
    record = truncation.classify("alpha beta gamma.", region_pixels=1000)
    assert record["classification"] == truncation.COMPLETE
    assert record["signals"] == {
        "stop_reason_declared": None,
        "unclosed_structure": False,
        "length_suspicious": False,
        "ends_abruptly": False,
    }


def test_an_engine_declared_length_stop_reason_is_authoritative_over_clean_text():
    """Even a reading that looks perfectly clean is `truncated` when the engine
    itself says it ran out of budget -- the one signal this module treats as
    outranking the other three."""
    record = truncation.classify("alpha beta gamma.", region_pixels=1000, stop_reason="length")
    assert record["classification"] == truncation.TRUNCATED
    assert record["signals"]["stop_reason_declared"] == "length"


def test_an_engine_declared_stop_reason_does_not_force_complete_over_bad_signals():
    """`stop` is not itself proof of completeness; it only refrains from
    forcing `truncated`. The computed signals still get to classify."""
    record = truncation.classify("cut off mid-", region_pixels=100_000, stop_reason="stop")
    assert record["classification"] in (truncation.TRUNCATED, truncation.UNKNOWN)


def test_an_unrecognized_stop_reason_refuses_rather_than_guesses():
    with pytest.raises(ValueError, match="neither 'stop' nor 'length'"):
        truncation.classify("text", region_pixels=100, stop_reason="banana")


@pytest.mark.parametrize("pair", [("(", ")"), ("[", "]"), ("“", "”")])
def test_an_unclosed_structural_mark_is_flagged(pair):
    opener, _closer = pair
    assert truncation.has_unclosed_structure(f"reading with {opener}unclosed") is True


def test_balanced_structure_is_not_flagged():
    assert truncation.has_unclosed_structure("a (balanced) reading [with marks]") is False


def test_ends_abruptly_uses_the_same_hyphen_rubric_as_attestatores_content_health():
    """Same rubric, named the same way as
    `pipeline/3_attestatores/run.py::content_health`, so both stages judge an
    abrupt ending consistently."""
    assert truncation.ends_abruptly("cut off mid-") is True
    assert truncation.ends_abruptly("a complete sentence") is False
    assert truncation.ends_abruptly("Jean Dupont, soussigné") is False, (
        "a real parish-register act routinely ends on a name, not a period -- "
        "punctuation is deliberately not the abrupt-ending rubric"
    )


def test_length_suspicious_needs_a_positive_region_pixel_count():
    with pytest.raises(ValueError, match="must be positive"):
        truncation.is_length_suspicious("text", 0)


def test_an_empty_reading_is_never_length_suspicious():
    """`no-readable-text` is the honest outcome for an empty reading, decided
    elsewhere; this signal must not smuggle a truncation classification onto
    an intentionally empty text."""
    assert truncation.is_length_suspicious("", 1_000_000) is False


def test_a_tiny_reading_under_a_huge_region_is_length_suspicious():
    assert truncation.is_length_suspicious("x", 1_000_000) is True


def test_all_three_computed_signals_agreeing_suspicious_is_truncated_not_unknown():
    """The all-suspicious case the design note names explicitly: a split vote
    holds as `unknown`, but unanimous suspicion is confident enough to call
    `truncated` outright."""
    text = "unclosed (mid-"
    record = truncation.classify(text, region_pixels=1_000_000)
    assert record["signals"]["unclosed_structure"] is True
    assert record["signals"]["length_suspicious"] is True
    assert record["signals"]["ends_abruptly"] is True
    assert record["classification"] == truncation.TRUNCATED


def test_a_split_vote_among_computed_signals_holds_as_unknown_never_complete():
    text = "unclosed (parenthetical but otherwise a normal length reading here"
    record = truncation.classify(text, region_pixels=1000)
    votes = sum(
        (
            record["signals"]["unclosed_structure"],
            record["signals"]["length_suspicious"],
            record["signals"]["ends_abruptly"],
        )
    )
    assert votes in (1, 2)
    assert record["classification"] == truncation.UNKNOWN


def test_holds_as_failure_covers_truncated_and_unknown_but_never_complete():
    assert truncation.holds_as_failure(truncation.TRUNCATED) is True
    assert truncation.holds_as_failure(truncation.UNKNOWN) is True
    assert truncation.holds_as_failure(truncation.COMPLETE) is False


def test_holds_as_failure_refuses_an_undeclared_classification():
    with pytest.raises(ValueError, match="not a declared truncation classification"):
        truncation.holds_as_failure("maybe")
