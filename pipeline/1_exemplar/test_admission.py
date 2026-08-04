"""The Exemplar's byte-led routes and its closed alarm vocabulary.

Spec 03's admission-by-bytes checks live here: a reader route never decides from an
extension, PDF and TIFF containers are sent to page fan-out, and a reader gap is an
alarm rather than a policy refusal.  The all-refused-folder check needs a run tree
and remains in `test_door.py`.
"""

import tomllib
from io import BytesIO

import pytest
from admission import (
    RASTER,
    RENDER_PAGES,
    SNIFFABLE_FORMATS,
    AdmissionOutcome,
    FormatPolicyRefusal,
    RefusalReason,
    classify_detected_format,
    duplicate_reason,
    inspect_source,
    load_format_policy,
    reason,
    reason_code,
)
from image_formats import MAX_DIMENSION, MAX_SOURCE_BYTES
from PIL import Image
from synthetic_sources import (
    png,
    single_gray_page_pdf,
    tiff,
)

from common.contracts.canonical import digest_bytes
from common.contracts.errors import ContractError

POLICY = load_format_policy()


# --- Decoder routes are configuration, and are checked ----------------------------


def _one_pixel_gif() -> bytes:
    """A complete synthetic GIF89a image for the ordinary-reader route."""
    return bytes.fromhex(
        "47494638396101000100800000000000ffffff21f90401000000002c00000000010001000002024401003b"
    )


def _decoder_jpeg(width: int = 5, height: int = 4, *, trailing: bytes = b"") -> bytes:
    """A pixel-decodable synthetic JPEG, optionally with scanner-style suffix bytes."""
    output = BytesIO()
    Image.new("RGB", (width, height), (17, 34, 51)).save(output, format="JPEG")
    return output.getvalue() + trailing


def test_the_shipped_decoder_routes_name_readers_not_formats_to_refuse():
    """The route table is exhaustive, but contains no rejection action.

    TIFF and PDF are containers whose pages must be fanned out.  The remaining
    currently-sniffed types make one raster-decoder attempt; a gap there is a named
    pipeline alarm rather than a decision to exclude a file format.
    """
    assert POLICY["png"] == POLICY["jpeg"] == POLICY["gif"] == RASTER
    assert POLICY["heic"] == POLICY["bmp"] == POLICY["webp"] == RASTER
    assert POLICY["tiff"] == POLICY["pdf"] == RENDER_PAGES
    assert set(POLICY.values()) <= {RASTER, RENDER_PAGES}


def test_the_decoder_routes_cover_exactly_the_formats_the_door_can_detect(tmp_path):
    """A missing route would make a fan-out decision by omission."""
    assert set(POLICY) == SNIFFABLE_FORMATS
    short = tmp_path / "short.toml"
    short.write_text('[format]\npng = "raster"\n', encoding="utf-8")
    with pytest.raises(FormatPolicyRefusal, match="Missing"):
        load_format_policy(short)


def test_the_decoder_routes_reject_an_unknown_row_name(tmp_path):
    rows = "\n".join(f'{name} = "raster"' for name in sorted(SNIFFABLE_FORMATS))
    path = tmp_path / "extra.toml"
    path.write_text(f'[format]\n{rows}\navif = "raster"\n', encoding="utf-8")
    with pytest.raises(FormatPolicyRefusal, match="Unknown"):
        load_format_policy(path)


def test_the_decoder_routes_reject_an_action_outside_their_closed_set(tmp_path):
    rows = "\n".join(
        f'{name} = "{"maybe" if name == "png" else "raster"}"' for name in sorted(SNIFFABLE_FORMATS)
    )
    path = tmp_path / "bad-action.toml"
    path.write_text(f"[format]\n{rows}\n", encoding="utf-8")
    with pytest.raises(FormatPolicyRefusal, match="not one of"):
        load_format_policy(path)


def test_a_reader_route_does_not_require_a_bespoke_structural_walker(tmp_path):
    """A reader gap is an alarm at byte admission, never a load-time format ban."""
    rows = "\n".join(f'{name} = "raster"' for name in sorted(SNIFFABLE_FORMATS))
    path = tmp_path / "all-raster.toml"
    path.write_text(f"[format]\n{rows}\n", encoding="utf-8")
    assert load_format_policy(path) == {name: RASTER for name in sorted(SNIFFABLE_FORMATS)}


def test_an_unreadable_admission_list_is_a_failed_check_not_an_empty_one(tmp_path):
    missing = tmp_path / "nothing-here.toml"
    with pytest.raises(FormatPolicyRefusal, match="could not be read"):
        load_format_policy(missing)


def test_the_shipped_decoder_routes_parse_as_the_one_table_they_claim_to_be():
    with open("config/admitted_formats.toml", "rb") as handle:
        assert set(tomllib.load(handle)) == {"format"}


def test_an_unknown_magic_or_handbuilt_missing_route_gets_a_generic_raster_attempt():
    """Neither case may reintroduce a format-policy refusal path."""
    partial_policy = {name: action for name, action in POLICY.items() if name != "webp"}
    assert classify_detected_format("webp", partial_policy) == RASTER
    assert classify_detected_format(None, POLICY) == RASTER


# --- Test 1: admission by bytes, never by extension ------------------------------


@pytest.mark.parametrize(
    ("data", "detected", "geometry"),
    [
        (png(2, 3), "png", (2, 3)),
        (_decoder_jpeg(3, 2), "jpeg", (3, 2)),
        (_one_pixel_gif(), "gif", (1, 1)),
    ],
)
def test_correct_bytes_are_admitted_and_their_true_geometry_is_read(data, detected, geometry):
    outcome = inspect_source(data, declared_sha256=None, policy=POLICY)
    assert outcome == AdmissionOutcome("admitted", None, detected, digest_bytes(data), geometry)


def test_the_declared_name_plays_no_part_at_all():
    """A `.txt` of a genuine JPEG and a `.png` of garbage decide identically to the
    same bytes under any other name, because no name is ever passed in. There is no
    parameter here to mislead — which is the point, and this is what would notice a
    suffix creeping back in as a "harmless" extra refusal."""
    assert (
        inspect_source(_decoder_jpeg(), declared_sha256=None, policy=POLICY).outcome == "admitted"
    )
    outcome = inspect_source(b"not an image", declared_sha256=None, policy=POLICY)
    assert outcome.outcome == "refused"
    assert reason_code(outcome.reason) is RefusalReason.UNRECOGNIZED_FORMAT


def test_a_declared_digest_that_does_not_match_the_bytes_is_refused_and_named():
    outcome = inspect_source(png(), declared_sha256="0" * 64, policy=POLICY)
    assert reason_code(outcome.reason) is RefusalReason.DIGEST_MISMATCH
    assert outcome.digest == digest_bytes(png())


def test_a_transfer_changed_raster_is_a_digest_alarm_before_the_decoder_reads_it():
    original = png()
    changed = bytearray(original)
    changed[changed.find(b"IDAT") + 5] ^= 0xFF

    outcome = inspect_source(bytes(changed), declared_sha256=digest_bytes(original), policy=POLICY)
    assert reason_code(outcome.reason) is RefusalReason.DIGEST_MISMATCH


def test_scanner_style_jpeg_suffix_bytes_are_admitted():
    """EOI closes the image; harmless bytes after it are not a corruption alarm."""
    data = _decoder_jpeg(trailing=b"scanner-metadata-after-eoi")
    outcome = inspect_source(data, declared_sha256=None, policy=POLICY)
    assert outcome == AdmissionOutcome("admitted", None, "jpeg", digest_bytes(data), (5, 4))


@pytest.mark.parametrize(
    ("data", "detected"),
    [(single_gray_page_pdf(), "pdf"), (tiff(4, 5), "tiff")],
)
def test_pdf_and_tiff_are_page_containers_not_single_image_refusals(data, detected):
    """The door owns their page count and one-time rendering, not `inspect_source`."""
    assert classify_detected_format(detected, POLICY) == RENDER_PAGES
    with pytest.raises(ValueError, match="page container"):
        inspect_source(data, declared_sha256=None, policy=POLICY)


# --- Test 2: the refusal vocabulary is closed, and every member is exercised ------


def _oversized_png() -> bytes:
    """A genuine PNG container declaring a geometry past the admission limits.

    Structurally sound and honestly what it says it is — it is simply larger than
    this door will inspect, which is a documented limit rather than damage.
    """
    return png(MAX_DIMENSION + 1, 2)


def _exercised() -> dict[RefusalReason, str]:
    """One real refusal per closed-set member, produced by the module under test.

    `UNREADABLE` and `DUPLICATE` are the two the door owns rather than this module:
    a read failure and a second copy of an already-admitted source are both facts
    about a *submission*, not about one file's bytes. They are produced here through
    the same one spelling and proven end to end in `test_door.py`.
    """
    return {
        RefusalReason.EMPTY: inspect_source(b"", declared_sha256=None, policy=POLICY).reason,
        RefusalReason.TOO_LARGE: inspect_source(
            b"\x89PNG\r\n\x1a\n" + b"\x00" * MAX_SOURCE_BYTES, declared_sha256=None, policy=POLICY
        ).reason,
        RefusalReason.UNRECOGNIZED_FORMAT: inspect_source(
            b"plain text", declared_sha256=None, policy=POLICY
        ).reason,
        RefusalReason.CORRUPT: inspect_source(
            png()[:-4], declared_sha256=None, policy=POLICY
        ).reason,
        RefusalReason.UNSUPPORTED_VARIANT: inspect_source(
            _oversized_png(), declared_sha256=None, policy=POLICY
        ).reason,
        RefusalReason.DIGEST_MISMATCH: inspect_source(
            png(), declared_sha256="0" * 64, policy=POLICY
        ).reason,
        RefusalReason.DUPLICATE: duplicate_reason(3),
        RefusalReason.UNREADABLE: reason(RefusalReason.UNREADABLE, "no such file"),
    }


def test_every_refusal_reason_in_the_closed_set_is_exercised():
    """An unused member would be an untested refusal path, which is exactly the gap
    invariant #3 exists to close. Asserted rather than trusted."""
    exercised = _exercised()
    assert set(exercised) == set(RefusalReason)
    for code, text in exercised.items():
        assert reason_code(text) is code, f"{code} produced {text!r}"


def test_the_two_refusal_voices_stay_apart():
    """Damaged bytes and an honest variant this door cannot read are different facts
    and different decisions for Tyrel. Collapsing them would tell him a photograph
    was corrupt when the truth is that we cannot read that flavour of it yet."""
    corrupt = inspect_source(png()[:-4], declared_sha256=None, policy=POLICY)
    unsupported = inspect_source(_oversized_png(), declared_sha256=None, policy=POLICY)
    assert reason_code(corrupt.reason) is RefusalReason.CORRUPT
    assert reason_code(unsupported.reason) is RefusalReason.UNSUPPORTED_VARIANT


def test_the_closed_alarm_vocabulary_has_no_format_policy_member():
    """A decoder gap remains visible work; it cannot be rebranded as a format ban."""
    assert "refused-format" not in {member.value for member in RefusalReason}
    with pytest.raises(ContractError):
        reason_code("refused-format: the route table excluded this file")


def test_a_reason_outside_the_closed_set_is_refused_when_it_is_read_back():
    """The skeleton's free-text reasons are what this spec replaced. A consumer that
    accepted one because it happened to be a string would have replaced nothing."""
    for text in ("page.png does not carry a PNG signature", "", None, 42, "invented: detail"):
        with pytest.raises(ContractError):
            reason_code(text)
