"""The shared signature reader: prefix size and the "could be a page" test."""

from common import image_sniff


def test_the_prefix_covers_everything_sniff_reads():
    assert image_sniff.SIGNATURE_PREFIX_BYTES >= max(
        image_sniff.PDF_HEADER_PREFIX_BYTES, image_sniff._FTYP_BRAND_SCAN_CEILING
    )


def test_a_named_signature_or_a_pillow_readable_header_identifies_as_an_image():
    assert image_sniff.identifies_as_image(b"\x89PNG\r\n\x1a\nbody")
    assert image_sniff.identifies_as_image(b"P6\n2 2\n255\n" + bytes(12))


def test_text_and_empty_bytes_do_not():
    assert not image_sniff.identifies_as_image(b"just some words")
    assert not image_sniff.identifies_as_image(b"")
