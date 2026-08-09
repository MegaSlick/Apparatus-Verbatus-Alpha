"""The predeclared, versioned text normalization used before CER/WER.

Raw human and model text is deliberately not changed in evidence.  This module only
derives a comparison form, using one named profile for every candidate and every
Testimonium.  It is not the old pipeline's search fold: case, accents, spelling,
and punctuation remain significant.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from uniseg.graphemecluster import grapheme_clusters

from .encoding import canonical_json_bytes, sha256_bytes
from .errors import MeasurementRefusal

_WHITESPACE = re.compile(r"\s+", flags=re.UNICODE)
_PRESENTATION_TRANSLATION = str.maketrans(
    {
        "\u2018": "'",  # left single quotation mark
        "\u2019": "'",  # right single quotation mark
        "\u02bc": "'",  # modifier letter apostrophe
        "\u2010": "-",  # hyphen
        "\u2011": "-",  # non-breaking hyphen
        "\ufb00": "ff",  # presentation ligatures only
        "\ufb01": "fi",
        "\ufb02": "fl",
        "\ufb03": "ffi",
        "\ufb04": "ffl",
        "\ufb05": "st",
        "\ufb06": "st",
    }
)


@dataclass(frozen=True, slots=True)
class NormalizationProfile:
    """A deliberately small, named normalization policy.

    The optional strict allographic profile exists so Tyrel can select it *before*
    an evaluation run.  A run records the exact profile digest; it cannot silently
    change long-s treatment after results appear.
    """

    profile_id: str
    map_long_s: bool

    def __post_init__(self) -> None:
        if not isinstance(self.profile_id, str) or not self.profile_id:
            raise MeasurementRefusal("normalization profile_id must be non-empty")

    def record(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "unicode_normalization": "NFC",
            "whitespace": "unicode-to-single-ascii-space-then-trim",
            "presentation_map": "apostrophe-hyphen-and-listed-compatibility-ligatures-v1",
            "map_long_s": self.map_long_s,
            "preserve": [
                "case",
                "remaining-punctuation",
                "diacritics",
                "digits",
                "historical-spelling",
                "abbreviations",
                "i-j",
                "u-v",
                "oe-ae",
            ],
            "character_units": "UAX29-extended-grapheme-clusters-uniseg-0.10.1",
            "word_units": "nonempty-runs-between-canonical-U+0020-spaces",
        }

    @property
    def digest(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.record()))


# The recommendation from the predeclared protocol.  This is not selected for a
# real run by default; a run plan must carry its profile ID and digest explicitly.
GRAPHEMIC_V1 = NormalizationProfile("graphemic-v1", map_long_s=True)
ALLOGRAPHIC_V1 = NormalizationProfile("allographetic-v1", map_long_s=False)

PROFILES: dict[str, NormalizationProfile] = {
    GRAPHEMIC_V1.profile_id: GRAPHEMIC_V1,
    ALLOGRAPHIC_V1.profile_id: ALLOGRAPHIC_V1,
}


def profile_by_id(profile_id: str) -> NormalizationProfile:
    """Resolve a closed profile vocabulary rather than accepting mutable knobs."""

    try:
        return PROFILES[profile_id]
    except KeyError as error:
        raise MeasurementRefusal(f"unknown normalization profile {profile_id!r}") from error


def require_canonical_profile(profile: NormalizationProfile) -> NormalizationProfile:
    """Refuse a same-name, changed-rule profile at the real-run boundary."""

    if not isinstance(profile, NormalizationProfile):
        raise MeasurementRefusal("real run normalization must be a named NormalizationProfile")
    canonical = profile_by_id(profile.profile_id)
    if profile != canonical or profile.digest != canonical.digest:
        raise MeasurementRefusal(
            "real run normalization profile differs from its predeclared canonical definition"
        )
    return canonical


def normalize_text(text: str, profile: NormalizationProfile) -> str:
    """Apply the exact ordered normalization contract to one private text string."""

    if not isinstance(text, str):
        raise MeasurementRefusal("only Unicode strings can be normalized")
    if not isinstance(profile, NormalizationProfile):
        raise MeasurementRefusal("normalization requires a named NormalizationProfile")
    normalized = unicodedata.normalize("NFC", text)
    normalized = _WHITESPACE.sub(" ", normalized).strip()
    normalized = normalized.translate(_PRESENTATION_TRANSLATION)
    if profile.map_long_s:
        normalized = normalized.replace("ſ", "s")
    return normalized


def character_units(text: str, profile: NormalizationProfile) -> tuple[str, ...]:
    """Return UAX #29 extended grapheme-cluster units after normalization."""

    return tuple(grapheme_clusters(normalize_text(text, profile)))


def word_units(text: str, profile: NormalizationProfile) -> tuple[str, ...]:
    """Return reproducible space-delimited word units; punctuation remains part of a word."""

    normalized = normalize_text(text, profile)
    return tuple(part for part in normalized.split(" ") if part)
