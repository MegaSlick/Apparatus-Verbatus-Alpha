"""One reading of "this looks like a secret", shared by every boundary that screens for one."""

from __future__ import annotations

import re
from typing import Final

CREDENTIAL_MARKERS: Final = ("key", "secret", "password", "credential", "bearer", "token")
PROVIDER_ENV_PREFIXES: Final = ("RUNPOD_", "AWS_", "HF_", "HUGGINGFACE_")
CREDENTIAL_VALUE_PREFIXES: Final = ("sk-", "hf_", "ghp_", "gho_", "github_pat_", "AKIA", "xox")
# Path, URL, query and quoting punctuation separate pieces; a secret can sit in any one of them.
CREDENTIAL_PIECE: Final = re.compile(r"""[^\s/\\:@=?&,;"'()\[\]{}<>]+""")


def looks_like_credential_field(name: str) -> bool:
    normalized = name.lower().replace("-", "_")
    return any(marker in normalized for marker in CREDENTIAL_MARKERS)


def looks_like_credential_env(name: str) -> bool:
    return name.startswith(PROVIDER_ENV_PREFIXES) or looks_like_credential_field(name)


def is_credential_piece(piece: str) -> bool:
    """A known provider prefix, or an opaque 20+ mixed alphanumeric run that is not lowercase hex.

    A piece with one dot is a file name and only the prefix applies; two or more dots
    (a JWT's three segments) are read as one run.
    """

    if piece.startswith(CREDENTIAL_VALUE_PREFIXES):
        return True
    if len(piece) < 20 or piece.count(".") == 1:
        return False
    if all(character in "0123456789abcdef." for character in piece):
        return False
    return any(character.isalpha() for character in piece) and any(
        character.isdigit() for character in piece
    )


def credential_piece(text: str) -> str | None:
    return next((p for p in CREDENTIAL_PIECE.findall(text) if is_credential_piece(p)), None)


def looks_like_credential_value(text: str) -> bool:
    return credential_piece(text) is not None
