"""dots.mocr through rednote-hilab's own parser: its image path, layout prompt and reader.

    <dots venv>/bin/python -m operations.bakeoff.native.dots_mocr run \\
        --pages DIR --out CACHE --store-root STORE

What runs is the vendor's `dots_mocr` package (github.com/rednote-hilab/dots.mocr at
the commit below), the way `dots_mocr/parser.py` parses one image with its command
line's defaults: `fetch_image` (RGB), then `get_image_by_fitz_doc` (the page re-rendered
through PyMuPDF at 200 dpi; `--no-fitz-preprocess` skips it, as the vendor's flag
does), the `prompt_layout_all_en` prompt behind the vendor's image placeholder, 16,384
tokens at temperature 0.1 and top_p 1.0, then `post_process_output`, which reads the
JSON answer (or salvages a broken one with the vendor's `OutputCleaner`). The server
takes the README's `vllm serve` flags, including `--trust-remote-code`: vLLM 0.30
has the model class but does not register its config, so the snapshot's
`configuration_dots.py` must be run. `cells_lines` turns the layout cells into plain
lines in the model's reading order.

The request goes out through `witness_run.post` with the body the vendor's
`inference_with_vllm` builds, so the raw response, finish reason and usage are kept.
"""

from __future__ import annotations

import argparse
import base64
import re
import time
from html import unescape
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from operations.bakeoff import witness_run as W
from operations.bakeoff.native import base

REPO = "dots-studio/dots.mocr"  # rednote-hilab/dots.mocr redirects here
REVISION = "e539fbb52280393adc081b289ec597430a0f9031"
PACKAGE = {
    "distribution": "dots_mocr",
    "repository": "github.com/rednote-hilab/dots.mocr",
    "commit": "23f3e5612fb8066d4034d5ecfc8f33a9243533eb",
}
PROMPT_MODE = "prompt_layout_all_en"
IMAGE_PLACEHOLDER = "<|img|><|imgpad|><|endofimg|>"  # `dots_mocr/model/inference.py`
SERVED = "model"  # the README's --served-model-name and the parser's default
# `dots_mocr/parser.py` command-line defaults.
TEMPERATURE = 0.1
TOP_P = 1.0
MAX_COMPLETION_TOKENS = 16_384
DPI = 200
VENDOR_THREADS = 16


def load_vendor() -> Any:
    from dots_mocr.utils.image_utils import (
        PILimage_to_base64,
        fetch_image,
        get_image_by_fitz_doc,
        smart_resize,
    )
    from dots_mocr.utils.layout_utils import post_process_output
    from dots_mocr.utils.prompts import dict_promptmode_to_prompt

    return SimpleNamespace(
        fetch_image=fetch_image,
        get_image_by_fitz_doc=get_image_by_fitz_doc,
        smart_resize=smart_resize,
        to_base64=PILimage_to_base64,
        post_process_output=post_process_output,
        prompt=dict_promptmode_to_prompt[PROMPT_MODE],
    )


def server_argv(vendor: Any, args: argparse.Namespace, weights: Path) -> list[str]:
    """The README's `vllm serve` line; host and port added, the snapshot as the model."""
    return [
        "serve", str(weights),
        "--host", "127.0.0.1", "--port", str(args.port),
        "--tensor-parallel-size", "1",
        "--gpu-memory-utilization", "0.9",
        "--chat-template-content-format", "string",
        "--served-model-name", SERVED,
        "--trust-remote-code",
    ]  # fmt: skip


def client_settings(vendor: Any, args: argparse.Namespace) -> dict:
    return {
        "package": PACKAGE,
        "served_model_name": SERVED,
        "prompt_mode": PROMPT_MODE,
        "temperature": TEMPERATURE,
        "top_p": TOP_P,
        "max_completion_tokens": MAX_COMPLETION_TOKENS,
        "fitz_preprocess": not args.no_fitz_preprocess,
        "dpi": DPI,
        "min_pixels": None,
        "max_pixels": None,
    }


def request_body(vendor: Any, image: Any) -> dict:
    """The body `inference_with_vllm` sends for the layout prompt (no system turn)."""
    return {
        "model": SERVED,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": vendor.to_base64(image)}},
                    {"type": "text", "text": f"{IMAGE_PLACEHOLDER}{vendor.prompt}"},
                ],
            }
        ],
        "max_completion_tokens": MAX_COMPLETION_TOKENS,
        "temperature": TEMPERATURE,
        "top_p": TOP_P,
    }


def read_page(vendor: Any, args: argparse.Namespace, page: Path, url: str):
    origin = vendor.fetch_image(str(page))
    if args.no_fitz_preprocess:
        image = vendor.fetch_image(origin, min_pixels=None, max_pixels=None)
    else:
        rendered = vendor.get_image_by_fitz_doc(origin, target_dpi=DPI)
        image = vendor.fetch_image(rendered, min_pixels=None, max_pixels=None)
    input_height, input_width = vendor.smart_resize(image.height, image.width)
    body = request_body(vendor, image)
    sent = base64.b64decode(body["messages"][0]["content"][0]["image_url"]["url"].split(",", 1)[1])
    started = time.monotonic()
    result = W.post(url, body, args.request_timeout)
    seconds = time.monotonic() - started
    answer = result["text"]
    cells, salvaged = None, None
    text = ""
    if result["error"] is None:
        cells, salvaged = vendor.post_process_output(
            answer, PROMPT_MODE, origin, image, min_pixels=None, max_pixels=None
        )
        text = salvaged_lines(cells) if salvaged else cells_lines(cells)
    request = {
        "unit": "page",
        "loaded_size": list(origin.size),
        "image_size": list(image.size),
        "image_sha256": base.sha256(sent),
        "server_input_size": [input_width, input_height],
        "prompt_sha256": base.sha256(vendor.prompt.encode("utf-8")),
        **client_settings(vendor, args),
    }
    extra = {
        "http_status": result["http_status"],
        "raw_response_b64": result["raw_response_b64"],
        "usage": result["usage"],
        "answer": answer,
        "json_salvaged": salvaged,
        "cells": cells if not salvaged else None,
    }
    raw = result["raw_response"]
    unit = base.unit(request, raw, text, result["finish_reason"], seconds, result["error"], **extra)
    return [unit], text


# --- layout cells to plain lines ----------------------------------------------------

_ROW_BREAK = re.compile(r"<\s*(/\s*tr|br|/\s*p|/\s*caption|/\s*h[1-6]|/\s*li)\b[^>]*>", re.I)
_CELL = re.compile(r"<\s*/?\s*t[dh]\b[^>]*>", re.I)
_TAG = re.compile(r"<[^>]+>")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+")
_BULLET = re.compile(r"^\s*[-*+]\s+")
_MARK = re.compile(r"(?<!\\)(\*\*|__|~~|\$\$)")
_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!$|])")


def _html_lines(text: str) -> list[str]:
    rows = _ROW_BREAK.sub("\n", text)
    return unescape(_TAG.sub("", _CELL.sub(" ", rows))).splitlines()


def _markdown_lines(text: str) -> list[str]:
    lines = []
    for line in _IMAGE.sub("", text).splitlines():
        line = _BULLET.sub("", _HEADING.sub("", line))
        lines.append(_ESCAPE.sub(r"\1", _MARK.sub("", line)))
    return lines


def cells_lines(cells: list[dict]) -> str:
    """The vendor's layout cells as plain lines, in the order the model gave them.

    That order is the vendor's reading order (the prompt asks for it, and
    `layoutjson2md` keeps it). Every category but Picture contributes, page headers and
    footers included (the vendor's `.md`, not its `_nohf.md`): Table text is HTML, each
    row a line with its cells joined by a space; Formula text is LaTeX, kept as written
    without `$$`; the rest is Markdown, read with heading marks, bullets, bold and
    escapes removed and its own line breaks kept.
    """
    lines: list[str] = []
    for cell in cells:
        category, text = cell.get("category"), str(cell.get("text") or "")
        if category == "Picture":
            continue
        if category == "Table":
            lines.extend(_html_lines(text))
        elif category == "Formula":
            lines.extend(text.replace("$$", "").splitlines())
        else:
            lines.extend(_markdown_lines(text))
    return base.plain(lines)


def salvaged_lines(text: str) -> str:
    """The vendor's salvage of a broken JSON answer: cell texts joined by blank lines."""
    return base.plain(_markdown_lines(_TAG.sub("", text or "")))


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--no-fitz-preprocess",
        action="store_true",
        help="skip the vendor's PyMuPDF 200 dpi re-render (its --no_fitz_preprocess)",
    )


ARM = base.Arm(
    name="dots-mocr",
    label="dots-mocr",
    repo=REPO,
    revision=REVISION,
    artifact="DotsMOCR",  # no "." in the name: the remote config is imported from it
    venv="dots-mocr",
    packages=("pymupdf", "cairosvg", "openai", "pillow"),
    load_vendor=load_vendor,
    server_argv=server_argv,
    read_page=read_page,
    client_settings=client_settings,
    add_arguments=add_arguments,
    default_concurrency=VENDOR_THREADS,
    default_port=8195,
    source=("https://github.com/rednote-hilab/dots.mocr", PACKAGE["commit"]),
)


if __name__ == "__main__":
    raise SystemExit(base.main(ARM))
