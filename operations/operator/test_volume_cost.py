"""F063: the volume's ongoing price is never a live provider quote."""

from __future__ import annotations

from .volume_cost import volume_cost_lines


def test_the_ongoing_price_line_discloses_it_is_supplied_not_observed():
    lines = volume_cost_lines(volume_id="vol123", hourly_usd="0.07")

    price_line = next(line for line in lines if "recorded ongoing price" in line)
    assert "$0.07 per hour" in price_line
    assert "supplied at launch" in price_line
    assert "no live quote" in price_line


def test_a_missing_volume_id_still_names_the_retained_volume_generically():
    lines = volume_cost_lines(volume_id=None, hourly_usd="0.07")

    assert lines[0].startswith("Closing the pod does not delete the retained volume,")
