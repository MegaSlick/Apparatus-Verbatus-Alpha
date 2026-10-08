"""The Chandra native arm against a stand-in `chandra` package and the fake vLLM server.

The real package is not installed in the project environment; the stand-in has the same
names and call shapes (`chandra-ocr` 0.2.0) and asks the fake server, so the command
line, the server start, the cache record and resume are all exercised without a GPU.
"""

import json
import re
import sys
import types
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from operations.bakeoff import witness_run as W
from operations.bakeoff.native import chandra_native as C
from operations.bakeoff.test_bakeoff_runner import _free_port, _pages

FAKE = Path(__file__).parents[1] / "fake_vllm_server.py"
RECORD_KEYS = {
    "schema", "model", "arm", "repo", "revision", "weights", "server", "page",
    "source_file", "source_sha256", "units", "text", "empty", "finish_reason", "loop",
    "loop_reasons", "seconds", "error", "written",
}  # fmt: skip


@dataclass
class _Item:
    image: Image.Image
    prompt: str | None = None
    prompt_type: str | None = None


@dataclass
class _Output:
    markdown: str
    html: str
    chunks: list
    raw: str
    page_box: list
    token_count: int
    images: dict
    error: bool


def _stub_chandra(monkeypatch, *, fail: bool = False) -> list[dict]:
    """Install a stand-in `chandra` package; return the generate() calls it saw."""
    calls: list[dict] = []
    settings = types.SimpleNamespace(
        VLLM_MODEL_NAME="chandra", MAX_OUTPUT_TOKENS=12384, MAX_VLLM_RETRIES=6
    )
    prompt = "OCR this image to HTML, arranged as layout blocks."

    class InferenceManager:
        def __init__(self, method="vllm"):
            assert method == "vllm"

        def generate(self, batch, max_output_tokens=None, **kwargs):
            calls.append({"max_output_tokens": max_output_tokens, **kwargs})
            if fail:
                return [_Output("", "", [], "", [0, 0, 1, 1], 0, {}, True)]
            body = {
                "model": settings.VLLM_MODEL_NAME,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": "data:image/png;base64,"}},
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
            }
            answer = W.post(kwargs["vllm_api_base"].removesuffix("/v1"), body, 30)
            raw = answer["text"]
            chunks = [{"bbox": [4, 3, 396, 297], "label": "Text", "content": "<p>Le dix mai</p>"}]
            return [_Output(parse_markdown(raw), raw, chunks, raw, [0, 0, 400, 300], 9, {}, False)]

    def parse_markdown(html, include_headers_footers=False, include_images=True):
        return re.sub(r"<[^>]+>", "", html)

    modules = {
        "chandra": types.ModuleType("chandra"),
        "chandra.input": types.SimpleNamespace(
            load_image=lambda path: Image.open(path).convert("RGB")
        ),
        "chandra.model": types.SimpleNamespace(InferenceManager=InferenceManager),
        "chandra.model.schema": types.SimpleNamespace(BatchInputItem=_Item),
        "chandra.model.util": types.SimpleNamespace(
            scale_to_fit=lambda image: image, detect_repeat_token=lambda text: False
        ),
        "chandra.model.vllm": types.SimpleNamespace(image_to_base64=_png_base64),
        "chandra.output": types.SimpleNamespace(parse_markdown=parse_markdown),
        "chandra.prompts": types.SimpleNamespace(PROMPT_MAPPING={"ocr_layout": prompt}),
        "chandra.settings": types.SimpleNamespace(settings=settings),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    return calls


def _png_base64(image):
    import base64
    import io

    out = io.BytesIO()
    image.save(out, format="PNG")
    return base64.b64encode(out.getvalue()).decode()


def _weights(tmp_path: Path) -> Path:
    folder = tmp_path / "weights"
    folder.mkdir()
    (folder / "config.json").write_text("{}")
    return folder


def _argv(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "run",
        "--pages", str(tmp_path / "pages"),
        "--out", str(tmp_path / "cache"),
        "--weights", str(_weights(tmp_path)),
        "--port", str(_free_port()),
        "--startup-timeout", "60",
        *extra,
        "--vllm-cmd", sys.executable, str(FAKE),
    ]  # fmt: skip


def test_runs_the_package_pipeline_writes_records_and_resumes(tmp_path, monkeypatch):
    calls = _stub_chandra(monkeypatch)
    _pages(tmp_path / "pages", 2)
    argv = _argv(tmp_path)
    assert C.base.main(C.ARM, argv) == 0
    cache = tmp_path / "cache" / "chandra-native"
    record = json.loads((cache / "p001.json").read_text())
    assert set(record) == RECORD_KEYS
    assert record["arm"] == "chandra-native" and record["model"] == "chandra-native"
    assert record["repo"] == C.REPO and record["revision"] == C.REVISION
    assert record["text"] == "Le dix mai" and record["error"] is None and not record["loop"]
    argv_sent = record["server"]["argv"]
    assert argv_sent[argv_sent.index("--max-num-seqs") + 1] == "32"
    assert "--enable-prefix-caching" in argv_sent and "chandra" in argv_sent
    (unit,) = record["units"]
    assert {"request", "raw_response", "text", "finish_reason", "seconds", "error"} <= set(unit)
    assert unit["finish_reason"] == "stop" and "data-bbox" in unit["raw_response"]
    assert unit["request"]["max_retries"] == 6 and unit["request"]["image_size"] == [400, 300]
    assert calls[0]["max_output_tokens"] == 12384 and calls[0]["include_headers_footers"] is False
    assert json.loads((cache / "run.json").read_text())["pages"] == 2

    assert C.base.main(C.ARM, argv) == 0  # everything cached: no server, nothing sent
    events = [
        json.loads(x)["event"] for x in (tmp_path / "cache/events.jsonl").read_text().splitlines()
    ]
    assert events.count("server-ready") == 1 and events[-1] == "nothing-to-do"
    assert len(calls) == 2


def test_a_failed_page_is_recorded_and_sent_again(tmp_path, monkeypatch):
    _stub_chandra(monkeypatch, fail=True)
    _pages(tmp_path / "pages", 1)
    assert C.base.main(C.ARM, _argv(tmp_path)) == 1
    path = tmp_path / "cache" / "chandra-native" / "p000.json"
    assert json.loads(path.read_text())["error"] and not W.cached_ok(path)


def test_refusals(tmp_path, monkeypatch):
    _pages(tmp_path / "pages", 1)
    for name in [m for m in sys.modules if m == "chandra" or m.startswith("chandra.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "chandra", None)  # the project venv has no package
    assert C.base.main(C.ARM, ["check"]) == 2
    assert C.base.main(C.ARM, _argv(tmp_path)) == 2
    _stub_chandra(monkeypatch)
    out = ["--pages", str(tmp_path / "pages"), "--out", str(tmp_path / "c")]
    assert C.base.main(C.ARM, ["run", *out]) == 2  # no weights
    assert C.base.main(C.ARM, ["run", *out, "--lines", "surya"]) == 2
    assert C.base.main(C.ARM, ["prepare", *out]) == 0


def test_the_launcher_scaling_rule():
    assert C.vendor_gpu_settings(80) == (8192, 64)
    assert C.vendor_gpu_settings(48) == (4096, 32)
    assert C.vendor_gpu_settings(24) == (2048, 16)


SAMPLE_MARKDOWN = r"""# Registre des actes

**Acte n\* 12** du *dix mai*

- Jean Dupont, cultivateur
- prix \$3 \_net\_

![Seal of the mayor, round, ink stamp](abc_3_img.webp)

<table><tr><th>Nom</th><th>Age</th></tr><tr><td>Marie &amp; Paul</td><td>32</td></tr></table>

---

Signé <sup>le</sup> maire $x^2$
"""


def test_markdown_lines_reads_the_package_markdown():
    assert C.markdown_lines(SAMPLE_MARKDOWN).splitlines() == [
        "Registre des actes",
        "Acte n* 12 du dix mai",
        "Jean Dupont, cultivateur",
        "prix $3 _net_",
        "Nom Age",
        "Marie & Paul 32",
        "Signé le maire x^2",
    ]
    assert C.markdown_lines(None) == ""
