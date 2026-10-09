"""Teklia PyLaia line recognisers: arms `pylaia-<model>[-lm]-<blla|surya>` for the models
`belfort` (French handwriting 1790-1946) and `popp` (Paris census tables). CTC models:
they misread letters but cannot invent them.

    python -m operations.bakeoff.lines.pylaia install
    python -m operations.bakeoff.lines.pylaia fetch --model belfort --store-root STORE
    python -m operations.bakeoff.lines.pylaia run --model belfort [--lm] --lines surya \
        --lines-dir SURYA --pages DIR --out CACHE --store-root STORE

Each page's line crops are converted to grey and resized to 128 px high, aspect kept,
with Lanczos (PyLaia's own resize filter): both models are a `LaiaCRNN` with one input
channel and the `none-16` image sequencer after three 2x poolings, so they read lines of
exactly 128 px (the model files say so; the popp card says 128 px, the belfort card
leaves the figure blank). PyLaia's decode resizes nothing itself. Then the vendor's own
command, in PyLaia's environment, with the YAML config this module writes per page:

    pylaia-htr-decode-ctc --config <stem>.yaml

Greedy CTC by default; `--lm` adds Teklia's 6-gram character language model through
PyLaia's `CTCLanguageDecoder` (torchaudio's flashlight beam search, CPU only).
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from operations.bakeoff.lines import harness

LINE_HEIGHT = 128
MODELS = {
    "belfort": {
        "repo": "Teklia/pylaia-belfort",
        "revision": "d35f921605314afc7324310081bee55a805a0b9f",
        "weights_sha256": "e73ff66f52effd625d4063b5549755bfa0b69ed9fc28edf6c6d30af111f723cd",
        "lm_sha256": "cbb30d655c189a8f13e3a87c47252a65198493ee6d0859dcd289f97cee4ee6fa",
    },
    "popp": {
        "repo": "Teklia/pylaia-popp",
        "revision": "f002691c2610670dc609d5d7d0a7ae692b643ebc",
        "weights_sha256": "535712a733c8d5e7406d90e39a8c82c065a127dffa6762690d4960cf9d206d7a",
        "lm_sha256": "e7d2dc6ec84ff4302315e61554fe7dfc3a44dc304bb1d83c560d6da3f4582bf6",
    },
}
WEIGHT_FILES = ("model", "weights.ckpt", "syms.txt", "tokens.txt", "lexicon.txt")
LM_FILE = "language_model.arpa"
# The model cards name no weight; this is the value in PyLaia's own documentation example.
LM_WEIGHT = 1.5
BATCH_SIZE = 8
_PREDICTION = re.compile(r"^(\d{4}) (.*)$")


def resized_dir(args: argparse.Namespace, stem: str) -> Path:
    return args.out / "_lines" / f"{args.lines}-h{LINE_HEIGHT}" / stem


def resize_crops(args: argparse.Namespace, prepared: list[harness.Prepared]) -> None:
    """Grey, 128 px crops beside the shared ones; existing files are kept."""
    for item in prepared:
        folder = resized_dir(args, item.page.stem)
        for row in item.lines["lines"]:
            dest = folder / row["file"]
            if not dest.is_file():
                harness.resize_to_height(item.source_dir / row["file"], dest, LINE_HEIGHT, "L")
        item.extra["resized_dir"] = folder


def decode_config(
    args: argparse.Namespace, weights: Path, image_list: Path, image_dir: Path
) -> dict[str, Any]:
    decode: dict[str, Any] = {
        "include_img_ids": True,
        "separator": " ",
        "join_string": "",
        "use_symbols": True,
        "convert_spaces": True,
        "input_space": "<space>",
        "output_space": " ",
        "temperature": 1.0,
        "use_language_model": bool(args.lm),
    }
    if args.lm:
        decode.update(
            language_model_path=str(weights / LM_FILE),
            language_model_weight=LM_WEIGHT,
            tokens_path=str(weights / "tokens.txt"),
            lexicon_path=str(weights / "lexicon.txt"),
            blank_token="<ctc>",
            unk_token="<unk>",
        )
    return {
        "syms": str(weights / "syms.txt"),
        "img_list": str(image_list),
        "img_dirs": [str(image_dir)],
        "common": {
            "train_path": str(weights),
            "experiment_dirname": ".",
            "model_filename": "model",
            "checkpoint": "weights.ckpt",
        },
        "data": {"batch_size": BATCH_SIZE, "color_mode": "L"},
        "decode": decode,
        "trainer": {"gpus": 1 if args.device == "cuda" else 0},
    }


def parse_predictions(stdout: str) -> dict[str, str]:
    """PyLaia's decode output, one `<image id> <text>` line per image, by id."""
    found = {}
    for line in stdout.splitlines():
        match = _PREDICTION.match(line.rstrip("\n"))
        if match:
            found[match.group(1)] = match.group(2)
    return found


class PylaiaRecogniser:
    def __init__(self, args: argparse.Namespace, weights: Path, runner: Any = None) -> None:
        import subprocess

        if args.lm and not (weights / LM_FILE).is_file():
            raise harness.Refusal(f"--lm needs {weights / LM_FILE}; run fetch first")
        self.args = args
        self.weights = weights
        self.decoder = args.venv_dir / "bin" / "pylaia-htr-decode-ctc"
        self.runner = runner or subprocess.run
        self.raw_dir = args.out / args.label / "_raw"
        self.log = args.out / args.label / "arm.log"

    def settings(self) -> dict[str, Any]:
        model = MODELS[self.args.model]
        return {
            "model": model["repo"],
            "weights_sha256": model["weights_sha256"],
            "preprocessing": f"grey, {LINE_HEIGHT} px high, aspect kept, Lanczos",
            "decoder": "CTC + 6-gram char LM (flashlight beam search)"
            if self.args.lm
            else "greedy CTC",
            "language_model_weight": LM_WEIGHT if self.args.lm else None,
            "batch_size": BATCH_SIZE,
            "device": self.args.device,
        }

    def read_page(self, prepared: harness.Prepared) -> harness.PageResult:
        rows = prepared.lines["lines"]
        if not rows:
            return harness.PageResult([], None)
        image_dir = prepared.extra.get("resized_dir") or resized_dir(self.args, prepared.page.stem)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        image_list = self.raw_dir / f"{prepared.page.stem}.lst"
        image_list.write_text("".join(f"{r['order']:04d}\n" for r in rows), "utf-8")
        config = self.raw_dir / f"{prepared.page.stem}.yaml"
        config.write_text(
            json.dumps(decode_config(self.args, self.weights, image_list, image_dir), indent=1),
            "utf-8",
        )
        argv = [str(self.decoder), "--config", str(config)]
        done, seconds = harness.subprocess_page(argv, self.log, self.runner)
        predictions = parse_predictions(done.stdout) if done.returncode == 0 else {}
        share = seconds / len(rows)
        units = []
        settings = self.settings()
        for row in rows:
            request = harness.line_request(prepared, row, settings)
            resized = image_dir / row["file"]
            request["image_sent"] = {"file": str(resized), "sha256": harness.sha256_file(resized)}
            key = f"{row['order']:04d}"
            if key not in predictions:
                error = f"pylaia exited {done.returncode}; no prediction for {key}"
                units.append(harness.unit(request, None, None, share, error))
                continue
            raw = f"{key} {predictions[key]}"
            units.append(harness.unit(request, raw, predictions[key], share))
        return harness.PageResult(units, argv)

    def read(self, prepared: list[harness.Prepared]):
        return harness.iterate(prepared, self.read_page)


def fetch(dest: Path, args: argparse.Namespace) -> Path:
    """The pinned Hugging Face snapshot, with the language model as a plain ARPA file for
    torchaudio's decoder (which reads ARPA or KenLM binaries, not gzip). At the pinned
    revisions `language_model.arpa.gz` already holds plain ARPA text despite its name."""
    from huggingface_hub import snapshot_download

    model = MODELS[args.model]
    snapshot_download(model["repo"], revision=model["revision"], local_dir=dest)
    for name, digest in (
        ("weights.ckpt", "weights_sha256"),
        ("language_model.arpa.gz", "lm_sha256"),
    ):
        if harness.sha256_file(dest / name) != model[digest]:
            raise harness.Refusal(f"{dest / name} does not match the pinned SHA-256")
    arpa = dest / LM_FILE
    if not arpa.is_file():
        packed = dest / "language_model.arpa.gz"
        with open(packed, "rb") as head:
            gzipped = head.read(2) == b"\x1f\x8b"
        partial = arpa.with_suffix(".part")
        with gzip.open(packed, "rb") if gzipped else open(packed, "rb") as source:
            with open(partial, "wb") as plain:
                shutil.copyfileobj(source, plain)
        partial.replace(arpa)
    return dest


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model", choices=sorted(MODELS), default="belfort")
    parser.add_argument("--lm", action="store_true", help="decode with the 6-gram char LM")


ARM = harness.Arm(
    module="operations.bakeoff.lines.pylaia",
    repo="Teklia/pylaia-<model>",
    revision="per --model",
    artifact="pylaia-<model>",
    recipe=harness.VENVS / "pylaia",
    python="3.10",
    pins={"pylaia": "1.1.2", "torch": "1.13.1", "torchaudio": "0.13.1"},
    weight_files=WEIGHT_FILES,
    line_sources=("surya", "blla"),
    arm_name=lambda a: f"pylaia-{a.model}{'-lm' if a.lm else ''}-{a.lines}",
    recogniser=PylaiaRecogniser,
    add_arguments=_add_arguments,
    prepare_hook=resize_crops,
    fetch=fetch,
    identity=lambda a: (
        {
            "repo": MODELS[a.model]["repo"],
            "revision": MODELS[a.model]["revision"],
            "artifact": f"pylaia-{a.model}",
        }
        if getattr(a, "model", None)
        else {}
    ),
)


def main(argv: list[str] | None = None, recogniser: Any = None) -> int:
    return harness.main(ARM, argv, recogniser)


if __name__ == "__main__":
    sys.exit(main())
