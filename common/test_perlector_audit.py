"""`change_record`'s attribution, and the one-byte suffix-trim coincidence.

A witness-derived (`testimony-diff`) flag's location is itself computed by
`text_change_span`, trimming a common suffix against the one testimony that
located it. `change_record` trims a common suffix too, against the actual
re-proof result. Both trims are exact for the pair they compare, but the two
pairs are different, so a trailing character `before` shares with testimony
by coincidence — not because it is genuinely unchanged — can leave a flag's
recorded end one byte short of a re-proof envelope that reaches the true end
of the text. These tests pin the fix (a one-byte gap at `len(before)` for a
witness-derived flag is still contained) and its boundary (any wider gap, or
a gap against a non-witness-derived flag, still refuses).
"""

import pytest

from common.contracts.errors import SchemaRefusal
from common.perlector_audit import change_record, text_change_span


def _flag(flag_class: str, start: int, end: int) -> dict:
    return {"class": flag_class, "location": {"start": start, "end": end}}


def test_text_change_span_trims_shared_prefix_and_suffix():
    assert text_change_span("abcXdef", "abcYdef") == (3, 4)
    assert text_change_span("same text", "same text") == (9, 9)
    assert text_change_span("abc", "abcXYZ") == (3, 3)


def test_a_witness_derived_flag_shy_by_the_shared_final_character_still_contains_the_reproof():
    """The exact coincidence: a witness and the reading share a final character.

    `before` ends "...kappa"; the testimony that located the flag also ends
    in "a", so `text_change_span` trims that one shared byte off the flag's
    end (`len(before) - 1`, not `len(before)`). The re-proof rewrites the
    trailing word entirely (a real tail rewrite, the production shape named
    in the diagnosis) to something that does *not* end in "a", so the
    re-proof's own change span reaches the true end of the text untrimmed.
    Before the fix this refused; the fix credits the one-byte gap to the
    flag whose own trim produced it.
    """
    before = "reading alpha beta kappa"
    after = "reading alpha beta epsilon"
    flag_end = len(before) - 1  # what a witness ending in the same "a" trims to
    flags = [_flag("testimony-diff", 19, flag_end)]

    changes = change_record(before, after, flags)

    assert changes == [{"start": 19, "end": len(before), "triggering_flag_class": "testimony-diff"}]


def test_a_real_overrun_past_a_witness_derived_flag_still_refuses():
    """More than one byte past a witness-derived flag's end is real content.

    Here the re-proof changes two trailing words, not one, so its envelope
    reaches two characters past the flag's suffix-trimmed end rather than
    one. That is a genuine escape from the flagged location, and the one-byte
    slack the fix adds must not swallow it.
    """
    before = "reading alpha beta gamma kappa"
    after = "reading alpha beta ZZZZZ YYYYY"
    flag_end = before.index("gamma") + len("gamma")  # only "gamma" was ever flagged
    flags = [_flag("testimony-diff", 20, flag_end)]

    with pytest.raises(SchemaRefusal, match="changed text outside every flagged location"):
        change_record(before, after, flags)


def test_the_one_byte_slack_is_refused_for_a_non_witness_derived_flag():
    """`date-sequence`, `numbering`, `order`, `repetition` and `within-crop`
    locations are not suffix-trimmed against a testimony this function never
    sees, so the coincidence the slack exists for cannot occur for them. A
    one-byte gap past one of these flags must still refuse.
    """
    before = "reading alpha beta kappa"
    after = "reading alpha beta epsilon"
    flag_end = len(before) - 1
    flags = [_flag("repetition", 19, flag_end)]

    with pytest.raises(SchemaRefusal, match="changed text outside every flagged location"):
        change_record(before, after, flags)


def test_a_gap_that_does_not_reach_the_true_end_of_the_text_still_refuses():
    """The slack only ever applies at `len(before)`: a suffix trim can only
    ever fall short at the true end of a string, never in the middle, so a
    one-byte-short flag whose gap sits short of `len(before)` is not this
    coincidence and must still refuse.
    """
    before = "reading alpha beta kappa trailing tail"
    after = "reading alpha beta epsilon trailing tail"
    flag_end = before.index("kappa") + len("kappa") - 1  # one byte short, mid-string
    flags = [_flag("testimony-diff", 19, flag_end)]

    with pytest.raises(SchemaRefusal, match="changed text outside every flagged location"):
        change_record(before, after, flags)


def test_change_record_attributes_to_the_narrowest_containing_flag():
    before = "one two three four"
    after = "one TWO three four"
    flags = [_flag("date-sequence", 0, len(before)), _flag("testimony-diff", 4, 7)]

    changes = change_record(before, after, flags)

    assert changes == [{"start": 4, "end": 7, "triggering_flag_class": "testimony-diff"}]


def test_change_record_returns_nothing_for_identical_text():
    assert change_record("same", "same", [_flag("testimony-diff", 0, 4)]) == []
