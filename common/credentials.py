"""One reading of "this looks like a secret", shared by every boundary that screens for one.

Each caller's check is its baseline rule OR the shape rule, so it catches everything the
baseline catches. Accepted risk: the argv refusal and log redaction let a run ending in a
known file extension or domain pass, so "<opaque>.json" passes there. The argv refusal
does not catch "user:<opaque>" or an opaque path segment, as its baseline did not: run
folders and temp directories are long mixed-alphanumeric segments.
"""

from __future__ import annotations

import re
from typing import Final

CREDENTIAL_MARKERS: Final = ("key", "secret", "password", "credential", "bearer", "token")
PROVIDER_ENV_PREFIXES: Final = ("RUNPOD_", "AWS_", "HF_", "HUGGINGFACE_")
CREDENTIAL_VALUE_PREFIXES: Final = ("sk-", "hf_", "ghp_", "gho_", "github_pat_", "AKIA", "xox")
# Path, URL, query and quoting punctuation separate pieces; a secret can sit in any one of them.
CREDENTIAL_PIECE: Final = re.compile(r"""[^\s/\\:@=?&,;"'()\[\]{}<>]+""")
# A URL's user-info password and its query values: never exempted as a file or host.
_URL_SECRET: Final = re.compile(
    r"://[^/:@\s]*:(?P<password>[^/@\s]+)@|[?&][^=&#\s]*=(?P<query>[^&#\s]*)"
)
# Whole words the shape rule reads as data, not secrets: an ISO timestamp, an org/model id.
_TIMESTAMP_OR_MODEL_ID: Final = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?"
    r"|[A-Za-z][A-Za-z0-9_.]*/[A-Za-z][A-Za-z0-9_.]*(?:-[A-Za-z0-9_.]+)+"
)
_FILE_OR_HOST_SUFFIXES: Final = frozenset(
    "bin bz2 com csv dev gguf gz io internal invalid jpg json jsonl local log md net org pdf "
    "png py safetensors sh tar tif tiff toml txt xz yaml yml zip zst".split()
)


def looks_like_credential_field(name: str) -> bool:
    normalized = name.lower().replace("-", "_")
    return any(marker in normalized for marker in CREDENTIAL_MARKERS)


def looks_like_credential_env(name: str) -> bool:
    return name.startswith(PROVIDER_ENV_PREFIXES) or looks_like_credential_field(name)


# --- the shape rule -----------------------------------------------------------------------


def _opaque(run: str, files_and_hosts_pass: bool) -> bool:
    if len(run) < 20 or "\\" in run:
        return False
    if files_and_hosts_pass and run.rsplit(".", 1)[-1].lower() in _FILE_OR_HOST_SUFFIXES:
        return False
    if all(character in "0123456789abcdef." for character in run):
        return False
    return any(character.isalpha() for character in run) and any(
        character.isdigit() for character in run
    )


def is_credential_piece(piece: str, *, files_and_hosts_pass: bool = False) -> bool:
    """A known provider prefix, or an opaque run; a one-dot piece is a file name (prefix only)."""

    if piece.startswith(CREDENTIAL_VALUE_PREFIXES):
        return True
    return piece.count(".") != 1 and _opaque(piece, files_and_hosts_pass)


def _url_secret(text: str) -> str | None:
    for match in _URL_SECRET.finditer(text):
        secret = credential_piece(match.group("password") or match.group("query") or "")
        if secret is not None:
            return secret
    return None


def credential_piece(text: str, *, files_and_hosts_pass: bool = False) -> str | None:
    """The first URL secret, whole word, or piece of a word that reads as a secret."""

    if files_and_hosts_pass and (secret := _url_secret(text)) is not None:
        return secret
    for word in text.split():
        stripped = word.strip("\"'(),;:")
        if _opaque(stripped, files_and_hosts_pass) and not _TIMESTAMP_OR_MODEL_ID.fullmatch(
            stripped
        ):
            return stripped
        for piece in CREDENTIAL_PIECE.findall(word):
            if is_credential_piece(piece, files_and_hosts_pass=files_and_hosts_pass):
                return piece
    return None


def looks_like_credential_value(text: str, *, files_and_hosts_pass: bool = False) -> bool:
    return credential_piece(text, files_and_hosts_pass=files_and_hosts_pass) is not None


def _shape_argv_piece(value: str) -> str | None:
    """A bare value gets the full shape test; a path segment only a key prefix or a dotted token."""

    if not any(character in "/\\.:@" for character in value) and is_credential_piece(value):
        return value
    if (secret := _url_secret(value)) is not None:
        return secret
    return next(
        (
            piece
            for piece in CREDENTIAL_PIECE.findall(value)
            if piece.startswith(CREDENTIAL_VALUE_PREFIXES)
            or (piece.count(".") >= 2 and is_credential_piece(piece, files_and_hosts_pass=True))
        ),
        None,
    )


# --- each caller's baseline rule ---------------------------------------

_BASELINE_SAFE_CHARACTERS: Final = frozenset(" /\\.:@")


def _baseline_models_value(value: str) -> bool:
    if value.startswith(CREDENTIAL_VALUE_PREFIXES):
        return True
    if len(value) < 20 or any(character in _BASELINE_SAFE_CHARACTERS for character in value):
        return False
    if all(character in "0123456789abcdef" for character in value):
        return False
    return any(character.isalpha() for character in value) and any(
        character.isdigit() for character in value
    )


_BASELINE_NOTIFY_MARKERS: Final = (*CREDENTIAL_MARKERS, "apikey")
_BASELINE_NOTIFY_SAFE_CHARACTERS: Final = frozenset(" \t\\,;()[]{}'\"")


def _baseline_notify_word(word: str) -> bool:
    normalized = word.lower().replace("-", "_")
    if any(marker in normalized for marker in _BASELINE_NOTIFY_MARKERS):
        return True
    if word.startswith(CREDENTIAL_VALUE_PREFIXES):
        return True
    if len(word) < 20 or any(character in _BASELINE_NOTIFY_SAFE_CHARACTERS for character in word):
        return False
    if all(character in "0123456789abcdef" for character in word):
        return False
    return any(character.isalpha() for character in word) and any(
        character.isdigit() for character in word
    )


def _baseline_notify_rule(message: str) -> bool:
    for word in message.split():
        stripped = word.strip("\"'(),;:")
        if stripped and _baseline_notify_word(stripped):
            return True
    return False


def _baseline_fixture_rule(value: str) -> bool:
    if _baseline_models_value(value):
        return True
    return any(
        stripped and _baseline_models_value(stripped)
        for stripped in (word.strip("\"'(),;:") for word in value.split())
    )


_BASELINE_TOKEN_PARTS: Final = re.compile(r"""[^\s"'{}\[\],;:=]+""")


def _baseline_redaction_rule(token: str) -> bool:
    return _baseline_models_value(token) or any(
        _baseline_models_value(part) for part in _BASELINE_TOKEN_PARTS.findall(token)
    )


# --- what each caller asks --------------------------------------------------------------


def notification_carries_credential(message: str) -> bool:
    return _baseline_notify_rule(message) or looks_like_credential_value(message)


def fixture_value_carries_credential(value: str) -> bool:
    return _baseline_fixture_rule(value) or looks_like_credential_value(value)


def argv_credential_piece(value: str) -> str | None:
    return value if _baseline_models_value(value) else _shape_argv_piece(value)


def log_word_carries_credential(word: str) -> bool:
    return _baseline_redaction_rule(word) or looks_like_credential_value(
        word, files_and_hosts_pass=True
    )
