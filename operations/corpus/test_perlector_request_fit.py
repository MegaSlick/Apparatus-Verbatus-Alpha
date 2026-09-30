"""The request-fit measurement, proven on synthetic pages only."""

from operations.corpus.perlector_request_fit import (
    _clue,
    fit_table,
    gold_shapes,
    padded_crop,
    page_fit_table,
    page_shape,
)

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


def test_every_page_is_counted_under_each_feed_setting_and_context():
    pages = [
        {
            "width": 2550,
            "height": 3300,
            "records": [
                {"bbox": [100, 100 + 150 * index, 2000, 140], "text": "baptême de Pierre\n" * 6}
                for index in range(20)
            ]
            + [
                {"bbox": [100, 3200, 2000, 400], "text": "hors page\n"},
                {"bbox": [0, 0, 0, 5], "text": "x"},
            ],
        },
        {"width": 2550, "height": 3300, "records": []},
    ]
    table = page_fit_table(pages)
    assert len(table) == 6
    for cell in table.values():
        assert cell["fit"] + cell["refused_context"] + cell["refused_answer_cap"] == 2
    assert table["page request, default feed, 32768"]["fit"] == 2
    # Two witnesses in record units and one in lines; one Surya line per text line.
    shape = page_shape(pages[0])
    assert [len(units) for _chair, _adapter, units in shape["witnesses"]] == [21, 21, 121]
    assert len(shape["surya"]["lines"]) == 121 and len(shape["surya"]["blocks"]) == 21
