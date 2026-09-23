"""The truncation detector's signals, classification, and the mandatory record.

ARCHITECTURE: "It reads through to the end -- truncation is a failure, not an
output." Spec_08: the detector's signals are declared, each response classified
`complete | truncated | unknown`, and `unknown` holds -- never passed as
complete.
"""

from pathlib import Path

import protocol
import pytest
import truncation

from common.contracts.errors import ContractError

ROOT = Path(__file__).resolve().parents[2]
# The sealed `[truncation]` table every verdict below is judged under, read
# the way the stage reads it. `POLICY` is the whole table; the floor is what the
# calibration pins at the end of this module speak about by name.
POLICY = protocol.load(ROOT / "config" / "perlector_protocol.toml")[0][protocol.TRUNCATION_TABLE]
FLOOR = POLICY[truncation.LENGTH_FLOOR_FIELD]

# This repository's own fixture geometry (proof/synthetic_pages.py, build_fixture.py):
# a 200x260 page, an act crop of 160x80, and the 34-character reading declared
# for it. The unit tests that are not about scale run at this geometry, which is
# the one the module's original tests ran at with `region_pixels=1000`.
FIXTURE_PAGE = 200 * 260
FIXTURE_REGION = 160 * 80
FIXTURE_TEXT = "SYNTHETIC ACT ONE alpha beta gamma"

# A photographed 300-DPI letter leaf and a single line band cut from it, the
# geometry the pre-launch review measured the retired constant against (F082):
# 2,550x104 is one line at a 104 px pitch, doubled from the 52 px at 150 DPI
# `config/pdf_render.toml` records measuring -- that file states no 300-DPI
# pitch of its own.
LEAF_PAGE = 2550 * 3300
LINE_BAND = 2550 * 104
# A realistic character count for one dense register line.
LINE_TEXT = "L'an mil sept cent quarante deux le douze de may a este baptise Jean fils de"
assert 60 <= len(LINE_TEXT) <= 90


def classify(text, *, region_pixels=FIXTURE_REGION, page_pixels=FIXTURE_PAGE, stop_reason=None):
    return truncation.classify(
        text,
        region_pixels=region_pixels,
        page_pixels=page_pixels,
        truncation_policy=POLICY,
        stop_reason=stop_reason,
    )


def length_suspicious(text, region_pixels, *, page_pixels=FIXTURE_PAGE, floor=FLOOR):
    return truncation.is_length_suspicious(
        text, region_pixels, page_pixels=page_pixels, length_floor_characters_per_page=floor
    )


def test_a_clean_short_reading_whose_engine_reported_stop_is_complete():
    record = classify("alpha beta gamma.", region_pixels=1000, stop_reason="stop")
    assert record["classification"] == truncation.COMPLETE
    assert record["signals"] == {
        "stop_reason_declared": "stop",
        "unclosed_structure": False,
        "length_suspicious": False,
        "ends_abruptly": False,
    }
    # What the length signal was judged from travels on the record -- every term
    # of the predicate, the floor included, so a reader holding nothing but the
    # record re-derives the signal rather than trusting it.
    assert record["measure"] == {
        "region_pixels": 1000,
        "page_pixels": FIXTURE_PAGE,
        "characters": len("alpha beta gamma."),
        "length_floor_characters_per_page": FLOOR,
    }


def test_a_clean_reading_with_no_engine_observation_at_all_holds_as_unknown():
    """The fail-closed half of the rule. Three clean computed signals say the
    reading does not *look* cut off, which is not the same claim as "the engine
    ran to its own end" -- an engine cut off at a sentence boundary produces
    exactly this text. With nothing observed from the engine the classification
    holds rather than reading as complete."""
    record = classify("alpha beta gamma.", region_pixels=1000)
    assert record["classification"] == truncation.UNKNOWN
    assert record["signals"]["stop_reason_declared"] is None
    assert truncation.holds_as_failure(record["classification"]) is True


def test_an_engine_declared_length_stop_reason_is_authoritative_over_clean_text():
    """Even a reading that looks perfectly clean is `truncated` when the engine
    itself says it ran out of budget -- the one signal this module treats as
    outranking the other three."""
    record = classify("alpha beta gamma.", region_pixels=1000, stop_reason="length")
    assert record["classification"] == truncation.TRUNCATED
    assert record["signals"]["stop_reason_declared"] == "length"


def test_an_engine_declared_stop_reason_does_not_force_complete_over_bad_signals():
    """`stop` is not itself proof of completeness; it only refrains from
    forcing `truncated`. The computed signals still get to classify."""
    record = classify("cut off mid-", region_pixels=FIXTURE_REGION, stop_reason="stop")
    assert record["classification"] in (truncation.TRUNCATED, truncation.UNKNOWN)


def test_an_unrecognized_stop_reason_refuses_rather_than_guesses():
    """A structured refusal, not a bare crash: `stop_reason` is the one signal
    in this module that will carry a real serving engine's own untrusted
    finish-reason string once a real reader occupies this seam, and every
    other boundary in this stage refuses unrecognized input by name rather
    than letting it escape as an unhandled exception."""
    with pytest.raises(ContractError, match="neither 'stop' nor 'length'"):
        classify("text", region_pixels=100, stop_reason="banana")


@pytest.mark.parametrize("pair", [("(", ")"), ("[", "]"), ("“", "”")])
def test_an_unclosed_structural_mark_is_flagged(pair):
    opener, _closer = pair
    assert truncation.has_unclosed_structure(f"reading with {opener}unclosed") is True


@pytest.mark.parametrize("pair", [("(", ")"), ("[", "]"), ("\u201c", "\u201d")])
def test_a_closed_structural_pair_is_not_flagged(pair):
    """The closer of every declared pair must close it: a detector that counted
    an opener and never its closer would flag every act carrying French
    quotation marks, and two of three signals would then hold ordinary quoted
    acts for review."""
    opener, closer = pair
    assert truncation.has_unclosed_structure(f"reading with {opener}closed{closer}") is False


def test_balanced_structure_is_not_flagged():
    assert truncation.has_unclosed_structure("a (balanced) reading [with marks]") is False


def test_ends_abruptly_flags_a_trailing_hyphen_and_spares_ordinary_endings():
    """Trailing whitespace is stripped before the hyphen is read, so "word-   "
    is abrupt; a name or a signature at the end of a reading is not."""
    assert truncation.ends_abruptly("cut off mid-") is True
    assert truncation.ends_abruptly("cut off mid-   ") is True, (
        "trailing whitespace must not hide a truncation"
    )
    assert truncation.ends_abruptly("a complete sentence") is False
    assert truncation.ends_abruptly("Jean Dupont, soussigné") is False, (
        "a real parish-register act routinely ends on a name, not a period -- "
        "punctuation is deliberately not the abrupt-ending rubric"
    )


def test_length_suspicious_needs_a_positive_region_pixel_count():
    with pytest.raises(ValueError, match="region_pixels must be positive"):
        length_suspicious("text", 0)


def test_length_suspicious_needs_a_positive_page_pixel_count():
    with pytest.raises(ValueError, match="page_pixels must be positive"):
        length_suspicious("text", 100, page_pixels=0)


def test_length_suspicious_refuses_a_floor_that_could_never_fire():
    with pytest.raises(ValueError, match="zero never fires"):
        length_suspicious("text", 100, floor=0)


@pytest.mark.parametrize("floor", [True, "50", 50.0, None, 0, -1])
def test_classify_refuses_a_policy_whose_floor_is_not_a_positive_integer(floor):
    """Zero and negatives by name, not as an escaping `ValueError`.

    `is_length_suspicious` raises `ValueError` for a floor of zero, which the
    stage boundary does not classify as a contract refusal -- so a hand-built
    policy carrying zero used to leave this module unnamed while a wrongly
    typed one was refused properly (CodeRabbit on PR #117).
    """
    with pytest.raises(ContractError, match="not a positive integer"):
        truncation.classify(
            "text",
            region_pixels=100,
            page_pixels=1000,
            truncation_policy={truncation.LENGTH_FLOOR_FIELD: floor},
            stop_reason="stop",
        )


def test_an_empty_reading_is_never_length_suspicious():
    """`no-readable-text` is the honest outcome for an empty reading, decided
    elsewhere; this signal must not smuggle a truncation classification onto
    an intentionally empty text."""
    assert length_suspicious("", FIXTURE_PAGE) is False


def test_a_tiny_reading_under_a_whole_page_is_length_suspicious():
    """One character for a whole page is one character per page-equivalent,
    under any floor above one -- at fixture scale and at leaf scale alike."""
    assert length_suspicious("x", FIXTURE_PAGE) is True
    assert length_suspicious("x", LEAF_PAGE, page_pixels=LEAF_PAGE) is True


def test_all_three_computed_signals_agreeing_suspicious_is_truncated_not_unknown():
    """The all-suspicious case the design note names explicitly: a split vote
    holds as `unknown`, but unanimous suspicion is confident enough to call
    `truncated` outright -- even when the engine claims it stopped normally,
    which is the direction that matters. An engine's `stop` never forces
    `complete`; it only permits it."""
    text = "unclosed (mid-"
    record = classify(text, region_pixels=FIXTURE_PAGE, stop_reason="stop")
    assert record["signals"]["unclosed_structure"] is True
    assert record["signals"]["length_suspicious"] is True
    assert record["signals"]["ends_abruptly"] is True
    assert record["classification"] == truncation.TRUNCATED


def test_a_split_vote_among_computed_signals_holds_as_unknown_never_complete():
    text = "unclosed (parenthetical but otherwise a normal length reading here"
    record = classify(text, region_pixels=1000, stop_reason="stop")
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


# --------------------------------------------------------------------------
# Scale. The length signal held every real act until 2026-09-14 (pre-launch
# review, F082): its floor was an absolute 2,000 pixels per character set to
# clear the 200x260 fixture, and a photographed leaf has far more pixels per
# character than a fixture page, not fewer. The tests below are the ones that
# would have caught that: the instrument is exercised at photographed scale,
# and the verdict is pinned invariant across the scales this pipeline meets.


def test_a_complete_line_band_from_a_photographed_leaf_is_complete():
    """The review's own counter-example, run through the shipped instrument.

    One full 300-DPI line band (2,550x104 px) carrying a realistic dense-line
    reading under a clean engine stop is `complete`. Under the retired
    constant it was `unknown` and held: 265,200 / 76 = 3,489 px/char > 2,000.
    """
    record = classify(LINE_TEXT, region_pixels=LINE_BAND, page_pixels=LEAF_PAGE, stop_reason="stop")
    assert record["signals"]["length_suspicious"] is False
    assert record["classification"] == truncation.COMPLETE
    assert truncation.holds_as_failure(record["classification"]) is False


def test_a_complete_act_crop_from_a_photographed_leaf_is_complete():
    """The review's second geometry: a 2,400x420 entry band with 380 characters
    (2,653 px/char, held under the retired constant) is complete now."""
    text = ("Jean Baptiste fils de Pierre et de Marie Anne " * 9)[:380]
    assert len(text) == 380
    record = classify(text, region_pixels=2400 * 420, page_pixels=LEAF_PAGE, stop_reason="stop")
    assert record["classification"] == truncation.COMPLETE


def test_a_reading_that_stopped_after_a_token_on_a_photographed_leaf_is_still_caught():
    """Scale invariance must not mean the signal never fires on real material:
    a tenth-of-a-page crop that produced three characters is suspicious at
    leaf scale exactly as it is at fixture scale."""
    tenth = LEAF_PAGE // 10
    assert length_suspicious("Jea", tenth, page_pixels=LEAF_PAGE) is True
    assert length_suspicious("Jea", FIXTURE_PAGE // 10, page_pixels=FIXTURE_PAGE) is True


@pytest.mark.parametrize("scale", [1, 4, 13, 160])
def test_the_verdict_is_invariant_under_uniform_rescaling(scale):
    """The same crop, the same page, the same text, at 1x, 4x, 13x and 160x the
    fixture's pixel count -- 160x is the ratio of an 8.4-megapixel leaf to the
    52,000-pixel fixture page. Every verdict, suspicious or clean, must be the
    same one at every scale; a fixture-calibrated constant inverts here."""
    for text in (FIXTURE_TEXT, "Jea", ""):
        at_fixture = length_suspicious(text, FIXTURE_REGION, page_pixels=FIXTURE_PAGE)
        at_scale = length_suspicious(text, FIXTURE_REGION * scale, page_pixels=FIXTURE_PAGE * scale)
        assert at_scale is at_fixture, (text, scale)


# --------------------------------------------------------------------------
# Calibration pins, the way `common/test_designator_recensor_ink_calibration.py`
# pins the ink thresholds: the sealed value, what it was reasoned against, and
# the one property the fixture forces on it, so a moved number fails here
# naming the reason rather than somewhere downstream naming a held act.


def test_the_sealed_floor_is_the_value_this_module_was_reasoned_at():
    """50 characters per page-equivalent, sealed in `[truncation]` with
    `calibrated_for_this_corpus = false`. Move the number and this pin together,
    with the provenance block that says why."""
    assert FLOOR == 50
    assert POLICY["provenance"]["calibrated_for_this_corpus"] is False
    assert POLICY["provenance"]["sample_count"] == 0
    assert "BOUNDED ABOVE BY THE FIXTURE" in POLICY["provenance"]["caveat"]


def test_the_fixture_acts_clear_the_floor_with_room_for_a_recrop():
    """The fixture is the upper bound on the floor, and the caveat says so.

    The two fixture acts are 34 and 40 characters over 160x80 and 160x100
    Designator bounds on a 200x260 page -- 138 and 130 characters per
    page-equivalent over those bounds -- but the Perlector measures the padded
    crops `config/designator_padding.toml` cuts, 188x99 and 188x124 px, where
    the same readings sit at 95 and 89. The floor must clear the padded figure
    or every fixture run holds, and it must clear it with room, because a
    fallback recrop enlarges a fixture region without adding a character.
    """
    for text, bounds, padded in (
        (FIXTURE_TEXT, 160 * 80, 188 * 99),
        ("SYNTHETIC ACT TWO delta epsilon zeta eta", 160 * 100, 188 * 124),
    ):
        assert 130 <= len(text) * FIXTURE_PAGE / bounds <= 140
        density = len(text) * FIXTURE_PAGE / padded
        assert 88 <= density <= 96, density
        assert length_suspicious(text, padded) is False
    # The recovered act of the review scenario: its region is the UNION of its
    # crop and its fallback recrop, 22,800 px, not their 41,412 px sum -- the
    # sum held it (see `run._region_pixels`). This is the lowest density the
    # fixture produces and the margin the caveat states: at least 1.5x.
    recovered = len(FIXTURE_TEXT) * FIXTURE_PAGE / 22_800
    assert 76 <= recovered <= 79, recovered
    assert recovered / FLOOR >= 1.5
    assert length_suspicious(FIXTURE_TEXT, 22_800) is False
    assert length_suspicious(FIXTURE_TEXT, 41_412) is True, (
        "the summed area would hold the recovered act; the union is what is measured"
    )


def test_a_real_act_crop_cut_off_after_one_line_is_not_caught_by_the_length_signal():
    """The residual risk this floor leaves, pinned rather than only described.

    The fixture bounds the floor from above, so on real material the length
    signal is weak in one direction: it can no longer hold a complete act by an
    accident of scale (F082), but a 2,400x420 act crop that should carry about
    380 characters and returned its first line's 40 is NOT length-suspicious,
    and under a clean engine stop that act is established `complete`. That is
    the silent loss principle 2 refuses, left standing because no real ink has
    been read through this instrument yet, and it is pinned here so that
    re-deriving the floor against a real run's own Perlectiones must move this
    test rather than pass it quietly. What still catches the same reading is
    pinned beside it (independent audit of 2026-09-14).
    """
    crop = 2400 * 420
    cut_off = "L'an mil sept cent quarante deux le douze de may"[:40]
    assert len(cut_off) == 40
    assert length_suspicious(cut_off, crop, page_pixels=LEAF_PAGE) is False
    assert (
        classify(cut_off, region_pixels=crop, page_pixels=LEAF_PAGE, stop_reason="stop")[
            "classification"
        ]
        == truncation.COMPLETE
    )
    # At this geometry the floor fires only at five characters or fewer, the
    # figure the sealed caveat names.
    assert length_suspicious("x" * 5, crop, page_pixels=LEAF_PAGE) is True
    assert length_suspicious("x" * 6, crop, page_pixels=LEAF_PAGE) is False
    # The three that do catch it, on the same reading and the same geometry.
    assert (
        classify(cut_off, region_pixels=crop, page_pixels=LEAF_PAGE, stop_reason="length")[
            "classification"
        ]
        == truncation.TRUNCATED
    )
    for unclean in (cut_off + "-", cut_off + " (Jean"):
        assert (
            classify(unclean, region_pixels=crop, page_pixels=LEAF_PAGE, stop_reason="stop")[
                "classification"
            ]
            == truncation.UNKNOWN
        )
    assert "RESIDUAL RISK NOW RUNS THE OTHER WAY" in POLICY["provenance"]["caveat"]


def test_the_floor_is_far_below_a_real_pages_density_and_that_is_stated():
    """At 300 DPI a full line band reads at about 2,200 characters per
    page-equivalent, so the floor is a fortieth of a real page's density. That
    makes the signal weak on real material and honest: it cannot hold a
    complete act by an accident of scale. The provenance says which."""
    band_density = len(LINE_TEXT) * LEAF_PAGE / LINE_BAND
    assert band_density > 40 * FLOOR
    assert "weak signal on real material" in POLICY["provenance"]["caveat"]


# --------------------------------------------------------------------------
# The record's closed shape and the sealed table's refusals.


def test_the_shared_validator_refuses_a_record_without_its_measure():
    """A raw record that cannot say what its length signal was judged from is
    not a closed record: `region_pixels` is on the finding now (F082)."""
    from common.contracts.errors import SchemaRefusal
    from common.perlector_audit import validate_truncation_record

    record = classify("alpha beta gamma.", stop_reason="stop")
    assert validate_truncation_record(record, label="x") == record
    without = {"classification": record["classification"], "signals": record["signals"]}
    with pytest.raises(SchemaRefusal, match="not a closed truncation record"):
        validate_truncation_record(without, label="x")
    for bad in ({"region_pixels": 1}, {**record["measure"], "region_pixels": 0}):
        with pytest.raises(SchemaRefusal, match="measure"):
            validate_truncation_record({**record, "measure": bad}, label="x")


def test_the_record_carries_the_floor_it_was_judged_under():
    """principle 6: the record protects the past on its own.

    A consumer holding this block and nothing else -- not the run's
    `config/perlector_protocol.toml` -- has every term of the predicate and can
    say for itself whether the signal follows (independent audit of
    2026-09-14). The Armarium's ink re-measurement row already took this
    standard for its own noise floor; this is the same one applied twice.
    """
    record = classify("alpha beta gamma.", stop_reason="stop")
    measure = record["measure"]
    assert measure["length_floor_characters_per_page"] == FLOOR
    assert (
        measure["characters"] * measure["page_pixels"]
        < measure["length_floor_characters_per_page"] * measure["region_pixels"]
    ) is record["signals"]["length_suspicious"]


def test_the_shared_validator_re_derives_the_length_signal_from_the_measure():
    """The signal is no longer the producer's word: the block proves it.

    A record whose `length_suspicious` disagrees with its own geometry and
    floor is refused, which is what makes the `measure` block load-bearing
    rather than decorative.
    """
    from common.contracts.errors import SchemaRefusal
    from common.perlector_audit import validate_truncation_record

    # One character over a whole page is genuinely suspicious; the same record
    # claiming otherwise is refused, and so is the mirror of it.
    suspicious = classify("x", region_pixels=FIXTURE_PAGE, stop_reason="stop")
    assert suspicious["signals"]["length_suspicious"] is True
    assert validate_truncation_record(suspicious, label="x") == suspicious
    lying = {
        **suspicious,
        "classification": truncation.COMPLETE,
        "signals": {**suspicious["signals"], "length_suspicious": False},
    }
    with pytest.raises(SchemaRefusal, match="make it True"):
        validate_truncation_record(lying, label="x")
    clean = classify(FIXTURE_TEXT, stop_reason="stop")
    assert clean["signals"]["length_suspicious"] is False
    overclaiming = {
        **clean,
        "classification": truncation.UNKNOWN,
        "signals": {**clean["signals"], "length_suspicious": True},
    }
    with pytest.raises(SchemaRefusal, match="make it False"):
        validate_truncation_record(overclaiming, label="x")


def test_the_shared_validator_binds_the_character_count_to_the_text():
    """A re-derivation is only worth the character count it runs on.

    The caller that holds the reading the record was measured over passes it,
    and a record counting some other text is refused rather than validating
    cleanly on its own word (independent audit of 2026-09-14).
    """
    from common.contracts.errors import SchemaRefusal
    from common.perlector_audit import validate_truncation_record

    record = classify(FIXTURE_TEXT, stop_reason="stop")
    assert validate_truncation_record(record, label="x", text=FIXTURE_TEXT) == record
    with pytest.raises(SchemaRefusal, match="characters but the text"):
        validate_truncation_record(record, label="x", text=FIXTURE_TEXT + "!")


def test_the_shared_validator_binds_the_floor_to_the_one_this_run_sealed():
    """A re-derivation is only worth the floor it runs on, either.

    `length_suspicious` is recomputed from the record's own measure, so a
    record that names its own floor agrees with itself whatever that floor is:
    under a sealed floor of 50, a re-proof record naming floor 1 derives the
    signal false, classifies `complete`, and clears an audit hold the sealed
    policy would have held. The caller that holds the sealed table passes it,
    and a record judged under any other floor is refused before the signal is
    derived (CodeRabbit on PR #117).
    """
    from common.contracts.errors import SchemaRefusal
    from common.perlector_audit import validate_truncation_record

    # A short reading over a whole page: suspicious under the sealed floor,
    # clean under a floor of one, which is the forgery this refuses.
    under_the_seal = classify("x", region_pixels=FIXTURE_PAGE, stop_reason="stop")
    assert under_the_seal["signals"]["length_suspicious"] is True
    assert (
        validate_truncation_record(
            under_the_seal, label="x", length_floor_characters_per_page=FLOOR
        )
        == under_the_seal
    )

    forged = truncation.classify(
        "x",
        region_pixels=FIXTURE_PAGE,
        page_pixels=FIXTURE_PAGE,
        truncation_policy={truncation.LENGTH_FLOOR_FIELD: 1},
        stop_reason="stop",
    )
    # Internally consistent, and complete: exactly what makes the floor
    # load-bearing rather than decorative.
    assert forged["signals"]["length_suspicious"] is False
    assert forged["classification"] == truncation.COMPLETE
    assert validate_truncation_record(forged, label="x") == forged
    with pytest.raises(SchemaRefusal, match="judged under length floor 1 but this run sealed"):
        validate_truncation_record(forged, label="x", length_floor_characters_per_page=FLOOR)


def test_a_policy_with_no_floor_at_all_is_refused_by_name():
    """Every other boundary in this module refuses by name; so does this one.

    The sealed path cannot reach it -- `protocol.validate_truncation_table`
    guarantees the key -- but a hand-built policy could, and a bare `KeyError`
    is not a refusal the stage boundary classifies (independent audit of
    2026-09-14).
    """
    with pytest.raises(ContractError, match="declares no length_floor_characters_per_page"):
        truncation.classify(
            "alpha",
            region_pixels=FIXTURE_REGION,
            page_pixels=FIXTURE_PAGE,
            truncation_policy={},
            stop_reason="stop",
        )


def _protocol_with(replacement: str, tmp_path: Path) -> Path:
    shipped = (ROOT / "config" / "perlector_protocol.toml").read_text(encoding="utf-8")
    edited = shipped.replace("length_floor_characters_per_page = 50", replacement)
    assert edited != shipped
    path = tmp_path / "perlector_protocol.toml"
    path.write_text(edited, encoding="utf-8")
    return path


def test_the_sealed_table_refuses_a_floor_that_switches_the_signal_off(tmp_path):
    with pytest.raises(ContractError, match="floor of zero never fires"):
        protocol.load(_protocol_with("length_floor_characters_per_page = 0", tmp_path))


def test_the_sealed_table_refuses_a_calibration_claim_without_a_sample(tmp_path):
    """The same rule every calibrated block in `config/` is held to."""
    shipped = (ROOT / "config" / "perlector_protocol.toml").read_text(encoding="utf-8")
    claimed = shipped.replace(
        "calibrated_for_this_corpus = false", "calibrated_for_this_corpus = true"
    )
    assert claimed != shipped
    path = tmp_path / "perlector_protocol.toml"
    path.write_text(claimed, encoding="utf-8")
    with pytest.raises(ContractError, match="records no sample"):
        protocol.load(path)


def test_a_protocol_without_the_truncation_table_is_refused_rather_than_defaulted(tmp_path):
    shipped = (ROOT / "config" / "perlector_protocol.toml").read_text(encoding="utf-8")
    cut = shipped[: shipped.index("[truncation]")]
    path = tmp_path / "perlector_protocol.toml"
    path.write_text(cut, encoding="utf-8")
    with pytest.raises(ContractError, match="closed schema"):
        protocol.load(path)
