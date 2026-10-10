"""Run witness models natively over a folder of page images and cache every raw answer.

    python -m operations.bakeoff.witness_run run --model chandra --pages DIR --out CACHE
    python -m operations.bakeoff.witness_run run-all --models chandra,dai,churro ...

One JSON per page per model at `<out>/<label>/<page stem>.json`. A page already cached
without an error is skipped, so a run resumes where it stopped. The card is kept busy:
the server takes many sequences at once, the client keeps its queue full, and `run-all`
prepares the next model's images (and DAI's record crops) and pre-reads its weights
while the current model is on the card. `nvidia-smi` is logged every 10 s beside the
cache, with an `events.jsonl` timeline, so idle gaps show afterwards.

An experiment tool, deliberately outside the pipeline's custody machinery.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from common.repetition_loop import LoopScanner
from operations.bakeoff import arms as A
from operations.bakeoff.score import loop_flag
from operations.serving.http import SSE_DONE, sse_data, sse_events

SCHEMA = "bakeoff-witness-page.v1"
IMAGE_SUFFIXES = {".tif", ".tiff", ".png", ".jpg", ".jpeg"}
OFFLINE_ENV = {"HF_HUB_OFFLINE": "1", "VLLM_NO_USAGE_STATS": "1", "DO_NOT_TRACK": "1"}
# Why a request was stopped before the engine ended it.
REQUEST_TIMEOUT = "request-timeout"
REPETITION_LOOP = "repetition-loop"
TERMINAL_STOPS = frozenset({REQUEST_TIMEOUT, REPETITION_LOOP})
_events_lock = threading.Lock()


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def event(out: Path, name: str, **facts: Any) -> None:
    out.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"t": now(), "event": name, **facts}, sort_keys=True)
    # The queue runner and its arms (separate processes) share this file: one O_APPEND
    # write per line, under an advisory lock where the file system offers one, so lines
    # never interleave even on a network volume where appends alone are not atomic.
    data = (line + "\n").encode("utf-8")
    with _events_lock:
        fd = os.open(out / "events.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError:
                pass
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
        finally:
            os.close(fd)
    print(line, flush=True)


def list_pages(pages: Path) -> list[Path]:
    found = sorted(p for p in pages.rglob("*") if p.suffix.lower() in IMAGE_SUFFIXES)
    stems: dict[str, Path] = {}
    for path in found:
        if path.stem in stems:
            raise SystemExit(f"two pages share the stem {path.stem!r}: {stems[path.stem]}, {path}")
        stems[path.stem] = path
    return found


def write_json(path: Path, value: Any) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def cached_ok(path: Path) -> bool:
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("error") is None
    except (OSError, ValueError):
        return False


def cache_conflict(path: Path, setup: dict[str, Any]) -> list[str]:
    """The fields of `setup` (checkpoint, revision, recipe) a cached page was written under
    differently. A page cached before recipes were recorded has no `recipe` to compare."""
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return sorted(k for k, v in setup.items() if k in old and old[k] != v)


def terminal_failure(path: Path) -> dict[str, Any] | None:
    """The cached page's failure when sending it again at its settings cannot help.

    A request that ran to the request timeout or into a loop would do the same again,
    so such a page is recorded as failed, with its reasons and the digest of the
    settings it was sent under (`settings_sha256`), and is never re-sent under them.
    """
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    failure = record.get("failure") if isinstance(record, dict) else None
    return failure if isinstance(failure, dict) and failure.get("terminal") else None


def settled(path: Path) -> bool:
    """Cached without an error, or failed in a way a retry at the same settings repeats."""
    return cached_ok(path) or terminal_failure(path) is not None


def settings_digest(
    repo: str | None,
    revision: str | None,
    requests: list[dict],
    serving: dict[str, Any] | None = None,
) -> str:
    """What a page was sent under: the checkpoint, every request record, and the serving
    conditions a timeout depends on (`serving`: the request timeout, how many requests
    were in flight, the server's batch and memory settings and engine options), so a page
    that timed out is sent again once any of them changes."""
    value = {"repo": repo, "revision": revision, "requests": requests, "serving": serving}
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


# --- the vLLM server ----------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    """Whether the process exists and is not a zombie left for its new parent to reap."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


class Server:
    """One `vllm serve` in its own session. A server kept by an earlier process (the
    smoke run's, `--keep-server`) is taken over with `Server.adopted`, by its pid."""

    def __init__(self, prefix: list[str], argv: list[str], port: int, log: Path) -> None:
        self.url = f"http://127.0.0.1:{port}"
        log.parent.mkdir(parents=True, exist_ok=True)
        self._log = open(log, "ab")
        self.argv = [*prefix, *argv]
        self.process: subprocess.Popen | None = subprocess.Popen(
            self.argv,
            stdout=self._log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env={**os.environ, **OFFLINE_ENV},
            start_new_session=True,
        )
        self.pid = self.process.pid

    @classmethod
    def adopted(cls, handoff: dict[str, Any]) -> Server:
        server = cls.__new__(cls)
        server.url, server.argv, server.pid = handoff["url"], handoff["argv"], handoff["pid"]
        server.process, server._log = None, None
        return server

    def handoff(self) -> dict[str, Any]:
        return {"url": self.url, "argv": self.argv, "pid": self.pid}

    def alive(self) -> bool:
        if self.process is not None:
            return self.process.poll() is None
        return _pid_alive(self.pid)

    def answers(self, served_name: str) -> bool:
        try:
            with urllib.request.urlopen(self.url + "/v1/models", timeout=5) as response:
                models = json.loads(response.read())
        except (OSError, ValueError, urllib.error.URLError):
            return False
        names = [m.get("id") for m in models.get("data", []) if isinstance(m, dict)]
        return served_name in names

    def wait_ready(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"server exited with {self.process.returncode}; see its log")
            try:
                with urllib.request.urlopen(self.url + "/v1/models", timeout=5) as response:
                    if response.status == 200:
                        return
            except (OSError, urllib.error.URLError):
                pass
            time.sleep(2)
        raise RuntimeError(f"server not ready after {timeout:.0f} s")

    def stop(self) -> None:
        if self.process is None:
            self._stop_adopted()
            return
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
                self.process.wait(timeout=60)
            except (subprocess.TimeoutExpired, ProcessLookupError):
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.process.wait(timeout=30)
        self._log.close()

    def _is_recorded_server(self) -> bool:
        """Whether the process at the hand-off's pid still runs the recorded command: a
        stale file's pid may have been given to an unrelated process."""
        try:
            raw = Path(f"/proc/{self.pid}/cmdline").read_bytes()
        except OSError:
            return False
        return raw.rstrip(b"\0").decode("utf-8", "replace").split("\0") == self.argv

    def _stop_adopted(self) -> None:
        """Stop a server this process did not start: no child to wait on, so watch its pid.
        A pid that is no longer that server is left alone."""
        if not self._is_recorded_server():
            return
        for signum, grace in ((signal.SIGTERM, 60.0), (signal.SIGKILL, 30.0)):
            if not _pid_alive(self.pid):
                return
            try:
                os.killpg(self.pid, signum)
            except (ProcessLookupError, PermissionError):
                return
            deadline = time.monotonic() + grace
            while _pid_alive(self.pid) and time.monotonic() < deadline:
                time.sleep(0.2)


def _timed_out(failure: BaseException) -> bool:
    return isinstance(failure, TimeoutError) or isinstance(
        getattr(failure, "reason", None), TimeoutError
    )


def _stop_error(stop: str, timeout: float, loop: dict[str, Any] | None) -> str:
    if stop == REQUEST_TIMEOUT:
        return f"{REQUEST_TIMEOUT} after {timeout:g} s"
    assert loop is not None
    return f"{REPETITION_LOOP}: {loop['kind']} of {loop['block_lines']} line(s) x{loop['repeats']}"


def _socket_of(response: Any) -> Any:
    """The socket under an `http.client` response, or None."""
    return getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)


def read_chunk(response: Any, deadline: float, timeout: float) -> bytes:
    """One read of a streamed reply that never waits past `deadline` (`time.monotonic`).

    The socket's timeout is the time left, at most `timeout`, so a reply that trickles
    in or stalls near the end is cut at the deadline, not one socket timeout after it;
    `TimeoutError` when no time is left.
    """
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("the request's total deadline passed")
    sock = _socket_of(response)
    if sock is not None:
        sock.settimeout(min(timeout, left))
    return response.read1(1 << 16) if hasattr(response, "read1") else response.read(1 << 16)


def _decoded(raw: bytes) -> tuple[str | None, str | None]:
    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, base64.b64encode(raw).decode("ascii")


def post(
    url: str, body: dict[str, Any], timeout: float, loop_guard: dict[str, int] | None = None
) -> dict[str, Any]:
    """One request; `stop` says why it was cut short (`REQUEST_TIMEOUT`, `REPETITION_LOOP`).

    A streamed body (`"stream": true`, the Perlector guard) is read as it arrives and
    abandoned at the first loop `loop_guard` finds, or when `timeout` has passed in all.
    """
    if body.get("stream"):
        return _post_stream(url, body, timeout, loop_guard)
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url + "/v1/chat/completions", data=data, headers={"Content-Type": "application/json"}
    )
    started = time.monotonic()
    status, raw, error, stop = None, b"", None, None
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as failure:
        status, raw, error = failure.code, failure.read(), f"HTTP {failure.code}"
    except (OSError, urllib.error.URLError) as failure:
        if _timed_out(failure):
            stop, error = REQUEST_TIMEOUT, _stop_error(REQUEST_TIMEOUT, timeout, None)
        else:
            error = f"{type(failure).__name__}: {failure}"
    seconds = round(time.monotonic() - started, 3)
    raw_text, raw_b64 = _decoded(raw)
    result = {"http_status": status, "raw_response": raw_text, "raw_response_b64": raw_b64}
    result.update(seconds=seconds, error=error, finish_reason=None, usage=None, text=None)
    result.update(stop=stop, loop_stop=None)
    if error is None:
        try:
            parsed = json.loads(raw)
            choice = parsed["choices"][0]
            result.update(
                finish_reason=choice.get("finish_reason"),
                usage=parsed.get("usage"),
                text=choice["message"].get("content") or "",
            )
        except (ValueError, KeyError, IndexError, TypeError) as failure:
            result["error"] = f"unreadable response: {type(failure).__name__}"
    return result


def _post_stream(
    url: str, body: dict[str, Any], timeout: float, loop_guard: dict[str, int] | None
) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url + "/v1/chat/completions", data=data, headers={"Content-Type": "application/json"}
    )
    started = time.monotonic()
    deadline = started + timeout
    scanner = LoopScanner(loop_guard) if loop_guard else None
    status, raw, error, stop = None, bytearray(), None, None
    pending, parts, finish, usage, ended = b"", [], None, None, False
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            while stop is None and not ended:
                chunk = read_chunk(response, deadline, timeout)
                if not chunk:
                    break
                raw += chunk
                events, pending = sse_events(pending + chunk)
                for item in events:
                    try:
                        text = sse_data(item)
                    except UnicodeDecodeError:
                        continue
                    if text == SSE_DONE:
                        ended = True
                        continue
                    try:
                        payload = json.loads(text) if text is not None else None
                    except ValueError:
                        continue
                    if not isinstance(payload, dict):
                        continue
                    usage = payload.get("usage") or usage
                    choices = payload.get("choices") or []
                    if not choices or not isinstance(choices[0], dict):
                        continue
                    finish = choices[0].get("finish_reason") or finish
                    piece = (choices[0].get("delta") or {}).get("content") or ""
                    parts.append(piece)
                    if scanner is not None and scanner.feed(piece) is not None:
                        stop = REPETITION_LOOP
                        break
                if stop is None and time.monotonic() > deadline:
                    stop = REQUEST_TIMEOUT
    except urllib.error.HTTPError as failure:
        status, raw, error = failure.code, bytearray(failure.read()), f"HTTP {failure.code}"
    except (OSError, urllib.error.URLError) as failure:
        if _timed_out(failure):
            stop = REQUEST_TIMEOUT
        else:
            error = f"{type(failure).__name__}: {failure}"
    loop = scanner.finding if scanner is not None else None
    if stop is not None:
        error = _stop_error(stop, timeout, loop)
    elif error is None and finish is None:
        error = "unreadable stream: no finish reason"
    raw_text, raw_b64 = _decoded(bytes(raw))
    return {
        "http_status": status,
        "raw_response": raw_text,
        "raw_response_b64": raw_b64,
        "seconds": round(time.monotonic() - started, 3),
        "error": error,
        "finish_reason": finish,
        "usage": usage,
        "text": "".join(parts) if status == 200 else None,
        "stop": stop,
        "loop_stop": loop if stop == REPETITION_LOOP else None,
    }


# --- one model ----------------------------------------------------------------------


class ModelJob:
    """Everything one model's run needs; `prepare` is CPU-only and may run early."""

    def __init__(self, name: str, args: argparse.Namespace) -> None:
        self.arm = A.ARMS[name]
        self.args = args
        self.label = args.label if (args.label and not args.run_all) else name
        self.dir = args.out / self.label
        self.tier = args.tier or self.arm.default_tier
        self.recipe = getattr(args, "recipe", None)
        self.row = A.serving_row(self.arm.chair, self.tier, self.recipe)
        self.max_model_len = args.max_model_len or self.row["max_model_len"]
        identity = A.chair_identity(self.arm.chair)
        self.repo = args.repo or identity["repo"]
        self.revision = args.revision or identity["revision"]
        self.row = A.arm_row(self.arm, self.row, self.repo)
        self.prompt_text = args.prompt_file.read_text("utf-8") if args.prompt_file else None
        self.guard = args.guard or A.default_guard(self.arm)
        self.served_name = f"bakeoff-{self.label}"
        self.weights: Path | None = None
        self.prepared: list[tuple[Path, list[tuple[dict, dict]]]] = []
        self.allowed_set = getattr(args, "allowed_tokens", None)
        self.allowed_tokens: tuple[str, tuple[int, ...]] | None = None

    def _allowed_tokens(self) -> tuple[str, tuple[int, ...]] | None:
        """The named output-token set, from the served snapshot's tokenizer; off unless asked."""
        if self.allowed_set is None:
            return None
        if self.allowed_tokens is None:
            from operations.bakeoff import allowed_tokens as T

            weights = self.weights or self.resolve_weights()
            self.allowed_tokens = (
                self.allowed_set,
                T.load(self.allowed_set, weights / "tokenizer.json"),
            )
        return self.allowed_tokens

    def resolve_weights(self) -> Path:
        explicit = self.args.weights
        if self.args.run_all and self.label in self.args.weights_map:
            explicit = Path(self.args.weights_map[self.label])
        self.weights = A.resolve_weights(
            self.arm, explicit, self.args.store_root, self.args.cache_root
        )
        return self.weights

    def _records(self, page: Path, png: bytes, detector: Any) -> list[dict[str, Any]]:
        """The detector's records for a page, cached with the settings that found them.

        A cache written under other detector settings is not reused. With
        `--record-fallback whole-page`, a page with no record becomes one whole-page
        record, marked as such in the cache and in each unit's name.
        """
        cache = self.dir / "_records" / f"{page.stem}.json"
        settings = {
            "conf": self.args.detector_conf,
            "imgsz": self.args.detector_imgsz,
            "fallback": self.args.record_fallback,
        }
        if cache.is_file():
            cached = json.loads(cache.read_text("utf-8"))
            if cached.get("settings", {}) == settings:
                return cached["records"]
        found = detector()
        records = found.records(png)
        detected = len(records)
        if not records and self.args.record_fallback == "whole-page":
            records = [A.whole_page_record(*A._size(png))]
        cache.parent.mkdir(parents=True, exist_ok=True)
        write_json(
            cache,
            {
                "page": page.stem,
                "detector": A.chair_identity(A.DETECTOR_CHAIR),
                "detector_settings": getattr(found, "settings", None),
                "settings": settings,
                "detected": detected,
                "order": "left column first, then top to bottom",
                "records": records,
            },
        )
        return records

    def prepare(self, pages: list[Path]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        event(self.args.out, "prepare-start", model=self.label)
        detector_box: list[Any] = []

        def detector() -> Any:
            if not detector_box:
                weights = self.args.detector_weights or A.resolve_weights(
                    A.Arm("detector", A.DETECTOR_CHAIR, A.DETECTOR_ARTIFACT, "page", ""),
                    None,
                    self.args.store_root,
                    self.args.cache_root,
                )
                detector_box.append(
                    A.RecordDetector(
                        weights,
                        self.args.detector_threads,
                        conf=self.args.detector_conf,
                        imgsz=self.args.detector_imgsz,
                    )
                )
            return detector_box[0]

        for page in pages:
            cached = self.dir / f"{page.stem}.json"
            if cached_ok(cached):
                differ = cache_conflict(cached, self.checkpoint())
                if differ:
                    raise SystemExit(
                        f"{cached} was cached under another setup ({', '.join(differ)} "
                        "differ); use a new --label"
                    )
                continue
            png = A.load_page_png(page)
            records = self._records(page, png, detector) if self.arm.scope == "record" else None
            requests = [
                A.build_request(
                    self.arm,
                    unit,
                    row=self.row,
                    served_name=self.served_name,
                    max_model_len=self.max_model_len,
                    prompt_text=self.prompt_text,
                    repo=self.repo,
                    allowed_tokens=self._allowed_tokens(),
                    guard=self.guard,
                )
                for unit in A.page_units(self.arm, png, records)
            ]
            failure = terminal_failure(cached)
            digest = settings_digest(
                self.repo, self.revision, [r for _, r in requests], self.serving_settings()
            )
            if failure is not None and failure.get("settings_sha256") == digest:
                event(
                    self.args.out,
                    "page-not-retried",
                    model=self.label,
                    page=page.stem,
                    reasons=failure.get("reasons"),
                )
                continue
            self.prepared.append((page, requests))
        event(self.args.out, "prepare-done", model=self.label, pages=len(self.prepared))

    def server_command(self) -> tuple[list[str], list[str]]:
        """(the command prefix, the `serve` arguments) this model's server runs."""
        A.assert_row_quantization(self.row, self.weights)
        argv = A.server_argv(
            self.row,
            self.weights,
            port=self.args.port,
            served_name=self.served_name,
            gpu_memory_utilization=self.args.gpu_memory_utilization,
            max_num_seqs=self.args.max_num_seqs,
            max_model_len=self.max_model_len,
            max_num_batched_tokens=self.args.max_num_batched_tokens,
        )
        prefix = self.args.vllm_cmd or [sys.executable, "-m", "vllm.entrypoints.cli.main"]
        return prefix, argv

    def adopt(self, path: Path | None) -> Server | None:
        """The server a `--keep-server` run left at `path`, if it is this model's own and
        answers; any other is stopped. The hand-off file is consumed either way."""
        if path is None or not path.is_file():
            return None
        try:
            handoff = json.loads(path.read_text("utf-8"))
            server = Server.adopted(handoff)
        except (OSError, ValueError, KeyError, TypeError):
            path.unlink(missing_ok=True)
            return None
        path.unlink(missing_ok=True)
        prefix, argv = self.server_command()
        if server.argv != [*prefix, *argv]:
            reason = "another server command"
        elif not server.alive():
            reason = "not running"
        elif not server.answers(self.served_name):
            reason = "not answering"
        else:
            event(self.args.out, "server-adopted", model=self.label, pid=server.pid)
            return server
        server.stop()
        event(self.args.out, "server-not-adopted", model=self.label, reason=reason)
        return None

    def serve(self) -> Server:
        prefix, argv = self.server_command()
        event(
            self.args.out,
            "server-start",
            model=self.label,
            weights=str(self.weights),
            **({"recipe": self.recipe} if self.recipe else {}),
        )
        server = Server(prefix, argv, self.args.port, self.dir / "server.log")
        try:
            server.wait_ready(self.args.startup_timeout)
        except BaseException:
            server.stop()
            event(self.args.out, "server-failed", model=self.label)
            raise
        event(self.args.out, "server-ready", model=self.label)
        return server

    def send_all(self, url: str, server_argv: list[str] | None) -> dict[str, Any]:
        concurrency = self.args.concurrency or 2 * self.args.max_num_seqs
        units = [(page, i, b, r) for page, reqs in self.prepared for i, (b, r) in enumerate(reqs)]
        remaining = {page: len(reqs) for page, reqs in self.prepared}
        results: dict[Path, list[Any]] = {page: [None] * len(reqs) for page, reqs in self.prepared}
        for page, reqs in self.prepared:
            if not reqs:  # DAI with no record found: a valid, empty page
                self._write_page(page, [], server_argv, url)
        event(self.args.out, "requests-start", model=self.label, units=len(units))
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            futures: dict[Future, tuple[Path, int, dict]] = {
                pool.submit(
                    post,
                    url,
                    body,
                    self.args.request_timeout,
                    record["guard"].get("loop_guard"),
                ): (page, i, record)
                for page, i, body, record in units
            }
            for future in as_completed(futures):
                page, i, record = futures[future]
                results[page][i] = {"request": record, **future.result()}
                remaining[page] -= 1
                if remaining[page] == 0:
                    self._write_page(page, results[page], server_argv, url)
        wall = round(time.monotonic() - started, 3)
        summary = {
            "model": self.label,
            "pages": len(self.prepared),
            "units": len(units),
            "wall_seconds": wall,
            "concurrency": concurrency,
            "max_num_seqs": self.args.max_num_seqs,
            "finished": now(),
        }
        write_json(self.dir / "run.json", summary)
        event(self.args.out, "requests-done", model=self.label, wall_seconds=wall)
        return summary

    def _write_page(self, page: Path, units: list[dict], argv: list[str] | None, url: str) -> None:
        source = page.read_bytes()
        texts = [u["text"] or "" for u in units]
        errors = [u["error"] for u in units if u["error"]]
        finishes = [u["finish_reason"] for u in units]
        text = (
            "\n".join(t for t in texts if t.strip())
            if self.arm.scope == "record"
            else (texts[0] if texts else "")
        )
        loops = [
            (True, REPETITION_LOOP)
            if u.get("loop_stop")
            else loop_flag(u["text"] or "", u["finish_reason"])
            for u in units
        ]
        record = {
            "schema": SCHEMA,
            "model": self.label,
            "arm": self.arm.name,
            "repo": self.repo,
            "revision": self.revision,
            "recipe": self.recipe,
            "weights": str(self.weights),
            "server": {"url": url, "argv": argv},
            "page": page.stem,
            "source_file": page.name,
            "source_sha256": hashlib.sha256(source).hexdigest(),
            "units": units,
            "text": text,
            "empty": not text.strip(),
            "finish_reason": "length"
            if "length" in finishes
            else (finishes[0] if finishes else "no-records"),
            "record_units": [u["request"]["unit"] for u in units],
            "whole_page_fallback": any(u["request"]["unit"] == "whole-page" for u in units),
            "loop": any(f for f, _ in loops),
            "loop_reasons": [r for f, r in loops if f],
            "seconds": round(sum(u["seconds"] for u in units), 3),
            "guard": self.guard,
            "stops": sorted({u["stop"] for u in units if u.get("stop")}),
            "error": "; ".join(errors) if errors else None,
            "failure": self._failure(units),
            "written": now(),
        }
        write_json(self.dir / f"{page.stem}.json", record)

    def checkpoint(self) -> dict[str, Any]:
        """What a label's cached pages must agree on: the model served behind it."""
        return {"repo": self.repo, "revision": self.revision, "recipe": self.recipe}

    def _failure(self, units: list[dict]) -> dict[str, Any] | None:
        """A terminal failure when every unit in error was stopped by a timeout or a loop."""
        failed = [u for u in units if u["error"]]
        if not failed or any(u.get("stop") not in TERMINAL_STOPS for u in failed):
            return None
        return {
            "terminal": True,
            "reasons": sorted({u["stop"] for u in failed}),
            "settings": self.serving_settings(),
            "settings_sha256": settings_digest(
                self.repo, self.revision, [u["request"] for u in units], self.serving_settings()
            ),
        }

    def serving_settings(self) -> dict[str, Any]:
        """The serving conditions a timeout depends on, beside the requests themselves."""
        return {
            "request_timeout": self.args.request_timeout,
            "concurrency": self.args.concurrency or 2 * self.args.max_num_seqs,
            "max_num_seqs": self.args.max_num_seqs,
            "max_model_len": self.max_model_len,
            "max_num_batched_tokens": self.args.max_num_batched_tokens
            or self.row.get("max_num_batched_tokens"),
            "gpu_memory_utilization": self.args.gpu_memory_utilization,
            "recipe": self.recipe,
            "engine": {
                k: self.row.get(k) for k in ("quantization", "kv_cache_dtype", "speculative_config")
            },
        }


def warm(weights: Path, stage_dir: Path | None, name: str) -> Path:
    """Copy the next model's weights to local disk, or read them into the page cache."""
    if stage_dir is not None:
        dest = stage_dir / name
        for src in weights.rglob("*"):
            if src.is_file():
                target = dest / src.relative_to(weights)
                if not target.is_file() or target.stat().st_size != src.stat().st_size:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(src, target.with_suffix(target.suffix + ".part"))
                    os.replace(target.with_suffix(target.suffix + ".part"), target)
        return dest
    for src in weights.rglob("*"):
        if src.is_file():
            with open(src, "rb") as handle:
                while handle.read(1 << 24):
                    pass
    return weights


class GpuLog:
    def __init__(self, out: Path) -> None:
        self.process = None
        exe = shutil.which("nvidia-smi")
        if exe:
            out.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            self._file = open(out / f"gpu-{stamp}.csv", "ab")
            query = "timestamp,index,utilization.gpu,memory.used,memory.total,power.draw"
            self.process = subprocess.Popen(
                [exe, f"--query-gpu={query}", "--format=csv", "-l", "10"],
                stdout=self._file,
                stderr=subprocess.DEVNULL,
            )

    def stop(self) -> None:
        if self.process is not None:
            self.process.terminate()
            self.process.wait(timeout=10)
            self._file.close()


def run_models(names: list[str], args: argparse.Namespace) -> None:
    """Each model in turn on the card; CPU work and weight reads for the next overlap it.

    The server for a model starts while its pages are still being prepared; the next
    model's preparation and weight pre-read start as soon as this one's preparation is
    done; the next server starts as soon as this one has answered its last request and
    stopped.

    `--adopt-server` takes over the server a `--keep-server` run left (one model load
    for a smoke and its full run); `--keep-server` leaves this run's server up and
    writes the hand-off when the run ends normally. Any other end stops it.
    """
    pages = list_pages(args.pages)[: args.limit or None]
    jobs = [ModelJob(name, args) for name in names]
    keep_path, adopt_path = args.keep_server, args.adopt_server
    gpu = GpuLog(args.out)
    cpu = ThreadPoolExecutor(max_workers=2)
    try:
        for job in jobs:
            if not args.server_url:
                job.resolve_weights()
        prep = cpu.submit(jobs[0].prepare, pages)
        for index, job in enumerate(jobs):
            pending = [p for p in pages if not settled(job.dir / f"{p.stem}.json")]
            server, kept = job.adopt(adopt_path) if not args.server_url else None, False
            if server is not None and not pending:
                server.stop()
                event(args.out, "server-stopped", model=job.label)
                server = None
            if pending and server is None and not args.server_url:
                server = job.serve()
            try:
                prep.result()
                if job.prepared and server is None and not args.server_url:
                    # A failed page whose settings changed is sent again.
                    server = job.serve()
                nxt = jobs[index + 1] if index + 1 < len(jobs) else None
                staged = None
                if nxt is not None:
                    prep = cpu.submit(nxt.prepare, pages)
                    if nxt.weights is not None:
                        staged = cpu.submit(warm, nxt.weights, args.stage_dir, nxt.label)
                if not job.prepared:
                    event(args.out, "nothing-to-do", model=job.label)
                elif server is None:
                    job.send_all(args.server_url.rstrip("/"), None)
                else:
                    job.send_all(server.url, server.argv)
                if server is not None and keep_path is not None:
                    keep_path.parent.mkdir(parents=True, exist_ok=True)
                    write_json(keep_path, server.handoff())
                    kept = True
                    event(args.out, "server-kept", model=job.label, pid=server.pid)
            finally:
                if server is not None and not kept:
                    server.stop()
                    event(args.out, "server-stopped", model=job.label)
            if staged is not None:
                nxt.weights = staged.result()
    finally:
        cpu.shutdown(wait=False, cancel_futures=True)
        gpu.stop()


def fetch(names: list[str], store_root: Path) -> None:
    """Download each arm's pinned snapshot to `<store_root>/hf/<artifact>` (the store layout).

    A bench convenience for a volume without the pipeline's model store; it pins the
    revision but does not check the pipeline's manifests.
    """
    from huggingface_hub import snapshot_download

    wanted = [A.ARMS[n] for n in names if A.ARMS[n].artifact]
    if any(arm.scope == "record" for arm in wanted):
        wanted.append(A.Arm("detector", A.DETECTOR_CHAIR, A.DETECTOR_ARTIFACT, "page", ""))
    for arm in wanted:
        identity = A.chair_identity(arm.chair)
        dest = store_root / "hf" / arm.artifact
        print(f"fetching {identity['repo']}@{identity['revision']} -> {dest}", flush=True)
        snapshot_download(identity["repo"], revision=identity["revision"], local_dir=dest)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    f = sub.add_parser("fetch", help="download pinned weights into a store layout")
    f.add_argument("--models", required=True, help="comma-separated")
    f.add_argument("--store-root", type=Path, required=True)
    for command in ("run", "run-all"):
        p = sub.add_parser(command)
        if command == "run":
            p.add_argument("--model", required=True, choices=sorted(A.ARMS))
            p.add_argument("--weights", type=Path, help="a local snapshot directory")
            p.add_argument("--label", help="cache folder name (default: the model name)")
            p.add_argument(
                "--keep-server",
                type=Path,
                help="leave the server running at the end and write its hand-off here",
            )
            p.add_argument(
                "--adopt-server",
                type=Path,
                help="take over the server a --keep-server run left here, if it is this one",
            )
        else:
            p.add_argument("--models", required=True, help="ordered, comma-separated")
            p.add_argument("--stage-dir", type=Path, help="copy next weights to local disk here")
            p.add_argument("--weights", nargs="*", default=[], help="NAME=PATH snapshots")
        p.add_argument("--pages", type=Path, required=True)
        p.add_argument("--out", type=Path, required=True)
        p.add_argument("--store-root", type=Path, help="the volume's model store")
        p.add_argument("--cache-root", type=Path, help="the pod's chair cache")
        p.add_argument("--detector-weights", type=Path)
        p.add_argument("--detector-threads", type=int, default=1)
        p.add_argument(
            "--detector-conf", type=float, help="record detector confidence (default: the row's)"
        )
        p.add_argument(
            "--detector-imgsz", type=int, help="record detector image size (default: the row's)"
        )
        p.add_argument(
            "--record-fallback",
            choices=["none", "whole-page"],
            default="none",
            help="a page with no record: skip it (none) or show DAI the whole page",
        )
        p.add_argument("--tier", help="serving row tier (default per model)")
        p.add_argument(
            "--recipe",
            help="serve the chair's row with this recipe name, from the run or the variants "
            "catalogue (default: the chair's run row)",
        )
        p.add_argument(
            "--allowed-tokens",
            choices=["latin-json-v1"],
            help="restrict output to a named token set from the snapshot's tokenizer (default off)",
        )
        p.add_argument("--max-model-len", type=int)
        p.add_argument("--max-num-seqs", type=int, default=32)
        p.add_argument("--max-num-batched-tokens", type=int, help="default: the row's")
        p.add_argument("--gpu-memory-utilization", type=float, default=0.92)
        p.add_argument("--concurrency", type=int, help="default: 2 x max-num-seqs")
        p.add_argument("--port", type=int, default=8190)
        p.add_argument("--server-url", help="use a running server instead of starting one")
        p.add_argument("--vllm-cmd", nargs="+", help="command prefix before 'serve'")
        p.add_argument("--startup-timeout", type=float, default=1200)
        p.add_argument("--request-timeout", type=float, default=1800)
        p.add_argument("--prompt-file", type=Path, help="qwen-blind: replace the plain prompt")
        p.add_argument(
            "--guard",
            choices=A.GUARDS,
            help="perlector: the Perlector's page cap where no cap is sent, and its streaming "
            "loop detector; none: the vendor's request as it is (default: perlector for the "
            "reader arms, none for the witness arms)",
        )
        p.add_argument("--repo")
        p.add_argument("--revision")
        p.add_argument("--limit", type=int, help="first N pages only (a smoke run)")
    args = parser.parse_args(argv)
    if args.command == "fetch":
        return args
    args.run_all = args.command == "run-all"
    if args.run_all:
        args.weights_map = dict(item.split("=", 1) for item in args.weights)
        args.weights, args.label = None, None
        args.keep_server, args.adopt_server = None, None
    else:
        args.stage_dir, args.weights_map = None, {}
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "fetch":
        fetch([n.strip() for n in args.models.split(",")], args.store_root)
        return 0
    names = [n.strip() for n in args.models.split(",")] if args.run_all else [args.model]
    unknown = [n for n in names if n not in A.ARMS]
    if unknown:
        raise SystemExit(f"unknown models {unknown}; known: {sorted(A.ARMS)}")
    # A plain `kill` of the runner still stops its server (which runs in its own session).
    previous = signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    try:
        run_models(names, args)
    finally:
        signal.signal(signal.SIGTERM, previous)
    return 0


if __name__ == "__main__":
    sys.exit(main())
