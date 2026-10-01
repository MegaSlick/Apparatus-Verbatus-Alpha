"""The request-fit measurement, proven on synthetic pages only."""

import pytest

from common.request_capacity import RequestCapacityRefusal
from operations.corpus import perlector_request_fit
from operations.corpus.perlector_request_fit import (
    page_fit_table,
    page_shape,
    skipped_gold_records,
)


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
    assert len(table) == 8
    assert skipped_gold_records(pages) == 1
    for cell in table.values():
        assert cell["fit"] + cell["refused_context"] == 2
        assert cell["reserve_clamped"] == 0
    assert table["page request, default feed, 32768"]["fit"] == 2
    # Two witnesses in record units and one in lines; one Surya line per text line.
    shape = page_shape(pages[0])
    assert [len(units) for _chair, _adapter, units in shape["witnesses"]] == [21, 21, 121]
    assert len(shape["surya"]["lines"]) == 121 and len(shape["surya"]["blocks"]) == 21


def test_a_refusal_with_no_capacity_record_is_not_counted_as_a_context_refusal(monkeypatch):
    """Only a refusal carrying its arithmetic is a context refusal; any other stops the table."""

    def refuse(_row, _feed):
        raise RequestCapacityRefusal("the prompt is malformed")

    monkeypatch.setattr(perlector_request_fit, "page_request", refuse)
    page = {"width": 2550, "height": 3300, "records": [{"bbox": [1, 1, 500, 200], "text": "a"}]}
    with pytest.raises(RequestCapacityRefusal, match="malformed"):
        page_fit_table([page])
