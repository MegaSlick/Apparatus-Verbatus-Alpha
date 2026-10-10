"""The truncation detector's signals, classification, and the mandatory record.

ARCHITECTURE: "It reads through to the end -- truncation is a failure, not an
output." The detector's signals are declared, each response is classified
`complete | truncated | unknown`, and `unknown` holds -- never passed as
complete.
"""

from pathlib import Path

import protocol
import pytest

from common import truncation
from common.contracts.errors import ContractError

ROOT = Path(__file__).resolve().parents[2]
# The sealed `[truncation]` table every verdict below is judged under, read
# the way the stage reads it. `POLICY` is the whole table; the floor is what the
# calibration pins at the end of this module speak about by name.
POLICY = protocol.load(ROOT / "config" / "perlector_protocol.toml")[0][protocol.TRUNCATION_TABLE]
FLOOR = POLICY[truncation.LENGTH_FLOOR_FIELD]
GATE = POLICY[truncation.LEGIBLE_PAGE_FIELD]

# The synthetic fixture's geometry (proof/synthetic_pages.py, build_fixture.py):
# a 200x260 page and a 160x80 region. The unit tests that are not about scale
# run at this geometry.
FIXTURE_PAGE = 200 * 260
FIXTURE_REGION = 160 * 80

# A photographed 300-DPI letter leaf and a single line band cut from it:
# 2,550x104 is one line at a 104 px pitch, doubled from the 52 px at 150 DPI
# `config/pdf_render.toml` records measuring -- that file states no 300-DPI
# pitch of its own.
LEAF_PAGE = 2550 * 3300
LINE_BAND = 2550 * 104
# A reading long enough to be clean over a tenth of a leaf.
LEAF_CLEAN_TEXT = "Jean Baptiste fils de Pierre et de Marie Anne " * 20
# A realistic character count for one dense register line.
LINE_TEXT = "L'an mil sept cent quarante deux le douze de may a este baptise Jean fils de"
assert 60 <= len(LINE_TEXT) <= 90


def classify(
    text,
    *,
    region_pixels=FIXTURE_REGION,
    page_pixels=FIXTURE_PAGE,
    stop_reason=None,
):
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
        "length_suspicious": None,
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
        "legible_page_pixels": GATE,
        "length_judged": False,
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


def test_an_empty_reading_is_never_length_suspicious():
    """`no-readable-text` is the honest outcome for an empty reading, decided
    elsewhere; this signal must not smuggle a truncation classification onto
    an intentionally empty text."""
    assert length_suspicious("", FIXTURE_PAGE) is False


def test_a_tiny_reading_under_a_whole_page_is_length_suspicious():
    """One character for a whole leaf is one character per page-equivalent,
    under any floor above one."""
    assert length_suspicious("x", LEAF_PAGE, page_pixels=LEAF_PAGE) is True


def test_a_page_too_small_to_hold_legible_lines_is_recorded_as_not_judged():
    """The fixture's 200x260 page cannot carry the 32 lines the floor's density
    describes, so a short reading there is not short of anything: the length is
    not judged, and the record says so rather than calling it clean."""
    record = classify("x", stop_reason="stop")
    assert record["signals"]["length_suspicious"] is None
    assert record["measure"]["length_judged"] is False
    assert record["classification"] == truncation.COMPLETE


def test_a_not_judged_length_votes_neither_way():
    """With the length unjudged the verdict comes from the other two signals: both
    suspicious is truncated, one is unknown, and neither is diluted by the length."""
    both = classify("unclosed (mid-", stop_reason="stop")
    assert both["signals"]["length_suspicious"] is None
    assert both["classification"] == truncation.TRUNCATED
    one = classify("cut off mid-", stop_reason="stop")
    assert one["classification"] == truncation.UNKNOWN


def test_the_gate_is_judged_on_the_page_the_reading_is_of():
    small = classify("x", region_pixels=GATE - 1, page_pixels=GATE - 1, stop_reason="stop")
    assert small["measure"]["length_judged"] is False
    assert small["measure"]["page_pixels"] == GATE - 1
    assert small["signals"]["length_suspicious"] is None
    at_gate = classify("x", region_pixels=GATE, page_pixels=GATE, stop_reason="stop")
    assert at_gate["measure"]["length_judged"] is True
    assert at_gate["signals"]["length_suspicious"] is True


def test_the_shipped_gate_judges_a_72_dpi_letter_page_and_not_the_fixture():
    """Raising the gate past a 72-DPI letter PDF would silence the signal on real
    scans; it must stay under 612x792 and over the 200x260 fixture page."""
    assert FIXTURE_PAGE < GATE <= 612 * 792
    letter = classify("x", region_pixels=612 * 792, page_pixels=612 * 792, stop_reason="stop")
    assert letter["measure"]["length_judged"] is True


def test_an_honest_act_at_the_calibrated_median_low_tail_is_not_flagged():
    """The 0.5th percentile of honest RecordGold acts over their bare boxes reads
    1,146 characters per page-equivalent; a floor near it flags honest acts. 720,
    well under it, is not flagged."""
    region = LEAF_PAGE // 10
    at_the_tail = "a" * 72
    assert 72 * LEAF_PAGE // region == 720
    assert length_suspicious(at_the_tail, region, page_pixels=LEAF_PAGE) is False


def test_all_three_computed_signals_agreeing_suspicious_is_truncated_not_unknown():
    """The all-suspicious case: a split vote
    holds as `unknown`, but unanimous suspicion is confident enough to call
    `truncated` outright -- even when the engine claims it stopped normally,
    which is the direction that matters. An engine's `stop` never forces
    `complete`; it only permits it."""
    text = "unclosed (mid-"
    record = classify(text, region_pixels=LEAF_PAGE, page_pixels=LEAF_PAGE, stop_reason="stop")
    assert record["signals"]["unclosed_structure"] is True
    assert record["signals"]["length_suspicious"] is True
    assert record["signals"]["ends_abruptly"] is True
    assert record["classification"] == truncation.TRUNCATED


def test_a_split_vote_among_computed_signals_holds_as_unknown_never_complete():
    text = "unclosed (parenthetical but otherwise a normal length reading here"
    record = classify(text, region_pixels=1000, stop_reason="stop")
    votes = sum(
        record["signals"][name] is True
        for name in ("unclosed_structure", "length_suspicious", "ends_abruptly")
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
# Scale. The length signal must ask the same question at every scale: a
# photographed leaf has far more pixels per character than a fixture page, so an
# absolute pixels-per-character ratio would hold every ordinary act. The tests
# below exercise the instrument at photographed scale and pin the verdict
# invariant across the scales this pipeline meets.


def test_a_complete_line_band_from_a_photographed_leaf_is_complete():
    """One full 300-DPI line band (2,550x104 px) carrying a realistic dense-line
    reading under a clean engine stop is `complete`."""
    record = classify(LINE_TEXT, region_pixels=LINE_BAND, page_pixels=LEAF_PAGE, stop_reason="stop")
    assert record["signals"]["length_suspicious"] is False
    assert record["classification"] == truncation.COMPLETE
    assert truncation.holds_as_failure(record["classification"]) is False


def test_a_complete_act_crop_from_a_photographed_leaf_is_complete():
    """A 2,400x420 entry band with 380 characters (2,653 px/char) is complete."""
    text = ("Jean Baptiste fils de Pierre et de Marie Anne " * 9)[:380]
    assert len(text) == 380
    record = classify(text, region_pixels=2400 * 420, page_pixels=LEAF_PAGE, stop_reason="stop")
    assert record["classification"] == truncation.COMPLETE


def test_a_reading_that_stopped_after_a_token_on_a_photographed_leaf_is_still_caught():
    """Scale invariance must not mean the signal never fires on real material:
    a tenth-of-a-page crop that produced three characters is suspicious."""
    tenth = LEAF_PAGE // 10
    assert length_suspicious("Jea", tenth, page_pixels=LEAF_PAGE) is True


@pytest.mark.parametrize("scale", [1, 4, 13])
def test_the_verdict_is_invariant_under_uniform_rescaling(scale):
    """The same crop, the same page, the same text, at 1x, 4x and 13x the pixel
    count of a 0.83-megapixel leaf (13x is a 10.8-megapixel photograph). Every
    verdict, suspicious or clean, must be the same one at every scale; an
    absolute pixels-per-character constant inverts here."""
    base_page = 800 * 1040
    assert base_page >= GATE
    base_region = base_page // 8
    for text in (LEAF_CLEAN_TEXT, "Jea", ""):
        at_base = length_suspicious(text, base_region, page_pixels=base_page)
        at_scale = length_suspicious(text, base_region * scale, page_pixels=base_page * scale)
        assert at_scale is at_base, (text, scale)


ACT_CROP = 2400 * 420  # four lines at the 104 px pitch


def test_a_real_act_crop_cut_off_after_one_line_must_not_be_complete():
    cut_off = "L'an mil sept cent quarante deux le douze de may"[:40]
    record = classify(cut_off, region_pixels=ACT_CROP, page_pixels=LEAF_PAGE, stop_reason="stop")
    assert record["signals"]["length_suspicious"] is True
    assert record["classification"] == truncation.UNKNOWN
    assert truncation.holds_as_failure(record["classification"]) is True


def test_half_a_line_cut_from_a_four_line_crop_is_caught_too():
    """The floor catches a reading of a line or less; a whole 76-character line of
    the same crop reads 634 and is not caught (the floor's caveat says why)."""
    record = classify(
        LINE_TEXT[:30], region_pixels=ACT_CROP, page_pixels=LEAF_PAGE, stop_reason="stop"
    )
    assert record["classification"] != truncation.COMPLETE


@pytest.mark.parametrize(
    ("text", "region"),
    [
        ("Bapt. de Jean", 900 * 104),  # a marginal note, its region cut to fit
        ("Le 3 mars, enterré Marie Roy, âgée de 60 ans, en présence de son fils.", 2550 * 104),
        (LINE_TEXT + " " + LINE_TEXT[:50], 2550 * 208),  # a two-line burial in a two-line region
        (LINE_TEXT, 2550 * 104 * 3),  # one line in a region holding three
    ],
)
def test_a_short_complete_act_in_a_region_that_fits_it_is_not_flagged(text, region):
    record = classify(text, region_pixels=region, page_pixels=LEAF_PAGE, stop_reason="stop")
    assert record["signals"]["length_suspicious"] is False
    assert record["classification"] == truncation.COMPLETE


# --------------------------------------------------------------------------
# The record's closed shape and the sealed table's refusals.


def test_the_record_carries_the_floor_it_was_judged_under():
    """The record protects the past on its own.

    A consumer holding this block and nothing else -- not the run's
    `config/perlector_protocol.toml` -- has every term of the predicate and can
    say for itself whether the signal follows. The Armarium's ink re-measurement row already took this
    standard for its own noise floor; this is the same one applied twice.
    """
    record = classify(
        "alpha beta gamma.",
        region_pixels=LEAF_PAGE // 10,
        page_pixels=LEAF_PAGE,
        stop_reason="stop",
    )
    measure = record["measure"]
    assert measure["length_floor_characters_per_page"] == FLOOR
    assert record["signals"]["length_suspicious"] is True
    assert (
        measure["characters"] * measure["page_pixels"]
        < measure["length_floor_characters_per_page"] * measure["region_pixels"]
    ) is record["signals"]["length_suspicious"]


def _protocol_with(replacement: str, tmp_path: Path) -> Path:
    shipped = (ROOT / "config" / "perlector_protocol.toml").read_text(encoding="utf-8")
    edited = shipped.replace("length_floor_characters_per_page = 400", replacement)
    assert edited != shipped
    path = tmp_path / "perlector_protocol.toml"
    path.write_text(edited, encoding="utf-8")
    return path


@pytest.mark.parametrize("floor", ["0", "-1", "true", "50.0", '"50"'])
def test_the_sealed_table_refuses_a_floor_that_is_not_a_positive_integer(tmp_path, floor):
    """A floor of zero never fires: the signal switched off by a value, not a decision."""
    with pytest.raises(ContractError, match="floor of zero never fires"):
        protocol.load(_protocol_with(f"length_floor_characters_per_page = {floor}", tmp_path))


def test_the_sealed_table_refuses_a_gate_that_judges_every_page(tmp_path):
    shipped = (ROOT / "config" / "perlector_protocol.toml").read_text(encoding="utf-8")
    edited = shipped.replace("legible_page_pixels = 100000", "legible_page_pixels = 0")
    assert edited != shipped
    path = tmp_path / "perlector_protocol.toml"
    path.write_text(edited, encoding="utf-8")
    with pytest.raises(ContractError, match="legible_page_pixels is not a positive integer"):
        protocol.load(path)


def test_the_sealed_table_refuses_a_calibration_claim_without_a_sample(tmp_path):
    """The same rule every calibrated block in `config/` is held to."""
    shipped = (ROOT / "config" / "perlector_protocol.toml").read_text(encoding="utf-8")
    claimed = shipped.replace(
        "calibrated_for_this_corpus = false", "calibrated_for_this_corpus = true"
    ).replace("sample_count = 4572", "sample_count = 0")
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
