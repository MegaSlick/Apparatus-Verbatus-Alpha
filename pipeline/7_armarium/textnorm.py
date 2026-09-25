r"""Derived search-fold normalization for Armarium projections.

Makes a lossy search key from established text, deterministic only **for a
given Unicode database**: Python ships a different UCD per version (15.0 on
3.12, 16.0 on 3.14 -- CI runs both -- and 15.1 on 3.13, which nothing in the
ladder runs today), and characters assigned between them fold differently. Never establishes, replaces, compares, or selects a reading:
callers must retain the literal Archetypus text beside any value returned
here and label the value as derived; nothing this function returns may
round-trip back into ``text`` (principle 5).

``_SUBSTITUTIONS`` and ``_APOSTROPHES`` are adapted from ``local/textnorm.py``
in the old repository (principle 12) because each entry records a fact about
this project's actual source material, not something re-derivable from
Unicode:

* ``ȣ``/``Ȣ`` (U+0223/U+0222, the Algonquian/Iroquoian "8" digraph) fold to
  the ASCII digit ``8`` because that is how this corpus already spells the
  digraph, so a model emitting the real ligature keys the same as the
  ``8``-spelled form. The digit itself is never stripped: it is what
  distinguishes an indigenous name token from an ordinary French one.
* ``œ``/``Œ`` and ``æ``/``Æ`` have no NFD decomposition at all, so an accent
  fold alone would leave them standing; expanding them is what makes
  "sœur"/"soeur" collide as a search expects.
* The apostrophe family is stripped, not turned into a space: in this corpus
  an apostrophe is an elision mark inside one name unit (d'Amours,
  d'Argenteuil), not a word separator, so "damours" must hit "d'Amours" as
  one token. The grave accent stands in for cursive transcription.

Decomposing before substituting (the old file did the reverse) is what makes
``search_fold(search_fold(s)) == search_fold(s)`` hold: ``ǣ`` (U+01E3) isn't
in the table, so substituting first left a second pass needed to turn its
NFD-decomposed ``æ`` + combining macron into ``ae``.
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
        character for character in decomposed if unicodedata.category(character) != "Mn"
    )
    substituted = "".join(_SUBSTITUTIONS.get(character, character) for character in without_marks)
    folded: list[str] = []
    for character in substituted.casefold():
        if character in _APOSTROPHES:
            continue
        folded.append(character if character.isalnum() else " ")
    return " ".join("".join(folded).split())
