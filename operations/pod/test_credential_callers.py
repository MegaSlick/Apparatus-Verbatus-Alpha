"""Every credential-screening caller catches at least what its baseline caught.

The baseline functions below are origin/main's `models.looks_like_credential_field` and
`looks_like_credential_value`, `notify_hooks._unsafe_reason`, `fixture._scrub`'s leaf check,
`bootstrap_main.refuse_credential_looking_argv` and `manager._redacted`. They are oracles:
each real caller is run over a seeded corpus and must catch everything its baseline does.
"""

from __future__ import annotations

import random
import re

import pytest

from common.test_credentials import (
    FILES_AND_HOSTS,
    OPAQUE,
    PASSING_VALUES,
    SHAPED_VALUES,
    TIMESTAMPS_AND_MODEL_IDS,
)
from operations.serving.manager import _redacted

from .bootstrap_main import PlanRefusal, refuse_credential_looking_argv
from .fixture import SCRUBBED, _scrub
from .notify_hooks import _unsafe_reason

# --- baseline oracles ---------------------------------------------------------------------

_MARKERS = ("key", "secret", "password", "credential", "bearer", "token")
_PREFIXES = ("sk-", "hf_", "ghp_", "gho_", "github_pat_", "AKIA", "xox")
_SAFE = frozenset(" /\\.:@")


def baseline_field(value: str) -> bool:
    normalized = value.lower().replace("-", "_")
    return any(marker in normalized for marker in _MARKERS)


def baseline_value(value: str) -> bool:
    if value.startswith(_PREFIXES):
        return True
    if len(value) < 20 or any(character in _SAFE for character in value):
        return False
    if all(character in "0123456789abcdef" for character in value):
        return False
    return any(character.isalpha() for character in value) and any(
        character.isdigit() for character in value
    )


_WORD_MARKERS = (*_MARKERS, "apikey")
_WORD_SAFE = frozenset(" \t\\,;()[]{}'\"")
_HOST_PATH = re.compile(r"(?:[\w-]+\.)+[a-zA-Z]{2,}/\S", re.ASCII)


def _baseline_word(word: str) -> bool:
    normalized = word.lower().replace("-", "_")
    if any(marker in normalized for marker in _WORD_MARKERS):
        return True
    if word.startswith(_PREFIXES):
        return True
    if len(word) < 20 or any(character in _WORD_SAFE for character in word):
        return False
    if all(character in "0123456789abcdef" for character in word):
        return False
    return any(character.isalpha() for character in word) and any(
        character.isdigit() for character in word
    )


def baseline_notify_refuses(message: str) -> bool:
    lowered = message.lower()
    if "http://" in lowered or "https://" in lowered or _HOST_PATH.search(message):
        return True
    for word in message.split():
        stripped = word.strip("\"'(),;:")
        if stripped and _baseline_word(stripped):
            return True
    return False


def baseline_fixture_scrubs(value: str) -> bool:
    if baseline_value(value):
        return True
    return any(
        stripped and baseline_value(stripped)
        for stripped in (word.strip("\"'(),;:") for word in value.split())
    )


def baseline_argv_refuses(argv: list[str]) -> bool:
    previous = ""
    for token in argv:
        if token.startswith("--") and "=" in token:
            flag, _, value = token.partition("=")
        elif token.startswith("--"):
            previous = token
            continue
        else:
            flag, value = previous, token
        previous = ""
        if flag == "--keep-env":
            continue
        if value and (baseline_field(value) or baseline_value(value)):
            return True
    return False


_LOG_ASSIGNMENT = re.compile(
    r"""(?P<lead>["']?)(?P<name>[A-Za-z_][A-Za-z0-9_.-]*)(?P=lead)\s*[:=]\s*"""
    r"""(?P<quote>["']?)(?P<value>[^\s"',;}\]]+)(?P=quote)"""
)
_LOG_BEARER = re.compile(r"""(?i)\bbearer\s+(?P<value>[^\s"',;]+)""")
_TOKEN_PARTS = re.compile(r"""[^\s"'{}\[\],;:=]+""")


def _redact_value(match: re.Match[str]) -> str:
    whole = match.group(0)
    start = match.start("value") - match.start(0)
    end = match.end("value") - match.start(0)
    return f"{whole[:start]}[redacted]{whole[end:]}"


def baseline_redacted(text: str) -> str:
    def redact_named(match: re.Match[str]) -> str:
        if not baseline_field(match.group("name")):
            return match.group(0)
        return _redact_value(match)

    def redact_by_shape(token: str) -> str:
        if baseline_value(token):
            return "[redacted]"
        return _TOKEN_PARTS.sub(
            lambda part: "[redacted]" if baseline_value(part.group(0)) else part.group(0), token
        )

    lines = []
    for raw in text.splitlines():
        line = _LOG_ASSIGNMENT.sub(redact_named, raw)
        line = _LOG_BEARER.sub(_redact_value, line)
        lines.append(
            "".join(
                part if index % 2 else redact_by_shape(part)
                for index, part in enumerate(re.split(r"(\s+)", line))
            )
        )
    return "\n".join(lines)


# --- the real callers ----------------------------------------------------------------------


def notify_refuses(message: str) -> bool:
    return _unsafe_reason(message) is not None


def fixture_scrubs(value: str) -> bool:
    return _scrub({"message": value}, "body", []) == {"message": SCRUBBED}


def argv_refuses(argv: list[str]) -> bool:
    try:
        refuse_credential_looking_argv(argv)
    except PlanRefusal:
        return True
    return False


def _corpus() -> list[str]:
    listed = [
        *SHAPED_VALUES,
        *PASSING_VALUES,
        *TIMESTAMPS_AND_MODEL_IDS,
        *FILES_AND_HOSTS,
        "Tr0ub4dor&3xK9pLm2Qz=out.json",
        "path\\to=Tr0ub4dor&3xK9pLm2Qz",
        "deadbeef01.cafebabe02",
        "https://user:" + OPAQUE + ".json@example.invalid/x",
    ]
    for length in range(17, 20):
        run = ("aB3xK9pLm2Qz7Tr0ub4dor" * 2)[:length]
        listed += [f"{a}{run}{b}" for a, b in ("()", ",,", ";;", '""', "''", "(,", "';")]
    fragments = [
        *"abcdefXYZ0123456789",
        *".:/@=&?()[]{}<>;,'\"\\-_+#% \t\n",
        "hf_",
        "ghp_",
        "gho_",
        "AKIA",
        "xox",
        "sk-",
        "key",
        "token",
        "Bearer ",
        "deadbeef",
    ]
    generator = random.Random(20260925)
    for _ in range(20_000):
        target = generator.randint(17, 25)
        text = ""
        while len(text) < target:
            text += generator.choice(fragments)
        listed.append(text)
    return listed


CORPUS = _corpus()


@pytest.mark.parametrize(
    ("baseline", "caller"),
    [
        (baseline_notify_refuses, notify_refuses),
        (baseline_fixture_scrubs, fixture_scrubs),
        (lambda v: baseline_argv_refuses(["--run-id", v]), lambda v: argv_refuses(["--run-id", v])),
        (lambda v: baseline_redacted(v) != v, lambda v: _redacted(v) != v),
    ],
    ids=["notify", "fixture", "argv", "redaction"],
)
def test_no_caller_lets_through_what_its_baseline_caught(baseline, caller) -> None:  # type: ignore[no-untyped-def]
    assert [value for value in CORPUS if baseline(value) and not caller(value)] == []


@pytest.mark.parametrize("value", TIMESTAMPS_AND_MODEL_IDS)
def test_a_timestamp_or_model_id_passes_the_fixture_argv_and_logs(value: str) -> None:
    assert not fixture_scrubs(value)
    assert not argv_refuses(["--run-id", value])
    assert _redacted(f"INFO {value}") == f"INFO {value}"
