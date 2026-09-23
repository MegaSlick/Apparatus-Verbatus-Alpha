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
    serve_and_run,
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

    proxy = _CompletionProxy(timeout, ledger, (FakeTimeout,), admission, lambda _text: False)
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
        lambda _text: False,
        api_key="unused",
        _isolation_timeout_seconds=90,
    )
    response = client.chat.completions.create(model="attestator-1-chandra", messages=[])

    assert response.model == "attestator-1-chandra"
    assert captured["max_retries"] == 0
    assert captured["timeout"] == 90
    assert (tmp_path / "research" / "reading-01-response.raw").read_bytes() == RawResponse.content


def test_parse_failure_retains_received_bytes_and_aborts_retry(tmp_path):
    ledger = ReadingLedger.create(tmp_path / "research")
    admission = _DeadlineAdmission(
        ledger,
        datetime(2026, 9, 22, 3, 0, tzinfo=UTC),
        minimum_attempt_seconds=60,
        now=lambda: datetime(2026, 9, 22, 2, 0, tzinfo=UTC),
    )
    raw_body = b'{"choices":'

    class RawResponse:
        content = raw_body

        def parse(self):
            raise ValueError("truncated response")

    class Client:
        models = object()
        with_raw_response = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=lambda **_request: RawResponse())
            )
        )

    client = _NoRetryOpenAI(
        lambda **_kwargs: Client(),
        ledger,
        (),
        admission,
        lambda _text: False,
        api_key="unused",
        _isolation_timeout_seconds=90,
    )
    with pytest.raises(DurabilityRefusal, match="could not be retained"):
        client.chat.completions.create(model="attestator-1-chandra", messages=[])

    retained = json.loads((tmp_path / "research" / "reading-ledger.json").read_text())
    assert retained["entries"][0]["state"] == "received-unparseable"
    assert (tmp_path / "research" / "reading-01-response.raw").read_bytes() == raw_body


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("shape", "could not be classified normally"),
        ("detector", "could not retain native response evidence"),
    ],
)
def test_classification_failure_retains_received_bytes_and_aborts_retry(
    tmp_path, failure, message
):
    ledger = ReadingLedger.create(tmp_path / "research")
    admission = _DeadlineAdmission(
        ledger,
        datetime(2026, 9, 22, 3, 0, tzinfo=UTC),
        minimum_attempt_seconds=60,
        now=lambda: datetime(2026, 9, 22, 2, 0, tzinfo=UTC),
    )
    raw_body = b'{"choices":[{"message":{"content":"native"}}]}'
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=None if failure == "shape" else "native")
            )
        ],
        usage=SimpleNamespace(completion_tokens=1),
        model="attestator-1-chandra",
        _native_raw_body=raw_body,
    )

    def refuse_classification(_text):
        if failure == "detector":
            raise ValueError("detector refused response")
        return False

    proxy = _CompletionProxy(
        lambda **_request: response,
        ledger,
        (),
        admission,
        refuse_classification,
    )
    with pytest.raises(DurabilityRefusal, match=message):
        proxy.create(model="attestator-1-chandra", messages=[])

    retained = json.loads((tmp_path / "research" / "reading-ledger.json").read_text())
    assert retained["entries"][0]["state"] == "received-unclassifiable"
    assert (tmp_path / "research" / "reading-01-response.raw").read_bytes() == raw_body


def test_nonbytes_response_records_received_without_claiming_raw_retention(tmp_path):
    ledger = ReadingLedger.create(tmp_path / "research")
    admission = _DeadlineAdmission(
        ledger,
        datetime(2026, 9, 22, 3, 0, tzinfo=UTC),
        minimum_attempt_seconds=60,
        now=lambda: datetime(2026, 9, 22, 2, 0, tzinfo=UTC),
    )

    class RawResponse:
        content = "not-original-bytes"

    class Client:
        models = object()
        with_raw_response = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=lambda **_request: RawResponse())
            )
        )

    client = _NoRetryOpenAI(
        lambda **_kwargs: Client(),
        ledger,
        (),
        admission,
        lambda _text: False,
        api_key="unused",
        _isolation_timeout_seconds=90,
    )
    with pytest.raises(DurabilityRefusal, match="could not be retained"):
        client.chat.completions.create(model="attestator-1-chandra", messages=[])

    retained = json.loads((tmp_path / "research" / "reading-ledger.json").read_text())
    assert retained["entries"][0]["state"] == "received-unretained"
    assert "response_sha256" not in retained["entries"][0]
    assert not (tmp_path / "research" / "reading-01-response.raw").exists()


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


def test_serve_and_run_uses_started_endpoint_and_always_stops(tmp_path, monkeypatch):
    from common.chairs import receipts as receipt_module
    from common.chairs import registry as registry_module
    from operations.pod import preflight as preflight_module
    from operations.serving import config as config_module
    from operations.serving import manager as manager_module
    from session_verification import chandra_native_isolation as harness

    page = tmp_path / "page.tif"
    page.write_bytes(b"approved-page")
    monkeypatch.setattr(harness, "PAGE_SHA256", harness._sha256_path(page))
    monkeypatch.setattr(harness, "_require_upstream", lambda _root: {"clean": True})
    monkeypatch.setattr(
        receipt_module,
        "receipt_record",
        lambda _receipt: {"schema": "fake-serving-receipt.v1"},
    )

    class FakeRegistry:
        @classmethod
        def from_toml(cls, _path, *, cache_root):
            assert cache_root == tmp_path / "cache"
            return cls()

        def resolve(self, role):
            assert role == "attestator_1"
            return SimpleNamespace(revision=harness.MODEL_REVISION)

    monkeypatch.setattr(registry_module, "ChairRegistry", FakeRegistry)
    monkeypatch.setattr(
        config_module,
        "load_serving_recipes",
        lambda _path: SimpleNamespace(source_sha256="a" * 64),
    )
    monkeypatch.setattr(
        preflight_module,
        "load_placement_table",
        lambda _path, *, source_bytes: {"source_bytes": source_bytes},
    )

    managers = []

    class FakeManager:
        def __init__(self, **kwargs):
            self.publisher = kwargs["receipt_publisher"]
            self.stopped = []
            managers.append(self)

        def start(self, identity, tier):
            assert identity.revision == harness.MODEL_REVISION
            assert tier == "generic-80gb-plus"
            self.publisher.publish(object(), {"event": "started"})
            return SimpleNamespace(endpoint="http://127.0.0.1:8999/v1")

        def stop(self, handle):
            self.stopped.append(handle)

    monkeypatch.setattr(manager_module, "ServingManager", FakeManager)
    observed = {}

    def refuse_run(arguments):
        observed["arguments"] = arguments
        raise DurabilityRefusal("bounded fake run refusal")

    monkeypatch.setattr(harness, "run_native", refuse_run)
    recipes = tmp_path / "recipes.toml"
    recipes.write_text(
        '[[profiles]]\nchair = "attestator_1"\ntier = "generic-80gb-plus"\n'
        "max_model_len = 18000\nstartup_timeout_seconds = 300\n"
    )
    placement = tmp_path / "placement.toml"
    placement.write_text("schema = 'pod-placement.v1'\n")
    arguments = SimpleNamespace(
        page=page,
        upstream_root=tmp_path / "upstream",
        diagnostic_root=tmp_path / "diagnostic",
        recipes_config=recipes,
        placement_config=placement,
        models_config=tmp_path / "models.toml",
        cache_root=tmp_path / "cache",
        served_model_id="attestator-1-chandra",
        evidence_root=tmp_path / "lifecycle",
        residency_lock=tmp_path / "residency.lock",
        vllm_api_base="http://caller-controlled.invalid/v1",
    )

    with pytest.raises(DurabilityRefusal, match="fake run refusal"):
        serve_and_run(arguments)

    assert observed["arguments"].vllm_api_base == "http://127.0.0.1:8999/v1"
    assert arguments.vllm_api_base == "http://caller-controlled.invalid/v1"
    assert len(managers) == 1
    assert managers[0].stopped == [SimpleNamespace(endpoint="http://127.0.0.1:8999/v1")]
    assert (tmp_path / "lifecycle" / "serving-receipt.json").exists()
    assert (tmp_path / "lifecycle" / "serving-launch-audit.json").exists()
    assert (tmp_path / "lifecycle" / "serving-evidence.json").exists()


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
    transport_failure = False

    class FakeTimeout(Exception):
        pass

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
            if transport_failure:
                raise FakeTimeout("delivery may have reached the server")
            return RawResponse(len(attempts))

    def fake_openai(**_kwargs):
        return FakeClient()

    fake_module = types.ModuleType("openai")
    fake_module.OpenAI = fake_openai
    fake_module.APITimeoutError = FakeTimeout
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
    assert [item["temperature"] for item in attempts] == [
        0.0,
        0.2,
        0.4,
        0.6000000000000001,
        0.8,
        0.8,
        0.8,
    ]
    repeated_summary = json.loads(
        (tmp_path / "repeated-evidence" / "summary.json").read_text()
    )
    assert repeated_summary["terminal"] == "repeat-exhausted"
    assert repeated_summary["returned_attempt_ordinal"] == 7

    attempts.clear()
    transport_failure = True
    arguments.output = tmp_path / "ambiguous-evidence"
    result = harness.run_native(arguments)

    assert result == 3
    assert len(attempts) == 1
    ambiguous = json.loads(
        (tmp_path / "ambiguous-evidence" / "reading-ledger.json").read_text()
    )
    assert ambiguous["entries"][0]["state"] == "delivery-unknown"
