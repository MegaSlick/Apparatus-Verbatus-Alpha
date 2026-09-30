"""The Designator establishes no text: the keys none of its artifacts may carry."""

from common.contracts.errors import ContractError

# Fields a Designator artifact may never carry, at any depth: this stage
# establishes no text, and a transcription would otherwise pass as geometry-shaped
# JSON. "reason" and "rationale" describe which rule fired, not ink, so they stay.
FORBIDDEN_TEXT_KEYS = frozenset(
    {
        "text",
        "reported",
        "transcription",
        "transcript",
        "content",
        "reading",
        "literal",
        "token",
        "tokens",
        # Not text: the retired picker's words for an elected witness
        # (GLOSSARY, "Retired terms"). No stage elects a witness.
        "chosen",
        "pivot",
    }
)


def refuse_text_fields(value, path: str = "$", *, kind: str = "act-group") -> None:
    """Walk a payload and refuse any forbidden content-bearing key, at any depth."""
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in FORBIDDEN_TEXT_KEYS:
                raise ContractError(
                    f"payload at {path}.{key} carries a forbidden content field; a "
                    f"Designator {kind} artifact carries no text at the schema boundary"
                )
            refuse_text_fields(item, f"{path}.{key}", kind=kind)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            refuse_text_fields(item, f"{path}[{index}]", kind=kind)
