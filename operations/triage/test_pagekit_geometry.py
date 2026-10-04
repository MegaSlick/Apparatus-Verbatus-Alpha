"""pagekit's page geometry as triage rows: the Door's page, rendered from the original
scan by `common.imaging.render_triage_derivative`, holds pagekit's page where the
mapping says, and loses none of its ink. Synthetic pages only."""

from __future__ import annotations

import io
import json
from collections import deque
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from common.contracts import triage as triage_manifest
from common.imaging import render_triage_derivative
from operations.triage.pagekit_geometry import (
    GUTTER,
    door_affine,
    door_colour_mode,
    make_manifest,
    make_row,
    map_pages,
)
from pagekit.geometry import Chain, apply
from pagekit.output import execute
from pagekit.prepare import plan

INK, PAPER = 20, 215


def _scan(folder: Path, size, dots, name="scan.png", tag=None, mode="L", ink=INK, paper=PAPER):
    folder.mkdir(parents=True, exist_ok=True)
    image = Image.new(mode, size, paper)
    draw = ImageDraw.Draw(image)
    for x, y in dots:
        draw.rectangle((x - 5, y - 5, x + 5, y + 5), fill=ink)
    path = folder / name
    if tag is None:
        image.save(path, dpi=(300, 300))
    else:
        exif = Image.Exif()
        exif[0x0112] = tag  # the orientation tag
        image.save(path, dpi=(300, 300), exif=exif)
    return path


def _prepare(tmp_path: Path, scan: Path, overrides: list[dict], settings=None):
    fixes = tmp_path / "fixes.json"
    for entry in overrides:
        entry["source"] = scan.relative_to(tmp_path).as_posix()
    fixes.write_text(json.dumps({"schema": "pagekit-overrides.v1", "overrides": overrides}))
    prepared = plan([scan], tmp_path / "out", overrides_path=fixes, settings_overrides=settings)
    manifest = execute(prepared)
    pages = prepared.pages
    # pagekit's own paper colour, as its manifest records it.
    fills = [
        colour if isinstance(colour, list) else [colour]
        for colour in (entry["geometry"]["fill"]["colour"] for entry in manifest["pages"])
    ]
    with Image.open(scan) as stored:
        master_mode = stored.mode
    modes = [door_colour_mode(page.mode, master_mode) for page in pages]
    mapped = map_pages(
        [page.chain for page in pages], pages[0].steps["split"]["value"], fills, modes
    )
    return pages, mapped


def _door(scan: Path, part: dict) -> Image.Image:
    data, _ = render_triage_derivative(scan.read_bytes(), page_index=0, part=part)
    return Image.open(io.BytesIO(data))


def _blobs(image: Image.Image) -> list[tuple[float, float, int]]:
    """Centre (continuous coordinates) and size of each dark blob."""
    image = image.convert("L")
    width, height = image.size
    pixels = image.load()
    seen: set[tuple[int, int]] = set()
    found = []
    for y in range(height):
        for x in range(width):
            if pixels[x, y] >= 100 or (x, y) in seen:
                continue
            queue, points = deque([(x, y)]), []
            seen.add((x, y))
            while queue:
                px, py = queue.popleft()
                points.append((px, py))
                for nx, ny in ((px + 1, py), (px - 1, py), (px, py + 1), (px, py - 1)):
                    if (
                        0 <= nx < width
                        and 0 <= ny < height
                        and (nx, ny) not in seen
                        and pixels[nx, ny] < 100
                    ):
                        seen.add((nx, ny))
                        queue.append((nx, ny))
            if len(points) >= 20:
                found.append(
                    (
                        sum(p[0] for p in points) / len(points) + 0.5,
                        sum(p[1] for p in points) / len(points) + 0.5,
                        len(points),
                    )
                )
    return found


@pytest.mark.parametrize("size", [(700, 1000), (703, 1000), (700, 1001), (701, 999)])
@pytest.mark.parametrize("turns", [0, 1, 2, 3])
def test_a_page_with_no_skew_is_cut_exactly_as_pagekit_cut_it(tmp_path, turns, size):
    dots = [(150, 160), (520, 140), (330, 600), (140, 880), (560, 860)]
    scan = _scan(tmp_path / "scans", size, dots)
    # The margin carries pagekit's crop past the scan's edge, where it shows paper.
    width, height = size if turns % 2 == 0 else size[::-1]
    box = [10, 20, width - 10, height - 20]
    pages, mapped = _prepare(
        tmp_path,
        scan,
        [
            {"step": "orientation", "value": turns},
            {"step": "content_box", "page": 1, "value": box},
        ],
    )
    (page,), (door_page,) = pages, mapped
    assert page.chain.crop_box[0] < 0, "the crop no longer reaches past the scan"
    assert door_page.notes == ()
    assert (
        door_page.part["rotation"]["rotation_millidegrees"] == [0, 90_000, 180_000, -90_000][turns]
    )
    door = _door(scan, door_page.part)
    with Image.open(tmp_path / "out" / page.output_name) as prepared:
        assert door.size == prepared.size
        assert door_page.pagekit_box_in_door == (0, 0, *prepared.size)
        assert door.tobytes() == prepared.convert("L").tobytes()


def _ink(image: Image.Image, x: float, y: float, reach: int = 8) -> int:
    """How much ink lies within `reach` of (x, y): the darkness below paper, summed,
    which interpolation keeps where a thresholded pixel count would not."""
    grey = image.convert("L")
    left, top = round(x) - reach, round(y) - reach
    window = grey.crop((left, top, left + 2 * reach, top + 2 * reach))
    return sum(max(0, PAPER - level) * count for level, count in enumerate(window.histogram()))


def _matched(door_blobs, pagekit_blobs, offset):
    pairs = []
    for x, y, size in pagekit_blobs:
        nearest = min(
            door_blobs, key=lambda b: (b[0] - offset[0] - x) ** 2 + (b[1] - offset[1] - y) ** 2
        )
        pairs.append(((x, y, size), nearest))
    return pairs


@pytest.mark.parametrize(("turns", "skew"), [(1, 1.5), (3, -2.0), (0, 0.8), (2, -0.7)])
def test_a_turned_skewed_cropped_page_lands_where_pagekit_put_it(tmp_path, turns, skew):
    dots = [(130, 130), (570, 130), (350, 500), (130, 870), (570, 870), (300, 300)]
    scan = _scan(tmp_path / "scans", (700, 1000), dots)
    # The crop runs 14 px outside the outermost dots, in the levelled page's pixels, so
    # a Door page that lost any of pagekit's crop would lose part of a dot.
    levelled = Chain.build((700, 1000), turns, {"pages": 1}, 0, 0.0, skew)
    centres = levelled.forward([(x + 0.5, y + 0.5) for x, y in dots])
    box = [
        round(min(x for x, _ in centres)) - 14,
        round(min(y for _, y in centres)) - 14,
        round(max(x for x, _ in centres)) + 14,
        round(max(y for _, y in centres)) + 14,
    ]
    pages, mapped = _prepare(
        tmp_path,
        scan,
        [
            {"step": "orientation", "value": turns},
            {"step": "skew", "page": 1, "value": skew},
            {"step": "content_box", "page": 1, "value": box},
            {"step": "margin", "page": 1, "value": 0},
        ],
    )
    (page,), (door_page,) = pages, mapped
    rotation = door_page.part["rotation"]["rotation_millidegrees"]
    assert rotation % 90_000 != 0
    assert door_page.notes == ()
    door = _door(scan, door_page.part)
    with Image.open(tmp_path / "out" / page.output_name) as opened:
        prepared = opened.copy()
    # Cut tight: pagekit's page, placed to within half a pixel on each axis.
    assert door.size == door_page.door_size == prepared.size
    assert all(abs(value) <= 0.5 for value in door_page.pagekit_box_in_door[:2])
    pagekit_blobs = _blobs(prepared)
    assert len(pagekit_blobs) == len(dots)
    door_blobs = _blobs(door)
    # No ink lost: every dot pagekit's page holds is on the Door's page, whole.
    for (x, y, _size), (dx, dy, _door_size) in _matched(
        door_blobs, pagekit_blobs, door_page.pagekit_box_in_door[:2]
    ):
        assert abs(dx - door_page.pagekit_box_in_door[0] - x) < 0.5
        assert abs(dy - door_page.pagekit_box_in_door[1] - y) < 0.5
        assert _ink(door, dx, dy) == pytest.approx(_ink(prepared, x, y), rel=0.03)
    # The Door's map from the scan agrees with where the dots really are.
    to_door, _ = door_affine(door_page.part)
    for (x, y), (dx, dy) in zip(
        sorted(dots), sorted(apply(to_door, [(x + 0.5, y + 0.5) for x, y in dots])), strict=True
    ):
        assert min((bx - dx) ** 2 + (by - dy) ** 2 for bx, by, _ in door_blobs) < 0.25, (x, y)


@pytest.mark.parametrize(("turns", "skew"), [(0, 2.0), (1, -1.5)])
def test_a_skewed_pages_margin_is_pagekits_paper_never_black(tmp_path, turns, skew):
    scan = _scan(tmp_path / "scans", (500, 700), [])
    pages, mapped = _prepare(
        tmp_path,
        scan,
        [{"step": "orientation", "value": turns}, {"step": "skew", "page": 1, "value": skew}],
    )
    (page,), (door_page,) = pages, mapped
    door = _door(scan, door_page.part)
    with Image.open(tmp_path / "out" / page.output_name) as prepared:
        assert door.size == prepared.size
        # The levelled page's corners lie beyond the scan: pagekit and the Door fill them
        # with the same recorded paper level.
        for corner in ((0, 0), (door.width - 1, 0), (0, door.height - 1)):
            assert door.getpixel(corner) == prepared.getpixel(corner) == PAPER
    assert door_page.part["fill"] == {"levels": [PAPER]}
    assert door.getextrema()[0] > 150, "black reached the Door's page"


def test_a_leaning_split_with_overlap_drops_no_ink_across_the_frames_pages(tmp_path):
    # Ink across the gutter, in the band both of pagekit's pages keep past the cut.
    dots = [(x, y) for x in (120, 560, 610, 640, 670, 700, 740, 1280) for y in (120, 500, 880)]
    scan = _scan(tmp_path / "scans", (1400, 1000), dots, name="spread.png")
    pages, mapped = _prepare(
        tmp_path,
        scan,
        [
            {"step": "split", "value": {"pages": 2, "cut": [[620, 0], [680, 1000]]}},
            # The left page's own crop stops short of the bottom, where the right page
            # still reaches past the straight split into the left page's region.
            {"step": "content_box", "page": 1, "value": [0, 0, 500, 400]},
            {"step": "margin", "page": 1, "value": 0},
            {"step": "skew", "page": 2, "value": 0.9},
        ],
    )
    assert [note.code for note in mapped[0].notes] == [GUTTER]
    assert [note.code for note in mapped[1].notes] == [GUTTER]
    row = make_row(
        corpus_id="synthetic",
        source_sha256=pages[0].source.sha256,
        frame=pages[0].source.size,
        pages=mapped,
        revision="0.1.0",
        confidence=0,
        human_override=True,
    )
    manifest = make_manifest("synthetic", [row])
    assert triage_manifest.validate_manifest(manifest) is manifest  # regions partition
    assert row["actor"] == {"kind": "producer", "identity": "pagekit", "revision": "0.1.0"}
    assert {part["colour_mode"] for part in row["split"]["parts"]} == {"keep"}
    # Every dot either of pagekit's pages shows is, whole, on one of the Door's pages.
    shown_dots = set()
    for page in pages:
        with Image.open(tmp_path / "out" / page.output_name) as prepared:
            to_source = page.chain.output_to_source()
            for x, y, _size in _blobs(prepared):
                ((sx, sy),) = apply(to_source, [(x, y)])
                shown_dots.add(
                    min(dots, key=lambda d: (d[0] + 0.5 - sx) ** 2 + (d[1] + 0.5 - sy) ** 2)
                )
    assert {(640, 500), (670, 500), (700, 500), (640, 880)} <= shown_dots
    assert mapped[0].part["region"]["w"] == 650  # the cut's middle: (640, 880) is left of it
    on_door = set()
    for door_page in mapped:
        door = _door(scan, door_page.part)
        to_door, _ = door_affine(door_page.part)
        found = _blobs(door)
        for dot in shown_dots:
            ((dx, dy),) = apply(to_door, [(dot[0] + 0.5, dot[1] + 0.5)])
            if any(
                (bx - dx) ** 2 + (by - dy) ** 2 < 0.25 and size >= 100 for bx, by, size in found
            ):
                on_door.add(dot)
    assert on_door == shown_dots


def test_a_quarter_turned_spread_splits_the_scan_across_its_other_axis(tmp_path):
    scan = _scan(tmp_path / "scans", (1000, 1400), [(500, 300), (500, 1100)], name="side.png")
    _pages, mapped = _prepare(
        tmp_path,
        scan,
        [
            {"step": "orientation", "value": 1},
            {"step": "split", "value": {"pages": 2, "cut": [[700, 0], [700, 1000]]}},
        ],
    )
    regions = [page.part["region"] for page in mapped]
    # One clockwise turn puts the upright left page at the bottom of the scan.
    assert [(r["x"], r["w"]) for r in regions] == [(0, 1000), (0, 1000)]
    assert regions[0]["y"] == regions[1]["h"] and regions[1]["y"] == 0
    assert {page.part["rotation"]["rotation_millidegrees"] for page in mapped} == {90_000}


def test_a_page_whose_margin_runs_further_past_the_scan_than_the_door_allows_is_refused(
    tmp_path,
):
    """pagekit's 2 mm margin allowance is a quarter of a 100-pixel-wide scan, past the
    fifth the Door lets a post-crop run beyond the rotated scan: the mapping refuses it
    here, with the Door's own check, rather than the Door refusing it on the pod."""
    from operations.triage.pagekit_geometry import MappingError

    scan = _scan(tmp_path / "scans", (100, 80), [])
    with pytest.raises(MappingError, match="post-crop"):
        _prepare(tmp_path, scan, [{"step": "content_box", "page": 1, "value": [0, 0, 100, 80]}])


@pytest.mark.parametrize("tag", [1, 3, 6, 8])
def test_a_scan_whose_orientation_tag_turns_it_is_cut_exactly_as_pagekit_cut_it(tmp_path, tag):
    """The Door renders the scan as stored; a tag that turns it folds into the rotation."""
    dots = [(150, 160), (520, 140), (330, 600), (140, 880)]
    scan = _scan(tmp_path / "scans", (703, 1000), dots, tag=tag)
    pages, mapped = _prepare(tmp_path, scan, [])
    (page,), (door_page,) = pages, mapped
    assert page.chain.tag == tag
    door = _door(scan, door_page.part)
    with Image.open(tmp_path / "out" / page.output_name) as prepared:
        assert door.size == prepared.size
        assert door.tobytes() == prepared.convert("L").tobytes()


@pytest.mark.parametrize("tag", [2, 4, 5, 7])
def test_a_scan_whose_orientation_tag_mirrors_it_is_refused(tmp_path, tag):
    """Triage turns a scan but never mirrors it, so a mirrored tag cannot reach the Door."""
    from operations.triage.pagekit_geometry import MappingError

    scan = _scan(tmp_path / "scans", (703, 1000), [(150, 160)], tag=tag)
    with pytest.raises(MappingError, match="mirror"):
        _prepare(tmp_path, scan, [])


@pytest.mark.parametrize(
    ("ink", "rule", "colour_mode"),
    [
        ((60, 60, 60), "luminance", "grayscale"),  # equal channels: exact
        ((120, 30, 40), "luminance", "grayscale"),  # a reviewed luminance conversion
    ],
)
def test_a_grey_page_is_rendered_grey_by_the_door_exactly_as_pagekit_made_it(
    tmp_path, ink, rule, colour_mode
):
    dots = [(150, 160), (520, 140), (330, 600)]
    scan = _scan(tmp_path / "scans", (701, 999), dots, mode="RGB", ink=ink, paper=(215, 215, 215))
    pages, mapped = _prepare(
        tmp_path,
        scan,
        [
            {"step": "output_mode", "page": 1, "value": "grey"},
            {"step": "orientation", "value": 1},
        ],
        settings={"grey_rule": rule},
    )
    (page,), (door_page,) = pages, mapped
    assert page.mode["mode"] == "grey"
    assert door_page.part["colour_mode"] == colour_mode
    door = _door(scan, door_page.part)
    with Image.open(tmp_path / "out" / page.output_name) as prepared:
        assert prepared.mode == door.mode == "L"
        assert door.size == prepared.size
        assert door.tobytes() == prepared.tobytes()


def test_a_grey_page_made_from_one_channel_is_refused(tmp_path):
    """The Door converts to grey only by luminance, so a one-channel rule has no row."""
    from operations.triage.pagekit_geometry import MappingError

    scan = _scan(
        tmp_path / "scans",
        (701, 999),
        [(150, 160)],
        mode="RGB",
        ink=(120, 30, 40),
        paper=(215, 215, 215),
    )
    with pytest.raises(MappingError, match="grey"):
        _prepare(
            tmp_path,
            scan,
            [{"step": "output_mode", "page": 1, "value": "grey"}],
            settings={"grey_rule": "red"},
        )


@pytest.mark.parametrize("turns", [0, 1])
def test_a_padded_page_re_derives_exactly_as_pagekit_padded_it(tmp_path, turns):
    dots = [(150, 160), (520, 140), (330, 600), (140, 880)]
    scan = _scan(tmp_path / "scans", (703, 1000), dots)
    pages, mapped = _prepare(
        tmp_path, scan, [{"step": "orientation", "value": turns}], settings={"padding_px": 40}
    )
    (page,), (door_page,) = pages, mapped
    assert page.chain.padding == (40, 40, 40, 40)
    door = _door(scan, door_page.part)
    with Image.open(tmp_path / "out" / page.output_name) as prepared:
        assert door.size == prepared.size
        assert door.tobytes() == prepared.convert("L").tobytes()


def test_padding_past_the_doors_limit_is_refused_at_prepare(tmp_path):
    from operations.triage.pagekit_geometry import MappingError

    scan = _scan(tmp_path / "scans", (400, 500), [(150, 160)])
    with pytest.raises(MappingError, match="post-crop"):
        _prepare(tmp_path, scan, [], settings={"padding_px": 120})


@pytest.mark.parametrize("tag", [3, 6, 8])
def test_a_tagged_tiff_scan_is_cut_by_the_door_as_pagekit_cut_it(tmp_path, tag):
    """Pillow turns a TIFF upright as it opens it, and the Door and the Exemplar open
    masters through Pillow, so the Door's frame is that turned one. The row must
    describe the same frame (its size is checked against it at the Door) and its page
    must be pagekit's."""
    dots = [(150, 160), (520, 140), (330, 600), (140, 880)]
    folder = tmp_path / "scans"
    folder.mkdir()
    image = Image.new("L", (703, 1000), PAPER)
    draw = ImageDraw.Draw(image)
    for x, y in dots:
        draw.rectangle((x - 5, y - 5, x + 5, y + 5), fill=INK)
    exif = Image.Exif()
    exif[0x0112] = tag
    scan = folder / "scan.tif"
    image.save(scan, dpi=(300, 300), exif=exif, compression="tiff_adobe_deflate")
    pages, mapped = _prepare(tmp_path, scan, [])
    (page,), (door_page,) = pages, mapped
    with Image.open(scan) as opened:
        assert page.chain.source_size == opened.size, "the row's frame is not the Door's"
    door = _door(scan, door_page.part)
    with Image.open(tmp_path / "out" / page.output_name) as prepared:
        assert door.size == prepared.size
        assert door.tobytes() == prepared.convert("L").tobytes()


@pytest.mark.parametrize("compression", ["raw", "tiff_adobe_deflate", "tiff_lzw"])
@pytest.mark.parametrize("tag", range(1, 9))
def test_every_reader_opens_a_tagged_tiff_from_its_bytes_as_pagekit_does(
    tmp_path, monkeypatch, compression, tag
):
    """The image library turns a TIFF by its tag on opening it, but opened by path an
    uncompressed one with tag 5 to 8 can come out with its stored size. pagekit, the
    Door's decoder and the renderer the Door and the Exemplar share all open a master
    from its bytes, so the row's frame is the frame the Door opens and its page is
    pagekit's, byte for byte, for every tag and compression."""
    monkeypatch.syspath_prepend(
        str(Path(__file__).resolve().parents[2] / "pipeline" / "1_exemplar")
    )
    from image_formats import decode_raster

    dots = [(40, 50), (150, 40), (90, 250)]
    folder = tmp_path / "scans"
    folder.mkdir()
    image = Image.new("L", (203, 300), PAPER)
    draw = ImageDraw.Draw(image)
    for x, y in dots:
        draw.rectangle((x - 5, y - 5, x + 5, y + 5), fill=INK)
    exif = Image.Exif()
    exif[0x0112] = tag
    scan = folder / "scan.tif"
    image.save(scan, dpi=(300, 300), exif=exif, compression=compression)
    data = scan.read_bytes()
    with Image.open(io.BytesIO(data)) as opened:
        from_bytes = opened.size

    pages, mapped = _prepare(tmp_path, scan, [])
    (page,), (door_page,) = pages, mapped

    assert page.chain.source_size == from_bytes
    decoded = decode_raster(data, page_index=0)
    assert (decoded.width, decoded.height) == from_bytes
    door = _door(scan, door_page.part)
    with Image.open(tmp_path / "out" / page.output_name) as prepared:
        assert door.size == prepared.size
        assert door.tobytes() == prepared.convert("L").tobytes()
