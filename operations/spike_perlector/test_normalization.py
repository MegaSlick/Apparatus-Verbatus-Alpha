from importlib.metadata import version

from operations.spike_perlector.normalization import (
    ALLOGRAPHIC_V1,
    GRAPHEMIC_V1,
    character_units,
    normalize_text,
    word_units,
)


def test_nfc_whitespace_and_named_presentation_variants_are_canonicalized():
    assert normalize_text("e\u0301\tA\u2019B\u2011C\n", GRAPHEMIC_V1) == "é A'B-C"


def test_a_mapped_long_s_recomposes_so_one_character_is_not_two_readings():
    """The mappings run after the first NFC, so the output needed a second one.

    `ſ` + U+0301 has no precomposed form, so NFC leaves it decomposed. Mapping
    the long-s then yielded `s` + U+0301 while the same character written
    precomposed stayed U+015B. Two correct readings of one character compared as
    a substitution, and CER charged the candidate for reading it right.
    """

    from_long_s = normalize_text("ſ́", GRAPHEMIC_V1)
    precomposed = normalize_text("ś", GRAPHEMIC_V1)
    assert from_long_s == precomposed == "ś"


def test_the_profile_record_names_the_two_pass_rule_the_code_actually_applies():
    """The record is what the profile digest is taken over.

    While it read `"NFC"` and the code ran NFC, then its mappings, then NFC
    again, two runs normalizing by different rules produced the same profile
    digest and nothing downstream could tell them apart.
    """

    assert GRAPHEMIC_V1.record()["unicode_normalization"] == "NFC-then-mappings-then-NFC"
    assert GRAPHEMIC_V1.digest != ALLOGRAPHIC_V1.digest


def test_only_listed_presentation_ligatures_expand():
    assert normalize_text("\ufb01 \u0153 \u00e6", GRAPHEMIC_V1) == "fi œ æ"


def test_historical_distinctions_remain_significant_under_recommended_profile():
    normalized = normalize_text("A, é i u œ", GRAPHEMIC_V1)
    assert normalized == "A, é i u œ"
    assert normalized != normalize_text("a e j v oe", GRAPHEMIC_V1)


def test_long_s_is_a_sealed_profile_difference():
    assert normalize_text("ſ", GRAPHEMIC_V1) == "s"
    assert normalize_text("ſ", ALLOGRAPHIC_V1) == "ſ"
    assert GRAPHEMIC_V1.digest != ALLOGRAPHIC_V1.digest


def test_character_units_are_extended_graphemes_and_words_are_space_delimited():
    assert character_units("e\u0301", GRAPHEMIC_V1) == ("é",)
    assert word_units("alpha, beta", GRAPHEMIC_V1) == ("alpha,", "beta")


def test_the_long_s_t_ligature_preserves_long_s_under_the_preserving_profile():
    """The precomposed long-s+t ligature must normalize the same as a scribe's
    decomposed long-s-then-t under whichever profile is selected -- the same
    ink should not score differently depending on which of the two encodings
    a candidate happens to emit (audit-c finding 2)."""

    ligature = "ﬅ"
    decomposed = "ſt"
    assert normalize_text(ligature, GRAPHEMIC_V1) == normalize_text("st", GRAPHEMIC_V1) == "st"
    assert (
        normalize_text(ligature, ALLOGRAPHIC_V1)
        == normalize_text(decomposed, ALLOGRAPHIC_V1)
        == decomposed
    )


def test_the_normalization_profile_records_the_actually_installed_uniseg_version():
    """A profile digest naming a uniseg version nobody checks could seal a
    version that segments under different rules than the one recorded
    (audit-d finding F8)."""

    assert version("uniseg") in GRAPHEMIC_V1.record()["character_units"]
