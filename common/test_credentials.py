import pytest

from common.credentials import (
    argv_credential_piece,
    log_word_carries_credential,
    looks_like_credential_value,
)

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

TIMESTAMPS_AND_MODEL_IDS = ("2026-09-25T10:00:00Z", "Qwen/Qwen3-27B-Instruct")

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


@pytest.mark.parametrize("value", TIMESTAMPS_AND_MODEL_IDS)
def test_a_timestamp_or_model_id_is_data(value: str) -> None:
    assert not looks_like_credential_value(value)
    assert not looks_like_credential_value(value, files_and_hosts_pass=True)


@pytest.mark.parametrize("value", FILES_AND_HOSTS)
def test_a_file_or_host_passes_only_when_the_caller_asks(value: str) -> None:
    assert looks_like_credential_value(value)
    assert not looks_like_credential_value(value, files_and_hosts_pass=True)


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
