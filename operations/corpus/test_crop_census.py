"""The crop census, proven on synthetic pages only."""

import json
from dataclasses import replace
from io import BytesIO

import pytest
from PIL import Image

from common import page_render
from common.contracts.errors import SchemaRefusal
from common.request_capacity import (
    CHAT_IMAGE_TOKENS,
    CHAT_TURN_TOKENS,
    RequestCapacityRefusal,
    request_fits,
    smart_resize,
)
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
    page_max_tokens,
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


RENDERS = {
    (900, 700): (900, 700),
    (2560, 1000): (2560, 1000),
    (2561, 1000): (2560, 1000),
    (1000, 2561): (1000, 2560),
    (4000, 5500): (1862, 2560),
    (6001, 3): (2560, 1),
}


@pytest.mark.parametrize("size", RENDERS)
def test_the_render_size_rule_is_the_size_the_renderer_produces(size):
    assert page_render.render_size(size, 2560) == RENDERS[size]
    image = BytesIO()
    Image.new("L", size, 255).save(image, format="PNG")
    rendered, transform = page_render._downscale_page(image.getvalue(), maximum_edge=2560)
    assert transform["target_dimensions"] == {"w": RENDERS[size][0], "h": RENDERS[size][1]}
    with Image.open(BytesIO(rendered)) as shown:
        assert shown.size == RENDERS[size]


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
    assert entry["page_gain_bp"] == gain_bp((4000, 5500), (width, height)) == 21518
    assert entry["need"] == admitted["need"] and entry["headroom"] == admitted["headroom"]
    # The processor shrinks the 2000x687 crop to 1984x672, so the crop gains a little less.
    assert entry["crop_native"] == [2000, 687] and entry["crop_seen"] == [1984, 672]
    assert entry["gain_bp"] == 21196
    assert entry["k"] == 8 and entry["k_capped"] is True
    assert entry["qualifies"] is True


def test_a_crop_the_processor_shrinks_gains_only_what_the_chair_sees():
    row = perlector_row()
    whole = census_page(
        row,
        sealed_protocol(),
        _page("huge", 8000, 10000),
        {**OPTIONS, "crop_width_bp": 10000, "crop_height_bp": 10000},
    )
    assert whole["page_gain_bp"] == 39062
    # A whole-page crop is shrunk to the same size as the render, so it gains nothing.
    assert whole["crop_seen"] == whole["seen"] == [2048, 2560]
    assert whole["gain_bp"] == 10000 and whole["qualifies"] is False


def test_a_page_below_the_processor_minimum_is_enlarged_and_gains_nothing():
    entry = census_page(perlector_row(), sealed_protocol(), _page("tiny", 200, 250, 0), OPTIONS)
    assert entry["seen"][0] * entry["seen"][1] >= 65536
    # Enlargement adds no detail, so neither the page nor its crop gains anything.
    assert entry["page_gain_bp"] == 10000 and entry["gain_bp"] == 10000
    assert entry["qualifies"] is False


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
    # Round two carries the longest round-one reply the engine allows (the page cap,
    # since the context leaves more) and two turns; then room for exactly two crops.
    per_crop = request_fits(row, [crop], 0, 0)["need"] + CHAT_IMAGE_TOKENS
    base = capacity["need"] + page_max_tokens() + 2 * CHAT_TURN_TOKENS
    tight = replace(row, max_model_len=base + 2 * per_crop + per_crop - 1)
    tight_request = page_request(tight, feed)
    assert tight_request["max_tokens"] == page_max_tokens() > capacity["answer_budget"]
    assert admitted_crops(tight, tight_request, crop, 8) == 2
    assert admitted_crops(tight, tight_request, crop, 1) == 1
    roomy = replace(row, max_model_len=base + 3 * per_crop)
    assert admitted_crops(roomy, page_request(roomy, feed), crop, 8) == 3


def test_round_two_carries_the_round_one_max_tokens_not_the_estimated_reserve():
    feed = page_feed_for(sealed_protocol(), page_shape(_page("big", 4000, 5500)), {})
    row = perlector_row()
    request = page_request(row, feed)
    crop = crop_size((4000, 5500), 5000, 2500)
    per_crop = request_fits(row, [crop], 0, 0)["need"] + CHAT_IMAGE_TOKENS
    # Room for one crop after a reply carried at the reserve, none after one at max_tokens.
    tight = replace(
        row,
        max_model_len=request["capacity"]["need"]
        + request["capacity"]["answer_budget"]
        + 2 * CHAT_TURN_TOKENS
        + per_crop,
    )
    tight_request = page_request(tight, feed)
    assert tight_request["max_tokens"] > tight_request["capacity"]["answer_budget"]
    assert admitted_crops(tight, tight_request, crop, 8) == 0


def test_a_page_whose_request_is_refused_is_counted_with_no_crops(monkeypatch):
    row = perlector_row()
    monkeypatch.setattr(crop_census, "perlector_row", lambda: replace(row, max_model_len=8000))
    report = census([_page("big", 4000, 5500)], OPTIONS, "0" * 64)
    (entry,) = report["pages"]
    assert entry["page_request"] == "refused" and entry["headroom"] < 0
    assert entry["k"] == 0 and entry["k_capped"] is False and entry["qualifies"] is False
    assert report["summary"]["page_request_refused"] == 1
    assert report["summary"]["k_histogram"][0] == {"k": 0, "pages": 1}


def test_quantiles_are_nearest_rank():
    assert quantiles([]) == [{"q": q, "value": None} for q in crop_census.QUANTILES_BP]
    values = list(range(10, 110, 10))
    assert [cell["value"] for cell in quantiles(values)] == [10, 10, 30, 50, 80, 90, 100]
    assert [cell["q"] for cell in quantiles(values)] == list(crop_census.QUANTILES_BP)


def test_the_report_is_deterministic_and_summarised(tmp_path, capsys):
    manifest = tmp_path / "page_manifest.jsonl"
    pages = [_page("pg-b", 4000, 5500), _page("pg-a", 1800, 2400), _page("pg-c", 2550, 3300, 0)]
    manifest.write_text("".join(json.dumps(page) + "\n" for page in pages), encoding="utf-8")
    first, second = tmp_path / "one.json", tmp_path / "two.json"
    assert main([str(manifest), "--out", str(first)]) == 0
    printed = capsys.readouterr().out
    assert main([str(manifest), "--out", str(second)]) == 0
    assert first.read_bytes() == second.read_bytes()
    report = json.loads(first.read_bytes())
    assert (
        first.read_text("utf-8")
        == json.dumps(report, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    )
    assert [entry["page_id"] for entry in report["pages"]] == ["pg-a", "pg-b", "pg-c"]
    summary = report["summary"]
    assert summary["pages"] == 3 and summary["qualifying"] == 1
    assert summary["qualifying_bp"] == 3333
    assert [cell["k"] for cell in summary["k_histogram"]] == list(range(9))
    assert sum(cell["pages"] for cell in summary["k_histogram"]) == 3
    inputs = report["inputs"]
    assert inputs["max_model_len"] == 65536 and inputs["maximum_edge"] == 2560
    assert set(inputs["config_sha256"]) == set(crop_census.CONFIGS)
    assert "qualifying (gain >= 1.50, k >= 1): 1 (33.33%)" in printed
    assert "k histogram: 0: 0" in printed
    assert crop_census.ESTIMATE in printed
    # The printed summary carries counts only: no page id, no gold text.
    assert "baptême" not in printed
    assert "pg-" not in printed


def test_an_empty_manifest_reports_no_pages(tmp_path, capsys):
    manifest = tmp_path / "page_manifest.jsonl"
    manifest.write_text("", encoding="utf-8")
    assert main([str(manifest), "--out", str(tmp_path / "out.json")]) == 0
    report = json.loads((tmp_path / "out.json").read_bytes())
    assert report["pages"] == [] and report["summary"]["qualifying_bp"] == 0
    assert {cell["value"] for cell in report["summary"]["gain_bp_quantiles"]} == {None}
    assert "0 pages" in capsys.readouterr().out


@pytest.mark.parametrize(
    "arguments",
    [
        ["--crop-width", "0"],
        ["--crop-width", "0.00004"],
        ["--crop-height", "1.5"],
        ["--min-gain", "0.5"],
        ["--min-gain", "inf"],
        ["--max-crops", "0"],
    ],
)
def test_out_of_range_inputs_are_refused(tmp_path, arguments):
    manifest = tmp_path / "page_manifest.jsonl"
    manifest.write_text("", encoding="utf-8")
    with pytest.raises(SystemExit) as stopped:
        main([str(manifest), "--out", str(tmp_path / "out.json"), *arguments])
    assert stopped.value.code == 2


def _refused(tmp_path, capsys, pages, *arguments) -> str:
    manifest = tmp_path / "page_manifest.jsonl"
    manifest.write_text("".join(json.dumps(page) + "\n" for page in pages), encoding="utf-8")
    assert main([str(manifest), "--out", str(tmp_path / "out.json"), *arguments]) == 2
    assert not (tmp_path / "out.json").exists()
    return capsys.readouterr().err


@pytest.mark.parametrize(
    ("pages", "message"),
    [
        ([{**_page("a", 900, 700), "page_id": None}], "no page_id"),
        ([_page("a", 900, 700), _page("a", 1800, 2400)], "appears twice"),
        ([{**_page("a", 900, 700), "width": "900"}], "positive integer width"),
        ([[1, 2]], "not an object"),
    ],
)
def test_a_manifest_line_the_census_cannot_count_is_refused(tmp_path, capsys, pages, message):
    assert message in _refused(tmp_path, capsys, pages)


def _record_with(**fields) -> dict:
    page = _page("a", 900, 700, 1)
    page["records"][0].update(fields)
    return page


@pytest.mark.parametrize(
    "page",
    [
        _record_with(bbox=[1, 2, 3]),
        _record_with(bbox=[1, 2, 3, "4"]),
        _record_with(bbox=[1, 2, 3, True]),
        _record_with(text=None),
        {**_page("a", 900, 700), "records": ["r0"]},
    ],
)
def test_a_record_the_census_cannot_count_is_refused(tmp_path, capsys, page):
    assert "four-integer bbox and a text" in _refused(tmp_path, capsys, [page])


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"{\n", "manifest line 1 is not JSON"),
        (b"\n" + json.dumps(_page("a", 900, 700)).encode() + b"\n{\n", "line 3 is not JSON"),
        (b"\xff\n", "not UTF-8"),
    ],
)
def test_a_manifest_that_is_not_utf8_json_lines_is_refused(tmp_path, capsys, body, message):
    manifest = tmp_path / "page_manifest.jsonl"
    manifest.write_bytes(body)
    assert main([str(manifest), "--out", str(tmp_path / "out.json")]) == 2
    assert not (tmp_path / "out.json").exists()
    assert message in capsys.readouterr().err


def test_a_capacity_refusal_without_a_record_refuses_the_census(tmp_path, capsys, monkeypatch):
    def refuse(_row, _feed):
        raise RequestCapacityRefusal("no record")

    monkeypatch.setattr(crop_census, "page_request", refuse)
    assert "no record" in _refused(tmp_path, capsys, [_page("a", 4000, 5500)])


def test_a_crop_the_processor_refuses_is_a_clean_refusal(tmp_path, capsys):
    assert "aspect ratio" in _refused(
        tmp_path, capsys, [_page("a", 4000, 5500)], "--crop-height", "0.0002"
    )


def test_only_the_legible_page_render_is_counted():
    sealed = sealed_protocol()
    sealed = {**sealed, "feed": {**sealed["feed"], "page_image": "full"}}
    with pytest.raises(SchemaRefusal, match="legible"):
        census_page(perlector_row(), sealed, _page("a", 4000, 5500), OPTIONS)


def test_a_render_rule_that_drifts_from_the_page_request_is_refused(monkeypatch):
    monkeypatch.setattr(crop_census, "render_size", lambda size, edge: (size[0] - 1, size[1]))
    with pytest.raises(SchemaRefusal, match="page render"):
        census_page(perlector_row(), sealed_protocol(), _page("a", 4000, 5500), OPTIONS)


def test_k_never_passes_the_census_s_own_crop_limit():
    """The protocol seals no image ceiling for a page request; `max_crops` bounds k."""
    entry = census_page(
        perlector_row(), sealed_protocol(), _page("big", 4000, 5500), {**OPTIONS, "max_crops": 2}
    )
    assert entry["k_cap"] == 2 and entry["k"] == 2 and entry["k_capped"] is True


def test_a_crop_gain_needs_both_sizes():
    with pytest.raises(ValueError):
        gain_bp((4000, 5500), (1856, 2560), (2000, 687))
