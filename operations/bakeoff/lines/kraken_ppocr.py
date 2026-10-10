"""kraken line recognisers: arms `kraken-<model>-<blla|surya>` for the models `ppocrv6`
(PP-OCRv6 medium, the default), `mccatmus` (McCATMuS v1, 16th c. to present, mostly
French) and `mcfondue` (Manu McFondue v4, French 17th-20th c.). CTC line models: they
misread letters but cannot invent them.

    python -m operations.bakeoff.lines.kraken_ppocr install
    python -m operations.bakeoff.lines.kraken_ppocr fetch [--model M] --store-root STORE
    python -m operations.bakeoff.lines.kraken_ppocr run [--model M] --lines blla --pages DIR \
        --out CACHE --store-root STORE
    python -m operations.bakeoff.lines.kraken_ppocr run [--model M] --lines surya \
        --lines-dir SURYA --pages DIR --out CACHE --store-root STORE

`--lines blla` is kraken's own whole path, segmentation and recognition in one command,
as the PP-OCRv6 model card gives it (with ALTO out instead of text, so line order is kept):

    kraken -d cpu|cuda:0 -a -i <page> <raw>.xml segment -bl ocr -m <model file>

Each TextLine of the answer becomes one unit (its polygon's box, baseline and text).

`--lines surya` recognises the cached Surya line crops in kraken's no-segmentation mode,
the CLI's documented path for pre-cut line images (each image one bbox line):

    kraken -d cpu|cuda:0 -i 0001.png 0001.txt -i 0002.png 0002.txt ... ocr -s -m <model file>

All decode with kraken's default greedy CTC decoder; there is no language model. The
device is always named: kraken's default `auto` would take a visible GPU on a CPU run.
McCATMuS and McFondue are kraken 4.x CoreML `.mlmodel` files, which kraken 7 reads with
its `coreml` loader; McCATMuS writes NFD, and `harness.plain` turns every answer to NFC.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.request
from pathlib import Path
from typing import Any

from operations.bakeoff.lines import blla, harness

# Each file pinned by its Zenodo record, Zenodo's MD5 and the SHA-256 measured here.
MODELS = {
    "ppocrv6": {
        "record": "21788410",
        "doi": "10.5281/zenodo.21788410",
        "file": "medium.safetensors",
        "sha256": "15313b51ace64cbfa81f8f6ef25ad64f04e5a6fb7f7823e67b107527bc081ac9",
        "md5": "e3411a453ce3b9e9efae3f8631d85762",
        "size": 63_779_644,
        "artifact": "kraken-ppocrv6-medium",
    },
    "mccatmus": {
        "record": "13788177",
        "doi": "10.5281/zenodo.13788177",
        "file": "McCATMuS_nfd_nofix_V1.mlmodel",
        "sha256": "dfb911ba25fd11f93efc1b0c340957162981ecfdaac0ee1e26793d491f770244",
        "md5": "e531463f631303c700750784b4f9ed63",
        "size": 16_173_802,
        "artifact": "kraken-mccatmus-v1",
    },
    "mcfondue": {
        "record": "10886224",
        "doi": "10.5281/zenodo.10886224",
        "file": "ManuMcFondue.mlmodel",
        "sha256": "96e32e782b6627aa57961a6ed5a84c522174630c6bc9f6ebc83eedce1f5e490e",
        "md5": "c1c3c628f79f19a7a8116a9afd5c47d2",
        "size": 16_377_369,
        "artifact": "kraken-mcfondue-v4",
    },
}
DEFAULT_MODEL = "ppocrv6"


def model_of(args: argparse.Namespace) -> dict[str, Any]:
    return MODELS[getattr(args, "model", None) or DEFAULT_MODEL]


class KrakenRecogniser:
    """Runs kraken's own command once per page and reads the files it writes."""

    def __init__(self, args: argparse.Namespace, weights: Path, runner: Any = None) -> None:
        import subprocess

        self.args = args
        self.pinned = model_of(args)
        self.model = weights / self.pinned["file"]
        self.kraken = args.venv_dir / "bin" / "kraken"
        self.runner = runner or subprocess.run
        self.raw_dir = args.out / args.label / "_raw"
        self.log = args.out / args.label / "arm.log"

    def _prefix(self) -> list[str]:
        return [
            str(self.kraken),
            *blla.device_argv(self.args.device),
            "--threads",
            str(self.args.threads),
        ]

    def settings(self) -> dict[str, Any]:
        return {
            "model": self.pinned["file"],
            "model_sha256": self.pinned["sha256"],
            "decoder": "kraken greedy CTC (default), no language model",
            "padding": "kraken default (16 px left and right)",
            "device": self.args.device,
            "threads": self.args.threads,
        }

    def page_argv(self, page: Path, xml: Path) -> list[str]:
        return [
            *self._prefix(),
            "-a",
            "-i",
            str(page),
            str(xml),
            "segment",
            "-bl",
            "ocr",
            "-m",
            str(self.model),
        ]

    def lines_argv(self, pairs: list[tuple[Path, Path]]) -> list[str]:
        argv = self._prefix()
        for crop, text in pairs:
            argv += ["-i", str(crop), str(text)]
        return [*argv, "ocr", "-s", "-m", str(self.model)]

    def read_page(self, prepared: harness.Prepared) -> harness.PageResult:
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        xml = self.raw_dir / f"{prepared.page.stem}.xml"
        xml.unlink(missing_ok=True)
        argv = self.page_argv(prepared.page, xml)
        done, seconds = harness.subprocess_page(
            argv, self.log, self.runner, harness.thread_env(self.args.threads)
        )
        request = {
            "line_source": "blla (this command's own segment step)",
            "image": {"file": str(prepared.page), "sha256": harness.sha256_file(prepared.page)},
            "settings": self.settings(),
        }
        if done.returncode != 0 or not xml.is_file():
            error = f"kraken exited {done.returncode}; see arm.log"
            request["unit"] = "page"
            return harness.PageResult([harness.unit(request, None, None, seconds, error)], argv)
        return harness.PageResult(blla.alto_units(prepared.page, xml, request, seconds), argv)

    def read_lines(self, prepared: harness.Prepared) -> harness.PageResult:
        rows = prepared.lines["lines"]
        if not rows:
            return harness.PageResult([], None)
        folder = self.raw_dir / prepared.page.stem
        folder.mkdir(parents=True, exist_ok=True)
        pairs = [(prepared.source_dir / r["file"], folder / f"{r['order']:04d}.txt") for r in rows]
        for _, text in pairs:
            text.unlink(missing_ok=True)
        argv = self.lines_argv(pairs)
        done, seconds = harness.subprocess_page(
            argv, self.log, self.runner, harness.thread_env(self.args.threads)
        )
        share = seconds / len(rows)
        units = []
        for row, (_, text_file) in zip(rows, pairs, strict=True):
            request = harness.line_request(prepared, row, self.settings())
            if done.returncode != 0 or not text_file.is_file():
                error = f"kraken exited {done.returncode}; no output for this line"
                units.append(harness.unit(request, None, None, share, error))
                continue
            raw = text_file.read_text("utf-8")
            units.append(harness.unit(request, raw, raw, share))
        return harness.PageResult(units, argv)

    def read(self, prepared: list[harness.Prepared]):
        one = self.read_page if self.args.lines == "blla" else self.read_lines
        return harness.iterate(prepared, one)


def fetch(dest: Path, args: argparse.Namespace) -> Path:
    """The model file from its Zenodo record, checked against Zenodo's MD5 and our SHA-256."""
    pinned = model_of(args)
    target = dest / pinned["file"]
    if target.is_file() and harness.sha256_file(target) == pinned["sha256"]:
        return target
    dest.mkdir(parents=True, exist_ok=True)
    url = f"https://zenodo.org/records/{pinned['record']}/files/{pinned['file']}?download=1"
    return download_checked(url, target, pinned["md5"], pinned["sha256"])


def download_checked(url: str, target: Path, md5: str, sha256: str) -> Path:
    partial = target.with_suffix(target.suffix + ".part")
    digests = (hashlib.md5(usedforsecurity=False), hashlib.sha256())
    with urllib.request.urlopen(url, timeout=120) as response, open(partial, "wb") as handle:
        while chunk := response.read(1 << 20):
            handle.write(chunk)
            for digest in digests:
                digest.update(chunk)
    if (digests[0].hexdigest(), digests[1].hexdigest()) != (md5, sha256):
        partial.unlink()
        raise harness.Refusal(f"{url} does not match the pinned digests")
    partial.replace(target)
    return target


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model", choices=sorted(MODELS), default=DEFAULT_MODEL)


def _identity(args: argparse.Namespace) -> dict[str, str]:
    pinned = model_of(args)
    return {
        "repo": f"https://doi.org/{pinned['doi']}",
        "revision": f"{pinned['file']}@sha256:{pinned['sha256']}",
        "artifact": pinned["artifact"],
    }


ARM = harness.Arm(
    module="operations.bakeoff.lines.kraken_ppocr",
    repo="https://doi.org/<per --model>",
    revision="per --model",
    artifact="kraken-<model>",
    recipe=harness.VENVS / "kraken",
    python="3.12",
    pins={"kraken": "7.1.1", "torch": "2.14.0"},
    weight_files=(),
    line_sources=("blla", "surya"),
    arm_name=lambda args: f"kraken-{args.model}-{args.lines}",
    recogniser=KrakenRecogniser,
    add_arguments=_add_arguments,
    fetch=fetch,
    needs_lines=lambda args: args.lines == "surya",
    identity=_identity,
    weight_files_for=lambda args: (model_of(args)["file"],),
)


def main(argv: list[str] | None = None, recogniser: Any = None) -> int:
    return harness.main(ARM, argv, recogniser)


if __name__ == "__main__":
    sys.exit(main())
