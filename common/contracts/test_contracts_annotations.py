"""The annotation layer's own contract, tested where it is defined.

Read-back callers hand `validate_annotations` whatever a packaged record's JSON
holds, so every malformed value must reach a named refusal, never a TypeError.
"""

from __future__ import annotations

import pytest

from common.contracts.annotations import validate_annotations
from common.contracts.errors import SchemaRefusal

TEXT = "Maria Theresia"


def _uncertain(**overrides) -> dict:
    note = {
        "kind": "uncertain",
        "start": 0,
        "end": 5,
        "certainty": "low",
        "alternatives": ["Marla"],
    }
    note.update(overrides)
    return note


def test_a_sound_uncertain_note_validates():
    assert validate_annotations([_uncertain()], TEXT, None, "annotations") == [_uncertain()]


@pytest.mark.parametrize(
    ("note", "match"),
    (
        (_uncertain(kind=["uncertain"]), "has kind"),
        (_uncertain(kind={"uncertain": True}), "has kind"),
        (_uncertain(certainty=["low"]), "has certainty"),
        (_uncertain(certainty={"low": 1}), "has certainty"),
    ),
)
def test_an_unhashable_kind_or_certainty_is_a_named_refusal(note, match):
    with pytest.raises(SchemaRefusal, match=match):
        validate_annotations([note], TEXT, None, "annotations")
