"""The floor calibration script, proven on synthetic pages only."""

from operations.corpus.length_floor_calibration import densities, padded_area, percentile, report

PADDING = {"top_bp": 1000, "bottom_bp": 1000, "left_bp": 1000, "right_bp": 1000}


def test_padding_grows_the_box_by_a_fraction_of_its_own_sides():
    assert padded_area((100, 100, 100, 100), (1000, 1000), PADDING, clamp=False) == 120 * 120


def test_the_page_edge_clamp_shrinks_a_box_padded_past_the_page():
    unclamped = padded_area((0, 0, 100, 100), (1000, 1000), PADDING, clamp=False)
    assert padded_area((0, 0, 100, 100), (1000, 1000), PADDING, clamp=True) == 110 * 110
    assert unclamped == 120 * 120


def test_density_is_characters_scaled_from_the_region_to_the_page():
    pages = [
        {"width": 1000, "height": 1000, "records": [{"bbox": [0, 0, 100, 100], "text": "a" * 5}]}
    ]
    assert densities(pages, PADDING)["bare"] == [5 * 1_000_000 / 10_000]


def test_an_empty_act_is_skipped_and_the_report_counts_flags():
    pages = [
        {
            "width": 1000,
            "height": 1000,
            "records": [
                {"bbox": [0, 0, 100, 100], "text": "  "},
                {"bbox": [0, 0, 100, 100], "text": "a"},
            ],
        }
    ]
    found = densities(pages, PADDING)
    assert len(found["bare"]) == 1
    assert "floor 400: flags 1 (100.00%)" in report(found, (400,))
    assert percentile([1.0, 3.0], 50) == 2.0
