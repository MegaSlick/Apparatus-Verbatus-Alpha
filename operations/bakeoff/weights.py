"""Every bake-off arm's weights, fetched by the arm's preparation so its command runs offline.

    python -m operations.bakeoff.weights fetch --store-root STORE NAME [NAME ...]
    python -m operations.bakeoff.weights sizes NAME [NAME ...]

A queue arm's `prepare` names what its command reads; a fresh, empty volume is filled the
first time, and a later preparation that finds a name present and verified downloads
nothing. Three kinds of name:

- Roster artifacts (`chandra-ocr-2`, `dai-recordgold-atr`, `churro-3B`,
  `yolov26-record-detection`, `qwen3.8-27B`, `surya2-detection`): fetched and verified by
  the project's model store (`common/chairs/model_store.py::materialize_real_roster`, for
  the chair that uses the artifact) into `<store>/hf/<artifact>` (`local/` for Surya's
  bundle), then refused unless the store's manifest digest is the one
  `config/models-real.toml` pins. A present artifact is hashed again, never downloaded.
- Bake-off snapshots (`qwen3.5-27b`, `qwen3.5-9b`, `DotsMOCR`, `qwen3.8-27b-fp8`,
  `qwen3.8-27b-nvfp4`): the Hub repository at the commit `weight_pins.json` names, into
  `<store>/hf/<name>` (where witness_run and the native arms look), every file checked
  against the pinned size and its Hub digest (SHA-256 for LFS files, the git blob SHA-1
  for the rest; a row that also carries a SHA-256 is checked by that, so the pin also gives
  the canonical model-store manifest a later chair would seal). `<name>.verified.json` beside
  the snapshot records each file's size and modification time once it verified, so an
  unchanged snapshot is not hashed again.
- Line-arm weights (`kraken-*`, `pylaia-*`, `party-v2`, `surya-ocr-2`): the arm module's
  own `fetch`, which checks its pinned digests, into `<store>/hf/<artifact>`.

No repository named here is gated, so no token is needed; a `HF_TOKEN` in the environment
is used by the Hub client if present, and never read from a file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PINS = Path(__file__).with_name("weight_pins.json")
MANIFESTS = ROOT / "config" / "manifests"
SURYA_ENV = ROOT / "operations" / "serving" / "surya"
ROSTER = (
    "chandra-ocr-2",
    "dai-recordgold-atr",
    "churro-3B",
    "yolov26-record-detection",
    "qwen3.8-27B",
    "surya2-detection",
)
# Line-arm weights: (module, options for its fetch, bytes downloaded).
LINE_WEIGHTS: dict[str, tuple[str, dict[str, Any], int]] = {
    "kraken-ppocrv6-medium": ("kraken_ppocr", {"model": "ppocrv6"}, 63_779_644),
    "kraken-mccatmus-v1": ("kraken_ppocr", {"model": "mccatmus"}, 16_173_802),
    "kraken-mcfondue-v4": ("kraken_ppocr", {"model": "mcfondue"}, 16_377_369),
    "pylaia-belfort": ("pylaia", {"model": "belfort"}, 54_099_324),
    "pylaia-popp": ("pylaia", {"model": "popp"}, 47_094_280),
    "party-v2": ("party", {}, 1_043_313_381),
    "surya-ocr-2": ("surya_rec", {"serve": True}, 1_374_074_232),
}


class WeightsRefusal(RuntimeError):
    """A name is unknown, or what was fetched is not what is pinned."""


def pins() -> dict[str, Any]:
    return json.loads(PINS.read_text("utf-8"))


def known() -> list[str]:
    return [*ROSTER, *pins(), *LINE_WEIGHTS]


def size_of(name: str) -> int:
    """Bytes the name downloads onto a fresh volume."""
    if name in ROSTER:
        rows = json.loads((MANIFESTS / f"{name}.json").read_text("utf-8"))
        return sum(row["size"] for row in rows)
    if name in LINE_WEIGHTS:
        return LINE_WEIGHTS[name][2]
    table = pins().get(name)
    if table is None:
        raise WeightsRefusal(f"unknown weights {name!r}; known: {known()}")
    return sum(row["size"] for row in table["files"].values())


def names_in(argv: tuple[str, ...] | list[str] | None) -> list[str]:
    """The names a `weights fetch` command fetches; [] for any other command."""
    argv = list(argv or [])
    if "operations.bakeoff.weights" not in argv or "fetch" not in argv:
        return []
    names, rest = [], argv[argv.index("fetch") + 1 :]
    skip = False
    for item in rest:
        if skip:
            skip = False
        elif item.startswith("--"):
            skip = "=" not in item
        else:
            names.append(item)
    return names


# --- roster artifacts: the project's model store --------------------------------------


def _chair_of(artifact: str) -> str:
    from common.chairs.model_store import REQUIRED_ARTIFACTS

    return next(item.chair for item in REQUIRED_ARTIFACTS if item.artifact == artifact)


def fetch_roster(store: Path, artifacts: list[str], materialize: Callable | None = None) -> None:
    from common.chairs.model_store import load_download_record

    if materialize is None:
        materialize = _store_materialize
    roles = sorted({_chair_of(a) for a in artifacts})
    receipt = materialize(store, roles)
    if not receipt.get("selection_complete"):
        raise WeightsRefusal(f"the model store did not complete {artifacts}")
    with open(ROOT / "config" / "models-real.toml", "rb") as handle:
        chairs = tomllib.load(handle)["chairs"]
    rows = {row["artifact"]: row for row in load_download_record(store)["artifacts"]}
    for artifact in artifacts:
        pinned = chairs[_chair_of(artifact)]["digest_manifest"]
        if rows[artifact].get("digest_manifest") != pinned:
            raise WeightsRefusal(
                f"{artifact} in the store measures manifest {rows[artifact].get('digest_manifest')}"
                f", not the pinned {pinned}"
            )


def _store_materialize(store: Path, roles: list[str]) -> dict[str, Any]:
    from common.chairs.model_store import materialize_real_roster
    from common.chairs.registry import HuggingFaceMaterializationFetcher
    from operations.serving.surya_detector import SuryaBundleFetcher

    return materialize_real_roster(
        store,
        HuggingFaceMaterializationFetcher.from_huggingface_hub(),
        SuryaBundleFetcher(str(SURYA_ENV)),
        roles=roles,
    )


# --- bake-off snapshots: pinned here ----------------------------------------------------


def _digest(path: Path, row: dict[str, Any]) -> bool:
    if "sha256" in row:
        digest = hashlib.sha256()
    else:
        digest = hashlib.sha1(usedforsecurity=False)
        digest.update(f"blob {row['size']}\0".encode())
    with open(path, "rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest() == row.get("sha256", row.get("git_sha1"))


def snapshot_problems(dest: Path, table: dict[str, Any]) -> list[str]:
    """Pinned files missing or different; files the stamp vouches for are not re-hashed."""
    stamp_path = dest.with_name(dest.name + ".verified.json")
    try:
        stamp = json.loads(stamp_path.read_text("utf-8"))
    except (OSError, ValueError):
        stamp = {}
    problems, seen = [], {}
    for relative, row in table["files"].items():
        path = dest / relative
        try:
            stat = path.stat()
        except OSError:
            problems.append(relative)
            continue
        seen[relative] = [stat.st_size, stat.st_mtime_ns]
        if stat.st_size != row["size"]:
            problems.append(relative)
        elif stamp.get(relative) != seen[relative] and not _digest(path, row):
            problems.append(relative)
    if not problems:
        stamp_path.write_text(json.dumps({"revision": table["revision"], **seen}), "utf-8")
    return problems


def fetch_snapshot(store: Path, name: str, download: Callable | None = None) -> Path:
    table = pins()[name]
    dest = store / "hf" / name
    if dest.is_dir() and not snapshot_problems(dest, table):
        print(f"{name}: present and verified at {dest}", flush=True)
        return dest
    if download is None:
        from huggingface_hub import snapshot_download as download
    print(f"fetching {table['repo']}@{table['revision']} -> {dest}", flush=True)
    download(table["repo"], revision=table["revision"], local_dir=dest)
    problems = snapshot_problems(dest, table)
    if problems:
        raise WeightsRefusal(f"{dest} differs from the pin in {problems[:5]}")
    return dest


# --- line-arm weights: their modules' own fetch ---------------------------------------


def fetch_line(store: Path, name: str) -> Path:
    import importlib

    module_name, options, _size = LINE_WEIGHTS[name]
    module = importlib.import_module(f"operations.bakeoff.lines.{module_name}")
    arm = module.ARM if hasattr(module, "ARM") else module.arm()
    args = argparse.Namespace(**options)
    dest = store / "hf" / arm.resolved(args)["artifact"]
    return Path(arm.fetch(dest, args))


def fetch(store: Path, names: list[str]) -> None:
    unknown = [n for n in names if n not in known()]
    if unknown:
        raise WeightsRefusal(f"unknown weights {unknown}; known: {known()}")
    store.mkdir(parents=True, exist_ok=True)
    roster = [n for n in names if n in ROSTER]
    if roster:
        fetch_roster(store, roster)
    for name in names:
        if name in LINE_WEIGHTS:
            print(f"{name}: {fetch_line(store, name)}", flush=True)
        elif name not in ROSTER:
            fetch_snapshot(store, name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    f = sub.add_parser("fetch", help="fetch and verify into the store, or verify only")
    f.add_argument("--store-root", type=Path, required=True)
    f.add_argument("names", nargs="+")
    s = sub.add_parser("sizes", help="what each name downloads onto a fresh volume")
    s.add_argument("names", nargs="+")
    args = parser.parse_args(argv)
    try:
        if args.command == "sizes":
            for name in args.names:
                print(f"{name}: {size_of(name) / 1e9:.2f} GB")
            return 0
        fetch(args.store_root, args.names)
    except WeightsRefusal as refusal:
        print(f"refused: {refusal}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
