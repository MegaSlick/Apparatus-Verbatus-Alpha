"""The request-fit measurement, proven on synthetic pages only."""

from operations.corpus.perlector_request_fit import _clue, fit_table, gold_shapes, padded_crop

PADDING = {"top_bp": 1000, "bottom_bp": 1000, "left_bp": 1000, "right_bp": 1000}


def test_a_crop_is_padded_by_its_own_sides_and_clamped_to_the_page():
    assert padded_crop((0, 100, 100, 100), (1000, 1000), PADDING) == (0, 90, 110, 120)


def test_every_act_is_counted_under_each_setting_with_its_neighbours():
    pages = [
        {
            "width": 2550,
            "height": 3300,
            "records": [
                {"bbox": [100, 100, 2000, 600], "text": "baptême " * 40},
                {"bbox": [100, 800, 2000, 600], "text": "   "},
                {"bbox": [100, 1500, 2000, 600], "text": "sépulture " * 40},
            ],
        }
    ]
    acts = gold_shapes(pages, PADDING)
    assert [act["before"] is None for act in acts] == [True, False]
    assert _clue(acts[1], acts[1]["before"])[1] is True
    table = fit_table(acts)
    assert table["sealed rule, neighbours"]["fit"] == 2
    assert table["newly refused by the page render"] == {"count": 0}


def test_a_neighbour_on_another_page_is_not_the_same_page():
    pages = [
        {"width": 2550, "height": 3300, "records": [{"bbox": [1, 1, 500, 200], "text": "a"}]},
        {"width": 2550, "height": 3300, "records": [{"bbox": [1, 1, 500, 200], "text": "b"}]},
    ]
    first, second = gold_shapes(pages, PADDING)
    assert _clue(second, second["before"])[1] is False
    assert _clue(first, first["after"])[1] is False
