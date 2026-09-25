"""One reading of "this looks like a secret", shared by every boundary that screens for one."""

from __future__ import annotations

import re
from typing import Final

CREDENTIAL_MARKERS: Final = ("key", "secret", "password", "credential", "bearer", "token")
PROVIDER_ENV_PREFIXES: Final = ("RUNPOD_", "AWS_", "HF_", "HUGGINGFACE_")
CREDENTIAL_VALUE_PREFIXES: Final = ("sk-", "hf_", "ghp_", "gho_", "github_pat_", "AKIA", "xox")
# Path, URL, query and quoting punctuation separate pieces; a secret can sit in any one of them.
CREDENTIAL_PIECE: Final = re.compile(r"""[^\s/\\:@=?&,;"'()\[\]{}<>]+""")
_FILE_OR_HOST_SUFFIXES: Final = frozenset(
    "bin bz2 com csv dev gguf gz io internal invalid jpg json jsonl local log md net org pdf "
    "png py safetensors sh tar tif tiff toml txt xz yaml yml zip zst".split()
)


def looks_like_credential_field(name: str) -> bool:
    normalized = name.lower().replace("-", "_")
    return any(marker in normalized for marker in CREDENTIAL_MARKERS)


def looks_like_credential_env(name: str) -> bool:
    return name.startswith(PROVIDER_ENV_PREFIXES) or looks_like_credential_field(name)


def _opaque(run: str) -> bool:
    """20+ characters mixing letters and digits, not lowercase hex, not a file name or host."""

    if len(run) < 20 or "\\" in run:
        return False
    if "." in run and run.rsplit(".", 1)[1].lower() in _FILE_OR_HOST_SUFFIXES:
        return False
    if all(character in "0123456789abcdef." for character in run):
        return False
    return any(character.isalpha() for character in run) and any(
        character.isdigit() for character in run
    )


def is_credential_piece(piece: str) -> bool:
    """A known provider prefix, or an opaque run; a one-dot piece is a file name (prefix only)."""

    if piece.startswith(CREDENTIAL_VALUE_PREFIXES):
        return True
    return piece.count(".") != 1 and _opaque(piece)


def credential_piece(text: str) -> str | None:
    """The first whole word, or piece of one, that reads as a secret."""

    for word in text.split():
        stripped = word.strip("\"'(),;:")
        if _opaque(stripped):
            return stripped
        piece = next((p for p in CREDENTIAL_PIECE.findall(word) if is_credential_piece(p)), None)
        if piece is not None:
            return piece
    return None


def looks_like_credential_value(text: str) -> bool:
    return credential_piece(text) is not None
