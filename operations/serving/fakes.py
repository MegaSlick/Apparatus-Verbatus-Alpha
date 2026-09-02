"""A shared fake serving endpoint for stage tests built against :class:`ChairClient`.

Mirrors of :mod:`operations.serving.test_manager`'s own fakes (``FakeHttp``,
``FakeLauncher``, ``FakeProcess``, ``FakePackages``, ``FakeRegistry``) — not
moved from there, so that 4,500-line manager-lifecycle suite stays untouched.
Attestatores and Perlector stage tests both need one scripted endpoint that
speaks the reading contract; deduplicating the two families of fakes is a
named follow-on, not a job this module does.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from common.chairs.errors import ServingRecipeRefusal
from common.chairs.models import ChairIdentity, ServingDetails, VerifiedSnapshot
from common.chairs.receipts import build_receipt

from .client import ChairClient, RetainBytes
from .http import EndpointUnavailable, HttpResponse
from .manager import AdapterCalibration, ReceiptPublication, ServingManager


class _Absent:
    """The sentinel for a ``finish_reason`` key omitted from the wire entirely.

    Distinct from ``None``: passing ``None`` scripts an explicit JSON
    ``null``, while ``ABSENT`` scripts a response whose ``choices[0]`` carries
    no ``finish_reason`` key at all. :func:`operations.serving.http._finish_reason`
    treats both the same way (verbatim absence, never a default) — the fake
    lets one test prove that even though the two wire shapes differ.
    """

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return "ABSENT"


ABSENT: Any = _Absent()


@dataclass(frozen=True, slots=True)
class ScriptedAnswer:
    """One scripted reply for the next reading POST the fake endpoint receives.

    ``body``, when given, overrides ``content``/``finish_reason``/``usage``/
    ``model`` entirely and is returned as the raw response bytes verbatim —
    the shape a malformed-body test needs. Otherwise the fake builds one
    chat-completions choice from the other fields.
    """

    content: str | None = None
    finish_reason: Any = ABSENT
    usage: Mapping[str, int] | None = None
    model: str | None = None
    status: int = 200
    body: bytes | None = None


class FakeBlobStore:
    """A minimal content-addressed store: the client's ``retain`` and the
    fake endpoint's response-as-arrival check both point at one instance."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.written: list[bytes] = []

    def retain(self, data: bytes) -> dict[str, str]:
        sha256 = hashlib.sha256(data).hexdigest()
        directory = self.root / "blobs" / "sha256"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{sha256}.bin"
        path.write_bytes(data)
        self.written.append(data)
        return {"relative_path": f"blobs/sha256/{sha256}.bin", "sha256": sha256}

    def __len__(self) -> int:
        return len(self.written)


class FakeProcess:
    """A loopback-process shape only; no real subprocess is ever created."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.exit_code: int | None = None
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = 0

    def poll(self) -> int | None:
        return self.exit_code

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.exit_code = 0

    def kill(self) -> None:
        self.kill_calls += 1
        self.exit_code = -9

    def wait(self, timeout_seconds: float) -> int:
        del timeout_seconds
        self.wait_calls += 1
        if self.exit_code is None:
            raise TimeoutError("fake child is still live")
        return self.exit_code

    def read_tail(self, maximum_bytes: int = 16_384) -> str:
        del maximum_bytes
        return ""


class FakeEndpoint:
    """A scripted OpenAI-compatible loopback endpoint.

    Health and ``/models`` always answer ready, advertising ``served_model_id``.
    A manager's readiness probe is exactly one POST, made once inside
    ``ServingManager.start`` before any :class:`~operations.serving.client.ChairClient`
    reading is possible; this fake auto-answers that first POST (never
    consuming a scripted answer, never recorded in ``requests``) and treats
    every POST after it as a reading. Each of those pops the next
    :class:`ScriptedAnswer`, in order, and records the decoded request body in
    ``requests`` before it answers.
    """

    def __init__(
        self,
        *,
        served_model_id: str,
        blob_store: FakeBlobStore | None = None,
        assert_retained_before_next_request: bool = False,
    ) -> None:
        self.served_model_id = served_model_id
        self.blob_store = blob_store
        # Deliberately opt-in, not a blanket invariant: a non-200 or
        # wrong-model reading is refused with *no* blob written by design
        # (`ChairClient._refuse_bytes_from_the_wrong_source`), so a test
        # sequence that scripts one of those before a following read must not
        # trip this check. Set true only where every prior reading in the
        # sequence is expected to retain.
        self.assert_retained_before_next_request = assert_retained_before_next_request
        self._answers: list[ScriptedAnswer] = []
        self.requests: list[dict[str, object]] = []
        self._process: FakeProcess | None = None
        self._readiness_probe_answered = False

    def script(self, *answers: ScriptedAnswer) -> None:
        self._answers.extend(answers)

    def bind(self, process: FakeProcess) -> None:
        self._process = process

    def _available(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def request(
        self, method: str, url: str, *, body: bytes | None, timeout_seconds: float
    ) -> HttpResponse:
        del timeout_seconds
        if not self._available():
            # Before launch and after a verified stop, no listener owns this
            # loopback port — the exact TCP fact `_assert_endpoint_unoccupied`
            # and `_assert_endpoint_absent` both require to proceed.
            raise EndpointUnavailable(
                f"fake endpoint unavailable at {url}", definitively_absent=True
            )
        if url.endswith("/health"):
            return HttpResponse(200, b'{"status":"ok"}')
        if url.endswith("/models"):
            return HttpResponse(200, json.dumps({"data": [{"id": self.served_model_id}]}).encode())
        if method != "POST":
            return HttpResponse(404, b"{}")
        decoded = json.loads(body) if body is not None else None
        if not self._readiness_probe_answered:
            # `ServingManager.start` makes exactly one such POST, always
            # before a `ChairClient` can issue its first reading. Readiness
            # itself is proven elsewhere (operations/serving/test_manager.py);
            # this fake only needs it to succeed, and it must never consume a
            # scripted reading answer or pollute the reading-call count.
            self._readiness_probe_answered = True
            return self._auto_probe_response(decoded, url)
        if (
            self.requests
            and self.assert_retained_before_next_request
            and self.blob_store is not None
        ):
            # Response-as-arrival: the previous reading's raw bytes must
            # already be on disk before this, its second reading request,
            # ever reaches the endpoint.
            if len(self.blob_store) < len(self.requests):
                raise AssertionError(
                    "the prior reading's raw response was not retained before "
                    "the next reading request was sent"
                )
        self.requests.append(decoded)
        answer = self._answers.pop(0)
        if answer.body is not None:
            return HttpResponse(answer.status, answer.body)
        choice: dict[str, object] = {"message": {"content": answer.content}}
        if answer.finish_reason is not ABSENT:
            choice["finish_reason"] = answer.finish_reason
        payload: dict[str, object] = {
            "model": answer.model if answer.model is not None else self.served_model_id,
            "choices": [choice],
        }
        if answer.usage is not None:
            payload["usage"] = dict(answer.usage)
        return HttpResponse(answer.status, json.dumps(payload).encode())

    def _auto_probe_response(self, decoded: dict[str, object] | None, url: str) -> HttpResponse:
        model_id = decoded.get("model") if isinstance(decoded, dict) else None
        if url.endswith("/chat/completions"):
            choice: dict[str, object] = {"message": {"content": "ready"}}
        else:
            choice = {"text": "ready"}
        return HttpResponse(
            200,
            json.dumps({"model": model_id or self.served_model_id, "choices": [choice]}).encode(),
        )


class FakeLauncher:
    def __init__(self, endpoint: FakeEndpoint) -> None:
        self.endpoint = endpoint
        self.calls: list[tuple[tuple[str, ...], Path]] = []
        self.processes: list[FakeProcess] = []

    def launch(
        self,
        argv: tuple[str, ...],
        log_path: Path,
        *,
        inheritable_fds: tuple[int, ...] = (),
    ) -> FakeProcess:
        self.calls.append((argv, log_path))
        process = FakeProcess(9000 + len(self.processes))
        self.processes.append(process)
        self.endpoint.bind(process)
        return process


class FakePackages:
    def __init__(self, versions: Mapping[str, str]) -> None:
        self.versions = dict(versions)

    def version(self, package: str) -> str:
        return self.versions[package]


class FakeRegistry:
    def __init__(self, identities: Mapping[str, ChairIdentity], tmp_path: Path) -> None:
        self.identities = dict(identities)
        self.snapshots = {
            role: VerifiedSnapshot(chair_identity, tmp_path / role, chair_identity.digest_manifest)
            for role, chair_identity in identities.items()
        }

    def resolve(self, role: str) -> ChairIdentity:
        return self.identities[role]

    def ensure(self, identity: ChairIdentity) -> VerifiedSnapshot:
        return self.snapshots[identity.role]

    def receipt(self, identity: ChairIdentity, details: ServingDetails):
        return build_receipt(identity, details)

    def refuse_recipe_start(self, identity: ChairIdentity, difference: str) -> None:
        raise ServingRecipeRefusal(identity.role, difference)


class FakePublisher:
    """Publishes a receipt/audit/evidence triple content-addressed by the audit."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, Mapping[str, object]]] = []

    def publish(self, receipt: object, launch_audit: Mapping[str, object]) -> ReceiptPublication:
        self.calls.append((receipt, launch_audit))
        digest = hashlib.sha256(
            json.dumps(launch_audit, sort_keys=True, default=str).encode()
        ).hexdigest()
        return ReceiptPublication(
            {"relative_path": f"receipts/sha256/{digest}.json", "sha256": digest},
            {"relative_path": f"stages/blobs/sha256/{digest}-audit", "sha256": digest},
            {"relative_path": f"stages/blobs/sha256/{digest}-evidence", "sha256": digest},
        )


def fake_serving_factory(
    *,
    manager: ServingManager,
    retain: RetainBytes,
    decoding_config_sha256: str,
    read_receipt: Callable[[Mapping[str, str]], Mapping[str, object]],
    record_temperature: int = 0,
    adapter_calibration: AdapterCalibration | None = None,
) -> Callable[[Any, ChairIdentity, str], ChairClient]:
    """Build the ``serving_factory(context, chair, tier) -> ChairClient`` a
    stage's ``main`` calls under live mode, wired to one fake manager.

    ``context`` is accepted and ignored: production factories close over a
    real ``StageContext`` to build ``retain``/``read_receipt``, but this fake
    factory already has both, supplied directly by the test.
    """

    def factory(context: object, identity: ChairIdentity, tier: str) -> ChairClient:
        del context
        return ChairClient(
            manager=manager,
            identity=identity,
            tier=tier,
            retain=retain,
            decoding_config_sha256=decoding_config_sha256,
            record_temperature=record_temperature,
            read_receipt=read_receipt,
            adapter_calibration=adapter_calibration,
        )

    return factory
