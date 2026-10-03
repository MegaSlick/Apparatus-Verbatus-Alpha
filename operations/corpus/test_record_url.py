"""Pure tests for `operations/corpus/record_url.py`."""

import pytest

from operations.corpus import CorpusRefusal
from operations.corpus.record_url import SUPPORTED_ROTATIONS, parse_record_url

ONE_PAGE_URL = (
    "https://europe.iiif.teklia.com/iiif/2/geneanet%2FArdennes_BMS%2F380403%2F00026.jpg/"
    "239,208,1232,443/full/0/default.jpg"
)


# --- parse_record_url: the happy path ------------------------------------------


def test_parse_record_url_decodes_identifier_and_region():
    parsed = parse_record_url(ONE_PAGE_URL)
    assert parsed.identifier == "geneanet/Ardennes_BMS/380403/00026.jpg"
    assert parsed.identifier_encoded == "geneanet%2FArdennes_BMS%2F380403%2F00026.jpg"
    assert parsed.host == "europe.iiif.teklia.com"
    assert parsed.region == {"x": 239, "y": 208, "w": 1232, "h": 443}


def test_parse_record_url_preserves_a_literal_plus_verbatim():
    # Measured: quote(unquote(x)) != x for identifiers carrying a literal "+".
    # The encoded form must be threaded through unchanged, never re-escaped.
    url = (
        "https://europe.iiif.teklia.com/iiif/2/"
        "dai-cretdhi%2FIle_de_re_registres_AD17%2Fimg+LaCouarde%2Ffoo.jpg/"
        "1,1,10,10/full/0/default.jpg"
    )
    parsed = parse_record_url(url)
    assert (
        parsed.identifier_encoded
        == "dai-cretdhi%2FIle_de_re_registres_AD17%2Fimg+LaCouarde%2Ffoo.jpg"
    )
    assert parsed.identifier == "dai-cretdhi/Ile_de_re_registres_AD17/img+LaCouarde/foo.jpg"


# --- parse_record_url: every refusal fires by name -----------------------------


@pytest.mark.parametrize(
    "url,reason",
    [
        ("not a url at all", "unparseable-record-url"),
        (
            "https://evil.example.com/iiif/2/geneanet%2Fx%2Fy%2Fz.jpg/1,1,10,10/full/0/default.jpg",
            "unexpected-host",
        ),
        (
            "https://europe.iiif.teklia.com/iiif/2/geneanet%2Fx%2Fy%2Fz.jpg/1,1,10,10/max/0/default.jpg",
            "unsupported-size-parameter",
        ),
        (
            "https://europe.iiif.teklia.com/iiif/2/geneanet%2Fx%2Fy%2Fz.jpg/1,1,10,10/full/180/default.jpg",
            "unsupported-rotation-parameter",
        ),
        (
            "https://europe.iiif.teklia.com/iiif/2/geneanet%2Fx%2Fy%2Fz.jpg/1,1,10,10/full/0/gray.jpg",
            "unsupported-quality-parameter",
        ),
        (
            "https://europe.iiif.teklia.com/iiif/2/geneanet%2Fx%2Fy%2Fz.jpg/1,1,10,10/full/0/default.png",
            "unsupported-format-parameter",
        ),
        (
            "https://europe.iiif.teklia.com/iiif/2/geneanet%2Fx%2Fy%2Fz.jpg/1,1,0,10/full/0/default.jpg",
            "non-positive-region",
        ),
        (
            "https://europe.iiif.teklia.com/iiif/2/geneanet%2Fx%2Fy%2Fz.jpg/1,1,10,0/full/0/default.jpg",
            "non-positive-region",
        ),
        (
            "https://europe.iiif.teklia.com/iiif/2/geneanet%2Fx%2Fy%2Fz.jpg/-1,1,10,10/full/0/default.jpg",
            "non-positive-region",
        ),
        (
            "https://europe.iiif.teklia.com/iiif/2/onlyonesegment/1,1,10,10/full/0/default.jpg",
            "unparseable-record-url",
        ),
        (
            "https://europe.iiif.teklia.com/iiif/2/"
            "..%2F..%2Fetc%2Fpasswd.jpg/1,1,10,10/full/0/default.jpg",
            "unsafe-identifier-segment",
        ),
        (
            "https://europe.iiif.teklia.com/iiif/2/a%2F.%2Fb.jpg/1,1,10,10/full/0/default.jpg",
            "unsafe-identifier-segment",
        ),
        (
            "https://europe.iiif.teklia.com/iiif/2/a%2F%5C%2Fb.jpg/1,1,10,10/full/0/default.jpg",
            "unsafe-identifier-segment",
        ),
        (
            "https://europe.iiif.teklia.com/iiif/2/a%2Fb%0Ac.jpg/1,1,10,10/full/0/default.jpg",
            "unsafe-identifier-segment",
        ),
        (
            "https://europe.iiif.teklia.com/iiif/2/a%2F%2Fb.jpg/1,1,10,10/full/0/default.jpg",
            "unsafe-identifier-segment",
        ),
    ],
)
def test_parse_record_url_refuses_by_name(url, reason):
    with pytest.raises(CorpusRefusal, match=f"^{reason}:"):
        parse_record_url(url)


def test_parse_record_url_refuses_a_non_string():
    with pytest.raises(CorpusRefusal, match="^unparseable-record-url:"):
        parse_record_url(None)


def test_parse_record_url_refuses_a_trailing_newline():
    # `$` in Python matches before a trailing newline; the anchor must be `\Z`
    # so a record_url smuggling a newline after the format extension is refused
    # rather than silently accepted.
    with pytest.raises(CorpusRefusal, match="^unparseable-record-url:"):
        parse_record_url(ONE_PAGE_URL + "\n")


# --- parse_record_url: a caller that can convert may admit a 180-degree view ----

ROTATED_URL = (
    "https://europe.iiif.teklia.com/iiif/2/dai-cretdhi%2FIle_de_re%2Fimg%2Fx.jpg/"
    "376,1585,1544,324/full/180/default.jpg"
)


def test_the_default_parser_still_refuses_a_rotated_view_by_name():
    with pytest.raises(CorpusRefusal, match="^unsupported-rotation-parameter"):
        parse_record_url(ROTATED_URL)
    assert parse_record_url(ONE_PAGE_URL).rotation == "0"


def test_a_caller_naming_the_supported_rotations_receives_the_rotation_it_must_convert():
    parsed = parse_record_url(ROTATED_URL, rotations=SUPPORTED_ROTATIONS)
    assert parsed.rotation == "180"
    assert parsed.region == {"x": 376, "y": 1585, "w": 1544, "h": 324}, "stated in the view's frame"
    # Asking for a rotation nothing here can convert is refused even when asked.
    with pytest.raises(CorpusRefusal, match="^unsupported-rotation-parameter"):
        parse_record_url(ROTATED_URL.replace("/180/", "/90/"), rotations=frozenset({"0", "90"}))
    assert SUPPORTED_ROTATIONS == frozenset({"0", "180"})
