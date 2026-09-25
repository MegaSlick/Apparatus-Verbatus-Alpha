import pytest

from common.credentials import looks_like_credential_value

AWS_SHAPED = "wJalrXUtnFEMI/" + "K7MDENG/bPxRfiCYEXAMPLEKEY"
OPAQUE = "aB3fG9kL2mN7pQ5rS8tU1v"
JWT_SHAPED = "abcdefghij.klmnopqrst.uvwxyz1234"

SHAPED_VALUES = (
    "sk-not-a-real-key",
    OPAQUE,
    JWT_SHAPED,
    "QUJDREVGR0hJSktMTU5PUFFS+/=",
    "https://user" + ":" + OPAQUE + "@example.invalid/x",
    f"/workspace/{OPAQUE}/report.json",
    AWS_SHAPED,
    OPAQUE + ".",
    "user:" + OPAQUE,
    *(f"Tr0ub4dor{mark}3xK9pLm2Qz" for mark in "&?=()[]{}<>;,"),
)

PASSING_VALUES = ("a" * 40, "operations.pod.bootstrap_main", "/workspace/models/model.safetensors")

FILES_AND_HOSTS = (
    "/workspace/runs/report-" + "0123456789abcdef" * 2 + ".json",
    "/workspace/models/model-00001-of-00004.safetensors",
    "evidence-" + "0123456789abcdef" * 2 + ".tar.gz",
    "qwen3-27b-instruct.Q4_K_M.gguf",
    "abc123def456ghi789.proxy.runpod.net",
    OPAQUE + ".json",
)


@pytest.mark.parametrize("value", SHAPED_VALUES)
def test_a_credential_shape_is_caught(value: str) -> None:
    assert looks_like_credential_value(value)
    assert looks_like_credential_value(f"started ({value}) ok")


@pytest.mark.parametrize("value", SHAPED_VALUES)
def test_a_credential_shape_is_caught_where_files_and_hosts_pass(value: str) -> None:
    assert looks_like_credential_value(value, files_and_hosts_pass=True)


@pytest.mark.parametrize("value", PASSING_VALUES)
def test_an_identifier_passes(value: str) -> None:
    assert not looks_like_credential_value(value)


@pytest.mark.parametrize("value", FILES_AND_HOSTS)
def test_a_file_or_host_passes_only_when_the_caller_asks(value: str) -> None:
    assert looks_like_credential_value(value)
    assert not looks_like_credential_value(value, files_and_hosts_pass=True)


# --- differential: every caller's new check catches everything its pre-change check caught --
# The oracles below are the pre-change functions, copied from origin/main before the merge
# of common/credentials.py (operations/pod/models.py, notify_hooks.py, fixture.py and
# operations/serving/manager.py).

import random  # noqa: E402
import re  # noqa: E402

from common.credentials import (  # noqa: E402
    argv_credential_piece,
    fixture_value_carries_credential,
    log_word_carries_credential,
    looks_like_credential_field,
    notification_carries_credential,
)

_OLD_PREFIXES = ("sk-", "hf_", "ghp_", "gho_", "github_pat_", "AKIA", "xox")
_OLD_SAFE = frozenset(" /\\.:@")


def old_models_value(value: str) -> bool:
    if value.startswith(_OLD_PREFIXES):
        return True
    if len(value) < 20 or any(character in _OLD_SAFE for character in value):
        return False
    if all(character in "0123456789abcdef" for character in value):
        return False
    return any(character.isalpha() for character in value) and any(
        character.isdigit() for character in value
    )


_OLD_WORD_MARKERS = ("key", "secret", "password", "credential", "bearer", "token", "apikey")
_OLD_NOTIFY_SAFE = frozenset(" \t\\,;()[]{}'\"")


def old_notify_word(word: str) -> bool:
    normalized = word.lower().replace("-", "_")
    if any(marker in normalized for marker in _OLD_WORD_MARKERS):
        return True
    if word.startswith(_OLD_PREFIXES):
        return True
    if len(word) < 20 or any(character in _OLD_NOTIFY_SAFE for character in word):
        return False
    if all(character in "0123456789abcdef" for character in word):
        return False
    return any(character.isalpha() for character in word) and any(
        character.isdigit() for character in word
    )


def old_notify(message: str) -> bool:
    for word in message.split():
        stripped = word.strip("\"'(),;:")
        if stripped and old_notify_word(stripped):
            return True
    return False


def old_fixture(value: str) -> bool:
    if old_models_value(value):
        return True
    return any(
        stripped and old_models_value(stripped)
        for stripped in (word.strip("\"'(),;:") for word in value.split())
    )


def old_argv(value: str) -> bool:
    return bool(value) and (looks_like_credential_field(value) or old_models_value(value))


_OLD_TOKEN_PARTS = re.compile(r"""[^\s"'{}\[\],;:=]+""")


def old_redacts(token: str) -> bool:
    if old_models_value(token):
        return True
    redacted = _OLD_TOKEN_PARTS.sub(
        lambda part: "[redacted]" if old_models_value(part.group(0)) else part.group(0), token
    )
    return redacted != token


def _corpus() -> list[str]:
    listed = [
        *SHAPED_VALUES,
        *FILES_AND_HOSTS,
        *PASSING_VALUES,
        "Tr0ub4dor&3xK9pLm2Qz=out.json",
        "path\\to=Tr0ub4dor&3xK9pLm2Qz",
        "deadbeef01.cafebabe02",
        "https://user:" + OPAQUE + ".json@example.invalid/x",
        f"https://example.invalid/x?key={OPAQUE}.json",
    ]
    for length in range(17, 20):
        run = ("aB3xK9pLm2Qz7Tr0ub4dor" * 2)[:length]
        listed += [f"{a}{run}{b}" for a, b in ("()", ",,", ";;", '""', "''", "(,", "';")]
    generator = random.Random(20260925)
    alphabet = "abcdefXYZ0123456789" + ".:/@=&?()[]{}<>;,'\"\\-_+ " + "\t"
    for _ in range(20_000):
        listed.append("".join(generator.choice(alphabet) for _ in range(generator.randint(1, 60))))
    return listed


CORPUS = _corpus()


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (old_notify, notification_carries_credential),
        (old_fixture, fixture_value_carries_credential),
        (
            old_argv,
            lambda v: looks_like_credential_field(v) or argv_credential_piece(v) is not None,
        ),
    ],
    ids=["notify", "fixture", "argv"],
)
def test_no_caller_lets_through_what_its_old_check_caught(old, new) -> None:  # type: ignore[no-untyped-def]
    missed = [value for value in CORPUS if old(value) and not new(value)]
    assert missed == []


def test_log_redaction_lets_through_nothing_its_old_check_caught() -> None:
    words = [word for value in CORPUS for word in value.split()]
    missed = [word for word in words if old_redacts(word) and not log_word_carries_credential(word)]
    assert missed == []


@pytest.mark.parametrize(
    "value",
    [
        "https://user:" + OPAQUE + ".json@example.invalid/x",
        f"https://h.invalid/x?key={OPAQUE}.json",
    ],
)
def test_a_url_password_or_query_value_is_never_a_file_name(value: str) -> None:
    assert argv_credential_piece(value) is not None
    assert log_word_carries_credential(value)
