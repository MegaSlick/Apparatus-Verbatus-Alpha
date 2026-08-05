"""The Exemplar's byte-led routes and its closed alarm vocabulary.

Spec 03's admission-by-bytes checks live here: a reader route never decides from an
extension, PDF and TIFF containers are sent to page fan-out, and a reader gap is an
alarm rather than a policy refusal.  The all-refused-folder check needs a run tree
and remains in `test_door.py`.
"""

from io import BytesIO

import image_formats
import pytest
from admission import (
    ADMIT_OR_FAN_OUT,
    FORMAT_ROUTES,
    RENDER_PAGES,
    SNIFFABLE_FORMATS,
    AdmissionOutcome,
    RefusalReason,
    _refusal_code,
    classify_detected_format,
    inspect_source,
    load_format_policy,
    reason,
    reason_code,
)
from image_formats import MAX_DIMENSION, MAX_SOURCE_BYTES, FormatRefusal, FormatVerdict
from PIL import Image
from synthetic_sources import (
    heic,
    png,
    single_gray_page_pdf,
    tiff,
)

from common.contracts.canonical import digest_bytes
from common.contracts.errors import ContractError

POLICY = load_format_policy()


# --- Decoder routes are code-owned and exhaustive --------------------------------


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


def _synthetic_decoder_image(format_name: str) -> bytes:
    """Make one small ordinary image with the installed encoder under test.

    These are deliberately encoder-produced bytes, not hand-written signatures: a
    route that recognizes a WebP/AVIF/BMP header but cannot actually decode the
    file must fail this test.  There is no skip here.  Losing one of the installed
    codecs is an admission regression, not a passing unsupported environment.
    """
    output = BytesIO()
    Image.new("RGB", (7, 5), (17, 34, 51)).save(output, format=format_name)
    return output.getvalue()


def test_the_shipped_decoder_routes_name_readers_not_formats_to_refuse():
    """The route table is exhaustive, but contains no rejection action.

    PDF is the one format that is always a document of pages.  Every raster type,
    TIFF included, is admitted as its own bytes or fanned out according to the frame
    count its decoder reports; a gap there is a named pipeline alarm rather than a
    decision to exclude a file format.
    """
    assert POLICY["pdf"] == RENDER_PAGES
    assert all(action == ADMIT_OR_FAN_OUT for name, action in POLICY.items() if name != "pdf"), (
        POLICY
    )
    assert set(POLICY.values()) <= {ADMIT_OR_FAN_OUT, RENDER_PAGES}


def test_the_decoder_routes_cover_exactly_the_formats_the_door_can_detect():
    """The map is derived from the sniffer, so no format can route by omission."""
    assert set(POLICY) == SNIFFABLE_FORMATS
    assert POLICY == FORMAT_ROUTES


def test_a_reader_route_does_not_require_a_bespoke_structural_walker():
    """A reader gap is an alarm at byte admission, never a load-time format ban.

    Every sniffable raster format loads without this module owning a hand-written
    validator for it: HEIC and WebP have no bespoke structural walker here and are
    still routed to a decoder attempt rather than banned when the routing is read.
    """
    loaded = load_format_policy()
    assert loaded["pdf"] == RENDER_PAGES
    assert {name: action for name, action in loaded.items() if name != "pdf"} == {
        name: ADMIT_OR_FAN_OUT for name in sorted(SNIFFABLE_FORMATS - {"pdf"})
    }


def test_an_unknown_magic_or_handbuilt_missing_route_gets_a_generic_raster_attempt():
    """Neither case may reintroduce a format-policy refusal path."""
    partial_policy = {name: action for name, action in POLICY.items() if name != "webp"}
    assert classify_detected_format("webp", partial_policy) == ADMIT_OR_FAN_OUT
    assert classify_detected_format(None, POLICY) == ADMIT_OR_FAN_OUT


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


def test_pdf_is_a_page_container_not_a_single_image_refusal():
    """The door owns PDF's page count and one-time rendering, not `inspect_source`."""
    assert classify_detected_format("pdf", POLICY) == RENDER_PAGES
    with pytest.raises(ValueError, match="page container"):
        inspect_source(single_gray_page_pdf(), declared_sha256=None, policy=POLICY)


def test_a_single_page_tiff_is_admitted_as_one_image_and_never_re_encoded():
    """The common TIFF is one image, and its own bytes are what gets sealed.

    Lane B's finding, kept: a PDF is *always* a container, but a TIFF usually is
    not, and nothing in Tyrel's ruling asks for an ordinary flatbed scan to be
    decoded and re-encoded on its way in. `inspect_source` decides it here, which
    is what makes the sealed bytes the submitted bytes.
    """
    data = tiff(4, 5)
    assert classify_detected_format("tiff", POLICY) == ADMIT_OR_FAN_OUT
    outcome = inspect_source(data, declared_sha256=None, policy=POLICY)
    assert outcome == AdmissionOutcome("admitted", None, "tiff", digest_bytes(data), (4, 5))


@pytest.mark.parametrize(
    ("encoder", "sniffed", "decoded"),
    [
        ("BMP", "bmp", "bmp"),
        ("WEBP", "webp", "webp"),
        ("AVIF", "avif", "avif"),
        # PPM has no bespoke signature branch.  This is the generic Pillow
        # fallback that must remain a real decoder attempt rather than a route
        # silently exercised only by a hand-built policy assertion.
        ("PPM", None, "ppm"),
    ],
)
def test_installed_bmp_webp_avif_and_generic_fallback_decoders_admit_real_synthetic_bytes(
    encoder, sniffed, decoded
):
    """Every installed decoder route has an actual admission proof.

    Sol finding 20 found only signature/route coverage for these paths.  The
    generated bytes pass through `inspect_source`, so this proves both the routing
    decision and `Image.load()` rather than merely that the test knows a magic
    prefix.
    """
    data = _synthetic_decoder_image(encoder)

    assert image_formats.sniff(data) == sniffed
    assert inspect_source(data, declared_sha256=None, policy=POLICY) == AdmissionOutcome(
        "admitted", None, decoded, digest_bytes(data), (7, 5)
    )


# --- Test 2: the refusal vocabulary is closed, and every member is exercised ------


def _oversized_png() -> bytes:
    """A genuine PNG container declaring a geometry past the admission limits.

    Structurally sound and honestly what it says it is — it is simply larger than
    this door will inspect, which is a documented limit rather than damage.
    """
    return png(MAX_DIMENSION + 1, 2)


def _exercised() -> dict[RefusalReason, str]:
    """One real refusal per closed-set member, produced by the module under test.

    `UNREADABLE` is the one the door owns rather than this module: a read failure
    is a fact about a *submission*, not about one file's bytes. A duplicate is an
    admitted source fact, deliberately not a refusal-vocabulary member.
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


def test_format_alarm_severity_is_typed_not_inferred_from_its_message():
    alarm = FormatRefusal(
        FormatVerdict.UNSUPPORTED,
        "corrupt is deliberately the first word of this presentation detail",
    )
    alarm.args = ("corrupt presentation text changed independently of its type",)

    assert image_formats.FormatVerdict.UNSUPPORTED is alarm.verdict
    assert _refusal_code(alarm) is RefusalReason.UNSUPPORTED_VARIANT


# --- Installed readers distinguish valid images from damaged containers ----------


def test_an_iphone_native_heic_is_decoded_and_admitted():
    output = BytesIO()
    Image.new("RGB", (3, 2), (17, 34, 51)).save(output, format="HEIF", lossless=True)
    data = output.getvalue()

    outcome = inspect_source(data, declared_sha256=None, policy=POLICY)

    assert outcome == AdmissionOutcome("admitted", None, "heif", digest_bytes(data), (3, 2))
    assert image_formats.sniff(data) == "heic"
    assert image_formats.has_reader("heic")


def test_a_truncated_heic_file_type_box_is_typed_as_corruption():
    data = (28).to_bytes(4, "big") + heic()[4:]
    outcome = inspect_source(data, declared_sha256=None, policy=POLICY)

    assert outcome.outcome == "refused"
    assert reason_code(outcome.reason) is RefusalReason.CORRUPT
    assert "file-type box" in outcome.reason


def test_a_format_with_a_reader_that_still_fails_is_worded_about_the_bytes():
    """The other half of the same sentence, and it is a different fact.

    A GIF reader is installed, so a GIF that will not open says something about
    these bytes rather than about a reader this project owes. Collapsing the two —
    which is what happens when the wording keys on the format name alone — would
    file a real decoding failure under work we have not done.
    """
    # This one-byte change stays inside a complete GIF block sequence, so the
    # framing validator rightly leaves actual LZW decoding to Pillow. It gives this
    # test a real decoder-side failure rather than the older incomplete fake GIF,
    # which now correctly fails the structural completeness check first.
    data = bytearray(_one_pixel_gif())
    data[33] ^= 1
    outcome = inspect_source(bytes(data), declared_sha256=None, policy=POLICY)

    assert outcome.outcome == "refused"
    assert reason_code(outcome.reason) is RefusalReason.CORRUPT
    assert "installed decoder could not decode" in outcome.reason
    assert "has no reader" not in outcome.reason
    assert image_formats.has_reader("gif")
