"""Small, injected HTTP boundary for loopback vLLM checks and requests."""

from __future__ import annotations

import errno
import hashlib
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .errors import ReadinessError, ServingConfigurationError


class EndpointUnavailable(OSError):
    """A loopback request did not produce an HTTP response.

    ``definitively_absent`` is intentionally narrower than "the request did
    not work".  A refused TCP connection proves no listener owned that exact
    loopback port at that instant; a timeout, reset, or malformed local route
    does not.  Readiness can retry either condition, but the sequential lease
    may be released only after the former.
    """

    def __init__(self, detail: str, *, definitively_absent: bool = False) -> None:
        self.definitively_absent = definitively_absent
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """A raw HTTP response; parsing remains explicit at the caller."""

    status: int
    body: bytes


class HttpTransport(Protocol):
    """The only HTTP effect the manager needs; tests use scripted responses."""

    def request(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None,
        timeout_seconds: float,
    ) -> HttpResponse:
        """Make one bounded request or raise :class:`EndpointUnavailable`."""


# Every legitimate response here (health/models/chat-completions) is a small,
# local, well-known shape.  A response past this bound is refused rather than
# buffered whole into memory.
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse a 3xx rather than follow it.

    Every URL this module builds is provably ``127.0.0.1`` at construction
    time (``models_url``/``health_url``/``endpoint_for_probe``), but the
    stdlib's default opener follows a redirect Location header to *any* host
    with no same-origin check.  A loopback process that redirects — buggy,
    compromised, or racing this manager for the port — must not be able to
    make a readiness/health/inference probe silently answered by somewhere
    else.  Declining here surfaces the 3xx as an ordinary non-200 response.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler)


class UrllibHttpTransport:
    """Stdlib production transport; it contains no provider/model-host behavior."""

    def request(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None,
        timeout_seconds: float,
    ) -> HttpResponse:
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with _NO_REDIRECT_OPENER.open(request, timeout=timeout_seconds) as response:
                return HttpResponse(int(response.status), _bounded_read(response))
        except urllib.error.HTTPError as error:
            return HttpResponse(int(error.code), _bounded_read(error))
        except (OSError, urllib.error.URLError) as error:
            raise EndpointUnavailable(
                f"{method} {url}: {type(error).__name__}: {error}",
                definitively_absent=_connection_refused(error),
            ) from error


@dataclass(frozen=True, slots=True)
class OpenAIResult:
    """A structurally parsed, non-empty OpenAI-compatible answer."""

    model_id: str
    outputs: tuple[str, ...]
    response_sha256: str


def models_url(endpoint: str) -> str:
    return endpoint.rstrip("/") + "/models"


def health_url(endpoint: str) -> str:
    normalized = endpoint.rstrip("/")
    root = normalized[:-3] if normalized.endswith("/v1") else normalized
    return root.rstrip("/") + "/health"


def endpoint_for_probe(endpoint: str, kind: str) -> str:
    if kind == "chat-completions":
        return endpoint.rstrip("/") + "/chat/completions"
    if kind == "completions":
        return endpoint.rstrip("/") + "/completions"
    raise ServingConfigurationError(f"unknown OpenAI probe kind {kind!r}")


def parse_model_ids(response: HttpResponse) -> tuple[str, ...]:
    """Require the vLLM OpenAI model-list structure, never raw-text matching."""

    if response.status != 200:
        raise ReadinessError(
            "VLLM_MODELS_HTTP_ERROR", f"/v1/models returned HTTP {response.status}"
        )
    payload = _json_object(response, "VLLM_MODELS_RESPONSE_INVALID")
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise ReadinessError("VLLM_MODELS_RESPONSE_INVALID", "/v1/models has no data list")
    ids = tuple(
        row["id"]
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"]
    )
    return ids


def require_exact_model_id(response: HttpResponse, expected: str) -> tuple[str, ...]:
    """Return IDs only when the expected API alias occurs exactly as one member."""

    ids = parse_model_ids(response)
    if expected not in ids:
        raise ReadinessError(
            "VLLM_MODEL_ID_MISSING",
            f"expected model id {expected!r}; endpoint advertised {list(ids)!r}",
        )
    return ids


def request_body(
    payload: Mapping[str, object], *, model_id: str, seed: int, deterministic: bool
) -> bytes:
    """Render one request without allowing callers to lie about its target model."""

    value = dict(payload)
    supplied = value.pop("model", None)
    if supplied is not None and supplied != model_id:
        raise ServingConfigurationError(
            f"request named model {supplied!r}, not this service's exact id {model_id!r}"
        )
    if "stream" in value:
        raise ServingConfigurationError(
            "stream is manager-owned; serving probes require a complete response"
        )
    value["model"] = model_id
    value["stream"] = False
    if deterministic:
        for field, expected in (("temperature", 0), ("seed", seed)):
            supplied = value.get(field)
            if supplied is not None and supplied != expected:
                raise ServingConfigurationError(
                    f"deterministic probe {field}={supplied!r}, expected {expected!r}"
                )
            value[field] = expected
    return _canonical_json(value)


def parse_openai_answer(
    response: HttpResponse, *, kind: str, expected_model_id: str
) -> OpenAIResult:
    """Accept only a successful, exact-model, non-empty answer response."""

    if response.status != 200:
        raise ReadinessError(
            "VLLM_PROBE_HTTP_ERROR", f"inference probe returned HTTP {response.status}"
        )
    payload = _json_object(response, "VLLM_PROBE_RESPONSE_INVALID")
    if payload.get("model") != expected_model_id:
        raise ReadinessError(
            "VLLM_PROBE_MODEL_MISMATCH",
            f"inference response model={payload.get('model')!r}, expected {expected_model_id!r}",
        )
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ReadinessError("VLLM_PROBE_RESPONSE_INVALID", "inference response has no choices")
    outputs: list[str] = []
    for choice in choices:
        if not isinstance(choice, dict):
            raise ReadinessError(
                "VLLM_PROBE_RESPONSE_INVALID", "inference response has a non-object choice"
            )
        if kind == "chat-completions":
            message = choice.get("message")
            content = message.get("content") if isinstance(message, dict) else None
        elif kind == "completions":
            content = choice.get("text")
        else:  # config validates this before launch; keep the boundary closed.
            raise ServingConfigurationError(f"unknown OpenAI probe kind {kind!r}")
        if not isinstance(content, str) or not content.strip():
            raise ReadinessError(
                "VLLM_PROBE_RESPONSE_INVALID", "inference response has no non-empty text"
            )
        outputs.append(content)
    return OpenAIResult(
        model_id=expected_model_id,
        outputs=tuple(outputs),
        response_sha256=hashlib.sha256(response.body).hexdigest(),
    )


def outputs_sha256(result: OpenAIResult) -> str:
    """Digest semantic outputs, excluding volatile request/response metadata."""

    return hashlib.sha256(_canonical_json(list(result.outputs))).hexdigest()


def _json_object(response: HttpResponse, code: str) -> dict[str, Any]:
    try:
        value = json.loads(response.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReadinessError(code, f"response is not JSON: {error}") from error
    if not isinstance(value, dict):
        raise ReadinessError(code, "response is not a JSON object")
    return value


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _connection_refused(error: OSError) -> bool:
    """Return true only for the TCP condition that proves no listener exists."""

    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(reason, ConnectionRefusedError):
        return True
    return getattr(reason, "errno", None) == errno.ECONNREFUSED


def _bounded_read(response: Any) -> bytes:
    """Read at most ``_MAX_RESPONSE_BYTES``; an oversized body is refused, not buffered.

    The extra byte requested past the bound is what turns "read a lot" into a
    detectable overage rather than a response that merely happens to be
    exactly at the limit.
    """

    data = response.read(_MAX_RESPONSE_BYTES + 1)
    if len(data) > _MAX_RESPONSE_BYTES:
        raise EndpointUnavailable(
            f"response exceeded the {_MAX_RESPONSE_BYTES}-byte bound for a loopback serving check",
            definitively_absent=False,
        )
    return data
