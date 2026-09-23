"""Private, bounded runner for one pinned upstream Chandra experiment.

This is research instrumentation, never a Verbatus stage.  It imports the vendor
source only from a caller-provided cold-pod checkout and writes all page/request/
response bytes only to the private output directory supplied at runtime.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

UPSTREAM_COMMIT = "d4f7467435aa4137d9539f000ddf0b7ced3eb43f"
MODEL_REVISION = "af93b47dba1b47b6640c86ccf487ed2260ab9a09"
PAGE_SHA256 = "f4c1f729d5489ccbf7a2a5467b1cd2cad95df0701ce48ade85002092f920b68b"
MAX_PAGE_READINGS = 7


class DeliveryUnknown(BaseException):
    """Escape upstream's broad ``except Exception`` after ambiguous delivery."""


class LedgerExhausted(BaseException):
    pass


class DurabilityRefusal(BaseException):
    """Do not let upstream misclassify failed evidence publication as a model error."""


class ReceivedResponseRefusal(BaseException):
    """Carry a known HTTP response across parse/retention failure without retrying it."""

    def __init__(
        self,
        *,
        state: str,
        raw_body: bytes | None,
        error: BaseException,
    ) -> None:
        super().__init__(f"{type(error).__name__}: {error}")
        self.state = state
        self.raw_body = raw_body
        self.error_type = type(error).__name__
        self.detail = str(error)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _parse_deadline(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("deadline must include an explicit UTC offset")
    return parsed.astimezone(UTC)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _repeat_trigger(text: str, detector: Callable[..., bool]) -> bool:
    return detector(text) or (len(text) > 50 and detector(text, cut_from_end=50))


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _write_new(path: Path, body: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists():
        raise RuntimeError(f"refusing to overwrite research evidence {path}")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    completed = False
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(body)
            target.flush()
            os.fsync(target.fileno())
        completed = True
        _fsync_directory(path.parent)
    except BaseException:
        if not completed:
            try:
                path.unlink()
            except OSError:
                pass
        raise


def _write_json_new(path: Path, value: Any) -> None:
    _write_new(path, _canonical(value) + b"\n")


def _fsync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as error:
        _record_durability_refusal(directory, error)
        raise DurabilityRefusal(
            f"could not fsync evidence directory {directory}: {type(error).__name__}: {error}"
        ) from error


def _record_durability_refusal(directory: Path, error: OSError) -> None:
    """Best-effort visible record; its own directory sync is precisely what failed."""
    path = directory / "durability-refusal.json"
    try:
        _write_without_directory_sync(
            path,
            _canonical(
                {
                    "schema": "chandra-native-durability-refusal.v1",
                    "at": _now(),
                    "directory": str(directory),
                    "exception": type(error).__name__,
                    "detail": str(error),
                }
            )
            + b"\n",
        )
    except OSError:
        pass


def _write_without_directory_sync(path: Path, body: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as target:
        target.write(body)
        target.flush()
        os.fsync(target.fileno())


@dataclass
class ReadingLedger:
    root: Path
    entries: list[dict[str, Any]]

    @classmethod
    def create(cls, root: Path) -> "ReadingLedger":
        root.mkdir(mode=0o700, parents=True, exist_ok=False)
        ledger = cls(root=root, entries=[])
        ledger._persist()
        return ledger

    def _persist(self) -> None:
        target = self.root / "reading-ledger.json"
        payload = {
            "schema": "chandra-native-reading-ledger.v1",
            "max_page_readings": MAX_PAGE_READINGS,
            "entries": self.entries,
        }
        descriptor, temporary_name = tempfile.mkstemp(prefix=".ledger-", dir=self.root)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(_canonical(payload) + b"\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
            _fsync_directory(self.root)
        finally:
            if temporary.exists():
                temporary.unlink()

    def intent(self, request: dict[str, Any]) -> int:
        count = len(self.entries)
        if count >= MAX_PAGE_READINGS:
            raise LedgerExhausted(
                f"refusing page reading {count + 1}; cap is {MAX_PAGE_READINGS}"
            )
        ordinal = count + 1
        request_bytes = _canonical(request)
        _write_new(self.root / f"reading-{ordinal:02d}-request.json", request_bytes + b"\n")
        self.entries.append(
            {
                "ordinal": ordinal,
                "state": "intent",
                "at": _now(),
                "request_sha256": _sha256_bytes(request_bytes),
            }
        )
        self._persist()
        return ordinal

    def terminal(
        self,
        ordinal: int,
        *,
        state: str,
        metadata: dict[str, Any],
        raw_response: bytes | None = None,
    ) -> None:
        entry = next((item for item in self.entries if item["ordinal"] == ordinal), None)
        if entry is None or entry["state"] != "intent":
            raise RuntimeError(f"reading {ordinal} has no open intent")
        if raw_response is not None:
            _write_new(self.root / f"reading-{ordinal:02d}-response.raw", raw_response)
            metadata = {**metadata, "response_sha256": _sha256_bytes(raw_response)}
        _write_json_new(
            self.root / f"reading-{ordinal:02d}-terminal.json",
            {"state": state, **metadata},
        )
        entry.update({"state": state, "terminal_at": _now(), **metadata})
        self._persist()

    def hold(self, *, reason: str, metadata: dict[str, Any]) -> None:
        ordinal = len(self.entries) + 1
        _write_json_new(
            self.root / f"reading-{ordinal:02d}-hold.json",
            {"reason": reason, **metadata},
        )


class _DeadlineAdmission:
    def __init__(
        self,
        ledger: ReadingLedger,
        deadline: datetime,
        minimum_attempt_seconds: int,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self._ledger = ledger
        self._deadline = deadline
        self._minimum = timedelta(seconds=minimum_attempt_seconds)
        self._now = now

    def admit(self) -> None:
        observed = self._now()
        if observed + self._minimum < self._deadline:
            return
        self._ledger.hold(
            reason="deadline-admission-denied",
            metadata={
                "observed_at": observed.isoformat().replace("+00:00", "Z"),
                "deadline": self._deadline.isoformat().replace("+00:00", "Z"),
                "minimum_attempt_seconds": int(self._minimum.total_seconds()),
                "page_readings_completed": len(self._ledger.entries),
            },
        )
        raise LedgerExhausted("deadline leaves insufficient budget for another page reading")


class _CompletionProxy:
    def __init__(
        self,
        create: Callable[..., Any],
        ledger: ReadingLedger,
        transport_errors: tuple[type[BaseException], ...],
        admission: _DeadlineAdmission,
        repeat_detector: Callable[[str], bool],
    ):
        self._create = create
        self._ledger = ledger
        self._transport_errors = transport_errors
        self._admission = admission
        self._repeat_detector = repeat_detector

    def create(self, **request: Any) -> Any:
        try:
            self._admission.admit()
            ordinal = self._ledger.intent(request)
        except (DeliveryUnknown, LedgerExhausted, DurabilityRefusal):
            raise
        except Exception as error:
            raise DurabilityRefusal(
                f"could not durably publish page-reading intent: {type(error).__name__}: {error}"
            ) from error
        try:
            response = self._create(**request)
        except ReceivedResponseRefusal as error:
            self._terminal(
                ordinal,
                state=error.state,
                raw_response=error.raw_body,
                metadata={"exception": error.error_type, "detail": error.detail},
            )
            raise DurabilityRefusal(
                f"received response could not be retained normally: {error}"
            ) from error
        except BaseException as error:
            if isinstance(error, self._transport_errors):
                self._terminal(
                    ordinal,
                    state="delivery-unknown",
                    metadata={"exception": type(error).__name__, "detail": str(error)},
                )
                raise DeliveryUnknown(
                    "ambiguous delivery; native retry ladder must not replay"
                ) from error
            # HTTP status responses are known delivery outcomes and upstream may apply its
            # documented error retry. Unknown non-Exception failures never enter that path.
            status = getattr(error, "status_code", None)
            if isinstance(error, Exception) and isinstance(status, int):
                response = getattr(error, "response", None)
                body = getattr(response, "content", None)
                self._terminal(
                    ordinal,
                    state="known-error",
                    raw_response=body if isinstance(body, bytes) else None,
                    metadata={
                        "exception": type(error).__name__,
                        "status_code": status,
                        "detail": str(error),
                    },
                )
                raise
            self._terminal(
                ordinal,
                state="delivery-unknown",
                metadata={"exception": type(error).__name__, "detail": str(error)},
            )
            raise DeliveryUnknown(
                "unknown delivery; native retry ladder must not replay"
            ) from error
        try:
            raw = getattr(response, "_native_raw_body", None)
        except Exception as error:
            self._terminal(
                ordinal,
                state="received-unretained",
                metadata={"exception": type(error).__name__, "detail": str(error)},
            )
            raise DurabilityRefusal(
                f"received response bytes could not be recovered: {type(error).__name__}: {error}"
            ) from error
        try:
            if not isinstance(raw, bytes):
                raise ReceivedResponseRefusal(
                    state="received-unretained",
                    raw_body=None,
                    error=TypeError("OpenAI raw-response wrapper did not retain response bytes"),
                )
            usage = getattr(response, "usage", None)
            text = response.choices[0].message.content
            if not isinstance(text, str):
                raise ReceivedResponseRefusal(
                    state="received-unclassifiable",
                    raw_body=raw,
                    error=TypeError("upstream completion has no string native output"),
                )
            repeated = self._repeat_detector(text)
        except ReceivedResponseRefusal as error:
            self._terminal(
                ordinal,
                state=error.state,
                raw_response=error.raw_body,
                metadata={"exception": error.error_type, "detail": error.detail},
            )
            raise DurabilityRefusal(
                f"received response could not be classified normally: {error}"
            ) from error
        except Exception as error:
            self._terminal(
                ordinal,
                state="received-unclassifiable",
                raw_response=raw,
                metadata={"exception": type(error).__name__, "detail": str(error)},
            )
            raise DurabilityRefusal(
                f"could not retain native response evidence: {type(error).__name__}: {error}"
            ) from error
        self._terminal(
            ordinal,
            state="received",
            raw_response=raw,
            metadata={
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "model": getattr(response, "model", None),
                "repeat_trigger": repeated,
            },
        )
        return response

    def _terminal(
        self,
        ordinal: int,
        *,
        state: str,
        metadata: dict[str, Any],
        raw_response: bytes | None = None,
    ) -> None:
        try:
            self._ledger.terminal(
                ordinal,
                state=state,
                metadata=metadata,
                raw_response=raw_response,
            )
        except (DeliveryUnknown, LedgerExhausted, DurabilityRefusal):
            raise
        except Exception as error:
            raise DurabilityRefusal(
                f"could not durably publish page-reading terminal: {type(error).__name__}: {error}"
            ) from error


class _CapturedCompletion:
    """Parsed SDK object with the one original HTTP body retained beside it."""

    def __init__(self, parsed: Any, raw_body: bytes):
        self._parsed = parsed
        self._native_raw_body = raw_body

    def __getattr__(self, name: str) -> Any:
        return getattr(self._parsed, name)


class _NoRetryOpenAI:
    """Records each physical page request and forces SDK retry count to zero."""

    def __init__(
        self,
        original: Callable[..., Any],
        ledger: ReadingLedger,
        transport_errors: tuple[type[BaseException], ...],
        admission: _DeadlineAdmission,
        repeat_detector: Callable[[str], bool],
        **kwargs: Any,
    ):
        if kwargs.get("max_retries") not in (None, 0):
            raise RuntimeError("upstream requested hidden SDK retries")
        self._client = original(
            max_retries=0,
            timeout=kwargs.pop("_isolation_timeout_seconds"),
            **kwargs,
        )

        def raw_create(**request: Any) -> _CapturedCompletion:
            raw_response = self._client.with_raw_response.chat.completions.create(**request)
            body = raw_response.content
            if not isinstance(body, bytes):
                raise ReceivedResponseRefusal(
                    state="received-unretained",
                    raw_body=None,
                    error=TypeError("OpenAI raw response content was not bytes"),
                )
            try:
                parsed = raw_response.parse()
            except Exception as error:
                raise ReceivedResponseRefusal(
                    state="received-unparseable",
                    raw_body=body,
                    error=error,
                ) from error
            return _CapturedCompletion(parsed, body)

        self.chat = SimpleNamespace(
            completions=_CompletionProxy(
                raw_create,
                ledger,
                transport_errors,
                admission,
                repeat_detector,
            )
        )
        self.models = self._client.models


class _PrivateReceiptPublisher:
    """Retain manager lifecycle evidence outside the run-tree publication API."""

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)

    def _put(self, name: str, value: Any) -> dict[str, str]:
        body = _canonical(value) + b"\n"
        _write_new(self.root / name, body)
        return {"relative_path": name, "sha256": _sha256_bytes(body)}

    def publish(self, receipt: Any, launch_audit: Any) -> Any:
        from common.chairs.receipts import receipt_record
        from operations.serving.manager import ReceiptPublication

        receipt_reference = self._put("serving-receipt.json", receipt_record(receipt))
        audit_reference = self._put("serving-launch-audit.json", dict(launch_audit))
        evidence_reference = self._put(
            "serving-evidence.json",
            {
                "schema": "chandra-native-serving-evidence.v1",
                "receipt": receipt_reference,
                "launch_audit": audit_reference,
            },
        )
        return ReceiptPublication(receipt_reference, audit_reference, evidence_reference)


def _write_diagnostic_catalogue(source: Path, destination: Path) -> None:
    """Copy one real row into private evidence with only approved diagnostics changed."""
    source_bytes = source.read_bytes()
    source_text = source_bytes.decode("utf-8")
    stanzas = source_text.split("[[profiles]]")
    rewritten: list[str] = [stanzas[0]]
    matches = 0
    for stanza in stanzas[1:]:
        if 'chair = "attestator_1"' in stanza and 'tier = "generic-80gb-plus"' in stanza:
            if (
                "max_model_len = 18000" not in stanza
                or "startup_timeout_seconds = 300" not in stanza
            ):
                raise RuntimeError(
                    "attestator_1 80GB row does not have the reviewed diagnostic premises"
                )
            stanza = stanza.replace("max_model_len = 18000", "max_model_len = 20480", 1)
            stanza = stanza.replace("startup_timeout_seconds = 300", "startup_timeout_seconds = 600", 1)
            matches += 1
        rewritten.append(stanza)
    if matches != 1:
        raise RuntimeError(
            f"expected one attestator_1 generic-80gb-plus row, found {matches}"
        )
    _write_new(destination, "[[profiles]]".join(rewritten).encode("utf-8"))


def serve_and_run(args: argparse.Namespace) -> int:
    """Use the application's real lifecycle before the isolated upstream client."""
    from common.chairs.registry import ChairRegistry
    from common.contracts.canonical import digest_bytes
    from operations.pod.preflight import load_placement_table
    from operations.serving.config import ServingConfigInputs, load_serving_recipes
    from operations.serving.http import UrllibHttpTransport
    from operations.serving.manager import (
        MECHANICS_QUALIFICATION_PURPOSE,
        ServingManager,
    )
    from operations.serving.process import SubprocessLauncher
    from operations.serving.residency import FileResidencyLease

    page = args.page.resolve()
    if _sha256_path(page) != PAGE_SHA256:
        raise RuntimeError("input page differs from the approved original bad-page hash")
    _require_upstream(args.upstream_root.resolve())
    diagnostic_root = args.diagnostic_root.resolve()
    diagnostic_root.mkdir(mode=0o700, parents=True, exist_ok=False)
    recipes_path = diagnostic_root / "serving-recipes-diagnostic.toml"
    _write_diagnostic_catalogue(args.recipes_config.resolve(), recipes_path)
    placement_bytes = args.placement_config.read_bytes()
    recipes = load_serving_recipes(recipes_path)
    placement = load_placement_table(args.placement_config, source_bytes=placement_bytes)
    config_inputs = ServingConfigInputs(
        serving_recipes_sha256=recipes.source_sha256,
        pod_placement_sha256=digest_bytes(placement_bytes),
    )
    registry = ChairRegistry.from_toml(args.models_config, cache_root=args.cache_root)
    identity = registry.resolve("attestator_1")
    if identity.revision != MODEL_REVISION:
        raise RuntimeError("attestator_1 is not the required pinned Chandra identity")
    if args.served_model_id != "attestator-1-chandra":
        raise RuntimeError("the isolated client must use the diagnostic row's served model id")
    evidence_root = args.evidence_root.resolve()
    manager = ServingManager(
        registry=registry,
        recipes=recipes,
        config_inputs=config_inputs,
        launcher=SubprocessLauncher(),
        http=UrllibHttpTransport(),
        receipt_publisher=_PrivateReceiptPublisher(evidence_root),
        log_root=evidence_root / "serving-logs",
        residency_lease=FileResidencyLease(args.residency_lock),
        producer="session_verification.chandra_native_isolation",
        _launch_purpose=MECHANICS_QUALIFICATION_PURPOSE,
    )
    # Loading the table above is deliberate: it binds the retained placement bytes to
    # the diagnostic catalogue even though this one-chair research run has no smoke reader.
    del placement
    handle = manager.start(identity, "generic-80gb-plus")
    try:
        run_arguments = argparse.Namespace(**vars(args))
        run_arguments.vllm_api_base = handle.endpoint
        return run_native(run_arguments)
    finally:
        manager.stop(handle)


def _require_upstream(upstream_root: Path) -> dict[str, object]:
    observed = subprocess.run(
        ["git", "-C", str(upstream_root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if observed != UPSTREAM_COMMIT:
        raise RuntimeError(f"upstream checkout is {observed!r}, not pinned {UPSTREAM_COMMIT}")
    status = subprocess.run(
        ["git", "-C", str(upstream_root), "status", "--porcelain=v1", "--untracked-files=all"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if status:
        raise RuntimeError("upstream checkout has tracked or untracked changes")
    return {"commit": observed, "clean": True}


def ensure_role_cache(models_config: Path, cache_root: Path, evidence_root: Path) -> None:
    """Fetch and verify only the configured Chandra Attestator-1 manifest files."""
    from common.chairs.models import ChairIdentity
    from common.chairs.registry import ChairRegistry, HuggingFaceFetcher

    registry = ChairRegistry.from_toml(
        models_config,
        cache_root=cache_root,
        fetcher=HuggingFaceFetcher.from_huggingface_hub(),
    )
    identity = registry.resolve("attestator_1")
    if not isinstance(identity, ChairIdentity) or identity.revision != MODEL_REVISION:
        raise RuntimeError("attestator_1 is not the required pinned Chandra identity")
    snapshot = registry.ensure(identity)
    evidence_root.mkdir(mode=0o700, parents=True, exist_ok=False)
    _write_json_new(
        evidence_root / "registry-ensure.json",
        {
            "schema": "chandra-native-cache-ensure.v1",
            "role": identity.role,
            "repo": identity.repo,
            "revision": identity.revision,
            "digest_manifest": identity.digest_manifest,
            "snapshot": str(snapshot.root),
        },
    )


def run_native(args: argparse.Namespace) -> int:
    page = args.page.resolve()
    if _sha256_path(page) != PAGE_SHA256:
        raise RuntimeError("input page differs from the approved original bad-page hash")
    source_receipt = _require_upstream(args.upstream_root.resolve())
    ledger = ReadingLedger.create(args.output.resolve())
    deadline = _parse_deadline(args.deadline_utc)
    if args.minimum_attempt_seconds <= 0:
        raise ValueError("minimum attempt seconds must be positive")
    if args.sdk_timeout_seconds <= 0:
        raise ValueError("SDK timeout must be positive")
    if args.sdk_timeout_seconds + 30 > args.minimum_attempt_seconds:
        raise ValueError("minimum attempt must include SDK timeout plus 30 seconds of closeout")
    admission = _DeadlineAdmission(ledger, deadline, args.minimum_attempt_seconds)
    _write_json_new(
        ledger.root / "plan.json",
        {
            "schema": "chandra-native-isolation.v1",
            "page_sha256": PAGE_SHA256,
            "model_revision": MODEL_REVISION,
            "upstream_commit": UPSTREAM_COMMIT,
            "served_model_id": args.served_model_id,
            "vllm_api_base": args.vllm_api_base,
            "prompt_type": args.prompt_type,
            "max_page_readings": MAX_PAGE_READINGS,
            "sdk_max_retries": 0,
            "native_generation_retries": 6,
            "delivery_unknown_policy": "abort",
            "deadline": deadline.isoformat().replace("+00:00", "Z"),
            "minimum_attempt_seconds": args.minimum_attempt_seconds,
            "sdk_timeout_seconds": args.sdk_timeout_seconds,
        },
    )
    _write_json_new(
        ledger.root / "upstream-source.json",
        {"schema": "chandra-native-upstream-source.v1", **source_receipt},
    )
    sys.path.insert(0, str(args.upstream_root.resolve()))
    os.environ["VLLM_MODEL_NAME"] = args.served_model_id
    upstream = importlib.import_module("chandra.model.vllm")
    openai = importlib.import_module("openai")
    util = importlib.import_module("chandra.model.util")
    _write_json_new(
        ledger.root / "dependency-receipt.json",
        {
            "schema": "chandra-native-dependencies.v1",
            "openai": importlib.metadata.version("openai"),
            "Pillow": importlib.metadata.version("Pillow"),
        },
    )
    original = upstream.OpenAI
    transport_errors = tuple(
        item
        for item in (
            getattr(openai, "APITimeoutError", None),
            getattr(openai, "APIConnectionError", None),
        )
        if isinstance(item, type)
    )
    upstream.OpenAI = lambda **kwargs: _NoRetryOpenAI(
        original,
        ledger,
        transport_errors,
        admission,
        lambda text: _repeat_trigger(text, util.detect_repeat_token),
        _isolation_timeout_seconds=args.sdk_timeout_seconds,
        **kwargs,
    )
    try:
        with importlib.import_module("PIL.Image").open(page) as source:
            image = source.convert("RGB")
        item_type = importlib.import_module("chandra.model.schema").BatchInputItem
        item = item_type(image=image, prompt_type=args.prompt_type)
        results = upstream.generate_vllm(
            [item],
            max_retries=6,
            max_failure_retries=0,
            max_workers=1,
            vllm_api_base=args.vllm_api_base,
        )
    except (DeliveryUnknown, LedgerExhausted, DurabilityRefusal) as error:
        _write_json_new(
            ledger.root / "summary.json",
            {"terminal": type(error).__name__, "page_readings": len(ledger.entries)},
        )
        return 3
    finally:
        upstream.OpenAI = original
    result = results[0]
    raw = getattr(result, "raw", "")
    if not isinstance(raw, str):
        raise RuntimeError("upstream result did not retain native text output")
    _write_new(ledger.root / "native-output.txt", raw.encode("utf-8"))
    error = getattr(result, "error", None)
    final_repeat = _repeat_trigger(raw, util.detect_repeat_token)
    finish_reason = (
        "repeat-exhausted"
        if final_repeat and len(ledger.entries) == MAX_PAGE_READINGS
        else "repeat-before-cap"
        if final_repeat
        else "native-error"
        if error
        else "received"
    )
    _write_json_new(
        ledger.root / "summary.json",
        {
            "terminal": finish_reason,
            "error": error,
            "final_repeat_trigger": final_repeat,
            "returned_attempt_ordinal": len(ledger.entries),
            "token_count": getattr(result, "token_count", None),
            "page_readings": len(ledger.entries),
            "native_output_sha256": _sha256_bytes(raw.encode("utf-8")),
        },
    )
    return 2 if error or final_repeat else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    cache = commands.add_parser("ensure-role-cache")
    cache.add_argument("--models-config", type=Path, required=True)
    cache.add_argument("--cache-root", type=Path, required=True)
    cache.add_argument("--evidence-root", type=Path, required=True)
    run = commands.add_parser("run")
    run.add_argument("--upstream-root", type=Path, required=True)
    run.add_argument("--page", type=Path, required=True)
    run.add_argument("--prompt-type", default="ocr_layout")
    run.add_argument("--served-model-id", required=True)
    run.add_argument("--vllm-api-base", required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--deadline-utc", required=True)
    run.add_argument("--minimum-attempt-seconds", type=int, required=True)
    run.add_argument("--sdk-timeout-seconds", type=int, required=True)
    serve = commands.add_parser("serve-and-run")
    serve.add_argument("--models-config", type=Path, required=True)
    serve.add_argument("--recipes-config", type=Path, required=True)
    serve.add_argument("--placement-config", type=Path, required=True)
    serve.add_argument("--cache-root", type=Path, required=True)
    serve.add_argument("--diagnostic-root", type=Path, required=True)
    serve.add_argument("--evidence-root", type=Path, required=True)
    serve.add_argument("--residency-lock", type=Path, required=True)
    serve.add_argument("--upstream-root", type=Path, required=True)
    serve.add_argument("--page", type=Path, required=True)
    serve.add_argument("--prompt-type", default="ocr_layout")
    serve.add_argument("--served-model-id", default="attestator-1-chandra")
    serve.add_argument("--vllm-api-base", default="http://127.0.0.1:8102/v1")
    serve.add_argument("--output", type=Path, required=True)
    serve.add_argument("--deadline-utc", required=True)
    serve.add_argument("--minimum-attempt-seconds", type=int, required=True)
    serve.add_argument("--sdk-timeout-seconds", type=int, required=True)
    args = parser.parse_args(argv)
    if args.command == "ensure-role-cache":
        ensure_role_cache(args.models_config, args.cache_root, args.evidence_root)
        return 0
    if args.command == "serve-and-run":
        return serve_and_run(args)
    return run_native(args)


if __name__ == "__main__":
    raise SystemExit(main())
