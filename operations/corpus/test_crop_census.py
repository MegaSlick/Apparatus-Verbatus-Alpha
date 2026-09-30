"""The crop census, proven on synthetic pages only."""

import json
from dataclasses import replace
from io import BytesIO

import pytest
from PIL import Image

from common import page_render
from common.request_capacity import request_fits, smart_resize
from operations.corpus import crop_census
from operations.corpus.crop_census import (
    admitted_crops,
    census,
    census_page,
    crop_size,
    gain_bp,
    main,
    quantiles,
)
from operations.corpus.perlector_request_fit import (
    page_feed_for,
    page_request,
    page_shape,
    perlector_row,
    sealed_protocol,
)

OPTIONS = {"min_gain_bp": 15000, "crop_width_bp": 5000, "crop_height_bp": 1250, "max_crops": 8}


def _page(page_id: str, width: int, height: int, records: int = 8) -> dict:
    return {
        "page_id": page_id,
        "width": width,
        "height": height,
        "image": f"pages/{page_id}.jpg",
        "records": [
            {
                "record_id": f"r{index}",
                "bbox": [100, 100 + 300 * index, width - 200, 280],
                "text": "baptême de Pierre fils de Jean\n" * 6,
            }
            for index in range(records)
        ],
    }


def test_the_render_size_rule_is_the_size_the_renderer_produces():
    for size in [(900, 700), (2560, 1000), (2561, 1000), (4000, 5500), (6001, 3)]:
        image = BytesIO()
        Image.new("L", size, 255).save(image, format="PNG")
        rendered, transform = page_render._downscale_page(image.getvalue(), maximum_edge=2560)
        target = transform["target_dimensions"]
        assert (target["w"], target["h"]) == page_render.render_size(size, 2560)
        with Image.open(BytesIO(rendered)) as shown:
            assert shown.size == page_render.render_size(size, 2560)


def test_a_page_is_counted_from_the_request_the_page_path_sends():
    row, sealed = perlector_row(), sealed_protocol()
    entry = census_page(row, sealed, _page("big", 4000, 5500), OPTIONS)
    feed = page_feed_for(sealed, page_shape(_page("big", 4000, 5500)), {})
    admitted = page_request(row, feed)["capacity"]
    assert entry["sent"] == [1862, 2560]
    assert entry["seen"] == [
        admitted["images"][0]["resized_width"],
        admitted["images"][0]["resized_height"],
    ]
    height, width = smart_resize(2560, 1862, factor=32, min_pixels=65536, max_pixels=5299200)
    assert entry["seen"] == [width, height]
    assert entry["gain_bp"] == gain_bp((4000, 5500), (width, height)) == 21518
    assert entry["need"] == admitted["need"] and entry["headroom"] == admitted["headroom"]
    assert entry["crop_native"] == [2000, 687]
    assert entry["qualifies"] is True


def test_a_page_the_render_already_shows_near_native_does_not_qualify():
    entry = census_page(perlector_row(), sealed_protocol(), _page("small", 1800, 2400), OPTIONS)
    assert entry["sent"] == [1800, 2400] and entry["gain_bp"] < 15000
    assert entry["k"] >= 1 and entry["qualifies"] is False


def test_k_is_the_most_crops_round_two_fits_and_one_more_does_not():
    sealed = sealed_protocol()
    feed = page_feed_for(sealed, page_shape(_page("big", 4000, 5500)), {})
    row = perlector_row()
    capacity = page_request(row, feed)["capacity"]
    crop = crop_size((4000, 5500), 5000, 2500)
    # Room for exactly two crops above the admitted request.
    per_crop = request_fits(row, [crop], 0, 0)["need"] + 2
    tight = replace(row, max_model_len=capacity["need"] + 2 * per_crop + per_crop - 1)
    tight_capacity = page_request(tight, feed)["capacity"]
    assert admitted_crops(tight, tight_capacity, crop, 8) == 2
    assert admitted_crops(tight, tight_capacity, crop, 1) == 1
    roomy = replace(row, max_model_len=capacity["need"] + 3 * per_crop)
    assert admitted_crops(roomy, page_request(roomy, feed)["capacity"], crop, 8) == 3


def test_a_page_whose_request_is_refused_is_counted_with_no_crops(monkeypatch):
    row = perlector_row()
    monkeypatch.setattr(crop_census, "perlector_row", lambda: replace(row, max_model_len=8000))
    report = census([_page("big", 4000, 5500)], OPTIONS, "0" * 64)
    (entry,) = report["pages"]
    assert entry["page_request"] == "refused" and entry["headroom"] < 0
    assert entry["k"] == 0 and entry["qualifies"] is False
    assert report["summary"]["page_request_refused"] == 1
    assert report["summary"]["k_histogram"]["0"] == 1


def test_quantiles_are_nearest_rank():
    assert quantiles([]) == {str(q): None for q in crop_census.QUANTILES_BP}
    values = list(range(10, 110, 10))
    assert quantiles(values) == {
        "0": 10,
        "1000": 10,
        "2500": 30,
        "5000": 50,
        "7500": 80,
        "9000": 90,
        "10000": 100,
    }


def test_the_report_is_deterministic_and_summarised(tmp_path, capsys):
    manifest = tmp_path / "page_manifest.jsonl"
    pages = [_page("b", 4000, 5500), _page("a", 1800, 2400), _page("c", 2550, 3300, 0)]
    manifest.write_text("".join(json.dumps(page) + "\n" for page in pages), encoding="utf-8")
    first, second = tmp_path / "one.json", tmp_path / "two.json"
    assert main([str(manifest), "--out", str(first)]) == 0
    printed = capsys.readouterr().out
    assert main([str(manifest), "--out", str(second)]) == 0
    assert first.read_bytes() == second.read_bytes()
    report = json.loads(first.read_bytes())
    assert first.read_text("utf-8") == json.dumps(report, sort_keys=True, indent=2) + "\n"
    assert [entry["page_id"] for entry in report["pages"]] == ["a", "b", "c"]
    summary = report["summary"]
    assert summary["pages"] == 3 and summary["qualifying"] == 1
    assert summary["qualifying_bp"] == 3333
    assert sum(summary["k_histogram"].values()) == 3
    assert report["inputs"]["max_model_len"] == 65536
    assert report["inputs"]["maximum_edge"] == 2560
    assert "qualifying (gain >= 1.50, k >= 1): 1 (33.33%)" in printed
    assert "k histogram: 0: 0" in printed


@pytest.mark.parametrize(
    "arguments",
    [["--crop-width", "0"], ["--crop-height", "1.5"], ["--min-gain", "0.5"], ["--max-crops", "0"]],
)
def test_out_of_range_inputs_are_refused(tmp_path, arguments):
    manifest = tmp_path / "page_manifest.jsonl"
    manifest.write_text("", encoding="utf-8")
    with pytest.raises(SystemExit) as stopped:
        main([str(manifest), "--out", str(tmp_path / "out.json"), *arguments])
    assert stopped.value.code == 2
