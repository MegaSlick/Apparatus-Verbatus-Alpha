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

from operations.bakeoff import arms as A
from operations.bakeoff.score import loop_flag

SCHEMA = "bakeoff-witness-page.v1"
IMAGE_SUFFIXES = {".tif", ".tiff", ".png", ".jpg", ".jpeg"}
OFFLINE_ENV = {"HF_HUB_OFFLINE": "1", "VLLM_NO_USAGE_STATS": "1", "DO_NOT_TRACK": "1"}
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


# --- the vLLM server ----------------------------------------------------------------


class Server:
    def __init__(self, prefix: list[str], argv: list[str], port: int, log: Path) -> None:
        self.url = f"http://127.0.0.1:{port}"
        log.parent.mkdir(parents=True, exist_ok=True)
        self._log = open(log, "ab")
        self.argv = [*prefix, *argv]
        self.process = subprocess.Popen(
            self.argv,
            stdout=self._log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env={**os.environ, **OFFLINE_ENV},
            start_new_session=True,
        )

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


def post(url: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url + "/v1/chat/completions", data=data, headers={"Content-Type": "application/json"}
    )
    started = time.monotonic()
    status, raw, error = None, b"", None
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as failure:
        status, raw, error = failure.code, failure.read(), f"HTTP {failure.code}"
    except (OSError, urllib.error.URLError) as failure:
        error = f"{type(failure).__name__}: {failure}"
    seconds = round(time.monotonic() - started, 3)
    try:
        raw_text, raw_b64 = raw.decode("utf-8"), None
    except UnicodeDecodeError:
        raw_text, raw_b64 = None, base64.b64encode(raw).decode("ascii")
    result = {"http_status": status, "raw_response": raw_text, "raw_response_b64": raw_b64}
    result.update(seconds=seconds, error=error, finish_reason=None, usage=None, text=None)
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


# --- one model ----------------------------------------------------------------------


class ModelJob:
    """Everything one model's run needs; `prepare` is CPU-only and may run early."""

    def __init__(self, name: str, args: argparse.Namespace) -> None:
        self.arm = A.ARMS[name]
        self.args = args
        self.label = args.label if (args.label and not args.run_all) else name
        self.dir = args.out / self.label
        self.tier = args.tier or self.arm.default_tier
        self.row = A.serving_row(self.arm.chair, self.tier)
        self.max_model_len = args.max_model_len or self.row["max_model_len"]
        identity = A.chair_identity(self.arm.chair)
        self.repo = args.repo or identity["repo"]
        self.revision = args.revision or identity["revision"]
        self.prompt_text = args.prompt_file.read_text("utf-8") if args.prompt_file else None
        self.served_name = f"bakeoff-{self.label}"
        self.weights: Path | None = None
        self.prepared: list[tuple[Path, list[tuple[dict, dict]]]] = []

    def resolve_weights(self) -> Path:
        explicit = self.args.weights
        if self.args.run_all and self.label in self.args.weights_map:
            explicit = Path(self.args.weights_map[self.label])
        self.weights = A.resolve_weights(
            self.arm, explicit, self.args.store_root, self.args.cache_root
        )
        return self.weights

    def _records(self, page: Path, png: bytes, detector: Any) -> list[dict[str, int]]:
        cache = self.dir / "_records" / f"{page.stem}.json"
        if cache.is_file():
            return json.loads(cache.read_text("utf-8"))["records"]
        records = detector().records(png)
        cache.parent.mkdir(parents=True, exist_ok=True)
        write_json(
            cache,
            {
                "page": page.stem,
                "detector": A.chair_identity(A.DETECTOR_CHAIR),
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
                detector_box.append(A.RecordDetector(weights, self.args.detector_threads))
            return detector_box[0]

        for page in pages:
            if cached_ok(self.dir / f"{page.stem}.json"):
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
                )
                for unit in A.page_units(self.arm, png, records)
            ]
            self.prepared.append((page, requests))
        event(self.args.out, "prepare-done", model=self.label, pages=len(self.prepared))

    def serve(self) -> Server:
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
        event(self.args.out, "server-start", model=self.label, weights=str(self.weights))
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
                pool.submit(post, url, body, self.args.request_timeout): (page, i, record)
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
        loops = [loop_flag(u["text"] or "", u["finish_reason"]) for u in units]
        record = {
            "schema": SCHEMA,
            "model": self.label,
            "arm": self.arm.name,
            "repo": self.repo,
            "revision": self.revision,
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
            "loop": any(f for f, _ in loops),
            "loop_reasons": [r for f, r in loops if f],
            "seconds": round(sum(u["seconds"] for u in units), 3),
            "error": "; ".join(errors) if errors else None,
            "written": now(),
        }
        write_json(self.dir / f"{page.stem}.json", record)


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
    """
    pages = list_pages(args.pages)[: args.limit or None]
    jobs = [ModelJob(name, args) for name in names]
    gpu = GpuLog(args.out)
    cpu = ThreadPoolExecutor(max_workers=2)
    try:
        for job in jobs:
            if not args.server_url:
                job.resolve_weights()
        prep = cpu.submit(jobs[0].prepare, pages)
        for index, job in enumerate(jobs):
            pending = [p for p in pages if not cached_ok(job.dir / f"{p.stem}.json")]
            server = None
            if pending and not args.server_url:
                server = job.serve()
            try:
                prep.result()
                nxt = jobs[index + 1] if index + 1 < len(jobs) else None
                staged = None
                if nxt is not None:
                    prep = cpu.submit(nxt.prepare, pages)
                    if nxt.weights is not None:
                        staged = cpu.submit(warm, nxt.weights, args.stage_dir, nxt.label)
                if not pending:
                    event(args.out, "nothing-to-do", model=job.label)
                elif server is None:
                    job.send_all(args.server_url.rstrip("/"), None)
                else:
                    job.send_all(server.url, server.argv)
            finally:
                if server is not None:
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
        p.add_argument("--tier", help="serving row tier (default per model)")
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
