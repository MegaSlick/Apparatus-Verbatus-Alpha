"""The corpus register's preference screen names every consult §7 shape-one word."""

from common.corpus_register import _FORBIDDEN_PREFERENCE_FIELDS

# Consult §7 shape 1, in full. These are binding review vocabulary, so the
# recursive screen spells every one of them rather than the subset that happened
# to appear in a payload first.
SHAPE_ONE_WORDS = frozenset(
    {"primary", "canonical", "preferred", "best", "better", "winner", "selected", "chosen"}
)


def test_the_recursive_preference_screen_names_every_shape_one_word():
    assert SHAPE_ONE_WORDS <= _FORBIDDEN_PREFERENCE_FIELDS
