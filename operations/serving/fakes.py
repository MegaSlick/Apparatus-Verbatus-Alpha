"""Fakes for serving tests: a scripted endpoint speaking the reading contract,
and the process, launcher, package and registry stand-ins a `ServingManager`
needs. Stage tests and the serving package's own tests share them.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from common.chairs.errors import ServingRecipeRefusal
from common.chairs.models import ChairIdentity, ModelsConfig, ServingDetails, VerifiedSnapshot
from common.chairs.receipts import build_receipt
from common.decoding import load_decoding_policy

from .client import ChairClient, RetainBytes
from .config import SubprocessProfile
from .errors import ProcessLaunchError
from .http import EndpointUnavailable, HttpResponse
from .manager import ReceiptPublication, ServingManager
from .surya_detector import SuryaRun, contract, declared_page_documents, surya_run


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
    usage: Mapping[str, object] | None = None
    model: str | None = None
    status: int = 200
    body: bytes | None = None
    transport_failure: str | None = None


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

    def has(self, sha256: str) -> bool:
        """True only when the exact digest named is already on disk."""

        return (self.root / "blobs" / "sha256" / f"{sha256}.bin").exists()

    def __len__(self) -> int:
        return len(self.written)


class FakeProcess:
    """A loopback-process shape only; no real subprocess is ever created."""

    def __init__(
        self,
        pid: int,
        *,
        log_tail: str = "",
        log_tails: tuple[str, ...] = (),
        exits_immediately: int | None = None,
        ignore_terminate: bool = False,
        ignore_kill: bool = False,
    ) -> None:
        self.pid = pid
        self.exit_code = exits_immediately
        self.log_tail = log_tail
        self.log_tails = log_tails
        self.tail_reads = 0
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = 0
        self.ignore_terminate = ignore_terminate
        self.ignore_kill = ignore_kill
        # Another member of this process's group (vLLM's engine) that outlives the
        # leader until the group is signalled.
        self.engine_outlives_leader = False

    @property
    def group_running(self) -> bool:
        return self.exit_code is None or self.engine_outlives_leader

    @property
    def start_marker(self) -> str | None:
        return f"fake-start-{self.pid}"

    def poll(self) -> int | None:
        return self.exit_code

    def terminate(self) -> None:
        self.terminate_calls += 1
        if not self.ignore_terminate:
            self.engine_outlives_leader = False
            if self.exit_code is None:
                self.exit_code = 0

    def kill(self) -> None:
        self.kill_calls += 1
        if not self.ignore_kill:
            self.engine_outlives_leader = False
            if self.exit_code is None:
                self.exit_code = -9

    def wait(self, timeout_seconds: float) -> int:
        del timeout_seconds
        self.wait_calls += 1
        if self.group_running:
            raise TimeoutError("fake child or its group is still live")
        return self.exit_code

    def read_tail(self, maximum_bytes: int = 16_384) -> str:
        # `log_tails` is a log that grows between reads, which is what a
        # loading engine's own log does: the readiness loop reads it once per
        # poll, and whether the progress line *moved* is what tells "still
        # loading" from "stuck at 43% since the first poll".
        if self.log_tails:
            tail = self.log_tails[min(self.tail_reads, len(self.log_tails) - 1)]
            self.tail_reads += 1
            return tail[-maximum_bytes:]
        return self.log_tail[-maximum_bytes:]


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
        sticky_after_stop: bool = False,
    ) -> None:
        self.served_model_id = served_model_id
        self.blob_store = blob_store
        # A stopped process whose endpoint keeps answering: the exact ambiguity
        # `ServingManager._assert_endpoint_absent` exists to catch. Set true
        # only where a test needs `handle.stop()` to fail.
        self.sticky_after_stop = sticky_after_stop
        # Opt-in, not a blanket invariant: a test may drive this endpoint
        # through a seam that never reaches `ChairClient.read` at all.
        self.assert_retained_before_next_request = assert_retained_before_next_request
        self._answers: list[ScriptedAnswer] = []
        self.requests: list[dict[str, object]] = []
        self._process: FakeProcess | None = None
        self._readiness_probe_answered = False
        # The exact raw body this fake last served as a reading answer — not
        # merely a count, so the check below can name the one blob that must
        # already be retained, not just how many blobs exist in total.
        self._last_served_reading_sha256: str | None = None
        self._served_answer: ScriptedAnswer | None = None
        self.streams_stopped = 0

    def script(self, *answers: ScriptedAnswer) -> None:
        self._answers.extend(answers)

    def bind(self, process: FakeProcess) -> None:
        self._process = process

    def _available(self) -> bool:
        return self._process is not None and (
            self._process.poll() is None or self.sticky_after_stop
        )

    def stream(
        self,
        method: str,
        url: str,
        *,
        body: bytes,
        timeout_seconds: float,
        on_chunk: Callable[[bytes], bool],
    ) -> HttpResponse:
        """The next scripted reading, served as server-sent events, one per line of its
        content, then its finish, its usage and `[DONE]`; stopped where `on_chunk` says.

        A scripted `body` on a 200 is served as given, one event at a time; any other
        status is one whole body, as vLLM refuses a streamed request it cannot take.
        `streams_stopped` counts the replies the client stopped.
        """
        response = self.request(method, url, body=body, timeout_seconds=timeout_seconds)
        if response.status != 200 or url.endswith(("/health", "/models")):
            return response
        answer = self._served_answer
        if answer is not None and answer.body is None:
            events = _stream_events(answer, self.served_model_id)
        else:
            events = [event + b"\n\n" for event in response.body.split(b"\n\n") if event]
        received = b""
        for event in events:
            received += event
            if on_chunk(event):
                self.streams_stopped += 1
                break
        self._last_served_reading_sha256 = hashlib.sha256(received).hexdigest()
        return HttpResponse(200, received)

    def request(
        self, method: str, url: str, *, body: bytes | None, timeout_seconds: float
    ) -> HttpResponse:
        del timeout_seconds
        self._served_answer = None
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
            self._last_served_reading_sha256 is not None
            and self.assert_retained_before_next_request
            and self.blob_store is not None
        ):
            # Response-as-arrival: the *exact* bytes this fake served as the
            # previous reading must already be on disk, by their own digest,
            # before this next reading request ever reaches the endpoint. A
            # blob count alone would be satisfied by any retention order (the
            # client also retains a call-record blob per read); naming the
            # digest is what actually pins retain-before-parse.
            if not self.blob_store.has(self._last_served_reading_sha256):
                raise AssertionError(
                    "the prior reading's raw response "
                    f"(sha256={self._last_served_reading_sha256}) was not retained "
                    "before the next reading request was sent"
                )
        self.requests.append(decoded)
        answer = self._answers.pop(0)
        self._served_answer = answer
        if answer.transport_failure is not None:
            raise EndpointUnavailable(answer.transport_failure)
        if answer.body is not None:
            body = answer.body
        else:
            choice: dict[str, object] = {"message": {"content": answer.content}}
            if answer.finish_reason is not ABSENT:
                choice["finish_reason"] = answer.finish_reason
            payload: dict[str, object] = {
                "model": answer.model if answer.model is not None else self.served_model_id,
                "choices": [choice],
            }
            if answer.usage is not None:
                payload["usage"] = dict(answer.usage)
            body = json.dumps(payload).encode()
        # Every body this fake serves as a reading is retained by
        # `ChairClient.read`, whatever its status and whatever model it names:
        # retention happens before the wrong-source check, so a 400 explaining
        # a context overflow reaches disk before it is refused. The fake
        # therefore predicts retention for all of them.
        self._last_served_reading_sha256 = hashlib.sha256(body).hexdigest()
        return HttpResponse(answer.status, body)

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


def _stream_events(answer: ScriptedAnswer, served_model_id: str) -> list[bytes]:
    """One scripted answer as vLLM streams it: a chunk per line of its content, the
    finish reason on the last, a usage chunk when scripted, then `[DONE]`."""

    model = answer.model if answer.model is not None else served_model_id
    content = answer.content or ""
    pieces = content.splitlines(keepends=True) or [content]
    events = []
    for index, piece in enumerate(pieces):
        choice: dict[str, object] = {"index": 0, "delta": {"content": piece}}
        if index == len(pieces) - 1 and answer.finish_reason is not ABSENT:
            choice["finish_reason"] = answer.finish_reason
        events.append({"model": model, "choices": [choice]})
    if answer.usage is not None:
        events.append({"model": model, "choices": [], "usage": dict(answer.usage)})
    return [b"data: " + json.dumps(event).encode() + b"\n\n" for event in events] + [
        b"data: [DONE]\n\n"
    ]


# --------------------------- an engine's context refusals ---------------------------


def scripted_prompt_too_long(
    *,
    max_model_len: int,
    requested_tokens: int,
    prompt_tokens: int,
    completion_tokens: int = 0,
    model: str | None = None,
) -> ScriptedAnswer:
    """The refusal vLLM actually gives when the **prompt** exceeds the context.

    An answer cut off by its length is a ``finish_reason="length"`` inside an
    HTTP 200: the engine generated, and generation ran out of room. This is an
    HTTP **400** with no choices at all: the engine refused before it
    generated, because the request could not be admitted. A stage that read it
    as "the answer was cut off" would record a truncated reading where no
    reading exists.

    The body is vLLM's own OpenAI-compatible error envelope
    (``{"object": "error", "message": ..., "type": "BadRequestError", "param":
    null, "code": 400}``) carrying the sentence its context check emits. Its
    exact wording has never been observed from a live engine by this
    repository -- only its *shape* is asserted here, and what the stages are
    proven to do with it is refuse by name and retain it, never parse it.

    ``model`` is deliberately absent by default: vLLM's error envelope names no
    model, and a fake that added one would let the client's wrong-source check
    fire on the wrong code.
    """

    message = (
        f"This model's maximum context length is {max_model_len} tokens. "
        f"However, you requested {requested_tokens} tokens "
        f"({prompt_tokens} in the messages, {completion_tokens} in the completion). "
        "Please reduce the length of the messages or completion."
    )
    payload: dict[str, Any] = {
        "object": "error",
        "message": message,
        "type": "BadRequestError",
        "param": None,
        "code": 400,
    }
    if model is not None:
        payload["model"] = model
    return ScriptedAnswer(status=400, body=json.dumps(payload).encode())


class FakeLauncher:
    """Launches a `FakeProcess` and binds it to `endpoint` (anything with `bind`)."""

    def __init__(
        self,
        endpoint: Any,
        *,
        log_tail: str = "",
        log_tails: tuple[str, ...] = (),
        exits_immediately: int | None = None,
        ignore_terminate: bool = False,
        ignore_kill: bool = False,
    ) -> None:
        self.endpoint = endpoint
        self.log_tail = log_tail
        self.log_tails = log_tails
        self.exits_immediately = exits_immediately
        self.ignore_terminate = ignore_terminate
        self.ignore_kill = ignore_kill
        self.calls: list[tuple[tuple[str, ...], Path]] = []
        self.inherited_fds: list[tuple[int, ...]] = []
        self.processes: list[FakeProcess] = []
        self.attached: list[FakeProcess] = []

    def launch(
        self,
        argv: tuple[str, ...],
        log_path: Path,
        *,
        inheritable_fds: tuple[int, ...] = (),
    ) -> FakeProcess:
        self.calls.append((argv, log_path))
        self.inherited_fds.append(inheritable_fds)
        process = FakeProcess(
            9000 + len(self.processes),
            log_tail=self.log_tail,
            log_tails=self.log_tails,
            exits_immediately=self.exits_immediately,
            ignore_terminate=self.ignore_terminate,
            ignore_kill=self.ignore_kill,
        )
        self.processes.append(process)
        self.endpoint.bind(process)
        return process

    def attach(self, pid: int, start_marker: str, log_path: Path) -> FakeProcess:
        """The live process the shared endpoint is bound to, when it is the one named.

        Another manager's launcher started it; both launchers share one endpoint,
        as two stages on one pod share one card.
        """

        del log_path
        process = getattr(self.endpoint, "process", None) or getattr(
            self.endpoint, "_process", None
        )
        if (
            process is None
            or process.pid != pid
            or process.start_marker != start_marker
            or not process.group_running
        ):
            raise ProcessLaunchError(f"no live process group {pid} ({start_marker!r})")
        self.attached.append(process)
        return process


class FakePackages:
    def __init__(self, versions: Mapping[str, str]) -> None:
        self.versions = dict(versions)

    def version(self, package: str) -> str:
        return self.versions[package]


class FakeRegistry:
    def __init__(self, identities: Mapping[str, ChairIdentity], tmp_path: Path) -> None:
        self.identities = dict(identities)
        self.config = ModelsConfig(witness_floor=0, chairs=self.identities)
        self.snapshots = {
            role: VerifiedSnapshot(identity, tmp_path / role, identity.digest_manifest)
            for role, identity in identities.items()
        }
        self.ensure_calls: list[str] = []
        self.refusals: list[tuple[str, str]] = []
        self.receipts: list[tuple[str, ServingDetails]] = []

    def resolve(self, role: str) -> ChairIdentity:
        return self.identities[role]

    def ensure(self, identity: ChairIdentity) -> VerifiedSnapshot:
        assert self.identities[identity.role] == identity
        self.ensure_calls.append(identity.role)
        return self.snapshots[identity.role]

    def receipt(self, identity: ChairIdentity, details: ServingDetails):
        self.receipts.append((identity.role, details))
        return build_receipt(identity, details)

    def refuse_recipe_start(self, identity: ChairIdentity, difference: str) -> None:
        self.refusals.append((identity.role, difference))
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


def shipped_decoding_policy() -> tuple[dict[str, Any], str]:
    """The shipped decoding policy and its seal, as a stage's ``main`` loads them."""
    return load_decoding_policy()


def fake_serving_factory(
    *,
    manager: ServingManager,
    retain: RetainBytes,
    read_receipt: Callable[[Mapping[str, str]], Mapping[str, object]],
) -> Callable[[Any, ChairIdentity, str], ChairClient]:
    """Build the ``serving_factory(context, chair, tier) -> ChairClient`` a
    stage's ``main`` calls under live mode, wired to one fake manager.

    ``context`` is accepted and ignored: production factories close over a
    real ``StageContext`` to build ``retain``/``read_receipt``, but this fake
    factory already has both, supplied directly by the test. Each chair sends
    its row of the shipped decoding policy.
    """
    policy, digest = load_decoding_policy()

    def factory(context: object, identity: ChairIdentity, tier: str) -> ChairClient:
        del context
        return ChairClient(
            manager=manager,
            identity=identity,
            tier=tier,
            retain=retain,
            decoding_config_sha256=digest,
            decoding_policy=policy,
            read_receipt=read_receipt,
        )

    return factory


class InProcessSurya:
    """Stands in for Surya's runner process: a live pass's `surya_runner`.

    It answers from declared rows, in the surya-engine page shape the runner
    writes, and hands those bytes to the same `surya_detector.surya_run` the
    real subprocess path does, so the page documents are checked, the weights
    are cross-checked against the chair's manifest, and the receipt is built
    exactly as they would be for a real run. Its run facts name Surya's own
    checkpoint sources at the pinned commit and list the chair snapshot's own
    files as the weights. `reading_orders` gives a page a raster fallback, as
    `surya_detector.declared_page_documents` takes it. `calls` records each
    run's page ordinals and thread count; `checked` each environment check.
    """

    STARTED_AT = "2026-01-01T00:00:00Z"
    CPU_CAPABILITY = "in-process fake"

    def __init__(
        self,
        lines: Sequence[Mapping[str, Any]],
        blocks: Sequence[Mapping[str, Any]],
        *,
        reading_orders: Mapping[int, tuple[str, str | None]] | None = None,
    ) -> None:
        self.lines = list(lines)
        self.blocks = list(blocks)
        self.reading_orders = dict(reading_orders or {})
        self.cpu_capability = self.CPU_CAPABILITY
        self.calls: list[tuple[tuple[int, ...], int]] = []
        self.checked: list[str] = []

    @staticmethod
    def _versions(profile: SubprocessProfile) -> dict[str, str]:
        return {
            "surya_ocr": profile.required_packages["surya-ocr"],
            "torch": profile.required_packages["torch"],
        }

    def check(self, profile: SubprocessProfile) -> dict[str, str]:
        self.checked.append(profile.environment)
        return self._versions(profile)

    def __call__(
        self,
        profile: SubprocessProfile,
        bundle_root: Path,
        pages: Mapping[int, bytes],
        sizes: Mapping[int, tuple[int, int]],
        identity: ChairIdentity,
        *,
        manifest_rows: Sequence[Mapping[str, Any]],
    ) -> SuryaRun:
        self.calls.append((tuple(sorted(pages)), profile.threads))
        versions = self._versions(profile)
        weights = [
            {
                "path": path.relative_to(bundle_root).as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "size": path.stat().st_size,
            }
            for path in sorted(bundle_root.rglob("*"))
            if path.is_file() and path.name != contract.BUNDLE_FILE
        ]
        pin = contract.LAYOUT_REPOSITORY_REVISION
        run_facts = {
            "engine": "surya",
            **versions,
            "python": "in-process fake",
            "device": profile.device,
            "cpu_capability": self.cpu_capability,
            "machine": "in-process fake",
            "threads": profile.threads,
            "deterministic_algorithms": True,
            "settings": {name: "in-process fake" for name in contract.OUTPUT_SETTINGS},
            "checkpoints": {
                "text_detection": {
                    "source": "s3://text_detection/2025_05_07",
                    "revision": None,
                    "path": "text_detection/2025_05_07",
                },
                "layout": {
                    "source": "hf://datalab-to/surya_layout2",
                    "revision": pin,
                    "path": "surya_layout2",
                },
                "order": {
                    "source": "hf://datalab-to/surya_layout2/order",
                    "revision": pin,
                    "path": "surya_layout2/order",
                },
            },
            "weights": weights,
        }
        written = declared_page_documents(
            self.lines, self.blocks, sizes, run_facts, reading_orders=self.reading_orders
        )
        return surya_run(
            profile,
            identity,
            versions,
            self.STARTED_AT,
            written,
            sizes,
            manifest_rows=manifest_rows,
        )
