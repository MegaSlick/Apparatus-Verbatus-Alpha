"""Shared rules for the page-only smoke witness."""

PAGE_WITNESS_ALPHABET = "ABEFGHJMNRTYabdefghijmnqrty23456789"
PAGE_WITNESS_LENGTH = 43
PAGE_WITNESS_MAX_EDIT_DISTANCE = 2


def is_page_witness(value: object) -> bool:
    """Return whether ``value`` satisfies the generator's complete token shape."""

    return (
        isinstance(value, str)
        and len(value) == PAGE_WITNESS_LENGTH
        and all(character in PAGE_WITNESS_ALPHABET for character in value)
        and all(left != right for left, right in zip(value, value[1:], strict=False))
    )
