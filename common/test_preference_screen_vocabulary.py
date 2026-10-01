"""The corpus register's preference screen refuses every selection word."""

from common.corpus_register import _FORBIDDEN_PREFERENCE_FIELDS

# Words that name one witness or capture as chosen over another.
SELECTION_WORDS = frozenset(
    {"primary", "canonical", "preferred", "best", "better", "winner", "selected", "chosen"}
)


def test_the_recursive_preference_screen_names_every_selection_word():
    assert SELECTION_WORDS <= _FORBIDDEN_PREFERENCE_FIELDS
