from operations.spike_perlector.normalization import (
    ALLOGRAPHIC_V1,
    GRAPHEMIC_V1,
    character_units,
    normalize_text,
    word_units,
)


def test_nfc_whitespace_and_named_presentation_variants_are_canonicalized():
    assert normalize_text("e\u0301\tA\u2019B\u2011C\n", GRAPHEMIC_V1) == "é A'B-C"


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
