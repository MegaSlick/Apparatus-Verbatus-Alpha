"""The command line and cache record every CTC line arm shares.

    <arm module> run --pages DIR --out CACHE [--label NAME] [--limit N] [--weights PATH]
                     [--store-root DIR] [--lines surya|blla] [--lines-dir DIR]
                     [--device cpu|cuda] [--threads N] [--venv-dir DIR]
    <arm module> install [--venv-dir DIR]   # syncs the arm's pinned environment; idempotent
    <arm module> check [--venv-dir DIR]     # installed versions against the pins; no weights
    <arm module> prepare --pages DIR --out CACHE [--lines ...]   # line crops only
    <arm module> fetch --store-root DIR     # the pinned weights into <store>/hf/<artifact>

An arm module supplies an `Arm`: its pins, its environment, how to find its weights, and a
recogniser. A recogniser takes the prepared pages and yields one `PageResult` per page;
the real ones run the vendor's own command in the vendor's environment, and the tests
hand in fakes. The harness writes `<out>/<label>/<stem>.json` in the witness runner's
record shape (`bakeoff-witness-page.v1`), `run.json` and `arm.log` beside the pages, and
`events.jsonl` at the top. A page cached without an error is skipped, so a run resumes.

Exit codes: 0 every page cached, 1 a page errored, 2 refused (no weights, no environment,
no line source).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import unicodedata
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from operations.bakeoff import witness_run as W
from operations.bakeoff.score import loop_flag

ROOT = Path(__file__).resolve().parents[3]
VENVS = ROOT / "operations" / "bakeoff" / "lines" / "venvs"
UV_VERSION = "0.12.1"


class Refusal(RuntimeError):
    """The run cannot start as asked; nothing is written for any page."""


@dataclass
class Prepared:
    """One page ready for a recogniser: the page file and, for line arms, its crop index."""

    page: Path
    lines: dict[str, Any] | None = None
    source_dir: Path | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class PageResult:
    """What a recogniser returns for one page; `units` are in reading order."""

    units: list[dict[str, Any]]
    argv: list[str] | None
    error: str | None = None


@dataclass
class Arm:
    """One arm module's pins and hooks."""

    module: str
    repo: str
    revision: str
    artifact: str
    recipe: Path  # the folder holding pyproject.toml (and uv.lock when it resolved)
    python: str  # the recipe's Python, for an install without a lock
    pins: dict[str, str]  # distribution -> version the check expects
    weight_files: tuple[str, ...]  # files a weights folder must hold
    line_sources: tuple[str, ...]  # the `--lines` choices; () for none
    arm_name: Callable[[argparse.Namespace], str]
    recogniser: Callable[[argparse.Namespace, Path], Any]
    add_arguments: Callable[[argparse.ArgumentParser], None] = lambda parser: None
    prepare_hook: Callable[[argparse.Namespace, list[Prepared]], None] = lambda a, p: None
    fetch: Callable[[Path, argparse.Namespace], Path] | None = None
    needs_lines: Callable[[argparse.Namespace], bool] = lambda args: True
    # Arms whose repo, revision and artifact depend on an option (PyLaia's --model).
    identity: Callable[[argparse.Namespace], dict[str, str]] | None = None
    # Arms whose weight files depend on an option (kraken's --model).
    weight_files_for: Callable[[argparse.Namespace], tuple[str, ...]] | None = None

    def resolved(self, args: argparse.Namespace) -> dict[str, str]:
        fixed = {"repo": self.repo, "revision": self.revision, "artifact": self.artifact}
        return {**fixed, **(self.identity(args) if self.identity else {})}

    def files(self, args: argparse.Namespace) -> tuple[str, ...]:
        return self.weight_files_for(args) if self.weight_files_for else self.weight_files

    def default_venv(self) -> Path:
        return self.recipe / ".venv"


# --- units and records ----------------------------------------------------------------


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def line_request(prepared: Prepared, row: dict[str, Any], settings: dict[str, Any]) -> dict:
    """Everything about one line crop sent to a recogniser."""
    crop = prepared.source_dir / row["file"]
    from PIL import Image

    with Image.open(crop) as image:
        size, mode = list(image.size), image.mode
    return {
        "unit": f"line-{row['order']:04d}",
        "line_source": prepared.lines["source"],
        "order": row["order"],
        "bbox": row["bbox"],
        "image": {"file": str(crop), "size": size, "mode": mode, "sha256": sha256_file(crop)},
        "settings": settings,
    }


def plain(text: str | None) -> str:
    """Plain lines: NFC (kraken's and Party's models write NFD), each line's whitespace
    collapsed, empty lines dropped."""
    lines = (
        " ".join(line.split()) for line in unicodedata.normalize("NFC", text or "").splitlines()
    )
    return "\n".join(line for line in lines if line)


def unit(
    request: dict[str, Any],
    raw: str | None,
    text: str | None,
    seconds: float,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "request": request,
        "raw_response": raw,
        "text": plain(text) if error is None else text,
        "finish_reason": "stop" if error is None else None,
        "seconds": round(seconds, 3),
        "error": error,
    }


def page_record(arm: Arm, args: argparse.Namespace, page: Path, result: PageResult) -> dict:
    texts = [u["text"] or "" for u in result.units]
    errors = [u["error"] for u in result.units if u["error"]]
    if result.error:
        errors.insert(0, result.error)
    text = plain("\n".join(texts))
    finish = "error" if errors else ("stop" if result.units else "no-lines")
    looped = loop_flag(text, finish)
    return {
        "schema": W.SCHEMA,
        "model": args.label,
        "arm": args.arm,
        "repo": arm.resolved(args)["repo"],
        "revision": arm.resolved(args)["revision"],
        "weights": str(args.weights),
        "server": {"url": None, "argv": result.argv},
        "page": page.stem,
        "source_file": page.name,
        "source_sha256": sha256_file(page),
        "units": result.units,
        "text": text,
        "empty": not text.strip(),
        "finish_reason": finish,
        "loop": looped[0],
        "loop_reasons": [looped[1]] if looped[0] else [],
        "seconds": round(sum(u["seconds"] for u in result.units), 3),
        "error": "; ".join(errors) if errors else None,
        "written": W.now(),
    }


# --- line sources ---------------------------------------------------------------------


def prepare_lines(arm: Arm, args: argparse.Namespace, pages: list[Path]) -> list[Prepared]:
    if not arm.line_sources or not arm.needs_lines(args):
        return [Prepared(page) for page in pages]
    if args.lines == "surya":
        from operations.bakeoff.lines import surya_lines

        if args.lines_dir is None:
            raise Refusal("--lines surya needs --lines-dir (the Surya runner's page documents)")
        try:
            indexes = surya_lines.prepare(pages, args.lines_dir, args.out)
        except surya_lines.LinesRefusal as refusal:
            raise Refusal(str(refusal)) from refusal
    else:
        from operations.bakeoff.lines import blla

        kraken_venv = args.kraken_venv or blla.DEFAULT_VENV
        missing = [
            p for p in pages if not (args.out / "_lines" / "blla" / f"{p.stem}.xml").is_file()
        ]
        if missing and not (kraken_venv / "bin" / "kraken").is_file():
            raise Refusal(
                f"no blla segmentation for {[p.stem for p in missing]} and no kraken at "
                f"{kraken_venv}; run `python -m operations.bakeoff.lines.blla prepare` first"
            )
        try:
            indexes = blla.prepare(pages, args.out, kraken_venv, args.device)
        except RuntimeError as failure:
            raise Refusal(str(failure)) from failure
    folder = args.out / "_lines" / args.lines
    prepared = [Prepared(p, indexes[p.stem], folder / p.stem) for p in pages]
    arm.prepare_hook(args, prepared)
    return prepared


# --- the subcommands ------------------------------------------------------------------


def resolve_weights(arm: Arm, args: argparse.Namespace) -> Path:
    candidates = [args.weights] if args.weights else []
    if not candidates and args.store_root:
        candidates.append(args.store_root / "hf" / arm.resolved(args)["artifact"])
    for path in candidates:
        if all((path / name).is_file() for name in arm.files(args)):
            return path
    raise Refusal(
        f"no weights for {arm.module}: pass --weights or --store-root holding "
        f"{list(arm.files(args))} (looked in {[str(c) for c in candidates] or 'nothing'})"
    )


def run(arm: Arm, args: argparse.Namespace, recogniser: Any = None) -> int:
    """`recogniser`, when given, is a factory `(args, weights)` used in place of the arm's
    own, and the arm's environment is not required."""
    pages = W.list_pages(args.pages)[: args.limit or None]
    args.arm = arm.arm_name(args)
    args.label = args.label or args.arm
    args.weights = resolve_weights(arm, args)
    if recogniser is None:
        python = args.venv_dir / "bin" / "python"
        if not python.is_file():
            raise Refusal(f"no environment at {args.venv_dir}; run `{arm.module} install` first")
    reader = (recogniser or arm.recogniser)(args, args.weights)
    folder = args.out / args.label
    folder.mkdir(parents=True, exist_ok=True)
    pending = [p for p in pages if not W.cached_ok(folder / f"{p.stem}.json")]
    if not pending:
        W.event(args.out, "nothing-to-do", model=args.label)
        return 0
    W.event(args.out, "prepare-start", model=args.label)
    prepared = prepare_lines(arm, args, pending)
    W.event(args.out, "prepare-done", model=args.label, pages=len(prepared))
    units = sum(len((p.lines or {}).get("lines", [])) or 1 for p in prepared)
    W.event(args.out, "requests-start", model=args.label, units=units)
    started = time.monotonic()
    log = folder / "arm.log"
    for item, result in reader.read(prepared):
        record = page_record(arm, args, item.page, result)
        W.write_json(folder / f"{item.page.stem}.json", record)
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(
                json.dumps({"t": W.now(), "page": item.page.stem, "error": record["error"]}) + "\n"
            )
    wall = round(time.monotonic() - started, 3)
    W.write_json(
        folder / "run.json",
        {
            "model": args.label,
            "pages": len(prepared),
            "units": units,
            "wall_seconds": wall,
            "concurrency": 1,
            "max_num_seqs": None,
            "finished": W.now(),
        },
    )
    W.event(args.out, "requests-done", model=args.label, wall_seconds=wall)
    return 0 if all(W.cached_ok(folder / f"{p.stem}.json") for p in pages) else 1


def install(arm: Arm, venv_dir: Path, runner: Any = subprocess.run) -> int:
    """`uv sync --frozen` against the recipe's lock, into `venv_dir`; without a lock, a
    plain venv with the exact pins. Both leave a synced environment untouched."""
    env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(venv_dir.resolve())}
    if (arm.recipe / "uv.lock").is_file():
        argv = ["uv", "sync", "--frozen", "--project", str(arm.recipe)]
        return runner(argv, env=env, check=False).returncode
    steps = [
        ["uv", "venv", "--allow-existing", "--python", arm.python, str(venv_dir)],
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(venv_dir / "bin" / "python"),
            *(f"{name}=={version}" for name, version in arm.pins.items()),
        ],
    ]
    for argv in steps:
        code = runner(argv, env=env, check=False).returncode
        if code:
            return code
    return 0


def installed_versions(venv_dir: Path, names: list[str], runner: Any = subprocess.run) -> dict:
    script = (
        "import json, sys\nfrom importlib import metadata\nout = {}\n"
        "for name in sys.argv[1:]:\n"
        "    try:\n        out[name] = metadata.version(name)\n"
        "    except metadata.PackageNotFoundError:\n        out[name] = None\n"
        "print(json.dumps(out))\n"
    )
    python = venv_dir / "bin" / "python"
    if not python.is_file():
        return {name: None for name in names}
    done = runner(
        [str(python), "-I", "-c", script, *names], capture_output=True, text=True, check=False
    )
    if done.returncode != 0:
        return {name: None for name in names}
    return json.loads(done.stdout)


def check(arm: Arm, venv_dir: Path, runner: Any = subprocess.run) -> int:
    found = installed_versions(venv_dir, list(arm.pins), runner)
    report = {
        "module": arm.module,
        "repo": arm.repo,
        "revision": arm.revision,
        "line_sources": list(arm.line_sources),
        "recipe": str(arm.recipe),
        "locked": (arm.recipe / "uv.lock").is_file(),
        "venv": str(venv_dir),
        "pins": arm.pins,
        "installed": found,
        "ok": all(found.get(name) == version for name, version in arm.pins.items()),
    }
    print(json.dumps(report, indent=1))
    return 0 if report["ok"] else 2


def parser_for(arm: Arm) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"python -m {arm.module}")
    sub = parser.add_subparsers(dest="command", required=True)
    commands = {
        name: sub.add_parser(name) for name in ("run", "install", "check", "prepare", "fetch")
    }
    for name, p in commands.items():
        if name in ("install", "check", "run"):
            p.add_argument("--venv-dir", type=Path, default=arm.default_venv())
        if name in ("run", "prepare"):
            p.add_argument("--pages", type=Path, required=True)
            p.add_argument("--out", type=Path, required=True)
            p.add_argument("--limit", type=int)
            p.add_argument("--device", default="cpu", choices=["cpu", "cuda"])
            p.add_argument("--kraken-venv", type=Path, help="kraken's environment, for blla")
            if arm.line_sources:
                p.add_argument("--lines", choices=arm.line_sources, default=arm.line_sources[0])
                p.add_argument("--lines-dir", type=Path, help="the Surya runner's documents")
        if name in ("run", "fetch"):
            p.add_argument("--store-root", type=Path, required=name == "fetch")
        if name == "run":
            p.add_argument("--label")
            p.add_argument("--weights", type=Path)
            p.add_argument("--threads", type=int, default=4)
        if name in ("run", "prepare", "check", "fetch"):
            arm.add_arguments(p)
    return parser


def main(arm: Arm, argv: list[str] | None = None, recogniser: Any = None) -> int:
    args = parser_for(arm).parse_args(argv)
    try:
        if args.command == "install":
            return install(arm, args.venv_dir)
        if args.command == "check":
            return check(arm, args.venv_dir)
        if args.command == "fetch":
            if arm.fetch is None:
                raise Refusal(f"{arm.module} has no fetch")
            print(arm.fetch(args.store_root / "hf" / arm.resolved(args)["artifact"], args))
            return 0
        if args.command == "prepare":
            args.arm = arm.arm_name(args)
            pages = W.list_pages(args.pages)[: args.limit or None]
            prepared = prepare_lines(arm, args, pages)
            print(f"prepared {len(prepared)} pages for {args.arm}")
            return 0
        return run(arm, args, recogniser)
    except Refusal as refusal:
        print(f"refused: {refusal}", file=sys.stderr)
        return 2


def thread_env(threads: int) -> dict[str, str]:
    """Caps a vendor command's math libraries at the arm's thread share. Without it torch
    sizes its pool from the host's cores (128 on a RunPod host), not the pod's quota."""

    n = str(max(1, int(threads)))
    return {name: n for name in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS")}


def subprocess_page(
    argv: list[str], log: Path, runner: Any, env: dict[str, str] | None = None
) -> tuple[subprocess.CompletedProcess, float]:
    """One vendor command, its output appended to the arm's log, offline."""
    started = time.monotonic()
    done = runner(
        argv,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **W.OFFLINE_ENV, **(env or {})},
    )
    seconds = time.monotonic() - started
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(f"$ {' '.join(argv)}\n{done.stdout}{done.stderr}\n")
    return done, seconds


def iterate(prepared: list[Prepared], one: Callable[[Prepared], PageResult]) -> Iterator:
    for item in prepared:
        yield item, one(item)


def resize_to_height(source: Path, dest: Path, height: int, mode: str) -> None:
    """The crop at a fixed height, aspect kept, Lanczos (PyLaia's own resize filter)."""
    from PIL import Image

    with Image.open(source) as image:
        image = image.convert(mode)
        width = max(1, image.width * height // max(1, image.height))
        dest.parent.mkdir(parents=True, exist_ok=True)
        image.resize((width, height), resample=Image.Resampling.LANCZOS).save(dest, format="PNG")
