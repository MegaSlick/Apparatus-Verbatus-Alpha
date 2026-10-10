"""An orientation tag with unequal resolutions: every axis lands where it should.
Each test here fails under the mutation named in its docstring. Synthetic pages only."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from pagekit import _orient_testpages as pages
from pagekit.__main__ import main
from pagekit.answer import Answer
from pagekit.measure import measure
from pagekit.output import MANIFEST_NAME, execute
from pagekit.pipeline import DETECTORS
from pagekit.prepare import Detector, Source, StepContext, plan
from pagekit.project import load_settings
from pagekit.volume import measure_page

ORIENTATION = 0x0112
STORED_DPI = (300, 150)  # across and down the stored pixels


@pytest.fixture(autouse=True)
def _neutral(monkeypatch):
    monkeypatch.setattr("pagekit.pipeline.DETECTORS", {})


def _stored_page() -> Image.Image:
    """A page that is upright after one quarter turn clockwise (tag 6): 600 x 400 stored,
    400 x 600 upright."""
    upright = pages.page(size=(400, 600), seed=21, margin=(40, 60, 40, 60))
    return upright.transpose(Image.Transpose.ROTATE_90)


def _save(folder: Path, carrier: str, tag: int | None = 6, image=None, dpi=STORED_DPI) -> Path:
    image = _stored_page() if image is None else image
    path = folder / "src" / ("page.tif" if carrier == "tiff" else "page.png")
    path.parent.mkdir(parents=True, exist_ok=True)
    if carrier == "tiff":
        image.save(path, "TIFF", dpi=dpi, **({"tiffinfo": {ORIENTATION: tag}} if tag else {}))
    else:
        exif = Image.Exif()
        if tag:
            exif[ORIENTATION] = tag
        image.save(path, "PNG", dpi=dpi, exif=exif)
    return path


def _overrides(folder: Path, entries: list[dict]) -> Path:
    path = folder / "fix.json"
    path.write_text(json.dumps({"schema": "pagekit-overrides.v1", "overrides": entries}))
    return path


@pytest.mark.parametrize("carrier", ["png", "tiff"])
def test_a_tag_swaps_the_resolution_into_the_upright_axes(tmp_path, carrier):
    """Catches: no axis swap for tags 5 to 8 in the tagged resolution."""
    _save(tmp_path, carrier)
    out = tmp_path / "out"
    main(["prepare", str(tmp_path / "src"), "--output", str(out), "--crop", "content"])
    (page,) = json.loads((out / MANIFEST_NAME).read_text())["pages"]
    assert page["upright_resolution"] == pytest.approx([150, 300], abs=0.05)
    assert page["output"]["resolution"] == pytest.approx([150, 300], abs=0.05)
    # The margin (5 mm) is converted per upright axis: about 30 px across, 59 down,
    # past the whole page box, then held to the allowance (2 mm: 12 and 24 px).
    assert page["output"]["size"] == [400 + 2 * 12, 600 + 2 * 24]


@pytest.mark.parametrize("carrier", ["png", "tiff"])
def test_the_batch_check_measures_millimetres_in_the_upright_axes(tmp_path, carrier):
    """Catches: no axis swap for tags 5 to 8 in the batch checks."""
    _save(tmp_path, carrier)
    content = [
        {
            "source": f"src/page.{'tif' if carrier == 'tiff' else 'png'}",
            "step": "content_box",
            "page": 1,
            "value": [40, 60, 340, 540],
        }
    ]
    (page,) = plan(
        [tmp_path / "src"], tmp_path / "out", overrides_path=_overrides(tmp_path, content)
    ).pages
    found = measure_page(page)
    assert found["content_width"] == pytest.approx(300 / 150 * 25.4, rel=1e-3)
    assert found["content_height"] == pytest.approx(480 / 300 * 25.4, rel=1e-3)


@pytest.mark.parametrize("carrier", ["png", "tiff"])
def test_measure_measures_the_cut_in_the_upright_axes(tmp_path, carrier):
    """Catches: no axis swap for tags 5 to 8 in measure."""
    spread = pages.spread(size=(800, 600), seed=22, gutter=(380, 420)).transpose(
        Image.Transpose.ROTATE_90
    )
    _save(tmp_path, carrier, image=spread)
    cut = {"pages": 2, "cut": [[400.0, 0.0], [400.0, 599.0]]}
    detectors = {"split": Detector("t/1", lambda context: Answer(cut, 0.9, "Given.", ()))}
    execute(plan([tmp_path / "src"], tmp_path / "out", detectors=detectors))
    name = "page.tif" if carrier == "tiff" else "page.png"
    gold = tmp_path / "gold.json"
    gold.write_text(
        json.dumps(
            {
                "schema": "pagekit-gold.v1",
                "sources": [{"source": name, "cut": [[415, 0], [415, 600]]}],
            }
        )
    )
    steps = measure(tmp_path / "out", gold)["steps"]
    assert steps["cut"]["error_largest"] == pytest.approx(15 / 150 * 25.4, abs=0.01)


def test_padding_in_millimetres_follows_each_axis_resolution(tmp_path):
    """Catches: padding axes swapped."""
    _save(tmp_path, "png", tag=None, image=_stored_page().transpose(Image.Transpose.ROTATE_270))
    out = tmp_path / "out"
    main(["prepare", str(tmp_path / "src"), "--output", str(out), "--padding", "4mm"])
    (page,) = json.loads((out / MANIFEST_NAME).read_text())["pages"]
    assert page["geometry"]["regions"]["padding"] == {
        "left": round(4 * 300 / 25.4),
        "top": round(4 * 150 / 25.4),
        "right": round(4 * 300 / 25.4),
        "bottom": round(4 * 150 / 25.4),
    }


def test_a_density_one_percent_off_the_ratio_is_refused(tmp_path, capsys):
    """Catches: the density ratio tolerance widened to 0.05."""
    _save(tmp_path, "png", tag=None, dpi=(150, 150))
    entry = [{"source": "src/page.png", "step": "density", "page": 1, "value": [600, 594]}]
    status = main(
        [
            "prepare",
            str(tmp_path / "src"),
            "--output",
            str(tmp_path / "out"),
            "--overrides",
            str(_overrides(tmp_path, entry)),
        ]
    )
    assert status == 2 and "ratio" in capsys.readouterr().err


def test_the_photographed_region_is_clipped_to_the_written_page(tmp_path):
    """Catches: the photographed polygon not clipped to the content area."""
    _save(tmp_path, "png", tag=None, dpi=(150, 150))
    small = [
        {"source": "src/page.png", "step": "content_box", "page": 1, "value": [100, 100, 300, 250]}
    ]
    out = tmp_path / "out"
    main(
        [
            "prepare",
            str(tmp_path / "src"),
            "--output",
            str(out),
            "--padding",
            "10px",
            "--overrides",
            str(_overrides(tmp_path, small)),
        ]
    )
    (page,) = json.loads((out / MANIFEST_NAME).read_text())["pages"]
    left, top, right, bottom = page["geometry"]["regions"]["content"]
    photographed = page["geometry"]["regions"]["photographed"]
    assert sorted({tuple(round(v) for v in point) for point in photographed}) == sorted(
        {(left, top), (right, top), (right, bottom), (left, bottom)}
    )


def _context(size, tag, values) -> StepContext:
    image = Image.new("L", size, 220)
    data = image.tobytes()
    source = Source(
        path=Path("/nowhere/s.png"),
        relative="s.png",
        sha256=hashlib.sha256(data).hexdigest(),
        bytes=len(data),
        size=size,
        mode="L",
        file_dpi=(150.0, 150.0),
        tag_found=tag,
    )
    settings = {name: entry["value"] for name, entry in load_settings().items()}
    return StepContext(
        "split", None, source, (150.0, 150.0), values, settings, lambda: image, {}, tag
    )


def test_comparing_a_hand_set_cut_uses_the_tagged_frames_height():
    """Catches: the split comparison using the stored height instead of the tagged
    frame's."""
    # Stored 2000 x 1000; tag 6 makes the frame 1000 x 2000. A cut leaning 20 px over
    # the frame's height is 3.4 mm off at 150 dpi, under the 5 mm compare setting; over
    # the stored height (1000) it would be measured at the wrong place.
    context = _context((2000, 1000), 6, {"orientation": 0})
    manual = {"pages": 2, "cut": [[500.0, 0.0], [500.0, 2000.0]]}
    found = Answer({"pages": 2, "cut": [[500.0, 0.0], [520.0, 2000.0]]}, 0.9, "Found.", ())
    assert DETECTORS["split"].compare(manual, found, context) is None
    steeper = Answer({"pages": 2, "cut": [[500.0, 0.0], [540.0, 2000.0]]}, 0.9, "Found.", ())
    assert "6.8 mm" in DETECTORS["split"].compare(manual, steeper, context)


def test_the_paper_colour_of_each_page_follows_the_tag(tmp_path):
    """Catches: the paper colour ignoring the tag."""
    # A narrow left page with darker paper; read without the tag, its side of the cut
    # would take mostly the right page's paper.
    upright = Image.new("L", (800, 600), 200)
    upright.paste(240, (250, 0, 800, 600))
    draw = ImageDraw.Draw(upright)
    draw.rectangle((60, 100, 200, 140), fill=40)
    draw.rectangle((400, 100, 700, 140), fill=40)
    _save(tmp_path, "png", image=upright.transpose(Image.Transpose.ROTATE_90), dpi=(150, 150))
    cut = [
        {
            "source": "src/page.png",
            "step": "split",
            "value": {"pages": 2, "cut": [[250, 0], [250, 600]]},
        }
    ]
    out = tmp_path / "out"
    main(
        [
            "prepare",
            str(tmp_path / "src"),
            "--output",
            str(out),
            "--overrides",
            str(_overrides(tmp_path, cut)),
        ]
    )
    left, right = json.loads((out / MANIFEST_NAME).read_text())["pages"]
    assert (left["geometry"]["fill"]["colour"], right["geometry"]["fill"]["colour"]) == (200, 240)


def test_untrusted_tiff_keeps_its_stored_resolution_axes(tmp_path):
    """An untrusted tag on a TIFF takes the stored pixels and their own axes."""
    path = _save(tmp_path, "tiff")
    distrust = [{"source": "src/page.tif", "step": "tag_trust", "value": False}]
    out = tmp_path / "out"
    main(
        [
            "prepare",
            str(tmp_path / "src"),
            "--output",
            str(out),
            "--overrides",
            str(_overrides(tmp_path, distrust)),
        ]
    )
    (page,) = json.loads((out / MANIFEST_NAME).read_text())["pages"]
    assert page["upright_resolution"] == pytest.approx([300, 150], abs=0.05)
    with Image.open(io.BytesIO(path.read_bytes())) as opened:
        opened.load()
        assert tuple(page["geometry"]["source_size"]) == opened.size[::-1]
