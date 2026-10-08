"""Churro-3B the way Stanford's own runner ran it when the model was released.

    <churro venv>/bin/python -m operations.bakeoff.native.churro_native run \\
        --pages DIR --out CACHE --store-root STORE

The runner is `run_churro_ocr.py` in github.com/stanford-oval/Churro at the commit
below (the last before the pinned weights): it starts `vllm serve` with the flags of
`utils/docker/servers.py::start_vllm_server` (context 20,000, the `max_completion_tokens`
of the `churro` row in `utils/llm/models.py`), opens each page as RGB, downscales it to
fit 2,500 x 2,500 and encodes it (`utils/llm/messages.py::encode_image`), and sends
the system prompt alone with the image as the whole user turn, at temperature 0.6 and
no answer bound (`utils/llm/core.py`, through LiteLLM, which retries once). A duplicated
prompt at the start of the answer is trimmed (`trim_leading_prompt`), and the page's
text is the repository's own extractor, `evaluation/xml_utils.py::
extract_actual_text_from_xml`, re-expressed here over the same `lxml` parser.

Requests go out through `witness_run.post`, not LiteLLM: LiteLLM's disk cache and its
start-up fetch of a model price list are not wanted on the pod, and the request body
it builds for an OpenAI-compatible server is just these fields.
"""

from __future__ import annotations

import argparse
import base64
import io
import re
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from operations.bakeoff import witness_run as W
from operations.bakeoff.native import base

REPO = "stanford-oval/churro-3B"
REVISION = "ca2150ea465d5a3d67818c50e234b9422619c75d"
RUNNER = {
    "repository": "github.com/stanford-oval/Churro",
    "commit": "c8b2f28834991ebc96faef31562662bddc8062ee",
    "script": "run_churro_ocr.py",
}
SERVED = "churro"  # the runner serves the engine key as the model name
SYSTEM_PROMPT = "Transcribe the entirety of this historical document to XML format."
TEMPERATURE = 0.6  # `MODEL_MAP["churro"]["static_params"]`
VENDOR_MAX_MODEL_LEN = 20_000  # `max_completion_tokens`, passed to vLLM as --max-model-len
MAX_IMAGE_DIM = 2500  # `utils/llm/messages.py::_MAX_IMAGE_DIM`
VENDOR_CONCURRENCY = 64  # `run_churro_ocr.py --max-concurrency` default
ATTEMPTS = 2  # LiteLLM's `num_retries=1` for a vLLM model


def load_vendor() -> Any:
    from lxml import etree

    return SimpleNamespace(etree=etree)


def server_argv(vendor: Any, args: argparse.Namespace, weights: Path) -> list[str]:
    """`start_vllm_server`'s flags; host and port added, the snapshot as the model."""
    return [
        "serve", str(weights),
        "--host", "127.0.0.1", "--port", str(args.port),
        "--gpu-memory-utilization", "0.9",
        "--data-parallel-size", "1",
        "--trust-remote-code",
        "--tensor-parallel-size", "1",
        "--max-model-len", str(args.max_model_len),
        "--served-model-name", SERVED,
    ]  # fmt: skip


def client_settings(vendor: Any, args: argparse.Namespace) -> dict:
    return {
        "runner": RUNNER,
        "served_model_name": SERVED,
        "system_prompt": SYSTEM_PROMPT,
        "temperature": TEMPERATURE,
        "max_tokens": None,
        "max_model_len": args.max_model_len,
        "attempts": ATTEMPTS,
        "max_image_dim": MAX_IMAGE_DIM,
    }


def vendor_image(path: Path) -> tuple[bytes, str, tuple[int, int]]:
    """(encoded bytes, image format, size) as `load_image` then `encode_image` make them."""
    from PIL import Image

    with Image.open(path) as opened:
        image = opened if opened.mode == "RGB" else opened.convert("RGB")
        image.load()
        image_format = image.format or "PNG"
        if image_format not in ("PNG", "JPEG", "WEBP"):
            image_format = "PNG"
        width, height = image.size
        if width > MAX_IMAGE_DIM or height > MAX_IMAGE_DIM:
            scale = min(MAX_IMAGE_DIM / width, MAX_IMAGE_DIM / height)
            image = image.resize((int(width * scale), int(height * scale)), Image.LANCZOS)
        if image_format == "JPEG" and image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        out = io.BytesIO()
        options = {"quality": 95, "optimize": True} if image_format == "JPEG" else {}
        image.save(out, format=image_format, **options)
        return out.getvalue(), image_format, image.size


def request_body(data: bytes, image_format: str) -> dict:
    url = f"data:image/{image_format.lower()};base64," + base64.b64encode(data).decode("ascii")
    return {
        "model": SERVED,
        "messages": [
            {"role": "system", "content": [{"type": "text", "text": SYSTEM_PROMPT}]},
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}}]},
        ],
        "temperature": TEMPERATURE,
    }


def trim_leading_prompt(text: str, prompt: str) -> str:
    """`run_churro_ocr.py::trim_leading_prompt`."""
    if prompt and text.startswith(prompt):
        return text[len(prompt) :].lstrip("\n ")
    return text


def read_page(vendor: Any, args: argparse.Namespace, page: Path, url: str):
    data, image_format, size = vendor_image(page)
    body = request_body(data, image_format)
    started = time.monotonic()
    attempts = 0
    while True:
        attempts += 1
        result = W.post(url, body, args.request_timeout)
        if result["error"] is None or attempts == ATTEMPTS:
            break
    seconds = time.monotonic() - started
    answer = trim_leading_prompt(result["text"] or "", SYSTEM_PROMPT)
    extracted, note = extract_actual_text_from_xml(answer, vendor.etree)
    text = base.plain(extracted.splitlines())
    request = {
        "unit": "page",
        "image_size": list(size),
        "image_format": image_format,
        "image_sha256": base.sha256(data),
        "prompt_sha256": base.sha256(SYSTEM_PROMPT.encode("utf-8")),
        "sampling": {k: v for k, v in body.items() if k not in ("model", "messages")},
        "attempts_made": attempts,
        **client_settings(vendor, args),
    }
    extra = {
        "http_status": result["http_status"],
        "raw_response_b64": result["raw_response_b64"],
        "usage": result["usage"],
        "answer": result["text"],
        "extract_note": note,
    }
    raw = result["raw_response"]
    unit = base.unit(request, raw, text, result["finish_reason"], seconds, result["error"], **extra)
    return [unit], text


# --- the repository's own text extractor --------------------------------------------
#
# `evaluation/xml_utils.py` at the runner commit (Apache-2.0, github.com/stanford-oval/
# Churro). The tag list is the element names of `evaluation/historical_doc.xsd` at that
# commit, as `xmlschema.XMLSchema(...).elements` lists them, plus the extractor's own
# additions; the vendor reads the XSD at run time, the list is carried here instead.

XSD_ELEMENTS = [
    "HistoricalDocument", "Metadata", "Page", "Header", "Footer", "PageNumber",
    "FolioNumber", "CatchWord", "SignatureMark", "Body", "Paragraph", "MarginalNote",
    "InterlinearNote", "Table", "TableRow", "TableCell", "Heading", "DateLine",
    "DatedEntry", "RecordEntry", "BlockQuotation", "List", "Item", "Figure", "Caption",
    "Seal", "Stamp", "Watermark", "Formula", "MusicalNotation", "Gap", "Line", "Above",
    "Initial", "Emphasis", "Illegible", "Deletion", "Addition",
]  # fmt: skip
EXTRA_TAGS = [
    "PhysicalDescription", "Language", "Script", "PhysicalDescription", "Description",
    "WritingDirection", "TranscriptionNote", "Footer", "Header",
]  # fmt: skip
_ALLOWED_XML = re.compile(
    r"(<\?xml.*?\?>|</?(?:"
    + "|".join(re.escape(t) for t in XSD_ELEMENTS + EXTRA_TAGS)
    + r")(?:\b[^>]*?)?/?>)",
    re.DOTALL,
)
_LINE_BREAK = re.compile(r"<(/)?(lb|br)\s*/?>", re.IGNORECASE)


def _escape_chunk(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _escape_xml(text: str) -> str:
    """Escape `<`, `>`, `&` everywhere except inside the allowed tags."""
    parts, last = [], 0
    for match in _ALLOWED_XML.finditer(text):
        if match.start() > last:
            parts.append(_escape_chunk(text[last : match.start()]))
        parts.append(match.group(0))
        last = match.end()
    if last < len(text):
        parts.append(_escape_chunk(text[last:]))
    return "".join(parts)


def _remove_tag(text: str, tag: str) -> str:
    if f"<{tag}" not in text:
        return text
    text = re.sub(rf"<{tag}\b[^>]*>.*?</{tag}>", "", text, flags=re.DOTALL)
    return re.sub(rf"<{tag}\b[^>]*/>", "", text)


def extract_actual_text_from_xml(xml_content: str, etree: Any) -> tuple[str, str | None]:
    """(text of each page's Header, Body and Footer, a note when the vendor gave up).

    Not XML at all: the answer as it is. Description, Deletion, Illegible and Gap
    elements are removed with their content; the rest is parsed with lxml's recovering
    parser, every text node of those three parts becomes one stripped line, and pages are
    separated by a blank line. Where the vendor logs an error and returns "", so does
    this, with the reason as the note.
    """
    if "HistoricalDocument" not in xml_content:
        return xml_content, None
    xml_content = _escape_xml(xml_content)
    for tag in ("Description", "Deletion", "Illegible", "Gap"):
        xml_content = _remove_tag(xml_content, tag)
    try:
        root = etree.fromstring(xml_content.encode("utf-8"), etree.XMLParser(recover=True))
        if root is None:
            return "", "unparseable XML"
        namespace = root.nsmap.get(None) or ""
        prefix = "docns:" if namespace else ""
        nsmap = {"docns": namespace} if namespace else {}
        pages = []
        for page in root.xpath(f".//{prefix}Page", namespaces=nsmap):
            parts = []
            for tag in ("Header", "Body", "Footer"):
                for element in page.xpath(f".//{prefix}{tag}", namespaces=nsmap):
                    lines = [_LINE_BREAK.sub("", t).strip() for t in element.itertext()]
                    lines = [line for line in lines if line]
                    if lines:
                        parts.append("\n".join(lines))
            if parts:
                pages.append("\n".join(parts))
    except Exception as failure:  # noqa: BLE001 -- the vendor returns "" on any failure
        return "", f"{type(failure).__name__}: {failure}"
    if not pages:
        return "", "no text in any Page's Header, Body or Footer"
    text = _LINE_BREAK.sub("", "\n\n".join(pages))
    return re.sub(r"\n\s*\n+", "\n\n", text).strip(), None


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--max-model-len", type=int, default=VENDOR_MAX_MODEL_LEN)


ARM = base.Arm(
    name="churro-native",
    label="churro-native",
    repo=REPO,
    revision=REVISION,
    artifact="churro-3B",
    venv="churro-native",
    packages=("lxml", "pillow"),
    load_vendor=load_vendor,
    server_argv=server_argv,
    read_page=read_page,
    client_settings=client_settings,
    add_arguments=add_arguments,
    default_concurrency=VENDOR_CONCURRENCY,
    default_port=8193,
)


if __name__ == "__main__":
    raise SystemExit(base.main(ARM))
