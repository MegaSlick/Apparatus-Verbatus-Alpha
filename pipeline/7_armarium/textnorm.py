"""Derived search-fold normalization for Armarium projections.

This module deliberately has one small job: make a lossy, deterministic search
key from an established text.  It never establishes, replaces, compares, or
selects a reading.  Callers must retain the literal Archetypus text beside any
value returned here and label the value as derived.

The ordering is intentional.  Combining marks are removed before the explicit
ligature substitutions, so a marked ``\N{LATIN SMALL LETTER AE WITH MACRON}``
first becomes ``\N{LATIN SMALL LETTER AE}`` and then ``ae`` in the same pass.
That makes the result a fixed point rather than requiring a second fold.
"""

from __future__ import annotations

import unicodedata
from typing import Final

from common.contracts.errors import SchemaRefusal

TEXTNORM_REVISION: Final = "armarium-textnorm-v1"

_SUBSTITUTIONS: Final = {
    "ȣ": "8",
    "Ȣ": "8",
    "œ": "oe",
    "Œ": "oe",
    "æ": "ae",
    "Æ": "ae",
}
_APOSTROPHES: Final = frozenset({"'", "’", "ʼ", "`"})


def search_fold(text: str) -> str:
    """Return the derived, accent-folded search key for one literal string.

    A missing literal is not an empty search key.  Treating it as one would make
    a provenance or text omission look like a harmless no-match instead of the
    schema refusal it is.
    """
    if not isinstance(text, str):
        raise SchemaRefusal("text normalization requires one literal string")

    decomposed = unicodedata.normalize("NFD", text)
    without_marks = "".join(
        character
        for character in decomposed
        if unicodedata.category(character) != "Mn"
    )
    substituted = "".join(_SUBSTITUTIONS.get(character, character) for character in without_marks)
    folded: list[str] = []
    for character in substituted.casefold():
        if character in _APOSTROPHES:
            continue
        folded.append(character if character.isalnum() else " ")
    return " ".join("".join(folded).split())
