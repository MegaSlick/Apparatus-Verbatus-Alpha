"""The command line, cache record and page loop every native arm shares.

    <python> -m operations.bakeoff.native.<module> run --pages DIR --out CACHE [--label NAME]
        [--limit N] [--weights PATH | --store-root DIR] [--server-url URL | --vllm-cmd ...]
    <python> -m operations.bakeoff.native.<module> install --venv-dir DIR
    <python> -m operations.bakeoff.native.<module> check
    <python> -m operations.bakeoff.native.<module> prepare --pages DIR --out CACHE
    <python> -m operations.bakeoff.native.<module> fetch --store-root DIR

An arm module describes itself as an `Arm`: its identity, a function that imports the
vendor's own code (and fails in the wrong environment), the vendor's `vllm serve` flags,
and `read_page`, which sends one page through the vendor's path and returns the cached
units and the page's plain text. Everything else -- resume, events, the cache record,
`run.json`, exit codes -- is here, so the arms differ only where their vendors differ.

Each arm's client runs in its own small environment (`venvs/<arm>/pyproject.toml`); the
vLLM server it talks to runs from the project environment (`--vllm-cmd`, by default
`.venv/bin/python -m vllm.entrypoints.cli.main` at the repository root).
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from operations.bakeoff import witness_run as W
from operations.bakeoff.score import loop_flag

ROOT = Path(__file__).resolve().parents[3]
VENVS = Path(__file__).resolve().parent / "venvs"


class Refusal(Exception):
    """The arm cannot run as asked (wrong environment, missing weights); exit code 2."""


@dataclass(frozen=True)
class Arm:
    name: str  # the cache record's `arm`; score.py keys on it
    label: str  # the default cache folder
    repo: str
    revision: str
    artifact: str  # `<store_root>/hf/<artifact>`
    venv: str  # `venvs/<venv>/pyproject.toml`
    packages: tuple[str, ...]  # distributions `check` reports
    load_vendor: Callable[[], Any]
    server_argv: Callable[[Any, argparse.Namespace, Path], list[str]]
    read_page: Callable[[Any, argparse.Namespace, Path, str], tuple[list[dict], str]]
    client_settings: Callable[[Any, argparse.Namespace], dict]
    add_arguments: Callable[[argparse.ArgumentParser], None]
    default_concurrency: int
    default_port: int
    source: tuple[str, str] | None = None  # (git URL, commit) installed as a source checkout


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def unit(
    request: dict,
    raw: str | None,
    text: str | None,
    finish_reason: str | None,
    seconds: float,
    error: str | None,
    **extra: Any,
) -> dict:
    """One unit of a cache record, in the keys every arm writes."""
    return {
        "request": request,
        "raw_response": raw,
        "text": text,
        "finish_reason": finish_reason,
        "seconds": round(seconds, 3),
        "error": error,
        **extra,
    }


def plain(lines: list[str]) -> str:
    """Lines with runs of whitespace collapsed and empty lines dropped."""
    cleaned = (" ".join(line.split()) for line in lines)
    return "\n".join(line for line in cleaned if line)


# --- the cache record ---------------------------------------------------------------


def page_record(
    arm: Arm,
    label: str,
    page: Path,
    units: list[dict],
    text: str,
    weights: Path | None,
    server: dict,
) -> dict:
    """The `bakeoff-witness-page.v1` record, the keys `witness_run` writes."""
    errors = [u["error"] for u in units if u["error"]]
    finishes = [u["finish_reason"] for u in units]
    loops = [loop_flag(u["text"] or "", u["finish_reason"]) for u in units]
    return {
        "schema": W.SCHEMA,
        "model": label,
        "arm": arm.name,
        "repo": arm.repo,
        "revision": arm.revision,
        "weights": str(weights) if weights else None,
        "server": server,
        "page": page.stem,
        "source_file": page.name,
        "source_sha256": sha256(page.read_bytes()),
        "units": units,
        "text": text,
        "empty": not text.strip(),
        "finish_reason": "length" if "length" in finishes else (finishes[0] if finishes else None),
        "loop": any(flag for flag, _ in loops),
        "loop_reasons": [reason for flag, reason in loops if flag],
        "seconds": round(sum(u["seconds"] for u in units), 3),
        "error": "; ".join(errors) if errors else None,
        "written": W.now(),
    }


# --- weights, server, vendor code ---------------------------------------------------


def resolve_weights(arm: Arm, args: argparse.Namespace) -> Path:
    if args.weights is not None:
        candidate = args.weights
    elif args.store_root is not None:
        candidate = args.store_root / "hf" / arm.artifact
    else:
        raise Refusal("no weights: pass --weights PATH or --store-root DIR")
    if not (candidate / "config.json").is_file():
        raise Refusal(f"no snapshot at {candidate} (no config.json); run `fetch` first")
    return candidate


def vllm_prefix(args: argparse.Namespace) -> list[str]:
    if args.vllm_cmd:
        return list(args.vllm_cmd)
    python = sys.executable if importlib.util.find_spec("vllm") else str(ROOT / ".venv/bin/python")
    return [python, "-m", "vllm.entrypoints.cli.main"]


def load_vendor_or_refuse(arm: Arm) -> Any:
    try:
        return arm.load_vendor()
    except (ImportError, OSError) as failure:
        raise Refusal(
            f"{arm.name}: the vendor code is not importable here ({failure}); run this arm "
            f"with its own environment (`install --venv-dir DIR`, recipe venvs/{arm.venv})"
        ) from failure


def check_page_level(args: argparse.Namespace) -> None:
    if getattr(args, "lines", None):
        raise Refusal("a page-level model: --lines does not apply")
    if getattr(args, "device", "cuda") != "cuda":
        raise Refusal("the model runs on a vLLM server on the GPU: --device must be cuda")


# --- subcommands --------------------------------------------------------------------


def _read_safely(arm: Arm, vendor: Any, args: argparse.Namespace, page: Path, url: str):
    started = time.monotonic()
    try:
        return arm.read_page(vendor, args, page, url)
    except Exception as failure:  # noqa: BLE001 -- one bad page is recorded, the run goes on
        error = f"{type(failure).__name__}: {failure}"
        request = {"unit": "page", "page": page.stem}
        return [unit(request, None, None, None, time.monotonic() - started, error)], ""


def run(arm: Arm, args: argparse.Namespace) -> int:
    check_page_level(args)
    os.environ.update(W.OFFLINE_ENV)
    vendor = load_vendor_or_refuse(arm)
    label = args.label or arm.label
    folder = args.out / label
    pages = W.list_pages(args.pages)[: args.limit or None]
    pending = [p for p in pages if not W.cached_ok(folder / f"{p.stem}.json")]
    folder.mkdir(parents=True, exist_ok=True)
    W.event(args.out, "prepare-start", model=label)
    W.event(args.out, "prepare-done", model=label, pages=len(pending))
    if not pending:
        W.event(args.out, "nothing-to-do", model=label)
        return 0
    weights = None if args.server_url else resolve_weights(arm, args)
    server, argv = None, None
    if args.server_url:
        url = args.server_url.rstrip("/")
    else:
        argv = arm.server_argv(vendor, args, weights)
        W.event(args.out, "server-start", model=label, weights=str(weights))
        server = W.Server(vllm_prefix(args), argv, args.port, folder / "server.log")
        url = server.url
    try:
        if server is not None:
            try:
                server.wait_ready(args.startup_timeout)
            except BaseException:
                W.event(args.out, "server-failed", model=label)
                raise
            W.event(args.out, "server-ready", model=label)
        info = {
            "url": url,
            "argv": server.argv if server else None,
            "client": arm.client_settings(vendor, args),
        }
        return _send_all(arm, vendor, args, label, folder, pending, url, weights, info)
    finally:
        if server is not None:
            server.stop()
            W.event(args.out, "server-stopped", model=label)


def _send_all(arm, vendor, args, label, folder, pending, url, weights, info) -> int:
    concurrency = args.concurrency or arm.default_concurrency
    W.event(args.out, "requests-start", model=label, units=len(pending))
    started, failed = time.monotonic(), 0
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = {pool.submit(_read_safely, arm, vendor, args, p, url): p for p in pending}
        for future in as_completed(futures):
            page = futures[future]
            units, text = future.result()
            record = page_record(arm, label, page, units, text, weights, info)
            W.write_json(folder / f"{page.stem}.json", record)
            failed += record["error"] is not None
    wall = round(time.monotonic() - started, 3)
    summary = {
        "model": label,
        "pages": len(pending),
        "units": len(pending),
        "wall_seconds": wall,
        "concurrency": concurrency,
        "max_num_seqs": None,
        "finished": W.now(),
    }
    W.write_json(folder / "run.json", summary)
    W.event(args.out, "requests-done", model=label, wall_seconds=wall, errors=failed)
    return 1 if failed else 0


def prepare(arm: Arm, args: argparse.Namespace) -> int:
    check_page_level(args)
    label = args.label or arm.label
    W.event(args.out, "prepare-start", model=label)
    W.event(args.out, "prepare-done", model=label, pages=0)
    print(f"{arm.name}: nothing to prepare; the vendor prepares each image as it sends it")
    return 0


def check(arm: Arm) -> int:
    versions = {}
    for name in arm.packages:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    try:
        arm.load_vendor()
        vendor = "importable"
    except (ImportError, OSError) as failure:
        vendor = f"not importable: {failure}"
    report = {
        "arm": arm.name,
        "repo": arm.repo,
        "revision": arm.revision,
        "python": sys.version.split()[0],
        "packages": versions,
        "vendor_code": vendor,
        "recipe": str(VENVS / arm.venv / "pyproject.toml"),
    }
    print(json.dumps(report, indent=1))
    return 0 if vendor == "importable" else 2


def install(arm: Arm, venv_dir: Path) -> int:
    """`uv sync --locked` from the recipe, then the vendor's checkout if it has one."""
    recipe = VENVS / arm.venv
    if not (recipe / "uv.lock").is_file():
        raise Refusal(f"no uv.lock beside {recipe / 'pyproject.toml'}; run `uv lock` there first")
    command = ["uv", "sync", "--locked", "--project", str(recipe)]
    env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(venv_dir)}
    print(" ".join(command), f"(UV_PROJECT_ENVIRONMENT={venv_dir})", flush=True)
    code = subprocess.run(command, env=env, check=False).returncode
    if code == 0 and arm.source is not None:
        install_source(arm.source, venv_dir, arm.venv)
    return code


def install_source(source: tuple[str, str], venv_dir: Path, name: str) -> None:
    """Check out the vendor's repository at its commit and put it on the venv's path.

    The way the vendor's own instructions install it (`pip install -e .` in a clone), for
    a package whose built wheel would leave out part of the code.
    """
    url, commit = source
    dest = venv_dir / "src" / name
    if not (dest / ".git").is_dir():
        subprocess.run(["git", "clone", "--quiet", url, str(dest)], check=True)
    have = subprocess.run(
        ["git", "-C", str(dest), "cat-file", "-e", f"{commit}^{{commit}}"], check=False
    )
    if have.returncode != 0:
        subprocess.run(["git", "-C", str(dest), "fetch", "--quiet", "origin", commit], check=True)
    subprocess.run(["git", "-C", str(dest), "checkout", "--quiet", "--detach", commit], check=True)
    (site,) = (venv_dir / "lib").glob("python3*/site-packages")
    (site / f"{name}-source.pth").write_text(f"{dest}\n", encoding="utf-8")
    print(f"{url}@{commit} -> {dest}", flush=True)


def fetch(arm: Arm, store_root: Path) -> int:
    from huggingface_hub import snapshot_download

    dest = store_root / "hf" / arm.artifact
    print(f"fetching {arm.repo}@{arm.revision} -> {dest}", flush=True)
    snapshot_download(arm.repo, revision=arm.revision, local_dir=dest)
    return 0


# --- the command line ---------------------------------------------------------------


def parse_args(arm: Arm, argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog=f"operations.bakeoff.native ({arm.name})")
    sub = parser.add_subparsers(dest="command", required=True)
    i = sub.add_parser("install", help="create or sync this arm's own environment")
    i.add_argument("--venv-dir", type=Path, required=True)
    sub.add_parser("check", help="installed versions and the pinned revision; no weights")
    f = sub.add_parser("fetch", help="download the pinned snapshot into a store layout")
    f.add_argument("--store-root", type=Path, required=True)
    for name in ("run", "prepare"):
        p = sub.add_parser(name)
        p.add_argument("--pages", type=Path, required=True)
        p.add_argument("--out", type=Path, required=True)
        p.add_argument("--label", help=f"cache folder name (default: {arm.label})")
        p.add_argument("--limit", type=int, help="first N pages only (a smoke run)")
        p.add_argument("--weights", type=Path, help="a local snapshot directory")
        p.add_argument("--store-root", type=Path, help="the model store (<root>/hf/<artifact>)")
        p.add_argument("--lines", choices=["surya", "blla"], help="not used: page-level model")
        p.add_argument("--lines-dir", type=Path, help="not used: page-level model")
        p.add_argument("--device", choices=["cpu", "cuda"], default="cuda")
        p.add_argument("--threads", type=int, default=1, help="not used: the server does the work")
        p.add_argument("--server-url", help="use a running server instead of starting one")
        p.add_argument("--vllm-cmd", nargs="+", help="command prefix before 'serve'")
        p.add_argument("--port", type=int, default=arm.default_port)
        p.add_argument("--startup-timeout", type=float, default=1200)
        p.add_argument("--request-timeout", type=float, default=1800)
        p.add_argument(
            "--concurrency", type=int, help=f"pages in flight (default {arm.default_concurrency})"
        )
        arm.add_arguments(p)
    return parser.parse_args(argv)


def main(arm: Arm, argv: list[str] | None = None) -> int:
    args = parse_args(arm, argv)
    try:
        if args.command == "install":
            return install(arm, args.venv_dir)
        if args.command == "check":
            return check(arm)
        if args.command == "fetch":
            return fetch(arm, args.store_root)
        if args.command == "prepare":
            return prepare(arm, args)
        # A plain `kill` of the arm still stops its server (which runs in its own session).
        previous = signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
        try:
            return run(arm, args)
        finally:
            signal.signal(signal.SIGTERM, previous)
    except Refusal as refusal:
        print(f"refused: {refusal}", file=sys.stderr)
        return 2
