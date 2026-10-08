"""The dots.mocr arm against a stand-in `dots_mocr` package and the fake vLLM server.

The stand-in has the vendor's names and call shapes and the vendor's real layout prompt;
its image functions are plain Pillow (the real ones need PyMuPDF, which only the arm's
own environment has).
"""

import base64
import io
import json
import sys
import types
from pathlib import Path

from PIL import Image

from operations.bakeoff.native import dots_mocr as D
from operations.bakeoff.native.test_chandra_native import RECORD_KEYS, _weights
from operations.bakeoff.test_bakeoff_runner import _free_port, _pages

FAKE = Path(__file__).parents[1] / "fake_vllm_server.py"
LAYOUT_PROMPT = (
    "Please output the layout information from the PDF image, including each layout "
    "element's bbox, its category, and the corresponding text content within the bbox."
)


def _stub_dots(monkeypatch, *, salvage: bool = False) -> list[str]:
    """Install a stand-in `dots_mocr` package; return the image steps it was asked for."""
    steps: list[str] = []

    def fetch_image(image, min_pixels=None, max_pixels=None):
        steps.append("fetch")
        return (image if isinstance(image, Image.Image) else Image.open(image)).convert("RGB")

    def get_image_by_fitz_doc(image, target_dpi=200):
        steps.append(f"fitz-{target_dpi}")
        return image.resize((image.width * 2, image.height * 2))

    def to_base64(image, format="PNG"):
        out = io.BytesIO()
        image.save(out, format=format)
        return f"data:image/{format.lower()};base64," + base64.b64encode(out.getvalue()).decode()

    def post_process_output(response, prompt_mode, origin, image, min_pixels, max_pixels):
        if salvage:
            return "Folio 1\n\nLe dix **mai**", True
        return json.loads(response), False

    modules = {
        "dots_mocr": types.ModuleType("dots_mocr"),
        "dots_mocr.utils": types.ModuleType("dots_mocr.utils"),
        "dots_mocr.utils.image_utils": types.SimpleNamespace(
            fetch_image=fetch_image,
            get_image_by_fitz_doc=get_image_by_fitz_doc,
            smart_resize=lambda h, w: (h // 28 * 28, w // 28 * 28),
            PILimage_to_base64=to_base64,
        ),
        "dots_mocr.utils.layout_utils": types.SimpleNamespace(
            post_process_output=post_process_output
        ),
        "dots_mocr.utils.prompts": types.SimpleNamespace(
            dict_promptmode_to_prompt={"prompt_layout_all_en": LAYOUT_PROMPT}
        ),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    return steps


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


def test_runs_the_vendor_parser_path_and_resumes(tmp_path, monkeypatch):
    steps = _stub_dots(monkeypatch)
    _pages(tmp_path / "pages", 2)
    argv = _argv(tmp_path)
    assert D.base.main(D.ARM, argv) == 0
    record = json.loads((tmp_path / "cache" / "dots-mocr" / "p000.json").read_text())
    assert set(record) == RECORD_KEYS and record["arm"] == "dots-mocr"
    assert record["repo"] == D.REPO and record["revision"] == D.REVISION
    assert record["text"] == "Folio 1\nLe dix mai" and record["finish_reason"] == "stop"
    (unit,) = record["units"]
    assert unit["cells"][1]["category"] == "Text" and unit["json_salvaged"] is False
    assert unit["request"]["image_size"] == [800, 600]  # the stand-in fitz render doubled it
    assert unit["request"]["fitz_preprocess"] is True and "fitz-200" in steps
    argv_sent = record["server"]["argv"]
    assert "--trust-remote-code" in argv_sent
    assert argv_sent[argv_sent.index("--chat-template-content-format") + 1] == "string"
    assert D.base.main(D.ARM, argv) == 0
    events = (tmp_path / "cache" / "events.jsonl").read_text().splitlines()
    assert json.loads(events[-1])["event"] == "nothing-to-do"


def test_the_request_body_is_the_vendor_s(monkeypatch):
    _stub_dots(monkeypatch)
    vendor = D.load_vendor()
    body = D.request_body(vendor, Image.new("RGB", (56, 56)))
    image_part, text_part = body["messages"][0]["content"]
    assert text_part["text"] == "<|img|><|imgpad|><|endofimg|>" + LAYOUT_PROMPT
    assert image_part["image_url"]["url"].startswith("data:image/png;base64,")
    assert {k: body[k] for k in ("max_completion_tokens", "temperature", "top_p")} == {
        "max_completion_tokens": 16384,
        "temperature": 0.1,
        "top_p": 1.0,
    }
    assert len(body["messages"]) == 1  # no system turn for the layout prompt


def test_no_fitz_and_a_salvaged_answer(tmp_path, monkeypatch):
    steps = _stub_dots(monkeypatch, salvage=True)
    _pages(tmp_path / "pages", 1)
    assert D.base.main(D.ARM, _argv(tmp_path, "--no-fitz-preprocess")) == 0
    record = json.loads((tmp_path / "cache" / "dots-mocr" / "p000.json").read_text())
    assert record["text"] == "Folio 1\nLe dix mai" and record["units"][0]["json_salvaged"]
    assert not any(step.startswith("fitz") for step in steps)
    assert record["units"][0]["request"]["image_size"] == [400, 300]


def test_refuses_without_the_vendor_package(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "dots_mocr", None)
    assert D.base.main(D.ARM, ["check"]) == 2


SAMPLE_CELLS = [
    {"bbox": [40, 20, 960, 60], "category": "Page-header", "text": "Registre 1854"},
    {"bbox": [40, 80, 960, 120], "category": "Section-header", "text": "## Naissances"},
    {"bbox": [40, 130, 960, 300], "category": "Text", "text": "Le **dix** mai\nest né Jean\\_"},
    {"bbox": [40, 310, 960, 340], "category": "List-item", "text": "- témoin Paul"},
    {"bbox": [600, 350, 900, 500], "category": "Picture"},
    {
        "bbox": [40, 520, 960, 700],
        "category": "Table",
        "text": "<table><tr><td>Nom</td><td>Age</td></tr><tr><td>Marie &amp; Luc</td>"
        "<td>32</td></tr></table>",
    },
    {"bbox": [40, 710, 960, 740], "category": "Formula", "text": "$$x^{2}$$"},
    {"bbox": [40, 950, 960, 980], "category": "Page-footer", "text": "12"},
]


def test_cells_lines_reads_the_layout_answer_in_order():
    assert D.cells_lines(SAMPLE_CELLS).splitlines() == [
        "Registre 1854",
        "Naissances",
        "Le dix mai",
        "est né Jean_",
        "témoin Paul",
        "Nom Age",
        "Marie & Luc 32",
        "x^{2}",
        "12",
    ]
