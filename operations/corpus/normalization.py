"""The named, versioned text normalization applied before CER/WER.

Evidence keeps its raw text; this module only derives a comparison form, the same
profile for the reference and for every reading scored against it. Case, accents,
spelling and punctuation remain significant.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from importlib.metadata import version as _installed_version

from uniseg.graphemecluster import grapheme_clusters


class MeasurementRefusal(ValueError):
    """A text cannot be normalized or scored as asked.

    Not a `CorpusRefusal`: the modules that score refuse a text this would refuse
    first, by name and by record.
    """


# Read once, from the environment actually running, not asserted as a literal:
# a profile digest naming a uniseg version nobody checks could seal a version
# that segments under different rules than the one recorded.
_UNISEG_VERSION = _installed_version("uniseg")

# Unicode's White_Space property, as code-point ranges. Python's `\s` also
# matches U+001C..U+001F, which are separators but not White_Space.
_WHITE_SPACE_RANGES: tuple[tuple[int, int], ...] = (
    (0x0009, 0x000D),
    (0x0020, 0x0020),
    (0x0085, 0x0085),
    (0x00A0, 0x00A0),
    (0x1680, 0x1680),
    (0x2000, 0x200A),
    (0x2028, 0x2029),
    (0x202F, 0x202F),
    (0x205F, 0x205F),
    (0x3000, 0x3000),
)
_WHITESPACE = re.compile(
    "[" + "".join(f"\\u{low:04x}-\\u{high:04x}" for low, high in _WHITE_SPACE_RANGES) + "]+"
)

# Mapped one character at a time, in a single pass, after the first NFC. No
# replacement contains a mapped character, so the order of entries is immaterial.
_PRESENTATION_MAP: dict[str, str] = {
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
    "\ufb06": "st",  # the plain st ligature; the long-s+t one depends on the profile
}

# The one text bound in this instrument, for every field that can reach a
# quadratic comparison: scoring.py's Levenshtein.editops is worse-than-linear in
# the product of its two input lengths, and adjudication.py's SequenceMatcher is
# quadratic outright on repetitive input. One act's diplomatic transcription --
# an entry, or at most a short letter or essay (GLOSSARY's "act") -- is never
# near this length; text this long is a mis-pasted file, not a reading.
MAX_TEXT_LENGTH = 20_000


# UAX #15's Stream-Safe Text Format caps a run of non-starters at 30, and this
# instrument adopts that cap for a measured reason: uniseg's grapheme
# segmentation is quadratic in the length of a *single* cluster. Measured
# against uniseg 0.10.1 -- one base character carrying 4,000
# combining marks segments in 5.9s and 8,000 in 23.5s, so MAX_TEXT_LENGTH alone
# would let 20 KB of vendor output cost minutes of CPU per scored cell. Real
# diplomatic transcription never stacks more than a handful of marks on one
# character; polytonic Greek reaches three. The property table must be the same
# pinned Unicode-16 table that performs segmentation: Python 3.13 and 3.14 ship
# different ``unicodedata`` versions and otherwise disagree about newly assigned
# combining marks such as U+0897.
MAX_COMBINING_RUN = 30


@dataclass(frozen=True, slots=True)
class NormalizationProfile:
    """A small, named normalization policy.

    ``graphemic-v1`` folds the long s into ``s``; ``allographic-v1`` keeps it. A
    record carries the profile digest, so a change to any rule changes the digest.
    """

    profile_id: str
    map_long_s: bool

    def __post_init__(self) -> None:
        if not isinstance(self.profile_id, str) or not self.profile_id:
            raise MeasurementRefusal("normalization profile_id must be non-empty")

    def character_map(self) -> dict[str, str]:
        """The exact single-character mapping this profile applies.

        The long-s+t ligature maps to whatever its two letters map to, so the
        same ink normalizes alike whether it arrives precomposed or as ``ſt``.
        """

        mapping = dict(_PRESENTATION_MAP)
        mapping["\ufb05"] = "st" if self.map_long_s else "\u017ft"
        if self.map_long_s:
            mapping["\u017f"] = "s"
        return mapping

    def record(self) -> dict[str, object]:
        """Every definition the profile digest binds: the rules themselves, not labels."""

        return {
            "profile_id": self.profile_id,
            # `normalize_text` runs NFC, then the whitespace and character
            # mappings, then NFC again, because a mapping can leave a composable
            # sequence behind.
            "unicode_normalization": "NFC-then-mappings-then-NFC",
            "whitespace": {
                "rule": "each-run-to-one-U+0020-then-trim",
                "code_point_ranges": [
                    [f"U+{low:04X}", f"U+{high:04X}"] for low, high in _WHITE_SPACE_RANGES
                ],
            },
            "character_map": [
                [f"U+{ord(source):04X}", [f"U+{ord(item):04X}" for item in target]]
                for source, target in sorted(self.character_map().items())
            ],
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
            "text_bounds": {
                "max_text_length": MAX_TEXT_LENGTH,
                "max_combining_run": MAX_COMBINING_RUN,
            },
            "character_units": f"UAX29-extended-grapheme-clusters-uniseg-{_UNISEG_VERSION}",
            "word_units": "nonempty-runs-between-canonical-U+0020-spaces",
        }

    @property
    def digest(self) -> str:
        encoded = json.dumps(
            self.record(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


GRAPHEMIC_V1 = NormalizationProfile("graphemic-v1", map_long_s=True)
ALLOGRAPHIC_V1 = NormalizationProfile("allographic-v1", map_long_s=False)

PROFILES: dict[str, NormalizationProfile] = {
    GRAPHEMIC_V1.profile_id: GRAPHEMIC_V1,
    ALLOGRAPHIC_V1.profile_id: ALLOGRAPHIC_V1,
}


def normalize_text(text: str, profile: NormalizationProfile) -> str:
    """Apply the exact ordered normalization contract to one private text string."""

    if not isinstance(text, str):
        raise MeasurementRefusal("only Unicode strings can be normalized")
    if not isinstance(profile, NormalizationProfile):
        raise MeasurementRefusal("normalization requires a named NormalizationProfile")
    normalized = unicodedata.normalize("NFC", text)
    normalized = _WHITESPACE.sub(" ", normalized).strip(" ")
    normalized = normalized.translate(str.maketrans(profile.character_map()))
    # NFC again, because the mappings above run after the first one and can
    # leave a composable sequence behind. `ſ` + U+0301 has no precomposed form,
    # so the first NFC leaves it decomposed; mapping the long-s then yields
    # `s` + U+0301, which does compose to `ś`. Without this line the same ink
    # normalizes to different bytes depending on which spelling it arrived in,
    # and two correct readings of one character score as a substitution.
    return unicodedata.normalize("NFC", normalized)


def character_units(text: str, profile: NormalizationProfile) -> tuple[str, ...]:
    """Return UAX #29 extended grapheme-cluster units after normalization."""

    return tuple(grapheme_clusters(normalize_text(text, profile)))


def word_units(text: str, profile: NormalizationProfile) -> tuple[str, ...]:
    """Return reproducible space-delimited word units; punctuation remains part of a word."""

    normalized = normalize_text(text, profile)
    return tuple(part for part in normalized.split(" ") if part)
