r"""Derived search-fold normalization for Armarium projections.

This module deliberately has one small job: make a lossy, deterministic search
key from an established text.  It never establishes, replaces, compares, or
selects a reading.  Callers must retain the literal Archetypus text beside any
value returned here and label the value as derived.  GOVERNANCE 5's one-text rule
is untouched by it: the Archetypus ``text`` field is never written from here and
nothing this function returns may round-trip back into one.

**The substitution table and the apostrophe set are carried from the window, and
named as carried** (CLAUDE.md, Quarantine: "a line carried across is the exception
and is never silent ... adapted, renamed and reformatted are the same act as
copied").  Source: ``local/textnorm.py`` in the old repository, read through the
window.  What is carried is the *data* in ``_SUBSTITUTIONS`` and ``_APOSTROPHES``,
because each entry records a fact about this project's actual source material that
cannot be re-derived from Unicode or from inside this container:

* ``ȣ``/``Ȣ`` (U+0223/U+0222, the Algonquian/Iroquoian "8" digraph) fold to the
  ASCII digit ``8`` because that is how this corpus's data already spells the
  digraph -- the old file records 400 of 400 names in the Oka seed list using
  literal ``8`` and none using the Unicode glyph.  The substitution exists so a
  model that emits the real ligature folds to the same key as the ``8``-spelled
  form.  The digit itself is never folded away by any later step, on purpose: it
  is what distinguishes an indigenous name token from an ordinary French one, and
  removing it would false-match the two.  Nothing may add ``8`` to a strip set.
* ``œ``/``Œ`` and ``æ``/``Æ`` have no NFD decomposition at all, so an accent fold
  alone would leave them standing; expanding them is what makes "sœur"/"soeur"
  collide the way a person typing a search expects.
* The apostrophe family is *stripped* rather than turned into a space, because in
  this corpus an apostrophe is an elision mark inside one name unit -- d'Amours,
  d'Argenteuil -- not a word separator, so "damours" must hit "d'Amours" as one
  token.  The grave accent is in the set as a cursive-transcription stand-in.
  This is a recorded choice: a corpus needing apostrophe-as-separator would revisit
  it here.

The *code* below is written new and does one thing the old file did not.  The old
pipeline substituted before decomposing, which is not idempotent: ``ǣ`` (U+01E3)
is not in the table, so it decomposed to ``æ`` plus a combining macron and folded
to ``æ``, and only a *second* pass turned that into ``ae``.  Decomposing first and
substituting after makes the marked ligature reach ``ae`` in one pass, so
``search_fold(search_fold(s)) == search_fold(s)`` holds -- the idempotence property
the old file's own docstring claimed and its ordering did not deliver.
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
