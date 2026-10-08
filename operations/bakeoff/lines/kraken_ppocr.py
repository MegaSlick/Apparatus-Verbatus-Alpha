"""kraken with the PP-OCRv6 (medium) line recogniser: arms `kraken-ppocrv6-blla` and
`kraken-ppocrv6-surya`. A CTC line model: it misreads letters but cannot invent them.

    python -m operations.bakeoff.lines.kraken_ppocr install
    python -m operations.bakeoff.lines.kraken_ppocr fetch --store-root STORE
    python -m operations.bakeoff.lines.kraken_ppocr run --lines blla --pages DIR --out CACHE \
        --store-root STORE
    python -m operations.bakeoff.lines.kraken_ppocr run --lines surya --lines-dir SURYA \
        --pages DIR --out CACHE --store-root STORE

`--lines blla` is kraken's own whole path, segmentation and recognition in one command,
as the model card gives it (with ALTO out instead of text, so line order is kept):

    kraken -a -i <page> <raw>.xml segment -bl ocr -m medium.safetensors

`--lines surya` recognises the cached Surya line crops in kraken's no-segmentation mode,
the CLI's documented path for pre-cut line images (each image one bbox line):

    kraken -i 0001.png 0001.txt -i 0002.png 0002.txt ... ocr -s -m medium.safetensors

Both decode with kraken's default greedy CTC decoder; there is no language model.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.request
from pathlib import Path
from typing import Any

from operations.bakeoff.lines import blla, harness

ZENODO_RECORD = "21788410"
DOI = "10.5281/zenodo.21788410"
MODEL_FILE = "medium.safetensors"
MODEL_SHA256 = "15313b51ace64cbfa81f8f6ef25ad64f04e5a6fb7f7823e67b107527bc081ac9"
MODEL_MD5 = "e3411a453ce3b9e9efae3f8631d85762"
MODEL_SIZE = 63_779_644
ARTIFACT = "kraken-ppocrv6-medium"


class KrakenRecogniser:
    """Runs kraken's own command once per page and reads the files it writes."""

    def __init__(self, args: argparse.Namespace, weights: Path, runner: Any = None) -> None:
        import subprocess

        self.args = args
        self.model = weights / MODEL_FILE
        self.kraken = args.venv_dir / "bin" / "kraken"
        self.runner = runner or subprocess.run
        self.raw_dir = args.out / args.label / "_raw"
        self.log = args.out / args.label / "arm.log"

    def _prefix(self) -> list[str]:
        argv = [str(self.kraken), "--threads", str(self.args.threads)]
        if self.args.device == "cuda":
            argv += ["-d", "cuda:0"]
        return argv

    def settings(self) -> dict[str, Any]:
        return {
            "model": MODEL_FILE,
            "model_sha256": MODEL_SHA256,
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
        done, seconds = harness.subprocess_page(argv, self.log, self.runner)
        request = {
            "unit": "page",
            "line_source": "kraken blla (the same command's segment step)",
            "image": {"file": str(prepared.page), "sha256": harness.sha256_file(prepared.page)},
            "settings": self.settings(),
        }
        if done.returncode != 0 or not xml.is_file():
            error = f"kraken exited {done.returncode}; see arm.log"
            return harness.PageResult([harness.unit(request, None, None, seconds, error)], argv)
        raw = xml.read_text("utf-8")
        text = blla.alto_text(raw.encode("utf-8"))
        page_unit = harness.unit(request, raw, None, seconds)
        page_unit["text"] = harness.plain(text)
        return harness.PageResult([page_unit], argv)

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
        done, seconds = harness.subprocess_page(argv, self.log, self.runner)
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


def fetch(dest: Path, _args: argparse.Namespace) -> Path:
    """The model file from its Zenodo record, checked against Zenodo's MD5 and our SHA-256."""
    target = dest / MODEL_FILE
    if target.is_file() and harness.sha256_file(target) == MODEL_SHA256:
        return target
    dest.mkdir(parents=True, exist_ok=True)
    url = f"https://zenodo.org/records/{ZENODO_RECORD}/files/{MODEL_FILE}?download=1"
    return download_checked(url, target, MODEL_MD5, MODEL_SHA256)


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


ARM = harness.Arm(
    module="operations.bakeoff.lines.kraken_ppocr",
    repo=f"https://doi.org/{DOI}",
    revision=f"{MODEL_FILE}@sha256:{MODEL_SHA256}",
    artifact=ARTIFACT,
    recipe=harness.VENVS / "kraken",
    python="3.12",
    pins={"kraken": "7.1.1", "torch": "2.14.0"},
    weight_files=(MODEL_FILE,),
    line_sources=("blla", "surya"),
    arm_name=lambda args: f"kraken-ppocrv6-{args.lines}",
    recogniser=KrakenRecogniser,
    fetch=fetch,
    needs_lines=lambda args: args.lines == "surya",
)


def main(argv: list[str] | None = None, recogniser: Any = None) -> int:
    return harness.main(ARM, argv, recogniser)


if __name__ == "__main__":
    sys.exit(main())
