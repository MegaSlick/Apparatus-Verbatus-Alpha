"""RecordGold's `record_url`, parsed into the page it names and the box on it.

RecordGold's `record_url` is a IIIF Image API 2 crop request, e.g.

    https://europe.iiif.teklia.com/iiif/2/geneanet%2FArdennes_BMS%2F380403%2F00026.jpg/
    239,208,1232,443/full/0/default.jpg

    host           europe.iiif.teklia.com
    identifier     geneanet/Ardennes_BMS/380403/00026.jpg   (percent-decoded)
    region         x,y,w,h in full-page pixels
    size           full
    rotation       0 or 180
    quality        default
    format         jpg

Every field but the region is a closed vocabulary, and this parser refuses
anything outside it **by name** rather than normalising it. Forty of the 7,720
rows across `val`/`train` carry `rotation=180`.

**The rotation vocabulary is one value by default and two at most.** `rotation`
accepts `"0"` unless the caller names more, and the only further value it will
ever name is `"180"` (`SUPPORTED_ROTATIONS`): a caller holding the stored page's
dimensions can carry a 180-degree box honestly, and `local_admission.py` is that
caller.

The identifier's last `/`-segment is the page's own filename (`designation`);
everything before it is the volume path (`volume_and_designation`).
`identifier_encoded` is the raw, already-percent-escaped path segment exactly as
`record_url` carried it: RecordGold identifiers contain literal `+`, which a
`unquote`/`quote` round trip would re-escape as `%2B`.
"""

import urllib.parse
from re import compile as _compile
from typing import Any, NamedTuple

from . import CorpusRefusal

EXPECTED_HOST = "europe.iiif.teklia.com"
EXPECTED_SIZE = "full"
EXPECTED_ROTATION = "0"
# The one rotation this parser accepts by default, and the one more it may be
# asked to accept. A 180-degree IIIF view is a defined geometry -- the region's
# `x,y,w,h` are stated in the rotated frame and map to the stored page as
# `(W - x - w, H - y - h, w, h)` -- so a caller that holds the stored page's
# width and height can carry the box across honestly; `local_admission.py` is
# that caller. 90 and 270 swap the axes and are never admitted.
SUPPORTED_ROTATIONS = frozenset({"0", "180"})
EXPECTED_QUALITY = "default"
EXPECTED_FORMAT = "jpg"

# https://<host>/iiif/2/<identifier>/<x>,<y>,<w>,<h>/<size>/<rotation>/<quality>.<format>
_URL_RE = _compile(
    r"^https://(?P<host>[^/]+)/iiif/2/(?P<identifier>.+)/"
    r"(?P<x>-?\d+),(?P<y>-?\d+),(?P<w>\d+),(?P<h>\d+)/"
    r"(?P<size>[^/]+)/(?P<rotation>[^/]+)/(?P<quality>[^./]+)\.(?P<format>[A-Za-z0-9]+)\Z"
)

RECORD_URL_REFUSAL_REASONS = frozenset(
    {
        "unparseable-record-url",
        "unexpected-host",
        "unsupported-size-parameter",
        "unsupported-rotation-parameter",
        "unsupported-quality-parameter",
        "unsupported-format-parameter",
        "non-positive-region",
        "unsafe-identifier-segment",
    }
)


class Refusal(CorpusRefusal):
    reasons = RECORD_URL_REFUSAL_REASONS


class ParsedRecordUrl(NamedTuple):
    identifier: str
    """Percent-decoded, human-readable, the grouping key for a page."""

    identifier_encoded: str
    """The raw escaped path segment exactly as `record_url` carried it."""

    host: str
    region: dict[str, int]
    """`x,y,w,h` exactly as `record_url` states them, in the frame of `rotation`."""

    rotation: str
    """The IIIF rotation parameter exactly as `record_url` states it.

    Required, with no default: a `ParsedRecordUrl` built without it would claim
    the record is upright, which is the one thing a caller carrying boxes must
    never be told by omission.
    """


def unsafe_segment(segment: str) -> bool:
    """Whether a decoded path segment is unsafe to carry into a filesystem path:
    empty, a traversal, a backslash or a control character."""
    return (
        not segment
        or segment in (".", "..")
        or "\\" in segment
        or any(ord(character) < 32 for character in segment)
    )


def parse_record_url(
    record_url: Any, *, rotations: frozenset[str] = frozenset({EXPECTED_ROTATION})
) -> ParsedRecordUrl:
    """Parse one `record_url`, refusing anything this parser does not recognise.

    `rotations` names the IIIF rotation values the caller can honestly carry;
    every other value is refused by name. The default admits only `"0"`. A caller
    passing `SUPPORTED_ROTATIONS` receives the rotation on the result and owns
    the frame conversion; a value outside `SUPPORTED_ROTATIONS` is refused even
    when asked for, because no conversion for it exists here.
    """
    if not isinstance(record_url, str):
        raise Refusal(f"unparseable-record-url: record_url must be a string, got {record_url!r}")
    match = _URL_RE.match(record_url)
    if match is None:
        raise Refusal(
            f"unparseable-record-url: {record_url!r} does not match the IIIF Image "
            "API 2 crop shape this parser recognises"
        )
    host = match.group("host")
    if host != EXPECTED_HOST:
        raise Refusal(f"unexpected-host: {host!r} in {record_url!r}, expected {EXPECTED_HOST!r}")

    size = match.group("size")
    if size != EXPECTED_SIZE:
        raise Refusal(
            f"unsupported-size-parameter: {size!r} in {record_url!r}, only {EXPECTED_SIZE!r} is recognised"
        )
    rotation = match.group("rotation")
    if rotation not in rotations or rotation not in SUPPORTED_ROTATIONS:
        raise Refusal(
            f"unsupported-rotation-parameter: {rotation!r} in {record_url!r}, only "
            f"{sorted(rotations & SUPPORTED_ROTATIONS)!r} recognised here — a rotation this "
            "caller cannot convert would put the region's x,y,w,h in a different frame "
            "from the stored pixels"
        )
    quality = match.group("quality")
    if quality != EXPECTED_QUALITY:
        raise Refusal(
            f"unsupported-quality-parameter: {quality!r} in {record_url!r}, only "
            f"{EXPECTED_QUALITY!r} is recognised"
        )
    fmt = match.group("format")
    if fmt != EXPECTED_FORMAT:
        raise Refusal(
            f"unsupported-format-parameter: {fmt!r} in {record_url!r}, only {EXPECTED_FORMAT!r} is recognised"
        )

    x, y, w, h = (int(match.group(name)) for name in ("x", "y", "w", "h"))
    if x < 0 or y < 0 or w <= 0 or h <= 0:
        raise Refusal(f"non-positive-region: x={x} y={y} w={w} h={h} in {record_url!r}")

    identifier_encoded = match.group("identifier")
    identifier = urllib.parse.unquote(identifier_encoded)
    if not identifier or "/" not in identifier:
        raise Refusal(
            f"unparseable-record-url: identifier {identifier!r} carries no volume/page "
            f"structure in {record_url!r}"
        )
    for segment in identifier.split("/"):
        if unsafe_segment(segment):
            raise Refusal(
                f"unsafe-identifier-segment: identifier {identifier!r} carries the "
                f"unsafe path segment {segment!r} in {record_url!r} — this parser "
                "refuses anything it does not recognise rather than normalising it"
            )
    return ParsedRecordUrl(
        identifier=identifier,
        identifier_encoded=identifier_encoded,
        host=host,
        region={"x": x, "y": y, "w": w, "h": h},
        rotation=rotation,
    )


def volume_and_designation(identifier: str) -> tuple[str, str]:
    segments = identifier.split("/")
    return "/".join(segments[:-1]), segments[-1]
