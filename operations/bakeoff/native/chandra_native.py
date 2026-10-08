"""Chandra OCR 2 through datalab's own `chandra` package, its pipeline end to end.

    <chandra venv>/bin/python -m operations.bakeoff.native.chandra_native run \\
        --pages DIR --out CACHE --store-root STORE

What runs is the package (`chandra-ocr` 0.2.0, github.com/datalab-to/chandra at the
commit below): `chandra.input.load_image`, then
`chandra.model.InferenceManager(method="vllm").generate`, which applies `scale_to_fit`,
sends the `ocr_layout` prompt with a 12,384-token bound at temperature 0 and top_p 0.1,
and re-asks up to six times (temperature +0.2 each time, top_p 0.95) when the answer
ends in a repeated run or the request fails; then `chandra.output.parse_markdown`. The
server takes the flags of the package's own launcher (`chandra/scripts/vllm.py`), on
the project's vLLM rather than datalab's Docker image. `markdown_lines` turns the
package's markdown into plain lines.
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import re
import time
from html import unescape
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from operations.bakeoff.native import base

REPO = "datalab-to/chandra-ocr-2"
REVISION = "af93b47dba1b47b6640c86ccf487ed2260ab9a09"
PACKAGE = {
    "distribution": "chandra-ocr",
    "version": "0.2.0",
    "repository": "github.com/datalab-to/chandra",
    "commit": "d4f7467435aa4137d9539f000ddf0b7ced3eb43f",
}
PROMPT_TYPE = "ocr_layout"  # what `chandra.scripts.cli` asks for every page
SERVED_DEFAULT = "chandra"  # `chandra.settings.Settings.VLLM_MODEL_NAME`

# `chandra/scripts/vllm.py`: settings scaled from an 80 GB H100 by the card's memory.
BASELINE_VRAM_GB = 80
BASELINE_MAX_BATCHED_TOKENS = 8192
BASELINE_MAX_NUM_SEQS = 64
VENDOR_MAX_MODEL_LEN = 18_000
VENDOR_PIXELS = {"min_pixels": 3136, "max_pixels": 6291456}
VENDOR_BATCH_SIZE = 28  # `chandra.scripts.cli`: pages per batch on vLLM, one worker each


def vendor_gpu_settings(vram_gb: int) -> tuple[int, int]:
    """(max-num-batched-tokens, max-num-seqs) by `chandra/scripts/vllm.py::get_gpu_settings`."""
    ratio = vram_gb / BASELINE_VRAM_GB
    batched = max(1024, 2 ** math.floor(math.log2(BASELINE_MAX_BATCHED_TOKENS * ratio)))
    seqs = max(8, (int(BASELINE_MAX_NUM_SEQS * ratio) // 8) * 8)
    return batched, seqs


def load_vendor() -> Any:
    os.environ.setdefault("VLLM_MODEL_NAME", SERVED_DEFAULT)
    from chandra.input import load_image
    from chandra.model import InferenceManager
    from chandra.model.schema import BatchInputItem
    from chandra.model.util import detect_repeat_token, scale_to_fit
    from chandra.model.vllm import image_to_base64
    from chandra.output import parse_markdown
    from chandra.prompts import PROMPT_MAPPING
    from chandra.settings import settings

    return SimpleNamespace(
        load_image=load_image,
        manager=InferenceManager(method="vllm"),
        BatchInputItem=BatchInputItem,
        detect_repeat_token=detect_repeat_token,
        scale_to_fit=scale_to_fit,
        image_to_base64=image_to_base64,
        parse_markdown=parse_markdown,
        prompt=PROMPT_MAPPING[PROMPT_TYPE],
        settings=settings,
    )


def server_argv(vendor: Any, args: argparse.Namespace, weights: Path) -> list[str]:
    """The launcher's `vllm serve` flags; host and port added, the snapshot as the model."""
    batched, seqs = vendor_gpu_settings(args.vram_gb)
    return [
        "serve", str(weights),
        "--host", "127.0.0.1", "--port", str(args.port),
        "--no-enforce-eager",
        "--max-num-seqs", str(seqs),
        "--dtype", "bfloat16",
        "--max-model-len", str(args.max_model_len),
        "--max-num-batched-tokens", str(batched),
        "--gpu-memory-utilization", ".85",
        "--enable-prefix-caching",
        "--mm-processor-kwargs", json.dumps(VENDOR_PIXELS),
        "--served-model-name", vendor.settings.VLLM_MODEL_NAME,
    ]  # fmt: skip


def _bounds(vendor: Any, args: argparse.Namespace) -> tuple[int, int]:
    tokens = args.max_output_tokens or vendor.settings.MAX_OUTPUT_TOKENS
    retries = vendor.settings.MAX_VLLM_RETRIES if args.max_retries is None else args.max_retries
    return tokens, retries


def client_settings(vendor: Any, args: argparse.Namespace) -> dict:
    tokens, retries = _bounds(vendor, args)
    return {
        "package": PACKAGE,
        "served_model_name": vendor.settings.VLLM_MODEL_NAME,
        "prompt_type": PROMPT_TYPE,
        "max_output_tokens": tokens,
        "max_retries": retries,
        "temperature": 0.0,
        "top_p": 0.1,
        "retry_rule": "on a repeated tail or an error: temperature min(0.2 n, 0.8), top_p 0.95",
        "include_images": True,
        "include_headers_footers": args.include_headers_footers,
    }


def read_page(vendor: Any, args: argparse.Namespace, page: Path, url: str):
    tokens, retries = _bounds(vendor, args)
    started = time.monotonic()
    image = vendor.load_image(str(page))
    sent = vendor.scale_to_fit(image)
    sent_png = base64.b64decode(vendor.image_to_base64(sent))
    item = vendor.BatchInputItem(image=image, prompt_type=PROMPT_TYPE)
    (result,) = vendor.manager.generate(
        [item],
        max_output_tokens=tokens,
        max_retries=retries,
        max_workers=1,
        include_images=True,
        include_headers_footers=args.include_headers_footers,
        vllm_api_base=url + "/v1",
    )
    seconds = time.monotonic() - started
    text = markdown_lines(result.markdown)
    request = {
        "unit": "page",
        "loaded_size": list(image.size),
        "image_size": list(sent.size),
        "image_sha256": base.sha256(sent_png),
        "prompt_sha256": base.sha256(vendor.prompt.encode("utf-8")),
        **client_settings(vendor, args),
    }
    error = "chandra generate_vllm reported an error after its retries" if result.error else None
    finish = None if result.error else ("length" if result.token_count >= tokens else "stop")
    extra = {
        "finish_reason_basis": "inferred from token_count; the package does not return it",
        "token_count": result.token_count,
        "final_answer_repeats": bool(vendor.detect_repeat_token(result.raw or "")),
        "markdown": result.markdown,
        "markdown_with_headers_footers": vendor.parse_markdown(
            result.raw or "", include_headers_footers=True
        ),
        "blocks": [{"label": c.get("label"), "bbox": c.get("bbox")} for c in result.chunks],
    }
    return [base.unit(request, result.raw, text, finish, seconds, error, **extra)], text


# --- markdown to plain lines --------------------------------------------------------

_TABLE = re.compile(r"<table\b.*?</table>", re.I | re.S)
_ROW_BREAK = re.compile(r"<\s*(/\s*tr|br|/\s*p|/\s*caption)\b[^>]*>", re.I)
_CELL = re.compile(r"<\s*/?\s*t[dh]\b[^>]*>", re.I)
_TAG = re.compile(r"<[^>]+>")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"(?<!\\)\[([^\]]*)\]\([^)]*\)")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+")
_BULLET = re.compile(r"^\s*-\s+")
_RULE = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")
_MARK = re.compile(r"(?<!\\)(\*+|~~|\$+)")
_ESCAPE = re.compile(r"\\([*_$\[\]()])")


def _table_lines(match: re.Match) -> str:
    rows = _ROW_BREAK.sub("\n", match.group(0))
    return "\n" + unescape(_TAG.sub("", _CELL.sub(" ", rows))) + "\n"


def markdown_lines(markdown: str | None) -> str:
    """Chandra's markdown (`chandra.output.parse_markdown`) as plain lines.

    Tables stay HTML in that markdown: each row becomes a line, its cells joined by a
    space. Images are dropped (their alt text describes a picture, it is not ink); link
    text is kept. Heading marks, `-` bullets, horizontal rules, emphasis and math
    delimiters (`*`, `~~`, `$`) are markup and go; the escapes markdownify adds for
    literal `*`, `_`, `$`, brackets and parentheses are undone; any other tag is removed
    and its text kept. One line per line of the markdown, empty lines dropped.
    """
    text = _TABLE.sub(_table_lines, markdown or "")
    text = re.sub(r"<\s*br\s*/?>", "\n", text, flags=re.I)
    lines = []
    for line in text.splitlines():
        if _RULE.match(line):
            continue
        line = _IMAGE.sub("", line)
        line = _LINK.sub(r"\1", line)
        line = _BULLET.sub("", _HEADING.sub("", line))
        line = _ESCAPE.sub(r"\1", _MARK.sub("", line))
        lines.append(unescape(_TAG.sub("", line)))
    return base.plain(lines)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--vram-gb", type=int, default=48, help="card memory for the launcher's scaling rule"
    )
    parser.add_argument("--max-model-len", type=int, default=VENDOR_MAX_MODEL_LEN)
    parser.add_argument("--max-output-tokens", type=int, help="default: the package's 12,384")
    parser.add_argument("--max-retries", type=int, help="default: the package's 6")
    parser.add_argument(
        "--include-headers-footers",
        action="store_true",
        help="keep Page-Header/Page-Footer blocks in the text (the CLI drops them)",
    )


ARM = base.Arm(
    name="chandra-native",
    label="chandra-native",
    repo=REPO,
    revision=REVISION,
    artifact="chandra-ocr-2",
    venv="chandra-native",
    packages=("chandra-ocr", "openai", "beautifulsoup4", "markdownify", "pillow"),
    load_vendor=load_vendor,
    server_argv=server_argv,
    read_page=read_page,
    client_settings=client_settings,
    add_arguments=add_arguments,
    default_concurrency=VENDOR_BATCH_SIZE,
    default_port=8191,
)


if __name__ == "__main__":
    raise SystemExit(base.main(ARM))
