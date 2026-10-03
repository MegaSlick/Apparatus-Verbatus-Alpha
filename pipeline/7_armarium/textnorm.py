r"""Derived search-fold normalization for Armarium projections.

Makes a lossy search key from established text. The key is deterministic only for a
given Unicode database: each Python version ships its own (15.0 on 3.12, 15.1 on 3.13,
16.0 on 3.14), and characters assigned between them fold differently. It never
establishes, replaces, compares or selects a reading: callers keep the literal
Archetypus text beside any value returned here and label the value as derived, and
nothing returned here may round-trip back into ``text``.

``_SUBSTITUTIONS`` and ``_APOSTROPHES`` record facts about the register material this
project reads, which Unicode alone would not give:

* ``ȣ``/``Ȣ`` (U+0223/U+0222, the Algonquian/Iroquoian "8" digraph) fold to the ASCII
  digit ``8``, because the corpus spells the digraph with the digit; a model emitting
  the real ligature then keys the same as the ``8``-spelled form. The digit itself is
  never stripped: it is what distinguishes an Indigenous name token from an ordinary
  French one. Nothing may add ``8`` to a strip set.
* ``œ``/``Œ`` and ``æ``/``Æ`` have no NFD decomposition at all, so an accent fold alone
  would leave them standing; expanding them is what makes "sœur"/"soeur" collide as a
  search expects.
* The apostrophe family is stripped, not turned into a space: in this corpus an
  apostrophe is an elision mark inside one name unit (a surname such as d'Exemple),
  not a word separator, so "dexemple" must hit "d'Exemple" as one token. The grave
  accent stands in for cursive transcription.

Decomposing before substituting makes ``search_fold(search_fold(s)) == search_fold(s)``
hold: ``ǣ`` (U+01E3) is not in the table, so substituting first would leave a second
pass needed to turn its NFD-decomposed ``æ`` + combining macron into ``ae``.
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
