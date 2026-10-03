"""Crop checks on synthetic pages drawn here; no real register material."""

from __future__ import annotations

import ast
import hashlib
import json
import shutil
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from pagekit.__main__ import main
from pagekit.check import SCHEMA, CheckError, check, otsu_threshold, report_json

HERE = Path(__file__).resolve().parent
SMALL = {"min_short_side_px": 500}


def _lines(draw: ImageDraw.ImageDraw, x0: int, x1: int, y0: int, y1: int) -> None:
    """Text-like bars: 12 px tall, one every 30 px, with word gaps."""
    for y in range(y0, y1, 30):
        for x in range(x0, x1, 90):
            draw.rectangle((x, y, min(x + 70, x1) - 1, y + 11), fill=20)


def _page(tmp_path: Path, size=(1000, 1400), name="page.png", dpi=(300, 300), extra=None) -> Path:
    image = Image.new("L", size, 235)
    draw = ImageDraw.Draw(image)
    _lines(draw, 150, 850, 200, 1200)
    if extra:
        extra(draw)
    path = tmp_path / name
    if dpi:
        image.save(path, dpi=dpi)
    else:
        image.save(path)
    return path


def _spread(tmp_path: Path) -> Path:
    image = Image.new("L", (2000, 1400), 235)
    draw = ImageDraw.Draw(image)
    _lines(draw, 150, 900, 200, 1200)
    _lines(draw, 1100, 1850, 200, 1200)
    path = tmp_path / "spread.png"
    image.save(path, dpi=(300, 300))
    return path


def _checks(report: dict) -> set[str]:
    return {flag["check"] for flag in report["flags"]}


def test_clean_crop_inside_margins_raises_no_flag(tmp_path):
    report = check(_page(tmp_path), [(100, 150, 900, 1250)], overrides=SMALL)
    assert report["flags"] == []
    assert report["verdict"] == "no_flags"
    assert report["checks"]["ink_discarded"]["discarded_ink_pixels"] == 0
    assert report["page"]["ink_detected"] is True
    assert report["page"]["page_ink_pixels"] > 0


def test_margin_note_outside_the_crop_is_flagged_as_discarded(tmp_path):
    def note(draw):
        draw.rectangle((920, 600, 979, 699), fill=30)

    report = check(_page(tmp_path, extra=note), [(100, 150, 900, 1250)], overrides=SMALL)
    discarded = report["checks"]["ink_discarded"]
    assert _checks(report) == {"ink_discarded"}
    # Every pixel of the note touches another, so none is cleared as a lone speck.
    assert discarded["discarded_ink_pixels"] == 60 * 100
    assert discarded["discarded_ink_bbox"] == [920, 600, 980, 700]
    assert 0 < discarded["discarded_ink_share"] < 1
    # The note does not reach the crop edge, so no edge is reported as cutting it.
    assert not any(edge["flag"] for edge in report["checks"]["ink_cut_at_edge"])


def test_text_line_cut_by_a_crop_edge_is_flagged_at_that_edge(tmp_path):
    report = check(_page(tmp_path), [(100, 150, 625, 1250)], overrides=SMALL)
    cut = [edge for edge in report["checks"]["ink_cut_at_edge"] if edge["flag"]]
    assert [(edge["crop"], edge["edge"]) for edge in cut] == [(0, "right")]
    assert cut[0]["crossing_positions"] >= 5
    assert all(start >= 150 and end <= 1250 for start, end in cut[0]["crossing_spans"])
    assert "ink_cut_at_edge" in _checks(report)


def test_split_on_the_gutter_raises_no_flag(tmp_path):
    report = check(
        _spread(tmp_path), [(100, 150, 1000, 1250), (1000, 150, 1900, 1250)], overrides=SMALL
    )
    split = report["checks"]["split_off_gutter"]
    assert split["split_source"] == "between two side-by-side crops"
    assert split["split_x"] == 1000
    assert split["gutter_run"][0] <= 1000 < split["gutter_run"][1]
    assert split["split_outside_gutter_px"] == 0
    assert report["flags"] == []


def test_split_off_the_gutter_is_flagged(tmp_path):
    report = check(_spread(tmp_path), [(100, 150, 1900, 1250)], split_x=700, overrides=SMALL)
    split = report["checks"]["split_off_gutter"]
    assert split["split_source"] == "declared"
    assert split["split_outside_gutter_px"] > 0
    assert split["split_column_ink_share"] > 0
    assert "split_off_gutter" in _checks(report)


def test_missing_dpi_is_flagged(tmp_path):
    report = check(_page(tmp_path, dpi=None), [(100, 150, 900, 1250)], overrides=SMALL)
    assert report["checks"]["resolution"]["dpi"] is None
    assert _checks(report) == {"resolution"}


def test_short_side_below_the_minimum_is_flagged(tmp_path):
    report = check(_page(tmp_path), [(100, 150, 900, 1250)])
    resolution = report["checks"]["resolution"]
    assert resolution["dpi"] == [300.0, 300.0]
    assert resolution["short_side_px"] == 1000
    assert _checks(report) == {"resolution"}


def test_dark_scanner_backdrop_is_outside_the_page(tmp_path):
    def backdrop(draw):
        draw.rectangle((0, 0, 999, 39), fill=10)
        draw.rectangle((960, 0, 999, 1399), fill=10)

    report = check(_page(tmp_path, extra=backdrop), [(100, 150, 900, 1250)], overrides=SMALL)
    assert report["page"]["page_area"] == [0, 40, 960, 1400]
    assert report["flags"] == []


def test_a_page_with_no_ink_detected_goes_to_review_not_no_flags(tmp_path):
    path = tmp_path / "blank.png"
    Image.new("L", (800, 1000), 240).save(path, dpi=(300, 300))
    report = check(path, [(50, 50, 750, 950)], overrides=SMALL)
    assert report["page"]["ink_detected"] is False
    assert _checks(report) == {"ink_detection"}
    assert report["verdict"] == "review"
    assert main(["check", "--master", str(path), "--crop", "50,50,750,950"]) == 1


def test_thin_pen_lines_outside_the_crop_are_counted_and_lone_specks_are_not(tmp_path):
    def note(draw):
        for y in range(300, 600, 10):
            draw.line((920, y, 990, y + 3), fill=20, width=1)

    report = check(_page(tmp_path, extra=note), [(100, 150, 900, 1250)], overrides=SMALL)
    assert report["checks"]["ink_discarded"]["discarded_ink_pixels"] >= 30 * 70
    assert "ink_discarded" in _checks(report)

    def specks(draw):
        for y in range(300, 1300, 20):
            draw.point((950, y), fill=20)

    report = check(_page(tmp_path, extra=specks), [(100, 150, 900, 1250)], overrides=SMALL)
    assert report["checks"]["ink_discarded"]["discarded_ink_pixels"] == 0
    assert report["flags"] == []


def test_a_sixteen_bit_master_is_refused_loudly(tmp_path, capsys):
    path = tmp_path / "deep.png"
    Image.new("I;16", (800, 1000), 40000).save(path)
    assert main(["check", "--master", str(path), "--crop", "50,50,750,950"]) == 2
    assert "not supported" in capsys.readouterr().err


def test_any_failure_while_reading_the_master_is_cannot_check(tmp_path, monkeypatch, capsys):
    path = _page(tmp_path)

    def broken(*_args, **_kwargs):
        raise RuntimeError("decoder fell over")

    monkeypatch.setattr("pagekit.check.Image.open", broken)
    assert main(["check", "--master", str(path), "--crop", "100,150,900,1250"]) == 2
    assert "decoder fell over" in capsys.readouterr().err


def test_the_digest_names_the_bytes_that_were_measured(tmp_path, monkeypatch):
    path = _page(tmp_path)
    opened = []
    real_open = Image.open

    def spy(source, *args, **kwargs):
        opened.append(source)
        return real_open(source, *args, **kwargs)

    monkeypatch.setattr("pagekit.check.Image.open", spy)
    check(path, [(100, 150, 900, 1250)], overrides=SMALL)
    assert len(opened) == 1 and opened[0].getvalue() == path.read_bytes()


def test_otsu_splits_a_two_level_histogram_between_the_levels():
    histogram = [0] * 256
    histogram[40], histogram[200] = 100, 900
    assert 40 <= otsu_threshold(histogram) < 200


def test_same_input_gives_byte_identical_json_and_master_is_untouched(tmp_path):
    path = _page(tmp_path)
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    first = report_json(check(path, [(100, 150, 625, 1250)]))
    second = report_json(check(path, [(100, 150, 625, 1250)]))
    elsewhere = tmp_path / "copy"
    elsewhere.mkdir()
    shutil.copy(path, elsewhere / path.name)
    third = report_json(check(elsewhere / path.name, [(100, 150, 625, 1250)]))
    assert first == second == third
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_report_is_the_closed_v1_shape(tmp_path):
    path = _page(tmp_path)
    report = check(path, [(100, 150, 900, 1250)])
    assert set(report) == {
        "schema",
        "tool",
        "input",
        "page",
        "checks",
        "flags",
        "verdict",
        "thresholds",
        "thresholds_measured",
        "thresholds_note",
    }
    assert report["schema"] == SCHEMA == "pagekit-crop-check.v1"
    assert report["thresholds_measured"] is False
    assert {entry["status"] for entry in report["thresholds"].values()} == {"UNMEASURED"}
    assert set(report["checks"]) == {
        "ink_discarded",
        "ink_cut_at_edge",
        "split_off_gutter",
        "resolution",
    }
    assert report["input"]["master"] == {
        "name": "page.png",
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "bytes": path.stat().st_size,
    }
    assert len(report["checks"]["ink_cut_at_edge"]) == 4


@pytest.mark.parametrize(
    "crops, split_x",
    [([], None), ([(0, 0, 1001, 10)], None), ([(10, 10, 10, 20)], None), ([(0, 0, 9, 9)], 0)],
)
def test_unusable_inputs_are_refused(tmp_path, crops, split_x):
    with pytest.raises(CheckError):
        check(_page(tmp_path), crops, split_x)


def test_cli_json_round_trip(tmp_path, capsys):
    path = _page(tmp_path)
    status = main(["check", "--master", str(path), "--crop", "100,150,625,1250", "--json"])
    printed = capsys.readouterr().out
    assert status == 1
    assert printed == report_json(check(path, [(100, 150, 625, 1250)]))
    assert json.loads(printed)["verdict"] == "review"


def test_cli_exit_codes(tmp_path, capsys):
    path = _page(tmp_path)
    clean = ["check", "--master", str(path), "--crop", "100,150,900,1250"]
    assert main([*clean, "--min-short-side-px", "500"]) == 0
    assert main(["check", "--master", str(path), "--crop", "1,2,3"]) == 2
    assert "pagekit:" in capsys.readouterr().err


def test_pagekit_imports_nothing_from_this_repository():
    allowed = set(sys.stdlib_module_names) | {"PIL", "pagekit", "pytest"}
    for source in sorted(HERE.rglob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module]
            else:
                continue
            for name in names:
                assert name.split(".")[0] in allowed, f"{source} imports {name}"


def test_crop_edges_on_or_past_the_page_border_are_not_compared(tmp_path):
    def backdrop(draw):
        draw.rectangle((960, 0, 999, 1399), fill=10)

    report = check(_page(tmp_path, extra=backdrop), [(0, 0, 1000, 1400)], overrides=SMALL)
    assert [edge["compared"] for edge in report["checks"]["ink_cut_at_edge"]] == [False] * 4
    assert report["flags"] == []
