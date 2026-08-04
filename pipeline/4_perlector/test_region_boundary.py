"""The Perlector refuses a crop that cannot be traced back to an Exemplar page.

Ruling 1 is why this check exists: "the goal of the pipeline is that every output
is linked to the original image so we can cross reference to the source and cite
and rebuild it." A crop the reader accepts without a page locator is a reading
that reaches the export with nothing to cite — GOALS 5's traceability broken in the
one stage that establishes text.

The check itself was landed with no test at all, which was found by deleting it and
watching the whole suite stay green. A check nothing can fail is not a check.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.contracts.canonical import digest_bytes  # noqa: E402
from common.contracts.errors import SchemaRefusal  # noqa: E402
from common.imaging import encode_grayscale_png  # noqa: E402


def _load_perlector():
    """Load this stage's `run.py` by path, never by the bare name `run`.

    Every stage's entry point is called `run.py`, so `import run` in a whole-suite
    session resolves to whichever stage happened to be imported first — a test that
    silently checks the Designator's boundary instead of the Perlector's.
    """
    path = Path(__file__).resolve().parent / "run.py"
    spec = importlib.util.spec_from_file_location("perlector_run_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


perlector = _load_perlector()


class _Tree:
    """Just enough run tree to hand `verify_region` one crop's bytes."""

    def __init__(self, data: bytes):
        self._data = data

    def read_bytes(self, relative_path: str) -> bytes:
        return self._data


class _Context:
    def __init__(self, data: bytes):
        self.tree = _Tree(data)


def _region(data: bytes, **transform) -> dict:
    return {
        "payload": {
            "region_id": "region-1",
            "image_path": "2_designator/blobs/sha256/" + digest_bytes(data),
            "image_sha256": digest_bytes(data),
            "transform": {
                "bounds": {"x": 0, "y": 0, "w": 4, "h": 3},
                "source_page_ordinal": 1,
                "source_page_id": "page-abc",
                **transform,
            },
        }
    }


@pytest.fixture
def crop() -> bytes:
    return encode_grayscale_png(4, 3, [bytearray([128] * 4) for _ in range(3)])


def test_a_region_naming_its_exemplar_page_verifies(crop):
    verified = perlector.verify_region(_Context(crop), _region(crop))

    assert verified["source_page_ordinal"] == 1
    assert verified["source_page_id"] == "page-abc"
    assert verified["verified_dimensions"] == {"w": 4, "h": 3}


@pytest.mark.parametrize(
    "transform",
    [
        pytest.param({"source_page_ordinal": None}, id="ordinal-absent"),
        pytest.param({"source_page_ordinal": "1"}, id="ordinal-a-string"),
        pytest.param({"source_page_ordinal": True}, id="ordinal-a-bool"),
        pytest.param({"source_page_ordinal": -1}, id="ordinal-negative"),
        pytest.param({"source_page_id": None}, id="page-id-absent"),
        pytest.param({"source_page_id": ""}, id="page-id-empty"),
        pytest.param({"source_page_id": 7}, id="page-id-not-a-string"),
    ],
)
def test_a_region_with_no_usable_exemplar_locator_refuses(crop, transform):
    """Each of these is a crop nothing could cite back to a page of ink.

    `True` is in the list on purpose: it is an `int` in Python, so an ordinal check
    written the obvious way accepts it, and a boolean that reads as ordinal 1 is
    exactly the kind of thing that traces an act back to the wrong page.
    """
    with pytest.raises(SchemaRefusal, match="no valid Exemplar page locator"):
        perlector.verify_region(_Context(crop), _region(crop, **transform))
