from importlib.metadata import version

import pytest

from operations.spike_perlector.errors import MeasurementRefusal
from operations.spike_perlector.normalization import (
    ALLOGRAPHIC_V1,
    GRAPHEMIC_V1,
    MAX_COMBINING_RUN,
    MAX_TEXT_LENGTH,
    PREDECLARED_PROFILE,
    PROFILES,
    character_units,
    normalize_text,
    require_canonical_profile,
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
    """The record is what the profile digest is taken over, so it names the rule
    that runs: NFC, then the mappings, then NFC again."""

    assert GRAPHEMIC_V1.record()["unicode_normalization"] == "NFC-then-mappings-then-NFC"
    assert GRAPHEMIC_V1.digest != ALLOGRAPHIC_V1.digest


# The profile digests under the locked uniseg. A change to any mapping, the
# whitespace class, a text bound or the record's shape changes a digest, and
# this test makes that change a reviewed edit rather than a silent one.
PINNED_PROFILE_DIGESTS = {
    "graphemic-v1": "4fc470678289338900c4fa897b8c5b4f3acc1ca3fd429ef6ccffaeae352cc58c",
    "allographic-v1": "534d2771d5fbbc1d6eaed6d96e5c51e56fbd5a19b3a3cfecf1c9fc2aca5773b9",
}


def test_profile_digests_are_pinned():
    assert {profile.profile_id: profile.digest for profile in PROFILES.values()} == (
        PINNED_PROFILE_DIGESTS
    )


def test_the_profile_digest_binds_the_actual_definitions_not_their_labels():
    record = GRAPHEMIC_V1.record()
    character_map = dict(
        (source, "".join(chr(int(item[2:], 16)) for item in target))
        for source, target in record["character_map"]
    )
    assert character_map["U+2019"] == "'"
    assert character_map["U+FB05"] == "st"
    assert character_map["U+017F"] == "s"
    assert dict(ALLOGRAPHIC_V1.record()["character_map"]).get("U+017F") is None
    assert ["U+2000", "U+200A"] in record["whitespace"]["code_point_ranges"]
    assert record["text_bounds"] == {
        "max_text_length": MAX_TEXT_LENGTH,
        "max_combining_run": MAX_COMBINING_RUN,
    }


@pytest.mark.parametrize(
    ("raw", "graphemic", "allographic"),
    [
        ("  Jean\u00a0\u00a0Baptiste\n", "Jean Baptiste", "Jean Baptiste"),
        ("l\u2019an\u2011dit", "l'an-dit", "l'an-dit"),
        ("\ufb01ls \ufb05e", "fils ste", "fils \u017fte"),
        ("Mai\u017fon", "Maison", "Mai\u017fon"),
        ("\u017f\u0301", "\u015b", "\u017f\u0301"),
        ("e\u0301glise \u0153uvre", "\u00e9glise \u0153uvre", "\u00e9glise \u0153uvre"),
        ("a\x1cb", "a\x1cb", "a\x1cb"),
        ("a\u3000\u2028b", "a b", "a b"),
    ],
)
def test_golden_normalizations(raw, graphemic, allographic):
    assert normalize_text(raw, GRAPHEMIC_V1) == graphemic
    assert normalize_text(raw, ALLOGRAPHIC_V1) == allographic


def test_whitespace_is_unicode_white_space_not_pythons_wider_isspace():
    """README section 6 folds Unicode whitespace. Python's `str.isspace` and `\\s`
    also accept the information separators U+001C..U+001F, which are not
    White_Space; everything else they accept is."""

    python_space = {chr(code) for code in range(0x110000) if chr(code).isspace()}
    folded = {
        character
        for character in python_space
        if normalize_text(f"a{character}b", GRAPHEMIC_V1) == "a b"
    }
    assert python_space - folded == {"\x1c", "\x1d", "\x1e", "\x1f"}
    assert normalize_text("\x1ca\x1f", GRAPHEMIC_V1) == "\x1ca\x1f"


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


def test_graphemic_v1_is_the_only_predeclared_real_run_profile():
    assert PREDECLARED_PROFILE is GRAPHEMIC_V1
    assert require_canonical_profile(GRAPHEMIC_V1) is GRAPHEMIC_V1
    with pytest.raises(MeasurementRefusal, match="predeclared graphemic-v1"):
        require_canonical_profile(ALLOGRAPHIC_V1)


def test_character_units_are_extended_graphemes_and_words_are_space_delimited():
    assert character_units("e\u0301", GRAPHEMIC_V1) == ("é",)
    assert word_units("alpha, beta", GRAPHEMIC_V1) == ("alpha,", "beta")


def test_the_long_s_t_ligature_preserves_long_s_under_the_preserving_profile():
    """The precomposed long-s+t ligature must normalize the same as a scribe's
    decomposed long-s-then-t under whichever profile is selected -- the same
    ink should not score differently depending on which of the two encodings
    a candidate happens to emit."""

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
    version that segments under different rules than the one recorded."""

    assert version("uniseg") in GRAPHEMIC_V1.record()["character_units"]
