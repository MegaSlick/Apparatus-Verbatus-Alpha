"""Offline contract tests for the private Chandra isolation harness."""

import base64
import io
import json
import os
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from session_verification.chandra_native_isolation import (
    DeliveryUnknown,
    DurabilityRefusal,
    LedgerExhausted,
    ReadingLedger,
    _CompletionProxy,
    _DeadlineAdmission,
    _NoRetryOpenAI,
    _repeat_trigger,
    _write_diagnostic_catalogue,
)


def test_reading_ledger_counts_terminal_calls_against_the_seven_call_cap(tmp_path):
    ledger = ReadingLedger.create(tmp_path / "research")
    for ordinal in range(1, 8):
        actual = ledger.intent({"ordinal": ordinal})
        ledger.terminal(actual, state="received", metadata={"completion_tokens": 1})

    with pytest.raises(LedgerExhausted, match="cap is 7"):
        ledger.intent({"ordinal": 8})

    retained = json.loads((tmp_path / "research" / "reading-ledger.json").read_text())
    assert [entry["state"] for entry in retained["entries"]] == ["received"] * 7


def test_reading_ledger_writes_intent_before_the_terminal_result(tmp_path):
    ledger = ReadingLedger.create(tmp_path / "research")
    ordinal = ledger.intent({"model": "attestator-1-chandra", "messages": []})

    persisted = json.loads((tmp_path / "research" / "reading-ledger.json").read_text())
    assert len(persisted["entries"]) == 1
    entry = persisted["entries"][0]
    assert entry["ordinal"] == ordinal
    assert entry["state"] == "intent"
    assert isinstance(entry["at"], str)
    assert len(entry["request_sha256"]) == 64
    assert (tmp_path / "research" / "reading-01-request.json").exists()


def test_deadline_admission_retains_a_hold_without_spending_a_page_reading(tmp_path):
    ledger = ReadingLedger.create(tmp_path / "research")
    observed = datetime(2026, 9, 22, 2, 0, tzinfo=UTC)
    admission = _DeadlineAdmission(
        ledger,
        observed + timedelta(seconds=60),
        minimum_attempt_seconds=60,
        now=lambda: observed,
    )

    with pytest.raises(LedgerExhausted, match="insufficient budget"):
        admission.admit()

    assert ledger.entries == []
    hold = json.loads((tmp_path / "research" / "reading-01-hold.json").read_text())
    assert hold["reason"] == "deadline-admission-denied"
    assert hold["page_readings_completed"] == 0


def test_ambiguous_transport_escapes_the_upstream_exception_retry_boundary(tmp_path):
    class FakeTimeout(Exception):
        pass

    ledger = ReadingLedger.create(tmp_path / "research")
    admission = _DeadlineAdmission(
        ledger,
        datetime(2026, 9, 22, 3, 0, tzinfo=UTC),
        minimum_attempt_seconds=1,
        now=lambda: datetime(2026, 9, 22, 2, 0, tzinfo=UTC),
    )

    def timeout(**_request):
        raise FakeTimeout("delivery may have reached the server")

    proxy = _CompletionProxy(timeout, ledger, (FakeTimeout,), admission)
    with pytest.raises(DeliveryUnknown, match="must not replay"):
        proxy.create(model="attestator-1-chandra", messages=[])

    retained = json.loads((tmp_path / "research" / "reading-ledger.json").read_text())
    assert retained["entries"][0]["state"] == "delivery-unknown"


def test_diagnostic_catalogue_changes_only_the_named_row_premises(tmp_path):
    source = tmp_path / "source.toml"
    source.write_text(
        "schema = 'serving-recipes.v1'\n"
        '[[profiles]]\nchair = "attestator_1"\ntier = "generic-80gb-plus"\n'
        "max_model_len = 18000\nstartup_timeout_seconds = 300\n"
        '[[profiles]]\nchair = "attestator_2"\ntier = "generic-80gb-plus"\n'
        "max_model_len = 18000\nstartup_timeout_seconds = 300\n"
    )
    destination = tmp_path / "diagnostic.toml"
    _write_diagnostic_catalogue(source, destination)

    rendered = destination.read_text()
    assert rendered.count("max_model_len = 20480") == 1
    assert rendered.count("startup_timeout_seconds = 600") == 1
    assert rendered.count("max_model_len = 18000") == 1
    assert rendered.count("startup_timeout_seconds = 300") == 1


def test_sdk_wrapper_forces_timeout_and_retains_original_raw_response(tmp_path):
    ledger = ReadingLedger.create(tmp_path / "research")
    admission = _DeadlineAdmission(
        ledger,
        datetime(2026, 9, 22, 3, 0, tzinfo=UTC),
        minimum_attempt_seconds=60,
        now=lambda: datetime(2026, 9, 22, 2, 0, tzinfo=UTC),
    )
    captured = {}

    class Parsed:
        model = "attestator-1-chandra"
        usage = SimpleNamespace(completion_tokens=3)

    class RawResponse:
        content = b'{"choices":[{"message":{"content":"native"}}]}'

        def parse(self):
            return Parsed()

    def raw_create(**_request):
        return RawResponse()

    class Client:
        models = object()
        with_raw_response = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=raw_create))
        )

    def original(**kwargs):
        captured.update(kwargs)
        return Client()

    client = _NoRetryOpenAI(
        original,
        ledger,
        (),
        admission,
        api_key="unused",
        _isolation_timeout_seconds=90,
    )
    response = client.chat.completions.create(model="attestator-1-chandra", messages=[])

    assert response.model == "attestator-1-chandra"
    assert captured["max_retries"] == 0
    assert captured["timeout"] == 90
    assert (tmp_path / "research" / "reading-01-response.raw").read_bytes() == RawResponse.content


def test_terminal_publication_refusal_cannot_be_retried_as_a_native_error(tmp_path, monkeypatch):
    ledger = ReadingLedger.create(tmp_path / "research")
    admission = _DeadlineAdmission(
        ledger,
        datetime(2026, 9, 22, 3, 0, tzinfo=UTC),
        minimum_attempt_seconds=60,
        now=lambda: datetime(2026, 9, 22, 2, 0, tzinfo=UTC),
    )
    calls = 0

    class Response:
        choices = [SimpleNamespace(message=SimpleNamespace(content="native"))]
        usage = SimpleNamespace(completion_tokens=1)
        model = "attestator-1-chandra"
        _native_raw_body = b'{"native":true}'

    def create(**_request):
        nonlocal calls
        calls += 1
        return Response()

    def refuse_terminal(*_args, **_kwargs):
        raise DurabilityRefusal("directory fsync refused")

    monkeypatch.setattr(ledger, "terminal", refuse_terminal)
    proxy = _CompletionProxy(create, ledger, (), admission, lambda _text: False)
    with pytest.raises(DurabilityRefusal, match="fsync refused"):
        proxy.create(model="attestator-1-chandra", messages=[])

    assert calls == 1


def test_repeat_trigger_matches_the_vendor_end_cut_predicate():
    calls = []

    def detector(text, cut_from_end=0):
        calls.append((text, cut_from_end))
        return cut_from_end == 50

    assert _repeat_trigger("x" * 51, detector) is True
    assert calls == [("x" * 51, 0), ("x" * 51, 50)]


@pytest.mark.skipif(
    not os.environ.get("CHANDRA_UPSTREAM_TEST_ROOT"),
    reason="requires a separately fetched, exact upstream Chandra checkout",
)
def test_pinned_upstream_retries_real_rgb_request_with_fake_sdk(tmp_path, monkeypatch):
    """Exercise upstream generate_vllm, not merely this harness's proxy."""
    from PIL import Image

    from session_verification import chandra_native_isolation as harness

    upstream_root = Path(os.environ["CHANDRA_UPSTREAM_TEST_ROOT"])
    page = tmp_path / "synthetic.tif"
    Image.new("L", (4, 4), 1).save(page)
    monkeypatch.setattr(harness, "PAGE_SHA256", harness._sha256_path(page))
    attempts = []
    replies = ["x" * 30, "native answer"]

    class Parsed:
        def __init__(self, content):
            self.choices = [SimpleNamespace(message=SimpleNamespace(content=content))]
            self.usage = SimpleNamespace(completion_tokens=7)
            self.model = "attestator-1-chandra"

    class RawResponse:
        def __init__(self, ordinal):
            self.content = f'{{"attempt":{ordinal}}}'.encode()
            self._ordinal = ordinal

        def parse(self):
            return Parsed(replies[self._ordinal - 1])

    class FakeClient:
        models = object()

        def __init__(self):
            self.with_raw_response = SimpleNamespace(
                chat=SimpleNamespace(
                    completions=SimpleNamespace(create=self.create)
                )
            )

        def create(self, **request):
            attempts.append(request)
            return RawResponse(len(attempts))

    def fake_openai(**_kwargs):
        return FakeClient()

    fake_module = types.ModuleType("openai")
    fake_module.OpenAI = fake_openai
    fake_module.APITimeoutError = type("APITimeoutError", (Exception,), {})
    fake_module.APIConnectionError = type("APIConnectionError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "openai", fake_module)
    for module_name in list(sys.modules):
        if module_name == "chandra" or module_name.startswith("chandra."):
            monkeypatch.delitem(sys.modules, module_name, raising=False)

    arguments = SimpleNamespace(
        page=page,
        upstream_root=upstream_root,
        output=tmp_path / "evidence",
        deadline_utc="2099-01-01T00:00:00Z",
        minimum_attempt_seconds=120,
        sdk_timeout_seconds=90,
        served_model_id="attestator-1-chandra",
        prompt_type="ocr_layout",
        vllm_api_base="http://127.0.0.1:8102/v1",
    )
    result = harness.run_native(arguments)

    assert result == 0
    assert [(item["temperature"], item["top_p"]) for item in attempts] == [
        (0.0, 0.1),
        (0.2, 0.95),
    ]
    image_url = attempts[0]["messages"][0]["content"][0]["image_url"]["url"]
    png = base64.b64decode(image_url.split(",", 1)[1])
    with Image.open(io.BytesIO(png)) as converted:
        assert converted.mode == "RGB"
    assert (tmp_path / "evidence" / "reading-01-response.raw").read_bytes() == b'{"attempt":1}'
    assert (tmp_path / "evidence" / "reading-02-response.raw").read_bytes() == b'{"attempt":2}'
    summary = json.loads((tmp_path / "evidence" / "summary.json").read_text())
    assert summary["terminal"] == "received"
    assert summary["returned_attempt_ordinal"] == 2

    attempts.clear()
    replies[:] = ["x" * 30] * 7
    arguments.output = tmp_path / "repeated-evidence"
    result = harness.run_native(arguments)

    assert result == 2
    assert len(attempts) == 7
    assert [item["temperature"] for item in attempts] == [0.0, 0.2, 0.4, 0.6, 0.8, 0.8, 0.8]
    repeated_summary = json.loads(
        (tmp_path / "repeated-evidence" / "summary.json").read_text()
    )
    assert repeated_summary["terminal"] == "repeat-exhausted"
    assert repeated_summary["returned_attempt_ordinal"] == 7
