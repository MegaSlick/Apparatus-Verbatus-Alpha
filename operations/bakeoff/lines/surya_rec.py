"""Surya's own recogniser over its own detections: arm `surya-rec-surya`.

In surya-ocr 0.22.1 the recogniser is not a CTC line model: `RecognitionPredictor` sends
each Surya layout block (or a whole page) to Datalab's Surya OCR 2 vision-language model
(650M parameters, Qwen3.5 architecture) with the prompt "OCR this block image to HTML."
and returns HTML per block. It takes layout blocks, not text lines. `--mode page` (the
default, arm `surya-rec-surya`) is Surya's own full-page call and reads no cached
detections; `--mode blocks` (arm `surya-rec-surya-blocks`, its own cache folder) sends
the layout blocks the repository's Surya runner already cached (`--lines-dir`, matched
to pages as `surya_lines.py` says). Both run in Surya's own environment
(`operations/serving/surya/.venv`), through a worker this file also holds:

    operations/serving/surya/.venv/bin/python -I operations/bakeoff/lines/surya_rec.py \
        worker --job <job.json> --result <result.json>

The worker imports only the standard library and Surya. The model is served by Surya's
own backends: llama.cpp's `llama-server` with the pinned GGUF files (CPU, or Metal on a
Mac; `llama-server` must be on PATH), or any OpenAI-compatible server already serving
the model under the name `datalab-to/surya-ocr-2` (`--server-url`, e.g. vLLM on the GPU).
Greedy decoding, one request per block, the block's token budget Surya's own.

    python -m operations.bakeoff.lines.surya_rec run --pages DIR --out CACHE \
        --store-root STORE [--server-url http://127.0.0.1:8000/v1 | --serve] \
        [--mode blocks --lines-dir SURYA]

`--serve` is the pod's GPU path: the arm starts vLLM itself on the Hub checkpoint
(`datalab-to/surya-ocr-2` at `HF_REVISION`, `<store>/hf/surya-ocr-2`, fetched by
`fetch --serve`) under the name Surya asks for, with the witness runner's own server
launcher, sends the pages through Surya's vLLM backend, and stops the server when the
pages are done or the run fails, so the card is held for this run only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

GGUF_REPO = "datalab-to/surya-ocr-2-gguf"
GGUF_REVISION = "6a3a4c30e5e74446d4f8b6afd05b2f2da970f470"
GGUF_FILES = {
    "surya-2.gguf": "1f18abe17b1ed8b4e47ee9b1ad0e274c93daf5efbb6b29a04ff1712e37051e05",
    "surya-2-mmproj.gguf": "98c0563673b1657ff6d021d1e5f04af06cbf61bb40c63ac613e8bb71b42fb2c0",
}
SERVED_NAME = "datalab-to/surya-ocr-2"
# The Hub checkpoint vLLM serves (`Qwen3_5ForConditionalGeneration`, registered in the
# locked vLLM 0.30.0): pinned commit and the weight file's LFS SHA-256.
HF_REPO = "datalab-to/surya-ocr-2"
HF_REVISION = "3b3d4cdf88d6928b0acdc75181b13206ea67c4a3"
HF_ARTIFACT = "surya-ocr-2"
HF_WEIGHTS = {
    "model.safetensors": "5755f82a997dd0b111964fa8b31cc2daef7aeb7a706bbd17d73d6a93ef3f723e"
}
# The Hub snapshot's files vLLM reads; the README's sample images are left out.
HF_PATTERNS = ["*.json", "*.jinja", "model.safetensors", "LICENSE", "README.md"]
# Surya's full-page answer bound is 12,288 tokens; a page image adds a few thousand.
SERVE_MAX_MODEL_LEN = 32768
# Surya 0.22.1's own prompts (surya/inference/prompts.py), named here for the record;
# the worker sends whatever Surya sends.
PROMPTS = {
    "blocks": "OCR this block image to HTML.",
    "page": "OCR this image to HTML. Each block is a div with data-label and data-bbox "
    "(x0 y0 x1 y1, normalized 0-1000).",
}
PARALLEL = 8


# --- the worker: Surya's environment, standard library and Surya only ---------------


def worker(job_path: Path, result_path: Path) -> int:
    job = json.loads(job_path.read_text("utf-8"))
    env = {
        "TORCH_DEVICE": "cpu",
        "DISABLE_TQDM": "true",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "SURYA_INFERENCE_PARALLEL": str(job["parallel"]),
    }
    if job.get("server_url"):
        env.update(SURYA_INFERENCE_URL=job["server_url"], SURYA_INFERENCE_BACKEND="vllm")
    else:
        env.update(
            SURYA_INFERENCE_BACKEND="llamacpp",
            SURYA_GGUF_LOCAL_MODEL_PATH=job["gguf_model"],
            SURYA_GGUF_LOCAL_MMPROJ_PATH=job["gguf_mmproj"],
        )
    os.environ.update(env)

    from importlib import metadata

    from PIL import Image
    from surya.inference import SuryaInferenceManager
    from surya.layout.schema import LayoutResult
    from surya.recognition import RecognitionPredictor

    manager = SuryaInferenceManager()
    predictor = RecognitionPredictor(manager)
    pages = []
    try:
        for page in job["pages"]:
            started = time.monotonic()
            try:
                image = Image.open(page["image"]).convert("RGB")
                if job["mode"] == "blocks":
                    layout = LayoutResult.model_validate(page["layout"])
                    (result,) = predictor([image], [layout], full_page=False)
                else:
                    (result,) = predictor([image], full_page=True)
                blocks = [
                    block.model_dump(mode="json", exclude={"raw_logprobs"})
                    for block in result.blocks
                ]
                pages.append({"stem": page["stem"], "blocks": blocks, "error": None})
            except Exception as failure:  # noqa: BLE001 -- one page's failure is recorded, not fatal
                pages.append({"stem": page["stem"], "blocks": [], "error": repr(failure)})
            pages[-1]["seconds"] = round(time.monotonic() - started, 3)
    finally:
        manager.stop()
    facts = {"surya_ocr": metadata.version("surya-ocr"), "backend": manager.method, "env": env}
    result_path.write_text(json.dumps({"facts": facts, "pages": pages}), "utf-8")
    return 0


# --- the arm: project environment ---------------------------------------------------


def html_lines(html: str) -> list[str]:
    """A block's HTML as plain lines: block and break tags end a line, cells are spaced."""
    from operations.bakeoff.score import _strip_html

    lines = (" ".join(line.split()) for line in _strip_html(html or "").splitlines())
    return [line for line in lines if line]


class SuryaRecogniser:
    def __init__(self, args: argparse.Namespace, weights: Path, runner: Any = None) -> None:
        import subprocess

        from operations.bakeoff.lines import harness

        if args.mode == "blocks" and args.lines_dir is None:
            raise harness.Refusal("--mode blocks needs --lines-dir (the Surya runner's documents)")
        if args.serve and args.server_url:
            raise harness.Refusal("--serve starts its own server; leave out --server-url")
        if not args.serve and not args.server_url:
            if not all((weights / n).is_file() for n in GGUF_FILES):
                raise harness.Refusal(f"no GGUF files in {weights}; run fetch, or pass --serve")
        self.args = args
        self.server_url = args.server_url
        self.weights = weights
        self.runner = runner or subprocess.run
        self.raw_dir = args.out / args.label / "_raw"
        self.log = args.out / args.label / "arm.log"

    def job(self, prepared: list[Any]) -> dict[str, Any]:
        """The worker's job. Page mode reads no cached documents; block mode sends each
        page's cached layout, refused when a document is missing or another page's."""
        from operations.bakeoff.lines import harness, surya_lines

        pages = []
        try:
            documents = (
                surya_lines.match_documents(self.args.lines_dir, [p.page for p in prepared])
                if self.args.mode == "blocks"
                else {}
            )
            for item in prepared:
                layout = None
                if self.args.mode == "blocks":
                    path = documents[item.page.stem]
                    layout = surya_lines.read_document(path, item.page)["layout"]
                pages.append(
                    {"stem": item.page.stem, "image": str(item.page.resolve()), "layout": layout}
                )
        except surya_lines.LinesRefusal as refusal:
            raise harness.Refusal(str(refusal)) from refusal
        return {
            "mode": self.args.mode,
            "parallel": PARALLEL,
            "server_url": self.server_url,
            "gguf_model": str(self.weights / "surya-2.gguf"),
            "gguf_mmproj": str(self.weights / "surya-2-mmproj.gguf"),
            "pages": pages,
        }

    def read(self, prepared: list[Any]):

        self.raw_dir.mkdir(parents=True, exist_ok=True)
        server = self.serve() if self.args.serve else None
        try:
            yield from self._read(prepared)
        finally:
            if server is not None:
                from operations.bakeoff import witness_run as W

                server.stop()
                W.event(self.args.out, "server-stopped", model=self.args.label)

    def serve(self) -> Any:
        """vLLM on the card with this arm's checkpoint, ready, through witness_run's own
        launcher; refused (exit 2) when it does not come up."""
        from operations.bakeoff import witness_run as W
        from operations.bakeoff.lines import harness

        argv = serve_argv(self.weights, self.args.port, self.args.gpu_memory_utilization)
        prefix = self.args.vllm_cmd or [sys.executable, "-m", "vllm.entrypoints.cli.main"]
        W.event(self.args.out, "server-start", model=self.args.label, argv=[*prefix, *argv])
        server = W.Server(prefix, argv, self.args.port, self.raw_dir.parent / "server.log")
        try:
            server.wait_ready(self.args.startup_timeout)
        except RuntimeError as failure:
            server.stop()
            W.event(self.args.out, "server-failed", model=self.args.label)
            raise harness.Refusal(f"vLLM did not come up ({failure}); see server.log") from failure
        W.event(self.args.out, "server-ready", model=self.args.label)
        self.server_url = server.url + "/v1"
        return server

    def _read(self, prepared: list[Any]):
        from operations.bakeoff.lines import harness

        job_path, result_path = self.raw_dir / "job.json", self.raw_dir / "result.json"
        job = self.job(prepared)
        job_path.write_text(json.dumps(job), "utf-8")
        result_path.unlink(missing_ok=True)
        argv = [
            str(self.args.venv_dir / "bin" / "python"),
            "-I",
            str(Path(__file__).resolve()),
            "worker",
            "--job",
            str(job_path),
            "--result",
            str(result_path),
        ]
        done, seconds = harness.subprocess_page(argv, self.log, self.runner)
        result = json.loads(result_path.read_text("utf-8")) if result_path.is_file() else None
        answers = {page["stem"]: page for page in (result or {}).get("pages", [])}
        for item in prepared:
            answer = answers.get(item.page.stem)
            if answer is None or answer["error"]:
                why = answer["error"] if answer else f"worker exited {done.returncode}; see arm.log"
                request = {"unit": "page", "image": {"file": str(item.page)}}
                yield (
                    item,
                    harness.PageResult(
                        [harness.unit(request, None, None, seconds / len(prepared), why)], argv
                    ),
                )
                continue
            share = answer["seconds"] / max(1, len(answer["blocks"]))
            units = []
            for block in sorted(answer["blocks"], key=lambda b: b["reading_order"]):
                request = {
                    "unit": f"block-{block['reading_order']:03d}",
                    "line_source": "surya layout blocks (cached runner documents)",
                    "label": block["label"],
                    "polygon": block["polygon"],
                    "skipped": block["skipped"],
                    "settings": {
                        "mode": self.args.mode,
                        "prompt": PROMPTS[self.args.mode],
                        "decoding": "greedy",
                        "backend": (result or {}).get("facts", {}).get("backend"),
                        "served": SERVED_NAME,
                    },
                }
                text = "\n".join(html_lines(block["html"]))
                error = "Surya block OCR failed" if block.get("error") else None
                entry = harness.unit(request, block["html"], None, share, error)
                entry["text"] = harness.plain(text) if error is None else None
                units.append(entry)
            yield item, harness.PageResult(units, argv)


def serve_argv(weights: Path, port: int, gpu_memory_utilization: float) -> list[str]:
    """`vllm serve` for the checkpoint: the name Surya's vLLM backend asks for, bf16, the
    checkpoint's own image processing, sampling left to Surya's requests (greedy)."""
    return [
        "serve", str(weights), "--tokenizer", str(weights),
        "--host", "127.0.0.1", "--port", str(port),
        "--served-model-name", SERVED_NAME,
        "--dtype", "bfloat16", "--seed", "0",
        "--max-model-len", str(SERVE_MAX_MODEL_LEN),
        "--gpu-memory-utilization", f"{gpu_memory_utilization:.2f}",
        "--generation-config", "vllm",
        "--no-enable-log-requests", "--no-trust-remote-code",
    ]  # fmt: skip


def fetch(dest: Path, args: argparse.Namespace) -> Path:
    from huggingface_hub import hf_hub_download, snapshot_download

    from operations.bakeoff.lines import harness

    if getattr(args, "serve", False):
        snapshot_download(HF_REPO, revision=HF_REVISION, local_dir=dest, allow_patterns=HF_PATTERNS)
        for name, digest in HF_WEIGHTS.items():
            if harness.sha256_file(dest / name) != digest:
                raise harness.Refusal(f"{dest / name} does not match the pinned SHA-256")
        return dest
    for name, digest in GGUF_FILES.items():
        path = Path(hf_hub_download(GGUF_REPO, name, revision=GGUF_REVISION, local_dir=dest))
        if harness.sha256_file(path) != digest:
            raise harness.Refusal(f"{path} does not match the pinned SHA-256")
    hf_hub_download(GGUF_REPO, "LICENSE", revision=GGUF_REVISION, local_dir=dest)
    return dest


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--server-url", help="an OpenAI-compatible server for the model")
    parser.add_argument(
        "--mode",
        choices=["page", "blocks"],
        default="page",
        help="page: Surya's default full-page OCR; blocks: OCR of the cached layout blocks",
    )
    parser.add_argument(
        "--serve", action="store_true", help="start vLLM on the card for this run (the pod)"
    )
    parser.add_argument("--port", type=int, default=8191)
    parser.add_argument("--vllm-cmd", nargs="+", help="command prefix before 'serve'")
    parser.add_argument("--startup-timeout", type=float, default=1200)
    parser.add_argument("--gpu-memory-utilization", type=float, default=0.92)


def _identity(args: argparse.Namespace) -> dict[str, str]:
    if getattr(args, "serve", False):
        return {"repo": HF_REPO, "revision": HF_REVISION, "artifact": HF_ARTIFACT}
    return {"repo": GGUF_REPO, "revision": GGUF_REVISION, "artifact": "surya-ocr-2-gguf"}


def arm() -> Any:
    from operations.bakeoff.lines import harness

    surya_env = harness.ROOT / "operations" / "serving" / "surya"
    return harness.Arm(
        module="operations.bakeoff.lines.surya_rec",
        repo=GGUF_REPO,
        revision=GGUF_REVISION,
        artifact="surya-ocr-2-gguf",
        recipe=surya_env,
        python="3.12",
        pins={"surya-ocr": "0.22.1"},
        weight_files=(),
        line_sources=("surya",),
        # The two modes answer differently, so they never share a cache folder.
        arm_name=lambda args: "surya-rec-surya" + ("-blocks" if args.mode == "blocks" else ""),
        recogniser=SuryaRecogniser,
        add_arguments=_add_arguments,
        fetch=fetch,
        needs_lines=lambda args: False,
        identity=_identity,
        weight_files_for=lambda args: (
            ("config.json", *HF_WEIGHTS) if getattr(args, "serve", False) else ()
        ),
    )


def main(argv: list[str] | None = None, recogniser: Any = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv[:1] == ["worker"]:
        parser = argparse.ArgumentParser()
        parser.add_argument("--job", type=Path, required=True)
        parser.add_argument("--result", type=Path, required=True)
        args = parser.parse_args(argv[1:])
        return worker(args.job, args.result)
    from operations.bakeoff.lines import harness

    return harness.main(arm(), argv, recogniser)


if __name__ == "__main__":
    sys.exit(main())
